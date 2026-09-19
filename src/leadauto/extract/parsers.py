"""Subject-routed parsers: each Task type yields a normalized ExtractedLead.

Ports the Power Query `Extract` logic (find label -> take to next newline -> trim)
plus the kununu-URL scan and slug->region mapping, validated against real org data.
"""
from __future__ import annotations

import re
from datetime import date

from ..models import ExtractedLead
from ..textnorm import is_true

# kununu URL country slug -> (region label, ISO2)
_REGION_BY_SLUG = {
    "de": ("Germany", "DE"),
    "at": ("Austria", "AT"),
    "ch": ("Switzerland", "CH"),
}


def _extract(text: str, label: str) -> str | None:
    """Value after `label`, up to the next line break, trimmed. None if absent/empty."""
    idx = text.find(label)
    if idx == -1:
        return None
    rest = text[idx + len(label):]
    end = len(rest)
    for nl in ("\r\n", "\n", "\r"):
        p = rest.find(nl)
        if p != -1:
            end = min(end, p)
    return rest[:end].strip() or None


def _kununu_url(text: str) -> str | None:
    """First https://www.kununu.com... token, up to the next whitespace."""
    m = re.search(r"https://www\.kununu\.com\S*", text)
    return m.group(0) if m else None


def _region_from_url(url: str | None) -> tuple[str | None, str | None]:
    if not url:
        return None, None
    after = url.split("kununu.com/", 1)
    if len(after) < 2:
        return None, None
    slug = after[1].split("/", 1)[0].lower()
    return _REGION_BY_SLUG.get(slug, (None, None))


def _parse_de_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        day, month, year = value.split(".")
        return date(int(year), int(month), int(day))
    except (ValueError, AttributeError):
        return None


def _clean_email(value: str | None) -> str | None:
    """Drop kununu placeholder/system addresses (e.g. user_123@kununu.com)."""
    if not value:
        return None
    value = value.strip()
    return None if "@kununu" in value.lower() else value


def _base(task: dict, lead: dict, task_type: str) -> ExtractedLead:
    return ExtractedLead(
        lead_id=lead.get("Id") or task.get("WhoId") or "",
        task_id=task.get("Id", ""),
        task_type=task_type,
        subject=task.get("Subject", ""),
        lead_company=lead.get("Company"),
        lead_email=lead.get("Email"),
        lead_first_name=lead.get("FirstName"),
        lead_last_name=lead.get("LastName"),
        lead_website=lead.get("Website"),
        lead_street=lead.get("Street"),
        lead_city=lead.get("City"),
        lead_postal_code=lead.get("PostalCode"),
        lead_country_code=lead.get("CountryCode"),
        lead_country=lead.get("Country"),
        lead_phone=lead.get("Phone"),
        lead_title=lead.get("Title"),
        lead_industry=lead.get("Industry"),
        lead_source=lead.get("LeadSource"),
        lead_opted_out_email=is_true(lead.get("HasOptedOutOfEmail")),
        lead_dbid=lead.get("KN_DBID__c"),
        lead_no_own_dbid=is_true(lead.get("KN_NoOwnDBID__c")),
        lead_employee_category=lead.get("KN_Employee__c"),
        lead_language=lead.get("KN_Language__c"),
    )


# --- per-type parsers --------------------------------------------------------

def parse_claimer(task: dict, lead: dict) -> ExtractedLead:
    desc = task.get("Description") or ""
    e = _base(task, lead, "claimer")
    e.company_name = _extract(desc, "- Profile-Name: ")
    e.profile_type = _extract(desc, "- Profil Type: ")
    e.company_size = _extract(desc, "- Unternehmensgröße: ")
    e.contact_email = _clean_email(_extract(desc, "- Email: "))
    e.contact_name = _extract(desc, "- User Name: ")
    e.kununu_user_uuid = _extract(desc, "- User-ID: ")
    e.kununu_profile_uuid = _extract(desc, "- Profil-ID: ")
    e.claimed_at = _parse_de_date(_extract(desc, "- Profil beansprucht am: "))
    e.kununu_url = _kununu_url(desc)
    e.region, e.country_code = _region_from_url(e.kununu_url)
    return e


