"""Bug-8327: calc-member re-query partition pins must emit SOURCE identifiers.

The re-query specs' partition pins (``ReQuerySpec.dim_col`` / ``partition_dims``
and ``DenomReQuerySpec.partition_dims``) are built by the planners from the
POST-alias axis column names. But the re-query SQL is bound by the query-router as
canonical postgres, whose binder resolves SOURCE dimension/level names, not MDX
hierarchy-alias names. On an ALIASED pivot the untranslated alias identifiers fail
to bind and fault the whole Execute re-query. Bug-8283 fixed only the Top-N
SURVIVOR predicate; this covers the partition-pin path with the SAME alias->source
translation, keeping the VALUES (already read from post-alias rows) untouched.
"""
import pytest

from src.dax.mdx_calc_members import (
    ReQuerySpec,
    DenomReQuerySpec,
    build_requery_sql,
    build_denominator_requery_sql,
)
from src.dax.xmla_server import _translate_requery_partition_identifiers


def _agg_spec():
    return ReQuerySpec(
        calc_name="Grp",
        measure_name="Sales",
        agg="avg",
        dim_col="ProductByRegion",      # alias identifier
        members=["P1", "P2"],
        model_slug="m",
        partition_dims=["RegionAlias"],  # alias identifier
        partition_values=["East"],
    )


def _denom_spec():
    return DenomReQuerySpec(
        calc_name="Grp",
        measure_name="Sales",
        agg="avg",
        model_slug="m",
        partition_key=("East",),
        partition_dims=["RegionAlias"],  # alias identifier
        partition_values=["East"],
    )


def test_translate_rewrites_identifiers_not_values():
    agg, denom = _agg_spec(), _denom_spec()
    _translate_requery_partition_identifiers(
        agg_specs=[agg], denom_specs=[denom],
        axis_aliases={"ProductByRegion": "Product", "RegionAlias": "Region"},
    )
    # Identifiers translated alias -> source.
    assert agg.dim_col == "Product"
    assert agg.partition_dims == ["Region"]
    assert denom.partition_dims == ["Region"]
    # Values are unchanged (they were read from the post-alias rows already).
    assert agg.partition_values == ["East"]
    assert denom.partition_values == ["East"]


def test_translated_sql_uses_source_columns():
    agg, denom = _agg_spec(), _denom_spec()
    _translate_requery_partition_identifiers(
        agg_specs=[agg], denom_specs=[denom],
        axis_aliases={"ProductByRegion": "Product", "RegionAlias": "Region"},
    )
    agg_sql = build_requery_sql(agg)
    denom_sql = build_denominator_requery_sql(denom)
    # The bindable source columns appear; the non-bindable alias names do not.
    assert '"Product"' in agg_sql and '"Region"' in agg_sql
    assert "ProductByRegion" not in agg_sql and "RegionAlias" not in agg_sql
    assert '"Region"' in denom_sql and "RegionAlias" not in denom_sql


def test_no_aliases_is_noop():
    agg, denom = _agg_spec(), _denom_spec()
    _translate_requery_partition_identifiers(
        agg_specs=[agg], denom_specs=[denom], axis_aliases={},
    )
    # A flat (unaliased) pivot must leave the specs exactly as built.
    assert agg.dim_col == "ProductByRegion"
    assert agg.partition_dims == ["RegionAlias"]
    assert denom.partition_dims == ["RegionAlias"]


def test_unmapped_identifier_maps_to_itself():
    # A dim that is NOT in the alias map (a non-aliased axis dim on a
    # partially-aliased pivot) must be left untouched, not dropped.
    agg = ReQuerySpec(
        calc_name="Grp", measure_name="Sales", agg="avg",
        dim_col="Product", members=["P1"], model_slug="m",
        partition_dims=["Channel"], partition_values=["Web"],
    )
    _translate_requery_partition_identifiers(
        agg_specs=[agg], denom_specs=[], axis_aliases={"Foo": "Bar"},
    )
    assert agg.dim_col == "Product"
    assert agg.partition_dims == ["Channel"]


