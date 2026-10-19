"""LeadFeeder-outcome campaign tagging (Wasil, 2026-10-19).

On a NON-dry Stage 1 run a lead of a tagged task type (config
`pipeline.lf_campaign_task_types`, `claimer` = "New Claimed Profile") becomes a
CampaignMember of exactly one of three campaigns (config `pipeline.lf_campaigns`), so
sales can work the leads by how LeadFeeder did:

- `unmatched`           no accepted LF match (`EnrichedLead.matched` is False, which
                        includes leads the post-retrieve sanity gate rejected)
- `matched_complete`    WRITTEN BACK this run, and the Lead's final `KN_Employee__c`
                        and `Street` are both filled
- `matched_incomplete`  written back, but either is still blank

Only those two populations are tagged: a lead LeadFeeder matched but this run did not
write back (outside the write-back scope, or a failed write-back) gets no campaign, so
it stays eligible for a later run. "Final" = the write-back payload over the Lead's
existing value. `Street` is the Lead field that becomes the Account's BillingStreet on
conversion.

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


def final_lead_fields(enriched, payload: dict | None) -> tuple:
    """(KN_Employee__c, Street) as a written-back Lead holds them after this run."""
    e = enriched.extracted
    payload = payload or {}
    return (payload.get("KN_Employee__c") or e.lead_employee_category,
            payload.get("Street") or e.lead_street)


def campaign_bucket(result, payload: dict | None, written: bool,
                    task_types: Sequence[str] | None = None) -> str | None:
    """The campaign bucket for one MatchResult, or None when it is not tagged: a task
    type outside `task_types` (None = every type), or an LF-matched lead that was not
    written back (see the module docstring)."""
    if task_types is not None and result.enriched.extracted.task_type not in task_types:
        return None
    if not result.enriched.matched:
        return UNMATCHED
    if not written:
        return None
    employee, street = final_lead_fields(result.enriched, payload)
    return MATCHED_COMPLETE if _filled(employee) and _filled(street) else MATCHED_INCOMPLETE


def campaign_members(results: Sequence, payloads: Sequence[dict | None],
                     written_ids: Iterable[str], campaigns: dict[str, str],
                     task_types: Sequence[str] | None = None
                     ) -> tuple[list[dict], Counter]:
    """CampaignMember rows ({CampaignId, LeadId}) for the taggable results, one per lead.

    `payloads` is index-aligned with `results` (only written leads' are read);
    `campaigns` maps bucket -> CampaignId (`Settings.lf_campaigns`) and a bucket without
    an id is not tagged. Status is left to the campaign's default member status.
    Returns (rows, per-bucket counts).
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
        bucket = campaign_bucket(r, payload, lead_id in written, task_types)
        campaign_id = campaigns.get(bucket) if bucket else None
        if not campaign_id:
            continue
        rows.append({"CampaignId": campaign_id, "LeadId": lead_id})
        counts[bucket] += 1
    return rows, counts
