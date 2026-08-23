"""Bug-8283 integration wiring: the calc-member re-query Top-N survivor
predicate must reach the re-query SQL on the REAL ``_handle_execute`` path.

The builder-level Bug-8283 tests (in ``test_topn_subtotal_member_set.py``)
exercise the SQL BUILDER seams (``_topn_member_predicate`` +
``build_denominator_requery_sql`` / ``build_requery_sql``) with a manually
passed predicate. They do NOT drive ``_handle_execute``, so they cannot prove
the fix actually APPENDS the survivor predicate into ``sp.extra_where`` on the
real request path (the unit-passes / production-path-unwired class). This test
closes that gap: it drives ``_handle_execute`` end-to-end (mocked
``execute_query``) with a TopCount pivot + an aggregate_set (custom-group) calc
member over a NON-ADDITIVE (avg) measure -- which re-queries via
``build_requery_sql`` -- and asserts the captured re-query SQL is constrained
to the surviving ranked members. Revert-verified: without the Bug-8283 append
the re-query drops to ``WHERE "Product" IN ('P1', 'P2')`` (group membership
only) and this test fails.
"""
import pytest


def _make_fakes(top_n_members, hidden_member):
    async def fake_resolve_model_id(catalog, tenant_slug, jwt_token):
        return "model-1", "project-1", None, None

    async def fake_get_model_measures(model_id, tenant_slug, jwt_token, **kw):
        # avg = NON-ADDITIVE -> denominator IS re-queried (the fixed path).
        return [{"id": "m1", "name": "Sales", "default_agg": "avg"}]

    async def fake_get_model_dimensions(model_id, tenant_slug, jwt_token, **kw):
        return [{"id": "d1", "name": "Product"}]

    async def fake_get_model_hierarchies(model_id, tenant_slug, jwt_token, **kw):
        return []

    async def fake_get_model_named_sets(*a, **kw):
        return []

    captured_sqls: list[str] = []

    async def fake_execute_query(sql, model_id, tenant_slug, jwt_token,
                                 protocol="dax", **_kwargs):
        captured_sqls.append(sql)
        # The DETAIL query has already applied Top-5 LIMIT: return ONLY the
        # five survivors (the hidden 6th never comes back on the detail path).
        return {
            "columns": ["Product", "Sales"],
            "rows": [{"Product": p, "Sales": v} for p, v in top_n_members],
        }

    return (
        fake_resolve_model_id, fake_get_model_measures, fake_get_model_dimensions,
        fake_get_model_hierarchies, fake_get_model_named_sets,
        fake_execute_query, captured_sqls,
    )


@pytest.mark.asyncio
async def test_topn_calc_member_denominator_requery_is_constrained(monkeypatch):
    from src.dax import xmla_server
    from defusedxml import ElementTree as ET

    survivors = [("P1", 100), ("P2", 90), ("P3", 80), ("P4", 70), ("P5", 60)]
    (fri, fmm, fmd, fmh, fns, feq, captured) = _make_fakes(survivors, ("P6", 50))

    monkeypatch.setattr(xmla_server, "_resolve_model_id", fri)
    monkeypatch.setattr(xmla_server, "get_model_measures", fmm)
    monkeypatch.setattr(xmla_server, "get_model_dimensions", fmd)
    monkeypatch.setattr(xmla_server, "get_model_hierarchies", fmh)
    monkeypatch.setattr(xmla_server, "get_model_named_sets", fns)
    monkeypatch.setattr(xmla_server, "execute_query", feq)

    # TopCount(5) ON ROWS + an aggregate_set (custom group) calc member over avg
    # Sales. The aggregate_set re-query flows through the SAME extra_where wiring
    # the Bug-8283 fix appends the survivor predicate to (build_requery_sql),
    # and this calc-member shape is the one proven to drive _handle_execute
    # (see test_bug_5189_5191_5254). If the survivor predicate is NOT appended,
    # the AVG custom-group re-query aggregates all six members (incl the hidden
    # P6) instead of the five survivors.
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
        method_el, tenant_slug="demo", jwt_token="tok", session_id="sid-8283",
    )

    # The aggregate_set re-query is the one WITHOUT a GROUP BY on Product (it
    # aggregates the whole custom group), distinct from the main detail query.
    requery_sqls = [
        s for s in captured
        if "AVG" in s.upper() and "GROUP BY" not in s.upper()
    ]
    assert requery_sqls, (
        f"expected an aggregate_set AVG re-query (no GROUP BY) to be issued; "
        f"captured={captured}"
    )
    # The survivor predicate is the FULL five-member IN list (P1..P5). The group
    # membership predicate alone is only ('P1', 'P2'); the fix ADDS the survivor
    # IN list on top. Require the five-member survivor list explicitly, so this
    # probe FAILS if the Bug-8283 append is reverted (verified: without the fix
    # the re-query is only ``WHERE "Product" IN ('P1', 'P2')``).
    survivor_pred = "IN ('P1', 'P2', 'P3', 'P4', 'P5')"
    for s in requery_sqls:
        assert "'P6'" not in s and '"P6"' not in s, (
            f"aggregate_set re-query includes the HIDDEN member P6 (unconstrained "
            f"-> wrong custom-group total): {s}"
        )
        assert survivor_pred in s, (
            f"aggregate_set re-query is NOT constrained to the Top-N survivors "
            f"(missing {survivor_pred!r}); the Bug-8283 append did not fire on "
            f"the _handle_execute path: {s}"
        )


