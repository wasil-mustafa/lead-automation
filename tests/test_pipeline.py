"""Pipeline orchestration tests (Stage 1 enrich+stage, Stage 2 gated convert).

Both stages are driven through fakes at the org boundary (a routing SfClient and an Apex
client), with LeadFeeder disabled (lf_client=None) so no credits are spent. The tests assert
the dry-run rails (no writes / no conversions), the watermark advance, and the QA artifact.
"""
import csv

import pytest
from openpyxl import load_workbook

from leadauto.config import Settings
from leadauto.convert import ConversionTarget
from leadauto.pipeline.stage1_enrich import run_stage1
from leadauto.pipeline.stage2_convert import (
    read_approved,
    run_stage2,
    targets_from_approved,
)
from leadauto.state import State, load_state

SETTINGS = Settings.load()

CLAIMER_SUBJECT = "Inbound Sales Task: New Claimed Profile"
PROFILE_UUID = "bc3e53a8-1795-489c-b966-315dc36e50e3"
USER_UUID = "7dcf0627-2a22-4c78-aebb-22976bb072a7"
CLAIMER_DESC = (
    "Das Profil Hufeland Klinikum GmbH https://www.kununu.com/de/hufeland-klinikum "
    "wurde aktiviert.\n"
    "Details: \n"
    "- Profile-Name: Hufeland Klinikum GmbH\n"
    "- Profil Type: claimed\n"
    "- Email: geschaeftsleitung@hufeland.de\n"
    "- User Name: Ramona Panknin\n"
    f"- User-ID: {USER_UUID}\n"
    f"- Profil-ID: {PROFILE_UUID}"
)


class FakeSfClient:
    def __init__(self, tasks=None, leads=None, accounts=None, contacts=None):
        self._tasks = tasks or []
        self._leads = leads or []
        self._accounts = accounts or []
        self._contacts = contacts or []
        self.updates: list[tuple] = []

    def query(self, soql, bulk=False):
        if "FROM Task" in soql:
            return [dict(t) for t in self._tasks]
        if "FROM Lead" in soql:
            return [dict(l) for l in self._leads]
        if "FROM Account" in soql:
            return [dict(a) for a in self._accounts]
        if "FROM Contact" in soql:
            return [dict(c) for c in self._contacts]
        return []

    def update_bulk(self, sobject, records):
        self.updates.append((sobject, records))
        return {"processed": len(records)}


def _task(tid="T1", who="00Q1", created="2026-06-19T10:00:00.000+0000"):
    return {"Id": tid, "WhoId": who, "Subject": CLAIMER_SUBJECT,
            "Description": CLAIMER_DESC, "CreatedDate": created}


def _lead(lid="00Q1"):
    return {"Id": lid, "FirstName": "Ramona", "LastName": "Panknin",
            "Company": "Hufeland Klinikum GmbH", "Email": "r@hufeland.de", "Website": None,
            "Status": "Open", "IsConverted": False, "LeadSource": "kununu",
            "KN_KununuProfileName__c": None, "KN_KununuUserUUID__c": None,
            "KN_KununuLinkProfile__c": None}


def _account(aid="001A"):
    return {"Id": aid, "Name": "Hufeland Klinikum", "Website": None,
            "KN_KununuProfileUuid__c": PROFILE_UUID, "KN_KununuLinkProfile__c": None,
            "KN_VATNumber__c": None, "KN_DBID__c": None, "KN_DBLegalCompanyName__c": None}


def _contact(cid="003C", aid="001A"):
    return {"Id": cid, "AccountId": aid, "Email": "geschaeftsleitung@hufeland.de",
            "KN_KununuUserUUID__c": USER_UUID, "KN_LinkedInProfile__c": None,
            "FirstName": "Ramona", "LastName": "Panknin", "Name": "Ramona Panknin"}


# --- Stage 1 -----------------------------------------------------------------

