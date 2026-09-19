"""QA artifact + review gate (Stage 1 output).

Builds the Excel workbook a human reviews before conversion. The `Review` sheet carries the
extracted/enriched signals plus the match decision and an empty `Approve?` column; the
reviewer sets `Approve?=yes`, commits the approved rows, and Stage 2 converts only those.
Other sheets surface the LeadFeeder contact split, out-of-scope skips, and a reconciling
summary. Pure data in, a `Workbook` out (write to disk via `write_workbook`).
"""
from __future__ import annotations

import csv
import re
from datetime import datetime, timezone
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

from .convert.preflight import check_lead
from .leadfeeder.enrich import CONTACT_SKIP_NOT_NEEDED
from .live_scope import live_scope_reason
from .textnorm import country_iso2
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
    "live_scope", "decision", "Approve?",
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
    # This key set pairs with soql._LEAD_FIELDS / check_lead's reads by convention —
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


# City-name prefixes that are not the city: 'Bad Homburg' vs 'Bad Kreuznach', 'St. Gallen'
# vs 'St. Pölten', 'Neu-Ulm' vs 'Neu-Isenburg' must NOT collapse to 'bad' / 'st' / 'neu'.
_CITY_PREFIXES = {"bad", "st", "sankt", "saint", "neu", "alt", "gross", "klein", "ober", "unter"}


def _norm_city(value) -> str:
    """Head word of a city, ASCII-folded both ways so 'Düsseldorf' == 'Duesseldorf' ==
    'Dusseldorf' and 'Köln' == 'Koeln'; trailing punctuation dropped. 'Frankfurt am Main'
    ~ 'Frankfurt', 'Bonn Bad-Godesberg' ~ 'Bonn', 'Berlin-Kreuzberg' ~ 'Berlin'. A leading
    prefix word ('Bad', 'St.', 'Neu') is kept TOGETHER with the word after it, so
    'Bad Homburg' != 'Bad Kreuznach' and 'St. Gallen' != 'St. Pölten'."""
    s = (value or "").strip().casefold()
    for a, b in (("ä", "a"), ("ö", "o"), ("ü", "u"), ("ß", "ss"), ("ae", "a"), ("oe", "o"),
                 ("ue", "u")):
        s = s.replace(a, b)
    s = re.sub(r"[^a-z0-9 \-]", " ", s).strip()
    words = [w for w in re.split(r"[ \-]+", s) if w]
    if not words:
        return ""
    if words[0] in _CITY_PREFIXES and len(words) > 1:
        return f"{words[0]} {words[1]}"
    return words[0]


def _norm_postal(value) -> str:
    s = (value or "").strip().upper().replace(" ", "")
    return s.split("-")[-1] if s else ""                   # "D-50969" ~ "50969"


def address_check_row(result) -> list:
    """One Address_Check row: task (badge) vs LeadFeeder vs current Lead address."""
    e = result.enriched.extracted
    c = result.enriched.company or {}
    lf_cc = country_iso2(c.get("Country")) or (c.get("Country") or None)
    task_has = bool(e.postal_code or e.city)
    lf_has = bool(c.get("Postal_Code") or c.get("City"))
    postal_match = city_match = ""
    if task_has and lf_has:
        postal_match = _yn(_norm_postal(e.postal_code) != "" and
                           _norm_postal(e.postal_code) == _norm_postal(c.get("Postal_Code")))
        city_match = _yn(_norm_city(e.city) != "" and _norm_city(e.city) == _norm_city(c.get("City")))
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
    live = ""
    if settings is not None:
        live = live_scope_reason(result, settings) or "yes"
    return {
        "lead_id": e.lead_id, "task_id": e.task_id, "task_type": e.task_type,
        "subject": e.subject,
        "company": e.best_company, "kununu_url": e.kununu_url, "region": e.region,
        "kununu_score": enr.kununu_score, "live_scope": live,
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
        "decision": result.decision, "Approve?": None,
    }


