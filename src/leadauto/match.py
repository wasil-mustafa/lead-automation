"""Module 3a — rule-based Dublettencheck of an enriched Lead vs existing Accounts/Contacts.

Ordered rules mirror the business decision tree (spec: docs/superpowers/specs/
2026-07-03-dublettencheck-rules-and-shadow-compare-design.md); first hit wins:

  1  no_lf_match       no accepted LF company                             -> skip
  2  name_address      normalized name equal + postal code + city equal   -> link
  3  name_vat          normalized name equal + VAT equal                  -> link
  4  kununu_over_name  a profile UUID/URL Account exists but name_address/
                       name_vat pointed at a DIFFERENT account — the
                       kununu-identity account wins (link IT, not the
                       name/address duplicate)                            -> link
  5  kununu_agree      profile UUID/URL on an Account + name agreement    -> link
  6  kununu_conflict   profile UUID/URL hit, name disagrees               -> review
  7  name_corroborated normalized name equal + a weak signal on the SAME
                       account (two independent axes)                     -> link
  8  name_only         normalized name equal on one account, nothing else -> review
  9  duplicate         normalized name equal on 2+ accounts, none singled
                       out — the org already holds duplicate accounts     -> review
  10 weak_signal       only VAT/DBID/host/email-domain/fuzzy point at one -> review
  11 no_match          nothing                                            -> create_new

`skip` is terminal: no accepted LF match means the lead can't be identified, so it is
excluded from account AND contact matching and never converts (Stage 2 drops it even if
approved). `review` is reserved for cases where a candidate account IS surfaced and a
human must adjudicate.

Contact matching keeps its identifier signals (kununu user UUID / email); numeric
scores survive only as contact-side display metadata.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field

from rapidfuzz import fuzz

from .leadfeeder.enrich import EnrichedLead
from .logging import get_logger
from .sf import soql
# Normalizers live in textnorm so the LeadFeeder client can share them (no import cycle).
# Re-exported under their historical names for existing callers/tests.
from .textnorm import (  # noqa: F401
    FREEMAIL_DOMAINS as _FREEMAIL_DOMAINS,
    LEGAL_SUFFIXES as _LEGAL_SUFFIXES,
    email_domain,
    normalize_city,
    normalize_host,
    name_like_pattern,
    normalize_name,
    normalize_postal,
    normalize_url as _normalize_url,
)

_FUZZY_FLOOR = 0.90  # rapidfuzz ratio below this never counts as a name match


def _norm_vat(value) -> str:
    return re.sub(r"\s+", "", str(value or "")).upper()


def normalize_kununu_url(value: str | None) -> str | None:
    return _normalize_url(value)


# --- result records ----------------------------------------------------------

@dataclass
class AccountMatch:
    account_id: str
    score: float | None   # display metadata only; rules decide, not scores
    reason: str


@dataclass
class ContactMatch:
    contact_id: str
    score: float
    reason: str


@dataclass
class MatchResult:
    enriched: EnrichedLead
    account: AccountMatch | None
    contact: ContactMatch | None
    decision: str  # "link" | "review" | "create_new"
    rule: str = ""
    evidence: list[str] = field(default_factory=list)


# --- existing-record index ---------------------------------------------------

@dataclass
class ExistingRecords:
    accounts: list[dict] = field(default_factory=list)
    contacts: list[dict] = field(default_factory=list)
    acct_by_profile_uuid: dict[str, dict] = field(default_factory=dict)
    acct_by_kununu_url: dict[str, dict] = field(default_factory=dict)
    acct_by_vat: dict[str, dict] = field(default_factory=dict)
    acct_by_dbid: dict[str, dict] = field(default_factory=dict)
    acct_by_host: dict[str, list[dict]] = field(default_factory=dict)
    acct_by_norm_name: dict[str, list[dict]] = field(default_factory=dict)
    contact_by_user_uuid: dict[str, dict] = field(default_factory=dict)
    contact_by_email: dict[str, list[dict]] = field(default_factory=dict)
    contact_by_linkedin: dict[str, list[dict]] = field(default_factory=dict)
    contacts_by_account: dict[str, list[dict]] = field(default_factory=dict)

    @classmethod
    def from_records(cls, accounts: list[dict], contacts: list[dict]) -> "ExistingRecords":
        by_puid: dict[str, dict] = {}
        by_url: dict[str, dict] = {}
        by_vat: dict[str, dict] = {}
        by_dbid: dict[str, dict] = {}
        by_host: dict[str, list[dict]] = defaultdict(list)
        by_name: dict[str, list[dict]] = defaultdict(list)
        for a in accounts:
            puid = (a.get("KN_KununuProfileUuid__c") or "").strip()
            if puid:
                by_puid.setdefault(puid, a)
            url = normalize_kununu_url(a.get("KN_KununuLinkProfile__c"))
            if url:
                by_url.setdefault(url, a)
            vat = _norm_vat(a.get("KN_VATNumber__c"))
            if vat:
                by_vat.setdefault(vat, a)
            dbid = (a.get("KN_DBID__c") or "").strip()
            if dbid:
                by_dbid.setdefault(dbid, a)
            host = normalize_host(a.get("Website"))
            if host:
                by_host[host].append(a)
            for raw_name in (a.get("Name"), a.get("KN_DBLegalCompanyName__c")):
                norm = normalize_name(raw_name)
                if norm:
                    by_name[norm].append(a)

        by_uuid: dict[str, dict] = {}
        by_email: dict[str, list[dict]] = defaultdict(list)
        by_linkedin: dict[str, list[dict]] = defaultdict(list)
        by_account: dict[str, list[dict]] = defaultdict(list)
        for c in contacts:
            uuid = (c.get("KN_KununuUserUUID__c") or "").strip()
            if uuid:
                by_uuid.setdefault(uuid, c)
            email = (c.get("Email") or "").strip().lower()
            if email:
                by_email[email].append(c)
            linkedin = _normalize_url(c.get("KN_LinkedInProfile__c"))
            if linkedin:
                by_linkedin[linkedin].append(c)
            acc = c.get("AccountId")
            if acc:
                by_account[acc].append(c)

        return cls(
            accounts=list(accounts), contacts=list(contacts),
            acct_by_profile_uuid=by_puid, acct_by_kununu_url=by_url,
            acct_by_vat=by_vat, acct_by_dbid=by_dbid,
            acct_by_host=dict(by_host), acct_by_norm_name=dict(by_name),
            contact_by_user_uuid=by_uuid, contact_by_email=dict(by_email),
            contact_by_linkedin=dict(by_linkedin), contacts_by_account=dict(by_account),
        )


# --- rule engine ---------------------------------------------------------------

RULE_DECISIONS = {
    "no_lf_match": "skip",
    "name_address": "link",
    "name_vat": "link",
    "kununu_over_name": "link",
    "kununu_agree": "link",
    "name_corroborated": "link",
    "kununu_conflict": "review",
    "name_only": "review",
    "duplicate": "review",
    "weak_signal": "review",
    "no_match": "create_new",
}


def _lead_name_norm(enriched: EnrichedLead) -> str | None:
    return normalize_name(enriched.matched_name or enriched.extracted.best_company)


def _account_names_norm(account: dict) -> set[str]:
    names = (account.get("Name"), account.get("KN_DBLegalCompanyName__c"))
    return {n for raw in names if (n := normalize_name(raw))}


def _name_equal_accounts(enriched: EnrichedLead, existing: ExistingRecords) -> list[dict]:
    target = _lead_name_norm(enriched)
    return existing.acct_by_norm_name.get(target, []) if target else []


def _address_equal(account: dict, company: dict) -> bool:
    plz = normalize_postal(company.get("Postal_Code"))
    city = normalize_city(company.get("City"))
    if not plz or not city:
        return False
    return (normalize_postal(account.get("BillingPostalCode")) == plz
            and normalize_city(account.get("BillingCity")) == city)


def _kununu_account(enriched: EnrichedLead, existing: ExistingRecords):
    """(account, signal_name) for a profile-UUID or kununu-URL hit, else None."""
    e = enriched.extracted
    puid = (e.kununu_profile_uuid or "").strip()
    if puid and puid in existing.acct_by_profile_uuid:
        return existing.acct_by_profile_uuid[puid], "kununu_profile_uuid"
    url = normalize_kununu_url(e.kununu_url)
    if url and url in existing.acct_by_kununu_url:
        return existing.acct_by_kununu_url[url], "kununu_url"
    return None


def _kununu_name_agreement(account: dict, enriched: EnrichedLead) -> int:
    """Max 0-100 agreement between the account's names and the lead's identities
    (LF company name, parsed profile/company name, feed assigned-company names)."""
    identities = [enriched.matched_name, enriched.extracted.best_company,
                  *(enriched.alt_names or [])]
    best = 0
    for acct_name in _account_names_norm(account):
        for ident in identities:
            ident_norm = normalize_name(ident)
            if ident_norm:
                best = max(best, int(fuzz.token_set_ratio(acct_name, ident_norm)))
    return best


def _best_fuzzy_account(enriched: EnrichedLead, existing: ExistingRecords) -> dict | None:
    """Closest non-identical fuzzy name hit (>= floor); exact equality is rule 2/3/6's job."""
    target = _lead_name_norm(enriched)
    if not target:
        return None
    best = None
    for norm, accts in existing.acct_by_norm_name.items():
        if norm == target:
            continue
        ratio = fuzz.token_sort_ratio(target, norm) / 100.0
        if ratio >= _FUZZY_FLOOR and (best is None or ratio > best[0]):
            best = (ratio, accts[0])
    return best[1] if best else None


