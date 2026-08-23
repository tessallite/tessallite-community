"""
Bug-6964 — CTE arbitrary-table containment breach (CRITICAL / SECURITY).

A CTE whose body references a table outside the semantic model must be
REJECTED by the binder, not silently executed against the source.  These
tests prove that:

 1. A direct probe query (``WITH stolen AS (SELECT * FROM secret_table)
    SELECT * FROM stolen``) is rejected.
 2. Nested / aliased CTE relations are also rejected.
 3. CTEs that reference only the model table itself still work.
 4. CTE aliases (intermediate result names) are not falsely rejected.

Run from tessallite/services/query-router/:
    pytest tests/test_bug_6964_cte_containment.py -v
"""
from __future__ import annotations

import types
from unittest.mock import AsyncMock, patch

import pytest

from src.ir.logical_query import (
    LogicalQuery,
    SemanticBindingError,
)


@pytest.fixture
def _mock_model():
    return types.SimpleNamespace(
        id="model-1",
        slug="modely",
        display_name="ModelY",
        deployed_version_id="v1",
    )


# Kept for direct-patch tests that only need the model loader.
_BINDER_PATCHES = ("src.semantic.binder._load_model",)


def _apply_binder_patches(mock_model):
    """Return a context manager stacking the standard binder patches.

    Bug-7979: provides an empty DeployedShape so the model passes the
    fail-closed check (deployed pointer + no shape -> error).
    """
    from contextlib import ExitStack
    from unittest.mock import patch as _patch
    from src.semantic.snapshot_resolver import DeployedShape

    # F-003-02: the binder now also enforces COLUMN containment for complex SQL,
    # so the model-table CTE "allowed" cases below must expose their referenced
    # physical columns in the deployed shape (region/id/x/col1) — otherwise the
    # new column gate would fail closed on an empty vocabulary. This does not
    # weaken the Bug-6964 table-containment intent; the rejected cases below scan
    # a NON-model table (secret_table/hr_salaries) and still fail on the table
    # gate before any column check.
    _model_cols = {"region", "id", "x", "col1"}
    shape = DeployedShape(
        measures=[], dimensions=[], hidden_column_ids=set(),
        physical_columns_all=set(_model_cols),
        physical_columns_visible=set(_model_cols),
        hierarchy_rows=[],
    )
    stack = ExitStack()
    stack.enter_context(_patch("src.semantic.binder._load_model", return_value=mock_model))
    stack.enter_context(_patch("src.semantic.binder.resolve_deployed_shape", return_value=shape))
    return stack


class TestBug6964DirectProbe:
    """The canonical exploit: one-relation CTE reading an arbitrary source table."""

    async def test_single_non_model_table_in_cte_rejected(self, _mock_model):
        """WITH stolen AS (SELECT secret_value FROM secret_table)
        SELECT * FROM stolen — must be REJECTED."""
        from src.semantic.binder import bind_query_to_model

        query = LogicalQuery(
            model_id="model-1",
            protocol="jdbc",
            raw_query=(
                "WITH stolen AS (SELECT secret_value FROM secret_table) "
                "SELECT * FROM stolen"
            ),
            requested_measures=[],
            requested_dimensions=[],
            filters=[],
            grain=[],
            order_by=[],
            limit=None,
            offset=None,
            query_fingerprint="fp-6964-1",
            from_tables=["secret_table", "stolen"],
            cte_aliases=["stolen"],
            has_complex_sql=True,
            select_star=True,
        )

        db = AsyncMock()
        with patch(_BINDER_PATCHES[0], return_value=_mock_model):
            with pytest.raises(SemanticBindingError, match="Unknown table"):
                await bind_query_to_model(query, db)


class TestBug6964NestedCTE:
    """Nested CTEs: each level's physical tables must be validated."""

    async def test_nested_cte_non_model_table_rejected(self, _mock_model):
        """WITH a AS (SELECT * FROM secret_table),
             b AS (SELECT * FROM a)
        SELECT * FROM b — must be REJECTED because of secret_table."""
        from src.semantic.binder import bind_query_to_model

        query = LogicalQuery(
            model_id="model-1",
            protocol="jdbc",
            raw_query=(
                "WITH a AS (SELECT * FROM secret_table), "
                "b AS (SELECT * FROM a) "
                "SELECT * FROM b"
            ),
            requested_measures=[],
            requested_dimensions=[],
            filters=[],
            grain=[],
            order_by=[],
            limit=None,
            offset=None,
            query_fingerprint="fp-6964-2",
            from_tables=["secret_table", "a", "b"],
            cte_aliases=["a", "b"],
            has_complex_sql=True,
            select_star=True,
        )

        db = AsyncMock()
        with patch(_BINDER_PATCHES[0], return_value=_mock_model):
            with pytest.raises(SemanticBindingError, match="Unknown table"):
                await bind_query_to_model(query, db)


