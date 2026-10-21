"""Tests for the sf client wrapper: query routing, the direct Bulk API 2.0 read
transport, /composite bundled reads, sObject-Collections updates, auth_info and the
SOAP convertLead path.
"""
import json
import types

import pytest

from leadauto.sf.client import SfClient, SfError


# --- update_records: REST sObject Collections PATCH ---------------------------
# One request per <=200 records replaces the 5-7 round-trip Bulk API job the CLI
# used to run per update (API-call reduction, 2026-07-18). Mock boundary is
# requests.request (the org HTTP boundary) + _run for auth_info.

def _fake_patch(responder, capture):
    def request(method, url, data=None, headers=None, timeout=None):
        assert method == "PATCH"
        body = json.loads(data)
        capture.append({"url": url, "headers": headers, "body": body})
        return responder(body, headers)
    return request


def _collections_ok(body, headers):
    return _FakeResponse(json.dumps(
        [{"id": r["Id"], "success": True, "errors": []} for r in body["records"]]))


def test_update_records_patches_composite_sobjects(monkeypatch):
    client = SfClient("headless", api_version="66.0")
    monkeypatch.setattr(client, "_run", _fake_org_display([]))
    calls: list[dict] = []
    monkeypatch.setattr("leadauto.sf.client.requests.request",
                        _fake_patch(_collections_ok, calls))
    r = client.update_records("Lead", [{"Id": "00Q1", "Email": "a@b.c"}])
    assert r == {"processed": 1, "failed": 0, "failures": [], "confirmed": True}
    call = calls[0]
    assert call["url"] == ("https://kununu.my.salesforce.com"
                           "/services/data/v66.0/composite/sobjects")
    assert call["headers"]["Authorization"] == "Bearer TOK1"
    assert call["headers"]["Content-Type"] == "application/json"
    assert call["body"]["allOrNone"] is False
    assert call["body"]["records"] == [
        {"attributes": {"type": "Lead"}, "Id": "00Q1", "Email": "a@b.c"}]


def test_update_records_partial_failure_maps_errors_to_request_ids(monkeypatch):
    # Failed rows come back with a null id — the failure must be keyed to the
    # request-order record Id so it can land in the QA Failures sheet.
    client = SfClient("headless")
    monkeypatch.setattr(client, "_run", _fake_org_display([]))

    def responder(body, headers):
        return _FakeResponse(json.dumps([
            {"id": "00Q1", "success": True, "errors": []},
            {"id": None, "success": False, "errors": [
                {"statusCode": "FIELD_CUSTOM_VALIDATION_EXCEPTION",
                 "message": "bad postal", "fields": ["PostalCode"]}]},
        ]))
    monkeypatch.setattr("leadauto.sf.client.requests.request",
                        _fake_patch(responder, []))
    r = client.update_records("Lead", [{"Id": "00Q1", "Email": "a@b.c"},
                                       {"Id": "00Q2", "PostalCode": "1"}])
    assert r["processed"] == 2 and r["failed"] == 1 and r["confirmed"] is True
    assert r["failures"] == [{"id": "00Q2",
                              "error": "FIELD_CUSTOM_VALIDATION_EXCEPTION: bad postal"}]


def test_update_records_chunks_at_200(monkeypatch):
    client = SfClient("headless")
    monkeypatch.setattr(client, "_run", _fake_org_display([]))
    calls: list[dict] = []
    monkeypatch.setattr("leadauto.sf.client.requests.request",
                        _fake_patch(_collections_ok, calls))
    records = [{"Id": f"00Q{i:04d}", "Email": "a@b.c"} for i in range(450)]
    r = client.update_records("Lead", records)
    assert r["processed"] == 450 and r["failed"] == 0
    assert [len(c["body"]["records"]) for c in calls] == [200, 200, 50]


def test_update_records_empty_is_noop_without_patch(monkeypatch):
    client = SfClient("headless")
    monkeypatch.setattr("leadauto.sf.client.requests.request",
                        lambda *a, **k: pytest.fail("must not PATCH"))
    assert client.update_records("Lead", []) == {
        "processed": 0, "failed": 0, "failures": [], "confirmed": True}


