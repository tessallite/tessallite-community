"""T0 tests for the shared attribute-relationship verifier (spec §7.6, §14).

These assert the CHECK SHAPES (the exact directional queries the correctness
argument depends on) and the driver's fail-closed behaviour against a fake source
that returns known counterexample rows — without a live DB. The known-answer
route tests through the real deploy/refresh pipeline are a later T1/T3 tier; here
we pin that the verifier itself asks the right questions and never returns
VERIFIED on a violation, timeout, error, or uncertified type.
"""
from __future__ import annotations

import pytest

from shared.semantic.attribute_relationship_verifier import (
    BIJECTION,
    BROKEN,
    ERR_FORWARD_VIOLATION,
    ERR_NULL_ENDPOINT,
    ERR_REVERSE_VIOLATION,
    ERR_TIMEOUT,
    ERR_UNCERTIFIED_COLLATION,
    ERR_UNSUPPORTED_TYPE,
    ERROR,
    FUNCTIONAL_N_TO_1,
    RelationColumns,
    VERIFIED,
    build_forward_check_sql,
    build_null_check_sql,
    build_reverse_check_sql,
    check_detail_type_certified,
    verify_relationship,
)

pytestmark = pytest.mark.unit


_COLS = RelationColumns(
    table_ref="geo_dim", key_physical="country_id", detail_physical="country_name",
    detail_type="TEXT", key_type="INT",
)


# ---------------------------------------------------------------------------
# Check-shape assertions (I-invariants: right question, right direction)
# ---------------------------------------------------------------------------


def test_forward_check_groups_by_key_counts_distinct_detail():
    sql = build_forward_check_sql(_COLS, "postgresql")
    assert 'GROUP BY "country_id"' in sql
    assert 'COUNT(DISTINCT "country_name") > 1' in sql


def test_reverse_check_groups_by_detail_counts_distinct_key():
    # Spec pitfall 17: the reverse strict check is GROUP BY detail COUNT(DISTINCT
    # key) — the OPPOSITE direction from forward. Getting this backwards would
    # reject valid shared labels (N:1) or admit a non-function.
    sql = build_reverse_check_sql(_COLS, "postgresql")
    assert 'GROUP BY "country_name"' in sql
    assert 'COUNT(DISTINCT "country_id") > 1' in sql


def test_null_check_rejects_either_endpoint():
    sql = build_null_check_sql(_COLS, "postgresql")
    assert '"country_id" IS NULL OR "country_name" IS NULL' in sql


def test_bigquery_uses_backticks_no_connector_branch():
    sql = build_forward_check_sql(_COLS, "bigquery")
    assert "`country_id`" in sql and "`country_name`" in sql


# ---------------------------------------------------------------------------
# Certified-type gate (spec §7.6.2)
# ---------------------------------------------------------------------------


def test_float_detail_is_unsupported():
    assert check_detail_type_certified("FLOAT") == ERR_UNSUPPORTED_TYPE
    assert check_detail_type_certified("DOUBLE") == ERR_UNSUPPORTED_TYPE


def test_text_detail_requires_collation_certification():
    assert check_detail_type_certified("TEXT") == ERR_UNCERTIFIED_COLLATION
    assert check_detail_type_certified("VARCHAR", text_collation_certified=True) is None


def test_exact_types_certified():
    for t in ("INT", "BIGINT", "NUMERIC", "DECIMAL", "DATE", "BOOLEAN"):
        assert check_detail_type_certified(t) is None


