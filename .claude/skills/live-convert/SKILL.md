---
name: live-convert
description: Go-live / widen-scope checklist for the fused Lead Automation pipeline - change live_scope or convert_campaigns, dry-run, read the QA Conversion sheet, commit the config, let the next run go live, verify in the org; also lists the three LF-outcome campaigns. Use when asked to "go live", "widen live_scope", "turn on auto-convert", "convert leads for real", "add the campaign", or about a "live run".
disable-model-invocation: true
---

# Going live / widening scope (fused pipeline)

**Conversion is irreversible.** SOAP `convertLead()` creates Accounts/Contacts,
flips the Lead, re-parents its Task and fires the org's triggers, flows and
validation rules. No undo; cleanup is manual work in production. A
required-reviewers GitHub Environment is not available on a private repo without a
paid plan (verified 2026-07-10), so the brake is this checklist.

Since 2026-11-01 there is no Stage 2, no `review/approved_*.csv`, no `Approve?`
column. **Every live `python -m leadauto run` converts in-process**: extract ->
LF enrich -> match -> write-back -> campaign tag -> convert. Scheduled runs
(cron-job.org job 8029899, every 2 h 08:48-16:48 Vienna, Mon-Fri) POST
`dry_run=false` to `pipeline.yml`, so **a config commit to `master` goes live on the
next firing, without anyone dispatching anything.** The commit is the go-live.

Do not commit, merge or push a `live_scope`/`convert_campaigns` change, and do not
dispatch `dry_run=false`, without an explicit, current instruction naming it as a
go-live.

## What gates a conversion (config/config.yaml, `pipeline.live_scope`)

A lead converts only if ALL hold (a lead can also arrive via the sweep, below):
- `decisions` contains its match decision (currently `[create_new]`; never list `skip`/`review`)
- `task_types` contains its task type (`claimer`, `ebp_trial`) - write-back scope
- THIS run tagged it into a bucket listed in `convert_campaigns` (currently `[matched_complete]`)
- preflight and the pool-link gate pass; SOAP `pipeline.convert_batch_size` is `3`

Other keys: `writeback_decisions` (write-back scope, enrich only), `max_kununu_score`
(null = no score axis), `allow_wide_match`. An absent `convert_campaigns` key = NO
campaign axis (decisions alone decide) - never delete the key to "disable" it; set
`[]`/remove buckets deliberately. An unknown bucket name raises at config load
(`Settings.live_convert_buckets`, config.py).

The **sweep** also converts: every run reads unconverted, queue-owned, `Status = New`
members of the `convert_campaigns` campaigns from EARLIER runs. Their decision is the
newest row in `state/predictions.jsonl`; no row or a decision outside `decisions` =>
`left_to_human`. So **widening `convert_campaigns` or `decisions` also converts the
existing backlog in those campaigns on the next run.** Count that backlog first.

## The three campaigns (config `pipeline.lf_campaigns`, `campaigns.py` BUCKETS)

Tagged on a non-dry run for task types in `lf_campaign_task_types` (`claimer`,
`ebp_trial` - same campaigns for both):

| bucket key (use in `convert_campaigns`) | Campaign | Lead outcome |
|---|---|---|
| `matched_complete` | EnrichLeadfedup | LF-matched, written back, `KN_Employee__c` AND `Street` filled. **The only auto-convert bucket today.** |
| `matched_incomplete` | EnrichLeadshortage | written back, one of them still blank; enriched, left to humans |
| `unmatched` | EnrichLeadmanually | no accepted LF match, or write-back failed with Block-mode `DUPLICATES_DETECTED`; for a human |

Campaign Ids: `config/config.yaml` `pipeline.lf_campaigns` (the source; not repeated here).

An LF-matched lead that was NOT written back gets no campaign. Tagged leads drop out
of every later read (anti-join), which is why the sweep exists. To add a new task type
to the campaigns: add it to `lf_campaign_task_types` (and `live_scope.task_types`) -
"same logic as the claimer tasks" (2026-10-21).

## Checklist

**a. Change the config.** Edit `config/config.yaml` `pipeline.live_scope`:
`decisions`, `convert_campaigns`, `task_types`, `writeback_decisions`. Say in the
comment who decided and the date, and the evidence (shadow-compare hit rate).
Precedent widenings: claimer enrich-only 2026-10-19, ebp_trial write-back 2026-10-20,
campaign-gated auto-convert of `create_new` 2026-10-31.

**b. Dry-run the new config.** Locally (see the `pipeline-dry-run` skill for the
env setup):
```powershell
$env:PYTHONPATH = "src"; python -m leadauto run --dry-run
```
Keep LeadFeeder ON: with `--no-leadfeeder` every lead becomes `skip`, so nobody can pass
`live_scope`. The key auto-loads from the repo's `.env` when present. LF match is free;
contact retrieves bill 1 credit per lead not retrieved in the last 12 months. A local dry
run appends rows to the tracked, CI-owned `state/predictions.jsonl` — restore it
afterwards (`git checkout -- state/predictions.jsonl`), never commit them.

