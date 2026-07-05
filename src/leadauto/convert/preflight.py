"""Read-only conversion preflight — mirrors the org's blocking validation rules.

Each check is named after the org rule it mirrors (Tooling-API audit 2026-07-05, see
convert/CLAUDE.md "Conversion requirements"). check_lead is a pure function over SF-named
field values so Stage 2 can run it on live query rows and Stage 1 can preview it on
post-write-back state. The org/Apex remains the ultimate arbiter — this only fails
readably and earlier.
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path

from ..logging import get_logger

# Same comma blob + substring semantics as KN_LeadValidatePrivateMail's CONTAINS().
# The substring match (short tokens can false-positive against e.g. "gmail"/"mail.com")
# is a DELIBERATE mirror of the org formula's CONTAINS semantics — do not "fix" it
# into a stricter match.
_FREEMAIL_BLOB = "gmail,yahoo,outlook,hotmail,aol,icloud,protonmail,zoho,mail.com"
_POSTAL_LEN_BY_COUNTRY = {"Germany": 5, "Switzerland": 4}
_COUNTRY_BY_CC = {"DE": "Germany", "CH": "Switzerland"}
_PHONE_RE = re.compile(r"\+[0-9]+")

PREFLIGHT_CSV_COLUMNS = ["lead_id", "check", "value", "detail"]


@dataclass
class Failure:
    check: str
    value: str
    detail: str


def _blank(v) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


def _is_true(v) -> bool:
    return v is True or str(v).strip().lower() == "true"


def check_lead(fields: dict, *, create_new: bool) -> list[Failure]:
    """Failures the org's validation rules would raise if this lead converted now."""
    if _is_true(fields.get("IsConverted")):
        return [Failure("already_converted", "", "Lead is already converted (would no-op)")]
    fails: list[Failure] = []

    if _blank(fields.get("KN_Employee__c")):
        fails.append(Failure("employee_category", "",
                             "KN_Employee__c blank (KN_EmployeeCategoryMandatory)"))

    dbid = (fields.get("KN_DBID__c") or "").strip()
    no_own = _is_true(fields.get("KN_NoOwnDBID__c"))
    if bool(dbid) == no_own:
        fails.append(Failure("dbid_xor", f"KN_DBID__c={dbid!r} KN_NoOwnDBID__c={no_own}",
                             "need exactly one of KN_DBID__c / KN_NoOwnDBID__c "
                             "(KN_DbIdValidation)"))
    if dbid and not dbid.isdigit():
        fails.append(Failure("dbid_numeric", dbid,
                             "KN_DBID__c must be digits only (KN_ValidateDandBonlyNumberLead)"))

    if _blank(fields.get("KN_Language__c")):
        fails.append(Failure("language", "",
                             "KN_Language__c blank (KN_LanguageCodemandatoryifqualified)"))

    country = (fields.get("Country") or "").strip() or _COUNTRY_BY_CC.get(
        (fields.get("CountryCode") or "").strip().upper(), "")
    if create_new and (_blank(fields.get("Street"))
                       or (_blank(fields.get("Country")) and _blank(fields.get("CountryCode")))):
        fails.append(Failure(
            "address_create_new",
            f"Street={fields.get('Street')!r} Country={fields.get('Country')!r}",
            "Street + Country required to create the Account "
            "(KN_ConvertedLeadAddressValidation)"))

    email = (fields.get("Email") or "").strip().lower()
    if email and "@" in email and not _is_true(fields.get("HasOptedOutOfEmail")):
        token = email.split("@", 1)[1].split(".", 1)[0]
        # The org rule KN_LeadValidatePrivateMail has a Status != 'New' condition that is
        # deliberately NOT mirrored here, because conversion always moves Status off 'New'
        # in the same transaction — at the moment this check predicts (conversion time),
        # the condition is always satisfied.
        if token and token in _FREEMAIL_BLOB:
            fails.append(Failure("freemail_email", email,
                                 "private email blocks conversion unless opted out "
                                 "(KN_LeadValidatePrivateMail)"))

    ecommerce = (fields.get("LeadSource") or "").strip() == "E-commerce"
    phone = (fields.get("Phone") or "").strip()
    if phone and not ecommerce and not _PHONE_RE.fullmatch(phone):
        fails.append(Failure("phone_format", phone,
                             "Phone must be + followed by digits only (KN_ValidatePhone)"))

    street = fields.get("Street") or ""
    if len(street) > 100:
        fails.append(Failure("street_length", street[:40] + "...",
                             "Street exceeds 100 characters (KN_Addresstoolong)"))

    postal = (fields.get("PostalCode") or "").strip()
    expected = _POSTAL_LEN_BY_COUNTRY.get(country)
    if postal and not ecommerce and expected is not None and len(postal) != expected:
        fails.append(Failure("postal_length", postal,
                             f"PostalCode must be {expected} chars for {country} "
                             "(KN_Checklengthpostalcode)"))
    return fails


def run_preflight(client, targets, logger=None):
    """Split targets into (convertible, failure-records) against live Lead state.

    One bulk query; each failing target yields one row per failed check
    (keys = PREFLIGHT_CSV_COLUMNS). create-new is assumed when the target
    has no account_id.
    """
    log = logger or get_logger("convert.preflight")
    if not targets:
        return [], []
    from ..sf import soql  # local import: keep module import-light for qa_artifact
    rows = client.query(soql.leads_preflight([t.lead_id for t in targets]))
    by_id = {row.get("Id"): row for row in rows}
    ok, failures = [], []
    for t in targets:
        row = by_id.get(t.lead_id)
        if row is None:
            failures.append({"lead_id": t.lead_id, "check": "not_found", "value": "",
                             "detail": "Lead not returned by the org (deleted or inaccessible)"})
            continue
        fails = check_lead(row, create_new=t.account_id is None)
        if fails:
            failures.extend({"lead_id": t.lead_id, "check": f.check,
                             "value": f.value, "detail": f.detail} for f in fails)
        else:
            ok.append(t)
    log.info("Preflight: %d/%d target(s) pass, %d failure record(s)",
             len(ok), len(targets), len(failures))
    return ok, failures


def write_failures_csv(failures: list[dict], path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=PREFLIGHT_CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(failures)
    return p
