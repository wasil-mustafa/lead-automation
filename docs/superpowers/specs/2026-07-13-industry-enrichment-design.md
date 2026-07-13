# Industry enrichment (Salesforce `Industry` / "Branche") — design

**Date:** 2026-07-13
**Status:** design — awaiting spec review
**Author:** Wasil + Claude

## Problem

The Salesforce Lead field `Industry` (label **"Branche"**) is never enriched by the pipeline.
We want to fill it automatically. The obvious source — LeadFeeder's industry — does **not**
match the picklist 1:1, so a translation layer is required.

### What the mismatch actually is (investigation, 2026-07-13)

Neither candidate source is a literal match, but the SF picklist and the kununu feed turn out
to be **two levels of the same kununu taxonomy**:

| Layer | Source | Values | Language |
|---|---|---|---|
| **Target** | `Lead.Industry` ("Branche") — picklist, **NOT restricted** | **20 coarse buckets** | English (UPPERCASE) |
| **Primary source** | kununu profile `profile_industry_name_de` (already in the DWH feed we download to `kununu_profiles.jsonl`) | **44 fine categories** | German |
| **Fallback source** | LeadFeeder `company["Industry"]` (already captured by `flatten_company`, shown in QA as `lf_industry`) | LinkedIn taxonomy, multi-valued (`"Software; IT"`) | English |

- The 20 English buckets are clearly **roll-ups** of the 44 German categories
  (e.g. `BANKING and INSURANCE` = `Finanz` + `Banken` + `Versicherung`). So the core
  translation is a bounded, kununu-owned **44 → 20 rollup**.
- The feed field `profile_industry_name_de` was confirmed present on ~all of the 467,052
  profiles scanned in the current `kununu_profiles.jsonl` (528 MB, dated 2026-07-10). Because
  every in-scope lead has a kununu profile (that is the pipeline trigger), the **kununu-first
  path covers essentially every lead** and the **LeadFeeder path is a genuine edge case**
  (profile absent from feed / feed disabled / null industry).
- The field is **not restricted**, so an off-list write does not hard-fail — it silently
  pollutes reporting. Therefore we only ever write one of the 20 exact labels, or nothing.

The 20 target labels (verbatim, from `sf sobject describe Lead` on org `headless`, 2026-07-13):

```
AUTOMOTIVE and VEHICLE MANUFACTURING
BANKING and INSURANCE
CONSTRUCTION and MINING
CONSULTING, AUDITING and LAW
ENERGY, WATER and ENVIRONMENT
EDUCATION, TRAINING and SCIENCE
HEALTHCARE and SOCIAL SERVICES
HOTEL and CATERING
INDUSTRY and MECHANICAL ENGINEERING
INTERNET and INFORMATION TECHNOLOGY
CONSUMER GOODS and RETAIL
CULTURE, SPORTS and LEISURE
MARKETING, PR and DESIGN
MEDIA and PUBLISHING
PUBLIC SERVICE, ASSOCIATIONS and INSTITUTIONS
HR SERVICES
PHARMACEUTICALS and MEDICAL TECHNOLOGY
TELECOMMUNICATION
TRANSPORT and LOGISTICS
OTHER
```

## Decisions (approved 2026-07-13)

1. **Source priority:** kununu profile industry first, LeadFeeder mapped value as fallback.
2. **Rollup authority:** use the draft table below; Wasil adjudicates the `†`-flagged categories
   during spec review (no canonical kununu mapping to import).
3. **Write policy:** the kununu-profile value **overwrites** an existing `Industry` (it *is* this
   taxonomy, so it is authoritative — consistent with how `KN_Region__c`/`CountryCode` overwrite);
   the LeadFeeder fallback is **fill-only-blank**.
4. **Unmapped values:** leave `Industry` **unset** and **log a warning** (do not guess, do not
   write `OTHER`). Unmapped values thus surface in logs/QA so the table can be extended.

## Architecture

Two static rollup tables + a thin resolver, mirroring the existing `writeback._EMPLOYEE_BINS`
pattern (a plain dict that is the single source of truth, org/taxonomy-verified, with unmapped
inputs logged rather than silently coerced).

