"""SOQL builder tests. Builders are pure string functions — no org needed.

Invariants that matter downstream:
- SfClient.query rejects double quotes, so builders must use single-quoted literals only.
- Datetime watermark is a SOQL datetime literal (unquoted, ISO 8601).
- Lead query carries the verified KN_* fields and IsConverted = false.
"""
import pytest

from leadauto.sf import soql

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
