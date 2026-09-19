# Stage 2 — Gated Lead Conversion

Scope: the conversion path — this package (`convert/converter.py`), `pipeline/stage2_convert.py`,
`cli.py stage2`, `.github/workflows/stage2.yml`, and `review/approved_*.csv`. Stage 2 runs on
**manual dispatch** OR **chained from a live Stage-1 run** (`workflow_call` from `stage1.yml`'s
`convert` job, since go-live 2026-07-18); no cron, no watermark. LIVE-PROVEN: 3-lead test
2026-07-10 (run 29126789599), 230-lead backlog 2026-07-17, first chained auto-run 2026-07-18
(run 29653343786, 3/3). The chained call checks out `ref: master` (NOT the triggering SHA) so it
sees the auto CSV committed mid-run, and reuses the `lead-automation-stage2` concurrency group
at the caller's job level (a called workflow's top-level `concurrency` is ignored).

## Flow

1. Reviewer fills `Approve?` in the QA artifact's Review sheet and commits the rows as
   `review/approved_<date>.csv`. `stage2` picks the newest by filename (`--approved` overrides).
   **Auto-approve mode (Wasil 2026-07-10; ACTIVE + narrowed to the live scope since go-live
   2026-07-18):** with config `pipeline.review_gate: false`, live Stage-1 runs emit + commit
   `review/approved_<UTCts>_auto.csv` themselves (`qa_artifact.write_auto_approved_csv`):
   exactly the rows passing the live-scope gate (`live_scope.py`: decision ∈
   `pipeline.live_scope.decisions` — currently `create_new` — AND kununu score <
   `max_kununu_score` 3.0; missing score fails closed) pre-approved, `skip`/`review` never
   included, write-back failures held back. Stage 2 is unchanged — preflight/pool-gate/
   clustering still run (see Safety model for the brakes).
2. `targets_from_approved` builds one `ConversionTarget(lead_id, account_id?, contact_id?)`
   per approved row (`Approve?` ∈ yes/y/true/x/1):
   - Rows with `decision == "skip"` (no LeadFeeder match) are dropped even if approved — a
     terminal skip never converts (Stage 1 excludes them from account/contact matching).
   - Account links whenever `account_match_id` is non-blank (reviewer validated it).
   - Contact links ONLY when `contact_reason` ∈ `{kununu_user_uuid, contact_email_in_account}`
     (identifier signals). Weaker reasons (`contact_email_global`, `linkedin`) deliberately
     create a fresh Contact under the linked Account.
   - Blanking `account_match_id`/`contact_match_id` in the CSV forces create-new.
   - Two new CSV columns (`lf_company_id`, `kununu_profile_uuid`) are captured for clustering
     and pool-account enrichment; `kununu_url` is also read for conflict detection.
3. run_preflight (convert/preflight.py) re-queries the approved leads' current state and drops
   any lead failing the "Conversion requirements" below — failures land in preflight_failures.csv
   (uploaded with the stage-2 log) and Stage2Result.preflight_failures; --no-preflight bypasses.
4. `resolve_pool_links` (convert/pool_enrich.py) reads its Accounts and Leads in ONE
   `SfClient.query_many` /composite call (2 → 1, 2026-07-25; deliberately NOT bundled with
   preflight's read — preflight decides which targets survive, and this step WRITES to the
   accounts it reads) and enriches linked Accounts owned by the pool
   (`Owner.Name='Rohdaten Pool'`, keyed by config `salesforce.rohdaten_pool_owner_id`). For each
   approved lead linking an Account it runs a two-part **kununu identity gate**, comparing the
   Account's stored identity to the values carried on the approved CSV: Account
   `KN_KununuProfileUuid__c` vs the lead's `kununu_profile_uuid`, and Account
   `KN_KununuLinkProfile__c` (the profile URL) vs the lead's `kununu_url` (normalized). Each
   comparison: blank → fill, equal → no-op, present-and-different → conflict (route to review,
   skip+report like preflight). Targets that pass the gate get their address/employee/region/VAT
   fields written from the lead's enrichment **where they differ** (overwrite-on-difference — note
   this is NOT fill-only-blank; the URL/UUID identity fields alone are fill-only-blank). Writes
   happen only on live runs; dry runs query but don't write. Targets failing the gate are dropped
   from conversion.