def _weak_hits(enriched: EnrichedLead, existing: ExistingRecords) -> list[tuple[str, dict]]:
    """Demoted signals: they surface candidates as evidence but never link."""
    e = enriched.extracted
    company = enriched.company or {}
    hits: list[tuple[str, dict]] = []
    vat = _norm_vat(company.get("VAT_Id") or e.vat_number)
    if vat and vat in existing.acct_by_vat:
        hits.append(("vat", existing.acct_by_vat[vat]))
    dbid = (company.get("Register_Id") or "").strip()
    if dbid and dbid in existing.acct_by_dbid:
        hits.append(("dbid", existing.acct_by_dbid[dbid]))
    host = normalize_host(company.get("Website") or e.lead_website)
    if host and host in existing.acct_by_host:
        hits.append(("website_host", existing.acct_by_host[host][0]))
    domain = email_domain(e.contact_email or e.lead_email)
    if domain and domain not in _FREEMAIL_DOMAINS and domain in existing.acct_by_host:
        hits.append(("email_domain", existing.acct_by_host[domain][0]))
    fuzzy = _best_fuzzy_account(enriched, existing)
    if fuzzy:
        hits.append(("fuzzy_name", fuzzy))
    return hits


def _fire(enriched: EnrichedLead, existing: ExistingRecords, settings,
          kununu, name_accts, weak) -> tuple[str, AccountMatch | None]:
    if not enriched.matched:
        return "no_lf_match", None
    company = enriched.company or {}
    # A profile-UUID/URL account is an exact kununu-identity hit. When name_address/
    # name_vat would fire on a DIFFERENT account (an org-duplicate), the kununu account
    # wins — humans follow the profile account (skipped-task backtest 2026-07-08, 14/14).
    kununu_id = kununu[0]["Id"] if kununu else None
    for acct in name_accts:
        if _address_equal(acct, company):
            if kununu_id and kununu_id != acct["Id"]:
                return "kununu_over_name", AccountMatch(kununu_id, None, "kununu_over_name")
            return "name_address", AccountMatch(acct["Id"], None, "name_address")
    vat = _norm_vat(company.get("VAT_Id") or enriched.extracted.vat_number)
    if vat:
        for acct in name_accts:
            if _norm_vat(acct.get("KN_VATNumber__c")) == vat:
                if kununu_id and kununu_id != acct["Id"]:
                    return "kununu_over_name", AccountMatch(kununu_id, None, "kununu_over_name")
                return "name_vat", AccountMatch(acct["Id"], None, "name_vat")
    if kununu:
        acct, _sig = kununu
        agree = _kununu_name_agreement(acct, enriched)
        rule = ("kununu_agree" if agree >= settings.dedupe_name_agree_min
                else "kununu_conflict")
        return rule, AccountMatch(acct["Id"], None, rule)
    if name_accts:
        # Name equality plus an independent weak signal (VAT/DBID/host/email-domain/fuzzy)
        # pointing at the SAME account is two axes → link. When corroboration singles out
        # exactly one account it also disambiguates a multi-candidate name collision.
        weak_ids = {acct["Id"] for _sig, acct in weak}
        corroborated = [a for a in name_accts if a["Id"] in weak_ids]
        if len(corroborated) == 1:
            return "name_corroborated", AccountMatch(
                corroborated[0]["Id"], None, "name_corroborated")
        if len(name_accts) > 1:
            # Several existing accounts share the normalized name and nothing singles one
            # out: the org already holds duplicate accounts — a human must resolve them.
            return "duplicate", AccountMatch(name_accts[0]["Id"], None, "duplicate")
        return "name_only", AccountMatch(name_accts[0]["Id"], None, "name_only")
    if weak:
        return "weak_signal", AccountMatch(weak[0][1]["Id"], None, "weak_signal")
    return "no_match", None


