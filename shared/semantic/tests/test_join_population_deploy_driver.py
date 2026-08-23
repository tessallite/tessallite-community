"""The deploy-time driver for join population classification (Bug-8615 G1).

Pins the DRIVER contract with a fake session + stubbed source, the same way
``test_attribute_relationship_deploy_verify.py`` pins its sibling driver:

* it stages one evidence row per join, bound to the deploy epoch;
* it CLEARS the model's previous verdicts first, so "no row" can only ever mean
  "not evaluated at the last deploy";
* it is fail-open in every measurement failure mode — a deploy can never fail
  because a source could not be measured (G5 policy consumes only returned
  measured rows);
* ``measure=False`` records conservative verdicts without touching the source;
* it issues the cheap two-query probe on a clean edge and only pays for the
  third (join-cardinality) query when a key is not unique in the data.
"""
from __future__ import annotations

import types
import uuid

import pytest

import shared.semantic.join_population_validator as jpv

pytestmark = pytest.mark.unit


class _Scalars:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return _Scalars(self._rows)


class _FakeDB:
    """Returns rows by the ORM entity the statement selects from."""

    def __init__(self, *, joins, tables, columns):
        self._by_entity = {
            "joins": joins, "model_tables": tables, "model_columns": columns,
        }
        self.staged: list = []
        self.deletes: list = []
        self.statements: list = []

    async def execute(self, stmt):
        self.statements.append(stmt)
        if stmt.__class__.__name__ == "Delete":
            self.deletes.append(getattr(getattr(stmt, "table", None), "name", ""))
            return _Result([])
        # SELECT: resolve the entity from the statement's FROM clause.
        froms = list(stmt.get_final_froms() or [])
        key = froms[0].name if froms else ""
        rows = self._by_entity.get(key, [])
        if key == "model_columns" and "is_primary_key" in str(
            stmt.whereclause if stmt.whereclause is not None else ""
        ):
            # The declared-key load (Bug-8668) filters in SQL; the fake has to
            # honour that filter or every column would read as a key column.
            # Matched on the WHERE clause only — the SELECT list names every
            # column, so testing the whole statement also caught the ordinary
            # endpoint-column load and starved it of its non-key columns.
            rows = [c for c in rows if getattr(c, "is_primary_key", False)]
        return _Result(rows)

    def add(self, row):
        self.staged.append(row)


def _table(tid, physical, table_type="dim_detail"):
    return types.SimpleNamespace(
        id=tid, physical_name=physical, table_type=table_type,
    )


def _column(cid, name, *, is_primary_key=False, table_id=None):
    return types.SimpleNamespace(
        id=cid, column_name=name, is_primary_key=is_primary_key,
        model_table_id=table_id,
    )


def _star(*, participation="preserve_base_rows", dim_pk=True, join_type="left"):
    """A one-edge star: fact.dim_id -> dim.id."""
    fact_id, dim_id = uuid.uuid4(), uuid.uuid4()
    fk_col, pk_col = uuid.uuid4(), uuid.uuid4()
    join = types.SimpleNamespace(
        id=uuid.uuid4(),
        left_table_id=fact_id, right_table_id=dim_id,
        left_column_id=fk_col, right_column_id=pk_col,
        join_type=join_type, population_participation=participation,
    )
    return _FakeDB(
        joins=[join],
        tables=[
            _table(fact_id, "demo.fact", table_type="fact"),
            _table(dim_id, "demo.dim"),
        ],
        columns=[
            _column(fk_col, "dim_id", table_id=fact_id),
            _column(pk_col, "id", is_primary_key=dim_pk, table_id=dim_id),
        ],
    ), join


