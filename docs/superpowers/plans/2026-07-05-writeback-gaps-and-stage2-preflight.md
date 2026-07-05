# Write-back Gaps + Stage-2 Preflight Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stage-1 write-back fills the fields the org's validation rules demand at Lead conversion (and stops writing D&B-owned fields); Stage 2 gains a read-only preflight that skips+reports leads that would fail those rules.

**Architecture:** Pure helpers in `writeback.py` build a fill-only-blank payload from the Lead's current values (newly pulled by extraction). A new `convert/preflight.py` holds pure check functions mirroring the org rules, used twice: live in `run_stage2` (bulk re-query → skip failing targets → CSV report) and statically in the QA Review sheet (`convert_blockers` column over post-write-back state).

**Tech Stack:** Python 3.12, dataclasses, pytest (`python -m pytest -q`, pythonpath=src via pyproject.toml), sf CLI wrapper (`SfClient.query`), openpyxl, typer.

**Spec:** `docs/superpowers/specs/2026-07-05-writeback-gaps-and-stage2-preflight-design.md`

## Global Constraints

- All NEW write-back fields are fill-only-blank — never overwrite an existing Lead value.
- `KN_NoOwnDBID__c = true` whenever Lead `KN_DBID__c` is blank and the tick isn't set (every processed lead, LF-matched or not).
- Drop ALL `KN_DB*` writes: `KN_DBLegalCompanyName__c`, `KN_DBStreetHouseNumber__c`, `KN_DBPostalCode__c`, `KN_DBCity__c`, `KN_DBISO2CountryCode__c`, `KN_DBNumberofEmployees__c`, `KN_DBRevenueinMillions__c`, `KN_DBMainIndustryTextperWZ08__c`, `KN_DBID__c`.
- No `Email`, `Salutation`, or `KN_Language__c` writes. No Salesforce metadata changes. Preflight is read-only.
- Bulk-API CSV booleans are written as `"true"`/`"false"` strings (existing `KN_KununuProfileClaimed__c` pattern).
- Org rule constants (verified 2026-07-05): Street ≤ 100 chars; postal length DE=5/CH=4; phone `+` + digits only; freemail blob `gmail,yahoo,outlook,hotmail,aol,icloud,protonmail,zoho,mail.com`; `LeadSource='E-commerce'` exempt from phone/postal rules.
- Commit after each task; run `python -m pytest -q` before every commit (expect all green).

---

### Task 1: Write-back helpers (`normalize_phone`, `split_first_name`, `employee_bin_from_size`)

**Files:**
- Modify: `src/leadauto/writeback.py` (add helpers near `employee_bin`, add `import re` at top)
- Test: `tests/test_writeback.py` (append)

**Interfaces:**
- Produces: `normalize_phone(value) -> str | None`, `split_first_name(source_name, last_name) -> str | None`, `employee_bin_from_size(size) -> str | None` — all importable from `leadauto.writeback`; Task 3 uses them in `lead_field_payload`.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_writeback.py`:

```python
from leadauto.writeback import normalize_phone, split_first_name, employee_bin_from_size


class TestNormalizePhone:
    def test_strips_separators(self):
        assert normalize_phone("+49 40 41 91-31") == "+4940419131"

    def test_double_zero_becomes_plus(self):
        assert normalize_phone("0049 (40) 419131") == "+4940419131"

    def test_dots_and_slashes_stripped(self):
        assert normalize_phone("+43.1/512 34 56") == "+4315123456"

    def test_national_number_rejected(self):
        assert normalize_phone("040 419131") is None  # no country prefix — can't infer

    def test_garbage_rejected(self):
        assert normalize_phone("+49 40 CALL-ME") is None

    def test_too_short_rejected(self):
        assert normalize_phone("+4912") is None  # fewer than 6 digits

    def test_empty_and_none(self):
        assert normalize_phone("") is None
        assert normalize_phone(None) is None


class TestSplitFirstName:
    def test_leading_remainder_when_ends_with_lastname(self):
        assert split_first_name("Max Mustermann", "Mustermann") == "Max"

    def test_case_insensitive(self):
        assert split_first_name("max MUSTERMANN", "Mustermann") == "max"

    def test_multiword_first(self):
        assert split_first_name("Anna Maria Schmidt", "Schmidt") == "Anna Maria"

    def test_mismatched_lastname_skipped(self):
        assert split_first_name("Max Mustermann", "Meier") is None

    def test_name_is_only_lastname(self):
        assert split_first_name("Mustermann", "Mustermann") is None

    def test_none_inputs(self):
        assert split_first_name(None, "Meier") is None
        assert split_first_name("Max Meier", None) is None


class TestEmployeeBinFromSize:
    def test_exact_label(self):
        assert employee_bin_from_size("51-100") == "51-100"

    def test_label_with_suffix_maps_upper_bound(self):
        assert employee_bin_from_size("51-100 Mitarbeiter") == "51-100"

    def test_german_dot_upper_bound(self):
        assert employee_bin_from_size("501-1.000") == "751-1.000"  # upper bound 1000 -> its bin

    def test_open_ended(self):
        assert employee_bin_from_size("10.001+") == "10.001-30.000"

    def test_no_number(self):
        assert employee_bin_from_size("unbekannt") is None

    def test_empty(self):
        assert employee_bin_from_size("") is None
        assert employee_bin_from_size(None) is None
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_writeback.py -q`
Expected: FAIL — `ImportError: cannot import name 'normalize_phone'`

- [ ] **Step 3: Implement** — in `src/leadauto/writeback.py`, add `import re` under `from datetime import date`, then after `revenue_millions` (which Task 3 removes; placement near `employee_bin` is fine):

```python
def normalize_phone(value) -> str | None:
    """Normalize to the org's +<digits> phone format; None when not confidently possible.

    Strips spaces/dashes/dots/parens/slashes, converts a 00 international prefix to +,
    and accepts only + followed by 6-15 digits (KN_ValidatePhone / KN_ContactValidPhoneNumber
    reject everything else). National 0... numbers are dropped: guessing a country is worse
    than leaving the field blank.
    """
    if not value:
        return None
    s = re.sub(r"[\s\-.()/]", "", str(value).strip())
    if s.startswith("00"):
        s = "+" + s[2:]
    return s if re.fullmatch(r"\+[0-9]{6,15}", s) else None


def split_first_name(source_name, last_name) -> str | None:
    """Leading remainder of `source_name` when it ends with the Lead's `last_name`.

    Guard against writing a different person's name: no last-name agreement, no split.
    """
    if not source_name or not last_name:
        return None
    src, ln = str(source_name).strip(), str(last_name).strip()
    if not ln or not src.lower().endswith(ln.lower()):
        return None
    first = src[: len(src) - len(ln)].strip()
    return first or None


