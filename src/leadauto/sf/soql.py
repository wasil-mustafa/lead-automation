"""SOQL query builders for the extraction reader.

Pure string functions, no I/O. Two constraints shape them:
- `SfClient.query` rejects double quotes, so every literal here is single-quoted.
- The watermark is injected as a SOQL *datetime literal* (ISO 8601, unquoted); string
  values (Subjects, Ids) are single-quoted with apostrophes escaped.

Tasks are queried first (they carry the Description tokens), then Leads are resolved by
the collected WhoIds — `Task` is not allowed in reverse semi-joins on `Lead`.
"""
from __future__ import annotations

from typing import Sequence

# Selected verbatim from the approved plan / verified org fields.
_TASK_FIELDS = "Id, WhoId, Who.Name, Subject, Description, CreatedDate"
_LEAD_FIELDS = (
    "Id, FirstName, LastName, Company, Email, Website, Status, IsConverted, LeadSource, "
    "HasOptedOutOfEmail, Phone, Street, City, PostalCode, Country, CountryCode, "
    "KN_DBID__c, KN_NoOwnDBID__c, KN_Employee__c, KN_Language__c, "
    "KN_KununuProfileName__c, KN_KununuUserUUID__c, KN_KununuLinkProfile__c"
)


def _quote(value: str) -> str:
    """Single-quoted SOQL string literal with backslash-escaped quotes."""
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def _in_list(values: Sequence[str]) -> str:
    return ", ".join(_quote(v) for v in values)


def tasks_since(watermark: str, subjects: Sequence[str],
                statuses: Sequence[str] = (), limit: int = 0) -> str:
    """In-scope Tasks created at/after `watermark`, oldest first.

    `watermark` is an unquoted SOQL datetime literal (e.g. 2026-05-17T00:00:00Z).
    When `statuses` is given, only Tasks in those statuses are returned — the freshness
    gate (open/unhandled). It is an explicit allowlist, NOT `IsClosed = false`, because
    the org's picklist flags 'Abgeschlossen' (Completed) as not-closed; an empty list
    disables the filter.
    """
    if not subjects:
        raise ValueError("tasks_since requires at least one subject")
    soql = (
        f"SELECT {_TASK_FIELDS} FROM Task "
        f"WHERE CreatedDate >= {watermark} "
        f"AND Subject IN ({_in_list(subjects)}) "
    )
    if statuses:
        soql += f"AND Status IN ({_in_list(statuses)}) "
    soql += "ORDER BY CreatedDate ASC"
    if limit and limit > 0:
        soql += f" LIMIT {limit}"
    return soql


def leads_by_ids(ids: Sequence[str], limit: int = 0) -> str:
    """Unconverted, queue-owned Leads for the given Ids.

    Queue-owned only (scope decision 2026-07-03): pool-user-owned leads
    (Rohdaten/Clean Pool) carry contact-attached tasks — out of scope for now.
    A Lead's owning Group can only be a Queue, so Owner.Type = 'Queue' is exact
    (verified through the Bulk-API route against org data).
    """
    if not ids:
        raise ValueError("leads_by_ids requires at least one Id")
    soql = (
        f"SELECT {_LEAD_FIELDS} FROM Lead "
        f"WHERE Id IN ({_in_list(ids)}) AND IsConverted = false "
        "AND Owner.Type = 'Queue'"
    )
    if limit and limit > 0:
        soql += f" LIMIT {limit}"
    return soql


# --- match lookups (Module 3a) ----------------------------------------------
# Existing Accounts/Contacts are pulled in one batched query each (no N+1). Exact
# identifier fields use `IN (...)`; URL/host fields use `LIKE` because the org stores
# full URLs while the matcher compares on normalized host/slug substrings.

_ACCOUNT_MATCH_FIELDS = (
    "Id, Name, Website, KN_KununuProfileUuid__c, KN_KununuLinkProfile__c, "
    "KN_VATNumber__c, KN_DBID__c, KN_DBLegalCompanyName__c, "
    "BillingPostalCode, BillingCity"
)
_CONTACT_MATCH_FIELDS = (
    "Id, AccountId, Email, KN_KununuUserUUID__c, KN_LinkedInProfile__c, "
    "FirstName, LastName, Name"
)


