"""
Integration test for Excel XMLA flow.

Simulates Excel's first four calls against the server endpoint (/xmla/):
1. DISCOVER_PROPERTIES → server capabilities
2. DISCOVER_DATASOURCES → single server datasource
3. MDSCHEMA_CATALOGS → list of tenants as databases
4. BeginSession → session establishment

This test guards the Excel compatibility behavior long-term.

Requires a running gateway on localhost:8080 with the acme-demo tenant
seeded (admin@acme-demo.com / acme-demo). Skipped otherwise — the suite
is an integration smoke-test, not a unit test.
"""
import base64
import urllib.error
import urllib.request
import pytest
from defusedxml import ElementTree as ET
from xml.etree.ElementTree import Element
from httpx import AsyncClient


_BASIC_CREDS = base64.b64encode(b"admin@acme-demo.com:acme-demo").decode()


def _gateway_accepts_seeded_creds() -> bool:
    """Return True only when the gateway answers 2xx/3xx/4xx-non-401 to a
    cheap XMLA probe with the acme-demo credentials.

    Returning False for connection errors OR a 401 keeps the suite from
    blowing up on workstations that aren't running the gateway or that
    have a different seed set."""
    body = (
        b'<?xml version="1.0"?><soap:Envelope '
        b'xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
        b'<soap:Body><Discover xmlns="urn:schemas-microsoft-com:xml-analysis">'
        b'<RequestType>DISCOVER_PROPERTIES</RequestType>'
        b'<Restrictions><RestrictionList/></Restrictions>'
        b'<Properties><PropertyList/></Properties></Discover>'
        b'</soap:Body></soap:Envelope>'
    )
    req = urllib.request.Request(
        "http://localhost:8080/api/v1/xmla/",
        data=body,
        headers={
            "Content-Type": "text/xml; charset=utf-8",
            "Authorization": f"Basic {_BASIC_CREDS}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as exc:
        return exc.code != 401
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _gateway_accepts_seeded_creds(),
    reason="gateway not running or acme-demo tenant not provisioned",
)


@pytest.fixture
def auth_headers():
    """Basic auth headers for the acme-demo tenant credentials."""
    credentials = base64.b64encode(b"admin@acme-demo.com:acme-demo").decode()
    return {"Authorization": f"Basic {credentials}"}


@pytest.fixture
def xmla_headers(auth_headers):
    """Headers for XMLA requests."""
    return {
        **auth_headers,
        "Content-Type": "text/xml; charset=utf-8",
    }


def _find_element(root: Element, tag: str) -> Element | None:
    """Find first element with given tag (case-insensitive)."""
    for el in root.iter():
        if el.tag.endswith(tag) or el.tag.split("}")[-1] == tag:
            return el
    return None


def _extract_session_id(response_text: str) -> str | None:
    """Extract SessionId from SOAP Header in response."""
    try:
        root = ET.fromstring(response_text)
        for el in root.iter():
            if el.tag.endswith("Session") or el.tag.split("}")[-1] == "Session":
                return el.get("SessionId")
    except ET.ParseError:
        pass
    return None


def _extract_rows(response_text: str) -> list[dict[str, str]]:
    """Extract row dicts from a DISCOVER response."""
    rows = []
    try:
        root = ET.fromstring(response_text)
        for row_el in root.iter():
            if row_el.tag.endswith("row") or row_el.tag.split("}")[-1] == "row":
                d = {}
                for child in row_el:
                    col = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                    d[col] = (child.text or "").strip()
                rows.append(d)
    except ET.ParseError:
        pass
    return rows


@pytest.mark.asyncio
async def test_excel_step1_discover_properties(xmla_headers: dict):
    """
    Step 1: DISCOVER_PROPERTIES - Server capabilities.

    Excel checks for ProviderName, ServerName, ProviderVersion, etc.
    Missing properties cause Excel to abort the wizard.
    """
    async with AsyncClient(base_url="http://localhost:8080", timeout=30.0) as client:
        body = '''<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <Discover xmlns="urn:schemas-microsoft-com:xml-analysis">
      <RequestType>DISCOVER_PROPERTIES</RequestType>
      <Restrictions><RestrictionList/></Restrictions>
      <Properties><PropertyList/></Properties>
    </Discover>
  </soap:Body>
</soap:Envelope>'''

        response = await client.post("/api/v1/xmla/", content=body.encode("utf-8"), headers=xmla_headers)

        assert response.status_code == 200, f"Expected 200, got {response.status_code}"

        # Check for required properties
        rows = _extract_rows(response.text)
        assert len(rows) >= 3, f"Expected at least 3 properties, got {len(rows)}"

        prop_names = {r.get("PropertyName", "") for r in rows}
        assert "ServerName" in prop_names, "ServerName property missing"
        assert "ProviderVersion" in prop_names, "ProviderVersion property missing"

        # Verify SSAS-compatible values
        for row in rows:
            if row.get("PROPERTY_NAME") == "ProviderName":
                assert "Analysis Services" in row.get("VALUE", ""), \
                    f"ProviderName should include 'Analysis Services', got {row.get('VALUE')}"
            if row.get("PROPERTY_NAME") == "ProviderType":
                assert row.get("VALUE") == "MDP", \
                    f"ProviderType should be 'MDP', got {row.get('VALUE')}"


@pytest.mark.asyncio
async def test_excel_step2_discover_datasources(xmla_headers: dict):
    """
    Step 2: DISCOVER_DATASOURCES - Server datasource.

    Excel expects ONE datasource representing the server.
    Catalogs (databases) are listed separately via MDSCHEMA_CATALOGS.
    """
    async with AsyncClient(base_url="http://localhost:8080", timeout=30.0) as client:
        body = '''<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Header>
    <BeginSession xmlns="urn:schemas-microsoft-com:xml-analysis"/>
  </soap:Header>
  <soap:Body>
    <Discover xmlns="urn:schemas-microsoft-com:xml-analysis">
      <RequestType>DISCOVER_DATASOURCES</RequestType>
      <Restrictions><RestrictionList/></Restrictions>
      <Properties><PropertyList/></Properties>
    </Discover>
  </soap:Body>
</soap:Envelope>'''

        response = await client.post("/api/v1/xmla/", content=body.encode("utf-8"), headers=xmla_headers)

        assert response.status_code == 200, f"Expected 200, got {response.status_code}"

        # Session ID should be returned
        session_id = _extract_session_id(response.text)
        assert session_id is not None, "SessionId not returned in response"

        # Check datasource rows
        rows = _extract_rows(response.text)
        assert len(rows) >= 1, f"Expected at least 1 datasource, got {len(rows)}"

        ds = rows[0]
        assert "Tessallite" in ds.get("ProviderName", ""), \
            f"ProviderName should include 'Tessallite', got {ds.get('ProviderName')}"
        assert len(ds.get("DataSourceInfo", "")) > 0, \
            f"DataSourceInfo should be non-empty, got {ds.get('DataSourceInfo')!r}"
        assert ds.get("URL", "").rstrip("/").endswith("/xmla"), \
            f"URL should point to server endpoint, got {ds.get('URL')!r}"


@pytest.mark.asyncio
async def test_excel_step3_mdschema_catalogs(xmla_headers: dict):
    """
    Step 3: MDSCHEMA_CATALOGS - List databases (tenants).

    Excel expects a list of all catalogs/databases the user can access.
    Each catalog corresponds to a tenant in Tessallite.
    """
    async with AsyncClient(base_url="http://localhost:8080", timeout=30.0) as client:
        body = '''<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Header>
    <Session xmlns="urn:schemas-microsoft-com:xml-analysis" SessionId="test-session"/>
  </soap:Header>
  <soap:Body>
    <Discover xmlns="urn:schemas-microsoft-com:xml-analysis">
      <RequestType>MDSCHEMA_CATALOGS</RequestType>
      <Restrictions><RestrictionList/></Restrictions>
      <Properties><PropertyList/></Properties>
    </Discover>
  </soap:Body>
</soap:Envelope>'''

        response = await client.post("/api/v1/xmla/", content=body.encode("utf-8"), headers=xmla_headers)

        assert response.status_code == 200, f"Expected 200, got {response.status_code}"

        # Check catalog rows
        rows = _extract_rows(response.text)
        assert len(rows) >= 1, f"Expected at least 1 catalog, got {len(rows)}"

        # Each catalog should have required fields
        for row in rows:
            assert len(row.get("CATALOG_NAME", "")) > 0, \
                f"CATALOG_NAME should be non-empty, got {row.get('CATALOG_NAME')!r}"

        catalog_names = {r.get("CATALOG_NAME") for r in rows}
        assert "modely" in catalog_names, (
            f"Expected 'modely' catalog for acme-demo tenant, got {sorted(catalog_names)!r}"
        )
        assert "modely_technical" in catalog_names, (
            f"Expected 'modely_technical' variant to be emitted alongside the business view, "
            f"got {sorted(catalog_names)!r}"
        )


@pytest.mark.asyncio
async def test_excel_step4_begin_session(xmla_headers: dict):
    """
    Step 4: BeginSession - Session establishment.

    Excel establishes a session at the start and echoes the SessionId
    in all subsequent requests.
    """
    async with AsyncClient(base_url="http://localhost:8080", timeout=30.0) as client:
        body = '''<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Header>
    <BeginSession xmlns="urn:schemas-microsoft-com:xml-analysis"/>
  </soap:Header>
  <soap:Body>
    <Discover xmlns="urn:schemas-microsoft-com:xml-analysis">
      <RequestType>DISCOVER_PROPERTIES</RequestType>
      <Restrictions><RestrictionList/></Restrictions>
      <Properties><PropertyList/></Properties>
    </Discover>
  </soap:Body>
</soap:Envelope>'''

        response = await client.post("/api/v1/xmla/", content=body.encode("utf-8"), headers=xmla_headers)

        assert response.status_code == 200, f"Expected 200, got {response.status_code}"

        # Session ID should be returned and echoed
        session_id = _extract_session_id(response.text)
        assert session_id is not None and len(session_id) > 0, \
            f"SessionId should be non-empty, got {session_id!r}"

        # Verify the session is echoed in subsequent request
        echo_body = f'''<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Header>
    <Session xmlns="urn:schemas-microsoft-com:xml-analysis" SessionId="{session_id}"/>
  </soap:Header>
  <soap:Body>
    <Discover xmlns="urn:schemas-microsoft-com:xml-analysis">
      <RequestType>DISCOVER_PROPERTIES</RequestType>
      <Restrictions><RestrictionList/></Restrictions>
      <Properties><PropertyList/></Properties>
    </Discover>
  </soap:Body>
</soap:Envelope>'''

        echo_response = await client.post("/api/v1/xmla/", content=echo_body.encode("utf-8"), headers=xmla_headers)

        assert echo_response.status_code == 200, f"Echo request failed: {echo_response.status_code}"

        echo_session_id = _extract_session_id(echo_response.text)
        assert echo_session_id == session_id, \
            f"SessionId should be echoed back, expected {session_id!r}, got {echo_session_id!r}"


@pytest.mark.asyncio
async def test_excel_catalog_routing(xmla_headers: dict):
    """
    Test that Catalog property routes to correct tenant.

    When Excel selects a database (catalog), it passes it in the
    Properties. The gateway should route to the corresponding model.
    Uses the seeded ``modely`` catalog for the acme-demo tenant.
    """
    async with AsyncClient(base_url="http://localhost:8080", timeout=30.0) as client:
        body = '''<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <Discover xmlns="urn:schemas-microsoft-com:xml-analysis">
      <RequestType>MDSCHEMA_CUBES</RequestType>
      <Restrictions><RestrictionList/></Restrictions>
      <Properties>
        <PropertyList>
          <Catalog>modely</Catalog>
        </PropertyList>
      </Properties>
    </Discover>
  </soap:Body>
</soap:Envelope>'''

        response = await client.post("/api/v1/xmla/", content=body.encode("utf-8"), headers=xmla_headers)

        assert response.status_code == 200, f"Expected 200, got {response.status_code}"

        # Should return at least one cube row for modely.
        rows = _extract_rows(response.text)
        assert len(rows) >= 1, f"Expected at least 1 cube row for modely, got {len(rows)}"


@pytest.mark.asyncio
async def test_excel_bogus_catalog_returns_fault(xmla_headers: dict):
    """Bug-XMLA-002 regression. An unknown catalog on a tenant-level
    Discover must return an XMLA Fault (404 status) with a clear
    "catalog not found" message instead of silently accepting the
    connection and deferring the failure until the first MDX query.
    """
    async with AsyncClient(base_url="http://localhost:8080", timeout=30.0) as client:
        body = '''<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <Discover xmlns="urn:schemas-microsoft-com:xml-analysis">
      <RequestType>MDSCHEMA_DIMENSIONS</RequestType>
      <Restrictions><RestrictionList/></Restrictions>
      <Properties>
        <PropertyList>
          <Catalog>definitely_not_a_real_catalog</Catalog>
        </PropertyList>
      </Properties>
    </Discover>
  </soap:Body>
</soap:Envelope>'''

        response = await client.post("/api/v1/xmla/", content=body.encode("utf-8"), headers=xmla_headers)
        assert response.status_code == 404
        assert "Fault" in response.text
        assert "not found" in response.text.lower()
        assert "definitely_not_a_real_catalog" in response.text
