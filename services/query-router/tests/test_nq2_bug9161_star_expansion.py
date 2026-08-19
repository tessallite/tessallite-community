"""NQ-2/Bug-9161 corrected Phase 1 — star expansion + PHYSICAL wrong-number repro.

THE CORE GOTCHA: ``force_route="source"`` alone does NOT compile a star
definition's closure. ``rewrite/source_sql.rewrite_for_source`` short-circuits a
``SELECT *`` with ``select_star=True`` and no persona-narrowed star into
``_substitute_table_names`` — ``SELECT * FROM <anchor_table>``, a single physical
table with NO joins (probe: ``SELECT * FROM "demo"."fact_sales"``). A
``SELECT * FROM model`` Named Query therefore collapsed to the fact table only —
wrong columns, wrong rows.

THE FIX: expand a row-preserving star definition to an EXPLICIT column list of
the model's exposed (non-hidden) fields in NQ-land BEFORE compilation
(``shared/named_query/star_expansion.py``), so ``select_star`` is False and the
source route falls through to ``_build_source_sql`` — the definition-scoped
closure with the model's DECLARED join types. Plain measures bind as
measure-as-dimension (raw detail columns, no aggregation — probe-verified);
calculated/variant measures have no detail-level rendering and are not expanded.

THE PHYSICAL REPRO (test_star_nq_yields_inner_join_closure_rows):
fact(3 rows) INNER dim(2 matching). A star NQ must yield 2 rows (INNER closure)
with dimension attributes, in BOTH the refresh-build compile (the /explain
canonical body) and the live serve compile (the central live helper's canonical
ExecuteRequest). The raw star compiles to the fact-only collapse (3 rows, no dim
attributes) — executing THAT is the wrong number. If the star-expansion is
removed, the expanded definition degenerates to the raw star and the 2-row
assertion fails (the test names the collapse explicitly).
"""
from __future__ import annotations

import sqlite3
import types
import uuid
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

if TYPE_CHECKING:
    from src.semantic.snapshot_resolver import DeployedShape

from shared.named_query import refresh as _refresh
from shared.named_query.population_contract import (
    NQ_POPULATION_CONTRACT_VERSION,
    named_query_population_fingerprint,
    named_query_population_manifest_matches,
)
from shared.named_query.star_expansion import (
    expand_named_query_star_definition,
    is_expandable_star_definition,
    is_row_preserving_star_definition,
)

pytestmark = pytest.mark.unit

_MODEL_ID = uuid.uuid4()
_VERSION_ID = uuid.uuid4()
_NQ_ID = uuid.uuid4()


# ---------------------------------------------------------------------------
# Star-expansion unit contract
# ---------------------------------------------------------------------------

def _snapshot(*, with_hidden: bool = False, with_calc: bool = False) -> dict:
    dims = [
        {"id": "d-sale", "name": "sale_id", "source_column_id": "c-sale"},
        {"id": "d-month", "name": "month_name", "source_column_id": "c-month"},
    ]
    measures = [
        {"id": "m-amount", "name": "amount", "measure_type": "standard",
         "variant_kind": None, "source_column_id": "c-amount"},
    ]
    cols = [
        {"id": "c-sale", "model_table_id": "t-fact", "column_name": "sale_id",
         "is_hidden": False},
        {"id": "c-month", "model_table_id": "t-dim", "column_name": "month_name",
         "is_hidden": False},
        {"id": "c-amount", "model_table_id": "t-fact", "column_name": "amount",
         "is_hidden": False},
    ]
    if with_hidden:
        dims.append({"id": "d-secret", "name": "secret_col",
                     "source_column_id": "c-secret"})
        measures.append({"id": "m-secret", "name": "secret_measure",
                         "measure_type": "standard", "variant_kind": None,
                         "source_column_id": "c-secret"})
        cols.append({"id": "c-secret", "model_table_id": "t-fact",
                     "column_name": "secret_col", "is_hidden": True})
    if with_calc:
        measures.append({"id": "m-profit", "name": "profit",
                         "measure_type": "calculated", "variant_kind": None,
                         "source_column_id": None,
                         "expression": "SUM(amount) - SUM(cost)"})
    return {
        "named_queries": [],
        "dimensions": dims,
        "measures": measures,
        "columns": cols,
        "tables": [
            {"id": "t-fact", "physical_name": "demo.fact_sales", "alias": "fs"},
            {"id": "t-dim", "physical_name": "demo.dim_dates", "alias": "dd"},
        ],
    }


def test_genuine_star_expands_to_explicit_exposed_fields() -> None:
    expanded = expand_named_query_star_definition(
        "SELECT * FROM modely", _snapshot(),
    )
    assert expanded == (
        'SELECT "sale_id", "month_name", "amount" FROM modely'
    )
    # Deterministic: the same snapshot + definition always expands the same.
    assert (
        expand_named_query_star_definition("SELECT * FROM modely", _snapshot())
        == expanded
    )


def test_non_star_shapes_pass_through_unchanged() -> None:
    shapes = [
        'SELECT "branch_id" FROM modely',                      # explicit projection
        "SELECT branch_id, COUNT(*) AS n FROM modely GROUP BY 1",  # aggregated
        "SELECT * FROM modely JOIN other ON 1=1",              # joined
        "WITH x AS (SELECT 1) SELECT * FROM x",                # CTE
    ]
    for sql in shapes:
        assert expand_named_query_star_definition(sql, _snapshot()) == sql


