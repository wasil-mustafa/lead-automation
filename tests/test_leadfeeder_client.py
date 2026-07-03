"""LeadfeederClient tests.

The pure flatteners (flatten_company / flatten_contact_details) are tested directly with
canned API JSON — that's where the field-extraction bugs live. The HTTP-orchestrating
methods (match, contact cascade, batch retrieve) are tested through a FakeSession that
stands in for `requests.Session` (the unavoidable external boundary) and records calls.
"""
import pytest

from leadauto.leadfeeder.client import (
    LeadfeederClient,
    LeadfeederError,
    flatten_company,
    flatten_contact_details,
)


class FakeResponse:
    def __init__(self, payload, status=200, headers=None):
        self._payload = payload
        self.status_code = status
        self.headers = headers or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.exceptions.HTTPError(f"HTTP {self.status_code}", response=self)


class FakeSession:
    """Routes by (method, url-fragment). Values may be a payload dict, a queue (list,
    popped per call), or a callable(params, body)->payload. Longest fragment wins."""

    def __init__(self, routes):
        self._routes = routes
        self.calls = []  # (method, url, params, body)

    def get(self, url, headers=None, params=None):
        return self._respond("GET", url, params, None)

    def post(self, url, headers=None, json=None, params=None):
        return self._respond("POST", url, params, json)

    def _respond(self, method, url, params, body):
        self.calls.append((method, url, params, body))
        candidates = sorted(
            (r for r in self._routes if r[0] == method and r[1] in url),
            key=lambda r: len(r[1]), reverse=True,
        )
        if not candidates:
            return FakeResponse({"data": []})
        value = self._routes[candidates[0]]
        if callable(value):
            value = value(params, body)
        elif isinstance(value, list):
            value = value.pop(0) if value else {"data": []}
        return value if isinstance(value, FakeResponse) else FakeResponse(value)

    def posts_to(self, fragment):
        return [c for c in self.calls if c[0] == "POST" and fragment in c[1]]


def _client(routes, seed_account=True, **kw):
    kw.setdefault("max_requests_per_min", 0)    # throttle off unless a test opts in
    kw.setdefault("sleep_fn", lambda *_: None)   # never actually sleep in tests
    client = LeadfeederClient(api_key="k", session=FakeSession(routes), **kw)
    if seed_account:
        client._account_id = "acc1"  # skip the /accounts round-trip for focused tests
    return client


# --- construction ------------------------------------------------------------

def test_missing_api_key_raises():
    with pytest.raises(LeadfeederError):
        LeadfeederClient(api_key="", session=FakeSession({}))


# --- pure: flatten_company ---------------------------------------------------

def test_flatten_company_extracts_nested_fields():
    attrs = {
        "url": "https://acme.de",
        "phones": [{"number": "+49 30 123"}],
        "emails": [{"address": "info@acme.de"}],
        "address": {"street_address": "Hauptstr 1", "postal_code": "10115",
                    "city": "Berlin", "country": "Germany"},
        "employee_count": 42, "employee_range": "11-50",
        "industries": {"industry": [{"name": "Software"}, {"name": "IT"}]},
        "revenue": {"value": 5, "currency": "EUR"},
        "vat_id": "DE123456789",
        "register": {"id": "HRB 1234", "status": "active"},
        "social_media_profiles": {"linkedin": [{"url": "https://linkedin.com/company/acme"}]},
        "description": "Line one\nLine two",
    }
    out = flatten_company(attrs)
    assert out["Website"] == "https://acme.de"
    assert out["Company_Phone"] == "+49 30 123"
    assert out["Company_Email"] == "info@acme.de"
    assert out["Street"] == "Hauptstr 1"
    assert out["Postal_Code"] == "10115"
    assert out["City"] == "Berlin"
    assert out["Industry"] == "Software; IT"
    assert out["VAT_Id"] == "DE123456789"
    assert out["Register_Id"] == "HRB 1234"
    assert out["Company_LinkedIn"] == "https://linkedin.com/company/acme"
    assert "\n" not in out["Description"] and out["Description"] == "Line one Line two"


def test_flatten_company_tolerates_empty_attrs():
    out = flatten_company({})
    assert out["Website"] == "" and out["VAT_Id"] == "" and out["Industry"] == ""


# --- pure: flatten_contact_details -------------------------------------------