def test_g5_legacy_snapshot_defaults_only_when_participation_key_is_absent():
    """JPG-G5-SOL-R2-20260822: preserve omission, reject bad tokens."""
    fact_id, dim_id = uuid.uuid4(), uuid.uuid4()
    fact_col, dim_col = uuid.uuid4(), uuid.uuid4()
    join_id = uuid.uuid4()

    def _snapshot(join_fields):
        return {
            "tables": [
                {"id": str(fact_id), "table_type": "fact"},
                {"id": str(dim_id), "table_type": "dim_detail"},
            ],
            "columns": [
                {"id": str(fact_col), "model_table_id": str(fact_id)},
                {"id": str(dim_col), "model_table_id": str(dim_id),
                 "is_primary_key": True},
            ],
            "joins": [{
                "id": str(join_id), "left_table_id": str(fact_id),
                "right_table_id": str(dim_id), "left_column_id": str(fact_col),
                "right_column_id": str(dim_col), **join_fields,
            }],
        }

    legacy_join = jpv._snapshot_graph(_snapshot({}))[0][0]
    invalid_join = jpv._snapshot_graph(
        _snapshot({"population_participation": "not-a-token"})
    )[0][0]
    assert legacy_join.population_participation == jpv.DEFAULT_POPULATION_PARTICIPATION
    assert invalid_join.population_participation == jpv.POPULATION_PARTICIPATION_UNDECLARED


def _patch_source(monkeypatch, side_rows, join_rows=None):
    """Stub ``execute_source_sql`` and record every statement it receives."""
    import shared.source_executor as se

    seen: list[str] = []

    async def _exec(conn_obj, sql, *, tenant_session=None, purpose=None,
                    tenant_slug=None):
        seen.append(sql)
        if "n_join_rows" in sql:
            return [join_rows or {"n_join_rows": 0}], []
        # The RETAINED table is the one in the outer FROM, i.e. before the
        # LEFT JOIN that brings in the DISTINCT far-key set. Match the fully
        # quoted reference so a column named ``dim_id`` cannot be mistaken for
        # the ``dim`` table.
        retained = sql.split("LEFT JOIN")[0]
        for needle, row in side_rows.items():
            if f'"demo"."{needle}"' in retained:
                return [row], []
        return [{}], []

    monkeypatch.setattr(se, "execute_source_sql", _exec)
    return seen


_FACT_SIDE = {
    "n_rows": 100, "n_key_non_null": 100, "n_key_distinct": 10, "n_matched": 100,
}
_DIM_SIDE = {
    "n_rows": 10, "n_key_non_null": 10, "n_key_distinct": 10, "n_matched": 10,
}


@pytest.mark.asyncio
async def test_a_clean_star_stages_one_neutral_ok_row(monkeypatch):
    db, join = _star()
    _patch_source(monkeypatch, {"fact": _FACT_SIDE, "dim": _DIM_SIDE})
    version_id = uuid.uuid4()

    staged = await jpv.validate_model_joins_on_deploy(
        db=db, model_id=uuid.uuid4(), deployed_version_id=version_id,
        deploy_epoch=7, conn_obj=types.SimpleNamespace(), connector="postgresql",
    )

    assert len(staged) == 1
    row = staged[0]
    assert row.join_id == join.id
    assert row.deploy_epoch == 7
    assert row.deployed_version_id == version_id
    assert row.classification == jpv.CLASSIFICATION_NEUTRAL
    assert row.status == jpv.STATUS_OK
    assert row.measured is True


@pytest.mark.asyncio
async def test_the_previous_verdicts_are_cleared_first(monkeypatch):
    """Otherwise a model whose validation was later switched off would keep
    showing the verdict from an older deploy as if it were current."""
    db, _join = _star()
    _patch_source(monkeypatch, {"fact": _FACT_SIDE, "dim": _DIM_SIDE})
    await jpv.validate_model_joins_on_deploy(
        db=db, model_id=uuid.uuid4(), deployed_version_id=None, deploy_epoch=1,
        conn_obj=types.SimpleNamespace(), connector="postgresql",
    )
    assert "join_population_checks" in db.deletes


@pytest.mark.asyncio
async def test_a_model_with_no_joins_clears_and_stages_nothing(monkeypatch):
    db = _FakeDB(joins=[], tables=[], columns=[])
    staged = await jpv.validate_model_joins_on_deploy(
        db=db, model_id=uuid.uuid4(), deployed_version_id=None, deploy_epoch=1,
        conn_obj=types.SimpleNamespace(), connector="postgresql",
    )
    assert staged == []
    assert "join_population_checks" in db.deletes


