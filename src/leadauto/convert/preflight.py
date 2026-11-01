"""Read-only conversion preflight — mirrors the org's blocking validation rules.

Each check is named after the org rule it mirrors (Tooling-API audit 2026-07-05, see
convert/CLAUDE.md "Conversion requirements"). check_lead is a pure function over SF-named
field values so Stage 2 can run it on live query rows and Stage 1 can preview it on
post-write-back state. The org/Apex remains the ultimate arbiter — this only fails
readably and earlier.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..logging import get_logger
from ..sf import soql
from ..textnorm import POSTAL_LEN_BY_CC, country_iso2, is_blank, is_true

# Same comma blob + substring semantics as KN_LeadValidatePrivateMail's CONTAINS().
# The substring match (short tokens can false-positive against e.g. "gmail"/"mail.com")
# is a DELIBERATE mirror of the org formula's CONTAINS semantics — do not "fix" it
# into a stricter match.
_FREEMAIL_BLOB = "gmail,yahoo,outlook,hotmail,aol,icloud,protonmail,zoho,mail.com"
_PHONE_RE = re.compile(r"\+[0-9]+")

PREFLIGHT_COLUMNS = ["lead_id", "check", "value", "detail"]


@dataclass
class Failure:
    check: str
    value: str
    detail: str


def check_lead(fields: dict, *, create_new: bool) -> list[Failure]:
    """Failures the org's validation rules would raise if this lead converted now."""
    if is_true(fields.get("IsConverted")):
        return [Failure("already_converted", "", "Lead is already converted (would no-op)")]
    fails: list[Failure] = []

    if is_blank(fields.get("KN_Employee__c")):
        fails.append(Failure("employee_category", "",
                             "KN_Employee__c blank (KN_EmployeeCategoryMandatory)"))

    dbid = (fields.get("KN_DBID__c") or "").strip()
    no_own = is_true(fields.get("KN_NoOwnDBID__c"))
    if bool(dbid) == no_own:
        fails.append(Failure("dbid_xor", f"KN_DBID__c={dbid!r} KN_NoOwnDBID__c={no_own}",
                             "need exactly one of KN_DBID__c / KN_NoOwnDBID__c "
                             "(KN_DbIdValidation)"))
    if dbid and not dbid.isdigit():
        fails.append(Failure("dbid_numeric", dbid,
                             "KN_DBID__c must be digits only (KN_ValidateDandBonlyNumberLead)"))

    if is_blank(fields.get("KN_Language__c")):
        fails.append(Failure("language", "",
                             "KN_Language__c blank (KN_LanguageCodemandatoryifqualified)"))

    cc = country_iso2(fields.get("CountryCode")) or country_iso2(fields.get("Country"))
    if create_new and (is_blank(fields.get("Street"))
                       or (is_blank(fields.get("Country")) and is_blank(fields.get("CountryCode")))):
        fails.append(Failure(
            "address_create_new",
            f"Street={fields.get('Street')!r} Country={fields.get('Country')!r}",
            "Street + Country required to create the Account "
            "(KN_ConvertedLeadAddressValidation)"))

    email = (fields.get("Email") or "").strip().lower()
    if email and "@" in email and not is_true(fields.get("HasOptedOutOfEmail")):
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
    expected = POSTAL_LEN_BY_CC.get(cc or "")
    if postal and not ecommerce and expected is not None and len(postal) != expected:
        fails.append(Failure("postal_length", postal,
                             f"PostalCode must be {expected} chars for {cc} "
                             "(KN_Checklengthpostalcode)"))
    return fails


def run_preflight(client, targets, logger=None, overlay: dict | None = None):
    """Split targets into (convertible, failure-records) against live Lead state.

    One bulk query; each failing target yields one row per failed check
    (keys = PREFLIGHT_COLUMNS). create-new is assumed when the target
    has no account_id. `overlay` = {15-char lead id: {field: value}} laid over the
    fetched row before the checks — a DRY run's preview of a write it would have made
    first (the sweep's `KN_NoOwnDBID__c` tick); live runs pass nothing.
    """
    log = logger or get_logger("convert.preflight")
    if not targets:
        return [], []
    rows = client.query(soql.leads_preflight([t.lead_id for t in targets]))
    by_id = {str(row.get("Id") or "")[:15]: row for row in rows}
    ok, failures = [], []
    for t in targets:
        row = by_id.get(str(t.lead_id)[:15])
        if row is None:
            failures.append({"lead_id": t.lead_id, "check": "not_found", "value": "",
                             "detail": "Lead not returned by the org (deleted or inaccessible)"})
            continue
        extra = (overlay or {}).get(str(t.lead_id)[:15])
        if extra:
            row = {**row, **extra}
        fails = check_lead(row, create_new=t.account_id is None)
        if fails:
            failures.extend({"lead_id": t.lead_id, "check": f.check,
                             "value": f.value, "detail": f.detail} for f in fails)
        else:
            ok.append(t)
    log.info("Preflight: %d/%d target(s) pass, %d failure record(s)",
             len(ok), len(targets), len(failures))
    return ok, failures