def evaluate_account_rules(
    enriched: EnrichedLead, existing: ExistingRecords, settings
) -> tuple[str, AccountMatch | None, list[str]]:
    """(fired rule, account, evidence). Evidence lists all demoted-signal hits, plus
    name/kununu hits when they did not fire — the full picture on Klärfall rows."""
    kununu = _kununu_account(enriched, existing)
    name_accts = _name_equal_accounts(enriched, existing)
    weak = _weak_hits(enriched, existing)
    rule, account = _fire(enriched, existing, settings, kununu, name_accts, weak)

    evidence = [f"{sig}→{acct['Id']}" for sig, acct in weak]
    if kununu and rule not in ("kununu_agree", "kununu_conflict"):
        evidence.insert(0, f"{kununu[1]}→{kununu[0]['Id']}")
    if rule == "duplicate":
        # surface EVERY same-name candidate so the reviewer can resolve the duplicates
        evidence.insert(0, "name→" + ",".join(a["Id"] for a in name_accts))
    elif name_accts and rule not in (
        "name_address", "name_vat", "name_only", "name_corroborated"
    ):
        evidence.insert(0, f"name→{name_accts[0]['Id']}")
    return rule, account, evidence


def match_contact(
    enriched: EnrichedLead, existing: ExistingRecords, account_id: str | None, settings
) -> ContactMatch | None:
    e = enriched.extracted
    candidates: list[tuple[float, str, dict]] = []

    uuid = (e.kununu_user_uuid or "").strip()
    if uuid and uuid in existing.contact_by_user_uuid:
        candidates.append((1.0, "kununu_user_uuid", existing.contact_by_user_uuid[uuid]))

    email = (e.contact_email or e.lead_email or "").strip().lower()
    if email and email in existing.contact_by_email:
        matches = existing.contact_by_email[email]
        in_acct = [c for c in matches if account_id and c.get("AccountId") == account_id]
        if in_acct:
            candidates.append((0.95, "contact_email_in_account", in_acct[0]))
        else:
            candidates.append((0.85, "contact_email_global", matches[0]))

    linkedin = _normalize_url((enriched.contact or {}).get("contact_linkedin"))
    if linkedin and linkedin in existing.contact_by_linkedin:
        candidates.append((0.85, "linkedin", existing.contact_by_linkedin[linkedin][0]))

    if not candidates:
        return None
    score, reason, contact = max(candidates, key=lambda c: c[0])
    return ContactMatch(contact_id=contact["Id"], score=score, reason=reason)