def test_limited_and_distinct_stars_expand_and_preserve_their_clauses() -> None:
    """NQ2C-F2: LIMIT / OFFSET / DISTINCT are population-irrelevant — a
    ``SELECT * FROM model LIMIT n`` still means "the model's exposed fields" —
    so the WIDE population predicate expands them and the expansion preserves
    the clauses (it only replaces ``ast.args["expressions"]``)."""
    assert expand_named_query_star_definition(
        "SELECT * FROM modely LIMIT 5", _snapshot(),
    ) == 'SELECT "sale_id", "month_name", "amount" FROM modely LIMIT 5'
    assert expand_named_query_star_definition(
        "SELECT * FROM modely LIMIT 5 OFFSET 2", _snapshot(),
    ) == 'SELECT "sale_id", "month_name", "amount" FROM modely LIMIT 5 OFFSET 2'
    assert expand_named_query_star_definition(
        "SELECT DISTINCT * FROM modely", _snapshot(),
    ) == 'SELECT DISTINCT "sale_id", "month_name", "amount" FROM modely'


def test_security_and_population_predicates_disagree_on_limit_distinct_functions() -> None:
    """NQ2C-F6: the RLS security predicate must stay NARROW (it underwrites a
    security proof — a DISTINCT/LIMIT definition stays materialised-INELIGIBLE
    under RLS) while the population predicate ACCEPTS the same shapes. A future
    merge of the two fails this test."""
    wide_only = [
        "SELECT * FROM modely LIMIT 1",
        "SELECT * FROM modely LIMIT 1 OFFSET 1",
        "SELECT DISTINCT * FROM modely",
        "SELECT * FROM modely WHERE MYFUNC(region) = 'EU'",
    ]
    for defn in wide_only:
        assert is_expandable_star_definition(defn), defn
        assert not is_row_preserving_star_definition(defn), defn
    agree = [
        "SELECT * FROM modely",
        "SELECT * FROM modely WHERE region = 'EU'",
    ]
    for defn in agree:
        assert is_expandable_star_definition(defn), defn
        assert is_row_preserving_star_definition(defn), defn
    neither = [
        "SELECT branch_id FROM modely",
        "SELECT * FROM modely JOIN other ON 1=1",
        "SELECT * FROM modely GROUP BY 1",
        "SELECT * FROM modely HAVING COUNT(*) > 1",
    ]
    for defn in neither:
        assert not is_expandable_star_definition(defn), defn
        assert not is_row_preserving_star_definition(defn), defn


def test_allowed_fields_narrows_the_expansion() -> None:
    """NQ2C-F1 mechanism: the serve handler passes the persona/CLS-permitted
    subset as ``allowed_fields``; the expansion projects ONLY those fields.
    Non-star definitions ignore the filter (the ordinary gates decide), and an
    empty narrowing raises the expansion's ValueError (the handler pre-empts
    this with the CLS-style 403)."""
    assert expand_named_query_star_definition(
        "SELECT * FROM modely", _snapshot(), allowed_fields={"month_name"},
    ) == 'SELECT "month_name" FROM modely'
    assert expand_named_query_star_definition(
        "SELECT DISTINCT * FROM modely", _snapshot(),
        allowed_fields={"month_name"},
    ) == 'SELECT DISTINCT "month_name" FROM modely'
    assert expand_named_query_star_definition(
        "SELECT branch_id FROM modely", _snapshot(), allowed_fields=set(),
    ) == "SELECT branch_id FROM modely"
    with pytest.raises(ValueError):
        expand_named_query_star_definition(
            "SELECT * FROM modely", _snapshot(), allowed_fields=set(),
        )


def test_filter_only_star_expands_and_preserves_the_where() -> None:
    expanded = expand_named_query_star_definition(
        "SELECT * FROM modely WHERE is_active = true", _snapshot(),
    )
    assert expanded == (
        'SELECT "sale_id", "month_name", "amount" FROM modely '
        "WHERE is_active = TRUE"
    )


def test_hidden_fields_are_excluded_from_the_expansion() -> None:
    expanded = expand_named_query_star_definition(
        "SELECT * FROM modely", _snapshot(with_hidden=True),
    )
    assert "secret" not in expanded.lower()
    assert expanded == 'SELECT "sale_id", "month_name", "amount" FROM modely'


def test_calculated_measures_are_not_expanded() -> None:
    # A calculated measure has no detail-level column (aggregation-only); every
    # existing star rendering omits it, and including it would push the compile
    # onto the aggregated measure path (different row population).
    expanded = expand_named_query_star_definition(
        "SELECT * FROM modely", _snapshot(with_calc=True),
    )
    assert "profit" not in expanded
    assert expanded == 'SELECT "sale_id", "month_name", "amount" FROM modely'


def test_star_over_a_model_with_no_exposed_fields_raises() -> None:
    with pytest.raises(ValueError, match="no non-hidden field"):
        expand_named_query_star_definition(
            "SELECT * FROM modely", {"dimensions": [], "measures": [], "columns": []},
        )


def test_row_preserving_predicate_classification() -> None:
    assert is_row_preserving_star_definition("SELECT * FROM modely")
    assert is_row_preserving_star_definition(
        "SELECT * FROM modely WHERE region = 'EU'"
    )
    assert not is_row_preserving_star_definition("SELECT * FROM modely LIMIT 1")
    assert not is_row_preserving_star_definition(
        "SELECT branch_id FROM modely"
    )
    assert not is_row_preserving_star_definition(
        "SELECT * FROM modely GROUP BY 1"
    )
    assert not is_row_preserving_star_definition(
        "WITH x AS (SELECT 1) SELECT * FROM x"
    )


# ---------------------------------------------------------------------------
# Population-contract manifest gate
# ---------------------------------------------------------------------------

def _fp(definition_sql: str = 'SELECT "a" FROM m') -> str:
    return named_query_population_fingerprint(
        model_id=_MODEL_ID,
        named_query_id=_NQ_ID,
        deployed_version_id=_VERSION_ID,
        deploy_epoch=3,
        definition_sql=definition_sql,
    )


