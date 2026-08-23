"""Bug-5462: WHERE filters on INTEGER-keyed dimensions must emit NUMERIC
literals, not string literals.

A BI client (Excel / Power BI / any XMLA client) renders a slicer member key
as a quoted string, e.g. ``"d_year" = '1999'``. When the target column is
INTEGER/NUMERIC, BigQuery rejects the comparison:

    400 No matching signature for operator = for argument types: INT64, STRING

The fix renders the filter literal according to the resolved column data type:
numeric columns get a bare numeric literal (``= 1999``), text columns keep the
string literal (``= 'Shoes'``). This is dialect-neutral — the predicate is
assembled as PostgreSQL-canonical and transpiled via sqlglot, so it is correct
for BigQuery, Postgres, and every other target.

This module covers the raw ``_qualify_where`` path inside
``_build_where_clause`` (taken when the WHERE has predicates that the IR's
LogicalFilter cannot represent, e.g. an OR). The extracted-filter path
(``_render_condition`` / ``_render_value``) is covered in
``test_query_rewriter.py``.
"""
from __future__ import annotations

import types

import pytest
import sqlglot

from src.rewrite import source_sql as S
from src.ir.logical_query import SemanticBindingError


def _make_bound_query(raw_query: str):
    lq = types.SimpleNamespace(
        has_unresolvable_where=True,
        input_dialect="postgres",
        raw_query=raw_query,
    )
    return types.SimpleNamespace(logical_query=lq, resolved_measures=[])


def _phys_expr_factory(mapping: dict[str, str]):
    def _get_phys_expr(name: str, *, pg_canonical: bool = False):
        return mapping.get(name)
    return _get_phys_expr


def _col_type_factory(mapping: dict[str, str]):
    def _get_col_type(name: str):
        return mapping.get(name)
    return _get_col_type


def _build(raw_query, *, dims, phys, ctypes):
    return S._build_where_clause(
        "SELECT x FROM t",
        _make_bound_query(raw_query),
        dimensions_by_name={d: object() for d in dims},
        _order_measures=[],
        field_expr_by_name={},
        filter_col_type_by_name={},
        _get_phys_expr=_phys_expr_factory(phys),
        _get_col_type=_col_type_factory(ctypes),
    )


def test_raw_where_integer_dim_emits_numeric_literal():
    # Double-quoted value (member key) is parsed as a Column on the value side;
    # the OR forces the raw-WHERE preservation path.
    out = _build(
        'SELECT x FROM t WHERE ("d_year" = "1999") OR ("d_year" = "2000")',
        dims=["d_year"],
        phys={"d_year": '"dt"."d_year"'},
        ctypes={"d_year": "INT64"},
    )
    where = out.split(" WHERE ", 1)[1]
    assert "= 1999" in where
    assert "= 2000" in where
    assert "'1999'" not in where


def test_raw_where_integer_dim_transpiles_for_bigquery():
    out = _build(
        'SELECT x FROM t WHERE ("d_year" = "1999") OR ("d_year" = "2000")',
        dims=["d_year"],
        phys={"d_year": '"dt"."d_year"'},
        ctypes={"d_year": "INT64"},
    )
    where = out.split(" WHERE ", 1)[1]
    bq = sqlglot.transpile(where, read="postgres", write="bigquery")[0]
    assert "= 1999" in bq
    assert "'1999'" not in bq
    assert "`dt`.`d_year`" in bq


def test_raw_where_string_dim_keeps_string_literal():
    out = _build(
        'SELECT x FROM t WHERE ("i_category" = "Shoes") OR ("i_category" = "Boots")',
        dims=["i_category"],
        phys={"i_category": '"it"."i_category"'},
        ctypes={"i_category": "STRING"},
    )
    where = out.split(" WHERE ", 1)[1]
    assert "'Shoes'" in where
    assert "'Boots'" in where


