# Name-recall fix + auto-approved conversions — design (2026-07-10)

Approved by Wasil 2026-07-10. Two changes preparing the create_new go-live.

## A. Account name-recall fix (`soql.accounts_by_signals`)

Problem (2026-07-08 backtest, miss class "candidate-recall gap"): candidate Accounts are
pulled by exact `Name IN (...)`, so normalized-equal spelling variants ("GmbH & Co.KG." vs
"Gmbh & Co. Kg") never enter the pool and the engine says create_new where it should link —
on the create_new path that creates a duplicate account.

Fix: keep the exact `Name IN` clause and ADD one prefix-anchored `Name LIKE` clause per name,
built from `textnorm.normalize_name` (the same normalization every name rule uses):
`"Kaufland Dienstleistung GmbH & Co.KG."` → base `kaufland dienstleistung` →
`Name LIKE 'kaufland%dienstleistung%'` (SOQL LIKE is case-insensitive; `%` between tokens
absorbs punctuation/suffix variance).

Guards:
- No LIKE clause when the normalized base is shorter than 4 characters (acronym-ish names
  would sweep too wide; exact IN still applies; the LF acronym sanity gate covers these).
- LIKE metacharacters stripped from tokens (`_like_escape`).
- Patterns deduped.

Precision is unaffected: LIKE only widens the candidate *pool*; all name rules compare
normalized names for equality. Residual limit: intra-token variance (`O'Brien` vs `OBrien`)
is still missed.

## B. Auto-approved conversions (Stage 1 emits the approved CSV)

Requirement (Wasil 2026-07-10): no manual `Approve?` filling. Once live, every lead with
decision `link` or `create_new` converts; `skip`/`review` never do (review rows stay in the
QA artifact for humans).

Mechanism: new config flag `conversion.auto_approve` (default **false** until go-live). On
LIVE Stage 1 runs only, after matching, Stage 1 writes `review/approved_auto_<UTCts>.csv`
containing exactly the Review-sheet rows with decision ∈ {link, create_new}, `Approve?`
pre-set to `yes`, with the columns `targets_from_approved` reads (lead_id, decision,
account_match_id, contact_match_id, contact_reason, lf_company_id, kununu_profile_uuid,
kununu_url, lf_contact_email). Stage 2 is untouched — preflight, clustering, pool-account
identity gate and batching all run as designed. `stage1.yml` commits `review/*.csv` on live
runs. Dry runs never emit a CSV.

Rollout brake (amended during implementation): a required-reviewers GitHub Environment is
NOT available on this repo (private repo on a free plan — API 422, verified 2026-07-10), so
the `environment:` reference was removed from `stage2.yml`. The brake is that Stage 2 is
dispatch-only with `dry_run` defaulting true: live conversion takes a deliberate manual
dispatch with dry_run unchecked.

## Also closed

- Lead→Contact field mapping confirmed working by Wasil (KN_LinkedInProfileLink__c open
  question in `convert/CLAUDE.md` closed).

## After landing

Measure the recall fix on the 2026-07-10 closed-task create_new population (156 leads): how
many flip to link/review. Use the still-create_new set to pick 3 certainly-new leads for the
first live conversion test.
