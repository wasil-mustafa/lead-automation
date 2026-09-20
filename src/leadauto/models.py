"""The records that flow through the pipeline, in one dependency-free module.

    Task + Lead  --parsers-->  ExtractedLead  --enrich-->  EnrichedLead  --match-->  MatchResult

They live here, not next to their producers, so a consumer of the TYPE (write-back, the
QA workbook, the rule engine) never has to import the module that PRODUCES it. Before
2026-09-19 `EnrichedLead` sat inside the LeadFeeder orchestrator, which forced the text
normalisers out into their own module, three lazy imports inside function bodies, and a
re-export shim in `match.py` — all to dodge the resulting import cycles.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


# --- extraction ------------------------------------------------------------------

@dataclass
class ExtractedLead:
    """Normalized record produced by the extraction parsers.

    Every parser fills whatever signals its task type yields; downstream matching uses
    the strongest signal present. Lead-native fields (lead_*) always come from the Lead
    record; the rest are parsed from the Task Description.
    """
    # source identifiers
    lead_id: str
    task_id: str
    task_type: str          # parser key: claimer | ebp_trial | event
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
    lead_title: str | None = None
    lead_industry: str | None = None
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
    address_country_code: str | None = None   # ISO2 of the PARSED address (event badge); not the kununu-URL country

    # parsed contact signals
    kununu_user_uuid: str | None = None
    contact_email: str | None = None
    contact_name: str | None = None
    position: str | None = None

    # content/marketing signals
    asset_name: str | None = None
    channel: str | None = None
    notes: str | None = None

    @property
    def best_company(self) -> str | None:
        """Company name to use for matching/LeadFeeder: parsed name first, else the Lead's."""
        return self.company_name or self.lead_company


# --- LeadFeeder enrichment -------------------------------------------------------

# Why a matched lead has no LF contact although LF was never asked for one. The QA
# `NoContact` sheet shows these verbatim; "not found in LeadFeeder" is reserved for the
# case where the search actually ran and held no such person.
CONTACT_SKIP_NOT_NEEDED = "lead already has email and phone"     # config: contact_lookup_only_when_needed
CONTACT_SKIP_NO_NAME = "no contact name to search"
CONTACT_SKIP_SEARCH_FAILED = "LeadFeeder contact search failed (transport error)"


@dataclass
class EnrichedLead:
    """An ExtractedLead plus whatever LeadFeeder returned. Feeds match/write-back."""
    extracted: ExtractedLead
    matched: bool = False
    lf_rejected: str | None = None  # post-retrieve sanity gate's rejection reason, or None
    lf_company_id: str | None = None
    matched_name: str | None = None
    match_score: float | None = None
    feed_country_code: str | None = None  # kununu feed profile_country_code (sanity-gate evidence)
    feed_industry: str | None = None      # kununu feed profile_industry_name_de (write-back source)
    kununu_score: float | None = None     # kununu feed profile_score (live-scope gate input)
    name_agreement: int | None = None  # gate's 0-100 name/domain agreement (see client._best_match)
    agreed_via: str | None = None  # which signal vouched: company_name / alt_name:<n> / email_domain
    alt_names: list[str] = field(default_factory=list)  # assigned-company names from the kununu feed
    match_pass: str = ""  # "primary" | "wide" (second-chance match); "" = never matched
    company: dict = field(default_factory=dict)   # flatten_company output (or {})
    contact_found: bool = False
    contact: dict = field(default_factory=dict)   # flattened details + contact_id/full_name
    # Why the contact cascade was NOT run for a matched lead (QA NoContact `reason`);
    # one of the CONTACT_SKIP_* reasons, or None when LF was asked (found or not).
    contact_skipped: str | None = None


# --- Dublettencheck result ---------------------------------------------------------

@dataclass
class AccountMatch:
    account_id: str
    reason: str   # the rule that fired (rules decide; there is no score on the account side)


@dataclass
class ContactMatch:
    contact_id: str
    score: float  # candidate ranking only (uuid > email-in-account > email/linkedin)
    reason: str


@dataclass
class MatchResult:
    enriched: EnrichedLead
    account: AccountMatch | None
    contact: ContactMatch | None
    decision: str  # "link" | "review" | "create_new" | "skip"
    rule: str = ""
    evidence: list[str] = field(default_factory=list)
