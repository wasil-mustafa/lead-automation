"""Prediction-log tests: JSONL round-trip, pending upsert, resolved immutability,
and the stage-1 dry-run hook."""
from types import SimpleNamespace

from leadauto.config import Settings
from leadauto.models import ExtractedLead
from leadauto.models import EnrichedLead
from leadauto.match import ExistingRecords
from leadauto.models import AccountMatch, MatchResult
from leadauto.predictions import (
    Prediction, load_predictions, record_predictions, save_predictions,
)

SETTINGS = Settings.load()


def _result(lead_id="00Q1", rule="name_only", decision="review", account_id="001A"):
    e = ExtractedLead(lead_id=lead_id, task_id="T1", task_type="claimer")
    enr = EnrichedLead(extracted=e, matched=True, matched_name="Acme GmbH",
                       lf_company_id="lf-1", company={"Website": "https://acme.de"})
    acct = AccountMatch(account_id, rule) if account_id else None
    return MatchResult(enriched=enr, account=acct, contact=None,
                       decision=decision, rule=rule, evidence=["vat→001A"])


def test_record_and_load_roundtrip(tmp_path):
    p = tmp_path / "predictions.jsonl"
    n = record_predictions([_result()], [{"Id": "00Q1", "KN_DBCity__c": "Berlin"}],
                           "2026-07-03T09:00:00+00:00", p)
    assert n == 1
    preds = load_predictions(p)
    assert preds[0].lead_id == "00Q1"
    assert preds[0].rule == "name_only"
    assert preds[0].payload == {"KN_DBCity__c": "Berlin"}   # Id stripped
    assert preds[0].lf_website == "https://acme.de"
    assert preds[0].status == "pending"


def test_upsert_replaces_pending_keeps_resolved(tmp_path):
    p = tmp_path / "predictions.jsonl"
    record_predictions([_result(rule="name_only")], [], "2026-07-01T09:00:00+00:00", p)
    preds = load_predictions(p)
    preds[0].status = "resolved"
    save_predictions(preds, p)
    record_predictions([_result(rule="name_address", decision="link"),
                        _result(lead_id="00Q2", rule="no_match", decision="create_new",
                                account_id=None)],
                       [], "2026-07-02T09:00:00+00:00", p)
    preds = {pr.lead_id: [q for q in load_predictions(p) if q.lead_id == pr.lead_id]
             for pr in load_predictions(p)}
    assert len(preds["00Q1"]) == 2            # resolved kept + fresh pending
    statuses = {q.status for q in preds["00Q1"]}
    assert statuses == {"resolved", "pending"}
    assert preds["00Q2"][0].rule == "no_match"


def test_stage1_records_predictions_on_dry_run(tmp_path, monkeypatch):
    from leadauto.pipeline import run as s1
    from leadauto.state import State

    e = ExtractedLead(lead_id="00Q9", task_id="T9", task_type="claimer",
                      company_name="Acme GmbH")
    monkeypatch.setattr(s1, "read_extracted_leads", lambda *a, **k: SimpleNamespace(
        leads=[e], skipped=[], processed_tasks=[]))
    monkeypatch.setattr(s1, "load_existing_records",
                        lambda *a, **k: ExistingRecords())
    p = tmp_path / "predictions.jsonl"
    s1.run_pipeline(None, None, SETTINGS, state=State(), dry_run=True, convert=False,
                  qa_path=None, state_path=None, predictions_path=p)
    preds = load_predictions(p)
    assert [q.lead_id for q in preds] == ["00Q9"]
    assert preds[0].rule == "no_lf_match"     # lf_client=None -> unmatched -> review

    # A NON-dry run records predictions for OUT-OF-SCOPE leads only (phased go-live):
    # this skip-decision lead is outside the live scope, so it still lands in the
    # shadow log — humans will convert it, keeping the backtest evidence flowing.
    p2 = tmp_path / "live.jsonl"
    monkeypatch.setattr(s1, "write_back",
                        lambda *a, **k: {"payloads": [], "failures": []})
    s1.run_pipeline(None, None, SETTINGS, state=State(), dry_run=False, convert=False,
                    qa_path=None, state_path=None, predictions_path=p2)
    preds = load_predictions(p2)
    assert [q.lead_id for q in preds] == ["00Q9"]
    assert preds[0].decision == "skip"


