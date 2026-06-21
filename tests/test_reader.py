"""Extraction-reader tests.

Pure helpers (dedupe, already-enriched) are tested directly. The orchestrator is tested
through a FakeClient that stands in for the live org boundary (the only unavoidable mock):
it routes by `FROM Task` / `FROM Lead` and returns canned records, so the reader's real
filtering / dedupe / dispatch logic is exercised end to end.
"""
from leadauto.config import Settings
from leadauto.extract.reader import (
    ExtractionResult,
    Skipped,
    is_already_enriched,
    newest_task_per_who,
    read_extracted_leads,
)

CLAIMER_SUBJECT = "Inbound Sales Task: New Claimed Profile"

CLAIMER_DESC = (
    "Das Profil Hufeland Klinikum GmbH https://www.kununu.com/de/hufeland-klinikum "
    "wurde aktiviert.\n"
    "Details: \n"
    "- Profile-Name: Hufeland Klinikum GmbH\n"
    "- Profil Type: claimed\n"
    "- Email: geschaeftsleitung@hufeland.de\n"
    "- User Name: Ramona Panknin\n"
    "- User-ID: 7dcf0627-2a22-4c78-aebb-22976bb072a7\n"
    "- Profil-ID: bc3e53a8-1795-489c-b966-315dc36e50e3"
)
CLAIMER_DESC_NEWER = CLAIMER_DESC.replace("Hufeland Klinikum GmbH", "Newer Company GmbH")


class FakeClient:
    """Stands in for SfClient at the org boundary; routes by SObject in the SOQL."""

    def __init__(self, tasks=None, leads=None):
        self._tasks = list(tasks or [])
        self._leads = list(leads or [])
        self.queries: list[str] = []

    def query(self, soql, bulk=False):
        self.queries.append(soql)
        if "FROM Task" in soql:
            return [dict(t) for t in self._tasks]
        if "FROM Lead" in soql:
            return [dict(l) for l in self._leads]
        return []


def _task(tid, who, subject=CLAIMER_SUBJECT, desc=CLAIMER_DESC,
          created="2026-06-19T10:00:00.000+0000"):
    return {"Id": tid, "WhoId": who, "Subject": subject,
            "Description": desc, "CreatedDate": created}


def _lead(lid, company="Acme GmbH", email="x@acme.de", enriched=None):
    return {
        "Id": lid, "FirstName": "F", "LastName": "L", "Company": company,
        "Email": email, "Website": None, "Status": "Open", "IsConverted": False,
        "LeadSource": "kununu", "KN_KununuProfileName__c": enriched,
        "KN_KununuUserUUID__c": None, "KN_KununuLinkProfile__c": None,
    }


SINCE = "2026-05-17T00:00:00Z"


# --- pure helpers ------------------------------------------------------------

def test_newest_task_per_who_keeps_latest():
    older = _task("T1", "00Q1", created="2026-06-01T00:00:00.000+0000")
    newer = _task("T2", "00Q1", created="2026-06-10T00:00:00.000+0000")
    result = newest_task_per_who([older, newer])
    assert len(result) == 1
    assert result[0]["Id"] == "T2"


def test_newest_task_per_who_drops_tasks_without_who():
    assert newest_task_per_who([_task("T1", None)]) == []


def test_is_already_enriched():
    assert is_already_enriched({"KN_KununuProfileName__c": "Acme GmbH"}) is True
    assert is_already_enriched({"KN_KununuProfileName__c": ""}) is False
    assert is_already_enriched({"KN_KununuProfileName__c": None}) is False
    assert is_already_enriched({}) is False


# --- orchestration -----------------------------------------------------------

def test_read_happy_path_extracts_lead():
    settings = Settings.load()  # only 'claimer' enabled
    client = FakeClient(tasks=[_task("T1", "00Q1")], leads=[_lead("00Q1")])
    result = read_extracted_leads(client, settings, since=SINCE)
    assert isinstance(result, ExtractionResult)
    assert len(result.leads) == 1
    e = result.leads[0]
    assert e.task_type == "claimer"
    assert e.lead_id == "00Q1"
    assert e.task_id == "T1"
    assert e.lead_company == "Acme GmbH"          # lead-native flows through
    assert e.company_name == "Hufeland Klinikum GmbH"  # parsed from Description
    assert result.skipped == []