def test_stage1_matches_links_and_advances_watermark(tmp_path):
    sf = FakeSfClient(tasks=[_task()], leads=[_lead()],
                      accounts=[_account()], contacts=[_contact()])
    qa = tmp_path / "qa.xlsx"
    state_path = tmp_path / "watermark.json"
    res = run_stage1(sf, None, SETTINGS, state=State(None, []), since="2026-05-01T00:00:00Z",
                     dry_run=True, qa_path=qa, state_path=state_path, predictions_path=None)
    assert len(res.results) == 1
    r = res.results[0]
    # LeadFeeder is disabled here (lf_client=None) so the lead never gets an accepted LF
    # company match; rule 1 (no_lf_match) always reviews, even though the account carries
    # the same kununu profile UUID the task's Description parses out (see brief invariant).
    assert r.rule == "no_lf_match"
    assert r.decision == "review"
    assert r.account is None
    # Contact matching is independent of the account rule: the kununu user UUID identifier
    # signal still resolves the existing Contact.
    assert r.contact.contact_id == "003C"
    # watermark advanced to the task's CreatedDate
    assert res.new_state.last_task_created == "2026-06-19T10:00:00.000+0000"
    assert "T1" in res.new_state.processed_task_ids
    # QA artifact written and readable
    assert qa.exists()
    assert "Review" in load_workbook(qa).sheetnames


def test_stage1_dry_run_writes_nothing_and_does_not_persist_state(tmp_path):
    sf = FakeSfClient(tasks=[_task()], leads=[_lead()],
                      accounts=[_account()], contacts=[_contact()])
    state_path = tmp_path / "watermark.json"
    res = run_stage1(sf, None, SETTINGS, state=State(None, []), since="2026-05-01T00:00:00Z",
                     dry_run=True, qa_path=tmp_path / "qa.xlsx", state_path=state_path,
                     predictions_path=None)
    assert sf.updates == []                 # no write-back
    assert res.writeback["processed"] == 0
    assert not state_path.exists()           # watermark not persisted in dry-run


def test_stage1_live_writes_back_and_persists_state(tmp_path):
    sf = FakeSfClient(tasks=[_task()], leads=[_lead()],
                      accounts=[_account()], contacts=[_contact()])
    state_path = tmp_path / "watermark.json"
    res = run_stage1(sf, None, SETTINGS, state=State(None, []), since="2026-05-01T00:00:00Z",
                     dry_run=False, qa_path=tmp_path / "qa.xlsx", state_path=state_path)
    assert len(sf.updates) == 1
    sobject, records = sf.updates[0]
    assert sobject == "Lead"
    assert records[0]["Id"] == "00Q1"
    assert records[0]["KN_KununuProfileName__c"] == "Hufeland Klinikum GmbH"
    assert state_path.exists()
    assert load_state(state_path).last_task_created == "2026-06-19T10:00:00.000+0000"


# --- Stage 2: approved -> targets --------------------------------------------
# targets_from_approved enforces the 15/18-char alphanumeric Salesforce Id shape (the
# approved CSV is hand-edited and its Ids are embedded in conversion Apex), so test rows
# must use realistic Ids rather than the "00Q1" shorthand used for pure Apex-string asserts.
LEAD1 = "00Q000000000001"
LEAD2 = "00Q000000000002"
LEAD3 = "00Q000000000003"
ACCT1 = "001000000000001"
ACCT2 = "001000000000002"
CONT1 = "003000000000001"
CONT2 = "003000000000002"


def test_contact_linked_only_on_identifier_reasons():
    rows = [
        {"Approve?": "yes", "lead_id": LEAD1, "account_match_id": ACCT1,
         "contact_match_id": CONT1, "contact_reason": "kununu_user_uuid"},
        {"Approve?": "yes", "lead_id": LEAD2, "account_match_id": ACCT1,
         "contact_match_id": CONT2, "contact_reason": "contact_email_global"},
    ]
    targets = targets_from_approved(rows, SETTINGS)
    assert targets[0].contact_id == CONT1
    assert targets[1].contact_id is None  # weak reason -> create fresh contact


