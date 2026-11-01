---
name: pipeline-dry-run
description: Dry-run the Lead Automation pipeline (`python -m leadauto run --dry-run`) locally with the environment set up correctly, then summarize the QA workbook. Use for "dry run", "pipeline dry run", "stage1 dry run" (this skill was named `stage1-dry-run` until 2026-11-01), "run the pipeline locally", "test the pipeline", "preview what would convert", or to check a rule-engine / live_scope change against real leads. Read-only against production; to dispatch the CI dry run instead, see the gh command in the body.
disable-model-invocation: true
---

# Pipeline dry run

Since 2026-11-01 there is ONE command, `python -m leadauto run` (the old `stage1` /
`stage2` commands are gone): extract -> enrich -> match -> write back -> campaign tag ->
convert -> QA workbook. With `--dry-run` it reads production and writes nothing.

**A dry run does:** extract tasks/leads, call LeadFeeder (free match; credits only if
enrichment retrieves run), apply the rule engine, build the QA workbook, append shadow
predictions to `state/predictions.jsonl` (dry runs always do), and **preview the convert
sweep** (earlier campaign-tagged, still-unconverted leads; ~2 read calls).
`state/predictions.jsonl` is tracked and CI-owned, and the sweep reads a lead's newest row
as its decision — so afterwards restore it (`git checkout -- state/predictions.jsonl`);
never commit local dry-run rows.

**Cost:** the extract and match reads, plus ~2 Salesforce read calls for the sweep
preview (`guard_bash.py`'s advisory table put the old `stage1` at ~12); LF match is free, contact
retrieves bill 1 LeadFeeder credit per lead not retrieved in the last 12 months.
**It does not:** write back to Leads, insert CampaignMembers, tick `KN_NoOwnDBID__c`,
preflight-write, call SOAP `convertLead`, or advance the watermark.

Run the bundled script rather than composing the command by hand:

```powershell
powershell -ExecutionPolicy Bypass -File .claude/skills/pipeline-dry-run/scripts/run_dry_run.ps1
```

Options: `-Limit <n>` (LIMIT-scope the task scan; use while iterating), `-NoLeadfeeder`
(no credits, no key needed), `-NoKununuFeed` (skip the feed download), `-BatchSize <n>`
(SOAP batch, default config 3), `-QaPath <path>` (must be outside OneDrive). They map
1:1 to `run`'s `--limit`, `--no-leadfeeder`, `--no-kununu-feed`, `--batch-size`,
`--qa-path`.

## What the script handles, and why each matters

1. **`PYTHONPATH=src`.** The package is not pip-installed; `python -m leadauto` fails
   without it.
2. **Missing local keys degrade silently.** The keys auto-load from the repo's `.env`
   when it exists (python-dotenv, `Settings.load` in `config.py`); the canonical local
   copy is `C:\Users\wasil\.secrets\kununu.env`. The LF key is shared with production,
   so a local run contends for the same quota. Without a key every lead
   becomes `no_lf_match` -> terminal skip, which looks exactly like a matching
   regression. The feed is read from **`KUNUNU_API_PASSWORD`** (HTTP Basic); without it
   the feed read degrades quietly to no alt names and empty `kununu_score`. Since
   2026-09-28 `live_scope.max_kununu_score` is `null`, so a missing score no longer
   zeroes `live_scope_pass`; it only costs name-agreement inputs. **Never set
   `KUNUNU_API_TOKEN` locally** (sent as Bearer; the feed 401s). The script warns when a
   key is in neither the shell env nor `.env`; set it ad hoc or pass the matching
   `-No...` switch to make the degradation deliberate.
3. **OneDrive.** The repo is inside OneDrive; an xlsx mid-sync can kill the openpyxl save
   with `PermissionError: [Errno 13]`. Output goes to `%TEMP%\leadauto-dryrun`, and a
   `-QaPath` containing `OneDrive` is refused. Also close any open copy of the workbook
   in Excel before re-running to the same path.
4. **`FORCE_COLOR`.** Some terminal wrappers export it and the `sf` CLI honours it even
   through a pipe, which breaks `--json` parsing. Dropped, with `NO_COLOR=1` set.

Prerequisite the script does *not* handle: the `sf` CLI must be authenticated to alias
**`headless`** (or `SF_AUTH_URL` set). It is production; `--dry-run` keeps it read-only.

## Write modes are refused

`--dry-run` is passed unconditionally and there is no switch to remove it. A live run is
a deliberate act: dispatch `pipeline.yml` with `dry_run` unchecked, normally after a
config change to `live_scope` / the campaign gate; the `live-convert` skill owns that
flow. The project's shell guard (`guard_bash.py`, wired to the Bash and PowerShell tools
since 2026-11-01) asks before a local `leadauto run --no-dry-run`, a bare `leadauto run`
while `pipeline.dry_run: false`, and `gh workflow run` / `gh api` with `dry_run=false`,
and denies `--batch-size > 3` without `--dry-run`. It does not see this script's inner
`python -m leadauto run` call (the script forces `--dry-run` anyway) or anything typed in
a terminal outside Claude Code. An "ask" is not a reason to go ahead: never run a live run
locally.

