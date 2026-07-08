"""Backtest of SKIPPED tasks: would the rule engine have found the right account?

Stage 1 skips every in-scope fresh Task whose WhoId does not resolve through
`leads_by_ids` ("lead not found or already converted"). In practice almost all of
those WhoIds are Contacts (prefix 003), because Salesforce re-parents a Lead's open
Tasks onto the new Contact at conversion. Two case kinds, each with a known ground
truth ("the account a human decided this belongs to"):

  * converted_lead — the WhoId Contact traces back to a converted Lead
    (Lead.ConvertedContactId). Input = Task + the original Lead record, exactly what
    Stage 1 would have seen pre-conversion. Truth = Lead.ConvertedAccountId.
  * contact_task — no Lead ever existed; the Task was logged directly on an existing
    Contact. Input = Task Description only (parsers take everything but lead_* fields
    from it). Truth = Contact.AccountId.

Every truth account exists in the org NOW, so the correct engine outcome is always
link (or a review that surfaces it). One nuance: where the human CREATED the account
at conversion (Account.CreatedDate on the conversion day), an engine `create_new`
agrees with the human's original action — it is reported as `create_agree` rather
than a plain miss, but it still means the engine cannot re-find that account today
(duplicate risk on a repeat lead).

Read-only against Salesforce; spends LeadFeeder company-retrieve credits only
(`include_contacts=False`).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .extract.models import ExtractedLead
from .extract.parsers import extract_lead
from .extract.reader import newest_task_per_who
from .leadfeeder.enrich import enrich_leads
from .logging import get_logger
from .match import MatchResult, load_existing_records, match_lead
from .qa_artifact import _add_sheet
from .sf import soql
from .sf.soql import _in_list
from .shadow_compare import _is_new_account, _is_true
from .state import REPO_ROOT, compute_since, load_state
from .textnorm import is_internal_email

DEFAULT_XLSX_PATH = REPO_ROOT / "backtest_skipped.xlsx"

_CASE_CONTACT_FIELDS = ("Id, AccountId, Email, FirstName, LastName, "
                        "KN_KununuUserUUID__c")
_CASE_LEAD_QUERY_FIELDS = (
    soql._LEAD_FIELDS + ", ConvertedDate, ConvertedAccountId, ConvertedContactId"
)

CASES_COLUMNS = [
    "task_id", "subject", "kind", "who_id", "input_company", "lead_id",
    "lf_matched", "lf_company", "name_agreement", "agreed_via",
    "rule", "decision", "predicted_account_id",
    "truth_account_id", "truth_account_name", "human_created_new",
    "result", "evidence", "contact_hit",
]


@dataclass
class BacktestCase:
    task: dict
    kind: str                      # "converted_lead" | "contact_task"
    truth_account_id: str
    truth_contact_id: str | None
    lead: dict | None = None       # converted Lead record (converted_lead only)
    extracted: ExtractedLead | None = None


def _candidate_account_ids(result: MatchResult) -> set[str]:
    """Every account the result points a human at: the matched account + evidence Ids."""
    ids: set[str] = set()
    if result.account:
        ids.add(result.account.account_id)
    for item in result.evidence:
        ids.update(re.findall(r"001\w{12,15}", item))
    return ids


def classify_case(result: MatchResult, truth_account_id: str,
                  human_created_new: bool) -> tuple[str, str]:
    """(result label, detail). The truth account exists in the org at backtest time,
    so link-to-truth is a hit, a review surfacing it is a soft hit, everything else
    is a miss — except `create_new` where the human also created the account."""
    predicted = result.account.account_id if result.account else None
    if result.decision == "skip":
        return "coverage_gap", "no accepted LF match; pipeline would drop the lead"
    if result.decision == "link":
        if predicted == truth_account_id:
            return "hit_link", "linked the correct account"
        return "miss_wrong_account", f"linked {predicted}, human chose {truth_account_id}"
    if result.decision == "review":
        if truth_account_id in _candidate_account_ids(result):
            return "hit_review", "review row surfaces the correct account"
        return "miss_review", "review row surfaces only wrong candidates"
    # create_new
    if human_created_new:
        return "create_agree", ("engine agrees with the human's create-new, but cannot "
                                "re-find the created account today (duplicate risk)")
    return "miss_create_new", (f"would create a duplicate of pre-existing "
                               f"{truth_account_id}")


def build_cases(sf_client, settings, since: str, logger=None) -> tuple[list[BacktestCase], dict]:
    """Rebuild the skipped population from the org and attach ground truth."""
    log = logger or get_logger("backtest")
    counts: dict[str, int] = {}

    tasks = sf_client.query(soql.tasks_since(since, settings.in_scope_subjects,
                                             settings.fresh_task_statuses))
    tasks = newest_task_per_who(tasks)
    counts["in_scope_tasks"] = len(tasks)

    contact_ids = sorted({t["WhoId"] for t in tasks
                          if str(t.get("WhoId") or "").startswith("003")})
    lead_ids = sorted({t["WhoId"] for t in tasks
                       if str(t.get("WhoId") or "").startswith("00Q")})
    counts["contact_whoids"] = len(contact_ids)

    contacts = sf_client.query(
        f"SELECT {_CASE_CONTACT_FIELDS} FROM Contact WHERE Id IN ({_in_list(contact_ids)})"
    ) if contact_ids else []
    contact_by_id = {c["Id"]: c for c in contacts}

    # Converted leads behind the contacts (task got re-parented at conversion).
    conv_leads = sf_client.query(
        f"SELECT {_CASE_LEAD_QUERY_FIELDS} FROM Lead "
        f"WHERE ConvertedContactId IN ({_in_list(contact_ids)})"
    ) if contact_ids else []
    # Lead-WhoId tasks whose lead has been converted since extraction saw them.
    if lead_ids:
        conv_leads += [
            l for l in sf_client.query(
                f"SELECT {_CASE_LEAD_QUERY_FIELDS} FROM Lead "
                f"WHERE Id IN ({_in_list(lead_ids)})")
            if _is_true(l.get("IsConverted"))
        ]
    lead_by_contact: dict[str, dict] = {}
    for l in conv_leads:
        cid = l.get("ConvertedContactId")
        if not cid:
            continue
        cur = lead_by_contact.get(cid)
        if cur is None or str(l.get("ConvertedDate") or "") > str(cur.get("ConvertedDate") or ""):
            lead_by_contact[cid] = l
    counts["contacts_with_converted_lead"] = len(lead_by_contact)

    cases: list[BacktestCase] = []
    for task in tasks:
        who = str(task.get("WhoId") or "")
        contact = contact_by_id.get(who)
        if contact is None:
            continue  # lead-WhoId task (pipeline handles it) or unresolvable
        lead = lead_by_contact.get(who)
        if lead is not None and lead.get("ConvertedAccountId"):
            case = BacktestCase(task=task, kind="converted_lead",
                                truth_account_id=lead["ConvertedAccountId"],
                                truth_contact_id=who, lead=lead)
            extract_input = lead
        elif contact.get("AccountId"):
            case = BacktestCase(task=task, kind="contact_task",
                                truth_account_id=contact["AccountId"],
                                truth_contact_id=who)
            # No lead ever existed: the parser gets everything from the Task
            # Description; only the row identifier comes from the contact.
            extract_input = {"Id": who}
        else:
            counts["no_truth_account"] = counts.get("no_truth_account", 0) + 1
            continue

        extracted = extract_lead(task, extract_input, settings)
        if extracted is None:
            counts["unparseable"] = counts.get("unparseable", 0) + 1
            continue
        internal = settings.excluded_email_domains
        if is_internal_email(extracted.lead_email, internal) or \
                is_internal_email(extracted.contact_email, internal) or \
                is_internal_email(contact.get("Email"), internal):
            counts["internal_email_excluded"] = counts.get("internal_email_excluded", 0) + 1
            continue
        case.extracted = extracted
        cases.append(case)

    counts["cases"] = len(cases)
    log.info("Backtest cases: %s", counts)
    return cases, counts


def run_backtest(sf_client, lf_client, settings, *, since: str | None = None,
                 xlsx_path: str | Path | None = None, now: datetime | None = None,
                 kununu_feed: bool = True, logger=None) -> dict:
    log = logger or get_logger("backtest")
    now = now or datetime.now(timezone.utc)
    xpath = Path(xlsx_path) if xlsx_path else DEFAULT_XLSX_PATH
    if since is None:
        since = compute_since(load_state(), settings.task_lookback_days, now)
    log.info("Backtest start (since=%s)", since)

    cases, counts = build_cases(sf_client, settings, since, logger=log)
    if not cases:
        log.info("No backtest cases found")
        return {"counts": counts, "results": [], "xlsx": None}

    extracteds = [c.extracted for c in cases]
    if lf_client is not None:
        enriched = enrich_leads(lf_client, extracteds, settings, logger=log,
                                use_kununu_feed=kununu_feed, include_contacts=False)
    else:
        from .leadfeeder.enrich import EnrichedLead
        log.warning("LeadFeeder disabled; every case will land on no_lf_match")
        enriched = [EnrichedLead(extracted=e) for e in extracteds]

    # Stage 1 batches are small; a 250+-case backtest pushes the one-shot candidate
    # query past Salesforce's 100k-char SOQL limit. Chunk the match phase instead —
    # candidate loading is per-batch by construction, so results are identical.
    match_results: list[MatchResult] = []
    chunk = 40
    for i in range(0, len(enriched), chunk):
        batch = enriched[i:i + chunk]
        existing = load_existing_records(sf_client, batch, settings, logger=log)
        match_results += [match_lead(e, existing, settings) for e in batch]

    truth_ids = sorted({c.truth_account_id for c in cases})
    truth_accounts = {a["Id"]: a for a in sf_client.query(soql.accounts_by_ids(truth_ids))}

    rows: list[dict] = []
    tally: dict[str, int] = {}
    for case, res in zip(cases, match_results):
        acct = truth_accounts.get(case.truth_account_id)
        converted_date = (case.lead or {}).get("ConvertedDate")
        human_created_new = (case.kind == "converted_lead"
                             and _is_new_account(acct, converted_date))
        label, detail = classify_case(res, case.truth_account_id, human_created_new)
        tally[label] = tally.get(label, 0) + 1
        contact_hit = bool(res.contact and case.truth_contact_id
                           and res.contact.contact_id == case.truth_contact_id)
        rows.append({
            "task_id": case.task.get("Id"), "subject": case.task.get("Subject"),
            "kind": case.kind, "who_id": case.task.get("WhoId"),
            "input_company": case.extracted.best_company,
            "lead_id": (case.lead or {}).get("Id"),
            "lf_matched": res.enriched.matched, "lf_company": res.enriched.matched_name,
            "name_agreement": res.enriched.name_agreement,
            "agreed_via": res.enriched.agreed_via,
            "rule": res.rule, "decision": res.decision,
            "predicted_account_id": res.account.account_id if res.account else None,
            "truth_account_id": case.truth_account_id,
            "truth_account_name": (acct or {}).get("Name"),
            "human_created_new": human_created_new,
            "result": label, "evidence": "; ".join(res.evidence), "detail": detail,
            "contact_hit": contact_hit,
        })

    _write_xlsx(rows, tally, counts, since, now, xpath)
    log.info("Backtest done: %s -> %s", tally, xpath)
    return {"counts": counts, "tally": tally, "results": rows, "xlsx": str(xpath)}


def _write_xlsx(rows, tally, counts, since, now, xpath: Path) -> None:
    from openpyxl import Workbook

    wb = Workbook()
    wb.remove(wb.active)
    _add_sheet(wb, "Cases", CASES_COLUMNS + ["detail"],
               [[r[c] for c in CASES_COLUMNS + ["detail"]] for r in rows])

    summary = [["generated_at", now.isoformat()], ["since", since]]
    summary += [[f"count_{k}", v] for k, v in counts.items()]
    matched = sum(1 for r in rows if r["lf_matched"])
    summary.append(["lf_match_rate", f"{matched}/{len(rows)}" if rows else "n/a"])
    for label in sorted(tally):
        summary.append([f"result_{label}", tally[label]])
    by_kind: dict[tuple[str, str], int] = {}
    by_rule: dict[tuple[str, str], int] = {}
    for r in rows:
        by_kind[(r["kind"], r["result"])] = by_kind.get((r["kind"], r["result"]), 0) + 1
        by_rule[(r["rule"], r["result"])] = by_rule.get((r["rule"], r["result"]), 0) + 1
    for (kind, label), n in sorted(by_kind.items()):
        summary.append([f"{kind}.{label}", n])
    for (rule, label), n in sorted(by_rule.items()):
        summary.append([f"rule.{rule}.{label}", n])
    contact_hits = sum(1 for r in rows if r["contact_hit"])
    summary.append(["contact_hits", f"{contact_hits}/{len(rows)}"])
    _add_sheet(wb, "Summary", ["metric", "value"], summary)

    xpath.parent.mkdir(parents=True, exist_ok=True)
    wb.save(xpath)