def parse_ebp_trial(task: dict, lead: dict) -> ExtractedLead:
    desc = task.get("Description") or ""
    e = _base(task, lead, "ebp_trial")
    e.kununu_url = _kununu_url(desc)
    e.region, e.country_code = _region_from_url(e.kununu_url)
    e.kununu_user_uuid = _extract(desc, "- User-ID: ")
    e.kununu_profile_uuid = _extract(desc, "- Profil-ID: ")
    e.contact_email = _clean_email(_extract(desc, "Email: "))
    # company name: between "für das Profil: " and the kununu URL
    m = re.search(r"für das Profil:\s*(.+?)\s+https://www\.kununu\.com", desc)
    if m:
        e.company_name = m.group(1).strip()
    # contact name: between "Der Benutzer " and " nutzt"
    m = re.search(r"Der Benutzer\s+(.*?)\s+nutzt", desc)
    if m and m.group(1).strip():
        e.contact_name = m.group(1).strip()
    return e


def _event_field(desc: str, label: str) -> str | None:
    """Value after `label` up to the next ';' in a badge-scan Description, trimmed."""
    idx = desc.find(label)
    if idx == -1:
        return None
    rest = desc[idx + len(label):]
    end = rest.find(";")
    value = (rest if end == -1 else rest[:end]).strip()
    return value or None


# "Gesundheitscampus-Süd 29, 44801 Bochum, DE" -> street / postal / city / ISO2.
_EVENT_ADDRESS_RE = re.compile(
    r"^(?P<street>.*?),\s*(?P<postal>[A-Z]{0,2}-?\d{4,5})\s+(?P<city>.+?),\s*(?P<cc>[A-Z]{2})$"
)


def parse_event(task: dict, lead: dict) -> ExtractedLead:
    """Event participation (badge scan) — added 2026-09-18 for the ZPE 2026 leads.

    The Description is ONE line of `;`-separated fields written by the badge scanner
    (`ZPE 2026; Review: 3; Title: ; Position: HR Consultant; Company size: 51-300
    employees; Industry: Consulting; Address: <street>, <postal> <city>, <CC>; Note: …`).
    It carries NO kununu URL and NO email, so the kununu URL — the one Task-side field
    the write-back needs — is read off the Lead's `KN_KununuLinkProfile__c` instead
    (set by hand from the exact-match workbook before the run; a URL in the Description
    is honoured first if one ever appears). Consequence: an event run must set
    `pipeline.include_enriched_leads: true`, because that same field is the reader's
    "already processed" marker. `contact_email` stays None (nothing to overwrite the
    Lead email with). Badge Position and address land in the PARSED fields
    (`position`, `street`, `postal_code`, `city`) — never in the `lead_*` fields, which
    the write-back reads as "what the Lead already has" (a parsed value copied there
    would look pre-filled and silently never be written). `writeback.lead_field_payload`
    fills a blank Title/address from these before falling back to LeadFeeder.
    """
    desc = task.get("Description") or ""
    e = _base(task, lead, "event")
    e.kununu_url = _kununu_url(desc) or (lead.get("KN_KununuLinkProfile__c") or "").strip() or None
    e.region, e.country_code = _region_from_url(e.kununu_url)
    e.position = _event_field(desc, "Position:")
    if e.position and e.position.lower() == "other":
        e.position = None
    e.company_size = _event_field(desc, "Company size:")
    e.notes = _event_field(desc, "Note:")
    address = _event_field(desc, "Address:")
    m = _EVENT_ADDRESS_RE.match(address) if address else None
    if m:
        e.street, e.postal_code, e.city = m.group("street").strip(), m.group("postal"), m.group("city").strip()
        e.address_country_code = m.group("cc")   # the badge's country, distinct from the kununu-URL slug
    return e


def parse_content(task: dict, lead: dict) -> ExtractedLead:  # phase 3
    raise NotImplementedError("content parser is not enabled yet (phase 3)")


PARSERS = {
    "claimer": parse_claimer,
    "ebp_trial": parse_ebp_trial,
    "event": parse_event,
    "content": parse_content,
}


def extract_lead(task: dict, lead: dict, settings) -> ExtractedLead | None:
    """Route a Task to its parser by Subject. Returns None if the subject is out of scope."""
    key = settings.parser_key_for(task.get("Subject", ""))
    if key is None or key not in settings.enabled_task_types:
        return None
    return PARSERS[key](task, lead)
