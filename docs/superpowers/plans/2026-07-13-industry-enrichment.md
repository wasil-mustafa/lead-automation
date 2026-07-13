# Industry (Branche) Enrichment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Populate the Salesforce `Lead.Industry` ("Branche") picklist from the lead's kununu profile industry (primary) or a mapped LeadFeeder industry (fallback).

**Architecture:** A new pure module `industry.py` holds two static rollup tables — kununu's 44 German fine categories → the 20-value SF picklist, and a small LeadFeeder→SF seed — plus a resolver. `writeback.lead_field_payload` calls the resolver: the kununu-profile value **overwrites** `Industry`; the LeadFeeder value **fills only when blank**; an unmapped value writes nothing and logs a warning. Thin plumbing carries `profile_industry_name_de` from the DWH feed through `EnrichedLead.feed_industry`, and adds `Industry` to the Lead SOQL projection for the fill-only-blank check and QA/shadow visibility.

**Tech Stack:** Python 3, pytest (pythonpath=src via pyproject.toml), stdlib `unicodedata`/`logging`. No new dependencies.

## Global Constraints

- Tests run with `python -m pytest -q` (config sets `pythonpath=src`). Full suite must stay green.
- Never write a value that is not one of the 20 exact SF picklist labels, or nothing. The picklist is **not restricted**, so off-list writes do not fail — they silently pollute reporting.
- The 20 valid target labels (verbatim, org `headless`):
  `AUTOMOTIVE and VEHICLE MANUFACTURING`, `BANKING and INSURANCE`, `CONSTRUCTION and MINING`, `CONSULTING, AUDITING and LAW`, `ENERGY, WATER and ENVIRONMENT`, `EDUCATION, TRAINING and SCIENCE`, `HEALTHCARE and SOCIAL SERVICES`, `HOTEL and CATERING`, `INDUSTRY and MECHANICAL ENGINEERING`, `INTERNET and INFORMATION TECHNOLOGY`, `CONSUMER GOODS and RETAIL`, `CULTURE, SPORTS and LEISURE`, `MARKETING, PR and DESIGN`, `MEDIA and PUBLISHING`, `PUBLIC SERVICE, ASSOCIATIONS and INSTITUTIONS`, `HR SERVICES`, `PHARMACEUTICALS and MEDICAL TECHNOLOGY`, `TELECOMMUNICATION`, `TRANSPORT and LOGISTICS`, `OTHER`.
- Write policy: kununu source **overwrites**; LeadFeeder source **fill-only-blank**; unmapped → leave blank + `logging` WARNING.
- German table keys are UTF-8 (umlauts: `Öffentliche Verwaltung`, `Steuerberatung/Wirtschaftsprüfung`; `&` in `Personalwesen & -beschaffung`). All lookups go through `_norm` (NFC + strip + casefold) on both table keys and inputs.
- Follow existing patterns: static dict like `writeback._EMPLOYEE_BINS`; loggers via `leadauto.logging.get_logger`.

---

### Task 1: `industry.py` — rollup tables + resolver (pure)

**Files:**
- Create: `src/leadauto/industry.py`
- Test: `tests/test_industry.py`

**Interfaces:**
- Consumes: `leadauto.logging.get_logger`.
- Produces:
  - `KUNUNU_TO_SF: dict[str, str]` (44 exact German keys → SF label)
  - `LF_TO_SF: dict[str, str]` (normalized LeadFeeder names → SF label, seed)
  - `VALID_SF_INDUSTRIES: frozenset[str]` (the 20 labels)
  - `sf_industry_from_kununu(name: str | None) -> str | None`
  - `sf_industry_from_leadfeeder(industry_field: str | None) -> str | None`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_industry.py`:

```python
"""industry.py: kununu/LeadFeeder industry -> SF Industry ('Branche') picklist."""
import logging

from leadauto.industry import (
    KUNUNU_TO_SF,
    LF_TO_SF,
    VALID_SF_INDUSTRIES,
    sf_industry_from_kununu,
    sf_industry_from_leadfeeder,
)


