"""SOQL builder tests. Builders are pure string functions — no org needed.

Invariants that matter downstream:
- SfClient.query rejects double quotes, so builders must use single-quoted literals only.
- Datetime watermark is a SOQL datetime literal (unquoted, ISO 8601).
- Lead query carries the verified KN_* fields and IsConverted = false.
"""
import pytest

from leadauto.sf import soql
from leadauto.sf.soql import accounts_by_ids, contacts_by_ids, leads_shadow_status

SUBJECTS = [
    "Inbound Sales Task: New Claimed Profile",
    "Inbound Sales Task: EBP Trial",
]
WATERMARK = "2026-05-17T00:00:00Z"


def test_tasks_since_selects_required_fields():
    q = soql.tasks_since(WATERMARK, SUBJECTS)
    assert "FROM Task" in q
    for field in ("Id", "WhoId", "Who.Name", "Subject", "Description", "CreatedDate"):
        assert field in q


def test_tasks_since_filters_and_orders():
    q = soql.tasks_since(WATERMARK, SUBJECTS)
    assert f"CreatedDate >= {WATERMARK}" in q          # datetime literal stays unquoted
    assert "'Inbound Sales Task: New Claimed Profile'" in q
    assert "'Inbound Sales Task: EBP Trial'" in q
    assert "ORDER BY CreatedDate ASC" in q


def test_tasks_since_never_emits_double_quotes():
    q = soql.tasks_since(WATERMARK, SUBJECTS)
    assert '"' not in q


def test_tasks_since_limit_appended_only_when_positive():
    assert "LIMIT" not in soql.tasks_since(WATERMARK, SUBJECTS)
    assert "LIMIT" not in soql.tasks_since(WATERMARK, SUBJECTS, limit=0)
    assert soql.tasks_since(WATERMARK, SUBJECTS, limit=5).rstrip().endswith("LIMIT 5")


def test_tasks_since_requires_subjects():
    with pytest.raises(ValueError):
        soql.tasks_since(WATERMARK, [])


def test_tasks_since_status_filter_when_given():
    q = soql.tasks_since(WATERMARK, SUBJECTS,
                         statuses=["In Progress", "in Arbeit", "Not Started"])
    assert "Status IN ('In Progress', 'in Arbeit', 'Not Started')" in q
    assert "ORDER BY CreatedDate ASC" in q  # status clause precedes ORDER BY


def test_tasks_since_no_status_clause_when_empty():
    assert "Status IN" not in soql.tasks_since(WATERMARK, SUBJECTS)
    assert "Status IN" not in soql.tasks_since(WATERMARK, SUBJECTS, statuses=[])


def test_tasks_since_status_filter_escapes_and_no_double_quotes():
    q = soql.tasks_since(WATERMARK, SUBJECTS, statuses=["O'Done"])
    assert "\\'" in q
    assert '"' not in q


def test_tasks_since_escapes_single_quote_in_subject():
    q = soql.tasks_since(WATERMARK, ["O'Brien Event"])
    assert "\\'" in q          # the apostrophe is escaped, not left bare
    assert '"' not in q


def test_leads_by_ids_selects_required_fields():
    q = soql.leads_by_ids(["00Q000000000001AAA"])
    assert "FROM Lead" in q
    for field in (
        "Id", "FirstName", "LastName", "Company", "Email", "Website",
        "Status", "IsConverted", "LeadSource",
        "KN_KununuProfileName__c", "KN_KununuUserUUID__c", "KN_KununuLinkProfile__c",
    ):
        assert field in q


def test_leads_by_ids_filters_ids_and_unconverted():
    q = soql.leads_by_ids(["00Q000000000001AAA", "00Q000000000002AAA"])
    assert "'00Q000000000001AAA'" in q
    assert "'00Q000000000002AAA'" in q
    assert "IsConverted = false" in q
    assert '"' not in q


def test_leads_by_ids_restricts_to_queue_owned():
    # Scope decision 2026-07-03: only queue-held leads for now. Pool-user-owned
    # leads (Rohdaten/Clean Pool) carry contact-attached tasks — handled later.
    q = soql.leads_by_ids(["00Q000000000001AAA"])
    assert "Owner.Type = 'Queue'" in q


def test_leads_by_ids_requires_ids():
    with pytest.raises(ValueError):
        soql.leads_by_ids([])


def test_account_match_fields_include_billing():
    q = soql.accounts_by_signals(names=["Acme GmbH"])
    assert "BillingPostalCode" in q and "BillingCity" in q


def test_leads_shadow_status():
    q = leads_shadow_status(["00Q1", "00Q2"])
    assert q.startswith("SELECT ")
    for f in ("IsConverted", "ConvertedDate", "ConvertedAccountId",
              "ConvertedContactId", "KN_Employee__c", "KN_DBRevenueinMillions__c",
              "KN_DBISO2CountryCode__c", "KN_DBPostalCode__c", "KN_DBCity__c"):
        assert f in q
    assert "WHERE Id IN ('00Q1', '00Q2')" in q
    with pytest.raises(ValueError):
        leads_shadow_status([])


def test_accounts_and_contacts_by_ids():
    aq = accounts_by_ids(["001A"])
    assert "CreatedDate" in aq and "BillingPostalCode" in aq and "FROM Account" in aq
    cq = contacts_by_ids(["003C"])
    assert "Email" in cq and "FROM Contact" in cq
    with pytest.raises(ValueError):
        accounts_by_ids([])
    with pytest.raises(ValueError):
        contacts_by_ids([])


def test_lead_fields_include_conversion_state():
    q = soql.leads_by_ids(["00Q1"])
    for f in ("Street", "City", "PostalCode", "Country", "CountryCode", "Phone",
              "HasOptedOutOfEmail", "KN_DBID__c", "KN_NoOwnDBID__c",
              "KN_Employee__c", "KN_Language__c"):
        assert f in q


def test_leads_preflight_no_owner_or_converted_filter():
    from leadauto.sf.soql import leads_preflight
    soql_str = leads_preflight(["00Q1", "00Q2"])
    assert "Owner.Type" not in soql_str and "IsConverted =" not in soql_str
    assert "'00Q1', '00Q2'" in soql_str
    assert "HasOptedOutOfEmail" in soql_str and "Country" in soql_str


def test_leads_preflight_requires_ids():
    from leadauto.sf.soql import leads_preflight
    with pytest.raises(ValueError):
        leads_preflight([])