def test_raw_where_phys_name_fallback_resolves_type():
    """When the semantic name (``year``) differs from the physical column name
    (``d_year``): the raw path rewrites the field side to the physical name
    before the value side is visited, so the col_type resolver is queried with
    the PHYSICAL name. The numeric type must still resolve so the literal stays
    numeric. (This exercises the real physical-name fallback, not the
    semantic-name lookup.)"""
    out = S._build_where_clause(
        "SELECT x FROM t",
        _make_bound_query(
            'SELECT x FROM t WHERE ("year" = "1999") OR ("year" = "2000")'
        ),
        dimensions_by_name={"year": object()},  # semantic name
        _order_measures=[],
        field_expr_by_name={},
        filter_col_type_by_name={},
        # _get_phys_expr maps semantic 'year' -> physical 'd_year' column.
        _get_phys_expr=_phys_expr_factory({"year": '"dt"."d_year"'}),
        # col-type resolver knows the PHYSICAL name only (post-rewrite lookup).
        _get_col_type=_col_type_factory({"d_year": "INT64"}),
    )
    where = out.split(" WHERE ", 1)[1]
    assert "= 1999" in where
    assert "'1999'" not in where


def test_raw_where_non_numeric_value_against_numeric_col_fails_loud():
    """A non-numeric member key against a numeric column must fail loud, not
    silently corrupt the predicate."""
    with pytest.raises(SemanticBindingError):
        _build(
            'SELECT x FROM t WHERE ("d_year" = "abc") OR ("d_year" = "2000")',
            dims=["d_year"],
            phys={"d_year": '"dt"."d_year"'},
            ctypes={"d_year": "INT64"},
        )


def test_raw_where_in_list_integer_dim_numeric():
    out = _build(
        'SELECT x FROM t WHERE "d_year" IN ("1999", "2000") OR "d_year" = "2001"',
        dims=["d_year"],
        phys={"d_year": '"dt"."d_year"'},
        ctypes={"d_year": "INT64"},
    )
    where = out.split(" WHERE ", 1)[1]
    assert "1999" in where and "'1999'" not in where
    assert "2000" in where and "'2000'" not in where


# ---------------------------------------------------------------------------
# Bug-5538 (Codex findings 2 & 3): genuine SQL string literals ('1999') — not
# double-quoted member-key Columns — in OR / IN / subselect shapes must ALSO be
# re-typed to numeric literals against an INTEGER column. These are the Excel
# set / multi-member slicer and subselect filter forms reported as failing.
# ---------------------------------------------------------------------------


def test_raw_where_string_literal_eq_integer_dim_numeric():
    """`d_year = '1999'` (a real string literal) against INT64 → bare 1999."""
    out = _build(
        "SELECT x FROM t WHERE \"d_year\" = '1999' OR \"d_year\" = '2000'",
        dims=["d_year"],
        phys={"d_year": '"dt"."d_year"'},
        ctypes={"d_year": "INT64"},
    )
    where = out.split(" WHERE ", 1)[1]
    assert "= 1999" in where
    assert "= 2000" in where
    assert "'1999'" not in where and "'2000'" not in where


def test_raw_where_string_literal_in_list_integer_dim_numeric():
    """Excel set/multi-member slicer: `IN ('1999','2000')` against INT64."""
    out = _build(
        "SELECT x FROM t WHERE \"d_year\" IN ('1999', '2000') OR \"d_year\" = '2001'",
        dims=["d_year"],
        phys={"d_year": '"dt"."d_year"'},
        ctypes={"d_year": "INT64"},
    )
    where = out.split(" WHERE ", 1)[1]
    assert "IN (1999, 2000)" in where
    assert "= 2001" in where
    assert "'1999'" not in where and "'2000'" not in where and "'2001'" not in where


def test_raw_where_string_literal_in_list_transpiles_for_bigquery():
    out = _build(
        "SELECT x FROM t WHERE \"d_year\" IN ('1999', '2000') OR \"d_year\" = '2001'",
        dims=["d_year"],
        phys={"d_year": '"dt"."d_year"'},
        ctypes={"d_year": "INT64"},
    )
    where = out.split(" WHERE ", 1)[1]
    bq = sqlglot.transpile(where, read="postgres", write="bigquery")[0]
    assert "IN (1999, 2000)" in bq
    assert "'1999'" not in bq
    assert "`dt`.`d_year`" in bq


def test_raw_where_string_literal_in_list_string_dim_unchanged():
    """A text column keeps quoted string members (must not regress)."""
    out = _build(
        "SELECT x FROM t WHERE \"i_category\" IN ('Shoes', 'Boots') OR \"i_category\" = 'Hats'",
        dims=["i_category"],
        phys={"i_category": '"it"."i_category"'},
        ctypes={"i_category": "STRING"},
    )
    where = out.split(" WHERE ", 1)[1]
    assert "'Shoes'" in where and "'Boots'" in where and "'Hats'" in where


