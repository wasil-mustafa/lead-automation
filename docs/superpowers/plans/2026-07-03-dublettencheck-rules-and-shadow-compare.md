# Dublettencheck Rule Engine + Shadow Comparison Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the score-threshold matcher with an ordered rule engine mirroring the business decision tree, and add a shadow-mode backtest that scores dry-run predictions against human conversions.

**Architecture:** `match.py` gets an ordered rule evaluation (first hit wins) instead of max-score + threshold bands; `MatchResult` carries the fired `rule` + `evidence`. Dry-run stage 1 logs predictions to `state/predictions.jsonl`; a new `shadow-compare` CLI command scores pending predictions against the org's actual conversions and emits `shadow_comparison.xlsx` + cumulative `state/shadow_results.jsonl`.

**Tech Stack:** Python 3.12, pytest (pythonpath=src), rapidfuzz, openpyxl, typer, sf CLI wrapper (`SfClient.query`), GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-07-03-dublettencheck-rules-and-shadow-compare-design.md`

## Global Constraints

- Everything runs under `dry_run: true`; no live Salesforce writes or conversions in this plan.
- No Salesforce metadata changes; only existing fields are read (`BillingPostalCode`, `BillingCity`, `IsConverted`, `ConvertedDate`, `ConvertedAccountId`, `ConvertedContactId`, `Account.CreatedDate` are all standard).
- Secrets only via env/GitHub Secrets. Never hardcode.
- `matching.auto_link_threshold` and `matching.review_threshold` are REMOVED; new key `matching.dedupe_name_agree_min: 85`.
- Invariant: a lead with `enriched.matched == False` (no accepted LF match) always gets decision `review` — never `link`, never `create_new`.
- `Lead.ConvertedDate` is DATE-only in Salesforce → "new account" detection = Account.CreatedDate same calendar day (UTC) as ConvertedDate (tolerate full datetime if the org returns one: then ±1 h).
- Run tests with `python -m pytest -q` from the repo root (Windows PowerShell; `$env:PYTHONPATH` not needed — pyproject sets pythonpath=src for pytest). For ad-hoc imports: `$env:PYTHONPATH = "src"`.
- Commit after every task. Branch: `dublettencheck-rules` (already created, spec committed).

## File Structure

| File | Change | Responsibility |
|---|---|---|
| `src/leadauto/textnorm.py` | modify | + `normalize_postal`, `normalize_city` |
| `src/leadauto/sf/soql.py` | modify | + Billing fields in account SELECT; + `leads_shadow_status`, `accounts_by_ids`, `contacts_by_ids` |
| `src/leadauto/leadfeeder/enrich.py` | modify | `EnrichedLead.alt_names` kept from the feed lookup |
| `src/leadauto/match.py` | rewrite core | ordered rule engine; `MatchResult.rule/.evidence`; `decide()` removed |
| `src/leadauto/config.py` + `config/config.yaml` | modify | + `dedupe_name_agree_min`; − `auto_link_threshold`, `review_threshold` |
| `src/leadauto/pipeline/stage2_convert.py` | modify | contact gate by `contact_reason` instead of score threshold |
| `src/leadauto/qa_artifact.py` | modify | + `match_rule`/`match_evidence` columns, − `account_score`; per-rule Summary counts |
| `src/leadauto/predictions.py` | create | prediction log read/write/upsert (`state/predictions.jsonl`) |
| `src/leadauto/pipeline/stage1_enrich.py` | modify | record predictions on dry runs |
| `src/leadauto/shadow_compare.py` | create | score pending predictions vs org conversions; xlsx + results JSONL |
| `src/leadauto/cli.py` | modify | + `shadow-compare` command |
| `.github/workflows/stage1.yml` | modify | shadow step + artifact + state commit on every scheduled run |
| `.gitignore` | modify | `!state/*.jsonl` |
| tests | per task | see tasks |

---

### Task 1: Address normalizers in textnorm

**Files:**
- Modify: `src/leadauto/textnorm.py` (append at end)
- Test: `tests/test_textnorm.py` (append)

**Interfaces:**
- Produces: `normalize_postal(value: str | None) -> str | None` (all whitespace stripped), `normalize_city(value: str | None) -> str | None` (casefold, trim, inner whitespace collapsed). Both return `None` for empty.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_textnorm.py`)

```python
from leadauto.textnorm import normalize_city, normalize_postal


def test_normalize_postal_strips_whitespace():
    assert normalize_postal(" 10 115 ") == "10115"
    assert normalize_postal("1010") == "1010"
    assert normalize_postal("") is None
    assert normalize_postal(None) is None


def test_normalize_city_casefolds_and_collapses():
    assert normalize_city("  Wien ") == "wien"
    assert normalize_city("Frankfurt  am   Main") == "frankfurt am main"
    assert normalize_city("MÜNCHEN") == "münchen"
    assert normalize_city("") is None
    assert normalize_city(None) is None
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_textnorm.py -q`
Expected: FAIL — `ImportError: cannot import name 'normalize_city'`

- [ ] **Step 3: Implement** (append to `src/leadauto/textnorm.py`)

```python
def normalize_postal(value: str | None) -> str | None:
    """Postal code with ALL whitespace removed (LF and org store it inconsistently)."""
    if not value:
        return None
    v = re.sub(r"\s+", "", str(value))
    return v or None


def normalize_city(value: str | None) -> str | None:
    """Casefolded city name with collapsed whitespace, for address-identity compare."""
    if not value:
        return None
    v = re.sub(r"\s+", " ", str(value)).strip().casefold()
    return v or None
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_textnorm.py -q` — Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/leadauto/textnorm.py tests/test_textnorm.py
git commit -m "feat(textnorm): postal-code and city normalizers for the address rule"
```

---

### Task 2: SOQL — Billing fields + shadow queries

**Files:**
- Modify: `src/leadauto/sf/soql.py`
- Test: `tests/test_soql.py` (append)

**Interfaces:**
- Produces: `_ACCOUNT_MATCH_FIELDS` includes `BillingPostalCode, BillingCity`; new query builders `leads_shadow_status(ids) -> str`, `accounts_by_ids(ids) -> str`, `contacts_by_ids(ids) -> str` (each raises `ValueError` on empty ids, mirroring `leads_by_ids`).
- Consumes: existing `_in_list`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_soql.py`)

```python
from leadauto.sf.soql import accounts_by_ids, contacts_by_ids, leads_shadow_status


def test_account_match_fields_include_billing():
    q = soql.accounts_by_signals(names=["Acme GmbH"])
    assert "BillingPostalCode" in q and "BillingCity" in q


def test_leads_shadow_status():
    q = leads_shadow_status(["00Q1", "00Q2"])
    assert q.startswith("SELECT ")
    for f in ("IsConverted", "ConvertedDate", "ConvertedAccountId",
              "ConvertedContactId", "KN_Employee__c", "KN_DBRevenueinMillions__c",
              "KN_DBISO2CountryCode__c", "KN_DBPostalCode__c", "KN_DBCity__c"):
        assert f in q
    assert "WHERE Id IN ('00Q1','00Q2')" in q
    with pytest.raises(ValueError):
        leads_shadow_status([])


def test_accounts_and_contacts_by_ids():
    aq = accounts_by_ids(["001A"])
    assert "CreatedDate" in aq and "BillingPostalCode" in aq and "FROM Account" in aq
    cq = contacts_by_ids(["003C"])
    assert "Email" in cq and "FROM Contact" in cq
    with pytest.raises(ValueError):
        accounts_by_ids([])
    with pytest.raises(ValueError):
        contacts_by_ids([])
```

(Adopt the existing import style of the file — it imports `soql` as a module plus names; keep consistent.)

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_soql.py -q` — Expected: FAIL (ImportError / missing Billing fields)

- [ ] **Step 3: Implement** in `src/leadauto/sf/soql.py`

Extend the account SELECT list:

```python
_ACCOUNT_MATCH_FIELDS = (
    "Id, Name, Website, KN_KununuProfileUuid__c, KN_KununuLinkProfile__c, "
    "KN_VATNumber__c, KN_DBID__c, KN_DBLegalCompanyName__c, "
    "BillingPostalCode, BillingCity"
)
```

Append at the end of the file:

```python
# --- shadow comparison (score dry-run predictions vs human conversions) ------

_LEAD_SHADOW_FIELDS = (
    "Id, IsConverted, ConvertedDate, ConvertedAccountId, ConvertedContactId, "
    "KN_Employee__c, KN_DBRevenueinMillions__c, KN_DBISO2CountryCode__c, "
    "KN_DBPostalCode__c, KN_DBCity__c"
)
_ACCOUNT_SHADOW_FIELDS = "Id, Name, CreatedDate, Website, BillingPostalCode, BillingCity"
_CONTACT_SHADOW_FIELDS = "Id, AccountId, Email, Phone"


def leads_shadow_status(ids: Sequence[str]) -> str:
    """Conversion status + current KN_* fields for predicted leads."""
    if not ids:
        raise ValueError("leads_shadow_status requires at least one Id")
    return f"SELECT {_LEAD_SHADOW_FIELDS} FROM Lead WHERE Id IN ({_in_list(ids)})"


def accounts_by_ids(ids: Sequence[str]) -> str:
    if not ids:
        raise ValueError("accounts_by_ids requires at least one Id")
    return f"SELECT {_ACCOUNT_SHADOW_FIELDS} FROM Account WHERE Id IN ({_in_list(ids)})"


def contacts_by_ids(ids: Sequence[str]) -> str:
    if not ids:
        raise ValueError("contacts_by_ids requires at least one Id")
    return f"SELECT {_CONTACT_SHADOW_FIELDS} FROM Contact WHERE Id IN ({_in_list(ids)})"
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_soql.py -q` — Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/leadauto/sf/soql.py tests/test_soql.py
git commit -m "feat(soql): Billing address in account match pull + shadow-compare queries"
```

---

### Task 3: Keep assigned-company alt names on EnrichedLead

**Files:**
- Modify: `src/leadauto/leadfeeder/enrich.py`
- Test: `tests/test_enrich.py` (append)

**Interfaces:**
- Produces: `EnrichedLead.alt_names: list[str]` — the assigned-company names sent to LF (`[]` without feed). Rule 4 (Task 4) consumes it.

- [ ] **Step 1: Write the failing test** (append to `tests/test_enrich.py`; reuse that file's existing fake client + settings fixtures — it already tests the alt-name payload path, model the new test on the existing `alt_names` payload test there)

```python
def test_alt_names_kept_on_enriched_lead(...):  # same fixtures as the existing alt-names test
    # profiles fixture maps the lead's profile uuid to ProfileInfo(name=..., companies=["Acme GmbH", "Acme Services GmbH"])
    results = enrich_leads(fake_client, [extracted], settings, profiles=profiles)
    assert results[0].alt_names == ["Acme Services GmbH"]  # own-name variant filtered, same as payload


def test_alt_names_default_empty():
    r = EnrichedLead(extracted=_extracted())
    assert r.alt_names == []
```

(The exact fixture names live in `tests/test_enrich.py` — copy the arrangement of its existing test that asserts `payload["alt_names"]`; the new assertion is on the returned `EnrichedLead` instead of the captured payload.)

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_enrich.py -q` — Expected: FAIL — `AttributeError: alt_names`

- [ ] **Step 3: Implement** in `src/leadauto/leadfeeder/enrich.py`

Add the field to the dataclass:

```python
@dataclass
class EnrichedLead:
    """An ExtractedLead plus whatever LeadFeeder returned. Feeds Module 3 (match/write-back)."""
    extracted: ExtractedLead
    matched: bool = False
    lf_company_id: str | None = None
    matched_name: str | None = None
    match_score: float | None = None
    name_agreement: int | None = None  # gate's 0-100 name/domain agreement (see client._best_match)
    agreed_via: str | None = None  # which signal vouched: company_name / alt_name:<n> / email_domain
    alt_names: list[str] = field(default_factory=list)  # assigned-company names from the kununu feed
    company: dict = field(default_factory=dict)   # flatten_company output (or {})
    contact_found: bool = False
    contact: dict = field(default_factory=dict)   # flattened details + contact_id/full_name
```

In the phase-1 loop, after `payload["alt_names"] = alts` is set, also keep them on the result. The existing block:

```python
                if alts:
                    payload["alt_names"] = alts
```

becomes:

```python
                if alts:
                    payload["alt_names"] = alts
                    result.alt_names = alts
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_enrich.py -q` — Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/leadauto/leadfeeder/enrich.py tests/test_enrich.py
git commit -m "feat(enrich): keep feed alt names on EnrichedLead for the dedupe rules"
```

---

### Task 4: Rule engine in match.py

**Files:**
- Modify: `src/leadauto/match.py` (replace scoring section; keep `ExistingRecords`, `load_existing_records`, `match_contact` as-is except noted)
- Modify: `src/leadauto/config.py` (+ `dedupe_name_agree_min` property), `config/config.yaml` (+ key; do NOT remove old keys yet — Task 5)
- Test: `tests/test_match.py` (replace the scoring/decide test sections; keep normalization + ExistingRecords + loader + contact tests)

**Interfaces:**
- Produces:
  - `RULE_DECISIONS: dict[str, str]` mapping the 8 rule names → `"link" | "review" | "create_new"`.
  - `evaluate_account_rules(enriched, existing, settings) -> tuple[str, AccountMatch | None, list[str]]` (rule, account, evidence).
  - `MatchResult` gains `rule: str = ""`, `evidence: list[str] = field(default_factory=list)`.
  - `AccountMatch.score: float | None` (display only, always `None` from rules).
  - `match_lead(enriched, existing, settings) -> MatchResult` unchanged signature.
  - `Settings.dedupe_name_agree_min -> int` (default 85).
  - REMOVED: `decide`, `match_account` (rule engine replaces both).
- Consumes: `normalize_postal`/`normalize_city` (Task 1), `EnrichedLead.alt_names` (Task 3), Billing fields in account dicts (Task 2).

- [ ] **Step 1: Write the failing tests.** In `tests/test_match.py`: delete the tests covering `match_account` scores and `decide` bands; keep and adapt builders. Add:

```python
from leadauto.match import RULE_DECISIONS, evaluate_account_rules

LF = dict(matched=True, matched_name="Acme GmbH", lf_company_id="lf-1")


def _lf_enriched(company=None, extracted=None, **kw):
    """An EnrichedLead that passed the LF gate."""
    base = {**LF, **kw}
    return _enriched(extracted=extracted, company=company or {}, **base)


def test_rule_no_lf_match_always_reviews():
    # Even a perfect kununu UUID hit must NOT link without an accepted LF match.
    ex = ExistingRecords.from_records([_acct("001A", KN_KununuProfileUuid__c="puid-1")], [])
    enr = _enriched(extracted=_extracted(kununu_profile_uuid="puid-1"))
    r = match_lead(enr, ex, SETTINGS)
    assert r.rule == "no_lf_match" and r.decision == "review"
    assert r.account is None
    assert "kununu_profile_uuid→001A" in r.evidence


def test_rule_name_address_links():
    ex = ExistingRecords.from_records(
        [_acct("001A", Name="Acme GmbH", BillingPostalCode="10115", BillingCity="Berlin")], [])
    enr = _lf_enriched(company={"Postal_Code": "10 115", "City": "berlin"})
    r = match_lead(enr, ex, SETTINGS)
    assert r.rule == "name_address" and r.decision == "link"
    assert r.account.account_id == "001A"


def test_rule_name_address_requires_both_plz_and_city():
    ex = ExistingRecords.from_records(
        [_acct("001A", Name="Acme GmbH", BillingPostalCode="10115", BillingCity="Hamburg")], [])
    enr = _lf_enriched(company={"Postal_Code": "10115", "City": "Berlin"})
    r = match_lead(enr, ex, SETTINGS)
    assert r.rule == "name_only" and r.decision == "review"


def test_rule_name_vat_links():
    ex = ExistingRecords.from_records([_acct("001A", Name="Acme GmbH", KN_VATNumber__c="DE 123")], [])
    enr = _lf_enriched(company={"VAT_Id": "de123"})
    r = match_lead(enr, ex, SETTINGS)
    assert r.rule == "name_vat" and r.decision == "link"


def test_rule_kununu_agree_links():
    ex = ExistingRecords.from_records(
        [_acct("001A", Name="Acme Services GmbH",
               KN_KununuLinkProfile__c="https://www.kununu.com/de/acme")], [])
    enr = _lf_enriched(extracted=_extracted(kununu_url="kununu.com/de/acme"),
                       matched_name="Zeta AG", alt_names=["Acme Services GmbH"])
    r = match_lead(enr, ex, SETTINGS)
    assert r.rule == "kununu_agree" and r.decision == "link"
    assert r.account.account_id == "001A"


def test_rule_kununu_conflict_reviews():
    ex = ExistingRecords.from_records(
        [_acct("001A", Name="Completely Different Firm",
               KN_KununuLinkProfile__c="https://www.kununu.com/de/acme")], [])
    enr = _lf_enriched(extracted=_extracted(kununu_url="kununu.com/de/acme"))
    r = match_lead(enr, ex, SETTINGS)
    assert r.rule == "kununu_conflict" and r.decision == "review"
    assert r.account.account_id == "001A"  # the conflicting account is surfaced


def test_rule_kununu_beats_name_only():
    # kununu URL (rules 4/5) is evaluated before bare name equality (rule 6).
    ex = ExistingRecords.from_records(
        [_acct("001A", Name="Acme GmbH"),
         _acct("001B", Name="Acme Holding",
               KN_KununuLinkProfile__c="https://www.kununu.com/de/acme")], [])
    enr = _lf_enriched(extracted=_extracted(kununu_url="kununu.com/de/acme"),
                       matched_name="Acme GmbH")
    r = match_lead(enr, ex, SETTINGS)
    assert r.rule in ("kununu_agree", "kununu_conflict")
    assert r.account.account_id == "001B"


def test_rule_name_only_reviews():
    ex = ExistingRecords.from_records([_acct("001A", Name="Acme GmbH")], [])
    r = match_lead(_lf_enriched(), ex, SETTINGS)
    assert r.rule == "name_only" and r.decision == "review"
    assert r.account.account_id == "001A"


def test_rule_weak_signal_reviews_with_evidence():
    ex = ExistingRecords.from_records([_acct("001A", KN_VATNumber__c="DE123")], [])
    enr = _lf_enriched(matched_name="Zeta AG", company={"VAT_Id": "DE123"})
    r = match_lead(enr, ex, SETTINGS)
    assert r.rule == "weak_signal" and r.decision == "review"
    assert r.account.account_id == "001A"
    assert "vat→001A" in r.evidence


def test_rule_no_match_creates():
    ex = ExistingRecords.from_records([], [])
    r = match_lead(_lf_enriched(), ex, SETTINGS)
    assert r.rule == "no_match" and r.decision == "create_new"
    assert r.account is None


def test_rule_decisions_cover_all_rules():
    assert set(RULE_DECISIONS) == {
        "no_lf_match", "name_address", "name_vat", "kununu_agree",
        "kununu_conflict", "name_only", "weak_signal", "no_match",
    }
    assert set(RULE_DECISIONS.values()) == {"link", "review", "create_new"}
```

Update the builders: `_acct` gains `"BillingPostalCode": None, "BillingCity": None` in its base dict. Remove `decide`/`match_account` from the import list.

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_match.py -q` — Expected: FAIL (ImportError on `RULE_DECISIONS`)

- [ ] **Step 3: Implement.** In `src/leadauto/match.py`:

Module docstring — replace the score table with:

```python
"""Module 3a — rule-based Dublettencheck of an enriched Lead vs existing Accounts/Contacts.

Ordered rules mirror the business decision tree (spec: docs/superpowers/specs/
2026-07-03-dublettencheck-rules-and-shadow-compare-design.md); first hit wins:

  1 no_lf_match      no accepted LF company                             -> review
  2 name_address     normalized name equal + postal code + city equal   -> link
  3 name_vat         normalized name equal + VAT equal                  -> link
  4 kununu_agree     profile UUID/URL on an Account + name agreement    -> link
  5 kununu_conflict  profile UUID/URL hit, name disagrees               -> review
  6 name_only        normalized name equal, nothing else                -> review
  7 weak_signal      only VAT/DBID/host/email-domain/fuzzy point at one -> review
  8 no_match         nothing                                            -> create_new

Contact matching keeps its identifier signals (kununu user UUID / email); numeric
scores survive only as contact-side display metadata.
"""
```

Add `import re` and keep the rapidfuzz import. Replace the scoring section (everything from `_best_fuzzy_account` through `match_lead`, and `decide`) with:

```python
RULE_DECISIONS = {
    "no_lf_match": "review",
    "name_address": "link",
    "name_vat": "link",
    "kununu_agree": "link",
    "kununu_conflict": "review",
    "name_only": "review",
    "weak_signal": "review",
    "no_match": "create_new",
}


def _norm_vat(value) -> str:
    return re.sub(r"\s+", "", str(value or "")).upper()


def _lead_name_norm(enriched: EnrichedLead) -> str | None:
    return normalize_name(enriched.matched_name or enriched.extracted.best_company)


def _account_names_norm(account: dict) -> set[str]:
    names = (account.get("Name"), account.get("KN_DBLegalCompanyName__c"))
    return {n for raw in names if (n := normalize_name(raw))}


def _name_equal_accounts(enriched: EnrichedLead, existing: ExistingRecords) -> list[dict]:
    target = _lead_name_norm(enriched)
    return existing.acct_by_norm_name.get(target, []) if target else []


def _address_equal(account: dict, company: dict) -> bool:
    plz = normalize_postal(company.get("Postal_Code"))
    city = normalize_city(company.get("City"))
    if not plz or not city:
        return False
    return (normalize_postal(account.get("BillingPostalCode")) == plz
            and normalize_city(account.get("BillingCity")) == city)


def _kununu_account(enriched: EnrichedLead, existing: ExistingRecords):
    """(account, signal_name) for a profile-UUID or kununu-URL hit, else None."""
    e = enriched.extracted
    puid = (e.kununu_profile_uuid or "").strip()
    if puid and puid in existing.acct_by_profile_uuid:
        return existing.acct_by_profile_uuid[puid], "kununu_profile_uuid"
    url = normalize_kununu_url(e.kununu_url)
    if url and url in existing.acct_by_kununu_url:
        return existing.acct_by_kununu_url[url], "kununu_url"
    return None


def _kununu_name_agreement(account: dict, enriched: EnrichedLead) -> int:
    """Max 0-100 agreement between the account's names and the lead's identities
    (LF company name, parsed profile/company name, feed assigned-company names)."""
    identities = [enriched.matched_name, enriched.extracted.best_company,
                  *(enriched.alt_names or [])]
    best = 0
    for acct_name in _account_names_norm(account):
        for ident in identities:
            ident_norm = normalize_name(ident)
            if ident_norm:
                best = max(best, int(fuzz.token_set_ratio(acct_name, ident_norm)))
    return best


def _best_fuzzy_account(enriched: EnrichedLead, existing: ExistingRecords) -> dict | None:
    """Closest non-identical fuzzy name hit (>= floor); exact equality is rule 2/3/6's job."""
    target = _lead_name_norm(enriched)
    if not target:
        return None
    best = None
    for norm, accts in existing.acct_by_norm_name.items():
        if norm == target:
            continue
        ratio = fuzz.token_sort_ratio(target, norm) / 100.0
        if ratio >= _FUZZY_FLOOR and (best is None or ratio > best[0]):
            best = (ratio, accts[0])
    return best[1] if best else None


def _weak_hits(enriched: EnrichedLead, existing: ExistingRecords) -> list[tuple[str, dict]]:
    """Demoted signals: they surface candidates as evidence but never link."""
    e = enriched.extracted
    company = enriched.company or {}
    hits: list[tuple[str, dict]] = []
    vat = _norm_vat(company.get("VAT_Id") or e.vat_number)
    if vat and vat in existing.acct_by_vat:
        hits.append(("vat", existing.acct_by_vat[vat]))
    dbid = (company.get("Register_Id") or "").strip()
    if dbid and dbid in existing.acct_by_dbid:
        hits.append(("dbid", existing.acct_by_dbid[dbid]))
    host = normalize_host(company.get("Website") or e.lead_website)
    if host and host in existing.acct_by_host:
        hits.append(("website_host", existing.acct_by_host[host][0]))
    domain = email_domain(e.contact_email or e.lead_email)
    if domain and domain not in _FREEMAIL_DOMAINS and domain in existing.acct_by_host:
        hits.append(("email_domain", existing.acct_by_host[domain][0]))
    fuzzy = _best_fuzzy_account(enriched, existing)
    if fuzzy:
        hits.append(("fuzzy_name", fuzzy))
    return hits


def _fire(enriched: EnrichedLead, existing: ExistingRecords, settings,
          kununu, name_accts, weak) -> tuple[str, AccountMatch | None]:
    if not enriched.matched:
        return "no_lf_match", None
    company = enriched.company or {}
    for acct in name_accts:
        if _address_equal(acct, company):
            return "name_address", AccountMatch(acct["Id"], None, "name_address")
    vat = _norm_vat(company.get("VAT_Id") or enriched.extracted.vat_number)
    if vat:
        for acct in name_accts:
            if _norm_vat(acct.get("KN_VATNumber__c")) == vat:
                return "name_vat", AccountMatch(acct["Id"], None, "name_vat")
    if kununu:
        acct, _sig = kununu
        agree = _kununu_name_agreement(acct, enriched)
        rule = ("kununu_agree" if agree >= settings.dedupe_name_agree_min
                else "kununu_conflict")
        return rule, AccountMatch(acct["Id"], None, rule)
    if name_accts:
        return "name_only", AccountMatch(name_accts[0]["Id"], None, "name_only")
    if weak:
        return "weak_signal", AccountMatch(weak[0][1]["Id"], None, "weak_signal")
    return "no_match", None


def evaluate_account_rules(
    enriched: EnrichedLead, existing: ExistingRecords, settings
) -> tuple[str, AccountMatch | None, list[str]]:
    """(fired rule, account, evidence). Evidence lists all demoted-signal hits, plus
    name/kununu hits when they did not fire — the full picture on Klärfall rows."""
    kununu = _kununu_account(enriched, existing)
    name_accts = _name_equal_accounts(enriched, existing)
    weak = _weak_hits(enriched, existing)
    rule, account = _fire(enriched, existing, settings, kununu, name_accts, weak)

    evidence = [f"{sig}→{acct['Id']}" for sig, acct in weak]
    if kununu and rule not in ("kununu_agree", "kununu_conflict"):
        evidence.insert(0, f"{kununu[1]}→{kununu[0]['Id']}")
    if name_accts and rule not in ("name_address", "name_vat", "name_only"):
        evidence.insert(0, f"name→{name_accts[0]['Id']}")
    return rule, account, evidence


def match_lead(enriched: EnrichedLead, existing: ExistingRecords, settings) -> MatchResult:
    rule, account, evidence = evaluate_account_rules(enriched, existing, settings)
    account_id = account.account_id if account else None
    contact = match_contact(enriched, existing, account_id, settings)
    return MatchResult(enriched=enriched, account=account, contact=contact,
                       decision=RULE_DECISIONS[rule], rule=rule, evidence=evidence)
```

Dataclass changes:

```python
@dataclass
class AccountMatch:
    account_id: str
    score: float | None   # display metadata only; rules decide, not scores
    reason: str


@dataclass
class MatchResult:
    enriched: EnrichedLead
    account: AccountMatch | None
    contact: ContactMatch | None
    decision: str  # "link" | "review" | "create_new"
    rule: str = ""
    evidence: list[str] = field(default_factory=list)
```

In `ExistingRecords.from_records`, key VAT normalized (rule 3 compares space/case-insensitively):

```python
            vat = _norm_vat(a.get("KN_VATNumber__c"))
            if vat:
                by_vat.setdefault(vat, a)
```

(`_norm_vat` must therefore be defined ABOVE the dataclass — place it right after the imports / `_FUZZY_FLOOR`.)

In `src/leadauto/config.py`, add below `lf_name_agree_rescue`:

```python
    @property
    def dedupe_name_agree_min(self) -> int:
        return int(self.raw["matching"].get("dedupe_name_agree_min", 85))
```

In `config/config.yaml` `matching:` block, add:

```yaml
  dedupe_name_agree_min: 85  # rule-4 floor: account name vs LF/profile/assigned-company names
```

- [ ] **Step 4: Run match tests, then the whole suite**

Run: `python -m pytest tests/test_match.py -q` — Expected: PASS.
Run: `python -m pytest -q` — Expected: failures ONLY in tests that encoded the old score bands. Fix them by intent, not by deletion:
  - `tests/test_pipeline.py` / `tests/test_cli.py`: any lead whose fake data has no LF match (`matched=False`, e.g. `--no-leadfeeder` paths) now gets decision `review` (was `create_new`). Update expected counts/strings accordingly.
  - `tests/test_qa_artifact.py`: `MatchResult(...)` constructions still work (new fields have defaults); assertions on `account_score` keep working until Task 6.
  - Any test asserting `decide(...)` or importing `match_account` must be updated to the rule API (most such cases were already rewritten in Step 1).

- [ ] **Step 5: Commit**

```bash
git add src/leadauto/match.py src/leadauto/config.py config/config.yaml tests/
git commit -m "feat(match): ordered Dublettencheck rule engine replaces score thresholds"
```

---

### Task 5: Retire thresholds; reason-based contact gate in stage 2

**Files:**
- Modify: `src/leadauto/config.py` (remove `auto_link_threshold`, `review_threshold` properties), `config/config.yaml` (remove both keys)
- Modify: `src/leadauto/pipeline/stage2_convert.py`
- Test: `tests/test_pipeline.py` (stage-2 section), `tests/test_config.py`

**Interfaces:**
- Produces: `stage2_convert._CONTACT_LINK_REASONS = {"kununu_user_uuid", "contact_email_in_account"}`; `targets_from_approved(rows, settings)` links a contact only when the row's `contact_reason` is in that set (behavior-identical to the old `contact_score >= 0.90`: those two reasons scored 1.0/0.95; `contact_email_global`/`linkedin` scored 0.85 → still excluded).
- REMOVED: `Settings.auto_link_threshold`, `Settings.review_threshold`.

- [ ] **Step 1: Write the failing tests.** In `tests/test_pipeline.py`, adapt the `targets_from_approved` tests: rows now carry `contact_reason` instead of relying on `contact_score`:

```python
def test_contact_linked_only_on_identifier_reasons():
    rows = [
        {"Approve?": "yes", "lead_id": "00Q1", "account_match_id": "001A",
         "contact_match_id": "003C", "contact_reason": "kununu_user_uuid"},
        {"Approve?": "yes", "lead_id": "00Q2", "account_match_id": "001A",
         "contact_match_id": "003D", "contact_reason": "contact_email_global"},
    ]
    targets = targets_from_approved(rows, SETTINGS)
    assert targets[0].contact_id == "003C"
    assert targets[1].contact_id is None  # weak reason -> create fresh contact
```

In `tests/test_config.py` add:

```python
def test_thresholds_removed_and_dedupe_floor_present():
    s = Settings.load()
    assert s.dedupe_name_agree_min == 85
    assert not hasattr(s, "auto_link_threshold")
    assert not hasattr(s, "review_threshold")
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_pipeline.py tests/test_config.py -q` — Expected: FAIL

- [ ] **Step 3: Implement.** In `stage2_convert.py` replace the score gate inside `targets_from_approved`:

```python
_CONTACT_LINK_REASONS = {"kununu_user_uuid", "contact_email_in_account"}
```

```python
        account_id = (row.get("account_match_id") or "").strip() or None
        contact_id = (row.get("contact_match_id") or "").strip() or None
        if contact_id and (row.get("contact_reason") or "").strip() not in _CONTACT_LINK_REASONS:
            contact_id = None
```

Update its docstring ("Contact is linked only when its match reason is an identifier signal — kununu user UUID or an email inside the linked account; weaker matches create a fresh contact"). Delete the two `Settings` properties and the two yaml keys (the yaml `matching:` block keeps `lf_*` + `dedupe_name_agree_min`). Fix any other test still referencing them (`grep -r auto_link_threshold src tests` must come back empty).

- [ ] **Step 4: Run the full suite**

Run: `python -m pytest -q` — Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/leadauto/config.py config/config.yaml src/leadauto/pipeline/stage2_convert.py tests/
git commit -m "feat(stage2): reason-based contact gate; retire score thresholds"
```

---

### Task 6: QA artifact — rule + evidence columns, per-rule summary

**Files:**
- Modify: `src/leadauto/qa_artifact.py`
- Test: `tests/test_qa_artifact.py`

**Interfaces:**
- Produces: `REVIEW_COLUMNS` with `match_rule`, `match_evidence` (after the `lf_*` block) and WITHOUT `account_score`; Summary sheet rows `rule_<name>` for all 8 rules.
- Consumes: `MatchResult.rule`, `MatchResult.evidence`, `RULE_DECISIONS` (Task 4).

- [ ] **Step 1: Write the failing tests** (append/adapt in `tests/test_qa_artifact.py`, using its existing result builders):

```python
def test_review_sheet_has_rule_and_evidence_columns():
    assert "match_rule" in REVIEW_COLUMNS and "match_evidence" in REVIEW_COLUMNS
    assert "account_score" not in REVIEW_COLUMNS
    # build one result with rule="name_only", evidence=["vat→001A"] via the file's builders,
    # then assert the Review row carries "name_only" and "vat→001A"


def test_summary_counts_rules():
    # one result rule="name_only", one rule="no_match"
    wb = build_workbook(results, [], {})
    metrics = {row[0].value: row[1].value for row in wb["Summary"].iter_rows(min_row=2)}
    assert metrics["rule_name_only"] == 1
    assert metrics["rule_no_match"] == 1
    assert metrics["rule_kununu_conflict"] == 0
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_qa_artifact.py -q` — Expected: FAIL

- [ ] **Step 3: Implement.** In `qa_artifact.py`:

```python
from .match import RULE_DECISIONS

REVIEW_COLUMNS = [
    "lead_id", "task_id", "task_type", "subject",
    "company", "kununu_url", "region",
    "contact_name", "contact_email",
    "lf_matched", "lf_matched_name", "lf_match_score", "lf_name_agreement", "lf_agreed_via",
    "lf_city", "lf_employees", "lf_industry", "lf_website",
    "match_rule", "match_evidence",
    "account_match_id", "account_reason",
    "contact_match_id", "contact_score", "contact_reason",
    "decision", "Approve?",
]
```

In `_review_row`, drop `"account_score"` and add:

```python
        "match_rule": result.rule,
        "match_evidence": "; ".join(result.evidence),
```

In `build_workbook`'s summary block, after the `decision_*` rows:

```python
    rules = [r.rule for r in results]
    summary.extend(["rule_" + name, rules.count(name)] for name in RULE_DECISIONS)
```

(Note `summary.extend(... for ...)` must append LISTS — keep the `[metric, value]` shape.)

- [ ] **Step 4: Run the full suite**

Run: `python -m pytest -q` — Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/leadauto/qa_artifact.py tests/test_qa_artifact.py
git commit -m "feat(qa): surface fired rule + evidence in Review sheet and Summary"
```

---

### Task 7: Prediction log + stage-1 integration

**Files:**
- Create: `src/leadauto/predictions.py`
- Modify: `src/leadauto/pipeline/stage1_enrich.py`, `.gitignore`
- Test: Create `tests/test_predictions.py`

**Interfaces:**
- Produces:
  - `Prediction` dataclass: `lead_id, task_id, predicted_at, rule, decision, account_id, contact_id, evidence, payload, lf_company_id, lf_company_name, lf_website, status ("pending"|"resolved"|"expired"), resolved_at, outcome`.
  - `load_predictions(path=None) -> list[Prediction]`, `save_predictions(preds, path=None)`, `record_predictions(results, payloads, predicted_at, path=None) -> int`.
  - `DEFAULT_PREDICTIONS_PATH = REPO_ROOT / "state" / "predictions.jsonl"`.
  - `run_stage1(..., predictions_path=DEFAULT_PREDICTIONS_PATH)` records predictions when `dry_run` is true; `predictions_path=None` disables.
- Consumes: `MatchResult` (Task 4), `write_back`'s `payloads` (unchanged).

- [ ] **Step 1: Write the failing tests** (`tests/test_predictions.py`):

```python
"""Prediction-log tests: JSONL round-trip, pending upsert, resolved immutability,
and the stage-1 dry-run hook."""
from types import SimpleNamespace

from leadauto.config import Settings
from leadauto.extract.models import ExtractedLead
from leadauto.leadfeeder.enrich import EnrichedLead
from leadauto.match import AccountMatch, ExistingRecords, MatchResult
from leadauto.predictions import (
    Prediction, load_predictions, record_predictions, save_predictions,
)

SETTINGS = Settings.load()


def _result(lead_id="00Q1", rule="name_only", decision="review", account_id="001A"):
    e = ExtractedLead(lead_id=lead_id, task_id="T1", task_type="claimer")
    enr = EnrichedLead(extracted=e, matched=True, matched_name="Acme GmbH",
                       lf_company_id="lf-1", company={"Website": "https://acme.de"})
    acct = AccountMatch(account_id, None, rule) if account_id else None
    return MatchResult(enriched=enr, account=acct, contact=None,
                       decision=decision, rule=rule, evidence=["vat→001A"])


def test_record_and_load_roundtrip(tmp_path):
    p = tmp_path / "predictions.jsonl"
    n = record_predictions([_result()], [{"Id": "00Q1", "KN_DBCity__c": "Berlin"}],
                           "2026-07-03T09:00:00+00:00", p)
    assert n == 1
    preds = load_predictions(p)
    assert preds[0].lead_id == "00Q1"
    assert preds[0].rule == "name_only"
    assert preds[0].payload == {"KN_DBCity__c": "Berlin"}   # Id stripped
    assert preds[0].lf_website == "https://acme.de"
    assert preds[0].status == "pending"


def test_upsert_replaces_pending_keeps_resolved(tmp_path):
    p = tmp_path / "predictions.jsonl"
    record_predictions([_result(rule="name_only")], [], "2026-07-01T09:00:00+00:00", p)
    preds = load_predictions(p)
    preds[0].status = "resolved"
    save_predictions(preds, p)
    record_predictions([_result(rule="name_address", decision="link"),
                        _result(lead_id="00Q2", rule="no_match", decision="create_new",
                                account_id=None)],
                       [], "2026-07-02T09:00:00+00:00", p)
    preds = {pr.lead_id: [q for q in load_predictions(p) if q.lead_id == pr.lead_id]
             for pr in load_predictions(p)}
    assert len(preds["00Q1"]) == 2            # resolved kept + fresh pending
    statuses = {q.status for q in preds["00Q1"]}
    assert statuses == {"resolved", "pending"}
    assert preds["00Q2"][0].rule == "no_match"


def test_stage1_records_predictions_on_dry_run(tmp_path, monkeypatch):
    from leadauto.pipeline import stage1_enrich as s1
    from leadauto.state import State

    e = ExtractedLead(lead_id="00Q9", task_id="T9", task_type="claimer",
                      company_name="Acme GmbH")
    monkeypatch.setattr(s1, "read_extracted_leads", lambda *a, **k: SimpleNamespace(
        leads=[e], skipped=[], processed_tasks=[]))
    monkeypatch.setattr(s1, "load_existing_records",
                        lambda *a, **k: ExistingRecords())
    p = tmp_path / "predictions.jsonl"
    s1.run_stage1(None, None, SETTINGS, state=State(), dry_run=True,
                  qa_path=None, state_path=None, predictions_path=p)
    preds = load_predictions(p)
    assert [q.lead_id for q in preds] == ["00Q9"]
    assert preds[0].rule == "no_lf_match"     # lf_client=None -> unmatched -> review

    # non-dry runs and predictions_path=None must not write
    p2 = tmp_path / "none.jsonl"
    s1.run_stage1(None, None, SETTINGS, state=State(), dry_run=True,
                  qa_path=None, state_path=None, predictions_path=None)
    assert not p2.exists()
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_predictions.py -q` — Expected: FAIL (no module `leadauto.predictions`)

- [ ] **Step 3: Implement.** Create `src/leadauto/predictions.py`:

```python
"""Shadow-mode prediction log (`state/predictions.jsonl`).

Every DRY stage-1 run records what the pipeline WOULD have done per lead; the
shadow-compare job later scores pending records against the human conversions that
actually happened (see shadow_compare.py). One JSON object per line. Upserts replace
a lead's pending record; resolved/expired records are immutable history.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .state import REPO_ROOT

DEFAULT_PREDICTIONS_PATH = REPO_ROOT / "state" / "predictions.jsonl"


@dataclass
class Prediction:
    lead_id: str
    task_id: str
    predicted_at: str              # ISO-8601 UTC of the dry run
    rule: str
    decision: str                  # link | review | create_new
    account_id: str | None = None  # predicted link target
    contact_id: str | None = None
    evidence: list[str] = field(default_factory=list)
    payload: dict = field(default_factory=dict)   # KN_* write-back payload (sans Id)
    lf_company_id: str | None = None
    lf_company_name: str | None = None
    lf_website: str | None = None
    status: str = "pending"        # pending | resolved | expired
    resolved_at: str | None = None
    outcome: dict | None = None    # filled by shadow_compare


def load_predictions(path: str | Path | None = None) -> list[Prediction]:
    p = Path(path) if path else DEFAULT_PREDICTIONS_PATH
    if not p.exists():
        return []
    return [Prediction(**json.loads(line))
            for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


def save_predictions(preds: list[Prediction], path: str | Path | None = None) -> None:
    p = Path(path) if path else DEFAULT_PREDICTIONS_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(json.dumps(asdict(pr), ensure_ascii=False) + "\n" for pr in preds),
                 encoding="utf-8")


def record_predictions(results: list, payloads: list[dict], predicted_at: str,
                       path: str | Path | None = None) -> int:
    """Upsert one pending Prediction per MatchResult; non-pending records are kept."""
    payload_by_lead = {p["Id"]: {k: v for k, v in p.items() if k != "Id"}
                       for p in payloads if p.get("Id")}
    existing = load_predictions(path)
    keep = [pr for pr in existing if pr.status != "pending"]
    pending = {pr.lead_id: pr for pr in existing if pr.status == "pending"}
    for r in results:
        e = r.enriched.extracted
        company = r.enriched.company or {}
        pending[e.lead_id] = Prediction(
            lead_id=e.lead_id, task_id=e.task_id, predicted_at=predicted_at,
            rule=r.rule, decision=r.decision,
            account_id=r.account.account_id if r.account else None,
            contact_id=r.contact.contact_id if r.contact else None,
            evidence=list(r.evidence),
            payload=payload_by_lead.get(e.lead_id, {}),
            lf_company_id=r.enriched.lf_company_id,
            lf_company_name=r.enriched.matched_name,
            lf_website=company.get("Website") or None,
        )
    save_predictions(keep + list(pending.values()), path)
    return len(pending)
```

In `stage1_enrich.py`: import `from ..predictions import DEFAULT_PREDICTIONS_PATH, record_predictions`; add parameter `predictions_path: str | Path | None = DEFAULT_PREDICTIONS_PATH` to `run_stage1`; after the `failures` block insert:

```python
    if dry_run and predictions_path is not None:
        stamp = (now or datetime.now(timezone.utc)).isoformat()
        n = record_predictions(results, wb.get("payloads", []), stamp, predictions_path)
        log.info("Recorded %d pending prediction(s) -> %s", n, predictions_path)
```

In `.gitignore`, extend the state exception block:

```gitignore
!state/
!state/*.json
!state/*.jsonl
```

- [ ] **Step 4: Run the full suite**

Run: `python -m pytest -q` — Expected: PASS (existing stage-1 tests keep passing: default path only writes when `dry_run` — check none of them run dry against the REAL repo state dir; if one does, pass `predictions_path=None` or a tmp path there).

- [ ] **Step 5: Commit**

```bash
git add src/leadauto/predictions.py src/leadauto/pipeline/stage1_enrich.py .gitignore tests/test_predictions.py
git commit -m "feat(shadow): prediction log written by dry-run stage 1"
```

---

### Task 8: Shadow comparison module

**Files:**
- Create: `src/leadauto/shadow_compare.py`
- Test: Create `tests/test_shadow_compare.py`

**Interfaces:**
- Consumes: `predictions.py` (Task 7), `soql.leads_shadow_status/accounts_by_ids/contacts_by_ids` (Task 2), `state._parse_sf_dt`, `textnorm` normalizers, `qa_artifact._add_sheet`.
- Produces: `run_shadow_compare(sf_client, *, predictions_path=None, results_path=None, xlsx_path=None, now=None, logger=None) -> dict` returning `{"checked", "resolved", "hits", "misses", "observed", "expired", "pending", "xlsx"}`; `DEFAULT_RESULTS_PATH = REPO_ROOT / "state" / "shadow_results.jsonl"`, `DEFAULT_XLSX_PATH = REPO_ROOT / "shadow_comparison.xlsx"`.

- [ ] **Step 1: Write the failing tests** (`tests/test_shadow_compare.py`):

```python
"""Shadow-compare tests: decision classification, field agreement, resolution flow.

FakeClient routes by SObject name in the SOQL string, same idea as the matcher's
loader tests."""
from datetime import datetime, timezone

from leadauto.predictions import Prediction, load_predictions, save_predictions
from leadauto.shadow_compare import run_shadow_compare

NOW = datetime(2026, 7, 4, 9, 0, tzinfo=timezone.utc)


class FakeClient:
    def __init__(self, leads=None, accounts=None, contacts=None):
        self.leads, self.accounts, self.contacts = leads or [], accounts or [], contacts or []

    def query(self, soql):
        if "FROM Lead" in soql:
            return self.leads
        if "FROM Account" in soql:
            return self.accounts
        return self.contacts


def _pred(lead_id="00Q1", decision="link", rule="name_address", account_id="001A",
          predicted_at="2026-07-03T09:00:00+00:00", **kw):
    return Prediction(lead_id=lead_id, task_id="T1", predicted_at=predicted_at,
                      rule=rule, decision=decision, account_id=account_id, **kw)


def _lead(lead_id="00Q1", account="001A", **kw):
    base = {"Id": lead_id, "IsConverted": True, "ConvertedDate": "2026-07-03",
            "ConvertedAccountId": account, "ConvertedContactId": None}
    base.update(kw)
    return base


def _account(aid="001A", created="2026-07-01T10:00:00.000+0000", **kw):
    base = {"Id": aid, "Name": "Acme GmbH", "CreatedDate": created,
            "Website": None, "BillingPostalCode": None, "BillingCity": None}
    base.update(kw)
    return base


def _run(tmp_path, preds, client):
    ppath, rpath, xpath = (tmp_path / "p.jsonl", tmp_path / "r.jsonl", tmp_path / "s.xlsx")
    save_predictions(preds, ppath)
    res = run_shadow_compare(client, predictions_path=ppath, results_path=rpath,
                             xlsx_path=xpath, now=NOW)
    return res, ppath, rpath, xpath


def test_link_hit_and_miss(tmp_path):
    preds = [_pred("00Q1", account_id="001A"), _pred("00Q2", account_id="001A")]
    client = FakeClient(
        leads=[_lead("00Q1", "001A"), _lead("00Q2", "001B")],
        accounts=[_account("001A"), _account("001B", created="2026-06-01T10:00:00.000+0000")])
    res, ppath, rpath, _ = _run(tmp_path, preds, client)
    assert res["hits"] == 1 and res["misses"] == 1 and res["resolved"] == 2
    statuses = {p.lead_id: p.status for p in load_predictions(ppath)}
    assert statuses == {"00Q1": "resolved", "00Q2": "resolved"}


def test_create_new_hit_same_day_account(tmp_path):
    # Lead.ConvertedDate is DATE-only: a new account = created the same UTC day.
    preds = [_pred("00Q1", decision="create_new", rule="no_match", account_id=None)]
    client = FakeClient(leads=[_lead()], accounts=[_account(created="2026-07-03T15:30:00.000+0000")])
    res, *_ = _run(tmp_path, preds, client)
    assert res["hits"] == 1


def test_create_new_miss_preexisting_account(tmp_path):
    preds = [_pred("00Q1", decision="create_new", rule="no_match", account_id=None)]
    client = FakeClient(leads=[_lead()], accounts=[_account(created="2025-01-01T00:00:00.000+0000")])
    res, *_ = _run(tmp_path, preds, client)
    assert res["misses"] == 1


def test_review_is_observed_not_scored(tmp_path):
    preds = [_pred("00Q1", decision="review", rule="kununu_conflict")]
    client = FakeClient(leads=[_lead()], accounts=[_account()])
    res, *_ = _run(tmp_path, preds, client)
    assert res["observed"] == 1 and res["hits"] == 0 and res["misses"] == 0


def test_unconverted_stays_pending_and_old_expires(tmp_path):
    preds = [_pred("00Q1", predicted_at="2026-07-02T09:00:00+00:00"),
             _pred("00Q2", predicted_at="2026-01-01T09:00:00+00:00")]  # > 90 days
    client = FakeClient(leads=[
        _lead("00Q1", None, IsConverted=False, ConvertedDate=None, ConvertedAccountId=None),
        _lead("00Q2", None, IsConverted=False, ConvertedDate=None, ConvertedAccountId=None)])
    res, ppath, *_ = _run(tmp_path, preds, client)
    statuses = {p.lead_id: p.status for p in load_predictions(ppath)}
    assert statuses == {"00Q1": "pending", "00Q2": "expired"}
    assert res["pending"] == 1 and res["expired"] == 1


def test_field_agreement(tmp_path):
    preds = [_pred("00Q1", payload={"KN_DBPostalCode__c": "10115", "KN_DBCity__c": "Berlin",
                                    "KN_Employee__c": "51-100"},
                   lf_website="https://acme.de")]
    client = FakeClient(
        leads=[_lead(KN_Employee__c="101-200")],
        accounts=[_account(Website="www.acme.de/jobs", BillingPostalCode="10 115",
                           BillingCity="berlin")])
    res, _, rpath, _ = _run(tmp_path, preds, client)
    import json
    rec = json.loads(rpath.read_text(encoding="utf-8").splitlines()[0])
    f = rec["fields"]
    assert f["postal_code"]["status"] == "agree"
    assert f["city"]["status"] == "agree"
    assert f["website"]["status"] == "agree"
    assert f["employee_bin"]["status"] == "differ"
    assert f["country"]["status"] == "lf_empty"


def test_xlsx_written(tmp_path):
    preds = [_pred("00Q1")]
    client = FakeClient(leads=[_lead()], accounts=[_account()])
    _, _, _, xpath = _run(tmp_path, preds, client)
    from openpyxl import load_workbook
    wb = load_workbook(xpath)
    assert set(wb.sheetnames) == {"Decisions", "Fields", "Summary"}
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_shadow_compare.py -q` — Expected: FAIL (no module)

- [ ] **Step 3: Implement.** Create `src/leadauto/shadow_compare.py`:

```python
"""Shadow comparison: score pending dry-run predictions against human conversions.

Reads `state/predictions.jsonl`, queries the org for the predicted leads' conversion
status, classifies each now-converted lead (decision HIT/MISS, review rows only
observed), scores field-level enrichment agreement, appends results to
`state/shadow_results.jsonl`, marks predictions resolved/expired, and writes a
human-readable `shadow_comparison.xlsx` (Decisions / Fields / Summary).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .logging import get_logger
from .predictions import DEFAULT_PREDICTIONS_PATH, load_predictions, save_predictions
from .qa_artifact import _add_sheet
from .sf import soql
from .state import REPO_ROOT, _parse_sf_dt
from .textnorm import normalize_city, normalize_host, normalize_postal

DEFAULT_RESULTS_PATH = REPO_ROOT / "state" / "shadow_results.jsonl"
DEFAULT_XLSX_PATH = REPO_ROOT / "shadow_comparison.xlsx"
_EXPIRE_DAYS = 90

DECISIONS_COLUMNS = [
    "lead_id", "rule", "decision", "predicted_account_id",
    "converted_account_id", "converted_account_name", "account_created",
    "converted_date", "result", "detail",
]
FIELDS_COLUMNS = ["lead_id", "field", "status", "lf_value", "human_value"]


def _norm_country(v):
    return str(v).strip().upper() or None if v else None


def _norm_text(v):
    return (str(v).strip() or None) if v is not None else None


def _norm_revenue(v):
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return None


# (label, payload key, source record, org field, normalizer)
_FIELD_CHECKS = [
    ("postal_code", "KN_DBPostalCode__c", "account", "BillingPostalCode", normalize_postal),
    ("city", "KN_DBCity__c", "account", "BillingCity", normalize_city),
    ("country", "KN_DBISO2CountryCode__c", "lead", "KN_DBISO2CountryCode__c", _norm_country),
    ("employee_bin", "KN_Employee__c", "lead", "KN_Employee__c", _norm_text),
    ("revenue_millions", "KN_DBRevenueinMillions__c", "lead", "KN_DBRevenueinMillions__c",
     _norm_revenue),
]


def _verdict(ours_raw, theirs_raw, norm) -> dict:
    ours, theirs = norm(ours_raw), norm(theirs_raw)
    if ours is None:
        status = "lf_empty"
    elif theirs is None:
        status = "human_empty"
    else:
        status = "agree" if ours == theirs else "differ"
    return {"status": status, "lf": ours_raw, "human": theirs_raw}


def compare_fields(pred, lead: dict, account: dict | None) -> dict:
    out = {}
    for label, pkey, source, okey, norm in _FIELD_CHECKS:
        rec = (account or {}) if source == "account" else (lead or {})
        out[label] = _verdict(pred.payload.get(pkey), rec.get(okey), norm)
    out["website"] = _verdict(pred.lf_website, (account or {}).get("Website"), normalize_host)
    return out


def _is_new_account(account: dict | None, converted_date_raw) -> bool:
    """Salesforce creates the Account at conversion time, so a human 'create new'
    shows as Account.CreatedDate on the conversion day (ConvertedDate is DATE-only)."""
    if not account or not account.get("CreatedDate") or not converted_date_raw:
        return False
    created = _parse_sf_dt(account["CreatedDate"])
    raw = str(converted_date_raw).strip()
    if "T" in raw:
        return abs(created - _parse_sf_dt(raw)) <= timedelta(hours=1)
    return created.date().isoformat() == raw


def classify_decision(pred, lead: dict, account: dict | None) -> tuple[str, str]:
    """(result, detail) — result: hit | miss | observed."""
    converted_account = lead.get("ConvertedAccountId")
    if pred.decision == "link":
        if converted_account == pred.account_id:
            return "hit", "linked the predicted account"
        return "miss", f"human chose {converted_account}, predicted {pred.account_id}"
    if pred.decision == "create_new":
        if _is_new_account(account, lead.get("ConvertedDate")):
            return "hit", "human also created a new account"
        return "miss", f"human linked pre-existing {converted_account}"
    kind = "new account" if _is_new_account(account, lead.get("ConvertedDate")) \
        else f"existing account {converted_account}"
    return "observed", f"review case; human resolved to {kind}"


def run_shadow_compare(sf_client, *, predictions_path=None, results_path=None,
                       xlsx_path=None, now=None, logger=None) -> dict:
    log = logger or get_logger("shadow")
    ppath = Path(predictions_path) if predictions_path else DEFAULT_PREDICTIONS_PATH
    rpath = Path(results_path) if results_path else DEFAULT_RESULTS_PATH
    xpath = Path(xlsx_path) if xlsx_path else DEFAULT_XLSX_PATH
    now = now or datetime.now(timezone.utc)

    preds = load_predictions(ppath)
    pending = [p for p in preds if p.status == "pending"]
    stats = {"checked": len(pending), "resolved": 0, "hits": 0, "misses": 0,
             "observed": 0, "expired": 0, "pending": 0, "xlsx": str(xpath)}
    if not pending:
        log.info("Shadow compare: no pending predictions")
        return stats

    leads = sf_client.query(soql.leads_shadow_status([p.lead_id for p in pending]))
    lead_by_id = {l["Id"]: l for l in leads}
    acct_ids = sorted({l["ConvertedAccountId"] for l in leads if l.get("ConvertedAccountId")})
    accounts = sf_client.query(soql.accounts_by_ids(acct_ids)) if acct_ids else []
    acct_by_id = {a["Id"]: a for a in accounts}

    new_results: list[dict] = []
    for pred in pending:
        lead = lead_by_id.get(pred.lead_id)
        if not lead or not lead.get("IsConverted"):
            if now - _parse_sf_dt(pred.predicted_at) > timedelta(days=_EXPIRE_DAYS):
                pred.status = "expired"
                stats["expired"] += 1
            else:
                stats["pending"] += 1
            continue
        account = acct_by_id.get(lead.get("ConvertedAccountId"))
        result, detail = classify_decision(pred, lead, account)
        fields = compare_fields(pred, lead, account)
        record = {
            "lead_id": pred.lead_id, "resolved_at": now.isoformat(),
            "rule": pred.rule, "decision": pred.decision,
            "predicted_account_id": pred.account_id,
            "converted_account_id": lead.get("ConvertedAccountId"),
            "converted_account_name": (account or {}).get("Name"),
            "account_created": (account or {}).get("CreatedDate"),
            "converted_date": lead.get("ConvertedDate"),
            "result": result, "detail": detail, "fields": fields,
        }
        new_results.append(record)
        pred.status = "resolved"
        pred.resolved_at = now.isoformat()
        pred.outcome = {"result": result, "detail": detail}
        stats["resolved"] += 1
        stats[{"hit": "hits", "miss": "misses", "observed": "observed"}[result]] += 1

    save_predictions(preds, ppath)
    if new_results:
        rpath.parent.mkdir(parents=True, exist_ok=True)
        with rpath.open("a", encoding="utf-8") as fh:
            for rec in new_results:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    _write_xlsx(new_results, rpath, stats, now, xpath)
    log.info("Shadow compare: %d resolved (%d hit / %d miss / %d observed), "
             "%d pending, %d expired",
             stats["resolved"], stats["hits"], stats["misses"], stats["observed"],
             stats["pending"], stats["expired"])
    return stats


def _load_all_results(rpath: Path) -> list[dict]:
    if not rpath.exists():
        return []
    return [json.loads(line) for line in rpath.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def _write_xlsx(new_results, rpath, stats, now, xpath) -> None:
    from openpyxl import Workbook

    wb = Workbook()
    wb.remove(wb.active)
    decisions_rows = [[r[c] for c in ("lead_id", "rule", "decision", "predicted_account_id",
                                      "converted_account_id", "converted_account_name",
                                      "account_created", "converted_date", "result", "detail")]
                      for r in new_results]
    _add_sheet(wb, "Decisions", DECISIONS_COLUMNS, decisions_rows)

    field_rows = [[r["lead_id"], label, f["status"], str(f["lf"] or ""), str(f["human"] or "")]
                  for r in new_results for label, f in r["fields"].items()]
    _add_sheet(wb, "Fields", FIELDS_COLUMNS, field_rows)

    # Rolling summary over the FULL results history.
    history = _load_all_results(rpath)
    summary = [["generated_at", now.isoformat()],
               ["resolved_this_run", stats["resolved"]],
               ["still_pending", stats["pending"]],
               ["total_scored_ever", len(history)]]
    by_rule: dict[str, dict] = {}
    field_agg: dict[str, dict] = {}
    for r in history:
        b = by_rule.setdefault(r["rule"], {"hit": 0, "miss": 0, "observed": 0})
        b[r["result"]] += 1
        for label, f in r.get("fields", {}).items():
            fa = field_agg.setdefault(label, {"agree": 0, "differ": 0,
                                              "human_empty": 0, "lf_empty": 0})
            fa[f["status"]] += 1
    for rule, b in sorted(by_rule.items()):
        scored = b["hit"] + b["miss"]
        acc = f"{b['hit'] / scored:.0%}" if scored else "n/a"
        summary.append([f"rule_{rule}", f"hit={b['hit']} miss={b['miss']} "
                                        f"observed={b['observed']} accuracy={acc}"])
    for label, fa in sorted(field_agg.items()):
        comparable = fa["agree"] + fa["differ"]
        rate = f"{fa['agree'] / comparable:.0%}" if comparable else "n/a"
        summary.append([f"field_{label}", f"agree={fa['agree']} differ={fa['differ']} "
                                          f"human_empty={fa['human_empty']} "
                                          f"lf_empty={fa['lf_empty']} rate={rate}"])
    _add_sheet(wb, "Summary", ["metric", "value"], summary)
    xpath.parent.mkdir(parents=True, exist_ok=True)
    wb.save(xpath)
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_shadow_compare.py -q` — Expected: PASS. Then `python -m pytest -q` — full suite PASS.

- [ ] **Step 5: Commit**

```bash
git add src/leadauto/shadow_compare.py tests/test_shadow_compare.py
git commit -m "feat(shadow): compare dry-run predictions against human conversions"
```

---

### Task 9: CLI command + CI wiring

**Files:**
- Modify: `src/leadauto/cli.py`, `.github/workflows/stage1.yml`
- Test: `tests/test_cli.py` (append)

**Interfaces:**
- Produces: `python -m leadauto shadow-compare [--config P]`; stage1.yml runs it after auth and before Stage 1, uploads `shadow_comparison.xlsx`, and commits `state/predictions.jsonl` + `state/shadow_results.jsonl` on every run (watermark still only on non-dry runs).

- [ ] **Step 1: Write the failing test** (append to `tests/test_cli.py`, following its existing monkeypatch style for stage1/stage2 commands):

```python
def test_shadow_compare_command(monkeypatch):
    import leadauto.cli as cli
    monkeypatch.setattr(cli, "_build_sf_client", lambda s, log: object())
    monkeypatch.setattr(cli, "run_shadow_compare", lambda sf, logger: {
        "checked": 3, "resolved": 2, "hits": 1, "misses": 1, "observed": 0,
        "expired": 0, "pending": 1, "xlsx": "shadow_comparison.xlsx"})
    result = CliRunner().invoke(cli.app, ["shadow-compare"])
    assert result.exit_code == 0
    assert "resolved=2" in result.output and "hits=1" in result.output
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_cli.py -q` — Expected: FAIL (no such command)

- [ ] **Step 3: Implement.** In `cli.py` add `from .shadow_compare import run_shadow_compare` and:

```python
@app.command("shadow-compare")
def shadow_compare(
    config: Optional[Path] = typer.Option(None, "--config", help="Alternate config.yaml."),
) -> None:
    """Score pending dry-run predictions against human conversions in the org."""
    log = get_logger("cli.shadow")
    settings = Settings.load(config)
    sf = _build_sf_client(settings, log)
    res = run_shadow_compare(sf, logger=log)
    typer.echo(
        f"Shadow compare done: checked={res['checked']} resolved={res['resolved']} "
        f"hits={res['hits']} misses={res['misses']} observed={res['observed']} "
        f"expired={res['expired']} pending={res['pending']} | xlsx={res['xlsx']}"
    )
```

In `.github/workflows/stage1.yml`:

1. After the "Authenticate to Salesforce" step, insert:

```yaml
      - name: Shadow compare (score past dry-run predictions)
        continue-on-error: true   # a compare hiccup must not block enrichment
        env:
          PYTHONPATH: src
        run: python -m leadauto shadow-compare

      - name: Upload shadow comparison
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: lead-automation-shadow-${{ github.run_id }}
          path: shadow_comparison.xlsx
          if-no-files-found: ignore
```

2. Replace the "Commit advanced watermark" step with:

```yaml
      - name: Commit state (shadow files always; watermark only on live runs)
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"
          for f in state/predictions.jsonl state/shadow_results.jsonl; do
            [ -f "$f" ] && git add "$f"
          done
          if [ "${{ needs.gate.outputs.dry_run }}" != "true" ]; then
            git add state/watermark.json
          fi
          if git diff --cached --quiet; then
            echo "No state changes to commit."
          else
            git commit -m "chore: update lead-automation state [skip ci]"
            git push
          fi
```

(Note the step-level `if:` guard is GONE — the step now runs on every run; the dry-run condition moved inside for the watermark only.)

- [ ] **Step 4: Run the full suite + YAML sanity**

Run: `python -m pytest -q` — Expected: PASS.
Run: `python -c "import yaml; yaml.safe_load(open('.github/workflows/stage1.yml', encoding='utf-8'))"` — Expected: no error.

- [ ] **Step 5: Commit**

```bash
git add src/leadauto/cli.py .github/workflows/stage1.yml tests/test_cli.py
git commit -m "feat(cli+ci): shadow-compare command wired into the scheduled stage-1 run"
```

---

### Task 10: Final verification, docs, PR

**Files:**
- Modify: `CLAUDE.md` (status bullet + next steps)

- [ ] **Step 1: Full suite**

Run: `python -m pytest -q` — Expected: ALL PASS. Record the new test count.

- [ ] **Step 2: End-to-end dry sanity (no credits, no writes)**

Run: `$env:PYTHONPATH = "src"; python -m leadauto stage1 --dry-run --no-leadfeeder --limit 5 --qa-path out/qa_smoke.xlsx`
Expected: completes; every lead shows `decision=review`, `match_rule=no_lf_match` (no LF client → rule 1 by design); `state/predictions.jsonl` gains pending rows. **Then revert the predictions file** so real CI predictions start clean: `git checkout -- state/predictions.jsonl` (or `git clean` if untracked).

- [ ] **Step 3: Update CLAUDE.md**

Add a status bullet (top of "Current status" additions) summarizing: rule engine replaces thresholds (8 rules, spec link), thresholds removed from config, QA artifact rule/evidence columns, prediction log + shadow-compare command + CI wiring, and the invariant (no-LF ⇒ review). Update "Next steps" to add: "read the rule distribution of the next scheduled dry-run QA artifact" and "review shadow accuracy after a few days of human conversions".

- [ ] **Step 4: Commit + PR**

```bash
git add CLAUDE.md
git commit -m "docs: record Dublettencheck rule engine + shadow comparison status"
git push -u origin dublettencheck-rules
gh pr create --title "Dublettencheck rule engine + shadow comparison (dry-run)" --body "..."
```

PR body: summarize the rule table, config key changes (removed thresholds — call this out for the reviewer), shadow files committed by CI, and that scheduled runs stay forced-dry. End with the standard generation footer.

---

## Self-Review Notes (already applied)

- Spec §1–§8 each map to Tasks 4/2/–/6/5/7/8/10 respectively; contact side (§3) intentionally has no task (unchanged) except the stage-2 gate move forced by threshold removal (Task 5) — behavior-identical.
- `Lead.ConvertedDate` date-only handling contradicted the spec's "±1 h"; resolved as same-day with a datetime fallback (Task 8, `_is_new_account`).
- `stats` key bug called out explicitly in Task 8 Step 3 NOTE.
- Type checks: `Prediction` fields used in Task 8 (`payload`, `lf_website`, `predicted_at`, `status`, `outcome`) all defined in Task 7; `RULE_DECISIONS` consumed by Task 6 import; `normalize_postal/normalize_city` (Task 1) consumed by Tasks 4/8; `alt_names` (Task 3) consumed by Task 4's `_kununu_name_agreement`.