def test_all_44_kununu_values_present():
    # The feed scan (2026-07-13) found exactly these 44 distinct
    # profile_industry_name_de values; every one must map.
    assert len(KUNUNU_TO_SF) == 44


def test_every_kununu_target_is_a_valid_sf_label():
    for target in KUNUNU_TO_SF.values():
        assert target in VALID_SF_INDUSTRIES


def test_every_lf_target_is_a_valid_sf_label():
    for target in LF_TO_SF.values():
        assert target in VALID_SF_INDUSTRIES


def test_kununu_exact_hits():
    assert sf_industry_from_kununu("IT") == "INTERNET and INFORMATION TECHNOLOGY"
    assert sf_industry_from_kununu("Banken") == "BANKING and INSURANCE"
    assert sf_industry_from_kununu("Sonstige Branchen") == "OTHER"


def test_kununu_normalizes_case_whitespace_and_umlauts():
    assert sf_industry_from_kununu("  it  ") == "INTERNET and INFORMATION TECHNOLOGY"
    # umlaut key must resolve regardless of surrounding whitespace/case
    assert sf_industry_from_kununu("öffentliche verwaltung") == \
        "PUBLIC SERVICE, ASSOCIATIONS and INSTITUTIONS"
    assert sf_industry_from_kununu("Steuerberatung/Wirtschaftsprüfung") == \
        "CONSULTING, AUDITING and LAW"


def test_kununu_blank_and_none_return_none_without_logging(caplog):
    caplog.set_level(logging.WARNING)
    assert sf_industry_from_kununu(None) is None
    assert sf_industry_from_kununu("") is None
    assert sf_industry_from_kununu("   ") is None
    assert caplog.records == []


def test_kununu_unknown_value_returns_none_and_logs(caplog):
    caplog.set_level(logging.WARNING)
    assert sf_industry_from_kununu("Raumfahrt-Zeugs") is None
    assert any("Raumfahrt-Zeugs" in r.getMessage() for r in caplog.records)


def test_leadfeeder_first_mappable_token_wins():
    # LF joins multiple industries with '; '; the first token that maps wins.
    assert sf_industry_from_leadfeeder("Software; IT") == \
        "INTERNET and INFORMATION TECHNOLOGY"
    assert sf_industry_from_leadfeeder("Business Consulting and Services") == \
        "CONSULTING, AUDITING and LAW"


def test_leadfeeder_unknown_returns_none_and_logs(caplog):
    caplog.set_level(logging.WARNING)
    assert sf_industry_from_leadfeeder("Underwater Basket Weaving") is None
    assert any("Underwater Basket Weaving" in r.getMessage() for r in caplog.records)


def test_leadfeeder_blank_and_none_return_none_without_logging(caplog):
    caplog.set_level(logging.WARNING)
    assert sf_industry_from_leadfeeder(None) is None
    assert sf_industry_from_leadfeeder("") is None
    assert caplog.records == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_industry.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'leadauto.industry'`.

- [ ] **Step 3: Write the module**

Create `src/leadauto/industry.py`:

```python
"""Map a company's industry onto the SF `Lead.Industry` ('Branche') picklist.

Two levels of kununu's own taxonomy plus a LeadFeeder fallback resolve to the org's
20-value picklist (NOT restricted — an off-list write silently pollutes reporting, so
we only ever emit one of the 20 labels or nothing):

  * `KUNUNU_TO_SF` — the kununu profile feed's `profile_industry_name_de` (44 German
    fine categories, snapshot 2026-07-13) rolled up to the 20 English buckets. Primary.
  * `LF_TO_SF` — LeadFeeder's LinkedIn-taxonomy industry names. Fallback only (the
    kununu-first path covers ~every lead), so this is seeded from observed values and
    grows from QA evidence; an unmapped value maps to nothing.

Lookups normalize (NFC + strip + casefold) on both sides so case/whitespace/normal-form
never causes a silent miss. A non-empty source value that maps to nothing logs a WARNING
so new/unknown categories surface; an empty value is silent.
"""
from __future__ import annotations

import unicodedata

from .logging import get_logger

log = get_logger("industry")

