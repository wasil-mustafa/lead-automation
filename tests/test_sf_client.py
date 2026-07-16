"""Tests for the pure parsing in the sf client wrapper.

`parse_bulk_update_result` decodes `sf data update bulk --json` output into a failure summary.
The exact JSON shape is the documented best-understanding of Bulk API 2.0 output and must be
re-confirmed against a live run; the parser is defensive and reports `confirmed=False` when it
does not recognize the shape, so callers never assume an unverified success.
"""
import json
import re
import types

import pytest

from leadauto.sf.client import SfClient, SfError, parse_bulk_update_result


def test_parse_bulk_result_extracts_failures():
    out = json.dumps({"status": 0, "result": {
        "jobInfo": {"numberRecordsProcessed": 2, "numberRecordsFailed": 1},
        "records": {
            "failedResults": [{"sf__Id": "00Q1", "sf__Error": "BAD_PICKLIST: bad value"}],
            "successfulResults": [{"sf__Id": "00Q2"}],
        }}})
    r = parse_bulk_update_result(out)
    assert r["failed"] == 1
    assert r["failures"][0]["id"] == "00Q1"
    assert "BAD_PICKLIST" in r["failures"][0]["error"]
    assert r["confirmed"] is True


def test_parse_bulk_result_all_success():
    out = json.dumps({"status": 0, "result": {
        "jobInfo": {"numberRecordsProcessed": 3, "numberRecordsFailed": 0},
        "records": {"failedResults": []}}})
    r = parse_bulk_update_result(out)
    assert r["failed"] == 0
    assert r["failures"] == []
    assert r["confirmed"] is True


def test_parse_bulk_result_unrecognized_is_unconfirmed():
    r = parse_bulk_update_result("not json at all")
    assert r["confirmed"] is False
    assert r["failed"] == 0


# --- update_bulk: partial row failures exit the CLI non-zero -------------------
# `sf data update bulk --json` exits 1 when the job finishes with failed records
# (live-hit 2026-07-15, run 29430746090: 626-row backlog write-back crashed with the
# row errors discarded — they are on stdout, not stderr). update_bulk must parse
# stdout regardless of exit code and only raise when no row-level detail is present.
# subprocess.run is the mock boundary so the real _run/update_bulk interplay is tested.

def _fake_subprocess(returncode, stdout, stderr=""):
    def run(cmd, **kwargs):
        return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)
    return run


def test_update_bulk_partial_failure_returns_row_failures(monkeypatch):
    out = json.dumps({"status": 1, "result": {
        "jobInfo": {"numberRecordsProcessed": 2, "numberRecordsFailed": 1},
        "records": {
            "failedResults": [{"sf__Id": "00Q1",
                               "sf__Error": "FIELD_CUSTOM_VALIDATION_EXCEPTION: bad postal"}],
            "successfulResults": [{"sf__Id": "00Q2"}],
        }}})
    monkeypatch.setattr("leadauto.sf.client.subprocess.run", _fake_subprocess(1, out))
    client = SfClient("headless")
    r = client.update_bulk("Lead", [{"Id": "00Q1", "Email": "a@b.c"},
                                    {"Id": "00Q2", "Email": "c@d.e"}])
    assert r["failed"] == 1
    assert r["failures"][0]["id"] == "00Q1"
    assert "FIELD_CUSTOM_VALIDATION_EXCEPTION" in r["failures"][0]["error"]
    assert r["confirmed"] is True


def _failed_details_error(job_id="750Mz00000z2eflIAA"):
    # Real shape live-captured 2026-07-16 (run 29497405506): on partial failure the CLI
    # emits an SfCommandError envelope with the job id, NOT the success shape.
    return json.dumps({
        "name": "FailedRecordDetailsError",
        "message": "Job finished being processed but failed to process 36 records.",
        "exitCode": 1,
        "data": {"jobId": job_id, "state": "JobComplete"},
        "code": "FailedRecordDetailsError",
        "status": 1,
    })


