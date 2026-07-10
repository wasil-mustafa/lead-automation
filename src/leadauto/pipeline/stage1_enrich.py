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

from ..extract.reader import Skipped, read_extracted_leads
from ..leadfeeder.enrich import EnrichedLead, enrich_leads
from ..logging import get_logger
from ..match import MatchResult, load_existing_records, match_lead
from ..predictions import DEFAULT_PREDICTIONS_PATH, record_predictions
from ..qa_artifact import write_auto_approved_csv, write_workbook
from ..state import REPO_ROOT, State, advance, compute_since, load_state, save_state
from ..writeback import write_back


@dataclass
class Stage1Result:
    results: list[MatchResult] = field(default_factory=list)
    skipped: list[Skipped] = field(default_factory=list)
    new_state: State = field(default_factory=State)
    writeback: dict = field(default_factory=dict)
    failures: list = field(default_factory=list)
    qa_path: Path | None = None
    dry_run: bool = False


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

    existing = load_existing_records(sf_client, enriched, settings, logger=log)
    results = [match_lead(e, existing, settings) for e in enriched]

    wb = write_back(sf_client, enriched, dry_run=dry_run, logger=log)
    new_state = advance(state, extraction.processed_tasks)

    failures = wb.get("failures", [])
    if failures:
        log.warning("%d write-back failure(s) recorded in the QA Failures sheet", len(failures))

    if dry_run and predictions_path is not None:
        stamp = (now or datetime.now(timezone.utc)).isoformat()
        n = record_predictions(results, wb.get("payloads", []), stamp, predictions_path)
        log.info("Recorded %d pending prediction(s) -> %s", n, predictions_path)

    qa_out: Path | None = None
    if qa_path is not None:
        meta = {
            "generated_at": (now or datetime.now(timezone.utc)).isoformat(),
            "since": since,
            "new_watermark": new_state.last_task_created,
            "dry_run": dry_run,
        }
        qa_out = write_workbook(results, extraction.skipped, meta, qa_path, failures=failures)
        log.info("Wrote QA artifact -> %s", qa_out)

    # Auto-approve (review_gate: false): live runs stage the link/create_new rows for
    # Stage 2 without a human touching Approve?. Write-back failures are held back —
    # their lead fields would fail the conversion validation rules anyway.
    if not dry_run and not settings.review_gate:
        failed_ids = {f.get("lead_id") for f in failures}
        auto_csv = write_auto_approved_csv(results, review_dir, exclude_lead_ids=failed_ids)
        if auto_csv:
            log.info("Auto-approved conversion CSV -> %s", auto_csv)

    if not dry_run and state_path is not None:
        save_state(new_state, state_path)
        log.info("Watermark persisted -> %s (%s)", state_path, new_state.last_task_created)

    log.info("Stage 1 done: %d lead(s), %d skipped", len(results), len(extraction.skipped))
    return Stage1Result(
        results=results, skipped=extraction.skipped, new_state=new_state,
        writeback=wb, failures=failures, qa_path=qa_out, dry_run=dry_run,
    )
