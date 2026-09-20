# Second-chance company match — ported from Contact Presence Check

Written 2026-08-25. Shipped the same day. Supersedes step 1 of
`2026-08-23-companies-feed-match-payload.md`: the open question there ("does an extra
identifier in the match payload help or suppress?") was answered by measurement in
`Contact Presence Check`, and the answer rules OUT the change that doc proposed while
ruling IN a cheaper one.

## What was ported, and what was deliberately not

`Contact Presence Check`'s rescue pass (2026-08-23, `presence.rescue_accounts`) has two
independent halves:

1. **extra identifiers** — re-ask with `url` / `vat_id` / `city+postal_code` / the kununu
   profile name replacing `company_name`;
2. **a wider candidate list** — re-ask the SAME payload with
   `max_results_per_company: 20` instead of 3, because LF holds several company records
   for one company and ranks the wrong (emptier) one first.

**Only (2) was ported.** It needs no new data, no feed, no vendored-snapshot re-sync, and
it cannot regress a working match. (1) is measured-harmful on the primary payload and has
almost no fuel here — see the tables below.

## Why rescue-only, never the primary payload

Measured over there on 80 accounts already matched to a POPULATED company:

| added to the payload | kept | moved to a smaller company | lost the match |
|---|---|---|---|
| `url` | 67 | 8 | 4 |
| `vat_id` | 69 | 5 | 2 |
| `city` + `postal_code` | 68 | 4 | 4 |
| profile name replacing `company_name` | 59 | 21 (moved company) | — |

LF reads an extra identifier as a request for a more specific legal entity, and the more
specific entity is usually the emptier one. ~15% of working matches get worse. That is why
the primary pass keeps asking for exactly what it asked for during calibration, and why
`MATCH_RESULTS_PRIMARY = 3` is a named constant with a comment rather than a literal.

## The prize here

`state/predictions.jsonl`, 523 distinct leads:

| rule | distinct leads |
|---|---|
| `no_match` (-> create_new) | 317 |
| **`no_lf_match` (-> TERMINAL skip)** | **101 (19.3%)** |
| `weak_signal` | 45 |
| `name_address` | 34 |
| everything else | 26 |

Every `no_lf_match` lead is one the pipeline refuses to touch. On the 2026-08-18 dry run,
22 of 29 leads matched and 10 of those 22 found no LF contact — so the duplicate-company
failure mode plausibly costs contact enrichment too, not just matches.

## Fuel for the identifier arms — measured on 971 queue-owned unconverted leads (1 SOQL call)

| field | populated | verdict for an arm |
|---|---|---|
| `Company` | 100% | the primary payload already |
| `Website` | **3.3%** | `url` arm is dead. The best-performing arm over in CPC (48 of 73 recoveries) has no fuel here, because a Braze-created lead has no website — LF is what fills it |
| `City` | 29.6% | `geo` arm has some fuel (11 of 73 over there) |
| `PostalCode` | 28.5% | as above; `City OR PostalCode` = 287 leads |
| `KN_KununuProfileName__c` | **0.0%** | `prof` arm has no fuel from the Lead. kununu ids are empty on new Leads by design; the name would have to come from the Task or the feed |
| `ExtractedLead.vat_number` | declared, **filled by no parser** | `vat` arm has zero fuel, full stop |

Note the unmatched tail is mostly micro-businesses — `Praxis Dr. Resmini`,
`Dr. Ali A. Koravi`, one-person consultancies. Those are genuinely absent from LeadFeeder;
no identifier invents them. Expect materially less than CPC's 66% recovery.

## What shipped

- `client.match_companies(payloads, max_results=MATCH_RESULTS_PRIMARY)` — the width is a
  parameter, validated 1..20 (`MATCH_RESULTS_MAX`, from the machine-readable spec; the
  prose doc's 1..10 is wrong). Out-of-range raises rather than becoming an opaque 400 that
  degrades a whole chunk to `None`.
- `enrich.enrich_leads` — after the primary pass, the leads that matched **nothing** are
  re-asked once at `leadfeeder.match_wide_results` (20; `0` disables). A matched lead is
  never re-asked, which is the entire safety argument. `EnrichedLead.match_pass` records
  `primary` / `wide` / `""`.
- `live_scope.live_scope_reason` — a `wide` match is excluded with reason
  `match_pass_wide` unless `pipeline.live_scope.allow_wide_match: true`. It is predicted,
  written to the QA artifact and shadow-scored; it is not written back or converted.
- `lf_match_pass` on the QA Review sheet and on `Prediction`, so shadow-compare can score
  the two passes separately. Old prediction lines read back fine (the field defaults).

Cost per stage-1 run: **one extra free `/companies/match` call** per `match_batch` (200)
unmatched leads, 0 credits. A lead recovered here then pays the normal 1 credit at the
phase-2 company retrieve, and still faces `_match_sanity_reason`.

## MEASURED, same day: the widening recovers NOTHING here — 0 of 103

`python -m leadauto wide-match-backtest` replayed every `no_lf_match` lead in
`state/predictions.jsonl` at width 3 (control) and width 20 (treatment).
**103 replayed, 103 Task+Lead records still present, 0 credits charged** (the client's
`credits_charged` accumulator proves it, rather than the docs asserting it):

| outcome | n | meaning |
|---|---|---|
| `still_none` | 78 | neither width matched. The micro-business tail, as predicted |
| `drifted` | **25** | the CONTROL matches today — LF's index moved, the width is irrelevant |
| **`recovered`** | **0** | **the feature bought nothing** |

So `leadfeeder.match_wide_results` should go to **0**. Keep the code and the backtest —
they cost nothing sitting still, the mechanism is real in `Contact Presence Check`'s data
(duplicate company records), and re-enabling is one config line if this population ever
looks like that one. But it is not earning a call in the live path today, and
`live_scope.allow_wide_match` has nothing to admit.

### What the backtest found instead, and it is worth more

**25 terminally-skipped leads would match TODAY at the normal width** — coverage the
pipeline already lost and never revisits, because a `no_lf_match` skip is terminal and the
14-day lookback has long since passed those tasks.

**But every one of the 25 scores `name_agreement = 100`, and 11 of 25 matched a strictly
SHORTER name.** That is the truncated-name trap: the gate scores a truncated name as
perfect agreement, so `match_score` as low as **0.05** still passes via the
`name_agree_rescue` band (≥90 regardless of score).

| sent | LF matched | score | verdict |
|---|---|---|---|
| `SJD – Die Falken \| LV NRW` | `FALKEN` | 0.24 | **wrong company** |
| `4 Directions Management Consultants GmbH` | `4° Management` | 0.18 | **wrong company** |
| `ORBITER RESEARCH GmbH` | `Orbiter GmbH` | 0.05 | wrong — the country gate caught this one live |
| `BARDINI + KELLER AG` | `Bardini` | 0.12 | dropped a partner name |
| `Topgolf Wien` | `Topgolf` | 0.12 | parent |
| `Filter Queen GbmH Hallwil` | `Filter Queen` | 0.19 | parent |
| `Volksbank Hohenlimburg` | `Volksbank Hohenlimburg` | 0.94 | clean |

**This repo has no column that sees it.** `Contact Presence Check` built `lf_name_dropped`
for exactly this and measured **1,052 of 13,253 matched accounts (7.9%), all at agreement
100**. Porting that flag is now the highest-value LF-matching work here — it is offline,
free, and it is the only thing that distinguishes `Volksbank Hohenlimburg` from `FALKEN`.
Note this does not indict the gate: it was calibrated for *lead* matching, where a
parent-company hit is still useful. It indicts trusting agreement 100 without a flag.

## Open

1. ~~**Nothing is measured HERE yet.**~~ Measured — see above. Set
   `match_wide_results: 0`.
1a. **Port `lf_name_dropped` from `Contact Presence Check`** — the only signal that sees
   the truncated-name match. 11 of 25 drifted rows would carry it.
1b. **Decide what to do with the 25 drifted leads.** They are recoverable coverage, but
   they need a human read, not an automatic conversion — see the table.
2. **(superseded)** The next dry run's QA artifact is the evidence: count
   `lf_match_pass == "wide"` rows and eyeball `lf_matched_name` against `company`. Until
   that is done `allow_wide_match` stays false.
2. If the pass earns its keep, the cheapest identifier arm to add is `geo` from the
   **companies feed** (city 96.5%, postal 60.1% — far better than the Lead's 29%), which is
   step 3 of `2026-08-23-companies-feed-match-payload.md` and still blocked on the stale
   `_vendor/common/kununu_feed.py` snapshot (`tests/test_mpa_vendor.py` fails on it today,
   independently of this change).
3. `ExtractedLead.vat_number` is a dead field — no parser fills it. Either fill it from the
   task or drop it; today it silently reads as "this lead has no VAT".