def employee_bin_from_size(size) -> str | None:
    """Map a parsed kununu company-size string onto the KN_Employee__c picklist.

    Exact label match first; otherwise the range's upper bound (German dot separators
    stripped) goes through employee_bin(). Used only as a fill-blank fallback when
    LeadFeeder has no Employee_Count.
    """
    if not size:
        return None
    s = str(size).strip()
    if s in {label for _, label in _EMPLOYEE_BINS} | {"> 100.000"}:
        return s
    nums = re.findall(r"\d[\d.]*", s)
    if not nums:
        return None
    try:
        upper = int(nums[-1].replace(".", ""))
    except ValueError:
        return None
    return employee_bin(upper)
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_writeback.py -q`
Expected: PASS (all, including pre-existing)

- [ ] **Step 5: Commit**

```bash
git add src/leadauto/writeback.py tests/test_writeback.py
git commit -m "feat(writeback): phone/first-name/company-size normalization helpers"
```

---

### Task 2: Extraction pulls the Lead's current conversion fields

**Files:**
- Modify: `src/leadauto/sf/soql.py:17-20` (`_LEAD_FIELDS`)
- Modify: `src/leadauto/extract/models.py` (new `lead_*` fields on `ExtractedLead`)
- Modify: `src/leadauto/extract/parsers.py:69-80` (`_base`, new `_bool` helper)
- Test: `tests/test_parsers.py`, `tests/test_soql.py` (append)

**Interfaces:**
- Produces: `ExtractedLead.lead_street/.lead_city/.lead_postal_code/.lead_country_code/.lead_country/.lead_phone/.lead_source/.lead_dbid/.lead_employee_category/.lead_language` (all `str | None`) and `.lead_opted_out_email/.lead_no_own_dbid` (bool, default False). Tasks 3 and 7 consume these.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_parsers.py`:

```python
from leadauto.extract.parsers import parse_claimer


def test_base_maps_new_lead_native_fields():
    task = {"Id": "00T1", "WhoId": "00Q1", "Subject": "s", "Description": ""}
    lead = {
        "Id": "00Q1", "LastName": "Muster", "Street": "Gasse 1", "City": "Wien",
        "PostalCode": "1010", "CountryCode": "AT", "Country": "Austria",
        "Phone": "+431234567", "LeadSource": "Web", "HasOptedOutOfEmail": "true",
        "KN_DBID__c": "12345", "KN_NoOwnDBID__c": "false",
        "KN_Employee__c": "11-50", "KN_Language__c": "German",
    }
    e = parse_claimer(task, lead)
    assert e.lead_street == "Gasse 1"
    assert e.lead_city == "Wien"
    assert e.lead_postal_code == "1010"
    assert e.lead_country_code == "AT"
    assert e.lead_country == "Austria"
    assert e.lead_phone == "+431234567"
    assert e.lead_source == "Web"
    assert e.lead_opted_out_email is True
    assert e.lead_dbid == "12345"
    assert e.lead_no_own_dbid is False
    assert e.lead_employee_category == "11-50"
    assert e.lead_language == "German"


def test_base_bool_coercion_accepts_real_booleans():
    task = {"Id": "00T1", "WhoId": "00Q1", "Subject": "s", "Description": ""}
    lead = {"Id": "00Q1", "LastName": "M", "HasOptedOutOfEmail": False, "KN_NoOwnDBID__c": True}
    e = parse_claimer(task, lead)
    assert e.lead_opted_out_email is False
    assert e.lead_no_own_dbid is True
```

Append to `tests/test_soql.py`:

```python
from leadauto.sf.soql import leads_by_ids


def test_lead_fields_include_conversion_state():
    soql = leads_by_ids(["00Q1"])
    for f in ("Street", "City", "PostalCode", "Country", "CountryCode", "Phone",
              "HasOptedOutOfEmail", "KN_DBID__c", "KN_NoOwnDBID__c",
              "KN_Employee__c", "KN_Language__c"):
        assert f in soql
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_parsers.py tests/test_soql.py -q`
Expected: FAIL — assertions on missing fields / attributes

- [ ] **Step 3: Implement**

`src/leadauto/sf/soql.py` — replace `_LEAD_FIELDS`:

```python
_LEAD_FIELDS = (
    "Id, FirstName, LastName, Company, Email, Website, Status, IsConverted, LeadSource, "
    "HasOptedOutOfEmail, Phone, Street, City, PostalCode, Country, CountryCode, "
    "KN_DBID__c, KN_NoOwnDBID__c, KN_Employee__c, KN_Language__c, "
    "KN_KununuProfileName__c, KN_KununuUserUUID__c, KN_KununuLinkProfile__c"
)
```

`src/leadauto/extract/models.py` — extend the "lead-native" block of `ExtractedLead` (after `lead_website`):

```python
    lead_street: str | None = None
    lead_city: str | None = None
    lead_postal_code: str | None = None
    lead_country_code: str | None = None
    lead_country: str | None = None
    lead_phone: str | None = None
    lead_source: str | None = None
    lead_opted_out_email: bool = False
    lead_dbid: str | None = None
    lead_no_own_dbid: bool = False
    lead_employee_category: str | None = None
    lead_language: str | None = None
```

`src/leadauto/extract/parsers.py` — add above `_base`:

```python
def _bool(value) -> bool:
    """Coerce SF query booleans (True/False or Bulk-CSV 'true'/'false' strings)."""
    return value is True or str(value).strip().lower() == "true"
```

and extend `_base`'s `ExtractedLead(...)` call:

```python
        lead_street=lead.get("Street"),
        lead_city=lead.get("City"),
        lead_postal_code=lead.get("PostalCode"),
        lead_country_code=lead.get("CountryCode"),
        lead_country=lead.get("Country"),
        lead_phone=lead.get("Phone"),
        lead_source=lead.get("LeadSource"),
        lead_opted_out_email=_bool(lead.get("HasOptedOutOfEmail")),
        lead_dbid=lead.get("KN_DBID__c"),
        lead_no_own_dbid=_bool(lead.get("KN_NoOwnDBID__c")),
        lead_employee_category=lead.get("KN_Employee__c"),
        lead_language=lead.get("KN_Language__c"),
```

- [ ] **Step 4: Run the full suite** (reader/pipeline tests build leads too)

Run: `python -m pytest -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/leadauto/sf/soql.py src/leadauto/extract/models.py src/leadauto/extract/parsers.py tests/test_parsers.py tests/test_soql.py
git commit -m "feat(extract): pull the Lead's current conversion-relevant fields"
```

---

### Task 3: Payload rewrite — drop KN_DB*, add fill-only-blank conversion fields

**Files:**
- Modify: `src/leadauto/writeback.py` (`lead_field_payload`, remove `revenue_millions`, `_iso2` stays, add `_fill` + `_POSTAL_LEN_BY_CC`, update module docstring)
- Test: `tests/test_writeback.py` (append new, update stale)

**Interfaces:**
- Consumes: Task 1 helpers, Task 2 `lead_*` fields.
- Produces: `lead_field_payload(enriched) -> dict` — same signature, new field set. Task 7 calls it from `qa_artifact`.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_writeback.py` (uses the existing test-file pattern of building `ExtractedLead`/`EnrichedLead` directly):

```python
from leadauto.extract.models import ExtractedLead
from leadauto.leadfeeder.enrich import EnrichedLead
from leadauto.writeback import lead_field_payload


