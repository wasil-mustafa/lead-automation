"""enrich_leads orchestration tests.

A FakeClient stubs the LeadfeederClient methods so the real orchestration logic is
exercised: mapping ExtractedLead -> match args, matched/unmatched split, single batched
company retrieve, and contact assembly with lead-field fallbacks.
"""
import requests
from leadauto.config import Settings
from leadauto.extract.models import ExtractedLead
from leadauto.leadfeeder.enrich import EnrichedLead, enrich_leads

SETTINGS = Settings.load()


class FakeClient:
    def __init__(self):
        self.match_by_name: dict = {}     # company_name -> match dict | None
        self.company_attrs: dict = {}     # lf_company_id -> raw attrs
        self.contact_by_name: dict = {}   # name -> {contact_id, full_name} | None
        self.contact_attrs: dict = {}     # contact_id -> raw LF attributes
        self.calls: list = []

    def get_account_id(self):
        self.calls.append(("account",))
        return "acc1"

    def find_or_create_list(self, name, scope):
        self.calls.append(("list", name, scope))
        return f"{scope}-list"

    def match_companies(self, payloads):
        self.calls.append(("match_companies", tuple(
            (p["company_name"], p.get("email"), p.get("country"), p.get("country_code"))
            for p in payloads)))
        return [self.match_by_name.get(p["company_name"]) for p in payloads]

    def add_company_to_list(self, company_id, list_id):
        self.calls.append(("add_company", company_id, list_id))

    def retrieve_companies(self, ids):
        self.calls.append(("retrieve", tuple(ids)))
        return {i: self.company_attrs.get(i, {}) for i in ids}

    def search_contact(self, name, company_id):
        self.calls.append(("search", name, company_id))
        return self.contact_by_name.get(name)

    def retrieve_contacts(self, ids):
        self.calls.append(("retrieve_contacts", tuple(ids)))
        return {i: self.contact_attrs.get(i, {}) for i in ids}

    def add_contact_to_list(self, contact_id, list_id):
        self.calls.append(("add_contact", contact_id, list_id))


def _extracted(company="Acme GmbH", contact_name="Jane Doe", region="Germany", cc="DE",
               contact_email="jane@acme.de", lead_email=None, lead_company=None,
               first=None, last=None, lead_id="00Q1", task_id="T1"):
    return ExtractedLead(
        lead_id=lead_id, task_id=task_id, task_type="claimer",
        company_name=company, lead_company=lead_company, contact_name=contact_name,
        region=region, country_code=cc, contact_email=contact_email, lead_email=lead_email,
        lead_first_name=first, lead_last_name=last,
    )


def test_enrich_matched_with_contact():
    fc = FakeClient()
    fc.match_by_name = {"Acme GmbH": {"company_id": "c1", "match_score": 0.95, "matched_name": "Acme GmbH"}}
    fc.company_attrs = {"c1": {"url": "https://acme.de", "vat_id": "DE123"}}
    fc.contact_by_name = {"Jane Doe": {"contact_id": "k1", "full_name": "Jane Doe"}}
    fc.contact_attrs = {"k1": {"position": {"title": "CEO"},
                              "social_media_profiles": {"linkedin": [{"url": "https://li/x"}]}}}

    [r] = enrich_leads(fc, [_extracted()], SETTINGS)
    assert isinstance(r, EnrichedLead)
    assert r.matched is True
    assert (r.lf_company_id, r.match_score, r.matched_name) == ("c1", 0.95, "Acme GmbH")
    assert r.company["Website"] == "https://acme.de"   # flattened from attrs
    assert r.company["VAT_Id"] == "DE123"
    assert r.contact_found is True
    assert r.contact["position"] == "CEO"
    assert r.contact["contact_id"] == "k1"
    assert r.contact["contact_linkedin"] == "https://li/x"
    assert ("match_companies", (("Acme GmbH", "jane@acme.de", "Germany", "DE"),)) in fc.calls
    assert ("add_company", "c1", "company-list") in fc.calls
    assert ("add_contact", "k1", "contact-list") in fc.calls


