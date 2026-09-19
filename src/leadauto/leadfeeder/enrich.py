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
from .client import (MATCH_RESULTS_PRIMARY, flatten_company, flatten_contact_details,
                     match_email)


# Why a matched lead has no LF contact although LF was never asked for one. The QA
# `NoContact` sheet shows these verbatim; "not found in LeadFeeder" is reserved for the
# case where the search actually ran and held no such person.
CONTACT_SKIP_NOT_NEEDED = "lead already has email and phone"     # config: contact_lookup_only_when_needed
CONTACT_SKIP_NO_NAME = "no contact name to search"
CONTACT_SKIP_SEARCH_FAILED = "LeadFeeder contact search failed (transport error)"


@dataclass
class EnrichedLead:
    """An ExtractedLead plus whatever LeadFeeder returned. Feeds Module 3 (match/write-back)."""
    extracted: ExtractedLead
    matched: bool = False
    lf_rejected: str | None = None  # post-retrieve sanity gate's rejection reason, or None
    lf_company_id: str | None = None
    matched_name: str | None = None
    match_score: float | None = None
    feed_country_code: str | None = None  # kununu feed profile_country_code (sanity-gate evidence)
    feed_industry: str | None = None      # kununu feed profile_industry_name_de (write-back source)
    kununu_score: float | None = None     # kununu feed profile_score (live-scope gate input)
    name_agreement: int | None = None  # gate's 0-100 name/domain agreement (see client._best_match)
    agreed_via: str | None = None  # which signal vouched: company_name / alt_name:<n> / email_domain
    alt_names: list[str] = field(default_factory=list)  # assigned-company names from the kununu feed
    match_pass: str = ""  # "primary" | "wide" (second-chance match); "" = never matched
    company: dict = field(default_factory=dict)   # flatten_company output (or {})
    contact_found: bool = False
    contact: dict = field(default_factory=dict)   # flattened details + contact_id/full_name
    # Why the contact cascade was NOT run for a matched lead (QA NoContact `reason`);
    # None when it ran (found or not) or the lead never matched.
    contact_skipped: str | None = None   # one of the CONTACT_SKIP_* reasons, or None when LF was asked


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


