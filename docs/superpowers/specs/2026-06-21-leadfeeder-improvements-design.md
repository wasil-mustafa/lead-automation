# LeadFeeder step — throttle, retry & batching

**Date:** 2026-06-21
**Status:** Approved (design)
**Module:** `src/leadauto/leadfeeder/` (Module 2)

## Problem

The LeadFeeder enrichment step (`leadfeeder/client.py` + `leadfeeder/enrich.py`) works, but
the official LeadFeeder/Dealfront API docs reveal it leaves reliability and efficiency on the
table:

- **No rate-limit handling.** The API caps usage at **100 requests/minute per account** and
  returns `429` on overflow. The client only has a fixed `request_delay=0.05s` applied in a few
  spots (not between leads), and no `429`/`Retry-After` handling. On a larger batch the per-lead
  match/search/details calls can fire hundreds of requests with nothing capping the rate; a `429`
  surfaces as an unhandled `HTTPError` that is caught *per record* — so the lead silently gets no
  enrichment (or, in `retrieve_companies`, the whole batch is dropped).
- **Per-call work that the API exposes as batch.** Company match is sent **one company per call**
  in a per-lead loop, and contact details are fetched **one contact at a time** — even though the
  API accepts up to **200 companies/match call** and **100 contacts/retrieve call**.
- **Silently swallowed errors.** List-add failures are `except HTTPError: pass`.

### Grounding facts (from the official API docs)

- Base URL `https://api.leadfeeder.com/v1`, `X-Api-Key` auth. (Our code is on the correct, current
  API.)
- **Rate limit: 100 requests/minute per account** (per token for non-account endpoints); `429` on
  overflow, resets each minute.
- **Credit model is a 12-month subscription, not per-call:** 1 credit = 1 record subscription
  lasting 12 months; re-retrieving the same company/contact within that window is **free**.
  Matching and contact **search are free (0 credits)**; company/contact **retrieve** cost 1 credit
  per *new* record.
- `POST /v1/companies/match` — `companies` array, `minItems: 1`, **`maxItems: 200`**; free.
  Response `data` is a list-of-arrays aligned by input index, each match carrying `match_score`
  (0–1) + a `company_summary`.
- `POST /v1/contacts/search` — free. Response carries `full_name`, `gender`, `hierarchy_level`,
  and `has_mail`/`has_phones`/`has_social_accounts` flags **only** — **no emails/phones/position
  inline**. So a separate retrieve is genuinely required.
- `GET /v1/companies?ids=` and `GET /v1/contacts?ids=` — batch retrieve, comma-separated,
  **max 100 IDs**; `data` is an array of `{id, attributes}`; 1 credit per new record.

Sources: docs.leadfeeder.com/api/public (getting-started, match-companies, search-contacts,
retrieve-contacts, get-contact-details) + help.leadfeeder.com credits article.

## Scope

Four changes, all **behavior-neutral** — same matches, same scores, same credit model; only the
transport changes. **Out of scope:** changes to the contact-search cascade (match recall stays
identical) and new firmographic endpoints (financials/signals).

| # | Change |
|---|--------|
| #1 | Proactive throttle + `429`/`5xx` retry, centralized in the two HTTP methods |
| #6 | Batch company match (≤200/call): phase 1 loop → one call |
| #2 | Batch contact retrieve (≤100/call): phase 3 per-contact loop → one call |
| #4 | Company retrieve batch 20 → 100 |
| #5 | Surface swallowed list-add errors (log, don't `pass`) |

Files touched: `leadfeeder/client.py`, `leadfeeder/enrich.py`, `config.py`, `config/config.yaml`,
`cli.py`, `tests/test_leadfeeder_client.py`, `tests/test_enrich.py`.

## Design

### Component A — transport (throttle + retry)

All requests funnel through `api_get`/`api_post`. Route both through one private
`_request(kind, path, *, params=None, json=None)` that wraps every call:

- **Throttle (proactive).** Keep `_last_request_at` on an injected monotonic `clock`. Before each
  attempt, if less than `60 / max_requests_per_min` has elapsed since the last request, `sleep_fn`
  the remainder. This **replaces** the `request_delay` knob and the scattered `_sleep()` calls.
  Min-interval pacing guarantees we never burst above the cap.
- **Retry (reactive).** On `429` or any `5xx`: wait `Retry-After` (seconds) if the header is
  present, else exponential backoff `backoff_base * 2**attempt` (1, 2, 4, 8s). Retry up to
  `max_retries`. Retries also pass through the throttle (no retry storm). After exhaustion,
  `resp.raise_for_status()` raises `HTTPError` — exactly as today — so existing per-record/per-batch
  `except` blocks still isolate failures.
- **Retryable statuses:** `{429, 500, 502, 503, 504}`. Other `4xx` are non-transient → raise
  immediately.
- **Testability.** Inject `sleep_fn=time.sleep` and `clock=time.monotonic`. Tests pass a recording
  no-op `sleep_fn` (instant) and a controllable clock to assert spacing/retries.

`api_get`/`api_post` become one-line wrappers over `_request`, so `match`, `search`, both
retrieves, and list-adds inherit throttle+retry with no per-method changes.

### Component B — batch company match (phase 1)

New `match_companies(payloads: list[dict]) -> list[dict | None]`:

