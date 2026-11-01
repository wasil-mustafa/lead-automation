"""QA artifact (the pipeline run's workbook).

Builds the Excel workbook a human reads after a run. The `Review` sheet carries the
extracted/enriched signals, the match decision and the live-scope verdicts; `Conversion`
carries what the run converted (this run's leads + the sweep of earlier runs' campaign
members) or, on a dry run, would convert. Other sheets surface the write-back payload,
the LeadFeeder contact split, out-of-scope skips, failures and a reconciling summary.
The review/approval gate (`Approve?` column, `review/approved_*.csv`) was removed on
2026-11-01 when conversion was fused into the run. Pure data in, a `Workbook` out
(write to disk via `write_workbook`).
"""
from __future__ import annotations

import re
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

from .campaigns import BUCKETS, campaign_bucket
from .convert.preflight import check_lead
from .models import CONTACT_SKIP_NOT_NEEDED
from .live_scope import (campaign_gate_reason, is_wide_match, live_scope_reason,
                         writeback_reason)
from .textnorm import country_iso2, normalize_city, normalize_postal
from .match import RULE_DECISIONS
from .writeback import lead_field_payload, lf_contact_email

REVIEW_COLUMNS = [
    "lead_id", "task_id", "task_type", "subject",
    "company", "kununu_url", "kununu_profile_uuid", "region", "kununu_score",
    "contact_name", "contact_email",
    "lf_matched", "lf_matched_name", "lf_match_score", "lf_name_agreement", "lf_agreed_via",
    "lf_match_pass", "lf_rejected",
    "lf_city", "lf_employees", "lf_industry", "predicted_industry", "lf_website", "lf_company_id",
    "lf_contact_email",
    "match_rule", "match_evidence",
    "account_match_id", "account_reason",
    "contact_match_id", "contact_score", "contact_reason",
    "convert_blockers",
    "live_writeback", "live_scope", "lf_campaign", "decision",
]
# Conversion (2026-11-01): one row per conversion target — `source` run = a lead this
# run tagged into an auto-convert campaign bucket, sweep = an unconverted member of
# that campaign from an earlier run (convert/targets.py; `decision`/`predicted_at`
# then come from its recorded prediction) — plus one per swept lead left to a human.
# `outcome`: converted | already_converted | failed | missing | preflight_skipped |
# left_to_human | dry_run | error | not_attempted; `detail` names the checks, error
# or reason.
CONVERSION_COLUMNS = [
    "lead_id", "company", "source", "decision", "predicted_at",
    "outcome", "detail", "account_id", "contact_id",
]
# The "Enriching" sheet: Salesforce field API names, in the order a reviewer reads them.
# One row per Lead the write-back would touch, carrying EXACTLY the values
# `lead_field_payload` produces — so the sheet is what the pipeline would write, not a
# re-derivation of it. That makes a dry run applicable by hand: paste the block into
# Salesforce Inspector Reloaded's Data Import (object Lead, action Update, id field Id).
ENRICHING_COLUMNS = [
    "Id", "FirstName", "Email", "Phone", "Website", "Title",
    "Street", "City", "PostalCode", "CountryCode",
    "Industry", "KN_Region__c", "KN_Employee__c", "KN_VATNumber__c",
    "KN_KununuLinkProfile__c", "KN_LinkedInProfileLink__c", "KN_NoOwnDBID__c",
]
# One row per Lead whose EXISTING phone the write-back rewrites (config
# `pipeline.repair_national_phone`, ZPE 2026): `+0<digits>` -> `+<cc><digits>`. Phone is
# otherwise fill-only-blank, so a payload Phone on a lead that already has one can only be
# this repair. Empty sheet = nothing rewritten (also with the switch off).
PHONE_REPAIR_COLUMNS = ["lead_id", "company", "country", "phone_before", "phone_after"]
# Address_Check (Wasil, 2026-09-18): the badge-scan address the Task carries
# (`Address: <street>, <postal> <city>, <CC>` in its Kommentar/Description, parsed by
# parse_event into the ExtractedLead's `street/postal_code/city`) side by side with the
# LeadFeeder company address (the one the write-back fills a blank Lead address from)
# and the Lead's current address. `verdict` compares task vs LeadFeeder:
#   same            postal code AND city agree
#   same_city       city agrees, postal code differs (or one side has none)
#   different       both sides carry an address and neither agrees
#   no_task_address / no_lf_address   one side has nothing to compare
ADDRESS_CHECK_COLUMNS = [
    "lead_id", "company", "decision",
    "task_street", "task_postal", "task_city", "task_cc",
    "lf_street", "lf_postal", "lf_city", "lf_cc",
    "lead_street", "lead_postal", "lead_city", "lead_cc",
    "postal_match", "city_match", "verdict",
]
CONTACT_FOUND_COLUMNS = [
    "lead_id", "company", "contact_name", "position",
    "contact_emails", "best_contact_phone", "contact_linkedin",
]
# `reason`: why no LF contact is attached — the lookup was skipped (config
# `contact_lookup_only_when_needed`: the Lead already has email and phone), the company
# never matched, or LF simply holds no such person.
NOCONTACT_COLUMNS = ["lead_id", "company", "contact_name", "lf_matched", "reason"]
SKIPPED_COLUMNS = ["task_id", "lead_id", "subject", "reason"]
FAILURES_COLUMNS = ["lead_id", "stage", "error"]

