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
- ✅ **All modules built & unit-tested (162 tests pass):**
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
- ✅ **Scope simplified (2026-07-02):** only `claimer` + `ebp_trial` task types (config.yaml), `dry_run: true`.
  Signal coverage verified over ALL 6,217 in-scope tasks (180d): profile name / email / region ≈ 100%
  (EBP email 97.4% — misses are kununu-internal test profiles).
- ✅ **LeadFeeder match validated against a 1,101-lead control set** (`Export/control_leads_6m.xlsx`:
  leads converted → NEW accounts in the last 6 months, joined Task→ConvertedContactId — conversion
  re-parents Tasks onto the Contact, so `Who.Type='Lead'` only finds unconverted ones). Results in
  `Export/lf_match_check_6m.xlsx`. Key finding: **LF match_score alone is untrustworthy** (0.9+ on wrong
  companies, <0.2 on exact-name hits).
- ✅ **Name-agreement gate shipped** (`client._name_agreement`/`_best_match`, normalizers shared via
  `textnorm.py` — do NOT import `match.py` from the LF client, it cycles via `enrich`): accept when
  score ≥ floor AND name-agree ≥ 85, or rescue when name-agree ≥ 90 (token_set on suffix-normalized
  names + partial vs corporate email-domain root ≥5 chars, freemail excluded). Tunables
  `matching.lf_name_agree_{min,rescue}`. Control-set impact: 82.0%→89.4% accepted, wrong-company
  4.7%→1.1% of accepted, 115 rescues. `name_agreement` flows through `EnrichedLead` into the QA
  Review sheet (`lf_name_agreement`, next to the untrustworthy `lf_match_score`) so reviewers can
  calibrate the thresholds. 162 tests pass.
- ✅ **Assigned-company alt names in the LF gate** (spec `docs/superpowers/specs/2026-07-03-assigned-companies-gate-design.md`):
  `kununu_feed.py` streams the daily-cached DWH feed (join `profile_uuid`, URL-slug fallback) and
  `enrich.py` passes the profile's assigned-company names as `alt_names` (stripped before the LF POST);
  `_name_agreement` takes the max across profile name / alt names / email-domain root and reports
  `agreed_via` (QA column `lf_agreed_via`). Thresholds unchanged (85/90). Degrades to pre-feed behavior
  without credentials (`KUNUNU_API_TOKEN` env + GitHub secret; `--no-kununu-feed` to disable).
  190 tests pass.
- ✅ **Alt-names gate control-set-validated (2026-07-03)** — `KUNUNU_API_TOKEN` set (local `.env` +
  repo secret); first live feed pull OK (551 MB). **Feed field name confirmed:** `assigned_companies`
  = list of `{company_uuid, company_name}` — `_company_names` already handles it, NO code change.
  Harness: `scripts/control_check_alt_names.py` (local-only, gitignored; raw LF responses cached in
  `Export/lf_raw_match_altnames.json`), results in `Export/lf_match_check_6m_altnames.xlsx`.
  Feed coverage: 91.6% of control profiles resolved, but only 17.2% of leads get ≥1 alt name (most
  assigned companies share the profile's name). Impact vs the same-day no-alt gate: +2 accepted
  (86.2%→86.4%), 7 accepts vouched `via alt_name` — 3 exact account matches, 4 sibling legal entities
  from the profile's own assigned-companies list (the intended semantic; a name-only truth metric
  flags them as "wrong"). No stranger-company accepts added; **thresholds 85/90 kept**. Absolute rates
  sit ~3pp under the 2026-07-02 baseline because this harness passes country fields — see next bullet.
  The LF-gate ROI of alt names is small, but the feed/alt-names infra STAYS: it's planned input for a
  future **Salesforce duplicate-detection feature on live leads** (per Wasil, 2026-07-03). The
  `kununu-data` MCP server is the same bulk feed file — no per-lead query API to replace `ensure_feed`.
- ✅ **Country fields dropped from the LF match payload (2026-07-03, branch `drop-country-from-lf-match`):**
  LF's `country`/`country_code` filters SUPPRESS good matches — of 105 control leads with 0 candidates
  when country fields were sent, 60 regained candidates without them; 59 passed the gate and ALL
  agreed with the true converted account (0 wrong). `enrich.py` now sends only
  `company_name`/`email` (+ stripped `alt_names`); the name-agreement gate is the wrong-company
  guard. **Full-set confirmation without country** (`Export/lf_match_check_6m_nocountry.xlsx`):
  candidates 95.2%, production gate accepted 92.7% (vs 89.4% baseline / 86.4% with country),
  wrong-company 1.1% old-gate / 1.3% with alts (the +2 are kununu-verified sibling entities).
  Harness `scripts/control_check_alt_names.py` is now git-tracked (gitignore exception).
  Also learned: LF rejects lowercase `country_code` (400 — must be uppercase ISO 3166-1 alpha-2);
  moot now that we don't send it. 191 tests pass.
