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
or `stage2.yml` · legacy manual-conversion lessons `docs/legacy/convert-leads/`.

## Architecture — three object-centric modules

Code lives under `src/leadauto/`. Each module is independently runnable.
1. **Salesforce extraction** (`sf/`, `extract/`) — SOQL pulls new Leads + their Tasks; a
   `Subject → parser registry` (`extract/parsers.py`) routes each Task type to its own parser,
   emitting a normalized `ExtractedLead` (`extract/models.py`).
2. **LeadFeeder** (`leadfeeder/`) — match/contact/company-enrichment client. `client.py` centralizes a
   throttle (~95/min) + `429`/`5xx` retry (Retry-After/backoff) in `api_get`/`api_post`, and batches
   `match_companies` (≤200) / `retrieve_contacts` (≤100). `enrich.py` does one batched match (phase 1)
   and one batched contact retrieve (phase 3). Tunables in `config.yaml` `leadfeeder:`.
3. **Lead enrichment + conversion** (`match.py`, `writeback.py`, `convert/`) — rule-based match
   vs existing Accounts/Contacts → write-back to `KN_*` fields → gated, match-aware LeadConvert.

Cross-cutting: `config.py`, `logging.py`, `state.py` (file watermark), `qa_artifact.py`,
`pipeline/stage1_enrich.py` + `stage2_convert.py`, `cli.py`, `.github/workflows/`.

## Commands

```powershell
# Tests (pytest is configured with pythonpath=src via pyproject.toml) — 340 pass
python -m pytest -q

# Run a stage locally (set LEADFEEDER_API_KEY / KUNUNU_API_TOKEN as ad-hoc env vars if needed)
$env:PYTHONPATH = "src"; python -m leadauto stage1 --dry-run --no-leadfeeder
$env:PYTHONPATH = "src"; python -m leadauto stage2 --dry-run
$env:PYTHONPATH = "src"; python -m leadauto shadow-compare

# Read-only org inspection (alias `headless`); aggregates need --json, NOT csv
sf data query --query "SELECT ... FROM Lead WHERE ..." --target-org headless --result-format json

# Read-only LeadFeeder live probe
$env:PYTHONPATH = "src"; python scripts/leadfeeder_probe.py "<company>" "<cc>"
```

## Status

- **Built & unit-tested:** all three modules + cross-cutting; 340 tests green.
- **Live-validated (read-only):** CI Salesforce auth, Stage 1 extract→QA path, LeadFeeder
  match/contact cascade (org `headless`, LF account kununu Austria GmbH/276820; note: direct
  contact email/phone can be sparse on this LF tier).
- **Scheduled:** Stage 1 is triggered by an **external cron-job.org job** (id 8029899) hitting the
  `stage1.yml` `workflow_dispatch` at 09/12/15/18 Vienna; it POSTs `{"ref":"master"}` with no
  inputs ⇒ `dry_run` defaults **true** (runs stay dry; go live by dispatching with dry_run
  unchecked). Change the schedule in *that job*, not the workflow. GitHub's own `schedule:` cron
  was removed. Shadow files committed every run; watermark only on non-dry. Stage 2 is dispatch-only.
- **Scope:** `enabled_task_types` = `claimer`, `ebp_trial`; config `dry_run: true`.
- **NOT yet live:** Salesforce write-back and LeadConvert — no live write has ever run.
  The Dublettencheck rule distribution has only been smoke-tested WITHOUT LeadFeeder.
- **Write-back fills conversion requirements** (NoOwnDBID tick, address via CountryCode, employee
  fallback, FirstName, Phone — fill-only-blank); **Stage 2 preflights** approved leads and
  skips+reports rule violations (preflight_failures.csv).
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
- **Conversion (Stage 2):** see `src/leadauto/convert/CLAUDE.md` — mechanics, safety model,
  duplicate/validation-rule gotchas, live-testing plan.
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
  Wasil (deliberate, 2026-07-05 — converted records are assigned to him).
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
- **`sf` is wrapped** in `sf/client.py` via `subprocess(shell=True)` with double-quoted `--query`.
  Payloads go to temp files for apex/bulk. **Large SOQL auto-routes to Bulk API 2.0:** a plain
  `sf data query` puts SOQL in the GET URL, so long `IN(...)` lists overflow the header limit →
  HTTP 431; `SfClient.query` routes SOQL over 2000 chars through `sf data export bulk --query-file`.
  (`sf data query` has no `--bulk`/`--wait` flags.) **Bulk-export records are stringly typed** even
  with `--result-format json`: booleans arrive as `'true'`/`'false'` strings (`'false'` is truthy!)
  and nulls as `''` — coerce with an `_is_true`-style helper (see `extract/parsers.py`,
  `convert/preflight.py`, `shadow_compare.py`); live-proven 2026-07-06 when 299 pending
  predictions all "resolved" as misses.

