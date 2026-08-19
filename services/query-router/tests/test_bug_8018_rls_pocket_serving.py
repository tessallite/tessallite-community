"""Bug-8018 — RLS-safe pocket serving.

Bug-7033 left pockets CATEGORICALLY unserved under active row-level security.
Bug-8018 removes that categorical block and serves a pocket to an RLS principal
ONLY when it is PROVABLY RLS-correct, WITHOUT ever returning a forbidden row and
without a fail-closed 502. A pocket is proven safe only when:

  * ``security_dimension_columns`` is non-empty and no user_mapping rule applies;
  * the defining SQL is a row-preserving ``SELECT * FROM <table> [WHERE ...]``
    (no LIMIT / TABLESAMPLE / DISTINCT / GROUP / QUALIFY / JOIN / subquery / CTE /
    table-function / projection — finding [C]); AND
  * every security column is a MATERIALISED output column of the pocket, proven
    against the pocket's own ``row_manifest.columns`` — NOT inferred from the
    model shape, because a pocket materialises only its branch-dependent VISIBLE
    projection (finding [A]).

When served, the predicate is injected per-scan into the pocket-rewritten SQL
(route_type="pocket", predicate present, ``security_compiled`` carried for the
cache-miss source fallback). Structural source-only shapes (DAX time-variant,
disabled model, disabled aggregations, invalid objects) never serve a pocket
(finding [2]). Anything unproven falls back to source with the predicate.

Mutation guard: the predicate-injection is what prevents the leak — a pocket
route that returned the raw pocket SQL WITHOUT injection would fail the leak
assertions here.
"""
from __future__ import annotations

import types
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from src.routing.router import (
    route_query,
    _pocket_is_rls_safe,
    _pocket_projects_all_columns,
    _pocket_materialised_columns,
    NoAggregateMatchError,
)
from src.routing.pocket_matcher import PocketSkipReason
from src.security import CompiledPredicate, Principal
from src.ir.logical_query import PocketMatchResult

from conftest import make_aggregate, make_agg_col, make_dimension, make_measure
from test_query_flow import _bind
from test_row_security_routing import _role_rule, _user_mapping_rule, _db_returning

_PATCH_LOAD = "src.routing.aggregate_matcher.load_active_aggregates"
_PATCH_POCKET = "src.routing.router.find_best_pocket"
_PATCH_REWRITE = "src.routing.router.rewrite_for_pocket"


from shared.semantic.artifact_manifest import MANIFEST_VERSION

_RUN = "run-1"


def _manifest(cols, *, run_id=_RUN, version=MANIFEST_VERSION):
    if cols is None:
        return None
    return {
        "columns": [{"logical_name": c, "physical_column": c} for c in cols],
        "build_refresh_run_id": run_id,
        "manifest_version": version,
    }


def _pocket(
    defining_sql: str,
    *,
    pid: str = "pkt-rls",
    manifest_cols=("region_code", "flag", "revenue", "country"),
    run_id=_RUN,
    manifest=...,
):
    return types.SimpleNamespace(
        id=pid,
        model_id="model-1",
        status="fresh",
        query_fingerprint="fp-rls",
        last_refresh_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        predicates=[],
        physical_table_name="pocket_tbl",
        target_schema="public",
        defining_sql=defining_sql,
        active_refresh_run_id=_RUN,
        row_manifest=_manifest(manifest_cols, run_id=run_id) if manifest is ... else manifest,
    )


def _north_rule_principal_db():
    rule = _role_rule(
        "region.region_code",
        "dimension_equals('region.region_code', 'NORTH')",
        ["region_manager_north"],
    )
    principal = Principal(
        user_identity="alice@x", roles=frozenset({"region_manager_north"})
    )
    return rule, principal, _db_returning([rule])


# ---------------------------------------------------------------------------
# Unit: the row-preserving shape gate (finding [C] — positive whitelist)
# ---------------------------------------------------------------------------


