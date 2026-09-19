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
from leadauto.qa_artifact import (ENRICHING_COLUMNS, REVIEW_COLUMNS, build_workbook,
                                  write_workbook)
from leadauto.writeback import lead_field_payload


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
    assert set(wb.sheetnames) >= {"Review", "Enriching", "Contact_Found", "NoContact",
                                  "Skipped", "Summary"}


# --- Enriching sheet ---------------------------------------------------------

def test_enriching_sheet_is_the_write_back_payload_verbatim():
    """The sheet must BE `lead_field_payload`, not a re-derivation of it — that is what
    makes it safe to paste into Inspector Reloaded (Lead / Update / id field Id) to apply
    a dry run's enrichment by hand."""
    res = _result(company={"Website": "https://acme.de", "VAT_Id": "DE123"},
                  contact_details={"position": "Head of HR"})
    ws = build_workbook([res], [], meta={})["Enriching"]
    assert _headers(ws) == ENRICHING_COLUMNS
    payload = lead_field_payload(res.enriched)
    row = dict(zip(ENRICHING_COLUMNS, _rows(ws)[0]))
    assert row["Id"] == "00Q1"
    assert row["KN_VATNumber__c"] == payload["KN_VATNumber__c"] == "DE123"
    assert row["Title"] == payload["Title"] == "Head of HR"
    # A field the payload does not carry is blank, never a stale or invented value: an
    # import row is applied as-is, so a wrong cell here overwrites a real Lead field.
    for col in ENRICHING_COLUMNS:
        assert row[col] == payload.get(col, "")


def test_enriching_sheet_drops_rows_with_nothing_to_write():
    """A payload of only the Id means nothing was enriched, and the sheet must mirror
    `write_back`, which filters on exactly the same `len(p) > 1` (writeback.py:212).
    Keeping the row would put an all-blank line in an import file — an invitation to
    overwrite good data with nothing.

    Emptying such a lead takes some doing, which is the point: `KN_NoOwnDBID__c` is set
    for any lead WITHOUT a DBID, so a lead needs one before the payload can shrink to the
    Id alone. That is rare in practice, so the guard fires rarely — it is not dead."""
    bare = _result(lead_id="00Q_BARE")
    bare.enriched.extracted.kununu_url = None      # KN_KununuLinkProfile__c + CountryCode
    bare.enriched.extracted.region = None          # KN_Region__c
    bare.enriched.extracted.contact_email = None   # Email
    bare.enriched.extracted.contact_name = None    # FirstName
    bare.enriched.extracted.lead_dbid = "123456"   # suppresses the KN_NoOwnDBID__c tick
    assert set(lead_field_payload(bare.enriched)) == {"Id"}
    rich = _result(lead_id="00Q_RICH", company={"VAT_Id": "DE9"})
    ws = build_workbook([bare, rich], [], meta={})["Enriching"]
    assert [r[0] for r in _rows(ws)] == ["00Q_RICH"]


def test_review_header_includes_match_and_gate_columns():
    wb = build_workbook([_result()], [], meta={})
    headers = _headers(wb["Review"])
    assert headers == REVIEW_COLUMNS
    for col in ("decision", "account_match_id", "account_reason",
                "contact_match_id", "contact_score", "Approve?"):
        assert col in headers


def test_review_row_has_predicted_industry_column():
    assert "predicted_industry" in REVIEW_COLUMNS


def test_review_row_emits_predicted_industry_value():
    # predicted_industry mirrors what lead_field_payload would write to Industry;
    # the kununu feed's profile_industry_name_de ("IT") maps and overwrites.
    from leadauto.qa_artifact import _review_row
    result = _result()
    result.enriched.feed_industry = "IT"
    assert _review_row(result)["predicted_industry"] == "INTERNET and INFORMATION TECHNOLOGY"


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
    # Address comes from the LF company now (tasks carry none) — the payload can only
    # satisfy the address checks when the enrichment did.
    e = ExtractedLead(lead_id="00Q1", task_id="00T1", task_type="claimer",
                      lead_last_name="Muster", lead_language="German",
                      lead_source="Web", country_code="DE")
    # Employee band comes only from the LF headcount now (task size is never used).
    enr = EnrichedLead(extracted=e, company={"Street": "Weg 1", "City": "HH",
                                             "Postal_Code": "20095", "Country": "DE",
                                             "Employee_Count": 40})
    result = MatchResult(enriched=enr, account=None, contact=None,
                         decision="review", rule="no_lf_match", evidence=[])
    wb = build_workbook([result], skipped=[])
    ws = wb["Review"]
    header = [c.value for c in ws[1]]
    blockers = ws.cell(row=2, column=header.index("convert_blockers") + 1).value
    assert not blockers  # write-back payload satisfies employee/dbid/address checks


