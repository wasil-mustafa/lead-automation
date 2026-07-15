"""Extraction reader: live Tasks/Leads -> normalized ExtractedLead records.

Flow (the SOQL gotchas are why it's two queries, Tasks first):
  1. Query in-scope Tasks created since the watermark (oldest first).
  2. Drop tasks already processed (idempotency overlap window), then dedupe to the
     newest Task per WhoId.
  3. Resolve the surviving WhoIds to unconverted Leads in one batched query.
  4. Per task: skip if its lead is missing (converted / Contact WhoId) or already
     enriched; otherwise route through `extract_lead`.

The org boundary is the injected `client` (an `SfClient`); everything else here is pure
and unit-tested without a live org.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..logging import get_logger
from ..sf import soql
from ..textnorm import is_internal_email
from .models import ExtractedLead
from .parsers import extract_lead


@dataclass
class Skipped:
    """A task that reached the reader but was intentionally not extracted."""
    task_id: str
    lead_id: str
    subject: str
    reason: str


@dataclass
class ExtractionResult:
    leads: list[ExtractedLead] = field(default_factory=list)
    skipped: list[Skipped] = field(default_factory=list)
    # every task accounted for this run (after dedupe), used to advance the watermark
    # regardless of whether it produced a lead or was skipped.
    processed_tasks: list[dict] = field(default_factory=list)


def newest_task_per_who(tasks: list[dict]) -> list[dict]:
    """Keep only the newest Task (by CreatedDate string) per WhoId; drop ones with no WhoId.

    CreatedDate is the Salesforce ISO 8601 form, which sorts correctly as a string.
    """
    by_who: dict[str, dict] = {}
    for t in tasks:
        who = t.get("WhoId")
        if not who:
            continue
        current = by_who.get(who)
        if current is None or (t.get("CreatedDate") or "") > (current.get("CreatedDate") or ""):
            by_who[who] = t
    return list(by_who.values())


def is_already_enriched(lead: dict) -> bool:
    """KN_KununuLinkProfile__c doubles as the 'already processed' marker (it is always
    part of the write-back). KN_KununuProfileName__c still counts: it was the previous
    marker and a human-enriched lead carrying it needs no automation either way."""
    return bool((lead.get("KN_KununuLinkProfile__c") or "").strip()
                or (lead.get("KN_KununuProfileName__c") or "").strip())


def read_extracted_leads(
    client,
    settings,
    since: str,
    processed_task_ids=None,
    limit: int = 0,
    logger=None,
) -> ExtractionResult:
    """Read in-scope Tasks since `since` and return extracted leads + skip reasons."""
    log = logger or get_logger("extract.reader")
    processed = set(processed_task_ids or [])
    subjects = settings.in_scope_subjects
    if not subjects:
        log.info("No enabled task subjects in scope; nothing to extract.")
        return ExtractionResult()

    raw_tasks = client.query(
        soql.tasks_since(since, subjects, settings.fresh_task_statuses, limit=limit)
    )
    log.info("Fetched %d in-scope task(s) since %s", len(raw_tasks), since)

    fresh = [t for t in raw_tasks if t.get("Id") not in processed]
    dropped = len(raw_tasks) - len(fresh)
    if dropped:
        log.info("Skipped %d already-processed task(s) in the overlap window", dropped)
    tasks = newest_task_per_who(fresh)
    if not tasks:
        return ExtractionResult()

    who_ids = sorted({t["WhoId"] for t in tasks})
    leads_raw = client.query(soql.leads_by_ids(who_ids))
    lead_map = {l["Id"]: l for l in leads_raw}
    log.info("Resolved %d/%d WhoId(s) to unconverted queue-owned lead(s)", len(lead_map), len(who_ids))

    result = ExtractionResult(
        processed_tasks=[{"Id": t.get("Id"), "CreatedDate": t.get("CreatedDate")} for t in tasks]
    )
    for task in tasks:
        who = task.get("WhoId")
        tid = task.get("Id", "")
        subject = task.get("Subject", "")
        lead = lead_map.get(who)
        if lead is None:
            result.skipped.append(Skipped(tid, who or "", subject,
                                          "lead not found or already converted"))
            continue
        if not settings.include_enriched_leads and is_already_enriched(lead):
            result.skipped.append(Skipped(tid, who, subject,
                                          "lead already enriched (kununu marker field set)"))
            continue
        extracted = extract_lead(task, lead, settings)
        if extracted is None:
            result.skipped.append(Skipped(tid, who, subject, "subject out of scope"))
            continue
        internal = settings.excluded_email_domains
        if is_internal_email(extracted.lead_email, internal) or \
                is_internal_email(extracted.contact_email, internal):
            result.skipped.append(Skipped(tid, who, subject,
                                          "internal company email domain"))
            continue
        result.leads.append(extracted)

    log.info("Extracted %d lead(s); skipped %d", len(result.leads), len(result.skipped))
    return result
