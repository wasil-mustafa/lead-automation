"""Normalized record produced by the extraction parsers.

Every parser fills whatever signals its task type yields; downstream matching uses
the strongest signal present. Lead-native fields (lead_*) always come from the Lead
record; the rest are parsed from the Task Description.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import date


@dataclass
class ExtractedLead:
    # source identifiers
    lead_id: str
    task_id: str
    task_type: str          # parser key: claimer | ebp_trial | event | content
    subject: str = ""

    # lead-native (from the Lead record)
    lead_company: str | None = None
    lead_email: str | None = None
    lead_first_name: str | None = None
    lead_last_name: str | None = None
    lead_website: str | None = None
    lead_street: str | None = None
    lead_city: str | None = None
    lead_postal_code: str | None = None
    lead_country_code: str | None = None
    lead_country: str | None = None
    lead_phone: str | None = None
    lead_source: str | None = None
    lead_opted_out_email: bool = False
    lead_dbid: str | None = None
    lead_no_own_dbid: bool = False
    lead_employee_category: str | None = None
    lead_language: str | None = None

    # parsed company signals
    company_name: str | None = None
    kununu_url: str | None = None
    kununu_profile_uuid: str | None = None
    region: str | None = None
    country_code: str | None = None
    profile_type: str | None = None
    claimed_at: date | None = None
    company_size: str | None = None
    street: str | None = None
    postal_code: str | None = None
    city: str | None = None
    vat_number: str | None = None

    # parsed contact signals
    kununu_user_uuid: str | None = None
    contact_email: str | None = None
    contact_name: str | None = None
    position: str | None = None

    # content/marketing signals
    asset_name: str | None = None
    channel: str | None = None
    notes: str | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        if isinstance(d.get("claimed_at"), date):
            d["claimed_at"] = d["claimed_at"].isoformat()
        return d

    @property
    def best_company(self) -> str | None:
        """Company name to use for matching/LeadFeeder: parsed name first, else the Lead's."""
        return self.company_name or self.lead_company
