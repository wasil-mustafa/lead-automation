"""Module 3c — match-aware bulk Lead conversion via anonymous Apex."""
from .converter import (
    SENTINEL,
    ConversionTarget,
    build_convert_apex,
    convert_leads,
    enrich_contact_email2,
    parse_results,
)

__all__ = [
    "SENTINEL",
    "ConversionTarget",
    "build_convert_apex",
    "convert_leads",
    "enrich_contact_email2",
    "parse_results",
]