@pytest.mark.asyncio
async def test_measure_false_records_a_conservative_row_without_touching_source(
    monkeypatch,
):
    db, _join = _star()
    seen = _patch_source(monkeypatch, {})
    staged = await jpv.validate_model_joins_on_deploy(
        db=db, model_id=uuid.uuid4(), deployed_version_id=None, deploy_epoch=2,
        conn_obj=None, connector="", measure=False,
    )
    assert seen == [], "measure=False must issue no source query at all"
    assert len(staged) == 1
    assert staged[0].measured is False
    assert staged[0].classification != jpv.CLASSIFICATION_NEUTRAL
    assert staged[0].status == jpv.STATUS_WARNING
    assert staged[0].row_effect_ratio is None


@pytest.mark.asyncio
async def test_a_failing_source_probe_warns_and_never_raises(monkeypatch):
    import shared.source_executor as se

    async def _boom(*_a, **_kw):
        raise RuntimeError("source unreachable")

    monkeypatch.setattr(se, "execute_source_sql", _boom)
    db, _join = _star()
    staged = await jpv.validate_model_joins_on_deploy(
        db=db, model_id=uuid.uuid4(), deployed_version_id=None, deploy_epoch=1,
        conn_obj=types.SimpleNamespace(), connector="postgresql",
    )
    assert len(staged) == 1
    assert staged[0].measured is False
    assert staged[0].reason == jpv.REASON_MEASUREMENT_FAILED
    assert staged[0].status == jpv.STATUS_WARNING


@pytest.mark.asyncio
async def test_a_broken_session_is_swallowed_not_raised(monkeypatch):
    """The last backstop: a deploy must not fail because the classifier could
    not even read the model's joins."""

    class _BrokenDB:
        async def execute(self, stmt):
            raise RuntimeError("session gone")

        def add(self, row):  # pragma: no cover - never reached
            raise AssertionError

    staged = await jpv.validate_model_joins_on_deploy(
        db=_BrokenDB(), model_id=uuid.uuid4(), deployed_version_id=None,
        deploy_epoch=1, conn_obj=types.SimpleNamespace(), connector="postgresql",
    )
    assert staged == []


@pytest.mark.asyncio
async def test_an_unresolvable_endpoint_is_recorded_not_skipped(monkeypatch):
    """A join whose column row is gone must still produce a verdict — silently
    omitting it would make the rollup report a partially-checked model as fully
    evaluated."""
    db, join = _star()
    db._by_entity["model_columns"] = []   # columns vanished
    _patch_source(monkeypatch, {"fact": _FACT_SIDE, "dim": _DIM_SIDE})
    staged = await jpv.validate_model_joins_on_deploy(
        db=db, model_id=uuid.uuid4(), deployed_version_id=None, deploy_epoch=1,
        conn_obj=types.SimpleNamespace(), connector="postgresql",
    )
    assert len(staged) == 1
    assert staged[0].reason == jpv.REASON_UNRESOLVED_ENDPOINT
    assert staged[0].measured is False


@pytest.mark.asyncio
async def test_a_clean_edge_costs_two_queries_not_three(monkeypatch):
    """Deploy-time cost discipline: the join-cardinality probe is the expensive
    one and is provably unnecessary when both keys are unique in the data."""
    db, _join = _star()
    unique_fact = dict(_FACT_SIDE, n_key_distinct=100)
    seen = _patch_source(monkeypatch, {"fact": unique_fact, "dim": _DIM_SIDE})
    await jpv.validate_model_joins_on_deploy(
        db=db, model_id=uuid.uuid4(), deployed_version_id=None, deploy_epoch=1,
        conn_obj=types.SimpleNamespace(), connector="postgresql",
    )
    assert len(seen) == 2
    assert not any("n_join_rows" in s for s in seen)


@pytest.mark.asyncio
async def test_a_non_unique_key_pays_for_the_third_query(monkeypatch):
    db, _join = _star()
    seen = _patch_source(
        monkeypatch, {"fact": _FACT_SIDE, "dim": _DIM_SIDE},
        join_rows={"n_join_rows": 100},
    )
    await jpv.validate_model_joins_on_deploy(
        db=db, model_id=uuid.uuid4(), deployed_version_id=None, deploy_epoch=1,
        conn_obj=types.SimpleNamespace(), connector="postgresql",
    )
    assert len(seen) == 3
    assert sum("n_join_rows" in s for s in seen) == 1


