"""Matcher tests (Module 3a).

The pure scoring/normalization logic is tested directly against in-memory `ExistingRecords`
built from plain account/contact dicts (the same shape `SfClient.query` returns). The org
boundary (`load_existing_records`) is tested through a FakeClient that routes by SObject.

Signal priority and scores follow the approved plan's tables; thresholds come from the real
config.yaml (auto_link 0.90, review 0.60) via Settings.load().
"""
import pytest

from leadauto.config import Settings
from leadauto.extract.models import ExtractedLead
from leadauto.leadfeeder.enrich import EnrichedLead
from leadauto.match import (
    AccountMatch,
    ContactMatch,
    ExistingRecords,
    MatchResult,
    decide,
    email_domain,
    load_existing_records,
    match_account,
    match_contact,
    match_lead,
    normalize_host,
    normalize_kununu_url,
    normalize_name,
)

SETTINGS = Settings.load()  # auto_link 0.90, review 0.60


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


def test_normalize_kununu_url():
    a = normalize_kununu_url("https://www.kununu.com/de/hufeland-klinikum")
    b = normalize_kununu_url("http://kununu.com/de/hufeland-klinikum/")
    assert a == b
    assert normalize_kununu_url(None) is None


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


# --- account matching --------------------------------------------------------

def test_account_match_profile_uuid_is_top_priority():
    ex = ExistingRecords.from_records([_acct("001A", KN_KununuProfileUuid__c="puid-1")], [])
    enr = _enriched(_extracted(kununu_profile_uuid="puid-1"))
    m = match_account(enr, ex, SETTINGS)
    assert isinstance(m, AccountMatch)
    assert m.account_id == "001A"
    assert m.score == 1.0
    assert m.reason == "kununu_profile_uuid"


def test_account_match_kununu_url():
    ex = ExistingRecords.from_records(
        [_acct("001A", KN_KununuLinkProfile__c="https://www.kununu.com/de/acme")], [])
    enr = _enriched(_extracted(kununu_url="http://kununu.com/de/acme/"))
    m = match_account(enr, ex, SETTINGS)
    assert m.account_id == "001A"
    assert m.score == 0.97
    assert m.reason == "kununu_url"


def test_account_match_vat_from_leadfeeder_company():
    ex = ExistingRecords.from_records([_acct("001A", KN_VATNumber__c="DE811569869")], [])
    enr = _enriched(company={"VAT_Id": "DE811569869"})
    m = match_account(enr, ex, SETTINGS)
    assert m.account_id == "001A"
    assert m.score == 0.95
    assert m.reason == "vat"


def test_account_match_dbid_from_register_id():
    ex = ExistingRecords.from_records([_acct("001A", KN_DBID__c="DB-42")], [])
    enr = _enriched(company={"Register_Id": "DB-42"})
    m = match_account(enr, ex, SETTINGS)
    assert m.score == 0.90
    assert m.reason == "dbid"


def test_account_match_website_host_is_review_band():
    ex = ExistingRecords.from_records([_acct("001A", Website="https://www.acme.de")], [])
    enr = _enriched(company={"Website": "http://acme.de/jobs"})
    m = match_account(enr, ex, SETTINGS)
    assert m.score == 0.85
    assert m.reason == "website_host"


def test_account_match_email_domain():
    ex = ExistingRecords.from_records([_acct("001A", Website="https://www.acme.de")], [])
    enr = _enriched(_extracted(contact_email="max@acme.de"))
    m = match_account(enr, ex, SETTINGS)
    assert m.score == 0.70
    assert m.reason == "email_domain"


def test_account_match_fuzzy_name_review_band():
    ex = ExistingRecords.from_records([_acct("001A", Name="Hufeland Klinikum")], [])
    enr = _enriched(_extracted(company_name="Hufeland Klinikum GmbH"))
    m = match_account(enr, ex, SETTINGS)
    assert m.reason == "fuzzy_name"
    assert 0.60 <= m.score <= 0.80


def test_account_match_highest_signal_wins():
    # both a strong id and a weak host point at the same account
    ex = ExistingRecords.from_records(
        [_acct("001A", KN_KununuProfileUuid__c="puid-1", Website="https://acme.de")], [])
    enr = _enriched(_extracted(kununu_profile_uuid="puid-1"),
                    company={"Website": "https://acme.de"})
    m = match_account(enr, ex, SETTINGS)
    assert m.score == 1.0 and m.reason == "kununu_profile_uuid"


def test_account_match_ignores_freemail_email_domain():
    # a freemail/ISP domain must not link to an account that happens to share that host
    ex = ExistingRecords.from_records([_acct("001A", Website="https://web.de")], [])
    enr = _enriched(_extracted(contact_email="max@web.de"))
    assert match_account(enr, ex, SETTINGS) is None


def test_account_match_none_when_no_signal():
    ex = ExistingRecords.from_records([_acct("001A", Name="Totally Different")], [])
    enr = _enriched(_extracted(company_name="Nothing Alike Inc"))
    assert match_account(enr, ex, SETTINGS) is None


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


# --- decision + full match_lead ----------------------------------------------

def test_decide_bands():
    assert decide(1.0, SETTINGS) == "link"
    assert decide(0.90, SETTINGS) == "link"
    assert decide(0.85, SETTINGS) == "review"
    assert decide(0.60, SETTINGS) == "review"
    assert decide(0.59, SETTINGS) == "create_new"
    assert decide(None, SETTINGS) == "create_new"


def test_match_lead_link_with_account_and_contact():
    ex = ExistingRecords.from_records(
        [_acct("001A", KN_KununuProfileUuid__c="puid-1")],
        [_contact("003C", "001A", KN_KununuUserUUID__c="uuid-1")])
    enr = _enriched(_extracted(kununu_profile_uuid="puid-1", kununu_user_uuid="uuid-1"))
    res = match_lead(enr, ex, SETTINGS)
    assert isinstance(res, MatchResult)
    assert res.decision == "link"
    assert res.account.account_id == "001A"
    assert res.contact.contact_id == "003C"


def test_match_lead_review_band_no_autolink():
    ex = ExistingRecords.from_records([_acct("001A", Website="https://acme.de")], [])
    enr = _enriched(company={"Website": "https://acme.de"})
    res = match_lead(enr, ex, SETTINGS)
    assert res.decision == "review"
    assert res.account.account_id == "001A"
    assert res.contact is None


def test_match_lead_create_new_when_no_match():
    ex = ExistingRecords.from_records([], [])
    enr = _enriched(_extracted(company_name="Brand New GmbH"))
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
