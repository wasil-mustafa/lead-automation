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
