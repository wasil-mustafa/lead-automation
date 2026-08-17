"""kununu DWH profile feed: resolve leads' profiles to assigned-company names.

A kununu *main profile* can have several legal entities ("assigned companies") under
it. Their names are additional truthful identities for a lead and widen the LF
name-agreement gate's evidence (see the 2026-07-03 spec). The profile's `profile_score`
from the same records is the live-scope gate's input. The bulk feed (~200 MB) is cached
locally and refreshed daily; each run streams it ONCE, keeping only the batch's
profiles. Every failure path returns an empty result — the pipeline degrades to
pre-feed behavior, never blocks, and the live-scope gate then fails CLOSED.

THE ENDPOINT, THE CACHE PATH AND THE PARSER ARE NOT OWNED HERE (since 2026-08-17).
They come from `common.kununu_feed` in the Main-Profile-Account repo, reached through
`mpa_bridge`. This module keeps only what is Lead-Automation policy: the ProfileInfo
projection, slug-keyed lookup, and degrade-never-block.

Why: kununu switched the feed from JSONL to CSV on 2026-08-06. The private reader that
used to live here requested a hardcoded `profiles.jsonl` that has 404'd ever since, and
because every failure path here returns {}, eleven days of runs logged one WARNING and
carried on with no alt names and no profile scores. `common.kununu_feed` had followed
the switchover the same day and sniffs CSV vs JSONL from the file CONTENT. One reader,
one endpoint, one cache — a second copy is what made a dead feed look like a quiet one.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from .logging import get_logger
from .mpa_bridge import MpaBridgeError, common_kununu_feed


@dataclass
class ProfileInfo:
    name: str
    companies: list[str] = field(default_factory=list)
    city: str | None = None
    country_code: str | None = None
    industry: str | None = None
    score: float | None = None  # kununu profile score (feed `profile_score`); None = no reviews yet


def base_slug(url: str | None) -> str | None:
    """URL base slug: last path segment with the kununu dedup suffix removed.

    Dedup suffixes attach directly to a letter (siemens411 -> siemens); numbers
    after '-' are part of the name (promedis-24 stays). Same rule as the
    Profile-Matching repo's base_slug.
    """
    if not url:
        return None
    seg = url.rstrip("/").split("/")[-1].split("?")[0].strip().lower()
    if not seg:
        return None
    return re.sub(r"(?<=[a-z])\d+$", "", seg) or None


def _company_names(record: dict) -> list[str]:
    # Field name unconfirmed until the first live feed pull (the legacy analysis
    # script only read number_of_assigned_companies) — handle the plausible shapes;
    # this function is the single place to adjust afterwards.
    raw = record.get("assigned_companies") or record.get("assigned_company_names") or []
    names: list[str] = []
    for item in raw:
        if isinstance(item, str):
            name = item
        elif isinstance(item, dict):
            name = item.get("name") or item.get("company_name")
        else:
            name = None
        if name and name.strip():
            names.append(name.strip())
    return names


def _profile_score(record: dict) -> float | None:
    """The feed's `profile_score` is stringly typed ('2.5', '1'); missing/empty/garbage
    => None (a profile without reviews has no score at all)."""
    raw = record.get("profile_score")
    if raw in (None, ""):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def lookup_profiles(feed_path: str | Path, uuids: set, slugs: set,
                    logger=None) -> dict[str, ProfileInfo]:
    """One streaming pass over the feed; returns ProfileInfo keyed by uuid AND slug.

    Slug keys can be ambiguous across a dedup group (siemens, siemens1, ...): the
    first record wins, which is fine for alt-names — the group is one employer family.

    Records come from `common.kununu_feed.iter_feed_records`, which sniffs CSV vs JSONL
    from the file content and normalises both into the same record shape — so
    `profile_score` and `assigned_companies` resolve identically on either format. It
    yields the uuid already lowercased, so UUIDs are compared lowercased on both sides
    (Salesforce and the feed disagree on casing, and an exact compare silently drops
    the match).

    It streams EVERY record rather than pre-filtering on uuid, because a lead may be
    keyed only by URL slug and the reader filters on uuid alone.
    """
    log = logger or get_logger("kununu_feed")
    uuids = {u.strip().lower() for u in uuids if u and str(u).strip()}
    slugs = {s for s in slugs if s}
    if not uuids and not slugs:
        return {}
    found: dict[str, ProfileInfo] = {}
    try:
        feed_mod = common_kununu_feed()
        for uuid, d in feed_mod.iter_feed_records(str(feed_path)):
            slug = base_slug(d.get("profile_url"))
            if uuid not in uuids and slug not in slugs:
                continue
            companies = _company_names(d)
            if not companies:
                log.debug("kununu feed: profile %s matched but carries no assigned-company "
                          "names", uuid or slug)
            info = ProfileInfo(
                name=(d.get("profile_name") or "").strip(),
                companies=companies,
                city=d.get("profile_city"),
                country_code=d.get("profile_country_code"),
                industry=d.get("profile_industry_name_de"),
                score=_profile_score(d),
            )
            if uuid:
                found.setdefault(uuid, info)
            if slug:
                found.setdefault(slug, info)
    except (OSError, MpaBridgeError) as err:
        log.warning("kununu feed unreadable (%s); profile lookup skipped", err)
        return {}
    return found


def feed_cache_path(settings, logger=None) -> Path:
    """Where the feed is cached. `kununu_feed.cache_path` in config.yaml wins when set;
    otherwise `common.kununu_feed` decides (KUNUNU_CACHE, else a filename derived from
    the feed URL, so a CSV feed can never be read out of a cache written as JSONL).

    Config no longer carries a default, because the old one (`kununu_profiles.jsonl` at
    the repo root) named a format the endpoint stopped serving AND put a ~200 MB file
    inside the OneDrive-synced checkout.
    """
    configured = settings.kununu_feed_cache_path
    if configured is not None:
        return configured
    return Path(common_kununu_feed()._kununu_cache_path())


def ensure_feed(settings, logger=None) -> Path | None:
    """Cached feed path, refreshed when older than max_age_hours. None => no feed
    available this run (missing credentials / failed download with no cache).

    The URL, the auth header and the atomic .part download all belong to
    `common.kununu_feed.download_feed`; only the staleness policy is decided here.
    """
    log = logger or get_logger("kununu_feed")
    try:
        feed_mod = common_kununu_feed()
        path = feed_cache_path(settings, logger=log)
    except MpaBridgeError as err:
        log.warning("kununu feed unavailable (%s); profile lookup skipped", err)
        return None
    max_age = settings.kununu_feed_max_age_hours * 3600
    if path.exists() and (time.time() - path.stat().st_mtime) < max_age:
        return path
    log.info("Downloading kununu profile feed -> %s", path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        feed_mod.download_feed(str(path))
    except Exception as err:  # RuntimeError (no creds), URLError, OSError
        if path.exists():
            log.warning("kununu feed download failed (%s); using stale cache %s", err, path)
            return path
        log.warning("kununu feed download failed (%s) and no cache; lookup skipped", err)
        return None
    return path


def load_profiles_for_leads(extracteds, settings, logger=None) -> dict[str, ProfileInfo]:
    """ProfileInfo for the batch, keyed by uuid and slug. {} on any unavailability."""
    log = logger or get_logger("kununu_feed")
    uuids = {e.kununu_profile_uuid for e in extracteds if e.kununu_profile_uuid}
    slugs = {base_slug(e.kununu_url) for e in extracteds if e.kununu_url}
    slugs.discard(None)
    if not uuids and not slugs:
        return {}
    feed = ensure_feed(settings, logger=log)
    if feed is None:
        return {}
    try:
        found = lookup_profiles(feed, uuids, slugs, logger=log)
    except Exception as err:  # never let feed parsing sink the pipeline
        log.warning("kununu feed: profile lookup failed (%s); skipped", err)
        return {}
    # lookup_profiles keys UUIDs lowercased (the feed and Salesforce disagree on casing);
    # alias each caller's ORIGINAL spelling back in so `profiles.get(lead.uuid)` still hits.
    for original in uuids:
        info = found.get(str(original).strip().lower())
        if info is not None:
            found.setdefault(original, info)
    log.info("kununu feed: resolved %d of %d profile key(s)",
             len({id(v) for v in found.values()}), len(uuids | slugs))
    return found
