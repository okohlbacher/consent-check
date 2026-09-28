"""De-identify an MII KDS Consent (FHIR R4) JSON file.

Replaces identifying values of Patient / Organization (and the other identity
resources that MII Consent may reference: Practitioner, PractitionerRole,
RelatedPerson, Person) with "replaced", everywhere in the file: standalone
resources, Bundle entries, contained resources, and References to them.

Usage:  consent-deid <in.json> <out.json>   de-identify, write out.json
        consent-deid <in.json>               check only: "all clean" or the
                                             elements that need sanitizing
        consent-deid --selftest
"""
import json
import re
import sys

REDACT = "replaced"

# Elements that carry identity, per resource type. Their values are replaced.
IDENTIFYING = {
    "Patient": {"identifier", "name", "telecom", "address", "contact",
                "generalPractitioner", "managingOrganization", "link"},
    "Organization": {"identifier", "name", "alias", "telecom", "address",
                     "contact", "endpoint", "partOf"},
    "Practitioner": {"identifier", "name", "telecom", "address", "photo"},
    "PractitionerRole": {"identifier", "telecom", "practitioner", "organization",
                         "location", "healthcareService", "endpoint"},
    "RelatedPerson": {"identifier", "name", "telecom", "address", "photo"},
    "Person": {"identifier", "name", "telecom", "address", "photo", "link"},
}

# Removed rather than overwritten: a date/base64 element cannot hold the string
# "replaced" and stay schema-valid.
DROP = {"birthDate", "deceasedDateTime", "photo", "multipleBirthInteger"}

# Resource types whose References count as identifying.
IDENTITY_TYPES = set(IDENTIFYING)

# Element names that hold a Reference to an identity resource in MII Consent
# (Consent.patient/.organization/.performer/.provision.actor.reference,
# Provenance.agent.who, DocumentReference.subject/.custodian/.author,
# QuestionnaireResponse.subject/.author/.source, ...). Needed for logical
# references that carry only an identifier and no literal "Patient/x".
IDENTITY_KEYS = {"patient", "organization", "subject", "performer", "actor",
                 "custodian", "author", "who", "onBehalfOf", "requester",
                 "managingOrganization", "partOf", "generalPractitioner",
                 "practitioner", "source", "recipient", "asserter", "agent",
                 "assigner", "individual", "recorder", "enterer",
                 "verifiedWith"}

# Keys kept verbatim inside a redacted element: they describe structure, not identity.
KEEP = {"use", "type", "code", "coding", "rank", "extension"}

# URLs from these authorities are terminology/profile references, not site
# identity. Anything else ("https://uk-essen.de/...", "http://www.mhh.de/...")
# names the institution and is replaced. "medizininfor" also matches the
# truncated MII URLs some exports contain.
NEUTRAL_URL = ("hl7.org", "loinc.org", "fhir.de", "medizininfor", "snomed.info",
               "dicom.nema.org", "unitsofmeasure.org", "who.int", "ihe-d.de",
               "gematik.de", "dimdi.de", "bfarm.de")

_URL = re.compile(r"^https?://", re.I)
_REF = re.compile(r"\b(%s)/([A-Za-z0-9\-.]{1,64})" % "|".join(sorted(IDENTITY_TYPES)))
_TOP = re.compile(r"^\$\[\d+\]$")
_EMPTY_DIV = '<div xmlns="http://www.w3.org/1999/xhtml">%s</div>' % REDACT


def site_url(value):
    return (isinstance(value, str) and _URL.match(value)
            and not any(d in value for d in NEUTRAL_URL))


