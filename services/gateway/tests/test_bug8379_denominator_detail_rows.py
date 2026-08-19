"""Bug-8379: the DENOMINATOR re-query planner must partition over DETAIL rows
only, never the merged (detail + subtotal/grand-total) result set.

Bug-8323 fixed exactly this for the aggregate_set planner/evaluator and
explicitly scoped itself there; ``plan_denominator_requeried`` — the planner for
every non-additive ``% of Grand Total`` / ``% of Parent`` / ``% of Row|Column
Total`` denominator — was left iterating the merged rows and is the leg this
module guards.

Why a spurious spec is not merely wasteful:

* A subtotal row has ``None`` in every dimension column finer than its grain.
  ``_normalize_member_value`` maps that to ``BLANK_MEMBER`` and
  ``build_denominator_requery_sql`` pins it as ``(dim IS NULL OR dim = '')`` —
  a partition that describes no row the pivot displays.
* The evaluator never consumes it: ``evaluate_calc_members`` runs every
  grain-sensitive calc over detail rows and blanks the calc column on subtotal
  rows. So the spec is pure overhead.
* But it is a REQUIRED re-query (F-002-03). ``_handle_execute`` fails the WHOLE
  Execute closed when any planned re-query errors or trips the byte-ceiling /
  rate-limit guard (Bug-7745). A pivot that would have rendered correct
  percentages can therefore be denied outright because of a query that never
  needed to run — and on a deep hierarchy the planner emits one per distinct
  subtotal partition, multiplying both the cost and that exposure.

Test escape: Bug-8323's guard module asserted the property for the aggregate_set
planner only, so the denominator planner's identical loops were uncovered.
Guard: this module (planner unit + witness + real ``_handle_execute`` path +
structural enumeration guard). Tier: T2 fixed-bug regression.
"""
from __future__ import annotations

import ast
import pathlib

import pytest
from defusedxml import ElementTree as ET

from src.dax.mdx_calc_members import (
    BLANK_MEMBER,
    CalcMember,
    build_denominator_requery_sql,
    plan_denominator_requeried,
)
from src.dax.subtotal_engine import SUBTOTAL_LEVEL_KEY

_MEASURES = [{"name": "Amt", "default_agg": "avg"}]  # non-additive -> re-queried
_DIM_COLS = ["Region", "City"]


def _pct_parent_calc() -> CalcMember:
    return CalcMember(
        name="PctPar",
        expression="[Measures].[Amt] / ([Measures].[Amt], [City].[City].Parent)",
        calc_type="pct_parent",
        base_measure="Amt",
        ref_member_parts=["City", "City"],
    )


def _pct_axis_calc() -> CalcMember:
    return CalcMember(
        name="PctRow",
        expression="[Measures].[Amt] / ([Measures].[Amt], [City].[City].[All])",
        calc_type="pct_row_total",
        base_measure="Amt",
    )


def _merged_rows() -> list[dict]:
    """Two regions of detail plus a Region subtotal and a grand-total row.

    The subtotal rows are exactly what ``merge_grain_results`` produces: the
    coarser grain query returns only its own dimension columns, so every finer
    dimension reads back as ``None``.
    """
    return [
        {"Region": None, "City": None, "Amt": 20, SUBTOTAL_LEVEL_KEY: "GrandTotal"},
        {"Region": "R1", "City": None, "Amt": 15, SUBTOTAL_LEVEL_KEY: "Region"},
        {"Region": "R2", "City": None, "Amt": 30, SUBTOTAL_LEVEL_KEY: "Region"},
        {"Region": "R1", "City": "A", "Amt": 10, SUBTOTAL_LEVEL_KEY: "detail"},
        {"Region": "R1", "City": "B", "Amt": 20, SUBTOTAL_LEVEL_KEY: "detail"},
        {"Region": "R2", "City": "C", "Amt": 30, SUBTOTAL_LEVEL_KEY: "detail"},
    ]


# ---------------------------------------------------------------------------
# 1. Planner level
# ---------------------------------------------------------------------------

