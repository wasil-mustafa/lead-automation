"""QA-artifact tests (the review gate).

We build the workbook in memory and read cells back with openpyxl, so the test verifies the
real sheet structure (headers incl. the Approve? gate, per-decision content, the Skipped
reasons, and the Summary counts) without writing to disk except for the round-trip test.
"""
from openpyxl import load_workbook

from leadauto.extract.models import ExtractedLead
from leadauto.extract.reader import Skipped
from leadauto.leadfeeder.enrich import EnrichedLead
from leadauto.match import AccountMatch, ContactMatch, MatchResult
from leadauto.qa_artifact import REVIEW_COLUMNS, build_workbook, write_workbook


def _result(lead_id="00Q1", *, decision="link", account=None, contact=None,
            matched=False, matched_name=None, score=None,
            company=None, contact_found=False, contact_details=None):
    e = ExtractedLead(
        lead_id=lead_id, task_id="T_" + lead_id, task_type="claimer",
        subject="Inbound Sales Task: New Claimed Profile",
        company_name="Acme GmbH", kununu_url="https://www.kununu.com/de/acme",
        region="Germany", contact_name="Max M", contact_email="max@acme.de",
    )
    enr = EnrichedLead(
        extracted=e, matched=matched, matched_name=matched_name, match_score=score,
        company=company or {}, contact_found=contact_found, contact=contact_details or {},
    )
    return MatchResult(enriched=enr, account=account, contact=contact, decision=decision)


def _headers(ws):
    return [c.value for c in next(ws.iter_rows(max_row=1))]


def _rows(ws):
    return list(ws.iter_rows(min_row=2, values_only=True))


# --- sheets / structure ------------------------------------------------------

def test_workbook_has_expected_sheets():
    wb = build_workbook([_result()], [], meta={})
    assert set(wb.sheetnames) >= {"Review", "Contact_Found", "NoContact", "Skipped", "Summary"}


def test_review_header_includes_match_and_gate_columns():
    wb = build_workbook([_result()], [], meta={})
    headers = _headers(wb["Review"])
    assert headers == REVIEW_COLUMNS
    for col in ("decision", "account_match_id", "account_score", "account_reason",
                "contact_match_id", "contact_score", "Approve?"):
        assert col in headers


def test_review_row_maps_match_result():
    res = _result(
        decision="link",
        account=AccountMatch("001A", 1.0, "kununu_profile_uuid"),
        contact=ContactMatch("003C", 1.0, "kununu_user_uuid"),
        matched=True, matched_name="Acme AG", score=0.91,
    )
    wb = build_workbook([res], [], meta={})
    ws = wb["Review"]
    idx = {h: i for i, h in enumerate(_headers(ws))}
    row = _rows(ws)[0]
    assert row[idx["lead_id"]] == "00Q1"
    assert row[idx["decision"]] == "link"
    assert row[idx["account_match_id"]] == "001A"
    assert row[idx["account_score"]] == 1.0
    assert row[idx["account_reason"]] == "kununu_profile_uuid"
    assert row[idx["contact_match_id"]] == "003C"
    assert row[idx["Approve?"]] in (None, "")  # reviewer fills this in


def test_create_new_row_has_blank_account_columns():
    res = _result(decision="create_new", account=None, contact=None)
    ws = build_workbook([res], [], meta={})["Review"]
    idx = {h: i for i, h in enumerate(_headers(ws))}
    row = _rows(ws)[0]
    assert row[idx["decision"]] == "create_new"
    assert row[idx["account_match_id"]] in (None, "")


# --- contact split -----------------------------------------------------------

def test_contact_found_and_nocontact_routing():
    found = _result("00Q1", contact_found=True,
                    contact_details={"position": "CTO", "contact_emails": "max@acme.de",
                                     "best_contact_phone": "+49 30 1", "contact_linkedin": "u/max"})
    missing = _result("00Q2", contact_found=False)
    wb = build_workbook([found, missing], [], meta={})
    found_ids = [r[0] for r in _rows(wb["Contact_Found"])]
    nocontact_ids = [r[0] for r in _rows(wb["NoContact"])]
    assert "00Q1" in found_ids and "00Q1" not in nocontact_ids
    assert "00Q2" in nocontact_ids and "00Q2" not in found_ids


# --- skipped + summary -------------------------------------------------------

def test_skipped_sheet_lists_reasons():
    skipped = [Skipped("T9", "00Q9", "Inbound Sales Task: Kontaktanfrage", "subject out of scope")]
    ws = build_workbook([], skipped, meta={})["Skipped"]
    row = _rows(ws)[0]
    assert "00Q9" in row and "subject out of scope" in row


def test_summary_counts_and_meta():
    results = [
        _result("00Q1", decision="link", matched=True),
        _result("00Q2", decision="review", matched=True),
        _result("00Q3", decision="create_new"),
    ]
    skipped = [Skipped("T9", "00Q9", "subj", "out of scope")]
    wb = build_workbook(results, skipped, meta={"since": "2026-05-17T00:00:00Z", "dry_run": True})
    summary = {r[0]: r[1] for r in _rows(wb["Summary"]) if r and r[0]}
    assert summary["total_leads"] == 3
    assert summary["decision_link"] == 1
    assert summary["decision_review"] == 1
    assert summary["decision_create_new"] == 1
    assert summary["lf_matched"] == 2
    assert summary["skipped"] == 1
    assert summary["since"] == "2026-05-17T00:00:00Z"


def test_failures_sheet_lists_failures():
    failures = [{"lead_id": "00Q1", "stage": "writeback", "error": "BAD_PICKLIST: bad value"}]
    wb = build_workbook([], [], meta={}, failures=failures)
    assert "Failures" in wb.sheetnames
    row = _rows(wb["Failures"])[0]
    assert "00Q1" in row and "writeback" in row and "BAD_PICKLIST: bad value" in row


def test_write_workbook_roundtrips(tmp_path):
    path = tmp_path / "qa.xlsx"
    out = write_workbook([_result()], [], meta={}, path=path)
    assert out == path and path.exists()
    reopened = load_workbook(path)
    assert "Review" in reopened.sheetnames
