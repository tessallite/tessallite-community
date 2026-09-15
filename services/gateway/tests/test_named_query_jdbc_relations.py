"""Named Query relations on the JDBC catalogue (gateway).

``SELECT * FROM @name`` is the Named Query reference shape (invariant 5). The
query-router intercepts it, but the JDBC path reaches the router only after the
gateway resolves a MODEL from the query's tables — and ``@name`` names no model
table. These tests pin the gateway catalogue seam that makes the reference
resolvable:

  * deployed Named Queries are registered as ``@name`` relations carrying the
    owning model id and the snapshot-derived output columns;
  * model resolution retries the extracted table name with the ``@`` prefix
    (sqlglot strips the ``@`` into a Parameter node, so the extractor returns
    the bare name);
  * the relation's query_name is the ``@name`` itself, so relation rewriting
    never aliases it away before the router intercepts it.

Test escape: no test exercised the gateway catalogue for a reference shape
that names no model table, so the interceptor was unreachable over JDBC.
Guard: this module. Tier: T1.
"""
from __future__ import annotations

import pytest

from src.jdbc.server import PGWireServer
from src.router_client import build_named_query_relation_columns


def _server(**attrs) -> PGWireServer:
    srv = object.__new__(PGWireServer)
    for key, value in attrs.items():
        setattr(srv, key, value)
    return srv


# ---------------------------------------------------------------------------
# Relation column builder
# ---------------------------------------------------------------------------

def test_named_query_relation_columns_map_output_types() -> None:
    nq = {
        "name": "top_cities",
        "output_columns": [
            {"name": "city_name", "type": "string"},
            {"name": "total", "type": "number"},
            {"name": "flag", "type": "boolean"},
        ],
    }
    cols = build_named_query_relation_columns(nq)
    assert [c["name"] for c in cols] == ["city_name", "total", "flag"]
    assert [c["data_type"] for c in cols] == ["text", "float8", "bool"]
    assert [c["ordinal_position"] for c in cols] == [1, 2, 3]
    assert cols[0]["kind"] == "dimension"
    assert cols[1]["kind"] == "measure"


def test_named_query_relation_columns_tolerate_star_and_junk() -> None:
    nq = {
        "name": "slice",
        "output_columns": [
            {"name": "*", "type": "string"},
            {"name": "", "type": "string"},
            "not-a-dict",
            None,
        ],
    }
    cols = build_named_query_relation_columns(nq)
    assert [c["name"] for c in cols] == ["*"]
    assert cols[0]["data_type"] == "text"


# ---------------------------------------------------------------------------
# Model resolution of @name references
# ---------------------------------------------------------------------------

def test_resolution_retries_extracted_name_with_at_prefix() -> None:
    srv = _server(
        _table_model_id={
            "modely": "model-1",
            "@top_cities": "model-1",
            "@camelCase": "model-2",
        },
        _table_include_hidden={"@top_cities": False},
        _table_persona_id={"@top_cities": None},
        _model_id=None,
    )
    # sqlglot extracts ``top_cities`` (no @) from ``FROM @top_cities``.
    model_id, include_hidden, persona_id = srv._resolve_model_id_and_variant(
        "SELECT * FROM @top_cities"
    )
    assert model_id == "model-1"
    assert include_hidden is False
    assert persona_id is None


def test_resolution_is_case_insensitive_like_other_relations() -> None:
    srv = _server(
        _table_model_id={"@TopCities": "model-9"},
        _table_include_hidden={"@TopCities": True},
        _table_persona_id={"@TopCities": "p-1"},
        _model_id=None,
    )
    model_id, include_hidden, persona_id = srv._resolve_model_id_and_variant(
        "SELECT * FROM @topcities"
    )
    assert model_id == "model-9"
    assert include_hidden is True
    assert persona_id == "p-1"