def test_read_dedupes_to_newest_task_per_lead():
    settings = Settings.load()
    older = _task("T1", "00Q1", desc=CLAIMER_DESC,
                  created="2026-06-01T00:00:00.000+0000")
    newer = _task("T2", "00Q1", desc=CLAIMER_DESC_NEWER,
                  created="2026-06-10T00:00:00.000+0000")
    client = FakeClient(tasks=[older, newer], leads=[_lead("00Q1")])
    result = read_extracted_leads(client, settings, since=SINCE)
    assert len(result.leads) == 1
    assert result.leads[0].task_id == "T2"
    assert result.leads[0].company_name == "Newer Company GmbH"


def test_read_skips_already_enriched_lead():
    settings = Settings.load()
    client = FakeClient(tasks=[_task("T1", "00Q1")],
                        leads=[_lead("00Q1", enriched="Already GmbH")])
    result = read_extracted_leads(client, settings, since=SINCE)
    assert result.leads == []
    assert len(result.skipped) == 1
    assert result.skipped[0].lead_id == "00Q1"
    assert "enrich" in result.skipped[0].reason.lower()


def test_read_skips_lead_not_found_or_converted():
    settings = Settings.load()
    # Lead query returns nothing for the WhoId (converted, or WhoId was a Contact).
    client = FakeClient(tasks=[_task("T1", "00Q1")], leads=[])
    result = read_extracted_leads(client, settings, since=SINCE)
    assert result.leads == []
    assert len(result.skipped) == 1
    reason = result.skipped[0].reason.lower()
    assert "not found" in reason or "convert" in reason


def test_read_excludes_already_processed_tasks_silently():
    settings = Settings.load()
    client = FakeClient(tasks=[_task("T1", "00Q1")], leads=[_lead("00Q1")])
    result = read_extracted_leads(client, settings, since=SINCE,
                                  processed_task_ids={"T1"})
    assert result.leads == []
    assert result.skipped == []  # routine idempotency drop, not a skip-sheet entry
    assert not any("FROM Lead" in q for q in client.queries)  # no wasted lead query


def test_read_records_processed_tasks_for_watermark():
    settings = Settings.load()
    client = FakeClient(tasks=[_task("T1", "00Q1", created="2026-06-19T10:00:00.000+0000")],
                        leads=[_lead("00Q1")])
    result = read_extracted_leads(client, settings, since=SINCE)
    assert result.processed_tasks == [
        {"Id": "T1", "CreatedDate": "2026-06-19T10:00:00.000+0000"}
    ]


def test_read_records_processed_tasks_even_when_skipped():
    settings = Settings.load()
    # lead already enriched -> skipped, but the task is still "accounted for" (advance watermark)
    client = FakeClient(tasks=[_task("T1", "00Q1")],
                        leads=[_lead("00Q1", enriched="Already GmbH")])
    result = read_extracted_leads(client, settings, since=SINCE)
    assert [t["Id"] for t in result.processed_tasks] == ["T1"]


def test_read_passes_limit_to_task_query():
    settings = Settings.load()
    client = FakeClient(tasks=[_task("T1", "00Q1")], leads=[_lead("00Q1")])
    read_extracted_leads(client, settings, since=SINCE, limit=5)
    task_q = next(q for q in client.queries if "FROM Task" in q)
    assert "LIMIT 5" in task_q


def test_read_returns_empty_when_no_subjects_enabled():
    settings = Settings(
        raw={"task_types": {CLAIMER_SUBJECT: "claimer"}, "enabled_task_types": []},
        leadfeeder_api_key=None, sf_auth_url=None,
    )
    client = FakeClient(tasks=[_task("T1", "00Q1")], leads=[_lead("00Q1")])
    result = read_extracted_leads(client, settings, since=SINCE)
    assert result.leads == []
    assert result.skipped == []
    assert client.queries == []  # nothing enabled -> no org calls at all
