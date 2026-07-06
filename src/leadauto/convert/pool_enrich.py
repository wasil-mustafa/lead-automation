"""Stage 2 pool-Account enrichment (spec: docs/superpowers/specs/
2026-07-06-pool-account-enrichment-and-batch-dedup-design.md).

`account_update` is pure: given a linked Account row, the lead's enrichment row, and the
lead's kununu identity (carried from the approved CSV), it returns either a conflict reason
(route the lead to review) or the Account overwrite payload. Kununu profile URL/UUID are a
match-validity gate: blank -> fill, equal -> no-op, present-and-different -> conflict.
`resolve_pool_links` (added in Task 6) does the I/O.
"""
from __future__ import annotations

from ..logging import get_logger
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
