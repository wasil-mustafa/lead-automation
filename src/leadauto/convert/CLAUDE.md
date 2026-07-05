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
3. `convert_leads` runs anonymous Apex per batch (default 50, `--batch-size`):
   bulk `Database.convertLead(list, allOrNone=false)`, per-lead `setAccountId`/`setContactId`,
   `setDoNotCreateOpportunity(true)`, `setOwnerId(config owner_id)`. Converted status is
   resolved dynamically in Apex: the configured label if the org has it, else the org's first
   converted `LeadStatus` by `SortOrder`.
4. Results come back as ONE `@@LEADAUTO@@`-sentinel-wrapped `JSON.serialize` blob decoded by
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
- Config `owner_id` is Wasil (placeholder) — swap to the integration user before live runs.

## Gotchas

- **Duplicate rules can block `Database.convertLead` and Apex cannot bypass them** — the
  `setBypass*` methods on `Database.LeadConvert` are internal-only, and
  `Database.DMLOptions.DuplicateRuleHeader.AllowSave` does not apply to conversion (only to
  plain DML). If a Block rule fires: fix the duplicate or downgrade the rule to Alert.
  Provenance + failed REST workaround: `docs/legacy/convert-leads/`.
- **Validation rules block conversion of sparse leads.** The May-2026 manual runs had to patch
  `Street = ','`, `KN_Employee__c = '11-50'`, `KN_NoOwnDBID__c = true` before converting
  (`docs/legacy/convert-leads/convertLead.apex`). Re-verify the org's current Lead/Account/
  Contact validation rules before go-live; stage 1 write-back fills `KN_Employee__c` but not
  `Street`/`KN_NoOwnDBID__c`.
- `parse_results` returns `[]` on any missing sentinel / bad JSON and `convert_leads` only
  logs a WARNING for that batch — converted/failed counts silently exclude it. Check logs on
  any live run.
- LeadConvert **re-parents the Lead's Tasks onto the Contact** (`Who.Type` flips) — the reason
  control-set joins had to go through `ConvertedContactId`.
- `Lead.ConvertedDate` is Date-only (no time component) — shadow-compare treats "same UTC day"
  as the match window.

## Live-testing plan (prod only, no sandbox — risk-ordered)

1. **Link-existing path, real lead:** one approved lead with a confident `name_address`/
   `kununu_agree` link, `--batch-size 1`; verify owner/status/no Opportunity/task re-parenting;
   re-run to confirm no-op.
2. **New-account path, synthetic lead:** throwaway Lead + Task on a kununu-internal test
   profile; convert, inspect, delete. Check the "Delete converted leads" org permission FIRST
   or the test leaves a permanent stub.
3. **Existing-account + new-contact path:** synthetic lead whose company matches a disposable
   test account.