def test_pct_parent_denominator_emits_no_blank_partition_from_subtotal_rows():
    specs = plan_denominator_requeried(
        [_pct_parent_calc()], _MEASURES, "model", _DIM_COLS, _merged_rows(),
    )
    assert specs, "expected per-parent denominator specs for a non-additive measure"
    keys = sorted(sp.partition_key for sp in specs)
    assert keys == [("R1",), ("R2",)], (
        f"denominator planner partitioned over subtotal rows: {keys}"
    )
    for sp in specs:
        assert BLANK_MEMBER not in sp.partition_values, (
            f"spurious blank-member partition planned from a subtotal row: "
            f"{sp.partition_dims}={sp.partition_values}"
        )


def test_pct_axis_total_denominator_emits_no_blank_partition_from_subtotal_rows():
    specs = plan_denominator_requeried(
        [_pct_axis_calc()], _MEASURES, "model", _DIM_COLS, _merged_rows(),
        row_axis_dims=["Region"], col_axis_dims=["City"],
    )
    assert specs, "expected per-axis denominator specs for a non-additive measure"
    keys = sorted(sp.partition_key for sp in specs)
    assert keys == [("R1",), ("R2",)], (
        f"axis-total denominator planner partitioned over subtotal rows: {keys}"
    )


def test_no_blank_member_sql_is_generated_for_a_pivot_without_blank_members():
    """The consumer-visible shape of the defect: an ``IS NULL`` denominator.

    None of the six merged rows carries a genuinely blank member, so no
    denominator SQL may pin one.
    """
    specs = plan_denominator_requeried(
        [_pct_parent_calc()], _MEASURES, "model", _DIM_COLS, _merged_rows(),
    )
    sqls = [build_denominator_requery_sql(sp) for sp in specs]
    assert sqls
    for sql in sqls:
        assert "IS NULL" not in sql, (
            f"denominator re-query pins a blank member no displayed row has: {sql}"
        )


def test_witness_merged_rows_would_contaminate_without_the_filter():
    """Witness that the danger is real, not hypothetical.

    Feeding the planner the DETAIL rows plus the subtotal rows *as if they were
    detail* (what the pre-fix code effectively did) does produce the spurious
    blank partition and its ``IS NULL`` SQL. This pins the mechanism so a future
    reader can see what the filter is buying.
    """
    untagged = [
        {k: v for k, v in r.items() if k != SUBTOTAL_LEVEL_KEY}
        for r in _merged_rows()
    ]
    specs = plan_denominator_requeried(
        [_pct_parent_calc()], _MEASURES, "model", _DIM_COLS, untagged,
    )
    keys = sorted(sp.partition_key for sp in specs)
    assert (BLANK_MEMBER,) in keys
    sqls = [build_denominator_requery_sql(sp) for sp in specs]
    assert any("IS NULL" in s for s in sqls)


def test_a_genuinely_blank_detail_member_is_still_planned():
    """Guard against over-filtering.

    A DETAIL row whose parent member is really NULL must keep its
    ``(dim IS NULL OR dim = '')`` denominator — the evaluator normalises that row
    to ``BLANK_MEMBER`` and looks the key up. Dropping it would blank a real cell
    (``require_reaggregated`` with no result), trading one wrong number for
    another.
    """
    rows = _merged_rows() + [
        {"Region": None, "City": "D", "Amt": 7, SUBTOTAL_LEVEL_KEY: "detail"},
    ]
    specs = plan_denominator_requeried(
        [_pct_parent_calc()], _MEASURES, "model", _DIM_COLS, rows,
    )
    keys = sorted(sp.partition_key for sp in specs)
    assert (BLANK_MEMBER,) in keys, (
        "a real blank-member detail row lost its denominator re-query"
    )
    blank_sql = [
        build_denominator_requery_sql(sp)
        for sp in specs if sp.partition_key == (BLANK_MEMBER,)
    ]
    assert blank_sql and 'IS NULL' in blank_sql[0]


