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
