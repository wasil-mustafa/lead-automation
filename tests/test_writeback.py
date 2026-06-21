"""Write-back tests (Module 3b).

The field mapping is pure (EnrichedLead -> {Id, KN_*}); the bulk write goes through a
FakeClient so we assert the dry-run rail (no call) and the live path (one update_bulk call)
without touching an org.
"""
from datetime import date

from leadauto.extract.models import ExtractedLead
from leadauto.leadfeeder.enrich import EnrichedLead
from leadauto.writeback import (
    employee_bin,
    lead_field_payload,
    revenue_millions,
    write_back,
)


def _enriched(extracted=None, company=None, **kw) -> EnrichedLead:
    e = extracted or ExtractedLead(lead_id="00Q1", task_id="T1", task_type="claimer")
    return EnrichedLead(extracted=e, company=company or {}, **kw)


# --- transforms --------------------------------------------------------------

def test_employee_bin_bands():
    assert employee_bin(5) == "1-10"
    assert employee_bin(75) == "51-200"
    assert employee_bin(250) == "201-500"
    assert employee_bin(20000) == ">10000"


def test_employee_bin_empty_is_none():
    assert employee_bin(None) is None
    assert employee_bin("") is None
    assert employee_bin(0) is None


def test_revenue_millions():
    assert revenue_millions(5_000_000) == 5.0
    assert revenue_millions("12500000") == 12.5
    assert revenue_millions(None) is None
    assert revenue_millions("") is None


# --- field mapping -----------------------------------------------------------

def test_payload_maps_kununu_fields_and_marker():
    e = ExtractedLead(
        lead_id="00Q1", task_id="T1", task_type="claimer",
        company_name="Hufeland Klinikum GmbH",
        kununu_url="https://www.kununu.com/de/hufeland-klinikum",
        kununu_user_uuid="uuid-1", region="Germany",
        profile_type="claimed", claimed_at=date(2026, 6, 1),
    )
    payload = lead_field_payload(_enriched(e))
    assert payload["Id"] == "00Q1"
    assert payload["KN_KununuProfileName__c"] == "Hufeland Klinikum GmbH"  # processed marker
    assert payload["KN_KununuLinkProfile__c"] == "https://www.kununu.com/de/hufeland-klinikum"
    assert payload["KN_KununuUserUUID__c"] == "uuid-1"
    assert payload["KN_Region__c"] == "Germany"
    assert payload["KN_KununuProfileClaimed__c"] == "true"
    assert payload["KN_KununuProfileClaimedAt__c"] == "2026-06-01"


def test_payload_omits_empty_fields():
    e = ExtractedLead(lead_id="00Q1", task_id="T1", task_type="content")
    payload = lead_field_payload(_enriched(e))
    assert payload == {"Id": "00Q1"}  # nothing to write but the key


def test_payload_maps_firmographics_from_leadfeeder():
    company = {
        "Street": "Hauptstr. 1", "Postal_Code": "10115", "City": "Berlin",
        "Country": "DE", "Employee_Count": 250, "Revenue": 5_000_000,
        "VAT_Id": "DE811569869", "Industry": "Hospitals",
        "Company_LinkedIn": "https://linkedin.com/company/acme",
        "Company_Email": "info@acme.de", "Register_Id": "DB-42",
    }
    payload = lead_field_payload(_enriched(company=company, matched_name="Acme AG"))
    assert payload["KN_DBLegalCompanyName__c"] == "Acme AG"
    assert payload["KN_DBStreetHouseNumber__c"] == "Hauptstr. 1"
    assert payload["KN_DBPostalCode__c"] == "10115"
    assert payload["KN_DBCity__c"] == "Berlin"
    assert payload["KN_DBISO2CountryCode__c"] == "DE"
    assert payload["KN_DBNumberofEmployees__c"] == 250
    assert payload["KN_Employee__c"] == "201-500"
    assert payload["KN_DBRevenueinMillions__c"] == 5.0
    assert payload["KN_VATNumber__c"] == "DE811569869"
    assert payload["KN_DBMainIndustryTextperWZ08__c"] == "Hospitals"
    assert payload["KN_LinkedInProfileLink__c"] == "https://linkedin.com/company/acme"
    assert payload["KN_CompanyEmail__c"] == "info@acme.de"
    assert payload["KN_DBID__c"] == "DB-42"


# --- write_back orchestration ------------------------------------------------

class FakeClient:
    def __init__(self):
        self.calls: list[tuple] = []

    def update_bulk(self, sobject, records):
        self.calls.append((sobject, records))
        return {"processed": len(records)}


def test_write_back_dry_run_makes_no_calls():
    client = FakeClient()
    e = ExtractedLead(lead_id="00Q1", task_id="T1", task_type="claimer",
                      company_name="Acme GmbH")
    result = write_back(client, [_enriched(e)], dry_run=True)
    assert client.calls == []
    assert result["processed"] == 0
    assert len(result["payloads"]) == 1
    assert result["payloads"][0]["KN_KununuProfileName__c"] == "Acme GmbH"


def test_write_back_live_calls_update_bulk_once():
    client = FakeClient()
    e = ExtractedLead(lead_id="00Q1", task_id="T1", task_type="claimer",
                      company_name="Acme GmbH")
    result = write_back(client, [_enriched(e)], dry_run=False)
    assert len(client.calls) == 1
    sobject, records = client.calls[0]
    assert sobject == "Lead"
    assert records[0]["Id"] == "00Q1"
    assert result["processed"] == 1


class FailingClient:
    def update_bulk(self, sobject, records):
        return {"processed": len(records), "failed": 1,
                "failures": [{"id": records[0]["Id"], "error": "BAD_PICKLIST"}],
                "confirmed": True}


def test_write_back_surfaces_row_failures():
    e = ExtractedLead(lead_id="00Q1", task_id="T1", task_type="claimer", company_name="Acme")
    res = write_back(FailingClient(), [_enriched(e)], dry_run=False)
    assert res["failed"] == 1
    assert res["failures"][0]["lead_id"] == "00Q1"
    assert res["failures"][0]["stage"] == "writeback"


def test_write_back_skips_leads_with_no_enrichment():
    client = FakeClient()
    bare = ExtractedLead(lead_id="00Q9", task_id="T9", task_type="content")  # nothing parsed
    result = write_back(client, [_enriched(bare)], dry_run=False)
    assert client.calls == []  # only the Id, nothing to write -> skipped
    assert result["processed"] == 0
