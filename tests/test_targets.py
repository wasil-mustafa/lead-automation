"""convert/targets.py — in-memory conversion targets (2026-11-01).

`targets_from_results` carries the link rules the approved CSV used to enforce;
`sweep_targets` turns earlier runs' unconverted campaign members into targets from their
recorded prediction, ticking KN_NoOwnDBID__c first. Pure fakes, no org.
"""
import json

import pytest

from leadauto.config import Settings
from leadauto.convert import ConversionTarget
from leadauto.convert.targets import (SWEEP_NO_DECISION, SWEEP_TICK_FAILED,
                                      sweep_targets, targets_from_results)
from leadauto.models import AccountMatch, ContactMatch, EnrichedLead, ExtractedLead, MatchResult
from leadauto.sf.client import SfError

SETTINGS = Settings.load()

LEAD1, LEAD2, LEAD3 = "00Q000000000001", "00Q000000000002", "00Q000000000003"
ACCT1, CONT1 = "001000000000001", "003000000000001"


def _result(lead_id, decision="create_new", account=None, contact=None, **enr):
    e = ExtractedLead(lead_id=lead_id, task_id="T_" + lead_id, task_type="claimer",
                      company_name="Acme GmbH", kununu_url="https://www.kununu.com/de/acme",
                      kununu_profile_uuid="puid-1")
    return MatchResult(enriched=EnrichedLead(extracted=e, matched=True, lf_company_id="LF-1",
                                             contact={"contact_emails": "lf@acme.de"}, **enr),
                       account=account, contact=contact, decision=decision)


# --- targets_from_results ---------------------------------------------------------

def test_only_convert_ids_become_targets_with_cluster_and_email_keys():
    results = [_result(LEAD1), _result(LEAD2)]
    targets = targets_from_results(results, {LEAD2})
    assert targets == [ConversionTarget(LEAD2, lf_company_id="LF-1",
                                        kununu_profile_uuid="puid-1",
                                        kununu_url="https://www.kununu.com/de/acme",
                                        lf_contact_email="lf@acme.de")]


def test_convert_ids_match_on_15_chars():
    assert [t.lead_id for t in targets_from_results([_result(LEAD1 + "AAA")], {LEAD1})] == [
        LEAD1 + "AAA"]


def test_contact_linked_only_on_identifier_reasons():
    results = [
        _result(LEAD1, "link", AccountMatch(ACCT1, "name_address"),
                ContactMatch(CONT1, 1.0, "kununu_user_uuid")),
        _result(LEAD2, "link", AccountMatch(ACCT1, "name_address"),
                ContactMatch(CONT1, 0.5, "contact_email_global")),
    ]
    targets = targets_from_results(results, {LEAD1, LEAD2})
    assert targets[0].account_id == ACCT1 and targets[0].contact_id == CONT1
    assert targets[1].account_id == ACCT1 and targets[1].contact_id is None


def test_contact_link_dropped_without_account_link():
    # LeadConvert requires accountId alongside contactId -> create-new instead
    results = [_result(LEAD1, "review", None, ContactMatch(CONT1, 1.0, "kununu_user_uuid"))]
    t = targets_from_results(results, {LEAD1})[0]
    assert t.account_id is None and t.contact_id is None


def test_skip_decision_reaching_the_converter_raises():
    with pytest.raises(ValueError, match="skip"):
        targets_from_results([_result(LEAD1, "skip")], {LEAD1})


def test_skip_outside_convert_ids_is_ignored():
    assert targets_from_results([_result(LEAD1, "skip")], set()) == []


# --- sweep_targets ----------------------------------------------------------------

FEDUP = SETTINGS.lf_campaigns["matched_complete"]


class FakeSf:
    def __init__(self, members, tick=None):
        self.members = members
        self.queries = []
        self.updates = []
        self._tick = tick or (lambda records: {"processed": len(records), "failed": 0,
                                               "failures": []})

    def query(self, soql):
        self.queries.append(soql)
        return [dict(m) for m in self.members]

    def update_records(self, sobject, records, allow_duplicates=False):
        self.updates.append((sobject, records, allow_duplicates))
        return self._tick(records)


def _member(lid, dbid=None, no_own=False, nested=True, company="Alt GmbH"):
    if nested:   # REST
        return {"LeadId": lid, "Lead": {"attributes": {}, "Company": company,
                                        "KN_DBID__c": dbid, "KN_NoOwnDBID__c": no_own}}
    return {"LeadId": lid, "Lead.Company": company, "Lead.KN_DBID__c": dbid or "",
            "Lead.KN_NoOwnDBID__c": "true" if no_own else "false"}   # Bulk CSV