def test_review_sheet_carries_cluster_and_conflict_keys():
    from leadauto.extract.models import ExtractedLead
    from leadauto.leadfeeder.enrich import EnrichedLead
    from leadauto.match import MatchResult
    from leadauto.qa_artifact import REVIEW_COLUMNS, build_workbook

    assert "lf_company_id" in REVIEW_COLUMNS and "kununu_profile_uuid" in REVIEW_COLUMNS
    e = ExtractedLead(lead_id="00Q1", task_id="00T1", task_type="claimer",
                      company_name="Acme GmbH", kununu_profile_uuid="puid-123")
    enr = EnrichedLead(extracted=e, matched=True, lf_company_id="LF-999")
    res = MatchResult(enriched=enr, account=None, contact=None, decision="create_new")
    ws = build_workbook([res], [], meta={})["Review"]
    headers = [c.value for c in next(ws.iter_rows(max_row=1))]
    idx = {h: i for i, h in enumerate(headers)}
    row = list(ws.iter_rows(min_row=2, values_only=True))[0]
    assert row[idx["lf_company_id"]] == "LF-999"
    assert row[idx["kununu_profile_uuid"]] == "puid-123"


# --- live-scope columns (phased go-live, 2026-07-18) ---------------------------

def test_review_sheet_shows_kununu_score_and_live_scope_verdict():
    # Reviewers calibrate the go-live gate from this sheet: the score and the gate
    # verdict ("yes" / exclusion reason) sit on every row when settings are passed.
    assert "kununu_score" in REVIEW_COLUMNS and "live_scope" in REVIEW_COLUMNS
    passing = _result("00Q1", decision="create_new")
    passing.enriched.kununu_score = 2.5
    excluded = _result("00Q2", decision="link")
    excluded.enriched.kununu_score = 2.5
    unscored = _result("00Q3", decision="create_new")   # no feed hit -> fails closed
    ws = build_workbook([passing, excluded, unscored], [], meta={},
                        settings=_settings())["Review"]
    idx = {h: i for i, h in enumerate(_headers(ws))}
    rows = _rows(ws)
    assert rows[0][idx["kununu_score"]] == 2.5
    assert rows[0][idx["live_scope"]] == "yes"
    assert rows[1][idx["live_scope"]] == "decision_link"
    assert rows[2][idx["live_scope"]] == "score_missing"


def test_review_live_scope_blank_without_settings():
    # Legacy callers (no settings) keep working; the verdict column just stays blank.
    res = _result(decision="create_new")
    res.enriched.kununu_score = 2.5
    ws = build_workbook([res], [], meta={})["Review"]
    idx = {h: i for i, h in enumerate(_headers(ws))}
    assert _rows(ws)[0][idx["live_scope"]] in (None, "")


def test_summary_counts_live_scope_pass():
    passing = _result("00Q1", decision="create_new")
    passing.enriched.kununu_score = 1.9
    excluded = _result("00Q2", decision="create_new")
    excluded.enriched.kununu_score = 4.5
    wb = build_workbook([passing, excluded], [], meta={}, settings=_settings())
    metrics = {r[0]: r[1] for r in _rows(wb["Summary"]) if r and r[0]}
    assert metrics["live_scope_pass"] == 1


def test_summary_omits_live_scope_pass_without_settings():
    wb = build_workbook([_result()], [], meta={})
    metrics = {r[0] for r in _rows(wb["Summary"]) if r and r[0]}
    assert "live_scope_pass" not in metrics


# --- auto-approved CSV (review_gate: false, 2026-07-10; live-scope 2026-07-18) --

