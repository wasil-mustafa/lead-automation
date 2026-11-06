"""Pipeline orchestration tests (one fused run: extract -> enrich -> match -> write back
-> campaign tag -> convert, incl. the sweep of earlier runs' campaign members).

The run is driven through a fake at the org boundary (a routing SfClient whose
convert_lead echoes success), with LeadFeeder disabled (lf_client=None) or faked so no
credits are spent. The tests assert the dry-run rails (no writes / no conversions), the
watermark advance, the conversion gate, the sweep and the QA artifact.
"""
import json

import pytest
from openpyxl import load_workbook

from leadauto.config import Settings
from leadauto.pipeline.run import run_pipeline
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
    """Routing fake. `members` = CampaignMember rows the conversion sweep reads (REST
    shape: the Lead fields nested under "Lead"). Lead updates are APPLIED to the fake's
    leads, so the conversion preflight re-read sees the write-back (and the sweep's
    KN_NoOwnDBID__c tick) exactly as the org would."""

    def __init__(self, tasks=None, leads=None, accounts=None, contacts=None, members=None):
        self._tasks = tasks or []
        self._leads = leads or []
        self._accounts = accounts or []
        self._contacts = contacts or []
        self._members = members or []
        self.updates: list[tuple] = []
        self.creates: list[tuple] = []
        self.convert_calls: list[list] = []
        self.queries: list[str] = []

    def query(self, soql, bulk=False):
        self.queries.append(soql)
        if "FROM Task" in soql:
            return [dict(t) for t in self._tasks]
        if soql.lstrip().startswith("SELECT LeadId,"):   # the sweep read, not the
            return [dict(m) for m in self._members]    # reader anti-join
        if "FROM Lead" in soql:
            return [dict(l) for l in self._leads]
        if "FROM Account" in soql:
            return [dict(a) for a in self._accounts]
        if "FROM Contact" in soql:
            return [dict(c) for c in self._contacts]
        return []

    def query_many(self, queries):
        return {label: self.query(soql) for label, soql in queries.items()}

    def update_records(self, sobject, records, allow_duplicates=False):
        self.updates.append((sobject, records))
        self.allow_duplicates = allow_duplicates
        if sobject == "Lead":
            by_id = {l["Id"][:15]: l for l in self._leads}
            for rec in records:
                lead = by_id.get(rec["Id"][:15])
                if lead is not None:
                    lead.update({k: v for k, v in rec.items() if k != "Id"})
        return {"processed": len(records), "failed": 0, "failures": []}

    def create_records(self, sobject, records):
        self.creates.append((sobject, records))
        return {"processed": len(records), "created": len(records), "duplicates": 0,
                "failed": 0, "failures": []}

    def convert_lead(self, conversions, *, converted_status, owner_id):
        self.convert_calls.append(conversions)
        return [{"leadId": c["lead_id"], "success": True,
                 "accountId": "001NEW" + c["lead_id"][-3:],
                 "contactId": "003NEW" + c["lead_id"][-3:]}
                for c in conversions]


def _task(tid="T1", who="00Q1", created="2026-06-19T10:00:00.000+0000"):
    return {"Id": tid, "WhoId": who, "Subject": CLAIMER_SUBJECT,
            "Description": CLAIMER_DESC, "CreatedDate": created}


def _lead(lid="00Q1"):
    # KN_Language__c is org-auto-populated (convert/CLAUDE.md); the preflight needs it.
    return {"Id": lid, "FirstName": "Ramona", "LastName": "Panknin",
            "Company": "Hufeland Klinikum GmbH", "Email": "r@hufeland.de", "Website": None,
            "Status": "New", "IsConverted": False, "LeadSource": "kununu",
            "KN_Language__c": "German",
            "KN_KununuProfileName__c": None, "KN_KununuUserUUID__c": None,
            "KN_KununuLinkProfile__c": None}


def _member(lid, company="Alt GmbH", dbid=None, no_own=False,
            campaign="701Mz00001aPCDdIAO"):
    """A CampaignMember row as REST returns it (relationship fields nested)."""
    return {"CampaignId": campaign, "LeadId": lid,
            "Lead": {"attributes": {"type": "Lead"}, "Company": company,
                     "KN_DBID__c": dbid, "KN_NoOwnDBID__c": no_own}}


def _write_prediction(path, lid, decision="create_new", predicted_at="2026-10-30T07:53:54Z",
                      status="pending", lf_company_id=None):
    row = {"lead_id": lid, "task_id": "T_" + lid, "predicted_at": predicted_at,
           "rule": "no_match" if decision == "create_new" else "name_only",
           "decision": decision, "account_id": None, "contact_id": None, "evidence": [],
           "payload": {"KN_KununuLinkProfile__c": "https://www.kununu.com/de/alt"},
           "lf_company_id": lf_company_id, "lf_company_name": "Alt GmbH",
           "lf_website": None, "lf_match_pass": "primary", "written_back": True,
           "status": status, "resolved_at": None, "outcome": None}
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


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
    from leadauto.pipeline import run as s1

    tasks = [_task(tid=f"T{i}", who=f"00Q{i:04d}") for i in range(90)]
    leads = [dict(_lead(lid=f"00Q{i:04d}"), Email=f"r{i}@hufeland.de") for i in range(90)]
    sf = FakeSfClient(tasks=tasks, leads=leads)
    calls = []
    monkeypatch.setattr(s1, "load_existing_records",
                        lambda _sf, batch, *a, **k: calls.append(len(batch)) or ExistingRecords())
    res = s1.run_pipeline(sf, None, SETTINGS, state=State(None, []),
                        since="2026-05-01T00:00:00Z", dry_run=True,
                        qa_path=None, state_path=None, predictions_path=None)
    assert calls == [40, 40, 10]
    assert len(res.results) == 90


