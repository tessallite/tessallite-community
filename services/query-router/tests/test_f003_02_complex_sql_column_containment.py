"""
F-003-02 — complex-SQL column containment (CRITICAL / SECURITY).

The complex-SQL passthrough path used to treat allowed TABLE identity as
sufficient authorisation: the binder set resolved dims/measures empty (no
per-column check), the persona gate only blocked personas carrying allow-lists,
and the post-execute result-column audit returned early. Net effect: a
one-relation CTE/subquery over the ALLOWED model name could SELECT an UNMODELLED
physical source column (a column that exists in the source table but was never
exposed as a semantic dimension/measure) and receive it — same-tenant data
disclosure. The Bug-6964 table-containment fix does NOT close this column-level
form (the table IS the model; only the column is unmodelled).

Root-cause fix (binder, on the deployed-snapshot authority, A1-consistent):
before passthrough, walk every physical column reference across all scopes and
require each to be a modelled physical column of the deployed model; fail closed
on an unmodelled column, ambiguity, or parse failure. The validated model
physical set is published on ``allowed_physical_columns`` so the post-execute
result audit stays ACTIVE for complex SQL.

Test escape: no test drove a complex query that scanned the MODEL table but
selected a column the model never declared — the Bug-6964 suite only exercised
non-model TABLES, which the table gate already caught.
Guard: this file — unmodelled column in a CTE/subquery over the allowed model is
REJECTED; a modelled column is allowed; the result audit stays active.
Tier: T1 (producer/consumer security-containment contract).

Run from tessallite/services/query-router/:
    pytest tests/test_f003_02_complex_sql_column_containment.py -v
"""
from __future__ import annotations

import types
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import pytest

from src.ir.logical_query import LogicalQuery, SemanticBindingError


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


def _complex_query(raw: str, from_tables, cte_aliases, *, select_star=True):
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
        query_fingerprint="fp-f003-02",
        from_tables=list(from_tables),
        cte_aliases=list(cte_aliases),
        has_complex_sql=True,
        select_star=select_star,
    )


class TestUnmodelledColumnRejected:
    """The canonical exploit: a CTE over the ALLOWED model table selecting a
    column the model never exposed."""

    async def test_cte_unmodelled_column_over_model_table_rejected(self, _mock_model):
        # ``salary`` is NOT a modelled column of modely -> must be REJECTED even
        # though ``modely`` is the allowed model table.
        shape = _shape_with_columns({"region", "amount"})
        query = _complex_query(
            "WITH q AS (SELECT salary FROM modely) SELECT * FROM q",
            from_tables=["modely", "q"],
            cte_aliases=["q"],
        )
        db = AsyncMock()
        with _patched(_mock_model, shape):
            with pytest.raises(SemanticBindingError, match="salary"):
                await bind_and_get(query, db)

    async def test_subquery_unmodelled_column_rejected(self, _mock_model):
        shape = _shape_with_columns({"region", "amount"})
        query = _complex_query(
            "SELECT x.ssn FROM (SELECT ssn FROM modely) x",
            from_tables=["modely"],
            cte_aliases=[],
            select_star=False,
        )
        db = AsyncMock()
        with _patched(_mock_model, shape):
            with pytest.raises(SemanticBindingError, match="ssn"):
                await bind_and_get(query, db)

    async def test_window_function_over_unmodelled_column_rejected(self, _mock_model):
        shape = _shape_with_columns({"region", "amount"})
        query = _complex_query(
            "SELECT region, SUM(secret_bonus) OVER () FROM modely",
            from_tables=["modely"],
            cte_aliases=[],
            select_star=False,
        )
        db = AsyncMock()
        with _patched(_mock_model, shape):
            with pytest.raises(SemanticBindingError, match="secret_bonus"):
                await bind_and_get(query, db)

    async def test_having_aggregate_over_unmodelled_column_rejected(self, _mock_model):
        # sqlglot's Scope.columns OMITS a column inside a HAVING aggregate, so a
        # scope.columns-only walk would MISS this (a real containment bypass).
        # The scope-local collector must surface it.
        shape = _shape_with_columns({"region", "amount"})
        query = _complex_query(
            "SELECT region, SUM(amount) FROM modely GROUP BY region "
            "HAVING SUM(secret_bonus) > 0",
            from_tables=["modely"],
            cte_aliases=[],
            select_star=False,
        )
        db = AsyncMock()
        with _patched(_mock_model, shape):
            with pytest.raises(SemanticBindingError, match="secret_bonus"):
                await bind_and_get(query, db)

    async def test_set_operation_unmodelled_column_rejected(self, _mock_model):
        shape = _shape_with_columns({"region"})
        query = _complex_query(
            "SELECT region FROM modely UNION SELECT ssn FROM modely",
            from_tables=["modely"],
            cte_aliases=[],
            select_star=False,
        )
        db = AsyncMock()
        with _patched(_mock_model, shape):
            with pytest.raises(SemanticBindingError, match="ssn"):
                await bind_and_get(query, db)

    async def test_join_on_unmodelled_column_rejected(self, _mock_model):
        shape = _shape_with_columns({"region", "id"})
        query = _complex_query(
            "SELECT a.region FROM modely a JOIN modely b ON a.secret = b.id",
            from_tables=["modely"],
            cte_aliases=[],
            select_star=False,
        )
        db = AsyncMock()
        with _patched(_mock_model, shape):
            with pytest.raises(SemanticBindingError, match="secret"):
                await bind_and_get(query, db)

    async def test_where_subquery_unmodelled_column_rejected(self, _mock_model):
        shape = _shape_with_columns({"region", "amount"})
        query = _complex_query(
            "SELECT region FROM modely WHERE amount IN "
            "(SELECT secret FROM modely)",
            from_tables=["modely"],
            cte_aliases=[],
            select_star=False,
        )
        db = AsyncMock()
        with _patched(_mock_model, shape):
            with pytest.raises(SemanticBindingError, match="secret"):
                await bind_and_get(query, db)