def test_raw_where_subselect_literal_projection_integer_dim_numeric():
    """Subselect filter form: `d_year IN (SELECT '1999')` inherits the outer
    INTEGER column type — the projected member becomes a numeric literal."""
    out = _build(
        "SELECT x FROM t WHERE \"d_year\" IN (SELECT '1999') OR \"d_year\" = '2000'",
        dims=["d_year"],
        phys={"d_year": '"dt"."d_year"'},
        ctypes={"d_year": "INT64"},
    )
    where = out.split(" WHERE ", 1)[1]
    assert "SELECT 1999" in where
    assert "= 2000" in where
    assert "'1999'" not in where and "'2000'" not in where


def test_raw_where_string_literal_non_numeric_against_integer_fails_loud():
    """A non-numeric string literal against an integer column fails loud — no
    string literal for INT64, no bare token, no injection surface."""
    with pytest.raises(SemanticBindingError):
        _build(
            "SELECT x FROM t WHERE \"d_year\" = 'abc' OR \"d_year\" = '2000'",
            dims=["d_year"],
            phys={"d_year": '"dt"."d_year"'},
            ctypes={"d_year": "INT64"},
        )


def test_raw_where_string_literal_in_member_non_numeric_fails_loud():
    with pytest.raises(SemanticBindingError):
        _build(
            "SELECT x FROM t WHERE \"d_year\" IN ('1999', '1; DROP TABLE t') OR \"d_year\" = '2000'",
            dims=["d_year"],
            phys={"d_year": '"dt"."d_year"'},
            ctypes={"d_year": "INT64"},
        )


# ---------------------------------------------------------------------------
# Bug-5538 (Codex round-2 finding 2): a NON-string (numeric / dialect-parsed)
# RHS literal against a known numeric column must ALSO clear the strict
# ``value_is_numeric_literal`` validator — not only string literals. A
# non-conforming bare token (scientific, padded, hex) fails loud; a genuine
# integer renders bare.
# ---------------------------------------------------------------------------


def test_raw_where_numeric_literal_scientific_against_integer_fails_loud():
    """`d_year = 1e9` parses as a non-string numeric literal; against INT64 it
    must fail loud rather than pass a bare ``1e9`` token through."""
    with pytest.raises(SemanticBindingError):
        _build(
            'SELECT x FROM t WHERE "d_year" = 1e9 OR "d_year" = 2000',
            dims=["d_year"],
            phys={"d_year": '"dt"."d_year"'},
            ctypes={"d_year": "INT64"},
        )


def test_raw_where_numeric_literal_genuine_integer_renders_bare():
    """A genuine non-string integer literal against INT64 stays a bare numeric
    literal (rebuilt canonically) — no regression for already-numeric RHS."""
    out = _build(
        'SELECT x FROM t WHERE "d_year" = 1999 OR "d_year" = 2000',
        dims=["d_year"],
        phys={"d_year": '"dt"."d_year"'},
        ctypes={"d_year": "INT64"},
    )
    where = out.split(" WHERE ", 1)[1]
    assert "= 1999" in where and "= 2000" in where
    assert "'1999'" not in where


def test_raw_where_numeric_literal_in_list_against_integer():
    out = _build(
        'SELECT x FROM t WHERE "d_year" IN (1999, 2000) OR "d_year" = 2001',
        dims=["d_year"],
        phys={"d_year": '"dt"."d_year"'},
        ctypes={"d_year": "INT64"},
    )
    where = out.split(" WHERE ", 1)[1]
    assert "IN (1999, 2000)" in where and "= 2001" in where


# ---------------------------------------------------------------------------
# Bug-5539 (Codex round-3 findings 1 & 2): the strict numeric grammar must gate
# the BETWEEN-bound and unary-sign value shapes too, not only IN / binary
# comparison. A scientific / sign-wrapped form against a numeric column on the
# raw (unresolvable WHERE) path must fail loud; genuine integers/decimals still
# render bare on every shape.
# ---------------------------------------------------------------------------