def test_manifest_matches_requires_all_three_proofs() -> None:
    fp = _fp()
    good = {
        "manifest_version": 2,
        "build_refresh_run_id": "run-9",
        "row_definition_fingerprint": fp,
    }
    assert named_query_population_manifest_matches(
        manifest=good, active_refresh_run_id="run-9", expected_fingerprint=fp,
    )
    # Missing manifest / None expected -> never matches (legacy artifact).
    assert not named_query_population_manifest_matches(
        manifest=None, active_refresh_run_id="run-9", expected_fingerprint=fp,
    )
    assert not named_query_population_manifest_matches(
        manifest=good, active_refresh_run_id="run-9",
        expected_fingerprint=None,
    )
    # Superseded manifest version.
    assert not named_query_population_manifest_matches(
        manifest={**good, "manifest_version": 1},
        active_refresh_run_id="run-9", expected_fingerprint=fp,
    )
    # Not bound to the artifact's live build.
    assert not named_query_population_manifest_matches(
        manifest=good, active_refresh_run_id="run-10", expected_fingerprint=fp,
    )
    # Wrong fingerprint (definition edited after the build).
    assert not named_query_population_manifest_matches(
        manifest={**good, "row_definition_fingerprint": _fp('SELECT "b" FROM m')},
        active_refresh_run_id="run-9", expected_fingerprint=fp,
    )


# ---------------------------------------------------------------------------
# PHYSICAL wrong-number reproduction — fact(3) INNER dim(2) -> star NQ = 2 rows
# ---------------------------------------------------------------------------

def _physical_snapshot() -> dict:
    return {
        "named_queries": [
            {
                "id": str(_NQ_ID),
                "name": "star_nq",
                "definition_sql": "SELECT * FROM modely",
                "shape": "projection",
            },
        ],
        "dimensions": [
            {"id": "d-sale", "name": "sale_id", "source_column_id": "c-sale"},
            {"id": "d-month", "name": "month_name", "source_column_id": "c-month"},
        ],
        "measures": [
            {"id": "m-amount", "name": "amount", "measure_type": "standard",
             "variant_kind": None, "source_column_id": "c-amount"},
        ],
        "columns": [
            {"id": "c-sale", "model_table_id": "t-fact",
             "column_name": "sale_id", "is_hidden": False},
            {"id": "c-amount", "model_table_id": "t-fact",
             "column_name": "amount", "is_hidden": False},
            {"id": "c-date", "model_table_id": "t-fact",
             "column_name": "date_id", "is_hidden": False},
            {"id": "c-month", "model_table_id": "t-dim",
             "column_name": "month_name", "is_hidden": False},
            {"id": "c-dimdate", "model_table_id": "t-dim",
             "column_name": "date_id", "is_hidden": False},
        ],
        "tables": [
            {"id": "t-fact", "physical_name": "demo.fact_sales", "alias": "fs",
             "table_type": "fact", "model_id": str(_MODEL_ID)},
            {"id": "t-dim", "physical_name": "demo.dim_dates", "alias": "dd",
             "table_type": "dimension", "model_id": str(_MODEL_ID)},
        ],
        "joins": [
            {"id": "j-1", "model_id": str(_MODEL_ID),
             "left_table_id": "t-fact", "right_table_id": "t-dim",
             "left_column_id": "c-date", "right_column_id": "c-dimdate",
             "join_type": "inner"},
        ],
    }


def _physical_shape() -> "DeployedShape":
    from src.semantic.snapshot_resolver import DeployedShape

    snap = _physical_snapshot()

    def _row(cls, row: dict):
        valid = {c.name for c in cls.__table__.columns}
        return cls(**{k: v for k, v in row.items() if k in valid})

    from shared.db.models import Dimension, Measure

    dims = [_row(Dimension, d) for d in snap["dimensions"]]
    measures = [_row(Measure, m) for m in snap["measures"]]
    return DeployedShape(
        measures=measures,
        dimensions=dims,
        hidden_column_ids=set(),
        physical_columns_all={"sale_id", "amount", "date_id", "month_name"},
        physical_columns_visible={"sale_id", "amount", "date_id", "month_name"},
        hierarchy_rows=[],
        physical_column_ids={},
        attribute_relationships=[],
        dimensions_by_id={str(d.id): d for d in dims},
        columns_by_id={str(c["id"]): dict(c) for c in snap["columns"]},
        tables_by_id={str(t["id"]): dict(t) for t in snap["tables"]},
        join_rows=list(snap["joins"]),
        user_defined_attribute_rows=[],
        qualified_column_ids={},
        table_name_ids={},
    )


async def _compile_definition_sql(definition_sql: str) -> str:
    """Bind + source-rewrite a definition over the physical model, exactly the
    primitives the /explain canonical body and the live ExecuteRequest compile
    with (protocol jdbc, dialect postgres, include_hidden False, force_route
    source)."""
    from src.parsing.sql_parser import parse_sql_to_ir
    from src.rewrite.query_rewriter import rewrite_for_source
    from src.semantic.binder import bind_query_to_model

    model = types.SimpleNamespace(
        id=_MODEL_ID, slug="modely", display_name="modely",
        deployed_version_id=_VERSION_ID, deploy_epoch=3,
    )
    lq = parse_sql_to_ir(definition_sql, str(_MODEL_ID))
    db = types.SimpleNamespace()

    async def _shape(*a, **k):
        return _physical_shape()

    with (
        patch("src.semantic.binder._load_model",
              new=AsyncMock(return_value=model)),
        patch("src.semantic.binder.resolve_deployed_shape",
              new=AsyncMock(side_effect=_shape)),
    ):
        bound = await bind_query_to_model(lq, db)
    return await rewrite_for_source(bound, db, target_dialect="postgres")


