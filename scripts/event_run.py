"""One-off enrichment runner for event (badge-scan) leads — first used for ZPE 2026, 2026-09-18.

Why not `python -m leadauto run --config …`? Three things the CLI cannot do and this
population needs:

  * `since` is fixed to the event window instead of being derived from the shared
    watermark (which sits at 2026-07-18 and would drag every claimer/ebp task since
    July into the scan);
  * the run gets its OWN watermark file under --out, so a live run never advances
    `state/watermark.json` (that would hide the Jul–Sep claimer backlog from the next
    real live run);
  * no shadow predictions (the config also says so) and NO CONVERSION
    (`run_pipeline(convert=False)`): since the 2026-11-01 fusion a plain `run` converts
    in-process, but this population's config lists `link` in its conversion scope and
    the link path (+ pool-Account enrichment writes) has never run live — conversion
    for event leads stays a separate, deliberate, human-reviewed step.

Usage (dry by default):
    $env:PYTHONPATH = "src"
    python scripts/event_run.py --config config/config.zpe-2026-09.yaml --out <dir>
    python scripts/event_run.py --config config/config.zpe-2026-09.yaml --out <dir> --live
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from leadauto.cli import _build_lf_client, _build_sf_client  # noqa: E402
from leadauto.config import Settings  # noqa: E402
from leadauto.logging import get_logger  # noqa: E402
from leadauto.pipeline.run import run_pipeline  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", required=True, help="one-off config, e.g. config/config.zpe-2026-09.yaml")
    ap.add_argument("--out", required=True, help="output dir: QA workbook, own watermark")
    ap.add_argument("--since", default="2026-09-13T22:00:00Z",
                    help="SOQL datetime literal; Tasks created at/after it are scanned")
    ap.add_argument("--live", action="store_true", help="write back (default: dry); never converts")
    ap.add_argument("--no-leadfeeder", action="store_true", help="skip LeadFeeder (0 credits, no firmographics)")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    log = get_logger("event_run")
    settings = Settings.load(args.config)
    if "event" not in settings.enabled_task_types:
        log.error("%s does not enable the 'event' task type; refusing to run", args.config)
        return 2
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    qa_path = out / f"qa_event_{'live' if args.live else 'dry'}_{stamp}.xlsx"

    sf = _build_sf_client(settings, log)
    lf = None
    if not args.no_leadfeeder:
        if not settings.leadfeeder_api_key:
            log.error("LEADFEEDER_API_KEY not set; pass --no-leadfeeder to run without enrichment")
            return 2
        lf = _build_lf_client(settings, log)

    res = run_pipeline(
        sf, lf, settings,
        since=args.since,
        limit=args.limit,
        dry_run=not args.live,
        qa_path=qa_path,
        state_path=out / "watermark.json",
        predictions_path=None,
        convert=False,
        logger=log,
    )
    decisions = [r.decision for r in res.results]
    reasons = {}
    for s in res.skipped:
        reasons[s.reason] = reasons.get(s.reason, 0) + 1
    print(
        f"Stage 1 {'LIVE' if args.live else 'dry-run'} done: {len(res.results)} lead(s) | "
        f"link={decisions.count('link')} review={decisions.count('review')} "
        f"create_new={decisions.count('create_new')} skip={decisions.count('skip')} | "
        f"skipped={len(res.skipped)} {reasons} | wrote_back={res.writeback.get('processed', 0)} "
        f"failures={len(res.failures)} | QA={res.qa_path}"
    )
    if lf is not None:
        print(f"LeadFeeder credits charged this run: {getattr(lf, 'credits_charged', 'n/a')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