def _extracted(**kw):
    base = dict(lead_id="00Q1", task_id="00T1", task_type="claimer")
    base.update(kw)
    return ExtractedLead(**base)


class TestPayloadDropsDbFields:
    def test_kn_db_fields_never_written(self):
        e = _extracted()
        enr = EnrichedLead(extracted=e, matched=True, matched_name="ACME GmbH",
                           company={"Street": "Weg 2", "Postal_Code": "20095", "City": "HH",
                                    "Country": "de", "Employee_Count": 40, "Revenue": 5e6,
                                    "Industry": "IT", "Register_Id": "77123",
                                    "VAT_Id": "DE123", "Company_LinkedIn": "url",
                                    "Company_Email": "info@acme.de"})
        payload = lead_field_payload(enr)
        assert not any(k.startswith("KN_DB") for k in payload)


class TestNoOwnDbidTick:
    def test_set_true_when_lead_dbid_blank(self):
        enr = EnrichedLead(extracted=_extracted())
        assert lead_field_payload(enr)["KN_NoOwnDBID__c"] == "true"

    def test_not_written_when_lead_has_dbid(self):
        enr = EnrichedLead(extracted=_extracted(lead_dbid="12345"))
        assert "KN_NoOwnDBID__c" not in lead_field_payload(enr)

    def test_not_rewritten_when_already_ticked(self):
        enr = EnrichedLead(extracted=_extracted(lead_no_own_dbid=True))
        assert "KN_NoOwnDBID__c" not in lead_field_payload(enr)


class TestAddressFill:
    def test_parsed_address_beats_lf(self):
        e = _extracted(street="Parsed 1", city="ParsedCity", postal_code="10115",
                       country_code="de")
        enr = EnrichedLead(extracted=e, matched=True,
                           company={"Street": "LF 9", "City": "LFCity",
                                    "Postal_Code": "99999", "Country": "AT"})
        p = lead_field_payload(enr)
        assert p["Street"] == "Parsed 1"
        assert p["City"] == "ParsedCity"
        assert p["PostalCode"] == "10115"
        assert p["CountryCode"] == "DE"

    def test_lf_fallback_when_nothing_parsed(self):
        enr = EnrichedLead(extracted=_extracted(), matched=True,
                           company={"Street": "LF 9", "City": "LFCity",
                                    "Postal_Code": "1010", "Country": "AT"})
        p = lead_field_payload(enr)
        assert p["Street"] == "LF 9"
        assert p["CountryCode"] == "AT"

    def test_existing_lead_values_not_overwritten(self):
        e = _extracted(street="Parsed 1", city="C", postal_code="10115", country_code="DE",
                       lead_street="Human St", lead_city="HumanCity",
                       lead_postal_code="20095", lead_country_code="DE",
                       lead_country="Germany")
        p = lead_field_payload(EnrichedLead(extracted=e))
        for field in ("Street", "City", "PostalCode", "CountryCode"):
            assert field not in p

    def test_street_truncated_to_100(self):
        e = _extracted(street="x" * 150)
        assert len(lead_field_payload(EnrichedLead(extracted=e))["Street"]) == 100

    def test_invalid_de_postal_skipped(self):
        e = _extracted(postal_code="101", country_code="DE")
        assert "PostalCode" not in lead_field_payload(EnrichedLead(extracted=e))

    def test_ch_postal_four_digits_ok(self):
        e = _extracted(postal_code="8004", country_code="CH")
        assert lead_field_payload(EnrichedLead(extracted=e))["PostalCode"] == "8004"


class TestFirstNameAndPhone:
    def test_first_name_split_when_blank_and_agrees(self):
        e = _extracted(contact_name="Max Muster", lead_last_name="Muster")
        assert lead_field_payload(EnrichedLead(extracted=e))["FirstName"] == "Max"

    def test_first_name_not_overwritten(self):
        e = _extracted(contact_name="Max Muster", lead_last_name="Muster",
                       lead_first_name="Existing")
        assert "FirstName" not in lead_field_payload(EnrichedLead(extracted=e))

    def test_lf_contact_full_name_fallback(self):
        e = _extracted(lead_last_name="Muster")
        enr = EnrichedLead(extracted=e, contact_found=True,
                           contact={"full_name": "Eva Muster", "best_contact_phone": ""})
        assert lead_field_payload(enr)["FirstName"] == "Eva"

    def test_phone_normalized_from_lf_contact(self):
        enr = EnrichedLead(extracted=_extracted(), contact_found=True,
                           contact={"best_contact_phone": "+49 40 419131"})
        assert lead_field_payload(enr)["Phone"] == "+4940419131"

    def test_unnormalizable_phone_skipped(self):
        enr = EnrichedLead(extracted=_extracted(), contact_found=True,
                           contact={"best_contact_phone": "040 419131"})
        assert "Phone" not in lead_field_payload(enr)

    def test_phone_not_overwritten(self):
        enr = EnrichedLead(extracted=_extracted(lead_phone="+4311111"), contact_found=True,
                           contact={"best_contact_phone": "+49 40 419131"})
        assert "Phone" not in lead_field_payload(enr)


class TestEmployeeFallback:
    def test_lf_count_wins(self):
        e = _extracted(company_size="1.001-2.500")
        enr = EnrichedLead(extracted=e, matched=True, company={"Employee_Count": 40})
        assert lead_field_payload(enr)["KN_Employee__c"] == "11-50"

    def test_parsed_size_fallback_when_no_lf(self):
        e = _extracted(company_size="51-100 Mitarbeiter")
        assert lead_field_payload(EnrichedLead(extracted=e))["KN_Employee__c"] == "51-100"

    def test_fallback_respects_existing_value(self):
        e = _extracted(company_size="51-100", lead_employee_category="1-10")
        assert "KN_Employee__c" not in lead_field_payload(EnrichedLead(extracted=e))
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_writeback.py -q`
Expected: FAIL — `KN_DB*` keys present, new fields missing

- [ ] **Step 3: Implement** — in `src/leadauto/writeback.py`:

Delete `revenue_millions` (lines 53-60). Add after `_put`:

```python
_POSTAL_LEN_BY_CC = {"DE": 5, "CH": 4}  # KN_Checklengthpostalcode (Austria unchecked)


def _blank(value) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _fill(payload: dict, field: str, current, value) -> None:
    """_put, but only when the Lead's current value is blank (fill-only-blank policy)."""
    if _blank(current):
        _put(payload, field, value)
```

Replace the `# firmographics (from LeadFeeder)` block of `lead_field_payload` (keep the kununu block above it verbatim) with:

```python
    # firmographics (LeadFeeder) — KN_DB* fields are D&B-integration-owned, never written
    emp = employee_bin(company.get("Employee_Count"))
    if emp:
        _put(payload, "KN_Employee__c", emp)
    else:
        _fill(payload, "KN_Employee__c", e.lead_employee_category,
              employee_bin_from_size(e.company_size))
    _put(payload, "KN_VATNumber__c", company.get("VAT_Id") or e.vat_number)
    _put(payload, "KN_LinkedInProfileLink__c", company.get("Company_LinkedIn"))
    _put(payload, "KN_CompanyEmail__c", company.get("Company_Email"))

    # conversion requirements (org validation rules; all fill-only-blank)
    if _blank(e.lead_dbid) and not e.lead_no_own_dbid:
        payload["KN_NoOwnDBID__c"] = "true"  # KN_DbIdValidation XOR
    contact = enriched.contact or {}
    street = e.street or company.get("Street")
    _fill(payload, "Street", e.lead_street, (street or "")[:100] or None)
    _fill(payload, "City", e.lead_city, e.city or company.get("City"))
    country = (e.country_code or _iso2(company.get("Country")) or "").upper()
    if _blank(e.lead_country):
        _fill(payload, "CountryCode", e.lead_country_code, country or None)
    postal = e.postal_code or company.get("Postal_Code")
    effective_cc = ((e.lead_country_code or country) or "").upper()
    expected_len = _POSTAL_LEN_BY_CC.get(effective_cc)
    if postal and (expected_len is None or len(str(postal).strip()) == expected_len):
        _fill(payload, "PostalCode", e.lead_postal_code, str(postal).strip())
    _fill(payload, "FirstName", e.lead_first_name,
          split_first_name(e.contact_name or contact.get("full_name"), e.lead_last_name))
    _fill(payload, "Phone", e.lead_phone, normalize_phone(contact.get("best_contact_phone")))
```

Update the module docstring: replace the four `Org-verified 2026-07-03` bullets about `KN_DBRevenueinMillions__c`/`KN_DBISO2CountryCode__c` with one line: `KN_DB* fields are auto-populated by the org's D&B integration and are never written here (Wasil, 2026-07-05); conversion-requirement fields are fill-only-blank.`

- [ ] **Step 4: Run the suite; update stale tests**

Run: `python -m pytest -q`
Expected: pre-existing `tests/test_writeback.py` tests asserting `KN_DB*` keys or `revenue_millions` FAIL. Delete the `revenue_millions` tests; rewrite `KN_DB*`-asserting tests to assert the fields are ABSENT. Do not weaken unrelated assertions. Re-run until green.

- [ ] **Step 5: Commit**

```bash
git add src/leadauto/writeback.py tests/test_writeback.py
git commit -m "feat(writeback): fill conversion-required fields, stop writing D&B-owned KN_DB*"
```

---

### Task 4: Preflight checks (pure functions)

**Files:**
- Create: `src/leadauto/convert/preflight.py`
- Test: `tests/test_preflight.py` (new)

**Interfaces:**
- Produces: `Failure(check: str, value: str, detail: str)` dataclass and `check_lead(fields: dict, *, create_new: bool) -> list[Failure]` where `fields` uses SF API names (`KN_Employee__c`, `Street`, `Email`, `HasOptedOutOfEmail`, `LeadSource`, `IsConverted`, `Country`, `CountryCode`, ...). Tasks 5 and 7 consume `check_lead`.

- [ ] **Step 1: Write the failing tests** — create `tests/test_preflight.py`:

```python
"""check_lead mirrors the org's conversion-blocking validation rules (audit 2026-07-05)."""
from leadauto.convert.preflight import Failure, check_lead

GOOD = {
    "IsConverted": False, "KN_Employee__c": "11-50", "KN_DBID__c": "",
    "KN_NoOwnDBID__c": "true", "KN_Language__c": "German",
    "Street": "Weg 1", "Country": "Germany", "CountryCode": "DE",
    "PostalCode": "20095", "Phone": "+4940419131",
    "Email": "info@acme.de", "HasOptedOutOfEmail": False, "LeadSource": "Web",
}


def _checks(fields, create_new=True):
    return [f.check for f in check_lead(fields, create_new=create_new)]


def test_good_lead_passes():
    assert check_lead(GOOD, create_new=True) == []


def test_already_converted_short_circuits():
    assert _checks({**GOOD, "IsConverted": "true"}) == ["already_converted"]


def test_employee_category_blank():
    assert "employee_category" in _checks({**GOOD, "KN_Employee__c": ""})


def test_dbid_xor_neither():
    assert "dbid_xor" in _checks({**GOOD, "KN_DBID__c": "", "KN_NoOwnDBID__c": "false"})


def test_dbid_xor_both():
    assert "dbid_xor" in _checks({**GOOD, "KN_DBID__c": "123", "KN_NoOwnDBID__c": "true"})


def test_dbid_alone_ok_and_numeric_enforced():
    ok = {**GOOD, "KN_DBID__c": "12345", "KN_NoOwnDBID__c": "false"}
    assert check_lead(ok, create_new=True) == []
    assert "dbid_numeric" in _checks({**ok, "KN_DBID__c": "HRB 123"})


def test_language_blank():
    assert "language" in _checks({**GOOD, "KN_Language__c": None})


def test_address_required_only_for_create_new():
    no_street = {**GOOD, "Street": ""}
    assert "address_create_new" in _checks(no_street, create_new=True)
    assert "address_create_new" not in _checks(no_street, create_new=False)


def test_address_accepts_countrycode_when_country_missing():
    assert "address_create_new" not in _checks({**GOOD, "Country": ""})
    assert "address_create_new" in _checks({**GOOD, "Country": "", "CountryCode": ""})


def test_freemail_blocks_unless_opted_out():
    free = {**GOOD, "Email": "max@gmail.com"}
    assert "freemail_email" in _checks(free)
    assert "freemail_email" not in _checks({**free, "HasOptedOutOfEmail": "true"})
    assert "freemail_email" not in _checks({**GOOD, "Email": "max@mailbox.org"})


def test_freemail_matches_org_substring_semantics():
    assert "freemail_email" in _checks({**GOOD, "Email": "a@mail.com"})
    assert "freemail_email" in _checks({**GOOD, "Email": "a@gmail.de"})


def test_phone_format_and_ecommerce_exemption():
    bad = {**GOOD, "Phone": "040 419131"}
    assert "phone_format" in _checks(bad)
    assert "phone_format" not in _checks({**bad, "LeadSource": "E-commerce"})
    assert "phone_format" not in _checks({**GOOD, "Phone": ""})


def test_street_length():
    assert "street_length" in _checks({**GOOD, "Street": "x" * 101, })


def test_postal_length_by_country_name():
    assert "postal_length" in _checks({**GOOD, "PostalCode": "1234"})       # Germany needs 5
    ch = {**GOOD, "Country": "Switzerland", "CountryCode": "CH", "PostalCode": "80045"}
    assert "postal_length" in _checks(ch)
    assert "postal_length" not in _checks({**ch, "PostalCode": "8004"})
    at = {**GOOD, "Country": "Austria", "CountryCode": "AT", "PostalCode": "10"}
    assert "postal_length" not in _checks(at)  # rule only covers DE/CH


def test_postal_country_derived_from_countrycode():
    assert "postal_length" in _checks({**GOOD, "Country": "", "PostalCode": "1234"})  # CC=DE


def test_failure_carries_value_and_detail():
    fails = check_lead({**GOOD, "KN_DBID__c": "HRB 1", "KN_NoOwnDBID__c": "false"},
                       create_new=True)
    f = next(x for x in fails if x.check == "dbid_numeric")
    assert isinstance(f, Failure) and f.value == "HRB 1" and "digits" in f.detail
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_preflight.py -q`
Expected: FAIL — `ModuleNotFoundError: leadauto.convert.preflight`