def _physical_tables() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("ATTACH DATABASE ':memory:' AS demo")
    conn.execute(
        'CREATE TABLE "demo"."fact_sales" '
        "(sale_id TEXT, amount REAL, date_id TEXT)"
    )
    conn.execute(
        'CREATE TABLE "demo"."dim_dates" (date_id TEXT, month_name TEXT)'
    )
    # 3 fact rows; only d1 and d2 have a matching dimension row (lossy INNER).
    conn.execute(
        "INSERT INTO \"demo\".\"fact_sales\" VALUES "
        "('s1', 10.0, 'd1'), ('s2', 20.0, 'd2'), ('s3', 30.0, 'd3')"
    )
    conn.execute(
        "INSERT INTO \"demo\".\"dim_dates\" VALUES "
        "('d1', 'Jan'), ('d2', 'Feb')"
    )
    return conn


def _exec(sql: str, conn: sqlite3.Connection):
    cur = conn.execute(sql)
    cols = [d[0] for d in cur.description]
    return cols, [tuple(r) for r in cur.fetchall()]


def test_physical_repro_star_nq_yields_inner_join_closure_rows() -> None:
    """THE Bug-9161 corrected-Phase-1 repro.

    fact(3) INNER dim(2): a star Named Query must yield the 2 matching rows
    WITH dimension attributes — in BOTH the refresh-build compile and the live
    serve compile (identical canonical bodies over the identical EXPANDED
    deployed definition -> byte-identical SQL). The UNEXPANDED star compiles to
    the fact-only collapse (3 rows, no dimension attributes) — executing THAT
    is exactly the wrong number the fix eliminates. If the star-expansion is
    removed, the expanded definition degenerates to the raw star and the 2-row
    assertion fails.
    """
    raw_star_sql = None
    # 1. The collapse: an UNEXPANDED star definition is the fact-only raw star.
    import asyncio

    async def _compile_raw_star():
        return await _compile_definition_sql("SELECT * FROM modely")

    collapse = asyncio.run(_compile_raw_star())
    assert collapse.upper().startswith('SELECT * FROM "DEMO"."FACT_SALES"'), collapse
    assert "JOIN" not in collapse.upper()

    # 2. THE FIX: the expanded deployed definition is the explicit projection.
    expanded = expand_named_query_star_definition(
        "SELECT * FROM modely", _physical_snapshot(),
    )
    assert expanded == 'SELECT "sale_id", "month_name", "amount" FROM modely'

    # 3. Refresh build compile == live serve compile == the INNER-join closure.
    build_sql = asyncio.run(_compile_definition_sql(expanded))
    live_sql = asyncio.run(_compile_definition_sql(expanded))
    assert build_sql == live_sql
    up = build_sql.upper()
    assert 'INNER JOIN "DEMO"."DIM_DATES"' in up, build_sql
    assert "GROUP BY" not in up, "star population must stay detail-level"

    # 4. Execute the closure against the physical tables: 2 rows, dim attrs.
    conn = _physical_tables()
    collapse_cols, collapse_rows = _exec(collapse, conn)
    closure_cols, closure_rows = _exec(build_sql, conn)
    # The collapse returns all 3 fact rows and NO dimension attribute — the
    # wrong number/columns the fix eliminates.
    assert len(collapse_rows) == 3
    assert "month_name" not in {c.lower() for c in collapse_cols}
    # The canonical closure returns the 2 INNER-matching rows WITH the dim attr.
    assert len(closure_rows) == 2, f"expected 2 INNER rows, got {closure_rows}"
    assert "month_name" in {c.lower() for c in closure_cols}
    months = {row[closure_cols.index("month_name")] for row in closure_rows}
    assert months == {"Jan", "Feb"}


@pytest.mark.parametrize("defn", [
    "SELECT * FROM modely LIMIT 100",
    "SELECT DISTINCT * FROM modely",
])
def test_nq2c_f2_limited_and_distinct_star_still_reach_the_join_closure(defn) -> None:
    """NQ2C-F2: a star definition carrying LIMIT/DISTINCT must expand to the
    definition-scoped closure (the exposed semantic projection over the
    DECLARED joins). Pre-fix it passed through unchanged and compiled to the
    anchor-only collapse — the Bug-9161 wrong population, 3 rows and no
    dimension attribute."""
    import asyncio

    expanded = expand_named_query_star_definition(defn, _physical_snapshot())
    assert '"month_name"' in expanded, expanded
    sql = asyncio.run(_compile_definition_sql(expanded))
    up = sql.upper()
    assert 'INNER JOIN "DEMO"."DIM_DATES"' in up, sql
    assert not up.startswith('SELECT * FROM "DEMO"."FACT_SALES"'), sql
    conn = _physical_tables()
    _, rows = _exec(sql, conn)
    assert len(rows) == 2, f"expected the 2 INNER-closure rows, got {rows}"


# ---------------------------------------------------------------------------
# NQ2C-F1 — restricted readers narrow, never 403
# ---------------------------------------------------------------------------

