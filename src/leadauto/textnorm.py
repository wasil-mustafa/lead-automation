"""Shared text normalizers for company/host/email comparison.

Extracted from `match.py` so the LeadFeeder client can reuse them for its match
acceptance gate without importing the matcher (match -> enrich -> client would cycle).
"""
from __future__ import annotations

import re

# Freemail/ISP domains never identify a company, so they are excluded from the
# email-domain signals (they would otherwise create noisy matches).
FREEMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "web.de", "gmx.de", "gmx.net", "gmx.com",
    "yahoo.com", "yahoo.de", "hotmail.com", "hotmail.de", "outlook.com", "outlook.de",
    "live.com", "live.de", "t-online.de", "aol.com", "icloud.com", "me.com",
    "mail.com", "freenet.de", "posteo.de", "protonmail.com", "proton.me",
}
LEGAL_SUFFIXES = {
    "gmbh", "mbh", "ag", "kg", "kgaa", "ohg", "ug", "se", "co", "gbr", "ev", "ek",
    "ltd", "inc", "llc", "plc", "sa", "srl", "bv", "nv", "oy", "ab", "as",
    "haftungsbeschraenkt", "haftungsbeschränkt",
}


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


def normalize_url(value: str | None) -> str | None:
    """Lowercase URL without scheme/www and trailing slash (used for kununu & LinkedIn)."""
    if not value:
        return None
    v = _strip_scheme_www(value.strip().lower()).rstrip("/")
    return v or None


def normalize_name(value: str | None) -> str | None:
    """Casefold a company name and drop punctuation + legal-form tokens for fuzzy compare."""
    if not value:
        return None
    v = re.sub(r"[^\w\s&]", " ", value.lower(), flags=re.UNICODE)
    tokens = [t for t in v.split() if t and t not in LEGAL_SUFFIXES]
    return " ".join(tokens) or None


def alnum(value: str | None) -> str:
    """Lowercase value with everything but [a-z0-9] removed (for domain-vs-name compare)."""
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def domain_root(value: str | None) -> str | None:
    """Alphanumeric first label of a corporate email domain, for name-vs-domain compare.

    Returns None for freemail domains (they never identify a company) and for roots
    shorter than 5 chars (too little signal — 'bbc' would substring-match anything).
    """
    domain = email_domain(value) if value and "@" in value else (value or "").strip().lower() or None
    if not domain or domain in FREEMAIL_DOMAINS:
        return None
    root = re.sub(r"[^a-z0-9]", "", domain.split(".", 1)[0])
    return root if len(root) >= 5 else None


def normalize_postal(value: str | None) -> str | None:
    """Postal code with ALL whitespace removed (LF and org store it inconsistently)."""
    if not value:
        return None
    v = re.sub(r"\s+", "", str(value))
    return v or None


def normalize_city(value: str | None) -> str | None:
    """Casefolded city name with collapsed whitespace, for address-identity compare."""
    if not value:
        return None
    v = re.sub(r"\s+", " ", str(value)).strip().casefold()
    return v or None