@pytest.mark.asyncio
async def test_a_lossy_undeclared_join_is_blocked_but_still_only_staged(
    monkeypatch,
):
    """BLOCKED must be COMPUTED. The driver's only output is staged rows — it
    has no return path, exception, or flag by which it could stop a deploy."""
    db, _join = _star(participation="undeclared", join_type="inner")
    lossy_fact = dict(_FACT_SIDE, n_matched=17)
    _patch_source(
        monkeypatch, {"fact": lossy_fact, "dim": _DIM_SIDE},
        join_rows={"n_join_rows": 17},
    )
    staged = await jpv.validate_model_joins_on_deploy(
        db=db, model_id=uuid.uuid4(), deployed_version_id=None, deploy_epoch=1,
        conn_obj=types.SimpleNamespace(), connector="postgresql",
    )
    assert staged[0].status == jpv.STATUS_BLOCKED
    assert staged[0].classification == jpv.CLASSIFICATION_FILTERING
    assert staged[0].row_loss_ratio == pytest.approx(0.83)


@pytest.mark.asyncio
async def test_the_declared_flag_at_check_time_is_recorded_on_the_row(
    monkeypatch,
):
    """The health surface has to be able to show what the verdict was computed
    against, not just today's live declaration."""
    db, _join = _star(participation="population_defining", join_type="inner")
    lossy_fact = dict(_FACT_SIDE, n_matched=17)
    _patch_source(
        monkeypatch, {"fact": lossy_fact, "dim": _DIM_SIDE},
        join_rows={"n_join_rows": 17},
    )
    staged = await jpv.validate_model_joins_on_deploy(
        db=db, model_id=uuid.uuid4(), deployed_version_id=None, deploy_epoch=1,
        conn_obj=types.SimpleNamespace(), connector="postgresql",
    )
    assert staged[0].population_participation == "population_defining"
    # Deliberately lossy, deliberately declared -> OK.
    assert staged[0].status == jpv.STATUS_OK
    assert staged[0].classification == jpv.CLASSIFICATION_FILTERING


@pytest.mark.asyncio
async def test_the_probe_budget_stops_measuring_and_says_so(monkeypatch):
    """Deploy is a SYNCHRONOUS request. A wide model on a merely-slow source
    would otherwise hold it open for joins x 3 x the source statement timeout.
    Once the wall-clock budget is spent the remaining joins record an
    UNMEASURED verdict — honest, and the rollup's ``evaluated`` flag goes false
    — instead of the deploy stalling."""
    fact_id = uuid.uuid4()
    dim_ids = [uuid.uuid4() for _ in range(3)]
    joins, tables, columns = [], [_table(fact_id, "demo.fact", table_type="fact")], []
    for i, dim_id in enumerate(dim_ids):
        fk, pk = uuid.uuid4(), uuid.uuid4()
        joins.append(types.SimpleNamespace(
            id=uuid.UUID(int=i + 1),
            left_table_id=fact_id, right_table_id=dim_id,
            left_column_id=fk, right_column_id=pk,
            join_type="left", population_participation="preserve_base_rows",
        ))
        tables.append(_table(dim_id, f"demo.dim{i}"))
        columns += [
            _column(fk, f"dim{i}_id", table_id=fact_id),
            _column(pk, "id", is_primary_key=True, table_id=dim_id),
        ]
    db = _FakeDB(joins=joins, tables=tables, columns=columns)

    # Every table the first join probes must have data: an unrecognised result
    # shape is now a probe FAILURE (R4-1), not an implicit all-zero row, so a
    # fixture that omits a side would make this test measure nothing at all.
    seen = _patch_source(monkeypatch, {
        "fact": _FACT_SIDE, "dim0": _DIM_SIDE, "dim1": _DIM_SIDE,
        "dim2": _DIM_SIDE,
    })
    # A clock that stays inside the budget until the FIRST join has issued its
    # statements, then jumps past it. Keyed on observed source calls rather
    # than on a call count, so it does not encode how many times the
    # implementation happens to read the clock.
    monkeypatch.setattr(
        jpv.time, "monotonic", lambda: 0.0 if len(seen) < 3 else 999.0,
    )

    staged = await jpv.validate_model_joins_on_deploy(
        db=db, model_id=uuid.uuid4(), deployed_version_id=None, deploy_epoch=1,
        conn_obj=types.SimpleNamespace(), connector="postgresql",
        budget_seconds=10.0,
    )

    assert len(staged) == 3, "every join still gets a verdict row"
    measured = [r for r in staged if r.measured]
    exhausted = [
        r for r in staged
        if r.reason == jpv.REASON_MEASUREMENT_BUDGET_EXHAUSTED
    ]
    assert len(measured) == 1, "only the first join was inside the budget"
    assert len(exhausted) == 2
    assert all(r.status == jpv.STATUS_WARNING for r in exhausted)
    # And the source was not touched again after the budget ran out.
    assert len(seen) <= 3


