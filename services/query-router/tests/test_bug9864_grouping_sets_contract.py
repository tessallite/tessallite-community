"""Bug-9864 -- the query-router half of the rollup-lattice contract.

The XMLA gateway used to run one source query per rollup grain. It now sends the
ordinary finest-grain SQL plus a structured ``grouping_sets`` lattice and
``force_route="source"``; the router renders ``GROUP BY GROUPING SETS`` and one
``GROUPING()`` marker per grain column ON TOP of the same bound, persona-scoped
query the per-grain queries ran against.

Expressing the lattice as a request field rather than in the client's SQL is
load-bearing, and one test below pins the reason: client SQL carrying
``GROUP BY GROUPING SETS`` is still complex SQL, and complex SQL routes to
passthrough, which never binds semantic names to physical columns and is refused
outright for persona-scoped callers. If that ever silently changed, the security
argument for this design would need re-examining.

These fail on pre-fix code: ``LogicalQuery.grouping_sets``,
``_build_grouping_sets_render``, ``_validate_grouping_sets`` and
``_apply_grouping_sets`` did not exist.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import sqlglot
from fastapi import HTTPException

from src.api.routes import (
    GROUPING_MARKER_PREFIX,
    _apply_grouping_sets,
    _validate_grouping_sets,
    grouping_marker_name,
)
from src.ir.logical_query import SemanticBindingError, UnsupportedSQL
from src.parsing.sql_parser import _detect_complex_sql
from src.rewrite.source_sql import _build_grouping_sets_render


def _bound(grain, grouping_sets=None, **lq_over):
    lq = SimpleNamespace(
        grain=list(grain),
        grouping_sets=grouping_sets,
        requested_dimensions=list(grain),
        requested_measures=["base_amount"],
        select_expressions=[],
        **lq_over,
    )
    return SimpleNamespace(
        logical_query=lq,
        resolved_dimensions=[SimpleNamespace(name=g) for g in grain],
    )


def _exprs(grain):
    return {g: f'"t"."{g}_col"' for g in grain}


def _render(grain, sets, dialect="postgres", **kw):
    return _build_grouping_sets_render(
        _bound(grain, sets),
        dim_group_expr_by_name=kw.pop("exprs", None) or _exprs(grain),
        variant_extra_group_by=kw.pop("variant", []),
        target_dialect=dialect,
        qid=lambda n: '"' + n.replace('"', '""') + '"',
    )


# --------------------------------------------------------------------------
# Request validation
# --------------------------------------------------------------------------

def test_grouping_sets_requires_force_route_source():
    """An aggregate cannot roll COUNT_DISTINCT or AVG up exactly, so a lattice
    that could be served from one is refused rather than silently answered."""
    with pytest.raises(HTTPException) as info:
        _validate_grouping_sets([["a"]], force_route=None)
    assert info.value.status_code == 422
    assert info.value.detail["error_type"] == "grouping_sets_requires_source"

    for bad in ("aggregate", "pocket", "raw"):
        with pytest.raises(HTTPException):
            _validate_grouping_sets([["a"]], force_route=bad)

    # The supported combination is accepted.
    assert _validate_grouping_sets([["a"]], force_route="source") is None
    # And an absent lattice is always fine.
    assert _validate_grouping_sets(None, force_route=None) is None


def test_malformed_grouping_sets_are_refused():
    with pytest.raises(HTTPException) as info:
        _validate_grouping_sets([], force_route="source")
    assert info.value.detail["error_type"] == "grouping_sets_invalid"
    with pytest.raises(HTTPException):
        _validate_grouping_sets(["a"], force_route="source")  # not a list of lists
    with pytest.raises(HTTPException):
        _validate_grouping_sets([[1]], force_route="source")


def test_a_set_naming_a_dimension_outside_the_grain_is_refused():
    """The typed refusal the contract requires. Dropping the unknown name
    instead would delete a whole subtotal family from the client's pivot, and
    the name was never bound, so it was never checked against the persona."""
    body = SimpleNamespace(grouping_sets=[["region"], ["nonesuch"]])
    with pytest.raises(HTTPException) as info:
        _apply_grouping_sets(body, _bound(["region", "channel"]))
    assert info.value.status_code == 422
    assert info.value.detail["error_type"] == "grouping_sets_not_in_grain"
    assert "nonesuch" in info.value.detail["message"]


def test_valid_sets_are_normalised_onto_the_bound_query():
    bound = _bound(["region", "channel"])
    body = SimpleNamespace(grouping_sets=[["REGION"], ["region", "channel"], []])
    _apply_grouping_sets(body, bound)
    # Canonical grain spelling, duplicates removed, empty set preserved.
    assert bound.logical_query.grouping_sets == [
        ["region"], ["region", "channel"], [],
    ]


def test_no_grouping_sets_leaves_the_query_untouched():
    bound = _bound(["region"])
    _apply_grouping_sets(SimpleNamespace(grouping_sets=None), bound)
    assert bound.logical_query.grouping_sets is None


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def test_render_emits_the_lattice_and_one_marker_per_grain_column():
    render = _render(["region", "channel"], [["region"], ["channel"], []])
    assert render.group_by_clause == (
        ' GROUP BY GROUPING SETS (("t"."region_col"), ("t"."channel_col"), ())'
    )
    assert render.marker_pieces == [
        f'GROUPING("t"."region_col") AS "{GROUPING_MARKER_PREFIX}region"',
        f'GROUPING("t"."channel_col") AS "{GROUPING_MARKER_PREFIX}channel"',
    ]
    assert grouping_marker_name("region") == "_grouping__region"


def test_ordinary_queries_render_nothing_at_all():
    """No lattice means the plain GROUP BY path is untouched."""
    assert _render(["region"], None) is None
    assert _render(["region"], []) is None


@pytest.mark.parametrize("dialect", ["postgres", "bigquery"])
def test_rendered_lattice_parses_on_each_allowed_dialect(dialect):
    render = _render(["region", "channel"], [["region"], ["channel"], []],
                     dialect=dialect)
    sql = (
        'SELECT "t"."region_col", SUM("t"."amt") AS "amt", '
        + ", ".join(render.marker_pieces)
        + ' FROM "s"."t" AS "t"'
        + render.group_by_clause
    )
    out = sqlglot.transpile(sql, read="postgres", write=dialect)[0]
    assert sqlglot.parse_one(out, read=dialect) is not None
    assert "GROUPING SETS" in out.upper()
    if dialect == "bigquery":
        assert "`" in out and '"' not in out


def test_an_unlisted_dialect_is_refused_so_the_caller_falls_back():
    """The allow-list is the fallback switch: an unlisted dialect gets the typed
    refusal the gateway catches to keep the one-query-per-grain path."""
    with pytest.raises(UnsupportedSQL):
        _render(["region"], [["region"], []], dialect="tsql")


def test_a_grain_column_with_no_physical_expression_is_refused():
    with pytest.raises(SemanticBindingError):
        _render(["region", "channel"], [["region"], ["channel"]],
                exprs={"region": '"t"."region_col"'})


def test_duplicate_grouping_sets_are_refused():
    """PostgreSQL returns a repeated set's group twice; the caller maps rows
    back to grains by their marker vector and would count it twice."""
    with pytest.raises(SemanticBindingError):
        _render(["region", "channel"], [["region"], ["region"]])


def test_a_period_variant_extra_group_by_is_refused():
    """Variant expressions are appended to the grain unconditionally, so they
    would land in every grouping set -- not the requested lattice."""
    with pytest.raises(SemanticBindingError):
        _render(["region"], [["region"], []],
                variant=['DATE_TRUNC(\'month\', "t"."d")'])


def test_a_marker_name_colliding_with_a_projection_is_refused():
    bound = _bound(
        ["region"], [["region"], []],
    )
    bound.logical_query.requested_measures = ["_grouping__region"]
    with pytest.raises(SemanticBindingError):
        _build_grouping_sets_render(
            bound,
            dim_group_expr_by_name={"region": '"t"."region_col"'},
            variant_extra_group_by=[],
            target_dialect="postgres",
            qid=lambda n: '"' + n + '"',
        )


# --------------------------------------------------------------------------
# Why the lattice is a request field and not client SQL
# --------------------------------------------------------------------------

def test_client_sql_grouping_sets_is_still_complex_sql():
    """Pins the premise of the design. Complex SQL routes to passthrough, which
    substitutes the table but never binds semantic column names to physical
    ones, and is refused outright for every persona-scoped caller -- so the
    lattice cannot be expressed in the client's SQL."""
    tree = sqlglot.parse_one(
        'SELECT "region", SUM("amt") FROM "modely" '
        'GROUP BY GROUPING SETS (("region"), ())',
        read="postgres",
    )
    assert _detect_complex_sql(tree) is True
    plain = sqlglot.parse_one(
        'SELECT "region", SUM("amt") FROM "modely" GROUP BY "region"',
        read="postgres",
    )
    assert _detect_complex_sql(plain) is False


