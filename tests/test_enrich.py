"""enrich_leads orchestration tests.

A FakeClient stubs the LeadfeederClient methods so the real orchestration logic is
exercised: mapping ExtractedLead -> match args, matched/unmatched split, single batched
company retrieve, and contact assembly with lead-field fallbacks.
"""
import requests
from leadauto.config import Settings
from leadauto.extract.models import ExtractedLead
from leadauto.kununu_feed import ProfileInfo
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
    assert ("match_companies", (("Acme GmbH", "jane@acme.de", None, None),)) in fc.calls
    assert ("add_company", "c1", "company-list") in fc.calls
    assert ("add_contact", "k1", "contact-list") in fc.calls


def test_match_payload_omits_country_fields():
    # LF's country/country_code filters suppress good matches: on the 6-month control
    # set (2026-07-03 probe) 59 leads regained a candidate without them and ALL agreed
    # with the true converted account. The name-agreement gate guards wrong-company.
    class RawCapture(FakeClient):
        def match_companies(self, payloads):
            self.raw_payloads = payloads
            return super().match_companies(payloads)

    fc = RawCapture()
    enrich_leads(fc, [_extracted()], SETTINGS)
    [payload] = fc.raw_payloads
    assert "country" not in payload
    assert "country_code" not in payload


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
    assert ("match_companies", (("Lead Co", "john@lead.co", None, None),)) in fc.calls  # lead_company + lead_email
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


def test_alt_names_kept_on_enriched_lead():
    """EnrichedLead.alt_names preserves the assigned-company names from the kununu feed."""
    fc = FakeClient()
    fc.match_by_name = {"Siemens": {"company_id": "c1", "match_score": 0.30,
                                    "matched_name": "Siemens Mobility GmbH",
                                    "name_agreement": 96,
                                    "agreed_via": "alt_name:Siemens Mobility GmbH"}}
    fc.company_attrs = {"c1": {}}
    e = _extracted(company="Siemens", contact_name=None)
    e.kununu_profile_uuid = "u1"
    profiles = {"u1": ProfileInfo(name="Siemens",
                                  companies=["Siemens Mobility GmbH", "Siemens", "Siemens AG"])}
    [r] = enrich_leads(fc, [e], SETTINGS, profiles=profiles)
    # own name (normalized-equal) excluded; order preserved
    assert r.alt_names == ["Siemens Mobility GmbH"]


def test_alt_names_default_empty():
    """EnrichedLead.alt_names defaults to empty list."""
    r = EnrichedLead(extracted=_extracted())
    assert r.alt_names == []


# --- post-retrieve match sanity gate (country conflict + acronym guard) -------
# Live miss 2026-07-07 (00QMz00000mBy5FMAS): lead Company "SNG" (sng.ch, CH) was
# LF-matched to a company named "Sng" in Gabarret, FRANCE — the acronym made name
# agreement tautological, and the French firmographics (VAT FR..., address) were
# staged for write-back. company_summary carries no country/domain, so the gate
# runs after the phase-2 retrieve.

def _sng(company="SNG", email="andreas@sng.ch", cc="CH"):
    return _extracted(company=company, contact_name=None, contact_email=email,
                      region="Switzerland", cc=cc)


def test_country_conflict_rejects_match():
    fc = FakeClient()
    fc.match_by_name = {"SNG": {"company_id": "c1", "match_score": 0.9, "matched_name": "Sng"}}
    fc.company_attrs = {"c1": {"url": "https://www.sng.ch",
                               "address": {"country": "France"}}}
    [r] = enrich_leads(fc, [_sng()], SETTINGS)
    assert r.matched is False
    assert r.company == {}
    assert r.lf_rejected.startswith("country_conflict")
    assert not any(c[0] == "search" for c in fc.calls)  # no contact credit spent


def test_country_agreement_by_name_mapping_survives():
    # LF returns country names, the lead carries ISO2 — "Switzerland" must map to CH.
    fc = FakeClient()
    fc.match_by_name = {"SNG": {"company_id": "c1", "match_score": 0.9, "matched_name": "Sng"}}
    fc.company_attrs = {"c1": {"url": "https://www.sng.ch",
                               "address": {"country": "Switzerland"}}}
    [r] = enrich_leads(fc, [_sng()], SETTINGS)
    assert r.matched is True
    assert r.lf_rejected is None


def test_acronym_match_without_corroboration_rejected():
    # Same country, but nothing ties LF's "Sng" to the lead's sng.ch — acronym-only
    # name agreement is tautological (we searched on "SNG") and must not be trusted.
    fc = FakeClient()
    fc.match_by_name = {"SNG": {"company_id": "c1", "match_score": 0.9, "matched_name": "Sng"}}
    fc.company_attrs = {"c1": {"url": "https://elsewhere.ch",
                               "address": {"country": "CH"}}}
    [r] = enrich_leads(fc, [_sng()], SETTINGS)
    assert r.matched is False
    assert r.lf_rejected == "acronym_uncorroborated"


def test_acronym_match_corroborated_by_domain_survives():
    fc = FakeClient()
    fc.match_by_name = {"SNG": {"company_id": "c1", "match_score": 0.9, "matched_name": "SNG AG"}}
    fc.company_attrs = {"c1": {"url": "https://www.sng.ch/en",
                               "address": {"country": "CH"}}}
    [r] = enrich_leads(fc, [_sng()], SETTINGS)
    assert r.matched is True
    assert r.lf_rejected is None


