"""Module 3c — match-aware bulk Lead conversion via the SOAP Partner API."""
from .converter import (
    ConversionTarget,
    convert_leads,
    enrich_contact_email2,
)

__all__ = [
    "ConversionTarget",
    "convert_leads",
    "enrich_contact_email2",
]
