# Lead Automation — Project Guide

One cohesive Python pipeline that consolidates a fragile manual chain (SF report exports →
Power Query workbooks → `leadfeeder_pipeline.py` → Power Query → manual import → `convert.py`)
into a scheduled GitHub Actions job:

> Task on a NEW Salesforce Lead → extract info → enrich via LeadFeeder → dedupe vs existing
> Account/Contact → write enrichment back to the Lead → convert into a **new** or **existing**
> account/contact.

**Approved plan:** `C:\Users\wasil\.claude\plans\witty-watching-shannon.md` (full design, decisions, rollout).
**Session memories:** `~/.claude/projects/C--Users-wasil-OneDrive---kununu-Lead-Automation-Scripts/memory/` (org facts, preferences).

## Architecture — three object-centric modules
Code lives under `src/leadauto/`. Each module is independently runnable.
1. **Salesforce extraction** (`sf/`, `extract/`) — SOQL pulls new Leads + their Tasks; a
   `Subject → parser registry` (`extract/parsers.py`) routes each Task type to its own parser,
   emitting a normalized `ExtractedLead` (`extract/models.py`).
2. **LeadFeeder** (`leadfeeder/`) — ported match/contact/company-enrichment client.
3. **Lead enrichment + conversion** (`match.py`, `writeback.py`, `convert/`) — multi-signal match
   vs existing Accounts/Contacts → write-back to `KN_*` fields → gated, match-aware LeadConvert.

Cross-cutting: `config.py`, `logging.py`, `state.py` (file watermark), `qa_artifact.py`,
`pipeline/stage1_enrich.py` + `stage2_convert.py`, `cli.py`, `.github/workflows/`.

## Commands
```powershell
# Tests (pytest is configured with pythonpath=src via pyproject.toml)
python -m pytest -q

# Run anything that imports the package directly
$env:PYTHONPATH = "src"; python -c "from leadauto.config import Settings; print(Settings.load().in_scope_subjects)"

# Read-only org inspection (alias `headless`); aggregates need --json, NOT csv
sf data query --query "SELECT ... FROM Lead WHERE ..." --target-org headless --result-format json
```

