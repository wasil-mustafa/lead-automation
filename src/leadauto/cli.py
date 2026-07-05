"""Command-line entry point (`python -m leadauto stage1|stage2`).

Thin glue: resolve config + secrets, authenticate the org, build the LeadFeeder client (or
skip it), and call the stage orchestrators. Flags override config where given:
  stage1  --dry-run/--no-dry-run  --limit N  --leadfeeder/--no-leadfeeder  --qa-path P
  stage2  --approved P (default: newest review/approved_*.csv)  --dry-run  --batch-size N
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from .config import Settings
from .leadfeeder.client import LeadfeederClient
from .logging import get_logger
from .pipeline.stage1_enrich import run_stage1
from .pipeline.stage2_convert import run_stage2
from .shadow_compare import run_shadow_compare
from .sf.auth import ensure_org
from .sf.client import SfClient
from .state import DEFAULT_STATE_PATH, REPO_ROOT

app = typer.Typer(add_completion=False, help="Lead Automation pipeline.")

REVIEW_DIR = REPO_ROOT / "review"
DEFAULT_QA_PATH = REPO_ROOT / "qa_artifact.xlsx"


def find_latest_approved(review_dir: str | Path) -> Optional[Path]:
    """Newest `approved_*.csv` in the review dir (names sort chronologically). None if absent."""
    files = sorted(Path(review_dir).glob("approved_*.csv"))
    return files[-1] if files else None


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
def stage1(
    dry_run: Optional[bool] = typer.Option(None, "--dry-run/--no-dry-run",
                                           help="Override config dry_run."),
    limit: int = typer.Option(0, "--limit", help="LIMIT-scope the task scan (0 = config)."),
    leadfeeder: bool = typer.Option(True, "--leadfeeder/--no-leadfeeder",
                                    help="Disable to skip LeadFeeder enrichment (no credits)."),
    kununu_feed: bool = typer.Option(True, "--kununu-feed/--no-kununu-feed",
                                     help="Disable the kununu profile feed (assigned-company alt names)."),
    qa_path: Path = typer.Option(DEFAULT_QA_PATH, "--qa-path", help="QA artifact output path."),
    config: Optional[Path] = typer.Option(None, "--config", help="Alternate config.yaml."),
) -> None:
    """Extract -> enrich -> match -> write back -> QA artifact (no conversion)."""
    log = get_logger("cli.stage1")
    settings = Settings.load(config)
    sf = _build_sf_client(settings, log)
    lf = None
    if leadfeeder and settings.leadfeeder_api_key:
        lf = _build_lf_client(settings, log)
    elif leadfeeder:
        log.warning("LEADFEEDER_API_KEY not set; running without enrichment")

    res = run_stage1(sf, lf, settings, limit=limit, dry_run=dry_run,
                     qa_path=qa_path, state_path=DEFAULT_STATE_PATH, kununu_feed=kununu_feed,
                     logger=log)
    decisions = [r.decision for r in res.results]
    typer.echo(
        f"Stage 1 {'(dry-run) ' if res.dry_run else ''}done: "
        f"{len(res.results)} lead(s) | link={decisions.count('link')} "
        f"review={decisions.count('review')} create_new={decisions.count('create_new')} | "
        f"skipped={len(res.skipped)} | wrote_back={res.writeback.get('processed', 0)} | "
        f"QA={res.qa_path}"
    )


@app.command()
def stage2(
    approved: Optional[Path] = typer.Option(None, "--approved",
                                            help="Approved CSV (default: newest review/approved_*.csv)."),
    dry_run: Optional[bool] = typer.Option(None, "--dry-run/--no-dry-run",
                                           help="Override config dry_run."),
    batch_size: int = typer.Option(50, "--batch-size", help="Leads converted per Apex run."),
    preflight: bool = typer.Option(True, "--preflight/--no-preflight",
                                   help="Check conversion requirements and skip failing leads."),
    config: Optional[Path] = typer.Option(None, "--config", help="Alternate config.yaml."),
) -> None:
    """Convert the Leads approved in the QA artifact (review/approved_*.csv)."""
    log = get_logger("cli.stage2")
    settings = Settings.load(config)
    if approved is None:
        approved = find_latest_approved(REVIEW_DIR)
        if approved is None:
            typer.secho(f"No approved_*.csv found in {REVIEW_DIR}", fg=typer.colors.RED)
            raise typer.Exit(code=1)
        log.info("Using approved file %s", approved)
    sf = _build_sf_client(settings, log)
    res = run_stage2(sf, settings, approved, dry_run=dry_run, batch_size=batch_size,
                     preflight=preflight, logger=log)
    skipped_leads = len({f["lead_id"] for f in res.preflight_failures})
    typer.echo(
        f"Stage 2 {'(dry-run) ' if res.dry_run else ''}done: "
        f"{len(res.targets)} target(s) | converted={res.result.get('converted', 0)} "
        f"failed={res.result.get('failed', 0)} | preflight_skipped={skipped_leads}"
    )


@app.command("shadow-compare")
def shadow_compare(
    config: Optional[Path] = typer.Option(None, "--config", help="Alternate config.yaml."),
) -> None:
    """Score pending dry-run predictions against human conversions in the org."""
    log = get_logger("cli.shadow")
    settings = Settings.load(config)
    sf = _build_sf_client(settings, log)
    res = run_shadow_compare(sf, logger=log)
    typer.echo(
        f"Shadow compare done: checked={res['checked']} resolved={res['resolved']} "
        f"hits={res['hits']} misses={res['misses']} observed={res['observed']} "
        f"expired={res['expired']} pending={res['pending']} | xlsx={res['xlsx']}"
    )


if __name__ == "__main__":  # pragma: no cover
    app()
