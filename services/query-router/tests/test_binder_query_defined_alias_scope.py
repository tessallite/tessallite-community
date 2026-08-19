"""Query-defined projection names must resolve BEFORE model-column validation.

The complex-SQL path validated every identifier against the DEPLOYED MODEL's
physical columns without first considering the names the QUERY ITSELF defines.
So the single most common shape a BI client emits —

    SELECT source_system, SUM(transaction_amount) AS total_amount
    FROM modely
    ORDER BY total_amount DESC

— was rejected, at one of TWO gates depending on where the alias was defined:

  * ``_validate_complex_sql_columns`` (binder): an ORDER BY reference to an alias
    defined in the SAME scope was read as a physical column and rejected with
    "Unknown column(s) <alias> in model '<slug>'."
  * ``audit_result_columns`` (post-execute): a result column named by a CTE /
    derived-table output the outer query projects onward was read as an
    unauthorised column and the whole result was blocked.

Both are the same missing step — query-defined names never entered the
resolution scope — and both are fixed by one resolution-order change rather than
per-construct exemptions, so aggregate, CASE and window aliases are all covered
by the same rule.

THE BOUNDARY THIS FILE PINS. The fix must not make model-column validation
permissive: that validation is what stops complex SQL reading a column the
deployed model never exposed (F-003-02). Alias visibility in SQL is
clause-specific: an alias wins over an input column as a standalone ORDER BY
term, and as a standalone GROUP BY term when the name is not also a modelled
input column (F-003-05 / Bug-9059). HAVING, WHERE, the SELECT list, and
ORDER BY expressions still resolve to input columns.

Test escape: the e2e suite that would have caught this was reporting
"177 skipped ... exit 0" (the Bug-8532 silent-skip class), so nine real product
failures read as green; no unit test drove an ORDER BY over a query-defined
alias at all.
Guard: this file — three positive alias sources through the real bind + audit
path, and the negative cases that keep containment fail-closed.
Tier: T1 (producer/consumer contract: binder publishes the projection scope,
the result audit consumes it).

Run from tessallite/services/query-router/:
    pytest tests/test_binder_query_defined_alias_scope.py -v
"""
from __future__ import annotations

import types
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import pytest

from src.ir.logical_query import LogicalQuery, SemanticBindingError
from src.security.query_audit import SecurityAuditError, audit_result_columns

# Physical columns of the fake deployed model. ``secret_bonus`` is deliberately
# ABSENT — it stands for a column present in the source table but never modelled.
MODEL_COLUMNS = {
    "source_system",
    "transaction_amount",
    "transaction_currency",
    "payment_reference",
    "payment_status",
    "risk_score",
    "business_date",
}


@pytest.fixture
def _mock_model():
    return types.SimpleNamespace(
        id="model-1",
        slug="modely",
        display_name="ModelY",
        deployed_version_id="v1",
    )


def _shape_with_columns(cols: set[str]):
    from src.semantic.snapshot_resolver import DeployedShape

    lc = {c.lower() for c in cols}
    return DeployedShape(
        measures=[], dimensions=[], hidden_column_ids=set(),
        physical_columns_all=set(lc), physical_columns_visible=set(lc),
        hierarchy_rows=[],
    )


def _patched(mock_model, shape):
    stack = ExitStack()
    stack.enter_context(
        patch("src.semantic.binder._load_model", return_value=mock_model)
    )
    stack.enter_context(
        patch("src.semantic.binder.resolve_deployed_shape", return_value=shape)
    )
    return stack


def _complex_query(raw: str, *, from_tables, cte_aliases=()):
    return LogicalQuery(
        model_id="model-1",
        protocol="jdbc",
        raw_query=raw,
        requested_measures=[],
        requested_dimensions=[],
        filters=[],
        grain=[],
        order_by=[],
        limit=None,
        offset=None,
        query_fingerprint="fp-alias-scope",
        from_tables=list(from_tables),
        cte_aliases=list(cte_aliases),
        has_complex_sql=True,
        select_star=False,
    )


async def _bind(raw: str, *, from_tables=("modely",), cte_aliases=(), model, cols=None):
    from src.semantic.binder import bind_query_to_model

    shape = _shape_with_columns(MODEL_COLUMNS if cols is None else cols)
    query = _complex_query(raw, from_tables=from_tables, cte_aliases=cte_aliases)
    db = AsyncMock()
    with _patched(model, shape):
        return await bind_query_to_model(query, db)