VALID_SF_INDUSTRIES = frozenset({
    "AUTOMOTIVE and VEHICLE MANUFACTURING", "BANKING and INSURANCE",
    "CONSTRUCTION and MINING", "CONSULTING, AUDITING and LAW",
    "ENERGY, WATER and ENVIRONMENT", "EDUCATION, TRAINING and SCIENCE",
    "HEALTHCARE and SOCIAL SERVICES", "HOTEL and CATERING",
    "INDUSTRY and MECHANICAL ENGINEERING", "INTERNET and INFORMATION TECHNOLOGY",
    "CONSUMER GOODS and RETAIL", "CULTURE, SPORTS and LEISURE",
    "MARKETING, PR and DESIGN", "MEDIA and PUBLISHING",
    "PUBLIC SERVICE, ASSOCIATIONS and INSTITUTIONS", "HR SERVICES",
    "PHARMACEUTICALS and MEDICAL TECHNOLOGY", "TELECOMMUNICATION",
    "TRANSPORT and LOGISTICS", "OTHER",
})

# kununu profile_industry_name_de (44) -> SF bucket. Rollup approved 2026-07-13
# (see docs/superpowers/specs/2026-07-13-industry-enrichment-design.md).
KUNUNU_TO_SF: dict[str, str] = {
    "Automobil": "AUTOMOTIVE and VEHICLE MANUFACTURING",
    "Finanz": "BANKING and INSURANCE",
    "Banken": "BANKING and INSURANCE",
    "Versicherung": "BANKING and INSURANCE",
    "Bau/Architektur": "CONSTRUCTION and MINING",
    "Handwerk": "CONSTRUCTION and MINING",
    "Beratung/Consulting": "CONSULTING, AUDITING and LAW",
    "Steuerberatung/Wirtschaftsprüfung": "CONSULTING, AUDITING and LAW",
    "Recht": "CONSULTING, AUDITING and LAW",
    "Energie": "ENERGY, WATER and ENVIRONMENT",
    "Bildung": "EDUCATION, TRAINING and SCIENCE",
    "Forschung/Entwicklung": "EDUCATION, TRAINING and SCIENCE",
    "Seminare/Messen": "EDUCATION, TRAINING and SCIENCE",
    "Gesundheit/Soziales/Pflege": "HEALTHCARE and SOCIAL SERVICES",
    "Gastronomie": "HOTEL and CATERING",
    "Hotels/Beherbergung": "HOTEL and CATERING",
    "Industrie": "INDUSTRY and MECHANICAL ENGINEERING",
    "Maschinenbau": "INDUSTRY and MECHANICAL ENGINEERING",
    "Elektro/Elektronik": "INDUSTRY and MECHANICAL ENGINEERING",
    "Chemie": "INDUSTRY and MECHANICAL ENGINEERING",
    "Textil": "INDUSTRY and MECHANICAL ENGINEERING",
    "Druck/Verpackung": "INDUSTRY and MECHANICAL ENGINEERING",
    "IT": "INTERNET and INFORMATION TECHNOLOGY",
    "Internet": "INTERNET and INFORMATION TECHNOLOGY",
    "Handel": "CONSUMER GOODS and RETAIL",
    "Nahrungsmittel/Landwirtschaft": "CONSUMER GOODS and RETAIL",
    "Kunst/Kultur": "CULTURE, SPORTS and LEISURE",
    "Sport/Beauty": "CULTURE, SPORTS and LEISURE",
    "Tourismus": "CULTURE, SPORTS and LEISURE",
    "Marketing/Werbung/PR": "MARKETING, PR and DESIGN",
    "Marktforschung": "MARKETING, PR and DESIGN",
    "Medien": "MEDIA and PUBLISHING",
    "Öffentliche Verwaltung": "PUBLIC SERVICE, ASSOCIATIONS and INSTITUTIONS",
    "Vereine": "PUBLIC SERVICE, ASSOCIATIONS and INSTITUTIONS",
    "Administration/Verwaltung": "PUBLIC SERVICE, ASSOCIATIONS and INSTITUTIONS",
    "Personalwesen & -beschaffung": "HR SERVICES",
    "Einkauf": "HR SERVICES",
    "Medizin/Pharma": "PHARMACEUTICALS and MEDICAL TECHNOLOGY",
    "Medizintechnik": "PHARMACEUTICALS and MEDICAL TECHNOLOGY",
    "Telekommunikation": "TELECOMMUNICATION",
    "Transport/Verkehr/Logistik": "TRANSPORT and LOGISTICS",
    "Sonstige Branchen": "OTHER",
    "Dienstleistung": "OTHER",
    "Immobilien": "OTHER",
}