New module `src/leadauto/industry.py`:

```python
# Table A — kununu fine (German) -> SF coarse bucket (English). Closed set (44).
KUNUNU_TO_SF: dict[str, str] = { ... }   # keys = exact profile_industry_name_de strings

# Table B — LeadFeeder (LinkedIn taxonomy) -> SF coarse bucket. Seeded with observed
# values only; unmapped -> None (fallback path is edge-case, YAGNI on full LinkedIn set).
LF_TO_SF: dict[str, str] = { ... }       # keys = normalized LF industry names

def sf_industry_from_kununu(name: str | None) -> str | None
def sf_industry_from_leadfeeder(industry_field: str | None) -> str | None  # first ';'-token that maps
def _norm(s: str) -> str                 # casefold + strip + NFC — see Encoding gotcha
```

Resolution happens in `writeback.lead_field_payload`:

```
kununu_bucket = sf_industry_from_kununu(enriched.feed_industry)
if kununu_bucket:
    _put(payload, "Industry", kununu_bucket)          # OVERWRITE (authoritative)
else:
    lf_bucket = sf_industry_from_leadfeeder(company.get("Industry"))
    _fill(payload, "Industry", e.lead_industry, lf_bucket)   # fill-only-blank
# unmapped on both -> nothing written; resolver logged the miss
```

The resolver logs a warning when a **non-empty** source value maps to nothing (so genuinely
absent industries stay quiet, but unknown/new categories are surfaced).

### Draft 44 → 20 rollup (Table A)

`†` = judgment call for Wasil to confirm/adjust during spec review. All 44 feed values are covered.

| SF bucket | kununu `profile_industry_name_de` values |
|---|---|
| AUTOMOTIVE and VEHICLE MANUFACTURING | Automobil |
| BANKING and INSURANCE | Finanz, Banken, Versicherung |
| CONSTRUCTION and MINING | Bau/Architektur, Handwerk† |
| CONSULTING, AUDITING and LAW | Beratung/Consulting, Steuerberatung/Wirtschaftsprüfung, Recht |
| ENERGY, WATER and ENVIRONMENT | Energie |
| EDUCATION, TRAINING and SCIENCE | Bildung, Forschung/Entwicklung, Seminare/Messen† |
| HEALTHCARE and SOCIAL SERVICES | Gesundheit/Soziales/Pflege |
| HOTEL and CATERING | Gastronomie, Hotels/Beherbergung |
| INDUSTRY and MECHANICAL ENGINEERING | Industrie, Maschinenbau, Elektro/Elektronik†, Chemie†, Textil†, Druck/Verpackung† |
| INTERNET and INFORMATION TECHNOLOGY | IT, Internet |
| CONSUMER GOODS and RETAIL | Handel, Nahrungsmittel/Landwirtschaft† |
| CULTURE, SPORTS and LEISURE | Kunst/Kultur, Sport/Beauty, Tourismus† |
| MARKETING, PR and DESIGN | Marketing/Werbung/PR, Marktforschung† |
| MEDIA and PUBLISHING | Medien |
| PUBLIC SERVICE, ASSOCIATIONS and INSTITUTIONS | Öffentliche Verwaltung, Vereine, Administration/Verwaltung† |
| HR SERVICES | Personalwesen & -beschaffung, Einkauf† |
| PHARMACEUTICALS and MEDICAL TECHNOLOGY | Medizin/Pharma, Medizintechnik |
| TELECOMMUNICATION | Telekommunikation |
| TRANSPORT and LOGISTICS | Transport/Verkehr/Logistik |
| OTHER | Sonstige Branchen, Dienstleistung†, Immobilien† |

Full distinct set + frequencies (from the 467,052-row feed scan, 2026-07-13) is preserved in the
implementation plan so the table can be re-validated against a fresh feed.

### LeadFeeder fallback (Table B)

Seeded from values actually observed in our data plus obvious LinkedIn-taxonomy names. Kept
deliberately small — this path only fires when the kununu profile has no industry. Examples:

