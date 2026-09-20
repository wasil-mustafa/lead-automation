"""Shadow-mode prediction log (`state/predictions.jsonl`).

Every DRY stage-1 run records what the pipeline WOULD have done per lead; the
shadow-compare job later scores pending records against the human conversions that
actually happened (see shadow_compare.py). One JSON object per line. Upserts replace
a lead's pending record; resolved/expired records are immutable history.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .state import REPO_ROOT

DEFAULT_PREDICTIONS_PATH = REPO_ROOT / "state" / "predictions.jsonl"


@dataclass
class Prediction:
    lead_id: str
    task_id: str
    predicted_at: str              # ISO-8601 UTC of the dry run
    rule: str
    decision: str                  # link | review | create_new | skip
    account_id: str | None = None  # predicted link target
    contact_id: str | None = None
    evidence: list[str] = field(default_factory=list)
    payload: dict = field(default_factory=dict)   # KN_* write-back payload (sans Id)
    lf_company_id: str | None = None
    lf_company_name: str | None = None
    lf_website: str | None = None
    lf_match_pass: str = ""        # primary | wide (second-chance match) | "" (no match)
    status: str = "pending"        # pending | resolved | expired
    resolved_at: str | None = None
    outcome: dict | None = None    # filled by shadow_compare


def load_predictions(path: str | Path | None = None) -> list[Prediction]:
    p = Path(path) if path else DEFAULT_PREDICTIONS_PATH
    if not p.exists():
        return []
    return [Prediction(**json.loads(line))
            for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


def save_predictions(preds: list[Prediction], path: str | Path | None = None) -> None:
    p = Path(path) if path else DEFAULT_PREDICTIONS_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(json.dumps(asdict(pr), ensure_ascii=False) + "\n" for pr in preds),
                 encoding="utf-8")


def record_predictions(results: list, payloads: list[dict], predicted_at: str,
                       path: str | Path | None = None) -> int:
    """Upsert one pending Prediction per MatchResult; non-pending records are kept."""
    payload_by_lead = {p["Id"]: {k: v for k, v in p.items() if k != "Id"}
                       for p in payloads if p.get("Id")}
    existing = load_predictions(path)
    keep = [pr for pr in existing if pr.status != "pending"]
    pending = {pr.lead_id: pr for pr in existing if pr.status == "pending"}
    for r in results:
        e = r.enriched.extracted
        company = r.enriched.company or {}
        pending[e.lead_id] = Prediction(
            lead_id=e.lead_id, task_id=e.task_id, predicted_at=predicted_at,
            rule=r.rule, decision=r.decision,
            account_id=r.account.account_id if r.account else None,
            contact_id=r.contact.contact_id if r.contact else None,
            evidence=list(r.evidence),
            payload=payload_by_lead.get(e.lead_id, {}),
            lf_company_id=r.enriched.lf_company_id,
            lf_company_name=r.enriched.matched_name,
            lf_website=company.get("Website") or None,
            lf_match_pass=r.enriched.match_pass,
        )
    save_predictions(keep + list(pending.values()), path)
    return len(pending)
