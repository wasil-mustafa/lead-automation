"""Domain-rescue backtest tests — fully offline.

What these pin is the honesty of the report: a control that no longer conflicts is
`drifted`, never a recovery; a rescue hit whose website is not the lead's domain is
refused; a hit whose summary country still conflicts is `recovered_foreign` (production
pays a retrieve and rejects it again); and the population is only the gate-rejected
leads — `no_lf_match` rows that still carry an lf_company_id.
"""
import json

from leadauto.config import Settings
from leadauto.domain_rescue_backtest import classify, run_backtest, select_predictions
from leadauto.leadfeeder.client import MATCH_RESULTS_PRIMARY
from leadauto.predictions import Prediction
from tests.test_wide_match_backtest import FakeSf, _lead, _task

SETTINGS = Settings.load()


def _prediction(lead_id="00Q1", task_id="T1", rule="no_lf_match", lf="at1",
                at="2026-11-04T00:00:00Z"):
    return Prediction(lead_id=lead_id, task_id=task_id, predicted_at=at, rule=rule,
                      decision="skip", lf_company_id=lf)


class FakeLf:
    """Answers name payloads from `by_name`, url payloads from `by_url`."""

    def __init__(self, by_name=None, by_url=None):
        self.by_name, self.by_url = by_name or {}, by_url or {}
        self.widths, self.asked = [], []
        self.credits_charged = 0

    def match_companies(self, payloads, max_results=3):
        self.widths.append(max_results)
        self.asked.append([dict(p) for p in payloads])
        return [self.by_url.get(p["url"]) if p.get("url")
                else self.by_name.get(p["company_name"]) for p in payloads]


def _write_preds(tmp_path, preds):
    path = tmp_path / "preds.jsonl"
    path.write_text("".join(json.dumps(p.__dict__) + "\n" for p in preds), encoding="utf-8")
    return path


AT = {"company_id": "at1", "match_score": 0.89, "matched_name": "Acme GmbH",
      "matched_url": "https://www.acme-gmbh.at/", "matched_country_code": "AT"}
DE = {"company_id": "de1", "match_score": 0.88, "matched_name": "Acme GmbH",
      "name_agreement": 100, "matched_url": "https://www.acme.de", "matched_country_code": "DE"}


# --- population -----------------------------------------------------------------------

def test_only_gate_rejected_leads_are_replayed():
    preds = [_prediction(), _prediction("00Q2", "T2", lf=None),
             _prediction("00Q3", "T3", rule="no_match", lf="x")]
    assert [p.lead_id for p in select_predictions(preds)] == ["00Q1"]


# --- classification ---------------------------------------------------------------------

def test_recovered_needs_a_conflict_a_domain_hit_and_an_agreeing_country():
    assert classify(AT, "DE", "acme.de", DE, True) == "recovered"


def test_a_hit_whose_country_still_conflicts_is_foreign_not_recovered():
    ch = {**DE, "matched_country_code": "CH"}
    assert classify(AT, "DE", "acme.de", ch, True) == "recovered_foreign"


def test_a_hit_whose_website_is_not_the_domain_is_refused():
    other = {**DE, "matched_url": "https://acme-gmbh.de/"}
    assert classify(AT, "DE", "acme.de", other, True) == "rescue_url_mismatch"
    assert classify(AT, "DE", "acme.de", None, True) == "rescue_no_match"


def test_no_conflict_today_is_drift_never_a_recovery():
    assert classify({**AT, "matched_country_code": "DE"}, "DE", "acme.de", DE, False) \
        == "drifted_no_conflict"
    assert classify(None, "DE", "acme.de", DE, False) == "drifted_unmatched"


def test_freemail_and_missing_country_are_named_not_guessed():
    assert classify(AT, "DE", None, None, False) == "no_corporate_domain"
    assert classify({**AT, "matched_country_code": None}, "DE", "acme.de", None, False) \
        == "control_no_country"


# --- the run ------------------------------------------------------------------------------

def test_run_rescues_only_the_conflicting_leads_and_sends_the_domain_as_url(tmp_path):
    sf = FakeSf(tasks=[_task(), _task("T2", "00Q2", company="Beta GmbH")],
                leads=[_lead(), _lead("00Q2", "Beta GmbH")])
    # Acme: control conflicts (AT vs the /de/ slug), rescue hits acme.de.
    # Beta: control agrees today -> drifted, never re-asked.
    lf = FakeLf(by_name={"Acme GmbH": AT,
                         "Beta GmbH": {**AT, "company_id": "b1", "matched_name": "Beta GmbH",
                                       "matched_country_code": "DE"}},
                by_url={"acme.de": DE})
    preds = [_prediction(), _prediction("00Q2", "T2", lf="b1")]
    res = run_backtest(sf, lf, SETTINGS, xlsx_path=tmp_path / "r.xlsx",
                       predictions_path=_write_preds(tmp_path, preds), profiles={})
    assert lf.widths == [MATCH_RESULTS_PRIMARY, MATCH_RESULTS_PRIMARY]
    assert lf.asked[1] == [{"company_name": "Acme GmbH", "url": "acme.de"}]
    assert res["tally"] == {"recovered": 1, "drifted_no_conflict": 1}
    assert res["credits"] == 0
    assert (tmp_path / "r.xlsx").exists()
    by_lead = {r["lead_id"]: r for r in res["rows"]}
    assert by_lead["00Q1"]["rescue_website"] == "acme.de"
    assert by_lead["00Q1"]["lead_country"] == "DE"


def test_run_with_nothing_replayable_writes_no_report(tmp_path):
    path = tmp_path / "empty.jsonl"
    path.write_text("", encoding="utf-8")
    res = run_backtest(FakeSf(), FakeLf(), SETTINGS, xlsx_path=tmp_path / "r.xlsx",
                       predictions_path=path, profiles={})
    assert res["tally"] == {} and res["xlsx"] is None
    assert not (tmp_path / "r.xlsx").exists()
