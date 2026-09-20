"""Second-chance ("wide") match backtest tests — fully offline.

What these pin is the honesty of the report, not the plumbing: `drifted` must never be
counted as a recovery (LF's index moves on its own), the control arm must ask at the
calibrated width, and a lead whose Task or Lead record is gone must be DROPPED rather
than replayed with a guessed payload.
"""
from leadauto.config import Settings
from leadauto.leadfeeder.client import MATCH_RESULTS_PRIMARY
from leadauto.predictions import Prediction
from leadauto.wide_match_backtest import (build_cases, classify, run_backtest,
                                          select_cases)

SETTINGS = Settings.load()

# A real claimer Description (same shape as tests/test_parsers.py CLAIMER_DESC), because
# the point of the payload test is that the TASK-parsed name is what gets sent.
def _description(company="Acme GmbH", slug="acme", email="jane@acme.de"):
    return (
        f"Das Profil {company} https://www.kununu.com/de/{slug} wurde vor kurzem von  "
        "als kostenloses Arbeitgeberprofil aktiviert (claimed, beansprucht)."
        "Dadurch hat das Unternehmen Zugriff auf das Profil." + chr(10) +
        f"Bitte kontaktiere Deinen Ansprechpartner unter der E-Mail {email}, um ihn "
        "als Kunden zu gewinnen." + chr(10) * 2 +
        "Details: " + chr(10) +
        f"- Profile-Name: {company}" + chr(10) +
        "- Profil beansprucht am: 19.06.2026" + chr(10) +
        "- Profil Type: claimed" + chr(10) +
        f"- Email: {email}" + chr(10) +
        "- User Name: Jane Doe" + chr(10) +
        "- User-ID: 7dcf0627-2a22-4c78-aebb-22976bb072a7" + chr(10) +
        "- Profil-ID: bc3e53a8-1795-489c-b966-315dc36e50e3"
    )


def _prediction(lead_id="00Q1", task_id="T1", rule="no_lf_match", at="2026-08-01T00:00:00Z"):
    return Prediction(lead_id=lead_id, task_id=task_id, predicted_at=at,
                      rule=rule, decision="skip")


class FakeSf:
    """Answers the two batched reads by object name; records the SOQL it was given."""

    def __init__(self, tasks=(), leads=()):
        self.tasks, self.leads = list(tasks), list(leads)
        self.queries = []

    def query(self, soql):
        self.queries.append(soql)
        return self.tasks if " FROM Task " in soql else self.leads


class FakeLf:
    """Returns `by_name` at width 3 and `wide_by_name` above it, recording every width."""

    def __init__(self, by_name=None, wide_by_name=None):
        self.by_name = by_name or {}
        self.wide_by_name = wide_by_name or {}
        self.widths = []
        self.asked = []
        self.credits_charged = 0

    def match_companies(self, payloads, max_results=3):
        self.widths.append(max_results)
        self.asked.append([p.get("company_name") for p in payloads])
        table = (self.by_name if max_results <= MATCH_RESULTS_PRIMARY
                 else {**self.by_name, **self.wide_by_name})
        return [table.get(p["company_name"]) for p in payloads]


def _task(task_id="T1", who="00Q1", subject="Inbound Sales Task: New Claimed Profile",
          company="Acme GmbH"):
    return {"Id": task_id, "WhoId": who, "Subject": subject,
            "Description": _description(company),
            "CreatedDate": "2026-08-01T00:00:00.000+0000"}


def _lead(lead_id="00Q1", company="Acme GmbH"):
    return {"Id": lead_id, "Company": company, "Email": "jane@acme.de",
            "FirstName": "Jane", "LastName": "Doe"}


# --- population selection -----------------------------------------------------------

def test_only_no_lf_match_leads_are_replayable():
    # Every other rule means LF DID match; replaying those measures nothing.
    preds = [_prediction(lead_id="00Q1"),
             _prediction(lead_id="00Q2", rule="no_match"),
             _prediction(lead_id="00Q3", rule="name_address")]
    assert [pr.lead_id for pr in select_cases(preds)] == ["00Q1"]


def test_one_case_per_lead_newest_wins():
    # Predictions accumulate a row per lead per dry run; replaying each row would bill
    # the same question repeatedly and inflate every count in the report.
    preds = [_prediction(at="2026-07-01T00:00:00Z"),
             _prediction(at="2026-08-20T00:00:00Z"),
             _prediction(at="2026-08-02T00:00:00Z")]
    [case] = select_cases(preds)
    assert case.predicted_at == "2026-08-20T00:00:00Z"


