# Code Audit — Stage 1 (enrich) & Stage 2 (convert)

**Date:** 2026-07-07 · **Scope:** all of `src/leadauto/` (~4,200 LOC), both GitHub
workflows, `config/config.yaml` · **Baseline:** `master` @ `5e98f61`, working tree clean,
**345 tests passing** (CLAUDE.md says 340 — doc drift).

Findings are ranked by severity. File references are to the state of the code at the
baseline commit. Status column is for tracking fixes — update it as items land.

| # | Severity | Finding | Status |
|---|----------|---------|--------|
| H1 | High | Cluster secondaries orphaned on re-run | open |
| H2 | High | Double quote in a company name crashes Stage 1 | open |
| H3 | High | Live Stage 1 exits green on write-back failures | open |
| M4 | Medium | Name-match recall gap (exact SOQL vs normalized compare) | open |
| M5 | Medium | `CountryCode` can be written a full country name | open |
| M6 | Medium | `--limit N` interacts badly with the overlap window | open |
| M7 | Medium | No timeouts on LF HTTP or `sf` subprocess calls | open |
| M8 | Medium | `--no-leadfeeder` dry run corrupts pending predictions | open |
| M9 | Medium | Same-company dedup ignores linked targets | open |
| m10–m17 | Minor | See below | open |

---

## High — fix before go-live

### H1. Cluster secondaries are permanently orphaned on re-run

`src/leadauto/convert/converter.py:99-102` (Apex already-converted rows) and `:256-257`
(`acct_by_primary`).

The generated Apex emits `{leadId, success: true, alreadyConverted: true}` for leads that
were converted before the run — but **no `accountId`**. `convert_leads` builds
`acct_by_primary` only from rows carrying `accountId`, so an already-converted cluster
primary never resolves.

