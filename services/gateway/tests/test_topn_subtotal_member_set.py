"""F-002-01 regression: Top-N subtotal / grand-total must honour the ranked set.

A Top-N (TopCount/BottomCount) pivot applies its ranking as an
``ORDER BY <measure> ... LIMIT N`` on the DETAIL query only. Before this fix the
subtotal / grand-total grain queries were built from ``where_sql`` and never
received the ranked member set, so a Top-5 pivot showing five rows could print a
grand total that summed all six-plus members — a silently wrong board-pack
number (report S3 / Business-Logic filter mode: detail 400, grand total 450).

These are known-answer tests over the two deterministic seams that compose the
fix: ``_topn_member_predicate`` (resolve the surviving member set once from the
detail result) and ``_build_grain_sql`` (which applies the shared WHERE to every
grain query). Together they prove the grand total agrees with the visible
members.
"""

import pytest

from src.dax.xmla_server import (
    _topn_member_predicate,
    _topn_requery_survivor_predicate,
    _extract_topn_spec,
    _alias_result_dimensions_for_hierarchy_axes,
)
from src.dax.subtotal_engine import _build_grain_sql, SUBTOTAL_LEVEL_KEY
from src.dax.mdx_calc_members import (
    DenomReQuerySpec,
    ReQuerySpec,
    build_denominator_requery_sql,
    build_requery_sql,
)
from shared.connector_qualify import quote_identifier


def _q(name: str) -> str:
    return quote_identifier("postgresql", name)


# The S3 known-answer dataset: six products with descending Sales. Top-5 by Sales
# leaves 100+90+80+70+60 = 400 visible; the hidden sixth (50) must NOT appear in
# any subtotal or grand total (450 is the wrong answer).
_DETAIL_ROWS_TOP5 = [
    {"product": "P1", "Sales": 100},
    {"product": "P2", "Sales": 90},
    {"product": "P3", "Sales": 80},
    {"product": "P4", "Sales": 70},
    {"product": "P5", "Sales": 60},
    # P6 (Sales 50) is absent — the detail query's LIMIT 5 already dropped it.
]


class TestTopNSpecStillExtracts:
    def test_topcount_extracted(self):
        spec = _extract_topn_spec(
            "TopCount([Product].[Product].Members, 5, [Measures].[Sales])"
        )
        assert spec is not None
        assert spec.count == 5
        assert spec.measure == "Sales"
        assert spec.descending is True


class TestTopNMemberPredicateSingleColumn:
    def test_predicate_constrains_to_visible_members(self):
        pred = _topn_member_predicate(
            detail_rows=_DETAIL_ROWS_TOP5,
            grain_dim_cols=["product"],
            quote_fn=_q,
            dim_type_map={},
        )
        assert pred is not None
        # A compact IN over exactly the five surviving members, in appearance
        # order, quoted for postgres. The hidden P6 must not appear.
        assert pred == (
            "\"product\" IN ('P1', 'P2', 'P3', 'P4', 'P5')"
        )
        assert "P6" not in pred

    def test_predicate_none_without_rows(self):
        assert _topn_member_predicate(
            detail_rows=[], grain_dim_cols=["product"], quote_fn=_q,
        ) is None

    def test_predicate_none_without_grain_cols(self):
        assert _topn_member_predicate(
            detail_rows=_DETAIL_ROWS_TOP5, grain_dim_cols=[], quote_fn=_q,
        ) is None

    def test_null_member_key_matched_explicitly(self):
        rows = [{"product": "P1"}, {"product": None}]
        pred = _topn_member_predicate(
            detail_rows=rows, grain_dim_cols=["product"], quote_fn=_q,
        )
        assert pred is not None
        assert "\"product\" IN ('P1')" in pred
        assert "\"product\" IS NULL" in pred
        assert pred.startswith("(") and " OR " in pred


class TestTopNMemberPredicateMultiColumn:
    def test_composite_tuple_predicate(self):
        rows = [
            {"category": "A", "product": "P1"},
            {"category": "A", "product": "P2"},
            {"category": "B", "product": "P5"},
        ]
        pred = _topn_member_predicate(
            detail_rows=rows,
            grain_dim_cols=["category", "product"],
            quote_fn=_q,
        )
        assert pred is not None
        # OR of composite tuple equalities — matches the exact surviving member
        # COMBINATIONS, not the cross-product.
        assert pred == (
            "((\"category\" = 'A' AND \"product\" = 'P1') OR "
            "(\"category\" = 'A' AND \"product\" = 'P2') OR "
            "(\"category\" = 'B' AND \"product\" = 'P5'))"
        )

    def test_deduplicates_repeated_tuples(self):
        rows = [
            {"category": "A", "product": "P1"},
            {"category": "A", "product": "P1"},
            {"category": "A", "product": "P2"},
        ]
        pred = _topn_member_predicate(
            detail_rows=rows,
            grain_dim_cols=["category", "product"],
            quote_fn=_q,
        )
        # Two distinct tuples only.
        assert pred.count(" OR ") == 1