def test_projects_all_columns_accepts_bare_star():
    assert _pocket_projects_all_columns(
        "SELECT * FROM sales WHERE region_code = 'NORTH'"
    ) is True
    assert _pocket_projects_all_columns("SELECT * FROM sales") is True


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT region_code, revenue FROM sales",              # projection drops cols
        "SELECT s.* FROM sales s",                              # qualified star
        "SELECT * EXCEPT (secret) FROM sales",                 # column removal
        "SELECT COUNT(*) FROM sales",                          # aggregate
        "SELECT * FROM sales GROUP BY region_code",            # row-collapsing
        "SELECT DISTINCT * FROM sales",                        # row-collapsing
        "SELECT * FROM sales LIMIT 5",                         # partial rows
        "SELECT * FROM sales OFFSET 10",                       # partial rows
        "SELECT * FROM sales ORDER BY region_code",            # ordering
        "SELECT * FROM sales TABLESAMPLE SYSTEM (10)",         # random subset
        "SELECT * FROM ONLY sales",                            # PG inheritance exclusion
        "SELECT * FROM sales AS s",                            # aliased FROM
        "SELECT * FROM sales QUALIFY row_number() OVER () = 1",  # row-collapsing
        "SELECT * FROM generate_series(1, 10)",                # table function
        "SELECT * FROM (SELECT region_code FROM sales) q",     # derived FROM
        "WITH c AS (SELECT region_code FROM sales) SELECT * FROM c",  # CTE
        "SELECT * FROM a JOIN b ON a.id = b.id",               # join
        "SELECT * FROM a, b",                                   # comma join
        "SELECT * FROM sales UNION ALL SELECT * FROM archive",  # set op
        "not valid sql (((",                                    # unparseable
        "",                                                     # empty
        None,                                                  # missing
    ],
)
def test_projects_all_columns_rejects_non_row_preserving(sql):
    assert _pocket_projects_all_columns(sql) is False


# ---------------------------------------------------------------------------
# Unit: the materialised-column manifest reader (finding [A]/[B])
# ---------------------------------------------------------------------------


def test_materialised_columns_from_manifest():
    pkt = _pocket("SELECT * FROM sales", manifest_cols=("region_code", "revenue"))
    assert _pocket_materialised_columns(pkt) == {"region_code", "revenue"}


@pytest.mark.parametrize("rm", [None, {}, {"columns": None}, {"columns": []}])
def test_materialised_columns_none_when_manifest_absent(rm):
    pkt = types.SimpleNamespace(row_manifest=rm, active_refresh_run_id=_RUN)
    assert _pocket_materialised_columns(pkt) is None


def test_materialised_columns_uses_physical_when_no_logical():
    pkt = types.SimpleNamespace(
        active_refresh_run_id=_RUN,
        row_manifest={
            "columns": [{"physical_column": "region_code"}],
            "build_refresh_run_id": _RUN,
            "manifest_version": MANIFEST_VERSION,
        },
    )
    assert _pocket_materialised_columns(pkt) == {"region_code"}


def test_materialised_columns_none_when_manifest_is_stale_build():
    """Finding [1]: the manifest describes a DIFFERENT (previous) refresh run
    than the pocket's active run -> fail closed (the new build may have dropped
    the column the stale manifest still lists)."""
    pkt = _pocket("SELECT * FROM sales", manifest_cols=("region_code",),
                  run_id="stale-run")
    assert _pocket_materialised_columns(pkt) is None


def test_materialised_columns_none_on_unknown_manifest_version():
    pkt = _pocket("SELECT * FROM sales", manifest_cols=("region_code",),
                  manifest={
                      "columns": [{"logical_name": "region_code"}],
                      "build_refresh_run_id": _RUN,
                      "manifest_version": 999999,
                  })
    assert _pocket_materialised_columns(pkt) is None


def test_materialised_columns_preserves_exact_case():
    """Finding [4]: names are matched case-sensitively (injection quotes the
    column case-sensitively) — both reject AND accept directions."""
    pkt = _pocket("SELECT * FROM sales", manifest_cols=("Region_Code",))
    assert _pocket_materialised_columns(pkt) == {"Region_Code"}
    # Reject: a predicate for lowercase 'region_code' must NOT match 'Region_Code'.
    assert _pocket_is_rls_safe(pkt, _PRED) is False
    # Accept: a predicate for the exact mixed-case column IS safe (guards a
    # regression that case-mangles the accept side and silently kills serving).
    mixed_pred = CompiledPredicate(
        sql_expression='"Region_Code" = \'NORTH\'',
        active_rule_ids=("r1",),
        security_dimension_columns=("Region_Code",),
    )
    assert _pocket_is_rls_safe(pkt, mixed_pred) is True