def test_update_records_duplicate_header_only_when_allowed(monkeypatch):
    # Sforce-Duplicate-Rule-Header lets an Alert-mode duplicate rule save the row
    # (live-proven 2026-10-21); it must never ride an update that did not ask for it.
    client = SfClient("headless")
    monkeypatch.setattr(client, "_run", _fake_org_display([]))
    calls: list[dict] = []
    monkeypatch.setattr("leadauto.sf.client.requests.request",
                        _fake_patch(_collections_ok, calls))
    client.update_records("Lead", [{"Id": "00Q1", "Email": "a@b.c"}])
    client.update_records("Lead", [{"Id": "00Q1", "Email": "a@b.c"}],
                          allow_duplicates=True)
    assert "Sforce-Duplicate-Rule-Header" not in calls[0]["headers"]
    assert calls[1]["headers"]["Sforce-Duplicate-Rule-Header"] == "allowSave=true"
    assert calls[1]["headers"]["Authorization"] == "Bearer TOK1"


def test_update_records_requires_id():
    client = SfClient("headless")
    with pytest.raises(SfError, match="Id"):
        client.update_records("Lead", [{"Email": "a@b.c"}])


def test_update_records_retries_once_on_invalid_session(monkeypatch):
    # Same contract as convert_lead: a stale cached CLI token gets ONE auth
    # re-fetch + retry; the fresh token must ride the second request.
    client = SfClient("headless")
    tokens = iter(["STALE", "FRESH"])
    auth_calls: list[str] = []

    def run(cmd, check=True):
        auth_calls.append(cmd)
        return types.SimpleNamespace(returncode=0, stdout=json.dumps(
            {"result": {"accessToken": next(tokens),
                        "instanceUrl": "https://x.my.salesforce.com"}}), stderr="")
    monkeypatch.setattr(client, "_run", run)
    calls: list[dict] = []

    def responder(body, headers):
        if headers["Authorization"] == "Bearer STALE":
            return _FakeResponse(json.dumps([{
                "errorCode": "INVALID_SESSION_ID",
                "message": "Session expired or invalid"}]), status_code=401)
        return _collections_ok(body, headers)
    monkeypatch.setattr("leadauto.sf.client.requests.request",
                        _fake_patch(responder, calls))
    r = client.update_records("Lead", [{"Id": "00Q1", "Email": "a@b.c"}])
    assert r["failed"] == 0
    assert len(calls) == 2 and len(auth_calls) == 2
    assert calls[1]["headers"]["Authorization"] == "Bearer FRESH"


def test_update_records_http_error_raises_with_body(monkeypatch):
    client = SfClient("headless")
    monkeypatch.setattr(client, "_run", _fake_org_display([]))
    monkeypatch.setattr("leadauto.sf.client.requests.request", _fake_patch(
        lambda body, headers: _FakeResponse(json.dumps([{
            "errorCode": "INVALID_FIELD", "message": "No such column 'Nope'"}]),
            status_code=400), []))
    with pytest.raises(SfError, match="No such column"):
        client.update_records("Lead", [{"Id": "00Q1", "Nope": "x"}])


# --- create_records: REST sObject Collections POST ----------------------------

def _fake_post(responder, capture):
    def request(method, url, data=None, headers=None, timeout=None):
        assert method == "POST"
        body = json.loads(data)
        capture.append({"url": url, "body": body})
        return responder(body, headers)
    return request


def test_create_records_posts_composite_sobjects(monkeypatch):
    client = SfClient("headless", api_version="66.0")
    monkeypatch.setattr(client, "_run", _fake_org_display([]))
    calls: list[dict] = []
    monkeypatch.setattr("leadauto.sf.client.requests.request", _fake_post(
        lambda body, h: _FakeResponse(json.dumps(
            [{"id": "00v1", "success": True, "errors": []}])), calls))
    r = client.create_records("CampaignMember", [{"CampaignId": "701C", "LeadId": "00Q1"}])
    assert r == {"processed": 1, "created": 1, "duplicates": 0, "failed": 0, "failures": []}
    assert calls[0]["url"].endswith("/services/data/v66.0/composite/sobjects")
    assert calls[0]["body"] == {"allOrNone": False, "records": [
        {"attributes": {"type": "CampaignMember"}, "CampaignId": "701C", "LeadId": "00Q1"}]}


