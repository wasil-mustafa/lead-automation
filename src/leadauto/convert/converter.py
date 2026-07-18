"""Match-aware bulk Lead conversion via the SOAP Partner API.

History: the original implementation generated anonymous conversion Apex, but the
pipeline user lacks the "Author Apex" permission, so execute-anonymous runs in
restricted mode where `Database.convertLead` itself throws System.SecurityException
(root-caused 2026-07-10 — the May-2026 manual Apex conversions predate the revocation).
The user DOES hold Convert Leads, which the SOAP Partner API `convertLead()` call
honors, so conversion now goes through `client.convert_lead` (sf/soap.py builds and
parses the envelope). Semantics are unchanged:

  * **match-aware** — per-lead accountId/contactId from the approved match, so a
    matched Lead links to its existing Account/Contact instead of always creating new;
  * **idempotent** — already-converted inputs become explicit `alreadyConverted` result
    rows (never counted failed/missing), so a re-run is a true no-op that stays green.
    With `preverified=True` (Stage 2 ran preflight, whose Lead query selects IsConverted
    and drops converted leads) the per-batch `IsConverted = true` pre-query is skipped;
    the preflight→SOAP race is covered by mapping the org's already-converted SOAP error
    back to an `alreadyConverted` row. Without preverification the pre-query still runs
    per batch;
  * **reconciled** — every input lead must come back in the results; leads with no
    result row are counted `missing`, never silently dropped.

The converted LeadStatus is pinned in config (trusted without a per-run SOQL; a blank
config value falls back to querying the org's first converted status by SortOrder);
`dry_run` short-circuits before any org call.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

from ..logging import get_logger
from ..sf.client import guard_sf_id


def _id_in_list(ids) -> str:
    """Comma-joined quoted SOQL literals, each re-guarded to a bare Salesforce Id."""
    return ", ".join(f"'{guard_sf_id(i)}'" for i in ids)


def _short_id(sf_id: str) -> str:
    """Case-sensitive 15-char prefix: the org returns 18-char Ids, CSV rows may carry 15."""
    return (sf_id or "")[:15]


@dataclass
class ConversionTarget:
    """One Lead to convert; account_id/contact_id link to existing records when present.

    lf_company_id clusters same-company create-new leads; kununu_url/kununu_profile_uuid
    feed the Stage 2 pool-account conflict gate (see convert/pool_enrich.py);
    lf_contact_email feeds the post-convert KN_Email2__c enrichment on NEW Contacts."""
    lead_id: str
    account_id: str | None = None
    contact_id: str | None = None
    lf_company_id: str | None = None
    kununu_profile_uuid: str | None = None
    kununu_url: str | None = None
    lf_contact_email: str | None = None


def _resolve_converted_status(client, settings) -> str | None:
    """The configured converted-status label is trusted as-is — it is org config that
    never changes, so no LeadStatus SOQL per run (API-call reduction 2026-07-18; a
    wrong pin fails loudly org-side as an invalid-status conversion error). Only a
    blank config value falls back to querying the org's first converted LeadStatus
    by SortOrder. None when the org has no converted status."""
    pinned = (settings.converted_status or "").strip()
    if pinned:
        return pinned
    rows = client.query(
        "SELECT MasterLabel FROM LeadStatus WHERE IsConverted = true "
        "ORDER BY SortOrder NULLS LAST")
    labels = [r.get("MasterLabel") for r in rows if r.get("MasterLabel")]
    return labels[0] if labels else None


def _norm_email(value) -> str:
    return (value or "").strip().lower()


def enrich_contact_email2(client, targets: list[ConversionTarget], results: list[dict],
                          *, logger=None) -> dict:
    """Post-convert: stage the LF contact email as KN_Email2__c on NEW Contacts.

    Applies only to leads converted in THIS run into a fresh Contact (the target carried
    no contact link — an existing Contact is never touched) whose enrichment found a
    LeadFeeder contact email. The LF email lands in KN_Email2__c only when it differs from
    the Contact's primary Email (the task email, mapped from the Lead at conversion) and
    KN_Email2__c is still blank. Best-effort: a failure logs and never fails the run.
    """
    log = logger or get_logger("convert")
    lf_by_lead = {_short_id(t.lead_id): t.lf_contact_email
                  for t in targets if t.lf_contact_email and not t.contact_id}
    lf_by_contact: dict[str, str] = {}
    for r in results:
        if not r.get("success") or r.get("alreadyConverted"):
            continue
        cid = str(r.get("contactId") or "")
        lf_email = lf_by_lead.get(_short_id(str(r.get("leadId") or "")))
        if cid and lf_email:
            lf_by_contact[_short_id(cid)] = lf_email
    if not lf_by_contact:
        return {"processed": 0, "failed": 0}

    id_list = _id_in_list(sorted(lf_by_contact))
    updates: list[dict] = []
    try:
        contacts = client.query(
            f"SELECT Id, Email, KN_Email2__c FROM Contact WHERE Id IN ({id_list})")
        for c in contacts:
            lf_email = lf_by_contact.get(_short_id(str(c.get("Id") or "")))
            if not lf_email:
                continue
            if _norm_email(lf_email) == _norm_email(c.get("Email")):
                continue                       # same person/email — nothing to add
            if _norm_email(c.get("KN_Email2__c")):
                continue                       # fill-only-blank
            updates.append({"Id": c["Id"], "KN_Email2__c": lf_email})
        if not updates:
            return {"processed": 0, "failed": 0}
        res = client.update_records("Contact", updates)
        log.info("KN_Email2__c enrichment: %d contact(s) updated, %d failed",
                 res.get("processed", 0), res.get("failed", 0))
        return {"processed": res.get("processed", 0), "failed": res.get("failed", 0)}
    except Exception as exc:  # best-effort; must never fail a completed conversion run
        log.error("KN_Email2__c enrichment failed (conversions unaffected): %s", exc)
        return {"processed": 0, "failed": len(updates) or len(lf_by_contact)}


def _cluster_by_company(targets: list[ConversionTarget]) -> list[list[ConversionTarget]]:
    """Group create-new targets by lf_company_id (first-seen order); blank key => singleton.
    First member of each cluster is the primary (create-new), the rest are secondaries."""
    groups: dict[str, list[ConversionTarget]] = {}
    order: list[str] = []
    singletons: list[list[ConversionTarget]] = []
    for t in targets:
        key = (t.lf_company_id or "").strip()
        if not key:
            singletons.append([t])
            continue
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(t)
    return [groups[k] for k in order] + singletons


def _already_converted(client, batch: list[ConversionTarget]) -> set[str]:
    """15-char prefixes of batch leads the org already holds as IsConverted=true."""
    id_list = _id_in_list(t.lead_id for t in batch)
    rows = client.query(
        f"SELECT Id FROM Lead WHERE Id IN ({id_list}) AND IsConverted = true")
    return {_short_id(str(r.get("Id") or "")) for r in rows}


def _is_converted_race(row: dict) -> bool:
    """True when a failed SOAP row says the lead is ALREADY converted — the desired end
    state holds, so the row is a no-op, not a failure (same semantics the IsConverted
    pre-query gives; this covers the window between preflight/pre-query and the call)."""
    return any("CONVERTED_LEAD" in e.upper() or "ALREADY CONVERTED" in e.upper()
               for e in row.get("errors") or [])


def _run_convert(client, targets, settings, batch_size, log, phase_label="",
                 *, converted_status, preverified: bool = False):
    """Convert one target list in batches; return (results, missing_ids).

    Per batch: (1) pre-query already-converted inputs and emit explicit
    `alreadyConverted` rows (idempotent no-op, never `missing`) — skipped when
    `preverified` (preflight already proved IsConverted=false); (2) SOAP-convert the
    rest, mapping an already-converted SOAP error (the race window) back to an
    `alreadyConverted` row; (3) reconcile — any batch lead with no result row is
    reported `missing`.
    """
    results: list[dict] = []
    missing_ids: list[str] = []
    for i in range(0, len(targets), batch_size):
        batch = targets[i:i + batch_size]
        rows: list[dict] = []
        done = set() if preverified else _already_converted(client, batch)
        pending: list[ConversionTarget] = []
        for t in batch:
            if _short_id(t.lead_id) in done:
                rows.append({"leadId": t.lead_id, "success": True,
                             "alreadyConverted": True})
            else:
                pending.append(t)
        if pending:
            rows.extend(client.convert_lead(
                [{"lead_id": t.lead_id, "account_id": t.account_id,
                  "contact_id": t.contact_id} for t in pending],
                converted_status=converted_status, owner_id=settings.owner_id))
        for r in rows:
            if not r.get("success") and _is_converted_race(r):
                log.warning("%sLead %s was already converted when SOAP ran (converted "
                            "after the preflight/pre-query); counting as a no-op: %s",
                            phase_label, r.get("leadId"), "; ".join(r.get("errors") or []))
                r["success"] = True
                r["alreadyConverted"] = True
            elif not r.get("success"):
                log.error("%sLead %s conversion failed: %s", phase_label,
                          r.get("leadId"), "; ".join(r.get("errors") or ["(no error detail)"]))
        returned = {_short_id(str(r.get("leadId") or "")) for r in rows}
        absent = [t.lead_id for t in batch if _short_id(t.lead_id) not in returned]
        if absent:
            log.error("%sBatch %d: %d of %d lead(s) have no conversion result: %s",
                      phase_label, i // batch_size + 1, len(absent), len(batch),
                      ", ".join(absent))
            missing_ids.extend(absent)
        results.extend(rows)
    return results, missing_ids


def convert_leads(
    client, targets: list[ConversionTarget], settings, *,
    dry_run: bool = False, batch_size: int = 10, preverified: bool = False, logger=None,
) -> dict:
    """Convert the targets in batches; aggregate per-lead results. No org call under dry_run.

    `preverified=True` means the caller's preflight already queried every target's
    IsConverted this run (Stage 2 with preflight on): the per-batch idempotency
    pre-query is skipped and the preflight→SOAP race is handled via the SOAP error
    mapping in `_run_convert`.

    Every input lead is reconciled against the returned results: leads already converted
    before this run come back as `already_converted` (the pre-query emits an explicit row
    for them, so a re-run is a true no-op), while leads with NO result row at all come
    back in `missing`/`missing_ids`. No lead can silently drop out of the counts.
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

    converted_status = _resolve_converted_status(client, settings)
    if converted_status is None:
        log.error("No converted LeadStatus configured in this org; converting nothing")
        results = [{"leadId": t.lead_id, "success": False,
                    "errors": ["No converted LeadStatus configured in this org."]}
                   for t in targets]
        return {"converted": 0, "already_converted": 0, "failed": planned,
                "planned": planned, "missing": 0, "missing_ids": [], "results": results}

    existing_link = [t for t in targets if t.account_id]
    create_new = [t for t in targets if not t.account_id]
    clusters = _cluster_by_company(create_new)
    primaries = [c[0] for c in clusters]

    results1, missing1 = _run_convert(
        client, existing_link + primaries, settings, batch_size, log, "phase1 ",
        converted_status=converted_status, preverified=preverified)
    acct_by_primary = {_short_id(str(r.get("leadId") or "")): r["accountId"]
                       for r in results1 if r.get("success") and r.get("accountId")}

    phase2_targets: list[ConversionTarget] = []
    orphan_results: list[dict] = []
    for cluster in clusters:
        if len(cluster) < 2:
            continue
        primary, secondaries = cluster[0], cluster[1:]
        new_acct = acct_by_primary.get(_short_id(primary.lead_id))
        if new_acct:
            phase2_targets.extend(replace(s, account_id=new_acct) for s in secondaries)
        else:
            orphan_results.extend(
                {"leadId": s.lead_id, "success": False,
                 "errors": [f"primary {primary.lead_id} created no account; deferred"]}
                for s in secondaries)

    results2, missing2 = ((_run_convert(client, phase2_targets, settings, batch_size, log,
                                        "phase2 ", converted_status=converted_status,
                                        preverified=preverified))
                          if phase2_targets else ([], []))

    all_results = results1 + results2 + orphan_results
    missing_ids = missing1 + missing2
    already = sum(1 for r in all_results if r.get("alreadyConverted"))
    converted = sum(1 for r in all_results
                    if r.get("success") and not r.get("alreadyConverted"))
    failed = sum(1 for r in all_results if not r.get("success"))
    log.info("Converted %d lead(s), %d already converted, %d failed, %d missing",
             converted, already, failed, len(missing_ids))
    return {"converted": converted, "already_converted": already, "failed": failed,
            "planned": planned, "missing": len(missing_ids), "missing_ids": missing_ids,
            "results": all_results}
