"""Bug-8272 (WRONG RATIO): a calc-member denominator / aggregate-set re-query
must include the enumerated ROWS/COLUMNS axis member set, or a keep-only axis
selection produces a denominator aggregated over the FULL unfiltered domain --
a silently wrong % of Grand/Parent/Row/Column Total (a wrong board-pack ratio).

The main detail SQL (Bug-5548) and the subtotal/grain queries (Bug-5548 grain
merge) already restrict to an enumerated axis member set
(``{[Product].&[A],[Product].&[B]}``). The non-additive calc-member DENOMINATOR
re-queries (and the aggregate_set custom-group total) were built only from the
WHERE/subselect/label slicers -- never the axis member set -- so the numerator
was over the two kept members while the denominator was over ALL members.

Two levels of coverage:

1. Builder seam: extracting the axis members and feeding them as ``extra_where``
   into ``build_denominator_requery_sql`` scopes the denominator to the kept set.
   Revert guard: without the axis merge the denominator SQL carries NO member
   restriction (aggregates the whole domain).
2. Integration: drive the REAL ``_handle_execute`` path (mocked ``execute_query``)
   with a keep-only enumerated axis + a % of Grand Total over a NON-ADDITIVE
   (avg) measure, and assert the captured denominator re-query is constrained to
   the two kept members. This is the unit-passes-but-production-path-unwired
   guard -- it fails if the ``_rq_where_sql`` axis merge is reverted.
"""
import pytest

from src.dax.xmla_server import (
    _mdx_extract_axis_member_filters,
    _build_where_sql_clauses,
    _qi,
)
from src.dax.mdx_calc_members import (
    DenomReQuerySpec,
    build_denominator_requery_sql,
)


# ---------------------------------------------------------------------------
# 1. Builder seam: axis members -> extra_where -> constrained denominator SQL
# ---------------------------------------------------------------------------

def test_denominator_requery_scoped_to_enumerated_axis_members():
    # A keep-only axis selection of two members.
    axis_expr = (
        "{[Product].[Product].&[A], [Product].[Product].&[B]}"
    )
    dim_names = {"Product"}
    axis_filters = _mdx_extract_axis_member_filters(axis_expr, dim_names)
    assert axis_filters == {"Product": ["A", "B"]}

    # Build the WHERE the fix merges into _rq_where_sql.
    extra_where = _build_where_sql_clauses(
        axis_filters, lambda n: _qi("postgresql", n),
    )
    assert extra_where, "axis member filter must produce a WHERE clause"

    spec = DenomReQuerySpec(
        calc_name="Pct",
        measure_name="Sales",
        agg="avg",
        model_slug="m",
        partition_key=("__all__",),
        extra_where=list(extra_where),
    )
    sql = build_denominator_requery_sql(spec)

    # The denominator must be scoped to exactly the two kept members.
    assert 'AVG("Sales")' in sql
    assert '"Product" IN (' in sql
    assert "'A'" in sql and "'B'" in sql


def test_denominator_requery_unconstrained_without_axis_merge_is_wrong():
    # Revert guard: without the axis members in extra_where the denominator has
    # NO member restriction -> it aggregates the full domain (the wrong ratio).
    spec = DenomReQuerySpec(
        calc_name="Pct",
        measure_name="Sales",
        agg="avg",
        model_slug="m",
        partition_key=("__all__",),
        extra_where=[],  # the buggy pre-fix state
    )
    sql = build_denominator_requery_sql(spec)
    assert "WHERE" not in sql, (
        "a denominator with no axis-member merge aggregates the whole domain -- "
        "this is the wrong-ratio state Bug-8272 fixes"
    )


def test_bare_members_axis_adds_no_denominator_filter_no_regression():
    # A full-level .Members axis must NOT add a member filter (the denominator
    # legitimately spans the whole level). No regression for full-domain pivots.
    axis_filters = _mdx_extract_axis_member_filters(
        "{[Product].[Product].Members}", {"Product"},
    )
    assert axis_filters == {}


# ---------------------------------------------------------------------------
# 2. Integration: the REAL _handle_execute path wires the axis members into the
#    denominator re-query (production-path guard).
# ---------------------------------------------------------------------------

