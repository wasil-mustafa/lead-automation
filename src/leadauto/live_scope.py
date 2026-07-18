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
"""
from __future__ import annotations


def live_scope_reason(result, settings) -> str | None:
    """None => the MatchResult is in live scope; else a short exclusion reason
    (`decision_<d>` / `score_missing` / `score_<value>`) for the QA artifact."""
    if result.decision not in settings.live_decisions:
        return f"decision_{result.decision}"
    score = result.enriched.kununu_score
    if score is None:
        return "score_missing"
    if score >= settings.live_max_kununu_score:
        return f"score_{score:g}"
    return None