def test_create_records_duplicate_is_benign_and_failures_name_their_record(monkeypatch):
    client = SfClient("headless")
    monkeypatch.setattr(client, "_run", _fake_org_display([]))
    monkeypatch.setattr("leadauto.sf.client.requests.request", _fake_post(
        lambda body, h: _FakeResponse(json.dumps([
            {"id": "00v1", "success": True, "errors": []},
            {"id": None, "success": False, "errors": [
                {"statusCode": "DUPLICATE_VALUE", "message": "Already a campaign member."}]},
            {"id": None, "success": False, "errors": [
                {"statusCode": "INSUFFICIENT_ACCESS_ON_CROSS_REFERENCE_ENTITY",
                 "message": "no access"}]},
        ])), []))
    recs = [{"CampaignId": "701C", "LeadId": f"00Q{i}"} for i in (1, 2, 3)]
    r = client.create_records("CampaignMember", recs)
    assert (r["created"], r["duplicates"], r["failed"]) == (1, 1, 1)
    assert r["failures"] == [{
        "record": recs[2],
        "error": "INSUFFICIENT_ACCESS_ON_CROSS_REFERENCE_ENTITY: no access"}]


def test_create_records_chunks_at_200_and_empty_makes_no_call(monkeypatch):
    client = SfClient("headless")
    monkeypatch.setattr(client, "_run", _fake_org_display([]))
    calls: list[dict] = []
    monkeypatch.setattr("leadauto.sf.client.requests.request", _fake_post(
        lambda body, h: _FakeResponse(json.dumps(
            [{"id": "x", "success": True, "errors": []} for _ in body["records"]])), calls))
    assert client.create_records("CampaignMember", [])["processed"] == 0 and calls == []
    r = client.create_records("CampaignMember",
                              [{"CampaignId": "701C", "LeadId": f"00Q{i}"} for i in range(250)])
    assert r["created"] == 250
    assert [len(c["body"]["records"]) for c in calls] == [200, 50]


# --- query routing: direct REST GET for short queries, direct Bulk API 2.0 for long ones -
# Long IN(...) lists overflow the GET-URL header limit (HTTP 431), so they go through a
# Bulk query job — since 2026-07-25 over direct HTTP rather than `sf data export bulk`,
# which polled job status every 5 s and spent ~4-7 API calls where 2 suffice. Short
# queries stopped shelling out to the sf CLI on 2026-09-19: the CLI is now only the
# token source (auth_info).

class _RestOrg:
    """GET /query answering from a list of page bodies, in order."""
    def __init__(self, bodies):
        self.bodies = list(bodies)
        self.calls: list[tuple[str, str]] = []

    def __call__(self, method, url, data=None, headers=None, timeout=None):
        path = url.split("salesforce.com", 1)[-1]
        self.calls.append((method, path))
        assert headers["Authorization"] == "Bearer TOK1"
        return _FakeResponse(json.dumps(self.bodies.pop(0)))


def _long_id_query(n=300):
    ids = ", ".join(f"'{i:018d}'" for i in range(n))
    return f"SELECT Id FROM Lead WHERE Id IN ({ids}) AND IsConverted = false"


class _BulkOrg:
    """Minimal Bulk API 2.0 query-job endpoint over the requests.request boundary.

    `ready_after` = how many results GETs must be refused with a 4xx before the job's
    rows are served, so the not-ready fallback can be exercised. `pages` is a list of
    CSV bodies; every page but the last carries an `Sforce-Locator`.
    """

    def __init__(self, pages, ready_after=0, state="JobComplete"):
        self.pages, self.ready_after, self.state = pages, ready_after, state
        self.calls: list[tuple[str, str]] = []

    def __call__(self, method, url, data=None, headers=None, timeout=None):
        path = url.split("salesforce.com", 1)[-1]
        self.calls.append((method, path))
        if method == "POST" and path.endswith("/jobs/query"):
            assert json.loads(data)["operation"] == "query"
            return _FakeResponse(json.dumps({"id": "750JOB"}))
        if method == "GET" and "/results" in path:
            refusals = sum(1 for m, p in self.calls
                           if m == "GET" and "/results" in p and "locator=" not in p)
            if refusals <= self.ready_after:
                return _FakeResponse("job not ready", status_code=400)
            index = 1 if "locator=" in path else 0
            body = self.pages[index]
            more = index + 1 < len(self.pages)
            return _FakeResponse(body, headers={"Sforce-Locator": "L2"} if more else {})
        if method == "GET":                                    # job status
            return _FakeResponse(json.dumps({"state": self.state}))
        raise AssertionError(f"unexpected {method} {path}")

    def count(self, needle):
        return sum(1 for _m, p in self.calls if needle in p)


