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

# Spelled-out German legal forms -> the abbreviation everyone else uses. kununu profile
# names (and so our Task "Profile-Name") carry the register's long form for some
# companies; LeadFeeder's name search returns NOTHING for it. Measured 2026-11-06 on
# lead 00QMz00000rhwTdMAI: "PAU-Söhne Gesellschaft mit beschränkter Haftung" -> 0
# candidates, "PAU-Söhne GmbH" -> the right company at 0.8 / agreement 100. Applied
# BEFORE tokenizing in `normalize_name` (so the dedupe rules see the same name an
# Account carries) and to the name sent to LF (`enrich.build_match_payload`).
# Order matters: the "& Co." compound forms before their bare parents.
_LEGAL_FORM_LONG = [
    (r"gesellschaft\s+mit\s+beschr(?:ä|ae|a)nkter\s+haftung\s*&\s*co\.?\s*kg", "GmbH & Co. KG"),
    (r"gesellschaft\s+mit\s+beschr(?:ä|ae|a)nkter\s+haftung", "GmbH"),
    (r"aktiengesellschaft\s*&\s*co\.?\s*kg", "AG & Co. KG"),
    (r"aktiengesellschaft", "AG"),
    (r"kommanditgesellschaft\s+auf\s+aktien", "KGaA"),
    (r"kommanditgesellschaft", "KG"),
    (r"offene\s+handelsgesellschaft", "OHG"),
    (r"unternehmergesellschaft", "UG"),
    (r"eingetragene\s+genossenschaft", "eG"),
    (r"eingetragener\s+verein", "e.V."),
    (r"eingetragener\s+kaufmann", "e.K."),
    (r"eingetragene\s+kauffrau", "e.K."),
]
_LEGAL_FORM_LONG_RE = [(re.compile(rf"(?<!\w){p}(?!\w)", re.IGNORECASE), a)
                       for p, a in _LEGAL_FORM_LONG]


def abbreviate_legal_form(value: str | None) -> str | None:
    """`value` with every spelled-out German legal form replaced by its abbreviation
    ("X Gesellschaft mit beschränkter Haftung" -> "X GmbH"); unchanged otherwise.
    Whitespace is left as found, so a name without a long form is returned byte-equal."""
    if not value:
        return value
    out = value
    for rx, abbr in _LEGAL_FORM_LONG_RE:
        out = rx.sub(abbr, out)
    return out


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


def domains_agree(a: str | None, b: str | None) -> bool:
    """True when two hosts/domains identify the same site (subdomain-tolerant):
    'shop.baustein.com' agrees with 'baustein.com'. Hosts already normalized."""
    if not a or not b:
        return False
    return a == b or a.endswith("." + b) or b.endswith("." + a)


def email_domain(value: str | None) -> str | None:
    """Lowercase domain of an email address. None if absent or not an address."""
    if not value or "@" not in value:
        return None
    domain = value.strip().lower().rsplit("@", 1)[1].strip()
    return domain or None


def domain_sld(value: str | None) -> str | None:
    """Second-level label of an email address or host — 'kununu' from
    'a@mail.kununu.com', 'kununu.at', or a bare 'kununu'. Lowercase; None if empty.

    Assumes single-label public suffixes (.com/.at/.io/.de); a multi-label TLD like
    '.co.uk' would yield the wrong label ('co'), but the own-company domains this
    guards don't use them.
    """
    host = email_domain(value) if value and "@" in value else normalize_host(value)
    if not host:
        return None
    labels = [l for l in host.split(".") if l]
    if not labels:
        return None
    return labels[-2] if len(labels) >= 2 else labels[0]


def is_internal_email(value: str | None, domains) -> bool:
    """True if the email's second-level domain label is in `domains` (bare lowercase
    roots, e.g. {'kununu', 'xing', 'onlyfy'}). Matches any TLD; blank/garbage -> False."""
    if not domains:
        return False
    sld = domain_sld(value)
    return sld is not None and sld in domains


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
    v = re.sub(r"[^\w\s&]", " ", abbreviate_legal_form(value).lower(), flags=re.UNICODE)
    tokens = [t for t in v.split() if t and t not in LEGAL_SUFFIXES]
    return " ".join(tokens) or None


def name_like_pattern(value: str | None) -> str | None:
    """Prefix-anchored SOQL LIKE pattern for account name recall (2026-07-08 backtest gap).

    `Name IN (...)` is exact, so normalized-equal spelling variants ("GmbH & Co.KG." vs
    "Gmbh & Co. Kg") never enter the candidate pool. This builds the widening pattern from
    the SAME normalization every name rule uses: `%` between base tokens absorbs the
    punctuation/suffix variance and the trailing `%` absorbs the legal form. LIKE is
    case-insensitive in SOQL, and extra candidates cost nothing — the rules re-compare
    normalized names. None for bases under 4 chars (acronym-ish; a `LIKE 'azs%'` sweep is
    all noise and acronyms can't be trusted on name evidence anyway).
    Residual gap: variance inside a token ("O'Brien" vs "OBrien") is still missed.
    """
    base = normalize_name(value)
    if not base or len(base) < 4:
        return None
    tokens = [re.sub(r"[\\'%]", "", t) for t in base.split()]
    tokens = [t for t in tokens if t and t != "&"]  # standalone '&' is connector noise
    if not tokens:
        return None
    return "%".join(tokens) + "%"


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


