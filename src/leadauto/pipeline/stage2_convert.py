"""Stage 2 — convert only the Leads a human approved in the QA artifact.

The reviewer commits `review/approved_<date>.csv` (the Review sheet rows with `Approve?=yes`).
This stage reads those rows, turns each into a `ConversionTarget` — linking the matched
Account when present and the matched Contact only when its match reason is an identifier
signal — and runs the match-aware, idempotent bulk conversion. `dry_run` performs no
conversion. A read-only preflight (convert/preflight.py) re-checks each approved lead's
current field state against the org's conversion rules and skips+reports failures before
any Apex runs.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

from ..convert import ConversionTarget, convert_leads
from ..convert.preflight import run_preflight, write_failures_csv
from ..logging import get_logger

_APPROVE_YES = {"yes", "y", "true", "x", "1"}

# Contact reasons that identify the same person (vs. a name/heuristic match): linking on
# these is safe. Weaker reasons (contact_email_global, linkedin) create a fresh contact
# under the linked account instead.
_CONTACT_LINK_REASONS = {"kununu_user_uuid", "contact_email_in_account"}


@dataclass
class Stage2Result:
    targets: list[ConversionTarget] = field(default_factory=list)
    result: dict = field(default_factory=dict)
    dry_run: bool = False
    preflight_failures: list[dict] = field(default_factory=list)


def read_approved(path: str | Path) -> list[dict]:
    """Read the reviewer's approved CSV into a list of row dicts."""
    with Path(path).open(newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def _is_approved(row: dict) -> bool:
    return (row.get("Approve?") or "").strip().lower() in _APPROVE_YES


def targets_from_approved(rows: list[dict], settings) -> list[ConversionTarget]:
    """Build conversion targets from approved rows.

    Account is linked when `account_match_id` is present (the reviewer validated it).
    Contact is linked only when its match reason is an identifier signal — kununu user
    UUID or an email inside the linked account; weaker matches create a fresh contact.
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
        if contact_id and (row.get("contact_reason") or "").strip() not in _CONTACT_LINK_REASONS:
            contact_id = None
        targets.append(ConversionTarget(lead_id, account_id=account_id, contact_id=contact_id))
    return targets


def run_stage2(
    sf_client, settings, approved_csv: str | Path, *,
    dry_run: bool | None = None, batch_size: int = 50,
    preflight: bool = True, failures_csv: str | Path = "preflight_failures.csv",
    logger=None,
) -> Stage2Result:
    log = logger or get_logger("pipeline.stage2")
    dry_run = settings.dry_run if dry_run is None else dry_run
    rows = read_approved(approved_csv)
    targets = targets_from_approved(rows, settings)
    pf_failures: list[dict] = []
    if preflight:
        # Runs even with zero targets: run_preflight returns ([], []) without querying,
        # and the header-only CSV proves in the CI artifact that preflight ran.
        targets, pf_failures = run_preflight(sf_client, targets, logger=log)
        path = write_failures_csv(pf_failures, failures_csv)
        if pf_failures:
            log.warning("Preflight skipped %d lead(s); details -> %s",
                        len({f['lead_id'] for f in pf_failures}), path)
    log.info("Stage 2: %d approved row(s) -> %d conversion target(s) (dry_run=%s)",
             len(rows), len(targets), dry_run)
    result = convert_leads(sf_client, targets, settings,
                           dry_run=dry_run, batch_size=batch_size, logger=log)
    return Stage2Result(targets=targets, result=result, dry_run=dry_run,
                        preflight_failures=pf_failures)