def test_nq2c_f1_persona_allow_list_parity() -> None:
    """NQ2C-F1: under a persona allow-list the raw star NARROWS silently, but
    the EXPANDED full projection takes persona_gate's DENY branch (403) — the
    flip the fix exists to prevent. The NQ narrowing must therefore yield the
    persona-permitted subset, and THAT subset must pass the REAL
    ``enforce_persona`` without raising."""
    import types as _t

    from fastapi import HTTPException
    from src.security.persona_gate import enforce_persona

    d_ok, d_no = "d-month", "d-sale"
    m_amount = "m-amount"
    persona = _t.SimpleNamespace(
        id=uuid.uuid4(), name="restricted", included_measure_ids=[],
        included_dimension_ids=[d_ok], included_hierarchy_ids=[],
        default_filters={},
    )

    def _bound(select_star: bool, dims: list, measures: list):
        return _t.SimpleNamespace(
            logical_query=_t.SimpleNamespace(
                select_star=select_star, has_complex_sql=False,
            ),
            resolved_dimensions=[
                _t.SimpleNamespace(
                    id=_id, name=name, hierarchy_id=None,
                    source_column_id=None, user_defined_attribute_id=None,
                )
                for _id, name in dims
            ],
            resolved_measures=[
                _t.SimpleNamespace(id=_id, name=name)
                for _id, name in measures
            ],
            persona_narrowed_star=False,
        )

    # 1. Star -> narrowed silently (the behaviour BI tools rely on).
    star = _bound(True, [(d_ok, "month_name"), (d_no, "sale_id")],
                  [(m_amount, "amount")])
    enforce_persona(persona, star, None)
    assert [d.name for d in star.resolved_dimensions] == ["month_name"]

    # 2. The EXPANDED full projection -> DENY branch -> 403. This is the
    #    regression the fix removes: without pre-narrowing, every restricted
    #    reader of a star NQ got exactly this.
    expanded = _bound(False, [(d_ok, "month_name"), (d_no, "sale_id")],
                      [(m_amount, "amount")])
    with pytest.raises(HTTPException) as exc_info:
        enforce_persona(persona, expanded, None)
    assert exc_info.value.status_code == 403

    # 3. The NQ narrowing helper mirrors the gate over the SAME exposed rows.
    #    The measure is dropped too: a projected plain measure binds as a
    #    measure-as-dimension and the dimension allow-list's deny branch would
    #    403 it (its id is not in the dimension allow-list) — the narrowing
    #    must never produce a body the real gate denies.
    from src.api.routes import _named_query_allowed_star_fields

    allowed = _named_query_allowed_star_fields(
        persona=persona,
        snapshot=_snapshot(),
        persona_allow_lists=True,
        restricted_column_ids=set(),
    )
    assert allowed == {"month_name"}

    # 4. The NARROWED projection passes the REAL gate without raising.
    narrowed = _bound(False, [(d_ok, "month_name")], [])
    enforce_persona(persona, narrowed, None)


def test_nq2c_f1_hierarchy_allow_list_narrows_too() -> None:
    """NQ2C-F1 hierarchy half: ``included_hierarchy_ids`` narrows the exposed
    set exactly like enforce_persona's hierarchy star branch — hierarchy-less
    dims are kept, hierarchy dims not on the list are dropped, measures are
    unaffected."""
    import types as _t

    from src.api.routes import _named_query_allowed_star_fields

    persona = _t.SimpleNamespace(
        id=uuid.uuid4(), name="hier", included_measure_ids=[],
        included_dimension_ids=[], included_hierarchy_ids=["h-1"],
        default_filters={},
    )
    snapshot = {
        "dimensions": [
            {"id": "d-region", "name": "region", "hierarchy_id": "h-1",
             "source_column_id": "c-region"},
            {"id": "d-branch", "name": "branch_id", "hierarchy_id": None,
             "source_column_id": "c-branch"},
        ],
        "measures": [
            {"id": "m-amount", "name": "amount", "measure_type": "standard",
             "variant_kind": None, "source_column_id": "c-amount"},
        ],
        "columns": [
            {"id": "c-region", "column_name": "region", "is_hidden": False},
            {"id": "c-branch", "column_name": "branch_id", "is_hidden": False},
            {"id": "c-amount", "column_name": "amount", "is_hidden": False},
        ],
    }
    allowed = _named_query_allowed_star_fields(
        persona=persona,
        snapshot=snapshot,
        persona_allow_lists=True,
        restricted_column_ids=set(),
    )
    # h-1's region stays; the hierarchy-less branch dim stays; the measure is
    # unaffected by the hierarchy list.
    assert allowed == {"region", "branch_id", "amount"}

    persona_other = _t.SimpleNamespace(
        id=uuid.uuid4(), name="hier", included_measure_ids=[],
        included_dimension_ids=[], included_hierarchy_ids=["h-other"],
        default_filters={},
    )
    allowed_other = _named_query_allowed_star_fields(
        persona=persona_other,
        snapshot=snapshot,
        persona_allow_lists=True,
        restricted_column_ids=set(),
    )
    # region (hierarchy h-1) is not on the allow-list -> dropped.
    assert allowed_other == {"branch_id", "amount"}


def test_nq2c_f1_cls_data_tag_parity() -> None:
    """NQ2C-F1 second instance: under CLS data tags, a NON-star projection
    populates ``blocked`` (the caller 403s) where a star narrows. The NQ
    narrowing must exclude every restricted-closure field, and the narrowed
    projection must then pass the REAL ``_check_column_restrictions`` with an
    empty blocked list."""
    import types as _t

    from src.api.routes import _named_query_allowed_star_fields

    persona = _t.SimpleNamespace(
        id=uuid.uuid4(), name="tagged", included_measure_ids=[],
        included_dimension_ids=[], included_hierarchy_ids=[],
        default_filters={},
    )
    snapshot = _snapshot()
    restricted_ids = {"c-sale"}  # the sale_id column is tag-restricted

    allowed = _named_query_allowed_star_fields(
        persona=persona,
        snapshot=snapshot,
        persona_allow_lists=False,
        restricted_column_ids=restricted_ids,
    )
    # sale_id is dropped (its source column is restricted); month_name and the
    # plain amount measure stay.
    assert allowed == {"month_name", "amount"}

    # The narrowed projection passes the REAL runtime CLS check: no blocked
    # columns -> no 403. (The full projection would block sale_id — the flip
    # proven end-to-end by the decision-matrix real-_handle_execute CLS cell.)
    import asyncio

    from src.routing.router import _check_column_restrictions

    async def _run():
        bound = _t.SimpleNamespace(
            logical_query=_t.SimpleNamespace(
                select_star=False, has_complex_sql=False,
            ),
            resolved_measures=[
                _t.SimpleNamespace(
                    id="m-amount", name="amount", measure_type="standard",
                    source_column_id="c-amount", display_column_id=None,
                    user_defined_attribute_id=None, variant_of_measure_id=None,
                    expression=None, calc_expression=None,
                ),
            ],
            resolved_dimensions=[
                _t.SimpleNamespace(
                    id="d-month", name="month_name", hierarchy_id=None,
                    source_column_id="c-month", display_column_id=None,
                    user_defined_attribute_id=None, calc_expression=None,
                ),
            ],
            resolved_filters=[],
            persona_narrowed_star=False,
        )

        class _ScalarsAll:
            def __init__(self, rows):
                self._rows = list(rows)

            def scalars(self):
                return self

            def all(self):
                return self._rows

        db = AsyncMock()
        db.execute = AsyncMock(side_effect=[
            _ScalarsAll([_t.SimpleNamespace(data_tag_id="tag-1")]),
            _ScalarsAll(["c-sale"]),
        ])
        return await _check_column_restrictions(bound, persona, db)

    blocked = asyncio.run(_run())
    assert blocked == []


