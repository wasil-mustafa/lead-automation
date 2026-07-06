from leadauto.convert.pool_enrich import account_update


def _acct(**over):
    base = {"Id": "001A", "KN_KununuLinkProfile__c": None, "KN_KununuProfileUuid__c": None,
            "KN_KununuProfileName__c": None, "KN_Region__c": None, "KN_VATNumber__c": None,
            "KN_EmployeeCategory__c": None, "KN_KununuProfileClaimed__c": None,
            "KN_KununuProfileClaimedat__c": None, "BillingStreet": None,
            "BillingCity": None, "BillingPostalCode": None, "BillingCountryCode": None}
    base.update(over)
    return base


def test_blank_account_identity_is_filled():
    reason, payload = account_update(
        _acct(), {"KN_VATNumber__c": "DE123"},
        kununu_url="https://www.kununu.com/de/acme", kununu_profile_uuid="puid-1")
    assert reason is None
    assert payload["KN_KununuLinkProfile__c"] == "https://www.kununu.com/de/acme"
    assert payload["KN_KununuProfileUuid__c"] == "puid-1"
    assert payload["KN_VATNumber__c"] == "DE123"


def test_matching_identity_is_noop():
    acct = _acct(KN_KununuProfileUuid__c="puid-1",
                 KN_KununuLinkProfile__c="https://www.kununu.com/de/acme")
    reason, payload = account_update(acct, {}, kununu_url="https://www.kununu.com/de/acme",
                                     kununu_profile_uuid="puid-1")
    assert reason is None
    assert "KN_KununuProfileUuid__c" not in payload and "KN_KununuLinkProfile__c" not in payload


def test_differing_uuid_is_conflict():
    acct = _acct(KN_KununuProfileUuid__c="puid-OTHER")
    reason, payload = account_update(acct, {}, kununu_url=None, kununu_profile_uuid="puid-1")
    assert reason == "kununu_uuid_conflict" and payload == {}


def test_differing_url_is_conflict():
    acct = _acct(KN_KununuLinkProfile__c="https://www.kununu.com/de/other")
    reason, _ = account_update(acct, {}, kununu_url="https://www.kununu.com/de/acme",
                               kununu_profile_uuid=None)
    assert reason == "kununu_url_conflict"


def test_url_conflict_when_uuid_blank():
    acct = _acct(KN_KununuLinkProfile__c="https://www.kununu.com/de/other")
    reason, _ = account_update(acct, {}, kununu_url="https://www.kununu.com/de/acme",
                               kununu_profile_uuid="puid-1")
    assert reason == "kununu_url_conflict"


def test_overwrite_on_difference_only():
    acct = _acct(KN_VATNumber__c="OLD", KN_Region__c="Germany")
    reason, payload = account_update(
        acct, {"KN_VATNumber__c": "NEW", "KN_Region__c": "Germany", "KN_Employee__c": "11-50"},
        kununu_url=None, kununu_profile_uuid=None)
    assert reason is None
    assert payload["KN_VATNumber__c"] == "NEW"          # differs -> written
    assert "KN_Region__c" not in payload                # equal -> skipped
    assert payload["KN_EmployeeCategory__c"] == "11-50"  # Lead KN_Employee__c -> Account field


def test_lead_blank_never_written():
    reason, payload = account_update(_acct(KN_VATNumber__c="OLD"), {"KN_VATNumber__c": ""},
                                     kununu_url=None, kununu_profile_uuid=None)
    assert reason is None and "KN_VATNumber__c" not in payload
    assert list(payload) == ["Id"]                       # nothing to write but Id