def test_enrich_carries_name_agreement():
    # The gate's agreement value must survive into EnrichedLead so the QA sheet can show it.
    fc = FakeClient()
    fc.match_by_name = {"Acme GmbH": {"company_id": "c1", "match_score": 0.95,
                                      "matched_name": "Acme GmbH", "name_agreement": 100}}
    fc.company_attrs = {"c1": {}}
    [r] = enrich_leads(fc, [_extracted(contact_name=None)], SETTINGS)
    assert r.name_agreement == 100


def test_enrich_matched_without_contact_name_skips_search():
    fc = FakeClient()
    fc.match_by_name = {"Acme GmbH": {"company_id": "c1", "match_score": 0.9, "matched_name": "Acme GmbH"}}
    fc.company_attrs = {"c1": {}}
    [r] = enrich_leads(fc, [_extracted(contact_name=None, first=None, last=None)], SETTINGS)
    assert r.matched is True
    assert r.contact_found is False
    assert not any(c[0] == "search" for c in fc.calls)


def test_enrich_no_company_match():
    fc = FakeClient()  # match_by_name empty -> match returns None
    [r] = enrich_leads(fc, [_extracted()], SETTINGS)
    assert r.matched is False
    assert r.company == {}
    assert r.contact_found is False
    assert not any(c[0] == "search" for c in fc.calls)
    assert not any(c[0] == "retrieve" for c in fc.calls)  # nothing matched -> no credit spent


def test_enrich_no_company_name_skips_match_entirely():
    fc = FakeClient()
    [r] = enrich_leads(fc, [_extracted(company=None, lead_company=None)], SETTINGS)
    assert r.matched is False
    assert not any(c[0] == "match_companies" for c in fc.calls)


def test_enrich_retrieves_all_matched_companies_in_one_call():
    fc = FakeClient()
    fc.match_by_name = {
        "Acme GmbH": {"company_id": "c1", "match_score": 0.9, "matched_name": "Acme GmbH"},
        "Beta AG": {"company_id": "c2", "match_score": 0.9, "matched_name": "Beta AG"},
    }
    fc.company_attrs = {"c1": {"url": "a.de"}, "c2": {"url": "b.de"}}
    e1 = _extracted(company="Acme GmbH", contact_name=None)
    e2 = _extracted(company="Beta AG", contact_name=None, lead_id="00Q2", task_id="T2")
    results = enrich_leads(fc, [e1, e2], SETTINGS)
    retrieves = [c for c in fc.calls if c[0] == "retrieve"]
    assert len(retrieves) == 1
    assert set(retrieves[0][1]) == {"c1", "c2"}
    assert results[0].company["Website"] == "a.de"
    assert results[1].company["Website"] == "b.de"


def test_enrich_uses_lead_field_fallbacks():
    fc = FakeClient()
    fc.match_by_name = {"Lead Co": {"company_id": "c1", "match_score": 0.9, "matched_name": "Lead Co"}}
    fc.company_attrs = {"c1": {}}
    fc.contact_by_name = {"John Smith": {"contact_id": "k1", "full_name": "John Smith"}}
    fc.contact_attrs = {"k1": {"position": {"title": "Mgr"}}}
    e = _extracted(company=None, lead_company="Lead Co", contact_name=None,
                   first="John", last="Smith", contact_email=None, lead_email="john@lead.co")
    [r] = enrich_leads(fc, [e], SETTINGS)
    assert ("match_companies", (("Lead Co", "john@lead.co", "Germany", "DE"),)) in fc.calls  # lead_company + lead_email
    assert ("search", "John Smith", "c1") in fc.calls                        # first+last fallback
    assert r.contact_found is True


