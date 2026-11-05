"""The pipeline, one run (Wasil, 2026-11-01 — Stage 1 and Stage 2 fused):

    claimer / EBP-trial task appears -> Lead extracted -> LeadFeeder enrichment ->
    match vs existing Accounts/Contacts -> write-back -> LF-outcome campaign tag ->
    every lead in an auto-convert campaign bucket ("EnrichLeadfedup") is CONVERTED
    into a new Account + Contact, in the same process.

Until 2026-11-01 conversion was a separate Stage 2 fed by a committed
`review/approved_*.csv`; a lead tagged but not converted in the same CI run was then
stranded, because the reader anti-joins campaign members out of every later read.
Now the run also SWEEPS the org for unconverted members of those campaigns from
earlier runs (convert/targets.py) — nothing stays stranded.

The watermark advances to the newest task seen this run, but is only *persisted* on a
non-dry run (and by the Action's commit step) — after conversion, which never blocks
it: a conversion failure is recorded in the result and the QA workbook, and the CLI
exits non-zero only once everything is written. LeadFeeder is optional: pass
`lf_client=None` to run extraction + matching + QA without spending enrichment credits
(then nothing is tagged or converted either).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from collections import Counter

from ..campaigns import campaign_bucket, campaign_buckets, duplicate_failure_ids
from ..convert import ConversionTarget, convert_leads, enrich_contact_email2
from ..convert.pool_enrich import resolve_pool_links
from ..convert.preflight import run_preflight
from ..convert.targets import SweepResult, sweep_targets, targets_from_results
from ..extract.reader import Skipped, read_extracted_leads
from ..leadfeeder.enrich import enrich_leads
from ..models import EnrichedLead, MatchResult
from ..live_scope import (campaign_gate_reason, is_wide_match, live_scope_reason,
                          writeback_reason)
from ..logging import get_logger
from ..match import load_existing_records, match_lead
from ..predictions import (DEFAULT_PREDICTIONS_PATH, exclude_predictions,
                           record_predictions)
from ..sf.client import SfError
from ..qa_artifact import write_workbook
from ..state import State, advance, compute_since, load_state, save_state
from ..writeback import lead_field_payload, write_back


# Enriched leads per load_existing_records call (same size the skipped-task backtest
# proved out: ~40 leads' signals stay well under the 100k-char SOQL limit).
_MATCH_CHUNK = 40

PIPELINE_CONVERTED = "pipeline_converted"   # predictions.exclude_predictions reason


@dataclass
class PipelineResult:
    results: list[MatchResult] = field(default_factory=list)
    skipped: list[Skipped] = field(default_factory=list)
    new_state: State = field(default_factory=State)
    writeback: dict = field(default_factory=dict)
    failures: list = field(default_factory=list)
    qa_path: Path | None = None
    dry_run: bool = False
    campaigns: dict = field(default_factory=dict)  # bucket counts + created/failed
    targets: list[ConversionTarget] = field(default_factory=list)  # run + sweep
    conversion: dict = field(default_factory=dict)  # see _convert()
    sweep: SweepResult = field(default_factory=SweepResult)
    conversion_rows: list[dict] = field(default_factory=list)  # QA Conversion sheet

    @property
    def converted_ids(self) -> set[str]:
        """15-char ids of the leads THIS run converted (not already-converted ones)."""
        return _converted_ids(self.conversion)


def run_pipeline(
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
    convert: bool = True,
    batch_size: int | None = None,
    logger=None,
) -> PipelineResult:
    """One pipeline run. `convert=False` stops after the campaign tagging (the one-off
    event runner keeps conversion a separate, deliberate step); `batch_size` overrides
    `pipeline.convert_batch_size`."""
    log = logger or get_logger("pipeline")
    dry_run = settings.dry_run if dry_run is None else dry_run
    state = state if state is not None else load_state(state_path)
    if since is None:
        since = compute_since(state, settings.task_lookback_days, now)
    if not limit:
        limit = settings.batch_limit
    if batch_size is None:
        batch_size = settings.convert_batch_size

    log.info("Pipeline start (dry_run=%s, since=%s, limit=%s)", dry_run, since, limit)
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
    # narrower and decides what is converted. Everything else stays untouched
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
                        "tagging is off this run — none of this run's leads will convert")
        wide = sum(1 for r, reason in zip(results, wb_reasons)
                   if reason is None and is_wide_match(r))
        if wide:
            log.warning("Live scope: %d of those rest on a non-primary (wide / domain "
                        "rescue) LF match", wide)
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

    # What this run converts of its OWN leads: the candidates whose write-back
    # succeeded and — campaign axis on — whose CampaignMember insert succeeded too
    # (Wasil, 2026-10-31: convert only leads that ARE in the campaign, not leads that
    # should have been). Without the axis the conversion scope alone decides
    # (2026-07-18). A dry run converts nothing of its own (nothing was written).
    wb_failed = {f.get("lead_id") for f in failures if f.get("stage") != "campaign"}
    if dry_run:
        run_ids: set[str] = set()
    elif campaign_gate:
        run_ids = {lid for lid, bucket in tagged.items()
                   if lid in candidates and campaign_gate_reason(bucket, settings) is None}
    else:
        run_ids = candidates - wb_failed

    # Conversion: this run's leads + the sweep of earlier runs' campaign members.
    targets: list[ConversionTarget] = []
    sweep = SweepResult()
    conversion: dict = {}
    if convert:
        run_targets = targets_from_results(results, run_ids, logger=log)
        sweep = sweep_targets(
            sf_client, settings,
            exclude_ids=[r.enriched.extracted.lead_id for r in results],
            predictions_path=predictions_path, dry_run=dry_run, logger=log)
        targets = run_targets + sweep.targets
        conversion = _convert(sf_client, targets, settings, dry_run=dry_run,
                              batch_size=batch_size, overlay=_tick_overlay(sweep), log=log)
        conversion["targets_run"] = len(run_targets)
        conversion["targets_sweep"] = len(sweep.targets)
    converted = _converted_ids(conversion)

    # Shadow predictions: on a dry run every lead is recorded (nothing was touched);
    # on a live run every lead the pipeline did NOT convert is — humans convert
    # those, so they stay valid shadow evidence (a lead whose conversion failed or
    # was preflight-skipped included: the next sweep finds its decision here), while
    # our own conversions must never be scored as human decisions. A lead we wrote
    # back but left for a human still scores its account decision; `written_ids`
    # marks it so shadow-compare skips its field checks, which would only re-read our
    # own write-back off the converted records. Finally every lead converted this run
    # (own or swept) has any pending row excluded — an older row for a re-extracted
    # lead would otherwise survive.
    if predictions_path is not None and settings.record_predictions:
        stamp = (now or datetime.now(timezone.utc)).isoformat()
        if dry_run:
            shadow_results, shadow_payloads = results, wb.get("payloads", [])
        else:
            shadow_results = [r for r in results
                              if r.enriched.extracted.lead_id[:15] not in converted]
            shadow_payloads = [lead_field_payload(r.enriched, settings) for r in shadow_results]
        if shadow_results:
            n = record_predictions(shadow_results, shadow_payloads, stamp, predictions_path,
                                   written_back_ids=written_ids)
            log.info("Recorded %d pending prediction(s) -> %s", n, predictions_path)
        if converted:
            n = exclude_predictions(converted, PIPELINE_CONVERTED, predictions_path,
                                    resolved_at=stamp)
            if n:
                log.info("Excluded %d pending prediction(s) of pipeline-converted leads", n)

    conversion_rows = _conversion_rows(results, targets, sweep, conversion, dry_run)
    qa_out: Path | None = None
    if qa_path is not None:
        meta = {
            "generated_at": (now or datetime.now(timezone.utc)).isoformat(),
            "since": since,
            "new_watermark": new_state.last_task_created,
            "dry_run": dry_run,
            **campaigns,
            **_conversion_meta(conversion, sweep, conversion_rows),
        }
        qa_out = write_workbook(results, extraction.skipped, meta, qa_path,
                                failures=failures, settings=settings,
                                conversion=conversion_rows)
        log.info("Wrote QA artifact -> %s", qa_out)

    if not dry_run and state_path is not None:
        save_state(new_state, state_path)
        log.info("Watermark persisted -> %s (%s)", state_path, new_state.last_task_created)

    log.info("Pipeline done: %d lead(s), %d skipped, %d conversion target(s), %d converted",
             len(results), len(extraction.skipped), len(targets), len(converted))
    return PipelineResult(
        results=results, skipped=extraction.skipped, new_state=new_state,
        writeback=wb, failures=failures, qa_path=qa_out, dry_run=dry_run,
        campaigns=campaigns, targets=targets, conversion=conversion, sweep=sweep,
        conversion_rows=conversion_rows,
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


# --- conversion ---------------------------------------------------------------

def _empty_result(planned: int = 0) -> dict:
    return {"converted": 0, "already_converted": 0, "failed": 0, "planned": planned,
            "missing": 0, "missing_ids": [], "results": []}


def _convert(sf_client, targets, settings, *, dry_run, batch_size, overlay, log) -> dict:
    """Preflight -> pool-link gate -> SOAP conversion -> KN_Email2__c, in-process.

    Returns {targets, preflight_failures, result, error}. The whole phase is fenced:
    ANY exception (an `SfError`, a `requests` timeout out of the SOAP call, a parse
    error) lands in `error` and is logged — the run must still write its workbook and
    persist its state; the CLI turns `error`/`failed`/`missing` into a non-zero exit.
    """
    out = {"targets": len(targets), "preflight_failures": [], "result": _empty_result(),
           "error": None}
    if not targets:
        return out
    try:
        kept, pf_failures = run_preflight(sf_client, targets, logger=log, overlay=overlay)
        # Pool-account enrichment + kununu conflict gate (link targets only). Writes
        # only on live runs; conflicts are dropped from conversion and reported like
        # preflight failures.
        link_targets = [t for t in kept if t.account_id]
        other_targets = [t for t in kept if not t.account_id]
        kept_links, review_failures = resolve_pool_links(
            sf_client, link_targets, settings, dry_run=dry_run, logger=log)
        kept = kept_links + other_targets
        if review_failures:
            pf_failures.extend(review_failures)
            log.warning("Routed %d lead(s) to review (kununu conflict)", len(review_failures))
        if pf_failures:
            log.warning("Preflight skipped %d lead(s)", len({f["lead_id"] for f in pf_failures}))
        log.info("Conversion: %d target(s) -> %d after preflight (dry_run=%s, batch=%d)",
                 len(targets), len(kept), dry_run, batch_size)
        # preverified: preflight's Lead query already selects IsConverted and drops
        # converted leads, so the converter skips its idempotency pre-query (the
        # preflight->SOAP race maps back to alreadyConverted from the SOAP error).
        result = convert_leads(sf_client, kept, settings, dry_run=dry_run,
                               batch_size=batch_size, preverified=True, logger=log)
        # Post-convert: LF contact email -> KN_Email2__c on NEW Contacts when it differs
        # from the primary (task) email. No-op under dry_run (no results).
        result["email2"] = enrich_contact_email2(
            sf_client, kept, result.get("results", []), logger=log)
        out.update(preflight_failures=pf_failures, result=result)
    except Exception as exc:  # noqa: BLE001 — fenced on purpose, see docstring
        log.error("Conversion phase aborted: %s", exc, exc_info=True)
        out["error"] = f"{type(exc).__name__}: {exc}"[:500]
    return out


def _tick_overlay(sweep: SweepResult) -> dict | None:
    """Dry run only: preview the sweep's KN_NoOwnDBID__c tick in the preflight."""
    if not sweep.tick_pending:
        return None
    return {lid: {"KN_NoOwnDBID__c": True} for lid in sweep.tick_pending}


