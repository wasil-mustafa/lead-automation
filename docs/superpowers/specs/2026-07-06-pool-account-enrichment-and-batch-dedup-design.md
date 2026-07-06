# Pool-Account enrichment + in-batch same-company dedup — Design

**Date:** 2026-07-06 · **Status:** approved by Wasil (section-by-section)

## Problem

Two gaps in the Stage 2 conversion path, both surfaced reviewing the enrichment→conversion flow:

1. **Enrichment never reaches a linked Account.** `Database.convertLead` with `setAccountId`
   does not update the existing Account — Lead→Account field mapping only populates *newly
   created* records. So the `KN_*` enrichment Stage 1 writes to the Lead is discarded on the
   link path. This is intentional for real (sales-owned) Accounts, but **Accounts owned by the
   `Rohdaten Pool` user are raw, unqualified data and should be enriched.** 224,085 Accounts
   are pool-owned (org count, 2026-07-06), so the link path hits pool Accounts often.

2. **Same-company create-new leads duplicate Accounts.** Every `create_new` lead has an
   accepted LeadFeeder match (the `no_match` rule only fires after an LF match; a no-LF-match
   goes to `review`), so `lf_company_id` is populated on exactly the at-risk leads. Two
   `create_new` leads for the same LF company in one run each create their own Account. The
   org has two **active** Account duplicate rules (`Account_Duplicate_Rule`,
   `Standard_Account_Duplicate_Rule`); per the legacy lesson these **block** `convertLead`, so
   today the second lead most likely fails loudly (via the Stage 2 exit-non-zero path) rather
   than silently duplicating — either way the outcome is wrong. Desired: one Account, N Contacts.

## Decisions (Wasil, 2026-07-06)

- **Pool identity = Owner.** A linked Account is updatable iff `OwnerId = 005Mz00000Xw0jjIAB`
  (`Owner.Name = 'Rohdaten Pool'`, a User). Sales-owned Accounts are never touched. The Id is
  pinned in config (stable across renames); equivalent to the `Owner.Name` rule.
- **Timing = Stage 2, post-approval.** The pool-Account update runs only for human-approved
  links, at conversion time. Values come from the Lead's current `KN_*` fields plus keys carried
  in the approved CSV — no Salesforce write to the Lead is added.
- **Write policy = overwrite-on-difference.** For each mapped field where we have a value AND it
  differs from the Account's current value, write it. Never blank a field we have no value for;
  skip no-op writes (keeps the Bulk payload, field history, and any triggers minimal).
- **Kununu identity is a match-validity gate, not a plain field.** On the pool-Account update
  the kununu profile URL (`KN_KununuLinkProfile__c`) and profile UUID
  (`KN_KununuProfileUuid__c`) are treated specially: blank on the Account → fill; equal →
  no-op; **present on both and different → the match is suspect, so skip that lead's conversion
  and route it to review (report it); do not write the identity.** Comparison = normalized URL
  + trimmed UUID. This complements the Stage 1 `kununu_conflict` rule, catching conflicts on
  links made via name+address. Scope: pool-owned links (where we are about to write identity).
  The kununu profile *name* (`KN_KununuProfileName__c`) is not an identity gate — it is a plain
  overwrite-on-difference field.
- **Dedup = Stage 2 auto-cluster + link.** Cluster approved `create_new` leads by
  `lf_company_id` (strong key only; blank ⇒ singleton; never cluster on fuzzy name). Convert one
  primary per cluster (create-new), capture its new `AccountId`, then convert the cluster's
  secondaries linked to it → one Account, N Contacts. Fully automatic.
- **Enabling change (no metadata changes).** The Lead has no profile-UUID field and we add
  none. The pipeline-derived keys Stage 2 needs travel as new QA Review columns into the
  approved CSV: `lf_company_id` (clustering) and `kununu_profile_uuid` (= `e.kununu_profile_uuid`,
  for the conflict gate). The lead's profile URL is already the existing `kununu_url` column.

