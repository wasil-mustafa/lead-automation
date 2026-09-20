"""Matcher tests (Module 3a).

The pure rule engine is tested directly against in-memory `ExistingRecords` built from
plain account/contact dicts (the same shape `SfClient.query` returns). The org boundary
(`load_existing_records`) is tested through a FakeClient that routes by SObject.

Rules are ordered (first hit wins) and drive link/review/create_new via the fixed
`RULE_DECISIONS` mapping — not score thresholds; see the module docstring in
`match.py` and
`docs/superpowers/specs/2026-07-03-dublettencheck-rules-and-shadow-compare-design.md`.
Contact matching keeps its original identifier-signal scoring.
"""
import pytest

from leadauto.config import Settings
from leadauto.models import ExtractedLead
from leadauto.models import AccountMatch, ContactMatch, EnrichedLead, MatchResult
from leadauto.textnorm import email_domain, normalize_host, normalize_name, normalize_url
from leadauto.match import (
    ExistingRecords,
    RULE_DECISIONS,
    evaluate_account_rules,
    load_existing_records,
    match_contact,
    match_lead,
)

SETTINGS = Settings.load()  # dedupe_name_agree_min 85


# --- builders ----------------------------------------------------------------

def _extracted(**kw) -> ExtractedLead:
    base = dict(lead_id="00Q1", task_id="T1", task_type="claimer")
    base.update(kw)
    return ExtractedLead(**base)


def _enriched(extracted=None, company=None, contact=None, **kw) -> EnrichedLead:
    return EnrichedLead(
        extracted=extracted or _extracted(),
        company=company or {},
        contact=contact or {},
        **kw,
    )


def _acct(aid="001A", **kw) -> dict:
    base = {
        "Id": aid, "Name": None, "Website": None,
        "KN_KununuProfileUuid__c": None, "KN_KununuLinkProfile__c": None,
        "KN_VATNumber__c": None, "KN_DBID__c": None, "KN_DBLegalCompanyName__c": None,
        "BillingPostalCode": None, "BillingCity": None,
    }
    base.update(kw)
    return base


def _contact(cid="003C", account_id="001A", **kw) -> dict:
    base = {
        "Id": cid, "AccountId": account_id, "Email": None,
        "KN_KununuUserUUID__c": None, "KN_LinkedInProfile__c": None,
        "FirstName": None, "LastName": None, "Name": None,
    }
    base.update(kw)
    return base


# --- normalization helpers ---------------------------------------------------

def test_normalize_host_strips_scheme_www_path():
    assert normalize_host("https://www.Acme.de/jobs?x=1") == "acme.de"
    assert normalize_host("acme.de") == "acme.de"
    assert normalize_host("http://acme.de/") == "acme.de"


def test_normalize_host_empty_is_none():
    assert normalize_host("") is None
    assert normalize_host(None) is None


def test_email_domain():
    assert email_domain("Max@Acme.DE") == "acme.de"
    assert email_domain("noatsign") is None
    assert email_domain(None) is None


def test_normalize_name_strips_legal_suffix_and_case():
    assert normalize_name("Hufeland Klinikum GmbH") == "hufeland klinikum"
    assert normalize_name("ACME AG") == "acme"
    assert normalize_name("  Foo  Bar  ") == "foo bar"
    assert normalize_name(None) is None


def test_normalize_url():
    a = normalize_url("https://www.kununu.com/de/hufeland-klinikum")
    b = normalize_url("http://kununu.com/de/hufeland-klinikum/")
    assert a == b
    assert normalize_url(None) is None


# --- ExistingRecords indexing ------------------------------------------------

def test_existing_records_indexes_accounts_and_contacts():
    accts = [
        _acct("001A", KN_KununuProfileUuid__c="puid-1", Website="https://www.acme.de"),
        _acct("001B", KN_VATNumber__c="DE123", Name="Beta GmbH"),
    ]
    contacts = [
        _contact("003C", "001A", Email="max@acme.de", KN_KununuUserUUID__c="uuid-1"),
        _contact("003D", "001A", Email="eva@acme.de"),
    ]
    ex = ExistingRecords.from_records(accts, contacts)
    assert ex.acct_by_profile_uuid["puid-1"]["Id"] == "001A"
    assert ex.acct_by_vat["DE123"]["Id"] == "001B"
    assert ex.acct_by_host["acme.de"][0]["Id"] == "001A"
    assert ex.contact_by_user_uuid["uuid-1"]["Id"] == "003C"
    assert {c["Id"] for c in ex.contacts_by_account["001A"]} == {"003C", "003D"}
    assert ex.contact_by_email["max@acme.de"][0]["Id"] == "003C"


