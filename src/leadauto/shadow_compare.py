"""Shadow comparison: score pending dry-run predictions against human conversions.

Reads `state/predictions.jsonl`, queries the org for the predicted leads' conversion
status, classifies each now-converted lead (decision HIT/MISS, review rows only
observed), scores field-level enrichment agreement, appends results to
`state/shadow_results.jsonl`, marks predictions resolved/expired, and writes a
human-readable `shadow_comparison.xlsx` (Decisions / Fields / Summary).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .logging import get_logger
from .predictions import DEFAULT_PREDICTIONS_PATH, load_predictions, save_predictions
from .qa_artifact import _add_sheet
from .sf import soql
from .state import REPO_ROOT, _parse_sf_dt
from .textnorm import is_internal_email, normalize_city, normalize_host, normalize_postal
from .writeback import normalize_phone

DEFAULT_RESULTS_PATH = REPO_ROOT / "state" / "shadow_results.jsonl"
DEFAULT_XLSX_PATH = REPO_ROOT / "shadow_comparison.xlsx"
_EXPIRE_DAYS = 90

DECISIONS_COLUMNS = [
    "lead_id", "rule", "decision", "predicted_account_id",
    "converted_account_id", "converted_account_name", "account_created",
    "converted_date", "result", "detail",
]
FIELDS_COLUMNS = ["lead_id", "field", "status", "lf_value", "human_value"]


def _is_true(v) -> bool:
    """Coerce SF booleans: 300 pending IDs push the lead query over the Bulk-API
    threshold, and bulk export returns 'true'/'false' STRINGS ('false' is truthy!)."""
    return v is True or str(v).strip().lower() == "true"


def _norm_country(v):
    if not v:
        return None
    return str(v).strip().upper() or None


def _norm_text(v):
    return (str(v).strip() or None) if v is not None else None


def _norm_name(v):
    if v is None:
        return None
    return str(v).strip().casefold() or None


# (label, payload keys — first present wins, source record, org field, normalizer).
# Payload keys mirror what write-back actually writes; the legacy KN_DB* second
# keys keep scoring in-flight predictions recorded before write-back stopped
# writing the D&B-owned fields.
_FIELD_CHECKS = [
    ("postal_code", ("PostalCode", "KN_DBPostalCode__c"),
     "account", "BillingPostalCode", normalize_postal),
    ("city", ("City", "KN_DBCity__c"), "account", "BillingCity", normalize_city),
    ("country", ("CountryCode",), "account", "BillingCountryCode", _norm_country),
    ("employee_bin", ("KN_Employee__c",), "lead", "KN_Employee__c", _norm_text),
    ("first_name", ("FirstName",), "contact", "FirstName", _norm_name),
    ("phone", ("Phone",), "contact", "Phone", normalize_phone),
]


def _verdict(ours_raw, theirs_raw, norm) -> dict:
    ours, theirs = norm(ours_raw), norm(theirs_raw)
    if ours is None:
        status = "lf_empty"
    elif theirs is None:
        status = "human_empty"
    else:
        status = "agree" if ours == theirs else "differ"
    return {"status": status, "lf": ours_raw, "human": theirs_raw}


def compare_fields(pred, lead: dict, account: dict | None,
                   contact: dict | None = None) -> dict:
    recs = {"lead": lead or {}, "account": account or {}, "contact": contact or {}}
    out = {}
    for label, pkeys, source, okey, norm in _FIELD_CHECKS:
        ours = next((pred.payload[k] for k in pkeys if pred.payload.get(k) is not None), None)
        out[label] = _verdict(ours, recs[source].get(okey), norm)
    out["website"] = _verdict(pred.lf_website, recs["account"].get("Website"), normalize_host)
    return out


def _is_new_account(account: dict | None, converted_date_raw) -> bool:
    """Salesforce creates the Account at conversion time, so a human 'create new'
    shows as Account.CreatedDate on the conversion day (ConvertedDate is DATE-only)."""
    if not account or not account.get("CreatedDate") or not converted_date_raw:
        return False
    created = _parse_sf_dt(account["CreatedDate"])
    raw = str(converted_date_raw).strip()
    if "T" in raw:
        return abs(created - _parse_sf_dt(raw)) <= timedelta(hours=1)
    # Known edge: created.date() is a UTC calendar date compared against a
    # Date-only ConvertedDate, so a conversion near local midnight can land on
    # the "wrong" side by one day. Acceptable — this only affects the backtest
    # metric, never a live decision.
    return created.date().isoformat() == raw


def classify_decision(pred, lead: dict, account: dict | None) -> tuple[str, str]:
    """(result, detail) — result: hit | miss | observed."""
    converted_account = lead.get("ConvertedAccountId")
    if pred.decision == "link":
        if converted_account == pred.account_id:
            return "hit", "linked the predicted account"
        return "miss", f"human chose {converted_account}, predicted {pred.account_id}"
    if pred.decision == "create_new":
        if _is_new_account(account, lead.get("ConvertedDate")):
            return "hit", "human also created a new account"
        return "miss", f"human linked pre-existing {converted_account}"
    if pred.decision == "skip":
        # Terminal skip (no LF match). The human converting it is a coverage gap the
        # pipeline would have dropped — a miss, distinct from a wrong-account miss.
        kind = "new account" if _is_new_account(account, lead.get("ConvertedDate")) \
            else f"existing account {converted_account}"
        return "miss", f"human converted ({kind}); pipeline skipped (no LF match)"
    kind = "new account" if _is_new_account(account, lead.get("ConvertedDate")) \
        else f"existing account {converted_account}"
    return "observed", f"review case; human resolved to {kind}"


def run_shadow_compare(sf_client, *, predictions_path=None, results_path=None,
                       xlsx_path=None, now=None, excluded_domains=None,
                       logger=None) -> dict:
    log = logger or get_logger("shadow")
    ppath = Path(predictions_path) if predictions_path else DEFAULT_PREDICTIONS_PATH
    rpath = Path(results_path) if results_path else DEFAULT_RESULTS_PATH
    xpath = Path(xlsx_path) if xlsx_path else DEFAULT_XLSX_PATH
    now = now or datetime.now(timezone.utc)
    excluded_domains = excluded_domains or set()

    preds = load_predictions(ppath)
    pending = [p for p in preds if p.status == "pending"]
    stats = {"checked": len(pending), "resolved": 0, "hits": 0, "misses": 0,
             "observed": 0, "expired": 0, "excluded": 0, "pending": 0, "xlsx": str(xpath)}
    if not pending:
        log.info("Shadow compare: no pending predictions")
        return stats

    leads = sf_client.query(soql.leads_shadow_status([p.lead_id for p in pending]))
    lead_by_id = {l["Id"]: l for l in leads}
    acct_ids = sorted({l["ConvertedAccountId"] for l in leads if l.get("ConvertedAccountId")})
    accounts = sf_client.query(soql.accounts_by_ids(acct_ids)) if acct_ids else []
    acct_by_id = {a["Id"]: a for a in accounts}
    contact_ids = sorted({l["ConvertedContactId"] for l in leads if l.get("ConvertedContactId")})
    contacts = sf_client.query(soql.contacts_by_ids(contact_ids)) if contact_ids else []
    contact_by_id = {c["Id"]: c for c in contacts}

    new_results: list[dict] = []
    for pred in pending:
        lead = lead_by_id.get(pred.lead_id)
        if lead and is_internal_email(lead.get("Email"), excluded_domains):
            # Own-company lead (colleague test); never a real conversion to score.
            pred.status = "excluded"
            stats["excluded"] += 1
            continue
        if not lead or not _is_true(lead.get("IsConverted")):
            if now - _parse_sf_dt(pred.predicted_at) > timedelta(days=_EXPIRE_DAYS):
                pred.status = "expired"
                stats["expired"] += 1
            else:
                stats["pending"] += 1
            continue
        account = acct_by_id.get(lead.get("ConvertedAccountId"))
        contact = contact_by_id.get(lead.get("ConvertedContactId"))
        result, detail = classify_decision(pred, lead, account)
        fields = compare_fields(pred, lead, account, contact)
        record = {
            "lead_id": pred.lead_id, "resolved_at": now.isoformat(),
            "rule": pred.rule, "decision": pred.decision,
            "predicted_account_id": pred.account_id,
            "converted_account_id": lead.get("ConvertedAccountId"),
            "converted_account_name": (account or {}).get("Name"),
            "account_created": (account or {}).get("CreatedDate"),
            "converted_date": lead.get("ConvertedDate"),
            "result": result, "detail": detail, "fields": fields,
        }
        new_results.append(record)
        pred.status = "resolved"
        pred.resolved_at = now.isoformat()
        pred.outcome = {"result": result, "detail": detail}
        stats["resolved"] += 1
        stats[{"hit": "hits", "miss": "misses", "observed": "observed"}[result]] += 1

    save_predictions(preds, ppath)
    if new_results:
        rpath.parent.mkdir(parents=True, exist_ok=True)
        with rpath.open("a", encoding="utf-8") as fh:
            for rec in new_results:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    _write_xlsx(new_results, rpath, stats, now, xpath)
    log.info("Shadow compare: %d resolved (%d hit / %d miss / %d observed), "
             "%d pending, %d expired, %d excluded",
             stats["resolved"], stats["hits"], stats["misses"], stats["observed"],
             stats["pending"], stats["expired"], stats["excluded"])
    return stats


def _load_all_results(rpath: Path) -> list[dict]:
    if not rpath.exists():
        return []
    return [json.loads(line) for line in rpath.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def _write_xlsx(new_results, rpath, stats, now, xpath) -> None:
    from openpyxl import Workbook

    wb = Workbook()
    wb.remove(wb.active)
    decisions_rows = [[r[c] for c in ("lead_id", "rule", "decision", "predicted_account_id",
                                      "converted_account_id", "converted_account_name",
                                      "account_created", "converted_date", "result", "detail")]
                      for r in new_results]
    _add_sheet(wb, "Decisions", DECISIONS_COLUMNS, decisions_rows)

    field_rows = [[r["lead_id"], label, f["status"], str(f["lf"] or ""), str(f["human"] or "")]
                  for r in new_results for label, f in r["fields"].items()]
    _add_sheet(wb, "Fields", FIELDS_COLUMNS, field_rows)

    # Rolling summary over the FULL results history.
    history = _load_all_results(rpath)
    summary = [["generated_at", now.isoformat()],
               ["resolved_this_run", stats["resolved"]],
               ["excluded_this_run", stats["excluded"]],
               ["still_pending", stats["pending"]],
               ["total_scored_ever", len(history)]]
    by_rule: dict[str, dict] = {}
    field_agg: dict[str, dict] = {}
    for r in history:
        b = by_rule.setdefault(r["rule"], {"hit": 0, "miss": 0, "observed": 0})
        b[r["result"]] += 1
        for label, f in r.get("fields", {}).items():
            fa = field_agg.setdefault(label, {"agree": 0, "differ": 0,
                                              "human_empty": 0, "lf_empty": 0})
            fa[f["status"]] += 1
    for rule, b in sorted(by_rule.items()):
        scored = b["hit"] + b["miss"]
        acc = f"{b['hit'] / scored:.0%}" if scored else "n/a"
        summary.append([f"rule_{rule}", f"hit={b['hit']} miss={b['miss']} "
                                        f"observed={b['observed']} accuracy={acc}"])
    for label, fa in sorted(field_agg.items()):
        comparable = fa["agree"] + fa["differ"]
        rate = f"{fa['agree'] / comparable:.0%}" if comparable else "n/a"
        summary.append([f"field_{label}", f"agree={fa['agree']} differ={fa['differ']} "
                                          f"human_empty={fa['human_empty']} "
                                          f"lf_empty={fa['lf_empty']} rate={rate}"])
    _add_sheet(wb, "Summary", ["metric", "value"], summary)
    xpath.parent.mkdir(parents=True, exist_ok=True)
    wb.save(xpath)