### CI & scheduling

- **CI Salesforce login is flaky** (`sf org login sfdx-url` hangs intermittently — CLI issue, not a
  bad credential, which prints `invalid_grant`). Both workflows retry login 3× and pin
  `@salesforce/cli@2.140.6`; job-level `SF_AUTOUPDATE_DISABLE`/`SF_DISABLE_TELEMETRY`.
- **GitHub cron delivers this repo's scheduled runs hours late and drops most firings** — so Stage 1
  scheduling moved to an **external cron-job.org job** hitting `workflow_dispatch` (see Status; the
  GitHub `schedule:` block is gone). NEVER gate on execution wall-clock hour (the old Vienna-hour
  check silently skipped every run).

### LeadFeeder & kununu feed

- **LeadFeeder API** (`api.leadfeeder.com/v1`, `X-Api-Key`): 100 req/min per account;
  `POST /companies/match` ≤200 companies/call, `GET /companies|/contacts?ids=` ≤100 ids; match +
  contact-search are FREE, retrieves cost 1 credit/new record (free re-access for 12 months);
  `/contacts/search` returns no inline email/phone (separate retrieve). Retrieve payloads carry
  explicit `"name": null` entries — drop falsy items before joining lists. Match `country`/
  `country_code` filters SUPPRESS good matches → we deliberately send only `company_name`/`email`
  (+ alt names); `country_code` must be uppercase if ever reintroduced.
- **LF match acceptance:** `match_score` alone is untrustworthy (0.9+ on wrong companies, <0.2 on
  exact-name hits). The name-agreement gate (`client._name_agreement`/`_best_match`) accepts on
  score ≥ floor AND name-agree ≥ 85, or rescues on name-agree ≥ 90 (token_set on suffix-normalized
  names + corporate email-domain root; freemail excluded). Tunables `matching.lf_name_agree_{min,rescue}`.
  Agreement is the max across profile name / feed alt names / email-domain root; `agreed_via` and
  `name_agreement` flow into the QA Review sheet. Normalizers live in `textnorm.py` — do NOT import
  `match.py` from the LF client (import cycle via `enrich`).
- **kununu profile feed** (`kununu_feed.py`): daily-cached DWH bulk file (551 MB;
  `assigned_companies` = list of `{company_uuid, company_name}`) supplies alt names; joins on
  `profile_uuid` with URL-slug fallback; degrades to no-alt behavior without `KUNUNU_API_TOKEN`
  (`--no-kununu-feed` to disable). Also planned input for a future live-lead duplicate-detection
  feature — the `kununu-data` MCP server is the same file, no per-lead API.

### Matching, rule engine & shadow backtest

- **Dublettencheck rule engine** (`match.py`): ordered 10 rules, first hit wins —
  `no_lf_match`→**skip**, `name_address`→link, `name_vat`→link, `kununu_agree`→link,
  `kununu_conflict`→review, `name_corroborated`→link, `name_only`→review, `duplicate`→review,
  `weak_signal`→review, `no_match`→create_new.
  **Invariant: no accepted LF match ⇒ terminal `skip`** — the lead is excluded from BOTH account
  and contact matching (`match_lead` short-circuits: no account, no contact, no evidence), and
  Stage 2 drops an approved `skip` row rather than converting it. **`name_corroborated`** = normalized
  name equal AND an independent weak signal (VAT/DBID/host/email-domain/fuzzy) on the SAME account
  (two axes ⇒ auto-link; if the corroborator singles out one account it also disambiguates a name
  collision). **`name_only`** = name equal on exactly one account, nothing else. **`duplicate`** =
  name equal on 2+ accounts, none singled out (the org already holds duplicate accounts) — evidence
  surfaces EVERY candidate (`name→001A,001B`) instead of silently linking the first. `review` is
  reserved for cases where a candidate account IS surfaced and a human must adjudicate (kununu_conflict
  / name_only / duplicate / weak_signal). Decisions: `link | review | create_new | skip`. In shadow-compare a `skip` lead a
  human later converted scores as a coverage-gap **miss**. No score thresholds; `MatchResult`
  carries `rule` + `evidence`
  (demoted VAT/DBID/host/email-domain/fuzzy hits are review evidence only). `name_address` =
  normalized name + `BillingPostalCode` + `BillingCity`; `kununu_agree` splits on
  `matching.dedupe_name_agree_min` (85). QA Review sheet: `match_rule`/`match_evidence`.
