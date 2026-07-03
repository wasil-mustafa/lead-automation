"""LeadFeeder enrichment orchestration (Module 2).

Mirrors the legacy pipeline's phases but takes `ExtractedLead`s instead of Excel rows:
  1. resolve the company + contact lists,
  2. match each lead's company (free), adding hits to the company list,
  3. batch-retrieve enriched firmographics for all matched companies (1 credit each),
  4. run the contact cascade for matched leads that carry a name (1 credit each).

Per-record HTTP errors are isolated so one bad lead never sinks the batch.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import requests

from ..extract.models import ExtractedLead
from ..kununu_feed import base_slug, load_profiles_for_leads
from ..logging import get_logger
from ..textnorm import normalize_name
from .client import flatten_company, flatten_contact_details


@dataclass
class EnrichedLead:
    """An ExtractedLead plus whatever LeadFeeder returned. Feeds Module 3 (match/write-back)."""
    extracted: ExtractedLead
    matched: bool = False
    lf_company_id: str | None = None
    matched_name: str | None = None
    match_score: float | None = None
    name_agreement: int | None = None  # gate's 0-100 name/domain agreement (see client._best_match)
    agreed_via: str | None = None  # which signal vouched: company_name / alt_name:<n> / email_domain
    alt_names: list[str] = field(default_factory=list)  # assigned-company names from the kununu feed
    company: dict = field(default_factory=dict)   # flatten_company output (or {})
    contact_found: bool = False
    contact: dict = field(default_factory=dict)   # flattened details + contact_id/full_name


def _contact_name(e: ExtractedLead) -> str | None:
    if e.contact_name and e.contact_name.strip():
        return e.contact_name.strip()
    full = " ".join(p for p in (e.lead_first_name, e.lead_last_name) if p).strip()
    return full or None


def enrich_leads(client, extracteds: list[ExtractedLead], settings, logger=None,
                 profiles=None, use_kununu_feed: bool = True) -> list[EnrichedLead]:
    log = logger or get_logger("leadfeeder.enrich")
    if not extracteds:
        return []

    if profiles is None and use_kununu_feed and settings.kununu_feed_enabled:
        profiles = load_profiles_for_leads(extracteds, settings, logger=log)
    profiles = profiles or {}

    company_list_id = client.find_or_create_list(settings.lf_company_list, "company")
    contact_list_id = client.find_or_create_list(settings.lf_contact_list, "contact")

    # Phase 1 — company match (free, batched).
    results: list[EnrichedLead] = []
    match_payloads: list[dict] = []
    match_index: list[int] = []  # results-index for each payload
    for e in extracteds:
        result = EnrichedLead(extracted=e)
        company_name = e.best_company
        if company_name:
            email = e.contact_email or e.lead_email
            # No country/country_code: LF's country filter suppresses good matches
            # (2026-07-03 control probe: 59 candidates regained without it, all
            # correct). The name-agreement gate is what guards wrong-company.
            payload = {"company_name": company_name, "email": email or ""}
            info = (profiles.get(e.kununu_profile_uuid or "")
                    or profiles.get(base_slug(e.kununu_url) or ""))
            if info:
                own = normalize_name(company_name)
                alts = []
                for n in info.companies:
                    nn = normalize_name(n)
                    if nn and nn != own and n not in alts:
                        alts.append(n)
                if alts:
                    payload["alt_names"] = alts
                    result.alt_names = alts
            match_payloads.append(payload)
            match_index.append(len(results))
        results.append(result)

    matches = client.match_companies(match_payloads) if match_payloads else []
    for payload_i, match in enumerate(matches):
        if not match:
            continue
        r = results[match_index[payload_i]]
        r.matched = True
        r.lf_company_id = match["company_id"]
        r.matched_name = match.get("matched_name")
        r.match_score = match.get("match_score")
        r.name_agreement = match.get("name_agreement")
        r.agreed_via = match.get("agreed_via")
        try:
            client.add_company_to_list(r.lf_company_id, company_list_id)
        except requests.exceptions.RequestException as err:
            log.warning("add_company_to_list failed for %s: %s", r.lf_company_id, err)

    matched = [r for r in results if r.matched]

    # Phase 2 — batch-retrieve firmographics for all matched companies (1 credit each).
    if matched:
        enriched = client.retrieve_companies([r.lf_company_id for r in matched])
        for r in matched:
            r.company = flatten_company(enriched.get(r.lf_company_id, {}))

    # Phase 3 — contact search cascade (free), then ONE batched detail retrieve (1 credit each).
    search_hits: list[tuple[EnrichedLead, dict]] = []
    for r in matched:
        name = _contact_name(r.extracted)
        if not name:
            continue
        try:
            cmatch = client.search_contact(name, r.lf_company_id)
        except requests.exceptions.RequestException as err:
            log.warning("Contact search failed for %r: %s", name, err)
            continue
        if cmatch:
            search_hits.append((r, cmatch))

    details_by_id = (client.retrieve_contacts([c["contact_id"] for _, c in search_hits])
                     if search_hits else {})

    for r, cmatch in search_hits:
        cid = cmatch["contact_id"]
        details = flatten_contact_details(details_by_id.get(cid, {}))
        r.contact_found = True
        r.contact = {**details, "contact_id": cid, "full_name": cmatch.get("full_name", "")}
        try:
            client.add_contact_to_list(cid, contact_list_id)
        except requests.exceptions.RequestException as err:
            log.warning("add_contact_to_list failed for %s: %s", cid, err)

    log.info("LeadFeeder: %d/%d matched, %d contact(s) found",
             len(matched), len(results), sum(1 for r in results if r.contact_found))
    return results
