# Write-back gaps + Stage-2 conversion preflight — Design

**Date:** 2026-07-05 · **Status:** approved by Wasil (section-by-section)

## Problem

The org's active validation rules (Tooling-API audit 2026-07-05, see
`src/leadauto/convert/CLAUDE.md` "Conversion requirements") block Lead conversion unless
specific fields are set — and the current write-back leaves most of them empty. Over the
25,040 unconverted queue-owned leads: 71% violate the `KN_DBID__c`/`KN_NoOwnDBID__c` XOR,
97% lack `KN_Employee__c`, 49% lack Street/Country (required for the create-new path).
Separately, the `KN_DB*` fields the write-back currently fills are auto-populated by the
org's D&B integration and must not be written by us (per Wasil, 2026-07-05).

## Decisions (Wasil, 2026-07-05)

- Fix in BOTH stages: write-back closes the gaps at stage 1; a read-only preflight verifies
  actual field state at stage 2 and skips+reports failures (approach A).
- All new write-back fields are **fill-only-blank** — never overwrite an existing value.
- `KN_NoOwnDBID__c = true` always, unless the Lead's `KN_DBID__c` is populated.
- Everything `KN_DB*` is dropped from write-back (D&B auto-populates it), incl. `KN_DBID__c`
  (LF `Register_Id` is not needed).
- Email/freemail handling deferred to a later stage — preflight only *reports* freemail.
- Lead→Contact field mapping is confirmed working (no LinkedIn-rule concern).
- Owner: conversion-time `setOwnerId(config owner_id)` is sufficient; `owner_id` stays
  Wasil's user **deliberately** (the old "swap to integration user" note is obsolete).

## 1. Write-back changes (`writeback.py`)

**Drop** (9 D&B-owned fields): `KN_DBLegalCompanyName__c`, `KN_DBStreetHouseNumber__c`,
`KN_DBPostalCode__c`, `KN_DBCity__c`, `KN_DBISO2CountryCode__c`, `KN_DBNumberofEmployees__c`,
`KN_DBRevenueinMillions__c`, `KN_DBMainIndustryTextperWZ08__c`, `KN_DBID__c`.
`revenue_millions()` becomes dead code — remove with its tests.

**Keep unchanged:** kununu block (`KN_KununuProfileName__c` incl. processed-marker role,
`KN_KununuLinkProfile__c`, `KN_KununuUserUUID__c`, claimed flags, `KN_Region__c`),
`KN_Employee__c`, `KN_VATNumber__c`, `KN_LinkedInProfileLink__c`, `KN_CompanyEmail__c`.

**New writes — all fill-only-blank against the Lead's current value:**

| Field | Source (priority) | Guards |
|---|---|---|
| `KN_NoOwnDBID__c` | constant `true` | only when Lead `KN_DBID__c` blank AND tick not already set; written for every processed lead, LF-matched or not |
| `Street` | Task-parsed `street` → LF `Street` | truncate at 100 chars (`KN_Addresstoolong`) |
| `City` | Task-parsed `city` → LF `City` | — |
| `PostalCode` | Task-parsed `postal_code` → LF `Postal_Code` | skip unless length valid for country: DE=5, CH=4, others unchecked (`KN_Checklengthpostalcode`) |
| `CountryCode` | Task-parsed `country_code` → LF `Country` (both ISO-2) | uppercase; org has state/country picklists, so Salesforce derives `Country="Germany"` etc. — the exact value the validation rules compare |
| `FirstName` | parsed `contact_name` → LF contact `full_name` | only when the source name ends with the Lead's `LastName` (case-insensitive); the leading remainder becomes FirstName. Otherwise skip (don't write a different person's name) |
| `Phone` | LF **contact** `best_contact_phone` (never company switchboard) | `normalize_phone()`: strip spaces/dashes/dots/parens, `00…`→`+…`; accept only `+` + 6–15 digits; skip anything else (e.g. national `0…`) (`KN_ValidatePhone`) |
| `KN_Employee__c` fallback | existing LF `Employee_Count` mapping first; else parsed `company_size` (claimer `Unternehmensgröße:`) | exact picklist-label match after normalization, else parse the range's upper bound through `employee_bin()` |

**Not written:** `Salutation` (no data source; picklist `Mr./Ms./Mrs./Diverse/Not defined`),
`Email` (deferred), `KN_Language__c` (no source; only 16 of 25,040 blank — preflight flags it).

## 2. Extraction changes (`sf/soql.py`, `extract/models.py`, `extract/reader.py`)

