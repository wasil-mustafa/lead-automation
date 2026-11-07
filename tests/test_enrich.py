"""enrich_leads orchestration tests.

A FakeClient stubs the LeadfeederClient methods so the real orchestration logic is
exercised: mapping ExtractedLead -> match args, matched/unmatched split, single batched
company retrieve, and contact assembly with lead-field fallbacks.
"""
import requests
from leadauto.config import Settings
from leadauto.models import ExtractedLead
from leadauto.kununu_feed import ProfileInfo
from leadauto.leadfeeder.enrich import enrich_leads
from leadauto.models import EnrichedLead

SETTINGS = Settings.load()


class FakeClient:
    def __init__(self):
        self.match_by_name: dict = {}     # company_name -> match dict | None
        # Only visible at max_results > MATCH_RESULTS_PRIMARY: the candidates LF ranks
        # 4th-20th, which the second-chance pass is the only caller that ever sees.
        self.wide_match_by_name: dict = {}
        # Answers for the domain-rescue pass, keyed by the `url` it sends (the lead's
        # email domain). A rescue payload never falls through to the name tables.
        self.rescue_by_url: dict = {}
        # Answers for the url-ONLY third ask (payload flagged `url_only`), keyed by url.
        # Falls back to `rescue_by_url` so the older rescue tests see one answer twice.
        self.url_only_by_url: dict | None = None
        self.match_widths: list = []      # max_results of every match_companies call
        self.company_attrs: dict = {}     # lf_company_id -> raw attrs
        self.contact_by_name: dict = {}   # name -> {contact_id, full_name} | None
        self.contact_attrs: dict = {}     # contact_id -> raw LF attributes
        self.find_details_attrs: dict = {}  # attrs revealed by find_contact_data
        self.calls: list = []

    def get_account_id(self):
        self.calls.append(("account",))
        return "acc1"

    def find_or_create_list(self, name, scope):
        self.calls.append(("list", name, scope))
        return f"{scope}-list"

    def match_companies(self, payloads, max_results=3):
        self.calls.append(("match_companies", tuple(
            (p["company_name"], p.get("email"), p.get("country"), p.get("country_code"))
            + (("url_only",) if p.get("url_only") else ())
            for p in payloads)))
        self.match_widths.append(max_results)
        table = (self.match_by_name if max_results <= 3
                 else {**self.match_by_name, **self.wide_match_by_name})
        url_only = self.rescue_by_url if self.url_only_by_url is None else self.url_only_by_url
        return [(url_only if p.get("url_only") else self.rescue_by_url).get(p["url"])
                if p.get("url") else table.get(p["company_name"]) for p in payloads]

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

    def find_contact_data(self, contact_ids, timeout=180.0, poll_interval=10.0):
        self.calls.append(("find_contact_data", tuple(contact_ids)))
        # Simulates the async external-provider job: when attrs are staged in
        # find_details_attrs, they become visible to the next retrieve_contacts.
        if self.find_details_attrs:
            self.contact_attrs.update(self.find_details_attrs)
            return True
        return False


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
        def match_companies(self, payloads, max_results=3):
            self.raw_payloads = payloads
            return super().match_companies(payloads, max_results)

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
    # ...and the QA sheet must not claim LF "held no such person"
    from leadauto.models import CONTACT_SKIP_SEARCH_FAILED
    assert r.contact_skipped == CONTACT_SKIP_SEARCH_FAILED


def test_enrich_no_contact_name_is_named_as_the_skip_reason():
    from leadauto.models import CONTACT_SKIP_NO_NAME
    fc = FakeClient()
    fc.match_by_name = {"Acme GmbH": {"company_id": "c1", "match_score": 0.9, "matched_name": "Acme GmbH"}}
    fc.company_attrs = {"c1": {}}
    [r] = enrich_leads(fc, [_extracted(contact_name=None, first=None, last=None)], SETTINGS)
    assert r.contact_skipped == CONTACT_SKIP_NO_NAME
    # a lead LF was actually asked about keeps None (-> "not found in LeadFeeder")
    [asked] = enrich_leads(fc, [_extracted()], SETTINGS)
    assert asked.contact_skipped is None