def _make_fakes(members):
    async def fake_resolve_model_id(catalog, tenant_slug, jwt_token):
        return "model-1", "project-1", None, None

    async def fake_get_model_measures(model_id, tenant_slug, jwt_token, **kw):
        # avg = NON-ADDITIVE -> the denominator IS re-queried (the fixed path).
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
        # Return only the two kept members (the detail axis is already filtered).
        return {
            "columns": ["Product", "Sales"],
            "rows": [{"Product": p, "Sales": v} for p, v in members],
        }

    return (
        fake_resolve_model_id, fake_get_model_measures, fake_get_model_dimensions,
        fake_get_model_hierarchies, fake_get_model_named_sets,
        fake_execute_query, captured_sqls,
    )


@pytest.mark.asyncio
async def test_handle_execute_aggregate_set_requery_scoped_to_axis(monkeypatch):
    from src.dax import xmla_server
    from defusedxml import ElementTree as ET

    kept = [("A", 100), ("B", 60)]
    (fri, fmm, fmd, fmh, fns, feq, captured) = _make_fakes(kept)

    monkeypatch.setattr(xmla_server, "_resolve_model_id", fri)
    monkeypatch.setattr(xmla_server, "get_model_measures", fmm)
    monkeypatch.setattr(xmla_server, "get_model_dimensions", fmd)
    monkeypatch.setattr(xmla_server, "get_model_hierarchies", fmh)
    monkeypatch.setattr(xmla_server, "get_model_named_sets", fns)
    monkeypatch.setattr(xmla_server, "execute_query", feq)

    # REVERT-ISOLATING construction: the aggregate_set custom group is
    # {A, B, C}, but the pivot's keep-only ENUMERATED axis is {A, B} (member C
    # is dropped from the shown rows). The aggregate_set re-query ALWAYS carries
    # its own group-membership predicate ``Product IN ('A', 'B', 'C')`` (from
    # build_requery_sql, independent of Bug-8272). The Bug-8272 axis-member merge
    # ADDS a SEPARATE, ANDed axis keep-only clause ``Product IN ('A', 'B')`` (no
    # C). So WITH the fix the WHERE has TWO ``... IN (...)`` clauses (the group's
    # with C, and the axis's without C); WITHOUT the fix it has ONLY the group
    # clause containing C, and the re-query silently aggregates the off-axis
    # member C -> wrong custom-group total. Asserting the presence of the
    # axis-only ``IN ('A', 'B')`` clause isolates exactly the axis merge and
    # fails on revert.
    execute_xml = """<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <Execute xmlns="urn:schemas-microsoft-com:xml-analysis">
      <Command>
        <Statement>
WITH MEMBER [Product].[Product].[Custom Group] AS
  AGGREGATE({[Product].[Product].[A], [Product].[Product].[B], [Product].[Product].[C]})
SELECT
  {[Product].[Product].[A], [Product].[Product].[B]} ON ROWS,
  {[Measures].[Sales]} ON COLUMNS
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
        method_el, tenant_slug="demo", jwt_token="tok", session_id="sid-8272",
    )

    # The aggregate_set re-query is the AVG query WITHOUT a GROUP BY on Product
    # (it aggregates the whole custom group), distinct from the detail query.
    denom_sqls = [
        s for s in captured
        if "AVG" in s.upper() and "GROUP BY" not in s.upper()
    ]
    assert denom_sqls, (
        f"expected an aggregate_set AVG re-query (no GROUP BY); captured={captured}"
    )
    for s in denom_sqls:
        # The axis-derived keep-only clause (exactly {A, B}, WITHOUT C) must be
        # present. This is the revert-catching assertion: it is the SEPARATE IN
        # clause the Bug-8272 axis merge appends. Without the fix only the group
        # predicate ``IN ('A', 'B', 'C')`` exists and this exact clause is absent.
        axis_clause = "\"Product\" IN ('A', 'B')"
        assert axis_clause in s, (
            f"the Bug-8272 axis keep-only clause {axis_clause!r} (dropping the "
            f"off-axis group member C) is NOT in the aggregate_set re-query -> "
            f"the denominator aggregates C, a member the pivot does not show "
            f"(wrong custom-group total). The axis-member merge did not fire on "
            f"the _handle_execute path: {s}"
        )
        # Sanity: the group predicate (with C) is the OTHER IN clause.
        assert s.count(" IN (") >= 2, (
            f"expected two IN clauses (group + axis merge); got: {s}"
        )