def test_materialised_columns_none_on_malformed_entry():
    """A non-dict column entry means a malformed manifest -> fail closed."""
    pkt = types.SimpleNamespace(
        active_refresh_run_id=_RUN,
        row_manifest={
            "columns": [{"logical_name": "region_code"}, "junk"],
            "build_refresh_run_id": _RUN,
            "manifest_version": MANIFEST_VERSION,
        },
    )
    assert _pocket_materialised_columns(pkt) is None


# ---------------------------------------------------------------------------
# Unit: the RLS-safety gate
# ---------------------------------------------------------------------------

_PRED = CompiledPredicate(
    sql_expression="\"region_code\" = 'NORTH'",
    active_rule_ids=("r1",),
    security_dimension_columns=("region_code",),
)


def test_rls_safe_true_for_covered_bare_star_pocket():
    pkt = _pocket("SELECT * FROM sales WHERE region_code = 'NORTH'",
                  manifest_cols=("region_code", "revenue"))
    assert _pocket_is_rls_safe(pkt, _PRED) is True


def test_rls_safe_false_when_security_column_not_materialised():
    """Finding [A]/[1]: the security column is NOT in the pocket's materialised
    manifest columns (hidden / joined-table / not projected) -> never serve."""
    pkt = _pocket("SELECT * FROM sales WHERE region_code = 'NORTH'",
                  manifest_cols=("country", "revenue"))
    assert _pocket_is_rls_safe(pkt, _PRED) is False


def test_rls_safe_false_when_manifest_absent():
    """Finding [A]: cannot prove pocket contents -> fail closed to source."""
    pkt = _pocket("SELECT * FROM sales WHERE region_code = 'NORTH'",
                  manifest_cols=None)
    assert _pocket_is_rls_safe(pkt, _PRED) is False


def test_rls_safe_false_for_non_row_preserving_pocket():
    pkt = _pocket("SELECT region_code, revenue FROM sales",
                  manifest_cols=("region_code",))
    assert _pocket_is_rls_safe(pkt, _PRED) is False


def test_rls_safe_false_when_no_security_columns():
    pkt = _pocket("SELECT * FROM sales")
    deny_all = CompiledPredicate(
        sql_expression="0 = 1", active_rule_ids=("r1",),
        security_dimension_columns=(),
    )
    assert _pocket_is_rls_safe(pkt, deny_all) is False


def test_rls_safe_false_for_user_mapping_predicate():
    pkt = _pocket("SELECT * FROM sales")
    mapping_pred = CompiledPredicate(
        sql_expression='"region_code" IN (SELECT "region_code" FROM m)',
        active_rule_ids=("r1",),
        security_dimension_columns=("region_code",),
        mapping_source_ids=("src-1",),
    )
    assert _pocket_is_rls_safe(pkt, mapping_pred) is False


# ---------------------------------------------------------------------------
# Integration through route_query
# ---------------------------------------------------------------------------


async def test_rls_principal_served_from_covered_pocket():
    """A bare SELECT * pocket whose manifest covers the security column IS served
    under RLS, with the predicate injected into the pocket-targeted SQL."""
    m = make_measure("revenue")
    d = make_dimension("region_code")
    sql = "SELECT region_code, SUM(revenue) FROM sales GROUP BY region_code"
    bq = _bind(sql, [m], [d])
    _rule, principal, db = _north_rule_principal_db()
    pkt = _pocket("SELECT * FROM sales WHERE region_code = 'NORTH'")

    with (
        patch(_PATCH_LOAD, new_callable=AsyncMock) as mock_load,
        patch(_PATCH_POCKET, new_callable=AsyncMock) as mock_pocket,
        patch(_PATCH_REWRITE) as mock_rewrite,
    ):
        mock_load.return_value = []
        mock_pocket.return_value = PocketMatchResult(pocket=pkt)
        mock_rewrite.return_value = (
            "SELECT region_code, SUM(revenue) FROM pocket_tbl GROUP BY region_code"
        )
        decision = await route_query(bq, db, principal=principal)

    assert decision.route_type == "pocket"
    assert decision.pocket_id == str(pkt.id)
    assert decision.aggregate_id is None
    # LEAK GUARD (known answer): the RLS predicate is applied ON the pocket scan,
    # so only NORTH rows are returned. Reverting the injection (serving the raw
    # pocket SQL) would drop this predicate and leak every region's rows.
    assert "\"region_code\" = 'NORTH'" in decision.rewritten_query
    assert "pocket_tbl" in decision.rewritten_query
    assert "RLS-safe pocket" in decision.reason
    # MANDATORY leak-vector guard: the compiled predicate must ride on the
    # decision so the routed-cache-table-missing fallback (routes.py) re-injects
    # it into the source SQL instead of serving UNFILTERED source rows.
    assert decision.security_compiled is not None