The two features are orthogonal at runtime (a create-new cluster has no existing Account; a
cluster's secondaries link to the newly created, sales-owned Account, which is not pool-owned)
but share the Stage 2 convert touchpoint, so they ship as one change.

## 1. Stage 1 change — expose the cluster key + conflict key (`qa_artifact.py`)

Add two columns to `REVIEW_COLUMNS` and `_review_row`:
- `lf_company_id` (value: `enr.lf_company_id`) — feature 2 cluster key.
- `kununu_profile_uuid` (value: `e.kununu_profile_uuid`) — feature 1 conflict key.

`kununu_url` already exists in the sheet (the lead's profile URL). No other Stage 1 behavior
changes, and **no Lead field is written** (no metadata change). Older approved CSVs without the
new columns degrade gracefully: `targets_from_approved` reads `None`, so those `create_new`
leads become singletons and the UUID side of the conflict gate simply doesn't fire.

## 2. Feature 1 — pool-Account enrichment (`convert/pool_enrich.py`, new)

New Stage-2 step in `run_stage2`, ordered **after preflight, before convert**:

```
targets_from_approved → run_preflight → resolve_pool_links → convert_leads(cluster-aware)
```

`resolve_pool_links(client, link_targets, settings, logger)` — for approved link targets
(`account_id` set):
1. Query the Accounts: `SELECT Id, OwnerId, KN_KununuLinkProfile__c, KN_KununuProfileUuid__c,
   <other mapped fields> FROM Account WHERE Id IN :ids`.
2. Query the Leads' enrichment fields (may reuse preflight's Lead query — see Open Items).
3. Keep only pool-owned Accounts (`OwnerId == settings.rohdaten_pool_owner_id`); leave
   sales-owned links untouched and unchanged.
4. **Conflict gate (per pool link):** compare the lead's profile URL (approved CSV `kununu_url`,
   normalized via `textnorm.normalize_url`) with the Account's `KN_KununuLinkProfile__c`, and
   the lead's `kununu_profile_uuid` (trimmed) with the Account's `KN_KununuProfileUuid__c`. If
   either identity is present on both sides and differs → **skip this lead's conversion, report
   it (review), do not update the Account.** These reported leads are removed from the target
   set handed to `convert_leads`.
5. **Payload (surviving pool links):** for each mapped field, include it only when the lead
   value is non-empty AND differs from the Account's current value (overwrite-on-difference);
   blank identity fields are filled here. Push via the existing Bulk path (`client.update_bulk`).
6. Return a summary (pool-updated count, kununu-conflict/review count, skipped-non-pool count,
   per-field change counts) for logging and the Stage 2 result / report CSV.

The Account update runs regardless of whether the lead then converts (enriching a raw pool
Account with fresh company data is correct on its own); if a convert later fails, the enrichment
is not lost.

**Field map (Lead → Account), grounded in `describe` on `headless` 2026-07-06:**

| Lead source | Account field | Handling |
|---|---|---|
| `kununu_url` (CSV) | `KN_KununuLinkProfile__c` | identity: fill-if-blank / conflict-gated |
| `kununu_profile_uuid` (CSV) | `KN_KununuProfileUuid__c` | identity: fill-if-blank / conflict-gated |
| `KN_KununuProfileName__c` | `KN_KununuProfileName__c` | overwrite-on-difference |
| `KN_Region__c` | `KN_Region__c` | overwrite-on-difference (restricted picklist) |
| `KN_VATNumber__c` | `KN_VATNumber__c` | overwrite-on-difference |
| `KN_Employee__c` | `KN_EmployeeCategory__c` | **different name; verify picklist labels** |
| `KN_KununuProfileClaimed__c` | `KN_KununuProfileClaimed__c` | overwrite-on-difference |
| `KN_KununuProfileClaimedAt__c` | `KN_KununuProfileClaimedat__c` | note Account casing |
| `Street`/`City`/`PostalCode`/`CountryCode` | `BillingStreet`/`BillingCity`/`BillingPostalCode`/`BillingCountryCode` | overwrite-on-difference (see Open Items) |

Dropped (no Account target exists): `KN_LinkedInProfileLink__c` (company LinkedIn),
`KN_CompanyEmail__c`. Lead `KN_KununuUserUUID__c` is a Contact identity, not an Account field.

**Config:** add `rohdaten_pool_owner_id: "005Mz00000Xw0jjIAB"` to `config.yaml` and `Settings`.

## 3. Feature 2 — same-company clustering (`convert/converter.py`, `pipeline/stage2_convert.py`)

`ConversionTarget` gains `lf_company_id`, `kununu_profile_uuid`, and `kununu_url` (all `str |
None`), parsed from the approved CSV in `targets_from_approved` (the latter two feed the
Feature 1 conflict gate; `lf_company_id` feeds clustering).

