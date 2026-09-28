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
        "Email": email, "Website": None, "Status": "New", "IsConverted": False,
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
    # The skip is OFF in config.yaml since 2026-09-28; the switch still works when set.
    settings = Settings.load()
    settings.raw["pipeline"]["include_enriched_leads"] = False
    client = FakeClient(tasks=[_task("T1", "00Q1")],
                        leads=[_lead("00Q1", enriched="Already GmbH")])
    result = read_extracted_leads(client, settings, since=SINCE)
    assert result.leads == []
    assert len(result.skipped) == 1
    assert result.skipped[0].lead_id == "00Q1"
    assert "enrich" in result.skipped[0].reason.lower()


def test_include_enriched_leads_bypasses_marker_skip():
    # One-off backlog runs (pipeline.include_enriched_leads: true) re-process leads the
    # legacy manual pipeline enriched but never converted.
    settings = Settings.load()
    settings.raw["pipeline"]["include_enriched_leads"] = True
    client = FakeClient(tasks=[_task("T1", "00Q1")],
                        leads=[_lead("00Q1", enriched="Already GmbH")])
    result = read_extracted_leads(client, settings, since=SINCE)
    assert result.skipped == []
    assert [l.lead_id for l in result.leads] == ["00Q1"]


def test_config_yaml_processes_already_enriched_leads():
    # Wasil, 2026-09-28: the scheduled pipeline no longer skips marker-carrying leads.
    settings = Settings.load()
    assert settings.include_enriched_leads is True
    client = FakeClient(tasks=[_task("T1", "00Q1")],
                        leads=[_lead("00Q1", enriched="Already GmbH")])
    result = read_extracted_leads(client, settings, since=SINCE)
    assert [l.lead_id for l in result.leads] == ["00Q1"]


def test_read_skips_internal_lead_email():
    settings = Settings.load()  # config.yaml excludes kununu/xing/onlyfy
    client = FakeClient(tasks=[_task("T1", "00Q1")],
                        leads=[_lead("00Q1", email="tester@kununu.com")])
    result = read_extracted_leads(client, settings, since=SINCE)
    assert result.leads == []
    assert len(result.skipped) == 1
    assert result.skipped[0].reason == "internal company email domain"
    # still accounted for -> watermark advances, task is never re-fetched
    assert [t["Id"] for t in result.processed_tasks] == ["T1"]


def test_read_skips_internal_contact_email_from_description():
    settings = Settings.load()
    # Lead.Email is external, but the parsed Description contact email is internal.
    internal_desc = CLAIMER_DESC.replace("geschaeftsleitung@hufeland.de",
                                         "colleague@xing.com")
    client = FakeClient(tasks=[_task("T1", "00Q1", desc=internal_desc)],
                        leads=[_lead("00Q1", email="external@acme.de")])
    result = read_extracted_leads(client, settings, since=SINCE)
    assert result.leads == []
    assert len(result.skipped) == 1
    assert result.skipped[0].reason == "internal company email domain"


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
    settings.raw["pipeline"]["include_enriched_leads"] = False
    # lead already enriched -> skipped, but the task is still "accounted for" (advance watermark)
    client = FakeClient(tasks=[_task("T1", "00Q1")],
                        leads=[_lead("00Q1", enriched="Already GmbH")])
    result = read_extracted_leads(client, settings, since=SINCE)
    assert [t["Id"] for t in result.processed_tasks] == ["T1"]


