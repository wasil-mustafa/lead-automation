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
from ..textnorm import alnum, country_iso2, email_domain, normalize_host, normalize_name
from .client import flatten_company, flatten_contact_details


@dataclass
class EnrichedLead:
    """An ExtractedLead plus whatever LeadFeeder returned. Feeds Module 3 (match/write-back)."""
    extracted: ExtractedLead
    matched: bool = False
    lf_rejected: str | None = None  # post-retrieve sanity gate's rejection reason, or None
    lf_company_id: str | None = None
    matched_name: str | None = None
    match_score: float | None = None
    name_agreement: int | None = None  # gate's 0-100 name/domain agreement (see client._best_match)
    agreed_via: str | None = None  # which signal vouched: company_name / alt_name:<n> / email_domain
    alt_names: list[str] = field(default_factory=list)  # assigned-company names from the kununu feed
    company: dict = field(default_factory=dict)   # flatten_company output (or {})
    contact_found: bool = False
    contact: dict = field(default_factory=dict)   # flattened details + contact_id/full_name


def _domains_agree(a: str | None, b: str | None) -> bool:
    """True when two hosts/domains identify the same site (subdomain-tolerant)."""
    if not a or not b:
        return False
    return a == b or a.endswith("." + b) or b.endswith("." + a)


def _is_acronymish(raw_name: str | None, norm_name: str | None) -> bool:
    """Single-token names too short to identify a company: ≤3 chars always ('SNG'),
    4 chars only when written as an uppercase acronym ('ADAC' yes, 'Acme' no)."""
    if not norm_name or " " in norm_name:
        return False
    if len(norm_name) <= 3:
        return True
    return (len(norm_name) == 4
            and any(t.isupper() and len(t) == 4 for t in (raw_name or "").split()))


def _match_sanity_reason(e: ExtractedLead, company: dict) -> str | None:
    """Post-retrieve sanity gate on an accepted LF match; a reason string rejects it.

    Live miss 2026-07-07 (00QMz00000mBy5FMAS): lead "SNG" (sng.ch, CH) matched a
    company named "Sng" in France, staging a stranger's VAT/address for write-back.
    The match response (company_summary) carries no country or domain, so this must
    run after the phase-2 company retrieve. Both checks fail open on missing data.

    - country_conflict: the retrieved company's country contradicts the lead's.
    - acronym_uncorroborated: acronym-like names make name agreement tautological
      (LF searched on the acronym we sent), so the match must be corroborated by
      the lead's email domain matching the company's website/email domain, or by
      VAT equality against the task-extracted VAT.
    """
    lead_cc = next((cc for cc in (country_iso2(e.lead_country_code),
                                  country_iso2(e.country_code),
                                  country_iso2(e.lead_country)) if cc), None)
    comp_cc = country_iso2(company.get("Country"))
    if lead_cc and comp_cc and lead_cc != comp_cc:
        return f"country_conflict:{comp_cc}!={lead_cc}"

    if _is_acronymish(e.best_company, normalize_name(e.best_company)):
        lead_dom = email_domain(e.contact_email or e.lead_email)
        comp_doms = (normalize_host(company.get("Website")),
                     email_domain(company.get("Company_Email")))
        if any(_domains_agree(lead_dom, d) for d in comp_doms):
            return None
        lead_vat, comp_vat = alnum(e.vat_number), alnum(company.get("VAT_Id"))
        if lead_vat and lead_vat == comp_vat:
            return None
        return "acronym_uncorroborated"
    return None


def _contact_name(e: ExtractedLead) -> str | None:
    if e.contact_name and e.contact_name.strip():
        return e.contact_name.strip()
    full = " ".join(p for p in (e.lead_first_name, e.lead_last_name) if p).strip()
    return full or None


def enrich_leads(client, extracteds: list[ExtractedLead], settings, logger=None,
                 profiles=None, use_kununu_feed: bool = True,
                 include_contacts: bool = True) -> list[EnrichedLead]:
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

    # Phase 2 — batch-retrieve firmographics for all matched companies (1 credit each),
    # then the sanity gate: a rejected match degrades to unmatched (-> terminal skip)
    # so a wrong company's firmographics are never staged for write-back.
    if matched:
        enriched = client.retrieve_companies([r.lf_company_id for r in matched])
        for r in matched:
            r.company = flatten_company(enriched.get(r.lf_company_id, {}))
            reason = _match_sanity_reason(r.extracted, r.company)
            if reason:
                log.warning("LF match rejected for %r (lf %s, matched %r): %s",
                            r.extracted.best_company, r.lf_company_id,
                            r.matched_name, reason)
                r.lf_rejected = reason
                r.matched = False
                r.company = {}
        matched = [r for r in matched if r.matched]

    # Phase 3 — contact search cascade (free), then ONE batched detail retrieve (1 credit each).
    # `include_contacts=False` skips the whole phase (backtests that only need the
    # company side of enrichment shouldn't spend contact credits).
    search_hits: list[tuple[EnrichedLead, dict]] = []
    for r in matched if include_contacts else []:
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