`convert_leads` becomes two-phase (orchestration in Python; `build_convert_apex` unchanged —
each phase is an ordinary per-batch convert):
1. Split targets into **existing-link** (`account_id` set) and **create-new** (`account_id`
   None).
2. Cluster create-new by `lf_company_id`; blank key ⇒ singleton cluster. First member of each
   cluster = **primary** (create-new); the rest = **secondaries**.
3. **Phase 1:** convert existing-links + all primaries (batched at `batch_size`). Capture each
   primary's new `AccountId` from its `LeadConvertResult`.
4. **Phase 2:** for each cluster with a successful primary, set every secondary's `account_id`
   to the primary's new `AccountId`; convert the secondaries (batched).
5. Merge Phase 1 + Phase 2 results; reconcile `converted` / `already_converted` / `failed` /
   `missing` across both (the existing reconciliation extended to two passes).

Because each cluster contributes exactly one primary to Phase 1 and its secondaries to Phase 2,
a cluster is never split mid-creation regardless of `batch_size`.

## 4. Error handling & edge cases

- **Kununu conflict** (Feature 1): lead skipped + reported for review, Account not updated, not
  converted this run. Blank-on-account identity is filled (not a conflict). Conflict on one
  identity while the other is blank still routes to review (differ on the present one).
- **Primary convert fails** (validation/duplicate rule): its secondaries have no Account to link
  to. They are reported as `failed` with a clear reason (`primary <leadId> did not create an
  account`) and NOT converted — so they surface loudly and retry next run, rather than falling
  back to create-new (which would just hit the duplicate rule again).
- **Blank `lf_company_id`** on a create-new lead ⇒ singleton (converts create-new as today).
- **Pool query / update failure** in `resolve_pool_links`: log and continue to convert
  (enrichment is best-effort; it must not block a gated conversion). Surface in the summary.
- **Pool link, all mapped values already match** ⇒ empty payload ⇒ no DML.
- **Idempotent re-run:** already-converted leads still short-circuit via the existing
  `alreadyConverted` path; overwrite-on-difference means a re-run writes nothing new.

## 5. Testing

- **Feature 1 — pool gate & write:** pool-vs-sales owner gate (sales-owned skipped);
  overwrite-on-difference (unchanged fields omitted, differing written, lacked-values never
  written); field map incl. `KN_Employee__c → KN_EmployeeCategory__c`; Bulk payload shape; pool
  query/update failure is non-fatal.
- **Feature 1 — conflict gate:** URL blank → fill; URL/UUID equal → no-op; URL differs →
  skip+report+no-update; UUID differs → skip+report; URL differs but UUID blank → still review;
  normalized-URL equality (trivial URL diffs are not conflicts). Fake sf client at the
  query/`update_bulk` boundary.
- **Feature 2 — clustering:** by `lf_company_id` (singletons untouched, blanks not clustered);
  two-phase sequencing (primary's new `AccountId` feeds secondaries); primary-failure ⇒
  secondaries reported failed, not converted; cluster never split across batches; merged count
  reconciliation. Fake ApexClient echoing per-phase results (extend existing fakes).
- **Stage 1:** `lf_company_id` and `kununu_profile_uuid` present in the Review sheet and
  round-trip through the approved CSV into `ConversionTarget`.

## 6. Open items (resolve in planning / implementation)

- **Verify `KN_EmployeeCategory__c` picklist labels** match the Lead `KN_Employee__c` bands (the
  14 German-format labels in `writeback._EMPLOYEE_BINS`); if they differ, add a Lead→Account
  label map. Restricted picklists reject unknown labels.
- **Confirm the Account duplicate rules' action** (block vs alert on insert) via a metadata
  retrieve — determines whether today's failure mode for #2 is a loud convert failure or a
  silent duplicate. The fix behaves the same either way.
- **Decide whether the pool update maps address to `Billing*`** or leaves address to the
  create-new path only (address is only conversion-required on create-new; a pool Account may
  already have a curated billing address — overwrite-on-difference still changes it).
- **Review-report channel for kununu conflicts:** extend `preflight_failures.csv` with a
  distinct `kununu_conflict` reason, or emit a separate `review_conflicts.csv`; both ride the
  existing Stage 2 artifact upload. Pick during implementation.
- **Reuse vs re-query the Lead fields** between preflight and `resolve_pool_links` (one query vs
  two); pick during implementation.
- **`KN_AssignedToPool__c`** (formula checkbox) as a secondary cross-check on pool membership —
  optional; `OwnerId` stays authoritative.