def test_wide_pass_does_not_re_ask_a_lead_whose_primary_chunk_failed():
    # A failed primary chunk degrades to MATCH_CHUNK_FAILED, not None: those leads were
    # never judged at width 3, so re-asking them would stamp a hit `wide`, hold it out of
    # live scope and corrupt the wide-pass accuracy measurement.
    from leadauto.leadfeeder.client import MATCH_CHUNK_FAILED

    class FailingPrimary(FakeClient):
        def match_companies(self, payloads, max_results=3):
            self.calls.append(("match_companies", tuple(
                (p.get("company_name"), p.get("email"), None, None) for p in payloads)))
            self.match_widths.append(max_results)
            return [MATCH_CHUNK_FAILED] * len(payloads)

    fc = FailingPrimary()
    fc.wide_match_by_name = {"Acme GmbH": {"company_id": "c9", "match_score": 0.42,
                                           "matched_name": "Acme GmbH"}}
    [r] = enrich_leads(fc, [_extracted(contact_name=None)], _wide_settings(20))
    assert r.matched is False and r.match_pass == ""
    assert fc.match_widths == [3]                      # no second call at width 20


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
    def spy(payloads, max_results=3):
        captured["payloads"] = payloads
        return orig(payloads, max_results)
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
    def spy(payloads, max_results=3):
        captured["payloads"] = payloads
        return orig(payloads, max_results)
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


def test_enrich_carries_feed_industry_from_profile():
    # The kununu feed's profile_industry_name_de (ProfileInfo.industry) must land on
    # EnrichedLead.feed_industry for write-back to consume later.
    fc = FakeClient()
    e = _extracted(company="Acme GmbH", contact_name=None, region=None, cc=None)
    e.kununu_profile_uuid = "uuid-1"
    profiles = {"uuid-1": ProfileInfo(name="Acme", country_code="de", industry="IT")}
    [r] = enrich_leads(fc, [e], SETTINGS, profiles=profiles)
    assert r.feed_industry == "IT"


def test_enrich_carries_kununu_score_from_profile():
    # ProfileInfo.score (feed profile_score) must land on EnrichedLead.kununu_score —
    # it is the live-scope gate's input on non-dry runs.
    fc = FakeClient()
    e = _extracted(company="Acme GmbH", contact_name=None, region=None, cc=None)
    e.kununu_profile_uuid = "uuid-1"
    profiles = {"uuid-1": ProfileInfo(name="Acme", country_code="de", score=2.5)}
    [r] = enrich_leads(fc, [e], SETTINGS, profiles=profiles)
    assert r.kununu_score == 2.5


def test_enrich_kununu_score_none_without_profile():
    # No feed hit => no score => the live-scope gate fails closed downstream.
    fc = FakeClient()
    e = _extracted(company="Acme GmbH", contact_name=None, region=None, cc=None)
    [r] = enrich_leads(fc, [e], SETTINGS, profiles={})
    assert r.kununu_score is None


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


def test_find_contact_data_triggered_without_direct_dial():
    # Contact retrieved with only a mobile number -> phase 3b runs LF's async
    # 'Find contact details' and the re-retrieve picks up the revealed landline.
    fc = FakeClient()
    fc.match_by_name = {"Acme GmbH": {"company_id": "c1", "match_score": 0.9,
                                      "matched_name": "Acme GmbH"}}
    fc.company_attrs = {"c1": {}}
    fc.contact_by_name = {"Jane Doe": {"contact_id": "k1", "full_name": "Jane Doe"}}
    fc.contact_attrs = {"k1": {"phones": [{"number": "+49 170 111", "type": "mobile"}]}}
    fc.find_details_attrs = {"k1": {"phones": [
        {"number": "+49 170 111", "type": "mobile"},
        {"number": "+49 40 419131", "type": "landline"},
    ]}}
    [r] = enrich_leads(fc, [_extracted()], SETTINGS)
    assert ("find_contact_data", ("k1",)) in fc.calls
    assert r.contact["phone_office"] == "+49 40 419131"
    assert r.contact["contact_id"] == "k1"
    assert r.contact["full_name"] == "Jane Doe"


def test_find_contact_data_skipped_when_direct_dial_present():
    fc = FakeClient()
    fc.match_by_name = {"Acme GmbH": {"company_id": "c1", "match_score": 0.9,
                                      "matched_name": "Acme GmbH"}}
    fc.company_attrs = {"c1": {}}
    fc.contact_by_name = {"Jane Doe": {"contact_id": "k1", "full_name": "Jane Doe"}}
    fc.contact_attrs = {"k1": {"phones": [{"number": "+49 40 419131", "type": "landline"}]}}
    [r] = enrich_leads(fc, [_extracted()], SETTINGS)
    assert not any(c[0] == "find_contact_data" for c in fc.calls)


def test_find_contact_data_failure_degrades_gracefully():
    # Job returns False (nothing found / failed / timed out): keep what we had.
    fc = FakeClient()
    fc.match_by_name = {"Acme GmbH": {"company_id": "c1", "match_score": 0.9,
                                      "matched_name": "Acme GmbH"}}
    fc.company_attrs = {"c1": {}}
    fc.contact_by_name = {"Jane Doe": {"contact_id": "k1", "full_name": "Jane Doe"}}
    fc.contact_attrs = {"k1": {"position": {"title": "CEO"}}}
    [r] = enrich_leads(fc, [_extracted()], SETTINGS)
    assert ("find_contact_data", ("k1",)) in fc.calls
    assert r.contact_found is True
    assert r.contact["position"] == "CEO"