_HEADER_FONT = Font(bold=True)


def _yn(value: bool) -> str:
    return "yes" if value else "no"


def _convert_blockers(result, settings=None, payload: dict | None = None) -> str:
    """Preview of stage-2 preflight over the lead's post-write-back state.

    Current lead values overlaid with the write-back payload (fill-only-blank, so the
    overlay IS the post-run state). No proposed account match -> strictest create-new
    assumption. Empty string = would pass preflight today.
    """
    if result.decision == "skip":
        return ""  # terminal skip never converts — a preflight preview would be noise
    e = result.enriched.extracted
    # This key set pairs with soql.LEAD_FIELDS / check_lead's reads by convention —
    # extend both when adding a check.
    fields = {
        "IsConverted": False,
        "Street": e.lead_street, "City": e.lead_city, "PostalCode": e.lead_postal_code,
        "Country": e.lead_country, "CountryCode": e.lead_country_code,
        "Phone": e.lead_phone, "Email": e.lead_email,
        "HasOptedOutOfEmail": e.lead_opted_out_email, "LeadSource": e.lead_source,
        "KN_DBID__c": e.lead_dbid, "KN_NoOwnDBID__c": e.lead_no_own_dbid,
        "KN_Employee__c": e.lead_employee_category, "KN_Language__c": e.lead_language,
    }
    if payload is None:
        payload = lead_field_payload(result.enriched, settings)
    fields.update((k, v) for k, v in payload.items() if k != "Id")
    fails = check_lead(fields, create_new=result.account is None)
    return "; ".join(f.check for f in fails)


def address_check_row(result) -> list:
    """One Address_Check row: task (badge) vs LeadFeeder vs current Lead address."""
    e = result.enriched.extracted
    c = result.enriched.company or {}
    lf_cc = country_iso2(c.get("Country")) or (c.get("Country") or None)
    task_has = bool(e.postal_code or e.city)
    lf_has = bool(c.get("Postal_Code") or c.get("City"))
    postal_match = city_match = ""
    if task_has and lf_has:
        postal_match = _yn(normalize_postal(e.postal_code) is not None and
                           normalize_postal(e.postal_code) == normalize_postal(c.get("Postal_Code")))
        city_match = _yn(normalize_city(e.city) is not None
                         and normalize_city(e.city) == normalize_city(c.get("City")))
        if postal_match == "yes" and city_match == "yes":
            verdict = "same"
        elif city_match == "yes":
            verdict = "same_city"
        else:
            verdict = "different"
    elif not task_has:
        verdict = "no_task_address"
    else:
        verdict = "no_lf_address"
    # task_cc is the BADGE's country when the parser saw one; the kununu-URL country is
    # only a fallback, and labelling it as the badge country would hide an LF/badge
    # country disagreement (the class of miss that staged Canada/Bosnia onto DE leads).
    task_cc = (e.address_country_code or e.country_code or "").upper() or None
    return [
        e.lead_id, e.best_company, result.decision,
        e.street, e.postal_code, e.city, task_cc,
        c.get("Street"), c.get("Postal_Code"), c.get("City"), lf_cc,
        e.lead_street, e.lead_postal_code, e.lead_city, e.lead_country_code,
        postal_match, city_match, verdict,
    ]


