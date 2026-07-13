"""Map a company's industry onto the SF `Lead.Industry` ('Branche') picklist.

Two levels of kununu's own taxonomy plus a LeadFeeder fallback resolve to the org's
20-value picklist (NOT restricted — an off-list write silently pollutes reporting, so
we only ever emit one of the 20 labels or nothing):

  * `KUNUNU_TO_SF` — the kununu profile feed's `profile_industry_name_de` (44 German
    fine categories, snapshot 2026-07-13) rolled up to the 20 English buckets. Primary.
  * `LF_TO_SF` — LeadFeeder's LinkedIn-taxonomy industry names. Fallback only (the
    kununu-first path covers ~every lead), so this is seeded from observed values and
    grows from QA evidence; an unmapped value maps to nothing.

Lookups normalize (NFC + strip + casefold) on both sides so case/whitespace/normal-form
never causes a silent miss. A non-empty source value that maps to nothing logs a WARNING
so new/unknown categories surface; an empty value is silent.
"""
from __future__ import annotations

import unicodedata

from .logging import get_logger

log = get_logger("industry")

VALID_SF_INDUSTRIES = frozenset({
    "AUTOMOTIVE and VEHICLE MANUFACTURING", "BANKING and INSURANCE",
    "CONSTRUCTION and MINING", "CONSULTING, AUDITING and LAW",
    "ENERGY, WATER and ENVIRONMENT", "EDUCATION, TRAINING and SCIENCE",
    "HEALTHCARE and SOCIAL SERVICES", "HOTEL and CATERING",
    "INDUSTRY and MECHANICAL ENGINEERING", "INTERNET and INFORMATION TECHNOLOGY",
    "CONSUMER GOODS and RETAIL", "CULTURE, SPORTS and LEISURE",
    "MARKETING, PR and DESIGN", "MEDIA and PUBLISHING",
    "PUBLIC SERVICE, ASSOCIATIONS and INSTITUTIONS", "HR SERVICES",
    "PHARMACEUTICALS and MEDICAL TECHNOLOGY", "TELECOMMUNICATION",
    "TRANSPORT and LOGISTICS", "OTHER",
})

# kununu profile_industry_name_de (44) -> SF bucket. Rollup approved 2026-07-13
# (see docs/superpowers/specs/2026-07-13-industry-enrichment-design.md).
KUNUNU_TO_SF: dict[str, str] = {
    "Automobil": "AUTOMOTIVE and VEHICLE MANUFACTURING",
    "Finanz": "BANKING and INSURANCE",
    "Banken": "BANKING and INSURANCE",
    "Versicherung": "BANKING and INSURANCE",
    "Bau/Architektur": "CONSTRUCTION and MINING",
    "Handwerk": "CONSTRUCTION and MINING",
    "Beratung/Consulting": "CONSULTING, AUDITING and LAW",
    "Steuerberatung/Wirtschaftsprüfung": "CONSULTING, AUDITING and LAW",
    "Recht": "CONSULTING, AUDITING and LAW",
    "Energie": "ENERGY, WATER and ENVIRONMENT",
    "Bildung": "EDUCATION, TRAINING and SCIENCE",
    "Forschung/Entwicklung": "EDUCATION, TRAINING and SCIENCE",
    "Seminare/Messen": "EDUCATION, TRAINING and SCIENCE",
    "Gesundheit/Soziales/Pflege": "HEALTHCARE and SOCIAL SERVICES",
    "Gastronomie": "HOTEL and CATERING",
    "Hotels/Beherbergung": "HOTEL and CATERING",
    "Industrie": "INDUSTRY and MECHANICAL ENGINEERING",
    "Maschinenbau": "INDUSTRY and MECHANICAL ENGINEERING",
    "Elektro/Elektronik": "INDUSTRY and MECHANICAL ENGINEERING",
    "Chemie": "INDUSTRY and MECHANICAL ENGINEERING",
    "Textil": "INDUSTRY and MECHANICAL ENGINEERING",
    "Druck/Verpackung": "INDUSTRY and MECHANICAL ENGINEERING",
    "IT": "INTERNET and INFORMATION TECHNOLOGY",
    "Internet": "INTERNET and INFORMATION TECHNOLOGY",
    "Handel": "CONSUMER GOODS and RETAIL",
    "Nahrungsmittel/Landwirtschaft": "CONSUMER GOODS and RETAIL",
    "Kunst/Kultur": "CULTURE, SPORTS and LEISURE",
    "Sport/Beauty": "CULTURE, SPORTS and LEISURE",
    "Tourismus": "CULTURE, SPORTS and LEISURE",
    "Marketing/Werbung/PR": "MARKETING, PR and DESIGN",
    "Marktforschung": "MARKETING, PR and DESIGN",
    "Medien": "MEDIA and PUBLISHING",
    "Öffentliche Verwaltung": "PUBLIC SERVICE, ASSOCIATIONS and INSTITUTIONS",
    "Vereine": "PUBLIC SERVICE, ASSOCIATIONS and INSTITUTIONS",
    "Administration/Verwaltung": "PUBLIC SERVICE, ASSOCIATIONS and INSTITUTIONS",
    "Personalwesen & -beschaffung": "HR SERVICES",
    "Einkauf": "HR SERVICES",
    "Medizin/Pharma": "PHARMACEUTICALS and MEDICAL TECHNOLOGY",
    "Medizintechnik": "PHARMACEUTICALS and MEDICAL TECHNOLOGY",
    "Telekommunikation": "TELECOMMUNICATION",
    "Transport/Verkehr/Logistik": "TRANSPORT and LOGISTICS",
    "Sonstige Branchen": "OTHER",
    "Dienstleistung": "OTHER",
    "Immobilien": "OTHER",
}

