"""textnorm helpers (shared by the matcher and the LeadFeeder acceptance gate)."""
from leadauto.textnorm import (
    alnum,
    domain_root,
    domain_sld,
    email_domain,
    is_internal_email,
    normalize_name,
)


def test_normalize_name_strips_legal_suffixes_and_punctuation():
    assert normalize_name("BEWA solutions GmbH") == "bewa solutions"
    assert normalize_name("Alpinum Accounting AG") == "alpinum accounting"
    assert normalize_name("") is None


def test_alnum_squashes_to_lowercase_alphanumerics():
    assert alnum("Web.de GmbH & Co!") == "webdegmbhco"
    assert alnum(None) == ""


def test_domain_root_from_email_and_bare_domain():
    assert domain_root("kontakt@digitaleformate.de") == "digitaleformate"
    assert domain_root("vitness-poing.de") == "vitnesspoing"


def test_domain_root_rejects_freemail_and_short_roots():
    assert domain_root("jane@web.de") is None          # freemail
    assert domain_root("jane@googlemail.com") is None  # freemail
    assert domain_root("info@bbc.com") is None         # root < 5 chars
    assert domain_root(None) is None


def test_email_domain_basic():
    assert email_domain("A@Acme.DE") == "acme.de"
    assert email_domain("not-an-email") is None


def test_domain_sld_extracts_second_level_label():
    assert domain_sld("tester@kununu.com") == "kununu"
    assert domain_sld("a@mail.kununu.com") == "kununu"   # subdomain
    assert domain_sld("kununu.at") == "kununu"           # bare host, any TLD
    assert domain_sld("@xing") == "xing"                 # bare root with leading @
    assert domain_sld("onlyfy") == "onlyfy"              # bare root
    assert domain_sld("") is None
    assert domain_sld(None) is None


def test_is_internal_email_matches_any_tld():
    domains = {"kununu", "xing", "onlyfy"}
    assert is_internal_email("tester@kununu.com", domains) is True
    assert is_internal_email("tester@kununu.at", domains) is True
    assert is_internal_email("a@mail.onlyfy.io", domains) is True
    assert is_internal_email("Jobs@XING.COM", domains) is True   # case-insensitive


def test_is_internal_email_negatives():
    domains = {"kununu", "xing", "onlyfy"}
    assert is_internal_email("boss@notkununu.com", domains) is False  # not the SLD
    assert is_internal_email("boss@acme.de", domains) is False
    assert is_internal_email("not-an-email", domains) is False
    assert is_internal_email("", domains) is False
    assert is_internal_email(None, domains) is False
    assert is_internal_email("x@kununu.com", set()) is False          # nothing configured


def test_normalize_postal_strips_whitespace():
    from leadauto.textnorm import normalize_postal
    assert normalize_postal(" 10 115 ") == "10115"
    assert normalize_postal("1010") == "1010"
    assert normalize_postal("") is None
    assert normalize_postal(None) is None


def test_normalize_city_casefolds_and_collapses():
    from leadauto.textnorm import normalize_city
    assert normalize_city("  Wien ") == "wien"
    assert normalize_city("Frankfurt  am   Main") == "frankfurt am main"
    assert normalize_city("MÜNCHEN") == "münchen"
    assert normalize_city("") is None
    assert normalize_city(None) is None


# --- name_like_pattern (account-recall widening, 2026-07-10) ------------------

def test_name_like_pattern_builds_prefix_anchored_wildcards():
    from leadauto.textnorm import name_like_pattern
    # suffix + punctuation variants collapse to the same normalized base
    assert name_like_pattern("Kaufland Dienstleistung GmbH & Co.KG.") == "kaufland%dienstleistung%"
    assert name_like_pattern("Kaufland Dienstleistung Gmbh & Co. Kg") == "kaufland%dienstleistung%"


def test_name_like_pattern_skips_short_or_empty_bases():
    from leadauto.textnorm import name_like_pattern
    assert name_like_pattern("AZS") is None          # acronym-ish: too little signal
    assert name_like_pattern("SNG GmbH") is None     # base 'sng' < 4 chars
    assert name_like_pattern("GmbH") is None         # suffix-only -> empty base
    assert name_like_pattern("") is None
    assert name_like_pattern(None) is None


def test_name_like_pattern_never_emits_quotes_or_backslashes():
    from leadauto.textnorm import name_like_pattern
    p = name_like_pattern("O'Brien & Söhne\ GmbH")
    assert p is not None
    assert "'" not in p and "\\" not in p


def test_country_iso2_knows_leadfeeder_english_names_outside_europe():
    # 2026-09-18 ZPE dry run: LF matched Evonik to a record in "Canada", Henkel to
    # "Bosnia and Herzegovina" and Pax to "Argentina"; all three sailed through the
    # country sanity gate (which fails open on None) with a foreign address staged.
    from leadauto.textnorm import country_iso2
    assert country_iso2("Canada") == "CA"
    assert country_iso2("Bosnia and Herzegovina") == "BA"
    assert country_iso2("Argentina") == "AR"
    assert country_iso2("United States of America") == "US"
    assert country_iso2("South Korea") == "KR"
    assert country_iso2("Australia") == "AU"
    assert country_iso2("Nowhere Land") is None      # still fails open on the unknown
