# Lead Automation — Project Guide

One cohesive Python pipeline that consolidates a fragile manual chain (SF report exports →
Power Query workbooks → `leadfeeder_pipeline.py` → Power Query → manual import → `convert.py`)
into a scheduled GitHub Actions job:

> Task on a NEW Salesforce Lead → extract info → enrich via LeadFeeder → dedupe vs existing
> Account/Contact → write enrichment back to the Lead → convert into a **new** or **existing**
> account/contact.

**Pointers:** approved plan `C:\Users\wasil\.claude\plans\witty-watching-shannon.md` · design/specs
`docs/superpowers/{specs,plans}/` · session memories
`~/.claude/projects/C--Users-wasil-OneDrive---kununu-Lead-Automation/memory/` (+ older org facts under
the `...-Scripts` project path) · **Stage 2 (conversion) has its own guide:
`src/leadauto/convert/CLAUDE.md`** — read it for anything touching conversion, the review gate,
or `stage2.yml` · legacy manual-conversion lessons `docs/legacy/convert-leads/` ·
**task auto-close (standalone, read-only, propose-only): `autoclose/CLAUDE.md`**.

## Architecture — three object-centric modules

Code lives under `src/leadauto/`. Each module is independently runnable.
1. **Salesforce extraction** (`sf/`, `extract/`) — SOQL pulls new Leads + their Tasks; a
   `Subject → parser registry` (`extract/parsers.py`) routes each Task type to its own parser,
   emitting a normalized `ExtractedLead` (`extract/models.py`).
2. **LeadFeeder** (`leadfeeder/`) — match/contact/company-enrichment client. `client.py` centralizes a
   throttle (~95/min) + `429`/`5xx` retry (Retry-After/backoff) + a GET-only transport retry in
   `api_get`/`api_post`, and batches
   `match_companies` (≤200) / `retrieve_contacts` (≤100). `enrich.py` does one batched match (phase 1)
   and one batched contact retrieve (phase 3). Tunables in `config.yaml` `leadfeeder:`.
3. **Lead enrichment + conversion** (`match.py`, `writeback.py`, `convert/`) — rule-based match
   vs existing Accounts/Contacts → write-back to `KN_*` fields → gated, match-aware LeadConvert.

## Commands

```powershell
# Tests (pytest is configured with pythonpath=src via pyproject.toml) — 524 pass
python -m pytest -q

# Run a stage locally (set LEADFEEDER_API_KEY / KUNUNU_API_TOKEN as ad-hoc env vars if needed)
$env:PYTHONPATH = "src"; python -m leadauto stage1 --dry-run --no-leadfeeder
$env:PYTHONPATH = "src"; python -m leadauto stage2 --dry-run
$env:PYTHONPATH = "src"; python -m leadauto shadow-compare
# Backtest skipped (contact-reparented) tasks; LF needs Actions (key only in GH Secrets):
#   gh workflow run backtest-skipped.yml   — locally only the no-credit plumbing check works:
$env:PYTHONPATH = "src"; python -m leadauto backtest-skipped --no-leadfeeder

# Read-only org inspection (alias `headless`); aggregates need --json, NOT csv
sf data query --query "SELECT ... FROM Lead WHERE ..." --target-org headless --result-format json

# Read-only LeadFeeder live probe
$env:PYTHONPATH = "src"; python scripts/leadfeeder_probe.py "<company>" "<cc>"
```

## Status

- **Built & unit-tested:** all three modules + cross-cutting; 552 tests green.
- **Live-validated (read-only):** CI Salesforce auth, Stage 1 extract→QA path, LeadFeeder
  match/contact cascade (org `headless`, LF account kununu Austria GmbH/276820; note: direct
  contact email/phone can be sparse on this LF tier).
