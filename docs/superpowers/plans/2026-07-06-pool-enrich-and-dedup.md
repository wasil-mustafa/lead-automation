# Pool-Account Enrichment + In-Batch Same-Company Dedup — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** In Stage 2, enrich a linked Account with the lead's data when (and only when) it is owned by the `Rohdaten Pool` user, gated by a kununu identity check; and auto-cluster same-company create-new leads so one Account is created with many Contacts instead of duplicates.

**Architecture:** Two pipeline-derived keys (`lf_company_id`, `kununu_profile_uuid`) are added to the Stage 1 QA Review sheet so they flow through the approved CSV into `ConversionTarget`. Stage 2 gains a read-then-write `resolve_pool_links` step (after preflight, before convert) that overwrites-on-difference the enrichment fields of pool-owned linked Accounts and routes kununu-conflict leads to review. `convert_leads` becomes two-phase: cluster create-new leads by `lf_company_id`, convert one primary per cluster, then convert the secondaries linked to the primary's new Account.

**Tech Stack:** Python 3.12, pytest (pythonpath=src via pyproject.toml), the `sf` CLI wrapper in `src/leadauto/sf/client.py`, anonymous Apex string generation.

## Global Constraints

- **No Salesforce metadata changes.** No new fields; the lead's profile UUID travels via the approved CSV, never a Lead write.
- **Writes only when not `dry_run`.** `resolve_pool_links` may query in dry-run but must not call `update_bulk`.
- **`sf` SOQL uses single quotes only** (`SfClient.query` rejects double quotes).
- **Org alias is `headless`**, API v66.0. Pool owner Id `005Mz00000Xw0jjIAB` (`Owner.Name = 'Rohdaten Pool'`).
- **Field API names verified via describe on `headless` 2026-07-06** (spec §2 table). Account uses `KN_EmployeeCategory__c` (not `KN_Employee__c`) and `KN_KununuProfileClaimedat__c` (lowercase `at`).
- **Run the full suite green after every task:** `python -m pytest -q`.

## File Structure

- `config/config.yaml` — add `salesforce.rohdaten_pool_owner_id`.
- `src/leadauto/config.py` — `Settings.rohdaten_pool_owner_id` property.
- `src/leadauto/sf/soql.py` — new builders `accounts_pool_fields`, `leads_pool_enrich`.
- `src/leadauto/qa_artifact.py` — add `lf_company_id`, `kununu_profile_uuid` to `REVIEW_COLUMNS` / `_review_row`.
- `src/leadauto/convert/converter.py` — `ConversionTarget` new fields; `convert_leads` two-phase clustering; `_cluster_by_company`, `_run_convert` helpers.
- `src/leadauto/convert/pool_enrich.py` — **new**: `account_update` (pure) + `resolve_pool_links` (orchestration).
- `src/leadauto/pipeline/stage2_convert.py` — parse new CSV columns in `targets_from_approved`; call `resolve_pool_links` in `run_stage2`.
- Tests: `tests/test_soql.py`, `tests/test_qa_artifact.py`, `tests/test_convert.py`, `tests/test_pipeline.py`, and **new** `tests/test_pool_enrich.py`.

---

### Task 1: Config — `rohdaten_pool_owner_id`

**Files:**
- Modify: `config/config.yaml:4-11` (salesforce block)
- Modify: `src/leadauto/config.py:62-64` (after `owner_id`)
- Test: `tests/test_config.py` (create if absent) or extend existing config test

**Interfaces:**
- Produces: `Settings.rohdaten_pool_owner_id -> str | None`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_config.py` (create the file if it does not exist):

```python
from leadauto.config import Settings


def test_rohdaten_pool_owner_id_from_config():
    s = Settings(raw={"salesforce": {"rohdaten_pool_owner_id": "005Mz00000Xw0jjIAB"}},
                 leadfeeder_api_key=None, sf_auth_url=None)
    assert s.rohdaten_pool_owner_id == "005Mz00000Xw0jjIAB"


def test_rohdaten_pool_owner_id_absent_is_none():
    s = Settings(raw={"salesforce": {}}, leadfeeder_api_key=None, sf_auth_url=None)
    assert s.rohdaten_pool_owner_id is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_config.py -v`
Expected: FAIL with `AttributeError: 'Settings' object has no attribute 'rohdaten_pool_owner_id'`

- [ ] **Step 3: Add the property**

In `src/leadauto/config.py`, directly after the `owner_id` property (line ~64):

```python
    @property
    def rohdaten_pool_owner_id(self) -> str | None:
        """OwnerId of the 'Rohdaten Pool' user; linked Accounts with this owner are
        the only ones the Stage 2 pool-enrichment step may update."""
        return self.raw["salesforce"].get("rohdaten_pool_owner_id")
```

- [ ] **Step 4: Add the config value**

In `config/config.yaml`, inside the `salesforce:` block after `owner_id`:

```yaml
  # 'Rohdaten Pool' user: linked Accounts owned by this user are raw data and get
  # overwrite-enriched at conversion time (Owner.Name = 'Rohdaten Pool').
  rohdaten_pool_owner_id: "005Mz00000Xw0jjIAB"
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_config.py -v`
Expected: PASS (both tests)

- [ ] **Step 6: Commit**

```bash
git add src/leadauto/config.py config/config.yaml tests/test_config.py
git commit -m "feat(config): add rohdaten_pool_owner_id"
```

---

### Task 2: SOQL builders for the pool step

**Files:**
- Modify: `src/leadauto/sf/soql.py` (add two builders + two field constants near line 96)
- Test: `tests/test_soql.py`

**Interfaces:**
- Produces:
  - `accounts_pool_fields(ids: Sequence[str]) -> str` — selects `Id, OwnerId`, kununu identity, and the mapped Account fields.
  - `leads_pool_enrich(ids: Sequence[str]) -> str` — selects the Lead fields used to build the Account overwrite payload.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_soql.py`:

```python
def test_accounts_pool_fields_selects_owner_and_identity():
    q = soql.accounts_pool_fields(["001000000000001"])
    assert "OwnerId" in q
    assert "KN_KununuProfileUuid__c" in q and "KN_KununuLinkProfile__c" in q
    assert "KN_EmployeeCategory__c" in q          # Account employee field name
    assert "BillingStreet" in q
    assert "'001000000000001'" in q and "FROM Account" in q


def test_leads_pool_enrich_selects_mapped_lead_fields():
    q = soql.leads_pool_enrich(["00Q000000000001"])
    assert "KN_KununuProfileName__c" in q and "KN_VATNumber__c" in q
    assert "KN_Employee__c" in q and "CountryCode" in q
    assert "FROM Lead" in q


def test_pool_builders_reject_empty():
    import pytest
    with pytest.raises(ValueError):
        soql.accounts_pool_fields([])
    with pytest.raises(ValueError):
        soql.leads_pool_enrich([])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_soql.py -k pool -v`
Expected: FAIL with `AttributeError: module ... has no attribute 'accounts_pool_fields'`

- [ ] **Step 3: Implement the builders**

In `src/leadauto/sf/soql.py`, after the match-field constants (line ~104):

```python
# --- Stage 2 pool-account enrichment ----------------------------------------
_ACCOUNT_POOL_FIELDS = (
    "Id, OwnerId, KN_KununuLinkProfile__c, KN_KununuProfileUuid__c, "
    "KN_KununuProfileName__c, KN_Region__c, KN_VATNumber__c, KN_EmployeeCategory__c, "
    "KN_KununuProfileClaimed__c, KN_KununuProfileClaimedat__c, "
    "BillingStreet, BillingCity, BillingPostalCode, BillingCountryCode"
)
_LEAD_POOL_FIELDS = (
    "Id, KN_KununuProfileName__c, KN_Region__c, KN_VATNumber__c, KN_Employee__c, "
    "KN_KununuProfileClaimed__c, KN_KununuProfileClaimedAt__c, "
    "Street, City, PostalCode, CountryCode"
)


def accounts_pool_fields(ids: Sequence[str]) -> str:
    """Owner + kununu identity + mapped enrichment fields for candidate linked Accounts."""
    if not ids:
        raise ValueError("accounts_pool_fields requires at least one Id")
    return f"SELECT {_ACCOUNT_POOL_FIELDS} FROM Account WHERE Id IN ({_in_list(ids)})"


def leads_pool_enrich(ids: Sequence[str]) -> str:
    """Lead fields used to build the pool-Account overwrite payload."""
    if not ids:
        raise ValueError("leads_pool_enrich requires at least one Id")
    return f"SELECT {_LEAD_POOL_FIELDS} FROM Lead WHERE Id IN ({_in_list(ids)})"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_soql.py -k pool -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add src/leadauto/sf/soql.py tests/test_soql.py
git commit -m "feat(soql): pool-account + lead-enrichment query builders"
```

---

### Task 3: Stage 1 — expose `lf_company_id` and `kununu_profile_uuid` in the Review sheet

**Files:**
- Modify: `src/leadauto/qa_artifact.py:21-32` (REVIEW_COLUMNS), `:79-98` (_review_row)
- Test: `tests/test_qa_artifact.py`

**Interfaces:**
- Produces: two new Review columns (`lf_company_id`, `kununu_profile_uuid`) present in `REVIEW_COLUMNS` and populated by `_review_row`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_qa_artifact.py`:

```python
def test_review_sheet_carries_cluster_and_conflict_keys():
    from leadauto.extract.models import ExtractedLead
    from leadauto.leadfeeder.enrich import EnrichedLead
    from leadauto.match import MatchResult
    from leadauto.qa_artifact import REVIEW_COLUMNS, build_workbook

    assert "lf_company_id" in REVIEW_COLUMNS and "kununu_profile_uuid" in REVIEW_COLUMNS
    e = ExtractedLead(lead_id="00Q1", task_id="00T1", task_type="claimer",
                      company_name="Acme GmbH", kununu_profile_uuid="puid-123")
    enr = EnrichedLead(extracted=e, matched=True, lf_company_id="LF-999")
    res = MatchResult(enriched=enr, account=None, contact=None, decision="create_new")
    ws = build_workbook([res], [], meta={})["Review"]
    headers = [c.value for c in next(ws.iter_rows(max_row=1))]
    idx = {h: i for i, h in enumerate(headers)}
    row = list(ws.iter_rows(min_row=2, values_only=True))[0]
    assert row[idx["lf_company_id"]] == "LF-999"
    assert row[idx["kununu_profile_uuid"]] == "puid-123"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_qa_artifact.py -k cluster_and_conflict -v`
Expected: FAIL (assert `lf_company_id in REVIEW_COLUMNS` is False)

- [ ] **Step 3: Add the columns**

In `src/leadauto/qa_artifact.py`, extend `REVIEW_COLUMNS` — insert after the `lf_website` entry (line 26), keeping the LF block together:

```python
    "lf_city", "lf_employees", "lf_industry", "lf_website", "lf_company_id",
