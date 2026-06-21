"""Stage 2 — convert only the Leads a human approved in the QA artifact.

The reviewer commits `review/approved_<date>.csv` (the Review sheet rows with `Approve?=yes`).
This stage reads those rows, turns each into a `ConversionTarget` — linking the matched
Account when present and the matched Contact only when its score clears the auto-link bar —
and runs the match-aware, idempotent bulk conversion. `dry_run` performs no conversion.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

from ..convert import ConversionTarget, convert_leads
from ..logging import get_logger

_APPROVE_YES = {"yes", "y", "true", "x", "1"}


@dataclass
class Stage2Result:
    targets: list[ConversionTarget] = field(default_factory=list)
    result: dict = field(default_factory=dict)
    dry_run: bool = False


def read_approved(path: str | Path) -> list[dict]:
    """Read the reviewer's approved CSV into a list of row dicts."""
    with Path(path).open(newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def _is_approved(row: dict) -> bool:
    return (row.get("Approve?") or "").strip().lower() in _APPROVE_YES


def targets_from_approved(rows: list[dict], settings) -> list[ConversionTarget]:
    """Build conversion targets from approved rows.

    Account is linked when `account_match_id` is present (the reviewer validated it);
    Contact is linked only when `contact_match_id` is present AND `contact_score` clears
    the auto-link threshold — weaker contact matches create a fresh contact under the account.
    Blanking `account_match_id`/`contact_match_id` forces create-new.
    """
    targets: list[ConversionTarget] = []
    for row in rows:
        if not _is_approved(row):
            continue
        lead_id = (row.get("lead_id") or "").strip()
        if not lead_id:
            continue
        account_id = (row.get("account_match_id") or "").strip() or None
        contact_id = (row.get("contact_match_id") or "").strip() or None
        if contact_id:
            try:
                cscore = float(row.get("contact_score") or 0)
            except (TypeError, ValueError):
                cscore = 0.0
            if cscore < settings.auto_link_threshold:
                contact_id = None
        targets.append(ConversionTarget(lead_id, account_id=account_id, contact_id=contact_id))
    return targets


def run_stage2(
    sf_client, settings, approved_csv: str | Path, *,
    dry_run: bool | None = None, batch_size: int = 50, logger=None,
) -> Stage2Result:
    log = logger or get_logger("pipeline.stage2")
    dry_run = settings.dry_run if dry_run is None else dry_run
    rows = read_approved(approved_csv)
    targets = targets_from_approved(rows, settings)
    log.info("Stage 2: %d approved row(s) -> %d conversion target(s) (dry_run=%s)",
             len(rows), len(targets), dry_run)
    result = convert_leads(sf_client, targets, settings,
                           dry_run=dry_run, batch_size=batch_size, logger=log)
    return Stage2Result(targets=targets, result=result, dry_run=dry_run)
