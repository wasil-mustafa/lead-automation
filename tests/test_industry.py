"""industry.py: kununu/LeadFeeder industry -> SF Industry ('Branche') picklist."""
import logging

from leadauto.industry import (
    KUNUNU_TO_SF,
    LF_TO_SF,
    VALID_SF_INDUSTRIES,
    sf_industry_from_kununu,
    sf_industry_from_leadfeeder,
)


def test_all_44_kununu_values_present():
    # The feed scan (2026-07-13) found exactly these 44 distinct
    # profile_industry_name_de values; every one must map.
    assert len(KUNUNU_TO_SF) == 44


def test_every_kununu_target_is_a_valid_sf_label():
    for target in KUNUNU_TO_SF.values():
        assert target in VALID_SF_INDUSTRIES


def test_every_lf_target_is_a_valid_sf_label():
    for target in LF_TO_SF.values():
        assert target in VALID_SF_INDUSTRIES


def test_kununu_exact_hits():
    assert sf_industry_from_kununu("IT") == "INTERNET and INFORMATION TECHNOLOGY"
    assert sf_industry_from_kununu("Banken") == "BANKING and INSURANCE"
    assert sf_industry_from_kununu("Sonstige Branchen") == "OTHER"


def test_kununu_normalizes_case_whitespace_and_umlauts():
    assert sf_industry_from_kununu("  it  ") == "INTERNET and INFORMATION TECHNOLOGY"
    # umlaut key must resolve regardless of surrounding whitespace/case
    assert sf_industry_from_kununu("öffentliche verwaltung") == \
        "PUBLIC SERVICE, ASSOCIATIONS and INSTITUTIONS"
    assert sf_industry_from_kununu("Steuerberatung/Wirtschaftsprüfung") == \
        "CONSULTING, AUDITING and LAW"


def test_kununu_blank_and_none_return_none_without_logging(caplog):
    caplog.set_level(logging.WARNING)
    assert sf_industry_from_kununu(None) is None
    assert sf_industry_from_kununu("") is None
    assert sf_industry_from_kununu("   ") is None
    assert caplog.records == []


def test_kununu_unknown_value_returns_none_and_logs(caplog):
    caplog.set_level(logging.WARNING)
    assert sf_industry_from_kununu("Raumfahrt-Zeugs") is None
    assert any("Raumfahrt-Zeugs" in r.getMessage() for r in caplog.records)


def test_leadfeeder_first_mappable_token_wins():
    # LF joins multiple industries with '; '; the first token that maps wins.
    assert sf_industry_from_leadfeeder("Software; IT") == \
        "INTERNET and INFORMATION TECHNOLOGY"
    assert sf_industry_from_leadfeeder("Business Consulting and Services") == \
        "CONSULTING, AUDITING and LAW"


def test_leadfeeder_unknown_returns_none_and_logs(caplog):
    caplog.set_level(logging.WARNING)
    assert sf_industry_from_leadfeeder("Underwater Basket Weaving") is None
    assert any("Underwater Basket Weaving" in r.getMessage() for r in caplog.records)


def test_leadfeeder_blank_and_none_return_none_without_logging(caplog):
    caplog.set_level(logging.WARNING)
    assert sf_industry_from_leadfeeder(None) is None
    assert sf_industry_from_leadfeeder("") is None
    assert caplog.records == []
