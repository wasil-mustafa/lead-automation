"""LeadfeederClient tests.

The pure flatteners (flatten_company / flatten_contact_details) are tested directly with
canned API JSON — that's where the field-extraction bugs live. The HTTP-orchestrating
methods (match, contact cascade, batch retrieve) are tested through a FakeSession that
stands in for `requests.Session` (the unavoidable external boundary) and records calls.
"""
import pytest

from leadauto.leadfeeder.client import (
    MATCH_CHUNK_FAILED,
    MATCH_RESULTS_MAX,
    MATCH_RESULTS_PRIMARY,
    MATCH_RESULTS_WIDE,
    LeadfeederClient,
    LeadfeederError,
    flatten_company,
    flatten_contact_details,
    match_email,
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
    popped per call), or a callable(params, body)->payload. Longest fragment wins.
    An Exception instance (in a queue or standalone) is RAISED instead of returned —
    that is how a transport failure, which never yields a response, is modelled."""

    def __init__(self, routes):
        self._routes = routes
        self.calls = []  # (method, url, params, body)
        self.timeouts = []  # the `timeout=` every call was issued with

    def get(self, url, headers=None, params=None, timeout=None):
        self.timeouts.append(timeout)
        return self._respond("GET", url, params, None)

    def post(self, url, headers=None, json=None, params=None, timeout=None):
        self.timeouts.append(timeout)
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
        if isinstance(value, BaseException):
            raise value
        return value if isinstance(value, FakeResponse) else FakeResponse(value)

    def posts_to(self, fragment):
        return [c for c in self.calls if c[0] == "POST" and fragment in c[1]]


def match_company(client, company_name, **payload):
    """Single-company match through the batched endpoint (the shape the pipeline uses)."""
    return client.match_companies([{"company_name": company_name, **payload}])[0] or None


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


def test_flatten_company_tolerates_null_industry_names():
    # Real LF retrieve payloads carry industry entries with an explicit "name": null
    # (crashed the 2026-07-03 CI run: join over a None). Nulls must be dropped.
    attrs = {"industries": {"industry": [{"name": None}, {"name": "Retail"}, {}]}}
    assert flatten_company(attrs)["Industry"] == "Retail"


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


def test_flatten_contact_tolerates_null_departments():
    # Same null-item trap as industry names: a [null] entry must not crash the join.
    out = flatten_contact_details({"departments": [None, "Sales"]})
    assert out["departments"] == "Sales"


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
    match = match_company(client, "Acme", email="x@acme.de", country="Germany", country_code="DE")
    assert match == {"company_id": "c2", "match_score": 0.95, "matched_name": "Acme GmbH",
                     "name_agreement": 100, "agreed_via": "company_name"}


def test_match_company_returns_none_below_threshold():
    routes = {("POST", "/companies/match"): _match_payload([("c1", 0.50, "Low")])}
    client = _client(routes, min_match_score=0.70)
    assert match_company(client, "Acme") is None


def test_match_company_returns_none_when_no_matches():
    routes = {("POST", "/companies/match"): {"data": [[]]}}
    assert match_company(_client(routes), "Acme") is None


# --- the name-agreement gate (calibrated on the 6-month control set) ----------

def test_match_company_rejects_high_score_with_wrong_name():
    # Observed live: LF returned "GogoMil UG" at 0.91 for myITplanet — score alone lies.
    routes = {("POST", "/companies/match"): _match_payload([("c1", 0.91, "GogoMil UG")])}
    client = _client(routes, min_match_score=0.70)
    assert match_company(client, "myITplanet GmbH", email="info@myitplanet.de") is None


def test_match_company_rescues_low_score_with_exact_name():
    # Observed live: exact-name hits scored as low as 0.13 for small companies.
    routes = {("POST", "/companies/match"): _match_payload([("c1", 0.13, "Lingenfelder IT UG")])}
    client = _client(routes, min_match_score=0.70)
    match = match_company(client, "Lingenfelder IT")
    assert match["company_id"] == "c1" and match["name_agreement"] >= 90


def test_match_company_accepts_via_email_domain_root():
    # LF name differs from the kununu profile name but equals the corporate domain root.
    routes = {("POST", "/companies/match"): _match_payload([("c1", 0.90, "digitaleformate")])}
    client = _client(routes, min_match_score=0.70)
    match = match_company(client, "Digifom GmbH", email="kontakt@digitaleformate.de")
    assert match["company_id"] == "c1"


def test_match_company_freemail_domain_never_vouches():
    # googlemail.com would "agree" with a Googlemail-named candidate — freemail is excluded.
    routes = {("POST", "/companies/match"): _match_payload([("c1", 0.90, "Googlemail Ltd")])}
    client = _client(routes, min_match_score=0.70)
    assert match_company(client, "Acme", email="jane@googlemail.com") is None


def test_match_company_prefers_passing_candidate_over_higher_scoring_failing_one():
    routes = {("POST", "/companies/match"):
              _match_payload([("c1", 0.95, "Wrong Company AG"), ("c2", 0.80, "Acme GmbH")])}
    client = _client(routes, min_match_score=0.70)
    match = match_company(client, "Acme")
    assert match["company_id"] == "c2"


def test_match_company_rescued_by_alt_name():
    # Profile "Haniel" (holding), LF returns the assigned legal entity "TAKKT AG":
    # only the alt name vouches — the profile name itself does not.
    routes = {("POST", "/companies/match"):
              _match_payload([("c1", 0.30, "TAKKT AG")])}
    client = _client(routes, min_match_score=0.70)
    match = client.match_companies([{
        "company_name": "Haniel", "email": "",
        "alt_names": ["TAKKT AG", "CWS-boco International"],
    }])[0]
    assert match["company_id"] == "c1"
    assert match["agreed_via"] == "alt_name:TAKKT AG"
    assert match["name_agreement"] >= 90


def test_agreed_via_prefers_company_name_on_ties():
    # Subset scoring gives company_name 100 too ("Siemens" is a token-subset of
    # "Siemens Mobility GmbH"); attribution stays with the primary signal when it
    # already fully vouches — the alt name added no evidence.
    routes = {("POST", "/companies/match"):
              _match_payload([("c1", 0.30, "Siemens Mobility GmbH")])}
    client = _client(routes, min_match_score=0.70)
    match = client.match_companies([{
        "company_name": "Siemens", "email": "",
        "alt_names": ["Siemens Mobility GmbH"],
    }])[0]
    assert match["agreed_via"] == "company_name"


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


# --- the `email` key: LF 400s on a blank/garbage one, and a 400 fails the WHOLE
# chunk (verified live 2026-08-08), so one bad address would degrade up to 200
# companies to "no match" — indistinguishable downstream from a real no-match.

@pytest.mark.parametrize("value", ["", None, "   ", "n/a", "kein", "info@", "@acme.de",
                                   "info@acme", "a b@acme.de"])
def test_match_email_rejects_non_addresses(value):
    assert match_email(value) is None


@pytest.mark.parametrize("value", ["info@acme.de", " Info@Acme.de ", "a.b+c@sub.acme.co.uk"])
def test_match_email_keeps_real_addresses(value):
    assert match_email(value) == value.strip()


def test_blank_email_is_omitted_from_post_body():
    seen = {}
    def handler(params, body):
        seen.update(body)
        return _match_payload([("c1", 0.95, "Acme GmbH")])
    routes = {("POST", "/companies/match"): handler}
    client = _client(routes, min_match_score=0.70)
    client.match_companies([{"company_name": "Acme", "email": ""}])
    assert seen["companies"] == [{"company_name": "Acme"}]


def test_garbage_email_is_omitted_from_post_body():
    seen = {}
    def handler(params, body):
        seen.update(body)
        return _match_payload([("c1", 0.95, "Acme GmbH")])
    routes = {("POST", "/companies/match"): handler}
    client = _client(routes, min_match_score=0.70)
    client.match_companies([{"company_name": "Acme", "email": "n/a"}])
    assert seen["companies"] == [{"company_name": "Acme"}]


def test_valid_email_is_still_sent():
    seen = {}
    def handler(params, body):
        seen.update(body)
        return _match_payload([("c1", 0.95, "Acme GmbH")])
    routes = {("POST", "/companies/match"): handler}
    client = _client(routes, min_match_score=0.70)
    client.match_companies([{"company_name": "Acme", "email": "info@acme.de"}])
    assert seen["companies"] == [{"company_name": "Acme", "email": "info@acme.de"}]


def test_one_blank_email_does_not_strip_its_neighbours_emails():
    # The sanitizer is per-company: a bad address must not cost the rest of the chunk
    # its email signal (nor, before the fix, the whole chunk its answer).
    seen = {}
    def handler(params, body):
        seen.update(body)
        return {"data": [[], []]}
    routes = {("POST", "/companies/match"): handler}
    client = _client(routes, min_match_score=0.70)
    client.match_companies([{"company_name": "Acme", "email": ""},
                            {"company_name": "Beta", "email": "info@beta.de"}])
    assert seen["companies"] == [{"company_name": "Acme"},
                                 {"company_name": "Beta", "email": "info@beta.de"}]


def test_blank_email_still_feeds_the_local_gate_unchanged():
    # Stripping is transport-only: the gate still scores against the original payload.
    routes = {("POST", "/companies/match"): _match_payload([("c1", 0.90, "digitaleformate")])}
    client = _client(routes, min_match_score=0.70)
    match = client.match_companies([{"company_name": "Digifom GmbH",
                                     "email": "kontakt@digitaleformate.de"}])[0]
    assert match["agreed_via"] == "email_domain"


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


def _hit(cid, full_name):
    return {"id": cid, "attributes": {"full_name": full_name}}


# Attempts 1 and 2 used to return contacts[0] unverified. Measured 2026-08-10 on a
# 100-contact sample: 13 of 64 hits (20%) were a DIFFERENT person. Every name pair below
# is a real one from that run. This matters beyond a wrong label — enrich.py feeds the hit
# to retrieve_contacts(), which spends a credit and writes that person's email/phone onto
# a Salesforce Lead.
@pytest.mark.parametrize("searched,lf_name", [
    ("Christian Etscher", "Christina Zepf"),      # both names differ
    ("Grit Weidemann", "Paul Weimann"),
    ("Karen Dabow", "Karan Bruellau"),
    ("Peter Schmidt", "Roland Schmid"),           # surname one edit apart
    ("Julia Eberhardt", "Georg Eberhardt"),       # surname exact, first name differs
    ("Verena Schepperheyn", "Ann-Katrin Schepperheyn"),
    ("Barbara Meierhoefer", "Barbara Ehrmann"),   # first name exact, surname differs
    ("Marie Amberg", "Marie Ulm"),
    ("Katharina Huebner", "Katharina Landes"),
    ("Sabrina Friedrichs", "Sabrina Heidler"),
])
def test_search_contact_rejects_a_different_person(searched, lf_name):
    # Every attempt returns the same wrong person; none may be accepted.
    routes = {("POST", "/contacts/search"): {"data": [_hit("k1", lf_name)]}}
    client = _client(routes)
    assert client.search_contact(searched, "c1") is None


def test_search_contact_scans_the_page_instead_of_taking_the_first_hit():
    # The OR'd search returns namesakes first; the real person is further down the page.
    routes = {("POST", "/contacts/search"):
              {"data": [_hit("k1", "Christian Wiesinger"),
                        _hit("k2", "Christina Zepf"),
                        _hit("k3", "Christian Etscher")]}}
    client = _client(routes)
    assert client.search_contact("Christian Etscher", "c1")["contact_id"] == "k3"


def test_a_wrong_hit_does_not_short_circuit_the_later_attempts():
    # Attempt 2 returning a stranger must NOT stop the cascade: attempt 3 still runs and
    # finds the real person. Before the fix, `contacts` was non-empty and 3 and 4 were
    # skipped entirely.
    def handler(params, body):
        terms = body.get("search_terms")
        if terms == ["Peter Schmidt"]:
            return {"data": []}
        if terms == ["Peter", "Schmidt"]:
            return {"data": [_hit("wrong", "Roland Schmid")]}     # OR'd namesake
        if terms == ["Schmidt"]:
            return {"data": [_hit("right", "Peter Schmidt")]}     # attempt 3
        return {"data": []}
    client = _client({("POST", "/contacts/search"): handler})
    assert client.search_contact("Peter Schmidt", "c1") == {"contact_id": "right",
                                                            "full_name": "Peter Schmidt"}


def test_search_contact_accepts_a_middle_name_in_the_lf_record():
    routes = {("POST", "/contacts/search"):
              {"data": [_hit("k1", "Anna Maria Berger")]}}
    client = _client(routes)
    assert client.search_contact("Anna Berger", "c1")["contact_id"] == "k1"


def test_search_contact_accepts_a_middle_name_in_the_SEARCHED_name():
    # The mirror case, and a real live miss (2026-08-10): Salesforce carries "Peter Sven
    # Mitterhuemer", LeadFeeder stores "Peter Mitterhuemer". Requiring every part to
    # appear rejected the right person; first + last is the test that matters.
    routes = {("POST", "/contacts/search"):
              {"data": [_hit("k1", "Peter Mitterhuemer")]}}
    client = _client(routes)
    assert client.search_contact("Peter Sven Mitterhuemer", "c1")["contact_id"] == "k1"


def test_middle_name_tolerance_does_not_readmit_a_different_person():
    # Ignoring middles must not loosen the first/last check.
    routes = {("POST", "/contacts/search"):
              {"data": [_hit("k1", "Peter Sven Schmidhuber")]}}
    client = _client(routes)
    assert client.search_contact("Peter Sven Mitterhuemer", "c1") is None


def test_search_contact_tries_hyphen_segments_of_surname():
    # Live miss 2026-07-09: task name "Susann Kothe-Spies", LF stores "Susann Kothe" —
    # searching the unsplit surname can never hit. Segment searches must be tried.
    def handler(params, body):
        if body.get("search_terms") == ["Kothe"]:
            return {"data": [{"id": "k7", "attributes": {"full_name": "Susann Kothe"}}]}
        return {"data": []}
    client = _client({("POST", "/contacts/search"): handler})
    result = client.search_contact("Susann Kothe-Spies", "c1")
    assert result == {"contact_id": "k7", "full_name": "Susann Kothe"}


def test_search_contact_browse_filter_accepts_hyphen_segment():
    # Same scenario when only the browse fallback (no search_terms) returns her:
    # the local all-parts filter must accept "Kothe-Spies" via its segment "Kothe".
    def handler(params, body):
        if "search_terms" not in body:
            return {"data": [{"id": "k8", "attributes": {"full_name": "Susann Kothe"}}]}
        return {"data": []}
    client = _client({("POST", "/contacts/search"): handler})
    result = client.search_contact("Susann Kothe-Spies", "c1")
    assert result == {"contact_id": "k8", "full_name": "Susann Kothe"}


def test_search_contact_hyphen_segments_still_require_first_name():
    # Segment matching must not loosen the first-name check: a different person
    # sharing one surname segment is not a match.
    def handler(params, body):
        if body.get("search_terms") == ["Kothe"] or "search_terms" not in body:
            return {"data": [{"id": "k9", "attributes": {"full_name": "Susann Kothe"}}]}
        return {"data": []}
    client = _client({("POST", "/contacts/search"): handler})
    assert client.search_contact("Anna Kothe-Spies", "c1") is None


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


def test_every_request_carries_a_timeout():
    """Without one, requests waits forever: a stalled socket hangs the whole run and
    RETRYABLE_TRANSPORT's Timeout arm can never fire. Measured 2026-08-17 — a backtest
    sat 26 minutes on one open connection to api.leadfeeder.com at 0% CPU."""
    from leadauto.leadfeeder.client import DEFAULT_TIMEOUT
    client = _client({("GET", "/x"): {"data": []}, ("POST", "/y"): {"data": []}})
    client.api_get("/x")
    client.api_post("/y", payload={})
    assert client.session.timeouts == [DEFAULT_TIMEOUT, DEFAULT_TIMEOUT]
    assert all(t is not None for t in client.session.timeouts)


def test_timeout_is_overridable():
    client = _client({("GET", "/x"): {"data": []}}, timeout=7)
    client.api_get("/x")
    assert client.session.timeouts == [7]


# --- transport-level retry (dropped socket, no response object at all) --------

@pytest.mark.parametrize("exc_name", ["ConnectionError", "Timeout", "ChunkedEncodingError"])
def test_get_retries_on_transport_error_then_succeeds(exc_name):
    import requests
    exc = getattr(requests.exceptions, exc_name)("socket died")
    sleeps = []
    routes = {("GET", "/x"): [exc, FakeResponse({"data": ["ok"]})]}
    client = _client(routes, sleep_fn=sleeps.append, backoff_base=1.0)
    assert client.api_get("/x") == {"data": ["ok"]}
    assert sleeps == [1.0]  # backoff_base * 2**0; no Retry-After header exists to honor


def test_get_transport_retry_backs_off_exponentially():
    import requests
    sleeps = []
    routes = {("GET", "/x"): [requests.exceptions.ConnectionError("drop") for _ in range(3)]
                             + [FakeResponse({"data": []})]}
    client = _client(routes, sleep_fn=sleeps.append, backoff_base=1.0, max_retries=4)
    client.api_get("/x")
    assert sleeps == [1.0, 2.0, 4.0]


def test_get_transport_error_exhausts_retries_and_reraises():
    import requests
    routes = {("GET", "/x"): [requests.exceptions.ConnectionError("drop") for _ in range(10)]}
    client = _client(routes, max_retries=2)
    with pytest.raises(requests.exceptions.ConnectionError):
        client.api_get("/x")
    # 1 initial attempt + 2 retries, and the original exception type survives so the
    # per-chunk `except RequestException` handlers still degrade instead of crashing.
    assert len(client.session.calls) == 3


def test_post_transport_error_is_not_retried():
    """A re-POSTed /contacts/find-contact-data-jobs would bill the credits twice."""
    import requests
    routes = {("POST", "/x"): [requests.exceptions.ConnectionError("drop"),
                               FakeResponse({"data": ["would-have-succeeded"]})]}
    client = _client(routes, max_retries=4)
    with pytest.raises(requests.exceptions.ConnectionError):
        client.api_post("/x", payload={})
    assert len(client.session.calls) == 1


def test_match_chunk_still_degrades_after_transport_retries_are_exhausted():
    import requests
    routes = {("POST", "/companies/match"): requests.exceptions.ConnectionError("drop")}
    client = _client(routes, max_retries=2)
    out = client.match_companies([{"company_name": "A"}, {"company_name": "B"}])
    # falsy (callers degrade as before) but NOT None: "never asked" != "rejected"
    assert out == [MATCH_CHUNK_FAILED, MATCH_CHUNK_FAILED] and not any(out)
    assert all(m is not None for m in out)


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


def test_match_companies_chunk_failure_yields_falsy_sentinel():
    routes = {("POST", "/companies/match"): FakeResponse({}, status=500)}
    client = _client(routes, max_retries=0)
    out = client.match_companies([{"company_name": "x"}, {"company_name": "y"}])
    assert out == [MATCH_CHUNK_FAILED, MATCH_CHUNK_FAILED] and not any(out)
    assert all(m is not None for m in out)
    # the single-company wrapper still answers None (its callers compare `is None`)
    assert match_company(client, "x") is None


def test_match_companies_empty_input():
    assert _client({}).match_companies([]) == []


# --- candidate width (max_results_per_company) --------------------------------
# The primary pass must keep asking for 3: the acceptance gate was calibrated at that
# width, and more candidates can only outrank the accepted one on (score, agreement).
# The second-chance pass in enrich asks for 20, for unmatched leads only.

def test_match_companies_defaults_to_three_candidates():
    seen = {}
    def handler(params, body):
        seen.update(params)
        return {"data": [[]]}
    client = _client({("POST", "/companies/match"): handler})
    client.match_companies([{"company_name": "Acme"}])
    assert seen["max_results_per_company"] == MATCH_RESULTS_PRIMARY == 3


def test_match_companies_sends_the_requested_width():
    seen = {}
    def handler(params, body):
        seen.update(params)
        return {"data": [[]]}
    client = _client({("POST", "/companies/match"): handler})
    client.match_companies([{"company_name": "Acme"}], max_results=MATCH_RESULTS_WIDE)
    assert seen["max_results_per_company"] == MATCH_RESULTS_WIDE == 20


def test_match_companies_rejects_a_width_outside_the_api_range():
    # 20 is the machine-readable spec's ceiling; the prose doc's 10 is wrong. A bad
    # value must fail here, not as an opaque 400 that degrades the whole chunk to None.
    client = _client({("POST", "/companies/match"): lambda p, b: {"data": [[]]}})
    for bad in (0, -1, MATCH_RESULTS_MAX + 1):
        with pytest.raises(ValueError):
            client.match_companies([{"company_name": "Acme"}], max_results=bad)


def test_a_wider_call_still_gates_every_candidate():
    # The safety argument for widening: `_best_match` judges all candidates, so a wider
    # call cannot accept anything the narrow call would have rejected.
    handler = lambda p, b: {"data": [[
        {"id": "c1", "attributes": {"match_score": 0.95},
         "relationships": {"company_summary": {"attributes": {"name": "Totally Other"}}}},
        {"id": "c2", "attributes": {"match_score": 0.10},
         "relationships": {"company_summary": {"attributes": {"name": "Also Wrong"}}}},
    ]]}
    client = _client({("POST", "/companies/match"): handler})
    assert client.match_companies([{"company_name": "Acme GmbH"}],
                                  max_results=MATCH_RESULTS_WIDE) == [None]


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


# --- find contact data (async external-provider enrichment) -------------------

def test_find_contact_data_creates_job_and_polls_to_completed():
    routes = {
        ("POST", "/contacts/find-contact-data-jobs"): {"data": {"id": "job1", "attributes": {"status": "running"}}},
        ("GET", "/contacts/find-contact-data-jobs/job1"): [
            {"data": {"id": "job1", "attributes": {"status": "running"}}},
            {"data": {"id": "job1", "attributes": {"status": "completed"}}},
        ],
    }
    client = _client(routes)
    assert client.find_contact_data(["k1", "k2"], timeout=60, poll_interval=0) is True
    post = next(c for c in client.session.calls if c[0] == "POST")
    assert post[3]["data"]["attributes"]["contact_ids"] == ["k1", "k2"]
    assert post[3]["data"]["attributes"]["enrichment_type"] == "full"


def test_find_contact_data_chunks_over_100_ids():
    routes = {
        ("POST", "/contacts/find-contact-data-jobs"): lambda p, b: {
            "data": {"id": f"job{len(b['data']['attributes']['contact_ids'])}",
                     "attributes": {"status": "running"}}},
        ("GET", "/contacts/find-contact-data-jobs/"): {
            "data": {"attributes": {"status": "completed"}}},
    }
    client = _client(routes)
    assert client.find_contact_data([f"k{i}" for i in range(150)], poll_interval=0) is True
    posts = [c for c in client.session.calls if c[0] == "POST"]
    assert [len(c[3]["data"]["attributes"]["contact_ids"]) for c in posts] == [100, 50]


def test_find_contact_data_failed_job_returns_false():
    routes = {
        ("POST", "/contacts/find-contact-data-jobs"): {"data": {"id": "job1", "attributes": {"status": "running"}}},
        ("GET", "/contacts/find-contact-data-jobs/job1"): {"data": {"attributes": {"status": "failed"}}},
    }
    assert _client(routes).find_contact_data(["k1"]) is False


def test_find_contact_data_timeout_returns_false():
    routes = {
        ("POST", "/contacts/find-contact-data-jobs"): {"data": {"id": "job1", "attributes": {"status": "running"}}},
        ("GET", "/contacts/find-contact-data-jobs/job1"): {"data": {"attributes": {"status": "running"}}},
    }
    t = iter(range(0, 1000, 100))  # each clock() call advances 100s
    client = _client(routes, clock=lambda: next(t))
    assert client.find_contact_data(["k1"], timeout=150, poll_interval=0) is False


def test_find_contact_data_empty_input_no_calls():
    client = _client({})
    assert client.find_contact_data([]) is False
    assert client.session.calls == []