def test_stage1_skips_predictions_when_config_disabled(tmp_path, monkeypatch):
    # pipeline.record_predictions: false (one-off backlog runs) suppresses the
    # dry-run prediction log — those leads get converted by the pipeline itself,
    # so shadow-compare must never score them.
    from leadauto.pipeline import run as s1
    from leadauto.state import State

    settings = Settings.load()
    settings.raw["pipeline"]["record_predictions"] = False
    e = ExtractedLead(lead_id="00Q9", task_id="T9", task_type="claimer",
                      company_name="Acme GmbH")
    monkeypatch.setattr(s1, "read_extracted_leads", lambda *a, **k: SimpleNamespace(
        leads=[e], skipped=[], processed_tasks=[]))
    monkeypatch.setattr(s1, "load_existing_records",
                        lambda *a, **k: ExistingRecords())
    p = tmp_path / "predictions.jsonl"
    s1.run_pipeline(None, None, settings, state=State(), dry_run=True, convert=False,
                  qa_path=None, state_path=None, predictions_path=p)
    assert not p.exists()


# --- sweep helpers (2026-11-01) -----------------------------------------------------

def test_latest_by_lead_picks_newest_regardless_of_status(tmp_path):
    from leadauto.predictions import latest_by_lead
    p = tmp_path / "predictions.jsonl"
    record_predictions([_result(rule="name_only")], [], "2026-07-01T09:00:00+00:00", p)
    preds = load_predictions(p)
    preds[0].status = "expired"
    save_predictions(preds, p)
    record_predictions([_result(rule="no_match", decision="create_new", account_id=None)],
                       [], "2026-07-02T09:00:00+00:00", p)
    latest = latest_by_lead(load_predictions(p))
    assert latest["00Q1"].decision == "create_new"
    assert latest["00Q1"].predicted_at == "2026-07-02T09:00:00+00:00"


def test_exclude_predictions_marks_only_pending_rows_of_the_given_leads(tmp_path):
    from leadauto.predictions import exclude_predictions
    p = tmp_path / "predictions.jsonl"
    record_predictions([_result(), _result(lead_id="00Q2", account_id=None,
                                           decision="create_new", rule="no_match")],
                       [], "2026-07-01T09:00:00+00:00", p)
    preds = load_predictions(p)
    preds[0].status = "resolved"
    save_predictions(preds, p)
    assert exclude_predictions(["00Q1", "00Q2", "00Q9"], "pipeline_converted", p,
                               resolved_at="2026-11-01T08:00:00+00:00") == 1
    by_lead = {q.lead_id: q for q in load_predictions(p)}
    assert by_lead["00Q1"].status == "resolved"          # non-pending untouched
    assert by_lead["00Q2"].status == "excluded"
    assert by_lead["00Q2"].outcome == {"reason": "pipeline_converted"}
    assert by_lead["00Q2"].resolved_at == "2026-11-01T08:00:00+00:00"
    assert exclude_predictions([], "x", p) == 0
    assert exclude_predictions(["00Q2"], "x", tmp_path / "missing.jsonl") == 0


def test_exclude_predictions_matches_18_char_rows_against_15_char_ids(tmp_path):
    # The file holds 18-char ids; conversion results are keyed on 15 chars. Found by
    # review 2026-11-01: an exact compare excluded nothing, so shadow-compare would
    # have scored the pipeline's own conversions as human hits.
    from leadauto.predictions import exclude_predictions
    p = tmp_path / "predictions.jsonl"
    record_predictions([_result(lead_id="00QMz00000ra0nlMAA")], [],
                       "2026-10-30T07:53:54.555+00:00", p)
    assert exclude_predictions({"00QMz00000ra0nl"}, "pipeline_converted", p) == 1
    assert load_predictions(p)[0].status == "excluded"


def test_latest_by_lead_orders_mixed_timestamp_suffixes(tmp_path):
    # "...Z" vs "...+00:00" on the same second must not reorder; garbage never wins.
    from leadauto.predictions import latest_by_lead
    rows = [Prediction("00Q1", "T", "2026-10-30T07:00:00Z", "no_match", "create_new"),
            Prediction("00Q1", "T", "2026-10-30T07:00:00.000001+00:00", "name_only", "review"),
            Prediction("00Q1", "T", "not-a-date", "no_match", "create_new")]
    assert latest_by_lead(rows)["00Q1"].decision == "review"
