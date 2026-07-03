"""kununu DWH profile feed: resolve leads' profiles to assigned-company names.

A kununu *main profile* can have several legal entities ("assigned companies") under
it. Their names are additional truthful identities for a lead and widen the LF
name-agreement gate's evidence (see the 2026-07-03 spec). The bulk feed (~530 MB
JSONL) is cached locally and refreshed daily; each run streams it ONCE, keeping only
the batch's profiles. Every failure path returns an empty result — the pipeline
degrades to pre-feed behavior, never blocks.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .logging import get_logger


@dataclass
class ProfileInfo:
    name: str
    companies: list[str] = field(default_factory=list)
    city: str | None = None
    country_code: str | None = None


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


def lookup_profiles(feed_path: str | Path, uuids: set, slugs: set,
                    logger=None) -> dict[str, ProfileInfo]:
    """One streaming pass over the feed; returns ProfileInfo keyed by uuid AND slug.

    Slug keys can be ambiguous across a dedup group (siemens, siemens1, ...): the
    first record wins, which is fine for alt-names — the group is one employer family.
    """
    log = logger or get_logger("kununu_feed")
    uuids = {u for u in uuids if u}
    slugs = {s for s in slugs if s}
    if not uuids and not slugs:
        return {}
    found: dict[str, ProfileInfo] = {}
    bad = 0
    try:
        with open(feed_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    bad += 1
                    continue
                uuid = d.get("profile_uuid")
                slug = base_slug(d.get("profile_url"))
                if uuid not in uuids and slug not in slugs:
                    continue
                info = ProfileInfo(
                    name=(d.get("profile_name") or "").strip(),
                    companies=_company_names(d),
                    city=d.get("profile_city"),
                    country_code=d.get("profile_country_code"),
                )
                if uuid:
                    found.setdefault(uuid, info)
                if slug:
                    found.setdefault(slug, info)
    except OSError as err:
        log.warning("kununu feed unreadable (%s); profile lookup skipped", err)
        return {}
    if bad:
        log.warning("kununu feed: skipped %d malformed line(s)", bad)
    return found