# --------------------------------------------------------------------------
# The post-execute result-column audit and the grouping markers
# --------------------------------------------------------------------------

def _audit_bound(grain, grouping_sets, dims=None):
    lq = SimpleNamespace(
        grain=list(grain),
        grouping_sets=grouping_sets,
        requested_dimensions=list(grain),
        requested_measures=["base_amount"],
        select_expressions=[],
        has_complex_sql=False,
        has_passthrough_expressions=False,
        select_star=False,
    )
    return SimpleNamespace(
        logical_query=lq,
        resolved_dimensions=[SimpleNamespace(name=d)
                             for d in (dims if dims is not None else grain)],
        resolved_measures=[SimpleNamespace(name="base_amount",
                                           default_agg="sum")],
        allowed_physical_columns=set(),
        complex_projection_names=set(),
        has_passthrough_expressions=False,
    )


def test_grouping_markers_pass_the_result_column_audit():
    """Test escape that reached the live stack: the audit blocked the whole
    result because ``_grouping__*`` was not in the binder's resolved set, and
    the gateway silently fell back to one query per grain -- right numbers, no
    speed-up, and only a log line to say so."""
    from src.security.query_audit import audit_result_columns

    bound = _audit_bound(
        ["channel_name", "account_type"],
        [["channel_name"], ["account_type"], []],
    )
    audit_result_columns(
        bound,
        ["channel_name", "account_type", "_grouping__channel_name",
         "_grouping__account_type", "base_amount"],
        None,
    )


