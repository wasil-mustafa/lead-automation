"""LeadFeeder enrichment orchestration (Module 2).

Mirrors the legacy pipeline's phases but takes `ExtractedLead`s instead of Excel rows:
  1. resolve the company + contact lists,
  2. match each lead's company (free), adding hits to the company list,
  3. batch-retrieve enriched firmographics for all matched companies (1 credit each),
  4. run the contact cascade for matched leads that carry a name (1 credit each).

Per-record HTTP errors are isolated so one bad lead never sinks the batch.
"""
from __future__ import annotations

import requests

from ..models import (CONTACT_SKIP_NO_NAME, CONTACT_SKIP_NOT_NEEDED,
                      CONTACT_SKIP_SEARCH_FAILED, EnrichedLead, ExtractedLead)
from ..kununu_feed import base_slug, load_profiles_for_leads
from ..logging import get_logger
from ..textnorm import (FREEMAIL_DOMAINS, abbreviate_legal_form, country_iso2,
                        email_domain, is_blank, normalize_host, normalize_name)
from .client import (MATCH_RESULTS_PRIMARY, flatten_company, flatten_contact_details,
                     match_email)

# `EnrichedLead.match_pass` of a lead the domain-rescue pass matched (see
# `build_rescue_payload`). Not "primary", so `live_scope.is_wide_match` treats it like
# the wide pass: admitted to live scope only under `allow_wide_match`, flagged on the QA
# sheet as `yes_domain_match`.
MATCH_PASS_DOMAIN = "domain"
COUNTRY_CONFLICT = "country_conflict"


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


def lead_country(e: ExtractedLead, feed_cc: str | None = None) -> str | None:
    """The lead's ISO2 country as the sanity gate judges it, or None.

    kununu-derived countries first (URL slug, then the feed's profile_country_code):
    they are profile evidence, while the Lead's CountryCode/Country is often a
    defaulted 'DE'/'Germany' (2026-07-10 closed-task dry run: 37 correct AT/CH
    matches were rejected against that default).
    """
    return next((cc for cc in (country_iso2(e.country_code),
                               country_iso2(feed_cc),
                               country_iso2(e.lead_country_code),
                               country_iso2(e.lead_country)) if cc), None)


