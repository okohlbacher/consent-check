"""Deterministic checker for MII KDS Consent (mii-pr-consent-einwilligung).

Rules come from the profile differential in package
de.medizininformatikinitiative.kerndatensatz.consent#2025.0.1 plus the IG text
(policy-OID table, 30-year rule, per-policy validity, nested-provision rules).

    consent-check FILE_OR_DIR...            # issue list (or: python3 -m consent_check ...)
    consent-check --json FILE_OR_DIR...     # JSON issue report
    consent-check --install-validator       # download the HL7 validator

Each finding: (severity, category, rule, detail); categories are listed in CATEGORIES.
"""
import argparse, base64, collections, glob, json, os, re, shutil, subprocess, sys, tempfile, urllib.request
from datetime import date

# Policy CodeSystem + IG examples from the MII package (CC BY 4.0, see data/mii-consent-2025.0.1/NOTICE.md);
# override with CONSENT_PKG=<path to an unpacked package/ directory>.
PKG = os.environ.get("CONSENT_PKG", os.path.join(os.path.dirname(os.path.realpath(__file__)), "data", "mii-consent-2025.0.1"))
PROFILE = "https://www.medizininformatik-initiative.de/fhir/modul-consent/StructureDefinition/mii-pr-consent-einwilligung"
POLICY_CS = "urn:oid:2.16.840.1.113883.3.1937.777.24.5.3"
P = "2.16.840.1.113883.3.1937.777.24.5.3."
CAT_CS = "https://www.medizininformatik-initiative.de/fhir/modul-consent/CodeSystem/mii-cs-consent-consent_category"
CAT_CS_2027 = "https://www.medizininformatik-initiative.de/fhir/modul-consent/CodeSystem/mii-cs-consent-version-modules"
CAT_CODE = "2.16.840.1.113883.3.1937.777.24.2.184"

CATEGORIES = {
    "structure": "not a well-formed FHIR R4 Consent (resourceType, unknown elements, null/empty values, bad dateTime syntax, forbidden nesting)",
    "required": "a mandatory element of the MII profile is missing or wrong (scope, category slices, patient, dateTime, policy, provision periods, provision codes)",
    "policy_uri": "Consent.policy.uri is not a valid MII Broad Consent document OID of the form urn:oid:2.16.840.1.113883.3.1937.777.24.2.NNNN",
    "terminology": "provision codes not from the MII policy CodeSystem urn:oid:2.16.840.1.113883.3.1937.777.24.5.3, wrong category system, or grouping/deprecated codes",
    "periods": "validity periods deviate from the IG (overall 30 years; per-policy 5/30 years; nested period outside overall period)",
    "contradiction": "logically contradictory provisions (same policy permitted and denied, or a policy permitted while its prerequisite is denied)",
    "consistency": "internally or cross-export inconsistent (XACML vs policy.uri, backdated or late-signed consent, exports of one case disagree)",
}

# IG table "Eindeutige Identifikation des MII-Broad Consent" (2026.0.0 / 2027 ballot)
DOC_OIDS = {
    "1790": "1.6d", "4053": "1.6d Ablehnung", "2718": "1.6d Komplettwiderruf", "2719": "1.6d Teilwiderruf",
    "1791": "1.6f", "2720": "1.6f Komplettwiderruf", "2721": "1.6f Teilwiderruf",
    "2079": "1.7.2", "4054": "1.7.2 Ablehnung", "2722": "1.7.2 Komplettwiderruf", "2723": "1.7.2 Teilwiderruf",
    "3542": "1.7.2 Eltern/Sorgeberechtigte Minderjährige", "3543": "1.7.2 Minderjährige 7-11",
    "3544": "1.7.2 Minderjährige 12-17", "4031": "Zusatzmodul ACRIBiS", "4036": "Zusatzmodul PROM",
    "4037": "Zusatzmodul SNID", "4048": "Zusatzmodul DZPG",
}
MINOR_DOCS = {"3542", "3543", "3544"}  # consent ends at majority, not after 30 years
# IG code-systems page, column "Gültigkeit" (years; 0 = einmalig/one-off). Level-0 grouping codes have none.
VALIDITY = {**{c: 30 for c in (2, 3, 4, 5, 7, 8, 9, 37, 45, 46, 47, 49, 12, 13, 16, 17, 20, 22, 23, 51, 52, 53, 55,
                               27, 28, 29, 31, 43, 34, 56, 36, 65, 41, 42)},
            **{c: 5 for c in (6, 15, 39, 19, 21, 25, 58, 60, 62, 64)}, **{c: 0 for c in (11, 38, 40, 33)}}