5. `convert_leads` (convert/converter.py) clusters create-new leads by `lf_company_id`: groups
   of leads with the same company ID are deduplicated into one primary (the first in sort order)
   that creates a new Account, and secondaries that link to the primary's new Account. If a
   primary fails conversion, its secondaries are reported as failed orphans (not converted).
   Conversion runs over the **SOAP Partner API `convertLead()`** (`SfClient.convert_lead` →
   `sf/soap.py`; anonymous Apex is impossible — see Gotchas) per batch (default **3**,
   `--batch-size`; **keep ≤3 live** — each conversion cascades Account/Contact triggers, flows
   and validation/duplicate rules synchronously in ONE transaction per SOAP call, and >5
   exhausted a per-transaction governor limit on the 2026-07-17 backlog run: exactly the
   first 5 of each 10-lead batch converted, the rest failed).
   Each LeadConvert carries accountId/contactId when linked, `doNotCreateOpportunity=true`,
   `overwriteLeadSource=false`, `sendNotificationEmail=false`, `ownerId` = config owner_id.
   Converted status is PINNED in config (`salesforce.converted_status: Qualified`) and trusted
   as-is — no per-run LeadStatus SOQL (API-call reduction 2026-07-18; a wrong pin fails loudly
   org-side as an invalid-status conversion error). Only a blank config value falls back to
   `LeadStatus WHERE IsConverted = true ORDER BY SortOrder NULLS LAST` (first label wins); no
   converted status resolvable fails every lead without calling SOAP.
6. Idempotency: already-converted inputs get an explicit `alreadyConverted` result row (so a
   re-run is a true no-op — counted `already_converted`, never `missing`). With preflight on
   (the default), `convert_leads` runs `preverified=True`: preflight's Lead query already
   selects `IsConverted` and drops converted leads, so the converter skips the
   `IsConverted = true` pre-query entirely (saved ~⌈T/3⌉ queries — 77 on the 230-lead backlog
   run); a lead converted in the preflight→SOAP window comes back as an already-converted SOAP
   error (`CANNOT_UPDATE_CONVERTED_LEAD`-style) and is mapped back to `alreadyConverted`
   (`converter._is_converted_race`). Under `--no-preflight` the pre-query still runs, but
   ONCE per `_run_convert` phase over the whole target list (chunked at
   `_CONVERTED_PROBE_CHUNK` = 500 Ids), not once per batch — at the live `--batch-size 3`
   that was ⌈N/3⌉ calls (API-call reduction 2026-07-25). `SfClient.convert_lead` returns one result
   row per conversion in request order, backfilling a nil leadId (structural rejections) from
   that order, and retries ONCE on an `INVALID_SESSION_ID` fault after re-fetching auth
   (`SfClient.auth_info`, cached from `sf org display --json`). `convert_leads` still reconciles
   returned leadIds against its input: any lead with NO result row is counted `missing`. Only
   `failed`/`missing` fail the job; `already_converted` does not.
7. **Post-convert email enrichment** (`converter.enrich_contact_email2`, called by `run_stage2`):
   leads converted in this run into a NEW Contact (no contact link — linked existing Contacts
   are never touched) whose Review-sheet row carried an `lf_contact_email` get that LF email
   written to Contact `KN_Email2__c` ("Email 2", org-verified 2026-07-10) — only when it differs
   from the Contact's primary Email (the task email mapped from the Lead) and `KN_Email2__c` is
   still blank. Best-effort: a failure logs but never fails the conversion run; counts land in
   `result["email2"]`. No-op under dry_run (there are no conversion results to enrich).

## Commands

```powershell
python -m pytest -q                                            # full suite covers stage 2
$env:PYTHONPATH = "src"; python -m leadauto stage2 --dry-run   # newest review/approved_*.csv
python -m leadauto stage2 --approved review/approved_X.csv --batch-size 1 --no-dry-run
```

## Safety model

- `dry_run` short-circuits in `convert_leads` before ANY org call; config has `dry_run: true`
  and the workflow input defaults to true.
- IDs from the approved CSV are validated to the 15/18-char alphanumeric shape in
  `targets_from_approved` (raises `ValueError` → CLI exits non-zero) and re-guarded to
  alphanumeric-only before embedding in SOQL or the SOAP envelope (one shared guard,
  `sf.client.guard_sf_id`), so a malformed or malicious cell can neither inject nor crash a batch.
- A contact link without an account link is dropped in `targets_from_approved` (LeadConvert
  requires `setAccountId` alongside `setContactId`; the lead converts create-new instead).