| SF bucket | LeadFeeder industry (normalized) |
|---|---|
| CONSULTING, AUDITING and LAW | business consulting and services, management consulting, legal services, accounting |
| INTERNET and INFORMATION TECHNOLOGY | software, it, information technology & services, computer software, internet |
| … | (extended as new `lf_industry` values appear in QA) |

Multi-valued LF strings (`"Software; IT"`) are split on `;`; the **first token that maps** wins.
Unmapped → `None` (leaves `Industry` blank, logs the raw value).

## Plumbing changes (small, localized)

1. `src/leadauto/kununu_feed.py`
   - Add `industry: str | None = None` to `ProfileInfo`.
   - In `lookup_profiles`, set `industry=d.get("profile_industry_name_de")`.
2. `src/leadauto/leadfeeder/enrich.py`
   - Add `feed_industry: str | None = None` to `EnrichedLead`.
   - Set `result.feed_industry = info.industry` in the phase-1 loop, alongside the existing
     `result.feed_country_code = info.country_code`.
3. `src/leadauto/sf/soql.py`
   - Add `Industry` to `_LEAD_FIELDS` (feeds `leads_by_ids` and `leads_preflight`).
   - Add `Industry` to `_ACCOUNT_SHADOW_FIELDS` for shadow scoring.
4. `src/leadauto/extract/models.py` + `extract/parsers.py`
   - Add `lead_industry: str | None = None` to `ExtractedLead`; set `lead_industry=lead.get("Industry")`
     in `_base` (needed for the LF fill-only-blank branch and for QA/shadow visibility).
5. `src/leadauto/industry.py` (new) — the two tables + resolver described above.
6. `src/leadauto/writeback.py` — import the resolver; add the `Industry` block to
   `lead_field_payload` (per the Resolution snippet). Update the module docstring's field spec.
7. `src/leadauto/qa_artifact.py` — add a `predicted_industry` column next to the existing
   `lf_industry` so the QA sheet shows both the raw source and the resolved bucket.
8. `src/leadauto/shadow_compare.py` — score predicted `Industry` vs the converted Account's
   `Industry` (mirrors the existing `CountryCode`/`City` field checks).

### Encoding gotcha

Two Table-A keys carry umlauts (`Öffentliche Verwaltung`, `Steuerberatung/Wirtschaftsprüfung`;
`Personalwesen & -beschaffung` carries `&`). The feed is UTF-8; only console rendering mangles
them. Table A is keyed on the exact UTF-8 strings, and lookups go through `_norm` (NFC + casefold
+ strip) on both the table keys and the incoming value, so a stray whitespace/case/normal-form
difference never causes a silent miss.

## Testing

- `industry.py` unit tests: **every one of the 44 kununu values maps to a valid SF label**
  (assert each target ∈ the 20-value set); a sample of LF values map correctly; unmapped/None/empty
  return `None`; `_norm` handles the umlaut/`&`/case/whitespace variants.
- Plumbing tests: `ProfileInfo.industry` parsed; `EnrichedLead.feed_industry` populated in `enrich`.
- `writeback` tests: kununu source **overwrites** a differing `Industry`; LF source **fills only
  when blank**; unmapped writes nothing; kununu wins over LF when both present.
- `caplog` assertion that a non-empty unmapped value logs a warning (and an empty one does not).
- Full suite stays green (`python -m pytest -q`).

## Out of scope / YAGNI

- No mapping of the full LinkedIn taxonomy — Table B is seeded from observed values and grows
  from QA evidence.
- No Salesforce metadata changes (the picklist is untouched; we only write existing labels).
- No new API calls — both sources are already fetched today.
- Account-side industry backfill / `resolve_pool_links` is out of scope for this change.

## Open items for spec review

1. Confirm/adjust the **13 `†`-flagged** kununu→SF mappings.
2. Confirm Table B seed values (and whether the LF fallback is worth shipping at all given the
   near-complete kununu-first coverage, or deferred until a real miss appears).