- [ ] **Step 3: Implement** — create `src/leadauto/convert/preflight.py`:

```python
"""Read-only conversion preflight — mirrors the org's blocking validation rules.

Each check is named after the org rule it mirrors (Tooling-API audit 2026-07-05, see
convert/CLAUDE.md "Conversion requirements"). check_lead is a pure function over SF-named
field values so Stage 2 can run it on live query rows and Stage 1 can preview it on
post-write-back state. The org/Apex remains the ultimate arbiter — this only fails
readably and earlier.
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path

from ..logging import get_logger

# Same comma blob + substring semantics as KN_LeadValidatePrivateMail's CONTAINS().
_FREEMAIL_BLOB = "gmail,yahoo,outlook,hotmail,aol,icloud,protonmail,zoho,mail.com"
_POSTAL_LEN_BY_COUNTRY = {"Germany": 5, "Switzerland": 4}
_COUNTRY_BY_CC = {"DE": "Germany", "CH": "Switzerland"}
_PHONE_RE = re.compile(r"\+[0-9]+")

PREFLIGHT_CSV_COLUMNS = ["lead_id", "check", "value", "detail"]


@dataclass
class Failure:
    check: str
    value: str
    detail: str


def _blank(v) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


def _is_true(v) -> bool:
    return v is True or str(v).strip().lower() == "true"


def check_lead(fields: dict, *, create_new: bool) -> list[Failure]:
    """Failures the org's validation rules would raise if this lead converted now."""
    if _is_true(fields.get("IsConverted")):
        return [Failure("already_converted", "", "Lead is already converted (would no-op)")]
    fails: list[Failure] = []

    if _blank(fields.get("KN_Employee__c")):
        fails.append(Failure("employee_category", "",
                             "KN_Employee__c blank (KN_EmployeeCategoryMandatory)"))

    dbid = (fields.get("KN_DBID__c") or "").strip()
    no_own = _is_true(fields.get("KN_NoOwnDBID__c"))
    if bool(dbid) == no_own:
        fails.append(Failure("dbid_xor", f"KN_DBID__c={dbid!r} KN_NoOwnDBID__c={no_own}",
                             "need exactly one of KN_DBID__c / KN_NoOwnDBID__c "
                             "(KN_DbIdValidation)"))
    if dbid and not dbid.isdigit():
        fails.append(Failure("dbid_numeric", dbid,
                             "KN_DBID__c must be digits only (KN_ValidateDandBonlyNumberLead)"))

    if _blank(fields.get("KN_Language__c")):
        fails.append(Failure("language", "",
                             "KN_Language__c blank (KN_LanguageCodemandatoryifqualified)"))

    country = (fields.get("Country") or "").strip() or _COUNTRY_BY_CC.get(
        (fields.get("CountryCode") or "").strip().upper(), "")
    if create_new and (_blank(fields.get("Street"))
                       or (_blank(fields.get("Country")) and _blank(fields.get("CountryCode")))):
        fails.append(Failure(
            "address_create_new",
            f"Street={fields.get('Street')!r} Country={fields.get('Country')!r}",
            "Street + Country required to create the Account "
            "(KN_ConvertedLeadAddressValidation)"))

    email = (fields.get("Email") or "").strip().lower()
    if email and "@" in email and not _is_true(fields.get("HasOptedOutOfEmail")):
        token = email.split("@", 1)[1].split(".", 1)[0]
        if token and token in _FREEMAIL_BLOB:
            fails.append(Failure("freemail_email", email,
                                 "private email blocks conversion unless opted out "
                                 "(KN_LeadValidatePrivateMail)"))

    ecommerce = (fields.get("LeadSource") or "").strip() == "E-commerce"
    phone = (fields.get("Phone") or "").strip()
    if phone and not ecommerce and not _PHONE_RE.fullmatch(phone):
        fails.append(Failure("phone_format", phone,
                             "Phone must be + followed by digits only (KN_ValidatePhone)"))

    street = fields.get("Street") or ""
    if len(street) > 100:
        fails.append(Failure("street_length", street[:40] + "...",
                             "Street exceeds 100 characters (KN_Addresstoolong)"))

    postal = (fields.get("PostalCode") or "").strip()
    expected = _POSTAL_LEN_BY_COUNTRY.get(country)
    if postal and not ecommerce and expected is not None and len(postal) != expected:
        fails.append(Failure("postal_length", postal,
                             f"PostalCode must be {expected} chars for {country} "
                             "(KN_Checklengthpostalcode)"))
    return fails
```

