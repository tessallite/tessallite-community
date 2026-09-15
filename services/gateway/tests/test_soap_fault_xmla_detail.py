"""The Excel-visible half of a fault (owner's KPI review, 2026-09-04).

MSOLAP shows ``detail/Error/@Description`` from the XMLA fault namespace; a
bare SOAP faultstring leaves Excel with its generic "The query did not run".
Every gateway fault must therefore carry both, with the same escaped text.
"""

from defusedxml import ElementTree as ET

from src.dax import xmla_server

_SOAP = "{http://schemas.xmlsoap.org/soap/envelope/}"



def test_soap_fault_carries_the_xmla_error_detail_excel_displays() -> None:
    message = 'KPI status member \'[Measures].[Settlement Value Status]\' was requested with a dimension breakdown & refused <by design>'
    response = xmla_server._soap_fault(message, "Client")
    root = ET.fromstring(response.body)
    fault = root.find(f"{_SOAP}Body/{_SOAP}Fault")
    assert fault is not None
    # SSAS shape: faultcode XMLAnalysisError.<hex>, children in the SOAP
    # default namespace, Error carrying the same Description.
    assert fault.findtext(f"{_SOAP}faultcode") == "XMLAnalysisError.0xc10e0002"
    assert fault.findtext(f"{_SOAP}faultstring") == message
    error = fault.find(f"{_SOAP}detail/{_SOAP}Error")
    assert error is not None, "Excel reads the message from detail/Error"
    assert error.get("Description") == message
    assert error.get("ErrorCode") == "3238985730"
    assert error.get("Source") == "Tessallite XMLA Gateway"
    server = ET.fromstring(xmla_server._soap_fault("boom", "Server").body)
    assert server.find(f".//{_SOAP}faultcode").text == "XMLAnalysisError.0xc1010000"