def _like_escape(needle: str) -> str:
    """Strip characters unsafe inside a single-quoted SOQL LIKE pattern.

    Needles here are already-normalized hosts/slugs (lowercase, no apostrophes); we
    additionally drop SOQL/LIKE metacharacters so the literal stays a plain substring.
    """
    for ch in ("\\", "'", "%", "_"):
        needle = needle.replace(ch, "")
    return needle


def _like_any(field: str, needles: Sequence[str]) -> str | None:
    clauses = [f"{field} LIKE '%{_like_escape(n)}%'" for n in needles if n]
    if not clauses:
        return None
    return "(" + " OR ".join(clauses) + ")"


def accounts_by_signals(
    *,
    profile_uuids: Sequence[str] = (),
    kununu_urls: Sequence[str] = (),
    vats: Sequence[str] = (),
    dbids: Sequence[str] = (),
    hosts: Sequence[str] = (),
    names: Sequence[str] = (),
) -> str | None:
    """Accounts matching ANY collected signal; None when no signals were collected."""
    clauses: list[str] = []
    if profile_uuids:
        clauses.append(f"KN_KununuProfileUuid__c IN ({_in_list(profile_uuids)})")
    if vats:
        clauses.append(f"KN_VATNumber__c IN ({_in_list(vats)})")
    if dbids:
        clauses.append(f"KN_DBID__c IN ({_in_list(dbids)})")
    if names:
        clauses.append(f"Name IN ({_in_list(names)})")
    url_like = _like_any("KN_KununuLinkProfile__c", kununu_urls)
    if url_like:
        clauses.append(url_like)
    host_like = _like_any("Website", hosts)
    if host_like:
        clauses.append(host_like)
    if not clauses:
        return None
    return f"SELECT {_ACCOUNT_MATCH_FIELDS} FROM Account WHERE " + " OR ".join(clauses)


def contacts_by_signals(
    *,
    user_uuids: Sequence[str] = (),
    emails: Sequence[str] = (),
    linkedins: Sequence[str] = (),
    account_ids: Sequence[str] = (),
) -> str | None:
    """Contacts matching ANY direct signal or sitting in a candidate Account; None if empty."""
    clauses: list[str] = []
    if user_uuids:
        clauses.append(f"KN_KununuUserUUID__c IN ({_in_list(user_uuids)})")
    if emails:
        clauses.append(f"Email IN ({_in_list(emails)})")
    if linkedins:
        clauses.append(f"KN_LinkedInProfile__c IN ({_in_list(linkedins)})")
    if account_ids:
        clauses.append(f"AccountId IN ({_in_list(account_ids)})")
    if not clauses:
        return None
    return f"SELECT {_CONTACT_MATCH_FIELDS} FROM Contact WHERE " + " OR ".join(clauses)


# --- shadow comparison (score dry-run predictions vs human conversions) ------

_LEAD_SHADOW_FIELDS = (
    "Id, IsConverted, ConvertedDate, ConvertedAccountId, ConvertedContactId, "
    "KN_Employee__c, KN_DBRevenueinMillions__c, KN_DBISO2CountryCode__c, "
    "KN_DBPostalCode__c, KN_DBCity__c"
)
_ACCOUNT_SHADOW_FIELDS = "Id, Name, CreatedDate, Website, BillingPostalCode, BillingCity"
_CONTACT_SHADOW_FIELDS = "Id, AccountId, Email, Phone"


def leads_shadow_status(ids: Sequence[str]) -> str:
    """Conversion status + current KN_* fields for predicted leads."""
    if not ids:
        raise ValueError("leads_shadow_status requires at least one Id")
    return f"SELECT {_LEAD_SHADOW_FIELDS} FROM Lead WHERE Id IN ({_in_list(ids)})"


def accounts_by_ids(ids: Sequence[str]) -> str:
    if not ids:
        raise ValueError("accounts_by_ids requires at least one Id")
    return f"SELECT {_ACCOUNT_SHADOW_FIELDS} FROM Account WHERE Id IN ({_in_list(ids)})"


def contacts_by_ids(ids: Sequence[str]) -> str:
    if not ids:
        raise ValueError("contacts_by_ids requires at least one Id")
    return f"SELECT {_CONTACT_SHADOW_FIELDS} FROM Contact WHERE Id IN ({_in_list(ids)})"