def test_flatten_contact_prioritizes_direct_over_mobile_and_office():
    attrs = {
        "position": {"title": "CEO"},
        "emails": [{"address": "a@x.de"}, {"address": "b@x.de"}],
        "phones": [
            {"number": "111", "type": "Office"},
            {"number": "222", "type": "Mobile"},
            {"number": "333", "type": "Direct Dial"},
            {"number": "999", "type": "Fax"},                 # excluded
            {"number": "000", "type": "Mobile", "blacklisted": True},  # excluded
        ],
        "social_media_profiles": {
            "linkedin": [{"url": "https://linkedin.com/in/x"}],
            "xing": [{"url": "https://xing.com/x"}],
        },
        "departments": ["Sales", "Management"],
        "hierarchy_level": "c_level",
    }
    out = flatten_contact_details(attrs)
    assert out["position"] == "CEO"
    assert out["contact_emails"] == "a@x.de; b@x.de"
    assert out["phone_direct"] == "333"
    assert out["phone_mobile"] == "222"
    assert out["phone_office"] == "111"
    assert out["best_contact_phone"] == "333"          # direct wins
    assert "999" not in out["phones_all"]               # fax dropped
    assert "000" not in out["phones_all"]               # blacklisted dropped
    assert out["contact_linkedin"] == "https://linkedin.com/in/x"
    assert out["contact_xing"] == "https://xing.com/x"
    assert out["departments"] == "Sales; Management"
    assert out["hierarchy_level"] == "c_level"


def test_flatten_contact_falls_back_to_first_phone_when_no_typed_match():
    attrs = {"phones": [{"number": "555", "type": "switchboard"}]}
    out = flatten_contact_details(attrs)
    assert out["best_contact_phone"] == "555"


# --- match_company -----------------------------------------------------------

def _match_payload(matches):
    return {"data": [[
        {"id": m[0], "attributes": {"match_score": m[1]},
         "relationships": {"company_summary": {"attributes": {"name": m[2]}}}}
        for m in matches
    ]]}


def test_match_company_picks_highest_score_above_threshold():
    routes = {("POST", "/companies/match"):
              _match_payload([("c1", 0.62, "Low"), ("c2", 0.95, "Acme GmbH")])}
    client = _client(routes, min_match_score=0.70)
    match = client.match_company("Acme", email="x@acme.de", country="Germany", country_code="DE")
    assert match == {"company_id": "c2", "match_score": 0.95, "matched_name": "Acme GmbH",
                     "name_agreement": 100, "agreed_via": "company_name"}


def test_match_company_returns_none_below_threshold():
    routes = {("POST", "/companies/match"): _match_payload([("c1", 0.50, "Low")])}
    client = _client(routes, min_match_score=0.70)
    assert client.match_company("Acme") is None


def test_match_company_returns_none_when_no_matches():
    routes = {("POST", "/companies/match"): {"data": [[]]}}
    assert _client(routes).match_company("Acme") is None


# --- the name-agreement gate (calibrated on the 6-month control set) ----------

def test_match_company_rejects_high_score_with_wrong_name():
    # Observed live: LF returned "GogoMil UG" at 0.91 for myITplanet — score alone lies.
    routes = {("POST", "/companies/match"): _match_payload([("c1", 0.91, "GogoMil UG")])}
    client = _client(routes, min_match_score=0.70)
    assert client.match_company("myITplanet GmbH", email="info@myitplanet.de") is None


def test_match_company_rescues_low_score_with_exact_name():
    # Observed live: exact-name hits scored as low as 0.13 for small companies.
    routes = {("POST", "/companies/match"): _match_payload([("c1", 0.13, "Lingenfelder IT UG")])}
    client = _client(routes, min_match_score=0.70)
    match = client.match_company("Lingenfelder IT")
    assert match["company_id"] == "c1" and match["name_agreement"] >= 90


def test_match_company_accepts_via_email_domain_root():
    # LF name differs from the kununu profile name but equals the corporate domain root.
    routes = {("POST", "/companies/match"): _match_payload([("c1", 0.90, "digitaleformate")])}
    client = _client(routes, min_match_score=0.70)
    match = client.match_company("Digifom GmbH", email="kontakt@digitaleformate.de")
    assert match["company_id"] == "c1"


def test_match_company_freemail_domain_never_vouches():
    # googlemail.com would "agree" with a Googlemail-named candidate — freemail is excluded.
    routes = {("POST", "/companies/match"): _match_payload([("c1", 0.90, "Googlemail Ltd")])}
    client = _client(routes, min_match_score=0.70)
    assert client.match_company("Acme", email="jane@googlemail.com") is None


