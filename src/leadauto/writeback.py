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
from .models import EnrichedLead
from .textnorm import POSTAL_LEN_BY_CC, country_iso2, is_blank
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


_TRUNK_ZERO_CC = {"DE": "49", "AT": "43", "CH": "41"}


def repair_national_phone(lead_phone, country_code) -> str | None:
    """`+0<digits>` on a DE/AT/CH lead -> `+<cc><digits without the trunk zero>`.

    The ZPE 2026 badge import wrote national numbers with a '+' in front of the trunk
    zero (`+02091584338` for 0209 1584338). They pass KN_ValidatePhone but dial nowhere
    from abroad. Only the three countries whose trunk prefix is a single 0 are repaired;
    anything else, including a phone that is already international, returns None.
    """
    if not lead_phone:
        return None
    s = re.sub(r"[\s\-.()/]", "", str(lead_phone).strip())
    cc = _TRUNK_ZERO_CC.get((country_code or "").upper())
    if not cc or not re.fullmatch(r"\+0[1-9][0-9]{4,13}", s):
        return None
    return normalize_phone("+" + cc + s[2:])


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


def _put(payload: dict, field: str, value) -> None:
    """Set a field only when there is a real value (skip None / empty string)."""
    if value is None:
        return
    if isinstance(value, str) and not value.strip():
        return
    payload[field] = value


def _fill(payload: dict, field: str, current, value) -> None:
    """_put, but only when the Lead's current value is blank (fill-only-blank policy)."""
    if is_blank(current):
        _put(payload, field, value)


def lf_contact_email(contact: dict) -> str | None:
    """The first (best) email LeadFeeder holds for the contact, or None.

    `flatten_contact_details` joins the addresses with '; ' in LF's order, which puts
    the primary/verified address first. Also read by the QA sheet's `lf_contact_email`.
    """
    first = ((contact or {}).get("contact_emails") or "").split(";")[0].strip()
    return first or None


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


def lead_field_payload(enriched: EnrichedLead, settings=None) -> dict:
    """Build the Id-keyed write-back dict for one enriched Lead (only populated fields).

    Field spec (Wasil, 2026-07-10): from the Task only the kununu URL, Region and the
    contact email (-> standard Email, overwriting a differing Lead email; with no Task
    email the LF contact email fills a blank Lead.Email, 2026-09-18); everything
    else comes from LeadFeeder — VAT from LF VAT_Id, employee band from LF headcount
    (the task's Unternehmensgröße line is never used). LinkedIn/Title/Phone come from
    the LF contact; company LinkedIn is never used and mobile numbers are never written.
    `Industry` ← kununu profile `profile_industry_name_de` mapped to the picklist
    (overwrite), else LeadFeeder industry mapped (fill-only-blank).
    `settings` (optional) carries the one-off `pipeline.repair_national_phone` switch.
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
    # Without a Task email (event badge scans carry none) the LF contact's email fills
    # a BLANK Lead.Email only (Wasil, 2026-09-18) — never overwrites: an existing
    # address is the one the person gave us, and writing Lead.Email has the Braze
    # side effect documented in CLAUDE.md. Same fill-only-blank rule as Phone below.
    if is_blank(e.contact_email):
        _fill(payload, "Email", e.lead_email, lf_contact_email(contact))

    # firmographics (LeadFeeder): VAT and employee band always from LF (the task's
    # Unternehmensgröße line is ~90% empty and only ever 'small'/'large' — unusable).
    # KN_Employee__c overwrites: fresh LF headcount beats a stale manual pick.
    _put(payload, "KN_VATNumber__c", company.get("VAT_Id"))
    _put(payload, "KN_Employee__c", employee_bin(company.get("Employee_Count")))
    # Website is FILL-ONLY-BLANK, unlike the VAT/employee overwrites above: it is a
    # matching key (match.py hosts a lead on it), and replacing a human-entered domain
    # with LeadFeeder's guess would move the lead to a different account.
    _fill(payload, "Website", e.lead_website, company.get("Website"))

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
    # Title: a Task-parsed position (event badge scans carry one; claimer/trial tasks
    # never do) beats LeadFeeder's — the attendee stated it about themselves.
    _fill(payload, "Title", e.lead_title, e.position or contact.get("position"))

    # conversion requirements (org validation rules; all fill-only-blank)
    # KN_NoOwnDBID__c is deliberately NOT written (Wasil, 2026-10-20): it used to be ticked
    # for every DBID-less lead to satisfy KN_DbIdValidation; now it is left alone, so such a
    # lead fails Stage 2 preflight `dbid_xor` until a human or D&B resolves it.
    # address: always LeadFeeder (spec 2026-07-10, pinned by TestAddressFill). The event
    # parser stores the badge address in the parsed fields only; it is deliberately NOT
    # used here — changing that means changing the spec and its test together.
    _fill(payload, "Street", e.lead_street, (company.get("Street") or "")[:100] or None)
    _fill(payload, "City", e.lead_city, company.get("City"))
    # country: kununu-URL-derived, the same slug logic as KN_Region__c (Wasil, 2026-07-10)
    # — it OVERWRITES the Lead's often-defaulted country, mirroring the region write.
    # Without a kununu slug, the LF company country fills only a fully blank country.
    # (CountryCode is the writable API form; the org's picklist derives Country from it.)
    slug_cc = (e.country_code or "").upper() or None
    lf_cc = country_iso2(company.get("Country"))   # LF sends names ("Germany"); the org wants ISO2
    if slug_cc:
        _put(payload, "CountryCode", slug_cc)
    elif is_blank(e.lead_country):
        _fill(payload, "CountryCode", e.lead_country_code, lf_cc)
    postal = company.get("Postal_Code")
    effective_cc = (slug_cc or e.lead_country_code or lf_cc or "").upper()
    expected_len = POSTAL_LEN_BY_CC.get(effective_cc)
    if postal and (expected_len is None or len(str(postal).strip()) == expected_len):
        _fill(payload, "PostalCode", e.lead_postal_code, str(postal).strip())
    _fill(payload, "FirstName", e.lead_first_name,
          split_first_name(e.contact_name or contact.get("full_name"), e.lead_last_name))
    # Phone is fill-only-blank — with ONE gated exception: `pipeline.repair_national_phone`
    # (ZPE config only) rewrites a `+0…` national number already on the Lead to its
    # international form for the lead's country. A repaired phone is never replaced by
    # a LeadFeeder number; LF only fills a blank.
    repaired = (repair_national_phone(e.lead_phone, effective_cc)
                if settings is not None and settings.repair_national_phone else None)
    if repaired:
        payload["Phone"] = repaired
    else:
        _fill(payload, "Phone", e.lead_phone, normalize_phone(dialable_phone(contact, company)))

    return payload


def write_back(client, enriched_leads: list[EnrichedLead], dry_run: bool = False, logger=None,
               settings=None) -> dict:
    """Write enrichment back to Leads. Returns {processed, payloads}; no call under dry_run.

    Leads whose payload carries only the Id (nothing enriched) are skipped — there is
    nothing to write and we never want to issue an empty update.
    """
    log = logger or get_logger("writeback")
    payloads = [lead_field_payload(e, settings) for e in enriched_leads]
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