def test_targets_from_approved_skips_unapproved():
    rows = [
        {"Approve?": "", "lead_id": LEAD1, "account_match_id": ACCT1},
        {"Approve?": "no", "lead_id": LEAD2, "account_match_id": ACCT2},
        {"Approve?": "yes", "lead_id": LEAD3, "account_match_id": "", "contact_match_id": "",
         "decision": "create_new"},
    ]
    targets = targets_from_approved(rows, SETTINGS)
    assert targets == [ConversionTarget(LEAD3, account_id=None, contact_id=None)]


def test_targets_from_approved_rejects_malformed_id():
    # A malformed Id would be embedded in conversion Apex; fail the whole run loudly instead.
    rows = [{"Approve?": "yes", "lead_id": "00Q1", "account_match_id": ""}]
    with pytest.raises(ValueError, match="Malformed Salesforce Id"):
        targets_from_approved(rows, SETTINGS)


def test_contact_link_dropped_without_account_link():
    # setContactId requires setAccountId; a contact link with no account link would always
    # fail at convert time, so it is dropped and the lead converts create-new.
    rows = [{"Approve?": "yes", "lead_id": LEAD1, "account_match_id": "",
             "contact_match_id": CONT1, "contact_reason": "kununu_user_uuid"}]
    targets = targets_from_approved(rows, SETTINGS)
    assert targets == [ConversionTarget(LEAD1, account_id=None, contact_id=None)]


# --- Stage 2 orchestration ---------------------------------------------------

class FakeApexClient:
    def __init__(self):
        self.apex_calls = []

    def apex_run(self, apex):
        self.apex_calls.append(apex)
        from leadauto.convert import SENTINEL
        import json
        import re
        head = apex.split("Map<Id,Id>", 1)[0]
        ids = list(dict.fromkeys(re.findall(r"'(00Q[0-9A-Za-z]+)'", head)))
        out = [{"leadId": i, "success": True, "accountId": "001X"} for i in ids]
        return f"{SENTINEL}{json.dumps(out)}{SENTINEL}"


def _write_approved(path, rows):
    cols = ["lead_id", "account_match_id", "contact_match_id", "contact_score",
            "decision", "Approve?"]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in cols})


def test_read_approved_reads_rows(tmp_path):
    p = tmp_path / "approved.csv"
    _write_approved(p, [{"lead_id": "00Q1", "Approve?": "yes"}])
    rows = read_approved(p)
    assert rows[0]["lead_id"] == "00Q1" and rows[0]["Approve?"] == "yes"


def test_run_stage2_dry_run_makes_no_conversions(tmp_path):
    p = tmp_path / "approved.csv"
    _write_approved(p, [{"lead_id": LEAD1, "account_match_id": ACCT1,
                         "contact_score": "1.0", "Approve?": "yes"}])
    client = FakeApexClient()
    res = run_stage2(client, SETTINGS, p, dry_run=True, preflight=False)
    assert client.apex_calls == []
    assert res.result["converted"] == 0
    assert res.result["planned"] == 1


def test_run_stage2_live_converts_approved(tmp_path):
    p = tmp_path / "approved.csv"
    _write_approved(p, [
        {"lead_id": LEAD1, "account_match_id": ACCT1, "contact_score": "1.0", "Approve?": "yes"},
        {"lead_id": LEAD2, "Approve?": "no"},
    ])
    client = FakeApexClient()
    res = run_stage2(client, SETTINGS, p, dry_run=False, preflight=False)
    assert len(client.apex_calls) == 1
    assert res.result["converted"] == 1   # only the approved lead
    assert res.result["missing"] == 0     # the converted lead was reconciled in the results


# --- Stage 2: preflight gate --------------------------------------------------

import csv as _csv