# LeadFeeder (LinkedIn taxonomy) -> SF bucket. Seed; extend as new lf_industry
# values appear in QA. Keys are pre-normalized (lowercase) for readability.
LF_TO_SF: dict[str, str] = {
    "business consulting and services": "CONSULTING, AUDITING and LAW",
    "management consulting": "CONSULTING, AUDITING and LAW",
    "legal services": "CONSULTING, AUDITING and LAW",
    "law practice": "CONSULTING, AUDITING and LAW",
    "accounting": "CONSULTING, AUDITING and LAW",
    "software": "INTERNET and INFORMATION TECHNOLOGY",
    "software development": "INTERNET and INFORMATION TECHNOLOGY",
    "computer software": "INTERNET and INFORMATION TECHNOLOGY",
    "it services and it consulting": "INTERNET and INFORMATION TECHNOLOGY",
    "information technology & services": "INTERNET and INFORMATION TECHNOLOGY",
    "it": "INTERNET and INFORMATION TECHNOLOGY",
    "internet": "INTERNET and INFORMATION TECHNOLOGY",
    "retail": "CONSUMER GOODS and RETAIL",
}


def _norm(s: str) -> str:
    return unicodedata.normalize("NFC", s).strip().casefold()


_KUNUNU_NORM = {_norm(k): v for k, v in KUNUNU_TO_SF.items()}
_LF_NORM = {_norm(k): v for k, v in LF_TO_SF.items()}


def sf_industry_from_kununu(name: str | None) -> str | None:
    """SF bucket for a kununu `profile_industry_name_de`, or None (logs unknown)."""
    if not name or not name.strip():
        return None
    hit = _KUNUNU_NORM.get(_norm(name))
    if hit is None:
        log.warning("unmapped kununu industry %r (add to industry.KUNUNU_TO_SF)", name)
    return hit


def sf_industry_from_leadfeeder(industry_field: str | None) -> str | None:
    """SF bucket for a LeadFeeder industry string. LF joins multiple with '; ';
    the first token that maps wins. None if none map (logs the raw field)."""
    if not industry_field or not industry_field.strip():
        return None
    for token in industry_field.split(";"):
        hit = _LF_NORM.get(_norm(token))
        if hit is not None:
            return hit
    log.warning("unmapped LeadFeeder industry %r (add to industry.LF_TO_SF)", industry_field)
    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_industry.py -q`
Expected: PASS (10 tests).

- [ ] **Step 5: Commit**

```bash
git add src/leadauto/industry.py tests/test_industry.py
git commit -m "feat(industry): kununu/LeadFeeder industry -> SF Branche picklist mapping"
```

---

### Task 2: kununu feed carries the profile industry

**Files:**
- Modify: `src/leadauto/kununu_feed.py` (`ProfileInfo` dataclass ~25-30; `lookup_profiles` ProfileInfo construction ~102-107)
- Test: `tests/test_kununu_feed.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `ProfileInfo.industry: str | None` (populated from `profile_industry_name_de`).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_kununu_feed.py`:

```python
def test_lookup_captures_profile_industry(tmp_path):
    feed = _write_feed(tmp_path, [_record(profile_industry_name_de="IT")])
    found = lookup_profiles(feed, uuids={"u1"}, slugs=set())
    assert found["u1"].industry == "IT"


