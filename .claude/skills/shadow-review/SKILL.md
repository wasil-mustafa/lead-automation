---
name: shadow-review
description: Summarize the shadow backtest (per-rule decision accuracy, per-field enrichment agreement, pending sample) and judge whether the live scope can widen. Use when asked how accurate the pipeline is, whether to add link to live_scope, or to review shadow_comparison results.
disable-model-invocation: true
---

# Shadow accuracy review

Turns `state/shadow_results.jsonl` into the two numbers that gate the rollout:
**per-rule decision accuracy** (can this rule auto-convert?) and **per-field
enrichment agreement** (is the write-back spec right?).

## Step 1 — summarize what is already scored (free, offline)

```powershell
python .claude/skills/shadow-review/scripts/summarize_shadow.py
```

No Salesforce, no LeadFeeder, no network — every number comes off the committed
state files. Add `--rule <name>` to drill into one rule and list its misses
(`--misses N` to change how many).

Do this **first, always.** Most questions are answered here, and it costs nothing.

## Step 2 — only if the scored set is stale, run the compare

The compare reads human conversions out of the org: **~7-9 API calls** against a
quota shared with business-critical integrations. It also self-gates to roughly
once a day, so it is normally already current from the scheduled runs.

```powershell
$env:PYTHONPATH = "src"; python -m leadauto shadow-compare
```

Only pass `--min-interval-hours 0` to force it when the gate is the actual thing in
the way — never as a reflex. Human conversions resolve over *days*, so a second
compare within the same day re-reads the same predictions and changes nothing.

## How to read it

- **`hit` = the pipeline picked the same account the human did.** `miss` = a
  different one, or the pipeline skipped a lead the human converted (a coverage
  gap). `observed` = the human has not converted yet, so nothing is scored.
- **`no_lf_match` misses are coverage gaps, not wrong answers.** No accepted
  LeadFeeder match is a terminal skip by design; a human converting that lead
  anyway means enrichment did not reach it. Those never auto-convert, so they do
  not block widening — they size the opportunity being left on the table.
- **`create_new` accuracy is reported separately and means something different in
  kind.** There is no account to agree with; a hit means the human also created
  new, and a **miss means the human found an existing account the engine did not —
  i.e. the pipeline would have created a duplicate.** Read every one of those
  individually, no matter how good the rate looks. This population is exactly the
  current live scope, and the 2026-07-08 skipped-task backtest (87% on 283 cases)
  explicitly could **not** validate it: those cases were link-biased by
  construction.
- **`prefilled` in the FIELDS table is not agreement.** It means the Lead already
  carried a value, so the comparison says nothing about enrichment quality. Judge a
  field on `rate` (agree ÷ (agree + differ)) *and* on how many rows are actually
  comparable — `first_name` at 250 prefilled and 0 comparable is unmeasured, not
  perfect.
- **Sample health bounds everything above.** A rule with fewer than ~20 scored rows
  is an anecdote. Say so rather than quoting its percentage as a rate.

## The decision this feeds

Widening the rollout is a config commit to `pipeline.live_scope` in
`config/config.yaml` — either adding a decision (`link`) or raising
`max_kununu_score`. What to weigh, in order:

1. **For adding `link`:** the link-producing rules are `name_address`, `name_vat`,
   `kununu_over_name`, `kununu_agree`, `name_corroborated`. Report each one's own
   accuracy and scored count, not the blended average — they are independent rules
   and a strong one should not carry a weak one. A miss here writes a real Lead onto
   the wrong Account, which is worse than a duplicate and harder to spot.
2. **For raising `max_kununu_score`:** the score is a *demand* signal, not a match
   signal, so shadow accuracy says little about it. The 3.0 line is the human "No
   Demand" boundary; raising it moves the pipeline into leads humans currently work
   themselves. That is a business call for Wasil, not an accuracy inference — say
   so plainly instead of inferring a threshold from these tables.
3. **Never** propose adding `review` or `skip`. `review` means a candidate account
   was surfaced and a human must adjudicate; `skip` means there was no accepted LF
   match at all. The config hook blocks both anyway.

Present the recommendation with its evidence and its sample size, and leave the
commit to a deliberate decision. Do not edit `config.yaml` as part of "reviewing
accuracy".

## What not to do

- Do not re-run the compare to double-check a number, and do not query the org to
  verify a count — both cost API calls, and the state files are the record.
- Do not hand-edit `state/*.jsonl` to "fix" a row. Those files *are* the evidence;
  the PreToolUse guard blocks writes to them.
