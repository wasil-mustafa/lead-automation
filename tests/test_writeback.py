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
    normalize_phone,
    split_first_name,
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


# --- field mapping -----------------------------------------------------------

def test_payload_maps_kununu_fields_and_marker():
    # Task spec (2026-07-10): ONLY the kununu URL (the processed marker), Region and the
    # task contact email (-> standard Email) are written from the Task. VAT is LF-only.
    e = ExtractedLead(
        lead_id="00Q1", task_id="T1", task_type="claimer",
        company_name="Hufeland Klinikum GmbH",
        kununu_url="https://www.kununu.com/de/hufeland-klinikum",
        kununu_user_uuid="uuid-1", region="Germany",
        profile_type="claimed", claimed_at=date(2026, 6, 1),
        contact_email="max@hufeland.de", vat_number="DE811569869",
    )
    payload = lead_field_payload(_enriched(e))
    assert payload["Id"] == "00Q1"
    assert payload["KN_KununuLinkProfile__c"] == "https://www.kununu.com/de/hufeland-klinikum"
    assert payload["KN_Region__c"] == "Germany"
    assert payload["Email"] == "max@hufeland.de"
    for dropped in ("KN_KununuProfileName__c", "KN_KununuUserUUID__c",
                    "KN_KununuProfileClaimed__c", "KN_KununuProfileClaimedAt__c",
                    "KN_VATNumber__c"):
        assert dropped not in payload


def test_task_email_overwrites_differing_lead_email():
    # 2026-07-10 spec: the task email is the person who actually claimed/trialed —
    # it OVERWRITES whatever email the Lead was created with.
    e = _extracted(contact_email="max@acme.de", lead_email="existing@acme.de")
    assert lead_field_payload(EnrichedLead(extracted=e))["Email"] == "max@acme.de"


def test_payload_omits_empty_fields():
    # lead_dbid set so KN_NoOwnDBID__c (fill-only-blank) has nothing to fill either.
    e = ExtractedLead(lead_id="00Q1", task_id="T1", task_type="content", lead_dbid="12345")
    payload = lead_field_payload(_enriched(e))
    assert payload == {"Id": "00Q1"}  # nothing to write but the key


def test_payload_maps_firmographics_from_leadfeeder():
    # KN_DB* fields are D&B-integration-owned and must never appear in the payload.
    # Company LinkedIn and company email are deliberately NOT written (2026-07-10 spec:
    # LinkedIn contact-only, task email -> Email); VAT and employee band come from LF.
    company = {
        "Street": "Hauptstr. 1", "Postal_Code": "10115", "City": "Berlin",
        "Country": "DE", "Employee_Count": 250, "Revenue": 5_000_000,
        "VAT_Id": "DE811569869", "Industry": "Hospitals",
        "Company_LinkedIn": "https://linkedin.com/company/acme",
        "Company_Email": "info@acme.de", "Register_Id": "DB-42",
    }
    payload = lead_field_payload(_enriched(company=company, matched_name="Acme AG"))
    assert not any(k.startswith("KN_DB") for k in payload)
    assert payload["KN_Employee__c"] == "201-350"
    assert payload["KN_VATNumber__c"] == "DE811569869"
    assert "KN_LinkedInProfileLink__c" not in payload
    assert "KN_CompanyEmail__c" not in payload


def test_linkedin_and_title_come_from_lf_contact():
    enr = _enriched(contact={"contact_linkedin": "https://li/in/max",
                             "position": "Head of HR"})
    payload = lead_field_payload(enr)
    assert payload["KN_LinkedInProfileLink__c"] == "https://li/in/max"
    assert payload["Title"] == "Head of HR"


def test_title_fills_only_blank():
    e = _extracted(lead_title="CEO")
    enr = EnrichedLead(extracted=e, contact={"position": "Head of HR"})
    assert "Title" not in lead_field_payload(enr)


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
                      kununu_url="https://www.kununu.com/de/acme")
    result = write_back(client, [_enriched(e)], dry_run=True)
    assert client.calls == []
    assert result["processed"] == 0
    assert len(result["payloads"]) == 1
    assert result["payloads"][0]["KN_KununuLinkProfile__c"] == "https://www.kununu.com/de/acme"


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
    # lead_dbid set so KN_NoOwnDBID__c (fill-only-blank) has nothing to fill either.
    bare = ExtractedLead(lead_id="00Q9", task_id="T9", task_type="content",
                         lead_dbid="12345")  # nothing parsed
    result = write_back(client, [_enriched(bare)], dry_run=False)
    assert client.calls == []  # only the Id, nothing to write -> skipped
    assert result["processed"] == 0


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

    def test_lastname_inside_word_rejected(self):
        # "Bauer" is not the trailing word of "Neubauer" — no word boundary, no split.
        assert split_first_name("Michael Neubauer", "Bauer") is None

    def test_none_inputs(self):
        assert split_first_name(None, "Meier") is None
        assert split_first_name("Max Meier", None) is None


