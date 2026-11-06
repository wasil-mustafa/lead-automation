"""Conversion holdouts — swept leads the pipeline must stop retrying (2026-11-07).

Why: the sweep (`targets.sweep_targets`) re-reads every unconverted campaign member on
every run, so a lead whose SOAP `convertLead` fails for a PERMANENT, org-side reason
is retried five times a day and every run exits 1. Lead 00QMz00000rov6DMAQ (TÜV
Saarland Immobilienbewertung GmbH) did exactly that on 2026-11-06: four sibling
"TÜV Saarland …" Accounts exist, the Account duplicate rule fuzzy-matches the new
Account's name, and conversion cannot send the allow-save header (see
`convert/CLAUDE.md`, "Duplicate rules can block conversion"). Only a human can convert
it (the UI lets them save past an Alert-mode rule).

What: a committed JSONL file, `state/convert_holdouts.jsonl`, one row per lead:
`{"lead_id", "reason", "error", "company", "recorded_at"}`. `record_holdouts` appends a
row for every failed conversion result whose error carries a code in
`HOLDOUT_ERROR_CODES` (live runs only — a dry run converts nothing); `sweep_targets`
leaves every listed lead to a human (`sweep_held_out`, Conversion sheet) instead of
re-converting it. The run that RECORDS a holdout still exits 1 (its conversion did
fail — that single red run is the signal that a human has work); every later run is
green.

Releasing a lead: converting it by hand makes it `IsConverted`, so the sweep query
stops returning it and its row is inert. To make the pipeline retry instead (say, the
duplicate Account was merged away), delete its row from the file. Ids from this file
are only ever COMPARED against query rows (15-char prefix), never embedded in SOQL or
SOAP — the converter re-guards every id it sends regardless.

Other SOAP failures (a governor limit, a transient fault, a validation rule a human
can fix on the lead) are NOT held out: the next sweep retries them, as before.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..state import REPO_ROOT

HOLDOUTS_FILENAME = "convert_holdouts.jsonl"
DEFAULT_HOLDOUTS_PATH = REPO_ROOT / "state" / HOLDOUTS_FILENAME

# Conversion error codes that no retry can clear — a human must act on the lead.
HOLDOUT_ERROR_CODES = {"DUPLICATES_DETECTED"}
HOLDOUT_DUPLICATE = "convert_duplicate_detected"


@dataclass
class Holdout:
    lead_id: str
    reason: str
    error: str
    company: str | None = None
    recorded_at: str | None = None


def _short_id(sf_id) -> str:
    return str(sf_id or "")[:15]


def load_holdouts(path: str | Path | None = None) -> dict[str, Holdout]:
    """{15-char lead id: Holdout}; the LAST row for an id wins. A missing file is
    empty (no holdouts yet); a malformed line RAISES — this file gates writes, so a
    plausible-wrong read (a truncated file read as 'nothing held out') must not retry
    a lead a human already owns."""
    p = Path(path) if path else DEFAULT_HOLDOUTS_PATH
    out: dict[str, Holdout] = {}
    if not p.exists():
        return out
    for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            h = Holdout(**row)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"{p}:{n}: malformed holdout row: {exc}") from exc
        if not h.lead_id:
            raise ValueError(f"{p}:{n}: holdout row without lead_id")
        out[_short_id(h.lead_id)] = h
    return out


def holdout_reason(result_row: dict) -> str | None:
    """The holdout reason for a failed conversion result row, or None when the failure
    is one the next sweep should retry."""
    if result_row.get("success"):
        return None
    for err in result_row.get("errors") or []:
        code = str(err).split(":", 1)[0].strip().upper()
        if code in HOLDOUT_ERROR_CODES:
            return HOLDOUT_DUPLICATE
    return None


def record_holdouts(result_rows: list[dict], companies: dict[str, str | None],
                    path: str | Path | None = None,
                    recorded_at: str | None = None) -> list[Holdout]:
    """Append a holdout row for every failed result whose error is permanent; returns
    the rows written. `companies` = {lead id: company name} for the Conversion sheet.
    Leads already held out are not re-appended (the sweep would not have sent them)."""
    p = Path(path) if path else DEFAULT_HOLDOUTS_PATH
    known = load_holdouts(p)
    stamp = recorded_at or datetime.now(timezone.utc).isoformat()
    new: list[Holdout] = []
    for r in result_rows:
        reason = holdout_reason(r)
        lead_id = str(r.get("leadId") or "")
        if reason is None or not lead_id or _short_id(lead_id) in known:
            continue
        h = Holdout(lead_id=lead_id, reason=reason,
                    error="; ".join(str(e) for e in r.get("errors") or [])[:500],
                    company=companies.get(_short_id(lead_id)) or companies.get(lead_id),
                    recorded_at=stamp)
        known[_short_id(lead_id)] = h
        new.append(h)
    if new:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            for h in new:
                fh.write(json.dumps(asdict(h), ensure_ascii=False) + "\n")
    return new
