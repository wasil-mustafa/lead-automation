"""Live-scope gate tests (phased go-live, 2026-07-18).

Pure unit tests of live_scope_reason: the function only reads result.decision,
result.enriched.kununu_score and the two settings knobs, so SimpleNamespace stands in
for MatchResult and Settings.
"""
from types import SimpleNamespace

from leadauto.live_scope import campaign_gate_reason, live_scope_reason, writeback_reason


# --- campaign gate (Wasil, 2026-10-31) ----------------------------------------------

def _gate(buckets):
    return SimpleNamespace(live_convert_buckets=buckets)


def test_campaign_gate_absent_key_passes_everything():
    assert campaign_gate_reason("matched_incomplete", _gate(None)) is None
    assert campaign_gate_reason(None, _gate(None)) is None


def test_campaign_gate_passes_only_listed_buckets():
    gate = _gate(["matched_complete"])
    assert campaign_gate_reason("matched_complete", gate) is None
    assert campaign_gate_reason("matched_incomplete", gate) == "campaign_matched_incomplete"
    assert campaign_gate_reason("unmatched", gate) == "campaign_unmatched"


def test_campaign_gate_untagged_lead_fails_closed():
    # No bucket = not tagged (tagging off, LF disabled, write-back or insert failed).
    assert campaign_gate_reason(None, _gate(["matched_complete"])) == "campaign_none"
    assert campaign_gate_reason("", _gate(["matched_complete"])) == "campaign_none"


def _settings(decisions=("create_new",), max_score=3.0, allow_wide=False,
              writeback_decisions=None, task_types=None):
    # writeback_decisions=None mirrors Settings: an absent key falls back to decisions.
    wb = decisions if writeback_decisions is None else writeback_decisions
    return SimpleNamespace(live_decisions=list(decisions),
                           live_writeback_decisions=list(wb),
                           live_task_types=None if task_types is None else list(task_types),
                           live_max_kununu_score=max_score,
                           live_allow_wide_match=allow_wide)


def _result(decision="create_new", score=2.5, match_pass="primary", task_type="claimer"):
    return SimpleNamespace(decision=decision,
                           enriched=SimpleNamespace(
                               kununu_score=score, match_pass=match_pass,
                               extracted=SimpleNamespace(task_type=task_type)))


def test_in_scope_returns_none():
    assert live_scope_reason(_result(), _settings()) is None


def test_decision_outside_scope_names_the_decision():
    for d in ("link", "review", "skip"):
        assert live_scope_reason(_result(decision=d), _settings()) == f"decision_{d}"


def test_decision_check_runs_before_score():
    # An out-of-scope decision wins even when the score would also fail — the reason
    # names the primary exclusion, not the score.
    assert live_scope_reason(_result(decision="skip", score=None),
                             _settings()) == "decision_skip"


def test_missing_score_fails_closed():
    # Profile not in the feed / no reviews yet => the lead stays with humans.
    assert live_scope_reason(_result(score=None), _settings()) == "score_missing"


def test_score_at_cap_is_excluded():
    # Strict <: the 3.0 boundary case stays with humans (the human "No Demand" line).
    assert live_scope_reason(_result(score=3.0), _settings()) == "score_3"


def test_score_over_cap_is_excluded_with_value_in_reason():
    assert live_scope_reason(_result(score=4.2), _settings()) == "score_4.2"


def test_score_just_under_cap_passes():
    assert live_scope_reason(_result(score=2.99), _settings()) is None


def test_widened_decisions_admit_link():
    settings = _settings(decisions=("create_new", "link"))
    assert live_scope_reason(_result(decision="link"), settings) is None
    assert live_scope_reason(_result(decision="review"), settings) == "decision_review"


# --- second-chance ("wide") LF matches ---------------------------------------------
# A wide match is the candidate LF ranked 4th-20th. Everything downstream trusts an
# accepted match completely (firmographics onto the Lead, a new Account on conversion),
# so by default it predicts and shadow-scores but never writes.

def test_wide_match_is_out_of_live_scope_by_default():
    assert live_scope_reason(_result(match_pass="wide"),
                             _settings()) == "match_pass_wide"


def test_wide_holdout_runs_before_the_decision_and_score_checks():
    # The reason must name the pass, not the decision or the score, or the QA artifact
    # would attribute the exclusion to the wrong cause.
    assert live_scope_reason(_result(decision="skip", score=None, match_pass="wide"),
                             _settings()) == "match_pass_wide"


def test_wide_match_admitted_once_the_flag_is_set():
    settings = _settings(allow_wide=True)
    assert live_scope_reason(_result(match_pass="wide"), settings) is None
    # The other gates still apply to it.
    assert live_scope_reason(_result(match_pass="wide", score=4.0),
                             settings) == "score_4"


def test_primary_and_missing_match_pass_are_unaffected():
    # "" is what a lead that never matched carries; it is excluded by decision_skip
    # downstream, never by the wide hold-out.
    assert live_scope_reason(_result(match_pass="primary"), _settings()) is None
    assert live_scope_reason(_result(match_pass=""), _settings()) is None
    assert live_scope_reason(_result(match_pass=None), _settings()) is None


def test_null_cap_switches_the_score_axis_off():
    # max_kununu_score: null (Settings -> None) — the 2026-09 ZPE event run: decision
    # alone decides, a missing or high score no longer excludes.
    s = _settings(decisions=("create_new", "link"), max_score=None)
    assert live_scope_reason(_result(score=None), s) is None
    assert live_scope_reason(_result(decision="link", score=4.7), s) is None
    # ...but the decision gate and the wide-match hold are untouched.
    assert live_scope_reason(_result(decision="review", score=None), s) == "decision_review"
    assert live_scope_reason(_result(score=None, match_pass="wide"), s) == "match_pass_wide"


# --- write-back scope vs conversion scope (2026-10-19) ------------------------------
# Enrich-only go-live: claimer leads of every LF-matched decision are written back,
# nothing is converted, ebp_trial stays dry.

def _enrich_only():
    return _settings(decisions=(), max_score=None, allow_wide=True,
                     writeback_decisions=("create_new", "link", "review"),
                     task_types=("claimer",))


def test_enrich_only_writes_back_every_matched_claimer_decision():
    s = _enrich_only()
    for d in ("create_new", "link", "review"):
        assert writeback_reason(_result(decision=d), s) is None
        # ...and converts none of them.
        assert live_scope_reason(_result(decision=d), s) == f"decision_{d}"


def test_skip_is_never_written_back():
    assert writeback_reason(_result(decision="skip"), _enrich_only()) == "decision_skip"


def test_unlisted_task_type_stays_dry():
    s = _enrich_only()
    assert writeback_reason(_result(task_type="ebp_trial"), s) == "task_type_ebp_trial"
    assert live_scope_reason(_result(task_type="ebp_trial"), s) == "task_type_ebp_trial"


def test_absent_task_types_means_every_type():
    assert writeback_reason(_result(task_type="ebp_trial"), _settings()) is None


def test_absent_writeback_decisions_falls_back_to_conversion_scope():
    # The go-live behaviour: write-back scope == conversion scope.
    s = _settings()
    assert writeback_reason(_result(decision="create_new"), s) is None
    assert writeback_reason(_result(decision="link"), s) == "decision_link"


def test_conversion_still_requires_the_write_back_gate():
    # A decision in the conversion scope but outside the write-back scope never converts.
    s = _settings(decisions=("create_new",), writeback_decisions=("link",))
    assert live_scope_reason(_result(decision="create_new"), s) == "decision_create_new"