def test_is_text_detail_type_classifies_text_only():
    # Bug-7894: the classifier decides "defer to artifact-time collation
    # certification" (text) vs "permanent unsupported" (float/other). It must NOT
    # weaken the gate — a text type is still uncertified without the collation flag.
    from shared.semantic.attribute_relationship_verifier import is_text_detail_type

    for t in ("VARCHAR", "CHAR", "STRING", "TEXT", "varchar"):
        assert is_text_detail_type(t) is True
    for t in ("INT", "BIGINT", "NUMERIC", "DATE", "BOOLEAN", "FLOAT", "DOUBLE", ""):
        assert is_text_detail_type(t) is False
    # Classifying a type text does not certify it — the gate still refuses text
    # without an explicit collation certification.
    assert check_detail_type_certified("VARCHAR") == ERR_UNCERTIFIED_COLLATION


def test_verbose_connector_type_spellings_classify_correctly():
    # Bug-7894 R2 finding 2 (test escape): the real introspected spellings — NOT
    # the short hand-tokens the other fixtures use — must classify correctly, or
    # the whole text-certification path is inert on the primary connectors.
    from shared.semantic.attribute_relationship_verifier import (
        is_collation_stable_key_type,
        is_text_detail_type,
    )
    from shared.connector_qualify import normalize_type_token

    # PostgreSQL 'character varying' (and its normalized token 'CHARACTER'),
    # SQL Server 'nvarchar', Snowflake 'TEXT', BigQuery 'STRING' are all text.
    for spelling in (
        "character varying", "CHARACTER", "nvarchar", "nchar", "bpchar",
        "STRING", "text",
    ):
        assert is_text_detail_type(spelling) is True, spelling
        # A text detail is never certified without the collation flag.
        assert check_detail_type_certified(spelling) == ERR_UNCERTIFIED_COLLATION
        # A text KEY is never collation-stable.
        assert is_collation_stable_key_type(spelling) is False

    # The token the resolver actually produces for a PG varchar column.
    assert normalize_type_token("character varying") == "CHARACTER"
    assert is_text_detail_type(normalize_type_token("character varying")) is True

    # Verbose numeric/date spellings are still certifiable (non-text) and stable keys.
    for spelling in ("integer", "bigint", "timestamp without time zone", "boolean"):
        assert is_text_detail_type(spelling) is False
        assert check_detail_type_certified(spelling) is None
    assert is_collation_stable_key_type("integer") is True
    assert is_collation_stable_key_type("timestamp without time zone") is True

    # Float/approximate numerics remain UNSUPPORTED even in verbose form.
    for spelling in ("double precision", "float8", "real", "float64"):
        assert check_detail_type_certified(spelling) == ERR_UNSUPPORTED_TYPE
        assert is_collation_stable_key_type(spelling) is False


# ---------------------------------------------------------------------------
# Driver fail-closed behaviour against a fake source (known counterexamples)
# ---------------------------------------------------------------------------


class _FakeConn:
    connection_type = "postgresql"


def _fake_executor(row_map):
    """Return an execute_source_sql stub: maps a substring in the SQL to rows."""
    async def _exec(conn_obj, sql, *, tenant_session=None):
        for needle, rows in row_map.items():
            if needle in sql:
                return rows, []
        return [], []
    return _exec


@pytest.mark.asyncio
async def test_verified_when_all_checks_empty(monkeypatch):
    import shared.semantic.attribute_relationship_verifier as v
    monkeypatch.setattr(v, "execute_source_sql", _fake_executor({}), raising=False)
    # Patch the imported symbol used inside verify_relationship.
    import shared.source_executor as se
    monkeypatch.setattr(se, "execute_source_sql", _fake_executor({}))
    ev = await verify_relationship(
        cols=_COLS, cardinality=BIJECTION, connector="postgresql",
        conn_obj=_FakeConn(), text_collation_certified=True,
    )
    assert ev.status == VERIFIED