def test_acronym_match_corroborated_by_vat_survives():
    fc = FakeClient()
    fc.match_by_name = {"SNG": {"company_id": "c1", "match_score": 0.9, "matched_name": "SNG AG"}}
    fc.company_attrs = {"c1": {"url": "https://elsewhere.ch", "vat_id": "CHE-165.603.061",
                               "address": {"country": "CH"}}}
    e = _sng(email="andreas@gmail.com")  # freemail: domain can't corroborate
    e.vat_number = "CHE165603061"
    [r] = enrich_leads(fc, [e], SETTINGS)
    assert r.matched is True


def test_four_letter_uppercase_acronym_guarded_but_not_ordinary_names():
    fc = FakeClient()
    fc.match_by_name = {
        "ADAC": {"company_id": "c1", "match_score": 0.9, "matched_name": "Adac"},
        "Acme GmbH": {"company_id": "c2", "match_score": 0.9, "matched_name": "Acme GmbH"},
    }
    fc.company_attrs = {"c1": {"url": "https://other.de", "address": {"country": "DE"}},
                        "c2": {"url": "https://unrelated.de", "address": {"country": "DE"}}}
    e1 = _extracted(company="ADAC", contact_name=None, contact_email="x@adac.de")
    e2 = _extracted(company="Acme GmbH", contact_name=None, lead_id="00Q2", task_id="T2")
    r1, r2 = enrich_leads(fc, [e1, e2], SETTINGS)
    assert r1.matched is False            # uppercase 4-letter acronym, uncorroborated
    assert r2.matched is True             # "Acme" is a name, not an acronym


def test_url_country_beats_defaulted_lead_countrycode():
    # 2026-07-10 closed-task dry run: old queue leads carry a defaulted
    # CountryCode=DE/Country=Germany, which wrongly rejected 37 correct AT/CH
    # matches. The kununu-URL-derived country is task evidence and must win.
    fc = FakeClient()
    fc.match_by_name = {"Robatech AG": {"company_id": "c1", "match_score": 0.9,
                                        "matched_name": "Robatech AG"}}
    fc.company_attrs = {"c1": {"url": "https://robatech.ch",
                               "address": {"country": "Switzerland"}}}
    e = _extracted(company="Robatech AG", contact_name=None,
                   region="Switzerland", cc="CH")
    e.lead_country_code = "DE"
    e.lead_country = "Germany"
    [r] = enrich_leads(fc, [e], SETTINGS)
    assert r.matched is True
    assert r.lf_rejected is None


def test_feed_country_beats_defaulted_lead_countrycode():
    # No URL-derived country, but the kununu feed's profile_country_code says CH:
    # it must outrank the Lead's defaulted CountryCode=DE, just like the URL country.
    fc = FakeClient()
    fc.match_by_name = {"Robatech AG": {"company_id": "c1", "match_score": 0.9,
                                        "matched_name": "Robatech AG"}}
    fc.company_attrs = {"c1": {"address": {"country": "Switzerland"}}}
    e = _extracted(company="Robatech AG", contact_name=None, region=None, cc=None)
    e.kununu_profile_uuid = "uuid-1"
    e.lead_country_code = "DE"
    profiles = {"uuid-1": ProfileInfo(name="Robatech", country_code="ch")}
    [r] = enrich_leads(fc, [e], SETTINGS, profiles=profiles)
    assert r.feed_country_code == "ch"
    assert r.matched is True
    assert r.lf_rejected is None


def test_url_country_beats_feed_country():
    # Both kununu sources present but disagreeing: the URL country (task evidence
    # for THIS claim) wins over the feed's profile-level country.
    fc = FakeClient()
    fc.match_by_name = {"Acme GmbH": {"company_id": "c1", "match_score": 0.9,
                                      "matched_name": "Acme GmbH"}}
    fc.company_attrs = {"c1": {"address": {"country": "Austria"}}}
    e = _extracted(contact_name=None, region="Austria", cc="AT")
    e.kununu_profile_uuid = "uuid-1"
    profiles = {"uuid-1": ProfileInfo(name="Acme", country_code="de")}
    [r] = enrich_leads(fc, [e], SETTINGS, profiles=profiles)
    assert r.matched is True
    assert r.lf_rejected is None


def test_lead_countrycode_still_used_without_url_country():
    # No kununu-URL country -> the Lead's CountryCode remains the gate's evidence.
    fc = FakeClient()
    fc.match_by_name = {"Acme GmbH": {"company_id": "c1", "match_score": 0.9,
                                      "matched_name": "Acme GmbH"}}
    fc.company_attrs = {"c1": {"address": {"country": "France"}}}
    e = _extracted(contact_name=None, cc=None, region=None)
    e.lead_country_code = "DE"
    [r] = enrich_leads(fc, [e], SETTINGS)
    assert r.matched is False
    assert r.lf_rejected == "country_conflict:FR!=DE"


def test_no_countries_no_rejection():
    # Fail open: without a country on either side the conflict check must not fire.
    fc = FakeClient()
    fc.match_by_name = {"Acme GmbH": {"company_id": "c1", "match_score": 0.9,
                                      "matched_name": "Acme GmbH"}}
    fc.company_attrs = {"c1": {}}
    e = _extracted(contact_name=None, cc=None, region=None)
    [r] = enrich_leads(fc, [e], SETTINGS)
    assert r.matched is True
