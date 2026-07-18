"""Conversion tests (Module 3c) — SOAP-based.

Anonymous-Apex conversion is impossible in the org (the pipeline user lacks "Author
Apex"; Database.convertLead throws System.SecurityException in restricted mode), so
`convert_leads` drives `client.convert_lead` (SOAP Partner API) instead. The org
boundary is a FakeSoapClient answering the LeadStatus/IsConverted SOQL and echoing
success per conversion, so batching + reconciliation + aggregation run end to end
with no live conversion.
"""
import logging

import pytest

from leadauto.config import Settings
from leadauto.convert import (
    ConversionTarget,
    convert_leads,
    enrich_contact_email2,
)

SETTINGS = Settings.load()


class FakeSoapClient:
    """query() answers the converted-status + already-converted SOQL;
    convert_lead() echoes success (new account/contact) per conversion."""

    def __init__(self, statuses=("Qualified",), already_converted=()):
        self.statuses = list(statuses)
        self.already_converted = {i for i in already_converted}
        self.queries: list[str] = []
        self.convert_calls: list[dict] = []

    def query(self, soql, bulk=False):
        self.queries.append(soql)
        if "FROM LeadStatus" in soql:
            return [{"MasterLabel": s} for s in self.statuses]
        if "FROM Lead" in soql and "IsConverted = true" in soql:
            return [{"Id": i} for i in self.already_converted if i in soql]
        return []

    def convert_lead(self, conversions, *, converted_status, owner_id):
        self.convert_calls.append({"conversions": conversions,
                                   "converted_status": converted_status,
                                   "owner_id": owner_id})
        return [{"leadId": c["lead_id"], "success": True,
                 "accountId": "001NEW", "contactId": "003NEW"} for c in conversions]


# --- convert_leads orchestration ---------------------------------------------

def test_convert_leads_dry_run_makes_no_calls():
    client = FakeSoapClient()
    targets = [ConversionTarget("00Q1", account_id="001A"), ConversionTarget("00Q2")]
    res = convert_leads(client, targets, SETTINGS, dry_run=True)
    assert client.convert_calls == [] and client.queries == []
    assert res["converted"] == 0
    assert res["planned"] == 2


def test_convert_leads_live_converts_and_aggregates():
    client = FakeSoapClient()
    targets = [ConversionTarget("00Q1", account_id="001A", contact_id="003C"),
               ConversionTarget("00Q2")]
    res = convert_leads(client, targets, SETTINGS, dry_run=False, batch_size=50)
    assert len(client.convert_calls) == 1
    call = client.convert_calls[0]
    # match-aware: the linked lead carries its account/contact ids, create-new does not
    assert call["conversions"] == [
        {"lead_id": "00Q1", "account_id": "001A", "contact_id": "003C"},
        {"lead_id": "00Q2", "account_id": None, "contact_id": None},
    ]
    assert call["converted_status"] == SETTINGS.converted_status
    assert call["owner_id"] == SETTINGS.owner_id
    assert res["converted"] == 2
    assert res["failed"] == 0
    assert len(res["results"]) == 2


def test_convert_leads_batches_by_size():
    client = FakeSoapClient()
    targets = [ConversionTarget(f"00Q{i}") for i in range(120)]
    res = convert_leads(client, targets, SETTINGS, dry_run=False, batch_size=50)
    assert len(client.convert_calls) == 3          # 50 + 50 + 20
    assert res["converted"] == 120
    # the converted-status SOQL runs once for the whole run, not per batch
    assert sum(1 for q in client.queries if "FROM LeadStatus" in q) == 1


def test_convert_leads_empty_is_noop():
    client = FakeSoapClient()
    res = convert_leads(client, [], SETTINGS, dry_run=False)
    assert client.convert_calls == [] and client.queries == []
    assert res["converted"] == 0 and res["planned"] == 0


def test_convert_leads_prefers_configured_status_falls_back_to_first():
    client = FakeSoapClient(statuses=("Closed - Converted", "Qualified"))
    convert_leads(client, [ConversionTarget("00Q1")], SETTINGS, dry_run=False)
    # config says Qualified and the org has it -> Qualified wins over the first row
    assert client.convert_calls[0]["converted_status"] == "Qualified"

    client2 = FakeSoapClient(statuses=("Closed - Converted",))
    convert_leads(client2, [ConversionTarget("00Q1")], SETTINGS, dry_run=False)
    assert client2.convert_calls[0]["converted_status"] == "Closed - Converted"


def test_convert_leads_no_converted_status_fails_all_without_converting():
    client = FakeSoapClient(statuses=())
    targets = [ConversionTarget("00Q1"), ConversionTarget("00Q2")]
    res = convert_leads(client, targets, SETTINGS, dry_run=False)
    assert client.convert_calls == []
    assert res["failed"] == 2 and res["converted"] == 0 and res["missing"] == 0
    assert all("No converted LeadStatus" in r["errors"][0] for r in res["results"])