def test_unknown_reference_falls_back_to_session_model_then_none() -> None:
    srv = _server(
        _table_model_id={"modely": "model-1"},
        _table_include_hidden={},
        _table_persona_id={},
        _model_id="session-model",
    )
    assert srv._resolve_model_id_and_variant(
        "SELECT * FROM @undeployed_name"
    )[0] == "session-model"

    srv._model_id = None
    assert srv._resolve_model_id_and_variant(
        "SELECT * FROM @undeployed_name"
    )[0] is None


def test_column_type_map_reads_at_keyed_relation() -> None:
    srv = _server(
        _table_columns={
            "@top_cities": [
                {"name": "total", "data_type": "float8"},
            ],
        },
    )
    type_map = srv._column_type_map("SELECT * FROM @top_cities")
    assert type_map.get("total") == "float8"


# ---------------------------------------------------------------------------
# Persona-variant Named Query relations (Bug-9186)
# ---------------------------------------------------------------------------

def _variant_server() -> PGWireServer:
    """A catalogue carrying a base and a persona-variant Named Query relation.

    The variant's ``query_name`` is the canonical ``@name`` the query-router
    intercepts, exactly as a persona-variant MODEL relation aliases back to the
    model slug.
    """
    return _server(
        _table_model_id={
            "modely": "model-1",
            "modely_analyst": "model-1",
            "@top_cities": "model-1",
            "@top_cities_analyst": "model-1",
        },
        _table_include_hidden={},
        _table_persona_id={
            "modely": None,
            "modely_analyst": "p-1",
            "@top_cities": None,
            "@top_cities_analyst": "p-1",
        },
        _table_query_name={
            "modely": "modely",
            "modely_analyst": "modely",
            "@top_cities": "@top_cities",
            "@top_cities_analyst": "@top_cities",
        },
        _model_id=None,
    )


def test_a_persona_variant_named_query_relation_carries_its_persona() -> None:
    """Bug-9186 / decision 4.3: EVERY relation on a persona-variant catalogue
    forwards that persona to the router.

    Every ``@name`` relation used to be registered with ``persona_id=None``, so
    a privileged caller impersonating a persona through its variant catalogue
    reached the router UNRESTRICTED on a Named Query -- the gap F-008-05 closed
    for ``$KPIs`` and never applied here.
    """
    srv = _variant_server()
    for sql in (
        'SELECT * FROM "@top_cities_analyst"',
        "SELECT * FROM @top_cities_analyst",
    ):
        model_id, _hidden, persona_id = srv._resolve_model_id_and_variant(sql)
        assert model_id == "model-1", sql
        assert persona_id == "p-1", sql

    # The base relation is unchanged: no persona of its own.
    assert srv._resolve_model_id_and_variant(
        "SELECT * FROM @top_cities"
    )[2] is None


def test_a_variant_named_query_reference_folds_back_to_the_canonical_name() -> None:
    """The router intercepts the literal ``@name``; the variant suffix must be
    folded away before dispatch or the reference is unresolvable.

    Both spellings a BI client sends have to fold. The UNQUOTED one is the one
    that bites: sqlglot parses ``FROM @name`` as an ``exp.Parameter``, which the
    table rewriter never visited, and rewriting that parameter IN PLACE renders
    as ``$name`` in the postgres dialect -- a silently different token the
    router's recogniser would never match.
    """
    srv = _variant_server()

    quoted = srv._rewrite_exposed_relations('SELECT * FROM "@top_cities_analyst"')
    assert '"@top_cities"' in quoted
    assert "analyst" not in quoted

    unquoted = srv._rewrite_exposed_relations("SELECT * FROM @top_cities_analyst")
    assert '"@top_cities"' in unquoted
    assert "analyst" not in unquoted
    assert "$" not in unquoted, (
        "a postgres Parameter renders as $name; the fold must emit the "
        "quoted @name relation the router recognises"
    )

    # A base-surface reference is left exactly as it arrived.
    assert srv._rewrite_exposed_relations(
        "SELECT * FROM @top_cities"
    ) == "SELECT * FROM @top_cities"
