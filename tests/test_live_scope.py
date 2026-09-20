"""Live-scope gate tests (phased go-live, 2026-07-18).

Pure unit tests of live_scope_reason: the function only reads result.decision,
result.enriched.kununu_score and the two settings knobs, so SimpleNamespace stands in
for MatchResult and Settings.
"""
from types import SimpleNamespace

from leadauto.live_scope import live_scope_reason


def _settings(decisions=("create_new",), max_score=3.0, allow_wide=False):
    return SimpleNamespace(live_decisions=list(decisions),
                           live_max_kununu_score=max_score,
                           live_allow_wide_match=allow_wide)


def _result(decision="create_new", score=2.5, match_pass="primary"):
    return SimpleNamespace(decision=decision,
                           enriched=SimpleNamespace(kununu_score=score,
                                                    match_pass=match_pass))


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