def test_no_countries_no_rejection():
    # Fail open: without a country on either side the conflict check must not fire.
    fc = FakeClient()
    fc.match_by_name = {"Acme GmbH": {"company_id": "c1", "match_score": 0.9,
                                      "matched_name": "Acme GmbH"}}
    fc.company_attrs = {"c1": {}}
    e = _extracted(contact_name=None, cc=None, region=None)
    [r] = enrich_leads(fc, [e], SETTINGS)
    assert r.matched is True


# --- the second-chance ("wide") match ----------------------------------------------
# Rescue-only by construction: `no_lf_match` is a TERMINAL skip (101 of 523 shadow
# predictions, 19.3%), and one cause is that the primary call sees only LF's top 3
# candidates while LF holds several records per company. What these tests pin is the
# asymmetry that makes it safe: a lead that already matched is never re-asked.

def _wide_settings(width):
    import copy
    raw = copy.deepcopy(SETTINGS.raw)
    raw["leadfeeder"]["match_wide_results"] = width
    # The domain rescue (on in the shipped config) also re-asks an unmatched lead
    # since 2026-11-06; off here so these tests see the wide pass alone.
    raw["leadfeeder"]["match_domain_rescue"] = False
    return Settings(raw=raw, leadfeeder_api_key=None, sf_auth_url=None)


def test_wide_pass_recovers_a_lead_the_primary_pass_missed():
    fc = FakeClient()
    fc.wide_match_by_name = {"Acme GmbH": {"company_id": "c9", "match_score": 0.42,
                                           "matched_name": "Acme GmbH",
                                           "name_agreement": 100,
                                           "agreed_via": "company_name"}}
    fc.company_attrs = {"c9": {}}
    [r] = enrich_leads(fc, [_extracted(contact_name=None)], _wide_settings(20))
    assert r.matched is True
    assert r.lf_company_id == "c9"
    assert r.match_pass == "wide"
    assert fc.match_widths == [3, 20]


def test_a_primary_match_is_never_re_asked():
    # The whole safety argument: widening cannot move a working match if the working
    # match is never sent again. One call, at the calibrated width.
    fc = FakeClient()
    fc.match_by_name = {"Acme GmbH": {"company_id": "c1", "match_score": 0.95,
                                      "matched_name": "Acme GmbH"}}
    fc.wide_match_by_name = {"Acme GmbH": {"company_id": "c-other", "match_score": 0.99,
                                           "matched_name": "Acme Holding AG"}}
    fc.company_attrs = {"c1": {}, "c-other": {}}
    [r] = enrich_leads(fc, [_extracted(contact_name=None)], _wide_settings(20))
    assert r.lf_company_id == "c1"
    assert r.match_pass == "primary"
    assert fc.match_widths == [3]


def test_wide_pass_re_asks_only_the_unmatched_payloads():
    fc = FakeClient()
    fc.match_by_name = {"Acme GmbH": {"company_id": "c1", "match_score": 0.95,
                                      "matched_name": "Acme GmbH"}}
    fc.wide_match_by_name = {"Nobody GmbH": {"company_id": "c2", "match_score": 0.3,
                                             "matched_name": "Nobody GmbH"}}
    fc.company_attrs = {"c1": {}, "c2": {}}
    leads = [_extracted(contact_name=None),
             _extracted(company="Nobody GmbH", contact_name=None, contact_email=None,
                        lead_id="00Q2", task_id="T2")]
    hit, rescued = enrich_leads(fc, leads, _wide_settings(20))
    assert (hit.match_pass, rescued.match_pass) == ("primary", "wide")
    assert fc.match_widths == [3, 20]
    # The second call carries the unmatched company ONLY.
    match_calls = [c for c in fc.calls if c[0] == "match_companies"]
    assert len(match_calls) == 2
    assert [n for n, *_ in match_calls[1][1]] == ["Nobody GmbH"]


def test_the_shipped_default_is_disabled():
    # Measured 0 recoveries on 103 leads (2026-08-25 backtest), so the live path does not
    # spend the call. This test is what makes flipping it back a deliberate act.
    assert SETTINGS.lf_match_wide_results == 0


def test_wide_pass_disabled_by_config():
    fc = FakeClient()
    fc.wide_match_by_name = {"Acme GmbH": {"company_id": "c9", "match_score": 0.42,
                                           "matched_name": "Acme GmbH"}}
    fc.company_attrs = {"c9": {}}
    [r] = enrich_leads(fc, [_extracted(contact_name=None)], _wide_settings(0))
    assert r.matched is False
    assert r.match_pass == ""
    assert fc.match_widths == [3]
    assert not any(c[0] == "retrieve" for c in fc.calls)   # no match -> no credit spent