- **Scheduled:** Stage 1 is triggered by an **external cron-job.org job** (id 8029899) hitting the
  `stage1.yml` `workflow_dispatch` at 09/12/15/18 Vienna; it POSTs `{"ref":"master"}` with no
  inputs ⇒ `dry_run` defaults **true** — so **scheduled runs are STILL DRY even after the
  2026-07-18 go-live**; the last step to full automation is changing the cron job's payload to
  `{"ref":"master","inputs":{"dry_run":"false"}}` (Wasil's call). Change the schedule in *that
  job*, not the workflow. GitHub's own `schedule:` cron was removed. Shadow files committed every
  run; watermark only on non-dry. Stage 2 runs on manual dispatch OR chained from a live Stage-1
  run (`workflow_call`, see the go-live bullet).
- **Scope:** `enabled_task_types` = `claimer`, `ebp_trial`; config `pipeline.dry_run: true`
  (local-run default; CI takes the dispatch input), `review_gate: false` (since go-live),
  `task_lookback_days: 14` (was 90 for the initial backlog sweep, which is done; the window is a
  BACKWARD overlap off the watermark, so shrinking it can't miss new tasks), and the
  **live-scope gate** `pipeline.live_scope` — see next bullet.
- **LIVE-PROVEN end to end (2026-07-10):** first live write-back (3 test leads, KN_* fields
  org-verified) AND first live conversion — stage2 run 29126789599 converted
  dbeyond/coderis/econoomy via SOAP (3 converted, 0 failed/missing; owner=Wasil, Qualified,
  no Opportunity, task re-parented, KN_Email2__c on econoomy only); the live idempotency
  re-run was a green no-op (preflight skipped all 3 as IsConverted). Scheduled runs remain
  DRY (config `dry_run: true`); going live at scale is now a config/dispatch decision.
  The Dublettencheck rules were validated WITH LeadFeeder by the 2026-07-08 skipped-task
  backtest (87% correct account on 283 cases — see Matching section); the create_new path
  is shadow-scored plus this 3-lead live test.
- **GO-LIVE (phased) 2026-07-18 — first fully automated E2E run:** run 29653343786 did live
  write-back → committed the auto-approved CSV → **chained Stage 2** (`stage1.yml` `convert` job
  calls `stage2.yml` via `workflow_call`, batch_size 3, checkout `ref: master` to see the CSV
  committed mid-run) → converted 3/3 (GS Gestelltechnik/HGW Winterthur/Angeringer; org-verified:
  Qualified, owner **Rohdaten Pool** on Lead/Account/Contact, no Opportunity, tasks re-parented).
  **Live scope** (`live_scope.py` + config `pipeline.live_scope`): on a non-dry run ONLY leads
  with decision ∈ `decisions` (currently `create_new`) AND kununu profile score <
  `max_kununu_score` (3.0, strict — the human "No Demand" line) are written back + staged;
  missing score fails closed; every other lead stays untouched (humans keep it) and gets a
  shadow prediction (live-scope rows never do — we convert those ourselves). Score source:
  feed `profile_score` (stringly typed) → `ProfileInfo.score` → `EnrichedLead.kununu_score`.
  QA Review sheet gained `kununu_score` + `live_scope` (yes/exclusion-reason) columns and a
  Summary `live_scope_pass` count. Widen the rollout in config (add `link`, raise the cap).
  **Widened 2026-09-28 (Wasil):** `max_kununu_score: null` (no score axis — a lead
  scoring 3.0 or more, or with no score, now goes live; feed outage no longer fails closed),
  `allow_wide_match: true` (flagged: QA `live_scope` = `yes_wide_match`, Summary
  `live_scope_wide_match`, Stage 1 WARNING; moot while `match_wide_results: 0`), and
  `pipeline.include_enriched_leads: true` (no already-enriched marker skip — the
  `lead_statuses: [New]` filter already keeps human-worked leads out). Owner (Rohdaten
  Pool) and the no-score-passes behaviour are deliberate (Wasil, same day).
  **Split + enrich-only 2026-10-19 (Wasil):** write-back and conversion are now separate
  gates (`live_scope.writeback_reason` vs `live_scope_reason`). Config: `task_types:
  [claimer]` (ebp_trial stays dry — predicted, never written), `writeback_decisions:
  [create_new, link, review]` (every LF-matched claimer is enriched; never `skip`) and
  `decisions: []` (nothing auto-converts ⇒ no auto CSV ⇒ no chained Stage 2; humans
  convert). QA gained a `live_writeback` column + Summary `live_writeback_pass`. A
  written-back lead still gets a shadow prediction flagged `written_back`, and
  shadow-compare scores only its decision (its fields would match our own write-back).
  Absent keys keep the old behaviour (write-back scope = `decisions`, every task type).
- **Write-back field spec (Wasil, 2026-07-10, `writeback.py`):** from the Task ONLY kununu URL
  (= the processed marker, replacing `KN_KununuProfileName__c` — reader accepts either), Region
  (derived from the URL's country slug: `/de/`→Germany, `/at/`→Austria, `/ch/`→Switzerland) and
  contact email → standard `Email` (**overwrites** a differing Lead email — the claimer/trial
  person is fresher). `KN_KununuUserUUID__c` is NOT written: the DWH auto-enriches it once the
  kununu URL is set. Everything else from LeadFeeder: `KN_VATNumber__c` ← LF `VAT_Id` (task VAT
  never written), `KN_Employee__c` ← LF headcount ONLY (the task's Unternehmensgröße line —
  ~90% empty, only `small`/`large` — is never used; overwrite deliberate), address always LF —
  EXCEPT country: `CountryCode` gets the slug-derived country with the same overwrite logic as
  `KN_Region__c` (LF company country only fills a fully blank country when there is no slug);
  shadow-compare scores `CountryCode` vs the converted Account's `BillingCountryCode`.
  From the LF *contact*: `Title` ← position, `KN_LinkedInProfileLink__c` ← contact LinkedIn
  (company LinkedIn/email never), `Phone` = direct-dial-typed number first (LF types phones;
  `direct` > `office`/`landline`; NEVER mobile) → LF company phone; a found contact without a
  direct dial triggers the async **find-contact-data job** (`client.find_contact_data`, 1 credit
  only when data is actually found; tunables `leadfeeder.find_*`). Plus NoOwnDBID tick +
  FirstName (nice-to-have). **Email from LeadFeeder (2026-09-18, event leads):** when the
  Task carries no email, the LF contact's first email (`writeback.lf_contact_email`) fills a
  BLANK `Email` only — never overwrites. **Contact lookup only when needed**
  (`leadfeeder.contact_lookup_only_when_needed`, default false; true in the ZPE config): the
  contact cascade (search + retrieve credit + find-contact-data) runs only for matched leads
  whose Lead still lacks an email or a phone, and the phone job only when the Lead has no
  phone — Email/Phone are fill-only-blank, so a lookup for a lead that has both buys nothing
  (ZPE 2026: 116/116 had Email, 109/116 Phone ⇒ 7 lookups instead of 94). Cost: Title/
  LinkedIn/FirstName are not fetched for those leads. `EnrichedLead.contact_skipped` names
  the reason on the QA `NoContact` sheet (`reason` column) + Summary `contact_lookups_skipped`.
  **Phone repair (`pipeline.repair_national_phone`, default false; true in the ZPE config,
  2026-09-18):** the ONE gated exception to Phone's fill-only-blank rule. 46 of 109 ZPE
  badge-scan leads carried `+0209…` — a national number with `+` glued onto the trunk zero,
  which `KN_ValidatePhone` accepts (it checks only `+` and 6-15 digits) but which dials
  nowhere. `writeback.repair_national_phone` rewrites `+0<digits>` to `+49/+43/+41` + the
  rest by the lead's effective country (slug > Lead `CountryCode` > LF); other countries and
  already-international numbers are left alone, and a repaired phone is never replaced by a
  LeadFeeder number. `lead_field_payload`/`write_back` take an optional `settings` for it;
  Stage 1 and the QA sheet pass theirs, so the Enriching sheet shows what will be written.
  **Post-convert (Stage 2, `convert.enrich_contact_email2`):** on NEW
  Contacts only, the LF contact email lands in Contact `KN_Email2__c` when it differs from the
  primary Email (fill-only-blank, best-effort; carried via the Review sheet's `lf_contact_email`
  column). **Stage 2 preflights** approved leads and skips+reports rule violations
  (preflight_failures.csv).
- **LF-outcome campaign tagging (2026-10-19, `campaigns.py`, config `pipeline.lf_campaigns`):**
  on a NON-dry Stage 1 run, `claimer` leads only (`pipeline.lf_campaign_task_types`)
  that were either WRITTEN BACK or not LF-matched become a CampaignMember of one
  campaign: no accepted LF match → `unmatched` 701Mz00001aKIjZIAW "EnrichLeadmanually";
  written back with final `KN_Employee__c` AND `Street` filled (payload over the
  existing value) → `matched_complete` 701Mz00001aPCDdIAO "EnrichLeadfedup"; written
  back with either blank → `matched_incomplete` 701Mz00001aP9hDIAS "EnrichLeadshortage"
  (Wasil, 2026-10-19). An LF-matched lead NOT written back (out of write-back scope,
  failed write-back) gets no campaign, so tagging — a permanent exclusion — never
  swallows a lead a wider scope could still write. Never tags with LeadFeeder disabled
  (everything would look unmatched). Insert =
  `SfClient.create_records` (sObject Collections POST, +1 call per ≤200 leads;
  `DUPLICATE_VALUE` counts as already-member); failures land on the QA Failures sheet
  (stage `campaign`), never hold a lead back, and a request-level `SfError` is caught so
  it never aborts the run. Tagged leads are excluded from every later run (dry too) by an
  anti-join in `leads_by_ids` (0 extra calls; they show as "lead not found, already
  converted or LF-campaign tagged"). QA Review `lf_campaign` column previews the bucket
  on dry runs. The three campaigns were `IsActive = false` when shipped (default member
  status `Sent`).
- **Stage 2 pool-account enrichment + same-company dedup** (PR #18): `resolve_pool_links`
  overwrite-enriches `Rohdaten Pool`-owned linked Accounts behind a kununu identity gate, and
  `convert_leads` clusters create-new leads by `lf_company_id` into one Account + N Contacts. See
  `src/leadauto/convert/CLAUDE.md`.

## Conventions & gotchas (verified against org `headless`)

### Salesforce org, secrets & write-back

- **Org alias is `headless`** (production, kununu.my.salesforce.com, API v66.0). NOT `myOrg`.
- **Secrets via env / GitHub Secrets only.** CI auth secret is **`SFDX_AUTH_URL`** (regenerate:
  `sf org display --target-org headless --verbose --json | jq -r .result.sfdxAuthUrl` → `gh secret set`);
  `config.py` reads `SF_AUTH_URL` with `SFDX_AUTH_URL` fallback. Plus `LEADFEEDER_API_KEY` and
  `KUNUNU_API_TOKEN`. **All local plaintext copies were deleted 2026-07-05** (OneDrive-synced repo);
  keys live ONLY in GitHub Secrets, rotation deliberately skipped. For local runs set env vars ad hoc.
  Never hardcode.
- **Conversion (Stage 2) runs over the SOAP Partner API `convertLead()`** (`sf/soap.py` +
  `SfClient.convert_lead`) — anonymous-Apex conversion is IMPOSSIBLE: the pipeline user lacks
  "Author Apex", so execute-anonymous runs restricted and `Database.convertLead`/`JSON.serialize`
  throw `System.SecurityException` (root-caused 2026-07-10; the CI CLI mangles it into empty
  `--json` output that mimics a flake). See `src/leadauto/convert/CLAUDE.md` — mechanics,
  safety model, duplicate/validation-rule gotchas, live-testing plan.
- **Record updates run over REST sObject Collections** (`SfClient.update_records`, since
  2026-07-18): one `PATCH /composite/sobjects` per ≤200 records (allOrNone=false, inline
  per-row errors, one 401→auth-refresh retry) riding the CLI's cached token via `auth_info` —
  replaced the `sf data update bulk` jobs (5-7 raw requests + polling each) at all three call
  sites (Lead write-back, pool Accounts, Contact KN_Email2__c). The Stage 2 converted status
  is likewise pinned (`salesforce.converted_status`); the per-run LeadStatus SOQL only runs
  when the config value is blank.
- **Write-back transforms are org-verified** (`writeback.py` header): revenue in millions ✓,
  uppercase ISO2 country ✓, `KN_Region__c` restricted picklist ✓, `KN_Employee__c` restricted
  picklist with the org's real 14 German-format bands (e.g. `751-1.000`, `> 100.000`) in
  `_EMPLOYEE_BINS` — wrong labels are rejected rows.
- **No Salesforce metadata changes.** Watermark = committed `state/watermark.json`; review gate =
  QA-artifact `Approve?` column; write-back only to *existing* `KN_*` fields.

### Salesforce data model, scope & SOQL

- **Who produces our Tasks, and what they now carry [2026-08-20].** Stage 1's input is
  generated by the Braze interface documented in Confluence
  (`Braze -> SF: Marketing Touchpoint + Lead/Contact Creation/Update`, spec updated
  2026-08-06); full notes in `Braze SF Interfaces\2026-08-20-interface-findings.md` §5.
  Three consequences:
  - **Eight kununu identifiers are now structured Task fields, not Description tokens:**
    `KN_KununuProfileUuid__c`, `KN_KununuLinkProfile__c`, `KN_kununuProfileName__c`,
    `KN_StatusKununuProfile__c`, `KN_KununuProfileClaimed__c`, `KN_KununuUserUUID__c`,
    `KN_Product__c`, `KN_VisitorID__c`. `_TASK_FIELDS` in `sf/soql.py` still selects only
    `Id, WhoId, Who.Name, Subject, Description, CreatedDate`, so we parse free text
    (`Description` = `description_1`–`description_7` concatenated) for data already on the
    record. Adding them costs **zero extra API calls** and retires the fallback chain in
    `docs\content-task-profile-triage.md`; `autoclose_tasks.py` likewise reads the profile
    UUID off the Account instead of the Task. Verify each field name against the org before
    relying on it — note the lowercase `k` in `KN_kununuProfileName__c` versus the Lead's
    `KN_KununuProfileName__c`.
  - **Our universe is pre-filtered upstream.** A touchpoint with
    `b2b_form_submission/scoring = 0` whose email is not yet in SF is never sent to SF at all
    and no lead is created. And `scoring = 75` is rewritten to `0` when the user is already a
    customer of that product (`b2b_profile_types.paid > 0` for EBP, `hiring_extension > 0` for
    jobs), which is why some tasks arrive already low-scored — that is the spec, not our
    autoclose rules.
  - **Two IDs from the spec that appear nowhere in this repo:** Task
    `RecordTypeId = 012Mz000008PVc1IAG` (old `012b0000000TfHuAAK`), and new-lead `OwnerId` =
    SF Group `KN_LeadEntry` = `00GMz00000g1rF4MAI`. Braze-created leads land on that queue.
- **The write-back's email overwrite has a documented side effect [2026-08-20].** Writing
  `Lead.Email` (see the write-back field spec above — the overwrite is deliberate) makes
  Salesforce auto-clear `KN_KununuUserUUID__c`, `KN_OptinStatus__c`, `KN_OptinChangedDate__c`,
  `KN_ContractOptInDate__c` and `KN_MarketingSubscriptionGroups__c` on that record. Consent
  itself is safe — kununu core is the data-leading system and SF is only a mirror — but the
  Braze link is severed permanently, because the UUID is only refilled from Braze
  `External_ID` while Braze is still keyed to the *old* address. A future touchpoint from the
  old address then resolves via `contact_id` → `lead_id` → email → **create a lead**, i.e. it
  generates duplicates for us. Keep the overwrite (the claimer is fresher), but do not
  "improve" it into a Contact-email overwrite, and do not repeat the belief that
  `KN_KununuUserUUID__c` is enriched "once the kununu URL is set" — it comes from Braze
  `External_ID`, only when the SF field is empty, next time the user enters a sync segment.
- **528 Rohdaten-Pool contacts have an email the warehouse knows is undeliverable
  [2026-08-20].** Our conversion scope is that pool. There is no Salesforce field carrying
  this (`KN_KununuEmailInvalid__c` does not exist in the org); the signal is
  `users__export_mcp.is_subscription_validated`, joined on `user_uuid` ↔
  `KN_KununuUserUUID__c`. Worth considering as a preflight input; costs no SF quota.
- **Task `Subject` is the task-type router** (`config/config.yaml` `task_types`). In scope:
  `Inbound Sales Task: {New Claimed Profile | EBP Trial | Eventteilnahme | Whitepaper Download |
  Video Download}`. Out: `Kontaktanfrage` (stays human), `Marketing Touchpoint: Ecom`.
  `enabled_task_types` gates the phased rollout.
- **Task `Status` is the freshness gate.** `tasks_since` filters `Status IN (fresh_task_statuses)`
  = `In Progress | in Arbeit | Not Started`. Do NOT use `IsClosed = false`: the bilingual/legacy
  picklist flags `Abgeschlossen` (Completed) as `IsClosed=false` and would leak handled tasks;
  there is no status literally named `Open`. Empty list disables the filter.
- **Leads scope:** `leads_by_ids` filters `Owner.Type = 'Queue'`, and since 2026-09-28 the
  reader keeps only `Lead.Status = New` (`pipeline.lead_statuses: [New]`, Wasil: any other
  status means a human already worked the lead). So "No Demand" leads are now OUT of scope —
  no write-back, no shadow prediction; their tasks still count as processed for the watermark. The few
  pool-user-owned leads (contact-attached tasks) are out of scope for now. Config `owner_id` =
  the **'Rohdaten Pool' user since 2026-07-18** (the live scope is pool-quality leads; revisit
  when it widens — Wasil's own id is kept in the config comment).
- **Own-company emails are out of scope.** Stage 1 extraction AND shadow scoring skip any lead
  whose lead/contact email second-level domain ∈ `excluded_email_domains` (`kununu`, `xing`,
  `onlyfy`) — internal colleagues doing profile-claim/trial tests, not sales leads. Matched on the
  SLD so any TLD counts (`kununu.com`/`.at`); helper `textnorm.is_internal_email`.
- **Real Lead fields:** `Email`, `KN_CompanyEmail__c` (there is NO `KN_Email__c`),
  `KN_KununuProfileName__c`, `KN_KununuUserUUID__c`, `KN_KununuLinkProfile__c`, `KN_Region__c`,
  `KN_VATNumber__c`, plus `KN_DB*` firmographics. Account match: `KN_KununuProfileUuid__c`,
  `KN_KununuLinkProfile__c`, `KN_VATNumber__c`, `KN_DBID__c`, `Website`. Contact match: `Email`,
  `AccountId`, `KN_KununuUserUUID__c`. **kununu IDs are empty on new Leads** — they come from the
  Task Description; existing Accounts/Contacts carry them.
- **SOQL:** `Task` not allowed in reverse semi-joins (query Tasks first, then Leads by `WhoId`);
  `Subject` filterable but not groupable; `Description` is long-text → not LIKE-filterable.
- **Every data call is direct HTTP on the CLI's cached token** (`sf/client.py`); the `sf` CLI
  is shelled out ONLY for `sf org display` / `show-access-token` (`auth_info`). Short SOQL is
  a REST `GET /query` (since 2026-09-19 — before that it shelled out to `sf data query`, which
  is where the double-quote ban, the `FORCE_COLOR` env dance and the cp1252 decode crash came
  from; the env fix stays because `org display --json` is still parsed). **Large SOQL
  auto-routes to Bulk API 2.0:** a GET puts SOQL in the URL, so long `IN(...)` lists overflow the
  header limit → HTTP 431; `SfClient.query` routes SOQL over 2000 chars to a **direct** Bulk
  query job (`_query_bulk`, since 2026-07-25 — the `sf data export bulk` shell-out polled status
  every 5 s and cost ~4-7 requests where 2 do). Its rules, do NOT re-tune without re-measuring:
  read `/results` as the completion probe (no status poll on the happy path); page via
  `Sforce-Locator`; NEVER send `maxRecords` (it only shrinks the page ⇒ more calls); a first-page
  4xx means "not ready" but a mid-pagination failure stays fatal (a retry restarts at page 1 and
  duplicates rows); an EMPTY body is confirmed with a state poll, never trusted as "no rows".
  **Bulk records are stringly typed** (the results are CSV): booleans arrive as `'true'`/`'false'`
  strings (`'false'` is truthy!) and nulls as `''` — coerce with an `_is_true`-style helper (see
  `extract/parsers.py`, `convert/preflight.py`, `shadow_compare.py`); live-proven 2026-07-06 when
  299 pending predictions all "resolved" as misses.
- **Several INDEPENDENT reads go through `SfClient.query_many`**, which bundles them into one
  `/composite` request — ≤25 subrequests bill as a **single** API call (unlike `/composite/batch`,
  which bills each). Used by `shadow_compare` (accounts + contacts) and `convert/pool_enrich`
  (accounts + leads). A dependent read must stay its own call, and a query over 2000 chars falls
  back to its own bulk job. `done == false` is never returned as the answer — `nextRecordsUrl` is
  followed, because a truncated read is a plausible-looking wrong answer.
- **API-call budget is a standing constraint** (the org's allocation is shared with other teams'
  integrations): state the cost of any new query/write before adding it, prefer direct HTTP on the
  cached CLI token > `sf` CLI, `/composite` > N reads, one batched `IN (...)` > per-record loops,
  and never put a query inside a per-batch loop. The reusable reviewer for this is the
  **`api-budget-auditor` subagent** in the sibling repo
  (`../Main-Profile-Account/.claude/agents/`), whose `common/sf_bulk.py` is the prior art the
  bulk transport above was ported from.

### CI & scheduling

- **CI Salesforce login is flaky** (`sf org login sfdx-url` hangs intermittently — CLI issue, not a
  bad credential, which prints `invalid_grant`). Both workflows retry login 3× and pin
  `@salesforce/cli@2.140.6`; job-level `SF_AUTOUPDATE_DISABLE`/`SF_DISABLE_TELEMETRY`.
- **GitHub cron delivers this repo's scheduled runs hours late and drops most firings** — so Stage 1
  scheduling moved to an **external cron-job.org job** hitting `workflow_dispatch` (see Status; the
  GitHub `schedule:` block is gone). NEVER gate on execution wall-clock hour (the old Vienna-hour
  check silently skipped every run).
- **The `runner` context does not exist in a job-level `env:` block, and neither does
  `secrets`** — a job-level `KUNUNU_CACHE: ${{ runner.temp }}/...` makes the file an INVALID
  WORKFLOW, which GitHub reports as a 0-second `startup_failure` with no job, no log and no API
  annotation. Cost on 2026-08-19: every push created three red runs and `stage1.yml` was dead for
  ~20 minutes across two scheduled dispatches before it was reverted. Env that needs `runner` or
  `secrets` goes on the STEP, where both are available. There are only three workflows
  (`stage1.yml`, `stage2.yml`, `backtest-skipped.yml`) — a fourth was added that day for the
  Enriching sheet and reverted the same day: it was a dry Stage 1 with a different artifact name,
  i.e. Stage 1 with extra steps. **The sheet is a Stage 1 feature, not a workflow** (Wasil,
  2026-08-19).

### LeadFeeder & kununu feed

- **LeadFeeder API** (`api.leadfeeder.com/v1`, `X-Api-Key`): 100 req/min per account;
  `POST /companies/match` ≤200 companies/call, `GET /companies|/contacts?ids=` ≤100 ids; match +
  contact-search are FREE, retrieves cost 1 credit/new record (free re-access for 12 months);
  `/contacts/search` returns no inline email/phone (separate retrieve). Retrieve payloads carry
  explicit `"name": null` entries — drop falsy items before joining lists. Match `country`/
  `country_code` filters SUPPRESS good matches → we deliberately send only `company_name`/`email`
  (+ alt names); `country_code` must be uppercase if ever reintroduced.
- **Never send a blank/garbage `email` in a match payload — one poisons the whole chunk.**
  LF answers `"email": ""` (and any non-address) with `400 invalid_parameter: The 'email'
  field must contain a valid email address.`; omitting the key is accepted. Because
  `match_companies` degrades a failed chunk to `None`, a single such lead turned **up to
  200** leads into `no_lf_match` → the rule engine's terminal `skip` — a batch-scale silent
  wrong answer that looks exactly like "LF knew none of them". Found + fixed 2026-08-08
  (probed live; `enrich.py` had shipped `email or ""` since the start). `client.match_email`
  /`match_api_payload` now sanitize at the transport boundary, so no caller can reintroduce
  it; the local name-agreement gate still scores against the ORIGINAL payload, so stripping
  costs no matching signal.
- **LF match acceptance:** `match_score` alone is untrustworthy (0.9+ on wrong companies, <0.2 on
  exact-name hits). The name-agreement gate (`client._name_agreement`/`_best_match`) accepts on
  score ≥ floor AND name-agree ≥ 85, or rescues on name-agree ≥ 90 (token_set on suffix-normalized
  names + corporate email-domain root; freemail excluded). Tunables `matching.lf_name_agree_{min,rescue}`.
  Agreement is the max across profile name / feed alt names / email-domain root; `agreed_via` and
  `name_agreement` flow into the QA Review sheet. Normalizers live in `textnorm.py` — do NOT import
  `match.py` from the LF client (import cycle via `enrich`).
- **Second-chance match: unmatched leads are re-asked at `max_results_per_company: 20`
  (2026-08-25).** `no_lf_match` is a TERMINAL skip and it was **101 of 523 distinct shadow
  predictions (19.3%)** — the largest single loss of coverage in the pipeline. One cause is
  that the primary call asks for LF's top 3 candidates while LF holds several company
  records for one company and ranks the wrong (emptier) one first. `enrich_leads` now
  re-asks the SAME payload, once, for the leads that matched **nothing**
  (`leadfeeder.match_wide_results`, 0 disables); `_best_match` gates every extra candidate,
  so the pass cannot accept anything the narrow call rejected, and a matched lead is never
  re-asked — **that asymmetry is the whole safety argument**. `EnrichedLead.match_pass`
  (`primary` / `wide` / `""`) rides to the QA sheet (`lf_match_pass`) and `Prediction`.
  Cost: one free call per 200 unmatched leads, 0 credits; a recovered lead pays the usual
  1 credit at phase 2 and still faces `_match_sanity_reason`.
  **MEASURED and DISABLED the same day (`match_wide_results: 0`): it recovered 0 of 103.**
  `python -m leadauto wide-match-backtest` replayed every historical `no_lf_match` lead at
  both widths — 78 still match nothing, 25 now match at width 3 anyway (LF index drift, not
  width), **0 recovered**. The code, the tests and the backtest stay: the duplicate-company
  mechanism is real in `Contact Presence Check`'s data, it just does not bite on this
  population, whose unmatched tail is micro-businesses genuinely absent from LF. Re-enabling
  is one config line. Do NOT re-litigate it as a win without re-running that backtest.
  **A `wide` match is held OUT of live scope** (`match_pass_wide`) until its accuracy is
  measured here — flip `pipeline.live_scope.allow_wide_match`. Prior art, the measurements
  and why the OTHER half of that rescue pass (extra `url`/`vat_id`/`city` identifiers) was
  deliberately NOT ported: `2026-08-25-second-chance-match.md`. **Never widen the PRIMARY
  pass and never add an identifier to it** — measured on 80 already-matched accounts in
  `Contact Presence Check`, `url` moved 8 and lost 4, `vat_id` 5/2, `city+postal` 4/4: LF
  reads an extra identifier as a request for a more specific (emptier) legal entity.
  `MATCH_RESULTS_PRIMARY = 3` is a named constant for that reason.
- **Contact-search cascade returns only NAME-VERIFIED hits (fixed 2026-08-10).** All four
  attempts of `client.search_contact` now filter candidates through one predicate
  (`_all_parts_ok`: every part of the searched name must appear in LF's `full_name`, hyphen
  segments counted). Three bugs of one class were removed: (a) attempts 1-2 returned
  `contacts[0]` **unverified**, and attempt 2 ORs the name parts, so it matched anyone sharing
  *either* name; (b) `[0]` was taken instead of scanning the page, and LF's ordering is **not
  stable** (replaying "Peter Schmidt" returned "Tim Schmidt" where the previous run gave
  "Roland Schmid"); (c) attempt 3 verified only the FIRST name, assuming its server-side
  surname search was exact — **it is fuzzy and also matches the title field**: searching
  "Hübner" returns "Markus Huber", "Weidemann" returns "Paul Weimann", so a lead named "Paul
  Weidemann" was accepted as "Paul Weimann". Measured by `Contact Presence Check` over 100
  contacts: **13 of 64 hits (20%) were a different person**. This is not just a mislabel here —
  `enrich.py` feeds the hit to `retrieve_contacts`, which spends a credit and writes that
  person's email/phone onto a real Lead. Because a filtered attempt leaves `contacts` empty,
  the `if not contacts:` guards now fall through to the later attempts instead of stopping on
  a stranger. Recall cost measured at **zero** (all 13 rejected people are genuinely absent
  from LF). Regression tests use the real name pairs; do not loosen the predicate to an
  edit-distance rule — "Schmidt"/"Schmid" and "Christian"/"Christina" are each one edit from a
  *wrong* person.
- **A dropped socket is retried on GETs ONLY (fixed 2026-08-17).** `_request` retried
  `RETRYABLE_STATUS` and nothing else, so a `ConnectionError`/`Timeout`/`ChunkedEncodingError`
  raised out of `session.get` before the status check ever ran — fatal to a long run, or a
  200-record chunk silently written off by the per-chunk `except RequestException` handlers.
  `RETRYABLE_TRANSPORT` now feeds the same `_retry_delay` + `sleep_fn` backoff. **GET only, and
  do not "fix" that**: POST `/contacts/find-contact-data-jobs` is not idempotent and a blind
  re-POST bills the credits twice; a POST logs and re-raises on the first failure. Exhaustion
  re-raises the ORIGINAL exception, not `LeadfeederError`, so the chunk handlers still degrade
  exactly as before. Found by `Contact Presence Check`, whose `enrich_pilot.get_with_retry` is
  the prior art — a `WinError 10053` killed a 50-minute run there AFTER 485 retrieve credits
  were spent, with 9 more drops recovered in the same run.
  **Half of that fix was dead on arrival without `DEFAULT_TIMEOUT = (10, 180)`** (same day,
  measured): `requests` with no `timeout=` waits forever, so nothing ever raised `Timeout` and
  a STALLED socket — as opposed to a dropped one — hung the process instead of retrying. A
  backtest sat 26 minutes on one established connection to `api.leadfeeder.com` at 0% CPU
  before it was killed by hand; the read budget is 180 s because a 200-company
  `/companies/match` is genuinely slow. `SfClient` has had `_HTTP_TIMEOUT_S = 120` all along —
  the LF client was the outlier. It earned its keep on the next run, where a
  `POST /companies/<id>/lists` timed out, logged, was not retried, and the run continued.
- **Post-retrieve match sanity gate** (`enrich._match_sanity_reason`, 2026-07-09, from the live
  "SNG"→France miss): after the phase-2 company retrieve (match responses carry no country/domain),
  an accepted match is degraded to unmatched (→ terminal skip; reason in `EnrichedLead.lf_rejected`
  + QA `lf_rejected` column) when (a) the company's country contradicts the lead's
  (`textnorm.country_iso2`, fails open on unmapped names) or (b) the lead name is acronym-like
  (single token ≤3 chars, or 4-char uppercase) and neither lead-email-domain ↔ company
  website/email domain nor VAT equality corroborates — acronym name agreement is tautological
  since LF searched on the acronym we sent.
- **THE FEED ENDPOINT, CACHE PATH AND PARSER ARE NOT OWNED HERE (since 2026-08-17).** They come
  from `common.kununu_feed` in the Main-Profile-Account repo, reached through
  `src/leadauto/mpa_bridge.py` (`MPA_ROOT` env, else the sibling `../Main-Profile-Account`,
  else the vendored snapshot — see the CI bullet below). **kununu switched the feed from
  JSONL to CSV on 2026-08-06 and the private reader here never followed** — it requested a
  hardcoded `profiles.jsonl` that has 404'd ever since, and because every failure path returns
  `{}` the pipeline logged ONE warning and ran eleven days with no alt names and, worse, **no
  `profile_score`, so the live-scope gate failed closed and nothing could have converted even
  with `dry_run: false`**. `common` had followed the switchover the same day and sniffs CSV vs
  JSONL from the file CONTENT. Two consequences to keep: `url` and `cache_path` are **absent**
  from `config.yaml` on purpose (a default is how this drifts again — override with
  `KUNUNU_FEED_URL` / `KUNUNU_CACHE`, which `common` reads at call time), and the old
  `cache_path` default also wrote ~200 MB into the OneDrive-synced checkout. **UUID lookup is
  now case-insensitive** — the feed lowercases, Salesforce does not, and the old exact compare
  silently dropped those matches.
  **CI reads the feed off a VENDORED SNAPSHOT of `common/`** (Wasil's call, 2026-08-19):
  `src/leadauto/_vendor/common/{__init__,kununu_feed}.py` are byte-identical copies, because the
  reader lives in another private repo and the `GITHUB_TOKEN` Actions injects is scoped to this
  one — the alternatives were a classic PAT (read on every repo its owner can see) or a deploy
  key on someone else's repo. **This deliberately relaxes the global "import it, never vendor
  it" rule, so do not treat the guards as optional:** `common_kununu_feed()` still prefers the
  REAL checkout and only falls back, at WARNING level, so every local run and any CI run with
  `MPA_ROOT` pointed at a checkout executes the canonical module; one that is PRESENT but broken raises
  instead of falling back (different fault, different fix); and `tests/test_mpa_vendor.py` hashes
  the snapshot against the live files (CRLF-normalised) and FAILS on drift, skipping only where
  Main-Profile-Account is absent — i.e. it fires on the machines where the suite is actually run.
  **Never edit the copies** — fix upstream and re-sync per `_vendor/PROVENANCE.md`, which is the
  ONE place that records the source commit and snapshot date (do not copy them here). Two traps it carries: on the snapshot `common`'s default cache
  anchors on ITS `REPO_ROOT` = `_vendor/`, i.e. ~200 MB back inside the OneDrive checkout, so
  `feed_cache_path` redirects to the temp dir when `KUNUNU_CACHE` is unset (CI sets it); and only
  two files are vendored because `kununu_feed` imports nothing else from its package (`from .
  import REPO_ROOT`) — a test pins that, and a new intra-package import must be vendored with it.
  No cross-repo checkout is wired into CI: setting `MPA_ROOT` on a runner would upgrade it to the
  canonical reader, but that needs a credential for a repo owned by someone else, which is the
  whole reason the snapshot exists.
  Two other CI bugs were fixed alongside: the workflows passed **`KUNUNU_API_TOKEN`**, which
  `common.kununu_feed` refuses by design (the feed is Basic, not Bearer) so the feed was
  unauthenticated even with a checkout — the secret of that name holds the password and is now
  mapped to `KUNUNU_API_PASSWORD`; and the `actions/cache` block still named
  `kununu_profiles.jsonl`, so it cached a path nothing writes — it now caches the `KUNUNU_CACHE`
  dir under `runner.temp` (`profiles.csv`, key `kununu-feed-csv-<date>`), out of the checkout.
- **kununu profile feed** (`kununu_feed.py`): daily-cached DWH bulk file (~200 MB CSV;
  `assigned_companies` = list of `{company_uuid, company_name}`) supplies alt names AND
  `profile_score` (stringly typed; missing/garbage → None), the live-scope gate's input —
  a feed outage therefore fails the gate closed (no scores ⇒ nothing converts, safe); joins on
  `profile_uuid` with URL-slug fallback; degrades to no-alt behavior without `KUNUNU_API_TOKEN`
  (`--no-kununu-feed` to disable). Also planned input for a future live-lead duplicate-detection
  feature — the `kununu-data` MCP server is the same file, no per-lead API.

### Matching, rule engine & shadow backtest

- **Dublettencheck rule engine** (`match.py`): ordered 11 rules, first hit wins —
  `no_lf_match`→**skip**, `name_address`→link, `name_vat`→link, `kununu_over_name`→link,
  `kununu_agree`→link, `kununu_conflict`→review, `name_corroborated`→link, `name_only`→review,
  `duplicate`→review, `weak_signal`→review, `no_match`→create_new.
  **Invariant: no accepted LF match ⇒ terminal `skip`** — the lead is excluded from BOTH account
  and contact matching (`match_lead` short-circuits: no account, no contact, no evidence), and
  Stage 2 drops an approved `skip` row rather than converting it. **`name_corroborated`** = normalized
  name equal AND an independent weak signal (VAT/host/email-domain/fuzzy) on the SAME account
  (two axes ⇒ auto-link; if the corroborator singles out one account it also disambiguates a name
  collision). **`name_only`** = name equal on exactly one account, nothing else. **`duplicate`** =
  name equal on 2+ accounts, none singled out (the org already holds duplicate accounts) — evidence
  surfaces EVERY candidate (`name→001A,001B`) instead of silently linking the first. `review` is
  reserved for cases where a candidate account IS surfaced and a human must adjudicate (kununu_conflict
  / name_only / duplicate / weak_signal). **`kununu_over_name`** = a profile-UUID/URL account exists
  but `name_address`/`name_vat` fired on a *different* (org-duplicate) account → link the kununu
  account, not the name/address duplicate (humans followed the profile account 14/14 in the
  2026-07-08 backtest); evidence surfaces both (`name→<dup>` + `kununu_profile_uuid→<won>`). Only the
  cross-account conflict triggers it — same-account name_address/name_vat keep their own rule.
  Decisions: `link | review | create_new | skip`. In shadow-compare a `skip` lead a
  human later converted scores as a coverage-gap **miss**. No score thresholds; `MatchResult`
  carries `rule` + `evidence`
  (demoted VAT/host/email-domain/fuzzy hits are review evidence only). `name_address` =
  normalized name + `BillingPostalCode` + `BillingCity`; `kununu_agree` splits on
  `matching.dedupe_name_agree_min` (85). QA Review sheet: `match_rule`/`match_evidence`.
- **Skipped-task backtest (2026-07-08):** the rule engine scored **87% correct account on 283**
  contact-reparented (link-biased) cases — validates dedupe, NOT `create_new`. Gotcha: ~280 leads
  in one match batch blow the **100k-char SOQL limit** in `accounts_by_signals` — chunk
  `load_existing_records` in batches of 40. Full record (population breakdown, per-miss analysis,
  `backtest-skipped` mechanics): `docs/skipped-task-backtest-2026-07-08.md`.
- **Shadow backtest:** every DRY stage-1 run appends predictions to committed
  `state/predictions.jsonl`; `python -m leadauto shadow-compare` (in the scheduled workflow,
  read-only) scores them against real human conversions into `state/shadow_results.jsonl` +
  a `shadow_comparison.xlsx` artifact. `Lead.ConvertedDate` is Date-only → "human created a new
  account" = `Account.CreatedDate` same UTC day. Field checks mirror the write-back payload
  (`PostalCode`/`City`/`CountryCode` vs the converted account's Billing address,
  `FirstName`/`Phone` vs the converted Contact, `KN_Employee__c` vs the lead); legacy
  `KN_DB*` payload keys still score in-flight pre-KN_DB-removal predictions.
  **The compare self-gates to ~once a day** (`--min-interval-hours`, default 20; `0` forces it):
  it costs ~7-9 Salesforce requests and scores HUMAN conversions, which resolve over days, so
  running it on all 4 daily dispatches re-read the same predictions. The gate reads a committed
  timestamp (`state/shadow_state.json`, written by `record_compare_run`, committed by
  `stage1.yml`) — NOT the wall-clock hour, which silently skipped every run once (see CI &
  scheduling) — and fails OPEN on an unreadable file. **The xlsx artifact is produced on
  EVERY run (2026-09-22):** a gated-off or nothing-pending run writes it from committed
  state via `write_skipped_report` (empty Decisions/Fields, rolling Summary with
  `compare_ran=no`, `skip_reason`, `last_compare_at`; zero org calls), so a missing
  `lead-automation-shadow-<run_id>` artifact always means the step failed, never a skip.

### Repo layout

- **`scripts/` == legacy `Scripts/` on case-insensitive Windows** (same physical dir).
  `.gitignore` ignores `scripts/*` and tracks ONLY `leadfeeder_probe.py` +
  `control_check_alt_names.py`. The untracked `Convert Leads/` folder is the legacy manual
  converter (gitignored; keepers preserved in `docs/legacy/convert-leads/`).

## Open items (resume here)

0. **Closed-task backlog conversion: DONE 2026-07-17** (230 leads converted; full record in
   `docs/backlog-conversion-2026-07.md`). Two standing consequences: **keep `--batch-size` ≤3 for
   live conversions** (batched SOAP `convertLead()` runs one transaction per call and ~5
   conversions' cascaded triggers/flows exhaust a per-transaction governor limit), and the
   remaining follow-up batches are 36 preflight drops + 32 review + 27 link rows.

1. **Phased go-live: DONE 2026-07-18** (see Status GO-LIVE bullet) — `review_gate: false`
   committed, live-scope gate shipped, first automated E2E run converted 3/3. **The one
   remaining move to full automation:** change the cron-job.org payload to
   `{"ref":"master","inputs":{"dry_run":"false"}}` so scheduled runs go live (they currently
   stay dry — dispatch with dry_run unchecked for a manual live run). Since 2026-10-19 a
   live run is enrich-only for claimer (see the Split bullet), so flipping the payload
   writes back claimer leads and converts nothing. Widening the scope
   (add `ebp_trial` to `live_scope.task_types`, decisions back into `live_scope.decisions`
   to auto-convert) is a config commit,
   informed by shadow accuracy (item 4). A required-reviewers Environment is NOT possible
   (needs a paid plan on private repos, verified 2026-07-10).
2. **Port `lf_name_dropped` from `Contact Presence Check`** — the highest-value LF-matching
   work here, and the 2026-08-25 backtest is why. `lf_name_agreement = 100` does NOT mean
   the right company: the gate scores a TRUNCATED name as perfect agreement, so
   `match_score` as low as **0.05** passes via the `name_agree_rescue` band. Of 25
   `no_lf_match` leads that would match today, **11 matched a strictly shorter name** —
   `SJD – Die Falken | LV NRW` -> `FALKEN` (0.24), `4 Directions Management Consultants
   GmbH` -> `4° Management` (0.18), `BARDINI + KELLER AG` -> `Bardini` (0.12),
   `Topgolf Wien` -> `Topgolf`. CPC measured the same class at **1,052 of 13,253 matched
   accounts (7.9%), all at agreement 100**, and `lf_name_dropped` is the only column that
   sees it. Offline, free, no new call. This does not indict the gate — it was calibrated
   for *lead* matching, where a parent hit is still useful — it indicts trusting agreement
   100 with no flag. Detail: `2026-08-25-second-chance-match.md`.
   - Also from that backtest: **25 terminally-skipped leads would match today**, coverage
     the pipeline lost and never revisits (a `no_lf_match` skip is terminal and the
     lookback has passed those tasks). Recoverable, but they need a human read — see the
     table in that doc, not an automatic conversion.
   - Two dead ends recorded so no one re-derives them: `ExtractedLead.vat_number` was filled
     by **NO parser** and was REMOVED 2026-09-19 (so was the Register_Id-vs-DUNS `dbid` axis, which could never match); `Lead.Website` is populated
     on only **3.3%** of queue-owned leads — which is why no identifier rescue arm exists.
3. **Task-type rollout:** `claimer` + `ebp_trial` are enabled; add `event`, then `content`,
   calibrating from the review sheet's false-match rate.
4. **Validate the rule engine WITH LeadFeeder:** largely answered by the 2026-07-08 skipped-task
   backtest (87% correct on 283 link-biased cases — see Matching section). Follow-ups from its
   misses: (a) **DONE (2026-07-08): `kununu_over_name` rule shipped** — the kununu-identity account
   (profile-UUID/URL hit) now wins over a `name_address`/`name_vat` hit on a *different* account,
   linking the kununu account (humans followed it 14/14 in the misses) rather than demoting to
   review. Standalone `kununu_conflict` review is unchanged; `name_corroborated`/`name_only`/
   `duplicate` were already unreachable when a kununu account exists. (b) **DONE
   (2026-07-10): `accounts_by_signals` recall gap closed** — normalized `Name LIKE`
   widening (see Matching section). Still read the scheduled dry-run QA artifacts for
   the create_new side, which the backtest cannot validate.
5. **Review shadow accuracy after a few days:** `shadow_comparison.xlsx` (Decisions = per-lead
   hit/miss vs the human-chosen account; Fields = enrichment agreement) — the go-live evidence.
6. **Write-back failure handling: RESOLVED 2026-07-18** — all record updates (Lead write-back,
   pool Accounts, Contact KN_Email2__c) moved off CLI bulk jobs onto `SfClient.update_records`
   (REST sObject Collections PATCH, ≤200 records/request, allOrNone=false): per-row errors are
   inline in the one response, so the bulk-shape/`confirmed=False` question is gone.
   Live-proven small-scale 2026-07-18 (go-live run: 3 leads written, 0 failures). Still open
   from the old item: watermark exclusion for failed rows vs. Failures sheet + overlap-window
   retry.
7. **Minor:** `sf/auth.py` writes the auth URL to a predictable temp path; `stage2_convert`
   re-derives the auto-link predicate; `stage1.yml` push lacks rebase/retry (mitigated by the
   concurrency group); add `caplog` assertions to the LF chunk-failure tests;
   `qa_artifact._convert_blockers` false-positives `postal_length` when the payload changes the
   country (it merges payload `CountryCode` over the lead's stale `Country` name — leads default
   to Germany; the real preflight reads post-write-back state and passes, proven on the go-live
   run). **Test gotcha (fixed 8ee8eb8, keep honoring it):** any `dry_run=False` `run_stage1`
   test MUST pass `review_dir=tmp_path` — with `review_gate: false` in config it otherwise
   writes a fake auto-approved CSV into the repo's real `review/` dir, which Stage 1's
   detect-step glob (`review/approved_*_auto.csv`, first untracked match) could feed to Stage 2.

## Git

Remote `github.com/wasil-mustafa/lead-automation` (private). **Default branch `master` is what the
scheduler runs** — fixes must land there; no active feature branch. PR history lives in
git log/GitHub — don't re-document it here.