# --- account rule engine ------------------------------------------------------

LF = dict(matched=True, matched_name="Acme GmbH", lf_company_id="lf-1")


def _lf_enriched(company=None, extracted=None, **kw):
    """An EnrichedLead that passed the LF gate."""
    base = {**LF, **kw}
    return _enriched(extracted=extracted, company=company or {}, **base)


def test_rule_no_lf_match_skips_account_and_contact_matching():
    # No accepted LF match ⇒ the lead is excluded from account AND contact matching
    # entirely (even a perfect kununu UUID / email hit surfaces nothing) and is a
    # terminal skip, NOT a review — there is nothing for a human to adjudicate.
    ex = ExistingRecords.from_records(
        [_acct("001A", KN_KununuProfileUuid__c="puid-1")],
        [_contact("003C", "001A", KN_KununuUserUUID__c="uuid-1", Email="max@acme.de")])
    enr = _enriched(extracted=_extracted(
        kununu_profile_uuid="puid-1", kununu_user_uuid="uuid-1", contact_email="max@acme.de"))
    r = match_lead(enr, ex, SETTINGS)
    assert r.rule == "no_lf_match" and r.decision == "skip"
    assert r.account is None
    assert r.contact is None
    assert r.evidence == []


def test_rule_name_address_links():
    ex = ExistingRecords.from_records(
        [_acct("001A", Name="Acme GmbH", BillingPostalCode="10115", BillingCity="Berlin")], [])
    enr = _lf_enriched(company={"Postal_Code": "10 115", "City": "berlin"})
    r = match_lead(enr, ex, SETTINGS)
    assert r.rule == "name_address" and r.decision == "link"
    assert r.account.account_id == "001A"


def test_rule_name_address_requires_both_plz_and_city():
    ex = ExistingRecords.from_records(
        [_acct("001A", Name="Acme GmbH", BillingPostalCode="10115", BillingCity="Hamburg")], [])
    enr = _lf_enriched(company={"Postal_Code": "10115", "City": "Berlin"})
    r = match_lead(enr, ex, SETTINGS)
    assert r.rule == "name_only" and r.decision == "review"


def test_rule_name_vat_links():
    ex = ExistingRecords.from_records([_acct("001A", Name="Acme GmbH", KN_VATNumber__c="DE 123")], [])
    enr = _lf_enriched(company={"VAT_Id": "de123"})
    r = match_lead(enr, ex, SETTINGS)
    assert r.rule == "name_vat" and r.decision == "link"


def test_rule_kununu_agree_links():
    ex = ExistingRecords.from_records(
        [_acct("001A", Name="Acme Services GmbH",
               KN_KununuLinkProfile__c="https://www.kununu.com/de/acme")], [])
    enr = _lf_enriched(extracted=_extracted(kununu_url="kununu.com/de/acme"),
                       matched_name="Zeta AG", alt_names=["Acme Services GmbH"])
    r = match_lead(enr, ex, SETTINGS)
    assert r.rule == "kununu_agree" and r.decision == "link"
    assert r.account.account_id == "001A"


def test_rule_kununu_conflict_reviews():
    ex = ExistingRecords.from_records(
        [_acct("001A", Name="Completely Different Firm",
               KN_KununuLinkProfile__c="https://www.kununu.com/de/acme")], [])
    enr = _lf_enriched(extracted=_extracted(kununu_url="kununu.com/de/acme"))
    r = match_lead(enr, ex, SETTINGS)
    assert r.rule == "kununu_conflict" and r.decision == "review"
    assert r.account.account_id == "001A"  # the conflicting account is surfaced


def test_rule_kununu_beats_name_only():
    # kununu URL (rules 4/5) is evaluated before bare name equality (rule 6).
    ex = ExistingRecords.from_records(
        [_acct("001A", Name="Acme GmbH"),
         _acct("001B", Name="Acme Holding",
               KN_KununuLinkProfile__c="https://www.kununu.com/de/acme")], [])
    enr = _lf_enriched(extracted=_extracted(kununu_url="kununu.com/de/acme"),
                       matched_name="Acme GmbH")
    r = match_lead(enr, ex, SETTINGS)
    assert r.rule in ("kununu_agree", "kununu_conflict")
    assert r.account.account_id == "001B"