def _bulk_client(monkeypatch, org):
    client = SfClient("headless", api_version="66.0")
    monkeypatch.setattr(client, "_run", _fake_org_display([]))
    monkeypatch.setattr("leadauto.sf.client.requests.request", org)
    monkeypatch.setattr("leadauto.sf.client._BULK_POLL_INITIAL_S", 0)
    monkeypatch.setattr("leadauto.sf.client._BULK_POLL_INTERVAL_S", 0)
    return client


def test_query_short_is_one_rest_get_and_strips_attributes(monkeypatch):
    client = SfClient("headless")
    monkeypatch.setattr(client, "_run", _fake_org_display([]))
    org = _RestOrg([{"done": True, "totalSize": 1,
                     "records": [{"Id": "00Q1", "attributes": {"type": "Lead"}}]}])
    monkeypatch.setattr("leadauto.sf.client.requests.request", org)
    out = client.query("SELECT Id FROM Lead WHERE Id IN ('00Q1')")
    assert out == [{"Id": "00Q1"}]                     # attributes stripped
    assert org.calls == [("GET", "/services/data/v66.0/query/?q=SELECT%20Id%20FROM%20Lead"
                                 "%20WHERE%20Id%20IN%20%28%2700Q1%27%29")]


def test_query_short_follows_next_records_url(monkeypatch):
    # done=false is never the answer: a truncated read is a plausible-looking wrong one.
    client = SfClient("headless")
    monkeypatch.setattr(client, "_run", _fake_org_display([]))
    org = _RestOrg([
        {"done": False, "nextRecordsUrl": "/services/data/v66.0/query/01g-2000",
         "records": [{"Id": "00Q1"}]},
        {"done": True, "records": [{"Id": "00Q2"}]},
    ])
    monkeypatch.setattr("leadauto.sf.client.requests.request", org)
    assert client.query("SELECT Id FROM Lead") == [{"Id": "00Q1"}, {"Id": "00Q2"}]
    assert org.calls[1] == ("GET", "/services/data/v66.0/query/01g-2000")


def test_query_short_http_error_raises_with_body(monkeypatch):
    client = SfClient("headless")
    monkeypatch.setattr(client, "_run", _fake_org_display([]))
    monkeypatch.setattr("leadauto.sf.client.requests.request",
                        lambda *a, **k: _FakeResponse(
                            json.dumps([{"message": "No such column 'Nope'"}]), status_code=400))
    with pytest.raises(SfError, match="No such column"):
        client.query("SELECT Nope FROM Lead")


def test_query_double_quotes_are_no_longer_a_shell_problem(monkeypatch):
    # The ban existed for the shell-quoted `sf data query` string; REST carries the SOQL
    # URL-encoded, so a double quote is just a character (SOQL itself still wants ').
    client = SfClient("headless")
    monkeypatch.setattr(client, "_run", _fake_org_display([]))
    org = _RestOrg([{"done": True, "records": []}])
    monkeypatch.setattr("leadauto.sf.client.requests.request", org)
    assert client.query('SELECT Id FROM Lead WHERE Name = "x"') == []
    assert "%22x%22" in org.calls[0][1]


def test_query_long_routes_to_bulk_job_in_two_calls(monkeypatch):
    # The happy path is exactly 2 API calls: create + one result page. Reading the
    # results IS the completion check, so no status poll is spent.
    org = _BulkOrg(["Id,Name\n00Q1,Acme\n00Q2,Globex\n"])
    client = _bulk_client(monkeypatch, org)
    out = client.query(_long_id_query())
    assert out == [{"Id": "00Q1", "Name": "Acme"}, {"Id": "00Q2", "Name": "Globex"}]
    assert len(org.calls) == 2
    assert org.count("/jobs/query") == 2 and org.count("/results") == 1
    assert not any(p.endswith("/jobs/query/750JOB") for _m, p in org.calls)  # no poll