# English/German (+ a few local) country names. country_iso2 fails open (None) on
# anything unmapped, so an unknown spelling can never create a FALSE conflict — but an
# unmapped name also lets a genuinely foreign LeadFeeder record through the sanity gate
# with its address (2026-09-18 ZPE dry run: "Canada", "Bosnia and Herzegovina" and
# "Argentina" each staged a foreign street/city onto a DE lead). LeadFeeder writes
# English names, so the English list below is meant to be COMPLETE for the countries LF
# can return; German spellings cover the org's own picklist labels.
_COUNTRY_TO_ISO2 = {
    "germany": "DE", "deutschland": "DE",
    "austria": "AT", "österreich": "AT", "oesterreich": "AT",
    "switzerland": "CH", "schweiz": "CH", "suisse": "CH", "svizzera": "CH",
    "liechtenstein": "LI", "luxembourg": "LU", "luxemburg": "LU",
    "france": "FR", "frankreich": "FR", "italy": "IT", "italien": "IT", "italia": "IT",
    "netherlands": "NL", "niederlande": "NL", "nederland": "NL",
    "belgium": "BE", "belgien": "BE", "poland": "PL", "polen": "PL", "polska": "PL",
    "czech republic": "CZ", "czechia": "CZ", "tschechien": "CZ",
    "slovakia": "SK", "slowakei": "SK", "slovenia": "SI", "slowenien": "SI",
    "hungary": "HU", "ungarn": "HU", "croatia": "HR", "kroatien": "HR",
    "denmark": "DK", "dänemark": "DK", "sweden": "SE", "schweden": "SE",
    "norway": "NO", "norwegen": "NO", "finland": "FI", "finnland": "FI",
    "spain": "ES", "spanien": "ES", "españa": "ES", "portugal": "PT",
    "united kingdom": "GB", "great britain": "GB", "großbritannien": "GB",
    "ireland": "IE", "irland": "IE", "greece": "GR", "griechenland": "GR",
    "romania": "RO", "rumänien": "RO", "bulgaria": "BG", "bulgarien": "BG",
    "turkey": "TR", "türkei": "TR", "türkiye": "TR", "united states": "US", "usa": "US",
    "united states of america": "US", "vereinigte staaten": "US",
    # rest of Europe
    "iceland": "IS", "island": "IS", "estonia": "EE", "estland": "EE", "latvia": "LV",
    "lettland": "LV", "lithuania": "LT", "litauen": "LT", "ukraine": "UA", "belarus": "BY",
    "moldova": "MD", "republic of moldova": "MD", "russia": "RU", "russian federation": "RU",
    "russland": "RU", "serbia": "RS", "serbien": "RS", "bosnia and herzegovina": "BA",
    "bosnia & herzegovina": "BA", "bosnien und herzegowina": "BA", "montenegro": "ME",
    "north macedonia": "MK", "macedonia": "MK", "nordmazedonien": "MK", "albania": "AL",
    "albanien": "AL", "kosovo": "XK", "malta": "MT", "cyprus": "CY", "zypern": "CY",
    "monaco": "MC", "andorra": "AD", "san marino": "SM", "vatican city": "VA",
    "gibraltar": "GI", "isle of man": "IM", "jersey": "JE", "guernsey": "GG",
    "faroe islands": "FO",
    # Americas
    "canada": "CA", "kanada": "CA", "mexico": "MX", "mexiko": "MX", "brazil": "BR",
    "brasilien": "BR", "argentina": "AR", "argentinien": "AR", "chile": "CL",
    "colombia": "CO", "kolumbien": "CO", "peru": "PE", "venezuela": "VE", "ecuador": "EC",
    "uruguay": "UY", "paraguay": "PY", "bolivia": "BO", "costa rica": "CR", "panama": "PA",
    "guatemala": "GT", "cuba": "CU", "dominican republic": "DO", "puerto rico": "PR",
    # Asia / Middle East
    "china": "CN", "people's republic of china": "CN", "hong kong": "HK", "taiwan": "TW",
    "japan": "JP", "south korea": "KR", "korea, republic of": "KR", "republic of korea": "KR",
    "india": "IN", "indien": "IN", "singapore": "SG", "singapur": "SG", "malaysia": "MY",
    "indonesia": "ID", "indonesien": "ID", "thailand": "TH", "vietnam": "VN",
    "viet nam": "VN", "philippines": "PH", "philippinen": "PH", "pakistan": "PK",
    "bangladesh": "BD", "sri lanka": "LK", "kazakhstan": "KZ", "uzbekistan": "UZ",
    "israel": "IL", "united arab emirates": "AE", "vereinigte arabische emirate": "AE",
    "uae": "AE", "saudi arabia": "SA", "saudi-arabien": "SA", "qatar": "QA", "katar": "QA",
    "kuwait": "KW", "bahrain": "BH", "oman": "OM", "jordan": "JO", "jordanien": "JO",
    "lebanon": "LB", "libanon": "LB", "iran": "IR", "iraq": "IQ", "irak": "IQ",
    "georgia": "GE", "georgien": "GE", "armenia": "AM", "armenien": "AM", "azerbaijan": "AZ",
    "aserbaidschan": "AZ",
    # Africa
    "south africa": "ZA", "südafrika": "ZA", "egypt": "EG", "ägypten": "EG", "morocco": "MA",
    "marokko": "MA", "tunisia": "TN", "tunesien": "TN", "algeria": "DZ", "algerien": "DZ",
    "nigeria": "NG", "kenya": "KE", "kenia": "KE", "ghana": "GH", "ethiopia": "ET",
    "äthiopien": "ET", "tanzania": "TZ", "tansania": "TZ", "uganda": "UG", "senegal": "SN",
    "ivory coast": "CI", "côte d'ivoire": "CI", "cameroon": "CM", "kamerun": "CM",
    "angola": "AO", "mozambique": "MZ", "namibia": "NA", "zimbabwe": "ZW", "mauritius": "MU",
    # Oceania
    "australia": "AU", "australien": "AU", "new zealand": "NZ", "neuseeland": "NZ",
}