## CI alternative

To run the dry run on a runner (it has the keys and feed token):

```powershell
gh workflow run pipeline.yml -f dry_run=true -f limit=20 -f leadfeeder=true
```

Inputs (`.github/workflows/pipeline.yml`): `dry_run` (default true), `limit` (0 =
unlimited), `leadfeeder` (default true), `config` (alternate config path). There is no
feed input; CI always reads the feed. The QA workbook is uploaded as artifact
`lead-automation-qa-<run_id>`. A manual dispatch is dry by default, but the cron-job.org
job sends `dry_run=false`, so scheduled runs are LIVE.

## After the run

Report from the QA workbook (sheets **Review**, **Conversion**, **Summary**), plus the
CLI's own final line (leads, `link` / `review` / `create_new`, `skipped`, `wrote_back`
= 0 on a dry run, `targets` split run/sweep, `converted` / `failed` / `missing`,
`preflight_skipped`, `left_to_human`).

- **Review** (one row per lead): `match_rule` / `match_evidence` and `decision`. Check the
  mix is plausible first: a run that is suddenly all `no_lf_match` means a missing key,
  not a broken engine. `review` rows by rule (`kununu_conflict`, `name_only`,
  `duplicate`, `weak_signal`) mean a candidate account WAS surfaced and a human must
  adjudicate. `lf_rejected` = matches degraded by the post-retrieve sanity gate (a spike
  is worth reading case by case). Judge acceptance on `lf_name_agreement` /
  `lf_agreed_via`, never `lf_match_score` alone (0.9+ on wrong companies).
- **Gates, per lead:** `live_writeback` (would be written back), `live_scope` (`yes`,
  `yes_wide_match`, or an exclusion reason such as `campaign_none` /
  `campaign_matched_incomplete`), and `lf_campaign` (the bucket it would be tagged into
  on a live run: `unmatched`, `matched_complete` = "EnrichLeadfedup" = the only
  auto-convert bucket, `matched_incomplete`). `convert_blockers` flags preflight
  problems; known false positive: `postal_length` when the payload changes the country
  (the sheet merges payload `CountryCode` over the lead's stale `Country`; the real
  preflight reads post-write-back state and passes).
- **Conversion** (one row per conversion target): on a live run `source` = `run` (tagged
  by this run) or `sweep` (earlier campaign member still unconverted). **On a dry run every
  row is `source = sweep`** (campaign tagging is off), with outcome `dry_run` or
  `left_to_human` (no prediction row, or a decision outside `live_scope.decisions`; fail
  closed); this run's would-convert leads are the Review rows with `live_scope` = `yes`.
  `decision` / `predicted_at` come from the recorded prediction; `detail` names the
  checks or reason.
- **Summary:** `live_writeback_pass`, `live_scope_pass`, `live_scope_wide_match`,
  `campaign_<bucket>` counts, and the conversion counters `convert_targets_run`,
  `convert_targets_sweep`, `converted`, `convert_failed`, `convert_missing`,
  `convert_preflight_skipped`, `convert_left_to_human`, `convert_dry_run`,
  `sweep_dbid_ticked`, `convert_error`, then `rule_<name>` tallies.
  `live_scope_pass` counts what the Review verdict (including the campaign gate) says
  would proceed.

Read counts and a few sample rows out of the xlsx with a small script rather than
pasting lead data into the transcript. The PreToolUse guard prompts before opening any
`*.xlsx`; that prompt is expected here.

Do **not** re-run to double-check a number, and do **not** query the org to verify one.
Both cost calls against a quota shared with business-critical integrations; the workbook
on disk is the record of the run.