def test_query_bulk_flag_forces_bulk(monkeypatch):
    org = _BulkOrg(["Id\n00Q1\n"])
    client = _bulk_client(monkeypatch, org)
    assert client.query("SELECT Id FROM Lead LIMIT 1", bulk=True) == [{"Id": "00Q1"}]
    assert org.calls[0][0] == "POST"


def test_bulk_rows_are_stringly_typed_like_the_csv_they_come_from(monkeypatch):
    # Bulk results are CSV: booleans are 'true'/'false' STRINGS and nulls are ''.
    # Callers coerce with their own _is_true helpers — the transport must not "fix"
    # this, or the coercions downstream would start seeing a different shape.
    org = _BulkOrg(["Id,IsConverted,City\n00Q1,false,\n"])
    client = _bulk_client(monkeypatch, org)
    assert client.query(_long_id_query()) == [
        {"Id": "00Q1", "IsConverted": "false", "City": ""}]


def test_bulk_follows_sforce_locator_pages(monkeypatch):
    org = _BulkOrg(["Id\n00Q1\n", "Id\n00Q2\n"])
    client = _bulk_client(monkeypatch, org)
    assert client.query(_long_id_query()) == [{"Id": "00Q1"}, {"Id": "00Q2"}]
    assert org.count("locator=") == 1


def test_bulk_falls_back_to_polling_when_results_are_not_ready(monkeypatch):
    # A 4xx on the FIRST results page means "job still running", not failure.
    org = _BulkOrg(["Id\n00Q1\n"], ready_after=1)
    client = _bulk_client(monkeypatch, org)
    assert client.query(_long_id_query()) == [{"Id": "00Q1"}]
    assert any(p.endswith("/jobs/query/750JOB") for _m, p in org.calls)   # polled


def test_bulk_empty_body_from_unfinished_job_is_not_reported_as_no_rows(monkeypatch):
    # An empty body is indistinguishable from a zero-row result, so it is confirmed
    # with one state poll: reporting "no rows" for a running job is a WRONG answer.
    class _Org:
        def __init__(self):
            self.calls: list[tuple[str, str]] = []
            self.polls = 0

        def __call__(self, method, url, data=None, headers=None, timeout=None):
            path = url.split("salesforce.com", 1)[-1]
            self.calls.append((method, path))
            if method == "POST":
                return _FakeResponse(json.dumps({"id": "750JOB"}))
            if "/results" in path:
                # empty until the job has actually been observed complete
                body = "Id\n00Q1\n" if self.polls else ""
                return _FakeResponse(body)
            self.polls += 1
            return _FakeResponse(json.dumps(
                {"state": "InProgress" if self.polls == 1 else "JobComplete"}))

    org = _Org()
    client = _bulk_client(monkeypatch, org)
    out = client.query(_long_id_query())
    assert out == [{"Id": "00Q1"}]            # NOT the empty first read
    assert org.polls >= 2                     # confirmed by state polls, not trusted


def test_bulk_failed_job_raises(monkeypatch):
    org = _BulkOrg([""], state="Failed")
    client = _bulk_client(monkeypatch, org)
    with pytest.raises(SfError, match="Failed"):
        client.query(_long_id_query())


def test_bulk_mid_pagination_failure_is_fatal(monkeypatch):
    # Retrying mid-pagination would restart at page 1 and DUPLICATE rows, so only a
    # first-page 4xx may be swallowed.
    class _Org(_BulkOrg):
        def __call__(self, method, url, data=None, headers=None, timeout=None):
            if "locator=" in url:
                self.calls.append((method, url))
                return _FakeResponse("gone", status_code=404)
            return super().__call__(method, url, data=data, headers=headers,
                                    timeout=timeout)

    client = _bulk_client(monkeypatch, _Org(["Id\n00Q1\n", "Id\n00Q2\n"]))
    with pytest.raises(SfError, match="results of bulk job"):
        client.query(_long_id_query())


# --- query_many: independent reads bundled into ONE /composite call ------------
# Up to 25 subrequests bill as a single API call (unlike /composite/batch, which
# bills each). Only for reads that do not feed each other.