# --- outcome classification ---------------------------------------------------------

def test_a_control_match_is_drift_not_recovery():
    # The control arm exists for exactly this: LF's index changed since the prediction
    # was recorded, and the width had nothing to do with it.
    assert classify({"company_id": "c1"}, None) == "drifted"
    assert classify({"company_id": "c1"}, {"company_id": "c2"}) == "drifted"


def test_recovery_requires_the_control_to_have_found_nothing():
    assert classify(None, {"company_id": "c9"}) == "recovered"
    assert classify(None, None) == "still_none"


# --- payload rebuild ----------------------------------------------------------------

def test_payload_is_rebuilt_from_the_task_not_from_lead_company():
    # The pipeline sends the task-parsed name. Rebuilding from Lead.Company would ask LF
    # a different question than production asks.
    sf = FakeSf(tasks=[_task()], leads=[_lead(company="Acme Holding AG (do not send)")])
    cases, counts = build_cases(sf, SETTINGS, [_prediction()], profiles={})
    assert [c.payload["company_name"] for c in cases] == ["Acme GmbH"]
    assert counts["replayed"] == 1


def test_a_lead_whose_records_are_gone_is_dropped_not_guessed():
    sf = FakeSf(tasks=[], leads=[])
    cases, counts = build_cases(sf, SETTINGS, [_prediction()], profiles={})
    assert cases == []
    assert counts["dropped_record_gone"] == 1
    assert counts.get("replayed", 0) == 0


def test_both_salesforce_reads_are_batched():
    sf = FakeSf(tasks=[_task(), _task("T2", "00Q2", company="Beta GmbH")],
                leads=[_lead(), _lead("00Q2", "Beta GmbH")])
    build_cases(sf, SETTINGS, [_prediction(), _prediction("00Q2", "T2")], profiles={})
    assert len(sf.queries) == 2          # one Task read, one Lead read — never per-lead
    assert sum(1 for q in sf.queries if " FROM Task " in q) == 1


# --- the run ------------------------------------------------------------------------

def test_run_asks_control_at_three_and_only_re_asks_the_unmatched(tmp_path):
    sf = FakeSf(tasks=[_task(), _task("T2", "00Q2", company="Beta GmbH")],
                leads=[_lead(), _lead("00Q2", "Beta GmbH")])
    lf = FakeLf(by_name={"Beta GmbH": {"company_id": "c1", "matched_name": "Beta GmbH"}},
                wide_by_name={"Acme GmbH": {"company_id": "c9", "matched_name": "Acme GmbH",
                                            "match_score": 0.4, "name_agreement": 100,
                                            "agreed_via": "company_name"}})
    res = run_backtest(sf, lf, SETTINGS, xlsx_path=tmp_path / "r.xlsx",
                       predictions_path=_write_preds(tmp_path), profiles={})
    assert lf.widths == [MATCH_RESULTS_PRIMARY, 20]
    assert lf.asked[1] == ["Acme GmbH"]          # the matched lead is not re-asked
    assert res["tally"] == {"drifted": 1, "recovered": 1}
    assert (tmp_path / "r.xlsx").exists()


def test_run_reports_the_credits_the_client_counted(tmp_path):
    sf = FakeSf(tasks=[_task()], leads=[_lead()])
    lf = FakeLf()
    lf.credits_charged = 0
    res = run_backtest(sf, lf, SETTINGS, xlsx_path=tmp_path / "r.xlsx",
                       predictions_path=_write_preds(tmp_path), profiles={})
    assert res["credits"] == 0                   # match is free; this is the proof, not a claim
    assert res["tally"] == {"still_none": 1}


def test_run_with_nothing_replayable_writes_no_report(tmp_path):
    path = tmp_path / "empty.jsonl"
    path.write_text("", encoding="utf-8")
    res = run_backtest(FakeSf(), FakeLf(), SETTINGS, xlsx_path=tmp_path / "r.xlsx",
                       predictions_path=path, profiles={})
    assert res["xlsx"] is None
    assert not (tmp_path / "r.xlsx").exists()


def _write_preds(tmp_path):
    """Two no_lf_match predictions on disk, matching the FakeSf task/lead pair."""
    import json
    from dataclasses import asdict
    path = tmp_path / "predictions.jsonl"
    path.write_text(
        "".join(json.dumps(asdict(pr)) + "\n"
                for pr in (_prediction(), _prediction("00Q2", "T2"))),
        encoding="utf-8")
    return path