async def test_rls_uncovered_security_column_falls_to_source_not_502():
    """Finding [A]/[1]: when the security column is NOT in the pocket's
    materialised manifest, the pocket is NOT served — source route, no 502."""
    m = make_measure("revenue")
    d = make_dimension("region_code")
    sql = "SELECT region_code, SUM(revenue) FROM sales GROUP BY region_code"
    bq = _bind(sql, [m], [d])
    _rule, principal, db = _north_rule_principal_db()
    # region_code NOT materialised into the pocket (e.g. hidden / joined table).
    pkt = _pocket("SELECT * FROM sales WHERE region_code = 'NORTH'",
                  manifest_cols=("country", "revenue"))

    with (
        patch(_PATCH_LOAD, new_callable=AsyncMock) as mock_load,
        patch(_PATCH_POCKET, new_callable=AsyncMock) as mock_pocket,
        patch(_PATCH_REWRITE) as mock_rewrite,
    ):
        mock_load.return_value = []
        mock_pocket.return_value = PocketMatchResult(pocket=pkt)
        mock_rewrite.return_value = (
            "SELECT region_code, SUM(revenue) FROM pocket_tbl GROUP BY region_code"
        )
        decision = await route_query(bq, db, principal=principal)

    assert decision.route_type == "source"
    assert decision.pocket_id is None
    assert "pocket_tbl" not in decision.rewritten_query
    assert "\"region_code\" = 'NORTH'" in decision.rewritten_query
    assert decision.pocket_skipped_reason == PocketSkipReason.NOT_RLS_SAFE


async def test_rls_pocket_without_manifest_falls_to_source():
    """Finding [A]: a pocket with no row_manifest cannot prove its contents ->
    source route (the current default until the manifest producer lands)."""
    m = make_measure("revenue")
    d = make_dimension("region_code")
    sql = "SELECT region_code, SUM(revenue) FROM sales GROUP BY region_code"
    bq = _bind(sql, [m], [d])
    _rule, principal, db = _north_rule_principal_db()
    pkt = _pocket("SELECT * FROM sales WHERE region_code = 'NORTH'",
                  manifest_cols=None)

    with (
        patch(_PATCH_LOAD, new_callable=AsyncMock) as mock_load,
        patch(_PATCH_POCKET, new_callable=AsyncMock) as mock_pocket,
        patch(_PATCH_REWRITE) as mock_rewrite,
    ):
        mock_load.return_value = []
        mock_pocket.return_value = PocketMatchResult(pocket=pkt)
        mock_rewrite.return_value = "SELECT region_code FROM pocket_tbl"
        decision = await route_query(bq, db, principal=principal)

    assert decision.route_type == "source"
    assert decision.pocket_id is None
    assert "pocket_tbl" not in decision.rewritten_query
    assert "\"region_code\" = 'NORTH'" in decision.rewritten_query
    assert decision.pocket_skipped_reason == PocketSkipReason.NOT_RLS_SAFE


async def test_rls_dax_time_variant_never_served_from_pocket():
    """Finding [2]: a DAX time-variant query must go to source under RLS — a
    pocket would serve the BASE measure, not the variant (silent wrong number).
    The pocket matcher must not even be consulted."""
    m = make_measure("revenue")
    d = make_dimension("region_code")
    sql = "SELECT region_code, SUM(revenue) FROM sales GROUP BY region_code"
    bq = _bind(sql, [m], [d])
    bq.logical_query.time_variant_hints = {"revenue": "TOTALYTD"}
    _rule, principal, db = _north_rule_principal_db()
    pkt = _pocket("SELECT * FROM sales WHERE region_code = 'NORTH'")

    with (
        patch(_PATCH_LOAD, new_callable=AsyncMock) as mock_load,
        patch(_PATCH_POCKET, new_callable=AsyncMock) as mock_pocket,
        patch(_PATCH_REWRITE) as mock_rewrite,
    ):
        mock_load.return_value = []
        mock_pocket.return_value = PocketMatchResult(pocket=pkt)
        mock_rewrite.return_value = "SELECT region_code FROM pocket_tbl"
        decision = await route_query(bq, db, principal=principal)

    assert decision.route_type == "source"
    assert decision.pocket_id is None
    assert "pocket_tbl" not in decision.rewritten_query
    assert "\"region_code\" = 'NORTH'" in decision.rewritten_query
    # The pocket matcher must NEVER be consulted for a structurally source-only
    # query — otherwise a base-measure pocket could win.
    mock_pocket.assert_not_called()


