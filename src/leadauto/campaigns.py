"""LeadFeeder-outcome campaign tagging (Wasil, 2026-10-19).

On a NON-dry Stage 1 run every processed lead — live scope or not — becomes a
CampaignMember of exactly one of three campaigns (config `pipeline.lf_campaigns`), so
sales can work the leads by how LeadFeeder did:

- `unmatched`           no accepted LF match (`EnrichedLead.matched` is False, which
                        includes leads the post-retrieve sanity gate rejected)
- `matched_incomplete`  LF matched, but the Lead's final `KN_Employee__c` or `Street`
                        is still blank
- `matched_complete`    LF matched and both are filled

"Final" means what the Lead holds after this run: the write-back payload over the
existing value for a lead that was actually written, the existing value alone for every
other lead (out of live scope, or a write-back failure). `Street` is the Lead field that
becomes the Account's BillingStreet on conversion.

Leads already in any of the campaigns are excluded from later runs by an anti-join on
the lead read (`soql.leads_by_ids`), so a lead is tagged once. Pure functions, no I/O.
"""
from __future__ import annotations

from collections import Counter
from typing import Iterable, Sequence

MATCHED_COMPLETE = "matched_complete"
MATCHED_INCOMPLETE = "matched_incomplete"
UNMATCHED = "unmatched"
BUCKETS = (MATCHED_COMPLETE, MATCHED_INCOMPLETE, UNMATCHED)


def _filled(value) -> bool:
    # Bulk reads deliver nulls as '' — a blank string is not a value.
    return bool(str(value).strip()) if value is not None else False


def final_lead_fields(enriched, payload: dict | None, written: bool) -> tuple:
    """(KN_Employee__c, Street) as the Lead holds them after this run."""
    e = enriched.extracted
    employee, street = e.lead_employee_category, e.lead_street
    if written and payload:
        employee = payload.get("KN_Employee__c") or employee
        street = payload.get("Street") or street
    return employee, street


def campaign_bucket(result, payload: dict | None, written: bool) -> str:
    """The campaign bucket for one MatchResult (see the module docstring)."""
    if not result.enriched.matched:
        return UNMATCHED
    employee, street = final_lead_fields(result.enriched, payload, written)
    return MATCHED_COMPLETE if _filled(employee) and _filled(street) else MATCHED_INCOMPLETE


def campaign_members(results: Sequence, payloads: Sequence[dict | None],
                     written_ids: Iterable[str], campaigns: dict[str, str]
                     ) -> tuple[list[dict], Counter]:
    """CampaignMember rows ({CampaignId, LeadId}) for every result, one per lead.

    `payloads` is index-aligned with `results`; `campaigns` maps bucket -> CampaignId
    (`Settings.lf_campaigns`) and a bucket without an id is not tagged. Status is left
    to the campaign's default member status. Returns (rows, per-bucket counts).
    """
    written = set(written_ids)
    rows: list[dict] = []
    counts: Counter = Counter()
    seen: set[str] = set()
    for r, payload in zip(results, payloads):
        lead_id = r.enriched.extracted.lead_id
        if lead_id in seen:
            continue
        seen.add(lead_id)
        bucket = campaign_bucket(r, payload, lead_id in written)
        campaign_id = campaigns.get(bucket)
        if not campaign_id:
            continue
        rows.append({"CampaignId": campaign_id, "LeadId": lead_id})
        counts[bucket] += 1
    return rows, counts
