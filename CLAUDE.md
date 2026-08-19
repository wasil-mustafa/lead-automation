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
  FirstName (nice-to-have). **Post-convert (Stage 2, `convert.enrich_contact_email2`):** on NEW
  Contacts only, the LF contact email lands in Contact `KN_Email2__c` when it differs from the
  primary Email (fill-only-blank, best-effort; carried via the Review sheet's `lf_contact_email`
  column). **Stage 2 preflights** approved leads and skips+reports rule violations
  (preflight_failures.csv).
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

- **Task `Subject` is the task-type router** (`config/config.yaml` `task_types`). In scope:
  `Inbound Sales Task: {New Claimed Profile | EBP Trial | Eventteilnahme | Whitepaper Download |
  Video Download}`. Out: `Kontaktanfrage` (stays human), `Marketing Touchpoint: Ecom`.
  `enabled_task_types` gates the phased rollout.
- **Task `Status` is the freshness gate.** `tasks_since` filters `Status IN (fresh_task_statuses)`
  = `In Progress | in Arbeit | Not Started`. Do NOT use `IsClosed = false`: the bilingual/legacy
  picklist flags `Abgeschlossen` (Completed) as `IsClosed=false` and would leak handled tasks;
  there is no status literally named `Open`. Empty list disables the filter.
- **Leads scope:** `leads_by_ids` filters `Owner.Type = 'Queue'`. "No Demand" leads ARE in scope
  (humans set it for kununu score <3.0 — a triage practice that automation replaces); humans keep
  closing those tasks, so shadow evidence comes mostly from the non-No-Demand minority. The few
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
- **`sf` is wrapped** in `sf/client.py` via `subprocess(shell=True)` with double-quoted `--query`
  (short SOQL + apex only; apex payloads go to temp files). **Large SOQL auto-routes to Bulk API
  2.0:** a plain `sf data query` puts SOQL in the GET URL, so long `IN(...)` lists overflow the
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
- **`enriching.yml` ("Enriching") is the manual, paste-by-hand route** (2026-08-19, dispatch-only):
  a dry Stage 1 whose only output is the QA workbook's **Inspector Ready** sheet — the exact
  `lead_field_payload` in Salesforce field API names, pasted into Inspector Reloaded's Data
  Import (Lead / Update / id field Id). `--dry-run` is hardwired (no input to get wrong), it
  commits NOTHING (no watermark, no state, no approval CSV, `contents: read`) and skips
  shadow-compare so it cannot spend that once-a-day gate. It shares **stage1's concurrency
  group** on purpose: the LF rate limit and credit quota are per API key. Feed access (the
  optional `MPA_PAT` checkout + `KUNUNU_API_PASSWORD` mapping) is the same in all three
  workflows — see the kununu-feed section.

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
  `src/leadauto/mpa_bridge.py` (`MPA_ROOT` env, else the sibling `../Main-Profile-Account`;
  raises `MpaBridgeError`, never falls back to a local copy). **kununu switched the feed from
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
  **CI can now reach the feed, but only once the `MPA_PAT` secret exists** (wired 2026-08-19 in
  `stage1.yml`, `enriching.yml`, `backtest-skipped.yml`): each does a sparse `common`-only
  checkout of `Nicolaus-Pichorner/Main-Profile-Account` into `mpa/` and points `MPA_ROOT` at it,
  gated on a job-level `HAS_MPA` boolean so the step is SKIPPED — not failed — while the secret
  is absent. Until it is set, CI keeps running in the degraded no-alt-names/no-score state.
  Two other CI bugs went with it: the workflows passed **`KUNUNU_API_TOKEN`**, which
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
  name equal AND an independent weak signal (VAT/DBID/host/email-domain/fuzzy) on the SAME account
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
  (demoted VAT/DBID/host/email-domain/fuzzy hits are review evidence only). `name_address` =
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
  scheduling) — and fails OPEN on an unreadable file.

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
   stay dry — dispatch with dry_run unchecked for a manual live run). Widening the scope
   (add `link` to `live_scope.decisions`, raise `max_kununu_score`) is a config commit,
   informed by shadow accuracy (item 4). A required-reviewers Environment is NOT possible
   (needs a paid plan on private repos, verified 2026-07-10).
2. **Task-type rollout:** `claimer` + `ebp_trial` are enabled; add `event`, then `content`,
   calibrating from the review sheet's false-match rate.
3. **Validate the rule engine WITH LeadFeeder:** largely answered by the 2026-07-08 skipped-task
   backtest (87% correct on 283 link-biased cases — see Matching section). Follow-ups from its
   misses: (a) **DONE (2026-07-08): `kununu_over_name` rule shipped** — the kununu-identity account
   (profile-UUID/URL hit) now wins over a `name_address`/`name_vat` hit on a *different* account,
   linking the kununu account (humans followed it 14/14 in the misses) rather than demoting to
   review. Standalone `kununu_conflict` review is unchanged; `name_corroborated`/`name_only`/
   `duplicate` were already unreachable when a kununu account exists. (b) **DONE
   (2026-07-10): `accounts_by_signals` recall gap closed** — normalized `Name LIKE`
   widening (see Matching section). Still read the scheduled dry-run QA artifacts for
   the create_new side, which the backtest cannot validate.
4. **Review shadow accuracy after a few days:** `shadow_comparison.xlsx` (Decisions = per-lead
   hit/miss vs the human-chosen account; Fields = enrichment agreement) — the go-live evidence.
5. **Write-back failure handling: RESOLVED 2026-07-18** — all record updates (Lead write-back,
   pool Accounts, Contact KN_Email2__c) moved off CLI bulk jobs onto `SfClient.update_records`
   (REST sObject Collections PATCH, ≤200 records/request, allOrNone=false): per-row errors are
   inline in the one response, so the bulk-shape/`confirmed=False` question is gone.
   Live-proven small-scale 2026-07-18 (go-live run: 3 leads written, 0 failures). Still open
   from the old item: watermark exclusion for failed rows vs. Failures sheet + overlap-window
   retry.
6. **Minor:** `sf/auth.py` writes the auth URL to a predictable temp path; `stage2_convert`
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