@pytest.mark.asyncio
async def test_topn_calc_member_requery_constrained_under_aliased_hierarchy(
    monkeypatch,
):
    """R1 + R4 findings, exercised through the REAL handler path.

    When the axis uses a dimension-alias hierarchy, ``_handle_execute`` renames
    the result column ``Product`` -> ``ProductByRegion`` (line ~2807) BEFORE the
    calc-member block. Two things must hold together:
    - The survivor grain cols must be keyed on the POST-alias dimension set, or
      the re-query loses its Top-N constraint (R1). Reverting the keying to the
      pre-alias ``dim_names`` makes the predicate empty and this test fails.
    - The predicate SQL must emit the SOURCE identifier (``"Product"``), not the
      MDX hierarchy-alias name (``"ProductByRegion"``), because the re-query is
      bound by the query-router as canonical postgres and the binder resolves
      only source dimension/level names — the alias name would raise
      SemanticBindingError -> SOAP fault (R4 finding 1). This asserts the
      constrained re-query uses the bindable source column.
    """
    from src.dax import xmla_server
    from defusedxml import ElementTree as ET

    survivors = [("P1", 100), ("P2", 90), ("P3", 80), ("P4", 70), ("P5", 60)]
    (fri, fmm, fmd, fmh, fns, feq, captured) = _make_fakes(survivors, ("P6", 50))

    monkeypatch.setattr(xmla_server, "_resolve_model_id", fri)
    monkeypatch.setattr(xmla_server, "get_model_measures", fmm)
    monkeypatch.setattr(xmla_server, "get_model_dimensions", fmd)
    monkeypatch.setattr(xmla_server, "get_model_hierarchies", fmh)
    monkeypatch.setattr(xmla_server, "get_model_named_sets", fns)
    monkeypatch.setattr(xmla_server, "execute_query", feq)
    # Force the hierarchy-alias rename: Product (source) -> ProductByRegion.
    monkeypatch.setattr(
        xmla_server,
        "_extract_axis_hierarchy_dimension_aliases",
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
        method_el, tenant_slug="demo", jwt_token="tok", session_id="sid-8283-alias",
    )

    requery_sqls = [
        s for s in captured
        if "AVG" in s.upper() and "GROUP BY" not in s.upper()
    ]
    assert requery_sqls, (
        f"expected an aggregate_set AVG re-query under aliased hierarchy; "
        f"captured={captured}"
    )
    # Post-rename, the SURVIVOR predicate (the Bug-8283 append) must be on the
    # SOURCE column name ("Product"), the bindable identifier — NOT the MDX
    # hierarchy-alias name ("ProductByRegion"). If the keying used the pre-alias
    # set, no grain cols would match -> no survivor IN-list here (R1). If the
    # survivor predicate emitted the alias identifier, the router would fault
    # instead of returning the constrained % (R4 finding 1).
    #
    # NOTE: the aggregate_set spec's OWN group-membership predicate (``dim_col``
    # from ``_dim_cols``) and the parent-dim partition pins were a SEPARATE
    # alias-emission defect — they emitted the alias name (``"ProductByRegion" IN
    # ('P1', 'P2')``) and would fault the re-query on an aliased pivot. That is now
    # fixed by Bug-8327 (``_translate_requery_partition_identifiers``), with its own
    # dedicated guard in ``test_bug8327_requery_partition_pin_translation.py``. This
    # test asserts only what the Bug-8283 survivor-predicate fix guarantees: the
    # SURVIVOR IN-list is on the bindable source column.
    survivor_pred = "\"Product\" IN ('P1', 'P2', 'P3', 'P4', 'P5')"
    for s in requery_sqls:
        assert "'P6'" not in s, (
            f"aliased re-query includes hidden member P6 (unconstrained): {s}"
        )
        assert survivor_pred in s, (
            f"aliased-hierarchy re-query lost its Top-N survivor constraint or "
            f"emitted a non-bindable alias identifier for the survivor predicate "
            f"(expected source-column predicate {survivor_pred!r}): {s}"
        )