(`csv`, `Path`, `get_logger`, `PREFLIGHT_CSV_COLUMNS` are used by Task 5's additions to this file.)

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_preflight.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/leadauto/convert/preflight.py tests/test_preflight.py
git commit -m "feat(convert): pure preflight checks mirroring the org's conversion rules"
```

---

### Task 5: Live preflight — query, run, CSV report

**Files:**
- Modify: `src/leadauto/sf/soql.py` (add `leads_preflight` after `leads_by_ids`)
- Modify: `src/leadauto/convert/preflight.py` (add `run_preflight`, `write_failures_csv`)
- Test: `tests/test_preflight.py`, `tests/test_soql.py` (append)

**Interfaces:**
- Consumes: Task 4 `check_lead`; `ConversionTarget` (existing, has `.lead_id`/`.account_id`); `SfClient.query(soql) -> list[dict]`.
- Produces: `soql.leads_preflight(ids) -> str`; `run_preflight(client, targets, logger=None) -> tuple[list[ConversionTarget], list[dict]]` (dict keys = `PREFLIGHT_CSV_COLUMNS`); `write_failures_csv(failures, path) -> Path`. Task 6 consumes all three.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_soql.py`:

```python
import pytest
from leadauto.sf.soql import leads_preflight


def test_leads_preflight_no_owner_or_converted_filter():
    soql = leads_preflight(["00Q1", "00Q2"])
    assert "Owner.Type" not in soql and "IsConverted =" not in soql
    assert "'00Q1', '00Q2'" in soql
    assert "HasOptedOutOfEmail" in soql and "Country" in soql


def test_leads_preflight_requires_ids():
    with pytest.raises(ValueError):
        leads_preflight([])
```

Append to `tests/test_preflight.py`:

```python
from pathlib import Path

from leadauto.convert import ConversionTarget
from leadauto.convert.preflight import run_preflight, write_failures_csv


class FakeClient:
    def __init__(self, rows):
        self.rows = rows
        self.queries = []

    def query(self, soql):
        self.queries.append(soql)
        return self.rows


def test_run_preflight_splits_ok_and_failures():
    rows = [dict(GOOD, Id="00Q_OK"), dict(GOOD, Id="00Q_BAD", KN_Employee__c="")]
    targets = [ConversionTarget("00Q_OK"), ConversionTarget("00Q_BAD")]
    ok, failures = run_preflight(FakeClient(rows), targets)
    assert [t.lead_id for t in ok] == ["00Q_OK"]
    assert failures == [{"lead_id": "00Q_BAD", "check": "employee_category", "value": "",
                         "detail": "KN_Employee__c blank (KN_EmployeeCategoryMandatory)"}]


def test_run_preflight_create_new_depends_on_account_id():
    rows = [dict(GOOD, Id="00Q1", Street="")]
    linked = [ConversionTarget("00Q1", account_id="001X")]
    ok, failures = run_preflight(FakeClient(rows), linked)
    assert ok and not failures  # linked account -> no address requirement


def test_run_preflight_missing_lead_reported():
    ok, failures = run_preflight(FakeClient([]), [ConversionTarget("00QGONE")])
    assert not ok
    assert failures[0]["check"] == "not_found"


def test_run_preflight_empty_targets_no_query():
    client = FakeClient([])
    assert run_preflight(client, []) == ([], [])
    assert client.queries == []


def test_write_failures_csv(tmp_path):
    path = write_failures_csv(
        [{"lead_id": "00Q1", "check": "language", "value": "", "detail": "d"}],
        tmp_path / "preflight_failures.csv")
    text = Path(path).read_text(encoding="utf-8")
    assert text.splitlines()[0] == "lead_id,check,value,detail"
    assert "00Q1,language" in text
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_preflight.py tests/test_soql.py -q`
Expected: FAIL — names not defined

- [ ] **Step 3: Implement**

`src/leadauto/sf/soql.py` — after `leads_by_ids`:

```python
def leads_preflight(ids: Sequence[str]) -> str:
    """Current conversion-relevant state of the given Leads (Stage-2 preflight).

    No owner or IsConverted filter: already-converted or re-owned leads must come back
    so the preflight can REPORT them instead of silently dropping them.
    """
    if not ids:
        raise ValueError("leads_preflight requires at least one Id")
    return f"SELECT {_LEAD_FIELDS} FROM Lead WHERE Id IN ({_in_list(ids)})"
```

`src/leadauto/convert/preflight.py` — append:

```python
def run_preflight(client, targets, logger=None):
    """Split targets into (convertible, failure-records) against live Lead state.

    One bulk query; each failing target yields one row per failed check
    (keys = PREFLIGHT_CSV_COLUMNS). create-new is assumed when the target
    has no account_id.
    """
    log = logger or get_logger("convert.preflight")
    if not targets:
        return [], []
    from ..sf import soql  # local import: keep module import-light for qa_artifact
    rows = client.query(soql.leads_preflight([t.lead_id for t in targets]))
    by_id = {row.get("Id"): row for row in rows}
    ok, failures = [], []
    for t in targets:
        row = by_id.get(t.lead_id)
        if row is None:
            failures.append({"lead_id": t.lead_id, "check": "not_found", "value": "",
                             "detail": "Lead not returned by the org (deleted or inaccessible)"})
            continue
        fails = check_lead(row, create_new=t.account_id is None)
        if fails:
            failures.extend({"lead_id": t.lead_id, "check": f.check,
                             "value": f.value, "detail": f.detail} for f in fails)
        else:
            ok.append(t)
    log.info("Preflight: %d/%d target(s) pass, %d failure record(s)",
             len(ok), len(targets), len(failures))
    return ok, failures


def write_failures_csv(failures: list[dict], path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=PREFLIGHT_CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(failures)
    return p
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_preflight.py tests/test_soql.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/leadauto/sf/soql.py src/leadauto/convert/preflight.py tests/test_preflight.py tests/test_soql.py
git commit -m "feat(convert): live preflight query + skip/report split + CSV"
```

---

### Task 6: Stage-2 wiring — run_stage2, CLI, workflow artifact

**Files:**
- Modify: `src/leadauto/pipeline/stage2_convert.py` (preflight call, `Stage2Result.preflight_failures`)
- Modify: `src/leadauto/cli.py:92-116` (stage2 command: `--preflight/--no-preflight`, summary)
- Modify: `.github/workflows/stage2.yml:93-99` (upload CSV with the log)
- Test: `tests/test_pipeline.py`, `tests/test_cli.py` (append)

**Interfaces:**
- Consumes: Task 5 `run_preflight` / `write_failures_csv`.
- Produces: `run_stage2(..., preflight: bool = True, failures_csv: str | Path = "preflight_failures.csv")`; `Stage2Result.preflight_failures: list[dict]`.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_pipeline.py` (reuse that file's existing fake-client/settings fixtures if equivalent ones exist; otherwise use these self-contained fakes):

```python
import csv as _csv

from leadauto.pipeline.stage2_convert import run_stage2

_GOOD_ROW = {
    "IsConverted": False, "KN_Employee__c": "11-50", "KN_DBID__c": "",
    "KN_NoOwnDBID__c": "true", "KN_Language__c": "German", "Street": "Weg 1",
    "Country": "Germany", "CountryCode": "DE", "PostalCode": "20095",
    "Phone": "", "Email": "info@acme.de", "HasOptedOutOfEmail": False, "LeadSource": "Web",
}


class _PreflightSfFake:
    """query() answers the preflight SOQL; apex_run records conversions."""
    def __init__(self, rows):
        self.rows = rows
        self.apex_calls = []

    def query(self, soql):
        return self.rows

    def apex_run(self, apex):
        self.apex_calls.append(apex)
        return '@@LEADAUTO@@[{"leadId": "00Q_OK", "success": true}]@@LEADAUTO@@'


class _Settings:
    dry_run = False
    owner_id = "005X"
    converted_status = "Qualified"


def _approved_csv(tmp_path, rows):
    p = tmp_path / "approved_x.csv"
    cols = ["lead_id", "account_match_id", "contact_match_id", "contact_reason", "Approve?"]
    with p.open("w", newline="", encoding="utf-8") as fh:
        w = _csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    return p


def test_stage2_preflight_excludes_failing_leads(tmp_path):
    approved = _approved_csv(tmp_path, [
        {"lead_id": "00Q_OK", "Approve?": "yes"},
        {"lead_id": "00Q_BAD", "Approve?": "yes"},
    ])
    client = _PreflightSfFake([dict(_GOOD_ROW, Id="00Q_OK"),
                               dict(_GOOD_ROW, Id="00Q_BAD", KN_Language__c="")])
    res = run_stage2(client, _Settings(), approved,
                     failures_csv=tmp_path / "preflight_failures.csv")
    assert [t.lead_id for t in res.targets] == ["00Q_OK"]
    assert res.preflight_failures[0]["lead_id"] == "00Q_BAD"
    assert "00Q_BAD" not in "".join(client.apex_calls)
    assert (tmp_path / "preflight_failures.csv").exists()


def test_stage2_no_preflight_bypasses(tmp_path):
    approved = _approved_csv(tmp_path, [{"lead_id": "00Q_BAD", "Approve?": "yes"}])
    client = _PreflightSfFake([])
    res = run_stage2(client, _Settings(), approved, preflight=False,
                     failures_csv=tmp_path / "pf.csv")
    assert [t.lead_id for t in res.targets] == ["00Q_BAD"]
    assert res.preflight_failures == []
    assert not (tmp_path / "pf.csv").exists()


def test_stage2_dry_run_still_preflights(tmp_path):
    approved = _approved_csv(tmp_path, [{"lead_id": "00Q_BAD", "Approve?": "yes"}])
    client = _PreflightSfFake([dict(_GOOD_ROW, Id="00Q_BAD", KN_Employee__c="")])
    res = run_stage2(client, _Settings(), approved, dry_run=True,
                     failures_csv=tmp_path / "pf.csv")
    assert res.preflight_failures and not client.apex_calls
```

Append to `tests/test_cli.py` (match that file's existing invocation pattern — it uses `typer.testing.CliRunner` and monkeypatched stage functions; assert the new option exists and the summary line):

```python
def test_stage2_cli_reports_preflight_skips(monkeypatch, tmp_path):
    from leadauto import cli
    from leadauto.pipeline.stage2_convert import Stage2Result

    captured = {}

    def fake_run_stage2(sf, settings, approved, *, dry_run=None, batch_size=50,
                        preflight=True, failures_csv="preflight_failures.csv", logger=None):
        captured["preflight"] = preflight
        return Stage2Result(targets=[], result={"converted": 0, "failed": 0},
                            dry_run=True,
                            preflight_failures=[{"lead_id": "00Q1", "check": "language",
                                                 "value": "", "detail": "d"}])

    monkeypatch.setattr(cli, "run_stage2", fake_run_stage2)
    monkeypatch.setattr(cli, "_build_sf_client", lambda s, l: object())
    approved = tmp_path / "approved_1.csv"
    approved.write_text("lead_id,Approve?\n00Q1,yes\n", encoding="utf-8")

    from typer.testing import CliRunner
    result = CliRunner().invoke(cli.app, ["stage2", "--approved", str(approved),
                                          "--no-preflight"])
    assert result.exit_code == 0
    assert captured["preflight"] is False
    assert "preflight_skipped=1" in result.output
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_pipeline.py tests/test_cli.py -q`
Expected: FAIL — unexpected keyword `preflight` / missing attribute `preflight_failures`

- [ ] **Step 3: Implement**

`src/leadauto/pipeline/stage2_convert.py`:

```python
# add import
from ..convert.preflight import run_preflight, write_failures_csv

# Stage2Result gains:
@dataclass
class Stage2Result:
    targets: list[ConversionTarget] = field(default_factory=list)
    result: dict = field(default_factory=dict)
    dry_run: bool = False
    preflight_failures: list[dict] = field(default_factory=list)

# run_stage2 signature + body:
def run_stage2(
    sf_client, settings, approved_csv: str | Path, *,
    dry_run: bool | None = None, batch_size: int = 50,
    preflight: bool = True, failures_csv: str | Path = "preflight_failures.csv",
    logger=None,
) -> Stage2Result:
    log = logger or get_logger("pipeline.stage2")
    dry_run = settings.dry_run if dry_run is None else dry_run
    rows = read_approved(approved_csv)
    targets = targets_from_approved(rows, settings)
    pf_failures: list[dict] = []
    if preflight and targets:
        targets, pf_failures = run_preflight(sf_client, targets, logger=log)
        path = write_failures_csv(pf_failures, failures_csv)
        if pf_failures:
            log.warning("Preflight skipped %d lead(s); details -> %s",
                        len({f['lead_id'] for f in pf_failures}), path)
    log.info("Stage 2: %d approved row(s) -> %d conversion target(s) (dry_run=%s)",
             len(rows), len(targets), dry_run)
    result = convert_leads(sf_client, targets, settings,
                           dry_run=dry_run, batch_size=batch_size, logger=log)
    return Stage2Result(targets=targets, result=result, dry_run=dry_run,
                        preflight_failures=pf_failures)
```

Also update the module docstring's last sentence to mention the preflight: `A read-only preflight (convert/preflight.py) re-checks each approved lead's current field state against the org's conversion rules and skips+reports failures before any Apex runs.`

Note the CSV is written whenever preflight runs (even with zero failures) so the CI artifact always shows preflight ran. Adjust `test_stage2_no_preflight_bypasses` expectations accordingly — it asserts the file does NOT exist only in the `--no-preflight` case, which stays correct.

`src/leadauto/cli.py` stage2 command — add option and summary:

```python
    preflight: bool = typer.Option(True, "--preflight/--no-preflight",
                                   help="Check conversion requirements and skip failing leads."),
```

pass through: `res = run_stage2(sf, settings, approved, dry_run=dry_run, batch_size=batch_size, preflight=preflight, logger=log)` and extend the echo:

```python
    skipped_leads = len({f["lead_id"] for f in res.preflight_failures})
    typer.echo(
        f"Stage 2 {'(dry-run) ' if res.dry_run else ''}done: "
        f"{len(res.targets)} target(s) | converted={res.result.get('converted', 0)} "
        f"failed={res.result.get('failed', 0)} | preflight_skipped={skipped_leads}"
    )
```

`.github/workflows/stage2.yml` — replace the upload step's `path`:

```yaml
          path: |
            stage2_log.txt
            preflight_failures.csv
```

- [ ] **Step 4: Run the full suite**

Run: `python -m pytest -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/leadauto/pipeline/stage2_convert.py src/leadauto/cli.py .github/workflows/stage2.yml tests/test_pipeline.py tests/test_cli.py
git commit -m "feat(stage2): preflight gate — skip failing leads, report CSV, CLI flag"
```

---

### Task 7: QA Review sheet `convert_blockers` column

**Files:**
- Modify: `src/leadauto/qa_artifact.py:19-29` (`REVIEW_COLUMNS`), `:45-69` (`_review_row`), new `_convert_blockers`
- Test: `tests/test_qa_artifact.py` (append)

**Interfaces:**
- Consumes: Task 4 `check_lead`, Task 3 `lead_field_payload`, Task 2 `lead_*` fields; `MatchResult` (existing: `.enriched`, `.account`, `.rule`, `.evidence`, `.decision`, `.contact`).
- Produces: Review sheet column `convert_blockers` (between `contact_reason` and `decision`).

- [ ] **Step 1: Write the failing test** — append to `tests/test_qa_artifact.py` (reuse that file's existing result-factory helpers if present; otherwise this self-contained version):

```python
from leadauto.extract.models import ExtractedLead
from leadauto.leadfeeder.enrich import EnrichedLead
from leadauto.match import MatchResult
from leadauto.qa_artifact import REVIEW_COLUMNS, build_workbook


def _result(extracted, account=None):
    return MatchResult(enriched=EnrichedLead(extracted=extracted), account=account,
                       contact=None, decision="review", rule="no_lf_match", evidence=[])


def test_convert_blockers_column_lists_failed_checks():
    e = ExtractedLead(lead_id="00Q1", task_id="00T1", task_type="claimer",
                      lead_last_name="Muster")  # everything conversion-relevant blank
    wb = build_workbook([_result(e)], skipped=[])
    ws = wb["Review"]
    header = [c.value for c in ws[1]]
    assert "convert_blockers" in header
    assert header.index("convert_blockers") < header.index("decision")
    blockers = ws.cell(row=2, column=header.index("convert_blockers") + 1).value
    assert "employee_category" in blockers and "language" in blockers
    assert "address_create_new" in blockers  # no account match -> create-new assumption


def test_convert_blockers_empty_when_payload_fills_gaps():
    e = ExtractedLead(lead_id="00Q1", task_id="00T1", task_type="claimer",
                      lead_last_name="Muster", lead_language="German",
                      lead_source="Web", street="Weg 1", city="HH",
                      postal_code="20095", country_code="DE",
                      company_size="11-50")
    wb = build_workbook([_result(e)], skipped=[])
    ws = wb["Review"]
    header = [c.value for c in ws[1]]
    blockers = ws.cell(row=2, column=header.index("convert_blockers") + 1).value
    assert not blockers  # write-back payload satisfies employee/dbid/address checks
```

(If `MatchResult`'s constructor differs, mirror the construction used by existing tests in `tests/test_qa_artifact.py` — keep the assertions identical.)

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_qa_artifact.py -q`
Expected: FAIL — `convert_blockers` not in header

- [ ] **Step 3: Implement** — in `src/leadauto/qa_artifact.py`:

Imports:

```python
from .convert.preflight import check_lead
from .writeback import lead_field_payload
```

`REVIEW_COLUMNS`: insert `"convert_blockers",` on its own line between `"contact_match_id", "contact_score", "contact_reason",` and `"decision", "Approve?",`.

Add before `_review_row`:

```python
def _convert_blockers(result) -> str:
    """Preview of stage-2 preflight over the lead's post-write-back state.

    Current lead values overlaid with the write-back payload (fill-only-blank, so the
    overlay IS the post-run state). No proposed account match -> strictest create-new
    assumption. Empty string = would pass preflight today.
    """
    e = result.enriched.extracted
    fields = {
        "IsConverted": False,
        "Street": e.lead_street, "City": e.lead_city, "PostalCode": e.lead_postal_code,
        "Country": e.lead_country, "CountryCode": e.lead_country_code,
        "Phone": e.lead_phone, "Email": e.lead_email,
        "HasOptedOutOfEmail": e.lead_opted_out_email, "LeadSource": e.lead_source,
        "KN_DBID__c": e.lead_dbid, "KN_NoOwnDBID__c": e.lead_no_own_dbid,
        "KN_Employee__c": e.lead_employee_category, "KN_Language__c": e.lead_language,
    }
    payload = lead_field_payload(result.enriched)
    fields.update((k, v) for k, v in payload.items() if k != "Id")
    fails = check_lead(fields, create_new=result.account is None)
    return "; ".join(f.check for f in fails)
```

In `_review_row`'s returned dict add, right before `"decision": result.decision,`:

```python
        "convert_blockers": _convert_blockers(result),
```

- [ ] **Step 4: Run the full suite** (qa artifact tests assert column lists)

Run: `python -m pytest -q`
Expected: any pre-existing test asserting the exact `REVIEW_COLUMNS` list fails — update it to include `convert_blockers` in the right position. Then PASS.

- [ ] **Step 5: Commit**

```bash
git add src/leadauto/qa_artifact.py tests/test_qa_artifact.py
git commit -m "feat(qa): convert_blockers preview column on the Review sheet"
```

---

### Task 8: Smoke tests, docs, memory

**Files:**
- Modify: `src/leadauto/convert/CLAUDE.md`, `CLAUDE.md`
- Modify: `C:\Users\wasil\.claude\projects\C--Users-wasil-OneDrive---kununu-Lead-Automation\memory\stage2-live-testing-plan.md` (memory, not committed to repo)

**Interfaces:** none (verification + documentation).

- [ ] **Step 1: Full suite green**

Run: `python -m pytest -q`
Expected: PASS, count > 219

- [ ] **Step 2: Read-only smoke tests against the live org**

```powershell
$env:PYTHONPATH = "src"; python -m leadauto stage1 --dry-run --no-leadfeeder --limit 3
$env:PYTHONPATH = "src"; python -m leadauto stage2 --dry-run
```

Expected: stage1 completes, QA artifact has the `convert_blockers` column populated (open `qa_artifact.xlsx`, Review sheet); stage2 runs its preflight query and prints `preflight_skipped=N` (uses the newest committed `review/approved_*.csv`; if none exists, the "No approved_*.csv" error is the expected outcome — note it and move on). Neither writes anything (dry-run).

- [ ] **Step 3: Update docs**

`src/leadauto/convert/CLAUDE.md`:
- In "Flow", after item 2 add: `3. run_preflight (convert/preflight.py) re-queries the approved leads' current state and drops any lead failing the "Conversion requirements" below — failures land in preflight_failures.csv (uploaded with the stage-2 log) and Stage2Result.preflight_failures; --no-preflight bypasses.` (renumber the old 3/4 to 4/5).
- In "Safety model", replace the `owner_id` bullet with: `Config owner_id stays Wasil's user DELIBERATELY (decision 2026-07-05) — converted records are assigned to him at setOwnerId time; queue ownership ends at conversion.`
- In "Conversion requirements", append one line: `Stage-1 write-back now fills KN_NoOwnDBID__c / address (CountryCode) / KN_Employee__c fallback / FirstName / Phone (fill-only-blank; spec docs/superpowers/specs/2026-07-05-writeback-gaps-and-stage2-preflight-design.md), and the QA Review sheet previews these checks in convert_blockers.`

Root `CLAUDE.md`:
- "Leads scope" bullet: replace `Config owner_id = Wasil (placeholder — swap to the integration user before live conversions).` with `Config owner_id = Wasil (deliberate, 2026-07-05 — converted records are assigned to him).`
- "Status": add `- **Write-back fills conversion requirements** (NoOwnDBID tick, address via CountryCode, employee fallback, FirstName, Phone — fill-only-blank); **Stage 2 preflights** approved leads and skips+reports rule violations (preflight_failures.csv).`
- "Open items" item 1: drop `swap owner_id`.

Memory file `stage2-live-testing-plan.md`: remove the `Swap config owner_id...` prerequisite line; add `- owner_id stays Wasil deliberately (2026-07-05).` Keep the Environment prerequisite.

- [ ] **Step 4: Commit**

```bash
git add src/leadauto/convert/CLAUDE.md CLAUDE.md
git commit -m "docs: preflight + write-back conversion fields; owner_id stays Wasil deliberately"
```

- [ ] **Step 5: Push and verify**

```bash
git push origin master
git log --oneline -8
```

Expected: all task commits on master, working tree clean (`git status --short` empty except untracked local artifacts).