def test_a_marker_for_a_column_the_binder_never_resolved_is_still_blocked():
    """The authorisation is derived from the ALLOWED set, not from the request,
    so a marker cannot be used to smuggle an unresolved column name past the
    audit."""
    from src.security.query_audit import (
        SecurityAuditError,
        audit_result_columns,
    )

    bound = _audit_bound(
        ["channel_name"], [["channel_name"], []], dims=["channel_name"],
    )
    with pytest.raises(SecurityAuditError):
        audit_result_columns(
            bound,
            ["channel_name", "_grouping__channel_name",
             "_grouping__salary_band", "base_amount"],
            None,
        )


def test_markers_are_not_authorised_without_a_grouping_sets_request():
    """No lattice, no markers -- an ordinary query that somehow returned one is
    still a bypass and is still blocked."""
    from src.security.query_audit import (
        SecurityAuditError,
        audit_result_columns,
    )

    bound = _audit_bound(["channel_name"], None)
    with pytest.raises(SecurityAuditError):
        audit_result_columns(
            bound,
            ["channel_name", "_grouping__channel_name", "base_amount"],
            None,
        )


def test_execute_and_explain_both_apply_the_lattice():
    """Explain is the route-PLAN surface: it must plan the query execute runs.

    Found live -- explain rendered a plain GROUP BY for a grouping-sets request
    while execute rendered the lattice, so the plan contradicted the executor.
    A structural check because both call sites live inside long handlers that no
    unit test can drive without a database.
    """
    import inspect

    from src.api import routes

    for handler in (routes._handle_execute, routes._handle_explain):
        src = inspect.getsource(handler)
        assert "_apply_grouping_sets(body, bound)" in src, (
            f"{handler.__name__} must apply the grouping-sets lattice"
        )
