"""Tests for the sf client wrapper: query routing, sObject-Collections updates,
apex_run diagnostics/retry, auth_info and the SOAP convertLead path.
"""
import json
import re
import types

import pytest

from leadauto.sf.client import SfClient, SfError


# --- update_records: REST sObject Collections PATCH ---------------------------
# One request per <=200 records replaces the 5-7 round-trip Bulk API job the CLI
# used to run per update (API-call reduction, 2026-07-18). Mock boundary is
# requests.patch (the org HTTP boundary) + _run for auth_info.

def _fake_patch(responder, capture):
    def patch(url, data=None, headers=None, timeout=None):
        body = json.loads(data)
        capture.append({"url": url, "headers": headers, "body": body})
        return responder(body, headers)
    return patch


def _collections_ok(body, headers):
    return _FakeResponse(json.dumps(
        [{"id": r["Id"], "success": True, "errors": []} for r in body["records"]]))


def test_update_records_patches_composite_sobjects(monkeypatch):
    client = SfClient("headless", api_version="66.0")
    monkeypatch.setattr(client, "_run", _fake_org_display([]))
    calls: list[dict] = []
    monkeypatch.setattr("leadauto.sf.client.requests.patch",
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
    monkeypatch.setattr("leadauto.sf.client.requests.patch",
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
    monkeypatch.setattr("leadauto.sf.client.requests.patch",
                        _fake_patch(_collections_ok, calls))
    records = [{"Id": f"00Q{i:04d}", "Email": "a@b.c"} for i in range(450)]
    r = client.update_records("Lead", records)
    assert r["processed"] == 450 and r["failed"] == 0
    assert [len(c["body"]["records"]) for c in calls] == [200, 200, 50]


def test_update_records_empty_is_noop_without_patch(monkeypatch):
    client = SfClient("headless")
    monkeypatch.setattr("leadauto.sf.client.requests.patch",
                        lambda *a, **k: pytest.fail("must not PATCH"))
    assert client.update_records("Lead", []) == {
        "processed": 0, "failed": 0, "failures": [], "confirmed": True}


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
    monkeypatch.setattr("leadauto.sf.client.requests.patch",
                        _fake_patch(responder, calls))
    r = client.update_records("Lead", [{"Id": "00Q1", "Email": "a@b.c"}])
    assert r["failed"] == 0
    assert len(calls) == 2 and len(auth_calls) == 2
    assert calls[1]["headers"]["Authorization"] == "Bearer FRESH"


def test_update_records_http_error_raises_with_body(monkeypatch):
    client = SfClient("headless")
    monkeypatch.setattr(client, "_run", _fake_org_display([]))
    monkeypatch.setattr("leadauto.sf.client.requests.patch", _fake_patch(
        lambda body, headers: _FakeResponse(json.dumps([{
            "errorCode": "INVALID_FIELD", "message": "No such column 'Nope'"}]),
            status_code=400), []))
    with pytest.raises(SfError, match="No such column"):
        client.update_records("Lead", [{"Id": "00Q1", "Nope": "x"}])


# --- query routing: REST for short queries, Bulk API for long ones -----------
# Long IN(...) lists overflow the GET-URL header limit (HTTP 431); the client must
# route them through `sf data export bulk` instead. `_run` is the only mock (the org
# boundary): it records each command and simulates the two output channels.

def _fake_run(records, capture):
    def run(cmd):
        capture.append(cmd)
        if "data export bulk" in cmd:
            out = re.search(r'--output-file "([^"]+)"', cmd).group(1)
            with open(out, "w", encoding="utf-8") as fh:
                json.dump(records, fh)
            return types.SimpleNamespace(stdout="")
        return types.SimpleNamespace(stdout=json.dumps({"result": {"records": records}}))
    return run


def _long_id_query(n=300):
    ids = ", ".join(f"'{i:018d}'" for i in range(n))
    return f"SELECT Id FROM Lead WHERE Id IN ({ids}) AND IsConverted = false"


def test_query_short_uses_rest_and_strips_attributes(monkeypatch):
    client = SfClient("headless")
    cmds: list[str] = []
    monkeypatch.setattr(client, "_run",
                        _fake_run([{"Id": "00Q1", "attributes": {"type": "Lead"}}], cmds))
    out = client.query("SELECT Id FROM Lead WHERE Id IN ('00Q1')")
    assert out == [{"Id": "00Q1"}]                     # attributes stripped
    assert "data query --query" in cmds[0]
    assert "data export bulk" not in cmds[0]


def test_query_long_routes_to_bulk(monkeypatch):
    client = SfClient("headless")
    cmds: list[str] = []
    monkeypatch.setattr(client, "_run",
                        _fake_run([{"Id": "00Q1"}, {"Id": "00Q2"}], cmds))
    out = client.query(_long_id_query())
    assert [r["Id"] for r in out] == ["00Q1", "00Q2"]
    assert "data export bulk --query-file" in cmds[0]
    assert "data query --query" not in cmds[0]         # not sent as a GET-URL query


def test_query_bulk_flag_forces_bulk(monkeypatch):
    client = SfClient("headless")
    cmds: list[str] = []
    monkeypatch.setattr(client, "_run", _fake_run([{"Id": "00Q1"}], cmds))
    client.query("SELECT Id FROM Lead LIMIT 1", bulk=True)
    assert "data export bulk" in cmds[0]


# --- apex_run: diagnose failures from the --json payload, not a lack of expected output --

def _fake_apex_run(payload, capture):
    """Stand in for SfClient._run: record the cmd, return an ExecuteAnonymous --json blob."""
    def run(cmd, check=True):
        capture.append(cmd)
        return types.SimpleNamespace(returncode=0 if payload.get("success") else 1,
                                     stdout=json.dumps({"result": payload}), stderr="")
    return run


def test_apex_run_returns_logs_on_success(monkeypatch):
    client = SfClient("headless")
    cmds: list[str] = []
    logs = "USER_DEBUG|@@LEADAUTO@@[]@@LEADAUTO@@"
    monkeypatch.setattr(client, "_run", _fake_apex_run(
        {"compiled": True, "success": True, "logs": logs}, cmds))
    assert client.apex_run("System.debug('x');") == logs
    assert "--json" in cmds[0]


def test_apex_run_raises_on_compile_error(monkeypatch):
    client = SfClient("headless")
    monkeypatch.setattr(client, "_run", _fake_apex_run(
        {"compiled": False, "compileProblem": "Unexpected token", "success": False}, []))
    with pytest.raises(SfError, match="did not compile"):
        client.apex_run("this is not apex")


def test_apex_run_raises_on_runtime_exception(monkeypatch):
    client = SfClient("headless")
    monkeypatch.setattr(client, "_run", _fake_apex_run(
        {"compiled": True, "success": False,
         "exceptionMessage": "System.LimitException: Too many SOQL queries: 101",
         "exceptionStackTrace": "AnonymousBlock: line 1, column 1"}, []))
    with pytest.raises(SfError, match="Apex run failed"):
        client.apex_run("run that throws")


def test_apex_run_raises_on_unparseable_output(monkeypatch):
    client = SfClient("headless")
    monkeypatch.setattr("leadauto.sf.client._APEX_RETRY_DELAY_S", 0)

    def run(cmd, check=True):
        return types.SimpleNamespace(returncode=1, stdout="not json", stderr="boom")
    monkeypatch.setattr(client, "_run", run)
    with pytest.raises(SfError, match="no parseable"):
        client.apex_run("System.debug('x');")


# --- apex_run retry: the sf CLI intermittently dies on CI with exit 1 and EMPTY output
# (same class as the login hangs both workflows retry) — seen live 2026-07-10 on the
# first Stage-2 conversion dispatch. Retrying is safe: the conversion Apex only touches
# IsConverted=false leads and callers reconcile alreadyConverted rows.

def test_apex_run_retries_on_empty_output_then_succeeds(monkeypatch):
    client = SfClient("headless")
    monkeypatch.setattr("leadauto.sf.client._APEX_RETRY_DELAY_S", 0)
    calls: list[str] = []
    good = json.dumps({"result": {"compiled": True, "success": True, "logs": "ok-log"}})

    def run(cmd, check=True):
        calls.append(cmd)
        if len(calls) < 3:
            return types.SimpleNamespace(returncode=1, stdout="", stderr="")
        return types.SimpleNamespace(returncode=0, stdout=good, stderr="")
    monkeypatch.setattr(client, "_run", run)
    assert client.apex_run("System.debug('x');") == "ok-log"
    assert len(calls) == 3


def test_apex_run_gives_up_after_three_unparseable_attempts(monkeypatch):
    client = SfClient("headless")
    monkeypatch.setattr("leadauto.sf.client._APEX_RETRY_DELAY_S", 0)
    calls: list[str] = []

    def run(cmd, check=True):
        calls.append(cmd)
        return types.SimpleNamespace(returncode=1, stdout="", stderr="")
    monkeypatch.setattr(client, "_run", run)
    with pytest.raises(SfError, match="no parseable"):
        client.apex_run("System.debug('x');")
    assert len(calls) == 3


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
    def __init__(self, text, status_code=200):
        self.text = text
        self.status_code = status_code


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


def test_apex_run_does_not_retry_apex_level_failures(monkeypatch):
    """A parseable failure (compile error / runtime exception) is real — never re-run."""
    client = SfClient("headless")
    monkeypatch.setattr("leadauto.sf.client._APEX_RETRY_DELAY_S", 0)
    calls: list[str] = []

    def run(cmd, check=True):
        calls.append(cmd)
        return types.SimpleNamespace(returncode=1, stdout=json.dumps({"result": {
            "compiled": False, "compileProblem": "Unexpected token", "success": False}}),
            stderr="")
    monkeypatch.setattr(client, "_run", run)
    with pytest.raises(SfError, match="did not compile"):
        client.apex_run("bad apex")
    assert len(calls) == 1