def test_enrich_matches_all_companies_in_one_call():
    fc = FakeClient()
    fc.match_by_name = {
        "Acme GmbH": {"company_id": "c1", "match_score": 0.9, "matched_name": "Acme GmbH"},
        "Beta AG": {"company_id": "c2", "match_score": 0.9, "matched_name": "Beta AG"},
    }
    fc.company_attrs = {"c1": {}, "c2": {}}
    e1 = _extracted(company="Acme GmbH", contact_name=None)
    e2 = _extracted(company="Beta AG", contact_name=None, lead_id="00Q2", task_id="T2")
    results = enrich_leads(fc, [e1, e2], SETTINGS)
    match_calls = [c for c in fc.calls if c[0] == "match_companies"]
    assert len(match_calls) == 1
    assert {t[0] for t in match_calls[0][1]} == {"Acme GmbH", "Beta AG"}
    assert results[0].lf_company_id == "c1"
    assert results[1].lf_company_id == "c2"


def test_enrich_add_company_to_list_error_is_non_fatal():
    fc = FakeClient()
    fc.match_by_name = {"Acme GmbH": {"company_id": "c1", "match_score": 0.9, "matched_name": "Acme GmbH"}}
    fc.company_attrs = {"c1": {}}

    def boom(company_id, list_id):
        fc.calls.append(("add_company", company_id, list_id))
        raise requests.exceptions.HTTPError("boom")

    fc.add_company_to_list = boom
    [r] = enrich_leads(fc, [_extracted(contact_name=None)], SETTINGS)
    assert r.matched is True          # add-to-list error suppressed; lead still enriched
    assert r.lf_company_id == "c1"


def test_enrich_add_company_to_list_connection_error_is_non_fatal():
    # Not just HTTP errors: a dropped connection on a list-add must not sink the batch.
    fc = FakeClient()
    fc.match_by_name = {"Acme GmbH": {"company_id": "c1", "match_score": 0.9, "matched_name": "Acme GmbH"}}
    fc.company_attrs = {"c1": {}}

    def boom(company_id, list_id):
        raise requests.exceptions.ConnectionError("reset")

    fc.add_company_to_list = boom
    [r] = enrich_leads(fc, [_extracted(contact_name=None)], SETTINGS)
    assert r.matched is True


def test_enrich_search_contact_connection_error_is_non_fatal():
    fc = FakeClient()
    fc.match_by_name = {"Acme GmbH": {"company_id": "c1", "match_score": 0.9, "matched_name": "Acme GmbH"}}
    fc.company_attrs = {"c1": {}}

    def boom(name, company_id):
        raise requests.exceptions.ConnectionError("reset")

    fc.search_contact = boom
    [r] = enrich_leads(fc, [_extracted()], SETTINGS)
    assert r.matched is True           # company enrichment kept
    assert r.contact_found is False    # contact search failed soft


def test_enrich_retrieves_all_contacts_in_one_call():
    fc = FakeClient()
    fc.match_by_name = {
        "Acme GmbH": {"company_id": "c1", "match_score": 0.9, "matched_name": "Acme GmbH"},
        "Beta AG": {"company_id": "c2", "match_score": 0.9, "matched_name": "Beta AG"},
    }
    fc.company_attrs = {"c1": {}, "c2": {}}
    fc.contact_by_name = {
        "Jane Doe": {"contact_id": "k1", "full_name": "Jane Doe"},
        "John Roe": {"contact_id": "k2", "full_name": "John Roe"},
    }
    fc.contact_attrs = {"k1": {}, "k2": {}}
    e1 = _extracted(company="Acme GmbH", contact_name="Jane Doe")
    e2 = _extracted(company="Beta AG", contact_name="John Roe", lead_id="00Q2", task_id="T2")
    enrich_leads(fc, [e1, e2], SETTINGS)
    rc = [c for c in fc.calls if c[0] == "retrieve_contacts"]
    assert len(rc) == 1
    assert set(rc[0][1]) == {"k1", "k2"}


