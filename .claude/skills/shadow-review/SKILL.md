---
name: shadow-review
description: Measure how accurate the pipeline's predictions are against what humans actually did, from the shadow artifacts (state/shadow_results.jsonl) - per-rule and per-decision accuracy (e.g. the create_new flag, link rules), per-field enrichment agreement, optionally restricted to a date window - and judge whether to auto-convert more or widen live_scope. Use when asked "check the shadow artifacts since <date>", "how accurate is the create_new flag / rule X", "is it safe to auto-convert these leads", "should we add link to live_scope", to review shadow_comparison.xlsx / the shadow-compare results, or for an evaluation of the live system after N days.
---

# Shadow accuracy review

Turns `state/shadow_results.jsonl` into the numbers that gate the rollout:
**per-rule / per-decision accuracy** (can this rule auto-convert?) and
**per-field enrichment agreement** (is the write-back spec right?).

## Where the evidence lives

- `state/predictions.jsonl` - every prediction (`status`: pending | resolved |
  expired | excluded; `predicted_at`, `rule`, `decision`, `written_back`,
  `outcome`). `src/leadauto/predictions.py`, dataclass `Prediction`.
- `state/shadow_results.jsonl` - one row per *scored* prediction, appended by
  `python -m leadauto shadow-compare`: `lead_id, resolved_at, rule, decision,
  predicted_account_id, converted_account_id, converted_account_name,
  account_created, converted_date, result (hit|miss|observed), detail, fields`.
  `src/leadauto/shadow_compare.py:239-248`.
- `state/shadow_state.json` - `last_run` of the compare (the once-a-day gate).
- CI commits all three on every `pipeline.yml` run
  (`chore: update lead-automation state [skip ci]`,
  `.github/workflows/pipeline.yml`, the state-commit step). The local checkout is only
  as fresh as its last pull: `git fetch` and read `origin/master:state/...` (or pull)
  before quoting numbers. Each CI run also uploads a `lead-automation-shadow-<run_id>`
  workbook artifact (`shadow_comparison.xlsx`), but the state files are the record.

## Step 1 - summarize what is already scored (free, offline)

```powershell
python .claude/skills/shadow-review/scripts/summarize_shadow.py
```

No Salesforce, no LeadFeeder, no network - every number comes off the state files.
Flags (verified, `scripts/summarize_shadow.py:63-66`): `--rule <name>` drills into
one rule and lists its misses; `--misses N` changes how many (default 10). There
is **no date filter** - for a window, use Step 2.

Do this **first, always.** Most questions are answered here, and it costs nothing.

## Step 2 - a date window ("since the 19th", "last 7 days")

`summarize_shadow.py` is all-time. For a window, filter the results rows yourself.
Use `converted_date` (the day the human converted; Salesforce `Date`, so a
`YYYY-MM-DD` string - sometimes a full datetime, hence `[:10]`) and fall back to
`resolved_at` (when the compare scored it). Do not use `predicted_at` here: it is
not in the results file.

```python
# run from the repo root; set SINCE, and KEY to "decision" or "rule"
import json, collections
SINCE, KEY = "2026-10-19", "decision"
rows = [json.loads(l) for l in open("state/shadow_results.jsonl", encoding="utf-8") if l.strip()]
win = [r for r in rows if (r.get("converted_date") or r["resolved_at"])[:10] >= SINCE]
c = collections.defaultdict(collections.Counter)
for r in win:
    c[r[KEY]][r["result"]] += 1
for k, v in sorted(c.items()):
    n = v["hit"] + v["miss"]
    acc = f"acc={v['hit']/n:.1%}" if n else "acc=n/a"
    print(f"{k:<20} hit={v['hit']} miss={v['miss']} observed={v['observed']} n={n} {acc}")
```

(`KEY="decision"` gives the create_new / link / review / skip view; `"rule"` gives
the per-rule view. Write it to a scratch file and run it; do not edit the state
files.) **`skip` is always a miss by construction** (a coverage gap: the human
converted a lead the pipeline had no LF match for) - never quote it as an accuracy.

Report for every window:

1. **Accuracy per rule (or decision) with n** - `hit / (hit + miss)`.
2. **State the denominator.** It is *comparable rows only*: scored `hit` + `miss`.
   `observed` (review cases, nothing to agree with), `pending`, `expired` and
   `excluded` rows are not misses and are not in the number.
3. **How many sit outside it** - pending count and oldest age come from Step 1's
   SAMPLE HEALTH; the `excluded` count from `predictions.jsonl`
   (`status == "excluded"`, `outcome.reason == "pipeline_converted"`; excluded rows
   with no reason are own-company / internal-email leads, `shadow_compare.py`).
4. A rule under ~20 scored rows in the window is an anecdote - say so.

## Step 3 - if the scored set looks stale: read CI's, do not run the compare locally