class TestStarSubScopeContainment:
    """Fable FINDING-1: a ``SELECT *`` sub-scope (CTE/derived) enumerates no
    output name, yet its star silently carries every physical column of the
    underlying table. An outer reference to an UNMODELLED physical column through
    a star sub-scope must be REJECTED — it previously slipped BOTH the binder
    gate and the result audit (the ``AS <modelled-name>`` variant).
    """

    async def test_star_cte_outer_unmodelled_column_rejected(self, _mock_model):
        shape = _shape_with_columns({"region", "amount"})
        query = _complex_query(
            "WITH q AS (SELECT * FROM modely) SELECT salary FROM q",
            from_tables=["modely", "q"],
            cte_aliases=["q"],
        )
        db = AsyncMock()
        with _patched(_mock_model, shape):
            with pytest.raises(SemanticBindingError, match="salary"):
                await bind_and_get(query, db)

    async def test_star_derived_alias_qualified_unmodelled_rejected(self, _mock_model):
        shape = _shape_with_columns({"region", "amount"})
        query = _complex_query(
            "SELECT x.salary FROM (SELECT * FROM modely) x",
            from_tables=["modely"],
            cte_aliases=[],
            select_star=False,
        )
        db = AsyncMock()
        with _patched(_mock_model, shape):
            with pytest.raises(SemanticBindingError, match="salary"):
                await bind_and_get(query, db)

    async def test_star_cte_unmodelled_aliased_to_modelled_name_rejected(self, _mock_model):
        # The audit-bypass variant: the returned column is named ``region``
        # (authorised), so ONLY the binder gate can catch the unmodelled input.
        shape = _shape_with_columns({"region", "amount"})
        query = _complex_query(
            "WITH q AS (SELECT * FROM modely) SELECT salary AS region FROM q",
            from_tables=["modely", "q"],
            cte_aliases=["q"],
            select_star=False,
        )
        db = AsyncMock()
        with _patched(_mock_model, shape):
            with pytest.raises(SemanticBindingError, match="salary"):
                await bind_and_get(query, db)

    async def test_star_cte_where_unmodelled_rejected(self, _mock_model):
        shape = _shape_with_columns({"region", "amount"})
        query = _complex_query(
            "WITH q AS (SELECT * FROM modely) SELECT region FROM q "
            "WHERE salary > 100000",
            from_tables=["modely", "q"],
            cte_aliases=["q"],
            select_star=False,
        )
        db = AsyncMock()
        with _patched(_mock_model, shape):
            with pytest.raises(SemanticBindingError, match="salary"):
                await bind_and_get(query, db)

    async def test_nested_star_ctes_unmodelled_rejected(self, _mock_model):
        shape = _shape_with_columns({"region", "amount"})
        query = _complex_query(
            "WITH q AS (SELECT * FROM modely), r AS (SELECT * FROM q) "
            "SELECT salary FROM r",
            from_tables=["modely", "q", "r"],
            cte_aliases=["q", "r"],
        )
        db = AsyncMock()
        with _patched(_mock_model, shape):
            with pytest.raises(SemanticBindingError, match="salary"):
                await bind_and_get(query, db)

    async def test_star_cte_modelled_column_through_star_allowed(self, _mock_model):
        # A MODELLED column referenced through a star sub-scope is fine — the
        # star's underlying table is the model, so ``region`` is a legal read.
        shape = _shape_with_columns({"region", "amount"})
        query = _complex_query(
            "WITH q AS (SELECT * FROM modely) SELECT region FROM q",
            from_tables=["modely", "q"],
            cte_aliases=["q"],
            select_star=False,
        )
        db = AsyncMock()
        with _patched(_mock_model, shape):
            bound = await bind_and_get(query, db)
            assert bound is not None

    async def test_lateral_star_subscope_unmodelled_column_rejected(self, _mock_model):
        # Fable FINDING 2-1: sqlglot reports a LATERAL derived table's outer source
        # Scope with an EMPTY ``.selects`` (the SELECT * lives in a separate
        # intermediate scope), so an empty projection MUST be treated as opaque
        # (star). Otherwise ``s.salary`` — qualified by the LATERAL alias — would
        # be exempted as a "named" output and the unmodelled column disclosed.
        shape = _shape_with_columns({"region", "amount", "id"})
        query = _complex_query(
            "SELECT s.salary FROM modely m, LATERAL (SELECT * FROM modely) s",
            from_tables=["modely"],
            cte_aliases=[],
            select_star=False,
        )
        db = AsyncMock()
        with _patched(_mock_model, shape):
            with pytest.raises(SemanticBindingError, match="salary"):
                await bind_and_get(query, db)

    async def test_setop_cte_star_branch_unmodelled_rejected(self, _mock_model):
        # A CTE that is itself a UNION reports only its FIRST branch via
        # ``.selects``; a star in a LATER branch must still make the sub-scope
        # opaque, else ``SELECT salary FROM q`` reads the star branch's unmodelled
        # column (Fable-round-3 residual).
        shape = _shape_with_columns({"region", "amount"})
        query = _complex_query(
            "WITH q AS (SELECT region FROM modely UNION SELECT * FROM modely) "
            "SELECT salary FROM q",
            from_tables=["modely", "q"],
            cte_aliases=["q"],
            select_star=False,
        )
        db = AsyncMock()
        with _patched(_mock_model, shape):
            with pytest.raises(SemanticBindingError, match="salary"):
                await bind_and_get(query, db)

    async def test_setop_cte_star_first_branch_unmodelled_rejected(self, _mock_model):
        shape = _shape_with_columns({"region", "amount"})
        query = _complex_query(
            "WITH q AS (SELECT * FROM modely UNION SELECT region FROM modely) "
            "SELECT ssn FROM q",
            from_tables=["modely", "q"],
            cte_aliases=["q"],
            select_star=False,
        )
        db = AsyncMock()
        with _patched(_mock_model, shape):
            with pytest.raises(SemanticBindingError, match="ssn"):
                await bind_and_get(query, db)

    async def test_lateral_star_where_unmodelled_rejected(self, _mock_model):
        shape = _shape_with_columns({"region", "amount", "id"})
        query = _complex_query(
            "SELECT m.id FROM modely m, LATERAL (SELECT * FROM modely) s "
            "WHERE s.salary > 0",
            from_tables=["modely"],
            cte_aliases=[],
            select_star=False,
        )
        db = AsyncMock()
        with _patched(_mock_model, shape):
            with pytest.raises(SemanticBindingError, match="salary"):
                await bind_and_get(query, db)