def test_a_wide_match_still_faces_the_sanity_gate():
    # A rescued match is a weaker claim, so it must not bypass the post-retrieve gate:
    # the country conflict degrades it back to unmatched exactly as for a primary match.
    fc = FakeClient()
    fc.wide_match_by_name = {"Acme GmbH": {"company_id": "c9", "match_score": 0.42,
                                           "matched_name": "Acme GmbH"}}
    fc.company_attrs = {"c9": {"address": {"country": "France"}}}
    [r] = enrich_leads(fc, [_extracted(contact_name=None)], _wide_settings(20))
    assert r.matched is False
    assert r.lf_rejected == "country_conflict:FR!=DE"
    assert r.match_pass == "wide"   # kept as evidence of which pass proposed it


# --- contact lookup only when needed (Wasil, 2026-09-18) -----------------------------
# The cascade feeds Lead.Email and Lead.Phone, both fill-only-blank. A lead that already
# carries both would pay a retrieve credit for values the write-back never writes.

def _lookup_settings(only_needed: bool):
    import copy
    raw = copy.deepcopy(SETTINGS.raw)
    raw["leadfeeder"]["contact_lookup_only_when_needed"] = only_needed
    return Settings(raw=raw, leadfeeder_api_key=None, sf_auth_url=None)


def _matched_client():
    fc = FakeClient()
    fc.match_by_name = {"Acme GmbH": {"company_id": "c1", "match_score": 0.9,
                                      "matched_name": "Acme GmbH"}}
    fc.company_attrs = {"c1": {}}
    fc.contact_by_name = {"Jane Doe": {"contact_id": "k1", "full_name": "Jane Doe"}}
    fc.contact_attrs = {"k1": {"emails": [{"address": "jane@acme.de"}],
                              "phones": [{"number": "+49 170 111", "type": "mobile"}]}}
    return fc


def _event_lead(lead_email=None, lead_phone=None):
    # Event badge scans carry no Task email; the person's data lives on the Lead.
    return ExtractedLead(lead_id="00Q1", task_id="T1", task_type="event",
                         company_name="Acme GmbH", contact_name="Jane Doe",
                         contact_email=None, lead_email=lead_email, lead_phone=lead_phone)


def test_the_shipped_default_looks_every_matched_lead_up():
    assert SETTINGS.lf_contact_lookup_only_when_needed is False


def test_lookup_skipped_when_lead_has_email_and_phone():
    fc = _matched_client()
    [r] = enrich_leads(fc, [_event_lead("jane@acme.de", "+49 40 1")], _lookup_settings(True))
    assert r.matched is True
    assert r.contact_found is False
    assert r.contact_skipped == "lead already has email and phone"
    assert not any(c[0] in ("search", "retrieve_contacts", "find_contact_data")
                   for c in fc.calls)


def test_lookup_runs_when_lead_lacks_phone():
    fc = _matched_client()
    [r] = enrich_leads(fc, [_event_lead("jane@acme.de", None)], _lookup_settings(True))
    assert r.contact_found is True
    assert r.contact_skipped is None
    assert ("search", "Jane Doe", "c1") in fc.calls
    # mobile only -> the phone-finding job runs, because the lead still needs a phone
    assert ("find_contact_data", ("k1",)) in fc.calls


def test_lookup_runs_when_lead_lacks_email_but_no_phone_job_when_it_has_a_phone():
    fc = _matched_client()
    [r] = enrich_leads(fc, [_event_lead(None, "+49 40 1")], _lookup_settings(True))
    assert r.contact_found is True
    assert r.contact["contact_emails"] == "jane@acme.de"
    # the lead already has a phone: no credit on the async phone lookup
    assert not any(c[0] == "find_contact_data" for c in fc.calls)


def test_task_email_counts_as_having_an_email():
    # A claimer/trial Task email always lands on Lead.Email, so only the phone can
    # still be missing.
    fc = _matched_client()
    e = _extracted(contact_email="jane@acme.de", lead_email=None)
    e.lead_phone = "+49 40 1"
    [r] = enrich_leads(fc, [e], _lookup_settings(True))
    assert r.contact_skipped == "lead already has email and phone"


def test_gate_off_keeps_looking_up_fully_enriched_leads():
    fc = _matched_client()
    [r] = enrich_leads(fc, [_event_lead("jane@acme.de", "+49 40 1")], _lookup_settings(False))
    assert r.contact_found is True
    assert r.contact_skipped is None
    assert ("find_contact_data", ("k1",)) in fc.calls


