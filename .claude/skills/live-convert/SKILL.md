---
name: live-convert
description: Checklist and exact commands for a deliberate LIVE Salesforce conversion (Stage 2), plus how to verify the result afterwards. Use when asked to convert leads for real, go live with a batch, or run stage2 non-dry.
disable-model-invocation: true
---

# Live conversion (Stage 2)

**Conversion is irreversible.** SOAP `convertLead()` creates Accounts/Contacts,
flips the Lead, re-parents its Task and fires the org's triggers, flows and
validation rules. There is no undo, and cleaning up afterwards is manual work in
production. Everything below exists because the brake is deliberation, not
software: a GitHub Environment with required reviewers is not available on a
private repo without a paid plan (verified 2026-07-10).

This skill does not automate the live step. It prepares it, states the exact
command, and verifies afterwards. **You run the dispatch; do not run it on the
user's behalf without an explicit, current instruction naming this as a live run.**

## Before anything: know what would convert

Read the approved CSV that Stage 2 will pick up — by default the **newest**
`review/approved_*.csv`, which may not be the one anyone has in mind.

```powershell
Get-ChildItem "review\approved_*.csv" | Sort-Object Name | Select-Object -Last 3 Name,Length,LastWriteTime
```

Confirm out loud, with counts: how many rows, which decisions
(`create_new` / `link`), and that no `review` or `skip` row is present. Stage 2
drops `skip` rows and preflight skips rule violations into
`preflight_failures.csv`, but a wrong *file* is not something either check catches.

## The batch-size ceiling is not a style preference

`--batch-size` must be **≤ 3** for live runs. Measured during the 2026-07-17
backlog conversion: batched SOAP `convertLead()` runs **one transaction per call**,
and roughly 5 conversions' cascaded triggers/flows exhaust a per-transaction
governor limit — the bulk call then fails and the individual retries share the
already-spent context, so the whole batch dies together. The ceiling is this org's
automation load, not a Salesforce constant, so it does not scale with runner size.
The Bash guard denies `--batch-size > 3` on a non-dry command.

## Order of operations

1. **Dry-run the exact same inputs first.** Same approved file, same batch size.
   ```powershell
   $env:PYTHONPATH = "src"; python -m leadauto stage2 --dry-run --batch-size 3 --approved review\<file>.csv
   ```
   Read `preflight_failures.csv`: those leads will be skipped, not converted. A
   preflight failure is a finding to understand, not noise to move past.
2. **Prefer the workflow over a local run.** CI has the pinned CLI (2.140.6), the
   login retry that works around the intermittent `sf org login sfdx-url` hang, and
   it uploads `stage2_log.txt` + `preflight_failures.csv` as artifacts. A local live
   run leaves no artifact behind.
   ```powershell
   gh workflow run stage2.yml -f dry_run=false -f batch_size=3 -f approved="review/<file>.csv"
   ```
   `dry_run` defaults to **true** on dispatch — passing `dry_run=false` is the
   entire gate. Omitting `approved` uses the newest file, which is exactly the
   ambiguity step 0 removes.
3. **Watch the run**, then read the log artifact rather than inferring success from
   a green check.
   ```powershell
   gh run watch --exit-status <run-id>
   ```
   The job exits non-zero on `failed` or `missing` conversions. `already_converted`
   is a clean no-op — Stage 2 is idempotent (preflight pre-queries `IsConverted`),
   so re-running a partially completed batch is safe and is the correct recovery
   move.

## Verify in the org afterwards — one read, not several

Budget it: the org's API allocation is shared with other teams' integrations. One
batched query answers all of it, so do not check the leads one at a time.

```powershell
sf data query --query "SELECT Id, IsConverted, Status, ConvertedAccountId, ConvertedContactId, ConvertedOpportunityId, Owner.Name FROM Lead WHERE Id IN ('...','...')" --target-org headless --result-format json
```

What "correct" looks like, from the go-live run (2026-07-18): `IsConverted = true`,
`Status = Qualified` (pinned via `salesforce.converted_status`), owner **Rohdaten
Pool** on the Lead, Account and Contact, **no Opportunity**, and the Task
re-parented to the new Contact. On new Contacts only, the LF contact email may land
in `KN_Email2__c` when it differs from the primary Email (fill-only-blank,
best-effort — its absence is not a failure).

Note the results are stringly typed when the query routes to Bulk (a long
`IN (...)` does): `'false'` is truthy in Python. Read the JSON with your eyes here,
or coerce properly if scripting it.

## Report honestly

State converted / already / failed / missing / preflight-skipped as the run
reported them, name the leads that failed, and say what remains outstanding. A
partial batch is a normal outcome and belongs in the summary, not smoothed over.
If anything failed, the recovery is to fix the cause and re-run the same file —
never to hand-edit `review/` or `state/` (the file guard blocks that).
