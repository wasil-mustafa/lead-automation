"""Live-scope gate for the phased go-live (Wasil, 2026-07-18).

Only leads inside the live scope are written back and staged for conversion on a
non-dry run; everything else is left completely untouched in Salesforce (humans keep
working those leads) but still gets a shadow prediction recorded. The initial scope is
deliberately the no-sales-potential population: decision `create_new` AND kununu
profile score < 3.0 (the same "No Demand" triage line humans apply) — those convert
into Rohdaten-Pool-owned raw-data records.

A missing score (profile not in the feed, feed unavailable, no reviews yet) fails the
gate: fail-closed, the lead stays with humans. Tunables live in config.yaml under
`pipeline.live_scope`.

Widened 2026-09-28 (Wasil): config.yaml sets `max_kununu_score: null` (no score axis)
and `allow_wide_match: true`; an admitted wide match is flagged in the QA artifact.
"""
from __future__ import annotations


def is_wide_match(result) -> bool:
    """True when the accepted LF match came from the second-chance ("wide") pass."""
    return (result.enriched.match_pass or "primary") != "primary"


def live_scope_reason(result, settings) -> str | None:
    """None => the MatchResult is in live scope; else a short exclusion reason
    (`decision_<d>` / `score_missing` / `score_<value>` / `match_pass_wide`) for the QA
    artifact."""
    # A second-chance ("wide") LF match is a weaker claim than a primary one — it is the
    # candidate LF ranked 4th-20th — and every downstream write (firmographics onto the
    # Lead, a new Account on conversion) trusts the match completely. Prior art measured
    # rescued matches as ~6.5x more likely to be an umbrella/parent record than primary
    # ones (`Contact Presence Check`, 2026-08-23). So they predict and shadow-score but
    # do not convert until that accuracy is measured HERE. Flip with
    # `pipeline.live_scope.allow_wide_match: true`.
    if not settings.live_allow_wide_match and is_wide_match(result):
        return "match_pass_wide"
    if result.decision not in settings.live_decisions:
        return f"decision_{result.decision}"
    # `max_kununu_score: null` (Settings -> None) switches the score axis OFF for a run
    # whose population is NOT the No-Demand triage — e.g. the 2026-09 ZPE event leads,
    # where the decision alone decides. With a cap set, a missing score still fails
    # closed: that fail-closed default is what protects the scheduled runs.
    cap = settings.live_max_kununu_score
    if cap is None:
        return None
    score = result.enriched.kununu_score
    if score is None:
        return "score_missing"
    if score >= cap:
        return f"score_{score:g}"
    return None
