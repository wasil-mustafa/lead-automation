"""Backtest-skipped tests: case building from re-parented tasks, classification,
and the end-to-end no-LF smoke path (everything lands on coverage_gap).

FakeClient routes by SObject / WHERE shape, mirroring the reader and shadow tests.
"""
from datetime import datetime, timezone

from leadauto.backtest_skipped import (
    _candidate_account_ids,
    build_cases,
    classify_case,
    run_backtest,
)
from leadauto.config import Settings
from leadauto.models import ExtractedLead
from leadauto.models import EnrichedLead
from leadauto.models import AccountMatch, MatchResult

CLAIMER_SUBJECT = "Inbound Sales Task: New Claimed Profile"
CLAIMER_DESC = (
    "Das Profil Acme GmbH https://www.kununu.com/de/acme wurde aktiviert.\n"
    "- Profile-Name: Acme GmbH\n"
    "- Email: chef@acme.de\n"
    "- User Name: Anna Muster\n"
    "- User-ID: 7dcf0627-2a22-4c78-aebb-22976bb072a7\n"
    "- Profil-ID: bc3e53a8-1795-489c-b966-315dc36e50e3"
)
SINCE = "2026-06-01T00:00:00Z"
NOW = datetime(2026, 7, 8, 9, 0, tzinfo=timezone.utc)


class FakeClient:
    def __init__(self, tasks=None, contacts=None, converted_leads=None,
                 leads_by_id=None, accounts=None):
        self.tasks = tasks or []
        self.contacts = contacts or []
        self.converted_leads = converted_leads or []
        self.leads_by_id = leads_by_id or []
        self.accounts = accounts or []
        self.queries = []

    def query(self, soql, bulk=False):
        self.queries.append(soql)
        if "FROM Task" in soql:
            return [dict(t) for t in self.tasks]
        if "FROM Contact" in soql:
            return [dict(c) for c in self.contacts]
        if "FROM Lead" in soql and "ConvertedContactId IN" in soql:
            return [dict(l) for l in self.converted_leads]
        if "FROM Lead" in soql:
            return [dict(l) for l in self.leads_by_id]
        if "FROM Account" in soql:
            return [dict(a) for a in self.accounts]
        return []


def _task(tid="T1", who="0031", desc=CLAIMER_DESC):
    return {"Id": tid, "WhoId": who, "Subject": CLAIMER_SUBJECT,
            "Description": desc, "CreatedDate": "2026-06-19T10:00:00.000+0000"}


def _contact(cid="0031", account="001EX", email="chef@acme.de"):
    return {"Id": cid, "AccountId": account, "Email": email,
            "FirstName": "Anna", "LastName": "Muster", "KN_KununuUserUUID__c": None}


def _conv_lead(lid="00Q1", contact="0031", account="001EX", date="2026-06-20"):
    return {"Id": lid, "Company": "Acme GmbH", "Email": "chef@acme.de",
            "IsConverted": "true", "ConvertedDate": date,
            "ConvertedAccountId": account, "ConvertedContactId": contact}


def _result(decision="link", rule="name_address", account_id="001EX", evidence=None):
    enriched = EnrichedLead(extracted=ExtractedLead(
        lead_id="00Q1", task_id="T1", task_type="claimer"))
    account = AccountMatch(account_id, rule) if account_id else None
    return MatchResult(enriched=enriched, account=account, contact=None,
                       decision=decision, rule=rule, evidence=evidence or [])


# --- classification -----------------------------------------------------------

def test_classify_link_hit_and_miss():
    assert classify_case(_result(), "001EX", False)[0] == "hit_link"
    assert classify_case(_result(account_id="001OTHER"), "001EX", False)[0] \
        == "miss_wrong_account"


def test_classify_review_uses_candidate_and_evidence_ids():
    surfaced = _result(decision="review", rule="duplicate", account_id="001AAAAAAAAAAAA",
                       evidence=["name→001AAAAAAAAAAAA,001BBBBBBBBBBBB"])
    assert classify_case(surfaced, "001BBBBBBBBBBBB", False)[0] == "hit_review"
    assert classify_case(surfaced, "001CCCCCCCCCCCC", False)[0] == "miss_review"