DEPRECATED = {16, 17, 46, 47, 41, 42}  # 16/17/46/47 deprecated in 2026.0.0, 41/42 already in 2025.0.1
# prerequisite -> dependants: permitting a dependant while the prerequisite is DENIED is contradictory
REQUIRES = {3: (4, 5), 7: (8, 9, 49), 11: (12, 13, 38), 12: (13,), 15: (16, 17, 39), 20: (21, 22, 23, 55),
            45: (46, 47), 51: (52, 53)}

R4_CONSENT = {"resourceType", "id", "meta", "implicitRules", "language", "text", "contained", "extension",
              "modifierExtension", "identifier", "status", "scope", "category", "patient", "dateTime", "performer",
              "organization", "sourceAttachment", "sourceReference", "policy", "policyRule", "verification", "provision"}
R4_PROV = {"id", "extension", "modifierExtension", "type", "period", "actor", "action", "securityLabel", "purpose",
           "class", "code", "dataPeriod", "data", "provision"}
STATUS = {"draft", "proposed", "active", "rejected", "inactive", "entered-in-error"}
DT = re.compile(r"^\d{4}(-\d{2}(-\d{2}(T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2}))?)?)?$")


def _codes():
    out = {}
    def walk(cs, parent=None):
        for c in cs:
            out[c["code"]] = (c.get("display"), parent)
            walk(c.get("concept", []), c["code"])
    walk(json.load(open(f"{PKG}/CodeSystem-MiiConsentPolicyCodeSystem.json", encoding="utf-8"))["concept"])
    return out
CODES = _codes()


def ymd(s):
    try:
        return date(*map(int, s[:10].split("-")))
    except Exception:
        return None


def years(p):
    a, b = ymd(p.get("start") or ""), ymd(p.get("end") or "")
    return round((b - a).days / 365.25, 1) if a and b else None


def overlap(p, q):
    a1, b1, a2, b2 = (ymd((x or {}).get(k) or "") for x, k in ((p, "start"), (p, "end"), (q, "start"), (q, "end")))
    return not (a1 and b2 and a1 > b2) and not (a2 and b1 and a2 > b1)


def empties(x, path="Consent"):
    if x is None or x == "" or x == {} or x == []:
        yield path
    elif isinstance(x, dict):
        for k, v in x.items():
            yield from empties(v, f"{path}.{k}")
    elif isinstance(x, list):
        for i, v in enumerate(x):
            yield from empties(v, f"{path}[{i}]")


def unwrap_b64(s, max_layers=5):
    """Decode base64 repeatedly while the result is again base64 text -> (text, layers); layers 0 = not base64."""
    txt, layers = s, 0
    while layers < max_layers:
        try:
            dec = base64.b64decode(txt, validate=True).decode("utf-8")
        except Exception:
            break
        txt, layers = dec, layers + 1
        if not re.fullmatch(r"[A-Za-z0-9+/]+=*", txt.strip()) or len(txt.strip()) % 4:
            break
    return txt, layers


