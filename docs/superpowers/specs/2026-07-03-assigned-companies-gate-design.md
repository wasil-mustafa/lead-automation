# Assigned-company names in the LeadFeeder name-agreement gate

**Date:** 2026-07-03
**Status:** Approved (Approach A of three; multi-payload matching deferred as follow-up)

## Problem

The name-agreement gate (shipped 2026-07-03, PR #4) compares LeadFeeder's returned company
name against exactly two identity signals: the lead's kununu **profile name** and the
corporate **email-domain root**. But a kununu profile is a *main profile* that can have
several legal entities ("assigned companies") under it — the kununu DWH feed records this
per profile (`number_of_assigned_companies` plus the assigned companies' names).

When a lead's profile is "Siemens" and LeadFeeder correctly returns "Siemens Mobility GmbH",
today's gate sees name-agreement < 85 and rejects a legitimate match. Assigned-company names
are additional truthful identities for the same lead and should count as agreement evidence.

## Goal / success criteria

- The gate accepts LF candidates whose name agrees (>= existing thresholds) with **any** of:
  profile name, an assigned-company name, or the email-domain root. Thresholds stay
  `lf_name_agree_min=85` / `lf_name_agree_rescue=90` — wider evidence, not a looser gate.
- Measured on the 1,101-lead control set (`Export/control_leads_6m.xlsx`), acceptance should
  improve over the 89.4% baseline without raising the wrong-company rate (1.1% of accepted).
- Every failure mode degrades to current behavior; the pipeline never blocks on the feed.

## Non-goals

- No multi-payload LF matching (sending assigned-company names as their own match entries) —
  that is the deferred Approach B, which this design's plumbing enables later.
- No changes to Salesforce-side matching (`match.py`) or write-back.
- No use of feed data beyond names for the gate (city/score/etc. stay unused for now).

## Data source

kununu DWH bulk feed `profiles.jsonl` (~530 MB JSONL), default URL
`https://api.data.kununu.com/v1/feeds/data-management/profiles.jsonl`.
Auth via env: `KUNUNU_API_TOKEN` (Bearer) or `KUNUNU_API_USER`/`KUNUNU_API_PASSWORD` (Basic).
**Fresh credentials are required** — the `.env` committed to the public Profile-Matching repo
must be treated as leaked: rotate, purge from history, and never reuse those values here.
Bulk feed is the only access path (confirmed); freshness requirement: **daily** (confirmed).

Join keys, in order: `profile_uuid` (leads carry `kununu_profile_uuid` parsed from the Task
description's `- Profil-ID:` line), falling back to the URL base slug from `kununu_url`
(strip a trailing digit-suffix attached directly to a letter: `siemens411` -> `siemens`,
but `promedis-24` keeps its number — same rule as Profile-Matching's `base_slug`).

**Implementation checkpoint:** the exact feed field holding assigned-company names is
verified against a real record on the first live feed pull (the existing analysis script
only reads the count). The parser lives in one function so the field name is a one-line fix.

## Components

### 1. `src/leadauto/kununu_feed.py` (new)

- `ensure_feed(cache_path, max_age_hours=24) -> Path | None` — download when the cache file
  is missing or older than `max_age_hours`. Streamed download to `<cache>.part`, then atomic
  `os.replace`. On download failure with a stale cache present: log a warning, return the
  stale cache. On failure with no cache, or missing credentials: log a warning, return None.
- `lookup_profiles(feed_path, uuids, slugs) -> dict[str, ProfileInfo]` — ONE streaming pass
  over the JSONL, keeping only records whose `profile_uuid` is in `uuids` or whose URL base
  slug is in `slugs`. `ProfileInfo`: `{name, companies: list[str], city, country_code}`.
  Result dict is keyed by both uuid and slug for easy per-lead lookup. Malformed lines are
  skipped, counted, and logged.
- No module-level state; callers pass paths/settings explicitly.

### 2. Config / CLI

`config/config.yaml`:

```yaml
kununu_feed:
  enabled: true            # false => pipeline behaves exactly as today
  cache_path: kununu_profiles.jsonl   # gitignored; CI overrides via env/workflow
  max_age_hours: 24        # daily freshness (confirmed requirement)
```

`Settings` gains `kununu_feed_enabled`, `kununu_feed_cache_path`, `kununu_feed_max_age_hours`,
and `kununu_api_token`/`kununu_api_user`/`kununu_api_password` (env-only, never in yaml).
`cli.py` gains `--no-kununu-feed` (mirrors `--no-leadfeeder`) for runs without credentials.

### 3. Gate change (`leadfeeder/client.py`)

- Match payloads may carry `alt_names: list[str]` — internal metadata, **stripped before the
  LF POST** (`{k: v for k, v in p.items() if k != "alt_names"}` when building the request).
- `_name_agreement(matched_name, payload)` additionally scores `token_set_ratio` against each
  normalized name in `payload.get("alt_names", [])`; overall agreement stays the max. It now
  returns `(score, via)` where `via` identifies the winning signal: `"company_name"`,
  `"alt_name:<name>"`, or `"email_domain"`.
- `_best_match` result dict gains `"agreed_via"`. Acceptance logic and thresholds unchanged.

### 4. Wiring (`leadfeeder/enrich.py`)

Phase 1, before building match payloads:

1. Collect `kununu_profile_uuid` / `kununu_url`-slug for all extracted leads.
2. If the feed is enabled and available (`ensure_feed`), call `lookup_profiles` once for the
   whole batch.
3. For each lead with a found profile, set `payload["alt_names"]` to the assigned-company
   names (deduped, excluding names equal to the payload's `company_name` after normalization).
4. `EnrichedLead` gains `agreed_via: str | None`, populated from the match result.

Leads without UUID/slug, or profiles not in the feed: payload unchanged (today's behavior).

### 5. QA artifact (`qa_artifact.py`)

New Review-sheet column `lf_agreed_via` immediately after `lf_name_agreement`, so reviewers
see when an assigned company (rather than the profile name) carried the match.

### 6. CI (`.github/workflows/stage1.yml`)

- Cache the feed via `actions/cache` with key `kununu-feed-<yyyy-mm-dd>` and restore-key
  prefix `kununu-feed-` (yesterday's cache restores; `ensure_feed` then refreshes because
  it is >24 h old — stale-tolerant if the download fails).
- New GitHub secret for the kununu API credential (token preferred), exported to the run env.
- Stage 2 is unaffected (no feed use).

## Error handling summary

| Failure | Behavior |
|---|---|
| No credentials / `--no-kununu-feed` / `enabled: false` | warn once, no alt names, gate as today |
| Feed download fails, stale cache exists | warn, use stale cache |
| Feed download fails, no cache | warn, no alt names this run |
| Lead has no UUID and no usable slug | no alt names for that lead |
| Profile not in feed / malformed lines | per-lead fallback / skip + count in log |

## Testing & validation

- TDD throughout. Unit tests use small synthetic JSONL fixtures (`tmp_path`):
  - `lookup_profiles`: hit by uuid, hit by slug fallback, miss, malformed line skipped,
    single-pass over multiple keys.
  - `ensure_feed` staleness: fresh cache untouched; stale triggers download (mocked);
    download failure falls back to stale / None.
  - Gate: low-score LF candidate + assigned-company name agreement >= 90 -> rescued with
    `agreed_via` = `alt_name:<name>`; alt names never appear in the POST body; freemail
    exclusion and existing accept/reject cases unchanged.
  - Enrich wiring: alt_names populated from lookup, `agreed_via` lands on `EnrichedLead`.
  - QA: `lf_agreed_via` column position and value.
- **Control-set re-run:** repeat the 1,101-lead check with alt names active and compare
  acceptance / wrong-company / rescue counts against the 89.4% / 1.1% baseline. This gates
  enabling the feature in CI.

## Rollout

1. Implement + unit tests (config default `enabled: true`, harmless without credentials).
2. Rotate kununu API credentials (leaked `.env`); set local env + GitHub secret.
3. Control-set validation; record numbers in CLAUDE.md.
4. Enable in CI (cache step + secret) once validation looks good.