class TestQualifiedStarAllowed:
    """Fable FINDING 2-2: a qualified star ``alias.*`` parses to
    ``Column(this=Star, name='*')`` and is collected by find_all(Column) (a bare
    ``*`` is not). It expands to the already table-contained relation's physical
    columns and discloses nothing beyond a bare ``SELECT *`` (exempt), so it must
    NOT false-reject on a literal column named "*".
    """

    async def test_qualified_star_over_model_table_allowed(self, _mock_model):
        shape = _shape_with_columns({"region", "amount", "id"})
        query = _complex_query(
            "SELECT t.* FROM modely t JOIN modely u ON t.id = u.id",
            from_tables=["modely"],
            cte_aliases=[],
            select_star=False,
        )
        db = AsyncMock()
        with _patched(_mock_model, shape):
            bound = await bind_and_get(query, db)
            assert bound is not None

    async def test_qualified_star_over_cte_allowed(self, _mock_model):
        shape = _shape_with_columns({"region", "amount", "id"})
        query = _complex_query(
            "WITH q AS (SELECT * FROM modely) SELECT q.* FROM q",
            from_tables=["modely", "q"],
            cte_aliases=["q"],
        )
        db = AsyncMock()
        with _patched(_mock_model, shape):
            bound = await bind_and_get(query, db)
            assert bound is not None