async def test_rls_principal_falls_to_source_when_pocket_is_projection():
    """A pocket that is NOT a row-preserving bare SELECT * is never served."""
    m = make_measure("revenue")
    d = make_dimension("region_code")
    sql = "SELECT region_code, SUM(revenue) FROM sales GROUP BY region_code"
    bq = _bind(sql, [m], [d])
    _rule, principal, db = _north_rule_principal_db()
    pkt = _pocket("SELECT region_code, revenue FROM sales")  # projection => unsafe

    with (
        patch(_PATCH_LOAD, new_callable=AsyncMock) as mock_load,
        patch(_PATCH_POCKET, new_callable=AsyncMock) as mock_pocket,
        patch(_PATCH_REWRITE) as mock_rewrite,
    ):
        mock_load.return_value = []
        mock_pocket.return_value = PocketMatchResult(pocket=pkt)
        mock_rewrite.return_value = "SELECT region_code FROM pocket_tbl"
        decision = await route_query(bq, db, principal=principal)

    assert decision.route_type == "source"
    assert decision.pocket_id is None
    assert "pocket_tbl" not in decision.rewritten_query
    assert "\"region_code\" = 'NORTH'" in decision.rewritten_query
    assert decision.pocket_skipped_reason == PocketSkipReason.NOT_RLS_SAFE


async def test_rls_pocket_beats_rls_safe_aggregate():
    """Precedence invariant: when both a safe pocket and a safe aggregate match
    under RLS, the pocket wins (mirrors the non-RLS pocket-beats-aggregate)."""
    m = make_measure("revenue")
    d = make_dimension("region_code")
    agg = make_aggregate(["region_code"], [make_agg_col(m)])
    sql = "SELECT region_code, SUM(revenue) FROM sales GROUP BY region_code"
    bq = _bind(sql, [m], [d])
    _rule, principal, db = _north_rule_principal_db()
    pkt = _pocket("SELECT * FROM sales WHERE region_code = 'NORTH'")

    with (
        patch(_PATCH_LOAD, new_callable=AsyncMock) as mock_load,
        patch(_PATCH_POCKET, new_callable=AsyncMock) as mock_pocket,
        patch(_PATCH_REWRITE) as mock_rewrite,
    ):
        mock_load.return_value = [agg]
        mock_pocket.return_value = PocketMatchResult(pocket=pkt)
        mock_rewrite.return_value = (
            "SELECT region_code, SUM(revenue) FROM pocket_tbl GROUP BY region_code"
        )
        decision = await route_query(bq, db, principal=principal)

    assert decision.route_type == "pocket"
    assert decision.aggregate_id is None
    assert "\"region_code\" = 'NORTH'" in decision.rewritten_query


async def test_rls_user_mapping_never_served_from_pocket():
    """user_mapping predicates reference a mapping table on the SOURCE
    connection; never served from a pocket — source route only."""
    m = make_measure("revenue")
    d = make_dimension("region_code")
    sql = "SELECT region_code, SUM(revenue) FROM sales GROUP BY region_code"
    bq = _bind(sql, [m], [d])

    table = types.SimpleNamespace(
        id=uuid.uuid4(), physical_name="demo_data.user_region_map",
        source_id=uuid.uuid4(),
    )
    rule = _user_mapping_rule(
        "region.region_code", table.id, "user_id", "region_code"
    )
    principal = Principal(user_identity="alice@x", roles=frozenset())
    db = _db_returning([rule], mapping_table=table)
    pkt = _pocket("SELECT * FROM sales")  # covered star, but mapping forces source

    with (
        patch(_PATCH_LOAD, new_callable=AsyncMock) as mock_load,
        patch(_PATCH_POCKET, new_callable=AsyncMock) as mock_pocket,
        patch(_PATCH_REWRITE) as mock_rewrite,
    ):
        mock_load.return_value = []
        mock_pocket.return_value = PocketMatchResult(pocket=pkt)
        mock_rewrite.return_value = "SELECT region_code FROM pocket_tbl"
        decision = await route_query(bq, db, principal=principal)

    assert decision.route_type == "source"
    assert decision.pocket_id is None
    assert "pocket_tbl" not in decision.rewritten_query
    assert '"region_code" IN (SELECT "region_code" FROM' in decision.rewritten_query
    # Finding [3]: a user_mapping rule can never yield an RLS-safe pocket, so the
    # (DB-hitting) pocket matcher must be precluded, not merely rejected after.
    mock_pocket.assert_not_called()


