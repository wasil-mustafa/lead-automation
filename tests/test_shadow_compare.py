"""Shadow-compare tests: decision classification, field agreement, resolution flow.

FakeClient routes by SObject name in the SOQL string, same idea as the matcher's
loader tests."""
from datetime import datetime, timezone

from leadauto.predictions import Prediction, load_predictions, save_predictions
from leadauto.shadow_compare import run_shadow_compare

NOW = datetime(2026, 7, 4, 9, 0, tzinfo=timezone.utc)


class FakeClient:
    def __init__(self, leads=None, accounts=None, contacts=None):
        self.leads, self.accounts, self.contacts = leads or [], accounts or [], contacts or []

    def query(self, soql):
        if "FROM Lead" in soql:
            return self.leads
        if "FROM Account" in soql:
            return self.accounts
        return self.contacts


def _pred(lead_id="00Q1", decision="link", rule="name_address", account_id="001A",
          predicted_at="2026-07-03T09:00:00+00:00", **kw):
    return Prediction(lead_id=lead_id, task_id="T1", predicted_at=predicted_at,
                      rule=rule, decision=decision, account_id=account_id, **kw)


def _lead(lead_id="00Q1", account="001A", **kw):
    base = {"Id": lead_id, "IsConverted": True, "ConvertedDate": "2026-07-03",
            "ConvertedAccountId": account, "ConvertedContactId": None}
    base.update(kw)
    return base


def _account(aid="001A", created="2026-07-01T10:00:00.000+0000", **kw):
    base = {"Id": aid, "Name": "Acme GmbH", "CreatedDate": created,
            "Website": None, "BillingPostalCode": None, "BillingCity": None}
    base.update(kw)
    return base


def _run(tmp_path, preds, client):
    ppath, rpath, xpath = (tmp_path / "p.jsonl", tmp_path / "r.jsonl", tmp_path / "s.xlsx")
    save_predictions(preds, ppath)
    res = run_shadow_compare(client, predictions_path=ppath, results_path=rpath,
                             xlsx_path=xpath, now=NOW)
    return res, ppath, rpath, xpath


def test_link_hit_and_miss(tmp_path):
    preds = [_pred("00Q1", account_id="001A"), _pred("00Q2", account_id="001A")]
    client = FakeClient(
        leads=[_lead("00Q1", "001A"), _lead("00Q2", "001B")],
        accounts=[_account("001A"), _account("001B", created="2026-06-01T10:00:00.000+0000")])
    res, ppath, rpath, _ = _run(tmp_path, preds, client)
    assert res["hits"] == 1 and res["misses"] == 1 and res["resolved"] == 2
    statuses = {p.lead_id: p.status for p in load_predictions(ppath)}
    assert statuses == {"00Q1": "resolved", "00Q2": "resolved"}


def test_create_new_hit_same_day_account(tmp_path):
    # Lead.ConvertedDate is DATE-only: a new account = created the same UTC day.
    preds = [_pred("00Q1", decision="create_new", rule="no_match", account_id=None)]
    client = FakeClient(leads=[_lead()], accounts=[_account(created="2026-07-03T15:30:00.000+0000")])
    res, *_ = _run(tmp_path, preds, client)
    assert res["hits"] == 1


def test_create_new_miss_preexisting_account(tmp_path):
    preds = [_pred("00Q1", decision="create_new", rule="no_match", account_id=None)]
    client = FakeClient(leads=[_lead()], accounts=[_account(created="2025-01-01T00:00:00.000+0000")])
    res, *_ = _run(tmp_path, preds, client)
    assert res["misses"] == 1


def test_review_is_observed_not_scored(tmp_path):
    preds = [_pred("00Q1", decision="review", rule="kununu_conflict")]
    client = FakeClient(leads=[_lead()], accounts=[_account()])
    res, *_ = _run(tmp_path, preds, client)
    assert res["observed"] == 1 and res["hits"] == 0 and res["misses"] == 0


def test_bulk_export_string_booleans(tmp_path):
    # >2000-char SOQL routes through Bulk API export, which returns booleans as
    # 'true'/'false' STRINGS — a truthy 'false' must not resolve an unconverted lead.
    preds = [_pred("00Q1"), _pred("00Q2", account_id="001A")]
    client = FakeClient(leads=[
        _lead("00Q1", "", IsConverted="false", ConvertedDate="", ConvertedAccountId=""),
        _lead("00Q2", "001A", IsConverted="true")],
        accounts=[_account("001A")])
    res, ppath, *_ = _run(tmp_path, preds, client)
    statuses = {p.lead_id: p.status for p in load_predictions(ppath)}
    assert statuses == {"00Q1": "pending", "00Q2": "resolved"}
    assert res["pending"] == 1 and res["hits"] == 1