class TestQueryDefinedAliasResolves:
    """Three alias SOURCES, one rule. Each binds AND survives the result audit —
    the second gate is where the CTE / derived-table shapes actually failed."""

    async def test_aggregate_alias_in_order_by(self, _mock_model):
        # Q14.09 / Q15.03 / Q15.04 shape, single scope.
        bound = await _bind(
            "SELECT source_system, SUM(transaction_amount) AS total_amount "
            "FROM modely GROUP BY source_system ORDER BY total_amount DESC",
            model=_mock_model,
        )
        assert bound is not None
        assert "total_amount" in bound.complex_projection_names
        audit_result_columns(bound, ["source_system", "total_amount"], None)

    async def test_case_expression_alias_in_order_by(self, _mock_model):
        # Q15.05 shape, single scope.
        bound = await _bind(
            "SELECT CASE WHEN risk_score >= 90 THEN '90_PLUS' ELSE 'UNDER_90' END "
            "AS risk_band, COUNT(*) AS txn_count FROM modely ORDER BY risk_band",
            model=_mock_model,
        )
        assert bound is not None
        assert {"risk_band", "txn_count"} <= bound.complex_projection_names
        audit_result_columns(bound, ["risk_band", "txn_count"], None)

    async def test_window_function_alias_in_order_by(self, _mock_model):
        # Q18.02 / Q18.03 / Q18.04 / Q18.12 shape.
        bound = await _bind(
            "SELECT payment_reference, transaction_currency, "
            "ROW_NUMBER() OVER (PARTITION BY transaction_currency "
            "ORDER BY transaction_amount DESC) AS rn_in_currency "
            "FROM modely ORDER BY transaction_currency, rn_in_currency",
            model=_mock_model,
        )
        assert bound is not None
        assert "rn_in_currency" in bound.complex_projection_names
        audit_result_columns(
            bound,
            ["payment_reference", "transaction_currency", "rn_in_currency"],
            None,
        )

    async def test_derived_table_output_name_projected_and_audited(self, _mock_model):
        # Q14.09 as written: the alias lives in a DERIVED TABLE and the outer
        # query projects the name onward, so the RESULT AUDIT is the gate.
        bound = await _bind(
            "SELECT source_system, total_amount FROM ("
            "SELECT source_system, SUM(transaction_amount) AS total_amount "
            "FROM modely GROUP BY source_system) s "
            "WHERE total_amount > 0 ORDER BY total_amount DESC, source_system",
            model=_mock_model,
        )
        assert "total_amount" in bound.complex_projection_names
        audit_result_columns(bound, ["source_system", "total_amount"], None)

    async def test_cte_output_name_projected_and_audited(self, _mock_model):
        # Q15.05 as written: CASE alias defined in a CTE, projected by the outer
        # query, grouped and ordered by the CTE's output name.
        bound = await _bind(
            "WITH risk_bands AS (SELECT CASE WHEN risk_score >= 90 THEN 'HI' "
            "ELSE 'LO' END AS risk_band FROM modely) "
            "SELECT risk_band, COUNT(*) AS txn_count FROM risk_bands "
            "GROUP BY risk_band ORDER BY risk_band",
            from_tables=("modely", "risk_bands"),
            cte_aliases=("risk_bands",),
            model=_mock_model,
        )
        assert {"risk_band", "txn_count"} <= bound.complex_projection_names
        audit_result_columns(bound, ["risk_band", "txn_count"], None)

    async def test_alias_in_order_by_with_nulls_last(self, _mock_model):
        # Q15.04: ordering modifiers must not defeat the standalone-term rule.
        bound = await _bind(
            "SELECT source_system, SUM(transaction_amount) AS total_amount "
            "FROM modely GROUP BY source_system "
            "ORDER BY total_amount DESC NULLS LAST, source_system",
            model=_mock_model,
        )
        assert bound is not None