def test_flat_pivot_is_unaffected():
    """No SUBTOTAL_LEVEL_KEY at all: every row defaults to detail (no-op)."""
    flat = [
        {"Region": "R1", "City": "A", "Amt": 10},
        {"Region": "R2", "City": "C", "Amt": 30},
    ]
    specs = plan_denominator_requeried(
        [_pct_parent_calc()], _MEASURES, "model", _DIM_COLS, flat,
    )
    assert sorted(sp.partition_key for sp in specs) == [("R1",), ("R2",)]


def test_grand_total_denominator_still_planned_once():
    """``pct_grand_total`` takes no row-derived partition — it must be untouched."""
    calc = CalcMember(
        name="PctGT",
        expression="[Measures].[Amt] / ([Measures].[Amt], [City].[City].[All])",
        calc_type="pct_grand_total",
        base_measure="Amt",
    )
    specs = plan_denominator_requeried(
        [calc], _MEASURES, "model", _DIM_COLS, _merged_rows(),
    )
    assert [sp.partition_key for sp in specs] == ["__grand__"]


# ---------------------------------------------------------------------------
# 2. Real _handle_execute path (the unit-passes / production-unwired class)
# ---------------------------------------------------------------------------

_MDX_SUBTOTAL_PCT_PARENT = """<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <Execute xmlns="urn:schemas-microsoft-com:xml-analysis">
      <Command>
        <Statement>
WITH MEMBER [Measures].[PctPar] AS
  '[Measures].[Amt] / ([Measures].[Amt], [country_dim].[country_dim].Parent)'
SELECT {[Measures].[Amt], [Measures].[PctPar]} ON COLUMNS,
       {[GeoHierarchy].[GeoHierarchy].Members} ON ROWS
FROM [m]
        </Statement>
      </Command>
      <Properties><PropertyList><Catalog>m</Catalog></PropertyList></Properties>
    </Execute>
  </soap:Body>
</soap:Envelope>"""


def _install_subtotal_pivot_fakes(monkeypatch, xmla_server):
    """A two-level hierarchy pivot over a NON-ADDITIVE measure.

    The router answers each grain from the SQL shape, so ``merge_grain_results``
    builds a genuinely merged row set (grand total + Region subtotals + detail)
    exactly as production does.
    """
    captured: list[str] = []

    async def fri(catalog, tenant_slug, jwt_token):
        return "model-1", "project-1", None, None

    async def fmm(model_id, tenant_slug, jwt_token, **kw):
        return [{"id": "m1", "name": "Amt", "default_agg": "avg"}]

    async def fmd(model_id, tenant_slug, jwt_token, **kw):
        return [
            {"id": "d1", "name": "region_dim", "source_column_id": "col-region"},
            {"id": "d2", "name": "country_dim", "source_column_id": "col-country"},
        ]

    async def fmh(model_id, tenant_slug, jwt_token, **kw):
        return [{
            "id": "h-geo",
            "name": "GeoHierarchy",
            "levels": [
                {"ordinal": 0, "name": "Region",
                 "key_attribute": {"id": "col-region", "source": "physical_column"}},
                {"ordinal": 1, "name": "Country",
                 "key_attribute": {"id": "col-country", "source": "physical_column"}},
            ],
        }]

    async def fns(*a, **kw):
        return []

    async def feq(sql, model_id, tenant_slug, jwt_token, protocol="dax", **_kw):
        captured.append(sql)
        has_country = "country_dim" in sql
        has_region = "region_dim" in sql
        if has_country and has_region:
            return {
                "columns": ["region_dim", "country_dim", "Amt"],
                "rows": [
                    {"region_dim": "EMEA", "country_dim": "DE", "Amt": 10},
                    {"region_dim": "EMEA", "country_dim": "FR", "Amt": 20},
                    {"region_dim": "APAC", "country_dim": "JP", "Amt": 30},
                ],
            }
        if has_region:
            return {
                "columns": ["region_dim", "Amt"],
                "rows": [
                    {"region_dim": "EMEA", "Amt": 15},
                    {"region_dim": "APAC", "Amt": 30},
                ],
            }
        return {"columns": ["Amt"], "rows": [{"Amt": 20}]}

    for name, fn in {
        "_resolve_model_id": fri, "get_model_measures": fmm,
        "get_model_dimensions": fmd, "get_model_hierarchies": fmh,
        "get_model_named_sets": fns, "execute_query": feq,
    }.items():
        monkeypatch.setattr(xmla_server, name, fn)
    return captured


