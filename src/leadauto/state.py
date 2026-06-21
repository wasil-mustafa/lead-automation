"""File-based watermark state (no Salesforce metadata, per the plan).

`state/watermark.json` holds the newest processed Task CreatedDate and the set of processed
Task Ids. The reader scans from `compute_since` = watermark - `task_lookback_days`, and the
overlap window + `processed_task_ids` + per-WhoId dedupe make re-runs idempotent. The
watermark is advanced (and committed by the Action) only after a successful Stage 1.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_STATE_PATH = REPO_ROOT / "state" / "watermark.json"


@dataclass
class State:
    last_task_created: str | None = None
    processed_task_ids: list[str] = field(default_factory=list)


def _parse_sf_dt(value: str) -> datetime:
    """Parse a Salesforce datetime ('...+0000', 'Z', or fractional seconds) to aware UTC."""
    try:
        dt = datetime.fromisoformat(value.strip())
    except ValueError:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def load_state(path: str | Path | None = None) -> State:
    p = Path(path) if path else DEFAULT_STATE_PATH
    if not p.exists():
        return State()
    data = json.loads(p.read_text(encoding="utf-8")) or {}
    return State(
        last_task_created=data.get("last_task_created"),
        processed_task_ids=list(data.get("processed_task_ids", [])),
    )


def save_state(state: State, path: str | Path | None = None) -> None:
    p = Path(path) if path else DEFAULT_STATE_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "last_task_created": state.last_task_created,
        "processed_task_ids": list(state.processed_task_ids),
    }
    p.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def compute_since(state: State, lookback_days: int, now: datetime | None = None) -> str:
    """SOQL datetime literal to scan from: (watermark or now) minus the overlap window."""
    base = _parse_sf_dt(state.last_task_created) if state.last_task_created else (
        now or datetime.now(timezone.utc)
    )
    since = base.astimezone(timezone.utc) - timedelta(days=lookback_days)
    return since.strftime("%Y-%m-%dT%H:%M:%SZ")


def advance(state: State, processed_tasks: list[dict]) -> State:
    """Return a new State with the watermark moved to the newest processed Task and its
    Id folded into `processed_task_ids`. Never moves the watermark backwards."""
    if not processed_tasks:
        return State(state.last_task_created, list(state.processed_task_ids))

    latest_str = state.last_task_created
    latest_dt = _parse_sf_dt(latest_str) if latest_str else None
    for t in processed_tasks:
        created = t.get("CreatedDate")
        if not created:
            continue
        dt = _parse_sf_dt(created)
        if latest_dt is None or dt > latest_dt:
            latest_dt, latest_str = dt, created

    ids = list(dict.fromkeys([*state.processed_task_ids, *(t["Id"] for t in processed_tasks if t.get("Id"))]))
    return State(last_task_created=latest_str, processed_task_ids=ids)
