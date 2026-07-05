# Stage 2 — Gated Lead Conversion

Scope: the conversion path — this package (`convert/converter.py`), `pipeline/stage2_convert.py`,
`cli.py stage2`, `.github/workflows/stage2.yml`, and `review/approved_*.csv`. Stage 2 is
**dispatch-only** (no cron, no watermark) and has NOT yet run a live conversion.

## Flow

1. Reviewer fills `Approve?` in the QA artifact's Review sheet and commits the rows as
   `review/approved_<date>.csv`. `stage2` picks the newest by filename (`--approved` overrides).
2. `targets_from_approved` builds one `ConversionTarget(lead_id, account_id?, contact_id?)`
   per approved row (`Approve?` ∈ yes/y/true/x/1):
   - Account links whenever `account_match_id` is non-blank (reviewer validated it).
   - Contact links ONLY when `contact_reason` ∈ `{kununu_user_uuid, contact_email_in_account}`
     (identifier signals). Weaker reasons (`contact_email_global`, `linkedin`) deliberately
     create a fresh Contact under the linked Account.
   - Blanking `account_match_id`/`contact_match_id` in the CSV forces create-new.
3. run_preflight (convert/preflight.py) re-queries the approved leads' current state and drops
   any lead failing the "Conversion requirements" below — failures land in preflight_failures.csv
   (uploaded with the stage-2 log) and Stage2Result.preflight_failures; --no-preflight bypasses.
4. `convert_leads` runs anonymous Apex per batch (default 50, `--batch-size`):
   bulk `Database.convertLead(list, allOrNone=false)`, per-lead `setAccountId`/`setContactId`,
   `setDoNotCreateOpportunity(true)`, `setOwnerId(config owner_id)`. Converted status is
   resolved dynamically in Apex: the configured label if the org has it, else the org's first
   converted `LeadStatus` by `SortOrder`.
5. Results come back as ONE `@@LEADAUTO@@`-sentinel-wrapped `JSON.serialize` blob decoded by
   `parse_results` — never regex-scrape debug lines.

## Commands

```powershell
python -m pytest -q                                            # full suite covers stage 2
$env:PYTHONPATH = "src"; python -m leadauto stage2 --dry-run   # newest review/approved_*.csv
python -m leadauto stage2 --approved review/approved_X.csv --batch-size 1 --no-dry-run
```

## Safety model

- `dry_run` short-circuits in `convert_leads` before ANY Apex runs; config has `dry_run: true`
  and the workflow input defaults to true. These are currently the ONLY safeguards.
- Idempotent: the Apex re-filters `IsConverted = false`, so re-running a batch is a no-op.
- **The `lead-conversion` GitHub Environment does NOT exist yet** (verified 2026-07-05).
  `stage2.yml` references it and GitHub would auto-create it UNPROTECTED on first dispatch —
  create it with required reviewers in repo Settings → Environments BEFORE any live dispatch.
- Config `owner_id` stays Wasil's user DELIBERATELY (decision 2026-07-05) — converted records
  are assigned to him at setOwnerId time; queue ownership ends at conversion.

## Gotchas

- **Duplicate rules can block `Database.convertLead` and Apex cannot bypass them** — the
  `setBypass*` methods on `Database.LeadConvert` are internal-only, and
  `Database.DMLOptions.DuplicateRuleHeader.AllowSave` does not apply to conversion (only to
  plain DML). If a Block rule fires: fix the duplicate or downgrade the rule to Alert.
  Provenance + failed REST workaround: `docs/legacy/convert-leads/`.
- **Validation rules block conversion of sparse leads** — see "Conversion requirements" below
  (org-verified 2026-07-05). The May-2026 manual runs had to patch `Street = ','`,
  `KN_Employee__c = '11-50'`, `KN_NoOwnDBID__c = true` (`docs/legacy/convert-leads/convertLead.apex`).
- `parse_results` returns `[]` on any missing sentinel / bad JSON and `convert_leads` only
  logs a WARNING for that batch — converted/failed counts silently exclude it. Check logs on
  any live run.
- LeadConvert **re-parents the Lead's Tasks onto the Contact** (`Who.Type` flips) — the reason
  control-set joins had to go through `ConvertedContactId`.
- `Lead.ConvertedDate` is Date-only (no time component) — shadow-compare treats "same UTC day"
  as the match window.

## Conversion requirements (active validation rules, org-verified 2026-07-05)