def _preds(tmp_path, *rows):
    p = tmp_path / "predictions.jsonl"
    with p.open("w", encoding="utf-8") as fh:
        for lid, decision, at in rows:
            fh.write(json.dumps({
                "lead_id": lid, "task_id": "T", "predicted_at": at, "rule": "no_match",
                "decision": decision, "account_id": "001000000000009" if decision == "link" else None,
                "contact_id": None, "evidence": [], "lf_company_id": "LF-7",
                "payload": {"KN_KununuLinkProfile__c": "https://www.kununu.com/de/alt"},
                "lf_company_name": "Alt", "lf_website": None, "lf_match_pass": "primary",
                "written_back": True, "status": "pending", "resolved_at": None,
                "outcome": None}) + "\n")
    return p


def test_sweep_off_without_campaign_axis(tmp_path):
    s = Settings.load()
    s.raw["pipeline"] = dict(s.raw["pipeline"], live_scope={"decisions": ["create_new"]})
    sf = FakeSf([_member(LEAD1)])
    out = sweep_targets(sf, s, predictions_path=_preds(tmp_path), dry_run=False)
    assert out.queried is False and out.targets == [] and sf.queries == []


def test_sweep_reads_the_fedup_campaign_with_lead_statuses(tmp_path):
    sf = FakeSf([])
    out = sweep_targets(sf, SETTINGS, predictions_path=_preds(tmp_path), dry_run=False)
    assert out.queried is True and out.targets == []
    assert FEDUP in sf.queries[0] and "Lead.Status IN ('New')" in sf.queries[0]


@pytest.mark.parametrize("nested", [True, False])
def test_sweep_builds_create_new_targets_and_ticks_dbid(tmp_path, nested):
    sf = FakeSf([_member(LEAD1, nested=nested), _member(LEAD2, no_own=True, nested=nested),
                 _member(LEAD3, dbid="123", nested=nested)])
    preds = _preds(tmp_path, (LEAD1, "create_new", "2026-10-30T00:00:00Z"),
                   (LEAD2, "create_new", "2026-10-30T00:00:00Z"),
                   (LEAD3, "create_new", "2026-10-30T00:00:00Z"))
    out = sweep_targets(sf, SETTINGS, predictions_path=preds, dry_run=False)
    assert [t.lead_id for t in out.targets] == [LEAD1, LEAD2, LEAD3]
    assert out.targets[0] == ConversionTarget(
        LEAD1, lf_company_id="LF-7", kununu_url="https://www.kununu.com/de/alt")
    # only LEAD1 needs the tick; the header rides along like the write-back's
    assert sf.updates == [("Lead", [{"Id": LEAD1, "KN_NoOwnDBID__c": True}],
                           SETTINGS.writeback_allow_duplicates)]
    assert out.ticked == 1 and out.left == []
    assert out.info[LEAD1]["company"] == "Alt GmbH"


def test_sweep_decision_filter_and_fail_closed(tmp_path):
    sf = FakeSf([_member(LEAD1), _member(LEAD2), _member(LEAD3)])
    preds = _preds(tmp_path, (LEAD1, "review", "2026-10-30T00:00:00Z"),
                   (LEAD2, "link", "2026-10-30T00:00:00Z"))
    out = sweep_targets(sf, SETTINGS, predictions_path=preds, dry_run=False)
    assert out.targets == [] and sf.updates == []        # never ticked
    assert {l["lead_id"]: l["reason"] for l in out.left} == {
        LEAD1: "sweep_decision_review", LEAD2: "sweep_decision_link",
        LEAD3: SWEEP_NO_DECISION}
    assert out.left[0]["predicted_at"] == "2026-10-30T00:00:00Z"


def test_sweep_without_predictions_file_leaves_everything(tmp_path):
    sf = FakeSf([_member(LEAD1)])
    out = sweep_targets(sf, SETTINGS, predictions_path=None, dry_run=False)
    assert out.targets == [] and out.left[0]["reason"] == SWEEP_NO_DECISION