class TestGrandTotalAgreesWithVisibleMembers:
    """The end-to-end known answer: the grand-total grain query, when it carries
    the Top-N member predicate, sums only the visible members (400) — not all
    six (450)."""

    def _grand_total_sql(self, where_clauses):
        # Grand total = no grain dims, SUM(Sales) over the whole table with the
        # given WHERE clauses (the shape build_subtotal_queries produces for the
        # grand-total grain).
        return _build_grain_sql(
            grain_dims=[],
            mdx_measures=["Sales"],
            measure_agg={"Sales": "SUM"},
            measure_canonical={"sales": "Sales"},
            where_sql_clauses=where_clauses,
            model_slug="modelx",
        )

    def test_grand_total_without_constraint_is_unbounded(self):
        # Baseline (the OLD, buggy behaviour): no Top-N predicate -> the grand
        # total SQL has no member constraint, so it would sum all members.
        sql = self._grand_total_sql([])
        assert "WHERE" not in sql
        assert 'SUM("Sales")' in sql

    def test_grand_total_with_topn_predicate_is_constrained(self):
        pred = _topn_member_predicate(
            detail_rows=_DETAIL_ROWS_TOP5,
            grain_dim_cols=["product"],
            quote_fn=_q,
        )
        sql = self._grand_total_sql([pred])
        # The grand total now sums ONLY the five visible products.
        assert 'WHERE "product" IN (\'P1\', \'P2\', \'P3\', \'P4\', \'P5\')' in sql
        assert "P6" not in sql
        assert 'SUM("Sales")' in sql

    def test_subtotal_grain_also_constrained(self):
        # A Category-grain subtotal must sum only the surviving products too.
        rows = [
            {"category": "A", "product": "P1", "Sales": 100},
            {"category": "A", "product": "P2", "Sales": 90},
            {"category": "B", "product": "P3", "Sales": 80},
        ]
        pred = _topn_member_predicate(
            detail_rows=rows,
            grain_dim_cols=["category", "product"],
            quote_fn=_q,
        )
        sql = _build_grain_sql(
            grain_dims=["category"],
            mdx_measures=["Sales"],
            measure_agg={"Sales": "SUM"},
            measure_canonical={"sales": "Sales"},
            where_sql_clauses=[pred],
            model_slug="modelx",
        )
        assert "GROUP BY \"category\"" in sql
        assert "WHERE ((\"category\" = 'A'" in sql