# --- domain rescue (2026-11-05) ------------------------------------------------------
# Lead 00QMz00000rhynBMAQ "Baustein GmbH" (kundenservice@baustein.com, kununu /de/):
# name+email returned ONLY LF's Austrian "Baustein GmbH", which the sanity gate rightly
# rejected, while LF also held the German company whose website IS baustein.com.
# What these pin: the pass runs ONLY for country_conflict rejections, sends the email
# domain as `url` and never `country_code`, accepts only when LF's website is that
# domain, and a rescued company still faces the retrieve + sanity gate.

from leadauto.leadfeeder.enrich import (MATCH_PASS_DOMAIN, build_match_payload,
                                        build_rescue_payload, rescue_accepts)


def _rescue_settings(on: bool):
    import copy
    raw = copy.deepcopy(SETTINGS.raw)
    raw["leadfeeder"]["match_domain_rescue"] = on
    return Settings(raw=raw, leadfeeder_api_key=None, sf_auth_url=None)


def _baustein(email="kundenservice@baustein.com"):
    return _extracted(company="Baustein GmbH", contact_name=None, contact_email=email,
                      region="Germany", cc="DE")


def _conflict_client():
    """Primary match = the Austrian namesake (rejected AT != DE); rescue table empty."""
    fc = FakeClient()
    fc.match_by_name = {"Baustein GmbH": {"company_id": "at1", "match_score": 0.89,
                                          "matched_name": "Baustein GmbH",
                                          "matched_url": "https://www.baustein-gmbh.at/"}}
    fc.company_attrs = {"at1": {"url": "https://www.baustein-gmbh.at/",
                                "address": {"country": "Austria"}}}
    return fc


def test_rescue_payload_is_name_plus_email_domain_and_nothing_else():
    e = _baustein()
    rp = build_rescue_payload(e, {"company_name": "Baustein GmbH",
                                  "email": "kundenservice@baustein.com",
                                  "alt_names": ["Baustein"]})
    assert rp == {"company_name": "Baustein GmbH", "url": "baustein.com",
                  "alt_names": ["Baustein"]}
    assert "email" not in rp and "country_code" not in rp and "country" not in rp


def test_rescue_payload_refuses_freemail_and_no_payload():
    assert build_rescue_payload(_baustein("someone@gmail.com"),
                                {"company_name": "Baustein GmbH"}) is None
    assert build_rescue_payload(_baustein(), None) is None
    assert build_rescue_payload(_extracted(contact_email=None, lead_email=None,
                                           contact_name=None),
                                {"company_name": "Acme GmbH"}) is None


def test_rescue_accepts_only_when_lf_website_is_the_lead_domain():
    assert rescue_accepts({"company_id": "de1", "matched_url": "https://www.baustein.com"},
                          "baustein.com") is True
    assert rescue_accepts({"company_id": "de1", "matched_url": "https://shop.baustein.com/x"},
                          "baustein.com") is True       # subdomain-tolerant
    assert rescue_accepts({"company_id": "x", "matched_url": "https://baustein-gmbh.de/"},
                          "baustein.com") is False
    assert rescue_accepts({"company_id": "x"}, "baustein.com") is False   # no website: closed
    assert rescue_accepts(None, "baustein.com") is False


def test_domain_rescue_recovers_the_right_namesake():
    fc = _conflict_client()
    fc.rescue_by_url = {"baustein.com": {"company_id": "de1", "match_score": 0.88,
                                         "matched_name": "Baustein GmbH",
                                         "name_agreement": 100,
                                         "agreed_via": "company_name",
                                         "matched_url": "https://www.baustein.com"}}
    fc.company_attrs["de1"] = {"url": "https://www.baustein.com",
                               "address": {"country": "Germany", "city": "Berlin"}}
    [r] = enrich_leads(fc, [_baustein()], _rescue_settings(True))
    assert r.matched is True
    assert r.lf_company_id == "de1"
    assert r.match_pass == MATCH_PASS_DOMAIN
    assert r.lf_rejected is None
    assert r.lf_rescue == "recovered"
    assert r.company["City"] == "Berlin"
    # the rescue asked ONCE, at the primary width, with url and WITHOUT country_code
    rescue_calls = [c for c in fc.calls if c[0] == "match_companies"]
    assert len(rescue_calls) == 2 and fc.match_widths == [3, 3]
    assert rescue_calls[1] == ("match_companies", (("Baustein GmbH", None, None, None),))
    # two retrieves: the rejected primary company, then the rescued one
    assert [c for c in fc.calls if c[0] == "retrieve"] == [("retrieve", ("at1",)),
                                                             ("retrieve", ("de1",))]