def test_lookup_industry_absent_is_none(tmp_path):
    feed = _write_feed(tmp_path, [_record()])  # _record adds no industry key
    found = lookup_profiles(feed, uuids={"u1"}, slugs=set())
    assert found["u1"].industry is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_kununu_feed.py -k industry -q`
Expected: FAIL — `AttributeError: 'ProfileInfo' object has no attribute 'industry'`.

- [ ] **Step 3: Implement**

In `src/leadauto/kununu_feed.py`, add the field to `ProfileInfo`:

```python
@dataclass
class ProfileInfo:
    name: str
    companies: list[str] = field(default_factory=list)
    city: str | None = None
    country_code: str | None = None
    industry: str | None = None
```

And set it in `lookup_profiles` where `ProfileInfo(...)` is built:

```python
                info = ProfileInfo(
                    name=(d.get("profile_name") or "").strip(),
                    companies=companies,
                    city=d.get("profile_city"),
                    country_code=d.get("profile_country_code"),
                    industry=d.get("profile_industry_name_de"),
                )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_kununu_feed.py -q`
Expected: PASS (all, including the 2 new).

- [ ] **Step 5: Commit**

```bash
git add src/leadauto/kununu_feed.py tests/test_kununu_feed.py
git commit -m "feat(kununu_feed): capture profile_industry_name_de into ProfileInfo"
```

---

### Task 3: enrichment carries `feed_industry` onto `EnrichedLead`

**Files:**
- Modify: `src/leadauto/leadfeeder/enrich.py` (`EnrichedLead` dataclass ~24-39; phase-1 `if info:` block ~136-146)
- Test: `tests/test_enrich.py`

**Interfaces:**
- Consumes: `ProfileInfo.industry` (Task 2).
- Produces: `EnrichedLead.feed_industry: str | None`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_enrich.py` (mirrors the existing `feed_country_code` test near line 481):

```python
def test_enrich_carries_feed_industry_from_profile():
    from leadauto.kununu_feed import ProfileInfo
    fc = FakeClient()
    e = _extracted(kununu_profile_uuid="uuid-1")
    profiles = {"uuid-1": ProfileInfo(name="Acme", country_code="de", industry="IT")}
    [r] = enrich_leads(fc, [e], SETTINGS, profiles=profiles)
    assert r.feed_industry == "IT"
```

(If `_extracted` in this file has no `kununu_profile_uuid` parameter path, use the same
construction the neighbouring `feed_country_code` test uses — grep `feed_country_code`
in this file for the exact fixture and match it.)

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_enrich.py -k feed_industry -q`
Expected: FAIL — `AttributeError: 'EnrichedLead' object has no attribute 'feed_industry'`.

- [ ] **Step 3: Implement**

In `src/leadauto/leadfeeder/enrich.py`, add to `EnrichedLead` (next to `feed_country_code`):

```python
    feed_country_code: str | None = None  # kununu feed profile_country_code (sanity-gate evidence)
    feed_industry: str | None = None      # kununu feed profile_industry_name_de (write-back source)
```

And in the phase-1 loop's `if info:` block, alongside `result.feed_country_code = info.country_code`:

```python
            if info:
                result.feed_country_code = info.country_code
                result.feed_industry = info.industry
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_enrich.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/leadauto/leadfeeder/enrich.py tests/test_enrich.py
git commit -m "feat(enrich): carry kununu profile industry onto EnrichedLead.feed_industry"
```

---

### Task 4: Lead SOQL + `ExtractedLead` expose `Industry`

**Files:**
- Modify: `src/leadauto/sf/soql.py` (`_LEAD_FIELDS` ~18-22)
- Modify: `src/leadauto/extract/models.py` (`ExtractedLead`, after `lead_title` ~33)
- Modify: `src/leadauto/extract/parsers.py` (`_base` ~74-98)
- Test: `tests/test_soql.py`, `tests/test_parsers.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `ExtractedLead.lead_industry: str | None`; `Industry` present in the `leads_by_ids`/`leads_preflight` projection.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_soql.py`:

```python
def test_leads_by_ids_selects_industry():
    q = soql.leads_by_ids(["00Q000000000001AAA"])
    assert "Industry" in q
