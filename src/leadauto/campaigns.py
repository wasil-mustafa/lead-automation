"""LeadFeeder-outcome campaign tagging (Wasil, 2026-10-19).

On a NON-dry Stage 1 run a lead of a tagged task type (config
`pipeline.lf_campaign_task_types`: `claimer` = "New Claimed Profile", and since
2026-10-20 `ebp_trial` = "EBP Trial") becomes a
CampaignMember of exactly one of three campaigns (config `pipeline.lf_campaigns`), so
sales can work the leads by how LeadFeeder did:

- `unmatched`           no accepted LF match (`EnrichedLead.matched` is False, which
                        includes leads the post-retrieve sanity gate rejected)
- `matched_complete`    WRITTEN BACK this run, and the Lead's final `KN_Employee__c`
                        and `Street` are both filled
- `matched_incomplete`  written back, but either is still blank

Only those populations are tagged: a lead LeadFeeder matched but this run did not
write back (outside the write-back scope, or a failed write-back) gets no campaign, so
it stays eligible for a later run — EXCEPT a write-back that failed with
`DUPLICATES_DETECTED` (bucket `duplicate_detected`, Wasil 2026-10-20): a duplicate rule
blocks that lead on every rerun, so it goes to the `unmatched` campaign
("EnrichLeadmanually") for a human instead. Since 2026-10-21 the write-back sends the
duplicate-rule allowSave header (`pipeline.writeback_allow_duplicates`), so only a
Block-mode rule still lands a lead here. "Final" = the write-back payload over the Lead's
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
# Not a campaign of its own: counted separately, tagged into the `unmatched` campaign.
DUPLICATE_DETECTED = "duplicate_detected"
_CAMPAIGN_OF = {DUPLICATE_DETECTED: UNMATCHED}


def _filled(value) -> bool:
    # Bulk reads deliver nulls as '' — a blank string is not a value.
    return bool(str(value).strip()) if value is not None else False


def duplicate_failure_ids(failures: Iterable[dict]) -> set[str]:
    """Lead ids whose write-back a duplicate rule blocked (`DUPLICATES_DETECTED`)."""
    return {f.get("lead_id") for f in failures
            if f.get("stage") == "writeback" and f.get("lead_id")
            and "DUPLICATES_DETECTED" in (f.get("error") or "")}


def final_lead_fields(enriched, payload: dict | None) -> tuple:
    """(KN_Employee__c, Street) as a written-back Lead holds them after this run."""
    e = enriched.extracted
    payload = payload or {}
    return (payload.get("KN_Employee__c") or e.lead_employee_category,
            payload.get("Street") or e.lead_street)


def campaign_bucket(result, payload: dict | None, written: bool,
                    task_types: Sequence[str] | None = None,
                    duplicate: bool = False) -> str | None:
    """The campaign bucket for one MatchResult, or None when it is not tagged: a task
    type outside `task_types` (None = every type), or an LF-matched lead that was not
    written back (see the module docstring). `duplicate` = its write-back failed with
    DUPLICATES_DETECTED."""
    if task_types is not None and result.enriched.extracted.task_type not in task_types:
        return None
    if not result.enriched.matched:
        return UNMATCHED
    if duplicate:
        return DUPLICATE_DETECTED
    if not written:
        return None
    employee, street = final_lead_fields(result.enriched, payload)
    return MATCHED_COMPLETE if _filled(employee) and _filled(street) else MATCHED_INCOMPLETE


def campaign_buckets(results: Sequence, payloads: Sequence[dict | None],
                     written_ids: Iterable[str],
                     task_types: Sequence[str] | None = None,
                     duplicate_ids: Iterable[str] = ()) -> dict[str, str]:
    """lead_id -> bucket for every taggable result (first occurrence of a lead wins;
    untagged leads are absent). `payloads` is index-aligned with `results`."""
    written = set(written_ids)
    duplicates = set(duplicate_ids)
    buckets: dict[str, str] = {}
    for r, payload in zip(results, payloads):
        lead_id = r.enriched.extracted.lead_id
        if lead_id in buckets:
            continue
        bucket = campaign_bucket(r, payload, lead_id in written, task_types,
                                 duplicate=lead_id in duplicates)
        if bucket:
            buckets[lead_id] = bucket
    return buckets


def campaign_members(results: Sequence, payloads: Sequence[dict | None],
                     written_ids: Iterable[str], campaigns: dict[str, str],
                     task_types: Sequence[str] | None = None,
                     duplicate_ids: Iterable[str] = ()
                     ) -> tuple[list[dict], Counter]:
    """CampaignMember rows ({CampaignId, LeadId}) for the taggable results, one per lead.

    `payloads` is index-aligned with `results` (only written leads' are read);
    `campaigns` maps bucket -> CampaignId (`Settings.lf_campaigns`) and a bucket without
    an id is not tagged; `duplicate_ids` (`duplicate_failure_ids`) go to the `unmatched`
    campaign. Status is left to the campaign's default member status.
    Returns (rows, per-bucket counts).
    """
    rows: list[dict] = []
    counts: Counter = Counter()
    for lead_id, bucket in campaign_buckets(results, payloads, written_ids, task_types,
                                            duplicate_ids).items():
        campaign_id = campaigns.get(_CAMPAIGN_OF.get(bucket, bucket))
        if not campaign_id:
            continue
        rows.append({"CampaignId": campaign_id, "LeadId": lead_id})
        counts[bucket] += 1
    return rows, counts