@pytest.mark.asyncio
async def test_a_zero_budget_means_unbounded(monkeypatch):
    """0 disables the bound rather than disabling measurement — a deployment
    that would rather wait must be able to say so."""
    db, _join = _star()
    seen = _patch_source(monkeypatch, {"fact": _FACT_SIDE, "dim": _DIM_SIDE})
    monkeypatch.setattr(jpv.time, "monotonic", lambda: 10_000.0)
    staged = await jpv.validate_model_joins_on_deploy(
        db=db, model_id=uuid.uuid4(), deployed_version_id=None, deploy_epoch=1,
        conn_obj=types.SimpleNamespace(), connector="postgresql",
        budget_seconds=0,
    )
    assert staged[0].measured is True
    assert seen, "a zero budget must not suppress the probe"


@pytest.mark.asyncio
async def test_the_deadline_is_enforced_between_statements_not_only_joins(
    monkeypatch,
):
    """Bug-8656. Checking the budget only between joins lets a join that
    started just inside it still issue three sequential statements, so the real
    deploy bound becomes budget + 3x the source statement timeout. The deadline
    is checked before EVERY statement, so at most one can be in flight."""
    db, _join = _star()
    seen = _patch_source(monkeypatch, {"fact": _FACT_SIDE, "dim": _DIM_SIDE})
    # Inside the budget until the FIRST statement has run, past it after.
    # Keyed on observed source calls, so it does not encode how many times the
    # implementation happens to read the clock.
    monkeypatch.setattr(
        jpv.time, "monotonic", lambda: 0.0 if len(seen) < 1 else 999.0,
    )

    staged = await jpv.validate_model_joins_on_deploy(
        db=db, model_id=uuid.uuid4(), deployed_version_id=None, deploy_epoch=1,
        conn_obj=types.SimpleNamespace(), connector="postgresql",
        budget_seconds=10.0,
    )

    assert len(seen) == 1, "the second statement must not be issued"
    assert staged[0].measured is False
    assert staged[0].reason == jpv.REASON_MEASUREMENT_BUDGET_EXHAUSTED
    assert staged[0].status == jpv.STATUS_WARNING
    # A half-measured edge is discarded, never mixed into a ratio.
    assert staged[0].row_effect_ratio is None


@pytest.mark.asyncio
async def test_the_fact_anchor_is_deterministic(monkeypatch):
    """Bug-8659. The storage layer allows only one fact table per model, but
    the BFS root — and therefore every edge's near/far verdict — must not
    depend on the order the tables come back in if that guard is ever relaxed
    or bypassed.

    The two candidate anchors give genuinely DIFFERENT answers here, which is
    what makes this test non-vacuous: ``fact_lo`` is not connected to the join
    graph, so the walk resolves nothing and the edge is measured conservatively
    in both directions (the dimension-retained direction fans out 10x ->
    multiplying); ``fact_hi`` IS the edge's own endpoint, so the walk resolves
    the near side and the same data is neutral. Picking the anchor with a bare
    ``next()`` over a dict returned whichever came first, so the two orderings
    below disagreed.
    """
    # Ids fixed so "lowest by string" is unambiguous and the assertion below
    # can name the expected winner rather than accept either answer.
    fact_lo = uuid.UUID(int=1)
    fact_hi = uuid.UUID(int=2)
    dim = uuid.UUID(int=3)
    fk, pk = uuid.uuid4(), uuid.uuid4()

    def _build(table_order):
        join = types.SimpleNamespace(
            id=uuid.UUID(int=7),
            left_table_id=fact_hi, right_table_id=dim,
            left_column_id=fk, right_column_id=pk,
            join_type="left", population_participation="preserve_base_rows",
        )
        by_id = {
            fact_lo: _table(fact_lo, "demo.other", table_type="fact"),
            fact_hi: _table(fact_hi, "demo.fact", table_type="fact"),
            dim: _table(dim, "demo.dim"),
        }
        return _FakeDB(
            joins=[join],
            tables=[by_id[t] for t in table_order],
            columns=[
                _column(fk, "dim_id", is_primary_key=True, table_id=fact_hi),
                _column(pk, "id", is_primary_key=True, table_id=dim),
            ],
        )

    verdicts = []
    for order in ([fact_lo, fact_hi, dim], [dim, fact_hi, fact_lo]):
        _patch_source(
            monkeypatch, {"fact": _FACT_SIDE, "dim": _DIM_SIDE},
            join_rows={"n_join_rows": 100},
        )
        staged = await jpv.validate_model_joins_on_deploy(
            db=_build(order), model_id=uuid.uuid4(), deployed_version_id=None,
            deploy_epoch=1, conn_obj=types.SimpleNamespace(),
            connector="postgresql",
        )
        verdicts.append((staged[0].classification, staged[0].reason))

    assert verdicts[0] == verdicts[1], (
        "the same model must classify identically whatever order its tables "
        f"come back in, got {verdicts}"
    )
    # ``fact_lo`` sorts first, and it is disconnected from this edge, so the
    # deterministic answer is the conservative both-directions one.
    assert verdicts[0] == (
        jpv.CLASSIFICATION_MULTIPLYING, jpv.REASON_ORIENTATION_AMBIGUOUS,
    )