- Idempotent: already-converted leads become explicit `alreadyConverted` rows instead of
  failures, so re-running a batch (or an approved CSV where some rows already converted) is a
  no-op that exits **green** — counted `already_converted`, not `missing`/`failed`. With
  preflight on, preflight itself catches them (its query selects `IsConverted`) and the
  converter trusts it (`preverified=True`, no pre-query at all); the race window is covered
  by mapping the already-converted SOAP error to `alreadyConverted`. Under `--no-preflight`
  the converter's own `IsConverted = true` pre-query still runs — once per phase, not per batch.
- **No GitHub Environment gate** (resolved 2026-07-10): required-reviewers protection needs
  a paid plan on private repos (API returns 422 on this repo), so the `environment:`
  reference was REMOVED from `stage2.yml`. The conversion brakes: on **manual dispatch** the
  `dry_run` input defaults to true — live conversion takes a deliberate dispatch with dry_run
  unchecked. On the **chained path** (since go-live 2026-07-18) the brake sits upstream: the
  chain only fires from a live Stage-1 run (itself gated by Stage 1's dry_run input, default
  true — scheduled cron-job.org runs stay dry), and the auto CSV is already narrowed to the
  live scope (create_new + kununu score <3).
- Config `owner_id` = the **'Rohdaten Pool' user since 2026-07-18** (live scope is pool-quality
  leads; was Wasil's user for the 2026-07-10 test) — converted records are assigned to it at
  setOwnerId time; queue ownership ends at conversion.

## Gotchas

- **Anonymous-Apex conversion is impossible for the pipeline user** (root-caused 2026-07-10):
  his `Kununu - Base Profile` lacks **Author Apex** (revoked since May 2026; no perm set grants
  it), so execute-anonymous runs in RESTRICTED mode where the JSON class, JSONGenerator AND
  `Database.convertLead` itself throw `System.SecurityException` — bisect-proven with harmless
  snippets. He DOES hold Convert Leads (`KN_DataManager` perm set), which the SOAP Partner API
  honors — hence `sf/soap.py`. Extra trap: the CI CLI (2.140.6, Linux) mangles that runtime
  SecurityException into empty/unparseable `--json` output, which mimics a CLI flake (locally
  2.141.6 shows the real exception). `SfClient.apex_run` was DELETED 2026-09-19 (no caller); conversion must NEVER
  go back to Apex while the permission is absent.
- **Duplicate rules can block conversion** (Apex or SOAP — same engine underneath) — the
  `setBypass*` methods on `Database.LeadConvert` are internal-only, and
  `Database.DMLOptions.DuplicateRuleHeader.AllowSave` does not apply to conversion (only to
  plain DML). If a Block rule fires: fix the duplicate or downgrade the rule to Alert.
  Provenance + failed REST workaround: `docs/legacy/convert-leads/`.
- **Validation rules block conversion of sparse leads** — see "Conversion requirements" below
  (org-verified 2026-07-05). The May-2026 manual runs had to patch `Street = ','`,
  `KN_Employee__c = '11-50'`, `KN_NoOwnDBID__c = true` (`docs/legacy/convert-leads/convertLead.apex`).
- **SOAP LeadConvert element ORDER matters** (WSDL sequence: accountId?, contactId?,
  convertedStatus, doNotCreateOpportunity, leadId, overwriteLeadSource, ownerId?,
  sendNotificationEmail) — `build_convert_envelope` pins it and tests assert it; a shuffled
  envelope is rejected org-side. A SOAP Fault raises `SfError` (one INVALID_SESSION_ID retry);
  `convert_leads` reconciles returned leadIds against the batch and counts any absent lead as
  `missing` (logged at ERROR). The `stage2` CLI exits non-zero when `failed` or `missing` is
  non-zero, so CI fails loudly instead of reporting a false green.
- LeadConvert **re-parents the Lead's Tasks onto the Contact** (`Who.Type` flips) — the reason
  control-set joins had to go through `ConvertedContactId`.
- `Lead.ConvertedDate` is Date-only (no time component) — shadow-compare treats "same UTC day"
  as the match window.

## Conversion requirements (active validation rules, org-verified 2026-07-05)

Retrieved read-only via Tooling API (24 active rules on Lead/Account/Contact). Lead rules keyed
on `Status = 'Qualified'` OR `IsConverted = TRUE` fire DURING conversion (proven in May 2026);
leads sit unaffected until then. Population counts = all 25,040 unconverted queue-owned leads.

Fire at conversion (must be satisfied by write-back first):
- **`KN_DbIdValidation` — EXACTLY ONE of `KN_DBID__c` / `KN_NoOwnDBID__c=true`** (XOR: both set
  is also an error). 71% of leads currently violate it. Write-back now sets `KN_NoOwnDBID__c=true`
  whenever the Lead's `KN_DBID__c` is blank (closed 2026-07-05; `KN_DBID__c` itself is
  D&B-integration-owned and never written).