# LeadFeeder (LinkedIn taxonomy) -> SF bucket. Seed; extend as new lf_industry
# values appear in QA. Keys are pre-normalized (lowercase) for readability.
LF_TO_SF: dict[str, str] = {
    "business consulting and services": "CONSULTING, AUDITING and LAW",
    "management consulting": "CONSULTING, AUDITING and LAW",
    "legal services": "CONSULTING, AUDITING and LAW",
    "law practice": "CONSULTING, AUDITING and LAW",
    "accounting": "CONSULTING, AUDITING and LAW",
    "software": "INTERNET and INFORMATION TECHNOLOGY",
    "software development": "INTERNET and INFORMATION TECHNOLOGY",
    "computer software": "INTERNET and INFORMATION TECHNOLOGY",
    "it services and it consulting": "INTERNET and INFORMATION TECHNOLOGY",
    "information technology & services": "INTERNET and INFORMATION TECHNOLOGY",
    "it": "INTERNET and INFORMATION TECHNOLOGY",
    "internet": "INTERNET and INFORMATION TECHNOLOGY",
    "retail": "CONSUMER GOODS and RETAIL",
}


def _norm(s: str) -> str:
    return unicodedata.normalize("NFC", s).strip().casefold()


_KUNUNU_NORM = {_norm(k): v for k, v in KUNUNU_TO_SF.items()}
_LF_NORM = {_norm(k): v for k, v in LF_TO_SF.items()}


def sf_industry_from_kununu(name: str | None) -> str | None:
    """SF bucket for a kununu `profile_industry_name_de`, or None (logs unknown)."""
    if not name or not name.strip():
        return None
    hit = _KUNUNU_NORM.get(_norm(name))
    if hit is None:
        log.warning("unmapped kununu industry %r (add to industry.KUNUNU_TO_SF)", name)
    return hit


def sf_industry_from_leadfeeder(industry_field: str | None) -> str | None:
    """SF bucket for a LeadFeeder industry string. LF joins multiple with '; ';
    the first token that maps wins. None if none map (logs the raw field)."""
    if not industry_field or not industry_field.strip():
        return None
    for token in industry_field.split(";"):
        hit = _LF_NORM.get(_norm(token))
        if hit is not None:
            return hit
    log.warning("unmapped LeadFeeder industry %r (add to industry.LF_TO_SF)", industry_field)
    return None