def test_internal_lead_excluded_not_scored(tmp_path):
    # Own-company lead (colleague test) -> marked excluded, never scored; others still resolve.
    preds = [_pred("00Q1", account_id="001A"), _pred("00Q2", account_id="001A")]
    client = FakeClient(
        leads=[_lead("00Q1", "001A", Email="tester@kununu.com"),
               _lead("00Q2", "001A", Email="buyer@acme.de")],
        accounts=[_account("001A")])
    ppath, rpath, xpath = (tmp_path / "p.jsonl", tmp_path / "r.jsonl", tmp_path / "s.xlsx")
    save_predictions(preds, ppath)
    res = run_shadow_compare(client, predictions_path=ppath, results_path=rpath,
                             xlsx_path=xpath, now=NOW,
                             excluded_domains={"kununu", "xing", "onlyfy"})
    assert res["excluded"] == 1 and res["hits"] == 1 and res["resolved"] == 1
    statuses = {p.lead_id: p.status for p in load_predictions(ppath)}
    assert statuses == {"00Q1": "excluded", "00Q2": "resolved"}


def test_no_excluded_domains_scores_everything(tmp_path):
    # Default (no excluded_domains passed) leaves internal-looking leads scored as before.
    preds = [_pred("00Q1", account_id="001A")]
    client = FakeClient(leads=[_lead("00Q1", "001A", Email="tester@kununu.com")],
                        accounts=[_account("001A")])
    res, ppath, *_ = _run(tmp_path, preds, client)
    assert res["excluded"] == 0 and res["hits"] == 1


def test_unconverted_stays_pending_and_old_expires(tmp_path):
    preds = [_pred("00Q1", predicted_at="2026-07-02T09:00:00+00:00"),
             _pred("00Q2", predicted_at="2026-01-01T09:00:00+00:00")]  # > 90 days
    client = FakeClient(leads=[
        _lead("00Q1", None, IsConverted=False, ConvertedDate=None, ConvertedAccountId=None),
        _lead("00Q2", None, IsConverted=False, ConvertedDate=None, ConvertedAccountId=None)])
    res, ppath, *_ = _run(tmp_path, preds, client)
    statuses = {p.lead_id: p.status for p in load_predictions(ppath)}
    assert statuses == {"00Q1": "pending", "00Q2": "expired"}
    assert res["pending"] == 1 and res["expired"] == 1


def test_field_agreement(tmp_path):
    preds = [_pred("00Q1", payload={"PostalCode": "10115", "City": "Berlin",
                                    "CountryCode": "DE", "KN_Employee__c": "51-100",
                                    "FirstName": "Anna", "Phone": "+49 30 123456"},
                   lf_website="https://acme.de")]
    client = FakeClient(
        leads=[_lead(KN_Employee__c="101-200", ConvertedContactId="003C")],
        accounts=[_account(Website="www.acme.de/jobs", BillingPostalCode="10 115",
                           BillingCity="berlin", BillingCountryCode="de")],
        contacts=[{"Id": "003C", "AccountId": "001A",
                   "FirstName": "anna", "Phone": "+49-30-123456"}])
    res, _, rpath, _ = _run(tmp_path, preds, client)
    import json
    rec = json.loads(rpath.read_text(encoding="utf-8").splitlines()[0])
    f = rec["fields"]
    assert f["postal_code"]["status"] == "agree"
    assert f["city"]["status"] == "agree"
    assert f["country"]["status"] == "agree"
    assert f["website"]["status"] == "agree"
    assert f["first_name"]["status"] == "agree"
    assert f["phone"]["status"] == "agree"
    assert f["employee_bin"]["status"] == "differ"
    assert "revenue_millions" not in f  # KN_DB* no longer written back


def test_field_agreement_legacy_payload_keys(tmp_path):
    # In-flight predictions recorded before write-back stopped writing the
    # D&B-owned KN_DB* fields must still score postal/city via the legacy keys.
    preds = [_pred("00Q1", payload={"KN_DBPostalCode__c": "10115",
                                    "KN_DBCity__c": "Berlin"})]
    client = FakeClient(
        leads=[_lead()],
        accounts=[_account(BillingPostalCode="10115", BillingCity="Berlin")])
    _, _, rpath, _ = _run(tmp_path, preds, client)
    import json
    f = json.loads(rpath.read_text(encoding="utf-8").splitlines()[0])["fields"]
    assert f["postal_code"]["status"] == "agree"
    assert f["city"]["status"] == "agree"
    assert f["country"]["status"] == "lf_empty"
    assert f["phone"]["status"] == "lf_empty"


def test_contact_fields_without_converted_contact(tmp_path):
    # No ConvertedContactId → contact-side values are simply missing.
    preds = [_pred("00Q1", payload={"FirstName": "Anna", "Phone": "+4930123456"})]
    client = FakeClient(leads=[_lead()], accounts=[_account()])
    _, _, rpath, _ = _run(tmp_path, preds, client)
    import json
    f = json.loads(rpath.read_text(encoding="utf-8").splitlines()[0])["fields"]
    assert f["first_name"]["status"] == "human_empty"
    assert f["phone"]["status"] == "human_empty"


def test_xlsx_written(tmp_path):
    preds = [_pred("00Q1")]
    client = FakeClient(leads=[_lead()], accounts=[_account()])
    _, _, _, xpath = _run(tmp_path, preds, client)
    from openpyxl import load_workbook
    wb = load_workbook(xpath)
    assert set(wb.sheetnames) == {"Decisions", "Fields", "Summary"}