def test_convert_leads_already_converted_is_noop_not_missing():
    # Re-running an already-converted batch is a clean no-op: the pre-query finds the
    # leads IsConverted=true, they get explicit alreadyConverted rows, never reach the
    # SOAP call, and count as `already_converted`, not `missing`/`failed`/`converted`.
    client = FakeSoapClient(already_converted=("00Q1", "00Q2"))
    targets = [ConversionTarget("00Q1"), ConversionTarget("00Q2")]
    res = convert_leads(client, targets, SETTINGS, dry_run=False)
    assert client.convert_calls == []              # nothing left to convert
    assert res["already_converted"] == 2
    assert res["converted"] == 0
    assert res["failed"] == 0
    assert res["missing"] == 0 and res["missing_ids"] == []


def test_convert_leads_already_converted_matches_18_char_ids():
    # The org returns 18-char Ids; a 15-char target must still reconcile as already
    # converted (case-sensitive 15-char prefix comparison).
    lead15 = "00QMz00000kDmtx"
    client = FakeSoapClient(already_converted=(lead15 + "MAC",))
    client.already_converted = {lead15 + "MAC"}

    def query(soql, bulk=False):
        client.queries.append(soql)
        if "FROM LeadStatus" in soql:
            return [{"MasterLabel": "Qualified"}]
        if "IsConverted = true" in soql:
            return [{"Id": lead15 + "MAC"}]        # 18-char org form
        return []
    client.query = query
    res = convert_leads(client, [ConversionTarget(lead15)], SETTINGS, dry_run=False)
    assert res["already_converted"] == 1 and res["missing"] == 0


def test_convert_leads_preverified_skips_preconvert_queries():
    # Stage 2's preflight query already selects IsConverted and drops converted leads,
    # so preverified=True makes the per-batch IsConverted pre-query redundant — only
    # the once-per-run converted-status SOQL remains (was 77 extra queries on the
    # 230-lead backlog run at batch_size 3).
    client = FakeSoapClient()
    targets = [ConversionTarget(f"00Q{i}") for i in range(7)]
    res = convert_leads(client, targets, SETTINGS, dry_run=False, batch_size=3,
                        preverified=True)
    assert res["converted"] == 7
    assert sum(1 for q in client.queries
               if "FROM Lead " in q and "IsConverted = true" in q) == 0
    assert sum(1 for q in client.queries if "FROM LeadStatus" in q) == 1


def test_convert_leads_default_keeps_per_batch_preconvert_query():
    # Without preverification (the --no-preflight escape hatch), the per-batch
    # idempotency pre-query must keep running.
    client = FakeSoapClient()
    targets = [ConversionTarget(f"00Q{i}") for i in range(7)]
    convert_leads(client, targets, SETTINGS, dry_run=False, batch_size=3)
    assert sum(1 for q in client.queries
               if "FROM Lead " in q and "IsConverted = true" in q) == 3


def test_convert_leads_preverified_race_counts_already_converted(caplog):
    # A lead converted (e.g. by a human) between preflight and the SOAP call comes
    # back as a per-lead CANNOT_UPDATE_CONVERTED_LEAD error. The desired end state
    # (lead converted) already holds — count it already_converted, keep the run green.
    class _RacingClient(FakeSoapClient):
        def convert_lead(self, conversions, *, converted_status, owner_id):
            rows = []
            for c in conversions:
                if c["lead_id"] == "00Q2":
                    rows.append({"leadId": c["lead_id"], "success": False,
                                 "errors": ["CANNOT_UPDATE_CONVERTED_LEAD: cannot "
                                            "update a converted lead"]})
                else:
                    rows.append({"leadId": c["lead_id"], "success": True,
                                 "accountId": "001NEW"})
            return rows

    client = _RacingClient()
    targets = [ConversionTarget("00Q1"), ConversionTarget("00Q2")]
    with caplog.at_level(logging.WARNING):
        res = convert_leads(client, targets, SETTINGS, dry_run=False, preverified=True)
    assert res["converted"] == 1
    assert res["already_converted"] == 1
    assert res["failed"] == 0 and res["missing"] == 0
    assert any("00Q2" in r.message and "already converted" in r.message.lower()
               for r in caplog.records)


