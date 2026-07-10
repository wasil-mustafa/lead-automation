"""Conversion tests (Module 3c).

The generated Apex is asserted as a string (match-aware setAccountId/setContactId, bulk
convertLead with allOrNone=false, IsConverted re-filter, sentinel-wrapped JSON). The org
boundary is a FakeApexClient that echoes a serialized result for the lead ids it sees, so
`convert_leads` batching + parsing + aggregation run end to end with no live conversion.
"""
import json
import re

import pytest

from leadauto.config import Settings
from leadauto.convert import (
    SENTINEL,
    ConversionTarget,
    build_convert_apex,
    convert_leads,
    enrich_contact_email2,
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


def test_build_apex_always_emits_sentinel_and_guards_exceptions():
    # An uncaught Apex exception must not swallow the whole batch: the run is wrapped in
    # try/catch and the sentinel is always printed at ERROR level.
    apex = build_convert_apex([ConversionTarget("00Q1")], owner_id=None,
                              converted_status="Qualified")
    assert "try {" in apex and "catch (Exception" in apex
    assert "LoggingLevel.ERROR" in apex
    assert apex.count(SENTINEL) == 2   # one JSON.serialize call, opened + closed


def test_build_apex_rejects_id_injection():
    # A value that could break out of the Apex string literal is refused, not escaped.
    with pytest.raises(ValueError, match="non-alphanumeric"):
        build_convert_apex([ConversionTarget("00Q1'); System.abortJob('x")],
                           owner_id=None, converted_status="Qualified")


def test_build_apex_reports_already_converted_leads():
    # Already-converted inputs must get an explicit result row (idempotent no-op) rather
    # than being silently filtered out and later counted as `missing`.
    apex = build_convert_apex([ConversionTarget("00Q1")], owner_id=None,
                              converted_status="Qualified")
    assert "IsConverted = true" in apex
    assert "'alreadyConverted'" in apex


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


class _PartialApexClient:
    """Echoes a result only for the first lead of each batch; the rest go unaccounted for."""
    def apex_run(self, apex: str) -> str:
        head = apex.split("Map<Id,Id>", 1)[0]
        ids = list(dict.fromkeys(re.findall(r"'(00Q[0-9A-Za-z]+)'", head)))
        out = [{"leadId": ids[0], "success": True}] if ids else []
        return f"{SENTINEL}{json.dumps(out)}{SENTINEL}"


def test_convert_leads_reports_leads_missing_from_results():
    # A lead the Apex never returns at all (a run that died before the sentinel) must
    # surface as `missing`, never silently drop from the counts.
    client = _PartialApexClient()
    targets = [ConversionTarget("00Q1"), ConversionTarget("00Q2")]
    res = convert_leads(client, targets, SETTINGS, dry_run=False)
    assert res["converted"] == 1
    assert res["missing"] == 1
    assert res["missing_ids"] == ["00Q2"]


class _AlreadyConvertedApexClient:
    """Simulates a re-run: every input lead is already converted, so the Apex returns an
    `alreadyConverted` row for each (no fresh conversion happens)."""
    def apex_run(self, apex: str) -> str:
        head = apex.split("Map<Id,Id>", 1)[0]
        ids = list(dict.fromkeys(re.findall(r"'(00Q[0-9A-Za-z]+)'", head)))
        out = [{"leadId": i, "success": True, "alreadyConverted": True} for i in ids]
        return f"{SENTINEL}{json.dumps(out)}{SENTINEL}"


def test_convert_leads_already_converted_is_noop_not_missing():
    # Re-running an already-converted batch is a clean no-op: leads count as
    # `already_converted`, not `missing`/`failed`/`converted`, so the job stays green.
    client = _AlreadyConvertedApexClient()
    targets = [ConversionTarget("00Q1"), ConversionTarget("00Q2")]
    res = convert_leads(client, targets, SETTINGS, dry_run=False)
    assert res["already_converted"] == 2
    assert res["converted"] == 0
    assert res["failed"] == 0
    assert res["missing"] == 0 and res["missing_ids"] == []


def test_convert_leads_no_sentinel_marks_whole_batch_missing():
    class _SilentClient:
        def apex_run(self, apex: str) -> str:
            return "compiled, but produced no sentinel"

    res = convert_leads(_SilentClient(), [ConversionTarget("00Q1"), ConversionTarget("00Q2")],
                        SETTINGS, dry_run=False)
    assert res["converted"] == 0
    assert res["missing"] == 2
    assert set(res["missing_ids"]) == {"00Q1", "00Q2"}


# --- two-phase same-company clustering ---------------------------------------

class _ClusterApexClient:
    """Per-primary account ids so secondaries can link; records each phase's apex."""
    def __init__(self):
        self.calls = []

    def apex_run(self, apex: str) -> str:
        self.calls.append(apex)
        head = apex.split("Map<Id,Id>", 1)[0]
        ids = list(dict.fromkeys(re.findall(r"'(00Q[0-9A-Za-z]+)'", head)))
        out = []
        for i in ids:
            # a lead already linked (appears in acctByLead map) echoes that account;
            # otherwise it "creates" account 001<lead>.
            out.append({"leadId": i, "success": True, "accountId": f"001{i}"})
        return f"{SENTINEL}{json.dumps(out)}{SENTINEL}"


def test_convert_leads_clusters_same_company_create_new():
    client = _ClusterApexClient()
    targets = [ConversionTarget("00Q1", lf_company_id="LF-1"),
               ConversionTarget("00Q2", lf_company_id="LF-1"),
               ConversionTarget("00Q3", lf_company_id="LF-2")]
    res = convert_leads(client, targets, SETTINGS, dry_run=False, batch_size=50)
    assert res["converted"] == 3 and res["failed"] == 0 and res["missing"] == 0
    # Phase 2 ran and linked 00Q2 to the account 00Q1 created (00100Q1).
    phase2 = client.calls[-1]
    assert "00Q2" in phase2 and "'00Q2' => '00100Q1'" in phase2


def test_convert_leads_blank_company_not_clustered():
    client = _ClusterApexClient()
    targets = [ConversionTarget("00Q1"), ConversionTarget("00Q2")]  # both blank lf_company_id
    res = convert_leads(client, targets, SETTINGS, dry_run=False, batch_size=50)
    assert res["converted"] == 2
    # single phase only (no secondaries) -> exactly one apex call
    assert len(client.calls) == 1


# --- post-convert KN_Email2__c enrichment on NEW Contacts ---------------------

class _Email2Client:
    """query() answers the Contact SOQL; update_bulk records what would be written."""
    def __init__(self, contacts):
        self.contacts = contacts
        self.queries: list[str] = []
        self.updates: list[tuple] = []

    def query(self, soql, bulk=False):
        self.queries.append(soql)
        return [dict(c) for c in self.contacts]

    def update_bulk(self, sobject, records):
        self.updates.append((sobject, records))
        return {"processed": len(records), "failed": 0}


def test_email2_written_when_lf_email_differs():
    client = _Email2Client([{"Id": "003NEW", "Email": "task@acme.de", "KN_Email2__c": ""}])
    targets = [ConversionTarget("00Q1", lf_contact_email="lf.contact@acme.de")]
    results = [{"leadId": "00Q1", "success": True,
                "accountId": "001NEW", "contactId": "003NEW"}]
    out = enrich_contact_email2(client, targets, results)
    assert out == {"processed": 1, "failed": 0}
    sobject, records = client.updates[0]
    assert sobject == "Contact"
    assert records == [{"Id": "003NEW", "KN_Email2__c": "lf.contact@acme.de"}]


def test_email2_skipped_when_emails_equal_or_already_set():
    client = _Email2Client([
        {"Id": "003AAA", "Email": "Same@acme.de", "KN_Email2__c": ""},       # equal (case-insensitive)
        {"Id": "003BBB", "Email": "task@acme.de", "KN_Email2__c": "old@x.de"},  # fill-only-blank
    ])
    targets = [ConversionTarget("00Q1", lf_contact_email="same@acme.de"),
               ConversionTarget("00Q2", lf_contact_email="lf@acme.de")]
    results = [{"leadId": "00Q1", "success": True, "contactId": "003AAA"},
               {"leadId": "00Q2", "success": True, "contactId": "003BBB"}]
    out = enrich_contact_email2(client, targets, results)
    assert out == {"processed": 0, "failed": 0}
    assert client.updates == []


def test_email2_never_touches_linked_existing_contacts():
    # A target that linked an existing Contact (contact_id set) is out of scope even if
    # the enrichment carried an LF email; without any candidate, no SOQL runs at all.
    client = _Email2Client([])
    targets = [ConversionTarget("00Q1", account_id="001A", contact_id="003OLD",
                                lf_contact_email="lf@acme.de")]
    results = [{"leadId": "00Q1", "success": True, "contactId": "003OLD"}]
    out = enrich_contact_email2(client, targets, results)
    assert out == {"processed": 0, "failed": 0}
    assert client.queries == [] and client.updates == []


def test_email2_ignores_failed_and_already_converted_rows():
    client = _Email2Client([])
    targets = [ConversionTarget("00Q1", lf_contact_email="lf@acme.de"),
               ConversionTarget("00Q2", lf_contact_email="lf2@acme.de")]
    results = [{"leadId": "00Q1", "success": False, "errors": ["DUPLICATE"]},
               {"leadId": "00Q2", "success": True, "alreadyConverted": True}]
    out = enrich_contact_email2(client, targets, results)
    assert out == {"processed": 0, "failed": 0}
    assert client.queries == []


def test_email2_failure_never_raises():
    class _Boom:
        def query(self, soql, bulk=False):
            raise RuntimeError("org down")

    targets = [ConversionTarget("00Q1", lf_contact_email="lf@acme.de")]
    results = [{"leadId": "00Q1", "success": True, "contactId": "003NEW"}]
    out = enrich_contact_email2(_Boom(), targets, results)
    assert out["processed"] == 0 and out["failed"] == 1


def test_convert_leads_orphan_secondary_when_primary_fails():
    class _PrimaryFailsClient:
        def __init__(self):
            self.calls = []

        def apex_run(self, apex):
            self.calls.append(apex)
            head = apex.split("Map<Id,Id>", 1)[0]
            ids = list(dict.fromkeys(re.findall(r"'(00Q[0-9A-Za-z]+)'", head)))
            out = [{"leadId": i, "success": False, "errors": ["DUPLICATE"]} for i in ids]
            return f"{SENTINEL}{json.dumps(out)}{SENTINEL}"

    client = _PrimaryFailsClient()
    targets = [ConversionTarget("00Q1", lf_company_id="LF-1"),
               ConversionTarget("00Q2", lf_company_id="LF-1")]
    res = convert_leads(client, targets, SETTINGS, dry_run=False, batch_size=50)
    assert res["converted"] == 0
    assert res["failed"] == 2          # primary failed + secondary reported failed
    assert res["missing"] == 0
    assert len(client.calls) == 1      # no phase 2 (primary produced no account)