**Failure scenario:** a same-company cluster's primary converts, then the run dies before
phase 2 (or the secondaries' batch fails). On every subsequent re-run the primary returns
`alreadyConverted` without an account id → all secondaries are reported
`"primary … created no account; deferred"` → counted `failed` → CLI exits 1, forever.
This breaks the documented "re-run stays green" idempotency guarantee exactly in the
partial-failure case it exists for.

**Fix:** in the already-converted Apex query, select `ConvertedAccountId` and emit it as
`accountId` in the result row; `acct_by_primary` then resolves on re-runs with no Python
changes needed.

### H2. A double quote in any company name crashes the whole Stage 1 run

`src/leadauto/sf/client.py:89-90`.

`SfClient.query` raises `SfError` if `"` appears anywhere in the SOQL — **before** the
bulk-routing check. The guard exists for the shell-quoted `sf data query --query "…"`
path, but the bulk path (`--query-file`, `client.py:103`) carries the query in a file and
is immune.

Company names flow raw into `Name IN (...)` (`match.py:405-407` →
`soql.accounts_by_signals`, `soql.py:171`), and names come from Task Descriptions /
Lead.Company. A lead like `Café "Zentral" GmbH` makes `load_existing_records` raise,
failing the entire run. Because dry runs never persist the watermark and the task stays
fresh, **every scheduled run fails for up to 35 days** until the task ages out.

**Fix:** route quote-containing SOQL through `_query_bulk` instead of raising (move the
check inside the non-bulk branch), or strip `"` from name signals at collection time.

### H3. Live Stage 1 exits green on write-back failures

`src/leadauto/cli.py:82-89` (no exit-code handling), `src/leadauto/pipeline/stage1_enrich.py:72`
(watermark advances regardless).

`stage2` correctly exits 1 on `failed`/`missing` conversions, but `stage1` never inspects
`res.failures` or the bulk result's `confirmed` flag. On a live run: failed rows land in
the QA **Failures** sheet, the watermark still advances past them, and the job reports
success. Those leads are never retried and nothing alerts anyone. (Adjacent to tracked
open item 5 — write-back idempotency / watermark exclusion — but the exit-code gap is
untracked.)

**Fix (minimum):** exit non-zero when `failed > 0` or `confirmed == False` on a non-dry
run. Longer-term, resolve with open item 5 (exclude failed leads from the watermark or
add an overlap-window retry).

---

## Medium

### M4. Name-matching recall gap: candidates fetched exact, compared normalized

`src/leadauto/match.py:405-407, 420-421` vs `src/leadauto/sf/soql.py:171`.

The rule engine keys on `normalize_name` (legal suffixes and punctuation stripped), but
the candidate Account pull uses exact `Name IN (...)` (SOQL is case-insensitive but not
suffix-insensitive). Lead "ACME GmbH" vs account "ACME" (or "ACME GmbH & Co. KG") is
normalized-equal yet the account is never loaded unless another signal (host/VAT/UUID)
happens to catch it → `no_match` → `create_new` → **duplicate account created**. Also
`KN_DBLegalCompanyName__c` is indexed locally (`match.py:129`) but never queried by name.

**Fix idea:** add suffix-stripped name variants to the `names` signal set (and query
`KN_DBLegalCompanyName__c IN (...)` too), or use a `Name LIKE` on the normalized stem.
Calibrate against query size (the bulk route absorbs long IN lists).

### M5. `CountryCode` can be written a full country name

`src/leadauto/writeback.py:109-113` (`_iso2` passes non-2-letter values through) and
`:175-177`.

If the kununu-URL slug is absent and LeadFeeder's `address.country` is a full name
("Germany"), the payload becomes `CountryCode="GERMANY"` → guaranteed row rejection on
the restricted state/country picklist. Today this would surface as a write-back row
failure (see H3 for why that's quiet).

**Fix:** make `_iso2` return `None` for non-2-letter input. **Verify before first live
write-back** what LF actually returns in `address.country` (name vs ISO2) — e.g. via
`scripts/leadfeeder_probe.py`.

### M6. `--limit N` interacts badly with the overlap window

`src/leadauto/sf/soql.py:56` (LIMIT in SOQL, oldest-first) + `src/leadauto/extract/reader.py:85`
(processed-id filter runs *after* the query).

`LIMIT N` returns the N **oldest** tasks in the 35-day window — on a warm watermark those
are almost all already in `processed_task_ids`, so they get filtered and the run
processes ~nothing. The go-live plan's `stage1 --limit 5` would validate nothing while
looking like it ran.

**Fix:** apply the limit in Python after the processed-id filter (keep a generous SOQL
LIMIT as a fetch cap), or pick live-validation leads explicitly by Id.

### M7. No timeouts on the I/O boundary

- LeadFeeder: `session.get/post` without `timeout=` — `src/leadauto/leadfeeder/client.py:188-190`.
- Salesforce: `subprocess.run` without `timeout=` — `src/leadauto/sf/client.py:73-75`,
  `src/leadauto/sf/auth.py:28-30`.

A hung connection or a wedged `sf` process (a known CLI failure mode — the workflows
already retry the *login* step for this) stalls the CI job until the 6-hour default kills
it, burning the concurrency slot for the day's remaining scheduled runs.

**Fix:** `timeout=(10, 120)` on requests; a generous `timeout=` (e.g. 15 min) on
subprocess calls, mapped to `SfError`.

### M8. A `--no-leadfeeder` dry run silently corrupts the shadow backtest

`src/leadauto/pipeline/stage1_enrich.py:78-81` + `src/leadauto/predictions.py:60-64`.

Predictions are upserted by `lead_id`, and a no-LF run predicts `skip`/`no_lf_match` for
every lead. One casual local dry run without LF **replaces** the good pending LF-based
predictions for those leads; shadow-compare later scores them as coverage misses,
depressing the accuracy stats that are the go-live evidence.

**Fix:** don't record predictions when `lf_client is None` (pass
`predictions_path=None`), or never downgrade an existing pending prediction to a no-LF
one.

### M9. Same-company dedup only clusters create-new targets

`src/leadauto/convert/converter.py:249-252`.

If two approved leads share an `lf_company_id` but one links an existing account and the
other is create-new (reviewer edit, or asymmetric signals — one lead carried the kununu
UUID, the other didn't), the create-new one still creates a **brand-new account** instead
of joining the linked one.

**Fix idea:** before clustering, re-home create-new targets whose `lf_company_id` matches
a link target's onto that account (or route them to review).

---

## Minor

- **m10. Semicolon CSVs parse to zero targets.** German-locale Excel exports
  semicolon-delimited CSVs; `read_approved` (`pipeline/stage2_convert.py:44-47`) uses
  default comma parsing → 0 approved rows → green "converted=0" run. Use `csv.Sniffer`
  or hard-error when rows exist but none parse as approved.
- **m11. Pool enrichment may write Python booleans into the bulk CSV.**
  `convert/pool_enrich.py:62` puts the raw lead value in the payload; a REST-path query
  yields real `True`/`False` → CSV `"True"`. `_norm` is compare-only. Verify Bulk API
  accepts it or stringify in the payload. Also the pool-owner check (`:87`) is
  exact-string — a 15-char config id would silently disable pool enrichment (fails safe,
  but silently).
- **m12. `Who.Name` queried but never consumed** (`sf/soql.py:16`), and its shape differs
  between REST (nested dict) and bulk (flattened `"Who.Name"` key) — a latent trap if
  anyone starts reading it. Drop it or document the shape difference.
- **m13. `_kununu_url` regex `\S*` captures trailing punctuation**
  (`extract/parsers.py:37`) — a trailing `.` or `)` pollutes the written
  `KN_KununuLinkProfile__c` and the URL match key.
- **m14. `processed_task_ids` grows unboundedly** (`state.py:82`). Ids older than the
  lookback window can never match again; prune on `advance` to keep `watermark.json`
  bounded.
- **m15. `stage2.yml` interpolates `inputs.approved` directly into bash**
  (`.github/workflows/stage2.yml:92`) — command injection for anyone with dispatch
  rights. Low risk on a single-owner private repo; standard fix is `env:` indirection.
- **m16. `contact_email_global` picks `matches[0]` arbitrarily** (`match.py:343`) when
  several contacts share an email. Display-only (never links), so cosmetic.
- **m17. Doc drift:** CLAUDE.md says 340 tests; the suite is 345.

### Known/open items re-confirmed (already tracked, not re-counted above)

- Watermark advances past write-back failures (open item 5; see H3 for the untracked
  exit-code half).
- `sf/auth.py` writes the auth URL to a predictable temp path (open item 6).
- `stage1.yml` state push has no rebase/retry (open item 6; mitigated by the concurrency
  group).

---

## Verified strengths (checked, not assumed)

- **Terminal-skip invariant holds at all three layers:** `match_lead` short-circuits
  account *and* contact matching; Stage 2 drops approved `decision=skip` rows; shadow
  compare scores a human-converted skip as a coverage miss.
- **Bulk stringly-typing is consistently coerced** (`_bool`, `_is_true`, `_blank('')`) in
  parsers, preflight, and shadow-compare — the `'false'`-is-truthy class of bug
  (live-hit 2026-07-06) is defended everywhere it matters, including the fill-only-blank
  write-back checks against `''`.
- **Apex generation is injection-safe:** CSV-boundary 15/18 shape validation
  (`stage2_convert.py:28`) plus `_apex_id` alphanumeric fullmatch as last line of
  defense; owner/status literals escaped.
- **Conversion results cannot silently vanish:** sentinel at `LoggingLevel.ERROR`,
  try/catch fallback rows for unreported leads, Python-side reconciliation to
  `missing`, and `apex_run --json` raising on compile/runtime errors; CLI fails the job
  on `failed`/`missing` (modulo H1).
- **Preflight faithfully mirrors the org's validation rules,** including the
  deliberately-preserved `CONTAINS` substring semantics of `KN_LeadValidatePrivateMail`.

## Suggested fix order

1. **H1 + H2** — small, self-contained; land on `master` now (the scheduler runs master).
2. **H3, M5, M6** — part of go-live prep; they directly affect the planned live
   validation (`stage1 --limit 5` → gated `stage2`).
3. **M4, M7, M8, M9** — next hardening pass.
4. Minors opportunistically.