def test_classify_create_new_splits_on_human_created_new():
    created = _result(decision="create_new", rule="no_match", account_id=None)
    assert classify_case(created, "001EX", True)[0] == "create_agree"
    assert classify_case(created, "001EX", False)[0] == "miss_create_new"


def test_classify_skip_is_coverage_gap():
    skip = _result(decision="skip", rule="no_lf_match", account_id=None)
    assert classify_case(skip, "001EX", False)[0] == "coverage_gap"


def test_candidate_ids_parses_evidence():
    r = _result(decision="review", rule="weak_signal", account_id="001AAAAAAAAAAAA",
                evidence=["vat→001BBBBBBBBBBBB", "name→001CCCCCCCCCCCC,001DDDDDDDDDDDD"])
    assert _candidate_account_ids(r) == {
        "001AAAAAAAAAAAA", "001BBBBBBBBBBBB", "001CCCCCCCCCCCC", "001DDDDDDDDDDDD"}


# --- case building -------------------------------------------------------------

def test_build_cases_converted_lead_kind():
    settings = Settings.load()
    client = FakeClient(tasks=[_task()], contacts=[_contact()],
                        converted_leads=[_conv_lead()])
    cases, counts = build_cases(client, settings, SINCE)
    assert len(cases) == 1
    c = cases[0]
    assert c.kind == "converted_lead"
    assert c.truth_account_id == "001EX"
    assert c.truth_contact_id == "0031"
    assert c.extracted.lead_company == "Acme GmbH"      # from the converted Lead
    assert c.extracted.company_name == "Acme GmbH"      # from the Description
    assert counts["contacts_with_converted_lead"] == 1


def test_build_cases_contact_task_kind_uses_description_only():
    settings = Settings.load()
    client = FakeClient(tasks=[_task()], contacts=[_contact()])
    cases, counts = build_cases(client, settings, SINCE)
    assert len(cases) == 1
    c = cases[0]
    assert c.kind == "contact_task"
    assert c.truth_account_id == "001EX"
    assert c.lead is None
    assert c.extracted.lead_company is None             # no Lead ever existed
    assert c.extracted.company_name == "Acme GmbH"
    assert c.extracted.lead_id == "0031"                # row identifiable by WhoId


def test_build_cases_newest_converted_lead_wins():
    settings = Settings.load()
    client = FakeClient(tasks=[_task()], contacts=[_contact()],
                        converted_leads=[_conv_lead("00Q1", date="2026-05-01"),
                                         _conv_lead("00Q2", date="2026-06-20")])
    cases, _ = build_cases(client, settings, SINCE)
    assert cases[0].lead["Id"] == "00Q2"


def test_build_cases_excludes_internal_email():
    settings = Settings.load()  # config excludes kununu/xing/onlyfy
    desc = CLAIMER_DESC.replace("chef@acme.de", "tester@xing.com")
    client = FakeClient(tasks=[_task(desc=desc)],
                        contacts=[_contact(email="tester@xing.com")])
    cases, counts = build_cases(client, settings, SINCE)
    assert cases == []
    assert counts["internal_email_excluded"] == 1


def test_build_cases_skips_contact_without_account():
    settings = Settings.load()
    client = FakeClient(tasks=[_task()], contacts=[_contact(account=None)])
    cases, counts = build_cases(client, settings, SINCE)
    assert cases == []
    assert counts["no_truth_account"] == 1


# --- end-to-end smoke (no LeadFeeder -> everything coverage_gap) ---------------

def test_run_backtest_without_lf_writes_report(tmp_path):
    settings = Settings.load()
    client = FakeClient(
        tasks=[_task()], contacts=[_contact()], converted_leads=[_conv_lead()],
        accounts=[{"Id": "001EX", "Name": "Acme GmbH",
                   "CreatedDate": "2026-06-20T10:00:00.000+0000"}])
    xlsx = tmp_path / "bt.xlsx"
    res = run_backtest(client, None, settings, since=SINCE, xlsx_path=xlsx, now=NOW)
    assert res["tally"] == {"coverage_gap": 1}
    assert xlsx.exists()
    row = res["results"][0]
    assert row["truth_account_name"] == "Acme GmbH"
    assert row["human_created_new"] is True   # account created on conversion day