def country_iso2(value) -> str | None:
    """ISO2 for a 2-letter code or a common English/German country name; None if unknown."""
    if not value:
        return None
    v = str(value).strip()
    if len(v) == 2 and v.isalpha():
        return v.upper()
    return _COUNTRY_TO_ISO2.get(v.casefold())


def normalize_postal(value: str | None) -> str | None:
    """Postal code for identity compare: no whitespace, upper-cased, and a leading
    country prefix ('D-50969', 'CH-8001') dropped. LF and the org store it inconsistently."""
    if not value:
        return None
    v = re.sub(r"\s+", "", str(value)).upper()
    v = re.sub(r"^[A-Z]{1,2}-", "", v)
    return v or None


# City-name prefixes that are not the city: 'Bad Homburg' vs 'Bad Kreuznach', 'St. Gallen'
# vs 'St. Pölten', 'Neu-Ulm' vs 'Neu-Isenburg' must NOT collapse to 'bad' / 'st' / 'neu'.
_CITY_PREFIXES = {"bad", "st", "sankt", "saint", "neu", "alt", "gross", "klein", "ober", "unter"}


def normalize_city(value: str | None) -> str | None:
    """City identity for address compare: the head word, ASCII-folded both ways so
    'Düsseldorf' == 'Duesseldorf' == 'Dusseldorf'; 'Frankfurt am Main' ~ 'Frankfurt',
    'Bonn Bad-Godesberg' ~ 'Bonn', 'Berlin-Kreuzberg' ~ 'Berlin'. A leading prefix word
    ('Bad', 'St.', 'Neu') is kept TOGETHER with the word after it, so 'Bad Homburg' !=
    'Bad Kreuznach'. Used by the name_address link rule and the Address_Check sheet —
    ONE definition of "same city", on purpose (2026-09-19: there were two)."""
    if not value:
        return None
    s = str(value).strip().casefold()
    for a, b in (("ä", "a"), ("ö", "o"), ("ü", "u"), ("ß", "ss"), ("ae", "a"), ("oe", "o"),
                 ("ue", "u")):
        s = s.replace(a, b)
    s = re.sub(r"[^a-z0-9 \-]", " ", s).strip()
    words = [w for w in re.split(r"[ \-]+", s) if w]
    if not words:
        return None
    if words[0] in _CITY_PREFIXES and len(words) > 1:
        return f"{words[0]} {words[1]}"
    return words[0]


# --- Salesforce value coercions ---------------------------------------------------
# Bulk API 2.0 results are CSV, so booleans arrive as 'true'/'false' STRINGS ('false' is
# truthy!) and nulls as ''. REST returns real bools/None. Both shapes pass through here.

def is_true(value) -> bool:
    """True for a real True or the string 'true' (any case); everything else False."""
    return value is True or str(value).strip().lower() == "true"


def is_blank(value) -> bool:
    """None or a whitespace-only string. A non-string non-None value is never blank."""
    return value is None or (isinstance(value, str) and not value.strip())


# KN_Checklengthpostalcode: PostalCode length the org enforces per country (Austria is
# unchecked). One table for the write-back and the conversion preflight.
POSTAL_LEN_BY_CC = {"DE": 5, "CH": 4}