@pytest.mark.asyncio
async def test_uppercase_result_columns_do_not_read_as_a_clean_neutral(monkeypatch):
    """Bug-8670. Snowflake uppercases unquoted identifiers, so the probe's
    ``AS n_rows`` aliases came back as ``N_ROWS``. Reading them by lowercase
    name folded every count to 0 and turned a 40%-row-loss join into a
    MEASURED ``neutral``/OK — the one verdict this module must never invent."""
    db, _join = _star(dim_pk=True, join_type="inner")
    lossy_fact = {"n_rows": 100, "n_key_non_null": 100,
                  "n_key_distinct": 10, "n_matched": 60}
    _patch_source(monkeypatch, {"fact": lossy_fact, "dim": _DIM_SIDE},
                  join_rows={"n_join_rows": 60})
    import shared.source_executor as se
    inner = se.execute_source_sql

    async def _upper(conn_obj, sql, **kw):
        rows, cols = await inner(conn_obj, sql, **kw)
        return [{k.upper(): v for k, v in r.items()} for r in rows], cols

    monkeypatch.setattr(se, "execute_source_sql", _upper)
    staged = await jpv.validate_model_joins_on_deploy(
        db=db, model_id=uuid.uuid4(), deployed_version_id=uuid.uuid4(),
        deploy_epoch=1, conn_obj=object(), connector="snowflake",
    )
    row = staged[0]
    assert row.classification != jpv.CLASSIFICATION_NEUTRAL, (
        "a 40% row-loss join read as neutral because the counts came back "
        "under UPPERCASE keys and silently folded to zero"
    )
    assert row.row_loss_ratio == pytest.approx(0.4)
    assert row.status == jpv.STATUS_WARNING  # preserve_base_rows default


def test_the_probe_aliases_its_result_columns_explicitly(monkeypatch):
    """Belt to the braces above: the emitted SQL must QUOTE its result aliases,
    so a case-normalising dialect never renames them in the first place."""
    for connector, opener in (
        ("snowflake", '"'), ("postgresql", '"'), ("bigquery", "`"),
        ("sqlserver", "["),
    ):
        sql = jpv.build_side_probe_sql(
            connector=connector,
            retained_table="s.f", retained_column="k",
            joined_table="s.d", joined_column="k",
        )
        assert f"AS {opener}{jpv.COL_ROWS}" in sql, (connector, sql)
        assert f"AS {jpv.COL_ROWS}" not in sql, (
            f"{connector}: the result alias is unquoted, so a dialect that "
            "normalises identifier case will rename it"
        )