@pytest.mark.asyncio
async def test_handle_execute_issues_no_blank_member_denominator_requery(monkeypatch):
    """End-to-end: a subtotal pivot + a non-additive % of Parent member.

    ``_handle_execute`` passes the MERGED rows to ``plan_denominator_requeried``,
    so this is the wiring that makes the planner defect reachable from Excel. No
    re-query issued for this pivot may pin a blank member — none of the three
    displayed detail rows has one.
    """
    from src.dax import xmla_server

    captured = _install_subtotal_pivot_fakes(monkeypatch, xmla_server)
    root = ET.fromstring(_MDX_SUBTOTAL_PCT_PARENT)
    resp = await xmla_server._handle_execute(
        root if root.tag.endswith("Execute") else xmla_server._find_method(root),
        tenant_slug="demo", jwt_token="tok", session_id="sid-8379",
    )
    body = resp.body.decode("utf-8") if isinstance(resp.body, bytes) else str(resp.body)
    assert "Fault" not in body, body

    # The subtotal engine must actually have fired, or this proves nothing.
    assert len(captured) >= 3, f"expected detail + grain queries, got {captured}"
    denom_requeries = [
        s for s in captured
        if "AVG" in s.upper() and "GROUP BY" not in s.upper()
    ]
    assert denom_requeries, (
        f"expected non-additive denominator re-queries to be issued; got {captured}"
    )
    for sql in denom_requeries:
        assert "IS NULL" not in sql.upper(), (
            "a denominator re-query pins a blank member that no displayed row "
            f"has — planned from a subtotal row: {sql}"
        )


# ---------------------------------------------------------------------------
# 3. Structural enumeration guard (CLAUDE.md coverage-tool blind-spot rule)
# ---------------------------------------------------------------------------

def _dax_source_files():
    """Every python file under ``src/dax`` — RECURSIVELY, and never only the one
    module a finding happened to be reported against."""
    dax_dir = pathlib.Path(__file__).resolve().parents[1] / "src" / "dax"
    return dax_dir, [
        p for p in sorted(dax_dir.rglob("*.py"))
        if "__pycache__" not in p.parts
    ]


def _planner_functions():
    """Derive the planner set by PROPERTY, not by a hand-written list: any
    ``plan_*`` function anywhere under ``src/dax`` that takes a ``rows``
    parameter (positional, positional-only or keyword-only) is a re-query planner
    and is in scope.

    A hand-written tuple, a single-module parse, or a ``tree.body``-only walk all
    fail OPEN on a planner added later, nested, or defined in a sibling module —
    the exact enumeration blind spot CLAUDE.md requires auditing for (deep-review
    R2 finding 5).
    """
    _dir, paths = _dax_source_files()
    out = []
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for n in ast.walk(tree):
            if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            # R3 blind-spot audit: a module-private ``_plan_*`` planner and a
            # ``merged_rows`` / ``result_rows`` parameter were both invisible to
            # the original test (proven: it discovered NOTHING for
            # ``def _plan_new_requeried(calc, rows)``), so the guard failed OPEN
            # on the most likely shape of the NEXT planner in this module.
            if not n.name.lstrip("_").startswith("plan_"):
                continue
            args = n.args
            if any(
                a.arg == "rows" or a.arg.endswith("_rows")
                for a in list(args.posonlyargs) + list(args.args)
                + list(args.kwonlyargs)
            ):
                out.append((path.name, n))
    return out


_MARKER_NAMES = {"SUBTOTAL_LEVEL_KEY"}
_MARKER_LITERALS = {"_subtotal_level"}
# mdx_execute reads the GRAIN marker for hierarchical ordinals, which is not the
# detail filter this guard is about; only the LEVEL marker names the filter.