class TestBug6964AliasedCTE:
    """Aliased CTE body table: the alias itself is fine, the physical table must
    belong to the model."""

    async def test_aliased_cte_body_non_model_rejected(self, _mock_model):
        """WITH tmp AS (SELECT col1 FROM hr_salaries s)
        SELECT * FROM tmp — hr_salaries is not a model table, REJECTED."""
        from src.semantic.binder import bind_query_to_model

        query = LogicalQuery(
            model_id="model-1",
            protocol="jdbc",
            raw_query=(
                "WITH tmp AS (SELECT col1 FROM hr_salaries s) "
                "SELECT * FROM tmp"
            ),
            requested_measures=[],
            requested_dimensions=[],
            filters=[],
            grain=[],
            order_by=[],
            limit=None,
            offset=None,
            query_fingerprint="fp-6964-3",
            from_tables=["hr_salaries", "tmp"],
            cte_aliases=["tmp"],
            has_complex_sql=True,
            select_star=True,
        )

        db = AsyncMock()
        with patch(_BINDER_PATCHES[0], return_value=_mock_model):
            with pytest.raises(SemanticBindingError, match="Unknown table"):
                await bind_query_to_model(query, db)


class TestBug6964ModelTableCTEAllowed:
    """CTE that references the model's own table must still be allowed."""

    async def test_cte_scanning_model_table_allowed(self, _mock_model):
        """WITH filtered AS (SELECT * FROM modely WHERE region = 'US')
        SELECT * FROM filtered — modely is the model table, ALLOWED."""
        from src.semantic.binder import bind_query_to_model

        query = LogicalQuery(
            model_id="model-1",
            protocol="jdbc",
            raw_query=(
                "WITH filtered AS (SELECT * FROM modely WHERE region = 'US') "
                "SELECT * FROM filtered"
            ),
            requested_measures=[],
            requested_dimensions=[],
            filters=[],
            grain=[],
            order_by=[],
            limit=None,
            offset=None,
            query_fingerprint="fp-6964-4",
            from_tables=["modely", "filtered"],
            cte_aliases=["filtered"],
            has_complex_sql=True,
            select_star=True,
        )

        db = AsyncMock()
        with _apply_binder_patches(_mock_model):
            bound = await bind_query_to_model(query, db)
            assert bound is not None

    async def test_cte_scanning_display_name_table_allowed(self, _mock_model):
        """CTE body references model's display_name — allowed."""
        from src.semantic.binder import bind_query_to_model

        query = LogicalQuery(
            model_id="model-1",
            protocol="jdbc",
            raw_query=(
                "WITH filtered AS (SELECT * FROM ModelY WHERE x = 1) "
                "SELECT * FROM filtered"
            ),
            requested_measures=[],
            requested_dimensions=[],
            filters=[],
            grain=[],
            order_by=[],
            limit=None,
            offset=None,
            query_fingerprint="fp-6964-5",
            from_tables=["ModelY", "filtered"],
            cte_aliases=["filtered"],
            has_complex_sql=True,
            select_star=True,
        )

        db = AsyncMock()
        with _apply_binder_patches(_mock_model):
            bound = await bind_query_to_model(query, db)
            assert bound is not None


class TestBug6964MixedCTE:
    """CTE with both model and non-model tables: must reject."""

    async def test_mixed_model_and_external_table_rejected(self, _mock_model):
        """WITH a AS (SELECT * FROM modely),
             b AS (SELECT * FROM secret_table)
        SELECT * FROM a JOIN b ON ... — secret_table is rejected."""
        from src.semantic.binder import bind_query_to_model

        query = LogicalQuery(
            model_id="model-1",
            protocol="jdbc",
            raw_query=(
                "WITH a AS (SELECT * FROM modely), "
                "b AS (SELECT * FROM secret_table) "
                "SELECT * FROM a JOIN b ON a.id = b.id"
            ),
            requested_measures=[],
            requested_dimensions=[],
            filters=[],
            grain=[],
            order_by=[],
            limit=None,
            offset=None,
            query_fingerprint="fp-6964-6",
            from_tables=["modely", "secret_table", "a", "b"],
            cte_aliases=["a", "b"],
            has_complex_sql=True,
            select_star=True,
        )

        db = AsyncMock()
        with patch(_BINDER_PATCHES[0], return_value=_mock_model):
            with pytest.raises(SemanticBindingError, match="Unknown table"):
                await bind_query_to_model(query, db)