```

and add a new key just before `"match_rule"` is fine too; then in `_review_row` add the two values (after the `lf_website` mapping, line ~88):

```python
        "lf_company_id": enr.lf_company_id,
        "kununu_profile_uuid": e.kununu_profile_uuid,
```

Add `"kununu_profile_uuid"` to `REVIEW_COLUMNS` next to `kununu_url` (line 23):

```python
    "company", "kununu_url", "kununu_profile_uuid", "region",
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_qa_artifact.py -v`
Expected: PASS (new test + existing `test_review_header_includes_match_and_gate_columns` still passes because it compares against the updated `REVIEW_COLUMNS`)

- [ ] **Step 5: Commit**

```bash
git add src/leadauto/qa_artifact.py tests/test_qa_artifact.py
git commit -m "feat(qa): expose lf_company_id + kununu_profile_uuid on the Review sheet"
```

---

### Task 4: `ConversionTarget` fields + parse them from the approved CSV

**Files:**
- Modify: `src/leadauto/convert/converter.py:54-59` (ConversionTarget)
- Modify: `src/leadauto/pipeline/stage2_convert.py:56-93` (targets_from_approved)
- Test: `tests/test_pipeline.py`

**Interfaces:**
- Produces: `ConversionTarget(lead_id, account_id=None, contact_id=None, lf_company_id=None, kununu_profile_uuid=None, kununu_url=None)`; `targets_from_approved` populates the three new fields from CSV columns `lf_company_id`, `kununu_profile_uuid`, `kununu_url`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_pipeline.py` (near the other `targets_from_approved` tests):

```python
def test_targets_from_approved_carries_pool_and_cluster_keys():
    rows = [{"Approve?": "yes", "lead_id": LEAD1, "account_match_id": ACCT1,
             "contact_reason": "kununu_user_uuid", "contact_match_id": "",
             "lf_company_id": "LF-1", "kununu_profile_uuid": "puid-1",
             "kununu_url": "https://www.kununu.com/de/acme"}]
    t = targets_from_approved(rows, SETTINGS)[0]
    assert t.lf_company_id == "LF-1"
    assert t.kununu_profile_uuid == "puid-1"
    assert t.kununu_url == "https://www.kununu.com/de/acme"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_pipeline.py -k pool_and_cluster -v`
Expected: FAIL with `TypeError: __init__() got an unexpected keyword` or `AttributeError: 'ConversionTarget' object has no attribute 'lf_company_id'`

- [ ] **Step 3: Extend the dataclass**

In `src/leadauto/convert/converter.py`, replace the `ConversionTarget` dataclass body:

```python
@dataclass
class ConversionTarget:
    """One Lead to convert; account_id/contact_id link to existing records when present.

    lf_company_id clusters same-company create-new leads; kununu_url/kununu_profile_uuid
    feed the Stage 2 pool-account conflict gate (see convert/pool_enrich.py)."""
    lead_id: str
    account_id: str | None = None
    contact_id: str | None = None
    lf_company_id: str | None = None
    kununu_profile_uuid: str | None = None
    kununu_url: str | None = None
```

- [ ] **Step 4: Populate them in `targets_from_approved`**

In `src/leadauto/pipeline/stage2_convert.py`, inside the row loop, replace the final `targets.append(...)` line with:

```python
        targets.append(ConversionTarget(
            lead_id, account_id=account_id, contact_id=contact_id,
            lf_company_id=(row.get("lf_company_id") or "").strip() or None,
            kununu_profile_uuid=(row.get("kununu_profile_uuid") or "").strip() or None,
            kununu_url=(row.get("kununu_url") or "").strip() or None,
        ))
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_pipeline.py -k "pool_and_cluster or targets_from_approved or contact_link" -v`
Expected: PASS (new test + existing `targets_from_approved` tests unchanged)

- [ ] **Step 6: Commit**

```bash
git add src/leadauto/convert/converter.py src/leadauto/pipeline/stage2_convert.py tests/test_pipeline.py
git commit -m "feat(stage2): carry lf_company_id + kununu identity onto ConversionTarget"
```

---

### Task 5: Pure pool-enrichment logic — `account_update`

**Files:**
- Create: `src/leadauto/convert/pool_enrich.py`
- Test: `tests/test_pool_enrich.py`

**Interfaces:**
- Produces: `account_update(account: dict, lead: dict, *, kununu_url: str | None, kununu_profile_uuid: str | None) -> tuple[str | None, dict]`. Returns `(conflict_reason, {})` on a kununu identity conflict; otherwise `(None, payload)` where `payload` always contains `Id` plus any Account fields to write.
- Consumes: `textnorm.normalize_url`.