def _review_row(result, settings=None, payload: dict | None = None) -> dict:
    e = result.enriched.extracted
    enr = result.enriched
    company = enr.company or {}
    if payload is None:
        payload = lead_field_payload(enr, settings)
    acct = result.account
    con = result.contact
    # live_scope: "yes" when the row would be processed live, else the exclusion
    # reason (see live_scope.py). Blank when no settings were passed (legacy callers).
    # A second-chance ("wide") match admitted by `allow_wide_match` is flagged
    # `yes_wide_match`, so a reviewer can tell the weaker claim from a primary one.
    # live_writeback is the same verdict for the narrower-or-equal write-back gate:
    # "yes" with live_scope "decision_link" = enriched live, converted by a human.
    live = live_wb = ""
    if settings is not None:
        yes = "yes_wide_match" if is_wide_match(result) else "yes"
        live_wb = writeback_reason(result, settings) or yes
    # lf_campaign: the LeadFeeder-outcome campaign a live run tags this lead with
    # (campaigns.py), judged as if the write-back succeeded. Blank when the lead is not
    # tagged (matched but not written back, other task type) or tagging is off.
    campaign = ""
    if settings is not None and settings.lf_campaigns:
        campaign = campaign_bucket(result, payload, written=live_wb.startswith("yes"),
                                   task_types=settings.lf_campaign_task_types) or ""
    if settings is not None:
        # conversion verdict = scope gate, then the campaign gate (2026-10-31) on the
        # bucket above — `campaign_matched_incomplete` = enriched live, human converts.
        live = (live_scope_reason(result, settings)
                or campaign_gate_reason(campaign or None, settings) or yes)
    return {
        "lead_id": e.lead_id, "task_id": e.task_id, "task_type": e.task_type,
        "subject": e.subject,
        "company": e.best_company, "kununu_url": e.kununu_url, "region": e.region,
        "kununu_score": enr.kununu_score, "live_scope": live, "live_writeback": live_wb,
        "lf_campaign": campaign,
        "contact_name": e.contact_name, "contact_email": e.contact_email or e.lead_email,
        "lf_matched": _yn(enr.matched), "lf_matched_name": enr.matched_name,
        "lf_match_score": enr.match_score, "lf_name_agreement": enr.name_agreement,
        "lf_agreed_via": enr.agreed_via,
        "lf_match_pass": enr.match_pass,
        "lf_rejected": enr.lf_rejected,
        "lf_city": company.get("City"), "lf_employees": company.get("Employee_Count"),
        "lf_industry": company.get("Industry"), "predicted_industry": payload.get("Industry"),
        "lf_website": company.get("Website"),
        "lf_company_id": enr.lf_company_id,
        # first LF contact email — Stage 2 writes it to the converted Contact's
        # KN_Email2__c when it differs from the Contact's primary Email
        "lf_contact_email": lf_contact_email(enr.contact),
        "kununu_profile_uuid": e.kununu_profile_uuid,
        "match_rule": result.rule,
        "match_evidence": "; ".join(result.evidence),
        "account_match_id": acct.account_id if acct else None,
        "account_reason": acct.reason if acct else None,
        "contact_match_id": con.contact_id if con else None,
        "contact_score": con.score if con else None,
        "contact_reason": con.reason if con else None,
        "convert_blockers": _convert_blockers(result, settings, payload),
        "decision": result.decision,
    }