async def test_rls_deny_all_principal_never_consults_pocket_matcher():
    """Finding [3]: a deny-all principal (matches no rule -> no security columns)
    can never yield an RLS-safe pocket, so the pocket matcher is precluded; the
    query is served from source with the 0=1 deny-all predicate."""
    m = make_measure("revenue")
    d = make_dimension("region_code")
    sql = "SELECT region_code, SUM(revenue) FROM sales GROUP BY region_code"
    bq = _bind(sql, [m], [d])
    rule = _role_rule(
        "region.region_code",
        "dimension_equals('region.region_code', 'NORTH')",
        ["region_manager_north"],
    )
    # Principal matches NO rule -> fail-closed deny-all (empty security columns).
    principal = Principal(user_identity="bob@x", roles=frozenset({"viewer"}))
    db = _db_returning([rule])
    pkt = _pocket("SELECT * FROM sales WHERE region_code = 'NORTH'")

    with (
        patch(_PATCH_LOAD, new_callable=AsyncMock) as mock_load,
        patch(_PATCH_POCKET, new_callable=AsyncMock) as mock_pocket,
        patch(_PATCH_REWRITE) as mock_rewrite,
    ):
        mock_load.return_value = []
        mock_pocket.return_value = PocketMatchResult(pocket=pkt)
        mock_rewrite.return_value = "SELECT region_code FROM pocket_tbl"
        decision = await route_query(bq, db, principal=principal)

    assert decision.route_type == "source"
    assert decision.pocket_id is None
    assert "0 = 1" in decision.rewritten_query
    assert "pocket_tbl" not in decision.rewritten_query
    mock_pocket.assert_not_called()


async def test_force_route_pocket_served_when_rls_safe():
    """force_route='pocket' under RLS serves a proven, covered bare-star pocket."""
    m = make_measure("revenue")
    d = make_dimension("region_code")
    sql = "SELECT region_code, SUM(revenue) FROM sales GROUP BY region_code"
    bq = _bind(sql, [m], [d])
    _rule, principal, db = _north_rule_principal_db()
    pkt = _pocket("SELECT * FROM sales WHERE region_code = 'NORTH'")

    with (
        patch(_PATCH_LOAD, new_callable=AsyncMock) as mock_load,
        patch(_PATCH_POCKET, new_callable=AsyncMock) as mock_pocket,
        patch(_PATCH_REWRITE) as mock_rewrite,
    ):
        mock_load.return_value = []
        mock_pocket.return_value = PocketMatchResult(pocket=pkt)
        mock_rewrite.return_value = (
            "SELECT region_code, SUM(revenue) FROM pocket_tbl GROUP BY region_code"
        )
        decision = await route_query(
            bq, db, principal=principal, force_route="pocket",
        )

    assert decision.route_type == "pocket"
    assert "\"region_code\" = 'NORTH'" in decision.rewritten_query
    # The aggregate matcher must never be consulted under force_route='pocket'.
    mock_load.assert_not_called()


async def test_force_route_pocket_raises_with_skip_reason_when_not_rls_safe():
    """force_route='pocket' under RLS must RAISE (not silently source) when the
    only pocket is not RLS-safe, and the raise names the skip reason (finding [D])."""
    m = make_measure("revenue")
    d = make_dimension("region_code")
    sql = "SELECT region_code, SUM(revenue) FROM sales GROUP BY region_code"
    bq = _bind(sql, [m], [d])
    _rule, principal, db = _north_rule_principal_db()
    pkt = _pocket("SELECT region_code FROM sales")  # projection => unsafe

    with (
        patch(_PATCH_LOAD, new_callable=AsyncMock) as mock_load,
        patch(_PATCH_POCKET, new_callable=AsyncMock) as mock_pocket,
        patch(_PATCH_REWRITE) as mock_rewrite,
    ):
        mock_load.return_value = []
        mock_pocket.return_value = PocketMatchResult(pocket=pkt)
        mock_rewrite.return_value = "SELECT region_code FROM pocket_tbl"
        with pytest.raises(NoAggregateMatchError) as exc:
            await route_query(
                bq, db, principal=principal, force_route="pocket",
            )
    assert PocketSkipReason.NOT_RLS_SAFE in str(exc.value)


