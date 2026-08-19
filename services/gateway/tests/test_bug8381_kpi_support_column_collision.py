"""Bug-8381: a KPI support member must never overwrite a real result column.

The goal/status support members are not SQL columns — they are dropped from SQL
resolution and their constant is post-joined by WRITING into a row column keyed
on the support name. If the router already returned a column of that name, the
write destroys it on every row.

Bug-6888's bare-token detection made that reachable by accident (a dimension
named "aa Goal" alone tripped the match); the Bug-8288 lane fixed that false
positive by matching the full member unique name, and
``test_bug8288_native_pivot_kpi_status.test_dimension_named_like_goal_member_not_overwritten``
guards it. What remained — and what this module guards — is the GENUINE
collision: an MDX that legitimately requests BOTH ``[Measures].[X Goal]`` and a
dimension named "X Goal". The earlier collision guards compare only against
MEASURE names, so nothing stopped the post-join from flattening the dimension
column to the static constant.

Test escape: the existing guard only covered the dimension-ALONE case, so the
dimension-AND-member case still silently returned the constant in place of every
member value. Guard: this module. Tier: T2 fixed-bug regression.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

from src.dax import xmla_server

_MEASURES = [
    {"id": "m-fee", "name": "fee_amount", "default_agg": "sum",
     "is_hidden": False},
]

_GOAL_KPI = {
    "id": "k-nr", "name": "Net Revenue", "display_name": "Net Revenue",
    "expression": 'measure("fee_amount")', "value_measure_id": None,
    "target_type": "static", "target_value": 188914000.0,
    "status_expression": "", "trend_expression": "", "parent_kpi_id": None,
}

# A dimension whose caption is exactly the KPI's goal support member name.
_COLLIDING_DIM_NAME = "Net Revenue Goal"


async def _run(statement, monkeypatch, *, dim_name, router_columns, router_rows):
    async def fri(catalog, tenant_slug, jwt_token):
        return "model-1", "project-1", None, None

    async def fmm(model_id, tenant_slug, jwt_token, **kw):
        return list(_MEASURES)

    async def fmd(model_id, tenant_slug, jwt_token, **kw):
        return [{"id": "d1", "name": dim_name}]

    async def fmh(model_id, tenant_slug, jwt_token, **kw):
        return []

    async def fns(*a, **kw):
        return []

    async def fkpis(model_id, tenant_slug, jwt_token, **kw):
        return [dict(_GOAL_KPI)]

    async def feq(sql, model_id, tenant_slug, jwt_token, protocol="dax", **_kw):
        return {"columns": list(router_columns), "rows": [dict(r) for r in router_rows]}

    for name, fn in {
        "_resolve_model_id": fri, "get_model_measures": fmm,
        "get_model_dimensions": fmd, "get_model_hierarchies": fmh,
        "get_model_named_sets": fns, "get_model_kpis": fkpis,
        "execute_query": feq,
    }.items():
        monkeypatch.setattr(xmla_server, name, fn)

    execute_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <Execute xmlns="urn:schemas-microsoft-com:xml-analysis">
      <Command><Statement>{statement}</Statement></Command>
      <Properties><PropertyList><Catalog>m</Catalog></PropertyList></Properties>
    </Execute>
  </soap:Body>
</soap:Envelope>"""
    root = ET.fromstring(execute_xml)
    method_el = xmla_server._find_method(root)
    resp = await xmla_server._handle_execute(
        method_el, tenant_slug="demo", jwt_token="tok", session_id="sid-8381",
    )
    return resp.body.decode("utf-8") if isinstance(resp.body, bytes) else str(resp.body)


