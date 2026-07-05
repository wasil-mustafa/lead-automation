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