class _CompositeOrg:
    def __init__(self, bodies, status=200):
        self.bodies, self.status = bodies, status
        self.calls: list[dict] = []

    def __call__(self, method, url, data=None, headers=None, timeout=None):
        path = url.split("salesforce.com", 1)[-1]
        self.calls.append({"method": method, "path": path,
                           "body": json.loads(data) if data else None})
        if path.endswith("/composite"):
            subs = self.calls[-1]["body"]["compositeRequest"]
            return _FakeResponse(json.dumps({"compositeResponse": [
                {"referenceId": s["referenceId"], "httpStatusCode": self.status,
                 "body": self.bodies[i]} for i, s in enumerate(subs)]}))
        return _FakeResponse(json.dumps(self.bodies[-1]))      # nextRecordsUrl page


def _composite_client(monkeypatch, org):
    client = SfClient("headless", api_version="66.0")
    monkeypatch.setattr(client, "_run", _fake_org_display([]))
    monkeypatch.setattr("leadauto.sf.client.requests.request", org)
    return client


def test_query_many_bundles_short_reads_into_one_call(monkeypatch):
    org = _CompositeOrg([
        {"done": True, "records": [{"Id": "001A", "attributes": {"type": "Account"}}]},
        {"done": True, "records": [{"Id": "003A"}]},
    ])
    client = _composite_client(monkeypatch, org)
    out = client.query_many({"accounts": "SELECT Id FROM Account WHERE Id IN ('001A')",
                             "contacts": "SELECT Id FROM Contact WHERE Id IN ('003A')"})
    assert out == {"accounts": [{"Id": "001A"}], "contacts": [{"Id": "003A"}]}
    assert len(org.calls) == 1                       # two reads, ONE API call
    call = org.calls[0]
    assert call["method"] == "POST" and call["path"].endswith("/composite")
    subs = call["body"]["compositeRequest"]
    assert [s["method"] for s in subs] == ["GET", "GET"]
    assert all(s["url"].startswith("/services/data/v66.0/query/?q=") for s in subs)
    assert "FROM%20Account" in subs[0]["url"]        # SOQL is URL-encoded


def test_query_many_chunks_at_25_subrequests(monkeypatch):
    org = _CompositeOrg([{"done": True, "records": []}] * 25)
    client = _composite_client(monkeypatch, org)
    out = client.query_many({f"q{i}": f"SELECT Id FROM Lead WHERE Id IN ('00Q{i}')"
                             for i in range(30)})
    assert len(out) == 30
    assert [len(c["body"]["compositeRequest"]) for c in org.calls] == [25, 5]


def test_query_many_routes_long_queries_to_their_own_bulk_job(monkeypatch):
    # A query too long for a GET URL cannot ride a composite subrequest either.
    org = _BulkOrg(["Id\n00Q1\n"])
    client = _bulk_client(monkeypatch, org)
    out = client.query_many({"leads": _long_id_query()})
    assert out == {"leads": [{"Id": "00Q1"}]}
    assert org.calls[0][0] == "POST" and "/jobs/query" in org.calls[0][1]


def test_query_many_follows_next_records_url_instead_of_truncating(monkeypatch):
    # done=false must never be returned as the whole answer.
    org = _CompositeOrg([
        {"done": False, "nextRecordsUrl": "/services/data/v66.0/query/01g-2000",
         "records": [{"Id": "001A"}]},
        {"done": True, "records": [{"Id": "001B"}]},
    ])
    client = _composite_client(monkeypatch, org)
    out = client.query_many({"accounts": "SELECT Id FROM Account"})
    assert out == {"accounts": [{"Id": "001A"}, {"Id": "001B"}]}
    assert org.calls[1]["path"] == "/services/data/v66.0/query/01g-2000"


def test_query_many_raises_when_a_subrequest_fails(monkeypatch):
    org = _CompositeOrg([{"message": "No such column 'Nope'"}], status=400)
    client = _composite_client(monkeypatch, org)
    with pytest.raises(SfError, match="No such column"):
        client.query_many({"accounts": "SELECT Nope FROM Account"})


def test_query_many_empty_makes_no_call(monkeypatch):
    client = SfClient("headless")
    monkeypatch.setattr("leadauto.sf.client.requests.request",
                        lambda *a, **k: pytest.fail("must not call the org"))
    assert client.query_many({}) == {}


# --- auth_info + SOAP convert_lead --------------------------------------------
# Conversion goes over the SOAP Partner API (the org user lacks "Author Apex", so
# anonymous-Apex conversion throws System.SecurityException; SOAP honors his
# Convert Leads permission). auth_info supplies the session id + instance URL.

