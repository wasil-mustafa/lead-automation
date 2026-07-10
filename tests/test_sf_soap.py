"""SOAP Partner API convertLead: envelope generation + response parsing.

Anonymous Apex conversion is impossible in the org (the pipeline user lacks the
"Author Apex" permission — Database.convertLead throws System.SecurityException in
restricted mode, root-caused 2026-07-10). The user DOES hold Convert Leads, so the
converter drives the SOAP Partner API `convertLead()` call instead. These tests pin
the envelope's WSDL element sequence (order matters for SOAP validation), the XML
escaping, and the response/Fault parsing.
"""
import pytest

from leadauto.sf.client import SfError
from leadauto.sf.soap import build_convert_envelope, parse_convert_response

_PARTNER_NS = "urn:partner.soap.sforce.com"


# --- envelope generation ------------------------------------------------------

def test_envelope_carries_session_id_and_partner_namespace():
    env = build_convert_envelope("SESSION123", [{"lead_id": "00Q1"}],
                                 converted_status="Qualified", owner_id=None)
    assert "SESSION123" in env
    assert _PARTNER_NS in env
    assert "<urn:sessionId>SESSION123</urn:sessionId>" in env


def test_envelope_elements_follow_wsdl_sequence():
    # WSDL sequence: accountId?, contactId?, convertedStatus, doNotCreateOpportunity,
    # leadId, overwriteLeadSource, ownerId?, sendNotificationEmail. Order is enforced
    # by SOAP schema validation — a shuffled envelope is rejected org-side.
    env = build_convert_envelope(
        "S", [{"lead_id": "00Q1", "account_id": "001A", "contact_id": "003C"}],
        converted_status="Qualified", owner_id="005OWNER")
    order = [env.index(tag) for tag in (
        "<urn:accountId>001A<", "<urn:contactId>003C<",
        "<urn:convertedStatus>Qualified<", "<urn:doNotCreateOpportunity>true<",
        "<urn:leadId>00Q1<", "<urn:overwriteLeadSource>false<",
        "<urn:ownerId>005OWNER<", "<urn:sendNotificationEmail>false<")]
    assert order == sorted(order)


def test_envelope_omits_optional_elements_when_absent():
    env = build_convert_envelope("S", [{"lead_id": "00Q1"}],
                                 converted_status="Qualified", owner_id=None)
    assert "accountId" not in env
    assert "contactId" not in env
    assert "ownerId" not in env
    # fixed flags are always present
    assert "<urn:doNotCreateOpportunity>true</urn:doNotCreateOpportunity>" in env
    assert "<urn:sendNotificationEmail>false</urn:sendNotificationEmail>" in env
    assert "<urn:overwriteLeadSource>false</urn:overwriteLeadSource>" in env


def test_envelope_batches_multiple_conversions_in_order():
    env = build_convert_envelope("S", [{"lead_id": "00Q1"}, {"lead_id": "00Q2"}],
                                 converted_status="Qualified", owner_id=None)
    assert env.count("<urn:leadConverts>") == 2
    assert env.index("00Q1") < env.index("00Q2")


def test_envelope_escapes_xml_in_status():
    env = build_convert_envelope("S", [{"lead_id": "00Q1"}],
                                 converted_status="Closed <&> 'Won'", owner_id=None)
    assert "Closed &lt;&amp;&gt;" in env
    assert "<&>" not in env.split("Body", 1)[1]  # no raw markup injected into the body


def test_envelope_rejects_id_injection():
    with pytest.raises(ValueError, match="non-alphanumeric"):
        build_convert_envelope("S", [{"lead_id": "00Q1'><injected/>"}],
                               converted_status="Qualified", owner_id=None)


# --- response parsing ---------------------------------------------------------

def _response(results_xml: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
    xmlns="urn:partner.soap.sforce.com"
    xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <soapenv:Body>
    <convertLeadResponse>{results_xml}</convertLeadResponse>
  </soapenv:Body>
</soapenv:Envelope>"""


def test_parse_success_row():
    xml = _response("""
      <result>
        <accountId>001NEW</accountId>
        <contactId>003NEW</contactId>
        <leadId>00Q1</leadId>
        <opportunityId xsi:nil="true"/>
        <success>true</success>
      </result>""")
    rows = parse_convert_response(xml)
    assert rows == [{"leadId": "00Q1", "success": True,
                     "accountId": "001NEW", "contactId": "003NEW"}]


def test_parse_error_row_with_nil_lead_id():
    # A structurally rejected conversion (e.g. INVALID_ID probe) comes back with
    # success=false, an <errors> element, and a nil leadId — the caller backfills
    # the leadId from the request order.
    xml = _response("""
      <result>
        <errors>
          <message>invalid record id</message>
          <statusCode>INVALID_ID</statusCode>
        </errors>
        <leadId xsi:nil="true"/>
        <success>false</success>
      </result>""")
    rows = parse_convert_response(xml)
    assert rows == [{"leadId": None, "success": False,
                     "errors": ["INVALID_ID: invalid record id"]}]


def test_parse_preserves_result_order_for_mixed_batch():
    xml = _response("""
      <result><accountId>001A</accountId><contactId>003A</contactId>
        <leadId>00Q1</leadId><success>true</success></result>
      <result><errors><message>boom</message><statusCode>DUPLICATES_DETECTED</statusCode></errors>
        <leadId xsi:nil="true"/><success>false</success></result>
      <result><accountId>001B</accountId><contactId>003B</contactId>
        <leadId>00Q3</leadId><success>true</success></result>""")
    rows = parse_convert_response(xml)
    assert [r.get("leadId") for r in rows] == ["00Q1", None, "00Q3"]
    assert [r["success"] for r in rows] == [True, False, True]
    assert rows[1]["errors"] == ["DUPLICATES_DETECTED: boom"]


def test_parse_soap_fault_raises_sferror():
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
    xmlns:sf="urn:fault.partner.soap.sforce.com">
  <soapenv:Body>
    <soapenv:Fault>
      <faultcode>sf:INVALID_SESSION_ID</faultcode>
      <faultstring>INVALID_SESSION_ID: Invalid Session ID found in SessionHeader</faultstring>
    </soapenv:Fault>
  </soapenv:Body>
</soapenv:Envelope>"""
    with pytest.raises(SfError, match="INVALID_SESSION_ID"):
        parse_convert_response(xml)


def test_parse_unrecognized_xml_raises_sferror():
    with pytest.raises(SfError):
        parse_convert_response("<html>login page, not soap</html>")