def _converted_ids(conversion: dict) -> set[str]:
    return {str(r.get("leadId") or "")[:15]
            for r in (conversion.get("result") or {}).get("results", [])
            if r.get("success") and not r.get("alreadyConverted")}


def _conversion_rows(results, targets, sweep: SweepResult, conversion: dict,
                     dry_run: bool) -> list[dict]:
    """One QA `Conversion` row per target (run + sweep) and per swept lead left to a
    human — what the run did, or on a dry run would do, with each lead."""
    if not conversion and not sweep.left:
        return []
    by_lead = {r.enriched.extracted.lead_id[:15]: r for r in results}
    pf: dict[str, list[str]] = {}
    for f in conversion.get("preflight_failures", []):
        pf.setdefault(f["lead_id"][:15], []).append(f["check"])
    outcome_by_lead: dict[str, dict] = {}
    for r in (conversion.get("result") or {}).get("results", []):
        outcome_by_lead[str(r.get("leadId") or "")[:15]] = r
    missing = {str(i)[:15] for i in (conversion.get("result") or {}).get("missing_ids", [])}
    rows: list[dict] = []
    for t in targets:
        key = t.lead_id[:15]
        r = by_lead.get(key)
        info = sweep.info.get(t.lead_id, {})
        if r is not None:
            source, company, decision, predicted_at = (
                "run", r.enriched.extracted.best_company, r.decision, None)
        else:
            source, company, decision, predicted_at = (
                "sweep", info.get("company"), info.get("decision"), info.get("predicted_at"))
        account_id = contact_id = None
        if key in pf:
            outcome, detail = "preflight_skipped", "; ".join(pf[key])
        elif conversion.get("error"):
            outcome, detail = "error", conversion["error"]
        elif dry_run:
            outcome = "dry_run"
            detail = "dbid_tick_pending" if key in sweep.tick_pending else ""
        elif key in outcome_by_lead:
            res = outcome_by_lead[key]
            if res.get("alreadyConverted"):
                outcome, detail = "already_converted", ""
            elif res.get("success"):
                outcome, detail = "converted", ""
                account_id, contact_id = res.get("accountId"), res.get("contactId")
            else:
                outcome, detail = "failed", "; ".join(res.get("errors") or [])
        elif key in missing:
            outcome, detail = "missing", "no conversion result returned"
        else:
            outcome, detail = "not_attempted", ""
        rows.append({"lead_id": t.lead_id, "company": company, "source": source,
                     "decision": decision, "predicted_at": predicted_at,
                     "outcome": outcome, "detail": detail,
                     "account_id": account_id, "contact_id": contact_id})
    for left in sweep.left:
        detail = left["reason"]
        if left.get("error"):
            detail += f": {left['error']}"
        rows.append({"lead_id": left["lead_id"], "company": left.get("company"),
                     "source": "sweep", "decision": left.get("decision"),
                     "predicted_at": left.get("predicted_at"),
                     "outcome": "left_to_human", "detail": detail,
                     "account_id": None, "contact_id": None})
    return rows


def _conversion_meta(conversion: dict, sweep: SweepResult, rows: list[dict]) -> dict:
    """Summary-sheet counters, from the FINAL Conversion rows (never a mid-pipeline tally)."""
    if not conversion and not sweep.queried:
        return {}
    outcomes = Counter(r["outcome"] for r in rows)
    meta = {
        "convert_targets_run": conversion.get("targets_run", 0),
        "convert_targets_sweep": conversion.get("targets_sweep", 0),
        "converted": outcomes["converted"],
        "convert_already_converted": outcomes["already_converted"],
        "convert_failed": outcomes["failed"],
        "convert_missing": outcomes["missing"],
        "convert_preflight_skipped": outcomes["preflight_skipped"],
        "convert_left_to_human": outcomes["left_to_human"],
        "convert_dry_run": outcomes["dry_run"],
        "sweep_dbid_ticked": sweep.ticked,
    }
    if conversion.get("error"):
        meta["convert_error"] = conversion["error"]
    return meta