def _fake_org_display(capture, token="TOK1", url="https://kununu.my.salesforce.com"):
    def run(cmd, check=True):
        capture.append(cmd)
        if "show-access-token" in cmd:
            return types.SimpleNamespace(returncode=0, stdout=json.dumps(
                {"result": {"accessToken": "UNMASKED_" + token}}), stderr="")
        return types.SimpleNamespace(returncode=0, stdout=json.dumps(
            {"result": {"accessToken": token, "instanceUrl": url}}), stderr="")
    return run


def test_auth_info_parses_and_caches_org_display(monkeypatch):
    client = SfClient("headless")
    cmds: list[str] = []
    monkeypatch.setattr(client, "_run", _fake_org_display(cmds))
    info = client.auth_info()
    assert info == {"access_token": "TOK1",
                    "instance_url": "https://kununu.my.salesforce.com"}
    assert client.auth_info() is info          # cached: no second CLI call
    assert len(cmds) == 1
    assert "org display" in cmds[0] and "--json" in cmds[0]


def test_auth_info_unmasks_redacted_token_via_show_access_token(monkeypatch):
    # sf CLI >= ~2.141 REDACTS accessToken in `org display` output; the real token
    # must then come from `sf org auth show-access-token` (live-hit 2026-07-10:
    # SOAP got the literal placeholder string as its session id -> INVALID_SESSION_ID).
    client = SfClient("headless")
    cmds: list[str] = []
    monkeypatch.setattr(client, "_run", _fake_org_display(
        cmds, token="[REDACTED] Use 'sf org auth show-access-token' to view"))
    info = client.auth_info()
    assert info["access_token"].startswith("UNMASKED_")
    assert info["instance_url"] == "https://kununu.my.salesforce.com"
    assert len(cmds) == 2
    assert "show-access-token" in cmds[1]


def test_auth_info_raises_when_fields_missing(monkeypatch):
    client = SfClient("headless")

    def run(cmd, check=True):
        return types.SimpleNamespace(returncode=0,
                                     stdout=json.dumps({"result": {}}), stderr="")
    monkeypatch.setattr(client, "_run", run)
    with pytest.raises(SfError, match="accessToken|instanceUrl"):
        client.auth_info()


class _FakeResponse:
    def __init__(self, text, status_code=200, headers=None):
        self.text = text
        self.status_code = status_code
        self.headers = headers or {}

    @property
    def content(self) -> bytes:
        return self.text.encode("utf-8")


def _soap_success(lead_rows) -> str:
    results = "".join(
        f"<result><accountId>{r.get('accountId', '')}</accountId>"
        f"<contactId>{r.get('contactId', '')}</contactId>"
        f"<leadId>{r['leadId']}</leadId><success>true</success></result>"
        for r in lead_rows)
    return ('<?xml version="1.0"?><soapenv:Envelope '
            'xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" '
            'xmlns="urn:partner.soap.sforce.com"><soapenv:Body>'
            f"<convertLeadResponse>{results}</convertLeadResponse>"
            "</soapenv:Body></soapenv:Envelope>")


_SOAP_FAULT_SESSION = ('<?xml version="1.0"?><soapenv:Envelope '
                       'xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/">'
                       "<soapenv:Body><soapenv:Fault>"
                       "<faultcode>sf:INVALID_SESSION_ID</faultcode>"
                       "<faultstring>INVALID_SESSION_ID: session expired</faultstring>"
                       "</soapenv:Fault></soapenv:Body></soapenv:Envelope>")


def test_convert_lead_posts_partner_soap_and_returns_rows(monkeypatch):
    client = SfClient("headless", api_version="66.0")
    monkeypatch.setattr(client, "_run", _fake_org_display([]))
    posts: list[dict] = []

    def fake_post(url, data=None, headers=None, timeout=None):
        # the envelope is posted as UTF-8 bytes so the charset header is honest
        posts.append({"url": url, "data": data.decode("utf-8"), "headers": headers})
        return _FakeResponse(_soap_success(
            [{"leadId": "00Q1", "accountId": "001A", "contactId": "003A"}]))
    monkeypatch.setattr("leadauto.sf.client.requests.post", fake_post)

    rows = client.convert_lead([{"lead_id": "00Q1"}],
                               converted_status="Qualified", owner_id="005X")
    assert rows[0]["leadId"] == "00Q1" and rows[0]["success"] is True
    post = posts[0]
    assert post["url"] == "https://kununu.my.salesforce.com/services/Soap/u/66.0"
    assert post["headers"]["Content-Type"].startswith("text/xml")
    assert post["headers"]["SOAPAction"] == '""'
    assert "TOK1" in post["data"] and "00Q1" in post["data"]