def add_sheet(wb: Workbook, title: str, columns: list[str], rows: list[list]) -> None:
    ws = wb.create_sheet(title)
    ws.append(columns)
    for cell in ws[1]:
        cell.font = _HEADER_FONT
    for row in rows:
        ws.append(row)
    ws.freeze_panes = "A2"
    if rows or columns:
        ws.auto_filter.ref = f"A1:{get_column_letter(len(columns))}{max(len(rows) + 1, 1)}"
    for i, col in enumerate(columns, start=1):
        ws.column_dimensions[get_column_letter(i)].width = min(max(len(col) + 2, 12), 40)


def build_workbook(results: list, skipped: list, meta: dict | None = None,
                   failures: list | None = None, settings=None,
                   conversion: list | None = None) -> Workbook:
    """Assemble the QA workbook from match results + skip records + failures + the
    conversion rows (pipeline/run.py `_conversion_rows`) + run metadata."""
    meta = meta or {}
    failures = failures or []
    conversion = conversion or []
    wb = Workbook()
    wb.remove(wb.active)  # drop the default empty sheet

    # ONE write-back payload per result, shared by Review, Enriching and Phone_Repair —
    # so the three sheets are guaranteed to describe the same write.
    payloads = [lead_field_payload(r.enriched, settings) for r in results]

    # Review
    review_dicts = [_review_row(r, settings, p) for r, p in zip(results, payloads)]
    review_rows = [[d.get(c) for c in REVIEW_COLUMNS] for d in review_dicts]
    add_sheet(wb, "Review", REVIEW_COLUMNS, review_rows)

    # Enriching — the write-back payload, paste-ready. Rows whose payload is only the Id
    # are dropped: `write_back` skips them too (nothing enriched), and an all-blank row in
    # an import file is an invitation to overwrite good data with nothing.
    enriching_rows = []
    for payload in payloads:
        if len(payload) > 1:
            enriching_rows.append([payload.get(c, "") for c in ENRICHING_COLUMNS])
    add_sheet(wb, "Enriching", ENRICHING_COLUMNS, enriching_rows)

    # Phone_Repair — the before/after of every existing phone the write-back rewrites
    repair_rows = []
    for r, payload in zip(results, payloads):
        e = r.enriched.extracted
        before = (e.lead_phone or "").strip()
        after = payload.get("Phone")
        if before and after and after != before:
            repair_rows.append([e.lead_id, e.best_company,
                                payload.get("CountryCode") or e.lead_country_code,
                                before, after])
    add_sheet(wb, "Phone_Repair", PHONE_REPAIR_COLUMNS, repair_rows)

    # Address_Check — badge-scan address vs LeadFeeder vs the Lead's current address
    address_rows = [address_check_row(r) for r in results]
    add_sheet(wb, "Address_Check", ADDRESS_CHECK_COLUMNS, address_rows)

    # Contact split
    found_rows, nocontact_rows = [], []
    for r in results:
        e = r.enriched.extracted
        if r.enriched.contact_found:
            c = r.enriched.contact or {}
            found_rows.append([
                e.lead_id, e.best_company, e.contact_name, c.get("position"),
                c.get("contact_emails"), c.get("best_contact_phone"), c.get("contact_linkedin"),
            ])
        else:
            reason = (r.enriched.contact_skipped
                      or ("not found in LeadFeeder" if r.enriched.matched
                          else "no LeadFeeder company match"))
            nocontact_rows.append([e.lead_id, e.best_company, e.contact_name,
                                   _yn(r.enriched.matched), reason])
    add_sheet(wb, "Contact_Found", CONTACT_FOUND_COLUMNS, found_rows)
    add_sheet(wb, "NoContact", NOCONTACT_COLUMNS, nocontact_rows)

    # Skipped
    skipped_rows = [[s.task_id, s.lead_id, s.subject, s.reason] for s in skipped]
    add_sheet(wb, "Skipped", SKIPPED_COLUMNS, skipped_rows)

    # Failures (LeadFeeder / write-back errors surfaced for the reviewer)
    failure_rows = [[f.get("lead_id"), f.get("stage"), f.get("error")] for f in failures]
    add_sheet(wb, "Failures", FAILURES_COLUMNS, failure_rows)

    # Conversion — what the run converted (or, dry, would convert), run + sweep
    add_sheet(wb, "Conversion", CONVERSION_COLUMNS,
              [[row.get(c) for c in CONVERSION_COLUMNS] for row in conversion])

    # Summary
    decisions = [r.decision for r in results]
    rules = [r.rule for r in results]
    summary = [
        ["generated_at", meta.get("generated_at")],
        ["since", meta.get("since")],
        ["new_watermark", meta.get("new_watermark")],
        ["dry_run", meta.get("dry_run")],
        ["total_leads", len(results)],
        ["lf_matched", sum(1 for r in results if r.enriched.matched)],
        ["contacts_found", sum(1 for r in results if r.enriched.contact_found)],
        # only the config-driven skip; a missing name or a failed search is not a "lookup saved"
        ["contact_lookups_skipped", sum(1 for r in results
                                        if r.enriched.contact_skipped == CONTACT_SKIP_NOT_NEEDED)],
        ["phones_repaired", len(repair_rows)],
        *(["address_" + v, sum(1 for row in address_rows if row[-1] == v)]
          for v in ("same", "same_city", "different", "no_task_address", "no_lf_address")),
        ["decision_link", decisions.count("link")],
        ["decision_review", decisions.count("review")],
        ["decision_create_new", decisions.count("create_new")],
        ["decision_skip", decisions.count("skip")],
        ["skipped", len(skipped)],
    ]
    if settings is not None:
        summary.append(["live_writeback_pass",
                        sum(1 for r in results if writeback_reason(r, settings) is None)])
        # live_scope_pass counts the Review verdict, which includes the campaign gate
        live_ids = {d["lead_id"] for d in review_dicts if d["live_scope"].startswith("yes")}
        live = [r for r in results if r.enriched.extracted.lead_id in live_ids]
        summary.append(["live_scope_pass", len(live)])
        summary.append(["live_scope_wide_match", sum(1 for r in live if is_wide_match(r))])
        if settings.lf_campaigns:
            summary.extend(["campaign_" + b, sum(1 for d in review_dicts
                                                  if d["lf_campaign"] == b)]
                           for b in BUCKETS)
            # live runs only: what the CampaignMember insert actually did
            summary.extend([k, meta[k]] for k in ("campaign_duplicate_detected",
                                                  "campaign_created",
                                                  "campaign_already_member",
                                                  "campaign_failed") if k in meta)
    # conversion counters, tallied from the FINAL Conversion rows (pipeline/run.py);
    # absent when the run did not convert (convert=False) and swept nothing
    summary.extend([k, meta[k]] for k in CONVERSION_SUMMARY_KEYS if k in meta)
    summary.extend(["rule_" + name, rules.count(name)] for name in RULE_DECISIONS)
    add_sheet(wb, "Summary", ["metric", "value"], summary)

    return wb


CONVERSION_SUMMARY_KEYS = (
    "convert_targets_run", "convert_targets_sweep", "converted",
    "convert_already_converted", "convert_failed", "convert_missing",
    "convert_preflight_skipped", "convert_left_to_human", "convert_dry_run",
    "sweep_dbid_ticked", "convert_error",
)


def write_workbook(results: list, skipped: list, meta: dict | None = None,
                   path: str | Path = "qa_artifact.xlsx", failures: list | None = None,
                   settings=None, conversion: list | None = None) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    build_workbook(results, skipped, meta, failures, settings=settings,
                   conversion=conversion).save(p)
    return p