def test_raw_where_between_scientific_bounds_against_integer_fails_loud():
    """`int_dim BETWEEN 1e9 AND 2e9` (under OR -> raw path) must fail loud — the
    BETWEEN low/high bounds previously skipped the strict validator (finding 1)."""
    with pytest.raises(SemanticBindingError):
        _build(
            'SELECT x FROM t WHERE "d_year" BETWEEN 1e9 AND 2e9 OR "d_year" = 5',
            dims=["d_year"],
            phys={"d_year": '"dt"."d_year"'},
            ctypes={"d_year": "INT64"},
        )


def test_raw_where_between_one_scientific_bound_against_integer_fails_loud():
    """Both bounds are validated independently — a single scientific bound is
    enough to fail loud."""
    with pytest.raises(SemanticBindingError):
        _build(
            'SELECT x FROM t WHERE "d_year" BETWEEN 1998 AND 2e9 OR "d_year" = 5',
            dims=["d_year"],
            phys={"d_year": '"dt"."d_year"'},
            ctypes={"d_year": "INT64"},
        )


def test_raw_where_between_genuine_integer_bounds_render_bare():
    """A genuine integer BETWEEN against INT64 renders bare bounds (no regress)."""
    out = _build(
        'SELECT x FROM t WHERE "d_year" BETWEEN 1998 AND 2000 OR "d_year" = 5',
        dims=["d_year"],
        phys={"d_year": '"dt"."d_year"'},
        ctypes={"d_year": "INT64"},
    )
    where = out.split(" WHERE ", 1)[1]
    assert "BETWEEN 1998 AND 2000" in where


def test_raw_where_between_string_bounds_against_integer_retyped():
    """String BETWEEN bounds against a numeric column are re-typed to bare
    numerics (the BETWEEN shape now reaches the value-side re-typing)."""
    out = _build(
        "SELECT x FROM t WHERE \"d_year\" BETWEEN '1998' AND '2000' OR \"d_year\" = 5",
        dims=["d_year"],
        phys={"d_year": '"dt"."d_year"'},
        ctypes={"d_year": "INT64"},
    )
    where = out.split(" WHERE ", 1)[1]
    assert "BETWEEN 1998 AND 2000" in where
    assert "'1998'" not in where and "'2000'" not in where


def test_raw_where_negative_scientific_against_integer_fails_loud():
    """`d_year = -1e9` parses as Neg(Literal('1e9')); the SIGNED spelling
    (``-1e9``) must be validated and fail loud (finding 2) — not leave the Neg
    to emit a bare ``-1e9`` token."""
    with pytest.raises(SemanticBindingError):
        _build(
            'SELECT x FROM t WHERE "d_year" = -1e9 OR "d_year" = 5',
            dims=["d_year"],
            phys={"d_year": '"dt"."d_year"'},
            ctypes={"d_year": "INT64"},
        )


def test_raw_where_negative_integer_against_integer_renders_bare():
    """A genuine negative integer (`d_year = -5`) renders bare with its sign."""
    out = _build(
        'SELECT x FROM t WHERE "d_year" = -5 OR "d_year" = 2000',
        dims=["d_year"],
        phys={"d_year": '"dt"."d_year"'},
        ctypes={"d_year": "INT64"},
    )
    where = out.split(" WHERE ", 1)[1]
    assert "-5" in where and "= 2000" in where


def test_raw_where_negative_in_member_scientific_fails_loud():
    """A sign-wrapped scientific IN member (`IN (-1e9, 5)`) fails loud too —
    the Neg-step works inside an IN list, not only binary comparisons."""
    with pytest.raises(SemanticBindingError):
        _build(
            'SELECT x FROM t WHERE "d_year" IN (-1e9, 5) OR "d_year" = 2000',
            dims=["d_year"],
            phys={"d_year": '"dt"."d_year"'},
            ctypes={"d_year": "INT64"},
        )


# ---------------------------------------------------------------------------
# Bug-5538 (Codex round-2 finding 4): a semantic/physical bare-name collision
# must NOT mis-type the literal. The qualified (table_alias.column) resolver
# decides the rewritten physical column's type; an ambiguous bare name resolves
# to UNKNOWN (no wrong-type literal).
# ---------------------------------------------------------------------------


