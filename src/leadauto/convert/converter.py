"""Match-aware bulk Lead conversion (ported + extended from Scripts/convert.py).

Kept from the legacy script: dynamic converted-status resolution, owner assignment,
`setDoNotCreateOpportunity(true)`, and the bulk `Database.convertLead(List, allOrNone=false)`
partial-success call. Added per the plan:

  * **match-aware** — per-lead `setAccountId` / `setContactId` from the approved match, so a
    matched Lead links to its existing Account/Contact instead of always creating new;
  * **idempotent** — the Apex re-filters `IsConverted = false`, so re-running converts nothing;
  * **robust results** — instead of regex-scraping debug lines, the Apex emits one
    sentinel-wrapped `JSON.serialize` blob that `parse_results` decodes. The whole run is
    wrapped in try/catch and the sentinel is ALWAYS printed (at LoggingLevel.ERROR so no
    debug-level filter can drop it); an uncaught-exception batch reports every lead as
    failed instead of vanishing. `convert_leads` reconciles results against its input and
    reports leads with no result row as `missing` (e.g. already-converted ones the Apex
    query filters out).

`dry_run` short-circuits before any Apex runs. Alias/owner/status come from config (the dead
`myOrg` alias is gone — the injected client targets `headless`).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from ..logging import get_logger

SENTINEL = "@@LEADAUTO@@"

# Injection guard for values embedded in generated Apex Id literals. Shape validation
# (15/18 chars) happens at the CSV boundary in stage2_convert.targets_from_approved;
# this is the last line of defense for ANY caller.
_APEX_ID_RE = re.compile(r"[A-Za-z0-9]{1,18}")


def _apex_str(value: str) -> str:
    """Escape a value for safe embedding inside an Apex single-quoted string literal."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _apex_id(value: str) -> str:
    """Refuse to embed anything but a plain alphanumeric Id in generated Apex."""
    if not _APEX_ID_RE.fullmatch(value or ""):
        raise ValueError(f"Refusing to embed non-alphanumeric Salesforce Id in Apex: {value!r}")
    return value


def _short_id(sf_id: str) -> str:
    """Case-sensitive 15-char prefix: Apex serializes 18-char Ids, CSV rows may carry 15."""
    return (sf_id or "")[:15]


@dataclass
class ConversionTarget:
    """One Lead to convert; account_id/contact_id link to existing records when present.

    lf_company_id clusters same-company create-new leads; kununu_url/kununu_profile_uuid
    feed the Stage 2 pool-account conflict gate (see convert/pool_enrich.py)."""
    lead_id: str
    account_id: str | None = None
    contact_id: str | None = None
    lf_company_id: str | None = None
    kununu_profile_uuid: str | None = None
    kununu_url: str | None = None


def _id_set(targets: list[ConversionTarget]) -> str:
    return ", ".join(f"'{_apex_id(t.lead_id)}'" for t in targets)


def _id_map(pairs: list[tuple[str, str]]) -> str:
    if not pairs:
        return "new Map<Id,Id>()"
    entries = ", ".join(f"'{_apex_id(k)}' => '{_apex_id(v)}'" for k, v in pairs)
    return f"new Map<Id,Id>{{ {entries} }}"