```

Add to `tests/test_parsers.py` (a claimer/ebp_trial parse already exists there — add a
focused assertion; adapt the fixture to the file's existing `parse_*` test style):

```python
def test_base_captures_lead_industry():
    from leadauto.extract.parsers import _base
    lead = {"Id": "00Q1", "Industry": "IT"}
    e = _base({"WhoId": "00Q1"}, lead, "claimer")
    assert e.lead_industry == "IT"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_soql.py -k industry tests/test_parsers.py -k lead_industry -q`
Expected: FAIL — `"Industry" in q` is False; `AttributeError: ... 'lead_industry'`.

- [ ] **Step 3: Implement**

`src/leadauto/sf/soql.py` — add `Industry` to `_LEAD_FIELDS`:

```python
_LEAD_FIELDS = (
    "Id, FirstName, LastName, Company, Email, Website, Status, IsConverted, LeadSource, "
    "HasOptedOutOfEmail, Phone, Title, Industry, Street, City, PostalCode, Country, CountryCode, "
    "KN_DBID__c, KN_NoOwnDBID__c, KN_Employee__c, KN_Language__c, "
    "KN_KununuProfileName__c, KN_KununuUserUUID__c, KN_KununuLinkProfile__c"
)
```

`src/leadauto/extract/models.py` — add field after `lead_title`:

```python
    lead_title: str | None = None
    lead_industry: str | None = None
    lead_source: str | None = None
```

`src/leadauto/extract/parsers.py` — set it in `_base` after `lead_title=`:

```python
        lead_title=lead.get("Title"),
        lead_industry=lead.get("Industry"),
        lead_source=lead.get("LeadSource"),
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_soql.py tests/test_parsers.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/leadauto/sf/soql.py src/leadauto/extract/models.py src/leadauto/extract/parsers.py tests/test_soql.py tests/test_parsers.py
git commit -m "feat(extract): select Lead.Industry and expose ExtractedLead.lead_industry"
```

---

### Task 5: write-back resolves and writes `Industry`

**Files:**
- Modify: `src/leadauto/writeback.py` (imports ~19-24; `lead_field_payload` firmographics block ~150-153; module docstring)
- Test: `tests/test_writeback.py`

**Interfaces:**
- Consumes: `industry.sf_industry_from_kununu`, `industry.sf_industry_from_leadfeeder` (Task 1); `EnrichedLead.feed_industry` (Task 3); `ExtractedLead.lead_industry` (Task 4); `company["Industry"]` (existing).
- Produces: `payload["Industry"]` when resolvable.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_writeback.py` (match the file's existing `EnrichedLead`/`ExtractedLead`
construction helpers; the inline form below is self-contained):

```python
from leadauto.extract.models import ExtractedLead
from leadauto.leadfeeder.enrich import EnrichedLead
from leadauto.writeback import lead_field_payload


def _enriched(*, feed_industry=None, lf_industry=None, lead_industry=None):
    e = ExtractedLead(lead_id="00Q1", task_id="T1", task_type="claimer",
                      lead_industry=lead_industry)
    return EnrichedLead(extracted=e, feed_industry=feed_industry,
                        company={"Industry": lf_industry} if lf_industry else {})


def test_industry_kununu_overwrites_existing():
    p = lead_field_payload(_enriched(feed_industry="IT", lead_industry="OTHER"))
    assert p["Industry"] == "INTERNET and INFORMATION TECHNOLOGY"


def test_industry_leadfeeder_fills_only_when_blank():
    p = lead_field_payload(_enriched(lf_industry="Business Consulting and Services",
                                     lead_industry=None))
    assert p["Industry"] == "CONSULTING, AUDITING and LAW"


def test_industry_leadfeeder_skipped_when_lead_already_set():
    p = lead_field_payload(_enriched(lf_industry="Business Consulting and Services",
                                     lead_industry="OTHER"))
    assert "Industry" not in p


def test_industry_kununu_beats_leadfeeder():
    p = lead_field_payload(_enriched(feed_industry="Banken",
                                     lf_industry="Software", lead_industry=None))
    assert p["Industry"] == "BANKING and INSURANCE"


def test_industry_unmapped_writes_nothing():
    p = lead_field_payload(_enriched(feed_industry="Raumfahrt", lead_industry=None))
    assert "Industry" not in p
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_writeback.py -k industry -q`
Expected: FAIL — `KeyError: 'Industry'` / assertions fail (nothing writes Industry yet).

