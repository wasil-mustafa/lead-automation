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
            matched=False, matched_name=None, score=None, name_agreement=None, agreed_via=None,
            company=None, contact_found=False, contact_details=None):
    e = ExtractedLead(
        lead_id=lead_id, task_id="T_" + lead_id, task_type="claimer",
        subject="Inbound Sales Task: New Claimed Profile",
        company_name="Acme GmbH", kununu_url="https://www.kununu.com/de/acme",
        region="Germany", contact_name="Max M", contact_email="max@acme.de",
    )
    enr = EnrichedLead(
        extracted=e, matched=matched, matched_name=matched_name, match_score=score,
        name_agreement=name_agreement, agreed_via=agreed_via,
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
    for col in ("decision", "account_match_id", "account_reason",
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
    assert row[idx["account_reason"]] == "kununu_profile_uuid"
    assert row[idx["contact_match_id"]] == "003C"
    assert row[idx["Approve?"]] in (None, "")  # reviewer fills this in


def test_review_row_shows_name_agreement_next_to_score():
    # Reviewers calibrate the gate from this sheet; the score alone is untrustworthy,
    # so the agreement value must sit right beside it.
    res = _result(matched=True, matched_name="Acme AG", score=0.91, name_agreement=95)
    ws = build_workbook([res], [], meta={})["Review"]
    idx = {h: i for i, h in enumerate(_headers(ws))}
    assert idx["lf_name_agreement"] == idx["lf_match_score"] + 1
    assert _rows(ws)[0][idx["lf_name_agreement"]] == 95


def test_review_row_shows_agreed_via_next_to_agreement():
    res = _result(matched=True, matched_name="Siemens Mobility GmbH", score=0.30,
                  name_agreement=96, agreed_via="alt_name:Siemens Mobility GmbH")
    ws = build_workbook([res], [], meta={})["Review"]
    idx = {h: i for i, h in enumerate(_headers(ws))}
    assert idx["lf_agreed_via"] == idx["lf_name_agreement"] + 1
    assert _rows(ws)[0][idx["lf_agreed_via"]] == "alt_name:Siemens Mobility GmbH"


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


def test_review_sheet_has_rule_and_evidence_columns():
    assert "match_rule" in REVIEW_COLUMNS and "match_evidence" in REVIEW_COLUMNS
    assert "account_score" not in REVIEW_COLUMNS
    # build one result with rule="name_only", evidence=["vat→001A"]
    res = _result(
        decision="review",
        account=AccountMatch("001A", 1.0, "kununu_profile_uuid"),
        contact=None,
    )
    res.rule = "name_only"
    res.evidence = ["vat→001A"]
    wb = build_workbook([res], [], meta={})
    ws = wb["Review"]
    headers = _headers(ws)
    idx = {h: i for i, h in enumerate(headers)}
    row = _rows(ws)[0]
    assert row[idx["match_rule"]] == "name_only"
    assert row[idx["match_evidence"]] == "vat→001A"


def test_summary_counts_rules():
    from leadauto.match import RULE_DECISIONS
    res1 = _result("00Q1", decision="review")
    res1.rule = "name_only"
    res1.evidence = ["vat→001A"]
    res2 = _result("00Q2", decision="create_new")
    res2.rule = "no_match"
    res2.evidence = []
    wb = build_workbook([res1, res2], [], meta={})
    metrics = {row[0]: row[1] for row in _rows(wb["Summary"])}
    assert metrics.get("rule_name_only") == 1
    assert metrics.get("rule_no_match") == 1
    assert metrics.get("rule_kununu_conflict") == 0


# --- convert_blockers ---------------------------------------------------------

def _blockers_result(extracted, account=None):
    return MatchResult(enriched=EnrichedLead(extracted=extracted), account=account,
                       contact=None, decision="review", rule="no_lf_match", evidence=[])


def test_convert_blockers_column_lists_failed_checks():
    e = ExtractedLead(lead_id="00Q1", task_id="00T1", task_type="claimer",
                      lead_last_name="Muster")  # everything conversion-relevant blank
    wb = build_workbook([_blockers_result(e)], skipped=[])
    ws = wb["Review"]
    header = [c.value for c in ws[1]]
    assert "convert_blockers" in header
    assert header.index("convert_blockers") < header.index("decision")
    blockers = ws.cell(row=2, column=header.index("convert_blockers") + 1).value
    assert "employee_category" in blockers and "language" in blockers
    assert "address_create_new" in blockers  # no account match -> create-new assumption


def test_convert_blockers_empty_when_payload_fills_gaps():
    e = ExtractedLead(lead_id="00Q1", task_id="00T1", task_type="claimer",
                      lead_last_name="Muster", lead_language="German",
                      lead_source="Web", street="Weg 1", city="HH",
                      postal_code="20095", country_code="DE",
                      company_size="11-50")
    wb = build_workbook([_blockers_result(e)], skipped=[])
    ws = wb["Review"]
    header = [c.value for c in ws[1]]
    blockers = ws.cell(row=2, column=header.index("convert_blockers") + 1).value
    assert not blockers  # write-back payload satisfies employee/dbid/address checks
