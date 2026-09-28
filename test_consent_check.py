"""Tests for consent_check.py: one injected fault per check category, the consistency rules
(XACML vs policy.uri, backdating, late signature, cross-export) and the de-identification filter.
Run: python3 test_consent_check.py"""
import base64, copy, json
import consent_check as cc

base = json.load(open(f"{cc.PKG}/examples/Example_MII_Consent_Einwilligung.json", encoding="utf-8"))  # 1.6f, signed = start
rules = lambda r: {rule for s, c, rule, _ in cc.check(r) if c == "consistency"}

assert not rules(base)

r = copy.deepcopy(base)  # XACML says 1.8, policy.uri says 1.6f, double-encoded
r["policyRule"]["extension"][0]["valueBase64Binary"] = base64.b64encode(base64.b64encode(b"MII_BC_Erwachsene|1.8")).decode()
assert rules(r) == {"Xacml content is base64-encoded 2 times", "Xacml names a different Broad Consent version than policy.uri"}

r = copy.deepcopy(base)
r["provision"]["period"]["start"] = "1970-01-01"
assert any("backdated" in x for x in rules(r))

r = copy.deepcopy(base)
r["dateTime"] = "2021-03-01"
assert rules(r) == {"consent valid before it was signed (period.start before dateTime)"}

a, b = copy.deepcopy(base), copy.deepcopy(base)
assert cc.compare(a, b) == []
b["policy"][0]["uri"] = "urn:oid:2.16.840.1.113883.3.1937.777.24.2.3542"
b["provision"]["provision"][0]["type"] = "deny"
b["dateTime"] = "2020-10-01"
got = {rule for _, _, rule, _ in cc.compare(a, b)}
assert got == {"one export uses a minors' consent form, the other an adult form",
               "same policy permitted in one export and denied in the other",
               "signature dates differ between exports"}, got

g = copy.deepcopy(base)  # grouping code .18 (Biomaterial) denied in one export, child .20 permitted in the other
g["provision"]["provision"] = [{"type": "deny", "period": base["provision"]["period"],
                                "code": [{"coding": [{"system": cc.POLICY_CS, "code": cc.P + "18"}]}]}]
assert "grouping code in one export contradicts its child in the other" in {x[2] for x in cc.compare(base, g)}
# de-identification placeholder at a validator location -> artefact, not an export error
d = {"meta": {"extension": [{"url": "replaced"}]}, "identifier": [{"system": "replaced"}], "patient": {"reference": "Patient/1"}}
assert cc.anon_at(d, "Consent.meta.extension[0]") and cc.anon_at(d, "Consent.identifier[0]")
assert not cc.anon_at(d, "Consent") and not cc.anon_at(d, "Consent.patient") and not cc.anon_at(d, "$")
print("consistency checks PASS")


# --- injected-fault suite: each variant of the MII example must get exactly this verdict and these categories
def mutations():
    """Clean baseline (IG example 1) and one injected fault per failure type."""
    base = json.load(open(f"{cc.PKG}/examples/Example_MII_Consent_Einwilligung.json", encoding="utf-8"))
    base["policyRule"]["extension"][0]["valueBase64Binary"] = "PFBvbGljeS8+"  # "<Policy/>" instead of placeholder text
    P = cc.P

    def m(fn):
        r = copy.deepcopy(base)
        fn(r)
        return r

    def sub(r, n):
        return next(s for s in r["provision"]["provision"] if s["code"][0]["coding"][0]["code"] == P + str(n))

    def add_prov(r, n, typ, years=30):
        r["provision"]["provision"].append({"type": typ, "period": {"start": "2020-09-01", "end": f"{2020 + years}-08-31"},
                                            "code": [{"coding": [{"system": cc.POLICY_CS, "code": P + str(n)}]}]})
    return [
        ("baseline", base, "correct", set()),
        ("no_resourceType", m(lambda r: r.pop("resourceType")), "incorrect", {"structure"}),
        ("extra_element", m(lambda r: r.update(schemaVersion="2025.0.1")), "incorrect", {"structure"}),
        ("null_value", m(lambda r: r["provision"].update(code=None)), "incorrect", {"structure"}),
        ("datetime_no_tz", m(lambda r: r.update(dateTime="2020-09-01T00:00")), "incorrect", {"structure"}),
        ("no_patient", m(lambda r: r.pop("patient")), "incorrect", {"required"}),
        ("bare_oid", m(lambda r: r["policy"][0].update(uri=r["policy"][0]["uri"][8:])), "incorrect", {"policy_uri"}),
        ("policy_is_category", m(lambda r: r["policy"][0].update(uri="urn:oid:" + cc.CAT_CODE)), "incorrect", {"policy_uri"}),
        ("typo_system", m(lambda r: sub(r, 7)["code"][0]["coding"][0].update(system="urn:oid:2.16.840.113883.3.1937.777.24.5.3")),
         "incorrect", {"terminology"}),
        ("top_5_years", m(lambda r: r["provision"]["period"].update(end="2025-08-31")), "minor_issues", {"periods"}),
        ("permit_and_deny", m(lambda r: add_prov(r, 8, "deny")), "incorrect", {"contradiction"}),
        ("prereq_denied", m(lambda r: sub(r, 7).update(type="deny")), "incorrect", {"contradiction"}),
        ("level0_code", m(lambda r: sub(r, 8)["code"][0]["coding"][0].update(code=P + "1")), "minor_issues", {"terminology"}),
        ("profile_pkg_version", m(lambda r: r["meta"].update(profile=[cc.PROFILE + "|2025.0.1"])), "minor_issues", {"required"}),
        ("top_level_code", m(lambda r: r["provision"].update(code=[{"coding": [{"system": cc.POLICY_CS, "code": P + "7"}]}])),
         "incorrect", {"structure"}),
        ("grouping_denied", m(lambda r: add_prov(r, 1, "deny")), "incorrect", {"contradiction", "terminology"}),
        ("source_display_only", m(lambda r: r.update(sourceReference={"display": "SAP"})), "incorrect", {"required"}),
    ]


ok = True
for name, r, verdict, cats in mutations():
    got_v, got_c = cc.summary(cc.check(r))
    good = got_v == verdict and set(got_c) == cats
    ok &= good
    print(f"{'PASS' if good else 'FAIL'} {name:20} verdict={got_v:12} categories={got_c}")
assert ok, "checker disagrees with injected faults"
print("all tests PASS")