def test_domain_rescue_refuses_a_candidate_whose_website_is_not_the_domain():
    fc = _conflict_client()
    fc.rescue_by_url = {"baustein.com": {"company_id": "de2", "match_score": 0.9,
                                         "matched_name": "BauStein GmbH",
                                         "matched_url": "https://baustein-gmbh.de/"}}
    [r] = enrich_leads(fc, [_baustein()], _rescue_settings(True))
    assert r.matched is False
    assert r.lf_rejected == "country_conflict:AT!=DE"      # the original reason stands
    assert r.lf_rescue == "url_mismatch:baustein-gmbh.de"
    assert r.match_pass == "primary"
    assert ("retrieve", ("de2",)) not in fc.calls          # no credit on a refused one


def test_domain_rescue_no_match_keeps_the_rejection():
    fc = _conflict_client()
    [r] = enrich_leads(fc, [_baustein()], _rescue_settings(True))
    assert r.matched is False
    assert r.lf_rejected == "country_conflict:AT!=DE"
    assert r.lf_rescue == "no_match"


def test_domain_rescue_skips_freemail_leads_without_asking():
    fc = _conflict_client()
    [r] = enrich_leads(fc, [_baustein("nancy@gmx.de")], _rescue_settings(True))
    assert r.matched is False
    assert r.lf_rescue == "no_corporate_domain"
    assert fc.match_widths == [3]                           # no second match call


def test_domain_rescue_never_touches_an_acronym_rejection():
    fc = FakeClient()
    fc.match_by_name = {"SNG": {"company_id": "c1", "match_score": 0.9, "matched_name": "Sng"}}
    fc.company_attrs = {"c1": {"url": "https://elsewhere.ch", "address": {"country": "CH"}}}
    fc.rescue_by_url = {"sng.ch": {"company_id": "c2", "matched_name": "SNG AG",
                                   "matched_url": "https://sng.ch"}}
    [r] = enrich_leads(fc, [_sng()], _rescue_settings(True))
    assert r.matched is False
    assert r.lf_rejected == "acronym_uncorroborated"
    assert r.lf_rescue is None
    assert fc.match_widths == [3]


def test_a_rescued_company_still_faces_the_sanity_gate():
    # A .com domain registered abroad: the website agrees, the retrieved country does
    # not — rejected again, never trusted on the domain alone.
    fc = _conflict_client()
    fc.rescue_by_url = {"baustein.com": {"company_id": "ch1", "match_score": 0.88,
                                         "matched_name": "Baustein GmbH",
                                         "matched_url": "https://www.baustein.com"}}
    fc.company_attrs["ch1"] = {"url": "https://www.baustein.com",
                               "address": {"country": "Switzerland"}}
    [r] = enrich_leads(fc, [_baustein()], _rescue_settings(True))
    assert r.matched is False
    assert r.company == {}
    assert r.lf_rejected == "country_conflict:CH!=DE"
    assert r.lf_rescue == "rejected_again:country_conflict:CH!=DE"
    assert r.match_pass == MATCH_PASS_DOMAIN           # evidence of which pass proposed it
    assert not any(c[0] == "search" for c in fc.calls)  # no contact credit


def test_domain_rescue_disabled_by_config():
    fc = _conflict_client()
    fc.rescue_by_url = {"baustein.com": {"company_id": "de1", "matched_name": "Baustein GmbH",
                                         "matched_url": "https://www.baustein.com"}}
    [r] = enrich_leads(fc, [_baustein()], _rescue_settings(False))
    assert r.matched is False
    assert r.lf_rescue is None
    assert fc.match_widths == [3]


def test_domain_rescue_is_on_in_the_shipped_config():
    assert SETTINGS.lf_match_domain_rescue is True


def test_domain_rescue_is_off_when_the_key_is_absent():
    import copy
    raw = copy.deepcopy(SETTINGS.raw)
    raw["leadfeeder"].pop("match_domain_rescue", None)
    assert Settings(raw=raw, leadfeeder_api_key=None,
                    sf_auth_url=None).lf_match_domain_rescue is False


def test_domain_rescue_refuses_a_foreign_hit_before_paying_for_it():
    # Backtest 2026-11-05: 7 of 22 domain hits sat in another country per the match
    # summary (a .com registered abroad, a subsidiary on the group domain). The sanity
    # gate would reject them after the retrieve, so they are refused BEFORE it.
    fc = _conflict_client()
    fc.rescue_by_url = {"baustein.com": {"company_id": "ch1", "match_score": 0.88,
                                         "matched_name": "Baustein GmbH",
                                         "matched_url": "https://www.baustein.com",
                                         "matched_country_code": "CH"}}
    [r] = enrich_leads(fc, [_baustein()], _rescue_settings(True))
    assert r.matched is False
    assert r.lf_rejected == "country_conflict:AT!=DE"
    assert r.lf_rescue == "foreign:CH!=DE"
    assert r.match_pass == "primary"
    assert ("retrieve", ("ch1",)) not in fc.calls          # no credit spent