def match_lead(enriched: EnrichedLead, existing: ExistingRecords, settings) -> MatchResult:
    # No accepted LeadFeeder company match ⇒ exclude the lead from account AND contact
    # dedup entirely (don't even surface candidate evidence). Terminal skip, not review:
    # there is no candidate for a human to adjudicate, and it never converts.
    if not enriched.matched:
        return MatchResult(enriched=enriched, account=None, contact=None,
                           decision=RULE_DECISIONS["no_lf_match"], rule="no_lf_match")
    rule, account, evidence = evaluate_account_rules(enriched, existing, settings)
    account_id = account.account_id if account else None
    contact = match_contact(enriched, existing, account_id, settings)
    return MatchResult(enriched=enriched, account=account, contact=contact,
                       decision=RULE_DECISIONS[rule], rule=rule, evidence=evidence)


# --- org boundary ------------------------------------------------------------

def load_existing_records(client, enriched_leads: list[EnrichedLead], settings, logger=None) -> ExistingRecords:
    """Collect every match signal across the batch, then pull candidate Accounts/Contacts
    in one query each (Account first, so candidate Account Ids widen the Contact pull)."""
    log = logger or get_logger("match")
    profile_uuids: set[str] = set()
    kununu_urls: set[str] = set()
    vats: set[str] = set()
    dbids: set[str] = set()
    hosts: set[str] = set()
    names: set[str] = set()
    name_patterns: set[str] = set()  # normalized LIKE widening (backtest recall gap)
    user_uuids: set[str] = set()
    emails: set[str] = set()
    linkedins: set[str] = set()

    for enr in enriched_leads:
        e = enr.extracted
        company = enr.company or {}
        if e.kununu_profile_uuid:
            profile_uuids.add(e.kununu_profile_uuid.strip())
        url = normalize_kununu_url(e.kununu_url)
        if url:
            kununu_urls.add(url)
        vat = company.get("VAT_Id") or e.vat_number
        if vat:
            vats.add(vat.strip())
        dbid = company.get("Register_Id")
        if dbid:
            dbids.add(dbid.strip())
        host = normalize_host(company.get("Website") or e.lead_website)
        if host:
            hosts.add(host)
        domain = email_domain(e.contact_email or e.lead_email)
        if domain and domain not in _FREEMAIL_DOMAINS:
            hosts.add(domain)
        name = e.best_company
        if name:
            names.add(name.strip())
            pattern = name_like_pattern(name)
            if pattern:
                name_patterns.add(pattern)
        if e.kununu_user_uuid:
            user_uuids.add(e.kununu_user_uuid.strip())
        email = e.contact_email or e.lead_email
        if email:
            emails.add(email.strip())
        linkedin = (enr.contact or {}).get("contact_linkedin")
        if linkedin:
            linkedins.add(linkedin.strip())

    accounts: list[dict] = []
    acct_q = soql.accounts_by_signals(
        profile_uuids=sorted(profile_uuids), kununu_urls=sorted(kununu_urls),
        vats=sorted(vats), dbids=sorted(dbids), hosts=sorted(hosts), names=sorted(names),
        name_patterns=sorted(name_patterns),
    )
    if acct_q:
        accounts = client.query(acct_q)
    account_ids = sorted({a["Id"] for a in accounts if a.get("Id")})

    contacts: list[dict] = []
    contact_q = soql.contacts_by_signals(
        user_uuids=sorted(user_uuids), emails=sorted(emails),
        linkedins=sorted(linkedins), account_ids=account_ids,
    )
    if contact_q:
        contacts = client.query(contact_q)

    log.info("Loaded %d candidate account(s), %d candidate contact(s)",
             len(accounts), len(contacts))
    return ExistingRecords.from_records(accounts, contacts)