def test_enrich_add_contact_to_list_error_is_non_fatal():
    fc = FakeClient()
    fc.match_by_name = {"Acme GmbH": {"company_id": "c1", "match_score": 0.9, "matched_name": "Acme GmbH"}}
    fc.company_attrs = {"c1": {}}
    fc.contact_by_name = {"Jane Doe": {"contact_id": "k1", "full_name": "Jane Doe"}}
    fc.contact_attrs = {"k1": {}}

    def boom(contact_id, list_id):
        fc.calls.append(("add_contact", contact_id, list_id))
        raise requests.exceptions.HTTPError("boom")

    fc.add_contact_to_list = boom
    [r] = enrich_leads(fc, [_extracted()], SETTINGS)
    assert r.contact_found is True     # add-to-list error suppressed; contact still enriched


def test_enrich_add_contact_to_list_connection_error_is_non_fatal():
    fc = FakeClient()
    fc.match_by_name = {"Acme GmbH": {"company_id": "c1", "match_score": 0.9, "matched_name": "Acme GmbH"}}
    fc.company_attrs = {"c1": {}}
    fc.contact_by_name = {"Jane Doe": {"contact_id": "k1", "full_name": "Jane Doe"}}
    fc.contact_attrs = {"k1": {}}

    def boom(contact_id, list_id):
        raise requests.exceptions.ConnectionError("reset")

    fc.add_contact_to_list = boom
    [r] = enrich_leads(fc, [_extracted()], SETTINGS)
    assert r.contact_found is True


from leadauto.kununu_feed import ProfileInfo


def test_enrich_puts_assigned_companies_into_alt_names():
    fc = FakeClient()
    fc.match_by_name = {"Siemens": {"company_id": "c1", "match_score": 0.30,
                                    "matched_name": "Siemens Mobility GmbH",
                                    "name_agreement": 96,
                                    "agreed_via": "alt_name:Siemens Mobility GmbH"}}
    fc.company_attrs = {"c1": {}}
    captured = {}
    orig = fc.match_companies
    def spy(payloads):
        captured["payloads"] = payloads
        return orig(payloads)
    fc.match_companies = spy
    e = _extracted(company="Siemens", contact_name=None)
    e.kununu_profile_uuid = "u1"
    profiles = {"u1": ProfileInfo(name="Siemens",
                                  companies=["Siemens Mobility GmbH", "Siemens", "Siemens AG"])}
    [r] = enrich_leads(fc, [e], SETTINGS, profiles=profiles)
    # own name (normalized-equal) excluded; order preserved; agreed_via lands on the lead
    assert captured["payloads"][0]["alt_names"] == ["Siemens Mobility GmbH"]
    assert r.agreed_via == "alt_name:Siemens Mobility GmbH"


def test_enrich_no_profile_found_leaves_payload_unchanged():
    fc = FakeClient()
    fc.match_by_name = {"Acme GmbH": {"company_id": "c1", "match_score": 0.9,
                                      "matched_name": "Acme GmbH"}}
    fc.company_attrs = {"c1": {}}
    captured = {}
    orig = fc.match_companies
    def spy(payloads):
        captured["payloads"] = payloads
        return orig(payloads)
    fc.match_companies = spy
    [r] = enrich_leads(fc, [_extracted(contact_name=None)], SETTINGS, profiles={})
    assert "alt_names" not in captured["payloads"][0]
    assert r.agreed_via is None


def test_enrich_use_kununu_feed_false_skips_lookup(monkeypatch):
    from leadauto.leadfeeder import enrich as enrich_mod
    monkeypatch.setattr(enrich_mod, "load_profiles_for_leads",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("looked up")))
    fc = FakeClient()
    fc.match_by_name = {"Acme GmbH": {"company_id": "c1", "match_score": 0.9,
                                      "matched_name": "Acme GmbH"}}
    fc.company_attrs = {"c1": {}}
    [r] = enrich_leads(fc, [_extracted(contact_name=None)], SETTINGS, use_kununu_feed=False)
    assert r.matched is True
