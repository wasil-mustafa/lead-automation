---
name: stage1-dry-run
description: Run Stage 1 locally in dry mode with the environment set up correctly, then summarize the QA artifact. Use when asked for a local dry run, to preview what the pipeline would enrich/match, or to check a rule-engine change against real leads.
disable-model-invocation: true
---

# Stage 1 dry run

Runs extract → enrich → match → (skipped) write-back → QA artifact against the
production org **read-only**. No Salesforce writes, no conversions.

Run the bundled script rather than composing the command by hand — it encodes four
local-run gotchas, in order:

```powershell
powershell -ExecutionPolicy Bypass -File .claude/skills/stage1-dry-run/scripts/run_dry_run.ps1
```

Options: `-Limit <n>` (LIMIT-scope the task scan — use this while iterating),
`-NoLeadfeeder` (no credits, no key needed), `-NoKununuFeed` (skip the ~551 MB feed
download), `-QaPath <path>` (must be outside OneDrive).

## What the script handles, and why each matters

1. **`PYTHONPATH=src`.** The package is not pip-installed; `python -m leadauto`
   fails outright without it.
2. **`LEADFEEDER_API_KEY` / `KUNUNU_API_TOKEN` are not on this machine.** All local
   plaintext copies were deleted 2026-07-05; the keys live only in GitHub Secrets.
   Missing them does not error — the run *degrades*: without the LF key every lead
   becomes `no_lf_match` → terminal skip, and without the feed token there are no
   profile scores, so the live-scope gate fails closed and `live_scope_pass` reads
   0. Both look exactly like a matching regression. The script warns loudly for
   each; set them ad hoc in the shell for a real matching check, or pass the
   corresponding `-No…` switch to make the degradation deliberate.
3. **OneDrive.** The repo is inside OneDrive. If a previous run's xlsx is
   mid-sync, the openpyxl save can die with `PermissionError: [Errno 13]` partway
   through. Output goes to `%TEMP%\leadauto-dryrun` instead, and any `-QaPath`
   containing `OneDrive` is refused.
4. **`FORCE_COLOR`.** Some terminal wrappers export it and the `sf` CLI honours it
   even through a pipe, so `--json` output comes back ANSI-wrapped and
   `json.loads` fails. Dropped, with `NO_COLOR=1` set.

Prerequisite the script does *not* handle: the `sf` CLI must be authenticated to
the alias **`headless`** (or `SF_AUTH_URL` set). It is production — the whole point
of `--dry-run` is that this stays read-only.

## Write modes are refused

`--dry-run` is passed unconditionally and there is no switch to remove it. A live
run is a deliberate act: dispatch `stage1.yml` with `dry_run` unchecked, or see the
`/live-convert` skill. The project's Bash guard will also prompt on any command
carrying `--no-dry-run` or `dry_run=false`.

## After the run

Report, from the QA artifact (sheet **Review**, plus **Summary**):

- **Counts** from the CLI's own final line: leads, `link` / `review` /
  `create_new`, `skipped`, `wrote_back` (0 on a dry run by design).
- **Decision mix vs. the rule that produced it** — `match_rule` and
  `match_evidence`. Check the mix is plausible before anything else: a run that is
  suddenly all `no_lf_match` means a missing key (gotcha 2), not a broken engine.
  Note any `review` rows by rule: `kununu_conflict`, `name_only`, `duplicate`,
  `weak_signal` each mean a candidate account WAS surfaced and a human must
  adjudicate.
- **`lf_rejected`** — matches degraded by the post-retrieve sanity gate (country
  contradiction, or an acronym-like name with no corroboration). These are terminal
  skips; a spike here is worth reading case by case.
- **`name_agreement` / `agreed_via`** — how the LF match was accepted. Raw
  `match_score` alone is untrustworthy (0.9+ on wrong companies), so judge
  acceptance on these two.
- **Live scope** — `kununu_score`, `live_scope` (yes / exclusion reason) and the
  Summary's `live_scope_pass`. This is what *would* have been written back and
  converted on a live run. Everything else stays with humans.
- **`_convert_blockers`** flagged in the sheet, if any. Known false positive:
  `postal_length` when the payload changes the country (the sheet merges payload
  `CountryCode` over the lead's stale `Country` name, and leads default to
  Germany). The real Stage 2 preflight reads post-write-back state and passes.

Read counts and a few sample rows out of the xlsx with a small script rather than
pasting lead data into the transcript. The PreToolUse guard will prompt before
opening any `*.xlsx` — that prompt is expected here.

Do **not** re-run to double-check a number, and do **not** query the org to verify
one. Both cost calls against a quota shared with business-critical integrations;
the artifact on disk is the record of the run.