def test_rule_kununu_over_name_beats_name_address():
    # A kununu-identity account (profile UUID/URL) wins over a name+address hit on a
    # DIFFERENT (org-duplicate) account — humans follow the profile account (backtest
    # 2026-07-08, 14/14). Links the kununu account, surfacing the overridden one.
    ex = ExistingRecords.from_records(
        [_acct("001A", Name="Acme GmbH", BillingPostalCode="10115", BillingCity="Berlin"),
         _acct("001B", Name="Acme GmbH",
               KN_KununuProfileUuid__c="puid-1")], [])
    enr = _lf_enriched(extracted=_extracted(kununu_profile_uuid="puid-1"),
                       company={"Postal_Code": "10115", "City": "Berlin"})
    r = match_lead(enr, ex, SETTINGS)
    assert r.rule == "kununu_over_name" and r.decision == "link"
    assert r.account.account_id == "001B"          # the kununu account wins
    assert "name→001A" in r.evidence               # overridden name+address account surfaced
    assert "kununu_profile_uuid→001B" in r.evidence


def test_rule_kununu_over_name_beats_name_vat():
    ex = ExistingRecords.from_records(
        [_acct("001A", Name="Acme GmbH", KN_VATNumber__c="DE123"),
         _acct("001B", Name="Acme GmbH",
               KN_KununuLinkProfile__c="https://www.kununu.com/de/acme")], [])
    enr = _lf_enriched(extracted=_extracted(kununu_url="kununu.com/de/acme"),
                       company={"VAT_Id": "DE123"})
    r = match_lead(enr, ex, SETTINGS)
    assert r.rule == "kununu_over_name" and r.decision == "link"
    assert r.account.account_id == "001B"


def test_rule_name_address_on_kununu_account_stays_name_address():
    # When name+address and the kununu identity point at the SAME account there is no
    # conflict — the stronger name_address rule fires as before (same link target).
    ex = ExistingRecords.from_records(
        [_acct("001A", Name="Acme GmbH", BillingPostalCode="10115", BillingCity="Berlin",
               KN_KununuProfileUuid__c="puid-1")], [])
    enr = _lf_enriched(extracted=_extracted(kununu_profile_uuid="puid-1"),
                       company={"Postal_Code": "10115", "City": "Berlin"})
    r = match_lead(enr, ex, SETTINGS)
    assert r.rule == "name_address" and r.decision == "link"
    assert r.account.account_id == "001A"


def test_rule_name_only_reviews():
    ex = ExistingRecords.from_records([_acct("001A", Name="Acme GmbH")], [])
    r = match_lead(_lf_enriched(), ex, SETTINGS)
    assert r.rule == "name_only" and r.decision == "review"
    assert r.account.account_id == "001A"


def test_rule_name_corroborated_links():
    # Name equality + an independent weak signal (website host) on the SAME account = two
    # axes → link. (VAT/address would fire the stronger rules 2/3 first.)
    ex = ExistingRecords.from_records([_acct("001A", Name="Acme GmbH", Website="acme.de")], [])
    enr = _lf_enriched(company={"Website": "https://www.acme.de"})
    r = match_lead(enr, ex, SETTINGS)
    assert r.rule == "name_corroborated" and r.decision == "link"
    assert r.account.account_id == "001A"
    assert "website_host→001A" in r.evidence


def test_rule_name_corroborated_disambiguates_collision():
    # Two accounts share the name; a weak signal singles out ONE → link to that one, not review.
    ex = ExistingRecords.from_records(
        [_acct("001A", Name="Acme GmbH"),
         _acct("001B", Name="Acme GmbH", Website="acme.de")], [])
    enr = _lf_enriched(company={"Website": "https://www.acme.de"})
    r = match_lead(enr, ex, SETTINGS)
    assert r.rule == "name_corroborated" and r.decision == "link"
    assert r.account.account_id == "001B"


