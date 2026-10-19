"""LeadFeeder-outcome campaign buckets (campaigns.py): LF match + the Lead's FINAL
KN_Employee__c / Street decide which of the three campaigns a processed lead joins."""
import pytest

from leadauto.campaigns import (
    MATCHED_COMPLETE,
    MATCHED_INCOMPLETE,
    UNMATCHED,
    campaign_bucket,
    campaign_members,
)
from leadauto.models import EnrichedLead, ExtractedLead, MatchResult

CAMPAIGNS = {MATCHED_COMPLETE: "701C", MATCHED_INCOMPLETE: "701I", UNMATCHED: "701U"}


def _result(lead_id="00Q1", matched=True, street=None, employee=None):
    e = ExtractedLead(lead_id=lead_id, task_id="T1", task_type="claimer",
                      lead_street=street, lead_employee_category=employee)
    return MatchResult(enriched=EnrichedLead(extracted=e, matched=matched),
                       account=None, contact=None, decision="create_new")


def test_unmatched_ignores_the_fields():
    r = _result(matched=False, street="Hauptstr. 1", employee="11-50")
    assert campaign_bucket(r, {"Street": "x", "KN_Employee__c": "11-50"}, True) == UNMATCHED


def test_written_payload_completes_a_blank_lead():
    payload = {"Id": "00Q1", "Street": "Hauptstr. 1", "KN_Employee__c": "11-50"}
    assert campaign_bucket(_result(), payload, written=True) == MATCHED_COMPLETE


def test_unwritten_lead_is_judged_on_its_existing_values_only():
    # out of live scope (or a failed write-back): the payload never reached Salesforce
    payload = {"Id": "00Q1", "Street": "Hauptstr. 1", "KN_Employee__c": "11-50"}
    assert campaign_bucket(_result(), payload, written=False) == MATCHED_INCOMPLETE
    full = _result(street="Ring 2", employee="51-200")
    assert campaign_bucket(full, payload, written=False) == MATCHED_COMPLETE


@pytest.mark.parametrize("street,employee,payload", [
    ("Ring 2", None, {}),
    (None, "51-200", {}),
    ("", "", {}),                                    # bulk null
    ("   ", "51-200", {}),
    (None, None, {"Street": "Hauptstr. 1"}),         # only one field enriched
])
def test_either_field_blank_is_incomplete(street, employee, payload):
    r = _result(street=street, employee=employee)
    assert campaign_bucket(r, payload, written=True) == MATCHED_INCOMPLETE


def test_existing_value_survives_a_payload_without_that_field():
    r = _result(street="Ring 2")
    assert campaign_bucket(r, {"KN_Employee__c": "11-50"}, True) == MATCHED_COMPLETE


def test_members_one_row_per_lead_with_counts():
    results = [_result("00Q1", matched=False),
               _result("00Q2", street="Ring 2", employee="11-50"),
               _result("00Q3"),
               _result("00Q1", matched=False)]           # duplicate lead -> one row
    rows, counts = campaign_members(results, [{}] * 4, written_ids=[], campaigns=CAMPAIGNS)
    assert rows == [{"CampaignId": "701U", "LeadId": "00Q1"},
                    {"CampaignId": "701C", "LeadId": "00Q2"},
                    {"CampaignId": "701I", "LeadId": "00Q3"}]
    assert counts == {UNMATCHED: 1, MATCHED_COMPLETE: 1, MATCHED_INCOMPLETE: 1}


def test_bucket_without_a_campaign_id_is_not_tagged():
    rows, counts = campaign_members([_result(matched=False)], [{}], [],
                                    {MATCHED_COMPLETE: "701C"})
    assert rows == [] and not counts
