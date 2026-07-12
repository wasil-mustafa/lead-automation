"""SOAP Partner API `convertLead()` envelope generation + response parsing.

Anonymous Apex conversion is impossible in the org: the pipeline user lacks the
"Author Apex" permission, so `Database.convertLead` throws System.SecurityException
in restricted execute-anonymous mode (root-caused 2026-07-10). The user does hold
Convert Leads, which the SOAP Partner API honors — the converter therefore POSTs a
`convertLead` call to `{instanceUrl}/services/Soap/u/{api_version}`.

Element ORDER inside each <leadConverts> follows the Partner WSDL sequence
(accountId?, contactId?, convertedStatus, doNotCreateOpportunity, leadId,
overwriteLeadSource, ownerId?, sendNotificationEmail) — SOAP schema validation
rejects shuffled elements. Up to 100 LeadConverts per call.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

from .client import SfError, guard_sf_id

PARTNER_NS = "urn:partner.soap.sforce.com"


def build_convert_envelope(
    session_id: str, conversions: list[dict], *, converted_status: str,
    owner_id: str | None,
) -> str:
    """Build one convertLead SOAP envelope for a batch of conversions (≤100).

    Each conversion dict carries `lead_id` plus optional `account_id`/`contact_id`
    to link existing records. `doNotCreateOpportunity` is always true,
    `overwriteLeadSource`/`sendNotificationEmail` always false (same semantics as
    the retired conversion Apex).
    """
    parts: list[str] = []
    for c in conversions:
        fields: list[str] = []
        if c.get("account_id"):
            fields.append(f"<urn:accountId>{guard_sf_id(c['account_id'])}</urn:accountId>")
        if c.get("contact_id"):
            fields.append(f"<urn:contactId>{guard_sf_id(c['contact_id'])}</urn:contactId>")
        fields.append(f"<urn:convertedStatus>{escape(converted_status)}</urn:convertedStatus>")
        fields.append("<urn:doNotCreateOpportunity>true</urn:doNotCreateOpportunity>")
        fields.append(f"<urn:leadId>{guard_sf_id(c['lead_id'])}</urn:leadId>")
        fields.append("<urn:overwriteLeadSource>false</urn:overwriteLeadSource>")
        if owner_id:
            fields.append(f"<urn:ownerId>{guard_sf_id(owner_id)}</urn:ownerId>")
        fields.append("<urn:sendNotificationEmail>false</urn:sendNotificationEmail>")
        parts.append("<urn:leadConverts>" + "".join(fields) + "</urn:leadConverts>")

    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" '
        f'xmlns:urn="{PARTNER_NS}">'
        "<soapenv:Header><urn:SessionHeader>"
        f"<urn:sessionId>{escape(session_id)}</urn:sessionId>"
        "</urn:SessionHeader></soapenv:Header>"
        "<soapenv:Body><urn:convertLead>" + "".join(parts) + "</urn:convertLead></soapenv:Body>"
        "</soapenv:Envelope>"
    )


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _text(elem) -> str | None:
    """Element text, honoring xsi:nil (a nil element reads as None, not '')."""
    for k, v in elem.attrib.items():
        if _local(k) == "nil" and v in ("true", "1"):
            return None
    return elem.text


def parse_convert_response(xml_text: str) -> list[dict]:
    """Parse a convertLeadResponse into result-row dicts, in request order.

    Rows: {leadId, success, accountId?, contactId?} on success,
    {leadId (may be None — backfill from request order), success: False, errors:
    ['STATUS_CODE: message', ...]} on failure. A SOAP Fault (e.g. INVALID_SESSION_ID)
    raises SfError with the faultcode/faultstring.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise SfError(f"Unparseable SOAP response: {exc}\n{xml_text[:2000]}") from exc

    for elem in root.iter():
        if _local(elem.tag) == "Fault":
            code = msg = ""
            for child in elem.iter():
                if _local(child.tag) == "faultcode":
                    code = child.text or ""
                elif _local(child.tag) == "faultstring":
                    msg = child.text or ""
            raise SfError(f"SOAP Fault: {code} {msg}".strip())

    response = next((e for e in root.iter() if _local(e.tag) == "convertLeadResponse"), None)
    if response is None:
        raise SfError(f"No convertLeadResponse in SOAP body:\n{xml_text[:2000]}")

    rows: list[dict] = []
    for result in response:
        if _local(result.tag) != "result":
            continue
        row: dict = {"leadId": None, "success": False}
        errors: list[str] = []
        for child in result:
            name = _local(child.tag)
            if name == "leadId":
                row["leadId"] = _text(child)
            elif name == "success":
                row["success"] = (child.text or "").strip().lower() == "true"
            elif name in ("accountId", "contactId"):
                value = _text(child)
                if value:
                    row[name] = value
            elif name == "errors":
                code = msg = ""
                for e in child:
                    if _local(e.tag) == "statusCode":
                        code = e.text or ""
                    elif _local(e.tag) == "message":
                        msg = e.text or ""
                errors.append(f"{code}: {msg}" if code else msg)
        if errors:
            row["errors"] = errors
        rows.append(row)
    return rows