- **`KN_EmployeeCategoryMandatory` — `KN_Employee__c` required.** 97% currently blank; write-back
  fills it from LF `Employee_Count`, falling back to the claimer task's parsed company size
  (closed 2026-07-05; leads with neither source are caught by preflight).
- **`KN_LanguageCodemandatoryifqualified` — `KN_Language__c` required** (keyed on Qualified only).
  Only 16 leads blank — near-universally auto-populated; still needs a preflight check.
- **`KN_LeadValidatePrivateMail` — freemail `Email` (gmail/yahoo/outlook/hotmail/aol/icloud/
  protonmail/zoho/mail.com) BLOCKS when `Status != 'New'` unless `HasOptedOutOfEmail`.** ~7%
  (1,776) of leads are in this state — conversion-blocking; needs a policy (opt-out flag vs
  clear email vs route to review).
- **Account `KN_ConvertedLeadAddressValidation` — `BillingStreet` + `BillingCountry` required on
  accounts with `KN_ConvertedLeadCheck__c`** ⇒ Lead `Street` + `Country` must be populated for
  the create-new path (the legacy `Street=','` hack). 49% of leads lack Street or Country;
  write-back now fills the standard address (`Street`/`City`/`PostalCode`/`CountryCode`,
  fill-only-blank; closed 2026-07-05).

Fire on ANY Lead edit (constrain write-back itself, no status gate):
- `KN_ValidateDandBonlyNumberLead` — `KN_DBID__c` digits-only (`KN_DBID__c` is never written by
  the pipeline; the rule constrains D&B/human edits and is mirrored in preflight's
  `dbid_numeric` check).
- `KN_Addresstoolong` — `Street` ≤ 100 chars.
- `KN_Checklengthpostalcode` — `PostalCode` length 5 if `Country="Germany"`, 4 if
  `"Switzerland"` (English country names; Austria uncovered; E-commerce LeadSource exempt).
- `KN_ValidatePhone` (+ Contact `KN_ContactValidPhoneNumber`) — `Phone` must match
  `+<digits only>`, no spaces (e.g. `+4940419131`); normalize before ever writing phone.
- `KN_ProvideReasonForNoDemand` — No-Demand leads need a reason; 0 currently violate, so
  write-back updates are safe.

NOT enforced by any rule (layout-only, does not block API conversion): `KN_Region__c`, `City`,
`FirstName`/`Salutation`, email/phone presence. `LastName`/`Company` are schema-required.
The Lead→Contact field mapping works (incl. the `KN_LinkedInProfileLink__c` question —
confirmed by Wasil 2026-07-10; note write-back now only ever puts *contact* LinkedIn URLs
there, per the db76d3e spec).
Stage-1 write-back now fills KN_NoOwnDBID__c / address (CountryCode) / KN_Employee__c fallback /
FirstName / Phone (fill-only-blank; spec docs/superpowers/specs/2026-07-05-writeback-gaps-and-stage2-preflight-design.md),
and the QA Review sheet previews these checks in convert_blockers.

## Live validation history (prod only, no sandbox)

The create-new path is live-proven at three scales: 3-lead test 2026-07-10 (run 29126789599,
incl. green idempotency re-run), 230-lead backlog 2026-07-17 (runs 29578167853 + 29579163559 —
the run that established the batch-size ≤3 governor ceiling), and the first chained auto-run
2026-07-18 (run 29653343786, 3/3; org-verified: Qualified, owner Rohdaten Pool, no Opportunity,
tasks re-parented, addresses/Industry/employee bins on the new Accounts). The **link-existing
path has NOT yet converted live** (only 27 backlog link rows are staged; validate with
`--batch-size 1` and verify owner/status/no-Opportunity/task re-parenting before trusting it
at scale) — pool-account enrichment ran live only as part of dry-run queries so far.
