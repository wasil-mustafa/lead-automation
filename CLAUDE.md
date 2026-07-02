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
2. **LeadFeeder** (`leadfeeder/`) — match/contact/company-enrichment client. `client.py` centralizes a
   throttle (~95/min) + `429`/`5xx` retry (Retry-After/backoff) in `api_get`/`api_post`, and batches
   `match_companies` (≤200) / `retrieve_contacts` (≤100); `match_company`/`get_contact_details` are thin
   wrappers. `enrich.py` does one batched match (phase 1) and one batched contact retrieve (phase 3).
   Tunables in `config.yaml` `leadfeeder:` — `{max_requests_per_min, max_retries, retrieve_batch, match_batch}`.
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
  (repo secret **set 2026-07-02** from the local `headless` org's sfdx auth url — regenerate with
  `sf org display --target-org headless --verbose --json | jq -r .result.sfdxAuthUrl` and pipe into
  `gh secret set`; reused by both workflows' login step); `config.py` reads `SF_AUTH_URL` and falls back
  to `SFDX_AUTH_URL`. Plus `LEADFEEDER_API_KEY` (GitHub Secret + local `LEADFEEDER_API_KEY.txt`, gitignored).
  Never hardcode. The legacy LeadFeeder key in `Scripts/` is live (confirmed working) and must be rotated.
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
- **Large SOQL auto-routes to Bulk API 2.0.** A plain `sf data query` puts the SOQL in the GET URL, so a
  long `IN(...)` list (`leads_by_ids`, `accounts_by_signals`/`contacts_by_signals`) overflows Salesforce's
  request-header limit → **HTTP 431**. `SfClient.query` routes any SOQL over `_BULK_QUERY_THRESHOLD`
  (2000 chars) through `sf data export bulk --query-file` (query in the POST body); small queries keep the
  fast synchronous REST path. Fixed in PR #2. (`sf data query` has **no** `--bulk`/`--wait` flags — the
  old `bulk=True` branch was dead code.)