_GOOD_ROW = {
    "IsConverted": False, "KN_Employee__c": "11-50", "KN_DBID__c": "",
    "KN_NoOwnDBID__c": "true", "KN_Language__c": "German", "Street": "Weg 1",
    "Country": "Germany", "CountryCode": "DE", "PostalCode": "20095",
    "Phone": "", "Email": "info@acme.de", "HasOptedOutOfEmail": False, "LeadSource": "Web",
}


class _PreflightSfFake:
    """query() answers the preflight SOQL; apex_run echoes success for every lead it sees."""
    def __init__(self, rows):
        self.rows = rows
        self.apex_calls = []

    def query(self, soql):
        return self.rows

    def apex_run(self, apex):
        import json
        import re

        from leadauto.convert import SENTINEL
        self.apex_calls.append(apex)
        head = apex.split("Map<Id,Id>", 1)[0]
        ids = list(dict.fromkeys(re.findall(r"'(00Q[0-9A-Za-z]+)'", head)))
        out = [{"leadId": i, "success": True} for i in ids]
        return f"{SENTINEL}{json.dumps(out)}{SENTINEL}"


class _PreflightSettings:
    dry_run = False
    owner_id = "005X"
    converted_status = "Qualified"


def _approved_csv(tmp_path, rows, name="approved_x.csv"):
    p = tmp_path / name
    cols = ["lead_id", "account_match_id", "contact_match_id", "contact_reason", "Approve?"]
    with p.open("w", newline="", encoding="utf-8") as fh:
        w = _csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    return p


def test_stage2_preflight_excludes_failing_leads(tmp_path):
    approved = _approved_csv(tmp_path, [
        {"lead_id": LEAD1, "Approve?": "yes"},
        {"lead_id": LEAD2, "Approve?": "yes"},
    ])
    client = _PreflightSfFake([dict(_GOOD_ROW, Id=LEAD1),
                               dict(_GOOD_ROW, Id=LEAD2, KN_Language__c="")])
    res = run_stage2(client, _PreflightSettings(), approved,
                     failures_csv=tmp_path / "preflight_failures.csv")
    assert [t.lead_id for t in res.targets] == [LEAD1]
    assert res.preflight_failures[0]["lead_id"] == LEAD2
    assert LEAD2 not in "".join(client.apex_calls)
    assert (tmp_path / "preflight_failures.csv").exists()


def test_stage2_no_preflight_bypasses(tmp_path):
    approved = _approved_csv(tmp_path, [{"lead_id": LEAD2, "Approve?": "yes"}], "approved_y.csv")
    client = _PreflightSfFake([])
    res = run_stage2(client, _PreflightSettings(), approved, preflight=False,
                     failures_csv=tmp_path / "pf.csv")
    assert [t.lead_id for t in res.targets] == [LEAD2]
    assert res.preflight_failures == []
    assert not (tmp_path / "pf.csv").exists()


def test_stage2_dry_run_still_preflights(tmp_path):
    approved = _approved_csv(tmp_path, [{"lead_id": LEAD2, "Approve?": "yes"}], "approved_z.csv")
    client = _PreflightSfFake([dict(_GOOD_ROW, Id=LEAD2, KN_Employee__c="")])
    res = run_stage2(client, _PreflightSettings(), approved, dry_run=True,
                     failures_csv=tmp_path / "pf.csv")
    assert res.preflight_failures and not client.apex_calls


def test_stage2_preflight_writes_csv_even_with_zero_targets(tmp_path):
    # All rows unapproved -> zero targets; preflight (default on) must still write the
    # header-only CSV so the CI artifact always shows preflight ran.
    approved = _approved_csv(tmp_path, [{"lead_id": "00Q_NO", "Approve?": "no"}], "approved_w.csv")
    client = _PreflightSfFake([])
    res = run_stage2(client, _PreflightSettings(), approved,
                     failures_csv=tmp_path / "pf.csv")
    assert res.targets == []
    assert res.preflight_failures == []
    assert not client.apex_calls
    assert (tmp_path / "pf.csv").exists()
    assert (tmp_path / "pf.csv").read_text(encoding="utf-8").strip() == "lead_id,check,value,detail"