def build_convert_apex(
    targets: list[ConversionTarget], *, owner_id: str | None, converted_status: str,
    sentinel: str = SENTINEL,
) -> str:
    """Generate bulk, match-aware, idempotent conversion Apex for one batch of targets."""
    acct_pairs = [(t.lead_id, t.account_id) for t in targets if t.account_id]
    con_pairs = [(t.lead_id, t.contact_id) for t in targets if t.contact_id]
    owner_line = f"            lc.setOwnerId('{_apex_str(owner_id)}');\n" if owner_id else ""
    status_literal = _apex_str(converted_status)

    return f"""Set<Id> leadIds = new Set<Id>{{ {_id_set(targets)} }};
Map<Id,Id> acctByLead = {_id_map(acct_pairs)};
Map<Id,Id> conByLead = {_id_map(con_pairs)};

List<Map<String, Object>> out = new List<Map<String, Object>>();
Set<Id> reported = new Set<Id>();

try {{
    // Already-converted inputs are an idempotent no-op, not a silent drop: emit a result
    // row so reconciliation counts them (never as `missing`) and a re-run stays green.
    for (Lead l : [SELECT Id FROM Lead WHERE Id IN :leadIds AND IsConverted = true]) {{
        Map<String, Object> m = new Map<String, Object>();
        m.put('leadId', l.Id); m.put('success', true); m.put('alreadyConverted', true);
        out.add(m); reported.add(l.Id);
    }}

    // Resolve the converted LeadStatus: prefer the configured one, else the org's first converted status.
    String convertedStatus;
    for (LeadStatus s : [SELECT MasterLabel FROM LeadStatus WHERE IsConverted = true ORDER BY SortOrder NULLS LAST]) {{
        if (s.MasterLabel == '{status_literal}') {{ convertedStatus = s.MasterLabel; break; }}
        if (convertedStatus == null) {{ convertedStatus = s.MasterLabel; }}
    }}

    Map<Id, Lead> leadsById = new Map<Id, Lead>(
        [SELECT Id, Name FROM Lead WHERE Id IN :leadIds AND IsConverted = false]
    );

    List<Database.LeadConvert> conversions = new List<Database.LeadConvert>();

    if (convertedStatus == null) {{
        for (Lead l : leadsById.values()) {{
            Map<String, Object> m = new Map<String, Object>();
            m.put('leadId', l.Id); m.put('success', false);
            m.put('errors', new List<String>{{ 'No converted LeadStatus configured in this org.' }});
            out.add(m); reported.add(l.Id);
        }}
    }} else {{
        for (Lead l : leadsById.values()) {{
            Database.LeadConvert lc = new Database.LeadConvert();
            lc.setLeadId(l.Id);
{owner_line}            lc.setConvertedStatus(convertedStatus);
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
                out.add(m); reported.add(conversions[i].getLeadId());
            }}
        }}
    }}
}} catch (Exception ex) {{
    // Catching (not rethrowing) keeps any conversions committed before the exception; the
    // sentinel below must still print. Leads without a result row are reported failed.
    String err = ex.getTypeName() + ': ' + ex.getMessage();
    for (Id lid : leadIds) {{
        if (reported.contains(lid)) {{ continue; }}
        Map<String, Object> m = new Map<String, Object>();
        m.put('leadId', lid); m.put('success', false);
        m.put('errors', new List<String>{{ err }});
        out.add(m);
    }}
}}

// ERROR level: survives restrictive debug-log filter levels that would drop plain System.debug.
System.debug(LoggingLevel.ERROR, '{sentinel}' + JSON.serialize(out) + '{sentinel}');
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
    dry_run: bool = False, batch_size: int = 10, logger=None,
) -> dict:
    """Convert the targets in batches; aggregate per-lead results. No Apex runs under dry_run.

    Every input lead is reconciled against the returned results: leads already converted
    before this run come back as `already_converted` (the Apex emits an explicit row for
    them, so a re-run is a true no-op), while leads with NO result row at all (missing
    sentinel, or a run that died mid-batch) come back in `missing`/`missing_ids`. No lead
    can silently drop out of the counts.
    """
    log = logger or get_logger("convert")
    planned = len(targets)
    if dry_run:
        log.info("[dry-run] would convert %d lead(s) in batches of %d", planned, batch_size)
        return {"converted": 0, "already_converted": 0, "failed": 0, "planned": planned,
                "missing": 0, "missing_ids": [], "results": []}
    if not targets:
        return {"converted": 0, "already_converted": 0, "failed": 0, "planned": 0,
                "missing": 0, "missing_ids": [], "results": []}

    all_results: list[dict] = []
    missing_ids: list[str] = []
    for i in range(0, planned, batch_size):
        batch = targets[i:i + batch_size]
        apex = build_convert_apex(
            batch, owner_id=settings.owner_id, converted_status=settings.converted_status,
        )
        output = client.apex_run(apex)
        parsed = parse_results(output)
        returned = {_short_id(str(r.get("leadId") or "")) for r in parsed}
        absent = [t.lead_id for t in batch if _short_id(t.lead_id) not in returned]
        if absent:
            log.error("Batch %d: %d of %d lead(s) have no conversion result "
                      "(already converted, or the run died before the sentinel): %s",
                      i // batch_size + 1, len(absent), len(batch), ", ".join(absent))
            missing_ids.extend(absent)
        all_results.extend(parsed)

    already = sum(1 for r in all_results if r.get("alreadyConverted"))
    converted = sum(1 for r in all_results if r.get("success") and not r.get("alreadyConverted"))
    failed = sum(1 for r in all_results if not r.get("success"))
    log.info("Converted %d lead(s), %d already converted, %d failed, %d missing",
             converted, already, failed, len(missing_ids))
    return {"converted": converted, "already_converted": already, "failed": failed,
            "planned": planned, "missing": len(missing_ids), "missing_ids": missing_ids,
            "results": all_results}