@pytest.mark.asyncio
async def test_half_a_composite_key_is_not_constraint_backed_uniqueness(
    monkeypatch,
):
    """Bug-8668. ``is_primary_key`` is stamped on EVERY column of a composite
    PK, so joining on one half of it flags as declared-unique while the edge
    can still match N rows per retained row. ``pocket_population`` — the other
    consumer of the same flag, answering the same question — already refuses
    this shape; the classifier must not disagree with it.

    The measurement is deliberately clean (one variant per product TODAY), so
    the ONLY thing that can make this non-neutral is honouring the composite
    key.
    """
    fact_id, dim_id = uuid.uuid4(), uuid.uuid4()
    fk, pk_a, pk_b = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    join = types.SimpleNamespace(
        id=uuid.uuid4(),
        left_table_id=fact_id, right_table_id=dim_id,
        left_column_id=fk, right_column_id=pk_a,
        join_type="left", population_participation="preserve_base_rows",
    )
    db = _FakeDB(
        joins=[join],
        tables=[
            _table(fact_id, "demo.order_lines", table_type="fact"),
            _table(dim_id, "demo.product_variants"),
        ],
        columns=[
            _column(fk, "product_id", table_id=fact_id),
            # (product_id, variant_code) is ONE composite PK; introspection
            # stamps the flag on both of its columns.
            _column(pk_a, "product_id", is_primary_key=True, table_id=dim_id),
            _column(pk_b, "variant_code", is_primary_key=True, table_id=dim_id),
        ],
    )
    _patch_source(
        monkeypatch,
        {"order_lines": {"n_rows": 1000, "n_key_non_null": 1000,
                         "n_key_distinct": 300, "n_matched": 1000},
         "product_variants": {"n_rows": 300, "n_key_non_null": 300,
                              "n_key_distinct": 300, "n_matched": 300}},
        join_rows={"n_join_rows": 1000},
    )
    staged = await jpv.validate_model_joins_on_deploy(
        db=db, model_id=uuid.uuid4(), deployed_version_id=None, deploy_epoch=1,
        conn_obj=types.SimpleNamespace(), connector="postgresql",
    )
    assert staged[0].classification != jpv.CLASSIFICATION_NEUTRAL
    assert staged[0].reason == jpv.REASON_UNIQUENESS_NOT_DECLARED
    assert staged[0].status == jpv.STATUS_WARNING


@pytest.mark.asyncio
async def test_a_single_column_primary_key_is_constraint_backed(monkeypatch):
    """Mutation partner: the SAME shape with a single-column PK really is
    neutral, so the composite rule must not refuse every declared key."""
    db, _join = _star(dim_pk=True)
    _patch_source(
        monkeypatch, {"fact": _FACT_SIDE, "dim": _DIM_SIDE},
        join_rows={"n_join_rows": 100},
    )
    staged = await jpv.validate_model_joins_on_deploy(
        db=db, model_id=uuid.uuid4(), deployed_version_id=None, deploy_epoch=1,
        conn_obj=types.SimpleNamespace(), connector="postgresql",
    )
    assert staged[0].classification == jpv.CLASSIFICATION_NEUTRAL
    assert staged[0].status == jpv.STATUS_OK


@pytest.mark.asyncio
async def test_a_probe_that_returns_no_row_is_not_a_measured_neutral(monkeypatch):
    """R4-1, the generalisation of Bug-8670. The counts must come from the
    source, not from ``.get()`` defaulting a missing key to 0. An empty result
    set, or a driver whose column names do not match the emitted aliases,
    previously produced classification=neutral / status=OK / measured=True /
    reason=measured for a join that was never measured at all — all-zero counts
    are indistinguishable from a genuinely clean edge."""
    import shared.source_executor as se

    cases = (
        ("no rows", ([], [])),
        ("renamed columns",
         ([{"f0_": 100, "f1_": 100, "f2_": 10, "f3_": 60}],
          ["f0_", "f1_", "f2_", "f3_"])),
        ("a null count",
         ([{"n_rows": 100, "n_key_non_null": None,
            "n_key_distinct": 10, "n_matched": 60}],
          ["n_rows", "n_key_non_null", "n_key_distinct", "n_matched"])),
    )
    for label, payload in cases:
        async def _fake(conn_obj, sql, _p=payload, **kw):
            return _p
        monkeypatch.setattr(se, "execute_source_sql", _fake)
        db, _join = _star(dim_pk=True, join_type="inner")
        staged = await jpv.validate_model_joins_on_deploy(
            db=db, model_id=uuid.uuid4(), deployed_version_id=None,
            deploy_epoch=1, conn_obj=object(), connector="postgresql",
        )
        row = staged[0]
        assert row.measured is False, f"{label}: reported as measured"
        assert row.classification != jpv.CLASSIFICATION_NEUTRAL, (
            f"{label}: an unmeasured edge was recorded as a confident neutral"
        )
        assert row.reason == jpv.REASON_MEASUREMENT_FAILED, label
        assert row.row_loss_ratio is None and row.row_mult_ratio is None, label