class TestModelledColumnAllowed:
    """Complex SQL that references only modelled columns must still bind."""

    async def test_cte_modelled_columns_allowed(self, _mock_model):
        shape = _shape_with_columns({"region", "amount"})
        query = _complex_query(
            "WITH q AS (SELECT region, amount FROM modely WHERE region = 'US') "
            "SELECT * FROM q",
            from_tables=["modely", "q"],
            cte_aliases=["q"],
        )
        db = AsyncMock()
        with _patched(_mock_model, shape):
            bound = await bind_and_get(query, db)
            assert bound is not None
            # The result audit stays active for complex SQL: the validated model
            # physical set is published for the post-execute column audit.
            assert bound.allowed_physical_columns >= {"region", "amount"}

    async def test_pure_star_over_model_needs_no_column_vocabulary(self, _mock_model):
        # A pure ``SELECT *`` references no explicit physical column -> allowed
        # even with an empty deployed physical set (mirrors Bug-6964 table gate).
        shape = _shape_with_columns(set())
        query = _complex_query(
            "WITH q AS (SELECT * FROM modely) SELECT * FROM q",
            from_tables=["modely", "q"],
            cte_aliases=["q"],
        )
        db = AsyncMock()
        with _patched(_mock_model, shape):
            bound = await bind_and_get(query, db)
            assert bound is not None


class TestFailClosed:
    """Ambiguity / unverifiable vocabulary fails closed."""

    async def test_physical_ref_with_empty_vocabulary_fails_closed(self, _mock_model):
        # A physical column IS referenced but the deployed model exposes no
        # columns -> cannot prove containment -> REJECT.
        shape = _shape_with_columns(set())
        query = _complex_query(
            "WITH q AS (SELECT some_col FROM modely) SELECT * FROM q",
            from_tables=["modely", "q"],
            cte_aliases=["q"],
        )
        db = AsyncMock()
        with _patched(_mock_model, shape):
            with pytest.raises(SemanticBindingError):
                await bind_and_get(query, db)

    async def test_integ07_group_by_alias_fails_closed_on_empty_vocabulary(
        self, _mock_model,
    ):
        # INTEG-07: a GROUP BY of a SELECT alias is exempt only when it is
        # provably NOT a modelled input. With an EMPTY (unverifiable) vocabulary
        # ``name not in _model_set`` is vacuously true, so the pre-fix code
        # exempted the GROUP BY name and returned a bound query — an unverifiable
        # read slipped through. It must fail closed instead.
        shape = _shape_with_columns(set())
        query = _complex_query(
            "SELECT COUNT(*) AS g FROM modely GROUP BY g",
            from_tables=["modely"],
            cte_aliases=[],
            select_star=False,
        )
        db = AsyncMock()
        with _patched(_mock_model, shape):
            with pytest.raises(SemanticBindingError):
                await bind_and_get(query, db)

    async def test_integ06_unrelated_published_name_not_exempted(self, _mock_model):
        # INTEG-06: a NESTED subquery publishes the name ``leaked`` via
        # ``AS y(leaked)``. That subquery is NOT a source of the outer scope,
        # whose only source is the star derived table ``s`` (SELECT * FROM
        # modely); the outer bare ``leaked`` is therefore an UNMODELLED physical
        # read through the star. The prior exemption matched ``leaked`` against
        # ANY relation's published list statement-wide (including the nested,
        # out-of-scope ``y``) and suppressed validation. Restricting the
        # exemption to THIS scope's sources makes the read fail closed.
        shape = _shape_with_columns({"region", "amount"})
        query = _complex_query(
            "SELECT leaked FROM (SELECT * FROM modely) AS s "
            "WHERE EXISTS (SELECT 1 FROM (SELECT amount FROM modely) AS y(leaked))",
            from_tables=["modely"],
            cte_aliases=[],
            select_star=False,
        )
        db = AsyncMock()
        with _patched(_mock_model, shape):
            with pytest.raises(SemanticBindingError, match="leaked"):
                await bind_and_get(query, db)


async def bind_and_get(query, db):
    from src.semantic.binder import bind_query_to_model

    return await bind_query_to_model(query, db)