- **Shadow backtest:** every DRY stage-1 run appends predictions to committed
  `state/predictions.jsonl`; `python -m leadauto shadow-compare` (in the scheduled workflow,
  read-only) scores them against real human conversions into `state/shadow_results.jsonl` +
  a `shadow_comparison.xlsx` artifact. `Lead.ConvertedDate` is Date-only → "human created a new
  account" = `Account.CreatedDate` same UTC day. Field checks mirror the write-back payload
  (`PostalCode`/`City`/`CountryCode` vs the converted account's Billing address,
  `FirstName`/`Phone` vs the converted Contact, `KN_Employee__c` vs the lead); legacy
  `KN_DB*` payload keys still score in-flight pre-KN_DB-removal predictions.

### Repo layout

- **`scripts/` == legacy `Scripts/` on case-insensitive Windows** (same physical dir).
  `.gitignore` ignores `scripts/*` and tracks ONLY `leadfeeder_probe.py` +
  `control_check_alt_names.py`. The untracked `Convert Leads/` folder is the legacy manual
  converter (gitignored; keepers preserved in `docs/legacy/convert-leads/`).

## Open items (resume here)

1. **End-to-end live validation:** small live write-back (`stage1 --limit 5`, inspect `KN_*` on
   those Leads) → one gated `stage2` conversion. Prereqs + risk-ordered 3-path testing plan in
   `src/leadauto/convert/CLAUDE.md` (create the `lead-conversion` GitHub Environment FIRST — it
   does not exist yet).
2. **Task-type rollout:** `claimer` + `ebp_trial` are enabled; add `event`, then `content`,
   calibrating from the review sheet's false-match rate.
3. **Validate the rule engine WITH LeadFeeder:** read the `match_rule`/Summary breakdown of the
   next scheduled dry-run QA artifact; calibrate `dedupe_name_agree_min` if needed.
4. **Review shadow accuracy after a few days:** `shadow_comparison.xlsx` (Decisions = per-lead
   hit/miss vs the human-chosen account; Fields = enrichment agreement) — the go-live evidence.
5. **Write-back idempotency (live-validation task):** confirm the real `sf data update bulk --json`
   shape (the parser reports `confirmed=False` on unrecognized shapes → QA **Failures** sheet),
   then decide watermark exclusion for failed rows vs. Failures sheet + overlap-window retry.
6. **Minor:** `sf/auth.py` writes the auth URL to a predictable temp path; `stage2_convert`
   re-derives the auto-link predicate; `stage1.yml` push lacks rebase/retry (mitigated by the
   concurrency group); add `caplog` assertions to the LF chunk-failure tests.

## Git

Remote `github.com/wasil-mustafa/lead-automation` (private). **Default branch `master` is what the
scheduler runs** — fixes must land there; no active feature branch. PR history (#1–#22: LF
throttle/batching, Bulk-API 431 fix, CI login hardening, name-agreement gate, alt-names gate,
country-drop, employee bins, LF null-crash fix, queue scope + schedule, rule engine + shadow,
Task-status freshness, scheduled-gate fix, write-back gaps + Stage 2 preflight, own-company email
exclusion, Stage 2 conversion hardening, pool-account enrichment + same-company dedup, terminal
no_lf_match skip, name_corroborated + duplicate rules) lives in git log/GitHub — don't re-document
it here.
