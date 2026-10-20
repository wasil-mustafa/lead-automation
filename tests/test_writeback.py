"""Write-back tests (Module 3b).

The field mapping is pure (EnrichedLead -> {Id, KN_*}); the bulk write goes through a
FakeClient so we assert the dry-run rail (no call) and the live path (one update_records call)
without touching an org.
"""
from datetime import date

from leadauto.models import ExtractedLead
from leadauto.models import EnrichedLead
from leadauto.config import Settings
from leadauto.writeback import (
    employee_bin,
    lead_field_payload,
    lf_contact_email,
    normalize_phone,
    repair_national_phone,
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
        contact_email="max@hufeland.de",
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
    e = ExtractedLead(lead_id="00Q1", task_id="T1", task_type="content")
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


def test_website_fills_only_a_blank_lead_website():
    # LF fills a blank Website, but never replaces one the Lead already carries:
    # Website is a matching key, so an overwrite could re-point the lead.
    company = {"Website": "https://acme.de"}
    assert lead_field_payload(_enriched(company=company))["Website"] == "https://acme.de"
    # A lead that already has one keeps it: fill-only-blank omits the key entirely,
    # so the update never touches the field.
    e = _extracted(lead_website="https://old-acme.de")
    assert "Website" not in lead_field_payload(_enriched(e, company=company))


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


def test_task_parsed_position_beats_lf_contact_title_but_never_a_set_lead_title():
    # Event badge scans (2026-09-18) carry the attendee's own position; claimer/trial
    # tasks leave `position` None, so their payload is unchanged.
    e = _extracted(position="HR Consultant")
    enr = EnrichedLead(extracted=e, contact={"position": "Head of HR"})
    assert lead_field_payload(enr)["Title"] == "HR Consultant"
    e = _extracted(position="HR Consultant", lead_title="CEO")
    assert "Title" not in lead_field_payload(EnrichedLead(extracted=e, contact={}))


# --- write_back orchestration ------------------------------------------------

class FakeClient:
    def __init__(self):
        self.calls: list[tuple] = []

    def update_records(self, sobject, records):
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


def test_write_back_live_calls_update_records_once():
    client = FakeClient()
    e = ExtractedLead(lead_id="00Q1", task_id="T1", task_type="claimer",
                      company_name="Acme GmbH", kununu_url="https://www.kununu.com/de/acme")
    result = write_back(client, [_enriched(e)], dry_run=False)
    assert len(client.calls) == 1
    sobject, records = client.calls[0]
    assert sobject == "Lead"
    assert records[0]["Id"] == "00Q1"
    assert result["processed"] == 1


class FailingClient:
    def update_records(self, sobject, records):
        return {"processed": len(records), "failed": 1,
                "failures": [{"id": records[0]["Id"], "error": "BAD_PICKLIST"}],
                "confirmed": True}


def test_write_back_surfaces_row_failures():
    e = ExtractedLead(lead_id="00Q1", task_id="T1", task_type="claimer", company_name="Acme",
                      kununu_url="https://www.kununu.com/de/acme")
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


class TestNoOwnDbidLeftAlone:
    # Wasil, 2026-10-20: write-back never touches KN_NoOwnDBID__c (it used to tick it
    # for every DBID-less lead).
    def test_not_written_when_lead_dbid_blank(self):
        enr = EnrichedLead(extracted=_extracted())
        assert "KN_NoOwnDBID__c" not in lead_field_payload(enr)

    def test_not_written_when_lead_has_dbid(self):
        enr = EnrichedLead(extracted=_extracted(lead_dbid="12345"))
        assert "KN_NoOwnDBID__c" not in lead_field_payload(enr)

    def test_not_written_when_already_ticked(self):
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


class TestIndustry:
    def test_kununu_overwrites_existing(self):
        # kununu profile industry IS the SF taxonomy — authoritative, overwrites.
        e = _extracted(lead_industry="OTHER")
        enr = _enriched(e, feed_industry="IT")
        assert lead_field_payload(enr)["Industry"] == "INTERNET and INFORMATION TECHNOLOGY"

    def test_leadfeeder_fills_only_when_blank(self):
        e = _extracted()
        enr = _enriched(e, company={"Industry": "Business Consulting and Services"})
        assert lead_field_payload(enr)["Industry"] == "CONSULTING, AUDITING and LAW"

    def test_leadfeeder_skipped_when_lead_already_set(self):
        e = _extracted(lead_industry="OTHER")
        enr = _enriched(e, company={"Industry": "Business Consulting and Services"})
        assert "Industry" not in lead_field_payload(enr)

    def test_kununu_beats_leadfeeder(self):
        e = _extracted()
        enr = _enriched(e, feed_industry="Banken", company={"Industry": "Software"})
        assert lead_field_payload(enr)["Industry"] == "BANKING and INSURANCE"

    def test_unmapped_writes_nothing(self):
        e = _extracted()
        enr = _enriched(e, feed_industry="Raumfahrt")
        assert "Industry" not in lead_field_payload(enr)


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


class TestEmailFromLeadfeederContact:
    """Wasil, 2026-09-18: with no Task email (event badge scans), the LF contact's
    email fills a BLANK Lead.Email. It never overwrites, and a Task email still wins."""

    def test_lf_email_fills_blank_lead_email(self):
        enr = EnrichedLead(extracted=_extracted(task_type="event", contact_email=None,
                                                lead_email=None),
                           contact_found=True,
                           contact={"contact_emails": "jane@acme.de; j.doe@acme.de"})
        assert lead_field_payload(enr)["Email"] == "jane@acme.de"

    def test_lf_email_never_overwrites_existing_lead_email(self):
        enr = EnrichedLead(extracted=_extracted(task_type="event", contact_email=None,
                                                lead_email="me@firma.de"),
                           contact_found=True,
                           contact={"contact_emails": "jane@acme.de"})
        assert "Email" not in lead_field_payload(enr)

    def test_task_email_beats_lf_email(self):
        enr = EnrichedLead(extracted=_extracted(contact_email="claimer@acme.de",
                                                lead_email=None),
                           contact_found=True,
                           contact={"contact_emails": "jane@acme.de"})
        assert lead_field_payload(enr)["Email"] == "claimer@acme.de"

    def test_no_contact_no_email(self):
        enr = EnrichedLead(extracted=_extracted(task_type="event", contact_email=None,
                                                lead_email=None))
        assert "Email" not in lead_field_payload(enr)

    def test_lf_contact_email_helper(self):
        assert lf_contact_email({"contact_emails": " a@x.de ; b@x.de"}) == "a@x.de"
        assert lf_contact_email({"contact_emails": ""}) is None
        assert lf_contact_email({}) is None
        assert lf_contact_email(None) is None


def _settings(**pipeline):
    import copy
    raw = copy.deepcopy(Settings.load().raw)
    raw["pipeline"].update(pipeline)
    return Settings(raw=raw, leadfeeder_api_key=None, sf_auth_url=None)


class TestRepairNationalPhone:
    """Wasil, 2026-09-18: 46 of 109 ZPE badge-scan leads carry `+0<digits>` — a national
    number with '+' on the trunk zero. Repaired to the lead country's code, ZPE config only."""

    def test_helper_de_at_ch(self):
        assert repair_national_phone("+02091584338", "DE") == "+492091584338"
        assert repair_national_phone("+015146371938", "de") == "+4915146371938"
        assert repair_national_phone("+015123456", "AT") == "+4315123456"
        assert repair_national_phone("+0443334455", "CH") == "+41443334455"

    def test_helper_leaves_international_and_other_countries_alone(self):
        assert repair_national_phone("+4940419131", "DE") is None
        assert repair_national_phone("+420739583308", "DE") is None   # already +420
        assert repair_national_phone("+02091584338", "FR") is None    # trunk rule unknown
        assert repair_national_phone("+02091584338", None) is None
        assert repair_national_phone("+0091584338", "DE") is None     # 00… is not a trunk 0
        assert repair_national_phone("", "DE") is None
        assert repair_national_phone(None, "DE") is None

    def test_the_shipped_default_is_off(self):
        assert Settings.load().repair_national_phone is False

    def test_gated_payload_overwrites_national_phone(self):
        e = _extracted(lead_phone="+02091584338", lead_country_code="DE")
        enr = EnrichedLead(extracted=e, contact_found=True,
                           contact={"phone_direct": "+49 40 419131"})
        payload = lead_field_payload(enr, _settings(repair_national_phone=True))
        assert payload["Phone"] == "+492091584338"   # repaired, NOT replaced by LF

    def test_default_settings_keep_phone_fill_only_blank(self):
        e = _extracted(lead_phone="+02091584338", lead_country_code="DE")
        enr = EnrichedLead(extracted=e, contact_found=True,
                           contact={"phone_direct": "+49 40 419131"})
        assert "Phone" not in lead_field_payload(enr, _settings(repair_national_phone=False))
        assert "Phone" not in lead_field_payload(enr)   # no settings at all

    def test_gated_payload_uses_slug_country_over_lead_country(self):
        # country precedence mirrors CountryCode: kununu slug > Lead CountryCode > LF
        e = _extracted(lead_phone="+015123456", lead_country_code="DE", country_code="AT")
        assert lead_field_payload(EnrichedLead(extracted=e),
                                  _settings(repair_national_phone=True))["Phone"] == "+4315123456"

    def test_gated_payload_still_fills_a_blank_phone_from_leadfeeder(self):
        enr = EnrichedLead(extracted=_extracted(lead_phone=None, lead_country_code="DE"),
                           contact_found=True, contact={"phone_direct": "+49 40 419131"})
        assert lead_field_payload(enr, _settings(repair_national_phone=True))["Phone"] == "+4940419131"

    def test_gated_payload_leaves_a_correct_phone_alone(self):
        enr = EnrichedLead(extracted=_extracted(lead_phone="+4940419131", lead_country_code="DE"),
                           contact_found=True, contact={"phone_direct": "+49 30 1"})
        assert "Phone" not in lead_field_payload(enr, _settings(repair_national_phone=True))

    def test_write_back_passes_settings_through(self):
        class FakeClient:
            def __init__(self):
                self.payloads = None

            def update_records(self, sobject, payloads):
                self.payloads = payloads
                return {"processed": len(payloads), "failed": 0, "failures": [], "confirmed": True}

        fc = FakeClient()
        e = _extracted(lead_phone="+02091584338", lead_country_code="DE")
        write_back(fc, [EnrichedLead(extracted=e)], dry_run=False,
                   settings=_settings(repair_national_phone=True))
        assert fc.payloads[0]["Phone"] == "+492091584338"