def test_rule_duplicate_reviews_multiple_same_name_accounts():
    # Two existing accounts share the normalized name and nothing singles one out:
    # the org already holds duplicate accounts → review, surfacing EVERY candidate.
    ex = ExistingRecords.from_records(
        [_acct("001A", Name="Acme GmbH"), _acct("001B", Name="Acme GmbH")], [])
    r = match_lead(_lf_enriched(), ex, SETTINGS)
    assert r.rule == "duplicate" and r.decision == "review"
    assert r.account.account_id == "001A"  # first candidate is the placeholder link target
    assert "name→001A,001B" in r.evidence


def test_rule_weak_signal_reviews_with_evidence():
    ex = ExistingRecords.from_records([_acct("001A", KN_VATNumber__c="DE123")], [])
    enr = _lf_enriched(matched_name="Zeta AG", company={"VAT_Id": "DE123"})
    r = match_lead(enr, ex, SETTINGS)
    assert r.rule == "weak_signal" and r.decision == "review"
    assert r.account.account_id == "001A"
    assert "vat→001A" in r.evidence


def test_rule_no_match_creates():
    ex = ExistingRecords.from_records([], [])
    r = match_lead(_lf_enriched(), ex, SETTINGS)
    assert r.rule == "no_match" and r.decision == "create_new"
    assert r.account is None


def test_rule_decisions_cover_all_rules():
    assert set(RULE_DECISIONS) == {
        "no_lf_match", "name_address", "name_vat", "kununu_over_name", "kununu_agree",
        "name_corroborated", "kununu_conflict", "name_only", "duplicate", "weak_signal",
        "no_match",
    }
    assert set(RULE_DECISIONS.values()) == {"link", "review", "create_new", "skip"}
    assert RULE_DECISIONS["no_lf_match"] == "skip"


# --- contact matching --------------------------------------------------------

def test_contact_match_user_uuid_top_priority():
    ex = ExistingRecords.from_records(
        [_acct("001A")], [_contact("003C", "001A", KN_KununuUserUUID__c="uuid-1")])
    enr = _enriched(_extracted(kununu_user_uuid="uuid-1"))
    m = match_contact(enr, ex, account_id="001A", settings=SETTINGS)
    assert isinstance(m, ContactMatch)
    assert m.contact_id == "003C"
    assert m.score == 1.0
    assert m.reason == "kununu_user_uuid"


def test_contact_match_email_in_account_beats_global():
    ex = ExistingRecords.from_records(
        [_acct("001A"), _acct("001B")],
        [_contact("003C", "001A", Email="max@acme.de"),
         _contact("003D", "001B", Email="max@acme.de")])
    enr = _enriched(_extracted(contact_email="max@acme.de"))
    m = match_contact(enr, ex, account_id="001A", settings=SETTINGS)
    assert m.contact_id == "003C"
    assert m.score == 0.95
    assert m.reason == "contact_email_in_account"


def test_contact_match_email_global_when_no_account():
    ex = ExistingRecords.from_records(
        [_acct("001B")], [_contact("003D", "001B", Email="max@acme.de")])
    enr = _enriched(_extracted(contact_email="max@acme.de"))
    m = match_contact(enr, ex, account_id=None, settings=SETTINGS)
    assert m.contact_id == "003D"
    assert m.score == 0.85
    assert m.reason == "contact_email_global"


def test_contact_match_linkedin():
    ex = ExistingRecords.from_records(
        [_acct("001A")],
        [_contact("003C", "001A", KN_LinkedInProfile__c="https://linkedin.com/in/max")])
    enr = _enriched(contact={"contact_linkedin": "https://linkedin.com/in/max"})
    m = match_contact(enr, ex, account_id="001A", settings=SETTINGS)
    assert m.contact_id == "003C"
    assert m.score == 0.85
    assert m.reason == "linkedin"


def test_contact_match_none():
    ex = ExistingRecords.from_records([_acct("001A")], [_contact("003C", "001A")])
    enr = _enriched(_extracted(contact_email="nobody@nowhere.de"))
    assert match_contact(enr, ex, account_id="001A", settings=SETTINGS) is None


# --- full match_lead (account rule + contact together) ------------------------