class TestBug8283CalcMemberDenominatorConstrained:
    """Bug-8283: a calculated-member (Show-Values-As) denominator / aggregate
    re-query on a Top-N pivot must be scoped to the SAME surviving ranked member
    set as the detail query, or the % is computed over hidden members.

    The re-query builders (``build_denominator_requery_sql`` for pct_grand_total
    / pct_parent / pct_axis_total, ``build_requery_sql`` for aggregate_set) both
    append ``spec.extra_where`` to their WHERE. The fix appends the Top-N member
    predicate into that ``extra_where``, so these tests prove each re-query kind
    then constrains its denominator to the visible members.

    Known-answer: for the S3 Top-5 set {100,90,80,70,60} (hidden 6th = 50) the
    constrained denominator is 400, so the top member is 100/400 = 25.0% — NOT
    100/450 = 22.2% (the unconstrained, all-member wrong answer).
    """

    def _topn_pred(self):
        return _topn_member_predicate(
            detail_rows=_DETAIL_ROWS_TOP5,
            grain_dim_cols=["product"],
            quote_fn=_q,
        )

    def test_grand_total_denominator_requery_constrained(self):
        # pct_grand_total over a non-additive (avg) measure re-queries the
        # denominator from fact grain; with the Top-N predicate in extra_where
        # it must restrict to exactly the five surviving products.
        pred = self._topn_pred()
        spec = DenomReQuerySpec(
            calc_name="PctGT",
            measure_name="Sales",
            agg="avg",
            model_slug="modelx",
            partition_key="__grand__",
            extra_where=[pred],
        )
        sql = build_denominator_requery_sql(spec)
        assert "\"product\" IN ('P1', 'P2', 'P3', 'P4', 'P5')" in sql
        assert "P6" not in sql
        assert sql.startswith('SELECT AVG("Sales")')

    def test_parent_total_denominator_requery_constrained(self):
        # pct_parent pins the parent tuple AND (via extra_where) the surviving
        # member set, so the parent total only aggregates surviving children.
        pred = self._topn_pred()
        spec = DenomReQuerySpec(
            calc_name="PctParent",
            measure_name="Sales",
            agg="count_distinct",
            model_slug="modelx",
            partition_key=("A",),
            partition_dims=["category"],
            partition_values=["A"],
            extra_where=[pred],
        )
        sql = build_denominator_requery_sql(spec)
        assert "\"category\" = 'A'" in sql
        assert "\"product\" IN ('P1', 'P2', 'P3', 'P4', 'P5')" in sql
        assert "P6" not in sql

    def test_axis_total_denominator_requery_constrained(self):
        # pct_axis_total (% of Row/Column Total) pins the axis tuple; the Top-N
        # predicate must still restrict to survivors.
        pred = self._topn_pred()
        spec = DenomReQuerySpec(
            calc_name="PctAxis",
            measure_name="Sales",
            agg="max",
            model_slug="modelx",
            partition_key=("2024",),
            partition_dims=["year"],
            partition_values=["2024"],
            extra_where=[pred],
        )
        sql = build_denominator_requery_sql(spec)
        assert "\"year\" = '2024'" in sql
        assert "\"product\" IN ('P1', 'P2', 'P3', 'P4', 'P5')" in sql

    def test_aggregate_set_requery_constrained(self):
        # aggregate_set (custom group total) re-query also honours extra_where,
        # so a custom group total on a Top-N pivot sums only surviving members.
        pred = self._topn_pred()
        spec = ReQuerySpec(
            calc_name="MyGroup",
            measure_name="Sales",
            agg="avg",
            dim_col="product",
            members=["P1", "P2", "P3", "P4", "P5"],
            model_slug="modelx",
            extra_where=[pred],
        )
        sql = build_requery_sql(spec)
        # Both the group member list AND the Top-N survivor predicate are present.
        assert sql.count("\"product\" IN ('P1', 'P2', 'P3', 'P4', 'P5')") == 2
        assert "P6" not in sql

    def test_denominator_numeric_known_answer_25_not_22(self):
        """End-to-end known-answer at the denominator arithmetic level.

        The constrained denominator predicate selects exactly the survivors, so
        summing the visible measure values yields 400 and the top member's share
        is 25.0%. The unconstrained (all-member) denominator would be 450, giving
        the WRONG 22.2%.
        """
        survivors = {"P1": 100, "P2": 90, "P3": 80, "P4": 70, "P5": 60}
        hidden_all = dict(survivors, P6=50)

        # The predicate identifies exactly the survivor keys.
        pred = self._topn_pred()
        for member in survivors:
            assert f"'{member}'" in pred
        assert "'P6'" not in pred

        constrained_denom = sum(
            v for k, v in hidden_all.items() if f"'{k}'" in pred
        )
        assert constrained_denom == 400
        top_share = round(100 * survivors["P1"] / constrained_denom, 1)
        assert top_share == 25.0

        # Guard against the regression: the all-member denominator is the wrong
        # 450 -> 22.2%, which the constraint must prevent.
        wrong_denom = sum(hidden_all.values())
        assert wrong_denom == 450
        assert round(100 * survivors["P1"] / wrong_denom, 1) == 22.2