- `_LEAD_FIELDS` gains: `Street, City, PostalCode, CountryCode, Phone, KN_DBID__c,
  KN_NoOwnDBID__c, KN_Employee__c, KN_Language__c` (FirstName/LastName already pulled).
- `ExtractedLead` gains the matching lead-native fields (existing `lead_*` pattern):
  `lead_street, lead_city, lead_postal_code, lead_country_code, lead_phone, lead_dbid,
  lead_no_own_dbid, lead_employee_category, lead_language`; `_base()` populates them.
- No parser changes (street/postal/city/country_code/company_size/contact_name already parsed).
- These ride through `to_dict()` into the QA artifact and `state/predictions.jsonl`
  automatically — which is what the stage-1 preview column reuses.

## 3. Stage-2 preflight (`convert/preflight.py`, new)

Read-only; runs inside `run_stage2` after `targets_from_approved`, before any Apex —
**including under `dry_run`** (makes `stage2 --dry-run` a "would this batch convert?" check).

One bulk query for the approved leads' current state: new `soql.leads_preflight(ids)` —
extraction fields plus `Email`, `HasOptedOutOfEmail`, `LeadSource`; re-checks
`IsConverted = false` (already-converted rows are *reported*, not silently no-opped);
no queue-owner filter.

Checks are pure functions over field values, named after the org rule they mirror:

| Check | Mirrors | Fails when |
|---|---|---|
| `employee_category` | `KN_EmployeeCategoryMandatory` | `KN_Employee__c` blank |
| `dbid_xor` | `KN_DbIdValidation` | not exactly one of `KN_DBID__c` / `KN_NoOwnDBID__c=true` |
| `dbid_numeric` | `KN_ValidateDandBonlyNumberLead` | `KN_DBID__c` present, not digits-only |
| `language` | `KN_LanguageCodemandatoryifqualified` | `KN_Language__c` blank |
| `address_create_new` | `KN_ConvertedLeadAddressValidation` | target has no `account_id` AND (Street blank OR Country/CountryCode blank) |
| `freemail_email` | `KN_LeadValidatePrivateMail` | Email domain ∈ {gmail, yahoo, outlook, hotmail, aol, icloud, protonmail, zoho, mail.com} AND not `HasOptedOutOfEmail` |
| `phone_format` | `KN_ValidatePhone` | Phone present, not `+digits`; `LeadSource='E-commerce'` exempt |
| `street_length` | `KN_Addresstoolong` | Street > 100 chars |
| `postal_length` | `KN_Checklengthpostalcode` | postal length wrong for Germany(5)/Switzerland(4); E-commerce exempt |
| `already_converted` | — | `IsConverted = true` (informational skip) |

**Failure handling (skip + report):** failing targets are excluded from the Apex batches.
Failures → log + `preflight_failures.csv` beside the stage-2 log (lead_id, check, offending
value, detail); `Stage2Result` carries them; CLI prints `preflight_skipped=N`; `stage2.yml`
uploads the CSV. CLI gains `--no-preflight` as an escape hatch (the Apex/org remains the
ultimate arbiter). Nothing is auto-fixed at stage-2 time.

## 4. Stage-1 reviewer preview (`qa_artifact.py`)

Reuse the same pure check functions on each lead's **post-write-back** state (current Lead
values overlaid with the write-back payload). New Review-sheet column `convert_blockers`:
empty when the lead would pass preflight today, else e.g. `language; freemail_email`.
Address check previews against the create-new assumption when no account match is proposed
(strictest interpretation).

## 5. Testing

TDD. Unit: `normalize_phone`, FirstName split guard, company-size→bin mapping,
fill-only-blank semantics, XOR tick, each preflight check (incl. E-commerce exemptions,
freemail list, IsConverted), reader mapping of new `lead_*` fields, `run_stage2` integration
(exclusion, CSV, `--no-preflight`), QA column. Then read-only smoke:
`stage1 --dry-run --no-leadfeeder` and `stage2 --dry-run`.

## 6. Docs / memory updates

- Stage-2 CLAUDE.md: preflight section; owner_id note corrected (stays Wasil deliberately).
- Root CLAUDE.md: status + open items.
- Memory `stage2-live-testing-plan`: drop the owner-swap prerequisite.

## Out of scope (deliberate)

Email/freemail *handling* (policy TBD — preflight only reports), `Salutation`,
`KN_Language__c` write-back, duplicate-rule handling in the conversion Apex (next
converter-improvements step), any Salesforce metadata change (standing rule).