def check(r):
    out = []
    add = lambda sev, cat, rule, det="": out.append((sev, cat, rule, str(det)))

    # --- structure
    if r.get("resourceType") != "Consent":
        add("error", "structure", "resourceType missing/wrong", repr(r.get("resourceType")))
    for k in r:
        if k not in R4_CONSENT:
            add("error", "structure", "element not defined in FHIR R4 Consent", k)
    for p in empties(r):
        add("error", "structure", "null/empty value (forbidden in FHIR JSON)", p)
    prof = (r.get("meta") or {}).get("profile") or []
    if not prof:
        add("info", "required", "meta.profile not declared")
    for p in prof:
        base, _, ver = p.partition("|")
        if base != PROFILE:
            add("error", "required", "wrong meta.profile", p)
        elif ver and ver not in ("1.0.8", "1.0.9"):
            add("warning", "required", "meta.profile version is not a profile version", ver)
    if r.get("status") not in STATUS:
        add("error", "required", "status missing/invalid", repr(r.get("status")))

    # --- scope / category
    sc = (r.get("scope") or {}).get("coding") or []
    if len(sc) != 1 or sc[0].get("system") != "http://terminology.hl7.org/CodeSystem/consentscope" or sc[0].get("code") != "research":
        add("error", "required", "scope must be exactly consentscope#research", json.dumps(r.get("scope"))[:100])
    cats = r.get("category") or []
    loinc = [c for c in cats if any(x.get("system") == "http://loinc.org" and x.get("code") == "57016-8" for x in c.get("coding") or [])]
    mii = [c for c in cats if any(x.get("code") == CAT_CODE for x in c.get("coding") or [])]
    if len(cats) < 2:
        add("error", "required", "category needs ≥2 entries", len(cats))
    if len(loinc) != 1:
        add("error", "required", "category:loinc 57016-8 must occur once", len(loinc))
    if len(mii) != 1:
        add("error", "required", "category:mii …24.2.184 must occur once", len(mii))
    for c in mii:
        for x in c["coding"]:
            if x.get("code") == CAT_CODE and x.get("system") == CAT_CS_2027:
                add("error", "terminology", "category:mii uses 2027-ballot system mii-cs-consent-version-modules (2025.0.1 requires mii-cs-consent-consent_category)")
            elif x.get("code") == CAT_CODE and x.get("system") != CAT_CS:
                add("error", "terminology", "category:mii wrong system", x.get("system"))

    # --- patient / dateTime / source
    pat = r.get("patient")
    if not pat:
        add("error", "required", "patient missing (profile min 1, invariant ppc-3)")
    else:
        pid = pat.get("identifier")
        if not pat.get("reference") and not pid:
            add("error", "required", "patient has neither reference nor identifier")
        if pid and not (pid.get("system") and pid.get("value")):
            add("error", "required", "patient.identifier needs system and value")
    dt = r.get("dateTime")
    if not dt:
        add("error", "required", "dateTime missing")
    elif not DT.match(dt):
        add("error", "structure", "dateTime not valid FHIR (time needs seconds and timezone)", dt)
    sr = r.get("sourceReference")
    if sr is not None and not (sr or {}).get("reference"):
        add("error", "required", "sourceReference without reference (min 1)", json.dumps(sr))

    # --- policy
    pol = r.get("policy") or []
    if not pol:
        add("error", "required", "policy missing (min 1)")
    uris = [p.get("uri") for p in pol]
    doc_oids = set()
    for u in uris:
        if not u:
            add("error", "required", "policy.uri missing")
            continue
        oid = u[8:] if u.startswith("urn:oid:") else u
        if not u.startswith("urn:oid:") and re.fullmatch(r"\d+(\.\d+)+", u):
            add("error", "policy_uri", "policy.uri is a bare OID (needs urn:oid: prefix)", u)
        if oid == CAT_CODE:
            add("error", "policy_uri", "policy.uri is the category code …24.2.184, not a document OID", u)
        elif oid.startswith("2.16.840.1.113883.3.1937.777.24.2."):
            n = oid.rsplit(".", 1)[1]
            if n in DOC_OIDS:
                doc_oids.add(n)
            else:
                add("warning", "policy_uri", "unknown MII document OID", u)
        else:
            add("warning", "policy_uri", "policy.uri is not an MII document OID", u)
    if len(uris) != len(set(uris)):
        add("warning", "policy_uri", "duplicate policy.uri")
    minor = bool(doc_oids & MINOR_DOCS)

    # --- policyRule / extensions
    doc_versions = {DOC_OIDS[n].split()[0] for n in doc_oids}
    for e in ((r.get("policyRule") or {}).get("extension") or []):
        if e.get("url", "").endswith("/Xacml"):
            txt, layers = unwrap_b64(e.get("valueBase64Binary", ""))
            if layers == 0:
                add("error", "structure", "Xacml valueBase64Binary is not base64", e.get("valueBase64Binary", "")[:40])
                continue
            if layers > 1:
                add("warning", "consistency", f"Xacml content is base64-encoded {layers} times", txt[:40])
            if not txt.lstrip().startswith("<"):
                add("info", "required", "Xacml extension holds placeholder text, not XACML", txt[:40])
            m = re.search(r"\|(\d+(?:\.\d+)+)", txt)
            if m and doc_versions and m[1] not in doc_versions:
                add("warning", "consistency", "Xacml names a different Broad Consent version than policy.uri",
                    f"Xacml '{txt[:40]}' vs policy.uri version {'/'.join(sorted(doc_versions))}")
    for e in r.get("extension") or []:
        if e.get("url") == "http://fhir.de/ConsentManagement/StructureDefinition/DomainReference":
            sub = {x.get("url"): x for x in e.get("extension") or []}
            if "domain" not in sub:
                add("error", "required", "DomainReference without domain")
            if "status" in sub and not (sub["status"].get("valueCoding") or {}).get("code"):
                add("error", "required", "DomainReference.status coding without code")

    # --- top-level provision
    prov = r.get("provision")
    if not isinstance(prov, dict) or not prov:
        add("error", "required", "provision missing")
        return out
    for k in prov:
        if k not in R4_PROV:
            add("error", "structure", "element not defined in Consent.provision", k)
    if prov.get("type") not in ("deny", "permit"):
        add("error", "required", "provision.type missing/invalid", repr(prov.get("type")))
    elif prov["type"] != "deny":
        add("warning", "contradiction", "top-level provision is permit (IG opt-in pattern: deny + nested permits)")
    per = prov.get("period") or {}
    if not (per.get("start") and per.get("end")):
        add("error", "required", "provision.period start and end required")
    for v in per.values():
        if isinstance(v, str) and not DT.match(v):
            add("error", "structure", "provision.period not valid FHIR dateTime", v)
    for k in ("code", "action"):
        if prov.get(k) is not None:
            add("error", "structure", f"provision.{k} not allowed (max 0)")
    y = years(per)
    if y is not None and abs(y - 30) > 0.1:
        if minor and y < 30:
            add("info", "periods", "minor's consent: overall period ends before 30 years (expected: ends at majority)", f"{y} y")
        else:
            add("warning", "periods", "overall provision.period is not 30 years", f"{y} y ({per.get('start', '')[:10]}..{per.get('end', '')[:10]})")
    if minor and y is not None and abs(y - 30) <= 0.1:
        add("info", "periods", "minor's consent (…24.2.3542-3544) runs 30 years; IG: must end at majority")
    sig, start = ymd(dt or ""), ymd(per.get("start") or "")
    if sig and start:
        gap = (sig - start).days
        if gap > 365:
            add("warning", "consistency", "overall period backdated: starts >1 year before signature "
                "(retrospective use belongs in retrospective policy codes / dataPeriod)", f"start {start} vs signed {sig}")
        elif gap > 1:
            add("warning", "consistency", "consent valid before it was signed (period.start before dateTime)",
                f"start {start} vs signed {sig} ({gap} days)")
        elif gap < -1:
            add("info", "periods", "provision.period.start after signature dateTime", f"{start} vs {sig}")

    # --- nested provisions
    subs = prov.get("provision") or []
    if not subs:
        add("error", "required", "no nested provisions")
    by_code, validity_off = {}, []
    for i, s in enumerate(subs):
        tag = f"provision[{i}]"
        for k in s:
            if k not in R4_PROV:
                add("error", "structure", "element not defined in Consent.provision", f"{tag}.{k}")
        if s.get("type") not in ("deny", "permit"):
            add("error", "required", "nested provision.type missing/invalid", tag)
        sp = s.get("period") or {}
        if not (sp.get("start") and sp.get("end")):
            add("error", "required", "nested provision.period start and end required", tag)
        for v in sp.values():
            if isinstance(v, str) and not DT.match(v):
                add("error", "structure", "nested provision.period not valid FHIR dateTime", f"{tag}: {v}")
        a, b = ymd(sp.get("start") or ""), ymd(sp.get("end") or "")
        if a and b and b < a:
            add("error", "periods", "nested period ends before it starts", tag)
        if b and ymd(per.get("end") or "") and b > ymd(per["end"]):
            add("warning", "periods", "nested period ends after overall period", f"{tag}: {sp['end'][:10]} > {per['end'][:10]}")
        if a and ymd(per.get("start") or "") and a < ymd(per["start"]):
            add("warning", "periods", "nested period starts before overall period", tag)
        for k in ("action", "provision"):
            if s.get(k) is not None:
                add("error", "structure", f"nested provision.{k} not allowed (max 0)", tag)
        if not s.get("code"):
            add("error", "required", "nested provision.code missing (min 1)", tag)
        sy = years(sp)
        for c in s.get("code") or []:
            if not c.get("coding"):
                add("error", "terminology", "provision.code has text only, no coding", f"{tag}: {c.get('text')}")
            for x in c.get("coding") or []:
                code = x.get("code") or ""
                if x.get("system") != POLICY_CS:
                    add("error", "terminology", "provision code system is not urn:oid:…24.5.3 (required binding)", f"{tag}: {x.get('system')}")
                if code not in CODES:
                    add("error", "terminology", "provision code not in MII policy CodeSystem", f"{tag}: {code}")
                    continue
                n = int(code.rsplit(".", 1)[1])
                if CODES[code][1] is None:
                    add("warning", "terminology", "level-0 grouping code used as policy (has no validity; use level-1 codes)", f"{tag}: .{n}")
                if n in DEPRECATED:
                    add("info", "terminology", "policy code deprecated (2026.0.0)", f".{n}")
                v = VALIDITY.get(n)
                if s.get("type") == "permit" and v in (5, 30) and sy is not None and abs(sy - v) > 0.1 and not (minor and sy < v):
                    validity_off.append(f".{n}:{sy}y(IG {v}y)")
                by_code.setdefault(n, []).append((s.get("type"), sp, tag))

    if validity_off:
        add("warning", "periods", f"{len(validity_off)} nested provisions deviate from per-policy validity (IG: 5 or 30 y)", " ".join(validity_off[:6]) + (" …" if len(validity_off) > 6 else ""))

    # --- contradictions
    for n, lst in by_code.items():
        kinds = {t for t, _, _ in lst}
        if len(lst) > 1:
            add("warning", "contradiction" if kinds == {"permit", "deny"} else "required",
                "policy code appears in several provisions", f".{n}: {[t for _, _, t in lst]}")
        for t1, p1, g1 in lst:
            for t2, p2, g2 in lst:
                if (t1, t2) == ("permit", "deny") and overlap(p1, p2):
                    add("error", "contradiction", "same policy both permitted and denied in overlapping periods", f".{n}: {g1} permit vs {g2} deny")
    for code, (_, parent) in CODES.items():
        n = int(code.rsplit(".", 1)[1])
        if parent and n in by_code:
            pn = int(parent.rsplit(".", 1)[1])
            for t1, p1, g1 in by_code.get(pn, []):
                for t2, p2, g2 in by_code[n]:
                    if t1 != t2 and overlap(p1, p2):
                        add("error", "contradiction", f"grouping code .{pn} is {t1} but its child .{n} is {t2}", f"{g1} vs {g2}")
    for pre, deps in REQUIRES.items():
        denied = [p for t, p, _ in by_code.get(pre, []) if t == "deny"]
        for d in deps:
            for t, p, g in by_code.get(d, []):
                if t == "permit" and any(overlap(p, q) for q in denied):
                    add("error", "contradiction", f"policy .{d} permitted but its prerequisite .{pre} denied", g)
    return out


