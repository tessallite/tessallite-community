"""
Shared fixtures for query-router tests.

sys.path is configured via [tool.pytest.ini_options] pythonpath in pyproject.toml:
  - "."  → tessallite/services/query-router/   (enables "from src.xxx import")
  - "../../" → tessallite/                     (enables "from shared.xxx import")
"""
from __future__ import annotations
import contextlib
import types
from contextlib import ExitStack as _ExitStack
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from shared.config.fastapi_drift import check_fastapi_version_drift
from src.semantic.snapshot_resolver import DeployedShape as _DeployedShape

check_fastapi_version_drift()


_SYNTHETIC_BASE_TABLE_ID = "t-synthetic-base"


@contextlib.contextmanager
def single_table_population_model():
    """Declare the synthetic ``make_bound_query`` model as ONE relation.

    Bug-8580 added a row-population equivalence gate to ``find_best_pocket``:
    a pocket may only serve when the tables its ``SELECT *`` materialise plan
    joined are provably the same row population the query's own compiled plan
    would produce, and anything unproven fails closed to source.

    The synthetic fixtures in this conftest model a single-relation query
    (``FROM sales``) with SimpleNamespace dimensions that carry no physical
    column binding, so the real resolvers cannot derive a graph from them. This
    context manager states that shape explicitly — one table, no joins, the
    query plans over that table — which is exactly the population situation
    those tests were always written against. It asserts nothing and hides no
    lossy join: with one relation and zero joins, the pocket and query plans are
    identical by construction.

    Tests that exercise the gate itself build real multi-table graphs instead
    (see ``test_bug_8580_pocket_join_population.py``).
    """
    with patch(
        "src.routing.pocket_matcher._load_model_join_graph",
        new=AsyncMock(return_value=(_synthetic_graph(), {})),
    ), patch(
        "src.routing.pocket_matcher._query_plan_table_ids",
        new=lambda *_a, **_k: {_SYNTHETIC_BASE_TABLE_ID},
    ):
        yield


def _synthetic_graph():
    from src.routing.pocket_population import ModelJoinGraph

    return ModelJoinGraph(
        table_ids=frozenset({_SYNTHETIC_BASE_TABLE_ID}),
        anchor_table_id=_SYNTHETIC_BASE_TABLE_ID,
    )


class _EveryName(frozenset):
    """A declared-vocabulary stand-in: the one-relation model declares it all."""

    def __contains__(self, item):  # noqa: D105
        return True


class _OneRelation(dict):
    """A ``{object -> owning relation}`` map for a model with ONE relation."""

    def __init__(self, value):
        super().__init__()
        self._value = value

    def get(self, key, default=None):  # noqa: D102
        return self._value


def _synthetic_object_index():
    """``AggregateObjectIndex`` for the synthetic one-relation model (Bug-8664).

    The aggregate row-population gate resolves each grain name and measure id to
    its owning relation and refuses when one cannot be resolved. The synthetic
    fixtures carry ``SimpleNamespace`` dimensions/measures with no physical
    binding, so nothing resolves — the same situation
    ``single_table_population_model`` already states for the pocket route. With
    ONE relation and zero joins the aggregate's plan and the query's plan are
    the same single relation by construction, so this states that and hides no
    lossy join.
    """
    from src.routing.aggregate_population import AggregateObjectIndex

    return AggregateObjectIndex(
        dimension_names=_EveryName(),
        table_by_dimension_name=_OneRelation(_SYNTHETIC_BASE_TABLE_ID),
        measure_id_by_name=_OneRelation("m-synthetic"),
        table_by_measure_id=_OneRelation(_SYNTHETIC_BASE_TABLE_ID),
        expression_by_measure_id={},
    )