- ✅ **Queue-owned-leads scope + Vienna schedule (2026-07-03, PR #9):** `leads_by_ids` now filters
  `Owner.Type = 'Queue'` (works through the Bulk route — verified live: 299/301 of the current
  window). The 2 pool-user-owned leads (Rohdaten/Clean Pool) carry contact-attached tasks — **out of
  scope for now, revisit later** (Wasil). Stage 1 runs on cron `0 7,8,16,17 * * *` with a `gate` job
  keeping only firings at 09:00/18:00 Europe/Vienna (DST-proof) — **scheduled runs are FORCED
  dry-run** (flip in the gate job at go-live). **NOTE (2026-07-05, branch `task-status-fresh-filter`):
  the Vienna wall-clock-hour check was REMOVED — GitHub delivers this repo's scheduled runs hours late
  and drops most firings, so the exact-hour check (`HOUR == 9 || HOUR == 18`, evaluated at *execution*
  time) was ~never true and skipped `enrich` on every scheduled run. The gate now only forces dry-run;
  every delivered firing runs `enrich` (overlap guarded by the `concurrency` group). Cron simplified to
  `23 7,16 * * *` (twice daily, ~09:23/18:23 Vienna summer; DST drift accepted; :23 offset dodges the
  congested top-of-hour slot). See [[github-scheduled-runs-delayed-dropped]].** This replaced the 06:00-UTC weekday cron that would
  have fired with `--no-dry-run` (empty `inputs` on schedule events fell into the else branch —
  live-write time bomb defused); the watermark-commit guard footgun is fixed the same way
  (string output from the gate job). Owner facts: current window is 198× Lead 'Pre-Sales' Queue,
  101× Lead Entry Queue; 260/301 leads are status "No Demand" (open scope question); config
  `owner_id` = Wasil (placeholder — swap to the integration user before live conversions).
- ✅ **First live-credit CI crash fixed (2026-07-03, PR #8):** the first stage1 run WITH LeadFeeder
  credits crashed in `flatten_company` — LF retrieve payloads carry explicit `"name": null` industry
  entries and `"; ".join(...)` raised `TypeError` on the None (dry-run/no-LF paths never see real
  retrieve data, which is why they were green). Both raw-list joins (`Industry`, contact
  `departments`) now drop falsy items. Note: retrieved records are free to re-access for 12 months,
  so the crashed run's spent credits are NOT re-charged on the retry. 193 tests pass.
- ✅ **Dublettencheck rule engine + shadow comparison (2026-07-03, branch `dublettencheck-rules`):**
  Rewrote the account matcher as an ordered 8-rule engine mirroring the `Touchpoint automatismus logik`
  decision tree (spec `docs/superpowers/specs/2026-07-03-dublettencheck-rules-and-shadow-compare-design.md`,
  plan `docs/superpowers/plans/2026-07-03-dublettencheck-rules-and-shadow-compare.md`). Rules (first
  hit wins): `no_lf_match`→review, `name_address`→link, `name_vat`→link, `kununu_agree`→link,
  `kununu_conflict`→review, `name_only`→review, `weak_signal`→review, `no_match`→create_new. **Key
  invariant:** a lead with no accepted LeadFeeder match (`enriched.matched=False`) ALWAYS goes to
  review — never auto-links, never auto-creates, even on a perfect kununu UUID/URL hit. Score
  thresholds are GONE: `matching.auto_link_threshold`/`review_threshold` removed from config;
  `MatchResult` now carries `rule` + `evidence` (demoted VAT/DBID/host/email-domain/fuzzy hits shown
  as review evidence, never auto-linking). Name-only auto-link was deliberately downgraded to review
  (name collisions). `name_address` = normalized name + `BillingPostalCode` + `BillingCity` (street
  ignored); `name_vat` = normalized name + VAT; `kununu_agree` splits on `matching.dedupe_name_agree_min`
  (85) against LF name / `best_company` / feed `alt_names`. Stage-2 contact gate switched from
  `contact_score >= 0.90` to reason-based (`{kununu_user_uuid, contact_email_in_account}` — behavior-identical).
  QA Review sheet gains `match_rule`/`match_evidence` (dropped `account_score`); Summary counts all 8 rules.
  **Shadow backtest:** every DRY stage-1 run appends predictions to committed `state/predictions.jsonl`;
  `python -m leadauto shadow-compare` (wired into the scheduled workflow, read-only) scores pending
  predictions against the org's real human conversions — decision hit/miss/observed + per-field
  enrichment agreement — into committed `state/shadow_results.jsonl` + a `shadow_comparison.xlsx`
  artifact (Decisions/Fields/Summary). `Lead.ConvertedDate` is Date-only, so "human created a new
  account" = `Account.CreatedDate` same UTC day. stage1.yml now commits shadow files on every run,
  watermark still only on non-dry. 214 tests pass; read-only `--dry-run --no-leadfeeder` smoke test
  green (3 leads → all `no_lf_match`/review as expected without LF, predictions + QA columns verified).

## Branch `assigned-companies-gate` — COMPLETE, **PR #5 MERGED** (2026-07-03)
All 8 plan tasks of `docs/superpowers/plans/2026-07-03-assigned-companies-gate.md` are committed and
reviewed (per-task + whole-branch final review); 190 tests pass. See the "Assigned-company alt names"
status bullet above and Next-steps item 5 for what remains (live-only validation).
- PR #4 was **squash-merged** to master 2026-07-03; this branch then merged `origin/master` back in
  with `-s ours` (valid because master's tree was byte-identical to the branch's old base `b71b85f`
  — verified empty `git diff`), so PR #5's diff shows only this branch's work.
- ✅ Post-plan live validation done 2026-07-03 (secret set, feed field name confirmed, control set
  re-run) — see the "Alt-names gate control-set-validated" status bullet.
- Machine note: `gh auth setup-git` was run on the private laptop 2026-07-03 (git push works again).

## Next steps (resume here)
1. ✅ **Write-back fields org-verified (2026-07-03**, read-only describe + data sampling; findings in
   the `writeback.py` header): revenue unit (millions ✓), country code (uppercase alpha-2 ✓), and
   `KN_Region__c` (restricted picklist, parsers only emit valid labels ✓) all confirmed;
   `KN_Employee__c` is a RESTRICTED picklist whose real 14 bands (German dot separators, e.g.
   `751-1.000`, `> 100.000`) did NOT match the best-guess bins — fixed in `_EMPLOYEE_BINS` (PR #7;
   wrong labels would have been rejected rows).
2. **Validate end-to-end** per the plan: `stage1 --dry-run --no-leadfeeder` read-only sanity → small live
   `--limit 5` write-back (inspect KN_* on those Leads) → one gated `stage2` conversion (verify
   link-vs-create, owner/status, no Opportunity, re-run = no-op).
3. **Roll out task types**: enable `ebp_trial` in `config.yaml` `enabled_task_types`, then `event`, then
   `content`, calibrating thresholds from the review sheet's false-match rate.
4. **CI**: ✅ repo secrets set (`SFDX_AUTH_URL`, `LEADFEEDER_API_KEY`); ✅ login hardened (3× retry +
   pinned CLI); ✅ `stage1` dry-run smoke-tested green; ✅ `dry_run` guard footgun fixed + Vienna
   twice-daily schedule (PR #9). **Still TODO:** configure the `lead-conversion` Environment's
   required reviewers (the Stage 2 gate).
5. ✅ Alt-names gate validated on the control set (2026-07-03); feed field name confirmed; secret set.
   ✅ Country fields dropped from the LF match payload + full-set re-validation (92.7% accepted,
   wrong-company at baseline) — PR #6.
6. Rotate the legacy hardcoded LeadFeeder key in `Scripts/`.
7. **Dublettencheck rule engine — validate on live data (branch `dublettencheck-rules`):** the rule
   distribution has only been smoke-tested WITHOUT LeadFeeder (all leads → `no_lf_match`). Read the
   `match_rule`/Summary breakdown of the next scheduled dry-run QA artifact (WITH LeadFeeder) to see
   how leads split across `name_address`/`name_vat`/`kununu_agree`/`kununu_conflict`/`name_only`/
   `weak_signal`, and calibrate `dedupe_name_agree_min` if needed.
8. **Shadow comparison — review accuracy after a few days:** once humans have converted some of the
   leads the dry runs predicted, read `shadow_comparison.xlsx` (Decisions = per-lead hit/miss vs the
   human-chosen account; Fields = LF-vs-human enrichment agreement). This is the go-live evidence for
   the rule engine + the enrichment write-back.

Git: remote `github.com/wasil-mustafa/lead-automation` (private). **Default branch `master` is what the
scheduler runs** — fixes must land there. LeadFeeder throttle/retry/batching landed via PR #1; the Bulk-API/431
fix via PR #2; CI login hardening via PR #3; name-agreement gate merged via PR #4 (squash, 2026-07-03);
assigned-companies gate merged via PR #5, country-fields drop via PR #6 (both 2026-07-03). Active branch:
`dublettencheck-rules` (rule engine + shadow comparison, dry-run — PR pending).
Design + plans live in `docs/superpowers/{specs,plans}/`.

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
- ✅ **`dry_run` guard footgun — FIXED (PR #9):** stage1's effective dry-run is now the `gate`
  job's string output (schedule ⇒ forced `true`; dispatch ⇒ the checkbox), used by both the Run
  step and the watermark-commit guard. stage2 has no watermark step (dispatch-only), nothing to fix.
- **Minor:** `sf/auth.py` writes the auth URL to a predictable temp path (CI uses its own
  ephemeral path); `stage2_convert` re-derives the auto-link predicate (could centralize);
  `stage1.yml` push has no rebase/retry (mitigated by the concurrency group).
- **LeadFeeder follow-ups (non-blocking, noted in PR #1):** ✅ `enrich.py` `search_contact`/list-add
  `except` widened to `requests.exceptions.RequestException` (2026-07-03, with per-site ConnectionError tests).
  Still open: add `caplog` assertions to the chunk-failure tests if the project logger propagates.
  (Enrich list-add errors are now logged, not swallowed.)
