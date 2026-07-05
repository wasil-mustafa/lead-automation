"""check_lead mirrors the org's conversion-blocking validation rules (audit 2026-07-05)."""
from leadauto.convert.preflight import Failure, check_lead

GOOD = {
    "IsConverted": False, "KN_Employee__c": "11-50", "KN_DBID__c": "",
    "KN_NoOwnDBID__c": "true", "KN_Language__c": "German",
    "Street": "Weg 1", "Country": "Germany", "CountryCode": "DE",
    "PostalCode": "20095", "Phone": "+4940419131",
    "Email": "info@acme.de", "HasOptedOutOfEmail": False, "LeadSource": "Web",
}


def _checks(fields, create_new=True):
    return [f.check for f in check_lead(fields, create_new=create_new)]


def test_good_lead_passes():
    assert check_lead(GOOD, create_new=True) == []


def test_already_converted_short_circuits():
    assert _checks({**GOOD, "IsConverted": "true"}) == ["already_converted"]


def test_employee_category_blank():
    assert "employee_category" in _checks({**GOOD, "KN_Employee__c": ""})


def test_dbid_xor_neither():
    assert "dbid_xor" in _checks({**GOOD, "KN_DBID__c": "", "KN_NoOwnDBID__c": "false"})


def test_dbid_xor_both():
    assert "dbid_xor" in _checks({**GOOD, "KN_DBID__c": "123", "KN_NoOwnDBID__c": "true"})


def test_dbid_alone_ok_and_numeric_enforced():
    ok = {**GOOD, "KN_DBID__c": "12345", "KN_NoOwnDBID__c": "false"}
    assert check_lead(ok, create_new=True) == []
    assert "dbid_numeric" in _checks({**ok, "KN_DBID__c": "HRB 123"})


def test_language_blank():
    assert "language" in _checks({**GOOD, "KN_Language__c": None})


def test_address_required_only_for_create_new():
    no_street = {**GOOD, "Street": ""}
    assert "address_create_new" in _checks(no_street, create_new=True)
    assert "address_create_new" not in _checks(no_street, create_new=False)


def test_address_accepts_countrycode_when_country_missing():
    assert "address_create_new" not in _checks({**GOOD, "Country": ""})
    assert "address_create_new" in _checks({**GOOD, "Country": "", "CountryCode": ""})


def test_freemail_blocks_unless_opted_out():
    free = {**GOOD, "Email": "max@gmail.com"}
    assert "freemail_email" in _checks(free)
    assert "freemail_email" not in _checks({**free, "HasOptedOutOfEmail": "true"})
    assert "freemail_email" not in _checks({**GOOD, "Email": "max@mailbox.org"})


def test_freemail_matches_org_substring_semantics():
    assert "freemail_email" in _checks({**GOOD, "Email": "a@mail.com"})
    assert "freemail_email" in _checks({**GOOD, "Email": "a@gmail.de"})


def test_phone_format_and_ecommerce_exemption():
    bad = {**GOOD, "Phone": "040 419131"}
    assert "phone_format" in _checks(bad)
    assert "phone_format" not in _checks({**bad, "LeadSource": "E-commerce"})
    assert "phone_format" not in _checks({**GOOD, "Phone": ""})


def test_street_length():
    assert "street_length" in _checks({**GOOD, "Street": "x" * 101, })


def test_postal_length_by_country_name():
    assert "postal_length" in _checks({**GOOD, "PostalCode": "1234"})       # Germany needs 5
    ch = {**GOOD, "Country": "Switzerland", "CountryCode": "CH", "PostalCode": "80045"}
    assert "postal_length" in _checks(ch)
    assert "postal_length" not in _checks({**ch, "PostalCode": "8004"})
    at = {**GOOD, "Country": "Austria", "CountryCode": "AT", "PostalCode": "10"}
    assert "postal_length" not in _checks(at)  # rule only covers DE/CH


def test_postal_country_derived_from_countrycode():
    assert "postal_length" in _checks({**GOOD, "Country": "", "PostalCode": "1234"})  # CC=DE


def test_failure_carries_value_and_detail():
    fails = check_lead({**GOOD, "KN_DBID__c": "HRB 1", "KN_NoOwnDBID__c": "false"},
                       create_new=True)
    f = next(x for x in fails if x.check == "dbid_numeric")
    assert isinstance(f, Failure) and f.value == "HRB 1" and "digits" in f.detail


from pathlib import Path

from leadauto.convert import ConversionTarget
from leadauto.convert.preflight import run_preflight, write_failures_csv


class FakeClient:
    def __init__(self, rows):
        self.rows = rows
        self.queries = []

    def query(self, soql):
        self.queries.append(soql)
        return self.rows


def test_run_preflight_splits_ok_and_failures():
    rows = [dict(GOOD, Id="00Q_OK"), dict(GOOD, Id="00Q_BAD", KN_Employee__c="")]
    targets = [ConversionTarget("00Q_OK"), ConversionTarget("00Q_BAD")]
    ok, failures = run_preflight(FakeClient(rows), targets)
    assert [t.lead_id for t in ok] == ["00Q_OK"]
    assert failures == [{"lead_id": "00Q_BAD", "check": "employee_category", "value": "",
                         "detail": "KN_Employee__c blank (KN_EmployeeCategoryMandatory)"}]


def test_run_preflight_create_new_depends_on_account_id():
    rows = [dict(GOOD, Id="00Q1", Street="")]
    linked = [ConversionTarget("00Q1", account_id="001X")]
    ok, failures = run_preflight(FakeClient(rows), linked)
    assert ok and not failures  # linked account -> no address requirement


def test_run_preflight_missing_lead_reported():
    ok, failures = run_preflight(FakeClient([]), [ConversionTarget("00QGONE")])
    assert not ok
    assert failures[0]["check"] == "not_found"


def test_run_preflight_empty_targets_no_query():
    client = FakeClient([])
    assert run_preflight(client, []) == ([], [])
    assert client.queries == []


def test_write_failures_csv(tmp_path):
    path = write_failures_csv(
        [{"lead_id": "00Q1", "check": "language", "value": "", "detail": "d"}],
        tmp_path / "preflight_failures.csv")
    text = Path(path).read_text(encoding="utf-8")
    assert text.splitlines()[0] == "lead_id,check,value,detail"
    assert "00Q1,language" in text