class DeId:
    def __init__(self):
        self.dead = set()  # resource ids removed; scrubbed wherever they recur
        self.log = []      # (jsonpath, action, element)

    def pseudonym(self, _rtype, old):
        self.dead.add(old)
        return REDACT

    def redact(self, value):
        if isinstance(value, str):
            return REDACT
        if isinstance(value, dict):
            return {k: (v if k in KEEP and k != "extension" else self.redact(v))
                    for k, v in value.items()}
        if isinstance(value, list):
            return [self.redact(v) for v in value]
        return value  # numbers / bools / null carry no identity

    def put(self, node, field, new, path, action):
        """Assign only when it changes something, and log that as a finding."""
        if node[field] != new:
            node[field] = new
            self.log.append(("%s.%s" % (path, field), action, field))

    def walk(self, node, path="$", key=None):
        if isinstance(node, list):
            for i, item in enumerate(node):
                self.walk(item, "%s[%d]" % (path, i), key)
            return
        if not isinstance(node, dict):
            return

        rtype = node.get("resourceType")

        # 1a. any identifier: the value identifies, and the system URL names the site
        if "identifier" in node:
            self.put(node, "identifier", self.redact(node["identifier"]), path, "replaced")

        # 1b. a resource id is a linkage key into the sending system, and is often
        #     reused verbatim elsewhere in the file (sourceReference, fullUrl, ...)
        if isinstance(node.get("id"), str) and (rtype or path == "$" or _TOP.match(path)):
            self.put(node, "id", self.pseudonym(rtype, node["id"]), path, "pseudonymised")

        # 2. identity resources: redact their identifying elements
        if rtype in IDENTIFYING:
            for field in sorted(IDENTIFYING[rtype] | DROP):
                if field not in node:
                    continue
                if field in DROP:
                    del node[field]
                    self.log.append(("%s.%s" % (path, field), "removed", field))
                else:
                    self.put(node, field, self.redact(node[field]), path, "replaced")
        # 3. References pointing at an identity resource
        if self.is_identity_ref(node, key):
            for field in ("reference", "display", "identifier"):
                if field in node:
                    self.put(node, field, self.redact(node[field]), path, "replaced")

        # 4. narrative text can restate any of the above
        text = node.get("text")
        if isinstance(text, dict) and "div" in text:
            self.put(text, "div", _EMPTY_DIV, "%s.text" % path, "replaced")

        # 5. literal references / fullUrls: rewrite the id, keep the type
        for k, v in list(node.items()):
            if isinstance(v, str) and _REF.search(v) and not site_url(v):
                new = _REF.sub(lambda m: "%s/%s" % (m.group(1),
                                                    self.pseudonym(m.group(1), m.group(2))), v)
                self.put(node, k, new, path, "pseudonymised")

        # 6. institution-naming URLs anywhere (meta.source, meta.extension.url, ...)
        for k, v in list(node.items()):
            if site_url(v):
                self.put(node, k, REDACT, path, "replaced")

        for k, v in node.items():
            self.walk(v, "%s.%s" % (path, k), k)

    @staticmethod
    def is_identity_ref(node, key):
        ref = node.get("reference")
        if isinstance(ref, str) and _REF.search(ref):
            return True
        if isinstance(node.get("type"), str) and node["type"] in IDENTITY_TYPES:
            return True
        return key in IDENTITY_KEYS and any(f in node for f in
                                            ("reference", "identifier", "display"))


def scrub(node, dead, log, path="$"):
    """Second pass: a removed resource id is often echoed elsewhere in the file."""
    items = enumerate(node) if isinstance(node, list) else node.items()
    for k, v in items:
        p = "%s[%d]" % (path, k) if isinstance(node, list) else "%s.%s" % (path, k)
        if isinstance(v, str):
            new = v
            for old in dead:
                if new == old:
                    new = REDACT
                elif "/" + old in new:
                    new = new.replace("/" + old, "/" + REDACT)
            if new != v:
                node[k] = new
                log.append((p, "pseudonymised", k if isinstance(node, dict) else "value"))
        elif isinstance(v, (dict, list)):
            scrub(v, dead, log, p)


def deidentify(doc):
    d = DeId()
    d.walk(doc)
    d.dead.discard(REDACT)
    if d.dead:
        scrub(doc, d.dead, d.log)
    return doc, d.log


