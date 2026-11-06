"""convert/holdouts.py — swept leads the pipeline must stop retrying (2026-11-07).

Cause: lead 00QMz00000rov6DMAQ failed SOAP with DUPLICATES_DETECTED on four runs in a
row (2026-11-06) and every run exited 1. Pure file tests, no org.
"""
import json

import pytest

from leadauto.convert.holdouts import (HOLDOUT_DUPLICATE, Holdout, holdout_reason,
                                       load_holdouts, record_holdouts)

LEAD = "00QMz00000rov6DMAQ"
DUP = {"leadId": LEAD, "success": False,
       "errors": ["DUPLICATES_DETECTED: Duplicate detected!"]}


def test_missing_file_is_no_holdouts(tmp_path):
    assert load_holdouts(tmp_path / "none.jsonl") == {}


def test_malformed_row_raises_not_empty(tmp_path):
    p = tmp_path / "h.jsonl"
    p.write_text('{"lead_id": "00Q1", "reason": "r", "error": "e"}\n{broken\n', encoding="utf-8")
    with pytest.raises(ValueError, match="h.jsonl:2"):
        load_holdouts(p)
    p.write_text('{"reason": "r", "error": "e", "lead_id": ""}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="without lead_id"):
        load_holdouts(p)


def test_last_row_per_lead_wins_keyed_on_15_chars(tmp_path):
    p = tmp_path / "h.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in [
        {"lead_id": LEAD, "reason": "a", "error": "first"},
        {"lead_id": LEAD[:15], "reason": "b", "error": "second"},
    ]) + "\n", encoding="utf-8")
    out = load_holdouts(p)
    assert list(out) == [LEAD[:15]] and out[LEAD[:15]].error == "second"


@pytest.mark.parametrize("row, expected", [
    (DUP, HOLDOUT_DUPLICATE),
    ({"leadId": LEAD, "success": False,
      "errors": ["FIELD_CUSTOM_VALIDATION_EXCEPTION: x", "DUPLICATES_DETECTED: y"]},
     HOLDOUT_DUPLICATE),
    ({"leadId": LEAD, "success": True, "errors": ["DUPLICATES_DETECTED: y"]}, None),
    ({"leadId": LEAD, "success": False, "errors": ["LIMIT_EXCEEDED: Apex CPU"]}, None),
    ({"leadId": LEAD, "success": False, "errors": []}, None),
    ({"leadId": LEAD, "success": False}, None),
])
def test_only_duplicate_rule_failures_are_held_out(row, expected):
    assert holdout_reason(row) == expected


def test_record_appends_permanent_failures_once(tmp_path):
    p = tmp_path / "h.jsonl"
    rows = [DUP,
            {"leadId": "00Q2", "success": False, "errors": ["LIMIT_EXCEEDED: retry me"]},
            {"leadId": "00Q3", "success": True}]
    new = record_holdouts(rows, {LEAD[:15]: "TÜV Saarland Immobilienbewertung GmbH"}, p,
                          recorded_at="2026-11-07T10:00:00+00:00")
    assert new == [Holdout(LEAD, HOLDOUT_DUPLICATE, "DUPLICATES_DETECTED: Duplicate detected!",
                           "TÜV Saarland Immobilienbewertung GmbH", "2026-11-07T10:00:00+00:00")]
    assert [json.loads(l)["lead_id"] for l in p.read_text(encoding="utf-8").splitlines()] == [LEAD]
    # the same failure again (the lead was still swept somehow) is not duplicated
    assert record_holdouts([DUP], {}, p) == []
    assert len(p.read_text(encoding="utf-8").splitlines()) == 1


def test_record_nothing_creates_no_file(tmp_path):
    p = tmp_path / "sub" / "h.jsonl"
    assert record_holdouts([{"leadId": "00Q2", "success": False, "errors": ["X: y"]}], {}, p) == []
    assert not p.exists()