def test_nq2c_f1_empty_narrowing_reproduces_the_cls_403_not_the_value_error() -> None:
    """The empty-narrowed case must reproduce the EXISTING CLS 403
    (router's star-fully-restricted shape), not the expansion's ValueError —
    asserted at the helper level: every exposed field restricted -> empty set,
    and the handler turns that into the OBJECT_NOT_AVAILABLE 403."""
    import types as _t

    from src.api.routes import _named_query_allowed_star_fields

    persona = _t.SimpleNamespace(
        id=uuid.uuid4(), name="tagged", included_measure_ids=[],
        included_dimension_ids=[], included_hierarchy_ids=[],
        default_filters={},
    )
    snapshot = _snapshot()
    allowed = _named_query_allowed_star_fields(
        persona=persona,
        snapshot=snapshot,
        persona_allow_lists=False,
        restricted_column_ids={"c-sale", "c-month", "c-amount"},
    )
    assert allowed == set()


def test_physical_repro_build_and_live_send_the_same_expanded_definition() -> None:
    """End-to-end body-level proof: the refresh build's /explain body and the
    live helper's ExecuteRequest both carry the EXPANDED deployed definition
    with the canonical options — byte-identical inputs on both sides."""
    expanded = expand_named_query_star_definition(
        "SELECT * FROM modely", _physical_snapshot(),
    )

    # Build side: capture the /explain body the refresh posts.
    class _FakeResp:
        status_code = 200

        def json(self):
            return {"route_type": "source", "rewritten_query": "SELECT 1",
                    "security_rules_applied": []}

    class _FakeClient:
        captured = {}

        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            _FakeClient.captured = dict(json or {})
            return _FakeResp()

    import asyncio

    async def _build_body():
        with patch.object(_refresh.httpx, "AsyncClient", _FakeClient):
            await _refresh._get_rewritten_sql(
                _MODEL_ID, expanded, "bearer",
            )
        return _FakeClient.captured

    build_body = asyncio.run(_build_body())
    assert build_body["raw_query"] == expanded
    assert build_body["force_route"] == "source"

    # Live side: drive the REAL central live helper with a HOSTILE outer
    # reference (raw classification + include_hidden + foreign dialect +
    # session vars + caption dimensions) — the compiled body must still be
    # canonical and the EXPANDED definition.
    from src.api import routes as _routes

    live_response = _routes.ExecuteResponse(
        rows=[], columns=[], route_type="source", reason="",
        aggregate_id=None, execution_ms=1, bytes_processed=0, rows_returned=0,
    )
    exec_mock = AsyncMock(return_value=live_response)
    body = _routes.ExecuteRequest(
        model_id=str(_MODEL_ID),
        raw_query="SELECT * FROM @star_nq",
        protocol="dax",
        dialect="bigquery",
        include_hidden=True,
        force_route="raw",
        session_vars={"app.x": "1"},
        caption_dimensions=["month_name"],
    )
    nq = types.SimpleNamespace(name="star_nq")

    with patch.object(_routes, "_handle_execute", new=exec_mock):
        response = asyncio.run(
            _routes._execute_named_query_live(
                AsyncMock(), body, nq, expanded,
                persona=None, principal=None,
                user_identity="u", tenant_id="t",
                skip_reason="no_artifact",
            )
        )
    assert response.route_type == "source"
    sent: _routes.ExecuteRequest = exec_mock.await_args.args[0]
    assert sent.raw_query == expanded
    assert sent.force_route == "source"
    assert sent.protocol == "jdbc"
    assert sent.dialect == "postgres"
    assert sent.include_hidden is False
    assert sent.session_vars is None
    assert sent.caption_dimensions is None
    assert sent.model_id == str(_MODEL_ID)


def test_live_helper_asserts_the_source_route() -> None:
    """The route_type=="source" assertion is load-bearing: a non-source live
    compile would silently serve a different population than the artifact.
    NQ2C-F8: it fails closed with a TYPED 422 (``named_query_route_invariant``),
    never a bare RuntimeError surfacing as an unclassified 500, and never a
    live fallback."""
    from fastapi import HTTPException

    from src.api import routes as _routes

    non_source = _routes.ExecuteResponse(
        rows=[], columns=[], route_type="aggregate", reason="",
        aggregate_id=None, execution_ms=1, bytes_processed=0, rows_returned=0,
    )
    exec_mock = AsyncMock(return_value=non_source)
    body = _routes.ExecuteRequest(model_id=str(_MODEL_ID), raw_query="x")
    nq = types.SimpleNamespace(name="star_nq")

    with patch.object(_routes, "_handle_execute", new=exec_mock):
        with pytest.raises(HTTPException) as exc_info:
            import asyncio

            asyncio.run(
                _routes._execute_named_query_live(
                    AsyncMock(), body, nq, "SELECT 1",
                    persona=None, principal=None,
                    user_identity="u", tenant_id="t",
                    skip_reason="no_artifact",
                )
            )
    assert exc_info.value.status_code == 422
    assert exc_info.value.detail["error_type"] == "named_query_route_invariant"
    assert "route_type" not in str(exc_info.value.detail)