def _settings(**live_scope):
    from leadauto.config import Settings
    s = Settings.load()
    if live_scope:
        s.raw["pipeline"] = dict(s.raw["pipeline"], live_scope=live_scope)
    return s


def _auto_results():
    from leadauto.match import AccountMatch
    results = [
        _result("00Q000000000001", decision="link",
                account=AccountMatch(account_id="001000000000001", score=None,
                                     reason="name_address")),
        _result("00Q000000000002", decision="create_new"),
        _result("00Q000000000003", decision="review"),
        _result("00Q000000000004", decision="skip"),
    ]
    for r in results:
        r.enriched.kununu_score = 2.5      # under the live-scope cap
    return results


def test_auto_approved_csv_holds_live_scope_rows_only(tmp_path):
    # Default live scope = create_new only: the link/review/skip rows stay out even
    # though their kununu score is under the cap.
    import csv
    from leadauto.qa_artifact import write_auto_approved_csv
    path = write_auto_approved_csv(_auto_results(), tmp_path / "review", _settings())
    assert path is not None and path.name.startswith("approved_") and "auto" in path.name
    with path.open(newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    assert [r["lead_id"] for r in rows] == ["00Q000000000002"]
    assert all(r["Approve?"] == "yes" for r in rows)
    assert rows[0]["decision"] == "create_new"
    assert rows[0]["live_scope"] == "yes"


def test_auto_approved_csv_widened_decisions_include_link(tmp_path):
    # Widening the rollout is a config change: decisions [create_new, link] pulls the
    # link row in too (still score-gated).
    import csv
    from leadauto.qa_artifact import write_auto_approved_csv
    settings = _settings(decisions=["create_new", "link"], max_kununu_score=3.0)
    path = write_auto_approved_csv(_auto_results(), tmp_path / "review", settings)
    with path.open(newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    assert [r["lead_id"] for r in rows] == ["00Q000000000001", "00Q000000000002"]
    assert rows[0]["account_match_id"] == "001000000000001"


def test_auto_approved_csv_excludes_score_at_or_over_cap_and_missing(tmp_path):
    from leadauto.qa_artifact import write_auto_approved_csv
    results = _auto_results()
    results[1].enriched.kununu_score = 3.0     # boundary: 3.0 is NOT under the cap
    assert write_auto_approved_csv(results, tmp_path / "review", _settings()) is None
    results[1].enriched.kununu_score = None    # missing score fails closed
    assert write_auto_approved_csv(results, tmp_path / "review", _settings()) is None


def test_auto_approved_csv_excludes_writeback_failures(tmp_path):
    from leadauto.qa_artifact import write_auto_approved_csv
    assert write_auto_approved_csv(_auto_results(), tmp_path / "review", _settings(),
                                   exclude_lead_ids={"00Q000000000002"}) is None


def test_auto_approved_csv_skipped_when_nothing_qualifies(tmp_path):
    from leadauto.qa_artifact import write_auto_approved_csv
    only_skips = [_result("00Q000000000004", decision="skip"),
                  _result("00Q000000000003", decision="review")]
    assert write_auto_approved_csv(only_skips, tmp_path / "review", _settings()) is None
    assert not (tmp_path / "review").exists() or not list((tmp_path / "review").iterdir())


def test_auto_approved_csv_roundtrips_through_stage2(tmp_path):
    from leadauto.pipeline.stage2_convert import read_approved, targets_from_approved
    from leadauto.qa_artifact import write_auto_approved_csv
    settings = _settings(decisions=["create_new", "link"], max_kununu_score=3.0)
    path = write_auto_approved_csv(_auto_results(), tmp_path / "review", settings)
    targets = targets_from_approved(read_approved(path), settings)
    assert [t.lead_id for t in targets] == ["00Q000000000001", "00Q000000000002"]
    assert targets[0].account_id == "001000000000001"
    assert targets[1].account_id is None       # create_new stays create-new


def test_nocontact_reason_column_and_skipped_metric():
    # Wasil, 2026-09-18: a reviewer must see WHY no LF contact is attached — the lookup
    # was skipped (lead already has email and phone), LF held nobody, or no company match.
    skipped = _result("00Q1", matched=True, contact_found=False)
    skipped.enriched.contact_skipped = "lead already has email and phone"
    nobody = _result("00Q2", matched=True, contact_found=False)
    unmatched = _result("00Q3", matched=False, contact_found=False)
    wb = build_workbook([skipped, nobody, unmatched], [], meta={})
    ws = wb["NoContact"]
    assert _headers(ws)[-1] == "reason"
    reasons = {r[0]: r[-1] for r in _rows(ws)}
    assert reasons["00Q1"] == "lead already has email and phone"
    assert reasons["00Q2"] == "not found in LeadFeeder"
    assert reasons["00Q3"] == "no LeadFeeder company match"
    metrics = {r[0]: r[1] for r in _rows(wb["Summary"]) if r and r[0]}
    assert metrics["contact_lookups_skipped"] == 1
    assert metrics["contacts_found"] == 0


def test_phone_repair_sheet_lists_before_and_after(tmp_path):
    # Wasil, 2026-09-18: the phone fix gets its own sheet — one row per lead whose
    # existing `+0…` phone the write-back rewrites; leads with a correct or blank
    # phone never appear, and with the switch off the sheet is empty.
    import copy
    from leadauto.config import Settings
    raw = copy.deepcopy(Settings.load().raw)
    raw["pipeline"]["repair_national_phone"] = True
    on = Settings(raw=raw, leadfeeder_api_key=None, sf_auth_url=None)

    national = _result("00Q1")
    national.enriched.extracted.lead_phone = "+02091584338"
    national.enriched.extracted.lead_country_code = "DE"
    correct = _result("00Q2")
    correct.enriched.extracted.lead_phone = "+4940419131"
    correct.enriched.extracted.lead_country_code = "DE"
    blank = _result("00Q3", contact_found=True,
                    contact_details={"phone_direct": "+49 30 1"})

    wb = build_workbook([national, correct, blank], [], meta={}, settings=on)
    ws = wb["Phone_Repair"]
    assert _headers(ws) == ["lead_id", "company", "country", "phone_before", "phone_after"]
    rows = _rows(ws)
    assert len(rows) == 1
    assert rows[0][0] == "00Q1"
    assert rows[0][3] == "+02091584338" and rows[0][4] == "+492091584338"
    metrics = {r[0]: r[1] for r in _rows(wb["Summary"]) if r and r[0]}
    assert metrics["phones_repaired"] == 1

    off = build_workbook([national, correct, blank], [], meta={})
    assert _rows(off["Phone_Repair"]) == []


def test_address_check_sheet_compares_task_and_leadfeeder():
    # Wasil, 2026-09-18: badge-scan address (Task Kommentar "Address: Höninger Weg 104,
    # 50969 Köln, DE") vs the LeadFeeder company address vs the Lead's current address.
    same = _result("00Q1", matched=True,
                   company={"Street": "Hoeninger Weg 104", "Postal_Code": "50969",
                            "City": "Koeln", "Country": "Germany"})
    same.enriched.extracted.street, same.enriched.extracted.postal_code = "Höninger Weg 104", "50969"
    same.enriched.extracted.city, same.enriched.extracted.country_code = "Köln", "de"
    same.enriched.extracted.lead_city = "Bonn"

    city_only = _result("00Q2", matched=True,
                        company={"Postal_Code": "50667", "City": "Köln", "Country": "DE"})
    city_only.enriched.extracted.postal_code, city_only.enriched.extracted.city = "50969", "Köln"

    different = _result("00Q3", matched=True,
                        company={"Postal_Code": "80331", "City": "München", "Country": "DE"})
    different.enriched.extracted.postal_code, different.enriched.extracted.city = "50969", "Köln"

    no_task = _result("00Q4", matched=True, company={"Postal_Code": "80331", "City": "München"})
    no_lf = _result("00Q5", matched=False)
    no_lf.enriched.extracted.postal_code, no_lf.enriched.extracted.city = "50969", "Köln"

    wb = build_workbook([same, city_only, different, no_task, no_lf], [], meta={})
    ws = wb["Address_Check"]
    hdr = _headers(ws)
    rows = {r[0]: dict(zip(hdr, r)) for r in _rows(ws)}
    assert rows["00Q1"]["verdict"] == "same"
    assert rows["00Q1"]["task_cc"] == "DE" and rows["00Q1"]["lf_cc"] == "DE"
    assert rows["00Q1"]["lead_city"] == "Bonn"            # current Lead value shown too
    assert rows["00Q2"]["verdict"] == "same_city"
    assert rows["00Q2"]["postal_match"] == "no" and rows["00Q2"]["city_match"] == "yes"
    assert rows["00Q3"]["verdict"] == "different"
    assert rows["00Q4"]["verdict"] == "no_task_address"
    assert rows["00Q5"]["verdict"] == "no_lf_address"
    metrics = {r[0]: r[1] for r in _rows(wb["Summary"]) if r and r[0]}
    assert metrics["address_same"] == 1
    assert metrics["address_different"] == 1
    assert metrics["address_no_lf_address"] == 1


def test_address_check_city_compare_tolerates_umlaut_spellings_and_repeats():
    from leadauto.qa_artifact import _norm_city
    assert _norm_city("Dusseldorf") == _norm_city("Düsseldorf") == _norm_city("Duesseldorf")
    assert _norm_city("Leverkusen, 51371 Leverkusen") == _norm_city("Leverkusen")
    assert _norm_city("Bonn Bad-Godesberg") == _norm_city("Bonn")
    assert _norm_city("Köln") == _norm_city("Koeln")
    assert _norm_city("Berlin-Kreuzberg") == _norm_city("Berlin")
    assert _norm_city("Stuttgart") != _norm_city("Köln")
    # A prefix word is not the city: these pairs used to collapse to 'bad' / 'st' / 'neu'
    # and report a false city match on the Address_Check sheet.
    assert _norm_city("Bad Homburg") != _norm_city("Bad Kreuznach")
    assert _norm_city("St. Gallen") != _norm_city("St. Pölten")
    assert _norm_city("Neu-Ulm") != _norm_city("Neu-Isenburg")
    assert _norm_city("Bad Homburg v. d. Höhe") == _norm_city("Bad Homburg")
    assert _norm_city("Sankt Gallen") != _norm_city("Sankt Pölten")


def test_address_check_task_cc_is_the_badge_country_not_the_url_country():
    r = _result("00Q1", matched=True,
                company={"Postal_Code": "8001", "City": "Zürich", "Country": "Switzerland"})
    e = r.enriched.extracted
    e.postal_code, e.city = "8001", "Zürich"
    e.country_code, e.address_country_code = "DE", "CH"     # /de/ profile, CH badge
    wb = build_workbook([r], [], meta={})
    row = dict(zip(_headers(wb["Address_Check"]), _rows(wb["Address_Check"])[0]))
    assert row["task_cc"] == "CH" and row["lf_cc"] == "CH"
    # no badge country parsed -> the URL country is the fallback
    e.address_country_code = None
    wb = build_workbook([r], [], meta={})
    row = dict(zip(_headers(wb["Address_Check"]), _rows(wb["Address_Check"])[0]))
    assert row["task_cc"] == "DE"


def test_nocontact_reason_distinguishes_never_searched_from_not_found():
    from leadauto.leadfeeder.enrich import CONTACT_SKIP_NO_NAME, CONTACT_SKIP_SEARCH_FAILED
    searched = _result("00Q1", matched=True)                 # LF was asked, held nobody
    no_name = _result("00Q2", matched=True)
    no_name.enriched.contact_skipped = CONTACT_SKIP_NO_NAME
    failed = _result("00Q3", matched=True)
    failed.enriched.contact_skipped = CONTACT_SKIP_SEARCH_FAILED
    wb = build_workbook([searched, no_name, failed], [], meta={})
    ws = wb["NoContact"]
    rows = {r[0]: dict(zip(_headers(ws), r)) for r in _rows(ws)}
    assert rows["00Q1"]["reason"] == "not found in LeadFeeder"
    assert rows["00Q2"]["reason"] == CONTACT_SKIP_NO_NAME
    assert rows["00Q3"]["reason"] == CONTACT_SKIP_SEARCH_FAILED
    # neither of the new reasons is a "lookup saved" by the config gate
    metrics = {r[0]: r[1] for r in _rows(wb["Summary"]) if r and r[0]}
    assert metrics["contact_lookups_skipped"] == 0
