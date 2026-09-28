# consent-check

A command-line tool that checks FHIR R4 `Consent` resources against the
**MII Kerndatensatz Modul Consent**: the profile `mii-pr-consent-einwilligung`
in package `de.medizininformatikinitiative.kerndatensatz.consent` 2025.0.1,
together with the rules of its implementation guide (IG).

For every consent it reports errors and warnings from three sources:

| Source | Covers |
|---|---|
| **Rule checker** | Profile constraints plus IG rules that the profile itself does not encode: valid document OIDs, validity periods, grouping codes, contradictions. |
| **HL7 FHIR validator** | Formal FHIR R4 and profile conformance, terminology bindings, display names. It runs the official validator. |
| **Cross-export comparison** | Consistency between several exports of the same case, e.g. from two source systems. |

The repository also contains a de-identification script for consent files.

---

## Contents

1. [Requirements and installation](#1-requirements-and-installation)
2. [Checking consents](#2-checking-consents)
3. [Reading the report](#3-reading-the-report)
4. [What is checked](#4-what-is-checked)
5. [De-identifying consent files](#5-de-identifying-consent-files)
6. [Tests](#6-tests)
7. [Known issues in the specification](#7-known-issues-in-the-specification)
8. [License](#8-license)

---

## 1. Requirements and installation

- **Python 3.9+.** No third-party packages.
- **Java 17+.** Needed only for the HL7 validator.
- **Network access** to `github.com` (one-time validator download),
  `packages.fhir.org` (the validator loads the MII package on first use) and
  `tx.fhir.org` (terminology server). Checks without the validator run
  offline.

The installation provides two commands: `consent-check` and `consent-deid`.
Pick one of the routes below. After installing, download the HL7 validator
once (section 1.4).

### 1.1 pipx (macOS, Linux, Windows): recommended

[pipx](https://pipx.pypa.io) installs Python command-line tools into their own
isolated environment.

**macOS / Linux**

```bash
brew install pipx            # or: apt install pipx / dnf install pipx
pipx ensurepath              # once; then open a new terminal
pipx install consent-check
```

**Windows** (PowerShell)

```powershell
winget install Python.Python.3.12
py -m pip install --user pipx
py -m pipx ensurepath        # once; then open a new terminal
pipx install consent-check
```

**Pinned version.** `pipx install consent-check==1.1.0`, or straight from
GitHub: `pipx install git+https://github.com/okohlbacher/consent-check@v1.1.0`.

- **Update:** `pipx upgrade consent-check`.
- **Uninstall:** `pipx uninstall consent-check`.

### 1.2 Homebrew (macOS, Linux)

```bash
brew install okohlbacher/tap/consent-check
```

The formula also installs Java (`openjdk`) and wires it up for the validator,
so you only need to download the validator itself (section 1.4).

Homebrew only loads formulae from third-party taps that you trust. Installing
by the full name (`okohlbacher/tap/consent-check`) trusts the formula
automatically. If Homebrew refuses anyway, run
`brew trust --formula okohlbacher/tap/consent-check` and install again.

- **Update:** `brew upgrade consent-check`.
- **Uninstall:** `brew uninstall consent-check`.

### 1.3 From source

For development, or to run without installing:

```bash
git clone https://github.com/okohlbacher/consent-check.git
cd consent-check
python3 -m consent_check --help          # run in place (Windows: py -m consent_check)
python3 -m consent_check.deid --help
pip install -e .                         # optional: install the two commands, editable
```

### 1.4 Java and the HL7 validator

The validator is a Java program of about 200 MB. Python packages don't
include it; download it once:

```bash
consent-check --install-validator        # saves it to ~/.fhir/validator_cli.jar
```

The command also reports whether Java was found. If not, install Java 17+:

- **macOS:** `brew install openjdk` (already done with the Homebrew route).
- **Linux:** e.g. `apt install default-jre`.
- **Windows:** `winget install Microsoft.OpenJDK.21`.

`consent-check` uses `$JAVA_HOME/bin/java` when `JAVA_HOME` is set, otherwise
`java` from your `PATH`. Without Java or the validator, the other checks still
run, and the report says why the validator was skipped.

**Check the installation**

```bash
consent-check --version
consent-deid --selftest
```

### 1.5 Configuration

- **Validator location.** The validator can live elsewhere: pass
  `--validator PATH` (also together with `--install-validator`), or set
  `CONSENT_VALIDATOR_JAR`.
- **MII package files.** The MII policy CodeSystem and the IG examples the
  rule checker needs are bundled. To use another unpacked package, set
  `CONSENT_PKG=/path/to/package`.

## 2. Checking consents

```bash
consent-check FILE_OR_DIRECTORY [...]
```

**Input.** Any number of files and directories; directories are scanned for
`*.json`. A file may contain:

- a single `Consent`,
- a JSON array of `Consent` resources, or
- a `Bundle` whose entries are `Consent` resources.

**Options**

| Option | Effect |
|---|---|
| *(none)* | Human-readable issue list on stdout. |
| `--json` | JSON issue report on stdout (see [3.2](#32-json-report)). |
| `--no-hl7` | Skip the HL7 validator. Fast and offline. |
| `--no-cross` | Skip the cross-export comparison. |
| `-v`, `--verbose` | Also list info-level findings. |
| `--validator PATH` | Path to `validator_cli.jar` (default `~/.fhir/validator_cli.jar`). |
| `--tx URL` | Terminology server for the validator (default `https://tx.fhir.org/r4`). Use `n/a` to work offline. |

**Examples**

```bash
consent-check exports/                         # everything, text report
consent-check --json exports/ > report.json    # JSON for further processing
consent-check --no-hl7 exports/site_a.json     # quick offline check of one file
```

**Cross-export comparison.** The comparison needs files named
`<case>_<source>.json`, e.g. `CASE7_kis.json` and `CASE7_gics.json`. All
files sharing the part before the last `_` count as exports of the same
case, and every consent in one is compared with every consent in the other.

**Runtime.** Without the validator, the checks take milliseconds per consent.
With the validator, expect about 30 s of start-up plus a fraction of a second
per consent. All consents go to the validator in a single run.

## 3. Reading the report

Every finding has a **severity**:

| Severity | Meaning |
|---|---|
| `ERROR` | Violates the profile, FHIR, or a binding IG rule. The consent should be corrected. |
| `WARN` | Deviates from an IG recommendation, or is suspicious or inconsistent. Needs review. |
| `info` | Context only, shown with `-v`. |

Each consent gets a **verdict**:

| Verdict | Condition |
|---|---|
| `correct` | No errors and no warnings. |
| `minor_issues` | Warnings only. |
| `incorrect` | At least one error. |

### 3.1 Text report

```
MII consent check: 3 resources  correct=1  incorrect=1  minor_issues=1
profile de.medizininformatikinitiative.kerndatensatz.consent#2025.0.1;  HL7 validator: ~/.fhir/validator_cli.jar

== CASE7_kis.json#0  INCORRECT  (2 errors, 1 warnings)
   ERROR [policy_uri] policy.uri is a bare OID (needs urn:oid: prefix) — 2.16.840.1.113883.3.1937.777.24.2.2079
   ERROR [HL7] Consent.patient: minimum required = 1, but only found 0 (from …mii-pr-consent-einwilligung|1.0.8) — Consent
   WARN  [periods] overall provision.period is not 30 years — 5.0 y (2025-06-11..2030-06-11)

== Cross-export consistency (exports of the same case)
   CASE7: CASE7_kis.json#0 vs CASE7_gics.json#0
      ERROR same policy permitted in one export and denied in the other — .27: deny vs permit

== Most frequent issues (resources affected)
     5  ERROR [checker] policy.uri is a bare OID (needs urn:oid: prefix)
```

**Consent IDs.** `file.json#n` identifies the *n*-th consent (from 0) in that
file.

**Tags.** The bracket after the severity says where a finding comes from:

- `[HL7]`: the HL7 validator.
- `[structure]`, `[required]`, `[policy_uri]`, `[terminology]`, `[periods]`,
  `[contradiction]`, `[consistency]`: the rule checker (see [section 4](#4-what-is-checked)).

**Collapsing.** Findings that repeat at many locations are merged. `(x20)`
gives the count, followed by the first locations.

### 3.2 JSON report

```jsonc
{
  "profile": "https://www.medizininformatik-initiative.de/fhir/modul-consent/StructureDefinition/mii-pr-consent-einwilligung",
  "package": "de.medizininformatikinitiative.kerndatensatz.consent#2025.0.1",
  "hl7_validator": "/home/user/.fhir/validator_cli.jar",   // or "not run" / reason it was skipped
  "summary": {
    "resources": 3,
    "verdicts": {"correct": 1, "minor_issues": 1, "incorrect": 1},
    "frequent_issues": [{"severity": "error", "source": "checker", "rule": "…", "resources": 5}]
  },
  "resources": [{
    "id": "CASE7_kis.json#0", "file": "exports/CASE7_kis.json",
    "verdict": "incorrect", "categories": ["policy_uri", "hl7", "periods"],
    "issues": [{"severity": "error", "source": "checker", "category": "policy_uri",
                "rule": "policy.uri is a bare OID (needs urn:oid: prefix)",
                "detail": "2.16.840.1.113883.3.1937.777.24.2.2079"}]
  }],
  "cross_export": [{
    "case": "CASE7", "a": "CASE7_kis.json#0", "b": "CASE7_gics.json#0",
    "issues": [{"severity": "error", "source": "checker", "category": "consistency",
                "rule": "same policy permitted in one export and denied in the other", "detail": ".27: deny vs permit"}]
  }]
}
```

- **`source`**: `checker` or `hl7`.
- **`category`**: one of the check groups in [section 4](#4-what-is-checked), or `hl7`.
- **`categories`**: every category with at least one error or warning.

**Exit status.** The command exits with `0` whenever the report was
produced. Use the verdicts in the JSON report to fail a pipeline.

## 4. What is checked

### structure: well-formed FHIR R4

| Check | Severity |
|---|---|
| `resourceType` is `Consent` | error |
| No elements outside the FHIR R4 `Consent` definition (e.g. `schemaVersion`) | error |
| No `null`, empty string, empty object or empty array | error |
| `dateTime` values valid; a time needs seconds and a timezone | error |
| No `code`/`action` on the top-level provision; no third nesting level | error |
| XACML payload is valid base64 | error |

### required: mandatory elements of the MII profile

| Check | Severity |
|---|---|
| `scope` = `consentscope#research` (exactly one coding) | error |
| At least two categories: LOINC `57016-8` and MII `2.16.840.1.113883.3.1937.777.24.2.184` | error |
| `patient` present, as reference or identifier (system + value) | error |
| `dateTime`, `status`, `policy` present | error |
| Every provision has `type` and `period.start`/`period.end`; nested provisions have a code | error |
| `sourceReference` has a `reference`; `DomainReference` has `domain` and a status code | error |
| `meta.profile` version suffix, if present, is a profile version (`\|1.0.8`/`\|1.0.9`), not a package version | warning |

### policy_uri: document OID

`Consent.policy.uri` must be `urn:oid:` followed by one of the MII Broad
Consent document OIDs:

- **Broad Consent forms:** 1.6d, 1.6f, 1.7.2.
- **Minors' forms:** `…3542`, `…3543`, `…3544`.
- **Refusals and withdrawals.**
- **Add-on modules:** ACRIBiS, PROM, SNID, DZPG.

| Check | Severity |
|---|---|
| Bare OID without `urn:oid:` | error |
| Category code `…24.2.184` used as `policy.uri` | error |
| Unknown document OID, or not an MII OID (e.g. `urn:uuid:`) | warning |
| Duplicate `policy.uri` | warning |

### terminology: codes and code systems

| Check | Severity |
|---|---|
| Provision code system is exactly `urn:oid:2.16.840.1.113883.3.1937.777.24.5.3` | error |
| Provision code exists in the MII policy CodeSystem | error |
| Provision code has a coding, not only text | error |
| MII category code system is exactly `…/mii-cs-consent-consent_category` | error |
| Level-0 grouping code (e.g. `.1`, `.10`, `.14`) used instead of a concrete policy | warning |
| Deprecated policy code | info |

### periods: validity

| Check | Severity |
|---|---|
| Overall period is 30 years; for minors' forms it ends at majority | warning |
| Each permitted policy has its IG validity: 5 years (e.g. MDAT/BIOMAT erheben) or 30 years | warning |
| Nested period inside the overall period | warning |
| Nested period ends before it starts | error |

### contradiction: within one consent

| Check | Severity |
|---|---|
| Same policy both permitted and denied in overlapping periods | error |
| Grouping code and one of its child codes with opposite types | error |
| Policy permitted while its prerequisite is denied (e.g. *MDAT wissenschaftlich nutzen* without *MDAT speichern*) | error |
| Top-level provision is `permit` (IG: `deny` plus nested permits) | warning |

### consistency: within one consent and across exports

| Check | Severity |
|---|---|
| XACML in `policyRule` names a different Broad Consent version than `policy.uri` | warning |
| XACML base64-encoded more than once | warning |
| Overall period starts more than a year before signature (backdated) | warning |
| Consent valid before it was signed (`period.start` before `dateTime`) | warning |
| *Across exports:* one export uses a minors' form, the other an adult form | error |
| *Across exports:* same policy permitted in one and denied in the other | error |
| *Across exports:* grouping code in one contradicts a child code in the other | error |
| *Across exports:* different documents, signature dates, or policy codes covered | warning |

### HL7 validator findings

Validator issues are reported as-is under `[HL7]`, with these adjustments:

- **Merged:** repeated issues are collapsed into one line with a count.
- **Dropped:** the best-practice warning "resource should have narrative"
  (`dom-6`).
- **Shown as `info`:** issues on elements that hold the de-identification
  placeholder `"replaced"` (see [section 5](#5-de-identifying-consent-files)).
- **Shown as `info`:** three warnings caused by the specification rather than
  the data:
  - the MII category CodeSystem is missing from the package;
  - the FHIR base `consent-category` binding is non-mandatory;
  - the FHIR base `consent-policy` binding is non-mandatory.

## 5. De-identifying consent files

`consent-deid` removes identifying content before consent files are
shared, e.g. with this checker or with other people.

```bash
consent-deid in.json out.json   # write de-identified copy; log of changes on stderr
consent-deid in.json            # check only: prints "all clean" or the elements to sanitize
```

**What it changes**

- **Replaced with `"replaced"`:**
  - identifying elements of `Patient`, `Organization`, `Practitioner`,
    `PractitionerRole`, `RelatedPerson` and `Person` (identifier, name,
    telecom, address, …);
  - references to these resources, including their display and identifier;
  - all identifiers and resource IDs, wherever they recur in the file;
  - narrative text;
  - URLs that name the institution.
- **Removed:** `birthDate`, `deceasedDateTime`, `photo` and
  `multipleBirthInteger`, because these can't hold a placeholder string.
- **Kept:** clinical and consent content (gender, policy codes, periods), and
  terminology and profile URLs (HL7, LOINC, fhir.de, MII, …).

**Exit status in check mode:** `0` if clean, `1` if something needs
sanitizing.

The checker treats `"replaced"` as a valid placeholder. Validator issues it
causes are shown as `info`, not as errors.

## 6. Tests

The tests run from a source checkout (section 1.3). They are plain Python
scripts, need no test framework, and run offline.

```bash
python3 test_consent_check.py            # rule checker: consistency rules + injected faults
python3 -m consent_check.deid --selftest # de-identification
python3 -m consent_check --no-hl7 consent_check/data/mii-consent-2025.0.1/examples   # smoke test on the official MII examples
```

The GitHub Actions workflow (`.github/workflows/test.yml`) runs these three
commands on Linux, macOS and Windows for every push and pull request. It then
builds the package, installs it and runs the installed `consent-check` and
`consent-deid` commands.

### 6.1 `test_consent_check.py`: consistency rules

Starts from the official MII example consent (Broad Consent 1.6f) and checks:

| Case | Expected result |
|---|---|
| Unmodified example | no consistency finding |
| XACML says `MII_BC_Erwachsene\|1.8`, base64-encoded twice | "Xacml content is base64-encoded 2 times" and "Xacml names a different Broad Consent version than policy.uri" |
| Overall period starts 1970 | "overall period backdated" |
| Signed six months after the period starts | "consent valid before it was signed" |
| Two identical exports | no cross-export finding |
| Second export: minors' form, one policy denied, different signature date | "minors' … adult form", "same policy permitted … denied", "signature dates differ" |
| Second export denies grouping code `.18`, first permits child `.20` | "grouping code in one export contradicts its child in the other" |
| Validator location on an element holding `"replaced"` | recognised as a de-identification artefact; the resource root or unrelated elements are not |

### 6.2 `test_consent_check.py`: injected faults

Runs the complete rule checker on the official MII example and 16 variants,
each with one injected fault. It asserts that the verdict and the failing
categories are exactly as expected:

| Case | Injected fault | Expected verdict | Expected categories |
|---|---|---|---|
| `baseline` | none | correct | – |
| `no_resourceType` | `resourceType` removed | incorrect | structure |
| `extra_element` | non-FHIR element `schemaVersion` | incorrect | structure |
| `null_value` | `provision.code: null` | incorrect | structure |
| `datetime_no_tz` | `dateTime` `2020-09-01T00:00` | incorrect | structure |
| `top_level_code` | code on the top-level provision | incorrect | structure |
| `no_patient` | `patient` removed | incorrect | required |
| `source_display_only` | `sourceReference` with display only | incorrect | required |
| `profile_pkg_version` | `meta.profile` ends in `\|2025.0.1` | minor_issues | required |
| `bare_oid` | `policy.uri` without `urn:oid:` | incorrect | policy_uri |
| `policy_is_category` | `policy.uri` = category code `…24.2.184` | incorrect | policy_uri |
| `typo_system` | provision system `2.16.840.113883…` (digit missing) | incorrect | terminology |
| `level0_code` | grouping code `.1` instead of `.8` | minor_issues | terminology |
| `top_5_years` | overall period only 5 years | minor_issues | periods |
| `permit_and_deny` | policy `.8` permitted and denied | incorrect | contradiction |
| `prereq_denied` | `.7` (MDAT speichern) denied while `.8` permitted | incorrect | contradiction |
| `grouping_denied` | grouping code `.1` denied while its children are permitted | incorrect | contradiction, terminology |

### 6.3 `consent-deid --selftest`

De-identifies a synthetic Bundle (Patient, Organization, Consent, Provenance)
and asserts:

- **Nothing leaks:** none of the identifying strings remain anywhere in the
  output (name, address, phone, birth date, record number, practitioner,
  organization).
- **Right elements changed:** `birthDate` is removed, identifier system and
  value are replaced, resource IDs and references are pseudonymised.
- **Structure kept:** gender, identifier types, policy codes and source system
  names are unchanged.
- **Idempotent:** running it again on the output finds nothing left to change.

### 6.4 Testing with the HL7 validator

The automated tests run offline and don't use the validator. To check the
validator integration, run the full check on the official examples:

```bash
python3 -m consent_check consent_check/data/mii-consent-2025.0.1/examples
```

Expected: the rule checker gives the first example no findings. For the second
it reports one validity warning, because that example bundles the 30-year
policy `.7` into a 5-year provision. The validator additionally reports
"Wrong Display Name" errors on both examples. All of this is correct
behaviour; see [section 7](#7-known-issues-in-the-specification).

## 7. Known issues in the specification

- **`meta.profile` version.** In a canonical reference `url|version`, the
  version is the profile's `StructureDefinition.version`, not the package
  version.
  - Consent 2025.0.x: `1.0.8`. Consent 2026.0.0: `1.0.9` for the Einwilligung
    profile.
  - Other MII modules (e.g. base, laborbefund, medikation 2026.0.0) version
    their profiles with the package version. Consent adopts this scheme only
    from 2027.0.0, so `|2025.0.1` is a common mistake.
  - The checker warns about it.
- **Display names.** The IG text shows displays like `MDAT_erheben`, and the
  official examples use them, but the CodeSystem defines `MDAT erheben`. The
  HL7 validator reports these as errors, even on the official examples.
- **Category CodeSystem.** The package references
  `mii-cs-consent-consent_category` but doesn't include it, so the validator
  can't check that code. This checker verifies it itself.

**Rule sources**

- [MII IG Modul Consent](https://www.medizininformatik-initiative.de/Kerndatensatz/Modul_Consent/IGMIIKDSModulConsent-TechnischeImplementierung-FHIRProfile-Consent.html):
  profile, document-OID table, nested provisions.
- [MII Consent policy CodeSystem](https://www.medizininformatik-initiative.de/Kerndatensatz/Modul_Consent/IGMIIKDSModulConsent-TechnischeImplementierung-Terminologien.html):
  per-policy validity.
- [kerndatensatzmodul-consent on GitHub](https://github.com/medizininformatik-initiative/kerndatensatzmodul-consent).
- [FHIR R4 canonical references](http://hl7.org/fhir/R4/references.html#canonical).

## 8. License

The code is under the MIT License (see `LICENSE`).

The files in `consent_check/data/mii-consent-2025.0.1/` are © TMF e. V. and licensed under
CC BY 4.0 (see `consent_check/data/mii-consent-2025.0.1/NOTICE.md`).
