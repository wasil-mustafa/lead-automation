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
        self.creates: list[tuple] = []

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

    def query_many(self, queries):
        return {label: self.query(soql) for label, soql in queries.items()}

    def update_records(self, sobject, records):
        self.updates.append((sobject, records))
        return {"processed": len(records)}

    def create_records(self, sobject, records):
        self.creates.append((sobject, records))
        return {"processed": len(records), "created": len(records), "duplicates": 0,
                "failed": 0, "failures": []}


def _task(tid="T1", who="00Q1", created="2026-06-19T10:00:00.000+0000"):
    return {"Id": tid, "WhoId": who, "Subject": CLAIMER_SUBJECT,
            "Description": CLAIMER_DESC, "CreatedDate": created}


def _lead(lid="00Q1"):
    return {"Id": lid, "FirstName": "Ramona", "LastName": "Panknin",
            "Company": "Hufeland Klinikum GmbH", "Email": "r@hufeland.de", "Website": None,
            "Status": "New", "IsConverted": False, "LeadSource": "kununu",
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

def test_stage1_chunks_the_match_phase(monkeypatch):
    # Backlog-sized runs (500+ leads) must not load candidates in one query — the
    # signals SOQL breaches the 100k-char limit. Expect one load per 40-lead chunk.
    from leadauto.match import ExistingRecords
    from leadauto.pipeline import stage1_enrich as s1

    tasks = [_task(tid=f"T{i}", who=f"00Q{i:04d}") for i in range(90)]
    leads = [dict(_lead(lid=f"00Q{i:04d}"), Email=f"r{i}@hufeland.de") for i in range(90)]
    sf = FakeSfClient(tasks=tasks, leads=leads)
    calls = []
    monkeypatch.setattr(s1, "load_existing_records",
                        lambda _sf, batch, *a, **k: calls.append(len(batch)) or ExistingRecords())
    res = s1.run_stage1(sf, None, SETTINGS, state=State(None, []),
                        since="2026-05-01T00:00:00Z", dry_run=True,
                        qa_path=None, state_path=None, predictions_path=None)
    assert calls == [40, 40, 10]
    assert len(res.results) == 90


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
    assert r.decision == "skip"  # terminal skip, not review — nothing to adjudicate
    assert r.account is None
    # No accepted LF match excludes the lead from BOTH account and contact matching — the
    # kununu user UUID identifier signal is NOT resolved to the existing Contact.
    assert r.contact is None
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


def test_stage1_live_writes_back_and_persists_state(tmp_path, monkeypatch):
    # Live-scope pass required since the phased go-live: create_new + kununu score < 3.
    _matched_enrich(monkeypatch)
    sf = FakeSfClient(tasks=[_task()], leads=[_lead()])
    state_path = tmp_path / "watermark.json"
    res = run_stage1(sf, object(), SETTINGS, state=State(None, []), since="2026-05-01T00:00:00Z",
                     dry_run=False, qa_path=tmp_path / "qa.xlsx", state_path=state_path,
                     review_dir=tmp_path / "review", predictions_path=None)
    assert res.results[0].decision == "create_new"
    assert len(sf.updates) == 1
    sobject, records = sf.updates[0]
    assert sobject == "Lead"
    assert records[0]["Id"] == "00Q1"
    assert records[0]["KN_KununuLinkProfile__c"] == "https://www.kununu.com/de/hufeland-klinikum"
    assert state_path.exists()
    assert load_state(state_path).last_task_created == "2026-06-19T10:00:00.000+0000"


def test_stage1_live_leaves_out_of_scope_leads_untouched(tmp_path):
    # Phased go-live: a live run must NOT write back leads outside the live scope.
    # lf_client=None -> no LF match -> terminal skip -> decision_skip exclusion; the
    # lead's KN_* fields stay untouched, but the watermark still advances (the task
    # is seen once, then left to humans).
    sf = FakeSfClient(tasks=[_task()], leads=[_lead()],
                      accounts=[_account()], contacts=[_contact()])
    state_path = tmp_path / "watermark.json"
    res = run_stage1(sf, None, SETTINGS, state=State(None, []), since="2026-05-01T00:00:00Z",
                     dry_run=False, qa_path=None, state_path=state_path,
                     review_dir=tmp_path / "review", predictions_path=None)
    assert res.results[0].decision == "skip"
    assert sf.updates == []                  # untouched in Salesforce
    assert state_path.exists()               # but the task is consumed


def test_stage1_live_records_shadow_predictions_for_out_of_scope(tmp_path):
    # Out-of-scope leads on a LIVE run keep feeding the shadow backtest: humans will
    # convert them, so the prediction is valid evidence for widening the rollout.
    from leadauto.predictions import load_predictions
    sf = FakeSfClient(tasks=[_task()], leads=[_lead()],
                      accounts=[_account()], contacts=[_contact()])
    p = tmp_path / "predictions.jsonl"
    run_stage1(sf, None, SETTINGS, state=State(None, []), since="2026-05-01T00:00:00Z",
               dry_run=False, qa_path=None, state_path=tmp_path / "wm.json",
               review_dir=tmp_path / "review", predictions_path=p)
    preds = load_predictions(p)
    assert [q.lead_id for q in preds] == ["00Q1"]
    assert preds[0].decision == "skip"


def test_stage1_live_never_records_predictions_for_live_scope_rows(tmp_path, monkeypatch):
    # Live-scope rows are converted by the pipeline itself — shadow-compare must never
    # score our own conversions as human decisions.
    _matched_enrich(monkeypatch)
    sf = FakeSfClient(tasks=[_task(who="00Q000000000001")],
                      leads=[_lead("00Q000000000001")])
    p = tmp_path / "predictions.jsonl"
    run_stage1(sf, object(), _no_gate_settings(), state=State(None, []),
               since="2026-05-01T00:00:00Z", dry_run=False,
               qa_path=None, state_path=tmp_path / "wm.json",
               review_dir=tmp_path / "review", predictions_path=p)
    assert not p.exists()


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


def test_targets_from_approved_drops_approved_skip_rows():
    # A terminal skip (no LF match) never converts, even if a reviewer ticked Approve?.
    rows = [
        {"Approve?": "yes", "lead_id": LEAD1, "account_match_id": "", "contact_match_id": "",
         "decision": "skip"},
        {"Approve?": "yes", "lead_id": LEAD2, "account_match_id": "", "contact_match_id": "",
         "decision": "create_new"},
    ]
    targets = targets_from_approved(rows, SETTINGS)
    assert targets == [ConversionTarget(LEAD2, account_id=None, contact_id=None)]


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


def test_targets_from_approved_carries_pool_and_cluster_keys():
    rows = [{"Approve?": "yes", "lead_id": LEAD1, "account_match_id": ACCT1,
             "contact_reason": "kununu_user_uuid", "contact_match_id": "",
             "lf_company_id": "LF-1", "kununu_profile_uuid": "puid-1",
             "kununu_url": "https://www.kununu.com/de/acme",
             "lf_contact_email": "lf.contact@acme.de"}]
    t = targets_from_approved(rows, SETTINGS)[0]
    assert t.lf_company_id == "LF-1"
    assert t.kununu_profile_uuid == "puid-1"
    assert t.kununu_url == "https://www.kununu.com/de/acme"
    assert t.lf_contact_email == "lf.contact@acme.de"


# --- Stage 2 orchestration ---------------------------------------------------

class FakeConvertClient:
    """Fake SOAP-converting client: query() answers the LeadStatus / already-converted
    SOQL (plus the pool queries, empty here so resolve_pool_links passes through);
    convert_lead() echoes success per conversion."""

    def __init__(self):
        self.convert_calls = []

    def query(self, soql, bulk=False):
        if "FROM LeadStatus" in soql:
            return [{"MasterLabel": "Qualified"}]
        return []

    def query_many(self, queries):
        return {label: self.query(soql) for label, soql in queries.items()}

    def convert_lead(self, conversions, *, converted_status, owner_id):
        self.convert_calls.append(conversions)
        return [{"leadId": c["lead_id"], "success": True, "accountId": "001X"}
                for c in conversions]


def _write_approved(path, rows):
    cols = ["lead_id", "account_match_id", "contact_match_id", "contact_score",
            "decision", "lf_contact_email", "Approve?"]
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
    client = FakeConvertClient()
    res = run_stage2(client, SETTINGS, p, dry_run=True, preflight=False)
    assert client.convert_calls == []
    assert res.result["converted"] == 0
    assert res.result["planned"] == 1


def test_run_stage2_live_converts_approved(tmp_path):
    p = tmp_path / "approved.csv"
    _write_approved(p, [
        {"lead_id": LEAD1, "account_match_id": ACCT1, "contact_score": "1.0", "Approve?": "yes"},
        {"lead_id": LEAD2, "Approve?": "no"},
    ])
    client = FakeConvertClient()
    res = run_stage2(client, SETTINGS, p, dry_run=False, preflight=False)
    assert len(client.convert_calls) == 1
    assert [c["lead_id"] for c in client.convert_calls[0]] == [LEAD1]
    assert res.result["converted"] == 1   # only the approved lead
    assert res.result["missing"] == 0     # the converted lead was reconciled in the results


def test_run_stage2_preflight_preverifies_no_per_batch_isconverted_query(tmp_path):
    # With preflight on, its Lead query already proves IsConverted=false for every
    # surviving target — convert_leads must not re-query IsConverted per batch.
    class _PreflightClient(FakeConvertClient):
        def __init__(self):
            super().__init__()
            self.queries = []

        def query(self, soql, bulk=False):
            self.queries.append(soql)
            if "FROM LeadStatus" in soql:
                return [{"MasterLabel": "Qualified"}]
            if "FROM Lead" in soql and "IsConverted," in soql:  # preflight field list
                return [{"Id": LEAD1, "IsConverted": "false",
                         "KN_Employee__c": "11-50", "KN_DBID__c": "",
                         "KN_NoOwnDBID__c": "true", "KN_Language__c": "German",
                         "Email": "jane@acme.de", "Phone": "", "Street": "Foo 1",
                         "Country": "Germany", "PostalCode": "12345"}]
            return []

    p = tmp_path / "approved.csv"
    _write_approved(p, [{"lead_id": LEAD1, "account_match_id": ACCT1,
                         "contact_score": "1.0", "Approve?": "yes"}])
    client = _PreflightClient()
    res = run_stage2(client, SETTINGS, p, dry_run=False, preflight=True)
    assert res.result["converted"] == 1
    assert res.preflight_failures == []
    assert not any("FROM Lead " in q and "IsConverted = true" in q
                   for q in client.queries)


def test_run_stage2_enriches_email2_on_new_contact(tmp_path):
    """Create-new conversion + LF contact email differing from the task email ->
    one Contact update setting KN_Email2__c."""
    class _Email2ConvertClient(FakeConvertClient):
        def __init__(self):
            super().__init__()
            self.updates = []

        def query(self, soql, bulk=False):
            if "FROM Contact" in soql:
                return [{"Id": "003NEW", "Email": "task@acme.de", "KN_Email2__c": ""}]
            return super().query(soql, bulk=bulk)

        def convert_lead(self, conversions, *, converted_status, owner_id):
            self.convert_calls.append(conversions)
            return [{"leadId": c["lead_id"], "success": True,
                     "accountId": "001NEW", "contactId": "003NEW"}
                    for c in conversions]

        def update_records(self, sobject, records):
            self.updates.append((sobject, records))
            return {"processed": len(records), "failed": 0}

    p = tmp_path / "approved.csv"
    _write_approved(p, [{"lead_id": LEAD1, "decision": "create_new",
                         "lf_contact_email": "lf.contact@acme.de", "Approve?": "yes"}])
    client = _Email2ConvertClient()
    res = run_stage2(client, SETTINGS, p, dry_run=False, preflight=False)
    assert res.result["converted"] == 1
    assert res.result["email2"] == {"processed": 1, "failed": 0}
    assert client.updates == [("Contact", [{"Id": "003NEW",
                                            "KN_Email2__c": "lf.contact@acme.de"}])]


def test_run_stage2_dry_run_email2_is_noop(tmp_path):
    p = tmp_path / "approved.csv"
    _write_approved(p, [{"lead_id": LEAD1, "decision": "create_new",
                         "lf_contact_email": "lf@acme.de", "Approve?": "yes"}])
    client = FakeConvertClient()
    res = run_stage2(client, SETTINGS, p, dry_run=True, preflight=False)
    assert res.result["email2"] == {"processed": 0, "failed": 0}


# --- Stage 2: preflight gate --------------------------------------------------

import csv as _csv

_GOOD_ROW = {
    "IsConverted": False, "KN_Employee__c": "11-50", "KN_DBID__c": "",
    "KN_NoOwnDBID__c": "true", "KN_Language__c": "German", "Street": "Weg 1",
    "Country": "Germany", "CountryCode": "DE", "PostalCode": "20095",
    "Phone": "", "Email": "info@acme.de", "HasOptedOutOfEmail": False, "LeadSource": "Web",
}


class _PreflightSfFake:
    """query() answers the preflight (and conversion-support) SOQL; convert_lead
    echoes success for every lead it sees."""
    def __init__(self, rows):
        self.rows = rows
        self.convert_calls = []

    def query(self, soql, bulk=False):
        if "FROM LeadStatus" in soql:
            return [{"MasterLabel": "Qualified"}]
        if "IsConverted = true" in soql:
            return []
        return self.rows

    def query_many(self, queries):
        return {label: self.query(soql) for label, soql in queries.items()}

    def convert_lead(self, conversions, *, converted_status, owner_id):
        self.convert_calls.append(conversions)
        return [{"leadId": c["lead_id"], "success": True} for c in conversions]


class _PreflightSettings:
    dry_run = False
    owner_id = "005X"
    converted_status = "Qualified"
    rohdaten_pool_owner_id = None  # no pool owner configured -> resolve_pool_links passes through


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
    assert LEAD2 not in [c["lead_id"] for call in client.convert_calls for c in call]
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
    assert res.preflight_failures and not client.convert_calls


def test_stage2_preflight_writes_csv_even_with_zero_targets(tmp_path):
    # All rows unapproved -> zero targets; preflight (default on) must still write the
    # header-only CSV so the CI artifact always shows preflight ran.
    approved = _approved_csv(tmp_path, [{"lead_id": "00Q_NO", "Approve?": "no"}], "approved_w.csv")
    client = _PreflightSfFake([])
    res = run_stage2(client, _PreflightSettings(), approved,
                     failures_csv=tmp_path / "pf.csv")
    assert res.targets == []
    assert res.preflight_failures == []
    assert not client.convert_calls
    assert (tmp_path / "pf.csv").exists()
    assert (tmp_path / "pf.csv").read_text(encoding="utf-8").strip() == "lead_id,check,value,detail"


# --- Stage 2: pool-account enrichment + conflict gate ------------------------

def test_run_stage2_routes_kununu_conflict_to_failures(tmp_path, monkeypatch):
    from leadauto.pipeline import stage2_convert

    def fake_resolve(client, link_targets, settings, *, dry_run=False, logger=None):
        # simulate: the single link is a kununu conflict -> dropped + reported
        return [], [{"lead_id": LEAD1, "check": "kununu_uuid_conflict",
                     "value": ACCT1, "detail": "conflict"}]

    monkeypatch.setattr(stage2_convert, "resolve_pool_links", fake_resolve)
    p = tmp_path / "approved.csv"
    _write_approved(p, [{"lead_id": LEAD1, "account_match_id": ACCT1,
                         "contact_score": "1.0", "Approve?": "yes"}])
    client = FakeConvertClient()
    res = run_stage2(client, SETTINGS, p, dry_run=False, preflight=False,
                     failures_csv=tmp_path / "preflight_failures.csv")
    assert client.convert_calls == []              # conflicted lead never converted
    assert any(f["check"] == "kununu_uuid_conflict" for f in res.preflight_failures)


# --- Stage 1: auto-approved CSV (review_gate: false, 2026-07-10) ---------------

def _gate_settings(review_gate: bool):
    """Settings with review_gate AND the conversion scope pinned — never depend on the
    config.yaml values (review_gate flipped to false at go-live 2026-07-18; the
    scheduled config has converted nothing since 2026-10-19)."""
    s = Settings.load()
    s.raw["pipeline"] = dict(s.raw["pipeline"], review_gate=review_gate,
                             live_scope={"decisions": ["create_new"],
                                         "max_kununu_score": 3.0})
    return s


def _no_gate_settings():
    return _gate_settings(review_gate=False)


def _matched_enrich(monkeypatch):
    """Make enrich_leads return an accepted LF match so the decision is create_new,
    with a kununu score under the live-scope cap so the row passes the go-live gate."""
    from leadauto.models import EnrichedLead

    def fake_enrich(lf, extracteds, settings, logger=None, **kw):
        return [EnrichedLead(extracted=e, matched=True, lf_company_id="123",
                             matched_name=e.best_company, company={}, kununu_score=2.5)
                for e in extracteds]

    monkeypatch.setattr("leadauto.pipeline.stage1_enrich.enrich_leads", fake_enrich)


def test_stage1_live_no_gate_emits_auto_approved_csv(tmp_path, monkeypatch):
    _matched_enrich(monkeypatch)
    sf = FakeSfClient(tasks=[_task(who="00Q000000000001")],
                      leads=[_lead("00Q000000000001")])
    review_dir = tmp_path / "review"
    res = run_stage1(sf, object(), _no_gate_settings(), state=State(None, []),
                     since="2026-05-01T00:00:00Z", dry_run=False,
                     qa_path=tmp_path / "qa.xlsx", state_path=tmp_path / "wm.json",
                     review_dir=review_dir, predictions_path=None)
    assert res.results[0].decision == "create_new"
    files = list(review_dir.glob("approved_*auto*.csv"))
    assert len(files) == 1
    rows = read_approved(files[0])
    assert rows[0]["lead_id"] == "00Q000000000001" and rows[0]["Approve?"] == "yes"


def test_stage1_dry_run_never_emits_auto_csv(tmp_path, monkeypatch):
    _matched_enrich(monkeypatch)
    sf = FakeSfClient(tasks=[_task(who="00Q000000000001")],
                      leads=[_lead("00Q000000000001")])
    review_dir = tmp_path / "review"
    run_stage1(sf, object(), _no_gate_settings(), state=State(None, []),
               since="2026-05-01T00:00:00Z", dry_run=True,
               qa_path=tmp_path / "qa.xlsx", state_path=tmp_path / "wm.json",
               review_dir=review_dir, predictions_path=None)
    assert not review_dir.exists() or not list(review_dir.iterdir())


def test_stage1_review_gate_on_never_emits_auto_csv(tmp_path, monkeypatch):
    _matched_enrich(monkeypatch)
    sf = FakeSfClient(tasks=[_task(who="00Q000000000001")],
                      leads=[_lead("00Q000000000001")])
    review_dir = tmp_path / "review"
    run_stage1(sf, object(), _gate_settings(review_gate=True), state=State(None, []),
               since="2026-05-01T00:00:00Z", dry_run=False,
               qa_path=tmp_path / "qa.xlsx", state_path=tmp_path / "wm.json",
               review_dir=review_dir, predictions_path=None)
    assert not review_dir.exists() or not list(review_dir.iterdir())


def test_stage1_auto_csv_excludes_writeback_failures(tmp_path, monkeypatch):
    _matched_enrich(monkeypatch)

    class FailingSf(FakeSfClient):
        def update_records(self, sobject, records):
            super().update_records(sobject, records)
            return {"processed": 0, "failed": len(records),
                    "failures": [{"id": r["Id"], "error": "boom"} for r in records]}

    sf = FailingSf(tasks=[_task(who="00Q000000000001")],
                   leads=[_lead("00Q000000000001")])
    review_dir = tmp_path / "review"
    run_stage1(sf, object(), _no_gate_settings(), state=State(None, []),
               since="2026-05-01T00:00:00Z", dry_run=False,
               qa_path=tmp_path / "qa.xlsx", state_path=tmp_path / "wm.json",
               review_dir=review_dir, predictions_path=None)
    assert not review_dir.exists() or not list(review_dir.iterdir())


# --- Stage 1: enrich-only go-live for claimer (2026-10-19) ----------------------
# The scheduled config writes back every LF-matched claimer lead, converts nothing and
# keeps ebp_trial dry.
from leadauto.predictions import load_predictions  # noqa: E402

def _live_run(tmp_path, monkeypatch, task_type="claimer"):
    import dataclasses
    from leadauto.models import EnrichedLead

    def fake_enrich(lf, extracteds, settings, logger=None, **kw):
        return [EnrichedLead(extracted=dataclasses.replace(e, task_type=task_type),
                             matched=True, lf_company_id="123",
                             matched_name=e.best_company, company={}, kununu_score=4.5)
                for e in extracteds]

    monkeypatch.setattr("leadauto.pipeline.stage1_enrich.enrich_leads", fake_enrich)
    sf = FakeSfClient(tasks=[_task(who="00Q000000000001")],
                      leads=[_lead("00Q000000000001")])
    review_dir, preds = tmp_path / "review", tmp_path / "predictions.jsonl"
    res = run_stage1(sf, object(), Settings.load(), state=State(None, []),
                     since="2026-05-01T00:00:00Z", dry_run=False,
                     qa_path=tmp_path / "qa.xlsx", state_path=tmp_path / "wm.json",
                     review_dir=review_dir, predictions_path=preds)
    return sf, res, review_dir, preds


def test_enrich_only_claimer_is_written_back_but_not_staged(tmp_path, monkeypatch):
    sf, res, review_dir, preds = _live_run(tmp_path, monkeypatch)
    assert res.results[0].decision == "create_new"
    # score 4.5: no score axis any more, so a high-score claimer is enriched too
    assert [r["Id"] for _, recs in sf.updates for r in recs] == ["00Q000000000001"]
    assert not review_dir.exists() or not list(review_dir.glob("approved_*"))
    # A human converts it, so it stays shadow evidence: decision only, no field checks.
    pred = load_predictions(preds)
    assert [q.lead_id for q in pred] == ["00Q000000000001"]
    assert pred[0].written_back is True


def test_enrich_only_leaves_ebp_trial_dry(tmp_path, monkeypatch):
    sf, res, review_dir, preds = _live_run(tmp_path, monkeypatch, task_type="ebp_trial")
    assert sf.updates == []
    assert not review_dir.exists() or not list(review_dir.glob("approved_*"))
    pred = load_predictions(preds)
    assert [q.lead_id for q in pred] == ["00Q000000000001"]
    assert pred[0].written_back is False


# --- LF-outcome campaign tagging (campaigns.py, Wasil 2026-10-19) -------------

CAMPAIGNS = SETTINGS.lf_campaigns


def _enrich_with(monkeypatch, *, matched=True, company=None, task_type=None):
    import dataclasses

    from leadauto.models import EnrichedLead

    def fake_enrich(lf, extracteds, settings, logger=None, **kw):
        if task_type:
            extracteds = [dataclasses.replace(e, task_type=task_type) for e in extracteds]
        return [EnrichedLead(extracted=e, matched=matched,
                             lf_company_id="123" if matched else None,
                             matched_name=e.best_company if matched else None,
                             company=dict(company or {}) if matched else None,
                             kununu_score=2.5)
                for e in extracteds]

    monkeypatch.setattr("leadauto.pipeline.stage1_enrich.enrich_leads", fake_enrich)


def _live(sf, tmp_path, lf=object(), settings=None, dry_run=False):
    return run_stage1(sf, lf, settings or SETTINGS, state=State(None, []),
                      since="2026-05-01T00:00:00Z", dry_run=dry_run,
                      qa_path=tmp_path / "qa.xlsx", state_path=tmp_path / "wm.json",
                      review_dir=tmp_path / "review", predictions_path=None)


def test_config_carries_the_three_lf_campaigns():
    assert CAMPAIGNS == {"matched_complete": "701Mz00001aPCDdIAO",
                         "matched_incomplete": "701Mz00001aP9hDIAS",
                         "unmatched": "701Mz00001aKIjZIAW"}


@pytest.mark.parametrize("matched,company,bucket", [
    (True, {"Street": "Hauptstr. 1", "Employee_Count": 120}, "matched_complete"),
    (True, {"Street": "Hauptstr. 1"}, "matched_incomplete"),       # no headcount
    (True, {"Employee_Count": 120}, "matched_incomplete"),         # no street
    (False, None, "unmatched"),
])
def test_stage1_live_tags_each_lead_with_its_lf_campaign(tmp_path, monkeypatch,
                                                         matched, company, bucket):
    _enrich_with(monkeypatch, matched=matched, company=company)
    sf = FakeSfClient(tasks=[_task()], leads=[_lead()])
    res = _live(sf, tmp_path)
    assert sf.creates == [("CampaignMember",
                           [{"CampaignId": CAMPAIGNS[bucket], "LeadId": "00Q1"}])]
    assert res.campaigns[f"campaign_{bucket}"] == 1
    assert res.campaigns["campaign_created"] == 1
    review = load_workbook(tmp_path / "qa.xlsx")["Review"]
    header = [c.value for c in review[1]]
    assert review[2][header.index("lf_campaign")].value == bucket


def test_stage1_dry_run_tags_no_campaign(tmp_path, monkeypatch):
    _enrich_with(monkeypatch)
    sf = FakeSfClient(tasks=[_task()], leads=[_lead()])
    res = _live(sf, tmp_path, dry_run=True)
    assert sf.creates == [] and res.campaigns == {}


def test_stage1_live_without_leadfeeder_tags_no_campaign(tmp_path):
    # LF off => every lead looks unmatched; tagging them all 'unmatched' would be a lie.
    sf = FakeSfClient(tasks=[_task()], leads=[_lead()])
    res = _live(sf, tmp_path, lf=None)
    assert sf.creates == [] and res.campaigns == {}


def test_stage1_campaign_failure_is_reported_but_does_not_block_conversion(tmp_path,
                                                                           monkeypatch):
    _enrich_with(monkeypatch)

    class FailingCampaignSf(FakeSfClient):
        def create_records(self, sobject, records):
            self.creates.append((sobject, records))
            return {"processed": len(records), "created": 0, "duplicates": 0,
                    "failed": len(records),
                    "failures": [{"record": r, "error": "INSUFFICIENT_ACCESS: no"}
                                 for r in records]}

    sf = FailingCampaignSf(tasks=[_task(who="00Q000000000001")],
                           leads=[_lead("00Q000000000001")])
    res = _live(sf, tmp_path, settings=_no_gate_settings())
    assert res.failures == [{"lead_id": "00Q000000000001", "stage": "campaign",
                             "error": "INSUFFICIENT_ACCESS: no"}]
    assert res.campaigns["campaign_failed"] == 1
    assert len(list((tmp_path / "review").glob("approved_*auto*.csv"))) == 1


def test_stage1_live_writeback_failure_is_left_untagged(tmp_path, monkeypatch):
    # Tagging excludes a lead from every later run; a failed write-back must stay
    # re-processable, so it gets no campaign.
    _enrich_with(monkeypatch, company={"Street": "Hauptstr. 1", "Employee_Count": 120})

    class FailingSf(FakeSfClient):
        def update_records(self, sobject, records):
            return {"processed": 0, "failed": len(records),
                    "failures": [{"id": r["Id"], "error": "boom"} for r in records]}

    sf = FailingSf(tasks=[_task()], leads=[_lead()])
    _live(sf, tmp_path)
    assert sf.creates in ([], [("CampaignMember", [])])


def test_stage1_duplicate_detected_writeback_failure_goes_to_unmatched_campaign(
        tmp_path, monkeypatch):
    # Wasil, 2026-10-20: a duplicate rule blocks the write-back on every rerun, so the
    # lead is handed to "EnrichLeadmanually" instead of staying re-processable. It is
    # still a write-back failure: held out of the auto-approved conversion CSV.
    _enrich_with(monkeypatch, company={"Street": "Hauptstr. 1", "Employee_Count": 120})

    class DuplicateSf(FakeSfClient):
        def update_records(self, sobject, records):
            return {"processed": 0, "failed": len(records),
                    "failures": [{"id": r["Id"],
                                  "error": "DUPLICATES_DETECTED: Use one of these records?"}
                                 for r in records]}

    sf = DuplicateSf(tasks=[_task(who="00Q000000000001")],
                     leads=[_lead("00Q000000000001")])
    res = _live(sf, tmp_path, settings=_no_gate_settings())
    assert sf.creates == [("CampaignMember", [{"CampaignId": CAMPAIGNS["unmatched"],
                                               "LeadId": "00Q000000000001"}])]
    assert res.campaigns["campaign_duplicate_detected"] == 1
    summary = {r[0].value: r[1].value
               for r in load_workbook(tmp_path / "qa.xlsx")["Summary"].iter_rows(min_row=2)}
    assert summary["campaign_duplicate_detected"] == 1
    assert list((tmp_path / "review").glob("approved_*auto*.csv")) == []


def test_stage1_matched_lead_outside_writeback_scope_is_not_tagged(tmp_path, monkeypatch):
    # Matched but not written back: no campaign, so a later, wider scope can still
    # write it (tagging is a permanent exclusion).
    _enrich_with(monkeypatch, company={"Street": "Hauptstr. 1", "Employee_Count": 120})
    import leadauto.pipeline.stage1_enrich as s1
    monkeypatch.setattr(s1, "writeback_reason", lambda r, s: "decision_review")
    sf = FakeSfClient(tasks=[_task()], leads=[_lead()])
    res = _live(sf, tmp_path)
    assert sf.updates == []
    assert sf.creates in ([], [("CampaignMember", [])])
    assert not any(k.startswith("campaign_matched") for k in res.campaigns)


@pytest.mark.parametrize("matched", [True, False])
def test_stage1_non_claimer_leads_are_never_tagged(tmp_path, monkeypatch, matched):
    _enrich_with(monkeypatch, matched=matched, task_type="ebp_trial")
    sf = FakeSfClient(tasks=[_task()], leads=[_lead()])
    _live(sf, tmp_path)
    assert sf.creates in ([], [("CampaignMember", [])])


def test_stage1_campaign_request_error_never_aborts_the_run(tmp_path, monkeypatch):
    from leadauto.sf.client import SfError
    _enrich_with(monkeypatch)

    class ErroringSf(FakeSfClient):
        def create_records(self, sobject, records):
            raise SfError("Failed to sObject Collections POST (HTTP 500)")

    sf = ErroringSf(tasks=[_task(who="00Q000000000001")],
                    leads=[_lead("00Q000000000001")])
    res = _live(sf, tmp_path, settings=_no_gate_settings())
    assert res.campaigns["campaign_failed"] == 1
    assert [f["stage"] for f in res.failures] == ["campaign"]
    assert (tmp_path / "qa.xlsx").exists()                         # QA still written
    assert len(list((tmp_path / "review").glob("approved_*auto*.csv"))) == 1
