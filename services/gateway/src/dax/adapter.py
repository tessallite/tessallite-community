import logging

logger = logging.getLogger(__name__)

class XmlaAdapter:
    """
    Expert Directive Compliance Adapter.
    Normalizes inbound/outbound XML to match strict MSOLAP expectations.
    """

    @staticmethod
    def normalize_inbound(xml_body: bytes) -> bytes:
        """
        Disabled - case-insensitive XMLA parsing is handled by _local_name().
        Pass through unchanged to avoid creating malformed XML.
        """
        return xml_body

    @staticmethod
    def normalize_outbound(xml_response: str) -> str:
        """
        Final verification of outbound XML compliance.
        """
        # Ensure we don't have malformed xmlns:urn attributes
        xml_response = xml_response.replace('xmlns:urn:schemas-microsoft-com:xml-analysis:rowset"', 'xmlns="urn:schemas-microsoft-com:xml-analysis:rowset"')
        
        return xml_response
