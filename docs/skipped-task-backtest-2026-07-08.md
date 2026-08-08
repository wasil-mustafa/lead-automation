# Skipped-task backtest (2026-07-08, Actions run 28937405531)

Historical evaluation record, moved out of `CLAUDE.md` on 2026-07-25. The two conclusions still
referenced from the project guide stayed there: 87% correct account on 283 link-biased cases, and
the ~280-leads-per-match-batch SOQL gotcha (chunk `load_existing_records` in batches of 40).

## Why the skips exist

Salesforce **re-parents a converted Lead's open Tasks onto the new Contact**, so Stage-1 "lead not
found or already converted" skips are contact-WhoId (`003…`) tasks — of 283: 47 traced to converted
Leads (`Lead.ConvertedContactId`, truth = `ConvertedAccountId`), 236 logged directly on existing
Contacts (no Lead ever; truth = `Contact.AccountId`).

`python -m leadauto backtest-skipped` (+ dispatch-only `backtest-skipped.yml`) replays them through
the real enrich→match path, spending LF **company** credits only
(`enrich_leads(include_contacts=False)`).

## Results

- LF match 269/283 (95%)
- correct account 245/283 (87%: 235 link + 10 review-surfaced)
- contact re-found 236/283

## The 24 misses

- **14 = rule order** — truth sat in evidence as `kununu_profile_uuid→…` but `name_address`/
  `name_vat` fired first on an org-duplicate; humans follow the profile-UUID account. Fixed by the
  `kununu_over_name` rule (shipped 2026-07-08).
- **4** = engine linked the profile-carrying account while the human chose another duplicate
  (defensible).
- **1 = candidate-recall gap** — `accounts_by_signals` matched `Name IN (…)` exactly, so
  normalized-equal spelling variants ("GmbH & Co.KG." vs "Gmbh & Co. Kg") never entered the pool.
  CLOSED 2026-07-10: the account query now also carries a prefix-anchored `Name LIKE` per name
  (`textnorm.name_like_pattern`: normalized base tokens `%`-joined, skipped for bases <4 chars);
  precision untouched (name rules re-compare normalized names).

## Caveat

The population is **link-biased** (accounts existed — that's why they were skipped): it validates
dedupe, NOT `create_new`.

## Gotcha

~280 leads in one match batch blow the **100k-char SOQL limit** in `accounts_by_signals` — the
backtest chunks `load_existing_records` in batches of 40.