Retrieved read-only via Tooling API (24 active rules on Lead/Account/Contact). Lead rules keyed
on `Status = 'Qualified'` OR `IsConverted = TRUE` fire DURING conversion (proven in May 2026);
leads sit unaffected until then. Population counts = all 25,040 unconverted queue-owned leads.

Fire at conversion (must be satisfied by write-back first):
- **`KN_DbIdValidation` — EXACTLY ONE of `KN_DBID__c` / `KN_NoOwnDBID__c=true`** (XOR: both set
  is also an error). 71% of leads currently violate it. Write-back now sets `KN_NoOwnDBID__c=true`
  whenever the Lead's `KN_DBID__c` is blank (closed 2026-07-05; `KN_DBID__c` itself is
  D&B-integration-owned and never written).
- **`KN_EmployeeCategoryMandatory` — `KN_Employee__c` required.** 97% currently blank; write-back
  fills it from LF `Employee_Count`, falling back to the claimer task's parsed company size
  (closed 2026-07-05; leads with neither source are caught by preflight).
- **`KN_LanguageCodemandatoryifqualified` — `KN_Language__c` required** (keyed on Qualified only).
  Only 16 leads blank — near-universally auto-populated; still needs a preflight check.
- **`KN_LeadValidatePrivateMail` — freemail `Email` (gmail/yahoo/outlook/hotmail/aol/icloud/
  protonmail/zoho/mail.com) BLOCKS when `Status != 'New'` unless `HasOptedOutOfEmail`.** ~7%
  (1,776) of leads are in this state — conversion-blocking; needs a policy (opt-out flag vs
  clear email vs route to review).
- **Account `KN_ConvertedLeadAddressValidation` — `BillingStreet` + `BillingCountry` required on
  accounts with `KN_ConvertedLeadCheck__c`** ⇒ Lead `Street` + `Country` must be populated for
  the create-new path (the legacy `Street=','` hack). 49% of leads lack Street or Country;
  write-back now fills the standard address (`Street`/`City`/`PostalCode`/`CountryCode`,
  fill-only-blank; closed 2026-07-05).

Fire on ANY Lead edit (constrain write-back itself, no status gate):
- `KN_ValidateDandBonlyNumberLead` — `KN_DBID__c` digits-only (`KN_DBID__c` is never written by
  the pipeline; the rule constrains D&B/human edits and is mirrored in preflight's
  `dbid_numeric` check).
- `KN_Addresstoolong` — `Street` ≤ 100 chars.
- `KN_Checklengthpostalcode` — `PostalCode` length 5 if `Country="Germany"`, 4 if
  `"Switzerland"` (English country names; Austria uncovered; E-commerce LeadSource exempt).
- `KN_ValidatePhone` (+ Contact `KN_ContactValidPhoneNumber`) — `Phone` must match
  `+<digits only>`, no spaces (e.g. `+4940419131`); normalize before ever writing phone.
- `KN_ProvideReasonForNoDemand` — No-Demand leads need a reason; 0 currently violate, so
  write-back updates are safe.

NOT enforced by any rule (layout-only, does not block API conversion): `KN_Region__c`, `City`,
`FirstName`/`Salutation`, email/phone presence. `LastName`/`Company` are schema-required.
Open question: does the org's Lead→Contact field mapping copy `KN_LinkedInProfileLink__c`
(a company URL) into Contact `KN_LinkedInProfile__c`? Contact rule `KN_LinkedInLinkValidation`
requires `linkedin.com/in/` and would reject company links — verify the mapping before go-live.
Stage-1 write-back now fills KN_NoOwnDBID__c / address (CountryCode) / KN_Employee__c fallback /
FirstName / Phone (fill-only-blank; spec docs/superpowers/specs/2026-07-05-writeback-gaps-and-stage2-preflight-design.md),
and the QA Review sheet previews these checks in convert_blockers.

## Live-testing plan (prod only, no sandbox — risk-ordered)

1. **Link-existing path, real lead:** one approved lead with a confident `name_address`/
   `kununu_agree` link, `--batch-size 1`; verify owner/status/no Opportunity/task re-parenting;
   re-run to confirm no-op.
2. **New-account path, synthetic lead:** throwaway Lead + Task on a kununu-internal test
   profile; convert, inspect, delete. Check the "Delete converted leads" org permission FIRST
   or the test leaves a permanent stub.
3. **Existing-account + new-contact path:** synthetic lead whose company matches a disposable
   test account.