def test_update_bulk_fetches_row_failures_via_bulk_results(monkeypatch, tmp_path):
    failed_csv = tmp_path / "750Mz00000z2eflIAA-failed-records.csv"
    failed_csv.write_text('"sf__Id","sf__Error","Id","Email"\n'
                          '"00Q1","DUPLICATES_DETECTED:duplicate record:--","00Q1","a@b.c"\n',
                          encoding="utf-8")
    results_json = json.dumps({"status": 0, "result": {
        "processedRecords": 2, "successfulRecords": 1, "failedRecords": 1,
        "status": "JobComplete", "operation": "update", "object": "Lead",
        "successFilePath": str(tmp_path / "success.csv"),
        "failedFilePath": str(failed_csv)}})
    cmds: list[str] = []

    def run(cmd, **kwargs):
        cmds.append(cmd)
        if "data bulk results" in cmd:
            return types.SimpleNamespace(returncode=0, stdout=results_json, stderr="")
        return types.SimpleNamespace(returncode=1, stdout=_failed_details_error(), stderr="")
    monkeypatch.setattr("leadauto.sf.client.subprocess.run", run)
    client = SfClient("headless")
    r = client.update_bulk("Lead", [{"Id": "00Q1", "Email": "a@b.c"},
                                    {"Id": "00Q2", "Email": "c@d.e"}])
    assert r["failed"] == 1
    assert r["failures"] == [{"id": "00Q1",
                              "error": "DUPLICATES_DETECTED:duplicate record:--"}]
    assert r["confirmed"] is True
    assert any("data bulk results" in c and "750Mz00000z2eflIAA" in c for c in cmds)


def test_update_bulk_raises_when_bulk_results_yields_no_failures(monkeypatch, tmp_path):
    # A job id but no retrievable row failures (e.g. whole-job death) must still raise —
    # never report an unverified partial failure as handled.
    empty_csv = tmp_path / "failed.csv"
    empty_csv.write_text('"sf__Id","sf__Error"\n', encoding="utf-8")
    results_json = json.dumps({"status": 0, "result": {
        "failedRecords": 0, "status": "JobComplete",
        "failedFilePath": str(empty_csv)}})

    def run(cmd, **kwargs):
        if "data bulk results" in cmd:
            return types.SimpleNamespace(returncode=0, stdout=results_json, stderr="")
        return types.SimpleNamespace(returncode=1, stdout=_failed_details_error(), stderr="")
    monkeypatch.setattr("leadauto.sf.client.subprocess.run", run)
    client = SfClient("headless")
    with pytest.raises(SfError):
        client.update_bulk("Lead", [{"Id": "00Q1", "Email": "a@b.c"}])


def test_update_bulk_nonzero_exit_without_row_detail_raises_with_stdout(monkeypatch):
    # Whole-job failure (or unrecognized shape): raise, but keep the stdout payload in
    # the error — that is where the CLI puts its diagnostics, stderr only carries noise.
    monkeypatch.setattr("leadauto.sf.client.subprocess.run",
                        _fake_subprocess(1, "boom: job aborted", "update warning"))
    client = SfClient("headless")
    with pytest.raises(SfError, match="boom: job aborted"):
        client.update_bulk("Lead", [{"Id": "00Q1", "Email": "a@b.c"}])


def test_update_bulk_nonzero_exit_with_recognized_but_clean_result_raises(monkeypatch):
    # Exit 1 with a recognized shape but zero row failures = job-level failure that
    # must not be reported as success.
    out = json.dumps({"status": 1, "result": {
        "jobInfo": {"numberRecordsProcessed": 0, "numberRecordsFailed": 0},
        "records": {"failedResults": []}}})
    monkeypatch.setattr("leadauto.sf.client.subprocess.run", _fake_subprocess(1, out))
    client = SfClient("headless")
    with pytest.raises(SfError):
        client.update_bulk("Lead", [{"Id": "00Q1", "Email": "a@b.c"}])


def test_update_bulk_clean_exit_still_returns_summary(monkeypatch):
    out = json.dumps({"status": 0, "result": {
        "jobInfo": {"numberRecordsProcessed": 1, "numberRecordsFailed": 0},
        "records": {"failedResults": []}}})
    monkeypatch.setattr("leadauto.sf.client.subprocess.run", _fake_subprocess(0, out))
    client = SfClient("headless")
    r = client.update_bulk("Lead", [{"Id": "00Q1", "Email": "a@b.c"}])
    assert r == {"processed": 1, "failed": 0, "failures": [], "confirmed": True}


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