- [ ] **Step 3: Implement**

In `src/leadauto/writeback.py`, add the import:

```python
from .industry import sf_industry_from_kununu, sf_industry_from_leadfeeder
from .leadfeeder.enrich import EnrichedLead
from .logging import get_logger
```

In `lead_field_payload`, after the existing firmographics block (`KN_VATNumber__c` /
`KN_Employee__c`), add:

```python
    # Industry ('Branche'): the kununu profile industry IS this taxonomy, so it is
    # authoritative and OVERWRITES (like KN_Region__c/CountryCode). The LeadFeeder
    # industry is a mapped fallback and fills only a blank. Unmapped -> nothing written
    # (industry.py logs the miss). See the 2026-07-13 industry-enrichment design doc.
    kununu_industry = sf_industry_from_kununu(enriched.feed_industry)
    if kununu_industry:
        _put(payload, "Industry", kununu_industry)
    else:
        _fill(payload, "Industry", e.lead_industry,
              sf_industry_from_leadfeeder(company.get("Industry")))
```

Also update the module docstring's field spec paragraph to mention Industry (one sentence:
"`Industry` ← kununu profile `profile_industry_name_de` mapped to the picklist (overwrite),
else LeadFeeder industry mapped (fill-only-blank).").

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_writeback.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/leadauto/writeback.py tests/test_writeback.py
git commit -m "feat(writeback): enrich Lead.Industry from kununu profile (overwrite) / LF (fill)"
```

---

### Task 6: QA sheet shows the resolved `predicted_industry`

**Files:**
- Modify: `src/leadauto/qa_artifact.py` (`REVIEW_COLUMNS` ~23-36; `_review_row` ~85-110)
- Test: `tests/test_qa_artifact.py`

**Interfaces:**
- Consumes: `lead_field_payload` (already imported in this module).
- Produces: a `predicted_industry` column in the Review sheet.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_qa_artifact.py` (mirror how the file builds a `result` for `_review_row`;
if a builder/fixture exists, reuse it and only assert the new key):

```python
from leadauto.qa_artifact import REVIEW_COLUMNS, _review_row


def test_review_row_has_predicted_industry_column():
    assert "predicted_industry" in REVIEW_COLUMNS
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_qa_artifact.py -k predicted_industry -q`
Expected: FAIL — `'predicted_industry' in REVIEW_COLUMNS` is False.

- [ ] **Step 3: Implement**

In `src/leadauto/qa_artifact.py`, add the column after `lf_industry`:

```python
    "lf_city", "lf_employees", "lf_industry", "predicted_industry", "lf_website", "lf_company_id",
```

In `_review_row`, add the value (right after the `lf_industry` entry):

```python
        "lf_industry": company.get("Industry"),
        "predicted_industry": lead_field_payload(enr).get("Industry"),
        "lf_website": company.get("Website"),
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_qa_artifact.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/leadauto/qa_artifact.py tests/test_qa_artifact.py
git commit -m "feat(qa): surface resolved predicted_industry in the Review sheet"
```

---

### Task 7: shadow-compare scores Industry vs the converted Account

**Files:**
- Modify: `src/leadauto/sf/soql.py` (`_ACCOUNT_SHADOW_FIELDS` ~220-221; `_LEAD_SHADOW_FIELDS` ~216-219)
- Modify: `src/leadauto/shadow_compare.py` (`_FIELD_CHECKS` ~63-74)
- Test: `tests/test_shadow_compare.py`

**Interfaces:**
- Consumes: `pred.payload["Industry"]` (Task 5), account `Industry` field.
- Produces: an `industry` entry in `compare_fields` output.

Note: standard LeadConvert maps `Lead.Industry` → `Account.Industry`, so both use the same
picklist labels; exact-string comparison via `_norm_text` is appropriate.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_shadow_compare.py` (mirror the file's existing `compare_fields` tests —
they build a `pred` with a `.payload` dict and pass a `lead`/`account` dict):

```python
from leadauto.shadow_compare import compare_fields


