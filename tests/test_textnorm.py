"""textnorm helpers (shared by the matcher and the LeadFeeder acceptance gate)."""
from leadauto.textnorm import alnum, domain_root, email_domain, normalize_name


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