def _marker_local_names(tree) -> set[str]:
    """Every local name bound to the detail marker in *tree*, including
    ``from ... import SUBTOTAL_LEVEL_KEY as _SL`` aliases.

    Deep-review R4 finding 3: matching a fixed identifier set left the
    import-alias spelling invisible, so a verbatim re-implementation written as
    ``r.get(_SL, "detail")`` reported ZERO offenders — the guard failed OPEN one
    shape over from the attribute form R3 had just closed. Resolve the aliases
    from the module's own imports instead of guessing the spellings.
    """
    names = set(_MARKER_NAMES)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for a in node.names:
                if a.name in _MARKER_NAMES and a.asname:
                    names.add(a.asname)
    return names


def _names_the_detail_marker(node, local_names: set[str] | None = None) -> bool:
    """A marker reference by CONSTANT, by NAME (incl. import alias), or by
    ATTRIBUTE.

    The literal spelling is the form that actually shipped in ``mdx_execute.py``
    until this lane replaced it with the imported constants, so a guard blind to
    it could not catch a regression to the very shape the lane just fixed
    (deep-review R2 finding 5). The ATTRIBUTE form
    (``subtotal_engine.SUBTOTAL_LEVEL_KEY``, or ``import ... as _se``) was a
    confirmed blind spot: deep-review R3 ran this matcher over a module that
    re-implements the filter that way and it reported ZERO offenders — the guard
    failed OPEN on a shape it did not recognise.
    """
    allowed = local_names if local_names is not None else _MARKER_NAMES
    if isinstance(node, ast.Name):
        return node.id in allowed
    if isinstance(node, ast.Attribute):
        return node.attr in _MARKER_NAMES
    return isinstance(node, ast.Constant) and node.value in _MARKER_LITERALS


def test_no_module_outside_subtotal_engine_reimplements_the_detail_filter():
    """Bug-8379's root cause was FIVE hand-copied ``r.get(SUBTOTAL_LEVEL_KEY,
    "detail") == "detail"`` filters that drifted. select_detail_rows is now the
    single home; this fails closed the moment a sixth copy appears anywhere under
    src/dax, in ANY module (the invariant spans xmla_server.py too, which a
    guard scoped to mdx_calc_members.py cannot see), spelled EITHER as the
    imported constant or as the raw ``"_subtotal_level"`` string, and reached
    either by ``.get(...)`` or by subscript.

    The presence test ``SUBTOTAL_LEVEL_KEY in r`` (an ``ast.Compare``) is not a
    filter and is deliberately not matched.
    """
    dax_dir, paths = _dax_source_files()
    offenders: list[str] = []
    for path in paths:
        if path.name == "subtotal_engine.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        local = _marker_local_names(tree)
        for node in ast.walk(tree):
            hit = (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and node.args
                and _names_the_detail_marker(node.args[0], local)
            ) or (
                isinstance(node, ast.Subscript)
                and _names_the_detail_marker(node.slice, local)
            )
            if hit:
                offenders.append(f"{path.relative_to(dax_dir)}:{node.lineno}")
    assert offenders == [], (
        "the detail-grain filter was re-implemented instead of calling "
        f"select_detail_rows / select_non_detail_rows at {offenders}"
    )


