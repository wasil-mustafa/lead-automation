"""Module 3b — write LeadFeeder/kununu enrichment back to existing Lead KN_* fields.

The mapping is pure (`lead_field_payload`): an `EnrichedLead` becomes an Id-keyed dict of
only the fields we actually have a value for (empty values are omitted so a partial
enrichment never blanks an existing field). `write_back` then pushes them via the Bulk API
in one declarative, partial-success call — short-circuited entirely under `dry_run`.

`KN_KununuLinkProfile__c` doubles as the "already processed" marker (see reader).

Org-verified 2026-07-03 (read-only describe + data sampling on `headless`):
  * `KN_Employee__c` is a RESTRICTED picklist; `_EMPLOYEE_BINS` mirrors its 14 labels
    verbatim (German dot separators). `KN_Region__c` is also restricted — the parsers
    only emit Germany/Austria/Switzerland (or None), all valid.
  * `KN_KununuProfileClaimed__c` is a real checkbox — the Bulk API CSV accepts the
    "true"/"false" strings written here.
  * `KN_DB*` fields are auto-populated by the org's D&B integration and are never
    written here (Wasil, 2026-07-05); conversion-requirement fields are fill-only-blank.
"""
from __future__ import annotations

import re

from .industry import sf_industry_from_kununu, sf_industry_from_leadfeeder
from .leadfeeder.enrich import EnrichedLead
from .logging import get_logger

# (upper_bound, label); counts above the last bound map to "> 100.000".
# Labels = the org's restricted KN_Employee__c picklist verbatim (describe on `headless`,
# 2026-07-03), incl. the German dot thousands separators — any other value is rejected.
_EMPLOYEE_BINS: list[tuple[int, str]] = [
    (10, "1-10"), (50, "11-50"), (100, "51-100"), (200, "101-200"),
    (350, "201-350"), (500, "351-500"), (750, "501-750"), (1000, "751-1.000"),
    (2500, "1.001-2.500"), (5000, "2.501-5.000"), (10000, "5.001-10.000"),
    (30000, "10.001-30.000"), (100000, "30.001-100.000"),
]

def employee_bin(count) -> str | None:
    """Map an employee headcount onto the KN_Employee__c picklist band. None if absent."""
    try:
        n = int(count)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    for upper, label in _EMPLOYEE_BINS:
        if n <= upper:
            return label
    return "> 100.000"


def normalize_phone(value) -> str | None:
    """Normalize to the org's +<digits> phone format; None when not confidently possible.

    Strips spaces/dashes/dots/parens/slashes, converts a 00 international prefix to +,
    and accepts only + followed by 6-15 digits (KN_ValidatePhone / KN_ContactValidPhoneNumber
    reject everything else). National 0... numbers are dropped: guessing a country is worse
    than leaving the field blank.
    """
    if not value:
        return None
    s = re.sub(r"[\s\-.()/]", "", str(value).strip())
    if s.startswith("00"):
        s = "+" + s[2:]
    return s if re.fullmatch(r"\+[0-9]{6,15}", s) else None


def split_first_name(source_name, last_name) -> str | None:
    """Leading remainder of `source_name` when it ends with the Lead's `last_name`.

    Guard against writing a different person's name: no last-name agreement, no split.
    """
    if not source_name or not last_name:
        return None
    src, ln = str(source_name).strip(), str(last_name).strip()
    # Whole trailing word only: " Bauer" must not match inside "Neubauer". This also
    # rejects source == lastname (no first name to split off).
    if not ln or not src.lower().endswith(" " + ln.lower()):
        return None
    first = src[: len(src) - len(ln)].strip()
    return first or None


def _iso2(value) -> str | None:
    if not value:
        return None
    v = str(value).strip()
    return v.upper() if len(v) == 2 else v


def _put(payload: dict, field: str, value) -> None:
    """Set a field only when there is a real value (skip None / empty string)."""
    if value is None:
        return
    if isinstance(value, str) and not value.strip():
        return
    payload[field] = value


_POSTAL_LEN_BY_CC = {"DE": 5, "CH": 4}  # KN_Checklengthpostalcode (Austria unchecked)


def _blank(value) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _fill(payload: dict, field: str, current, value) -> None:
    """_put, but only when the Lead's current value is blank (fill-only-blank policy)."""
    if _blank(current):
        _put(payload, field, value)


def dialable_phone(contact: dict, company: dict) -> str | None:
    """The phone we are allowed to write: contact direct dial > company phone.

    LF types each contact number; a 'direct'-typed number is the true direct dial and
    wins, with the office/landline number as the next-best fixed line. Mobile numbers
    are NEVER used (Wasil, 2026-07-10). Falls back to the LF company's main phone when
    the contact has neither.
    """
    return ((contact or {}).get("phone_direct")
            or (contact or {}).get("phone_office")
            or (company or {}).get("Company_Phone")
            or None)