def _add_sheet(wb: Workbook, title: str, columns: list[str], rows: list[list]) -> None:
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
                   failures: list | None = None, settings=None) -> Workbook:
    """Assemble the QA workbook from match results + skip records + failures + run metadata."""
    meta = meta or {}
    failures = failures or []
    wb = Workbook()
    wb.remove(wb.active)  # drop the default empty sheet

    # ONE write-back payload per result, shared by Review, Enriching and Phone_Repair —
    # so the three sheets are guaranteed to describe the same write.
    payloads = [lead_field_payload(r.enriched, settings) for r in results]

    # Review
    review_rows = [[_review_row(r, settings, p).get(c) for c in REVIEW_COLUMNS]
                   for r, p in zip(results, payloads)]
    _add_sheet(wb, "Review", REVIEW_COLUMNS, review_rows)

    # Enriching — the write-back payload, paste-ready. Rows whose payload is only the Id
    # are dropped: `write_back` skips them too (nothing enriched), and an all-blank row in
    # an import file is an invitation to overwrite good data with nothing.
    enriching_rows = []
    for payload in payloads:
        if len(payload) > 1:
            enriching_rows.append([payload.get(c, "") for c in ENRICHING_COLUMNS])
    _add_sheet(wb, "Enriching", ENRICHING_COLUMNS, enriching_rows)

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
    _add_sheet(wb, "Phone_Repair", PHONE_REPAIR_COLUMNS, repair_rows)

    # Address_Check — badge-scan address vs LeadFeeder vs the Lead's current address
    address_rows = [address_check_row(r) for r in results]
    _add_sheet(wb, "Address_Check", ADDRESS_CHECK_COLUMNS, address_rows)

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
    _add_sheet(wb, "Contact_Found", CONTACT_FOUND_COLUMNS, found_rows)
    _add_sheet(wb, "NoContact", NOCONTACT_COLUMNS, nocontact_rows)

    # Skipped
    skipped_rows = [[s.task_id, s.lead_id, s.subject, s.reason] for s in skipped]
    _add_sheet(wb, "Skipped", SKIPPED_COLUMNS, skipped_rows)

    # Failures (LeadFeeder / write-back errors surfaced for the reviewer)
    failure_rows = [[f.get("lead_id"), f.get("stage"), f.get("error")] for f in failures]
    _add_sheet(wb, "Failures", FAILURES_COLUMNS, failure_rows)

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
        summary.append(["live_scope_pass", sum(
            1 for r in results if live_scope_reason(r, settings) is None)])
    summary.extend(["rule_" + name, rules.count(name)] for name in RULE_DECISIONS)
    _add_sheet(wb, "Summary", ["metric", "value"], summary)

    return wb


def write_auto_approved_csv(results: list, review_dir: str | Path, settings,
                            exclude_lead_ids: set | frozenset = frozenset(),
                            now: datetime | None = None) -> Path | None:
    """Auto-approved conversion CSV (`review_gate: false`, Wasil 2026-07-10; narrowed
    to the live scope for the phased go-live, 2026-07-18).

    Emits `review/approved_<UTCts>_auto.csv` holding exactly the Review rows inside the
    live scope (config `pipeline.live_scope`: eligible decision AND kununu score under
    the cap — see live_scope.py), `Approve?` pre-set — the same columns
    `targets_from_approved` reads, so Stage 2 (preflight, clustering, pool gate) runs
    unchanged. `review`/`skip` rows never qualify; leads in `exclude_lead_ids`
    (write-back failures — their fields would fail conversion validation anyway) are
    held back for the next run. Returns None without touching disk when nothing
    qualifies.
    """
    rows = [_review_row(r, settings) for r in results
            if live_scope_reason(r, settings) is None
            and r.enriched.extracted.lead_id not in exclude_lead_ids]
    if not rows:
        return None
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    path = Path(review_dir) / f"approved_{stamp}_auto.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=REVIEW_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({**row, "Approve?": "yes"})
    return path


def write_workbook(results: list, skipped: list, meta: dict | None = None,
                   path: str | Path = "qa_artifact.xlsx", failures: list | None = None,
                   settings=None) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    build_workbook(results, skipped, meta, failures, settings=settings).save(p)
    return p