@pytest.mark.asyncio
async def test_a_genuinely_zero_count_is_still_measured(monkeypatch):
    """Mutation partner: a real 0 is data, not a missing key. An empty source
    table must still produce a MEASURED verdict, or the shape check above would
    have turned every empty table into a probe failure."""
    empty = {"n_rows": 0, "n_key_non_null": 0, "n_key_distinct": 0,
             "n_matched": 0}
    db, _join = _star(dim_pk=True, join_type="inner")
    _patch_source(monkeypatch, {"fact": empty, "dim": empty})
    staged = await jpv.validate_model_joins_on_deploy(
        db=db, model_id=uuid.uuid4(), deployed_version_id=None, deploy_epoch=1,
        conn_obj=types.SimpleNamespace(), connector="postgresql",
    )
    assert staged[0].measured is True
    assert staged[0].row_loss_ratio == 0.0


@pytest.mark.asyncio
async def test_the_staged_verdict_records_its_classification_inputs(monkeypatch):
    """R5-1, writer side.

    The health surface decides staleness by comparing the live join against the
    fingerprint on the stored verdict, so the WRITER has to stamp it. The
    health tests build their own fixture rows and therefore cannot catch the
    deploy path dropping the stamp — a mutation run proved exactly that, so the
    guard belongs here, against the production writer.
    """
    db, join = _star(dim_pk=True)
    _patch_source(monkeypatch, {"fact": _FACT_SIDE, "dim": _DIM_SIDE},
                  join_rows={"n_join_rows": 100})
    staged = await jpv.validate_model_joins_on_deploy(
        db=db, model_id=uuid.uuid4(), deployed_version_id=None, deploy_epoch=1,
        conn_obj=types.SimpleNamespace(), connector="postgresql",
    )
    assert staged[0].inputs_fingerprint, "the verdict recorded no input fingerprint"
    assert staged[0].inputs_fingerprint == jpv.join_definition_fingerprint(join)


def test_the_fingerprint_moves_when_any_classification_input_moves():
    """Every attribute the classifier reads must be inside the hash. A
    fingerprint that covers only some inputs is the Bug-8667 mistake again,
    one level up."""
    base = types.SimpleNamespace(
        join_type="left", population_participation="preserve_base_rows",
        left_table_id=uuid.UUID(int=1), right_table_id=uuid.UUID(int=2),
        left_column_id=uuid.UUID(int=3), right_column_id=uuid.UUID(int=4),
    )
    original = jpv.join_definition_fingerprint(base)
    for field, changed in (
        ("join_type", "inner"),
        ("population_participation", "population_defining"),
        ("left_table_id", uuid.UUID(int=9)),
        ("right_table_id", uuid.UUID(int=9)),
        ("left_column_id", uuid.UUID(int=9)),
        ("right_column_id", uuid.UUID(int=9)),
    ):
        moved = types.SimpleNamespace(**vars(base))
        setattr(moved, field, changed)
        assert jpv.join_definition_fingerprint(moved) != original, (
            f"{field} is a classifier input but does not move the fingerprint"
        )


def test_the_fingerprint_is_stable_for_an_unchanged_join():
    """Mutation partner: a fingerprint that never matches would mark every
    verdict stale and make the surface useless."""
    join = types.SimpleNamespace(
        join_type="LEFT", population_participation="preserve_base_rows",
        left_table_id=uuid.UUID(int=1), right_table_id=uuid.UUID(int=2),
        left_column_id=uuid.UUID(int=3), right_column_id=uuid.UUID(int=4),
    )
    twin = types.SimpleNamespace(**vars(join))
    twin.join_type = "  left "      # same token, different spelling
    assert jpv.join_definition_fingerprint(join) == (
        jpv.join_definition_fingerprint(twin)
    ), "a cosmetic re-spelling of join_type must not read as a change"