def test_convert_leads_reports_leads_missing_from_results():
    # A lead the SOAP call never returns a row for must surface as `missing`,
    # never silently drop from the counts.
    class _PartialClient(FakeSoapClient):
        def convert_lead(self, conversions, *, converted_status, owner_id):
            first = conversions[0]
            return [{"leadId": first["lead_id"], "success": True}]

    client = _PartialClient()
    targets = [ConversionTarget("00Q1"), ConversionTarget("00Q2")]
    res = convert_leads(client, targets, SETTINGS, dry_run=False)
    assert res["converted"] == 1
    assert res["missing"] == 1
    assert res["missing_ids"] == ["00Q2"]


def test_convert_leads_failure_rows_aggregate_as_failed():
    class _FailingClient(FakeSoapClient):
        def convert_lead(self, conversions, *, converted_status, owner_id):
            return [{"leadId": c["lead_id"], "success": False,
                     "errors": ["DUPLICATES_DETECTED: Use one of these records?"]}
                    for c in conversions]

    client = _FailingClient()
    res = convert_leads(client, [ConversionTarget("00Q1")], SETTINGS, dry_run=False)
    assert res["failed"] == 1 and res["converted"] == 0 and res["missing"] == 0


def test_convert_leads_logs_each_failure_with_error_text(caplog):
    # Per-lead SOAP errors must land in the log — the 2026-07-17 backlog run failed
    # 115 leads and left no error text anywhere (results were only aggregated).
    class _FailingClient(FakeSoapClient):
        def convert_lead(self, conversions, *, converted_status, owner_id):
            return [{"leadId": c["lead_id"], "success": False,
                     "errors": ["LIMIT_EXCEEDED: too many SOQL queries"]}
                    for c in conversions]

    client = _FailingClient()
    with caplog.at_level(logging.ERROR):
        convert_leads(client, [ConversionTarget("00Q1")], SETTINGS, dry_run=False)
    assert any("00Q1" in r.message and "LIMIT_EXCEEDED: too many SOQL queries" in r.message
               for r in caplog.records)


def test_convert_leads_rejects_id_injection():
    # A lead id that could break out of the SOQL literal / SOAP envelope is refused.
    client = FakeSoapClient()
    with pytest.raises(ValueError, match="non-alphanumeric"):
        convert_leads(client, [ConversionTarget("00Q1'); DELETE ALL --")],
                      SETTINGS, dry_run=False)


# --- two-phase same-company clustering ---------------------------------------

class _ClusterClient(FakeSoapClient):
    """Per-primary account ids so secondaries can link; records each phase's calls."""

    def convert_lead(self, conversions, *, converted_status, owner_id):
        self.convert_calls.append({"conversions": conversions,
                                   "converted_status": converted_status,
                                   "owner_id": owner_id})
        return [{"leadId": c["lead_id"], "success": True,
                 "accountId": c.get("account_id") or f"001{c['lead_id']}"}
                for c in conversions]


def test_convert_leads_clusters_same_company_create_new():
    client = _ClusterClient()
    targets = [ConversionTarget("00Q1", lf_company_id="LF-1"),
               ConversionTarget("00Q2", lf_company_id="LF-1"),
               ConversionTarget("00Q3", lf_company_id="LF-2")]
    res = convert_leads(client, targets, SETTINGS, dry_run=False, batch_size=50)
    assert res["converted"] == 3 and res["failed"] == 0 and res["missing"] == 0
    # Phase 2 ran and linked 00Q2 to the account 00Q1 created (00100Q1).
    phase2 = client.convert_calls[-1]["conversions"]
    assert phase2 == [{"lead_id": "00Q2", "account_id": "00100Q1", "contact_id": None}]


def test_convert_leads_blank_company_not_clustered():
    client = _ClusterClient()
    targets = [ConversionTarget("00Q1"), ConversionTarget("00Q2")]  # both blank lf_company_id
    res = convert_leads(client, targets, SETTINGS, dry_run=False, batch_size=50)
    assert res["converted"] == 2
    # single phase only (no secondaries) -> exactly one SOAP call
    assert len(client.convert_calls) == 1


def test_convert_leads_orphan_secondary_when_primary_fails():
    class _PrimaryFailsClient(FakeSoapClient):
        def convert_lead(self, conversions, *, converted_status, owner_id):
            self.convert_calls.append({"conversions": conversions})
            return [{"leadId": c["lead_id"], "success": False, "errors": ["DUPLICATE"]}
                    for c in conversions]

    client = _PrimaryFailsClient()
    targets = [ConversionTarget("00Q1", lf_company_id="LF-1"),
               ConversionTarget("00Q2", lf_company_id="LF-1")]
    res = convert_leads(client, targets, SETTINGS, dry_run=False, batch_size=50)
    assert res["converted"] == 0
    assert res["failed"] == 2          # primary failed + secondary reported failed
    assert res["missing"] == 0
    assert len(client.convert_calls) == 1      # no phase 2 (primary produced no account)


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