@pytest.fixture(autouse=True)
def _synthetic_population_world(request):
    """Give the synthetic fixture model a coherent ONE-RELATION population world.

    Bug-8580 (pockets) and Bug-8664 (aggregates) both refuse to serve when the
    artifact's row population cannot be PROVEN equal to the query's own plan,
    and both fail closed when the model's join graph cannot be resolved at all.
    Every synthetic query in this conftest runs against an ``AsyncMock`` session,
    so the real resolvers legitimately resolve nothing — which would refuse every
    pocket and every aggregate in ~130 pre-existing tests that are about
    something else entirely.

    The discriminator is deliberately NOT the model id: it is whether the REAL
    resolver produced a graph. A test that patches ``resolve_deployed_shape``
    with a genuine multi-relation snapshot (``test_bug_8580_*``, the Bug-8664
    suite) therefore keeps the real graph and the real verdict, while a test
    running on a bare mock gets the one-relation world its fixtures describe.
    Nothing here can turn a resolvable lossy graph into a proven one.

    A test that asserts what the REAL resolvers do when they cannot resolve —
    the fail-closed leg itself — marks itself ``real_population_resolvers`` and
    this fixture stands aside. Without that opt-out this fixture would make the
    fail-closed guard untestable, which is the one thing it must never do.
    """
    if request.node.get_closest_marker("real_population_resolvers") is not None:
        yield
        return

    from src.routing import aggregate_population as _ap
    from src.routing import pocket_matcher as _pm

    synthetic = _synthetic_graph()
    synthetic_index = _synthetic_object_index()
    real_graph = _pm._load_model_join_graph
    real_query_tables = _pm._query_plan_table_ids
    real_index = _ap.load_aggregate_object_index

    async def _graph(model, db):
        graph, uda_tables = await real_graph(model, db)
        if graph is not None:
            return graph, uda_tables
        return synthetic, {}

    def _query_tables(bound_query, graph, uda_tables):
        if graph is not synthetic:
            return real_query_tables(bound_query, graph, uda_tables)
        return {_SYNTHETIC_BASE_TABLE_ID}

    async def _index(model, db, *, graph, table_id_by_uda_id):
        if graph is not synthetic:
            return await real_index(
                model, db, graph=graph, table_id_by_uda_id=table_id_by_uda_id,
            )
        return synthetic_index

    # ``AggregatePopulationChecker`` reaches both resolvers through their module
    # globals at call time, so patching the defining module is enough and there
    # is no by-name import in ``aggregate_matcher`` to patch separately.
    with patch.object(_pm, "_load_model_join_graph", _graph), patch.object(
        _pm, "_query_plan_table_ids", _query_tables
    ), patch.object(_ap, "load_aggregate_object_index", _index):
        yield


# ---------------------------------------------------------------------------
# Lightweight mock factories (use SimpleNamespace — no SQLAlchemy needed)
# ---------------------------------------------------------------------------

class _AnyJwtRole:
    """Test-only LocalUser role sentinel that matches the JWT role claim."""

    def __eq__(self, other):
        return isinstance(other, str)

    def __ne__(self, other):
        return not self.__eq__(other)


# ---------------------------------------------------------------------------
# Bug-7266: provide a deterministic HMAC signing key for cursor tests so
# keyset cursor signing works without requiring .env to be sourced.
# ---------------------------------------------------------------------------
_TEST_CURSOR_SIGNING_KEY = b"test-cursor-signing-key-for-unit-tests-only"


@pytest.fixture(autouse=True)
def _patch_cursor_signing_key():
    with patch(
        "src.drill.cursor._signing_key",
        return_value=_TEST_CURSOR_SIGNING_KEY,
    ):
        yield


@pytest.fixture(autouse=True)
def _regular_session_local_user_lookup():
    local_user = types.SimpleNamespace(
        email="user@example.com",
        is_active=True,
        role=_AnyJwtRole(),
        token_version=0,
    )
    result = types.SimpleNamespace(scalar_one_or_none=lambda: local_user)

    class _DB:
        async def execute(self, _stmt):
            return result

    async def _gen(_tenant_id):
        yield _DB()

    with patch("shared.auth.middleware.get_tenant_db", _gen):
        yield