def test_match_company_prefers_passing_candidate_over_higher_scoring_failing_one():
    routes = {("POST", "/companies/match"):
              _match_payload([("c1", 0.95, "Wrong Company AG"), ("c2", 0.80, "Acme GmbH")])}
    client = _client(routes, min_match_score=0.70)
    match = client.match_company("Acme")
    assert match["company_id"] == "c2"


def test_match_company_rescued_by_alt_name():
    # Profile "Siemens", LF returns the assigned legal entity: alt name vouches.
    routes = {("POST", "/companies/match"):
              _match_payload([("c1", 0.30, "Siemens Mobility GmbH")])}
    client = _client(routes, min_match_score=0.70)
    match = client.match_companies([{
        "company_name": "Siemens", "email": "",
        "alt_names": ["Siemens Mobility GmbH", "Siemens AG"],
    }])[0]
    assert match["company_id"] == "c1"
    assert match["agreed_via"] == "alt_name:Siemens Mobility GmbH"
    assert match["name_agreement"] >= 90


def test_match_company_alt_names_do_not_loosen_gate():
    # Unrelated alt names must not admit a wrong candidate.
    routes = {("POST", "/companies/match"): _match_payload([("c1", 0.91, "GogoMil UG")])}
    client = _client(routes, min_match_score=0.70)
    assert client.match_companies([{
        "company_name": "myITplanet GmbH", "email": "",
        "alt_names": ["myITplanet Services GmbH"],
    }]) == [None]


def test_alt_names_are_stripped_from_post_body():
    seen = {}
    def handler(params, body):
        seen.update(body)
        return _match_payload([("c1", 0.95, "Acme GmbH")])
    routes = {("POST", "/companies/match"): handler}
    client = _client(routes, min_match_score=0.70)
    client.match_companies([{"company_name": "Acme", "alt_names": ["Acme Sub GmbH"]}])
    assert all("alt_names" not in c for c in seen["companies"])


# --- search_contact cascade --------------------------------------------------

def test_search_contact_returns_first_hit_on_full_name():
    routes = {("POST", "/contacts/search"):
              {"data": [{"id": "k1", "attributes": {"full_name": "Jane Doe"}}]}}
    client = _client(routes)
    assert client.search_contact("Jane Doe", "c1") == {"contact_id": "k1", "full_name": "Jane Doe"}
    assert len(client.session.posts_to("/contacts/search")) == 1  # no further attempts


def test_search_contact_cascades_to_split_name():
    # attempt 1 (full name) empty -> attempt 2 (split) hits
    routes = {("POST", "/contacts/search"):
              [{"data": []}, {"data": [{"id": "k9", "attributes": {"full_name": "Jane Doe"}}]}]}
    client = _client(routes)
    result = client.search_contact("Jane Doe", "c1")
    assert result["contact_id"] == "k9"
    assert len(client.session.posts_to("/contacts/search")) == 2


def test_search_contact_returns_none_when_all_attempts_empty():
    routes = {("POST", "/contacts/search"):
              [{"data": []}, {"data": []}, {"data": []}, {"data": []}]}
    client = _client(routes)
    assert client.search_contact("Jane Doe", "c1") is None


# --- get_account_id ----------------------------------------------------------

def test_get_account_id_caches():
    routes = {("GET", "/accounts"):
              {"data": [{"id": "acc1", "attributes": {"name": "kununu"}}]}}
    client = _client(routes, seed_account=False)
    assert client.get_account_id() == "acc1"
    assert client.get_account_id() == "acc1"  # cached: no second /accounts call
    assert len([c for c in client.session.calls if "/accounts" in c[1]]) == 1


# --- retrieve_companies batching --------------------------------------------

def test_retrieve_companies_batches_by_retrieve_batch():
    def companies(params, body):
        ids = params["ids"].split(",")
        return {"data": [{"id": i, "attributes": {"url": f"{i}.de"}} for i in ids]}

    routes = {("GET", "/companies"): companies}
    client = _client(routes, retrieve_batch=2)
    enriched = client.retrieve_companies(["c1", "c2", "c3"])
    assert set(enriched) == {"c1", "c2", "c3"}
    assert enriched["c1"]["url"] == "c1.de"
    gets = [c for c in client.session.calls if c[0] == "GET" and "/companies" in c[1]]
    assert len(gets) == 2  # ceil(3 / 2)


# --- transport: throttle + retry ---------------------------------------------

def test_throttle_spaces_consecutive_requests():
    sleeps = []
    client = LeadfeederClient(
        api_key="k", session=FakeSession({}),
        max_requests_per_min=120, sleep_fn=sleeps.append, clock=lambda: 1000.0,
    )
    client._account_id = "acc1"
    client.api_get("/x")
    client.api_get("/x")
    assert sleeps == [0.5]  # 60/120s between back-to-back calls; first call un-delayed