Or in CI (inputs: `dry_run`, `limit`, `leadfeeder`, `config` - pipeline.yml). **CI sees
only what is pushed to the ref it runs:** push the edited `config.yaml` to a branch and
dispatch `gh workflow run pipeline.yml --ref <branch> -f dry_run=true` (its state step
then commits predictions/shadow files onto that branch; drop them before merging), or
commit a full alternate file and pass `-f config=config/<file>.yaml`. **A dispatch
without `--ref` dry-runs master's CURRENT config, not your change.** A dry run writes
nothing to Salesforce but still previews the sweep (2 read calls).

**c. Read the QA artifact** (`lead-automation-qa-<run_id>`, `qa_artifact.xlsx`):
- `Conversion` sheet - one row per target (`lead_id, company, source, decision,
  predicted_at, outcome, detail, account_id, contact_id`); `source` = run | sweep;
  `outcome` = converted / already_converted / failed / missing / preflight_skipped /
  left_to_human / dry_run / error / not_attempted. **On a dry run the Conversion sheet
  holds only SWEEP targets** (campaign tagging is off, so no `source = run` rows); this
  run's would-convert leads are the Review rows with `live_scope` = `yes`.
- `Summary`: `convert_targets_run`, `convert_targets_sweep`, `converted`,
  `convert_already_converted`, `convert_failed`, `convert_missing`,
  `convert_preflight_skipped`, `convert_left_to_human`, `convert_dry_run`,
  `sweep_dbid_ticked`, `convert_error`, `live_scope_pass` (`qa_artifact.py`
  `CONVERSION_SUMMARY_KEYS`).
- `Review` sheet `live_scope` / `lf_campaign` columns show why each lead is in or out.
Confirm out loud, with counts, which leads would convert and that they are the ones
intended. Preflight failures are Conversion rows now (no `preflight_failures.csv`).

**d. Commit the config** via the `worktree-pr` skill (the tree is dirty; build the
commit off `origin/master` — this repo's default branch). `master` is what the scheduler
runs, so the merge IS the go-live: only with the owner's explicit go-ahead.

**e. Go live.** The next scheduled run (<= 2 h, weekdays) picks it up. For an
immediate or off-hours run: `gh workflow run pipeline.yml -f dry_run=false`.
Watch it: `gh run watch --exit-status <run-id>`. The CLI exits 1 after any
conversion error, but the workbook and watermark are still written and state is
committed (`if: always()`), so read the artifact rather than the check colour.

**f. Verify in the org - one batched read, not several.** Cost: 1 REST call (a long
`IN (...)` over 2000 chars routes to a Bulk job, a few calls). The API allocation is
shared with other teams.
```powershell
sf data query --query "SELECT Id, IsConverted, Status, ConvertedAccountId, ConvertedContactId, ConvertedOpportunityId, Owner.Name FROM Lead WHERE Id IN ('...','...')" --target-org headless --result-format json
```
Correct looks like: `IsConverted = true`, `Status = Qualified`
(`salesforce.converted_status`), owner **Rohdaten Pool** on Lead, Account and Contact,
**no Opportunity**, Task re-parented to the new Contact. On new Contacts the LF contact
email may land in `KN_Email2__c` (fill-only-blank, best-effort; absence is not a
failure). Bulk results are stringly typed: `'false'` is truthy in Python.

## Traps

- **Batch size <= 3.** Batched SOAP `convertLead()` runs one transaction per call, and
  ~5 conversions' cascaded triggers/flows exhaust a per-transaction governor limit
  (2026-07-17 backlog); the bulk call fails and the retries share the spent context.
  The ceiling is this org's automation load; raise `convert_batch_size` only with a
  logged re-measurement. `.claude/hooks/guard_bash.py` denies `--batch-size > 3` on any
  command without `--dry-run`, from both the **Bash** and the **PowerShell** tool.
- **The guard is a speed bump, not a safety net.** Since 2026-11-01 it is wired to Bash
  AND PowerShell and asks before a live `leadauto run` (`--no-dry-run`, or a bare `run`
  while `config.yaml` has `pipeline.dry_run: false`) and before `gh workflow run` /
  `gh api` with `dry_run=false`. It only sees what Claude Code runs through those two
  tools — not a terminal, not a script that calls `leadauto` internally — and an "ask"
  is one click away from running. Never run a live run locally; prefer the workflow
  (pinned CLI, artifacts — a local live run leaves no artifact).
- **Duplicate rules.** The Lead write-back sends `Sforce-Duplicate-Rule-Header:
  allowSave=true` (`pipeline.writeback_allow_duplicates: true`, since 2026-10-21) because
  the Lead duplicate rules are Alert-mode. A Block-mode rule still rejects with
  `DUPLICATES_DETECTED`, and that lead is tagged `EnrichLeadmanually` (Summary
  `campaign_duplicate_detected`). Duplicate-rule bypass is not available to conversion.
- **Email overwrite.** The write-back overwrites `Lead.Email`, which makes Salesforce
  clear `KN_KununuUserUUID__c` and the consent mirrors (CLAUDE.md, 2026-08-20). Deliberate;
  do not extend it to Contacts.
- A swept lead that fails preflight or SOAP is retried on every run (5/day).

## Report honestly

State converted / already / failed / missing / preflight-skipped / left-to-human as
the run reported them, name the leads that failed, and say what remains outstanding. A
partial batch is a normal outcome and belongs in the summary. If something failed,
fix the cause and let the next run retry (failed leads stay in the sweep) - never
hand-edit `state/` (the file guard blocks that).