def consents(path):
    """Yield Consent-like dicts from a file holding a resource, a JSON array, or a Bundle."""
    data = json.load(open(path, encoding="utf-8"))
    items = data if isinstance(data, list) else [e.get("resource", {}) for e in data.get("entry", [])] if data.get("resourceType") == "Bundle" else [data]
    for i, r in enumerate(items):
        yield f"{os.path.basename(path)}#{i}", r


def summary(findings):
    """Overall verdict + set of categories with at least one error/warning."""
    cats = sorted({c for s, c, _, _ in findings if s in ("error", "warning")})
    verdict = "incorrect" if any(s == "error" for s, *_ in findings) else "minor_issues" if cats else "correct"
    return verdict, cats


def _policy(r):
    """-> ({code number: (type, period)}, set of document OID suffixes, signature date)"""
    codes = {}
    for s in ((r.get("provision") or {}) if isinstance(r.get("provision"), dict) else {}).get("provision") or []:
        for c in s.get("code") or []:
            for x in c.get("coding") or []:
                if str(x.get("code", "")).startswith(P):
                    codes[int(x["code"].rsplit(".", 1)[1])] = (s.get("type"), s.get("period") or {})
    docs = {u.rsplit(".", 1)[1] for u in (p.get("uri") or "" for p in r.get("policy") or [])
            if u.replace("urn:oid:", "").rsplit(".", 1)[0] == "2.16.840.1.113883.3.1937.777.24.2"}
    return codes, docs & set(DOC_OIDS), ymd(r.get("dateTime") or "")