def _build_qualified(raw_query, *, dims, phys, qualified_ctypes, bare_ctypes=None):
    """Harness variant that supplies BOTH a qualified (table.column) type
    resolver and a bare-name resolver, exercising finding-4 precedence."""
    def _get_col_type(name: str):
        return (bare_ctypes or {}).get(name)

    def _get_col_type_for_field(field_node):
        tbl = getattr(field_node, "table", None)
        col = getattr(field_node, "name", None)
        if tbl and col:
            q = qualified_ctypes.get(f"{tbl.lower()}.{col.lower()}")
            if q is not None:
                return q
        if col:
            return _get_col_type(col)
        return None

    return S._build_where_clause(
        "SELECT x FROM t",
        _make_bound_query(raw_query),
        dimensions_by_name={d: object() for d in dims},
        _order_measures=[],
        field_expr_by_name={},
        filter_col_type_by_name={},
        _get_phys_expr=_phys_expr_factory(phys),
        _get_col_type=_get_col_type,
        _get_col_type_for_field=_get_col_type_for_field,
    )


# ---------------------------------------------------------------------------
# Bug-5546: an INTEGER column must reject a FRACTIONAL numeric literal. The
# numeric grammar accepts decimals so FLOAT/NUMERIC columns keep working, but
# "d_year = 19.99" against INT64 makes BigQuery silently coerce + return an
# empty result — a type mismatch that must fail loud, not pass silently.
# ---------------------------------------------------------------------------


def test_raw_where_fractional_literal_against_integer_fails_loud():
    """`d_year = '19.99'` against INT64 fails loud — a fractional literal is not
    a valid integer member, must not silently coerce to an empty result."""
    with pytest.raises(SemanticBindingError):
        _build(
            "SELECT x FROM t WHERE \"d_year\" = '19.99' OR \"d_year\" = '2000'",
            dims=["d_year"],
            phys={"d_year": '"dt"."d_year"'},
            ctypes={"d_year": "INT64"},
        )


def test_raw_where_fractional_numeric_literal_against_integer_fails_loud():
    """Same guard for a non-string (dialect-parsed) fractional literal."""
    with pytest.raises(SemanticBindingError):
        _build(
            'SELECT x FROM t WHERE "d_year" = 19.99 OR "d_year" = 2000',
            dims=["d_year"],
            phys={"d_year": '"dt"."d_year"'},
            ctypes={"d_year": "INT64"},
        )


def test_raw_where_fractional_literal_against_float_renders_bare():
    """A fractional literal against a FLOAT column stays a bare numeric literal
    — the integer-only guard must not over-reach to true numeric columns."""
    out = _build(
        "SELECT x FROM t WHERE \"unit_price\" = '19.99' OR \"unit_price\" = '5.00'",
        dims=["unit_price"],
        phys={"unit_price": '"f"."unit_price"'},
        ctypes={"unit_price": "FLOAT64"},
    )
    where = out.split(" WHERE ", 1)[1]
    assert "19.99" in where and "5.00" in where
    assert "'19.99'" not in where


def test_raw_where_qualified_resolver_types_by_table_column():
    """When the rewritten field carries its table alias, the qualified resolver
    picks the CORRECT (numeric) type even though a same-named semantic/physical
    twin of a different type exists — emit a bare numeric literal."""
    out = _build_qualified(
        'SELECT x FROM t WHERE "d_year" = \'1999\' OR "d_year" = \'2000\'',
        dims=["d_year"],
        phys={"d_year": '"dt"."d_year"'},
        # Qualified key resolves the physical INT column.
        qualified_ctypes={"dt.d_year": "INT64"},
        # Bare name (collision twin) declares STRING — must NOT win.
        bare_ctypes={"d_year": "STRING"},
    )
    where = out.split(" WHERE ", 1)[1]
    assert "= 1999" in where and "= 2000" in where
    assert "'1999'" not in where


def test_raw_where_ambiguous_bare_name_resolves_to_unknown_keeps_string():
    """When the qualified resolver has no entry AND the bare name is ambiguous
    (resolver returns None), the value keeps the safe string-literal default —
    no wrong-typed (numeric) literal is guessed."""
    out = _build_qualified(
        'SELECT x FROM t WHERE "d_year" = \'1999\' OR "d_year" = \'2000\'',
        dims=["d_year"],
        phys={"d_year": '"dt"."d_year"'},
        # No qualified entry; bare resolver returns None (ambiguous → UNKNOWN).
        qualified_ctypes={},
        bare_ctypes={"d_year": None},
    )
    where = out.split(" WHERE ", 1)[1]
    assert "'1999'" in where and "'2000'" in where
