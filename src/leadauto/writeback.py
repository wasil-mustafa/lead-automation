"""Module 3b — write LeadFeeder/kununu enrichment back to existing Lead KN_* fields.

The mapping is pure (`lead_field_payload`): an `EnrichedLead` becomes an Id-keyed dict of
only the fields we actually have a value for (empty values are omitted so a partial
enrichment never blanks an existing field). `write_back` then pushes them via the Bulk API
in one declarative, partial-success call — short-circuited entirely under `dry_run`.

`KN_KununuProfileName__c` doubles as the "already processed" marker (see reader).

Org-verified 2026-07-03 (read-only describe + data sampling on `headless`):
  * `KN_Employee__c` is a RESTRICTED picklist; `_EMPLOYEE_BINS` mirrors its 14 labels
    verbatim (German dot separators). `KN_Region__c` is also restricted — the parsers
    only emit Germany/Austria/Switzerland (or None), all valid.
  * `KN_KununuProfileClaimed__c` is a real checkbox — the Bulk API CSV accepts the
    "true"/"false" strings written here.
  * `KN_DB*` fields are auto-populated by the org's D&B integration and are never
    written here (Wasil, 2026-07-05); conversion-requirement fields are fill-only-blank.
"""
from __future__ import annotations

import re
from datetime import date

from .leadfeeder.enrich import EnrichedLead
from .logging import get_logger

# (upper_bound, label); counts above the last bound map to "> 100.000".
# Labels = the org's restricted KN_Employee__c picklist verbatim (describe on `headless`,
# 2026-07-03), incl. the German dot thousands separators — any other value is rejected.
_EMPLOYEE_BINS: list[tuple[int, str]] = [
    (10, "1-10"), (50, "11-50"), (100, "51-100"), (200, "101-200"),
    (350, "201-350"), (500, "351-500"), (750, "501-750"), (1000, "751-1.000"),
    (2500, "1.001-2.500"), (5000, "2.501-5.000"), (10000, "5.001-10.000"),
    (30000, "10.001-30.000"), (100000, "30.001-100.000"),
]

# All valid KN_Employee__c labels (the bins plus the open-ended top band).
_EMPLOYEE_LABELS: frozenset[str] = frozenset(label for _, label in _EMPLOYEE_BINS) | {"> 100.000"}


def employee_bin(count) -> str | None:
    """Map an employee headcount onto the KN_Employee__c picklist band. None if absent."""
    try:
        n = int(count)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    for upper, label in _EMPLOYEE_BINS:
        if n <= upper:
            return label
    return "> 100.000"


def normalize_phone(value) -> str | None:
    """Normalize to the org's +<digits> phone format; None when not confidently possible.

    Strips spaces/dashes/dots/parens/slashes, converts a 00 international prefix to +,
    and accepts only + followed by 6-15 digits (KN_ValidatePhone / KN_ContactValidPhoneNumber
    reject everything else). National 0... numbers are dropped: guessing a country is worse
    than leaving the field blank.
    """
    if not value:
        return None
    s = re.sub(r"[\s\-.()/]", "", str(value).strip())
    if s.startswith("00"):
        s = "+" + s[2:]
    return s if re.fullmatch(r"\+[0-9]{6,15}", s) else None


def split_first_name(source_name, last_name) -> str | None:
    """Leading remainder of `source_name` when it ends with the Lead's `last_name`.

    Guard against writing a different person's name: no last-name agreement, no split.
    """
    if not source_name or not last_name:
        return None
    src, ln = str(source_name).strip(), str(last_name).strip()
    # Whole trailing word only: " Bauer" must not match inside "Neubauer". This also
    # rejects source == lastname (no first name to split off).
    if not ln or not src.lower().endswith(" " + ln.lower()):
        return None
    first = src[: len(src) - len(ln)].strip()
    return first or None


def employee_bin_from_size(size) -> str | None:
    """Map a parsed kununu company-size string onto the KN_Employee__c picklist.

    Exact label match first; otherwise the range's upper bound (German dot separators
    stripped) goes through employee_bin(). Used only as a fill-blank fallback when
    LeadFeeder has no Employee_Count.
    """
    if not size:
        return None
    s = str(size).strip()
    if s in _EMPLOYEE_LABELS:
        return s
    nums = re.findall(r"\d[\d.]*", s)
    if not nums:
        return None
    try:
        upper = int(nums[-1].replace(".", ""))
    except ValueError:
        return None
    return employee_bin(upper)


def _iso2(value) -> str | None:
    if not value:
        return None
    v = str(value).strip()
    return v.upper() if len(v) == 2 else v


def _put(payload: dict, field: str, value) -> None:
    """Set a field only when there is a real value (skip None / empty string)."""
    if value is None:
        return
    if isinstance(value, str) and not value.strip():
        return
    payload[field] = value


