"""Match-aware bulk Lead conversion (ported + extended from Scripts/convert.py).

Kept from the legacy script: dynamic converted-status resolution, owner assignment,
`setDoNotCreateOpportunity(true)`, and the bulk `Database.convertLead(List, allOrNone=false)`
partial-success call. Added per the plan:

  * **match-aware** — per-lead `setAccountId` / `setContactId` from the approved match, so a
    matched Lead links to its existing Account/Contact instead of always creating new;
  * **idempotent** — the Apex re-filters `IsConverted = false`, so re-running converts nothing;
  * **robust results** — instead of regex-scraping debug lines, the Apex emits one
    sentinel-wrapped `JSON.serialize` blob that `parse_results` decodes.

`dry_run` short-circuits before any Apex runs. Alias/owner/status come from config (the dead
`myOrg` alias is gone — the injected client targets `headless`).
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from ..logging import get_logger

SENTINEL = "@@LEADAUTO@@"


def _apex_str(value: str) -> str:
    """Escape a value for safe embedding inside an Apex single-quoted string literal."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


@dataclass
class ConversionTarget:
    """One Lead to convert; account_id/contact_id link to existing records when present."""
    lead_id: str
    account_id: str | None = None
    contact_id: str | None = None


def _id_set(targets: list[ConversionTarget]) -> str:
    return ", ".join(f"'{t.lead_id}'" for t in targets)


def _id_map(pairs: list[tuple[str, str]]) -> str:
    if not pairs:
        return "new Map<Id,Id>()"
    entries = ", ".join(f"'{k}' => '{v}'" for k, v in pairs)
    return f"new Map<Id,Id>{{ {entries} }}"


def build_convert_apex(
    targets: list[ConversionTarget], *, owner_id: str | None, converted_status: str,
    sentinel: str = SENTINEL,
) -> str:
    """Generate bulk, match-aware, idempotent conversion Apex for one batch of targets."""
    acct_pairs = [(t.lead_id, t.account_id) for t in targets if t.account_id]
    con_pairs = [(t.lead_id, t.contact_id) for t in targets if t.contact_id]
    owner_line = f"        lc.setOwnerId('{_apex_str(owner_id)}');\n" if owner_id else ""
    status_literal = _apex_str(converted_status)

    return f"""Set<Id> leadIds = new Set<Id>{{ {_id_set(targets)} }};
Map<Id,Id> acctByLead = {_id_map(acct_pairs)};
Map<Id,Id> conByLead = {_id_map(con_pairs)};

// Resolve the converted LeadStatus: prefer the configured one, else the org's first converted status.
String convertedStatus;
for (LeadStatus s : [SELECT MasterLabel FROM LeadStatus WHERE IsConverted = true ORDER BY SortOrder NULLS LAST]) {{
    if (s.MasterLabel == '{status_literal}') {{ convertedStatus = s.MasterLabel; break; }}
    if (convertedStatus == null) {{ convertedStatus = s.MasterLabel; }}
}}

Map<Id, Lead> leadsById = new Map<Id, Lead>(
    [SELECT Id, Name FROM Lead WHERE Id IN :leadIds AND IsConverted = false]
);

List<Map<String, Object>> out = new List<Map<String, Object>>();
List<Database.LeadConvert> conversions = new List<Database.LeadConvert>();

if (convertedStatus == null) {{
    for (Lead l : leadsById.values()) {{
        Map<String, Object> m = new Map<String, Object>();
        m.put('leadId', l.Id); m.put('success', false);
        m.put('errors', new List<String>{{ 'No converted LeadStatus configured in this org.' }});
        out.add(m);
    }}
}} else {{
    for (Lead l : leadsById.values()) {{
        Database.LeadConvert lc = new Database.LeadConvert();
        lc.setLeadId(l.Id);
{owner_line}        lc.setConvertedStatus(convertedStatus);
        lc.setDoNotCreateOpportunity(true);
        if (acctByLead.containsKey(l.Id)) {{ lc.setAccountId(acctByLead.get(l.Id)); }}
        if (conByLead.containsKey(l.Id)) {{ lc.setContactId(conByLead.get(l.Id)); }}
        conversions.add(lc);
    }}
    if (!conversions.isEmpty()) {{
        List<Database.LeadConvertResult> results = Database.convertLead(conversions, false);
        for (Integer i = 0; i < results.size(); i++) {{
            Database.LeadConvertResult r = results[i];
            Map<String, Object> m = new Map<String, Object>();
            m.put('leadId', conversions[i].getLeadId());
            m.put('success', r.isSuccess());
            if (r.isSuccess()) {{
                m.put('accountId', r.getAccountId());
                m.put('contactId', r.getContactId());
            }} else {{
                List<String> errs = new List<String>();
                for (Database.Error e : r.getErrors()) {{ errs.add(e.getStatusCode() + ': ' + e.getMessage()); }}
                m.put('errors', errs);
            }}
            out.add(m);
        }}
    }}
}}

System.debug('{sentinel}' + JSON.serialize(out) + '{sentinel}');
"""


def parse_results(output: str, sentinel: str = SENTINEL) -> list[dict]:
    """Decode the sentinel-wrapped JSON result blob from the Apex run output. [] if absent."""
    first = output.find(sentinel)
    if first == -1:
        return []
    start = first + len(sentinel)
    end = output.find(sentinel, start)
    if end == -1:
        return []
    blob = output[start:end].strip()
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def convert_leads(
    client, targets: list[ConversionTarget], settings, *,
    dry_run: bool = False, batch_size: int = 50, logger=None,
) -> dict:
    """Convert the targets in batches; aggregate per-lead results. No Apex runs under dry_run."""
    log = logger or get_logger("convert")
    planned = len(targets)
    if dry_run:
        log.info("[dry-run] would convert %d lead(s) in batches of %d", planned, batch_size)
        return {"converted": 0, "failed": 0, "planned": planned, "results": []}
    if not targets:
        return {"converted": 0, "failed": 0, "planned": 0, "results": []}

    all_results: list[dict] = []
    for i in range(0, planned, batch_size):
        batch = targets[i:i + batch_size]
        apex = build_convert_apex(
            batch, owner_id=settings.owner_id, converted_status=settings.converted_status,
        )
        output = client.apex_run(apex)
        parsed = parse_results(output)
        if not parsed:
            log.warning("Batch %d returned no parseable results", i // batch_size + 1)
        all_results.extend(parsed)

    converted = sum(1 for r in all_results if r.get("success"))
    failed = sum(1 for r in all_results if not r.get("success"))
    log.info("Converted %d lead(s), %d failed", converted, failed)
    return {"converted": converted, "failed": failed, "planned": planned, "results": all_results}
