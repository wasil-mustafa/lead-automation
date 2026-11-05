"""Command-line entry point (`python -m leadauto run`).

Thin glue: resolve config + secrets, authenticate the org, build the LeadFeeder client (or
skip it), and call the pipeline. Flags override config where given:
  run  --dry-run/--no-dry-run  --limit N  --leadfeeder/--no-leadfeeder  --qa-path P
       --batch-size N (SOAP conversions per call; config pipeline.convert_batch_size)
The former `stage1`/`stage2` commands were fused into `run` on 2026-11-01.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from .config import Settings
from .leadfeeder.client import MATCH_RESULTS_WIDE, LeadfeederClient
from .logging import get_logger
from .pipeline.run import run_pipeline
from .shadow_compare import (DEFAULT_MIN_INTERVAL_HOURS, compare_is_due,
                             last_compare_at, record_compare_run, run_shadow_compare,
                             write_skipped_report)
from .sf.auth import ensure_org
from .sf.client import SfClient
from .state import DEFAULT_STATE_PATH, REPO_ROOT

app = typer.Typer(add_completion=False, help="Lead Automation pipeline.")

DEFAULT_QA_PATH = REPO_ROOT / "qa_artifact.xlsx"


def _build_sf_client(settings: Settings, logger) -> SfClient:
    alias = ensure_org(settings, logger=logger)
    return SfClient(alias=alias, api_version=settings.api_version, logger=logger)


def _build_lf_client(settings: Settings, logger) -> LeadfeederClient:
    return LeadfeederClient(
        api_key=settings.leadfeeder_api_key,
        base_url=settings.lf_base_url,
        min_match_score=settings.lf_min_match_score,
        name_agree_min=settings.lf_name_agree_min,
        name_agree_rescue=settings.lf_name_agree_rescue,
        retrieve_batch=settings.lf_retrieve_batch,
        match_batch=settings.lf_match_batch,
        max_requests_per_min=settings.lf_max_requests_per_min,
        max_retries=settings.lf_max_retries,
        logger=logger,
    )


@app.command()
def run(
    dry_run: Optional[bool] = typer.Option(None, "--dry-run/--no-dry-run",
                                           help="Override config dry_run."),
    limit: int = typer.Option(0, "--limit", help="LIMIT-scope the task scan (0 = config)."),
    leadfeeder: bool = typer.Option(True, "--leadfeeder/--no-leadfeeder",
                                    help="Disable to skip LeadFeeder enrichment (no credits)."),
    kununu_feed: bool = typer.Option(True, "--kununu-feed/--no-kununu-feed",
                                     help="Disable the kununu profile feed (assigned-company alt names)."),
    qa_path: Path = typer.Option(DEFAULT_QA_PATH, "--qa-path", help="QA artifact output path."),
    batch_size: Optional[int] = typer.Option(
        None, "--batch-size",
        help="Leads per SOAP convertLead() call (default: config pipeline.convert_batch_size, "
             "3 — the live governor-limit ceiling; each conversion cascades triggers, flows "
             "and rules in one transaction)."),
    config: Optional[Path] = typer.Option(None, "--config", help="Alternate config.yaml."),
) -> None:
    """Extract -> enrich -> match -> write back -> campaign tag -> convert -> QA artifact."""
    log = get_logger("cli.run")
    settings = Settings.load(config)
    sf = _build_sf_client(settings, log)
    lf = None
    if leadfeeder and settings.leadfeeder_api_key:
        lf = _build_lf_client(settings, log)
    elif leadfeeder:
        log.warning("LEADFEEDER_API_KEY not set; running without enrichment")

    res = run_pipeline(sf, lf, settings, limit=limit, dry_run=dry_run,
                       qa_path=qa_path, state_path=DEFAULT_STATE_PATH,
                       kununu_feed=kununu_feed, batch_size=batch_size, logger=log)
    decisions = [r.decision for r in res.results]
    conv = res.conversion.get("result") or {}
    failed = conv.get("failed", 0)
    missing = conv.get("missing", 0)
    error = res.conversion.get("error")
    typer.echo(
        f"Pipeline {'(dry-run) ' if res.dry_run else ''}done: "
        f"{len(res.results)} lead(s) | link={decisions.count('link')} "
        f"review={decisions.count('review')} create_new={decisions.count('create_new')} | "
        f"skipped={len(res.skipped)} | wrote_back={res.writeback.get('processed', 0)} | "
        f"targets={len(res.targets)} (run={res.conversion.get('targets_run', 0)} "
        f"sweep={res.conversion.get('targets_sweep', 0)}) "
        f"converted={conv.get('converted', 0)} already={conv.get('already_converted', 0)} "
        f"failed={failed} missing={missing} "
        f"preflight_skipped={len({f['lead_id'] for f in res.conversion.get('preflight_failures', [])})} "
        f"left_to_human={len(res.sweep.left)} | QA={res.qa_path}"
    )
    if error:
        typer.secho(f"Conversion phase aborted: {error}", fg=typer.colors.RED)
    if failed or missing or error:
        # Failed, unaccounted-for or aborted conversions must fail the job, not just log —
        # AFTER the workbook and state were written (run_pipeline never skips those).
        # Already-converted leads (idempotent re-run) are a no-op and do NOT fail the job.
        raise typer.Exit(code=1)


@app.command("backtest-skipped")
def backtest_skipped(
    leadfeeder: bool = typer.Option(True, "--leadfeeder/--no-leadfeeder",
                                    help="Disable to skip LeadFeeder (all cases become no_lf_match)."),
    kununu_feed: bool = typer.Option(True, "--kununu-feed/--no-kununu-feed",
                                     help="Disable the kununu profile feed (assigned-company alt names)."),
    xlsx: Path = typer.Option(REPO_ROOT / "backtest_skipped.xlsx", "--xlsx",
                              help="Report output path."),
    config: Optional[Path] = typer.Option(None, "--config", help="Alternate config.yaml."),
) -> None:
    """Backtest skipped tasks (converted leads / contact-attached) against the rule engine."""
    from .backtest_skipped import run_backtest

    log = get_logger("cli.backtest")
    settings = Settings.load(config)
    sf = _build_sf_client(settings, log)
    lf = None
    if leadfeeder and settings.leadfeeder_api_key:
        lf = _build_lf_client(settings, log)
    elif leadfeeder:
        log.warning("LEADFEEDER_API_KEY not set; running without enrichment")
    res = run_backtest(sf, lf, settings, xlsx_path=xlsx, kununu_feed=kununu_feed, logger=log)
    typer.echo(f"Backtest done: counts={res['counts']} tally={res.get('tally')} "
               f"| xlsx={res.get('xlsx')}")


@app.command("wide-match-backtest")
def wide_match_backtest(
    max_results: int = typer.Option(MATCH_RESULTS_WIDE, "--max-results",
                                    help="Treatment width (API max 20). Control is always 3."),
    kununu_feed: bool = typer.Option(True, "--kununu-feed/--no-kununu-feed",
                                     help="Disable the feed: payloads then carry NO alt_names, "
                                          "so the result is not comparable to production."),
    xlsx: Path = typer.Option(REPO_ROOT / "wide_match_backtest.xlsx", "--xlsx",
                              help="Report output path."),
    config: Optional[Path] = typer.Option(None, "--config", help="Alternate config.yaml."),
) -> None:
    """Replay the `no_lf_match` leads at two candidate widths. Read-only, 0 credits."""
    from .wide_match_backtest import run_backtest as run_wide_backtest

    log = get_logger("cli.wide_backtest")
    settings = Settings.load(config)
    if not settings.leadfeeder_api_key:
        log.error("LEADFEEDER_API_KEY is required for this backtest.")
        raise typer.Exit(code=2)
    sf = _build_sf_client(settings, log)
    lf = _build_lf_client(settings, log)
    res = run_wide_backtest(sf, lf, settings, max_results=max_results,
                            xlsx_path=xlsx, use_kununu_feed=kununu_feed, logger=log)
    typer.echo(f"Wide-match backtest done: counts={res['counts']} tally={res['tally']} "
               f"| credits={res['credits']} | xlsx={res.get('xlsx')}")


@app.command("domain-rescue-backtest")
def domain_rescue_backtest(
    kununu_feed: bool = typer.Option(True, "--kununu-feed/--no-kununu-feed",
                                     help="Disable the feed: payloads then carry NO alt_names "
                                          "and no feed country, so the result is not "
                                          "comparable to production."),
    xlsx: Path = typer.Option(REPO_ROOT / "domain_rescue_backtest.xlsx", "--xlsx",
                              help="Report output path."),
    predictions: Optional[Path] = typer.Option(
        None, "--predictions",
        help="predictions.jsonl to replay (default: the committed state/ file). Useful "
             "when the local checkout lags origin."),
    config: Optional[Path] = typer.Option(None, "--config", help="Alternate config.yaml."),
) -> None:
    """Replay the sanity-gate-rejected leads through the domain rescue. Read-only, 0 credits."""
    from .domain_rescue_backtest import run_backtest as run_rescue_backtest

    log = get_logger("cli.domain_rescue_backtest")
    settings = Settings.load(config)
    if not settings.leadfeeder_api_key:
        log.error("LEADFEEDER_API_KEY is required for this backtest.")
        raise typer.Exit(code=2)
    sf = _build_sf_client(settings, log)
    lf = _build_lf_client(settings, log)
    res = run_rescue_backtest(sf, lf, settings, xlsx_path=xlsx, predictions_path=predictions,
                              use_kununu_feed=kununu_feed, logger=log)
    typer.echo(f"Domain-rescue backtest done: counts={res['counts']} tally={res['tally']} "
               f"| credits={res['credits']} | xlsx={res.get('xlsx')}")


@app.command("shadow-compare")
def shadow_compare(
    config: Optional[Path] = typer.Option(None, "--config", help="Alternate config.yaml."),
    min_interval_hours: float = typer.Option(
        DEFAULT_MIN_INTERVAL_HOURS, "--min-interval-hours",
        help="Skip if a compare already ran this recently (0 = always run). Stage 1 is "
             "dispatched 4x/day; human conversions resolve over days."),
) -> None:
    """Score pending dry-run predictions against human conversions in the org."""
    log = get_logger("cli.shadow")
    if not compare_is_due(min_interval_hours=min_interval_hours):
        # No org call, but the workbook is still produced: the workflow uploads it as an
        # artifact on every dispatch, so a skipped compare must not mean a missing file.
        last = last_compare_at()
        res = write_skipped_report(last_run=last, min_interval_hours=min_interval_hours,
                                   logger=log)
        typer.echo(f"Shadow compare skipped: last run "
                   f"{last.isoformat() if last else 'unknown'} is younger than "
                   f"{min_interval_hours}h (--min-interval-hours 0 to force); "
                   f"report written from committed state | xlsx={res['xlsx']}")
        return
    settings = Settings.load(config)
    sf = _build_sf_client(settings, log)
    res = run_shadow_compare(sf, excluded_domains=settings.excluded_email_domains, logger=log)
    record_compare_run()
    typer.echo(
        f"Shadow compare done: checked={res['checked']} resolved={res['resolved']} "
        f"hits={res['hits']} misses={res['misses']} observed={res['observed']} "
        f"expired={res['expired']} excluded={res['excluded']} pending={res['pending']} "
        f"| xlsx={res['xlsx']}"
    )


if __name__ == "__main__":  # pragma: no cover
    app()