# --- Integration: dim_col source-translation on the REAL _handle_execute path ---

def _make_fakes(top_n_members):
    async def fake_resolve_model_id(catalog, tenant_slug, jwt_token):
        return "model-1", "project-1", None, None

    async def fake_get_model_measures(model_id, tenant_slug, jwt_token, **kw):
        return [{"id": "m1", "name": "Sales", "default_agg": "avg"}]

    async def fake_get_model_dimensions(model_id, tenant_slug, jwt_token, **kw):
        return [{"id": "d1", "name": "Product"}]

    async def fake_get_model_hierarchies(model_id, tenant_slug, jwt_token, **kw):
        return []

    async def fake_get_model_named_sets(*a, **kw):
        return []

    captured: list[str] = []

    async def fake_execute_query(sql, model_id, tenant_slug, jwt_token,
                                 protocol="dax", **_kwargs):
        captured.append(sql)
        return {
            "columns": ["Product", "Sales"],
            "rows": [{"Product": p, "Sales": v} for p, v in top_n_members],
        }

    return (
        fake_resolve_model_id, fake_get_model_measures, fake_get_model_dimensions,
        fake_get_model_hierarchies, fake_get_model_named_sets,
        fake_execute_query, captured,
    )


@pytest.mark.asyncio
async def test_aggregate_set_dim_col_uses_source_identifier_under_alias(monkeypatch):
    """Under an aliased hierarchy the aggregate_set group-membership IN-list must
    now emit the SOURCE column ("Product"), not the MDX alias ("ProductByRegion").
    Reverting the Bug-8327 translation re-emits the non-bindable alias name."""
    from src.dax import xmla_server
    from defusedxml import ElementTree as ET

    survivors = [("P1", 100), ("P2", 90), ("P3", 80), ("P4", 70), ("P5", 60)]
    (fri, fmm, fmd, fmh, fns, feq, captured) = _make_fakes(survivors)

    monkeypatch.setattr(xmla_server, "_resolve_model_id", fri)
    monkeypatch.setattr(xmla_server, "get_model_measures", fmm)
    monkeypatch.setattr(xmla_server, "get_model_dimensions", fmd)
    monkeypatch.setattr(xmla_server, "get_model_hierarchies", fmh)
    monkeypatch.setattr(xmla_server, "get_model_named_sets", fns)
    monkeypatch.setattr(xmla_server, "execute_query", feq)
    monkeypatch.setattr(
        xmla_server, "_extract_axis_hierarchy_dimension_aliases",
        lambda *a, **k: {"ProductByRegion": "Product"},
    )

    execute_xml = """<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <Execute xmlns="urn:schemas-microsoft-com:xml-analysis">
      <Command>
        <Statement>
WITH MEMBER [Product].[Product].[Top Two] AS
  AGGREGATE({[Product].[Product].[P1], [Product].[Product].[P2]})
SELECT
  {[Measures].[Sales]} ON COLUMNS,
  TopCount([Product].[Product].Members, 5, [Measures].[Sales]) ON ROWS
FROM [m]
        </Statement>
      </Command>
      <Properties>
        <PropertyList><Catalog>m</Catalog></PropertyList>
      </Properties>
    </Execute>
  </soap:Body>
</soap:Envelope>"""

    root = ET.fromstring(execute_xml)
    method_el = xmla_server._find_method(root)
    assert method_el is not None
    await xmla_server._handle_execute(
        method_el, tenant_slug="demo", jwt_token="tok", session_id="sid-8327",
    )

    requery_sqls = [
        s for s in captured
        if "AVG" in s.upper() and "GROUP BY" not in s.upper()
    ]
    assert requery_sqls, f"expected an aggregate_set AVG re-query; captured={captured}"
    for s in requery_sqls:
        assert '"ProductByRegion"' not in s, (
            f"aggregate_set re-query emitted the non-bindable alias identifier "
            f"for the group-membership pin (Bug-8327 not applied): {s}"
        )
        assert '"Product" IN' in s, (
            f"aggregate_set group-membership IN-list is not on the bindable source "
            f"column 'Product': {s}"
        )
