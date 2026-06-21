"""Tests for the pure parsing in the sf client wrapper.

`parse_bulk_update_result` decodes `sf data update bulk --json` output into a failure summary.
The exact JSON shape is the documented best-understanding of Bulk API 2.0 output and must be
re-confirmed against a live run; the parser is defensive and reports `confirmed=False` when it
does not recognize the shape, so callers never assume an unverified success.
"""
import json

from leadauto.sf.client import parse_bulk_update_result


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