def compare(a, b):
    """Cross-export check: two Consents for the same patient/case must tell the same story."""
    out = []
    add = lambda sev, rule, det="": out.append((sev, "consistency", rule, str(det)))
    ca, da, sa = _policy(a)
    cb, db, sb = _policy(b)
    if da and db and bool(da & MINOR_DOCS) != bool(db & MINOR_DOCS):
        add("error", "one export uses a minors' consent form, the other an adult form",
            f"{sorted(DOC_OIDS[d] for d in da)} vs {sorted(DOC_OIDS[d] for d in db)}")
    elif da and db and da != db:
        add("warning", "exports reference different consent documents",
            f"{sorted(DOC_OIDS[d] for d in da)} vs {sorted(DOC_OIDS[d] for d in db)}")
    if sa and sb and sa != sb:
        add("warning", "signature dates differ between exports", f"{sa} vs {sb} ({abs((sa - sb).days)} days)")
    for n in sorted(set(ca) & set(cb)):
        if ca[n][0] != cb[n][0] and overlap(ca[n][1], cb[n][1]):
            add("error", "same policy permitted in one export and denied in the other", f".{n}: {ca[n][0]} vs {cb[n][0]}")
    parent = {int(c.rsplit(".", 1)[1]): int(p.rsplit(".", 1)[1]) for c, (_, p) in CODES.items() if p}
    for x, y, lx, ly in ((ca, cb, "A", "B"), (cb, ca, "B", "A")):
        for n, (t, per) in x.items():
            pn = parent.get(n)
            if pn in y and y[pn][0] != t and pn not in x and overlap(per, y[pn][1]):
                add("error", "grouping code in one export contradicts its child in the other",
                    f"{ly}: .{pn} {y[pn][0]} vs {lx}: .{n} {t}")
    only_a, only_b = sorted(set(ca) - set(cb)), sorted(set(cb) - set(ca))
    if only_a or only_b:
        add("warning", "exports cover different policy codes", f"only A: {only_a or '-'}; only B: {only_b or '-'}")
    return out