# ---------------------------------------------------------------------------
# Deployed-definition authority (build must use the DEPLOYED row, not the live)
# ---------------------------------------------------------------------------

def test_load_deployed_definition_uses_snapshot_not_live_row() -> None:
    """Deploy def A, edit the live row to B without deploying: the refresh
    build must read A from the deployed snapshot (governed content — invariant
    7), never the live ORM row."""
    import asyncio

    from shared.named_query.refresh import _load_deployed_named_query_definition

    snapshot = {
        "named_queries": [
            {
                "id": str(_NQ_ID),
                "name": "star_nq",
                "definition_sql": "SELECT * FROM modely",  # A (deployed)
                "shape": "projection",
                "row_cap": 500,
                "column_cap": 50,
            },
        ],
        "dimensions": [
            {"id": "d-1", "name": "branch_id", "source_column_id": "c-1"},
        ],
        "measures": [],
        "columns": [
            {"id": "c-1", "model_table_id": "t-1",
             "column_name": "branch_id", "is_hidden": False},
        ],
        "tables": [
            {"id": "t-1", "physical_name": "demo.sales", "alias": "sales"},
        ],
    }
    model = types.SimpleNamespace(
        id=_MODEL_ID, deployed_version_id=_VERSION_ID,
    )
    version = types.SimpleNamespace(
        id=_VERSION_ID, model_id=_MODEL_ID, snapshot_json=snapshot,
    )
    live_nq = types.SimpleNamespace(
        id=_NQ_ID, name="star_nq",
        definition_sql="SELECT * FROM modely WHERE edited = 1",  # B (live draft)
        row_cap=999,
    )
    db = AsyncMock()
    db.get = AsyncMock(return_value=version)

    _, deployed = asyncio.run(
        _load_deployed_named_query_definition(db, model, live_nq)
    )
    assert deployed.definition_sql == "SELECT * FROM modely"  # A, not B
    assert deployed.row_cap == 500  # deployed governance, not the live 999


def test_load_deployed_definition_fails_closed_when_undeployed_or_absent() -> None:
    import asyncio

    from shared.named_query.refresh import _load_deployed_named_query_definition

    model = types.SimpleNamespace(id=_MODEL_ID, deployed_version_id=None)
    live_nq = types.SimpleNamespace(id=_NQ_ID, name="star_nq")
    db = AsyncMock()
    with pytest.raises(ValueError, match="not deployed"):
        asyncio.run(
            _load_deployed_named_query_definition(db, model, live_nq)
        )

    # Deployed model, but the NQ id is not part of the deployed snapshot.
    snapshot = {
        "named_queries": [],
        "dimensions": [
            {"id": "d-1", "name": "branch_id", "source_column_id": "c-1"},
        ],
        "measures": [],
        "columns": [
            {"id": "c-1", "model_table_id": "t-1",
             "column_name": "branch_id", "is_hidden": False},
        ],
        "tables": [
            {"id": "t-1", "physical_name": "demo.sales", "alias": "sales"},
        ],
    }
    model2 = types.SimpleNamespace(
        id=_MODEL_ID, deployed_version_id=_VERSION_ID,
    )
    version = types.SimpleNamespace(
        id=_VERSION_ID, model_id=_MODEL_ID, snapshot_json=snapshot,
    )
    db2 = AsyncMock()
    db2.get = AsyncMock(return_value=version)
    with pytest.raises(ValueError, match="not part of the model's deployed"):
        asyncio.run(
            _load_deployed_named_query_definition(db2, model2, live_nq)
        )


def test_nq2c_f3_snapshot_loads_by_the_captured_build_pointer() -> None:
    """NQ2C-F3 / Bug-8412 ordering: the refresh build captures the deployed
    pointer FIRST, then loads the snapshot BY the captured version id — so the
    manifest stamp and the compiled content provably come from ONE pointer
    read. A deploy committing between the model-row read and the capture (the
    ORM row now naming a NEWER version than the capture) must not matter: the
    definition is read from the CAPTURED version's snapshot."""
    import asyncio

    from shared.db.models import ModelVersion
    from shared.named_query.refresh import _load_deployed_named_query_definition

    captured_version = types.SimpleNamespace(
        id=_VERSION_ID, model_id=_MODEL_ID, snapshot_json=_snapshot_single_field(),
    )
    newer_version_id = uuid.uuid4()
    # t1 state: the ORM row read before the capture names a NEWER deployment
    # than the capture (the Bug-9161 build's exact skew).
    model = types.SimpleNamespace(
        id=_MODEL_ID, deployed_version_id=newer_version_id,
    )
    live_nq = types.SimpleNamespace(id=_NQ_ID, name="star_nq")
    db = AsyncMock()
    db.get = AsyncMock(return_value=captured_version)

    _, deployed = asyncio.run(
        _load_deployed_named_query_definition(
            db, model, live_nq, deployed_version_id=_VERSION_ID,
        )
    )
    assert deployed.definition_sql == "SELECT * FROM modely"
    # The version row is read BY the captured id — never the model's ORM
    # pointer.
    assert db.get.await_args.args == (ModelVersion, _VERSION_ID)


