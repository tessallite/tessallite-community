"""Bug-9887: unrestricted member metadata stays structural and bounded."""
from defusedxml import ElementTree as ET
import pytest

from src.dax import xmla_server


def _method(restrictions: str = ""):
    root = ET.fromstring(
        f'''<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
          <soap:Body>
            <Discover xmlns="urn:schemas-microsoft-com:xml-analysis">
              <RequestType>MDSCHEMA_MEMBERS</RequestType>
              <Restrictions><RestrictionList>{restrictions}</RestrictionList></Restrictions>
              <Properties><PropertyList><Catalog>modely</Catalog></PropertyList></Properties>
            </Discover>
          </soap:Body>
        </soap:Envelope>'''
    )
    method = xmla_server._find_method(root)
    assert method is not None
    return method


def _install_metadata(monkeypatch):
    async def resolve(*_args, **_kwargs):
        return "model-1", "project-1", None, None

    async def measures(*_args, **_kwargs):
        return [{"name": "amount", "default_agg": "sum"}]

    async def dimensions(*_args, **_kwargs):
        return [
            {"name": "customer_id", "source": "column"},
            {"name": "country", "source": "column"},
        ]

    async def hierarchies(*_args, **_kwargs):
        return []

    async def models(*_args, **_kwargs):
        return []

    monkeypatch.setattr(xmla_server, "_resolve_model_id", resolve)
    monkeypatch.setattr(xmla_server, "get_model_measures", measures)
    monkeypatch.setattr(xmla_server, "get_model_dimensions", dimensions)
    monkeypatch.setattr(xmla_server, "get_model_hierarchies", hierarchies)
    monkeypatch.setattr(xmla_server, "list_all_models_for_tenant", models)


@pytest.mark.asyncio
async def test_unrestricted_members_do_not_fetch_or_emit_leaf_values(monkeypatch):
    _install_metadata(monkeypatch)

    async def must_not_fetch(*_args, **_kwargs):
        raise AssertionError("unrestricted metadata must not enumerate members")

    monkeypatch.setattr(xmla_server, "_load_discover_member_data", must_not_fetch)
    response = await xmla_server._handle_discover(
        _method(),
        tenant_slug="acme-demo",
        jwt_token="token",
        endpoint_url="http://127.0.0.1:8080/api/v1/xmla/",
        session_id="sid",
    )

    body = response.body.decode("utf-8")
    assert response.status_code == 200
    assert "[Dimensions].[customer_id].[All]" in body
    assert "[Dimensions].[country].[All]" in body
    assert "customer-123" not in body


@pytest.mark.asyncio
async def test_hierarchy_restriction_fetches_and_emits_targeted_members(monkeypatch):
    _install_metadata(monkeypatch)
    calls = []

    async def targeted(*_args, **kwargs):
        calls.append(kwargs["restrictions"])
        return {
            "customer_id": {
                "levels": ["customer_id"],
                "members": [{"name": "customer-123", "key": "customer-123"}],
            }
        }

    monkeypatch.setattr(xmla_server, "_load_discover_member_data", targeted)
    response = await xmla_server._handle_discover(
        _method(
            "<HIERARCHY_UNIQUE_NAME>"
            "[Dimensions].[customer_id]"
            "</HIERARCHY_UNIQUE_NAME>"
        ),
        tenant_slug="acme-demo",
        jwt_token="token",
        endpoint_url="http://127.0.0.1:8080/api/v1/xmla/",
        session_id="sid",
    )

    body = response.body.decode("utf-8")
    assert response.status_code == 200
    assert calls
    assert "customer-123" in body


@pytest.mark.parametrize(
    "group_name",
    ["[Dimensions]", "[Hierarchies]", "[Time]"],
)
def test_group_dimension_restriction_is_not_treated_as_one_field(group_name):
    # A group stays structural even when a persona leaves only one field in it.
    # Its restriction identifies the containing folder, not that remaining
    # field, so it must never trigger source member enumeration.
    dimensions = [{"name": "country", "source": "column"}]
    assert not xmla_server._member_discover_targets_one_field(
        {"DIMENSION_UNIQUE_NAME": [group_name]}, dimensions
    )


def test_ungrouped_dimension_named_like_a_group_can_target_members(monkeypatch):
    monkeypatch.setenv("TESSALLITE_XMLA_FIELD_LIST_GROUPING", "false")
    dimensions = [{"name": "Time", "source": "column"}]

    assert xmla_server._member_discover_targets_one_field(
        {"DIMENSION_UNIQUE_NAME": ["[Time]"]}, dimensions
    )