- Chunk into `≤ match_batch` (200).
- `POST /companies/match` with `{"companies": chunk}`, params `account_id` +
  `max_results_per_company=3`.
- Map the response's per-input-index arrays back to inputs. For each company: best-pick by
  `match_score`, apply the `min_match_score` floor — **identical logic to today's `match_company`**.
- Return a list aligned to input order; `None` = no/low match.
- A chunk that errors after retries is caught internally → that chunk's entries become `None` + a
  logged warning (symmetric with `retrieve_companies`). A failed chunk degrades to "no match"
  instead of crashing the run.

`match_company(...)` (singular) stays as a thin wrapper → `match_companies([payload])[0]` (keeps its
existing client tests green).

`enrich.py` phase 1 becomes:
1. Build a match payload for each lead with a `best_company` (remember index → lead).
2. One `match_companies(payloads)` call.
3. Map results back; for each matched lead set `matched`/`lf_company_id`/`matched_name`/
   `match_score` and call `add_company_to_list` (that endpoint is per-id, not batchable — stays a
   loop, now throttled).

### Component C — batch contact retrieve (phase 3)

New `retrieve_contacts(ids: list[str]) -> dict[str, dict]` mirroring `retrieve_companies`: chunk
`≤ retrieve_batch` (100), `GET /contacts?ids=`, map `id → attributes`, per-chunk `HTTPError`
isolation (log warning, continue).

`get_contact_details(id)` stays as a wrapper →
`flatten_contact_details(retrieve_contacts([id]).get(id, {}))`.

`enrich.py` phase 3 splits into:
- **3a** Run the search cascade (free, **unchanged**) for all matched-with-name leads; collect
  `contact_id`s.
- **3b** One `retrieve_contacts` batch over the collected ids.
- **3c** `flatten_contact_details` + assign + `add_contact_to_list` (per-id, throttled).

**Preserved nuance:** if a found `contact_id` is missing from the batch result, the lead still gets
`contact_found=True` with `{contact_id, full_name}` and empty details — same as today's per-contact
failure path.

### #4 & #5

- **#4:** `retrieve_batch` default 20 → 100 (shared by both retrieves; both endpoints cap at 100).
- **#5:** the two `except HTTPError: pass` blocks around list-adds become `log.warning(...)` with
  the id + error. No QA sheet — a list-add failure is a UI-convenience miss, not a data failure.

### Configuration

New `leadfeeder:` keys in `config/config.yaml`, each with a `Settings` property and passed through
`cli._build_lf_client`:

```yaml
leadfeeder:
  max_requests_per_min: 95   # stay under LeadFeeder's 100/min/account hard limit (headroom for
                             # clock jitter + the sliding-window boundary)
  max_retries: 4             # retries on 429/5xx before surfacing HTTPError
  retrieve_batch: 100        # GET /companies & /contacts (API max 100)
  match_batch: 200           # POST /companies/match (API max 200)
```

`backoff_base` (1.0s) stays an internal constructor default. The `request_delay` constructor param
is **removed** (replaced by the throttle); `min_match_score` is unchanged.

## Error-handling trade-off

Batching trades per-lead isolation for per-chunk isolation on the match/retrieve calls. Mitigated:
retries absorb transient failures first; chunk errors are caught and degrade to "no match"/"no
details" rather than crashing; and chunk sizes are large enough that real batches are usually a
single chunk. Net reliability is **up** vs. today's no-retry behavior.

## Testing

- **`tests/test_leadfeeder_client.py`:** add `headers={}` to `FakeResponse`; `_client` injects a
  recording no-op `sleep_fn` + a fake clock. New tests:
  - `429` → `Retry-After` → `200` (retried; slept the `Retry-After` value).
  - `429` exhausts `max_retries` → raises `HTTPError`.
  - `5xx` retry.
  - throttle spacing (back-to-back calls sleep the min-interval, via the fake clock).
  - `match_companies`: chunking > 200, index alignment, `min_match_score` floor, `None` on
    miss/chunk-failure.
  - `retrieve_contacts`: chunking > 100, `id → attrs` map, chunk-failure isolation.
  - singular `match_company`/`get_contact_details` wrappers still pass.
- **`tests/test_enrich.py`:** update `FakeClient` to expose `match_companies`/`retrieve_contacts`
  and update the call-record assertions; keep all existing scenarios (matched+contact,
  matched-no-name, no-match, lead-field fallbacks, single-batch retrieve) and add a multi-lead
  batch-match assertion.

Full suite (`python -m pytest -q`) must stay green.

## Validation

1. Build + unit-test against the `FakeSession`/`FakeClient` harness (above). No live calls.
2. **Live read-only probe** (final step), with `LEADFEEDER_API_KEY` set locally: match one known
   company → search its contacts → `retrieve_contacts` to confirm the live response shape and that
   throttle/retry behave. Read-only, ~free.

## Non-goals / future options

- Contact-search cascade trimming (trades match recall — needs live calibration).
- `has_email`/`has_phones` search flags to skip retrieving empty contacts (would change which
  contacts are revealed — out of the behavior-neutral scope).
- Richer firmographics via `GET /companies/{id}/financials` (revenue) and `POST /companies/signals`
  (intent) — a separate feature.
- Bulk list-add, if such an endpoint exists (list-adds currently stay per-id, throttled).