def test_every_requery_planner_scopes_rows_to_detail_before_reading_them():
    """Both re-query planners must rebind ``rows`` through ``select_detail_rows``
    before any other read of ``rows``.

    Bug-8323 fixed one planner with a per-use-site filter; the sibling planner
    kept its raw loops and became Bug-8379. Asserting the REBIND (not "a filter
    appears somewhere") is what makes a newly added loop safe by construction:
    after the rebind there is no merged-row binding left in scope to read.

    Reach of this guard (deep-review R1 finding 4d): it proves no read of the
    NAME ``rows`` precedes the rebind. Aliasing the parameter first
    (``merged = rows``) and reading the alias afterwards would pass — the
    sibling ``_eval_aggregate_set`` uses exactly that shape deliberately, so the
    alias form is not itself a defect, but a new planner must not use it to
    smuggle merged rows past the rebind.
    """
    planners = _planner_functions()
    assert {n.name for _mod, n in planners} >= {
        "plan_denominator_requeried", "plan_aggregate_requeried",
    }, "a known planner disappeared; re-derive this guard"
    for _mod, fn in planners:
        fn_name = f"{_mod}::{fn.name}"

        rebinds = [
            n.lineno for n in ast.walk(fn)
            if isinstance(n, ast.Assign)
            and any(getattr(t, "id", None) == "rows" for t in n.targets)
            and isinstance(n.value, ast.Call)
            and getattr(n.value.func, "id", None) == "select_detail_rows"
        ]
        assert rebinds, (
            f"{fn_name} does not rebind ``rows`` via select_detail_rows; it can "
            "derive re-query partitions from subtotal rows (Bug-8323/Bug-8379)"
        )
        first_rebind = min(rebinds)
        reads = [
            n.lineno for n in ast.walk(fn)
            if isinstance(n, ast.Name)
            and n.id == "rows"
            and isinstance(n.ctx, ast.Load)
            and n.lineno < first_rebind
        ]
        assert reads == [], (
            f"{fn_name} reads the MERGED ``rows`` at line(s) {reads}, before the "
            f"detail-scoping rebind at line {first_rebind}"
        )


def test_the_reimplementation_guard_itself_sees_every_marker_spelling():
    """Coverage-tool blind-spot audit (CLAUDE.md): the guard must be tested
    against the shapes it was NOT built for, not only the ones it was."""
    shapes = [
        'r.get(SUBTOTAL_LEVEL_KEY, "detail")',
        'r.get("_subtotal_level", "detail")',
        'r[SUBTOTAL_LEVEL_KEY]',
        'r["_subtotal_level"]',
        'r.get(subtotal_engine.SUBTOTAL_LEVEL_KEY, "detail")',
        'r.get(_se.SUBTOTAL_LEVEL_KEY, "detail")',
        ("from src.dax.subtotal_engine import SUBTOTAL_LEVEL_KEY as _SL\n"
         'r.get(_SL, "detail")'),
    ]
    for src in shapes:
        tree = ast.parse(src)
        _local = _marker_local_names(tree)
        hit = any(
            (
                isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr == "get"
                and n.args
                and _names_the_detail_marker(n.args[0], _local)
            ) or (
                isinstance(n, ast.Subscript)
                and _names_the_detail_marker(n.slice, _local)
            )
            for n in ast.walk(tree)
        )
        assert hit, f"the guard is blind to this re-implementation spelling: {src}"
    # And it must NOT fire on the legitimate presence test.
    presence = ast.parse("SUBTOTAL_LEVEL_KEY in r")
    assert not any(
        isinstance(n, ast.Subscript) and _names_the_detail_marker(n.slice)
        for n in ast.walk(presence)
    )


def test_the_planner_enumeration_guard_is_not_blind_to_private_or_renamed_planners():
    """Coverage-tool blind-spot audit. Discovery must key on the PROPERTY
    ("a planner that reads result rows"), not on one naming accident."""
    import ast as _ast
    src = (
        "def _plan_new_requeried(calc, rows):\n    return rows\n"
        "def plan_other(calc, merged_rows):\n    return merged_rows\n"
        "def plan_public(calc, rows):\n    return rows\n"
        "def helper(rows):\n    return rows\n"
    )
    found = []
    for n in _ast.walk(_ast.parse(src)):
        if not isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
            continue
        if not n.name.lstrip("_").startswith("plan_"):
            continue
        a = n.args
        if any(
            x.arg == "rows" or x.arg.endswith("_rows")
            for x in list(a.posonlyargs) + list(a.args) + list(a.kwonlyargs)
        ):
            found.append(n.name)
    assert sorted(found) == [
        "_plan_new_requeried", "plan_other", "plan_public",
    ], f"planner discovery has an enumeration blind spot: {found}"
