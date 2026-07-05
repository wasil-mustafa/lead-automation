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
    # Bands = the org's restricted KN_Employee__c picklist (describe on `headless`,
    # 2026-07-03), incl. the German dot thousands separators. Wrong labels -> row rejected.
    assert employee_bin(5) == "1-10"
    assert employee_bin(10) == "1-10"
    assert employee_bin(11) == "11-50"
    assert employee_bin(75) == "51-100"
    assert employee_bin(150) == "101-200"
    assert employee_bin(250) == "201-350"
    assert employee_bin(400) == "351-500"
    assert employee_bin(600) == "501-750"
    assert employee_bin(751) == "751-1.000"
    assert employee_bin(1500) == "1.001-2.500"
    assert employee_bin(3000) == "2.501-5.000"
    assert employee_bin(7500) == "5.001-10.000"
    assert employee_bin(20000) == "10.001-30.000"
    assert employee_bin(50000) == "30.001-100.000"
    assert employee_bin(100001) == "> 100.000"


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
    assert payload["KN_Employee__c"] == "201-350"
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


from leadauto.writeback import normalize_phone, split_first_name, employee_bin_from_size


class TestNormalizePhone:
    def test_strips_separators(self):
        assert normalize_phone("+49 40 41 91-31") == "+4940419131"

    def test_double_zero_becomes_plus(self):
        assert normalize_phone("0049 (40) 419131") == "+4940419131"

    def test_dots_and_slashes_stripped(self):
        assert normalize_phone("+43.1/512 34 56") == "+4315123456"

    def test_national_number_rejected(self):
        assert normalize_phone("040 419131") is None  # no country prefix — can't infer

    def test_garbage_rejected(self):
        assert normalize_phone("+49 40 CALL-ME") is None

    def test_too_short_rejected(self):
        assert normalize_phone("+4912") is None  # fewer than 6 digits

    def test_empty_and_none(self):
        assert normalize_phone("") is None
        assert normalize_phone(None) is None


class TestSplitFirstName:
    def test_leading_remainder_when_ends_with_lastname(self):
        assert split_first_name("Max Mustermann", "Mustermann") == "Max"

    def test_case_insensitive(self):
        assert split_first_name("max MUSTERMANN", "Mustermann") == "max"

    def test_multiword_first(self):
        assert split_first_name("Anna Maria Schmidt", "Schmidt") == "Anna Maria"

    def test_mismatched_lastname_skipped(self):
        assert split_first_name("Max Mustermann", "Meier") is None

    def test_name_is_only_lastname(self):
        assert split_first_name("Mustermann", "Mustermann") is None

    def test_none_inputs(self):
        assert split_first_name(None, "Meier") is None
        assert split_first_name("Max Meier", None) is None


class TestEmployeeBinFromSize:
    def test_exact_label(self):
        assert employee_bin_from_size("51-100") == "51-100"

    def test_label_with_suffix_maps_upper_bound(self):
        assert employee_bin_from_size("51-100 Mitarbeiter") == "51-100"

    def test_german_dot_upper_bound(self):
        assert employee_bin_from_size("501-1.000") == "751-1.000"  # upper bound 1000 -> its bin

    def test_open_ended(self):
        assert employee_bin_from_size("10.001+") == "10.001-30.000"

    def test_no_number(self):
        assert employee_bin_from_size("unbekannt") is None

    def test_empty(self):
        assert employee_bin_from_size("") is None
        assert employee_bin_from_size(None) is None