def test_stage1_matches_links_and_advances_watermark(tmp_path):
    sf = FakeSfClient(tasks=[_task()], leads=[_lead()],
                      accounts=[_account()], contacts=[_contact()])
    qa = tmp_path / "qa.xlsx"
    state_path = tmp_path / "watermark.json"
    res = run_pipeline(sf, None, SETTINGS, state=State(None, []), since="2026-05-01T00:00:00Z",
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
    res = run_pipeline(sf, None, SETTINGS, state=State(None, []), since="2026-05-01T00:00:00Z",
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
    res = run_pipeline(sf, object(), SETTINGS, state=State(None, []), since="2026-05-01T00:00:00Z",
                     dry_run=False, qa_path=tmp_path / "qa.xlsx", state_path=state_path,
                     predictions_path=None)
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
    res = run_pipeline(sf, None, SETTINGS, state=State(None, []), since="2026-05-01T00:00:00Z",
                     dry_run=False, qa_path=None, state_path=state_path,
                     predictions_path=None)
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
    run_pipeline(sf, None, SETTINGS, state=State(None, []), since="2026-05-01T00:00:00Z",
               dry_run=False, qa_path=None, state_path=tmp_path / "wm.json",
                     predictions_path=p)
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
    run_pipeline(sf, object(), _no_gate_settings(), state=State(None, []),
               since="2026-05-01T00:00:00Z", dry_run=False,
               qa_path=None, state_path=tmp_path / "wm.json",
                     predictions_path=p)
    assert not p.exists()


# --- Live run without the campaign axis (the 2026-07-18 go-live behaviour) --------

def _scope_settings(**scope):
    """Settings with the conversion scope pinned — never depend on the config.yaml
    values. No `convert_campaigns` key = no campaign axis: the conversion scope alone
    decides what converts (the go-live behaviour, still used by one-off configs)."""
    s = Settings.load()
    s.raw["pipeline"] = dict(s.raw["pipeline"],
                             live_scope={"decisions": ["create_new"],
                                         "max_kununu_score": 3.0, **scope})
    return s


def _no_gate_settings():
    return _scope_settings()


def _matched_enrich(monkeypatch):
    """Make enrich_leads return an accepted LF match so the decision is create_new,
    with a kununu score under the live-scope cap so the row passes the go-live gate."""
    from leadauto.models import EnrichedLead

    def fake_enrich(lf, extracteds, settings, logger=None, **kw):
        return [EnrichedLead(extracted=e, matched=True, lf_company_id="123",
                             matched_name=e.best_company,
                             company={"Street": "Hauptstr. 1", "Employee_Count": 120},
                             kununu_score=2.5)
                for e in extracteds]

    monkeypatch.setattr("leadauto.pipeline.run.enrich_leads", fake_enrich)


def test_live_no_campaign_axis_converts_the_create_new_lead_in_process(tmp_path, monkeypatch):
    _matched_enrich(monkeypatch)
    sf = FakeSfClient(tasks=[_task(who="00Q000000000001")],
                      leads=[_lead("00Q000000000001")])
    res = run_pipeline(sf, object(), _no_gate_settings(), state=State(None, []),
                       since="2026-05-01T00:00:00Z", dry_run=False,
                       qa_path=tmp_path / "qa.xlsx", state_path=tmp_path / "wm.json",
                       predictions_path=None)
    assert res.results[0].decision == "create_new"
    assert sf.updates[0][1][0]["KN_NoOwnDBID__c"] is True      # ticked for conversion
    assert [c["lead_id"] for call in sf.convert_calls for c in call] == ["00Q000000000001"]
    assert res.conversion["result"]["converted"] == 1
    assert res.converted_ids == {"00Q000000000001"}
    rows = {r[0].value: r for r in load_workbook(tmp_path / "qa.xlsx")["Conversion"].iter_rows(min_row=2)}
    assert rows["00Q000000000001"][2].value == "run"
    assert rows["00Q000000000001"][5].value == "converted"
    assert rows["00Q000000000001"][7].value == "001NEW001"
    assert _summary(tmp_path)["converted"] == 1
    assert _summary(tmp_path)["convert_targets_run"] == 1


def test_dry_run_never_converts(tmp_path, monkeypatch):
    _matched_enrich(monkeypatch)
    sf = FakeSfClient(tasks=[_task(who="00Q000000000001")],
                      leads=[_lead("00Q000000000001")])
    res = run_pipeline(sf, object(), _no_gate_settings(), state=State(None, []),
                       since="2026-05-01T00:00:00Z", dry_run=True,
                       qa_path=tmp_path / "qa.xlsx", state_path=tmp_path / "wm.json",
                       predictions_path=None)
    assert sf.updates == [] and sf.convert_calls == []
    assert res.targets == []
    # a sweep still previews (read-only), but with no campaign axis there is none
    assert not any(q.lstrip().startswith("SELECT LeadId,") for q in sf.queries)


def test_live_excludes_writeback_failures_from_conversion(tmp_path, monkeypatch):
    _matched_enrich(monkeypatch)

    class FailingSf(FakeSfClient):
        def update_records(self, sobject, records, allow_duplicates=False):
            super().update_records(sobject, records, allow_duplicates)
            return {"processed": 0, "failed": len(records),
                    "failures": [{"id": r["Id"], "error": "boom"} for r in records]}

    sf = FailingSf(tasks=[_task(who="00Q000000000001")],
                   leads=[_lead("00Q000000000001")])
    res = run_pipeline(sf, object(), _no_gate_settings(), state=State(None, []),
                       since="2026-05-01T00:00:00Z", dry_run=False,
                       qa_path=tmp_path / "qa.xlsx", state_path=tmp_path / "wm.json",
                       predictions_path=None)
    assert res.targets == [] and sf.convert_calls == []


def test_convert_false_stops_after_tagging(tmp_path, monkeypatch):
    # scripts/event_run.py: enrichment only, conversion stays a deliberate step
    _matched_enrich(monkeypatch)
    sf = FakeSfClient(tasks=[_task(who="00Q000000000001")],
                      leads=[_lead("00Q000000000001")])
    res = run_pipeline(sf, object(), _no_gate_settings(), state=State(None, []),
                       since="2026-05-01T00:00:00Z", dry_run=False, convert=False,
                       qa_path=tmp_path / "qa.xlsx", state_path=tmp_path / "wm.json",
                       predictions_path=None)
    assert sf.updates and sf.convert_calls == []
    assert res.conversion == {} and res.conversion_rows == []
    assert "converted" not in _summary(tmp_path)


# --- Live run, scheduled config, claimer + ebp_trial (2026-10-19 / -20 / -31) ------
# The scheduled config writes back every LF-matched claimer/ebp_trial lead. Since
# 2026-10-31 a create_new lead converts only when this run tagged it matched_complete
# ("EnrichLeadfedup"); these two runs use company={} -> matched_incomplete -> human.
from leadauto.predictions import load_predictions  # noqa: E402

def _live_run(tmp_path, monkeypatch, task_type="claimer"):
    import dataclasses
    from leadauto.models import EnrichedLead

    def fake_enrich(lf, extracteds, settings, logger=None, **kw):
        return [EnrichedLead(extracted=dataclasses.replace(e, task_type=task_type),
                             matched=True, lf_company_id="123",
                             matched_name=e.best_company, company={}, kununu_score=4.5)
                for e in extracteds]

    monkeypatch.setattr("leadauto.pipeline.run.enrich_leads", fake_enrich)
    sf = FakeSfClient(tasks=[_task(who="00Q000000000001")],
                      leads=[_lead("00Q000000000001")])
    preds = tmp_path / "predictions.jsonl"
    res = run_pipeline(sf, object(), Settings.load(), state=State(None, []),
                     since="2026-05-01T00:00:00Z", dry_run=False,
                     qa_path=tmp_path / "qa.xlsx", state_path=tmp_path / "wm.json",
                     predictions_path=preds)
    return sf, res, preds


def test_enrich_only_claimer_is_written_back_but_not_staged(tmp_path, monkeypatch):
    sf, res, preds = _live_run(tmp_path, monkeypatch)
    assert res.results[0].decision == "create_new"
    # score 4.5: no score axis any more, so a high-score claimer is enriched too
    assert [r["Id"] for _, recs in sf.updates for r in recs] == ["00Q000000000001"]
    # matched_incomplete (no LF street/headcount): no DBID tick, not staged
    assert "KN_NoOwnDBID__c" not in sf.updates[0][1][0]
    assert res.targets == [] and sf.convert_calls == []
    # A human converts it, so it stays shadow evidence: decision only, no field checks.
    pred = load_predictions(preds)
    assert [q.lead_id for q in pred] == ["00Q000000000001"]
    assert pred[0].written_back is True


def test_enrich_only_writes_back_ebp_trial_too(tmp_path, monkeypatch):
    # 2026-10-20 (Wasil): ebp_trial joined the live write-back scope. An unlisted type
    # staying dry is pinned in test_live_scope.test_unlisted_task_type_stays_dry.
    sf, res, preds = _live_run(tmp_path, monkeypatch, task_type="ebp_trial")
    assert [r["Id"] for _, recs in sf.updates for r in recs] == ["00Q000000000001"]
    assert res.targets == [] and sf.convert_calls == []
    pred = load_predictions(preds)
    assert [q.lead_id for q in pred] == ["00Q000000000001"]
    assert pred[0].written_back is True


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

    monkeypatch.setattr("leadauto.pipeline.run.enrich_leads", fake_enrich)


def _live(sf, tmp_path, lf=object(), settings=None, dry_run=False):
    return run_pipeline(sf, lf, settings or SETTINGS, state=State(None, []),
                      since="2026-05-01T00:00:00Z", dry_run=dry_run,
                      qa_path=tmp_path / "qa.xlsx", state_path=tmp_path / "wm.json",
                     predictions_path=None)


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
    assert [t.lead_id for t in res.targets] == ["00Q000000000001"]
    # a target, but LF gave no street/headcount: the in-process preflight holds it
    assert {f["check"] for f in res.conversion["preflight_failures"]} >= {"employee_category"}


def test_stage1_live_writeback_failure_is_left_untagged(tmp_path, monkeypatch):
    # Tagging excludes a lead from every later run; a failed write-back must stay
    # re-processable, so it gets no campaign.
    _enrich_with(monkeypatch, company={"Street": "Hauptstr. 1", "Employee_Count": 120})

    class FailingSf(FakeSfClient):
        def update_records(self, sobject, records, allow_duplicates=False):
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
        def update_records(self, sobject, records, allow_duplicates=False):
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
    assert res.targets == [] and sf.convert_calls == []


def test_stage1_matched_lead_outside_writeback_scope_is_not_tagged(tmp_path, monkeypatch):
    # Matched but not written back: no campaign, so a later, wider scope can still
    # write it (tagging is a permanent exclusion).
    _enrich_with(monkeypatch, company={"Street": "Hauptstr. 1", "Employee_Count": 120})
    import leadauto.pipeline.run as s1
    monkeypatch.setattr(s1, "writeback_reason", lambda r, s: "decision_review")
    sf = FakeSfClient(tasks=[_task()], leads=[_lead()])
    res = _live(sf, tmp_path)
    assert sf.updates == []
    assert sf.creates in ([], [("CampaignMember", [])])
    assert not any(k.startswith("campaign_matched") for k in res.campaigns)


@pytest.mark.parametrize("matched,company,bucket", [
    (True, {"Street": "Hauptstr. 1", "Employee_Count": 120}, "matched_complete"),
    (True, {"Street": "Hauptstr. 1"}, "matched_incomplete"),
    (False, None, "unmatched"),
])
def test_stage1_ebp_trial_leads_get_the_same_campaigns(tmp_path, monkeypatch,
                                                       matched, company, bucket):
    # 2026-10-20 (Wasil): ebp_trial is tagged exactly like claimer.
    _enrich_with(monkeypatch, matched=matched, company=company, task_type="ebp_trial")
    sf = FakeSfClient(tasks=[_task()], leads=[_lead()])
    res = _live(sf, tmp_path)
    assert sf.creates == [("CampaignMember",
                           [{"CampaignId": CAMPAIGNS[bucket], "LeadId": "00Q1"}])]
    assert res.campaigns[f"campaign_{bucket}"] == 1


@pytest.mark.parametrize("matched", [True, False])
def test_stage1_untagged_task_type_is_never_tagged(tmp_path, monkeypatch, matched):
    # 'event' is not in lf_campaign_task_types, so even an unmatched lead gets none.
    _enrich_with(monkeypatch, matched=matched, task_type="event")
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
    assert [t.lead_id for t in res.targets] == ["00Q000000000001"]
    # a target, but LF gave no street/headcount: the in-process preflight holds it
    assert {f["check"] for f in res.conversion["preflight_failures"]} >= {"employee_category"}




# --- Campaign conversion gate (Wasil, 2026-10-31) + in-process conversion (2026-11-01)
# write-back -> campaign -> convert ONLY the leads tagged matched_complete
# ("EnrichLeadfedup") this run, in the same process. Scheduled config (SETTINGS).

COMPLETE = {"Street": "Hauptstr. 1", "Employee_Count": 120}


def _live_preds(sf, tmp_path, **kw):
    preds = tmp_path / "predictions.jsonl"
    res = run_pipeline(sf, kw.pop("lf", object()), kw.pop("settings", SETTINGS),
                       state=State(None, []), since="2026-05-01T00:00:00Z",
                       dry_run=kw.pop("dry_run", False),
                       qa_path=tmp_path / "qa.xlsx", state_path=tmp_path / "wm.json",
                       predictions_path=preds, **kw)
    return res, preds


def _summary(tmp_path):
    return {r[0].value: r[1].value
            for r in load_workbook(tmp_path / "qa.xlsx")["Summary"].iter_rows(min_row=2)}


def _conversion_rows(tmp_path):
    ws = load_workbook(tmp_path / "qa.xlsx")["Conversion"]
    header = [c.value for c in ws[1]]
    return [dict(zip(header, (c.value for c in row))) for row in ws.iter_rows(min_row=2)]


def test_campaign_gate_converts_the_fedup_lead_with_dbid_tick(tmp_path, monkeypatch):
    _enrich_with(monkeypatch, company=COMPLETE)
    sf = FakeSfClient(tasks=[_task(who="00Q000000000001")], leads=[_lead("00Q000000000001")])
    res, preds = _live_preds(sf, tmp_path)
    assert res.results[0].decision == "create_new"
    written = sf.updates[0][1][0]
    assert written["KN_NoOwnDBID__c"] is True            # dbid_xor passes in the preflight
    assert sf.creates == [("CampaignMember", [{"CampaignId": CAMPAIGNS["matched_complete"],
                                               "LeadId": "00Q000000000001"}])]
    # converted in the same run, create-new (no account/contact link)
    assert sf.convert_calls == [[{"lead_id": "00Q000000000001", "account_id": None,
                                  "contact_id": None}]]
    assert res.conversion["result"]["converted"] == 1
    rows = _conversion_rows(tmp_path)
    assert [(r["source"], r["outcome"], r["account_id"]) for r in rows] == [
        ("run", "converted", "001NEW001")]
    summary = _summary(tmp_path)
    assert summary["convert_targets_run"] == 1 and summary["converted"] == 1
    # converted by us -> never shadow evidence
    assert not preds.exists() or load_predictions(preds) == []


def test_campaign_gate_leaves_the_shortage_lead_to_a_human(tmp_path, monkeypatch):
    _enrich_with(monkeypatch, company={"Street": "Hauptstr. 1"})    # no headcount
    sf = FakeSfClient(tasks=[_task(who="00Q000000000001")], leads=[_lead("00Q000000000001")])
    res, preds = _live_preds(sf, tmp_path)
    written = sf.updates[0][1][0]
    assert "KN_NoOwnDBID__c" not in written                # human-converted rule holds
    assert sf.creates[0][1][0]["CampaignId"] == CAMPAIGNS["matched_incomplete"]
    assert res.targets == [] and sf.convert_calls == []
    assert _summary(tmp_path)["convert_targets_run"] == 0
    review = load_workbook(tmp_path / "qa.xlsx")["Review"]
    header = [c.value for c in review[1]]
    assert review[2][header.index("live_scope")].value == "campaign_matched_incomplete"
    pred = load_predictions(preds)
    assert [q.lead_id for q in pred] == ["00Q000000000001"] and pred[0].written_back is True


def test_campaign_gate_holds_back_a_lead_whose_campaign_insert_failed(tmp_path, monkeypatch):
    # "Only convert leads that HAVE the campaign": a failed CampaignMember insert
    # means the lead is not in it, so it stays with a human (and in shadow).
    _enrich_with(monkeypatch, company=COMPLETE)

    class FailingCampaignSf(FakeSfClient):
        def create_records(self, sobject, records):
            self.creates.append((sobject, records))
            return {"processed": len(records), "created": 0, "duplicates": 0,
                    "failed": len(records),
                    "failures": [{"record": r, "error": "INSUFFICIENT_ACCESS: no"}
                                 for r in records]}

    sf = FailingCampaignSf(tasks=[_task(who="00Q000000000001")],
                           leads=[_lead("00Q000000000001")])
    res, preds = _live_preds(sf, tmp_path)
    assert res.campaigns["campaign_failed"] == 1
    assert res.targets == [] and sf.convert_calls == []
    assert [q.lead_id for q in load_predictions(preds)] == ["00Q000000000001"]


def test_campaign_gate_treats_already_member_as_tagged(tmp_path, monkeypatch):
    _enrich_with(monkeypatch, company=COMPLETE)

    class DupSf(FakeSfClient):
        def create_records(self, sobject, records):
            self.creates.append((sobject, records))
            return {"processed": len(records), "created": 0, "duplicates": len(records),
                    "failed": 0, "failures": []}

    sf = DupSf(tasks=[_task(who="00Q000000000001")], leads=[_lead("00Q000000000001")])
    res, _ = _live_preds(sf, tmp_path)
    assert [t.lead_id for t in res.targets] == ["00Q000000000001"]
    assert res.conversion["result"]["converted"] == 1


def test_campaign_gate_converts_nothing_of_its_own_without_leadfeeder(tmp_path, monkeypatch):
    # LF off => no tagging => the gate fails closed even for a create_new lead.
    import leadauto.pipeline.run as s1
    monkeypatch.setattr(s1, "live_scope_reason", lambda r, s: None)
    sf = FakeSfClient(tasks=[_task(who="00Q000000000001")], leads=[_lead("00Q000000000001")])
    res, _ = _live_preds(sf, tmp_path, lf=None)
    assert sf.creates == [] and sf.convert_calls == []
    assert _summary(tmp_path)["convert_targets_run"] == 0


def test_campaign_gate_absent_key_keeps_the_go_live_behaviour(tmp_path, monkeypatch):
    # _no_gate_settings has no convert_campaigns: the conversion scope alone converts,
    # an incomplete write-back included, and the DBID tick still follows.
    _enrich_with(monkeypatch, company={"Street": "Hauptstr. 1"})
    sf = FakeSfClient(tasks=[_task(who="00Q000000000001")], leads=[_lead("00Q000000000001")])
    res, _ = _live_preds(sf, tmp_path, settings=_no_gate_settings())
    assert sf.updates[0][1][0]["KN_NoOwnDBID__c"] is True
    assert [t.lead_id for t in res.targets] == ["00Q000000000001"]
    # no campaign axis => no sweep read either
    assert not any(q.lstrip().startswith("SELECT LeadId,") for q in sf.queries)


def test_preflight_skipped_lead_stays_shadow_evidence_and_is_reported(tmp_path, monkeypatch):
    # A lead the gate admits but the preflight rejects is left to a human: recorded as
    # a prediction (the next sweep finds its decision there) and shown on the sheet.
    _enrich_with(monkeypatch, company=COMPLETE)
    lead = dict(_lead("00Q000000000001"), KN_Language__c=None)
    sf = FakeSfClient(tasks=[_task(who="00Q000000000001")], leads=[lead])
    res, preds = _live_preds(sf, tmp_path)
    assert sf.convert_calls == []
    assert res.conversion["preflight_failures"][0]["check"] == "language"
    rows = _conversion_rows(tmp_path)
    assert [(r["outcome"], r["detail"]) for r in rows] == [("preflight_skipped", "language")]
    assert _summary(tmp_path)["convert_preflight_skipped"] == 1
    assert [q.lead_id for q in load_predictions(preds)] == ["00Q000000000001"]


def test_conversion_failure_never_blocks_watermark_or_workbook(tmp_path, monkeypatch):
    _enrich_with(monkeypatch, company=COMPLETE)

    class ExplodingSf(FakeSfClient):
        def convert_lead(self, conversions, *, converted_status, owner_id):
            raise ConnectionError("socket dropped mid-SOAP")

    sf = ExplodingSf(tasks=[_task(who="00Q000000000001")], leads=[_lead("00Q000000000001")])
    res, preds = _live_preds(sf, tmp_path)
    assert "ConnectionError" in res.conversion["error"]
    assert (tmp_path / "wm.json").exists() and (tmp_path / "qa.xlsx").exists()
    assert _conversion_rows(tmp_path)[0]["outcome"] == "error"
    assert _summary(tmp_path)["convert_error"].startswith("ConnectionError")
    # not converted by us => stays shadow evidence, and the next sweep can retry it
    assert [q.lead_id for q in load_predictions(preds)] == ["00Q000000000001"]


def test_failed_soap_row_is_recorded_and_left_to_shadow(tmp_path, monkeypatch):
    _enrich_with(monkeypatch, company=COMPLETE)

    class RejectingSf(FakeSfClient):
        def convert_lead(self, conversions, *, converted_status, owner_id):
            return [{"leadId": c["lead_id"], "success": False,
                     "errors": ["FIELD_CUSTOM_VALIDATION_EXCEPTION: nope"]}
                    for c in conversions]

    sf = RejectingSf(tasks=[_task(who="00Q000000000001")], leads=[_lead("00Q000000000001")])
    res, preds = _live_preds(sf, tmp_path)
    assert res.conversion["result"]["failed"] == 1
    row = _conversion_rows(tmp_path)[0]
    assert row["outcome"] == "failed" and "nope" in row["detail"]
    assert [q.lead_id for q in load_predictions(preds)] == ["00Q000000000001"]


# --- The sweep: unconverted campaign members from earlier runs (2026-11-01) --------
# Decision source = the lead's newest recorded prediction; create_new only; the
# KN_NoOwnDBID__c tick is written first (live) or previewed (dry).

OLD1, OLD2, OLD3 = "00Q000000000101", "00Q000000000102", "00Q000000000103"


def _sweep_fixture(tmp_path, monkeypatch, *, members, preds_rows, dry_run=False,
                   leads=None, settings=None):
    """This run has one shortage (human) lead; the org holds `members` from earlier."""
    _enrich_with(monkeypatch, company={"Street": "Hauptstr. 1"})
    preds = tmp_path / "predictions.jsonl"
    for lid, over in preds_rows:
        _write_prediction(preds, lid, **over)
    sf = FakeSfClient(tasks=[_task(who="00Q000000000001")],
                      leads=[_lead("00Q000000000001")] + list(leads or []),
                      members=members)
    res = run_pipeline(sf, object(), settings or SETTINGS, state=State(None, []),
                       since="2026-05-01T00:00:00Z", dry_run=dry_run,
                       qa_path=tmp_path / "qa.xlsx", state_path=tmp_path / "wm.json",
                       predictions_path=preds)
    return sf, res, preds


def _old_lead(lid, **over):
    base = dict(_lead(lid), Company="Alt GmbH", Street="Altweg 1", CountryCode="DE",
                KN_Employee__c="11-50", KN_DBID__c=None, KN_NoOwnDBID__c=False)
    base.update(over)
    return base


def test_sweep_converts_old_create_new_member_after_ticking_dbid(tmp_path, monkeypatch):
    sf, res, preds = _sweep_fixture(
        tmp_path, monkeypatch,
        members=[_member(OLD1)], leads=[_old_lead(OLD1)],
        preds_rows=[(OLD1, {"decision": "create_new"})])
    # one PATCH ticking the DBID flag, with the duplicate-rule header like the write-back
    tick = [recs for sobj, recs in sf.updates if recs and "KN_NoOwnDBID__c" in recs[0]
            and recs[0]["Id"] == OLD1]
    assert tick == [[{"Id": OLD1, "KN_NoOwnDBID__c": True}]]
    assert [c["lead_id"] for call in sf.convert_calls for c in call] == [OLD1]
    assert res.sweep.ticked == 1
    rows = {r["lead_id"]: r for r in _conversion_rows(tmp_path)}
    assert rows[OLD1]["source"] == "sweep" and rows[OLD1]["outcome"] == "converted"
    assert rows[OLD1]["decision"] == "create_new"
    assert rows[OLD1]["predicted_at"] == "2026-10-30T07:53:54Z"
    summary = _summary(tmp_path)
    assert summary["convert_targets_sweep"] == 1 and summary["sweep_dbid_ticked"] == 1
    # its pending prediction is excluded: shadow-compare must not score our conversion
    old = [q for q in load_predictions(preds) if q.lead_id == OLD1]
    assert [q.status for q in old] == ["excluded"]
    assert old[0].outcome == {"reason": "pipeline_converted"}
    # this run's own (shortage) lead is still recorded for humans
    assert [q.lead_id for q in load_predictions(preds) if q.status == "pending"] == [
        "00Q000000000001"]


def test_sweep_leaves_review_and_undecided_members_to_humans(tmp_path, monkeypatch):
    sf, res, preds = _sweep_fixture(
        tmp_path, monkeypatch,
        members=[_member(OLD1, company="Raiffeisen"), _member(OLD2, company="Nobody")],
        preds_rows=[(OLD1, {"decision": "review"})])     # OLD2: no prediction at all
    assert sf.convert_calls == []
    assert not any("KN_NoOwnDBID__c" in recs[0] and recs[0]["Id"] in (OLD1, OLD2)
                   for _, recs in sf.updates if recs)      # never ticked (2026-10-20 rule)
    rows = {r["lead_id"]: r for r in _conversion_rows(tmp_path)}
    assert rows[OLD1]["outcome"] == "left_to_human"
    assert rows[OLD1]["detail"] == "sweep_decision_review"
    assert rows[OLD2]["detail"] == "sweep_no_recorded_decision"
    assert _summary(tmp_path)["convert_left_to_human"] == 2
    assert [q.status for q in load_predictions(preds) if q.lead_id == OLD1] == ["pending"]


def test_sweep_skips_this_runs_own_leads_and_dedupes_members(tmp_path, monkeypatch):
    # This run's shortage lead is already a member when the sweep reads; a stale
    # create_new prediction for it must not convert it. A lead in two campaigns is one target.
    sf, res, preds = _sweep_fixture(
        tmp_path, monkeypatch,
        members=[_member("00Q000000000001"), _member(OLD1), _member(OLD1, campaign="701X")],
        leads=[_old_lead(OLD1)],
        preds_rows=[("00Q000000000001", {"decision": "create_new"}),
                    (OLD1, {"decision": "create_new"})])
    assert [c["lead_id"] for call in sf.convert_calls for c in call] == [OLD1]
    assert len(res.sweep.targets) == 1


def test_sweep_dry_run_previews_without_writing(tmp_path, monkeypatch):
    sf, res, preds = _sweep_fixture(
        tmp_path, monkeypatch, dry_run=True,
        members=[_member(OLD1)], leads=[_old_lead(OLD1)],
        preds_rows=[(OLD1, {"decision": "create_new"})])
    assert sf.updates == [] and sf.convert_calls == [] and sf.creates == []
    assert any(q.lstrip().startswith("SELECT LeadId,") for q in sf.queries)   # the read-only preview
    rows = {r["lead_id"]: r for r in _conversion_rows(tmp_path)}
    # the overlay previews the tick, so the preflight passes and the row is "dry_run"
    assert rows[OLD1]["outcome"] == "dry_run" and rows[OLD1]["detail"] == "dbid_tick_pending"
    assert _summary(tmp_path)["convert_dry_run"] == 1
    assert _summary(tmp_path)["sweep_dbid_ticked"] == 1
    # dry run: predictions recorded for everything, nothing excluded
    assert [q.status for q in load_predictions(preds) if q.lead_id == OLD1] == ["pending"]


def test_sweep_tick_failure_leaves_the_lead_to_a_human(tmp_path, monkeypatch):
    class TickRejectingSf(FakeSfClient):
        def update_records(self, sobject, records, allow_duplicates=False):
            if records and records[0].get("Id") == OLD1:
                self.updates.append((sobject, records))
                return {"processed": 1, "failed": 1,
                        "failures": [{"id": OLD1, "error": "FIELD_CUSTOM_VALIDATION: phone"}]}
            return super().update_records(sobject, records, allow_duplicates)

    _enrich_with(monkeypatch, company={"Street": "Hauptstr. 1"})
    preds = tmp_path / "predictions.jsonl"
    _write_prediction(preds, OLD1)
    sf = TickRejectingSf(tasks=[_task(who="00Q000000000001")],
                         leads=[_lead("00Q000000000001"), _old_lead(OLD1)],
                         members=[_member(OLD1)])
    run_pipeline(sf, object(), SETTINGS, state=State(None, []),
                 since="2026-05-01T00:00:00Z", dry_run=False,
                 qa_path=tmp_path / "qa.xlsx", state_path=tmp_path / "wm.json",
                 predictions_path=preds)
    assert sf.convert_calls == []
    row = {r["lead_id"]: r for r in _conversion_rows(tmp_path)}[OLD1]
    assert row["outcome"] == "left_to_human"
    assert row["detail"].startswith("sweep_dbid_tick_failed: FIELD_CUSTOM_VALIDATION")


def test_sweep_member_already_ticked_or_with_dbid_is_not_patched(tmp_path, monkeypatch):
    sf, res, preds = _sweep_fixture(
        tmp_path, monkeypatch,
        members=[_member(OLD1, no_own=True), _member(OLD2, dbid="123456")],
        leads=[_old_lead(OLD1, KN_NoOwnDBID__c=True), _old_lead(OLD2, KN_DBID__c="123456")],
        preds_rows=[(OLD1, {}), (OLD2, {})])
    assert not any(recs[0].get("Id") in (OLD1, OLD2) for _, recs in sf.updates if recs)
    assert sorted(c["lead_id"] for call in sf.convert_calls for c in call) == [OLD1, OLD2]
    assert res.sweep.ticked == 0


def test_sweep_uses_the_newest_prediction_per_lead(tmp_path, monkeypatch):
    sf, res, preds = _sweep_fixture(
        tmp_path, monkeypatch,
        members=[_member(OLD1)], leads=[_old_lead(OLD1)],
        preds_rows=[(OLD1, {"decision": "create_new", "predicted_at": "2026-09-01T00:00:00Z",
                            "status": "expired"}),
                    (OLD1, {"decision": "review", "predicted_at": "2026-10-30T00:00:00Z"})])
    assert sf.convert_calls == []
    assert _conversion_rows(tmp_path)[0]["detail"] == "sweep_decision_review"


# --- Conversion holdouts (2026-11-07): lead 00QMz00000rov6DMAQ failed SOAP with
# DUPLICATES_DETECTED on four runs in a row and every run exited 1. A permanent failure
# is recorded beside the watermark; the next sweep leaves the lead to a human.

class DuplicateSf(FakeSfClient):
    def convert_lead(self, conversions, *, converted_status, owner_id):
        self.convert_calls.append(conversions)
        return [{"leadId": c["lead_id"], "success": False,
                 "errors": ["DUPLICATES_DETECTED: Duplicate detected!"]}
                for c in conversions]


def _holdouts(tmp_path):
    p = tmp_path / "convert_holdouts.jsonl"
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()] if p.exists() else None


def test_duplicate_rule_failure_is_recorded_as_a_holdout_and_still_fails_this_run(
        tmp_path, monkeypatch):
    _enrich_with(monkeypatch, company={"Street": "Hauptstr. 1"})
    preds = tmp_path / "predictions.jsonl"
    _write_prediction(preds, OLD1)
    sf = DuplicateSf(tasks=[_task(who="00Q000000000001")],
                     leads=[_lead("00Q000000000001"), _old_lead(OLD1)], members=[_member(OLD1)])
    res = run_pipeline(sf, object(), SETTINGS, state=State(None, []),
                       since="2026-05-01T00:00:00Z", dry_run=False,
                       qa_path=tmp_path / "qa.xlsx", state_path=tmp_path / "wm.json",
                       predictions_path=preds)
    assert [c["lead_id"] for call in sf.convert_calls for c in call] == [OLD1]
    assert res.conversion["result"]["failed"] == 1          # the CLI exits 1 on this run
    assert res.conversion["held_out"] == [OLD1]
    rows = _holdouts(tmp_path)
    assert rows and rows[0]["lead_id"] == OLD1 and rows[0]["company"] == "Alt GmbH"
    assert rows[0]["reason"] == "convert_duplicate_detected"
    assert rows[0]["error"] == "DUPLICATES_DETECTED: Duplicate detected!"
    conv = {r["lead_id"]: r for r in _conversion_rows(tmp_path)}
    assert conv[OLD1]["outcome"] == "failed"
    assert conv[OLD1]["detail"].endswith("[held out: left to a human from the next run on]")
    assert _summary(tmp_path)["convert_held_out"] == 1
    # still shadow evidence: a human converts it, the prediction stays pending
    assert [q.status for q in load_predictions(preds) if q.lead_id == OLD1] == ["pending"]

    # NEXT run: same member, same decision -> left to a human, no SOAP call, green
    sf2 = DuplicateSf(tasks=[_task(who="00Q000000000001")],
                      leads=[_lead("00Q000000000001"), _old_lead(OLD1)], members=[_member(OLD1)])
    res2 = run_pipeline(sf2, object(), SETTINGS, state=State(None, []),
                        since="2026-05-01T00:00:00Z", dry_run=False,
                        qa_path=tmp_path / "qa.xlsx", state_path=tmp_path / "wm.json",
                        predictions_path=preds)
    assert sf2.convert_calls == []
    assert res2.conversion["result"]["failed"] == 0 and res2.conversion["held_out"] == []
    assert not any("KN_NoOwnDBID__c" in recs[0] and recs[0]["Id"] == OLD1
                   for _, recs in sf2.updates if recs)
    conv2 = {r["lead_id"]: r for r in _conversion_rows(tmp_path)}
    assert conv2[OLD1]["outcome"] == "left_to_human"
    assert conv2[OLD1]["detail"].startswith("sweep_held_out: convert_duplicate_detected")
    assert _summary(tmp_path)["convert_left_to_human"] == 1
    assert len(_holdouts(tmp_path)) == 1                      # not re-appended


def test_transient_failure_and_dry_run_record_no_holdout(tmp_path, monkeypatch):
    class LimitSf(FakeSfClient):
        def convert_lead(self, conversions, *, converted_status, owner_id):
            return [{"leadId": c["lead_id"], "success": False,
                     "errors": ["LIMIT_EXCEEDED: Apex CPU time limit"]} for c in conversions]

    _enrich_with(monkeypatch, company={"Street": "Hauptstr. 1"})
    preds = tmp_path / "predictions.jsonl"
    _write_prediction(preds, OLD1)
    for cls, dry in ((LimitSf, False), (DuplicateSf, True)):
        sf = cls(tasks=[_task(who="00Q000000000001")],
                 leads=[_lead("00Q000000000001"), _old_lead(OLD1)], members=[_member(OLD1)])
        res = run_pipeline(sf, object(), SETTINGS, state=State(None, []),
                           since="2026-05-01T00:00:00Z", dry_run=dry,
                           qa_path=tmp_path / "qa.xlsx", state_path=tmp_path / "wm.json",
                           predictions_path=preds)
        assert res.conversion["held_out"] == []
        assert _holdouts(tmp_path) is None                    # the next sweep retries it
