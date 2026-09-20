# Handoff — feed company locations into the LeadFeeder match payload

Written 2026-08-23. Project: `Lead Automation` (also applies to `Contact Presence Check`,
which delegates to the same client).

## The idea in one line

kununu's **companies feed** gives a `city` (96.5%) and `postal_code` (60.1%) for **every
assigned company** of a profile. The LF match payload today sends only `company_name`
(+ `email` when valid). City/postal are the two location fields the calibration says do
**not** suppress matches — so they are free precision we are not spending.

## Verified state of the code, 2026-08-23

`src/leadauto/leadfeeder/client.py`

- `match_api_payload(payload)` (line 59) strips **only** `_LOCAL_ONLY_MATCH_KEYS =
  {"alt_names"}` and drops a non-address `email`. **Everything else in the dict is sent
  verbatim** — so adding `city`/`postal_code` keys requires no transport change.
- `match_company()` (line ~405) already has optional `country` / `country_code`
  parameters. That path is deliberately unused by the pipeline.
- `_best_match()` (line 346) is the calibrated gate. **Do not touch it.** Tuned on the
  6-month converted-leads control set (2026-07-02) and a 283-case backtest.

`src/leadauto/leadfeeder/enrich.py` line ~132 — the actual call site. Payload built today:

```python
payload = {"company_name": company_name}
if match_email(email):
    payload["email"] = email
```

with the standing comment: *no country/country_code, LF's country filter **suppresses**
good matches (2026-07-03 control probe: 59 candidates regained without it, all correct)*.
`alt_names` feeds the local gate and never leaves the process.

## Blocker you hit first

`src/leadauto/_vendor/common/kununu_feed.py` is **stale**:

```
canonical  Main-Profile-Account\common\kununu_feed.py   40,530 bytes  sha 8846e1c11f9faf9b
vendored   _vendor/common/kununu_feed.py                32,011 bytes  sha 1fecadd6b733a9e1
'load_company_locations' in vendor: False
```

The companies-feed readers (added upstream 2026-08-21) are not in the snapshot, and
`tests/test_mpa_vendor.py` hashes the snapshot against the live files. So:

- Local runs are fine — `mpa_bridge.common_kununu_feed()` prefers the real checkout via
  `MPA_ROOT` or the `../Main-Profile-Account` sibling.
- **CI would fall back to the snapshot and `load_company_locations` would not exist.**
  Re-sync the vendored files (see `leadauto/_vendor/PROVENANCE.md`) — never edit them in
  place, fix upstream and re-copy.

This is the exact failure mode that comment block warns about: an unwatched copy requested
a dead `.jsonl` endpoint for eleven days after the 2026-08-06 CSV switchover, degrading to
"no profiles" — hence no `profile_score`, hence the live-scope gate failing closed and
nothing converting, all for one WARNING.

## Do this, in order

1. **Answer the open question before writing any code** (below). If LF ignores or
   suppresses on city/postal, stop here — the rest is wasted.
2. **Re-sync the vendored `common/`** so CI has `load_company_locations`. Run
   `pytest tests/test_mpa_vendor.py`.
3. **Load company locations once per run**, scoped to the profile UUIDs in play:
   ```python
   from leadauto.mpa_bridge import common_kununu_feed
   kf = common_kununu_feed()
   locs = kf.load_company_locations(kf._kununu_companies_cache_path(), needed=uuids)
   ```
   Its own cache env var is `KUNUNU_COMPANIES_CACHE` — **not** `KUNUNU_CACHE`, which names
   one path shared by every profiles-feed consumer. Download: 149 MB in ~8 s.
4. **Pick the city for the company being matched, not the profile.** The lead's
   `best_company` name is what goes into `company_name`; look the location up under that
   same name. Key with the *same* normalizer the lookup side uses (upstream passes
   `common.names.norm_name`) — but note this repo normalizes with
   `leadauto.textnorm.normalize_name`, a **different** function. Decide which one owns the
   key and write the decision down; keying with one and looking up with the other silently
   matches nothing.
5. **Add city/postal to the payload behind a settings flag, default off**, so a
   regression is one config line to revert.
6. **A/B it on a real slice.** Match is **free** (0 credits) and the response carries
   `meta.credits.charged` — sum and print it to prove that. Run the same N leads with and
   without the location keys and compare *accepted* matches after `_best_match`, not raw
   candidate counts. A payload change that raises candidates and lowers accepts is a loss.
7. **Only then** consider `Contact Presence Check`, which rides the same gate.

## The open question, and how to settle it cheaply

**Does `POST /companies/match` accept `city` / `postal_code` at all, and do they behave
like `country` (suppressing) or not?** Unknown. The 2026-07-03 probe measured *country*;
nobody has measured city or postal. Assume nothing — the whole idea rests on this.

Cheapest test: take ~50 leads that currently return **no** match, submit each twice
(name-only, and name+city), and diff the accepted set. Match calls are free, so this costs
API time only. An unknown key may be silently ignored, may 400 the **entire chunk** (up to
200 companies degrade to `None`, which downstream reads as "LF knew none of them"), or may
filter like `country`. Test with a chunk of 1 first for exactly that reason.

## Constraints that are not negotiable

- **Never send `country`/`country_code`.** Measured suppression, 59 correct candidates lost.
- **Never re-derive the acceptance gate.** Import `_best_match`; a local copy forks the
  calibration.
- **`match_score` alone is untrustworthy** — 0.9+ observed on entirely wrong companies.
- **A blank/garbage `email` 400s the whole chunk.** `match_api_payload` strips it at the
  boundary; keep any new field equally defensive.
- **LeadFeeder limits are per API key**, so a local A/B contends with production if it
  reuses the key. Note also `LEADFEEDER_API_KEY` was pasted into a chat on 2026-07-26 and
  should be revoked and replaced.
- Feed coverage is **DACH only** (de/at/ch) and 3.5% of company rows have no location.
  Missing location must mean "send name only", never an empty string field.
