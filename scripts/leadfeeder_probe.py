"""Read-only LeadFeeder probe — confirms the live API shape and that throttle/retry work.

Usage (PowerShell):
    $env:PYTHONPATH = "src"; $env:LEADFEEDER_API_KEY = "<key>"
    python scripts/leadfeeder_probe.py "kununu" "DE"

Spends 0 credits for match + search; retrieving one contact may cost 1 credit
(free if that contact was accessed in the last 12 months). No Salesforce calls.
"""
from __future__ import annotations

import sys

from leadauto.config import Settings
from leadauto.leadfeeder.client import LeadfeederClient, flatten_contact_details


def main() -> int:
    company = sys.argv[1] if len(sys.argv) > 1 else "kununu"
    country_code = sys.argv[2] if len(sys.argv) > 2 else "DE"
    settings = Settings.load()
    client = LeadfeederClient(
        api_key=settings.leadfeeder_api_key,
        base_url=settings.lf_base_url,
        min_match_score=settings.lf_min_match_score,
        retrieve_batch=settings.lf_retrieve_batch,
        match_batch=settings.lf_match_batch,
        max_requests_per_min=settings.lf_max_requests_per_min,
        max_retries=settings.lf_max_retries,
    )

    print(f"Account: {client.get_account_id()}")
    results = client.match_companies([{"company_name": company, "country_code": country_code}])
    match = results[0] if results else None
    print(f"Match: {match}")
    if not match:
        print("No company match; stopping (search/retrieve need a company id).")
        return 0

    hit = client.search_contact(company, match["company_id"])
    print(f"Search first hit: {hit}")
    if not hit:
        print("No contact found; search confirmed (free). Done.")
        return 0

    attrs = client.retrieve_contacts([hit["contact_id"]])
    print(f"retrieve_contacts keys: {list(attrs)}")
    print(f"Flattened: {flatten_contact_details(attrs.get(hit['contact_id'], {}))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
