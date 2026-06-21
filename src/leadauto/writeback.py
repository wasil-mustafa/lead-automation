"""Module 3b — write LeadFeeder/kununu enrichment back to existing Lead KN_* fields.

The mapping is pure (`lead_field_payload`): an `EnrichedLead` becomes an Id-keyed dict of
only the fields we actually have a value for (empty values are omitted so a partial
enrichment never blanks an existing field). `write_back` then pushes them via the Bulk API
in one declarative, partial-success call — short-circuited entirely under `dry_run`.

`KN_KununuProfileName__c` doubles as the "already processed" marker (see reader).

NOTE — verify against the org before the first live write (org-specific values we can't
confirm without a describe / live write):
  * `KN_Employee__c` picklist labels (the bins below are the common DACH banding).
  * `KN_DBRevenueinMillions__c` unit (we assume LeadFeeder revenue is absolute currency).
  * `KN_DBISO2CountryCode__c` expects ISO-3166 alpha-2; LeadFeeder `address.country` is
    passed through (already alpha-2 in samples) and falls back to the parsed country code.
"""
from __future__ import annotations

from datetime import date

from .leadfeeder.enrich import EnrichedLead
from .logging import get_logger

# (upper_bound, label); counts above the last bound map to ">10000".
_EMPLOYEE_BINS: list[tuple[int, str]] = [
    (10, "1-10"), (50, "11-50"), (200, "51-200"), (500, "201-500"),
    (1000, "501-1000"), (5000, "1001-5000"), (10000, "5001-10000"),
]


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
    return ">10000"


def revenue_millions(value) -> float | None:
    """Convert absolute revenue to millions, rounded to 2 dp. None if absent/non-numeric."""
    if value is None or value == "":
        return None
    try:
        return round(float(value) / 1_000_000, 2)
    except (TypeError, ValueError):
        return None


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

    # firmographics (from LeadFeeder)
    _put(payload, "KN_DBLegalCompanyName__c", enriched.matched_name)
    _put(payload, "KN_DBStreetHouseNumber__c", company.get("Street"))
    _put(payload, "KN_DBPostalCode__c", company.get("Postal_Code"))
    _put(payload, "KN_DBCity__c", company.get("City"))
    _put(payload, "KN_DBISO2CountryCode__c", _iso2(company.get("Country")) or e.country_code)
    _put(payload, "KN_DBNumberofEmployees__c", company.get("Employee_Count"))
    _put(payload, "KN_Employee__c", employee_bin(company.get("Employee_Count")))
    _put(payload, "KN_DBRevenueinMillions__c", revenue_millions(company.get("Revenue")))
    _put(payload, "KN_VATNumber__c", company.get("VAT_Id") or e.vat_number)
    _put(payload, "KN_DBMainIndustryTextperWZ08__c", company.get("Industry"))
    _put(payload, "KN_LinkedInProfileLink__c", company.get("Company_LinkedIn"))
    _put(payload, "KN_CompanyEmail__c", company.get("Company_Email"))
    _put(payload, "KN_DBID__c", company.get("Register_Id"))

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