class TestBug8283SurvivorPredicateHelper:
    """Bug-8283: pin the PRODUCTION derivation ``_topn_requery_survivor_predicate``
    directly, so a revert of any of its three guards (R3 detail-only filter, R1
    post-alias keying, R4 source-name translation) breaks a wired unit test —
    not just a witness that re-implements the logic.
    """

    def _aliased_state(self):
        # Detail result BEFORE aliasing: source column "product", five survivors.
        columns = ["product", "Sales"]
        rows = [
            {"product": "P1", "Sales": 100},
            {"product": "P2", "Sales": 90},
            {"product": "P3", "Sales": 80},
            {"product": "P4", "Sales": 70},
            {"product": "P5", "Sales": 60},
        ]
        dimensions_meta = [{"name": "product", "data_type": "text"}]
        # A dimension-alias axis renames "product" -> "ProductByRegion".
        alias_to_source = {"ProductByRegion": "product"}
        cols2, rows2, dims2 = _alias_result_dimensions_for_hierarchy_axes(
            columns=columns,
            rows=rows,
            dimensions_meta=dimensions_meta,
            alias_to_source=alias_to_source,
        )
        return cols2, rows2, dims2, alias_to_source

    def test_alias_state_setup(self):
        columns, rows, dimensions_meta, _ = self._aliased_state()
        # The source column name is REPLACED by the alias in the result columns.
        assert "ProductByRegion" in columns and "product" not in columns
        # Rows carry BOTH keys.
        assert rows[0]["ProductByRegion"] == "P1"
        assert rows[0]["product"] == "P1"

    def test_helper_emits_source_identifier_under_alias(self):
        # R1 + R4: the helper keys grain cols on the post-alias dim set, then
        # emits the SOURCE column name ("product") — the bindable identifier —
        # NOT the MDX alias ("ProductByRegion").
        columns, rows, dimensions_meta, axis_aliases = self._aliased_state()
        dim_names_set = {(d.get("name") or "") for d in dimensions_meta}
        pred, grain_cols = _topn_requery_survivor_predicate(
            rows=rows,
            columns=columns,
            dim_names_set=dim_names_set,
            axis_aliases=axis_aliases,
            dimensions_meta=dimensions_meta,
        )
        assert grain_cols == ["ProductByRegion"]  # post-alias basis
        assert pred == "\"product\" IN ('P1', 'P2', 'P3', 'P4', 'P5')"
        assert "ProductByRegion" not in pred  # alias must not reach the SQL

    def test_helper_pre_alias_keying_would_miss_regression(self):
        # R1 regression witness at the PRODUCTION level: if the helper keyed on a
        # pre-alias dim set, no grain cols match -> no predicate. (Passing the
        # pre-alias set here reproduces the reverted behaviour.)
        columns, rows, dimensions_meta, axis_aliases = self._aliased_state()
        pred, grain_cols = _topn_requery_survivor_predicate(
            rows=rows,
            columns=columns,
            dim_names_set={"product"},  # pre-alias set (source only)
            axis_aliases=axis_aliases,
            dimensions_meta=dimensions_meta,
        )
        assert grain_cols == [] and pred is None

    def _merged_rows(self):
        # Detail rows: two surviving (country, city) survivors after Top-N.
        detail = [
            {"country": "US", "city": "NYC", "Sales": 100,
             SUBTOTAL_LEVEL_KEY: "detail"},
            {"country": "US", "city": "LA", "Sales": 90,
             SUBTOTAL_LEVEL_KEY: "detail"},
        ]
        # Subtotal row (country grain): finer "city" column is None. Grand-total
        # row: both dims None. These MUST NOT enter the survivor set.
        subtotal = [
            {"country": "US", "city": None, "Sales": 190,
             SUBTOTAL_LEVEL_KEY: "Country"},
            {"country": None, "city": None, "Sales": 190,
             SUBTOTAL_LEVEL_KEY: "GrandTotal"},
        ]
        return subtotal + detail  # merge order: subtotals first, then detail

    def test_helper_excludes_subtotal_rows_from_survivor_set(self):
        # R3 finding 1 pinned at the PRODUCTION level: feeding MERGED rows to the
        # helper must still yield ONLY the detail survivor tuples — no spurious
        # ``IS NULL`` branch from the subtotal / grand-total rows. Reverting the
        # detail-only filter (rows instead of detail_rows) breaks this assertion.
        merged = self._merged_rows()
        columns = ["country", "city", "Sales"]
        dimensions_meta = [
            {"name": "country", "data_type": "text"},
            {"name": "city", "data_type": "text"},
        ]
        dim_names_set = {"country", "city"}
        pred, grain_cols = _topn_requery_survivor_predicate(
            rows=merged,
            columns=columns,
            dim_names_set=dim_names_set,
            axis_aliases={},  # no aliasing on this pivot
            dimensions_meta=dimensions_meta,
        )
        assert grain_cols == ["country", "city"]
        assert pred == (
            "((\"country\" = 'US' AND \"city\" = 'NYC') OR "
            "(\"country\" = 'US' AND \"city\" = 'LA'))"
        )
        assert "IS NULL" not in pred, (
            "subtotal / grand-total rows contaminated the survivor set with "
            "spurious NULL branches (detail-only filter reverted?)"
        )

    def test_witness_merged_rows_would_contaminate_without_filter(self):
        # Witness that the danger is real: the raw predicate over the merged rows
        # (bypassing the helper's filter) DOES inject the contaminating NULLs.
        merged = self._merged_rows()
        contaminated = _topn_member_predicate(
            detail_rows=merged, grain_dim_cols=["country", "city"], quote_fn=_q,
        )
        assert "IS NULL" in contaminated