def _match_sanity_reason(e: ExtractedLead, company: dict,
                         feed_cc: str | None = None) -> str | None:
    """Post-retrieve sanity gate on an accepted LF match; a reason string rejects it.

    Live miss 2026-07-07 (00QMz00000mBy5FMAS): lead "SNG" (sng.ch, CH) matched a
    company named "Sng" in France, staging a stranger's VAT/address for write-back.
    The match response (company_summary) carried no country or domain when this was
    written, so it runs after the phase-2 company retrieve — and stays there, because
    the retrieve is the record the write-back is built from. (The summary does carry
    both today, observed 2026-11-05; the domain-rescue pass uses that to corroborate a
    candidate before paying for its retrieve.) Both checks fail open on missing data.

    - country_conflict: the retrieved company's country contradicts the lead's.
    - acronym_uncorroborated: acronym-like names make name agreement tautological
      (LF searched on the acronym we sent), so the match must be corroborated by
      the lead's email domain matching the company's website/email domain.
    """
    lead_cc = lead_country(e, feed_cc)
    comp_cc = country_iso2(company.get("Country"))
    if lead_cc and comp_cc and lead_cc != comp_cc:
        return f"country_conflict:{comp_cc}!={lead_cc}"

    if _is_acronymish(e.best_company, normalize_name(e.best_company)):
        lead_dom = email_domain(e.contact_email or e.lead_email)
        comp_doms = (normalize_host(company.get("Website")),
                     email_domain(company.get("Company_Email")))
        if any(_domains_agree(lead_dom, d) for d in comp_doms):
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

    The name goes out with its legal form ABBREVIATED (`textnorm.abbreviate_legal_form`,
    2026-11-06): LF returns zero candidates for "PAU-Söhne Gesellschaft mit beschränkter
    Haftung" and the right company for "PAU-Söhne GmbH". The local name gate normalizes
    both spellings to the same tokens, so this changes what LF is asked, not what the
    gate accepts.
    """
    company_name = abbreviate_legal_form(e.best_company)
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


def lead_corporate_domain(e: ExtractedLead) -> str | None:
    """The lead's email domain when it can identify a company: lowercase, not freemail.
    None for a freemail/ISP address or no address at all."""
    dom = email_domain(e.contact_email or e.lead_email)
    if not dom or dom in FREEMAIL_DOMAINS:
        return None
    return dom


def build_rescue_payload(e: ExtractedLead, payload: dict | None) -> dict | None:
    """The domain-rescue `/companies/match` payload for a lead whose primary match the
    sanity gate rejected as `country_conflict`, or None when the lead cannot be rescued.

    Why it exists (2026-11-05, lead 00QMz00000rhynBMAQ "Baustein GmbH", email
    kundenservice@baustein.com, kununu /de/): `company_name` + `email` returned ONLY
    LF's Austrian "Baustein GmbH" (baustein-gmbh.at), which the gate rightly rejected,
    while LF also holds a German "Baustein GmbH" whose website IS baustein.com. LF's
    name+email match does not weigh the email domain against a company's own website.
    Since 2026-10-19, 17 leads (50 all-time) lost a real match this way, most with a
    corporate email domain.

    The payload is `company_name` + `url=<the lead's email domain>` (+ the local-only
    `alt_names`): measured live the same day, that shape returned exactly the baustein.com
    company at 0.88. `email` is dropped — it is what pulled the wrong country in. And
    NOT `country_code`: with it LF ranked two other German "Baustein GmbH"s above the
    right one (0.90/0.89 vs 0.80), so `_best_match` would have picked a stranger, which
    is the `CLAUDE.md` warning about country filters measured again.

    This touches only leads the pipeline has ALREADY lost, never the primary pass — the
    same asymmetry the wide pass rests on (`MATCH_RESULTS_PRIMARY`). Freemail leads
    cannot be rescued: their domain identifies nobody.
    """
    if not payload:
        return None
    dom = lead_corporate_domain(e)
    if not dom:
        return None
    out = {"company_name": payload["company_name"], "url": dom}
    if payload.get("alt_names"):
        out["alt_names"] = list(payload["alt_names"])
    return out


def rescue_accepts(match: dict | None, lead_domain: str | None) -> bool:
    """A domain-rescue candidate counts only when LF's own website for it is the lead's
    email domain (subdomain-tolerant). The name gate in `_best_match` already passed; this
    is the second axis, and it is what makes a rescued match a STRONGER claim than the
    primary name match, not a weaker one. A summary without a website fails closed."""
    if not match or not lead_domain:
        return False
    return _domains_agree(normalize_host(match.get("matched_url")), lead_domain)


def _contact_name(e: ExtractedLead) -> str | None:
    if e.contact_name and e.contact_name.strip():
        return e.contact_name.strip()
    full = " ".join(p for p in (e.lead_first_name, e.lead_last_name) if p).strip()
    return full or None


def lead_needs_email(e: ExtractedLead) -> bool:
    """No email will reach Lead.Email from what we already hold (Task email or Lead)."""
    return is_blank(e.contact_email) and is_blank(e.lead_email)


def lead_needs_phone(e: ExtractedLead) -> bool:
    """Lead.Phone is blank — the only case the write-back fills it (fill-only-blank)."""
    return is_blank(e.lead_phone)


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

    def _accept_result(r: EnrichedLead, match: dict, pass_name: str) -> None:
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

    def _accept(payload_i: int, match: dict, pass_name: str) -> None:
        _accept_result(results[match_index[payload_i]], match, pass_name)

    def _retrieve_and_gate(rs: list[EnrichedLead]) -> list[EnrichedLead]:
        """Phase 2 for `rs`: ONE batched retrieve (1 credit per new company), then the
        sanity gate. A rejected match degrades to unmatched (-> terminal skip) so a
        wrong company's firmographics are never staged for write-back."""
        enriched = client.retrieve_companies([r.lf_company_id for r in rs])
        for r in rs:
            r.company = flatten_company(enriched.get(r.lf_company_id, {}))
            reason = _match_sanity_reason(r.extracted, r.company, r.feed_country_code)
            if reason:
                log.warning("LF match rejected for %r (lf %s, matched %r, pass %s): %s",
                            r.extracted.best_company, r.lf_company_id,
                            r.matched_name, r.match_pass, reason)
                r.lf_rejected = reason
                r.matched = False
                r.company = {}
        return [r for r in rs if r.matched]

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
    # then the sanity gate (see `_retrieve_and_gate`).
    if matched:
        matched = _retrieve_and_gate(matched)

    # Phase 2b — domain rescue (2026-11-05, `build_rescue_payload`): a lead whose
    # primary match the gate threw out as `country_conflict` is re-asked ONCE with its
    # corporate email domain as `url`; the candidate counts only if the name gate passes
    # AND LF's website for it is that domain (`rescue_accepts`, judged on the match
    # summary, BEFORE the retrieve). A rescued company then goes through the SAME
    # retrieve + sanity gate as a primary one, so a `.com` domain registered abroad is
    # rejected again rather than trusted. Cost: one free match call per
    # `match_batch` candidates, 1 credit per rescued company at the retrieve.
    # `leadfeeder.match_domain_rescue` turns it on; the acronym rejection is never
    # rescued (its corroboration already failed on this very domain).
    #
    # Since 2026-11-06 the SAME pass also re-asks a lead whose primary (and wide) match
    # returned NO candidate at all — `no_lf_match`, the pipeline's largest terminal
    # loss (201 of 883 latest predictions). Found on 00QMz00000rhwTdMAI "PAU-Söhne
    # Gesellschaft mit beschränkter Haftung": name+email returned nothing, name+url
    # returned the company whose website IS pau-maschinenbau.de. Replayed over all 201
    # the same day: 13 of 117 still-unmatched corporate-domain leads recovered, 4 URL
    # mismatches and 1 foreign hit refused, 0 credits (`domain-rescue-backtest`, arm
    # `unmatched`). Such a recovery is `lf_rescue = recovered_unmatched` so the two
    # populations can be scored apart. A lead whose primary CHUNK failed was never
    # judged and is not re-asked (same rule as the wide pass).
    if settings.lf_match_domain_rescue:
        payload_of = {match_index[i]: p for i, p in enumerate(match_payloads)}
        unmatched_idx = {match_index[i] for i, m in enumerate(matches) if m is None}
        rescue: list[tuple[EnrichedLead, dict, str, bool]] = []
        for idx, r in enumerate(results):
            conflict = (r.lf_rejected or "").startswith(COUNTRY_CONFLICT)
            no_candidate = idx in unmatched_idx and not r.matched and not r.lf_rejected
            if not (conflict or no_candidate):
                continue
            rp = build_rescue_payload(r.extracted, payload_of.get(idx))
            if rp is None:
                r.lf_rescue = "no_corporate_domain"
                continue
            rescue.append((r, rp, rp["url"], no_candidate))
        if rescue:
            log.info("Domain rescue: re-asking %d lead(s) with their email domain as url "
                     "(%d country-conflict, %d without any candidate)", len(rescue),
                     sum(1 for *_, nc in rescue if not nc),
                     sum(1 for *_, nc in rescue if nc))
            rescue_matches = client.match_companies([rp for _, rp, _, _ in rescue],
                                                    max_results=MATCH_RESULTS_PRIMARY)
            rescued: list[EnrichedLead] = []
            for (r, rp, dom, no_candidate), match in zip(rescue, rescue_matches):
                # Website agrees but the summary already says the company sits in
                # another country (a .com registered abroad, a foreign subsidiary on
                # the group domain): the sanity gate would reject it after the
                # retrieve, so refuse it before paying. Backtest 2026-11-05: 7 of 22
                # domain hits, i.e. 7 credits per replay saved by this branch.
                foreign = country_iso2((match or {}).get("matched_country_code"))
                lead_cc = lead_country(r.extracted, r.feed_country_code)
                if rescue_accepts(match, dom) and foreign and lead_cc and foreign != lead_cc:
                    r.lf_rescue = f"foreign:{foreign}!={lead_cc}"
                elif rescue_accepts(match, dom):
                    log.info("Domain rescue recovered %r: %s -> %r (lf %s, %s, score %s)",
                             rp["company_name"], r.lf_rejected or "no candidate",
                             match.get("matched_name"), match.get("company_id"),
                             match.get("matched_url"), match.get("match_score"))
                    r.lf_rejected = None
                    r.lf_rescue = "recovered_unmatched" if no_candidate else "recovered"
                    _accept_result(r, match, MATCH_PASS_DOMAIN)
                    rescued.append(r)
                elif match:
                    r.lf_rescue = ("url_mismatch:"
                                   f"{normalize_host(match.get('matched_url')) or '-'}")
                else:
                    r.lf_rescue = "no_match"
            if rescued:
                matched.extend(_retrieve_and_gate(rescued))
                for r in rescued:
                    if not r.matched:
                        r.lf_rescue = f"rejected_again:{r.lf_rejected}"
            log.info("Domain rescue recovered %d of %d candidate(s)",
                     sum(1 for r in rescued if r.matched), len(rescue))

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
