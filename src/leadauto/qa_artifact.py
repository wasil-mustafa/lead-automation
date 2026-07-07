"""QA artifact + review gate (Stage 1 output).

Builds the Excel workbook a human reviews before conversion. The `Review` sheet carries the
extracted/enriched signals plus the match decision and an empty `Approve?` column; the
reviewer sets `Approve?=yes`, commits the approved rows, and Stage 2 converts only those.
Other sheets surface the LeadFeeder contact split, out-of-scope skips, and a reconciling
summary. Pure data in, a `Workbook` out (write to disk via `write_workbook`).
"""
from __future__ import annotations

from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

from .convert.preflight import check_lead
from .match import RULE_DECISIONS
from .writeback import lead_field_payload

REVIEW_COLUMNS = [
    "lead_id", "task_id", "task_type", "subject",
    "company", "kununu_url", "kununu_profile_uuid", "region",
    "contact_name", "contact_email",
    "lf_matched", "lf_matched_name", "lf_match_score", "lf_name_agreement", "lf_agreed_via",
    "lf_city", "lf_employees", "lf_industry", "lf_website", "lf_company_id",
    "match_rule", "match_evidence",
    "account_match_id", "account_reason",
    "contact_match_id", "contact_score", "contact_reason",
    "convert_blockers",
    "decision", "Approve?",
]
CONTACT_FOUND_COLUMNS = [
    "lead_id", "company", "contact_name", "position",
    "contact_emails", "best_contact_phone", "contact_linkedin",
]
NOCONTACT_COLUMNS = ["lead_id", "company", "contact_name", "lf_matched"]
SKIPPED_COLUMNS = ["task_id", "lead_id", "subject", "reason"]
FAILURES_COLUMNS = ["lead_id", "stage", "error"]

_HEADER_FONT = Font(bold=True)


def _yn(value: bool) -> str:
    return "yes" if value else "no"


def _convert_blockers(result) -> str:
    """Preview of stage-2 preflight over the lead's post-write-back state.

    Current lead values overlaid with the write-back payload (fill-only-blank, so the
    overlay IS the post-run state). No proposed account match -> strictest create-new
    assumption. Empty string = would pass preflight today.
    """
    e = result.enriched.extracted
    # This key set pairs with soql._LEAD_FIELDS / check_lead's reads by convention —
    # extend both when adding a check.
    fields = {
        "IsConverted": False,
        "Street": e.lead_street, "City": e.lead_city, "PostalCode": e.lead_postal_code,
        "Country": e.lead_country, "CountryCode": e.lead_country_code,
        "Phone": e.lead_phone, "Email": e.lead_email,
        "HasOptedOutOfEmail": e.lead_opted_out_email, "LeadSource": e.lead_source,
        "KN_DBID__c": e.lead_dbid, "KN_NoOwnDBID__c": e.lead_no_own_dbid,
        "KN_Employee__c": e.lead_employee_category, "KN_Language__c": e.lead_language,
    }
    payload = lead_field_payload(result.enriched)
    fields.update((k, v) for k, v in payload.items() if k != "Id")
    fails = check_lead(fields, create_new=result.account is None)
    return "; ".join(f.check for f in fails)


def _review_row(result) -> dict:
    e = result.enriched.extracted
    enr = result.enriched
    company = enr.company or {}
    acct = result.account
    con = result.contact
    return {
        "lead_id": e.lead_id, "task_id": e.task_id, "task_type": e.task_type,
        "subject": e.subject,
        "company": e.best_company, "kununu_url": e.kununu_url, "region": e.region,
        "contact_name": e.contact_name, "contact_email": e.contact_email or e.lead_email,
        "lf_matched": _yn(enr.matched), "lf_matched_name": enr.matched_name,
        "lf_match_score": enr.match_score, "lf_name_agreement": enr.name_agreement,
        "lf_agreed_via": enr.agreed_via,
        "lf_city": company.get("City"), "lf_employees": company.get("Employee_Count"),
        "lf_industry": company.get("Industry"), "lf_website": company.get("Website"),
        "lf_company_id": enr.lf_company_id,
        "kununu_profile_uuid": e.kununu_profile_uuid,
        "match_rule": result.rule,
        "match_evidence": "; ".join(result.evidence),
        "account_match_id": acct.account_id if acct else None,
        "account_reason": acct.reason if acct else None,
        "contact_match_id": con.contact_id if con else None,
        "contact_score": con.score if con else None,
        "contact_reason": con.reason if con else None,
        "convert_blockers": _convert_blockers(result),
        "decision": result.decision, "Approve?": None,
    }