async def test_force_route_aggregate_skips_pocket_block():
    """force_route='aggregate' under RLS must not consult the pocket matcher."""
    m = make_measure("revenue")
    d = make_dimension("region_code")
    agg = make_aggregate(["region_code"], [make_agg_col(m)])
    sql = "SELECT region_code, SUM(revenue) FROM sales GROUP BY region_code"
    bq = _bind(sql, [m], [d])
    _rule, principal, db = _north_rule_principal_db()

    with (
        patch(_PATCH_LOAD, new_callable=AsyncMock) as mock_load,
        patch(_PATCH_POCKET, new_callable=AsyncMock) as mock_pocket,
    ):
        mock_load.return_value = [agg]
        decision = await route_query(
            bq, db, principal=principal, force_route="aggregate",
        )

    # region_code is in the aggregate grain -> RLS-safe aggregate served.
    assert decision.route_type == "aggregate"
    mock_pocket.assert_not_called()


# ---------------------------------------------------------------------------
# R-001: RLS pocket serving must not 403 when the SOURCE owner is populated
# ---------------------------------------------------------------------------

_PATCH_OWNERS = "shared.security.predicate_compiler._load_security_column_owners"


async def test_r001_rls_pocket_served_when_owners_populated():
    """R-001 regression: under PRODUCTION config ``_load_security_column_owners``
    returns the security column's real SOURCE owner (e.g. ``('region_code',
    'sales')``). The pocket rewrite scans a single MATERIALISED table
    (``pocket_tbl``), never the source owner, so the owner-not-scanned
    fail-closed guard (F-007-05/Bug-8896) would reject every RLS principal with
    a 403 — even though the model has an RLS-safe pocket. A single materialised
    scan makes a bare ``WHERE <col> = ...`` unambiguous, so the pocket MUST
    serve (bare inject), not 403.

    Guards the serving-suite mock-DB escape: those tests compile against a mock
    DB whose ``_load_security_column_owners`` yields empty owners, so the
    owners-populated production path was never exercised until this test."""
    m = make_measure("revenue")
    d = make_dimension("region_code")
    sql = "SELECT region_code, SUM(revenue) FROM sales GROUP BY region_code"
    bq = _bind(sql, [m], [d])
    _rule, principal, db = _north_rule_principal_db()
    pkt = _pocket("SELECT * FROM sales WHERE region_code = 'NORTH'")

    with (
        patch(_PATCH_LOAD, new_callable=AsyncMock) as mock_load,
        patch(_PATCH_POCKET, new_callable=AsyncMock) as mock_pocket,
        patch(_PATCH_REWRITE) as mock_rewrite,
        patch(_PATCH_OWNERS, new_callable=AsyncMock) as mock_owners,
    ):
        mock_load.return_value = []
        mock_pocket.return_value = PocketMatchResult(pocket=pkt)
        mock_rewrite.return_value = (
            "SELECT region_code, SUM(revenue) FROM pocket_tbl GROUP BY region_code"
        )
        # Production shape: the security column has a real SOURCE owner.
        mock_owners.return_value = (("region_code", "sales"),)
        decision = await route_query(bq, db, principal=principal)

    # Served (NOT 403): the predicate is injected onto the single pocket scan.
    assert decision.route_type == "pocket"
    assert decision.pocket_id == str(pkt.id)
    assert "\"region_code\" = 'NORTH'" in decision.rewritten_query
    assert "pocket_tbl" in decision.rewritten_query
    # The FULL owners still ride on the decision so the cache-miss SOURCE
    # fallback (routes.py) keeps the self-join disambiguation it needs; the
    # suppression is only on the pocket injection argument, never on ``compiled``.
    assert decision.security_compiled is not None
    assert decision.security_compiled.security_column_owners == (
        ("region_code", "sales"),
    )