def _match_sanity_reason(e: ExtractedLead, company: dict,
                         feed_cc: str | None = None) -> str | None:
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
    # kununu-derived countries first (URL, then the feed's profile_country_code):
    # they are profile evidence, while the Lead's CountryCode/Country is often a
    # defaulted 'DE'/'Germany' (2026-07-10 closed-task dry run: 37 correct AT/CH
    # matches were rejected against that default).
    lead_cc = next((cc for cc in (country_iso2(e.country_code),
                                  country_iso2(feed_cc),
                                  country_iso2(e.lead_country_code),
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


def build_match_payload(e: ExtractedLead, profiles: dict) -> tuple[dict | None, object]:
    """The `/companies/match` payload for one lead, and the kununu ProfileInfo behind it.

    Returns `(None, info)` when the lead has no company name to send — there is nothing
    to ask LF about. `info` is None when the feed knows no profile for this lead.

    Factored out of `enrich_leads` (2026-08-25) so the second-chance-match backtest
    reproduces the PRODUCTION payload exactly. An approximated payload (say `Lead.Company`
    where the pipeline sends the task-parsed name) would measure a different question than
    the one the pipeline actually asks, and would read as a result about LF.

    Two standing constraints live here:
      - **no `country`/`country_code`**: LF's country filter SUPPRESSES good matches
        (2026-07-03 control probe: 59 candidates regained without it, all correct). The
        name-agreement gate is what guards wrong-company.
      - **`email` only when it is a real address**: LF 400s on a blank/garbage one and a
        400 fails the WHOLE chunk (`client.match_api_payload` enforces this at the
        transport boundary too — this keeps the payload honest at source).
    `alt_names` is a LOCAL gate input; `match_api_payload` strips it before the call.
    """
    company_name = e.best_company
    info = (profiles.get(e.kununu_profile_uuid or "")
            or profiles.get(base_slug(e.kununu_url) or ""))
    if not company_name:
        return None, info
    payload = {"company_name": company_name}
    email = e.contact_email or e.lead_email
    if match_email(email):
        payload["email"] = email
    if info:
        own = normalize_name(company_name)
        alts = []
        for n in info.companies:
            nn = normalize_name(n)
            if nn and nn != own and n not in alts:
                alts.append(n)
        if alts:
            payload["alt_names"] = alts
    return payload, info


def _contact_name(e: ExtractedLead) -> str | None:
    if e.contact_name and e.contact_name.strip():
        return e.contact_name.strip()
    full = " ".join(p for p in (e.lead_first_name, e.lead_last_name) if p).strip()
    return full or None


def _has(value) -> bool:
    return bool(value and str(value).strip())


def lead_needs_email(e: ExtractedLead) -> bool:
    """No email will reach Lead.Email from what we already hold (Task email or Lead)."""
    return not (_has(e.contact_email) or _has(e.lead_email))


def lead_needs_phone(e: ExtractedLead) -> bool:
    """Lead.Phone is blank — the only case the write-back fills it (fill-only-blank)."""
    return not _has(e.lead_phone)


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
        payload, info = build_match_payload(e, profiles)
        # The feed fields are applied ONLY alongside a payload, which is how this read
        # before the 2026-08-25 refactor. A lead with no company name is never matched,
        # so a `kununu_score` on it could only ever loosen the live-scope gate for a
        # record nothing else vouches for. Deliberate, not an oversight.
        if payload is not None:
            if info:
                result.feed_country_code = info.country_code
                result.feed_industry = info.industry
                result.kununu_score = info.score
            result.alt_names = list(payload.get("alt_names") or [])
            match_payloads.append(payload)
            match_index.append(len(results))
        results.append(result)

    def _accept(payload_i: int, match: dict, pass_name: str) -> None:
        r = results[match_index[payload_i]]
        r.matched = True
        r.match_pass = pass_name
        r.lf_company_id = match["company_id"]
        r.matched_name = match.get("matched_name")
        r.match_score = match.get("match_score")
        r.name_agreement = match.get("name_agreement")
        r.agreed_via = match.get("agreed_via")
        try:
            client.add_company_to_list(r.lf_company_id, company_list_id)
        except requests.exceptions.RequestException as err:
            log.warning("add_company_to_list failed for %s: %s", r.lf_company_id, err)

    matches = (client.match_companies(match_payloads, max_results=MATCH_RESULTS_PRIMARY)
               if match_payloads else [])
    for payload_i, match in enumerate(matches):
        if match:
            _accept(payload_i, match, "primary")

    # Second-chance match, rescue-only (2026-08-25). `no_lf_match` is a TERMINAL skip —
    # 101 of 523 shadow predictions (19.3%) died there — and one cause is that the primary
    # call asks for LF's top 3 candidates only, while LF holds several company records for
    # one company and ranks the wrong (emptier) one first. Re-asking the SAME payload for
    # 20 candidates lets `_best_match` gate the ones it never saw. It touches only leads
    # that matched nothing, so no working match can move: that asymmetry is the whole
    # safety argument, measured in `Contact Presence Check` (adding identifiers to the
    # PRIMARY payload regressed ~15% of working matches; rescue-only regressed 0).
    # Cost: one free call per `match_batch` unmatched leads, 0 credits. A match found here
    # still pays 1 credit at phase 2 like any other, and still faces the sanity gate.
    wide = settings.lf_match_wide_results
    # `is None` = LF judged the payload at the primary width and rejected it. A chunk that
    # FAILED (MATCH_CHUNK_FAILED, falsy but not None) was never judged: re-asking it here
    # would stamp any hit `match_pass="wide"`, hold it out of live scope and shadow-score it
    # as a wide match — polluting the accuracy measurement the flag exists to gather.
    retry_idx = [i for i, m in enumerate(matches) if m is None]
    never_asked = sum(1 for m in matches if m is not None and not m)
    if wide and never_asked:
        log.warning("Second-chance match: %d lead(s) skipped — their primary chunk failed, "
                    "so they were never judged at width %d", never_asked, MATCH_RESULTS_PRIMARY)
    if wide and retry_idx:
        log.info("Second-chance match: re-asking %d unmatched lead(s) with "
                 "max_results_per_company=%d", len(retry_idx), wide)
        wide_matches = client.match_companies([match_payloads[i] for i in retry_idx],
                                              max_results=wide)
        recovered = 0
        for payload_i, match in zip(retry_idx, wide_matches):
            if match:
                _accept(payload_i, match, "wide")
                recovered += 1
                log.info("Second-chance match recovered %r -> %r (lf %s, score %s, "
                         "agree %s via %s)",
                         match_payloads[payload_i].get("company_name"),
                         match.get("matched_name"), match.get("company_id"),
                         match.get("match_score"), match.get("name_agreement"),
                         match.get("agreed_via"))
        log.info("Second-chance match recovered %d of %d unmatched lead(s)",
                 recovered, len(retry_idx))

    matched = [r for r in results if r.matched]

    # Phase 2 — batch-retrieve firmographics for all matched companies (1 credit each),
    # then the sanity gate: a rejected match degrades to unmatched (-> terminal skip)
    # so a wrong company's firmographics are never staged for write-back.
    if matched:
        enriched = client.retrieve_companies([r.lf_company_id for r in matched])
        for r in matched:
            r.company = flatten_company(enriched.get(r.lf_company_id, {}))
            reason = _match_sanity_reason(r.extracted, r.company, r.feed_country_code)
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
    # `contact_lookup_only_when_needed` (config; Wasil, 2026-09-18) narrows the cascade
    # to leads whose Lead still lacks an email or a phone: those are the fields it feeds
    # (Email and Phone are fill-only-blank in writeback.py), so for a lead that has both
    # the search+retrieve would spend a credit on values that are never written.
    only_needed = settings.lf_contact_lookup_only_when_needed
    search_hits: list[tuple[EnrichedLead, dict]] = []
    skipped_enriched = 0
    for r in matched if include_contacts else []:
        name = _contact_name(r.extracted)
        if not name:
            r.contact_skipped = CONTACT_SKIP_NO_NAME
            continue
        if only_needed and not (lead_needs_email(r.extracted)
                                or lead_needs_phone(r.extracted)):
            r.contact_skipped = CONTACT_SKIP_NOT_NEEDED
            skipped_enriched += 1
            continue
        try:
            cmatch = client.search_contact(name, r.lf_company_id)
        except requests.exceptions.RequestException as err:
            log.warning("Contact search failed for %r: %s", name, err)
            r.contact_skipped = CONTACT_SKIP_SEARCH_FAILED
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

    # Phase 3b — contacts without a direct dial (mobile never counts) go through LF's
    # async 'Find contact details' external-provider lookup (1 credit only per contact
    # that actually gains data), then ONE re-retrieve refreshes their details.
    # Under `contact_lookup_only_when_needed` a lead that already has a phone is never
    # sent: the job exists to surface a phone the write-back could fill.
    if search_hits and settings.lf_find_contact_data:
        needy = [r for r, _ in search_hits
                 if not (r.contact.get("phone_direct") or r.contact.get("phone_office"))
                 and (not only_needed or lead_needs_phone(r.extracted))]
        if needy:
            ids = [r.contact["contact_id"] for r in needy]
            try:
                done = client.find_contact_data(
                    ids, timeout=settings.lf_find_details_timeout,
                    poll_interval=settings.lf_find_details_poll)
            except requests.exceptions.RequestException as err:
                log.warning("find_contact_data failed for %d contact(s): %s", len(ids), err)
                done = False
            if done:
                refreshed = client.retrieve_contacts(ids)
                for r in needy:
                    cid = r.contact["contact_id"]
                    if cid in refreshed:
                        r.contact = {**flatten_contact_details(refreshed[cid]),
                                     "contact_id": cid,
                                     "full_name": r.contact.get("full_name", "")}

    log.info("LeadFeeder: %d/%d matched, %d contact(s) found, %d lookup(s) skipped "
             "(lead already has email and phone)",
             len(matched), len(results), sum(1 for r in results if r.contact_found),
             skipped_enriched)
    return results
