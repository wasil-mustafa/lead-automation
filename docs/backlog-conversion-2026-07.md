# Closed-task backlog conversion (2026-07-15 → 2026-07-17) — DONE

Historical record, moved out of `CLAUDE.md` on 2026-07-25. The two still-operative facts stayed
in the project guide: keep `--batch-size` ≤3 for live conversions, and the remaining follow-up
batches (36 preflight drops + 32 review + 27 link rows).

## Scope

Converting the 626 leads behind closed claimer/EBP tasks — create_new only (266 rows,
`review/approved_20260715_backlog_createnew.csv`), converted records owned by Rohdaten Pool via
`config/config.backlog.yaml` (new flags `include_enriched_leads`, `record_predictions`; workflows
take a `config` input; reader + stage-1 match phase now chunk for the 100k-char SOQL limit).

Dry run 29425910623 ✓ (266 create_new / 301 skip / 32 review / 27 link).

## Stage 1 live write-back — DONE 2026-07-16

Run 29499738399 green — 639 leads, 603 written, 36 row failures in the QA Failures sheet
(29× DUPLICATES_DETECTED duplicate rule, 6× "Private Mail" validation rule, 1× invalid LF email);
the population had grown since the dry run (272 create_new / 30 link / 32 review).

Two earlier dispatches (29430746090, 29497405506) crashed on the same write-back and were fixed by
making `update_bulk` survive partial bulk failures. That whole CLI bulk-job path — and its
`FailedRecordDetailsError`/`sf data bulk results` failure machinery — was RETIRED 2026-07-18 in
favor of `SfClient.update_records` (one REST sObject-Collections PATCH per ≤200 records with inline
per-row errors).

NOTE: the run advanced + committed the shared watermark to 2026-07-16T12:03 (newest closed backlog
task), shrinking the scheduled dry runs' scan window.

## Stage 2 backlog conversion — DONE 2026-07-17

Dry run 29577236996 (230/266 pass preflight; 36 drops: 22 employee_category, 17
address_create_new, 1 dbid_xor, 1 freemail), then live run 29578167853 converted 115 but FAILED the
other 115 — in every 10-lead batch exactly the first 5 converted and the last 5 failed: **batched
SOAP `convertLead()` runs one transaction per call and ~5 conversions' cascaded triggers/flows
exhaust a per-transaction governor limit** (the 3-lead July test sat under it). Per-lead SOAP errors
were being discarded (fixed in 37bce35 — now logged at ERROR).

Retry run 29579163559 with `batch_size=3` converted the remaining 115/115 (0 failed/missing;
org-verified: 230/230 targets IsConverted, owner Rohdaten Pool, no Opportunities, KN_Email2__c on
28+25 contacts).