def test_retries_on_429_then_succeeds():
    sleeps = []
    routes = {("GET", "/companies"): [
        FakeResponse({}, status=429, headers={"Retry-After": "2"}),
        FakeResponse({"data": [{"id": "c1", "attributes": {"url": "x.de"}}]}),
    ]}
    client = _client(routes, sleep_fn=sleeps.append)
    out = client.api_get("/companies", params={"ids": "c1"})
    assert out["data"][0]["id"] == "c1"
    assert sleeps == [2.0]  # honored Retry-After


def test_429_exhausts_retries_and_raises():
    import requests
    routes = {("GET", "/companies"): [FakeResponse({}, status=429) for _ in range(10)]}
    client = _client(routes, max_retries=2)
    with pytest.raises(requests.exceptions.HTTPError):
        client.api_get("/companies")


def test_retries_on_500_without_retry_after_header():
    routes = {("GET", "/x"): [FakeResponse({}, status=500), FakeResponse({"data": []})]}
    client = _client(routes)
    assert client.api_get("/x") == {"data": []}


# --- match_companies (batch) -------------------------------------------------

def test_match_companies_aligns_results_by_input_index():
    def handler(params, body):
        return {"data": [
            [{"id": "c1", "attributes": {"match_score": 0.95},
              "relationships": {"company_summary": {"attributes": {"name": "Acme"}}}}],
            [],  # second company: no match
        ]}
    client = _client({("POST", "/companies/match"): handler})
    out = client.match_companies([{"company_name": "Acme"}, {"company_name": "Nope"}])
    assert out == [{"company_id": "c1", "match_score": 0.95, "matched_name": "Acme",
                    "name_agreement": 100, "agreed_via": "company_name"}, None]


def test_match_companies_chunks_over_match_batch():
    posts = []
    def handler(params, body):
        posts.append(len(body["companies"]))
        return {"data": [[] for _ in body["companies"]]}
    client = _client({("POST", "/companies/match"): handler}, match_batch=2)
    out = client.match_companies([{"company_name": f"C{i}"} for i in range(5)])
    assert posts == [2, 2, 1]
    assert out == [None] * 5


def test_match_companies_applies_score_floor():
    # Name disagrees, so the rescue band cannot apply — the floor must reject.
    handler = lambda p, b: {"data": [[
        {"id": "c1", "attributes": {"match_score": 0.50},
         "relationships": {"company_summary": {"attributes": {"name": "Other Co"}}}}]]}
    client = _client({("POST", "/companies/match"): handler}, min_match_score=0.70)
    assert client.match_companies([{"company_name": "Acme"}]) == [None]


def test_match_companies_chunk_failure_yields_none():
    routes = {("POST", "/companies/match"): FakeResponse({}, status=500)}
    client = _client(routes, max_retries=0)
    assert client.match_companies([{"company_name": "x"}, {"company_name": "y"}]) == [None, None]


def test_match_companies_empty_input():
    assert _client({}).match_companies([]) == []


# --- retrieve_contacts (batch) -----------------------------------------------

def test_retrieve_contacts_maps_ids_to_attributes():
    handler = lambda p, b: {"data": [
        {"id": "k1", "attributes": {"first_name": "Jane"}},
        {"id": "k2", "attributes": {"first_name": "John"}},
    ]}
    client = _client({("GET", "/contacts"): handler})
    assert client.retrieve_contacts(["k1", "k2"]) == {
        "k1": {"first_name": "Jane"}, "k2": {"first_name": "John"}}


def test_retrieve_contacts_chunks_over_retrieve_batch():
    seen = []
    def handler(params, body):
        seen.append(params["ids"])
        return {"data": []}
    client = _client({("GET", "/contacts"): handler}, retrieve_batch=2)
    client.retrieve_contacts(["a", "b", "c"])
    assert seen == ["a,b", "c"]


def test_retrieve_contacts_chunk_failure_isolated():
    routes = {("GET", "/contacts"): FakeResponse({}, status=500)}
    client = _client(routes, max_retries=0)
    assert client.retrieve_contacts(["k1"]) == {}  # logged + skipped, not raised


def test_get_contact_details_flattens_single_contact():
    handler = lambda p, b: {"data": [{"id": "k1", "attributes": {"position": {"title": "CEO"}}}]}
    client = _client({("GET", "/contacts"): handler})
    assert client.get_contact_details("k1")["position"] == "CEO"