def lead_field_payload(enriched: EnrichedLead) -> dict:
    """Build the Id-keyed write-back dict for one enriched Lead (only populated fields).

    Field spec (Wasil, 2026-07-10): from the Task only the kununu URL, Region and the
    contact email (-> standard Email, overwriting a differing Lead email); everything
    else comes from LeadFeeder — VAT from LF VAT_Id, employee band from LF headcount
    (the task's Unternehmensgröße line is never used). LinkedIn/Title/Phone come from
    the LF contact; company LinkedIn is never used and mobile numbers are never written.
    `Industry` ← kununu profile `profile_industry_name_de` mapped to the picklist
    (overwrite), else LeadFeeder industry mapped (fill-only-blank).
    """
    e = enriched.extracted
    company = enriched.company or {}
    contact = enriched.contact or {}
    payload: dict = {"Id": e.lead_id}

    # from the Task: kununu URL (doubles as the processed marker — see reader), region,
    # and the claimer/trial user's email onto the standard Email field. The task email
    # OVERWRITES a differing Lead email (Wasil, 2026-07-10): it is the person who
    # actually claimed/trialed, i.e. fresher than whatever the Lead was created with.
    # (KN_KununuUserUUID__c is NOT written — the DWH fills it once the URL is set.)
    _put(payload, "KN_KununuLinkProfile__c", e.kununu_url)
    _put(payload, "KN_Region__c", e.region)
    _put(payload, "Email", e.contact_email)

    # firmographics (LeadFeeder): VAT and employee band always from LF (the task's
    # Unternehmensgröße line is ~90% empty and only ever 'small'/'large' — unusable).
    # KN_Employee__c overwrites: fresh LF headcount beats a stale manual pick.
    _put(payload, "KN_VATNumber__c", company.get("VAT_Id"))
    _put(payload, "KN_Employee__c", employee_bin(company.get("Employee_Count")))

    # Industry ('Branche'): the kununu profile industry IS this taxonomy, so it is
    # authoritative and OVERWRITES (like KN_Region__c/CountryCode). The LeadFeeder
    # industry is a mapped fallback and fills only a blank. Unmapped -> nothing written
    # (industry.py logs the miss). See the 2026-07-13 industry-enrichment design doc.
    kununu_industry = sf_industry_from_kununu(enriched.feed_industry)
    if kununu_industry:
        _put(payload, "Industry", kununu_industry)
    else:
        _fill(payload, "Industry", e.lead_industry,
              sf_industry_from_leadfeeder(company.get("Industry")))

    # from the LF contact: LinkedIn, Title (position) — left empty when no contact found
    _put(payload, "KN_LinkedInProfileLink__c", contact.get("contact_linkedin"))
    _fill(payload, "Title", e.lead_title, contact.get("position"))

    # conversion requirements (org validation rules; all fill-only-blank)
    if _blank(e.lead_dbid) and not e.lead_no_own_dbid:
        payload["KN_NoOwnDBID__c"] = "true"  # KN_DbIdValidation XOR
    # address: always LeadFeeder (tasks carry no address)
    _fill(payload, "Street", e.lead_street, (company.get("Street") or "")[:100] or None)
    _fill(payload, "City", e.lead_city, company.get("City"))
    # country: kununu-URL-derived, the same slug logic as KN_Region__c (Wasil, 2026-07-10)
    # — it OVERWRITES the Lead's often-defaulted country, mirroring the region write.
    # Without a kununu slug, the LF company country fills only a fully blank country.
    # (CountryCode is the writable API form; the org's picklist derives Country from it.)
    slug_cc = (e.country_code or "").upper() or None
    lf_cc = (_iso2(company.get("Country")) or "").upper() or None
    if slug_cc:
        _put(payload, "CountryCode", slug_cc)
    elif _blank(e.lead_country):
        _fill(payload, "CountryCode", e.lead_country_code, lf_cc)
    postal = company.get("Postal_Code")
    effective_cc = (slug_cc or e.lead_country_code or lf_cc or "").upper()
    expected_len = _POSTAL_LEN_BY_CC.get(effective_cc)
    if postal and (expected_len is None or len(str(postal).strip()) == expected_len):
        _fill(payload, "PostalCode", e.lead_postal_code, str(postal).strip())
    _fill(payload, "FirstName", e.lead_first_name,
          split_first_name(e.contact_name or contact.get("full_name"), e.lead_last_name))
    _fill(payload, "Phone", e.lead_phone, normalize_phone(dialable_phone(contact, company)))

    return payload


def write_back(client, enriched_leads: list[EnrichedLead], dry_run: bool = False, logger=None) -> dict:
    """Write enrichment back to Leads. Returns {processed, payloads}; no call under dry_run.

    Leads whose payload carries only the Id (nothing enriched) are skipped — there is
    nothing to write and we never want to issue an empty update.
    """
    log = logger or get_logger("writeback")
    payloads = [lead_field_payload(e) for e in enriched_leads]
    writable = [p for p in payloads if len(p) > 1]  # more than just {"Id": ...}

    if dry_run:
        log.info("[dry-run] would write back %d/%d lead(s)", len(writable), len(payloads))
        return {"processed": 0, "failed": 0, "failures": [], "payloads": payloads}

    if not writable:
        log.info("Nothing to write back (%d lead(s) had no enrichment)", len(payloads))
        return {"processed": 0, "failed": 0, "failures": [], "payloads": payloads}

    res = client.update_records("Lead", writable)
    failures = [{"lead_id": f.get("id"), "stage": "writeback", "error": f.get("error", "")}
                for f in res.get("failures", [])]
    failed = res.get("failed", 0)
    if failed or not res.get("confirmed", True):
        log.warning("Write-back: %d row failure(s); result confirmed=%s",
                    failed, res.get("confirmed", True))
    log.info("Wrote back %d lead(s)", len(writable))
    return {"processed": res.get("processed", len(writable)), "failed": failed,
            "failures": failures, "payloads": payloads}
