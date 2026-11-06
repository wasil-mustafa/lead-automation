"""Conversion targets for the fused pipeline run (since 2026-11-01, Wasil).

Two sources, both in-memory — the committed `review/approved_*.csv` handoff between the
former Stage 1 and Stage 2 is gone:

* `targets_from_results` — the leads THIS run tagged into an auto-convert campaign
  bucket (`live_scope.convert_campaigns`, "EnrichLeadfedup"), built straight from their
  `MatchResult`s with the same link rules the approved CSV used to enforce.
* `sweep_targets` — the SWEEP: every unconverted, queue-owned member of those campaigns
  the org still holds from EARLIER runs (tagged before the converter existed, or whose
  SOAP call failed once). Without it such a lead is stranded forever: the reader
  anti-joins campaign members out of every later read. Its decision comes from the
  lead's newest recorded prediction (`state/predictions.jsonl`); a lead with no
  recorded decision, or one outside `live_scope.decisions`, is LEFT to a human and
  reported. Swept leads written back before 2026-10-31 carry no `KN_NoOwnDBID__c`
  tick, so the sweep ticks it (one PATCH, live runs only) before the preflight reads
  — `KN_DbIdValidation` demands exactly one of DBID / no-own-DBID at conversion.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..logging import get_logger
from ..predictions import latest_by_lead, load_predictions
from ..sf import soql
from ..sf.client import SfError
from ..textnorm import is_blank, is_true
from ..writeback import lf_contact_email
from .converter import ConversionTarget
from .holdouts import load_holdouts

# Ids here come straight from the org (query rows), never from a hand-edited file; the
# converter re-guards every Id before embedding it in SOQL/SOAP (`sf.client.guard_sf_id`).

# Contact reasons that identify the same person (vs. a name/heuristic match): linking on
# these is safe. Weaker reasons (contact_email_global, linkedin) create a fresh contact
# under the linked account instead.
CONTACT_LINK_REASONS = {"kununu_user_uuid", "contact_email_in_account"}

# Reasons a swept campaign member is left to a human (Conversion sheet `detail`).
SWEEP_NO_DECISION = "sweep_no_recorded_decision"
SWEEP_TICK_FAILED = "sweep_dbid_tick_failed"
SWEEP_DBID_UNKNOWN = "sweep_dbid_fields_missing"   # query row carried neither DBID field
SWEEP_HELD_OUT = "sweep_held_out"   # state/convert_holdouts.jsonl: a permanent SOAP failure


def _short_id(sf_id) -> str:
    """Case-sensitive 15-char prefix: the org returns 18-char Ids, predictions and
    config may carry 15."""
    return str(sf_id or "")[:15]


_MISSING = object()


def _rel(row: dict, rel: str, field_name: str, default=None):
    """A relationship field off a query row, whichever transport produced it: REST
    nests it (`row["Lead"]["KN_DBID__c"]`), Bulk CSV flattens it (`row["Lead.KN_DBID__c"]`).
    `default` is returned when the field is ABSENT in both shapes (a null value is
    returned as None, which is different)."""
    nested = row.get(rel)
    if isinstance(nested, dict) and field_name in nested:
        return nested.get(field_name)
    return row.get(f"{rel}.{field_name}", default)


def targets_from_results(results, convert_ids, *, logger=None) -> list[ConversionTarget]:
    """One ConversionTarget per MatchResult whose lead is in `convert_ids`.

    Account is linked when the match found one (`result.account`; none under the
    create_new-only scope). Contact is linked only when its match reason is an
    identifier signal — kununu user UUID or an email inside the linked account; a
    weaker match creates a fresh contact. A contact link without an account link is
    dropped (`LeadConvert` requires `accountId` alongside `contactId`). A `skip`
    decision never converts: the live-scope gate excludes it, so hitting one here is
    a programming error and raises (the caller does not fence this — a wrong gate
    must abort the run, not convert).
    """
    log = logger or get_logger("convert.targets")
    wanted = {_short_id(i) for i in convert_ids}
    targets: list[ConversionTarget] = []
    for r in results:
        e = r.enriched.extracted
        if _short_id(e.lead_id) not in wanted:
            continue
        if r.decision == "skip":
            raise ValueError(f"lead {e.lead_id}: a skip decision reached the converter")
        account_id = r.account.account_id if r.account else None
        contact_id = r.contact.contact_id if r.contact else None
        if contact_id and (r.contact.reason or "") not in CONTACT_LINK_REASONS:
            contact_id = None
        if contact_id and not account_id:
            log.warning("lead %s: dropping contact link %s — LeadConvert requires an "
                        "account link alongside it; converting create-new instead",
                        e.lead_id, contact_id)
            contact_id = None
        targets.append(ConversionTarget(
            e.lead_id, account_id=account_id, contact_id=contact_id,
            lf_company_id=r.enriched.lf_company_id or None,
            kununu_profile_uuid=e.kununu_profile_uuid or None,
            kununu_url=e.kununu_url or None,
            lf_contact_email=lf_contact_email(r.enriched.contact),
        ))
    return targets


@dataclass
class SweepResult:
    targets: list[ConversionTarget] = field(default_factory=list)
    left: list[dict] = field(default_factory=list)      # lead_id, company, reason, ...
    info: dict[str, dict] = field(default_factory=dict)  # lead_id -> company/decision/...
    ticked: int = 0            # KN_NoOwnDBID__c ticks written (live) or needed (dry)
    tick_pending: set[str] = field(default_factory=set)  # dry run: would be ticked
    queried: bool = False      # False = sweep off (no campaign axis / no campaigns)


def sweep_targets(sf_client, settings, *, exclude_ids=(), predictions_path=None,
                  holdouts_path=None, dry_run: bool, logger=None) -> SweepResult:
    """Unconverted members of the auto-convert campaigns, minus this run's own leads,
    turned into targets where a recorded decision allows it. One read; one PATCH on a
    live run when any target still needs its `KN_NoOwnDBID__c` tick.

    `exclude_ids` must be EVERY lead this run extracted, not only its targets: a
    this-run `review` lead is already a member when this reads, and a stale
    `create_new` prediction from an earlier task must not convert it.

    `holdouts_path` (`convert/holdouts.py`): members listed there failed SOAP for a
    reason no retry clears (a duplicate rule) and are left to a human every run
    instead of being re-converted — read BEFORE the decision, so a held-out lead is
    never ticked either. None = no holdout file (tests); the file missing = none.
    """
    log = logger or get_logger("convert.sweep")
    out = SweepResult()
    buckets = settings.live_convert_buckets
    campaign_ids = [settings.lf_campaigns.get(b) for b in (buckets or [])]
    campaign_ids = [c for c in campaign_ids if c]
    if not campaign_ids:
        return out
    out.queried = True
    rows = sf_client.query(
        soql.campaign_members_unconverted(campaign_ids, settings.lead_statuses))
    skip = {_short_id(i) for i in exclude_ids}
    members: dict[str, dict] = {}
    for row in rows:
        lead_id = str(row.get("LeadId") or "")
        key = _short_id(lead_id)
        if not key or key in skip or key in members:
            continue
        dbid = _rel(row, "Lead", "KN_DBID__c", _MISSING)
        no_own = _rel(row, "Lead", "KN_NoOwnDBID__c", _MISSING)
        members[key] = {"lead_id": lead_id,
                        "company": _rel(row, "Lead", "Company"),
                        # both fields ABSENT from the row (not null: absent) = a transport
                        # shape this code does not know; never tick on a guess
                        "dbid_known": dbid is not _MISSING and no_own is not _MISSING,
                        "dbid": None if dbid is _MISSING else dbid,
                        "no_own": is_true(None if no_own is _MISSING else no_own)}
    log.info("Sweep: %d unconverted campaign member(s) from earlier runs, %d row(s) "
             "dropped (this run's leads or duplicate memberships)",
             len(members), len(rows) - len(members))
    if not members:
        return out

    preds = {}
    if predictions_path is not None:
        preds = {_short_id(k): v for k, v in
                 latest_by_lead(load_predictions(predictions_path)).items()}
    holdouts = load_holdouts(holdouts_path) if holdouts_path is not None else {}
    allowed = set(settings.live_decisions)
    need_tick: list[str] = []
    for key, m in members.items():
        pr = preds.get(key)
        info = {"company": m["company"], "decision": pr.decision if pr else None,
                "predicted_at": pr.predicted_at if pr else None}
        out.info[m["lead_id"]] = info
        held = holdouts.get(key)
        if held is not None:
            out.left.append({"lead_id": m["lead_id"], "reason": SWEEP_HELD_OUT,
                             "error": f"{held.reason} ({held.recorded_at}): {held.error}",
                             **info})
            continue
        if pr is None:
            out.left.append({"lead_id": m["lead_id"], "reason": SWEEP_NO_DECISION, **info})
            continue
        if pr.decision not in allowed:
            out.left.append({"lead_id": m["lead_id"],
                             "reason": f"sweep_decision_{pr.decision}", **info})
            continue
        if not m["dbid_known"]:
            out.left.append({"lead_id": m["lead_id"], "reason": SWEEP_DBID_UNKNOWN, **info})
            continue
        if is_blank(m["dbid"]) and not m["no_own"]:
            need_tick.append(m["lead_id"])
        out.targets.append(ConversionTarget(
            m["lead_id"],
            account_id=pr.account_id if pr.decision == "link" else None,
            lf_company_id=pr.lf_company_id or None,
            kununu_url=(pr.payload or {}).get("KN_KununuLinkProfile__c") or None,
        ))

    if need_tick:
        if dry_run:
            out.tick_pending = {_short_id(i) for i in need_tick}
            out.ticked = len(need_tick)
            log.info("[dry-run] Sweep: would tick KN_NoOwnDBID__c on %d lead(s)",
                     len(need_tick))
        else:
            failed = _tick_no_own_dbid(sf_client, need_tick, settings, log)
            out.ticked = len(need_tick) - len(failed)
            if failed:
                keep = []
                for t in out.targets:
                    err = failed.get(_short_id(t.lead_id))
                    if err is None:
                        keep.append(t)
                    else:
                        out.left.append({"lead_id": t.lead_id, "reason": SWEEP_TICK_FAILED,
                                         "error": err, **out.info[t.lead_id]})
                out.targets = keep
    log.info("Sweep: %d target(s), %d left to humans, %d DBID tick(s)",
             len(out.targets), len(out.left), out.ticked)
    return out


def _tick_no_own_dbid(sf_client, lead_ids: list[str], settings, log) -> dict[str, str]:
    """PATCH `KN_NoOwnDBID__c = true` on the given leads; returns {15-char id: error}
    for the rows the org rejected (a request-level error rejects them all). Sends the
    duplicate-rule header like the write-back: an Alert-mode Lead duplicate rule fails
    ANY update without it (31 of 56 backlog write-backs died on that, 2026-10-21)."""
    records = [{"Id": i, "KN_NoOwnDBID__c": True} for i in lead_ids]
    try:
        res = sf_client.update_records("Lead", records,
                                       allow_duplicates=settings.writeback_allow_duplicates)
    except SfError as exc:
        log.error("Sweep: KN_NoOwnDBID__c tick failed for all %d lead(s): %s",
                  len(lead_ids), exc)
        return {_short_id(i): str(exc)[:500] for i in lead_ids}
    failed = {_short_id(f.get("id")): str(f.get("error"))[:500]
              for f in res.get("failures", [])}
    if failed:
        log.warning("Sweep: KN_NoOwnDBID__c tick rejected on %d of %d lead(s)",
                    len(failed), len(lead_ids))
    return failed