def test_match_lead_link_with_account_and_contact():
    # kununu URL hit + name agreement -> link (rule kununu_agree), contact matched too.
    ex = ExistingRecords.from_records(
        [_acct("001A", Name="Acme GmbH", KN_KununuLinkProfile__c="https://www.kununu.com/de/acme")],
        [_contact("003C", "001A", KN_KununuUserUUID__c="uuid-1")])
    enr = _lf_enriched(extracted=_extracted(kununu_url="kununu.com/de/acme", kununu_user_uuid="uuid-1"))
    res = match_lead(enr, ex, SETTINGS)
    assert isinstance(res, MatchResult)
    assert res.rule == "kununu_agree" and res.decision == "link"
    assert res.account.account_id == "001A"
    assert res.contact.contact_id == "003C"


def test_match_lead_review_band_no_autolink():
    # website_host is a demoted (weak) signal -> review, never auto-link.
    ex = ExistingRecords.from_records([_acct("001A", Website="https://acme.de")], [])
    enr = _lf_enriched(company={"Website": "https://acme.de"})
    res = match_lead(enr, ex, SETTINGS)
    assert res.rule == "weak_signal" and res.decision == "review"
    assert res.account.account_id == "001A"
    assert res.contact is None


def test_match_lead_create_new_when_no_match():
    ex = ExistingRecords.from_records([], [])
    enr = _lf_enriched(extracted=_extracted(company_name="Brand New GmbH"))
    res = match_lead(enr, ex, SETTINGS)
    assert res.decision == "create_new"
    assert res.account is None
    assert res.contact is None


# --- org boundary: load_existing_records -------------------------------------

class FakeClient:
    """Routes by SObject in the SOQL; records queries for assertions."""

    def __init__(self, accounts=None, contacts=None):
        self._accounts = list(accounts or [])
        self._contacts = list(contacts or [])
        self.queries: list[str] = []

    def query(self, soql, bulk=False):
        self.queries.append(soql)
        if "FROM Account" in soql:
            return [dict(a) for a in self._accounts]
        if "FROM Contact" in soql:
            return [dict(c) for c in self._contacts]
        return []


def test_load_existing_records_queries_by_collected_signals():
    enr = _enriched(
        _extracted(kununu_profile_uuid="puid-1", kununu_user_uuid="uuid-1",
                   contact_email="max@acme.de", company_name="Acme GmbH"),
        company={"VAT_Id": "DE123", "Website": "https://acme.de"},
    )
    client = FakeClient(
        accounts=[_acct("001A", KN_KununuProfileUuid__c="puid-1")],
        contacts=[_contact("003C", "001A", KN_KununuUserUUID__c="uuid-1")])
    ex = load_existing_records(client, [enr], SETTINGS)
    assert isinstance(ex, ExistingRecords)
    assert ex.acct_by_profile_uuid["puid-1"]["Id"] == "001A"
    assert ex.contact_by_user_uuid["uuid-1"]["Id"] == "003C"
    # one Account query and one Contact query (batched, no N+1)
    assert sum("FROM Account" in q for q in client.queries) == 1
    assert sum("FROM Contact" in q for q in client.queries) == 1
    # collected signal values appear in the account query
    acct_q = next(q for q in client.queries if "FROM Account" in q)
    assert "puid-1" in acct_q and "DE123" in acct_q


def test_load_existing_records_no_signals_skips_queries():
    enr = _enriched(_extracted())  # nothing populated
    client = FakeClient()
    ex = load_existing_records(client, [enr], SETTINGS)
    assert isinstance(ex, ExistingRecords)
    assert client.queries == []


def test_load_existing_records_widens_names_with_like_patterns():
    # The 2026-07-08 backtest recall gap: an org account spelled "Kaufland Dienstleistung
    # Gmbh & Co. Kg" never matched `Name IN ('Kaufland Dienstleistung GmbH & Co.KG.')`.
    # The account query must now also carry a normalized prefix-anchored LIKE.
    enr = _enriched(_extracted(company_name="Kaufland Dienstleistung GmbH & Co.KG."))
    client = FakeClient()
    load_existing_records(client, [enr], SETTINGS)
    acct_q = next(q for q in client.queries if "FROM Account" in q)
    assert "Name LIKE 'kaufland%dienstleistung%'" in acct_q


def test_load_existing_records_no_like_for_acronymish_names():
    enr = _enriched(_extracted(company_name="AZS"))
    client = FakeClient()
    load_existing_records(client, [enr], SETTINGS)
    acct_q = next(q for q in client.queries if "FROM Account" in q)
    assert "LIKE 'azs" not in acct_q          # too broad; exact IN still applies
    assert "Name IN ('AZS')" in acct_q