def _fixture_row_to_snapshot_dict(row) -> dict:
    """Serialise a fixture row (SimpleNamespace or ORM instance) to a snapshot
    dict, the same shape ``shared/model_snapshot/serialiser.py`` persists."""
    if isinstance(row, dict):
        return dict(row)
    table = getattr(type(row), "__table__", None)
    if table is not None:
        return {c.name: getattr(row, c.name, None) for c in table.columns}
    return dict(vars(row))


def deployed_shape_from_rows(
    *, tables=(), columns=(), joins=(), udas=(),
    measures=(), dimensions=(),
):
    """Build a deployed-snapshot shape double from live-style fixture rows.

    Bug-7981: a DEPLOYED model builds its physical graph from its pinned
    snapshot and has NO live-ORM fallback. An offline render test that mocks
    live ``ModelTable``/``ModelColumn``/``Join``/``UDA`` rows and marks its
    model deployed must therefore hand the SAME rows to the graph loader as a
    snapshot, so the test exercises the production deployed path instead of an
    authoring path that a deployed model can never take.

    Only the physical-graph families are populated from the caller's rows; the
    semantic families default to empty because these fixtures pass their
    measures/dimensions to ``BoundQuery`` directly.
    """
    return types.SimpleNamespace(
        tables_by_id={
            str(getattr(t, "id", "")): _fixture_row_to_snapshot_dict(t)
            for t in tables
        },
        columns_by_id={
            str(getattr(c, "id", "")): _fixture_row_to_snapshot_dict(c)
            for c in columns
        },
        join_rows=[_fixture_row_to_snapshot_dict(j) for j in joins],
        user_defined_attribute_rows=[
            _fixture_row_to_snapshot_dict(u) for u in udas
        ],
        measures=list(measures),
        dimensions=list(dimensions),
        hierarchy_rows=[],
        hidden_column_ids=set(),
        physical_columns_all={
            (getattr(c, "column_name", "") or "").lower() for c in columns
        } - {""},
        physical_columns_visible={
            (getattr(c, "column_name", "") or "").lower() for c in columns
        } - {""},
        physical_column_ids={},
        attribute_relationships=[],
        qualified_column_ids={},
        table_name_ids={},
        dimensions_by_id={},
    )


async def attach_fixture_deployed_shape(bound, db):
    """Pin a deployed snapshot on ``bound`` built from the rows ``db`` serves.

    Bug-7981: a DEPLOYED model has no live-ORM graph fallback — its physical
    graph comes from the pinned snapshot or the query is blocked. Offline
    render tests mark their model deployed but hand the loader a DB double that
    answers the LIVE graph selects. This helper issues those same selects
    against the double and republishes the result as the request's deployed
    snapshot, so the test exercises the production deployed path with exactly
    the rows it already intended to supply.

    Only for tests. A family the double cannot serve is treated as empty.
    """
    from sqlalchemy import select as sa_select
    from shared.db.models import (
        Join, ModelColumn, ModelTable, UserDefinedAttribute,
    )

    model = bound.model

    async def _rows(stmt) -> list:
        try:
            result = await db.execute(stmt)
            rows = result.scalars().all()
        except Exception:
            return []
        return list(rows) if isinstance(rows, (list, tuple)) else []

    columns = await _rows(
        sa_select(ModelColumn).where(ModelColumn.model_table_id.in_(
            sa_select(ModelTable.id).where(ModelTable.model_id == model.id)
        ))
    )
    udas = await _rows(
        sa_select(UserDefinedAttribute).where(
            UserDefinedAttribute.model_id == model.id
        )
    )
    tables = await _rows(
        sa_select(ModelTable).where(ModelTable.model_id == model.id)
    )
    joins = await _rows(sa_select(Join).where(Join.model_id == model.id))

    bound.deployed_shape = deployed_shape_from_rows(
        tables=tables, columns=columns, joins=joins, udas=udas,
    )
    return bound