def test_nq2c_f3_captured_pointer_missing_or_mismatched_fails_closed() -> None:
    """NQ2C-F3 fail-closed: a captured pointer whose version row is missing,
    belongs to another model, or carries a malformed snapshot must RAISE —
    never fall back to the live definition or to the model-row pointer."""
    import asyncio

    from shared.db.models import ModelVersion
    from shared.named_query.refresh import _load_deployed_named_query_definition

    live_nq = types.SimpleNamespace(id=_NQ_ID, name="star_nq")
    model = types.SimpleNamespace(
        id=_MODEL_ID, deployed_version_id=_VERSION_ID,
    )

    # Missing version row.
    db = AsyncMock()
    db.get = AsyncMock(return_value=None)
    with pytest.raises(ValueError, match="could not be found"):
        asyncio.run(
            _load_deployed_named_query_definition(
                db, model, live_nq, deployed_version_id=_VERSION_ID,
            )
        )
    # Version row for a DIFFERENT model.
    db = AsyncMock()
    db.get = AsyncMock(return_value=types.SimpleNamespace(
        id=_VERSION_ID, model_id=uuid.uuid4(),
        snapshot_json=_snapshot_single_field(),
    ))
    with pytest.raises(ValueError, match="could not be found"):
        asyncio.run(
            _load_deployed_named_query_definition(
                db, model, live_nq, deployed_version_id=_VERSION_ID,
            )
        )
    # Malformed snapshot (missing the family).
    db = AsyncMock()
    db.get = AsyncMock(return_value=types.SimpleNamespace(
        id=_VERSION_ID, model_id=_MODEL_ID,
        snapshot_json={"dimensions": []},
    ))
    with pytest.raises(ValueError, match="empty or malformed"):
        asyncio.run(
            _load_deployed_named_query_definition(
                db, model, live_nq, deployed_version_id=_VERSION_ID,
            )
        )
    assert ModelVersion is not None  # the import is the row type read above


def _snapshot_single_field() -> dict:
    return {
        "named_queries": [
            {
                "id": str(_NQ_ID),
                "name": "star_nq",
                "definition_sql": "SELECT * FROM modely",
                "shape": "projection",
                "row_cap": 500,
            },
        ],
        "dimensions": [
            {"id": "d-1", "name": "branch_id", "source_column_id": "c-1"},
        ],
        "measures": [],
        "columns": [
            {"id": "c-1", "model_table_id": "t-1",
             "column_name": "branch_id", "is_hidden": False},
        ],
        "tables": [
            {"id": "t-1", "physical_name": "demo.sales", "alias": "sales"},
        ],
    }


# ---------------------------------------------------------------------------
# Generation-guard pre-scan population re-proof
# ---------------------------------------------------------------------------

def test_generation_guard_reproves_population_contract_before_scan() -> None:
    """A definition edit + refresh landing between admission and the pre-scan
    must be refused BEFORE any row is scanned: the guard re-proves
    ``named_query_population_manifest_matches`` from the manifest already read
    in the admission column SELECT (no extra query)."""
    import asyncio

    from src.routing import named_query_generation_guard as _guard

    fp = named_query_population_fingerprint(
        model_id=_MODEL_ID, named_query_id=_NQ_ID,
        deployed_version_id=_VERSION_ID, deploy_epoch=3,
        definition_sql='SELECT "a" FROM m',
    )

    def _row(manifest_fp):
        return types.SimpleNamespace(
            id="art-1", status="fresh", active_refresh_run_id="run-1",
            physical_table_name="nq_x", target_schema="public",
            target_id="tgt-1", built_for_version_id=str(_VERSION_ID),
            built_for_epoch=3,
            row_manifest={
                "manifest_version": 2,
                "build_refresh_run_id": "run-1",
                "row_definition_fingerprint": manifest_fp,
            },
        )

    decision = types.SimpleNamespace(
        rewritten_query='SELECT * FROM "public"."nq_x"',
        target_dialect="postgres",
        admitted_generation=None,
    )
    model = types.SimpleNamespace(
        deployed_version_id=str(_VERSION_ID), deploy_epoch=3,
    )
    db = AsyncMock()

    def _run(expected_fp, artifact_row):
        with (
            patch.object(
                _guard, "fetch_columns",
                new=AsyncMock(return_value=artifact_row),
            ),
            patch.object(
                _guard, "_read_named_query_row",
                new=AsyncMock(return_value=types.SimpleNamespace(
                    model_id=str(_MODEL_ID), definition_sql="SELECT 1",
                )),
            ),
            patch.object(
                _guard, "_source_binding_matches",
                new=AsyncMock(return_value=True),
            ),
        ):
            return asyncio.run(
                _guard.assert_named_query_route_admissible(
                    db,
                    named_query_id=str(_NQ_ID),
                    artifact_id="art-1",
                    decision=decision,
                    model=model,
                    target=None,
                    conn=None,
                    security_compiled=None,
                    expected_population_fingerprint=expected_fp,
                )
            )

    class _FirstResult:
        def first(self):
            return None

    def _first(_stmt):
        return _FirstResult()

    db.execute = AsyncMock(side_effect=_first)

    # Matching fingerprint + live manifest -> admitted, returns the stamp.
    matching_row = _row(fp)
    stamp = _run(fp, matching_row)
    assert stamp is not None
    # Mismatched fingerprint (edited + refreshed since admission) -> refuse.
    with pytest.raises(_guard.NamedQueryGenerationChangedError, match="population"):
        _run(fp, _row("stale-fingerprint"))
    # None expected fingerprint proves nothing -> refuse (fail closed).
    with pytest.raises(_guard.NamedQueryGenerationChangedError, match="population"):
        _run(None, matching_row)
