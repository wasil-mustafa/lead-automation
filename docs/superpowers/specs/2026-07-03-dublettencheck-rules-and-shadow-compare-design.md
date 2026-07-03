# Dublettencheck rule engine + shadow comparison — design

**Date:** 2026-07-03
**Status:** approved (brainstormed with Wasil; source: `Touchpoint automatismus logik.jpg` decision tree)
**Scope:** replace the score-threshold matcher with an explicit rule engine that mirrors the
business decision tree, and add a shadow-mode backtest that scores dry-run predictions against
the human conversions that happen in the org. Everything runs under `dry_run: true`.

## Context

The business logic diagram (`Touchpoint automatismus logik.jpg`) defines the duplicate-detection
flow for a touchpoint on a Lead:

1. Lead not found on LeadFeeder → **Klärfall** (human review).
2. Found → enrich → duplicate check at Account level: *name + address*, *name + VAT*,
   or *name alone* → merge onto the Account (contact exists → merge onto the right Contact,
   else new Contact under the Account).
3. No duplicate → is the kununu profile URL stored on an Account?
   - Yes, name differs → **Klärfall**;
   - Yes, name agrees (also against the profile's assigned companies from the kununu API) → merge;
   - No → create new Account.

Decisions made during brainstorming (2026-07-03):

- **Name alone → review**, not auto-link (diagram taken conservatively; name collisions are real).
- **"Hash ebene"** = the profile's assigned-company names from the kununu DWH feed
  (`kununu_feed.py` — the infrastructure earmarked for exactly this). Comparison happens
  after normalization.
- **Address ident** = normalized postal code AND city (street ignored — free-text spelling
  makes full-street equality useless).
- **No-LF branch is literal**: a lead with no accepted LF match is always review — it never
  auto-links and never auto-creates, regardless of task-parsed signals.
- **Non-diagram signals demoted**: VAT-alone, DBID, website host, corporate email domain and
  fuzzy name never auto-link; they surface the candidate as review evidence.
- **Invariant:** conversion always happens after LeadFeeder enrichment. A lead without an
  accepted LF match can only be converted by a human (via the QA `Approve?` gate or manually).
- **Suffix stripping kept:** the shared `normalize_name` strips legal suffixes, so
  "Muster GmbH" and "Muster AG" count as equal names. For rule 2 (name+address) that means a
  sibling legal entity at the same address auto-links — consistent with the sibling-entity
  semantics validated on the control set (see CLAUDE.md, alt-names validation).

## 1. Decision model — ordered rule engine (`match.py`)

`match_account` + `decide` are replaced by a single ordered rule evaluation. First rule that
fires wins:

| # | Rule              | Condition                                                                                     | Decision   |
|---|-------------------|-----------------------------------------------------------------------------------------------|------------|
| 1 | `no_lf_match`     | `enriched.matched` is False (no accepted LF company)                                          | review     |
| 2 | `name_address`    | normalized name equal AND normalized postal code + city equal                                 | **link**   |
| 3 | `name_vat`        | normalized name equal AND VAT equal                                                           | **link**   |
| 4 | `kununu_agree`    | profile UUID or kununu URL hit on an Account AND account name agrees (≥ threshold) with any of: LF company name, kununu profile name, assigned-company names from the feed | **link** |
| 5 | `kununu_conflict` | UUID/URL hit but name agreement below threshold                                               | review     |
| 6 | `name_only`       | normalized name equal, nothing else corroborates                                              | review     |
| 7 | `weak_signal`     | only VAT-alone / DBID / website host / email domain / fuzzy name point at an account          | review     |
| 8 | `no_match`        | nothing matches                                                                               | create_new |

- `MatchResult` gains `rule: str` and `evidence: list[str]` (demoted signals that also fired,
  e.g. `["dbid→001…", "website_host→001…"]`). `AccountMatch.reason` carries the fired rule.
- Numeric scores leave the decision path entirely. `matching.auto_link_threshold` and
  `matching.review_threshold` are removed from `config.yaml` and `Settings`.
- Rule-4 name agreement reuses the token-based scoring shared with the LF gate
  (`textnorm` machinery), with a new tunable `matching.dedupe_name_agree_min: 85`.
- Name equality for rules 2/3/6 = `normalize_name(lead side) == normalize_name(account side)`,
  where the lead side is the LF-matched company name (fallback: `best_company`) and the account
  side is `Name` or `KN_DBLegalCompanyName__c` (both already indexed in `ExistingRecords`).

## 2. Data plumbing

- `soql._ACCOUNT_MATCH_FIELDS` += `BillingPostalCode, BillingCity` (SELECT only; candidates
  already arrive via `Name IN (...)` — no new WHERE clause).
- `textnorm.py` gains tiny normalizers: postal code = strip all whitespace; city = casefold +
  trim (umlaut folding as in `normalize_name`).
- LF address is already flattened by `flatten_company` → `company["Postal_Code"]`,
  `company["City"]`.
- `EnrichedLead` gains `alt_names: list[str]` — the assigned-company names `enrich_leads`
  already computes for the LF match payload are kept on the record instead of thrown away.
  Without feed credentials the list is empty and rule 4 falls back to LF/profile name only
  (same graceful degradation as the LF gate).

## 3. Contact side — unchanged

Mirrors the diagram's right branch and current behavior: linked Account + existing Contact →
link both; linked Account without a Contact → conversion creates the Contact under it
(`match_contact` + converter `setAccountId`/`setContactId` stay as they are).

## 4. QA artifact

- Review sheet gains `match_rule` and `match_evidence` columns (next to the `lf_*` columns).
- Summary sheet counts leads per fired rule per run.

## 5. Config

```yaml
matching:
  # (lf_* keys unchanged)
  dedupe_name_agree_min: 85   # rule-4 agreement floor (account name vs LF/profile/alt names)
  # auto_link_threshold / review_threshold REMOVED — decision is rule-based
```

## 6. Prediction log (written by every dry run)

Dry-run stage 1 appends its decisions to a committed `state/predictions.jsonl` — one record
per lead:

- run timestamp, lead id, task ids, fired `rule`, decision, predicted account id / contact id,
  evidence, the exact KN_* payload `write_back` would have written, LF company id + name.
- If a lead reappears in a later run (overlap window), the newest prediction replaces the
  pending one. Records carry `status: pending | resolved`; each lead is scored exactly once.
- CI: the PR-#9 watermark guard stays (watermark commits only on non-dry runs), but
  predictions and shadow results commit on **every scheduled run**. The existing concurrency
  group serializes runs.

## 7. Shadow comparison (`python -m leadauto shadow-compare`)

A step at the start of each scheduled stage1 run (same auth session):

1. Load pending predictions; query those Leads for `IsConverted, ConvertedDate,
   ConvertedAccountId, ConvertedContactId` + current KN_* fields; pull the converted Accounts
   (`CreatedDate, Name, Website, BillingPostalCode, BillingCity, KN_*` firmographics) and
   Contacts (`Email, Phone`).
2. Classify the **decision** for each now-converted lead:
   - predicted `link` → HIT if `ConvertedAccountId` == predicted account, else MISS
     (wrong account);
   - predicted `create_new` → HIT if the converted Account's `CreatedDate` ≈ `ConvertedDate`
     (tolerance ~1 h; Salesforce creates the Account at conversion time), MISS if the human
     linked a pre-existing Account the matcher should have found;
   - predicted `review` → not scored; the human outcome (linked vs new, which account) is
     recorded for threshold calibration.
3. Score **enrichment consistency** per field — predicted KN_* payload vs what humans left on
   the Lead / resulting Account: website host, postal code + city, country code, employee bin,
   revenue. Reported as agree / differ (both values shown) / human-empty / LF-empty.
4. Unconverted leads stay `pending` (humans may take days); pending expires after 90 days.
5. Output:
   - cumulative machine-readable results → committed `state/shadow_results.jsonl`;
   - per-run human-readable `shadow_comparison.xlsx` workflow artifact with three sheets:
     **Decisions** (per-lead hit/miss, both account names side by side), **Fields**
     (per-field agreement rates), **Summary** (rolling accuracy per rule).

Persistence choice: committed repo files (not workflow artifacts) — survives artifact expiry,
repo is private, volumes are small (~hundreds of JSONL lines/day).

## 8. Validation — all dry-run

- TDD rewrite of the match tests: one test block per rule, including the `kununu_conflict`
  and `no_lf_match` invariants and the demoted-signal evidence.
- Full suite green (193 tests pre-change).
- `stage1 --dry-run` over the current window; read the QA artifact's rule distribution
  together before anything goes live.
- Shadow comparison then accumulates daily accuracy while `dry_run: true` stays on; go-live
  criteria are judged on the Decisions/Fields accuracy sheets.

## Out of scope

- No Salesforce metadata changes; write-back only to existing KN_* fields (unchanged).
- No change to LF matching itself (name-agreement gate, thresholds 85/90 stay).
- Task-type rollout (`event`, `content`) unchanged.