_POSTAL_LEN_BY_CC = {"DE": 5, "CH": 4}  # KN_Checklengthpostalcode (Austria unchecked)


def _blank(value) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _fill(payload: dict, field: str, current, value) -> None:
    """_put, but only when the Lead's current value is blank (fill-only-blank policy)."""
    if _blank(current):
        _put(payload, field, value)


def lead_field_payload(enriched: EnrichedLead) -> dict:
    """Build the Id-keyed write-back dict for one enriched Lead (only populated fields)."""
    e = enriched.extracted
    company = enriched.company or {}
    payload: dict = {"Id": e.lead_id}

    # kununu signals (from the parsed Task)
    _put(payload, "KN_KununuProfileName__c", e.company_name)  # processed marker
    _put(payload, "KN_KununuLinkProfile__c", e.kununu_url)
    _put(payload, "KN_KununuUserUUID__c", e.kununu_user_uuid)
    _put(payload, "KN_Region__c", e.region)
    if e.profile_type:
        _put(payload, "KN_KununuProfileClaimed__c",
             "true" if e.profile_type.strip().lower() == "claimed" else "false")
    if isinstance(e.claimed_at, date):
        _put(payload, "KN_KununuProfileClaimedAt__c", e.claimed_at.isoformat())

    # firmographics (LeadFeeder) — KN_DB* fields are D&B-integration-owned, never written
    emp = employee_bin(company.get("Employee_Count"))
    if emp:
        _put(payload, "KN_Employee__c", emp)
    else:
        _fill(payload, "KN_Employee__c", e.lead_employee_category,
              employee_bin_from_size(e.company_size))
    _put(payload, "KN_VATNumber__c", company.get("VAT_Id") or e.vat_number)
    _put(payload, "KN_LinkedInProfileLink__c", company.get("Company_LinkedIn"))
    _put(payload, "KN_CompanyEmail__c", company.get("Company_Email"))

    # conversion requirements (org validation rules; all fill-only-blank)
    if _blank(e.lead_dbid) and not e.lead_no_own_dbid:
        payload["KN_NoOwnDBID__c"] = "true"  # KN_DbIdValidation XOR
    contact = enriched.contact or {}
    street = e.street or company.get("Street")
    _fill(payload, "Street", e.lead_street, (street or "")[:100] or None)
    _fill(payload, "City", e.lead_city, e.city or company.get("City"))
    country = (e.country_code or _iso2(company.get("Country")) or "").upper()
    if _blank(e.lead_country):
        _fill(payload, "CountryCode", e.lead_country_code, country or None)
    postal = e.postal_code or company.get("Postal_Code")
    effective_cc = ((e.lead_country_code or country) or "").upper()
    expected_len = _POSTAL_LEN_BY_CC.get(effective_cc)
    if postal and (expected_len is None or len(str(postal).strip()) == expected_len):
        _fill(payload, "PostalCode", e.lead_postal_code, str(postal).strip())
    _fill(payload, "FirstName", e.lead_first_name,
          split_first_name(e.contact_name or contact.get("full_name"), e.lead_last_name))
    _fill(payload, "Phone", e.lead_phone, normalize_phone(contact.get("best_contact_phone")))

    return payload


def write_back(client, enriched_leads: list[EnrichedLead], dry_run: bool = False, logger=None) -> dict:
    """Write enrichment back to Leads. Returns {processed, payloads}; no call under dry_run.

    Leads whose payload carries only the Id (nothing enriched) are skipped — there is
    nothing to write and we never want to issue an empty update.
    """
    log = logger or get_logger("writeback")
    payloads = [lead_field_payload(e) for e in enriched_leads]
    writable = [p for p in payloads if len(p) > 1]  # more than just {"Id": ...}

    if dry_run:
        log.info("[dry-run] would write back %d/%d lead(s)", len(writable), len(payloads))
        return {"processed": 0, "failed": 0, "failures": [], "payloads": payloads}

    if not writable:
        log.info("Nothing to write back (%d lead(s) had no enrichment)", len(payloads))
        return {"processed": 0, "failed": 0, "failures": [], "payloads": payloads}

    res = client.update_bulk("Lead", writable)
    failures = [{"lead_id": f.get("id"), "stage": "writeback", "error": f.get("error", "")}
                for f in res.get("failures", [])]
    failed = res.get("failed", 0)
    if failed or not res.get("confirmed", True):
        log.warning("Write-back: %d row failure(s); result confirmed=%s",
                    failed, res.get("confirmed", True))
    log.info("Wrote back %d lead(s)", len(writable))
    return {"processed": res.get("processed", len(writable)), "failed": failed,
            "failures": failures, "payloads": payloads}
