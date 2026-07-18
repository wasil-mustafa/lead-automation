"""Live-scope gate tests (phased go-live, 2026-07-18).

Pure unit tests of live_scope_reason: the function only reads result.decision,
result.enriched.kununu_score and the two settings knobs, so SimpleNamespace stands in
for MatchResult and Settings.
"""
from types import SimpleNamespace

from leadauto.live_scope import live_scope_reason


def _settings(decisions=("create_new",), max_score=3.0):
    return SimpleNamespace(live_decisions=list(decisions),
                           live_max_kununu_score=max_score)


def _result(decision="create_new", score=2.5):
    return SimpleNamespace(decision=decision,
                           enriched=SimpleNamespace(kununu_score=score))


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