def main(argv):
    if len(argv) == 2 and argv[1] == "--selftest":
        return selftest()
    if len(argv) not in (2, 3):
        sys.exit(__doc__)
    try:
        with open(argv[1], encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError) as exc:
        sys.exit("cannot read %s: %s" % (argv[1], exc))
    doc, log = deidentify(doc)

    if len(argv) == 2:  # check mode: report, write nothing
        if not log:
            print("all clean")
            return 0
        print("%d identifying element(s) in %s:" % (len(log), argv[1]))
        for path, action, field in log:
            print("  %-28s %-14s (%s)" % (path, field, action))
        return 1

    with open(argv[2], "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    for path, action, field in log:
        print("%-14s %s" % (action, path), file=sys.stderr)
    print("%d element(s) de-identified -> %s" % (len(log), argv[2]), file=sys.stderr)
    return 0


def selftest():
    doc = {
        "resourceType": "Bundle", "type": "transaction",
        "entry": [
            {"fullUrl": "http://diz.example.org/fhir/Patient/mpi-4711",
             "resource": {
                 "resourceType": "Patient", "id": "mpi-4711",
                 "text": {"status": "generated", "div": "<div>Erika Mustermann</div>"},
                 "identifier": [{"use": "usual",
                                 "type": {"coding": [{"code": "MR"}]},
                                 "system": "https://diz.example.org/pid",
                                 "value": "4711",
                                 "assigner": {"display": "UK Musterstadt"}}],
                 "name": [{"use": "official", "family": "Mustermann",
                           "given": ["Erika"],
                           "extension": [{"url": "http://hl7.org/fhir/StructureDefinition/humanname-own-name",
                                          "valueString": "Musterfrau"}]}],
                 "gender": "female", "birthDate": "1964-08-12",
                 "address": [{"line": ["Musterweg 1"], "city": "Musterstadt",
                              "postalCode": "12345", "country": "DE"}],
                 "telecom": [{"system": "phone", "value": "+49 30 123456"}],
                 "managingOrganization": {"reference": "Organization/diz-mst"}}},
            {"resource": {
                "resourceType": "Organization", "id": "diz-mst",
                "identifier": [{"system": "https://www.medizininformatik-initiative.de/fhir/"
                                          "NamingSystem/org-identifier", "value": "MST"}],
                "name": "DIZ Musterstadt"}},
            {"resource": {
                "resourceType": "Consent", "id": "c1", "status": "active",
                "patient": {"reference": "Patient/mpi-4711", "display": "Erika Mustermann"},
                "dateTime": "2025-03-01",
                "organization": [{"reference": "Organization/diz-mst"}],
                "performer": [{"reference": "Patient/mpi-4711"}],
                "policy": [{"uri": "urn:oid:2.16.840.1.113883.3.1937.777.24.2.1791"}],
                "provision": {"type": "deny", "period": {"start": "2025-03-01"},
                              "provision": [{"type": "permit",
                                             "code": [{"coding": [{"code": "2.16.840.1.113883.3.1937.777.24.5.3.6"}]}],
                                             "actor": [{"role": {"coding": [{"code": "CST"}]},
                                                        "reference": {"reference": "Organization/diz-mst",
                                                                      "display": "DIZ Musterstadt"}}]}]}}},
            {"resource": {
                "resourceType": "Provenance", "id": "p1",
                "target": [{"reference": "Consent/c1"}],
                "recorded": "2025-03-01T10:00:00+01:00",
                "agent": [{"who": {"reference": "Practitioner/dr-mueller",
                                   "display": "Dr. Müller"}}]}},
        ],
    }
    out, log = deidentify(doc)
    blob = json.dumps(out, ensure_ascii=False)

    for leak in ("Mustermann", "Erika", "Musterweg", "Musterstadt", "1964-08-12",
                 "4711", "Müller", "123456", "Musterfrau", "MST"):
        assert leak not in blob, "leaked %r" % leak

    pat = out["entry"][0]["resource"]
    org = out["entry"][1]["resource"]
    con = out["entry"][2]["resource"]
    assert pat["gender"] == "female"                      # research-relevant, kept
    assert "birthDate" not in pat                         # dropped, not stringified
    assert pat["identifier"][0]["system"] == REDACT   # the system URL names the site
    assert pat["identifier"][0]["value"] == REDACT
    assert pat["identifier"][0]["type"]["coding"][0]["code"] == "MR"  # structure kept
    assert pat["id"] == REDACT and org["id"] == REDACT
    assert con["patient"]["reference"] == REDACT
    assert out["entry"][0]["fullUrl"] == REDACT   # the endpoint host names the DIZ
    assert con["organization"][0]["reference"] == REDACT
    assert con["status"] == "active" and con["dateTime"] == "2025-03-01"
    assert out["entry"][3]["resource"]["target"][0]["reference"] == "Consent/replaced"
    assert con["policy"][0]["uri"].startswith("urn:oid:")  # consent semantics intact
    assert con["provision"]["provision"][0]["code"][0]["coding"][0]["code"].startswith("2.16")
    assert json.loads(blob)  # still valid JSON
    assert not deidentify(json.loads(blob))[1], "second pass found leftovers"

    # real-world shape: a bare JSON list of Consent excerpts, no Bundle, ids that
    # are not "Patient/x", and site-internal consent ids that must be KEPT.
    excerpts = [{
        "resourceType": "Consent", "id": "Consent-1751643", "status": "active",
        "identifier": [{"system": "https://www.uniklinik-freiburg.de/fhir/sid/consent_id",
                        "value": "Consent-1751643"}],
        "meta": {"profile": ["https://www.medizininformatik-initiative.de/fhir/modul-consent/"
                             "StructureDefinition/mii-pr-consent-einwilligung"],
                 "source": "http://www.uniklinikum-jena.de/fhir/origin/gics",
                 "extension": [{"url": "http://uk-essen.de/fhir/extension-importer-name",
                                "valueString": "gics"}]},
        "patient": {"reference": "93b9cdfa-7c09-8e8f-349c-6cfe182c8464",
                    "display": "25551899"},
        "organization": [{"display": "Uniklinik Ulm",
                          "identifier": {"system": "https://www.medizininformatik-initiative.de"
                                                   "/fhir/core/CodeSystem/core-location-identifier",
                                         "value": "UKHD"}}],
        "sourceReference": {"reference": "QuestionnaireResponse/Consent-1751643",
                            "display": "SAP"},
        "verification": [{"verified": True, "verificationDate": "2025-10-08T00:00:00+02:00",
                          "verifiedWith": {"reference": "Patient/ANk-A57597E6B1859CF8"}}],
        "extension": [{"url": "http://fhir.de/ConsentManagement/StructureDefinition/DomainReference",
                       "extension": [{"url": "domain",
                                      "valueReference": {"reference": "ResearchStudy/mii-broad-consent"}}]}],
        "policy": [{"uri": "urn:oid:2.16.840.1.113883.3.1937.777.24.2.2079"}],
    }]
    ex, exlog = deidentify(excerpts)
    c = ex[0]
    assert c["patient"] == {"reference": REDACT, "display": REDACT}
    assert c["organization"][0]["display"] == REDACT
    assert c["organization"][0]["identifier"]["value"] == REDACT
    assert c["verification"][0]["verifiedWith"]["reference"] == REDACT
    assert c["verification"][0]["verified"] is True
    assert c["id"] == REDACT
    assert c["identifier"][0] == {"system": REDACT, "value": REDACT}
    # the id is echoed in sourceReference and must not survive there either
    assert c["sourceReference"]["reference"] == "QuestionnaireResponse/replaced"
    assert c["sourceReference"]["display"] == "SAP"           # source system name, kept
    assert c["meta"]["source"] == REDACT                      # names the institution
    assert c["meta"]["extension"][0]["url"] == REDACT         # uk-essen.de
    assert c["meta"]["profile"][0].endswith("mii-pr-consent-einwilligung")  # MII, kept
    assert "1751643" not in json.dumps(ex)
    assert c["extension"][0]["extension"][0]["valueReference"]["reference"].startswith("ResearchStudy/")
    assert not deidentify(json.loads(json.dumps(ex)))[1], "excerpt re-check not clean"
    print("selftest ok (%d elements de-identified, re-check clean)" % len(log))
    return 0


def cli():
    """Entry point of the consent-deid command."""
    sys.exit(main(["consent-deid"] + sys.argv[1:]))


if __name__ == "__main__":
    cli()