## Conventions & gotchas (verified against org `headless`)
- **Org alias is `headless`** (production, kununu.my.salesforce.com, API v66.0). NOT `myOrg`.
- **Secrets via env / GitHub Secrets only:** the CI auth-url secret is named **`SFDX_AUTH_URL`**
  (existing repo secret, reused by both workflows' login step); `config.py` reads `SF_AUTH_URL` and
  falls back to `SFDX_AUTH_URL`. Plus `LEADFEEDER_API_KEY`. Never hardcode. The legacy LeadFeeder
  key in `Scripts/` is live and must be rotated.
- **Conversion is anonymous Apex** (`convert/converter.py`): bulk `Database.convertLead(list, false)`,
  per-lead `setAccountId`/`setContactId`, `IsConverted=false` re-filter (idempotent), results returned
  as a `@@LEADAUTO@@`-sentinel-wrapped `JSON.serialize` blob (no regex debug-scraping).
- **Write-back transforms need org verification before the FIRST live write** (see `writeback.py`
  header): `KN_Employee__c` picklist labels (bins are best-guess DACH bands), `KN_DBRevenueinMillions__c`
  unit (assumed absolute currency ÷ 1e6), `KN_DBISO2CountryCode__c` (LeadFeeder country passed through).
- **No Salesforce metadata changes.** Watermark = committed `state/watermark.json`; review gate =
  QA-artifact `Approve?` column; write-back only to *existing* `KN_*` fields.
- **Task `Subject` is the task-type router.** In scope (see `config/config.yaml` `task_types`):
  `Inbound Sales Task: {New Claimed Profile | EBP Trial | Eventteilnahme | Whitepaper Download | Video Download}`.
  Out: `Kontaktanfrage` (stays human), `Marketing Touchpoint: Ecom`. `enabled_task_types` gates the phased rollout.
- **Real Lead fields:** `Email`, `KN_CompanyEmail__c` (there is NO `KN_Email__c`), `KN_KununuProfileName__c`,
  `KN_KununuUserUUID__c`, `KN_KununuLinkProfile__c`, `KN_Region__c`, `KN_VATNumber__c`, plus `KN_DB*` firmographics.
  Account match: `KN_KununuProfileUuid__c`, `KN_KununuLinkProfile__c`, `KN_VATNumber__c`, `KN_DBID__c`, `Website`.
  Contact match: `Email`, `AccountId`, `KN_KununuUserUUID__c`. **kununu IDs are empty on new Leads** — they come
  from the Task Description; existing Accounts/Contacts carry them.
- **SOQL:** `Task` not allowed in reverse semi-joins (query Tasks first, then Leads by `WhoId`); `Subject`
  filterable but not groupable; `Description` is long-text → not LIKE-filterable (parse in code).
- **`sf` is wrapped** in `sf/client.py` via `subprocess(shell=True)` with double-quoted `--query` (portable
  Windows/Linux because SOQL literals use single quotes). Payloads written to temp files for apex/bulk.

## Current status
- ✅ **Phase 0** — repo scaffold, config, `sf/auth.py`, `sf/client.py`.
- ✅ **All modules built & unit-tested (119 tests pass, no live org/LeadFeeder calls):**
  - Module 1 — `sf/soql.py` (incl. `accounts_by_signals`/`contacts_by_signals`), `extract/reader.py`
    (`processed_tasks` feeds the watermark), `parsers.py`/`models.py`.
  - Module 2 — `leadfeeder/client.py` + `enrich.py` (`EnrichedLead`).
  - Module 3 — `match.py` (normalizers, `ExistingRecords` index, signal scoring, `load_existing_records`
    batched reads, link/review/create_new decision), `writeback.py` (KN_* mapping + dry-run bulk update),
    `convert/converter.py` (match-aware idempotent Apex).
  - Cross-cutting — `state.py` (watermark), `qa_artifact.py` (Review/Contact_Found/NoContact/Skipped/Summary
    + `Approve?` gate), `pipeline/stage1_enrich.py` + `stage2_convert.py`, `cli.py` (`python -m leadauto
    stage1|stage2`), `.github/workflows/{stage1,stage2}.yml`. Initial `state/watermark.json` committed.
- ⚠️ **Not yet validated against the live org** — no live write-back or conversion has been run.

## Next steps (resume here)
1. **Verify write-back field values against org `headless`** (read-only describe is fine): confirm the
   `KN_Employee__c` picklist labels, revenue unit, and country-code format flagged in `writeback.py`.
2. **Validate end-to-end** per the plan: `stage1 --dry-run --no-leadfeeder` read-only sanity → small live
   `--limit 5` write-back (inspect KN_* on those Leads) → one gated `stage2` conversion (verify
   link-vs-create, owner/status, no Opportunity, re-run = no-op).
3. **Roll out task types**: enable `ebp_trial` in `config.yaml` `enabled_task_types`, then `event`, then
   `content`, calibrating thresholds from the review sheet's false-match rate.
4. **CI**: set repo secrets (`SFDX_AUTH_URL`, `LEADFEEDER_API_KEY`), configure the `lead-conversion`
   Environment's required reviewers (the Stage 2 gate), smoke-test `stage1` `workflow_dispatch` dry-run.
5. Rotate the legacy hardcoded LeadFeeder key in `Scripts/`.

Nothing has been committed to git yet (repo has no commits; all work is in the working tree).

## Review follow-ups (from the whole-branch review)
Fixed this session: unique bulk temp file; freemail domains excluded from the email-domain
match signal; Apex `converted_status`/owner escaping; write-back row-failures surfaced via a
new QA **Failures** sheet (`update_bulk` parses `sf data update bulk --json`, reporting
`confirmed=False` on an unrecognized shape rather than assuming success).
Open:
- **`daily-tagger.yml`** references `main_profile_tag.py`, which is NOT in the repo — a
  separate automation, out of this plan's scope. Decide whether to keep/remove it (it would
  fail CI as-is). Not touched (not created here).
- **Write-back idempotency (live-validation task):** confirm the real `sf data update bulk
  --json` shape (the parser's assumed keys), then decide whether to exclude failed leads'
  tasks from the watermark advance vs. rely on the Failures sheet + overlap-window retry.
- **Minor:** `sf/auth.py` writes the auth URL to a predictable temp path (CI uses its own
  ephemeral path); `stage2_convert` re-derives the auto-link predicate (could centralize);
  `leadfeeder/enrich.py` swallows list-add HTTP errors silently; `stage1.yml` push has no
  rebase/retry (mitigated by the concurrency group).