def make_measure(
    name: str,
    default_agg: str = "sum",
    is_additive: bool = True,
    *,
    measure_type: str = "standard",
    expression: str | None = None,
    calc_agg_mode: str | None = None,
    semi_additive_behavior: str | None = None,
    variant_kind: str | None = None,
) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=f"m-{name}",
        name=name,
        default_agg=default_agg,
        is_additive=is_additive,
        measure_type=measure_type,
        expression=expression,
        calc_agg_mode=calc_agg_mode,
        semi_additive_behavior=semi_additive_behavior,
        variant_kind=variant_kind,
    )


def make_dimension(name: str) -> types.SimpleNamespace:
    return types.SimpleNamespace(id=f"d-{name}", name=name)


def make_hierarchy_dimension(
    name: str,
    *,
    hierarchy_id: str = "h-1",
    hierarchy_name: str = "Date",
    ordinal: int = 1,
) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=f"hlevel-{name}",
        name=name,
        is_hierarchy_level=True,
        hierarchy_id=hierarchy_id,
        hierarchy_name=hierarchy_name,
        hierarchy_level_id=f"lvl-{name}",
        hierarchy_level_ordinal=ordinal,
        dimension_kind="time",
        is_time_dim=True,
    )


def make_agg_col(
    measure: types.SimpleNamespace,
    stat_type: str | None = None,
) -> types.SimpleNamespace:
    st = stat_type or measure.default_agg or "sum"
    return types.SimpleNamespace(
        measure=measure,
        stat_type=st,
        physical_col_name=f"{measure.name}__{st}",
    )


def make_aggregate(
    grain: list[str],
    columns: list,
    *,
    status: str = "active",
    age_hours: int = 0,
    agg_id: str = "agg-1",
    physical_table_name: str = "agg_table",
    target_schema: str = "aggregates",
    persona_id: str | None = None,
    built_for_version_id: str | None = "v1",
    built_for_epoch: int | None = 0,
) -> types.SimpleNamespace:
    last_refreshed = datetime.now(timezone.utc) - timedelta(hours=age_hours)
    # F-013-02 (Bug-8250): the matcher requires an artifact built for the model's
    # currently-deployed (version_id, epoch). make_bound_query deploys the test
    # model at ("v1", epoch 0), so a fixture aggregate defaults to that binding
    # and remains servable; tests exercising the version gate pass a mismatched
    # (or None) built_for_version_id / built_for_epoch to prove it fails closed.
    return types.SimpleNamespace(
        id=agg_id,
        grain=grain,
        columns=columns,
        status=status,
        last_refreshed_at=last_refreshed,
        physical_table_name=physical_table_name,
        target_schema=target_schema,
        persona_id=persona_id,
        built_for_version_id=built_for_version_id,
        built_for_epoch=built_for_epoch,
    )


def make_bound_query(
    dimensions: list,
    measures: list,
    *,
    filters=None,
    raw_sql: str = "SELECT 1",
    order_by=None,
    limit=None,
    offset=None,
    grain=None,
    select_star: bool = False,
    has_distinct: bool = False,
    all_dimensions: list | None = None,
):
    from src.ir.logical_query import LogicalQuery, BoundQuery

    model = types.SimpleNamespace(id="model-1", slug="test_model", deployed_version_id="v1")
    lq = LogicalQuery(
        model_id="model-1",
        protocol="jdbc",
        raw_query=raw_sql,
        requested_measures=[m.name for m in measures],
        requested_dimensions=[d.name for d in dimensions],
        filters=filters or [],
        grain=grain if grain is not None else [d.name for d in dimensions],
        order_by=order_by or [],
        limit=limit,
        offset=offset,
        query_fingerprint="test_fingerprint_abc123",
        select_star=select_star,
        has_distinct=has_distinct,
    )
    dim_map = {d.name: d for d in dimensions}
    if all_dimensions:
        for d in all_dimensions:
            dim_map.setdefault(d.name, d)
    return BoundQuery(
        logical_query=lq,
        model=model,
        resolved_measures=measures,
        resolved_dimensions=dimensions,
        resolved_filters=filters or [],
        resolved_dimensions_by_name=dim_map,
    )