class TestContainmentStillFailsClosed:
    """The negative boundary: model-column validation must NOT have become
    permissive. Each of these is a bare name that is NOT a modelled column."""

    async def test_unknown_column_still_rejected_with_existing_message(
        self, _mock_model,
    ):
        with pytest.raises(SemanticBindingError) as exc:
            await _bind(
                "SELECT source_system, secret_bonus FROM modely "
                "ORDER BY source_system",
                model=_mock_model,
            )
        assert "Unknown column(s) secret_bonus in model 'modely'" in str(exc.value)
        assert "only reference columns exposed by the deployed model" in str(exc.value)

    async def test_unknown_column_in_order_by_still_rejected(self, _mock_model):
        # No alias of that name exists, so ORDER BY resolves it to an input
        # column — an unmodelled physical read.
        with pytest.raises(SemanticBindingError, match="secret_bonus"):
            await _bind(
                "SELECT source_system FROM modely ORDER BY secret_bonus",
                model=_mock_model,
            )

    async def test_alias_is_not_visible_in_where(self, _mock_model):
        # PostgreSQL resolves a bare WHERE name to an INPUT column, never to an
        # output alias. Exempting it would authorise reading an unmodelled
        # ``secret_bonus`` column merely because the query aliased something to
        # that name.
        with pytest.raises(SemanticBindingError, match="secret_bonus"):
            await _bind(
                "SELECT SUM(transaction_amount) AS secret_bonus FROM modely "
                "WHERE secret_bonus > 100",
                model=_mock_model,
            )

    async def test_alias_does_not_win_in_group_by(self, _mock_model):
        """F-003-05 / Bug-9059: GROUP BY of a SELECT alias that is NOT a
        modelled input column is output grouping, not a physical read."""
        bound = await _bind(
            "SELECT source_system AS secret_bonus, COUNT(*) FROM modely "
            "GROUP BY secret_bonus",
            model=_mock_model,
        )
        assert bound is not None

    async def test_group_by_modelled_input_is_physical_read(self, _mock_model):
        """GROUP BY of a modelled column name is a physical read of that input."""
        bound = await _bind(
            "SELECT source_system, COUNT(*) FROM modely GROUP BY source_system",
            model=_mock_model,
        )
        assert bound is not None

    async def test_f003_04_values_alias_list_binds(self, _mock_model):
        """Bug-9045 / F-003-04: LATERAL VALUES AS x(a, b) published names bind."""
        bound = await _bind(
            "SELECT m.source_system, x.component_name, x.component_amount "
            "FROM modely AS m CROSS JOIN LATERAL ("
            "VALUES ('transaction_amount', COALESCE(m.transaction_amount, 0))"
            ") AS x(component_name, component_amount)",
            model=_mock_model,
        )
        assert bound is not None

    async def test_f003_04_mixed_star_named_alias_binds(self, _mock_model):
        """Bug-9045: mixed star + named alias on a CTE is a named output."""
        bound = await _bind(
            "WITH q AS (SELECT *, 1 AS extra FROM modely) SELECT extra FROM q",
            from_tables=("modely",),
            cte_aliases=("q",),
            model=_mock_model,
        )
        assert bound is not None

    async def test_f003_04_star_cte_unmodelled_salary_still_rejected(self, _mock_model):
        """F-003-02 / Bug-9045 regression: star CTE cannot disclose salary."""
        with pytest.raises(SemanticBindingError, match="salary"):
            await _bind(
                "WITH q AS (SELECT * FROM modely) SELECT salary FROM q",
                from_tables=("modely",),
                cte_aliases=("q",),
                model=_mock_model,
            )

    async def test_alias_is_not_visible_in_having(self, _mock_model):
        with pytest.raises(SemanticBindingError, match="secret_bonus"):
            await _bind(
                "SELECT source_system, SUM(transaction_amount) AS secret_bonus "
                "FROM modely GROUP BY source_system HAVING secret_bonus > 0",
                model=_mock_model,
            )

    async def test_alias_is_not_visible_to_a_sibling_select_item(self, _mock_model):
        # A select-list alias is not in scope for other select-list items.
        with pytest.raises(SemanticBindingError, match="secret_bonus"):
            await _bind(
                "SELECT SUM(transaction_amount) AS secret_bonus, secret_bonus "
                "FROM modely",
                model=_mock_model,
            )

    async def test_alias_inside_an_order_by_expression_is_not_exempt(
        self, _mock_model,
    ):
        # An output name must STAND ALONE: ``ORDER BY x`` resolves to the output
        # column, ``ORDER BY x + 1`` does not.
        with pytest.raises(SemanticBindingError, match="secret_bonus"):
            await _bind(
                "SELECT SUM(transaction_amount) AS secret_bonus FROM modely "
                "ORDER BY secret_bonus + 1",
                model=_mock_model,
            )

    async def test_qualified_alias_reference_is_not_exempt(self, _mock_model):
        # ``modely.secret_bonus`` names a COLUMN OF THE TABLE, never the alias.
        with pytest.raises(SemanticBindingError, match="secret_bonus"):
            await _bind(
                "SELECT SUM(transaction_amount) AS secret_bonus FROM modely "
                "ORDER BY modely.secret_bonus",
                model=_mock_model,
            )

    async def test_result_audit_still_blocks_a_name_the_query_never_projected(
        self, _mock_model,
    ):
        # Defence in depth: a column arriving in the result that is neither a
        # modelled physical column nor a name the query's projection defines is
        # still a fail-closed block.
        bound = await _bind(
            "SELECT source_system, SUM(transaction_amount) AS total_amount "
            "FROM modely GROUP BY source_system ORDER BY total_amount DESC",
            model=_mock_model,
        )
        with pytest.raises(SecurityAuditError, match="secret_bonus"):
            audit_result_columns(
                bound, ["source_system", "total_amount", "secret_bonus"], None,
            )