@pytest.mark.asyncio
async def test_goal_member_and_same_named_dimension_together_fails_loud(monkeypatch):
    """The reported wrong-numbers shape: both the goal member AND a same-named
    dimension are on the axes, so the post-join would flatten every member value
    to the single static constant."""
    stmt = (
        "SELECT {[Measures].[fee_amount],[Measures].[Net Revenue Goal]} ON COLUMNS, "
        "{[Net Revenue Goal].[Net Revenue Goal].Members} ON ROWS FROM [m]"
    )
    body = await _run(
        stmt, monkeypatch,
        dim_name=_COLLIDING_DIM_NAME,
        router_columns=[_COLLIDING_DIM_NAME, "fee_amount"],
        router_rows=[
            {_COLLIDING_DIM_NAME: "North", "fee_amount": 10.0},
            {_COLLIDING_DIM_NAME: "South", "fee_amount": 20.0},
        ],
    )

    assert "Fault" in body, body
    assert "collides with the column" in body, body
    # The wrong answer must never be produced: neither the flattened constant
    # nor a half-overwritten member set may reach the client.
    assert "188914000" not in body, body


@pytest.mark.asyncio
async def test_collision_check_is_case_insensitive(monkeypatch):
    """SQL identifiers fold case; a dimension returned as 'net revenue goal'
    would still be the column the constant overwrites."""
    stmt = (
        "SELECT {[Measures].[fee_amount],[Measures].[Net Revenue Goal]} ON COLUMNS, "
        "{[Net Revenue Goal].[Net Revenue Goal].Members} ON ROWS FROM [m]"
    )
    body = await _run(
        stmt, monkeypatch,
        dim_name=_COLLIDING_DIM_NAME,
        router_columns=["net revenue goal", "fee_amount"],
        router_rows=[{"net revenue goal": "North", "fee_amount": 10.0}],
    )
    assert "Fault" in body, body
    assert "collides with the column" in body, body


@pytest.mark.asyncio
async def test_goal_member_without_a_colliding_column_still_serves(monkeypatch):
    """Control: the ordinary goal post-join must be untouched by the guard."""
    stmt = (
        "SELECT {[Measures].[fee_amount],[Measures].[Net Revenue Goal]} ON COLUMNS, "
        "{[Region].[Region].Members} ON ROWS FROM [m]"
    )
    body = await _run(
        stmt, monkeypatch,
        dim_name="Region",
        router_columns=["Region", "fee_amount"],
        router_rows=[
            {"Region": "North", "fee_amount": 10.0},
            {"Region": "South", "fee_amount": 20.0},
        ],
    )
    assert "Fault" not in body, body
    assert "North" in body and "South" in body, body
    assert "188914000" in body, body


@pytest.mark.asyncio
async def test_info_measure_constant_also_cannot_overwrite_a_column(monkeypatch):
    """Shared-primitive discipline (CLAUDE.md): the KPI goal/status post-joins
    are not the only constant writers on this path — the F-002-13 info/trust
    measures write ``r[internal] = const`` the same way. The internal names are
    a closed, underscore-prefixed set rather than user-chosen captions, so the
    exposure is smaller, but the class is identical and one guard now covers all
    three writers. Enumerating the siblings of a fixed primitive is exactly what
    the shared-primitive rule requires."""
    stmt = (
        "SELECT {[Measures].[fee_amount],[Measures].[_info_owner]} ON COLUMNS, "
        "{[_info_owner].[_info_owner].Members} ON ROWS FROM [m]"
    )
    body = await _run(
        stmt, monkeypatch,
        dim_name="_info_owner",
        router_columns=["_info_owner", "fee_amount"],
        router_rows=[{"_info_owner": "North", "fee_amount": 10.0}],
    )
    assert "Fault" in body, body
    assert "collides with the column" in body, body


