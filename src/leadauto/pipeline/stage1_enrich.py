"""Stage 1 — extract new Leads, enrich, match vs existing Accounts/Contacts, write back,
and emit the QA artifact for human review. No conversion happens here.

The watermark advances to the newest task seen this run, but is only *persisted* on a
successful, non-dry run (and by the Action's commit step). LeadFeeder is optional: pass
`lf_client=None` to run extraction + matching + QA without spending enrichment credits.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from collections import Counter

from ..campaigns import campaign_bucket, campaign_buckets, duplicate_failure_ids
from ..extract.reader import Skipped, read_extracted_leads
from ..leadfeeder.enrich import enrich_leads
from ..models import EnrichedLead, MatchResult
from ..live_scope import (campaign_gate_reason, is_wide_match, live_scope_reason,
                          writeback_reason)
from ..logging import get_logger
from ..match import load_existing_records, match_lead
from ..predictions import DEFAULT_PREDICTIONS_PATH, record_predictions
from ..sf.client import SfError
from ..qa_artifact import write_auto_approved_csv, write_workbook
from ..state import REPO_ROOT, State, advance, compute_since, load_state, save_state
from ..writeback import lead_field_payload, write_back


# Enriched leads per load_existing_records call (same size the skipped-task backtest
# proved out: ~40 leads' signals stay well under the 100k-char SOQL limit).
_MATCH_CHUNK = 40


@dataclass
class Stage1Result:
    results: list[MatchResult] = field(default_factory=list)
    skipped: list[Skipped] = field(default_factory=list)
    new_state: State = field(default_factory=State)
    writeback: dict = field(default_factory=dict)
    failures: list = field(default_factory=list)
    qa_path: Path | None = None
    dry_run: bool = False
    campaigns: dict = field(default_factory=dict)  # bucket counts + created/failed


def run_stage1(
    sf_client, lf_client, settings, *,
    state: State | None = None,
    since: str | None = None,
    limit: int = 0,
    dry_run: bool | None = None,
    now: datetime | None = None,
    qa_path: str | Path | None = None,
    state_path: str | Path | None = None,
    kununu_feed: bool = True,
    predictions_path: str | Path | None = DEFAULT_PREDICTIONS_PATH,
    review_dir: str | Path = REPO_ROOT / "review",
    logger=None,
) -> Stage1Result:
    log = logger or get_logger("pipeline.stage1")
    dry_run = settings.dry_run if dry_run is None else dry_run
    state = state if state is not None else load_state(state_path)
    if since is None:
        since = compute_since(state, settings.task_lookback_days, now)
    if not limit:
        limit = settings.batch_limit

    log.info("Stage 1 start (dry_run=%s, since=%s, limit=%s)", dry_run, since, limit)
    extraction = read_extracted_leads(
        sf_client, settings, since, state.processed_task_ids, limit=limit
    )

    if lf_client is not None:
        enriched = enrich_leads(lf_client, extraction.leads, settings, logger=log,
                                use_kununu_feed=kununu_feed)
    else:
        log.info("LeadFeeder disabled; skipping enrichment")
        enriched = [EnrichedLead(extracted=e) for e in extraction.leads]

    # Chunk the match phase like backtest_skipped: a backlog-sized run pushes the
    # one-shot candidate query past Salesforce's 100k-char SOQL limit. Candidate
    # loading is per-batch by construction, so results are identical.
    results: list[MatchResult] = []
    for i in range(0, len(enriched), _MATCH_CHUNK):
        batch = enriched[i:i + _MATCH_CHUNK]
        existing = load_existing_records(sf_client, batch, settings, logger=log)
        results += [match_lead(e, existing, settings) for e in batch]

    # Live-scope gates (phased go-live, see live_scope.py): on a non-dry run only leads
    # passing the WRITE-BACK gate are written; the conversion gate (`reasons`) is
    # narrower and decides what is staged for Stage 2. Everything else stays untouched
    # in Salesforce. Dry runs write nothing but compute every payload (predictions
    # need the field values).
    reasons = [live_scope_reason(r, settings) for r in results]
    wb_reasons = [writeback_reason(r, settings) for r in results]
    written_ids: set[str] = set()
    tagging = (not dry_run and bool(settings.lf_campaigns) and bool(results)
               and lf_client is not None)
    # Conversion candidates BEFORE the write (their payload ticks KN_NoOwnDBID__c):
    # conversion scope AND, when `live_scope.convert_campaigns` is set, the campaign
    # bucket the write-back will earn (judged on the payload, as if written). With the
    # campaign axis on but tagging off (dry run, LeadFeeder disabled, no campaigns
    # configured) nothing is a candidate — the gate fails closed.
    campaign_gate = settings.live_convert_buckets is not None
    candidates: set[str] = set()
    for r, reason in zip(results, reasons):
        if reason is not None:
            continue
        if campaign_gate:
            if not tagging:
                continue
            bucket = campaign_bucket(r, lead_field_payload(r.enriched, settings), True,
                                     settings.lf_campaign_task_types)
            if campaign_gate_reason(bucket, settings) is not None:
                continue
        candidates.add(r.enriched.extracted.lead_id)
    if dry_run:
        wb = write_back(sf_client, enriched, dry_run=True, logger=log, settings=settings)
    else:
        live_enriched = [r.enriched for r, reason in zip(results, wb_reasons)
                         if reason is None]
        log.info("Live scope: %d of %d lead(s) pass the write-back gate, %d the "
                 "conversion gate, %d the campaign gate", len(live_enriched), len(results),
                 sum(1 for reason in reasons if reason is None), len(candidates))
        if campaign_gate and not tagging and any(reason is None for reason in reasons):
            log.warning("Conversion gate: live_scope.convert_campaigns is set but campaign "
                        "tagging is off this run — nothing will be staged for Stage 2")
        wide = sum(1 for r, reason in zip(results, wb_reasons)
                   if reason is None and is_wide_match(r))
        if wide:
            log.warning("Live scope: %d of those rest on a second-chance (wide) LF match",
                        wide)
        wb = write_back(sf_client, live_enriched, dry_run=False, logger=log,
                        settings=settings, convert_ids=candidates)
        failed = {f.get("lead_id") for f in wb.get("failures", [])}
        written_ids = {e.extracted.lead_id for e in live_enriched} - failed
    new_state = advance(state, extraction.processed_tasks)

    failures = list(wb.get("failures", []))
    if failures:
        log.warning("%d write-back failure(s) recorded in the QA Failures sheet", len(failures))

    campaigns: dict = {}
    tagged: dict[str, str] = {}
    if not dry_run and settings.lf_campaigns and results:
        if lf_client is None:
            # Without LeadFeeder every lead looks unmatched — tagging would mislabel the run.
            log.warning("LF campaign tagging skipped: LeadFeeder is disabled for this run")
        else:
            campaigns, tagged = _tag_campaigns(sf_client, results, written_ids, failures,
                                               settings, log)

    # What this run converts: the candidates whose write-back succeeded and — campaign
    # axis on — whose CampaignMember insert succeeded too (Wasil, 2026-10-31: convert
    # only leads that ARE in the campaign, not leads that should have been).
    # None = no campaign axis, the conversion scope alone decides (2026-07-18).
    convert_ids: set[str] | None = None
    gate_meta: dict = {}
    if campaign_gate:
        convert_ids = {lid for lid, bucket in tagged.items()
                       if lid in candidates and campaign_gate_reason(bucket, settings) is None}
        if not dry_run:
            gate_meta = {"convert_staged": len(convert_ids)}

    # Shadow predictions: on a dry run every lead is recorded (nothing was touched);
    # on a live run only leads the pipeline does NOT convert are — humans convert
    # those, so they stay valid shadow evidence, while conversion-scope leads are
    # converted by the pipeline itself and must never be scored as human decisions.
    # A lead we wrote back but left for a human still scores its account decision;
    # `written_ids` marks it so shadow-compare skips its field checks, which would
    # only re-read our own write-back off the converted records.
    if predictions_path is not None and settings.record_predictions:
        if dry_run:
            shadow_results, shadow_payloads = results, wb.get("payloads", [])
        else:
            shadow_results = [r for r, reason in zip(results, reasons)
                              if reason is not None
                              or (convert_ids is not None
                                  and r.enriched.extracted.lead_id not in convert_ids)]
            shadow_payloads = [lead_field_payload(r.enriched, settings) for r in shadow_results]
        if shadow_results:
            stamp = (now or datetime.now(timezone.utc)).isoformat()
            n = record_predictions(shadow_results, shadow_payloads, stamp, predictions_path,
                                   written_back_ids=written_ids)
            log.info("Recorded %d pending prediction(s) -> %s", n, predictions_path)

    qa_out: Path | None = None
    if qa_path is not None:
        meta = {
            "generated_at": (now or datetime.now(timezone.utc)).isoformat(),
            "since": since,
            "new_watermark": new_state.last_task_created,
            "dry_run": dry_run,
            **campaigns,
            **gate_meta,
        }
        qa_out = write_workbook(results, extraction.skipped, meta, qa_path,
                                failures=failures, settings=settings)
        log.info("Wrote QA artifact -> %s", qa_out)

    # Auto-approve (review_gate: false): live runs stage the live-scope rows for
    # Stage 2 without a human touching Approve?. Write-back failures are held back —
    # their lead fields would fail the conversion validation rules anyway.
    if not dry_run and not settings.review_gate:
        failed_ids = {f.get("lead_id") for f in failures if f.get("stage") != "campaign"}
        auto_csv = write_auto_approved_csv(results, review_dir, settings,
                                           exclude_lead_ids=failed_ids,
                                           only_lead_ids=convert_ids)
        if auto_csv:
            log.info("Auto-approved conversion CSV -> %s", auto_csv)

    if not dry_run and state_path is not None:
        save_state(new_state, state_path)
        log.info("Watermark persisted -> %s (%s)", state_path, new_state.last_task_created)

    log.info("Stage 1 done: %d lead(s), %d skipped", len(results), len(extraction.skipped))
    return Stage1Result(
        results=results, skipped=extraction.skipped, new_state=new_state,
        writeback=wb, failures=failures, qa_path=qa_out, dry_run=dry_run,
        campaigns=campaigns,
    )


def _tag_campaigns(sf_client, results, written_ids, failures, settings, log
                   ) -> tuple[dict, dict[str, str]]:
    """Add the taggable leads to their LeadFeeder-outcome campaign (campaigns.py):
    one sObject-Collections POST per <=200 leads. Taggable = a tagged task type AND
    either written back this run (`written_ids`, which already excludes write-back
    failures), unmatched in LeadFeeder, or blocked by a duplicate rule on write-back
    (-> the unmatched campaign); an untagged lead stays eligible for later
    runs. Insert failures join `failures` (QA Failures sheet, stage 'campaign') but
    never hold a lead back from the write-back, and a failed request never aborts the
    run. Returns (summary counts, lead_id -> bucket of the leads that ARE members now —
    inserted or already there; an insert failure drops the lead from the map, which is
    what the campaign conversion gate reads).
    """
    # only written leads' payloads decide a bucket; nobody else's is built
    payloads = [lead_field_payload(r.enriched, settings)
                if r.enriched.extracted.lead_id in written_ids else None for r in results]
    buckets = campaign_buckets(results, payloads, written_ids,
                               settings.lf_campaign_task_types,
                               duplicate_ids=duplicate_failure_ids(failures))
    campaign_of = {**{b: b for b in buckets.values()}, "duplicate_detected": "unmatched"}
    rows, counts = [], Counter()
    for lead_id, bucket in buckets.items():
        campaign_id = settings.lf_campaigns.get(campaign_of[bucket])
        if campaign_id:
            rows.append({"CampaignId": campaign_id, "LeadId": lead_id})
            counts[bucket] += 1
    try:
        res = sf_client.create_records("CampaignMember", rows)
    except SfError as exc:
        log.warning("LF campaign tagging failed for all %d lead(s): %s", len(rows), exc)
        res = {"created": 0, "duplicates": 0, "failed": len(rows),
               "failures": [{"record": row, "error": str(exc)[:500]} for row in rows]}
    failed_ids: set[str] = set()
    for f in res.get("failures", []):
        failed_ids.add(f["record"].get("LeadId"))
        failures.append({"lead_id": f["record"].get("LeadId"), "stage": "campaign",
                         "error": f["error"]})
    tagged = {row["LeadId"]: buckets[row["LeadId"]] for row in rows
              if row["LeadId"] not in failed_ids}
    summary = {**{f"campaign_{b}": n for b, n in sorted(counts.items())},
               "campaign_created": res.get("created", 0),
               "campaign_already_member": res.get("duplicates", 0),
               "campaign_failed": res.get("failed", 0)}
    log.info("LF campaign tagging: %s", ", ".join(f"{k}={v}" for k, v in summary.items()))
    if res.get("failed"):
        log.warning("%d campaign insert failure(s) recorded in the QA Failures sheet",
                    res["failed"])
    return summary, tagged