@pytest.fixture(autouse=True)
def _patch_inactive_aggregates(request):
    # Bug-874: clear module-level join graph cache between tests to prevent
    # stale column references from polluting subsequent test runs.
    from src.rewrite.query_rewriter import invalidate_join_graph_cache
    invalidate_join_graph_cache()
    # F-003-14: clear the versioned deployed-shape and live-metadata caches so a
    # prior test's mocked metadata (same (model_id, deployed_version_id) key)
    # cannot leak into the next. These caches are keyed by the deploy version, so
    # in production they self-invalidate on re-deploy; only tests reuse a key with
    # different fixture data, so the clear is a test-isolation concern only.
    from src.semantic.snapshot_resolver import (
        invalidate as _invalidate_snapshot_cache,
        invalidate_live_metadata as _invalidate_live_metadata_cache,
        reset_request_pins as _reset_request_pins,
    )
    _invalidate_snapshot_cache()
    _invalidate_live_metadata_cache()
    # Bug-7981: drop any request-scoped deployment pin. Production gets a fresh
    # contextvar context per HTTP request; tests must get one per test.
    _reset_request_pins()
    # F-004-17: clear the per-version canonical dimension cache so a prior test's
    # patched/empty list cannot leak into the next test under the same key.
    from src.routing.aggregate_matcher import invalidate_canonical_dim_cache
    invalidate_canonical_dim_cache()
    # Bug-7000: clear pocket matcher caches between tests to prevent stale
    # parsed SQL trees or model table identifier sets from leaking.
    # Bug-8580: the row-population join graph is cached on the same key shape
    # and must be cleared here too, or one test's fixture graph decides another
    # test's pocket route (only masked today by ``-p no:randomly``).
    from src.routing.pocket_matcher import (
        invalidate_model_join_graph_cache,
        invalidate_model_table_cache,
        invalidate_parsed_sql_cache,
    )
    invalidate_model_table_cache()
    invalidate_model_join_graph_cache()
    invalidate_parsed_sql_cache()

    # Bug-6977: for a deployed model, _get_canonical_dims_cached now uses the
    # deployed snapshot as the sole authority (never reads live tables). Provide
    # a default empty snapshot shape so existing tests that do not care about
    # canonical dims get empty dims (matching the prior build_canonical_dim...
    # mock). Tests that need specific canonical dims must override this patch
    # with their own snapshot shape.
    # The empty shape must carry every attribute any consumer of DeployedShape
    # reads (binder, aggregate_matcher, pocket_matcher, snapshot_graph_resolvers,
    # calendar_support, the parameter binder). It used to be a hand-written
    # ``SimpleNamespace`` listing them one by one, which is a duplicate of the
    # dataclass that silently goes stale: every field added to DeployedShape had
    # to be remembered here too, and the annotations above it (A1, A2, Bug-7000,
    # F-013-01, F-016-02) are the archaeology of forgetting. Constructing the
    # REAL dataclass makes the drift unrepresentable — a new field arrives with
    # its own default and the fixture is correct by construction.
    _empty_shape = _DeployedShape(
        measures=[],
        dimensions=[],
        hidden_column_ids=set(),
        physical_columns_all=set(),
        physical_columns_visible=set(),
    )

    # A test that asserts what the REAL snapshot resolver does — that a genuine
    # ``snapshot_json`` produces the right pinned shape, or that an unusable one
    # fails closed — marks itself ``real_snapshot_resolver`` and keeps the real
    # function. Without the opt-out the always-succeeds empty shape would make
    # the fail-closed leg untestable, which is the one thing a fixture that
    # exists for convenience must never do. Same precedent and same reasoning as
    # ``real_population_resolvers`` above.
    _real_snapshot = (
        request.node.get_closest_marker("real_snapshot_resolver") is not None
    )
    _shape_patches = (
        []
        if _real_snapshot
        else [
            patch(
                "src.semantic.snapshot_resolver.resolve_deployed_shape",
                new_callable=AsyncMock,
                return_value=_empty_shape,
            ),
            # A2: the binder imports resolve_deployed_shape at module level,
            # binding the name into its own namespace. Patching the
            # snapshot_resolver copy alone does not replace the binder's bound
            # reference, so a deployed test model hits the fail-closed 503
            # instead of getting the empty shape. Patch the binder's copy too.
            # Similarly, the A2 snapshot_graph_resolvers module imports it
            # lazily, so patching at the module level covers it.
            patch(
                "src.semantic.binder.resolve_deployed_shape",
                new_callable=AsyncMock,
                return_value=_empty_shape,
            ),
        ]
    )

    with (
        _ExitStack() as _shape_stack,
        patch(
            "src.routing.aggregate_matcher.load_inactive_aggregates",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "shared.semantic.canonical_dimensions.build_canonical_dimension_list",
            new_callable=AsyncMock,
            return_value=[],
        ),
        # F-004-14: ``router.resolve_target_dialect`` was an unused import,
        # removed from router.py; only ``resolve_target_dialect_for_bound`` is
        # actually called. The stale patch on the removed symbol is dropped.
        patch(
            "src.routing.router.resolve_target_dialect_for_bound",
            new_callable=AsyncMock,
            return_value="postgres",
        ),
        patch(
            "src.rewrite.source_sql._resolve_target_dialect",
            new_callable=AsyncMock,
            return_value="postgres",
        ),
        patch(
            "src.routing.pocket_matcher.get_setting",
            new_callable=AsyncMock,
            return_value=False,
        ),
    ):
        for _p in _shape_patches:
            _shape_stack.enter_context(_p)
        yield