# ---------------------------------------------------------------- HL7 validator
VALIDATOR_JAR = os.environ.get("CONSENT_VALIDATOR_JAR", os.path.expanduser("~/.fhir/validator_cli.jar"))
VALIDATOR_URL = "https://github.com/hapifhir/org.hl7.fhir.core/releases/latest/download/validator_cli.jar"
# JAVA_HOME wins over PATH (Homebrew's openjdk is keg-only and not on PATH).
JAVA = os.path.join(os.environ["JAVA_HOME"], "bin", "java") if os.environ.get("JAVA_HOME") else "java"


def install_validator(path=VALIDATOR_JAR, url=VALIDATOR_URL):
    """Download the official HL7 validator to path (atomically: .part file, then rename)."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    part = path + ".part"
    print(f"downloading {url}\n         -> {path}", file=sys.stderr)
    with urllib.request.urlopen(url) as resp, open(part, "wb") as out:
        total, done = int(resp.headers.get("Content-Length") or 0), 0
        while chunk := resp.read(1 << 20):
            out.write(chunk)
            done += len(chunk)
            print(f"\r  {done >> 20} / {total >> 20} MB" if total else f"\r  {done >> 20} MB", end="", file=sys.stderr)
    os.replace(part, path)
    print(f"\ndone. Java is {'found' if shutil.which(JAVA) else 'NOT found -- install Java 17+'}.", file=sys.stderr)
HL7_SKIP = ("dom-6",)  # "resource should have narrative" best-practice noise
# Not document errors: the 2025.0.1 package does not ship the category CodeSystem, and the other two are
# 'should' (extensible/example) bindings of the FHIR base Consent resource.
HL7_INFO = ("mii-cs-consent-consent_category' could not be found", "ValueSet/consent-category|4.0.1", "ValueSet/consent-policy|4.0.1")
ANON = "replaced"  # de-identification placeholder in the source data


def anon_at(r, path):
    """True if the element at a validator location (e.g. Consent.meta.extension[0]) is, or directly holds,
    the de-identification placeholder -- the issue is then an artefact of de-identification, not of the export."""
    node = r
    for name, idx in re.findall(r"\.?([A-Za-z]+)|\[(\d+)\]", path.split(".", 1)[1] if "." in path else ""):
        node = node.get(name) if name and isinstance(node, dict) else node[int(idx)] if idx and isinstance(node, list) and int(idx) < len(node) else None
        if node is None:
            return False
    return node == ANON or (isinstance(node, dict) and ANON in node.values())


def hl7(items, jar=VALIDATOR_JAR, tx="https://tx.fhir.org/r4"):
    """Run the official HL7 validator once over all consents -> {id: [finding, ...]} (category 'hl7').
    Issues repeated at many locations are collapsed into one finding with a count."""
    with tempfile.TemporaryDirectory() as tmp:
        files = {}
        for i, (key, r) in enumerate(items):
            f = os.path.join(tmp, f"{i:04d}.json")
            json.dump(r, open(f, "w", encoding="utf-8"))
            files[f] = key
        out = os.path.join(tmp, "out.json")
        cmd = [JAVA, "-jar", jar, *files, "-version", "4.0.1", "-ig", "de.medizininformatikinitiative.kerndatensatz.consent#2025.0.1",
               "-profile", PROFILE, "-tx", tx, "-output", out]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if not os.path.exists(out):
            raise RuntimeError(f"HL7 validator failed (exit {proc.returncode}): {proc.stdout[-500:]}{proc.stderr[-500:]}")
        bundle = json.load(open(out, encoding="utf-8"))
    raw = dict(items)
    res = {k: [] for k in files.values()}
    for e in bundle.get("entry", []):
        oo = e["resource"]
        f = next((x["valueString"] for x in oo.get("extension", []) if x["url"].endswith("operationoutcome-file")), None)
        key = files.get(os.path.realpath(f)) or files.get(f) or next((k for p, k in files.items() if f and f.endswith(os.path.basename(p))), None)
        grouped = collections.OrderedDict()
        for iss in oo.get("issue", []):
            text = (iss.get("details") or {}).get("text") or iss.get("diagnostics") or ""
            if any(t in text for t in HL7_SKIP):
                continue
            sev = {"fatal": "error", "information": "info"}.get(iss["severity"], iss["severity"])
            loc = (iss.get("expression") or iss.get("location") or ["?"])[0]
            if anon_at(raw.get(key, {}), loc):
                sev, text = "info", text + " [de-identification placeholder 'replaced']"
            elif any(t in text for t in HL7_INFO):
                sev = "info"
            text = re.sub(r" \(codes = [^)]*\)", "", text)
            norm = re.sub(r"\[\d+\]", "[*]", text)
            g = grouped.setdefault((sev, norm), [0, []])
            g[0] += 1
            g[1].append(loc)
        res[key] = [(sev, "hl7", text if n == 1 else f"{text} (x{n})", ", ".join(locs[:3]) + (" …" if len(locs) > 3 else ""))
                    for (sev, text), (n, locs) in grouped.items()]
    return res


# ---------------------------------------------------------------- report
def collapse(findings, keep=3):
    """Merge findings with the same severity/category/rule; keep the first details and a count."""
    groups = collections.OrderedDict()
    for s, c, rule, det in findings:
        groups.setdefault((s, c, rule), []).append(det)
    return [(s, c, rule if len(d) == 1 else f"{rule} (x{len(d)})",
             "; ".join(x for x in d[:keep] if x) + (" …" if len(d) > keep else ""))
            for (s, c, rule), d in groups.items()]


def case_of(path):
    """CASE12_siteA.json -> CASE12 (exports of one case share the prefix before the last '_')."""
    base = os.path.splitext(os.path.basename(path))[0]
    return base.rsplit("_", 1)[0] if "_" in base else base


def build_report(paths, run_hl7=True, cross=True, jar=VALIDATOR_JAR, tx="https://tx.fhir.org/r4"):
    items = [(k, r, p) for p in paths for k, r in consents(p)]
    findings = {k: check(r) for k, r, _ in items}
    hl7_note = None
    if run_hl7:
        if not os.path.exists(jar):
            hl7_note = f"HL7 validator skipped: {jar} not found (run: consent-check --install-validator)"
        elif not shutil.which(JAVA):
            hl7_note = f"HL7 validator skipped: Java not found ({JAVA}); install Java 17+ or set JAVA_HOME"
        else:
            for k, f in hl7([(k, r) for k, r, _ in items], jar, tx).items():
                findings[k] += f
    resources = []
    for k, r, p in items:
        findings[k] = collapse(findings[k])
        verdict, cats = summary(findings[k])
        resources.append({"id": k, "file": p, "verdict": verdict, "categories": cats,
                          "issues": [{"severity": s, "source": "hl7" if c == "hl7" else "checker", "category": c,
                                      "rule": rule, "detail": det} for s, c, rule, det in findings[k]]})
    cross_res = []
    if cross:
        by_case = collections.defaultdict(list)
        for k, r, p in items:
            by_case[case_of(p)].append((k, r, p))
        for case, lst in by_case.items():
            for i, (ka, ra, pa) in enumerate(lst):
                for kb, rb, pb in lst[i + 1:]:
                    if pa != pb:
                        iss = compare(ra, rb)
                        cross_res.append({"case": case, "a": ka, "b": kb, "issues": [
                            {"severity": s, "source": "checker", "category": c, "rule": rule, "detail": det} for s, c, rule, det in iss]})
    freq = collections.Counter()
    for res in resources:
        freq.update({(i["severity"], i["source"], re.sub(r"'[^']*'", "'…'", re.sub(r"^\d+ | \(x\d+\)$", "", i["rule"])))
                     for i in res["issues"] if i["severity"] != "info"})
    return {"profile": PROFILE, "package": "de.medizininformatikinitiative.kerndatensatz.consent#2025.0.1",
            "hl7_validator": hl7_note or (jar if run_hl7 else "not run"),
            "summary": {"resources": len(resources), "verdicts": dict(collections.Counter(r["verdict"] for r in resources)),
                        "frequent_issues": [{"severity": s, "source": src, "rule": rule, "resources": n}
                                            for (s, src, rule), n in freq.most_common()]},
            "resources": resources, "cross_export": cross_res}


def print_report(rep, verbose=False):
    sev_order = {"error": 0, "warning": 1, "info": 2}
    tag = {"error": "ERROR", "warning": "WARN ", "info": "info "}
    s = rep["summary"]
    print(f"MII consent check: {s['resources']} resources  " + "  ".join(f"{k}={v}" for k, v in sorted(s["verdicts"].items())))
    print(f"profile {rep['package']};  HL7 validator: {rep['hl7_validator']}\n")
    for res in rep["resources"]:
        iss = sorted((i for i in res["issues"] if verbose or i["severity"] != "info"), key=lambda i: sev_order[i["severity"]])
        n = collections.Counter(i["severity"] for i in res["issues"])
        print(f"== {res['id']}  {res['verdict'].upper()}  ({n['error']} errors, {n['warning']} warnings)")
        for i in iss:
            src = "HL7" if i["source"] == "hl7" else i["category"]
            print(f"   {tag[i['severity']]} [{src}] {i['rule']}" + (f" — {i['detail']}" if i["detail"] else ""))
        print()
    if rep["cross_export"]:
        print("== Cross-export consistency (exports of the same case)")
        for c in rep["cross_export"]:
            iss = [i for i in c["issues"] if verbose or i["severity"] != "info"]
            print(f"   {c['case']}: {c['a']} vs {c['b']}" + ("  — consistent" if not iss else ""))
            for i in iss:
                print(f"      {tag[i['severity']]} {i['rule']} — {i['detail']}")
        print()
    print("== Most frequent issues (resources affected)")
    for f in s["frequent_issues"][:15]:
        print(f"   {f['resources']:3}  {tag[f['severity']]} [{f['source']}] {f['rule']}")


def main(argv=None):
    from . import __version__
    ap = argparse.ArgumentParser(prog="consent-check",
                                 description="Check MII KDS Consent resources (IG rules + HL7 validator + cross-export).")
    ap.add_argument("paths", nargs="*", help="JSON files or directories (a file may hold a Consent, an array, or a Bundle)")
    ap.add_argument("--json", action="store_true", help="print the JSON issue report instead of the text list")
    ap.add_argument("--no-hl7", action="store_true", help="skip the HL7 validator (fast, offline)")
    ap.add_argument("--no-cross", action="store_true", help="skip comparing exports of the same case (files named <case>_<source>.json)")
    ap.add_argument("--validator", default=VALIDATOR_JAR, help="path to validator_cli.jar (default %(default)s)")
    ap.add_argument("--install-validator", action="store_true", help="download the HL7 validator to --validator and exit")
    ap.add_argument("--tx", default="https://tx.fhir.org/r4", help="terminology server for the validator, or 'n/a' for offline")
    ap.add_argument("-v", "--verbose", action="store_true", help="also list info-level findings")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    a = ap.parse_args(argv)
    if a.install_validator:
        install_validator(a.validator)
        return 0
    if not a.paths:
        ap.error("no input files or directories given")
    sys.stdout.reconfigure(encoding="utf-8")  # Windows consoles/redirects default to a legacy code page
    paths = [p for x in a.paths for p in (sorted(glob.glob(os.path.join(x, "*.json"))) if os.path.isdir(x) else [x])]
    rep = build_report(paths, not a.no_hl7, not a.no_cross, a.validator, a.tx)
    if a.json:
        json.dump(rep, sys.stdout, ensure_ascii=False, indent=1)
    else:
        print_report(rep, a.verbose)
    return 0


if __name__ == "__main__":
    sys.exit(main())