The compare runs in CI before every `pipeline.yml` run and self-gates to roughly once
a day (`DEFAULT_MIN_INTERVAL_HOURS = 20`, `shadow_compare.py`), so it is normally
current. **Do not run `python -m leadauto shadow-compare` locally:** it costs
**~7-9 API calls** against a shared quota, and it rewrites the tracked, CI-owned
`state/predictions.jsonl`, `state/shadow_results.jsonl` and `state/shadow_state.json`,
which then conflict with the next pull. Instead `git fetch` and read
`origin/master:state/shadow_results.jsonl`, or check the latest run's
`lead-automation-shadow-<run_id>` Summary (`compare_ran`, `last_compare_at`). Forcing a
compare (`--min-interval-hours 0`) is the owner's call. Human conversions resolve over
*days*, so a second compare within the same day re-reads the same predictions and
changes nothing.

## How to read it

- **`hit` = the pipeline picked the same account the human did.** `miss` = a
  different one, or the pipeline skipped a lead the human converted (a coverage
  gap). `observed` = a review case, nothing is scored
  (`shadow_compare.py:114-133`). A prediction whose lead is not yet converted stays
  pending and is not in the results file at all.
- **The pipeline's own conversions are not in the accuracy number.** Since the
  single `python -m leadauto run` (2026-11-01; `cli.py:7`), every lead the pipeline
  converts has its pending prediction set to `excluded` with reason
  `pipeline_converted` (`pipeline/run.py:53`, `predictions.py:113-135`), so
  shadow-compare never scores our own conversion as a human hit. Consequences for
  `create_new` since auto-conversion started (2026-10-31): the number measures only
  the leads a **human** still converted - those outside the "EnrichLeadfedup"
  bucket, or left to humans - not the auto-converted population. It cannot
  validate the auto-converted leads themselves, and the human-worked subset is not
  a random sample of `create_new` (selection bias). Window numbers that straddle
  that date mix two populations; split them at it when it matters.
- **`no_lf_match` misses are coverage gaps, not wrong answers.** No accepted
  LeadFeeder match is a terminal skip by design; a human converting that lead
  anyway means enrichment did not reach it. Those never auto-convert, so they do
  not block widening - they size the opportunity being left on the table.
- **`create_new` accuracy is reported separately and means something different in
  kind.** There is no account to agree with; a hit means the human also created a
  new Account that day (`is_new_account`, `shadow_compare.py:98-111`, compares
  `Account.CreatedDate` to the Date-only `ConvertedDate`), and a **miss means the
  human found an existing account the engine did not - i.e. the pipeline would have
  created a duplicate.** Read every miss individually (`--rule no_match --misses
  N`), no matter how good the rate looks. The 2026-07-08 skipped-task backtest
  (87% on 283 cases) could **not** validate this population: those cases were
  link-biased by construction.
- **`prefilled` in the FIELDS table is not agreement.** It means the Lead already
  carried a value, so the comparison says nothing about enrichment quality. Judge a
  field on `rate` (agree / (agree + differ)) *and* on how many rows are actually
  comparable - `first_name` at 250 prefilled and 0 comparable is unmeasured, not
  perfect. Written-back leads carry empty `fields` (`shadow_compare.py:238`) - their
  payload would match itself.
- **Sample health bounds everything above.** A rule with fewer than ~20 scored rows
  is an anecdote. Say so rather than quoting its percentage as a rate.

## The decision this feeds

Two questions come up: "is it safe to auto-convert more?" and "can we widen the
rollout?". Both end as a config commit to `pipeline.live_scope` in
`config/config.yaml` (`decisions`, `convert_campaigns`, `max_kununu_score`; see the
`live_scope:` block at `config.yaml:167`). What to weigh, in order:

1. **For auto-converting `create_new`** (already on since 2026-10-31 for the
   "EnrichLeadfedup" bucket): accuracy of the rule that produces it (`no_match`),
   its n, and the individually read misses. A miss is a duplicate Account; a wrong
   `link` is worse (item 2). For extending it to
   `matched_incomplete` ("EnrichLeadshortage"), the shadow evidence says nothing
   about incompleteness - that is a business call, say so.
2. **For adding `link`:** the link-producing rules are `name_address`, `name_vat`,
   `kununu_over_name`, `kununu_agree`, `name_corroborated`. Report each one's own
   accuracy and scored count, not the blended average - they are independent rules
   and a strong one should not carry a weak one. A miss here writes a real Lead onto
   the wrong Account, which is worse than a duplicate and harder to spot.
3. **`max_kununu_score` is `null`** (no score axis since 2026-09-28). The score is a
   *demand* signal, not a match signal, so re-introducing a cap is a business call
   for Wasil, not an accuracy inference - say so plainly instead of inferring a
   threshold from these tables.
4. **Never** propose adding `review` or `skip`. `review` means a candidate account
   was surfaced and a human must adjudicate; `skip` means there was no accepted LF
   match at all. The config hook blocks both in the conversion scope anyway
(`.claude/hooks/check_config.py`, `FORBIDDEN_IN_LIVE_SCOPE`).

Present the recommendation with its evidence, its denominator and its sample size,
and leave the commit to a deliberate decision. Do not edit `config.yaml` as part of
"reviewing accuracy".

## What not to do

- Do not re-run the compare to double-check a number, and do not query the org to
  verify a count - both cost API calls, and the state files are the record.
- Do not hand-edit `state/*.jsonl` to "fix" a row. Those files *are* the evidence;
  the PreToolUse guard blocks writes to them.