def test_industry_agrees_with_converted_account(_pred_factory=None):
    # Use the file's existing pred builder if present; otherwise a SimpleNamespace with
    # .payload / .lf_website / .decision / .account_id works.
    from types import SimpleNamespace
    pred = SimpleNamespace(payload={"Industry": "BANKING and INSURANCE"},
                           lf_website=None)
    out = compare_fields(pred, lead={}, account={"Industry": "BANKING and INSURANCE"})
    assert out["industry"]["status"] == "agree"


def test_industry_differs_from_converted_account():
    from types import SimpleNamespace
    pred = SimpleNamespace(payload={"Industry": "BANKING and INSURANCE"}, lf_website=None)
    out = compare_fields(pred, lead={}, account={"Industry": "OTHER"})
    assert out["industry"]["status"] == "differ"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_shadow_compare.py -k industry -q`
Expected: FAIL — `KeyError: 'industry'` (no such check yet).

- [ ] **Step 3: Implement**

`src/leadauto/shadow_compare.py` — add to `_FIELD_CHECKS`:

```python
    ("phone", ("Phone",), "contact", "Phone", normalize_phone, ("Phone",)),
    ("industry", ("Industry",), "account", "Industry", _norm_text, ("Industry",)),
]
```

`src/leadauto/sf/soql.py` — add `Industry` to both shadow projections:

```python
_LEAD_SHADOW_FIELDS = (
    "Id, Email, IsConverted, ConvertedDate, ConvertedAccountId, ConvertedContactId, "
    "KN_Employee__c, FirstName, Phone, PostalCode, City, Country, CountryCode, Industry"
)
_ACCOUNT_SHADOW_FIELDS = ("Id, Name, CreatedDate, Website, BillingPostalCode, "
                          "BillingCity, BillingCountryCode, Industry")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_shadow_compare.py -q`
Expected: PASS.

- [ ] **Step 5: Run the full suite + commit**

Run: `python -m pytest -q`
Expected: PASS (all green).

```bash
git add src/leadauto/shadow_compare.py src/leadauto/sf/soql.py tests/test_shadow_compare.py
git commit -m "feat(shadow): score predicted Industry vs converted Account.Industry"
```

---

## Self-Review

**Spec coverage:**
- Two rollup tables + resolver → Task 1. ✓
- Write policy (kununu overwrite / LF fill / unmapped blank+log) → Task 1 (log) + Task 5 (policy). ✓
- Plumbing feed→enrich→extract→writeback → Tasks 2,3,4,5. ✓
- QA `predicted_industry` → Task 6. ✓
- Shadow scoring + `_ACCOUNT_SHADOW_FIELDS` Industry → Task 7. ✓
- Encoding gotcha (`_norm`, umlaut keys) → Task 1 (`_norm`, umlaut test). ✓
- 20-label constraint (never off-list) → Task 1 (`VALID_SF_INDUSTRIES` tests) + resolver returns only mapped labels. ✓

**Placeholder scan:** No TBD/TODO; every code step shows complete code; the two tests that
depend on file-local fixtures (Task 3 `_extracted`, Task 6/7 `pred`/`result` builders) name the
exact fallback construction inline so they run standalone.

**Type consistency:** `feed_industry`, `lead_industry`, `sf_industry_from_kununu`,
`sf_industry_from_leadfeeder`, `KUNUNU_TO_SF`, `LF_TO_SF`, `VALID_SF_INDUSTRIES` are used with the
same names/signatures across Tasks 1→7. `_norm_text` (shadow) and `_norm` (industry) are distinct
by design.

## Post-implementation (out of plan, for Wasil)

- Revisit the 13 `†` kununu→SF mappings if any look wrong in QA (Task 1 table is the single edit point).
- After a few dry runs, read `predicted_industry` in the QA sheet and the `field_industry` line in
  `shadow_comparison.xlsx` to calibrate; extend `LF_TO_SF` from any logged "unmapped LeadFeeder
  industry" warnings.
- Update `CLAUDE.md` write-back field spec to include Industry once live-verified.