# --- domain rescue of a lead with NO candidate (2026-11-06, PAU-Söhne) ---------------------

def _pau():
    return _extracted(company="PAU-Söhne Gesellschaft mit beschränkter Haftung",
                      contact_name=None, contact_email="s.pau@pau-maschinenbau.de",
                      region="Germany", cc="DE")


PAU_HIT = {"company_id": "1231563", "match_score": 0.8, "matched_name": "PAU-Söhne GmbH",
           "name_agreement": 100, "agreed_via": "company_name",
           "matched_url": "https://pau-maschinenbau.de", "matched_country_code": "DE"}


def test_the_name_sent_to_lf_carries_the_abbreviated_legal_form():
    # LF returned 0 candidates for the long form and the right company for "GmbH"
    # (measured 2026-11-06); the gate normalizes both the same, so only the ask changes.
    payload, _ = build_match_payload(_pau(), {})
    assert payload["company_name"] == "PAU-Söhne GmbH"


def test_domain_rescue_recovers_a_lead_lf_had_no_candidate_for():
    fc = FakeClient()                                   # name table empty: no candidate
    fc.rescue_by_url = {"pau-maschinenbau.de": PAU_HIT}
    fc.company_attrs = {"1231563": {"url": "https://pau-maschinenbau.de",
                                    "address": {"country": "Germany", "city": "Lauingen"}}}
    [r] = enrich_leads(fc, [_pau()], _rescue_settings(True))
    assert r.matched is True
    assert r.lf_company_id == "1231563"
    assert r.match_pass == MATCH_PASS_DOMAIN
    assert r.lf_rescue == "recovered_unmatched"         # scored apart from the conflict arm
    assert r.lf_rejected is None
    assert r.company["City"] == "Lauingen"
    assert fc.match_widths == [3, 3]
    assert [c for c in fc.calls if c[0] == "retrieve"] == [("retrieve", ("1231563",))]


def test_domain_rescue_refuses_an_unmatched_lead_whose_hit_is_another_website():
    fc = FakeClient()
    fc.rescue_by_url = {"pau-maschinenbau.de": {**PAU_HIT, "company_id": "x9",
                                                "matched_url": "https://pau-soehne.at"}}
    [r] = enrich_leads(fc, [_pau()], _rescue_settings(True))
    assert r.matched is False
    assert r.lf_rescue == "url_mismatch:pau-soehne.at"
    assert r.match_pass == ""
    assert not any(c[0] == "retrieve" for c in fc.calls)


def test_domain_rescue_of_an_unmatched_lead_needs_a_corporate_domain():
    fc = FakeClient()
    fc.rescue_by_url = {"gmx.de": PAU_HIT}             # would hit, must never be asked
    [r] = enrich_leads(fc, [_extracted(contact_name=None, contact_email="x@gmx.de")],
                       _rescue_settings(True))
    assert r.matched is False
    assert r.lf_rescue == "no_corporate_domain"
    assert fc.match_widths == [3]


def test_domain_rescue_never_re_asks_a_lead_whose_primary_chunk_failed():
    # MATCH_CHUNK_FAILED = never judged. Re-asking it would label a hit "domain" and
    # hold it out of live scope for a reason that is not true of it.
    from leadauto.leadfeeder.client import MATCH_CHUNK_FAILED

    class ChunkFails(FakeClient):
        def match_companies(self, payloads, max_results=3):
            self.match_widths.append(max_results)
            return [MATCH_CHUNK_FAILED] * len(payloads)

    fc = ChunkFails()
    fc.rescue_by_url = {"pau-maschinenbau.de": PAU_HIT}
    [r] = enrich_leads(fc, [_pau()], _rescue_settings(True))
    assert r.matched is False
    assert r.lf_rescue is None
    assert fc.match_widths == [3]


def test_domain_rescue_off_leaves_an_unmatched_lead_unmatched():
    fc = FakeClient()
    fc.rescue_by_url = {"pau-maschinenbau.de": PAU_HIT}
    [r] = enrich_leads(fc, [_pau()], _rescue_settings(False))
    assert r.matched is False and r.lf_rescue is None
    assert fc.match_widths == [3]


# --- url-only third ask (2026-11-07, IWK / KRANZ from Wasil's hand review) ------------------

from leadauto.leadfeeder.enrich import build_url_only_payload  # noqa: E402


def _iwk():
    return _extracted(company="IWK Communication Partner", contact_name=None,
                      contact_email="steffi.sorg@iwk-cp.com", region="Germany", cc="DE")


IWK_KR = {"company_id": "195895839", "match_score": 0.17, "matched_name": "IWK",
          "name_agreement": 100, "agreed_via": "company_name",
          "matched_url": "http://www.iwkco.com", "matched_country_code": "KR"}
