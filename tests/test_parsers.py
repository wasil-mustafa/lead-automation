"""Parser tests using real Task descriptions captured from org `headless`.

Fixtures are verbatim (abbreviated) samples:
  claimer  -> Task 00TMz00001I8KYPMA3 (Hufeland Klinikum)
  ebp      -> Task 00TMz00001IE70YMAT / 00TMz00001IEFqnMAH (Volksbank Mittelhessen)
"""
from datetime import date

from leadauto.extract.parsers import parse_claimer, parse_ebp_trial, extract_lead
from leadauto.config import Settings

CLAIMER_DESC = (
    "Das Profil Hufeland Klinikum GmbH https://www.kununu.com/de/hufeland-klinikum "
    "wurde vor kurzem von  als kostenloses Arbeitgeberprofil aktiviert (claimed, "
    "beansprucht).Dadurch hat das Unternehmen Zugriff auf das Profil.\n"
    "Bitte kontaktiere Deinen Ansprechpartner unter der E-Mail "
    "geschaeftsleitung@hufeland.de, um ihn als Kunden zu gewinnen.\n\n"
    "Details: \n"
    "- Profile-Name: Hufeland Klinikum GmbH\n"
    "- Profil beansprucht am: 19.06.2026\n"
    "- Profil Type: claimed\n"
    "- Unternehmensgröße: \n"
    "- Email: geschaeftsleitung@hufeland.de\n"
    "- User Name: Ramona Panknin\n"
    "- User-ID: 7dcf0627-2a22-4c78-aebb-22976bb072a7\n"
    "- Profil-ID: bc3e53a8-1795-489c-b966-315dc36e50e3"
)

EBP_DESC_PLACEHOLDER = (
    "Der Benutzer   nutzt den 14tägigen EBP-Trial für das Profil: "
    "Volksbank Mittelhessen eG https://www.kununu.com/de/volksbank-mittelhessen\n"
    "Zeitraum: 2026-06-19 00:00:00 UTC - 2026-07-03 00:00:00 UTC\n\n"
    "Email: user_2244040@kununu.com\n"
    "- User-ID: 9d3bb510-daa9-4143-a2d2-4588f0d4bc7b\n"
    "- Profil-ID: bb2187f8-0642-4054-b660-321ba8444783"
)

EBP_DESC_REAL_EMAIL = (
    "Der Benutzer Annabelle Wulff nutzt den 14tägigen EBP-Trial für das Profil: "
    "Volksbank Mittelhessen eG https://www.kununu.com/de/volksbank-mittelhessen\n"
    "Zeitraum: 2026-06-19 00:00:00 UTC - 2026-07-03 00:00:00 UTC\n\n"
    "Email: annabelle.wulff@web.de\n"
    "- User-ID: b81067a6-0654-48b3-b645-1bf8f294bcbd\n"
    "- Profil-ID: bb2187f8-0642-4054-b660-321ba8444783"
)


def _task(subject, desc, tid="00T000", who="00Q000"):
    return {"Id": tid, "Subject": subject, "Description": desc, "WhoId": who}


def _lead(company="Acme GmbH", email="x@acme.de", lid="00Q000"):
    return {
        "Id": lid, "Company": company, "Email": email,
        "FirstName": "First", "LastName": "Last", "Website": None,
    }


def test_parse_claimer_full():
    e = parse_claimer(_task("Inbound Sales Task: New Claimed Profile", CLAIMER_DESC), _lead())
    assert e.task_type == "claimer"
    assert e.company_name == "Hufeland Klinikum GmbH"
    assert e.kununu_url == "https://www.kununu.com/de/hufeland-klinikum"
    assert (e.region, e.country_code) == ("Germany", "DE")
    assert e.profile_type == "claimed"
    assert e.contact_email == "geschaeftsleitung@hufeland.de"
    assert e.contact_name == "Ramona Panknin"
    assert e.kununu_user_uuid == "7dcf0627-2a22-4c78-aebb-22976bb072a7"
    assert e.kununu_profile_uuid == "bc3e53a8-1795-489c-b966-315dc36e50e3"
    assert e.claimed_at == date(2026, 6, 19)
    assert e.company_size is None
    assert e.lead_company == "Acme GmbH"  # lead-native flows through


def test_parse_ebp_drops_kununu_placeholder_email():
    e = parse_ebp_trial(_task("Inbound Sales Task: EBP Trial", EBP_DESC_PLACEHOLDER), _lead())
    assert e.task_type == "ebp_trial"
    assert e.company_name == "Volksbank Mittelhessen eG"
    assert e.kununu_url == "https://www.kununu.com/de/volksbank-mittelhessen"
    assert e.country_code == "DE"
    assert e.kununu_user_uuid == "9d3bb510-daa9-4143-a2d2-4588f0d4bc7b"
    assert e.kununu_profile_uuid == "bb2187f8-0642-4054-b660-321ba8444783"
    assert e.contact_email is None        # user_xxx@kununu.com filtered out
    assert e.contact_name is None         # "Der Benutzer   nutzt" -> empty


def test_parse_ebp_keeps_real_email_and_name():
    e = parse_ebp_trial(_task("Inbound Sales Task: EBP Trial", EBP_DESC_REAL_EMAIL), _lead())
    assert e.contact_email == "annabelle.wulff@web.de"
    assert e.contact_name == "Annabelle Wulff"


def test_dispatch_skips_out_of_scope_subject():
    settings = Settings.load()  # only 'claimer' enabled
    assert extract_lead(_task("Inbound Sales Task: Kontaktanfrage", "x"), _lead(), settings) is None
    assert extract_lead(_task("Marketing Touchpoint: Ecom", "x"), _lead(), settings) is None


def test_dispatch_routes_claimer():
    settings = Settings.load()
    e = extract_lead(_task("Inbound Sales Task: New Claimed Profile", CLAIMER_DESC), _lead(), settings)
    assert e is not None and e.task_type == "claimer"


def test_base_maps_new_lead_native_fields():
    task = {"Id": "00T1", "WhoId": "00Q1", "Subject": "s", "Description": ""}
    lead = {
        "Id": "00Q1", "LastName": "Muster", "Street": "Gasse 1", "City": "Wien",
        "PostalCode": "1010", "CountryCode": "AT", "Country": "Austria",
        "Phone": "+431234567", "LeadSource": "Web", "HasOptedOutOfEmail": "true",
        "KN_DBID__c": "12345", "KN_NoOwnDBID__c": "false",
        "KN_Employee__c": "11-50", "KN_Language__c": "German",
    }
    e = parse_claimer(task, lead)
    assert e.lead_street == "Gasse 1"
    assert e.lead_city == "Wien"
    assert e.lead_postal_code == "1010"
    assert e.lead_country_code == "AT"
    assert e.lead_country == "Austria"
    assert e.lead_phone == "+431234567"
    assert e.lead_source == "Web"
    assert e.lead_opted_out_email is True
    assert e.lead_dbid == "12345"
    assert e.lead_no_own_dbid is False
    assert e.lead_employee_category == "11-50"
    assert e.lead_language == "German"


def test_base_bool_coercion_accepts_real_booleans():
    task = {"Id": "00T1", "WhoId": "00Q1", "Subject": "s", "Description": ""}
    lead = {"Id": "00Q1", "LastName": "M", "HasOptedOutOfEmail": False, "KN_NoOwnDBID__c": True}
    e = parse_claimer(task, lead)
    assert e.lead_opted_out_email is False
    assert e.lead_no_own_dbid is True
