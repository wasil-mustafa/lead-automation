"""Module 3a — multi-signal match of an enriched Lead vs existing Accounts/Contacts.

The matcher is deliberately split into pure scoring (operating on an in-memory
`ExistingRecords` index) and a thin org-boundary loader (`load_existing_records`) that
pulls candidate records in one batched query each. Both sides normalize URLs/hosts/names,
so the comparison is robust to the org storing full URLs and legal-suffix name variants.

Signal priority and scores follow the approved plan:

  Account  kununu_profile_uuid 1.00 · kununu_url 0.97 · vat 0.95 · dbid 0.90 ·
           website_host 0.85 · email_domain 0.70 · fuzzy_name 0.60–0.80
  Contact  kununu_user_uuid 1.00 · email_in_account 0.95 · email_global / linkedin 0.85

Decision (on the account score): >= auto_link -> link; >= review -> review; else create_new.
Only strong identifier signals reach the auto_link band, so host/domain/fuzzy never auto-link.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field

from rapidfuzz import fuzz

from .leadfeeder.enrich import EnrichedLead
from .logging import get_logger
from .sf import soql

_FUZZY_FLOOR = 0.90  # rapidfuzz ratio below this never counts as a name match
# Freemail/ISP domains never identify a company, so they are excluded from the
# email-domain account signal (they would otherwise create noisy review-band matches).
_FREEMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "web.de", "gmx.de", "gmx.net", "gmx.com",
    "yahoo.com", "yahoo.de", "hotmail.com", "hotmail.de", "outlook.com", "outlook.de",
    "live.com", "live.de", "t-online.de", "aol.com", "icloud.com", "me.com",
    "mail.com", "freenet.de", "posteo.de", "protonmail.com", "proton.me",
}
_LEGAL_SUFFIXES = {
    "gmbh", "mbh", "ag", "kg", "kgaa", "ohg", "ug", "se", "co", "gbr", "ev", "ek",
    "ltd", "inc", "llc", "plc", "sa", "srl", "bv", "nv", "oy", "ab", "as",
    "haftungsbeschraenkt", "haftungsbeschränkt",
}


# --- normalization helpers ---------------------------------------------------

def _strip_scheme_www(value: str) -> str:
    if "://" in value:
        value = value.split("://", 1)[1]
    if value.startswith("www."):
        value = value[4:]
    return value


def normalize_host(value: str | None) -> str | None:
    """Registrable host of a URL/host string: lowercase, no scheme/www/path. None if empty."""
    if not value:
        return None
    v = _strip_scheme_www(value.strip().lower())
    v = v.split("/", 1)[0].split("?", 1)[0].strip()
    return v or None


def email_domain(value: str | None) -> str | None:
    """Lowercase domain of an email address. None if absent or not an address."""
    if not value or "@" not in value:
        return None
    domain = value.strip().lower().rsplit("@", 1)[1].strip()
    return domain or None


def _normalize_url(value: str | None) -> str | None:
    """Lowercase URL without scheme/www and trailing slash (used for kununu & LinkedIn)."""
    if not value:
        return None
    v = _strip_scheme_www(value.strip().lower()).rstrip("/")
    return v or None


def normalize_kununu_url(value: str | None) -> str | None:
    return _normalize_url(value)


def normalize_name(value: str | None) -> str | None:
    """Casefold a company name and drop punctuation + legal-form tokens for fuzzy compare."""
    if not value:
        return None
    v = re.sub(r"[^\w\s&]", " ", value.lower(), flags=re.UNICODE)
    tokens = [t for t in v.split() if t and t not in _LEGAL_SUFFIXES]
    return " ".join(tokens) or None


# --- result records ----------------------------------------------------------

@dataclass
class AccountMatch:
    account_id: str
    score: float
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
            vat = (a.get("KN_VATNumber__c") or "").strip()
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


# --- scoring -----------------------------------------------------------------

def _best_fuzzy_account(name: str | None, existing: ExistingRecords):
    """Highest-scoring fuzzy name match (>= floor), mapped onto the 0.60–0.80 band."""
    target = normalize_name(name)
    if not target:
        return None
    best = None
    for norm, accts in existing.acct_by_norm_name.items():
        ratio = fuzz.token_sort_ratio(target, norm) / 100.0
        if ratio < _FUZZY_FLOOR:
            continue
        score = round(min(0.80, 0.60 + (ratio - _FUZZY_FLOOR) * 2.0), 2)
        if best is None or score > best[0]:
            best = (score, "fuzzy_name", accts[0])
    return best


def match_account(enriched: EnrichedLead, existing: ExistingRecords, settings) -> AccountMatch | None:
    e = enriched.extracted
    company = enriched.company or {}
    candidates: list[tuple[float, str, dict]] = []

    puid = (e.kununu_profile_uuid or "").strip()
    if puid and puid in existing.acct_by_profile_uuid:
        candidates.append((1.0, "kununu_profile_uuid", existing.acct_by_profile_uuid[puid]))

    url = normalize_kununu_url(e.kununu_url)
    if url and url in existing.acct_by_kununu_url:
        candidates.append((0.97, "kununu_url", existing.acct_by_kununu_url[url]))

    vat = (company.get("VAT_Id") or e.vat_number or "").strip()
    if vat and vat in existing.acct_by_vat:
        candidates.append((0.95, "vat", existing.acct_by_vat[vat]))

    dbid = (company.get("Register_Id") or "").strip()
    if dbid and dbid in existing.acct_by_dbid:
        candidates.append((0.90, "dbid", existing.acct_by_dbid[dbid]))

    host = normalize_host(company.get("Website") or e.lead_website)
    if host and host in existing.acct_by_host:
        candidates.append((0.85, "website_host", existing.acct_by_host[host][0]))

    domain = email_domain(e.contact_email or e.lead_email)
    if domain and domain not in _FREEMAIL_DOMAINS and domain in existing.acct_by_host:
        candidates.append((0.70, "email_domain", existing.acct_by_host[domain][0]))

    fuzzy = _best_fuzzy_account(e.best_company, existing)
    if fuzzy:
        candidates.append(fuzzy)

    if not candidates:
        return None
    score, reason, acct = max(candidates, key=lambda c: c[0])
    return AccountMatch(account_id=acct["Id"], score=score, reason=reason)


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


def decide(score: float | None, settings) -> str:
    """Map an account score onto the conversion decision band."""
    if score is None:
        return "create_new"
    if score >= settings.auto_link_threshold:
        return "link"
    if score >= settings.review_threshold:
        return "review"
    return "create_new"


def match_lead(enriched: EnrichedLead, existing: ExistingRecords, settings) -> MatchResult:
    account = match_account(enriched, existing, settings)
    decision = decide(account.score if account else None, settings)
    account_id = account.account_id if account else None
    contact = match_contact(enriched, existing, account_id, settings)
    return MatchResult(enriched=enriched, account=account, contact=contact, decision=decision)


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