@pytest.mark.asyncio
async def test_false_strict_reverse_violation_is_broken(monkeypatch):
    # IDs 1 and 2 both map to 'Shared': forward passes (each id -> one name) but
    # reverse fails (name 'Shared' -> two ids). A BIJECTION declaration must be
    # BROKEN, never VERIFIED (spec §14.2 adversarial case).
    import shared.source_executor as se
    monkeypatch.setattr(
        se, "execute_source_sql",
        _fake_executor({"HAVING COUNT(DISTINCT \"country_id\")": [{"country_name": "Shared"}]}),
    )
    ev = await verify_relationship(
        cols=_COLS, cardinality=BIJECTION, connector="postgresql",
        conn_obj=_FakeConn(), text_collation_certified=True,
    )
    assert ev.status == BROKEN
    assert ev.error_code == ERR_REVERSE_VIOLATION
    assert ev.failed_direction == "reverse"


@pytest.mark.asyncio
async def test_same_rows_as_functional_n_to_1_verify(monkeypatch):
    # The SAME shared-label rows declared FUNCTIONAL_N_TO_1 must VERIFY: only the
    # forward check runs, and each id maps to exactly one name.
    import shared.source_executor as se
    monkeypatch.setattr(se, "execute_source_sql", _fake_executor({}))
    ev = await verify_relationship(
        cols=_COLS, cardinality=FUNCTIONAL_N_TO_1, connector="postgresql",
        conn_obj=_FakeConn(), text_collation_certified=True,
    )
    assert ev.status == VERIFIED


@pytest.mark.asyncio
async def test_forward_violation_breaks_both_cardinalities(monkeypatch):
    # One key mapping to two details is a forward-function violation -> BROKEN
    # for N:1 too (the edge is not even a function).
    import shared.source_executor as se
    monkeypatch.setattr(
        se, "execute_source_sql",
        _fake_executor({"HAVING COUNT(DISTINCT \"country_name\")": [{"country_id": 7}]}),
    )
    ev = await verify_relationship(
        cols=_COLS, cardinality=FUNCTIONAL_N_TO_1, connector="postgresql",
        conn_obj=_FakeConn(), text_collation_certified=True,
    )
    assert ev.status == BROKEN
    assert ev.error_code == ERR_FORWARD_VIOLATION


@pytest.mark.asyncio
async def test_null_endpoint_breaks(monkeypatch):
    import shared.source_executor as se
    monkeypatch.setattr(
        se, "execute_source_sql",
        _fake_executor({"IS NULL OR": [{"?column?": 1}]}),
    )
    ev = await verify_relationship(
        cols=_COLS, cardinality=BIJECTION, connector="postgresql",
        conn_obj=_FakeConn(), text_collation_certified=True,
    )
    assert ev.status == BROKEN
    assert ev.error_code == ERR_NULL_ENDPOINT


@pytest.mark.asyncio
async def test_timeout_is_error_not_verified(monkeypatch):
    import shared.source_executor as se
    from shared.source_executor import QueryTimeoutError

    async def _boom(conn_obj, sql, *, tenant_session=None):
        raise QueryTimeoutError("slow")

    monkeypatch.setattr(se, "execute_source_sql", _boom)
    ev = await verify_relationship(
        cols=_COLS, cardinality=BIJECTION, connector="postgresql",
        conn_obj=_FakeConn(), text_collation_certified=True,
    )
    assert ev.status == ERROR
    assert ev.error_code == ERR_TIMEOUT


@pytest.mark.asyncio
async def test_uncertified_float_short_circuits_before_query(monkeypatch):
    import shared.source_executor as se

    async def _should_not_run(conn_obj, sql, *, tenant_session=None):
        raise AssertionError("verifier must not query for an uncertified type")

    monkeypatch.setattr(se, "execute_source_sql", _should_not_run)
    float_cols = RelationColumns(
        table_ref="t", key_physical="k", detail_physical="d",
        detail_type="DOUBLE", key_type="INT",
    )
    ev = await verify_relationship(
        cols=float_cols, cardinality=BIJECTION, connector="postgresql",
        conn_obj=_FakeConn(),
    )
    assert ev.status == ERROR
    assert ev.error_code == ERR_UNSUPPORTED_TYPE
