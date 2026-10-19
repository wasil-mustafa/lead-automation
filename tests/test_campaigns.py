"""LeadFeeder-outcome campaign buckets (campaigns.py): only written-back and
LF-unmatched leads of a tagged task type are tagged; a written lead's FINAL
KN_Employee__c / Street decide complete vs incomplete."""
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
FULL = {"Id": "00Q1", "Street": "Hauptstr. 1", "KN_Employee__c": "11-50"}


def _result(lead_id="00Q1", matched=True, street=None, employee=None, task_type="claimer"):
    e = ExtractedLead(lead_id=lead_id, task_id="T1", task_type=task_type,
                      lead_street=street, lead_employee_category=employee)
    return MatchResult(enriched=EnrichedLead(extracted=e, matched=matched),
                       account=None, contact=None, decision="create_new")


def test_unmatched_is_tagged_whether_or_not_written():
    assert campaign_bucket(_result(matched=False), None, written=False) == UNMATCHED


def test_written_payload_completes_a_blank_lead():
    assert campaign_bucket(_result(), FULL, written=True) == MATCHED_COMPLETE


def test_matched_but_not_written_is_not_tagged():
    # out of the write-back scope, or a failed write-back: stays eligible for later runs
    full = _result(street="Ring 2", employee="51-200")
    assert campaign_bucket(full, FULL, written=False) is None


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


@pytest.mark.parametrize("matched,written", [(False, False), (True, True)])
def test_other_task_types_are_never_tagged(matched, written):
    r = _result(matched=matched, task_type="ebp_trial")
    assert campaign_bucket(r, FULL, written, task_types=["claimer"]) is None
    assert campaign_bucket(r, FULL, written, task_types=None) is not None   # no axis


def test_members_one_row_per_lead_with_counts():
    results = [_result("00Q1", matched=False),
               _result("00Q2"),                               # written, LF filled both
               _result("00Q3"),                               # written, nothing filled
               _result("00Q4"),                               # matched, NOT written
               _result("00Q5", matched=False, task_type="ebp_trial"),
               _result("00Q1", matched=False)]                # duplicate lead -> one row
    payloads = [None, FULL, {}, FULL, None, None]
    rows, counts = campaign_members(results, payloads, ["00Q2", "00Q3"], CAMPAIGNS,
                                    task_types=["claimer"])
    assert rows == [{"CampaignId": "701U", "LeadId": "00Q1"},
                    {"CampaignId": "701C", "LeadId": "00Q2"},
                    {"CampaignId": "701I", "LeadId": "00Q3"}]
    assert counts == {UNMATCHED: 1, MATCHED_COMPLETE: 1, MATCHED_INCOMPLETE: 1}


def test_bucket_without_a_campaign_id_is_not_tagged():
    rows, counts = campaign_members([_result(matched=False)], [None], [],
                                    {MATCHED_COMPLETE: "701C"})
    assert rows == [] and not counts