def _add_sheet(wb: Workbook, title: str, columns: list[str], rows: list[list]) -> None:
    ws = wb.create_sheet(title)
    ws.append(columns)
    for cell in ws[1]:
        cell.font = _HEADER_FONT
    for row in rows:
        ws.append(row)
    ws.freeze_panes = "A2"
    if rows or columns:
        ws.auto_filter.ref = f"A1:{get_column_letter(len(columns))}{max(len(rows) + 1, 1)}"
    for i, col in enumerate(columns, start=1):
        ws.column_dimensions[get_column_letter(i)].width = min(max(len(col) + 2, 12), 40)


def build_workbook(results: list, skipped: list, meta: dict | None = None,
                   failures: list | None = None) -> Workbook:
    """Assemble the QA workbook from match results + skip records + failures + run metadata."""
    meta = meta or {}
    failures = failures or []
    wb = Workbook()
    wb.remove(wb.active)  # drop the default empty sheet

    # Review
    review_rows = [[_review_row(r).get(c) for c in REVIEW_COLUMNS] for r in results]
    _add_sheet(wb, "Review", REVIEW_COLUMNS, review_rows)

    # Contact split
    found_rows, nocontact_rows = [], []
    for r in results:
        e = r.enriched.extracted
        if r.enriched.contact_found:
            c = r.enriched.contact or {}
            found_rows.append([
                e.lead_id, e.best_company, e.contact_name, c.get("position"),
                c.get("contact_emails"), c.get("best_contact_phone"), c.get("contact_linkedin"),
            ])
        else:
            nocontact_rows.append([e.lead_id, e.best_company, e.contact_name,
                                   _yn(r.enriched.matched)])
    _add_sheet(wb, "Contact_Found", CONTACT_FOUND_COLUMNS, found_rows)
    _add_sheet(wb, "NoContact", NOCONTACT_COLUMNS, nocontact_rows)

    # Skipped
    skipped_rows = [[s.task_id, s.lead_id, s.subject, s.reason] for s in skipped]
    _add_sheet(wb, "Skipped", SKIPPED_COLUMNS, skipped_rows)

    # Failures (LeadFeeder / write-back errors surfaced for the reviewer)
    failure_rows = [[f.get("lead_id"), f.get("stage"), f.get("error")] for f in failures]
    _add_sheet(wb, "Failures", FAILURES_COLUMNS, failure_rows)

    # Summary
    decisions = [r.decision for r in results]
    rules = [r.rule for r in results]
    summary = [
        ["generated_at", meta.get("generated_at")],
        ["since", meta.get("since")],
        ["new_watermark", meta.get("new_watermark")],
        ["dry_run", meta.get("dry_run")],
        ["total_leads", len(results)],
        ["lf_matched", sum(1 for r in results if r.enriched.matched)],
        ["contacts_found", sum(1 for r in results if r.enriched.contact_found)],
        ["decision_link", decisions.count("link")],
        ["decision_review", decisions.count("review")],
        ["decision_create_new", decisions.count("create_new")],
        ["skipped", len(skipped)],
    ]
    summary.extend(["rule_" + name, rules.count(name)] for name in RULE_DECISIONS)
    _add_sheet(wb, "Summary", ["metric", "value"], summary)

    return wb


def write_workbook(results: list, skipped: list, meta: dict | None = None,
                   path: str | Path = "qa_artifact.xlsx", failures: list | None = None) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    build_workbook(results, skipped, meta, failures).save(p)
    return p