> **Verify before implementing:** the Account picklist `KN_EmployeeCategory__c` must accept the Lead `KN_Employee__c` band labels. Run:
> `sf data query --query "SELECT QualifiedApiName FROM PicklistValueInfo WHERE EntityParticleId IN (SELECT Id FROM EntityParticle WHERE QualifiedApiName='KN_EmployeeCategory__c' AND EntityDefinition.QualifiedApiName='Account')" --target-org headless --result-format json`
> (If PicklistValueInfo is unavailable, use `sf sobject describe --sobject Account` and read the field's picklistValues.) If the labels differ from `writeback._EMPLOYEE_BINS`, add a label map in this module before shipping — otherwise the Bulk update rejects those rows.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_pool_enrich.py`:

```python
from leadauto.convert.pool_enrich import account_update


def _acct(**over):
    base = {"Id": "001A", "KN_KununuLinkProfile__c": None, "KN_KununuProfileUuid__c": None,
            "KN_KununuProfileName__c": None, "KN_Region__c": None, "KN_VATNumber__c": None,
            "KN_EmployeeCategory__c": None, "KN_KununuProfileClaimed__c": None,
            "KN_KununuProfileClaimedat__c": None, "BillingStreet": None,
            "BillingCity": None, "BillingPostalCode": None, "BillingCountryCode": None}
    base.update(over)
    return base


def test_blank_account_identity_is_filled():
    reason, payload = account_update(
        _acct(), {"KN_VATNumber__c": "DE123"},
        kununu_url="https://www.kununu.com/de/acme", kununu_profile_uuid="puid-1")
    assert reason is None
    assert payload["KN_KununuLinkProfile__c"] == "https://www.kununu.com/de/acme"
    assert payload["KN_KununuProfileUuid__c"] == "puid-1"
    assert payload["KN_VATNumber__c"] == "DE123"


def test_matching_identity_is_noop():
    acct = _acct(KN_KununuProfileUuid__c="puid-1",
                 KN_KununuLinkProfile__c="https://www.kununu.com/de/acme")
    reason, payload = account_update(acct, {}, kununu_url="https://www.kununu.com/de/acme",
                                     kununu_profile_uuid="puid-1")
    assert reason is None
    assert "KN_KununuProfileUuid__c" not in payload and "KN_KununuLinkProfile__c" not in payload


def test_differing_uuid_is_conflict():
    acct = _acct(KN_KununuProfileUuid__c="puid-OTHER")
    reason, payload = account_update(acct, {}, kununu_url=None, kununu_profile_uuid="puid-1")
    assert reason == "kununu_uuid_conflict" and payload == {}


def test_differing_url_is_conflict():
    acct = _acct(KN_KununuLinkProfile__c="https://www.kununu.com/de/other")
    reason, _ = account_update(acct, {}, kununu_url="https://www.kununu.com/de/acme",
                               kununu_profile_uuid=None)
    assert reason == "kununu_url_conflict"


def test_url_conflict_when_uuid_blank():
    acct = _acct(KN_KununuLinkProfile__c="https://www.kununu.com/de/other")
    reason, _ = account_update(acct, {}, kununu_url="https://www.kununu.com/de/acme",
                               kununu_profile_uuid="puid-1")
    assert reason == "kununu_url_conflict"


def test_overwrite_on_difference_only():
    acct = _acct(KN_VATNumber__c="OLD", KN_Region__c="Germany")
    reason, payload = account_update(
        acct, {"KN_VATNumber__c": "NEW", "KN_Region__c": "Germany", "KN_Employee__c": "11-50"},
        kununu_url=None, kununu_profile_uuid=None)
    assert reason is None
    assert payload["KN_VATNumber__c"] == "NEW"          # differs -> written
    assert "KN_Region__c" not in payload                # equal -> skipped
    assert payload["KN_EmployeeCategory__c"] == "11-50"  # Lead KN_Employee__c -> Account field


def test_lead_blank_never_written():
    reason, payload = account_update(_acct(KN_VATNumber__c="OLD"), {"KN_VATNumber__c": ""},
                                     kununu_url=None, kununu_profile_uuid=None)
    assert reason is None and "KN_VATNumber__c" not in payload
    assert list(payload) == ["Id"]                       # nothing to write but Id
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_pool_enrich.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'leadauto.convert.pool_enrich'`

- [ ] **Step 3: Implement `account_update`**

Create `src/leadauto/convert/pool_enrich.py`:

```python
"""Stage 2 pool-Account enrichment (spec: docs/superpowers/specs/
2026-07-06-pool-account-enrichment-and-batch-dedup-design.md).

`account_update` is pure: given a linked Account row, the lead's enrichment row, and the
lead's kununu identity (carried from the approved CSV), it returns either a conflict reason
(route the lead to review) or the Account overwrite payload. Kununu profile URL/UUID are a
match-validity gate: blank -> fill, equal -> no-op, present-and-different -> conflict.
`resolve_pool_links` (added in Task 6) does the I/O.
"""
from __future__ import annotations

from ..textnorm import normalize_url

# (lead_field, account_field) — overwrite the Account field when the lead value differs.
# Account names verified via describe on headless 2026-07-06 (KN_EmployeeCategory__c, and
# KN_KununuProfileClaimedat__c with a lowercase 'at').
_OVERWRITE_MAP = [
    ("KN_KununuProfileName__c", "KN_KununuProfileName__c"),
    ("KN_Region__c", "KN_Region__c"),
    ("KN_VATNumber__c", "KN_VATNumber__c"),
    ("KN_Employee__c", "KN_EmployeeCategory__c"),
    ("KN_KununuProfileClaimed__c", "KN_KununuProfileClaimed__c"),
    ("KN_KununuProfileClaimedAt__c", "KN_KununuProfileClaimedat__c"),
    ("Street", "BillingStreet"),
    ("City", "BillingCity"),
    ("PostalCode", "BillingPostalCode"),
    ("CountryCode", "BillingCountryCode"),
]


def _norm(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).strip()


def account_update(account: dict, lead: dict, *, kununu_url, kununu_profile_uuid):
    """(conflict_reason, payload). conflict_reason set => route lead to review (payload {}).
    Otherwise payload = {'Id': ...} plus every Account field to overwrite."""
    acct_url = normalize_url(account.get("KN_KununuLinkProfile__c"))
    lead_url = normalize_url(kununu_url)
    if lead_url and acct_url and lead_url != acct_url:
        return "kununu_url_conflict", {}
    acct_uuid = _norm(account.get("KN_KununuProfileUuid__c"))
    lead_uuid = _norm(kununu_profile_uuid)
    if lead_uuid and acct_uuid and lead_uuid != acct_uuid:
        return "kununu_uuid_conflict", {}

    payload: dict = {"Id": account["Id"]}
    if lead_url and not acct_url:                 # identity: fill only when blank
        payload["KN_KununuLinkProfile__c"] = kununu_url
    if lead_uuid and not acct_uuid:
        payload["KN_KununuProfileUuid__c"] = lead_uuid
    for lead_field, acct_field in _OVERWRITE_MAP:  # firmographic/address: overwrite on diff
        val = lead.get(lead_field)
        if _norm(val) and _norm(val) != _norm(account.get(acct_field)):
            payload[acct_field] = val
    return None, payload
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_pool_enrich.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add src/leadauto/convert/pool_enrich.py tests/test_pool_enrich.py
git commit -m "feat(convert): pure pool-account overwrite + kununu conflict logic"
```

---

### Task 6: Orchestration — `resolve_pool_links`

**Files:**
- Modify: `src/leadauto/convert/pool_enrich.py` (add `resolve_pool_links`)
- Test: `tests/test_pool_enrich.py`

**Interfaces:**
- Consumes: `soql.accounts_pool_fields`, `soql.leads_pool_enrich`, `client.query`, `client.update_bulk`, `account_update`.
- Produces: `resolve_pool_links(client, link_targets, settings, *, dry_run=False, logger=None) -> tuple[list[ConversionTarget], list[dict]]` — `(surviving_targets, review_failures)`. `review_failures` rows use the preflight CSV keys (`lead_id`, `check`, `value`, `detail`). Non-pool and missing-account links pass through unchanged. Kununu-conflict leads are dropped from `surviving_targets` and returned as `review_failures`. Pool Account updates are pushed via `update_bulk` only when `not dry_run`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_pool_enrich.py`:

```python
from leadauto.convert.converter import ConversionTarget
from leadauto.convert.pool_enrich import resolve_pool_links

POOL = "005POOL"


class _Settings:
    rohdaten_pool_owner_id = POOL


class _FakeSf:
    def __init__(self, accounts, leads):
        self._accounts = accounts
        self._leads = leads
        self.updated = None

    def query(self, soql):
        return self._accounts if "FROM Account" in soql else self._leads

    def update_bulk(self, sobject, records):
        self.updated = (sobject, records)
        return {"processed": len(records), "failed": 0, "failures": [], "confirmed": True}


def test_non_pool_account_passes_through_untouched():
    sf = _FakeSf([{"Id": "001A", "OwnerId": "005SALES"}], [{"Id": "00Q1"}])
    targets = [ConversionTarget("00Q1", account_id="001A")]
    surviving, review = resolve_pool_links(sf, targets, _Settings(), dry_run=False)
    assert [t.lead_id for t in surviving] == ["00Q1"] and review == []
    assert sf.updated is None


def test_pool_account_gets_updated_and_lead_survives():
    sf = _FakeSf(
        [{"Id": "001A", "OwnerId": POOL, "KN_VATNumber__c": None,
          "KN_KununuLinkProfile__c": None, "KN_KununuProfileUuid__c": None}],
        [{"Id": "00Q1", "KN_VATNumber__c": "DE9"}])
    targets = [ConversionTarget("00Q1", account_id="001A", kununu_profile_uuid="puid-1")]
    surviving, review = resolve_pool_links(sf, targets, _Settings(), dry_run=False)
    assert [t.lead_id for t in surviving] == ["00Q1"] and review == []
    sobject, records = sf.updated
    assert sobject == "Account" and records[0]["Id"] == "001A"
    assert records[0]["KN_VATNumber__c"] == "DE9"
    assert records[0]["KN_KununuProfileUuid__c"] == "puid-1"


def test_kununu_conflict_routes_to_review_and_skips_update():
    sf = _FakeSf(
        [{"Id": "001A", "OwnerId": POOL, "KN_KununuProfileUuid__c": "puid-OTHER"}],
        [{"Id": "00Q1"}])
    targets = [ConversionTarget("00Q1", account_id="001A", kununu_profile_uuid="puid-1")]
    surviving, review = resolve_pool_links(sf, targets, _Settings(), dry_run=False)
    assert surviving == []
    assert review[0]["lead_id"] == "00Q1" and review[0]["check"] == "kununu_uuid_conflict"
    assert sf.updated is None      # conflicted lead's account is not written


def test_dry_run_queries_but_does_not_write():
    sf = _FakeSf(
        [{"Id": "001A", "OwnerId": POOL, "KN_VATNumber__c": None}],
        [{"Id": "00Q1", "KN_VATNumber__c": "DE9"}])
    targets = [ConversionTarget("00Q1", account_id="001A")]
    surviving, review = resolve_pool_links(sf, targets, _Settings(), dry_run=True)
    assert [t.lead_id for t in surviving] == ["00Q1"]
    assert sf.updated is None      # no write in dry-run
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_pool_enrich.py -k "pool_account or conflict_routes or non_pool or dry_run" -v`
Expected: FAIL with `ImportError: cannot import name 'resolve_pool_links'`

- [ ] **Step 3: Implement `resolve_pool_links`**

Append to `src/leadauto/convert/pool_enrich.py` (add imports at top: `from ..logging import get_logger`):

```python
def resolve_pool_links(client, link_targets, settings, *, dry_run=False, logger=None):
    """Enrich pool-owned linked Accounts; route kununu-conflict leads to review.

    Returns (surviving_targets, review_failures). Non-pool / missing-account links pass
    through unchanged. Writes to Salesforce only when not dry_run.
    """
    log = logger or get_logger("convert.pool_enrich")
    pool_owner = settings.rohdaten_pool_owner_id
    linked = [t for t in link_targets if t.account_id]
    if not linked or not pool_owner:
        return list(link_targets), []

    from ..sf import soql
    accounts = {a["Id"]: a for a in client.query(
        soql.accounts_pool_fields(sorted({t.account_id for t in linked})))}
    leads = {r["Id"]: r for r in client.query(
        soql.leads_pool_enrich(sorted({t.lead_id for t in linked})))}

    surviving, review, updates = [], [], []
    for t in link_targets:
        acct = accounts.get(t.account_id) if t.account_id else None
        if acct is None or _norm(acct.get("OwnerId")) != pool_owner:
            surviving.append(t)                       # not pool-owned -> leave alone
            continue
        reason, payload = account_update(
            acct, leads.get(t.lead_id, {}),
            kununu_url=t.kununu_url, kununu_profile_uuid=t.kununu_profile_uuid)
        if reason:
            review.append({"lead_id": t.lead_id, "check": reason, "value": t.account_id or "",
                           "detail": "kununu identity differs from the linked pool account; "
                                     "needs review"})
            continue                                  # do NOT convert this lead
        surviving.append(t)
        if len(payload) > 1:                          # more than just Id
            updates.append(payload)

    if updates and not dry_run:
        try:
            res = client.update_bulk("Account", updates)
            log.info("Pool-account enrichment: %d updated, %d failed",
                     res["processed"], res["failed"])
        except Exception as exc:                      # best-effort; must not block convert
            log.error("Pool-account enrichment failed (continuing to convert): %s", exc)
    elif updates:
        log.info("[dry-run] would update %d pool account(s)", len(updates))
    log.info("Pool links: %d converting, %d routed to review", len(surviving), len(review))
    return surviving, review
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_pool_enrich.py -v`
Expected: PASS (all tests)

- [ ] **Step 5: Commit**

```bash
git add src/leadauto/convert/pool_enrich.py tests/test_pool_enrich.py
git commit -m "feat(convert): resolve_pool_links — enrich pool accounts, gate kununu conflicts"
```

---

### Task 7: Wire `resolve_pool_links` into `run_stage2`

**Files:**
- Modify: `src/leadauto/pipeline/stage2_convert.py:96-120` (run_stage2)
- Test: `tests/test_pipeline.py`

**Interfaces:**
- Consumes: `resolve_pool_links` (Task 6), `write_failures_csv` (existing).
- Produces: `run_stage2` runs the pool step after preflight; kununu-conflict leads are excluded from conversion and merged into the reported failures CSV.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_pipeline.py`:

```python
def test_run_stage2_routes_kununu_conflict_to_failures(tmp_path, monkeypatch):
    from leadauto.pipeline import stage2_convert

    def fake_resolve(client, link_targets, settings, *, dry_run=False, logger=None):
        # simulate: the single link is a kununu conflict -> dropped + reported
        return [], [{"lead_id": LEAD1, "check": "kununu_uuid_conflict",
                     "value": ACCT1, "detail": "conflict"}]

    monkeypatch.setattr(stage2_convert, "resolve_pool_links", fake_resolve)
    p = tmp_path / "approved.csv"
    _write_approved(p, [{"lead_id": LEAD1, "account_match_id": ACCT1,
                         "contact_score": "1.0", "Approve?": "yes"}])
    client = FakeApexClient()
    res = run_stage2(client, SETTINGS, p, dry_run=False, preflight=False,
                     failures_csv=tmp_path / "preflight_failures.csv")
    assert client.apex_calls == []                 # conflicted lead never converted
    assert any(f["check"] == "kununu_uuid_conflict" for f in res.preflight_failures)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_pipeline.py -k kununu_conflict_to_failures -v`
Expected: FAIL with `AttributeError: module 'leadauto.pipeline.stage2_convert' has no attribute 'resolve_pool_links'`

- [ ] **Step 3: Wire it in**

In `src/leadauto/pipeline/stage2_convert.py`: add the import near the top (with the other convert imports):

```python
from ..convert.pool_enrich import resolve_pool_links
```

Then in `run_stage2`, immediately after the preflight block (after `write_failures_csv` is computed from preflight, before the `log.info("Stage 2: ...")` line), insert:

```python
    # Pool-account enrichment + kununu conflict gate (link targets only). Writes only on
    # live runs; kununu conflicts are dropped from conversion and reported like preflight.
    link_targets = [t for t in targets if t.account_id]
    other_targets = [t for t in targets if not t.account_id]
    kept_links, review_failures = resolve_pool_links(
        sf_client, link_targets, settings, dry_run=dry_run, logger=log)
    targets = kept_links + other_targets
    if review_failures:
        pf_failures.extend(review_failures)
        write_failures_csv(pf_failures, failures_csv)
        log.warning("Routed %d lead(s) to review (kununu conflict)", len(review_failures))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_pipeline.py -v`
Expected: PASS (new test + existing Stage 2 tests; the pass-through case leaves non-pool links untouched)

- [ ] **Step 5: Commit**

```bash
git add src/leadauto/pipeline/stage2_convert.py tests/test_pipeline.py
git commit -m "feat(stage2): run pool-account enrichment + conflict gate before convert"
```

---

### Task 8: Two-phase clustering in `convert_leads`

**Files:**
- Modify: `src/leadauto/convert/converter.py:175-217` (convert_leads) + add helpers
- Test: `tests/test_convert.py`

**Interfaces:**
- Consumes: `build_convert_apex`, `parse_results`, `_short_id` (existing), `dataclasses.replace`.
- Produces: `convert_leads` unchanged signature/return keys; internally clusters create-new targets by `lf_company_id` and converts primaries then secondaries. New helpers `_cluster_by_company(targets) -> list[list[ConversionTarget]]` and `_run_convert(client, targets, settings, batch_size, log, phase_label="") -> tuple[list[dict], list[str]]`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_convert.py`:

```python
class _ClusterApexClient:
    """Per-primary account ids so secondaries can link; records each phase's apex."""
    def __init__(self):
        self.calls = []

    def apex_run(self, apex: str) -> str:
        self.calls.append(apex)
        head = apex.split("Map<Id,Id>", 1)[0]
        ids = list(dict.fromkeys(re.findall(r"'(00Q[0-9A-Za-z]+)'", head)))
        out = []
        for i in ids:
            # a lead already linked (appears in acctByLead map) echoes that account;
            # otherwise it "creates" account 001<lead>.
            out.append({"leadId": i, "success": True, "accountId": f"001{i}"})
        return f"{SENTINEL}{json.dumps(out)}{SENTINEL}"


def test_convert_leads_clusters_same_company_create_new():
    client = _ClusterApexClient()
    targets = [ConversionTarget("00Q1", lf_company_id="LF-1"),
               ConversionTarget("00Q2", lf_company_id="LF-1"),
               ConversionTarget("00Q3", lf_company_id="LF-2")]
    res = convert_leads(client, targets, SETTINGS, dry_run=False, batch_size=50)
    assert res["converted"] == 3 and res["failed"] == 0 and res["missing"] == 0
    # Phase 2 ran and linked 00Q2 to the account 00Q1 created (00100Q1).
    phase2 = client.calls[-1]
    assert "00Q2" in phase2 and "'00Q2' => '00100Q1'" in phase2


def test_convert_leads_blank_company_not_clustered():
    client = _ClusterApexClient()
    targets = [ConversionTarget("00Q1"), ConversionTarget("00Q2")]  # both blank lf_company_id
    res = convert_leads(client, targets, SETTINGS, dry_run=False, batch_size=50)
    assert res["converted"] == 2
    # single phase only (no secondaries) -> exactly one apex call
    assert len(client.calls) == 1


def test_convert_leads_orphan_secondary_when_primary_fails():
    class _PrimaryFailsClient:
        def __init__(self):
            self.calls = []

        def apex_run(self, apex):
            self.calls.append(apex)
            head = apex.split("Map<Id,Id>", 1)[0]
            ids = list(dict.fromkeys(re.findall(r"'(00Q[0-9A-Za-z]+)'", head)))
            out = [{"leadId": i, "success": False, "errors": ["DUPLICATE"]} for i in ids]
            return f"{SENTINEL}{json.dumps(out)}{SENTINEL}"

    client = _PrimaryFailsClient()
    targets = [ConversionTarget("00Q1", lf_company_id="LF-1"),
               ConversionTarget("00Q2", lf_company_id="LF-1")]
    res = convert_leads(client, targets, SETTINGS, dry_run=False, batch_size=50)
    assert res["converted"] == 0
    assert res["failed"] == 2          # primary failed + secondary reported failed
    assert res["missing"] == 0
    assert len(client.calls) == 1      # no phase 2 (primary produced no account)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_convert.py -k "clusters_same or blank_company or orphan_secondary" -v`
Expected: FAIL (single-phase convert links nothing; `'00Q2' => '00100Q1'` absent)

- [ ] **Step 3: Add helpers and refactor `convert_leads`**

In `src/leadauto/convert/converter.py`, add `from dataclasses import dataclass, replace` (extend the existing import), then add helpers above `convert_leads`:

```python
def _cluster_by_company(targets: list[ConversionTarget]) -> list[list[ConversionTarget]]:
    """Group create-new targets by lf_company_id (first-seen order); blank key => singleton.
    First member of each cluster is the primary (create-new), the rest are secondaries."""
    groups: dict[str, list[ConversionTarget]] = {}
    order: list[str] = []
    singletons: list[list[ConversionTarget]] = []
    for t in targets:
        key = (t.lf_company_id or "").strip()
        if not key:
            singletons.append([t])
            continue
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(t)
    return [groups[k] for k in order] + singletons


def _run_convert(client, targets, settings, batch_size, log, phase_label=""):
    """Convert one target list in batches; return (results, missing_ids)."""
    results: list[dict] = []
    missing_ids: list[str] = []
    for i in range(0, len(targets), batch_size):
        batch = targets[i:i + batch_size]
        apex = build_convert_apex(
            batch, owner_id=settings.owner_id, converted_status=settings.converted_status)
        parsed = parse_results(client.apex_run(apex))
        returned = {_short_id(str(r.get("leadId") or "")) for r in parsed}
        absent = [t.lead_id for t in batch if _short_id(t.lead_id) not in returned]
        if absent:
            log.error("%sBatch %d: %d of %d lead(s) have no conversion result: %s",
                      phase_label, i // batch_size + 1, len(absent), len(batch),
                      ", ".join(absent))
            missing_ids.extend(absent)
        results.extend(parsed)
    return results, missing_ids
```

Then replace the body of `convert_leads` after the `dry_run` / empty guards (keep those guards, but add `"already_converted": 0` — already present from the hardening) with:

```python
    existing_link = [t for t in targets if t.account_id]
    create_new = [t for t in targets if not t.account_id]
    clusters = _cluster_by_company(create_new)
    primaries = [c[0] for c in clusters]

    results1, missing1 = _run_convert(
        client, existing_link + primaries, settings, batch_size, log, "phase1 ")
    acct_by_primary = {_short_id(str(r.get("leadId") or "")): r["accountId"]
                       for r in results1 if r.get("success") and r.get("accountId")}

    phase2_targets: list[ConversionTarget] = []
    orphan_results: list[dict] = []
    for cluster in clusters:
        if len(cluster) < 2:
            continue
        primary, secondaries = cluster[0], cluster[1:]
        new_acct = acct_by_primary.get(_short_id(primary.lead_id))
        if new_acct:
            phase2_targets.extend(replace(s, account_id=new_acct) for s in secondaries)
        else:
            orphan_results.extend(
                {"leadId": s.lead_id, "success": False,
                 "errors": [f"primary {primary.lead_id} created no account; deferred"]}
                for s in secondaries)

    results2, missing2 = ((_run_convert(client, phase2_targets, settings, batch_size, log,
                                        "phase2 ")) if phase2_targets else ([], []))

    all_results = results1 + results2 + orphan_results
    missing_ids = missing1 + missing2
    already = sum(1 for r in all_results if r.get("alreadyConverted"))
    converted = sum(1 for r in all_results
                    if r.get("success") and not r.get("alreadyConverted"))
    failed = sum(1 for r in all_results if not r.get("success"))
    log.info("Converted %d lead(s), %d already converted, %d failed, %d missing",
             converted, already, failed, len(missing_ids))
    return {"converted": converted, "already_converted": already, "failed": failed,
            "planned": planned, "missing": len(missing_ids), "missing_ids": missing_ids,
            "results": all_results}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_convert.py -v`
Expected: PASS (new cluster tests + existing convert tests — the single-batch/aggregation tests still hold because a no-secondary set produces exactly one phase).

- [ ] **Step 5: Run the full suite**

Run: `python -m pytest -q`
Expected: PASS (all)

- [ ] **Step 6: Commit**

```bash
git add src/leadauto/convert/converter.py tests/test_convert.py
git commit -m "feat(convert): cluster same-company create-new leads into one account, N contacts"
```

---

### Task 9: Docs + Stage 2 summary surfacing

**Files:**
- Modify: `src/leadauto/convert/CLAUDE.md` (flow + gotchas)
- Modify: `src/leadauto/cli.py` (stage2 summary — optional count surfacing)
- Test: none new (doc/informational)

- [ ] **Step 1: Update the Stage 2 guide**

In `src/leadauto/convert/CLAUDE.md`, add to the Flow section that (a) `resolve_pool_links` runs after preflight and overwrite-enriches pool-owned (`Owner.Name='Rohdaten Pool'`) linked Accounts, routing kununu profile-URL/UUID conflicts to review; and (b) `convert_leads` clusters create-new leads by `lf_company_id` (primary create-new + linked secondaries → one Account, N Contacts). Note the two new approved-CSV columns (`lf_company_id`, `kununu_profile_uuid`) and `config salesforce.rohdaten_pool_owner_id`.

- [ ] **Step 2: (Optional) surface counts in the CLI summary**

If `run_stage2` is extended to return pool/conflict counts, add them to the `stage2` echo in `src/leadauto/cli.py`. If not surfaced, note it as a follow-up. (No behavior change required for correctness.)

- [ ] **Step 3: Run the full suite**

Run: `python -m pytest -q`
Expected: PASS (all)

- [ ] **Step 4: Commit**

```bash
git add src/leadauto/convert/CLAUDE.md src/leadauto/cli.py
git commit -m "docs(stage2): document pool-account enrichment + clustering"
```

---

## Self-Review

**Spec coverage:**
- Pool identity = Owner → Task 1 (config), Task 6 (`OwnerId` check). ✓
- Timing = Stage 2 post-approval → Task 7 (after preflight, before convert). ✓
- Overwrite-on-difference → Task 5 (`account_update`). ✓
- Kununu identity gate (blank→fill, equal→no-op, differ→review) → Task 5 + Task 6 + Task 7. ✓
- URL + UUID comparison, no metadata change (UUID via CSV) → Task 3 (QA column), Task 4 (ConversionTarget), Task 5. ✓
- Dedup by `lf_company_id`, two-phase, one Account/N Contacts → Task 8. ✓
- Enabling columns `lf_company_id` + `kununu_profile_uuid` → Task 3. ✓
- Dry-run does not write → Task 6 (guarded `update_bulk`), Task 7 (passes `dry_run`). ✓
- Primary-failure ⇒ secondaries reported, not converted → Task 8 (`orphan_results`). ✓

**Placeholder scan:** Task 9 Step 2 is explicitly optional and self-contained; all code steps show complete code. No TBD/TODO in code steps.

**Type consistency:** `account_update(account, lead, *, kununu_url, kununu_profile_uuid) -> (reason, payload)` used identically in Task 5 and Task 6. `resolve_pool_links(...) -> (surviving, review_failures)` used identically in Task 6 and Task 7. `ConversionTarget` new fields (Task 4) consumed in Tasks 6 and 8. `_run_convert`/`_cluster_by_company` defined and used within Task 8. `_short_id`, `parse_results`, `build_convert_apex` are pre-existing in `converter.py`.

**Open verification (carried from spec):** `KN_EmployeeCategory__c` picklist-label compatibility — Task 5 includes the verify command and the fallback (add a label map) before shipping.