def _extracted(**kw):
    base = dict(lead_id="00Q1", task_id="00T1", task_type="claimer")
    base.update(kw)
    return ExtractedLead(**base)


class TestPayloadDropsDbFields:
    def test_kn_db_fields_never_written(self):
        e = _extracted()
        enr = EnrichedLead(extracted=e, matched=True, matched_name="ACME GmbH",
                           company={"Street": "Weg 2", "Postal_Code": "20095", "City": "HH",
                                    "Country": "de", "Employee_Count": 40, "Revenue": 5e6,
                                    "Industry": "IT", "Register_Id": "77123",
                                    "VAT_Id": "DE123", "Company_LinkedIn": "url",
                                    "Company_Email": "info@acme.de"})
        payload = lead_field_payload(enr)
        assert not any(k.startswith("KN_DB") for k in payload)


class TestNoOwnDbidTick:
    def test_set_true_when_lead_dbid_blank(self):
        enr = EnrichedLead(extracted=_extracted())
        assert lead_field_payload(enr)["KN_NoOwnDBID__c"] == "true"

    def test_not_written_when_lead_has_dbid(self):
        enr = EnrichedLead(extracted=_extracted(lead_dbid="12345"))
        assert "KN_NoOwnDBID__c" not in lead_field_payload(enr)

    def test_not_rewritten_when_already_ticked(self):
        enr = EnrichedLead(extracted=_extracted(lead_no_own_dbid=True))
        assert "KN_NoOwnDBID__c" not in lead_field_payload(enr)


class TestAddressFill:
    def test_address_always_from_lf(self):
        # Tasks carry no address (2026-07-10 spec): parsed lead-side address fields are
        # ignored, the LF company address is the only source. URL country still wins.
        e = _extracted(street="Parsed 1", city="ParsedCity", postal_code="10115",
                       country_code="de")
        enr = EnrichedLead(extracted=e, matched=True,
                           company={"Street": "LF 9", "City": "LFCity",
                                    "Postal_Code": "99999", "Country": "AT"})
        p = lead_field_payload(enr)
        assert p["Street"] == "LF 9"
        assert p["City"] == "LFCity"
        assert p["PostalCode"] == "99999"
        assert p["CountryCode"] == "DE"

    def test_lf_fallback_when_nothing_parsed(self):
        enr = EnrichedLead(extracted=_extracted(), matched=True,
                           company={"Street": "LF 9", "City": "LFCity",
                                    "Postal_Code": "1010", "Country": "AT"})
        p = lead_field_payload(enr)
        assert p["Street"] == "LF 9"
        assert p["CountryCode"] == "AT"

    def test_existing_lead_values_not_overwritten(self):
        # Street/City/PostalCode stay fill-only-blank. CountryCode is the exception:
        # a kununu-slug country writes like KN_Region__c (see the overwrite test below).
        e = _extracted(street="Parsed 1", city="C", postal_code="10115",
                       lead_street="Human St", lead_city="HumanCity",
                       lead_postal_code="20095", lead_country_code="DE",
                       lead_country="Germany")
        p = lead_field_payload(EnrichedLead(extracted=e))
        for field in ("Street", "City", "PostalCode", "CountryCode"):
            assert field not in p

    def test_slug_country_overwrites_lead_country(self):
        # Country is enriched with the same kununu-URL-slug logic as KN_Region__c
        # (Wasil, 2026-07-10): an /at/ profile overwrites a defaulted 'Germany' lead.
        e = _extracted(country_code="AT", region="Austria",
                       lead_country_code="DE", lead_country="Germany")
        p = lead_field_payload(EnrichedLead(extracted=e))
        assert p["CountryCode"] == "AT"
        assert p["KN_Region__c"] == "Austria"

    def test_lf_country_never_overwrites_without_slug(self):
        # No kununu slug: the LF company country only fills a fully blank country.
        e = _extracted(lead_country_code="DE", lead_country="Germany")
        enr = EnrichedLead(extracted=e, matched=True, company={"Country": "AT"})
        assert "CountryCode" not in lead_field_payload(enr)

    def test_street_truncated_to_100(self):
        enr = EnrichedLead(extracted=_extracted(), company={"Street": "x" * 150})
        assert len(lead_field_payload(enr)["Street"]) == 100

    def test_invalid_de_postal_skipped(self):
        enr = EnrichedLead(extracted=_extracted(country_code="DE"),
                           company={"Postal_Code": "101"})
        assert "PostalCode" not in lead_field_payload(enr)

    def test_ch_postal_four_digits_ok(self):
        enr = EnrichedLead(extracted=_extracted(country_code="CH"),
                           company={"Postal_Code": "8004"})
        assert lead_field_payload(enr)["PostalCode"] == "8004"