def test_sweep_link_decision_links_the_predicted_account_when_allowed(tmp_path):
    s = Settings.load()
    s.raw["pipeline"]["live_scope"] = dict(s.raw["pipeline"]["live_scope"],
                                           decisions=["create_new", "link"])
    sf = FakeSf([_member(LEAD1, no_own=True)])
    out = sweep_targets(sf, s, predictions_path=_preds(
        tmp_path, (LEAD1, "link", "2026-10-30T00:00:00Z")), dry_run=False)
    assert out.targets[0].account_id == "001000000000009"


def test_sweep_excludes_this_runs_leads_and_dedupes(tmp_path):
    sf = FakeSf([_member(LEAD1), _member(LEAD2 + "AAA"), _member(LEAD2 + "AAA")])
    preds = _preds(tmp_path, (LEAD1, "create_new", "x"), (LEAD2, "create_new", "x"))
    out = sweep_targets(sf, SETTINGS, exclude_ids=[LEAD1 + "AAA"], predictions_path=preds,
                        dry_run=False)
    assert [t.lead_id for t in out.targets] == [LEAD2 + "AAA"]


def test_sweep_dry_run_only_previews_the_tick(tmp_path):
    sf = FakeSf([_member(LEAD1)])
    out = sweep_targets(sf, SETTINGS, predictions_path=_preds(
        tmp_path, (LEAD1, "create_new", "x")), dry_run=True)
    assert sf.updates == [] and out.ticked == 1 and out.tick_pending == {LEAD1}
    assert [t.lead_id for t in out.targets] == [LEAD1]


def test_sweep_tick_row_failure_leaves_that_lead(tmp_path):
    def tick(records):
        return {"processed": 2, "failed": 1,
                "failures": [{"id": LEAD1, "error": "FIELD_CUSTOM_VALIDATION: phone"}]}
    sf = FakeSf([_member(LEAD1), _member(LEAD2)], tick=tick)
    preds = _preds(tmp_path, (LEAD1, "create_new", "x"), (LEAD2, "create_new", "x"))
    out = sweep_targets(sf, SETTINGS, predictions_path=preds, dry_run=False)
    assert [t.lead_id for t in out.targets] == [LEAD2]
    assert out.ticked == 1
    assert out.left == [{"lead_id": LEAD1, "reason": SWEEP_TICK_FAILED,
                         "error": "FIELD_CUSTOM_VALIDATION: phone", "company": "Alt GmbH",
                         "decision": "create_new", "predicted_at": "x"}]


def test_sweep_tick_request_error_leaves_all_ticked_leads(tmp_path):
    def tick(records):
        raise SfError("HTTP 500")
    sf = FakeSf([_member(LEAD1), _member(LEAD2, no_own=True)], tick=tick)
    preds = _preds(tmp_path, (LEAD1, "create_new", "x"), (LEAD2, "create_new", "x"))
    out = sweep_targets(sf, SETTINGS, predictions_path=preds, dry_run=False)
    assert [t.lead_id for t in out.targets] == [LEAD2]     # needed no tick
    assert out.left[0]["reason"] == SWEEP_TICK_FAILED and out.ticked == 0


def test_sweep_never_ticks_when_the_dbid_fields_are_absent_from_the_row(tmp_path):
    # Neither the nested nor the flat shape carries the two DBID fields: an unknown
    # transport shape. Ticking on a guess could set KN_NoOwnDBID__c on a lead that HAS
    # a DBID (KN_DbIdValidation) — leave it to a human instead.
    from leadauto.convert.targets import SWEEP_DBID_UNKNOWN
    sf = FakeSf([{"LeadId": LEAD1, "Lead": {"Company": "Alt GmbH"}}])
    out = sweep_targets(sf, SETTINGS, predictions_path=_preds(
        tmp_path, (LEAD1, "create_new", "x")), dry_run=False)
    assert out.targets == [] and sf.updates == []
    assert out.left[0]["reason"] == SWEEP_DBID_UNKNOWN


def test_sweep_null_dbid_is_blank_not_missing(tmp_path):
    # REST returns `"KN_DBID__c": null` for an empty field — that IS known and blank.
    sf = FakeSf([{"LeadId": LEAD1, "Lead": {"Company": "Alt GmbH", "KN_DBID__c": None,
                                            "KN_NoOwnDBID__c": False}}])
    out = sweep_targets(sf, SETTINGS, predictions_path=_preds(
        tmp_path, (LEAD1, "create_new", "x")), dry_run=False)
    assert [t.lead_id for t in out.targets] == [LEAD1] and out.ticked == 1