IWK_DE = {"company_id": "2397461", "match_score": 0.8, "matched_name": "IWK GmbH",
          "name_agreement": 100, "agreed_via": "company_name",
          "matched_url": "https://www.iwk-cp.com", "matched_country_code": "DE"}


def test_url_only_payload_keeps_the_name_local_and_sends_the_url_alone():
    from leadauto.leadfeeder.client import match_api_payload
    rp = build_rescue_payload(_iwk(), {"company_name": "IWK Communication Partner",
                                       "email": "steffi.sorg@iwk-cp.com",
                                       "lead_country_code": "DE"})
    assert rp == {"company_name": "IWK Communication Partner", "url": "iwk-cp.com",
                  "lead_country_code": "DE"}
    uo = build_url_only_payload(rp)
    assert uo["company_name"] == "IWK Communication Partner"   # the local gate's input
    assert match_api_payload(uo) == {"url": "iwk-cp.com"}       # what LF receives


def test_url_only_ask_recovers_the_domain_owner_name_plus_url_ranked_abroad():
    # Measured 2026-11-07: name+email and name+url both return only a Korean "IWK";
    # url alone returns "IWK GmbH" on iwk-cp.com (agreement 100, DE).
    fc = FakeClient()                                   # primary: no candidate
    fc.rescue_by_url = {"iwk-cp.com": IWK_KR}           # name+url: the Korean namesake
    fc.url_only_by_url = {"iwk-cp.com": IWK_DE}
    fc.company_attrs = {"2397461": {"url": "https://www.iwk-cp.com",
                                    "address": {"country": "Germany", "city": "Bonn"}}}
    [r] = enrich_leads(fc, [_iwk()], _rescue_settings(True))
    assert r.matched is True
    assert r.lf_company_id == "2397461"
    assert r.match_pass == MATCH_PASS_DOMAIN
    assert r.lf_rescue == "url_only_recovered_unmatched"
    assert r.company["City"] == "Bonn"
    assert fc.match_widths == [3, 3, 3]
    asks = [c for c in fc.calls if c[0] == "match_companies"]
    assert asks[2] == ("match_companies",
                       (("IWK Communication Partner", None, None, None, "url_only"),))
    assert [c for c in fc.calls if c[0] == "retrieve"] == [("retrieve", ("2397461",))]


def test_url_only_ask_is_skipped_when_name_plus_url_already_recovered():
    fc = FakeClient()
    fc.rescue_by_url = {"iwk-cp.com": IWK_DE}
    fc.url_only_by_url = {"iwk-cp.com": {**IWK_DE, "company_id": "other"}}
    fc.company_attrs = {"2397461": {"url": "https://www.iwk-cp.com",
                                    "address": {"country": "Germany"}}}
    [r] = enrich_leads(fc, [_iwk()], _rescue_settings(True))
    assert r.lf_company_id == "2397461" and r.lf_rescue == "recovered_unmatched"
    assert fc.match_widths == [3, 3]


def test_url_only_ask_still_refuses_a_stranger_who_owns_the_domain():
    # KRANZ Agentur / kranz.life is owned by "koelnistkool GmbH" (agreement 40): the
    # name gate inside _best_match yields None, so the lead stays unmatched and the
    # label records both refusals.
    fc = FakeClient()
    kranz_es = {"company_id": "203811039", "match_score": 0.58, "matched_name": "Kranz",
                "name_agreement": 100, "agreed_via": "company_name",
                "matched_url": "https://www.kranz.es", "matched_country_code": "ES"}
    fc.rescue_by_url = {"kranz.life": kranz_es}
    fc.url_only_by_url = {}                              # gated out -> None
    [r] = enrich_leads(fc, [_extracted(company="KRANZ Agentur", contact_name=None,
                                       contact_email="job@kranz.life")],
                       _rescue_settings(True))
    assert r.matched is False
    assert r.lf_rescue == "url_mismatch:kranz.es|url_only_no_match"
    assert fc.match_widths == [3, 3, 3]
    assert not any(c[0] == "retrieve" for c in fc.calls)


def test_url_only_ask_with_the_same_refusal_keeps_the_first_label():
    fc = _conflict_client()
    foreign = {"company_id": "ch1", "match_score": 0.88, "matched_name": "Baustein GmbH",
               "matched_url": "https://www.baustein.com", "matched_country_code": "CH"}
    fc.rescue_by_url = {"baustein.com": foreign}
    fc.url_only_by_url = {"baustein.com": foreign}
    [r] = enrich_leads(fc, [_baustein()], _rescue_settings(True))
    assert r.matched is False
    assert r.lf_rescue == "foreign:CH!=DE"
    assert fc.match_widths == [3, 3, 3]