def test_lead_resolution_chunks_ids_and_filters_contact_whoids():
    # 12,778 closed-task WhoIds in one leads_by_ids IN(...) breached the 100k-char SOQL
    # limit (backlog run 2026-07-15): only 00Q ids may be queried, in chunks of 500.
    settings = Settings.load()
    n = 1100
    tasks = [_task(f"T{i}", f"00Q{i:05d}") for i in range(n)]
    tasks.append(_task("TC", "003CONTACT"))
    client = FakeClient(tasks=tasks, leads=[_lead(f"00Q{i:05d}") for i in range(n)])
    result = read_extracted_leads(client, settings, since=SINCE)
    lead_queries = [q for q in client.queries if "FROM Lead" in q]
    assert len(lead_queries) == 3                      # 1100 ids -> 500/500/100
    assert not any("003CONTACT" in q for q in lead_queries)
    assert len(result.leads) == n
    assert [s.task_id for s in result.skipped] == ["TC"]  # contact task still accounted for


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


def test_event_lead_without_profile_link_is_skipped_with_link_it_is_extracted():
    # The ZPE one-off config (2026-09-18): event enabled, marker skip bypassed.
    settings = Settings.load("config/config.zpe-2026-09.yaml")
    desc = "ZPE 2026; Review: 3; Position: HR Consultant; Address: Weg 1, 44801 Bochum, DE; Note: x"
    tasks = [_task("T1", "00Q1", subject="Inbound Sales Task: Eventteilnahme", desc=desc),
             _task("T2", "00Q2", subject="Inbound Sales Task: Eventteilnahme", desc=desc)]
    linked = _lead("00Q2", company="SIVAplan GmbH", email="a@sivaplan.de")
    linked["KN_KununuLinkProfile__c"] = "https://www.kununu.com/de/sivaplan"
    client = FakeClient(tasks=tasks, leads=[_lead("00Q1"), linked])
    res = read_extracted_leads(client, settings, SINCE)
    assert [e.lead_id for e in res.leads] == ["00Q2"]
    assert res.leads[0].kununu_url == "https://www.kununu.com/de/sivaplan"
    assert [(s.task_id, s.reason) for s in res.skipped] == [
        ("T1", "event lead without kununu profile link")]
    # T1 is held OUT of the watermark: the operator fills the link in and re-runs, and the
    # task must be fetched again then — every other skip still advances it (see above).
    assert [t["Id"] for t in res.processed_tasks] == ["T2"]


def test_lead_statuses_allowlist_skips_other_statuses_post_query():
    # pipeline.lead_statuses (one-off "only Status = New" review runs): applied AFTER
    # the batched lead query — Status is already selected, so no extra API call — and
    # a lead in another status is skipped with its status named in the reason.
    settings = Settings.load()
    settings.raw["pipeline"]["lead_statuses"] = ["New"]
    new_lead = dict(_lead("00Q1"), Status="New")
    working = dict(_lead("00Q2"), Status="Working")
    blank = dict(_lead("00Q3"), Status=None)
    client = FakeClient(tasks=[_task("T1", "00Q1"), _task("T2", "00Q2"), _task("T3", "00Q3")],
                        leads=[new_lead, working, blank])
    result = read_extracted_leads(client, settings, since=SINCE)
    assert [l.lead_id for l in result.leads] == ["00Q1"]
    reasons = {s.lead_id: s.reason for s in result.skipped}
    assert reasons == {"00Q2": "lead status not in scope (Working)",
                       "00Q3": "lead status not in scope (blank)"}
    # the filter is post-query: the lead SOQL is unchanged and still selects Status
    lead_q = next(q for q in client.queries if "FROM Lead" in q)
    assert "Status" in lead_q and "Working" not in lead_q
    # skipped tasks are still accounted for (watermark semantics like any other skip)
    assert sorted(t["Id"] for t in result.processed_tasks) == ["T1", "T2", "T3"]


def test_lead_statuses_empty_means_no_filter():
    settings = Settings.load()
    settings.raw["pipeline"]["lead_statuses"] = []
    assert settings.lead_statuses == []
    client = FakeClient(tasks=[_task("T1", "00Q1")],
                        leads=[dict(_lead("00Q1"), Status="Working")])
    result = read_extracted_leads(client, settings, since=SINCE)
    assert [l.lead_id for l in result.leads] == ["00Q1"]