- **CI Salesforce login is flaky.** `sf org login sfdx-url` intermittently hangs (Node exit 13 / "unsettled
  top-level await") — a CLI/runtime issue, NOT a bad credential (which prints `invalid_grant`). Both
  workflows retry the login 3× and pin `@salesforce/cli@2.140.6` (unpinned `latest` risks a regression);
  job-level `SF_AUTOUPDATE_DISABLE`/`SF_DISABLE_TELEMETRY` quiet the CLI's background async work.
- **LeadFeeder API** (`api.leadfeeder.com/v1`, `X-Api-Key`): **100 req/min per account** (429 on overflow);
  `POST /companies/match` takes ≤200 companies/call, `GET /companies|/contacts?ids=` ≤100 ids/call; match +
  contact-search are FREE; retrieves cost 1 credit/new record (free re-access within 12 months);
  `/contacts/search` returns only id+full_name+`has_*` flags — **no inline email/phone** (separate retrieve needed).
- **`scripts/` == legacy `Scripts/` on case-insensitive Windows** (same physical dir, holds the live-key
  `leadfeeder_pipeline.py`/`convert.py`). `.gitignore` keeps `Scripts/`/`scripts/*` ignored and tracks ONLY
  `scripts/leadfeeder_probe.py` — the read-only live probe: `PYTHONPATH=src python scripts/leadfeeder_probe.py "<co>" "<cc>"`.

## Current status
- ✅ **Phase 0** — repo scaffold, config, `sf/auth.py`, `sf/client.py`.
- ✅ **All modules built & unit-tested (147 tests pass):**
  - Module 1 — `sf/soql.py` (incl. `accounts_by_signals`/`contacts_by_signals`), `extract/reader.py`
    (`processed_tasks` feeds the watermark), `parsers.py`/`models.py`.
  - Module 2 — `leadfeeder/client.py` + `enrich.py` (`EnrichedLead`); throttle/retry + batched match/retrieve.
  - Module 3 — `match.py` (normalizers, `ExistingRecords` index, signal scoring, `load_existing_records`
    batched reads, link/review/create_new decision), `writeback.py` (KN_* mapping + dry-run bulk update),
    `convert/converter.py` (match-aware idempotent Apex).
  - Cross-cutting — `state.py` (watermark), `qa_artifact.py` (Review/Contact_Found/NoContact/Skipped/Summary
    + `Approve?` gate), `pipeline/stage1_enrich.py` + `stage2_convert.py`, `cli.py` (`python -m leadauto
    stage1|stage2`), `.github/workflows/{stage1,stage2}.yml`. Initial `state/watermark.json` committed.
- ✅ **LeadFeeder read-only path live-validated** (org `headless`, account `kununu Austria GmbH`/276820):
  auth, batched `match_companies`, contact-search cascade, `retrieve_contacts` + `flatten_contact_details`.
  Data note: contact email/phone were empty for the sampled contact — direct contact details may be sparse on this tier.
- ✅ **CI auth + Stage 1 read-only path live-validated on `master` (2026-07-02):** `SFDX_AUTH_URL` set,
  the HTTP 431 on large `leads_by_ids`/match queries fixed via Bulk API routing (PR #2), login hardened
  (3× retry + pinned CLI, PR #3), and a `stage1 --dry-run --no-leadfeeder` `workflow_dispatch` completed
  green (auth → extract → QA artifact upload; no writes, no LeadFeeder credits).
- ⚠️ **Salesforce write-back + conversion NOT yet live-validated** — no live write-back or LeadConvert has run.

## Next steps (resume here)
1. **Verify write-back field values against org `headless`** (read-only describe is fine): confirm the
   `KN_Employee__c` picklist labels, revenue unit, and country-code format flagged in `writeback.py`.
2. **Validate end-to-end** per the plan: `stage1 --dry-run --no-leadfeeder` read-only sanity → small live
   `--limit 5` write-back (inspect KN_* on those Leads) → one gated `stage2` conversion (verify
   link-vs-create, owner/status, no Opportunity, re-run = no-op).
3. **Roll out task types**: enable `ebp_trial` in `config.yaml` `enabled_task_types`, then `event`, then
   `content`, calibrating thresholds from the review sheet's false-match rate.
4. **CI**: ✅ repo secrets set (`SFDX_AUTH_URL`, `LEADFEEDER_API_KEY`); ✅ login hardened (3× retry +
   pinned CLI); ✅ `stage1` dry-run smoke-tested green. **Still TODO:** configure the `lead-conversion`
   Environment's required reviewers (the Stage 2 gate); fix the `dry_run` guard footgun (see below).
5. Rotate the legacy hardcoded LeadFeeder key in `Scripts/`.

Git: remote `github.com/wasil-mustafa/lead-automation` (private). **Default branch `master` is what the
scheduler runs** — fixes must land there. LeadFeeder throttle/retry/batching landed via PR #1; the Bulk-API/431
fix via PR #2; CI login hardening via PR #3. Design + plans live in `docs/superpowers/{specs,plans}/`.

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
- **`dry_run` guard footgun (both workflows):** the watermark-commit step's
  `if: ${{ inputs.dry_run != 'true' }}` still RUNS during a dry run (GitHub coerces the boolean input
  vs. the string `'true'`); it's harmless today only because a dry run doesn't modify
  `state/watermark.json`. Change to `if: ${{ !inputs.dry_run }}`.
- **Minor:** `sf/auth.py` writes the auth URL to a predictable temp path (CI uses its own
  ephemeral path); `stage2_convert` re-derives the auto-link predicate (could centralize);
  `stage1.yml` push has no rebase/retry (mitigated by the concurrency group).
- **LeadFeeder follow-ups (non-blocking, noted in PR #1):** widen `enrich.py` `search_contact`/list-add
  `except` to `requests.exceptions.RequestException` (the client layer already does); add `caplog` assertions
  to the chunk-failure tests if the project logger propagates. (Enrich list-add errors are now logged, not swallowed.)
