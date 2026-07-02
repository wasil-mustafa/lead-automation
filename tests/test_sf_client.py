"""Tests for the pure parsing in the sf client wrapper.

`parse_bulk_update_result` decodes `sf data update bulk --json` output into a failure summary.
The exact JSON shape is the documented best-understanding of Bulk API 2.0 output and must be
re-confirmed against a live run; the parser is defensive and reports `confirmed=False` when it
does not recognize the shape, so callers never assume an unverified success.
"""
import json
import re
import types

from leadauto.sf.client import SfClient, parse_bulk_update_result


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
