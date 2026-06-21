"""Conversion tests (Module 3c).

The generated Apex is asserted as a string (match-aware setAccountId/setContactId, bulk
convertLead with allOrNone=false, IsConverted re-filter, sentinel-wrapped JSON). The org
boundary is a FakeApexClient that echoes a serialized result for the lead ids it sees, so
`convert_leads` batching + parsing + aggregation run end to end with no live conversion.
"""
import json
import re

from leadauto.config import Settings
from leadauto.convert import (
    SENTINEL,
    ConversionTarget,
    build_convert_apex,
    convert_leads,
    parse_results,
)

SETTINGS = Settings.load()


class FakeApexClient:
    def __init__(self):
        self.apex_calls: list[str] = []

    def apex_run(self, apex: str) -> str:
        self.apex_calls.append(apex)
        # lead ids live in the `Set<Id> leadIds` literal before the first Map<Id,Id>
        head = apex.split("Map<Id,Id>", 1)[0]
        ids = list(dict.fromkeys(re.findall(r"'(00Q[0-9A-Za-z]+)'", head)))
        out = [{"leadId": i, "success": True, "accountId": "001NEW", "contactId": "003NEW"}
               for i in ids]
        return f"45.0 APEX_CODE\nUSER_DEBUG|{SENTINEL}{json.dumps(out)}{SENTINEL}\nDONE"


# --- Apex generation ---------------------------------------------------------

def test_build_apex_is_match_aware():
    targets = [
        ConversionTarget("00Q1", account_id="001A", contact_id="003C"),  # link both
        ConversionTarget("00Q2"),                                        # create new
    ]
    apex = build_convert_apex(targets, owner_id="005OWNER", converted_status="Qualified")
    assert "'00Q1'" in apex and "'00Q2'" in apex
    assert "'00Q1' => '001A'" in apex   # account map entry
    assert "'00Q1' => '003C'" in apex   # contact map entry
    assert "setAccountId" in apex and "setContactId" in apex
    assert "setOwnerId('005OWNER')" in apex
    assert "Qualified" in apex
    assert "setDoNotCreateOpportunity(true)" in apex
    assert "Database.convertLead(conversions, false)" in apex
    assert "IsConverted = false" in apex
    assert "JSON.serialize" in apex
    assert SENTINEL in apex


def test_build_apex_omits_owner_when_absent():
    apex = build_convert_apex([ConversionTarget("00Q1")], owner_id=None,
                              converted_status="Qualified")
    assert "setOwnerId" not in apex


def test_build_apex_escapes_single_quote_in_status():
    apex = build_convert_apex([ConversionTarget("00Q1")], owner_id=None,
                              converted_status="Qual'ified")
    assert "Qual\\'ified" in apex   # apostrophe escaped for the Apex string literal
    assert "'Qual'ified'" not in apex  # unescaped form must not appear


# --- result parsing ----------------------------------------------------------

def test_parse_results_extracts_json_between_sentinels():
    payload = [{"leadId": "00Q1", "success": True, "accountId": "001A"}]
    output = f"junk before {SENTINEL}{json.dumps(payload)}{SENTINEL} junk after"
    parsed = parse_results(output)
    assert parsed == payload


def test_parse_results_no_sentinel_returns_empty():
    assert parse_results("nothing serialized here") == []


# --- convert_leads orchestration ---------------------------------------------

def test_convert_leads_dry_run_makes_no_calls():
    client = FakeApexClient()
    targets = [ConversionTarget("00Q1", account_id="001A"), ConversionTarget("00Q2")]
    res = convert_leads(client, targets, SETTINGS, dry_run=True)
    assert client.apex_calls == []
    assert res["converted"] == 0
    assert res["planned"] == 2


def test_convert_leads_live_converts_and_aggregates():
    client = FakeApexClient()
    targets = [ConversionTarget("00Q1", account_id="001A", contact_id="003C"),
               ConversionTarget("00Q2")]
    res = convert_leads(client, targets, SETTINGS, dry_run=False, batch_size=50)
    assert len(client.apex_calls) == 1
    assert res["converted"] == 2
    assert res["failed"] == 0
    assert len(res["results"]) == 2


def test_convert_leads_batches_by_size():
    client = FakeApexClient()
    targets = [ConversionTarget(f"00Q{i}") for i in range(120)]
    res = convert_leads(client, targets, SETTINGS, dry_run=False, batch_size=50)
    assert len(client.apex_calls) == 3          # 50 + 50 + 20
    assert res["converted"] == 120


def test_convert_leads_empty_is_noop():
    client = FakeApexClient()
    res = convert_leads(client, [], SETTINGS, dry_run=False)
    assert client.apex_calls == []
    assert res["converted"] == 0 and res["planned"] == 0