def test_convert_lead_backfills_nil_lead_id_from_request_order(monkeypatch):
    client = SfClient("headless")
    monkeypatch.setattr(client, "_run", _fake_org_display([]))
    body = ('<?xml version="1.0"?><soapenv:Envelope '
            'xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" '
            'xmlns="urn:partner.soap.sforce.com" '
            'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"><soapenv:Body>'
            "<convertLeadResponse>"
            "<result><leadId>00Q1</leadId><success>true</success></result>"
            "<result><errors><message>bad</message><statusCode>INVALID_ID</statusCode>"
            '</errors><leadId xsi:nil="true"/><success>false</success></result>'
            "</convertLeadResponse></soapenv:Body></soapenv:Envelope>")
    monkeypatch.setattr("leadauto.sf.client.requests.post",
                        lambda *a, **k: _FakeResponse(body))
    rows = client.convert_lead([{"lead_id": "00Q1"}, {"lead_id": "00Q2"}],
                               converted_status="Qualified", owner_id=None)
    assert rows[1]["leadId"] == "00Q2"       # backfilled from request order
    assert rows[1]["success"] is False


def test_convert_lead_retries_once_on_invalid_session(monkeypatch):
    client = SfClient("headless")
    tokens = iter(["STALE", "FRESH"])
    auth_calls: list[str] = []

    def run(cmd, check=True):
        auth_calls.append(cmd)
        return types.SimpleNamespace(returncode=0, stdout=json.dumps(
            {"result": {"accessToken": next(tokens),
                        "instanceUrl": "https://x.my.salesforce.com"}}), stderr="")
    monkeypatch.setattr(client, "_run", run)
    posts: list[str] = []

    def fake_post(url, data=None, headers=None, timeout=None):
        data = data.decode("utf-8")
        posts.append(data)
        if "STALE" in data:
            return _FakeResponse(_SOAP_FAULT_SESSION, status_code=500)
        return _FakeResponse(_soap_success([{"leadId": "00Q1"}]))
    monkeypatch.setattr("leadauto.sf.client.requests.post", fake_post)

    rows = client.convert_lead([{"lead_id": "00Q1"}],
                               converted_status="Qualified", owner_id=None)
    assert rows[0]["success"] is True
    assert len(posts) == 2                    # stale session retried once
    assert len(auth_calls) == 2               # auth re-fetched after the fault
    assert "FRESH" in posts[1]


def test_convert_lead_other_fault_does_not_retry(monkeypatch):
    client = SfClient("headless")
    monkeypatch.setattr(client, "_run", _fake_org_display([]))
    posts: list[str] = []
    fault = _SOAP_FAULT_SESSION.replace("INVALID_SESSION_ID", "UNKNOWN_EXCEPTION")

    def fake_post(url, data=None, headers=None, timeout=None):
        posts.append(data)
        return _FakeResponse(fault, status_code=500)
    monkeypatch.setattr("leadauto.sf.client.requests.post", fake_post)
    with pytest.raises(SfError, match="UNKNOWN_EXCEPTION"):
        client.convert_lead([{"lead_id": "00Q1"}],
                            converted_status="Qualified", owner_id=None)
    assert len(posts) == 1


def test_convert_lead_rejects_oversized_batch(monkeypatch):
    client = SfClient("headless")
    conversions = [{"lead_id": f"00Q{i:03d}"} for i in range(101)]
    with pytest.raises(SfError, match="100"):
        client.convert_lead(conversions, converted_status="Qualified", owner_id=None)


def test_convert_lead_empty_is_noop_without_post(monkeypatch):
    client = SfClient("headless")
    monkeypatch.setattr("leadauto.sf.client.requests.post",
                        lambda *a, **k: pytest.fail("must not POST"))
    assert client.convert_lead([], converted_status="Qualified", owner_id=None) == []