# ---------------------------------------------------------------------------
# Bug-8532: surface the live-tier state in the run summary
# ---------------------------------------------------------------------------
#
# The silent skip is fixed at its source (test_batch_queries.py now errors on a
# present-but-broken gateway instead of skipping), so only a GENUINELY ABSENT
# stack can still remove the 174 JDBC cases. That skip is defensible -- this
# suite is a coding tier and its e2e subset is opt-in, so a developer without
# Docker must not get 174 red tests -- but it must not be quiet either: on
# 2026-08-03 two runs of the same command over the same 3678 collected tests
# reported "3501 passed, 176 skipped" and "22 failed, 3646 passed", and only the
# matching totals revealed that 174 tests had vanished rather than passed.
#
# So the summary states it outright. Exit codes are deliberately untouched here.

_BATCH_MODULE = "test_batch_queries.py"


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    import sys

    # Look the module up by file rather than by dotted name: the name pytest
    # gives it depends on rootdir and import mode, and a name-based lookup that
    # silently misses would make this banner quietly stop appearing -- which is
    # the same class of defect it exists to report.
    readiness = None
    for module in list(sys.modules.values()):
        if getattr(module, "__file__", None) and str(module.__file__).endswith(
            _BATCH_MODULE
        ):
            readiness = getattr(module, "_READINESS", None)
            break
    if readiness is None:
        return  # the batch module was not collected in this run

    executed = sum(
        1
        for outcome in ("passed", "failed", "xfailed", "xpassed", "error")
        for report in terminalreporter.stats.get(outcome, [])
        if _BATCH_MODULE in getattr(report, "nodeid", "")
    )
    if executed:
        return

    terminalreporter.write_sep("=", "live JDBC tier did not run", red=True)
    terminalreporter.write_line(
        f"  [{readiness.readiness.value}] 174 test_batch_query cases were NOT "
        f"executed."
    )
    terminalreporter.write_line(f"  reason: {readiness.detail}")
    terminalreporter.write_line(
        "  This run carries NO live JDBC-gateway evidence. The pass count above "
        "is coding-tier only."
    )