class TestFirstNameAndPhone:
    def test_first_name_split_when_blank_and_agrees(self):
        e = _extracted(contact_name="Max Muster", lead_last_name="Muster")
        assert lead_field_payload(EnrichedLead(extracted=e))["FirstName"] == "Max"

    def test_first_name_not_overwritten(self):
        e = _extracted(contact_name="Max Muster", lead_last_name="Muster",
                       lead_first_name="Existing")
        assert "FirstName" not in lead_field_payload(EnrichedLead(extracted=e))

    def test_lf_contact_full_name_fallback(self):
        e = _extracted(lead_last_name="Muster")
        enr = EnrichedLead(extracted=e, contact_found=True,
                           contact={"full_name": "Eva Muster", "best_contact_phone": ""})
        assert lead_field_payload(enr)["FirstName"] == "Eva"

    def test_phone_direct_dial_first(self):
        enr = EnrichedLead(extracted=_extracted(), contact_found=True,
                           contact={"phone_direct": "+49 40 419131",
                                    "phone_mobile": "+49 170 1111111"},
                           company={"Company_Phone": "+49 40 999999"})
        assert lead_field_payload(enr)["Phone"] == "+4940419131"

    def test_phone_office_counts_as_direct_dial(self):
        # The current LF schema types numbers landline/mobile/fax — landline IS the
        # direct dial. Company phone only when the contact has no landline.
        enr = EnrichedLead(extracted=_extracted(), contact_found=True,
                           contact={"phone_office": "+49 40 419131"},
                           company={"Company_Phone": "+49 40 999999"})
        assert lead_field_payload(enr)["Phone"] == "+4940419131"

    def test_phone_mobile_never_used_company_fallback(self):
        enr = EnrichedLead(extracted=_extracted(), contact_found=True,
                           contact={"phone_mobile": "+49 170 1111111"},
                           company={"Company_Phone": "+49 40 999999"})
        assert lead_field_payload(enr)["Phone"] == "+4940999999"

    def test_phone_mobile_only_leaves_field_empty(self):
        enr = EnrichedLead(extracted=_extracted(), contact_found=True,
                           contact={"phone_mobile": "+49 170 1111111"})
        assert "Phone" not in lead_field_payload(enr)

    def test_unnormalizable_phone_skipped(self):
        enr = EnrichedLead(extracted=_extracted(), contact_found=True,
                           contact={"phone_direct": "040 419131"})
        assert "Phone" not in lead_field_payload(enr)

    def test_phone_not_overwritten(self):
        enr = EnrichedLead(extracted=_extracted(lead_phone="+4311111"), contact_found=True,
                           contact={"phone_direct": "+49 40 419131"})
        assert "Phone" not in lead_field_payload(enr)


class TestEmployeeAlwaysLeadfeeder:
    def test_lf_headcount_wins_over_task_size(self):
        # 2026-07-10 spec: KN_Employee__c ALWAYS comes from the LF headcount — the task's
        # Unternehmensgröße line (~90% empty, only 'small'/'large') is never used.
        e = _extracted(company_size="1.001-2.500")
        enr = EnrichedLead(extracted=e, matched=True, company={"Employee_Count": 40})
        assert lead_field_payload(enr)["KN_Employee__c"] == "11-50"

    def test_task_size_alone_writes_nothing(self):
        e = _extracted(company_size="51-100 Mitarbeiter")
        assert "KN_Employee__c" not in lead_field_payload(EnrichedLead(extracted=e))

    def test_lf_value_overwrites_existing(self):
        # Overwrite is deliberate: fresh LF headcount beats a stale manual pick.
        e = _extracted(lead_employee_category="1-10")
        enr = EnrichedLead(extracted=e, matched=True, company={"Employee_Count": 75})
        assert lead_field_payload(enr)["KN_Employee__c"] == "51-100"