def test_every_post_join_constant_writer_sits_after_the_collision_guard() -> None:
    """Coverage-tool blind spot (CLAUDE.md; deep-review finding 7).

    The guard's own comment claims a new constant writer "inherits it" — that is
    only true if the writer is placed AFTER it. Nothing enforced that. The
    query-router has a real AST enumeration guard for its analogous property;
    this gives the gateway one. Promoted from the review that found the gap.
    """
    import ast
    import pathlib

    path = (
        pathlib.Path(__file__).resolve().parents[1]
        / "src" / "dax" / "xmla_server.py"
    )
    tree = ast.parse(path.read_text(encoding="utf-8"))
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_handle_execute"
    )
    guard_line = min(
        n.lineno for n in ast.walk(fn)
        if isinstance(n, ast.FunctionDef) and n.name == "_const_column_collision"
    )
    # Any ``columns = list(columns) + [X]`` is a post-join constant append.
    appends = [
        n.lineno for n in ast.walk(fn)
        if isinstance(n, ast.Assign)
        and any(getattr(t, "id", None) == "columns" for t in n.targets)
        and isinstance(n.value, ast.BinOp)
        and isinstance(n.value.op, ast.Add)
    ]
    assert appends, "constant-append shape changed; re-derive this guard"
    early = [ln for ln in appends if ln < guard_line]
    assert early == [], (
        f"post-join constant writer(s) at line(s) {early} run BEFORE the "
        f"collision guard at line {guard_line} and can overwrite a real column"
    )

    # Deep-review R3 finding 7: the append is only the visible half. The
    # property that actually matters is that no ``r[<name>] = <const>`` ROW
    # write precedes the guard — that is the write which destroys a real
    # column's values. Check it directly rather than inferring it from the
    # append, and cover ``columns.append(...)`` / ``columns += [...]`` too, so
    # a writer that uses a different append idiom cannot slip past.
    row_writes = [
        n.lineno for n in ast.walk(fn)
        if isinstance(n, ast.Assign)
        and any(
            isinstance(t, ast.Subscript) and getattr(t.value, "id", None) == "r"
            for t in n.targets
        )
    ]
    assert row_writes, "row-write shape changed; re-derive this guard"
    early_writes = [ln for ln in row_writes if ln < guard_line]
    assert early_writes == [], (
        f"post-join row write(s) at line(s) {early_writes} run BEFORE the "
        f"collision guard at line {guard_line} and can overwrite a real "
        "column's values on every row"
    )

    other_append_idioms = [
        n.lineno for n in ast.walk(fn)
        if (
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "append"
            and getattr(n.func.value, "id", None) == "columns"
        ) or (
            isinstance(n, ast.AugAssign)
            and getattr(n.target, "id", None) == "columns"
        )
    ]
    early_other = [ln for ln in other_append_idioms if ln < guard_line]
    assert early_other == [], (
        f"column append(s) at line(s) {early_other} use an idiom this guard "
        "did not previously recognise AND run before the collision guard"
    )


def test_a_calc_member_named_like_a_dimension_column_does_not_destroy_it():
    """Bug-8381 sibling. The collision guard covers the three CONSTANT writers in
    _handle_execute. evaluate_calc_members is a FOURTH name-keyed row writer on
    the same response path — ``r[calc.name] = value`` — and its name is chosen by
    the client. A member named like a dimension column overwrites that column on
    every row BEFORE build_real_execute_response derives dim_members_map, so the
    ROWS axis renders the calculated numbers as the dimension's members."""
    from src.dax.mdx_calc_members import CalcMember, evaluate_calc_members

    rows = [{"Product": "P1", "Amt": 10}, {"Product": "P2", "Amt": 30}]
    calc = CalcMember(
        name="Product",
        expression="[Measures].[Amt] / ([Measures].[Amt],[Product].[Product].[All])",
        calc_type="pct_grand_total", base_measure="Amt",
    )
    with pytest.raises(ValueError, match=r"(?i)collide|overwrite"):
        evaluate_calc_members(
            [calc], rows, ["Amt"], ["Product"],
            [{"name": "Amt", "default_agg": "sum"}],
        )
    assert [r["Product"] for r in rows] == ["P1", "P2"], (
        f"the dimension column was destroyed: {rows}"
    )
