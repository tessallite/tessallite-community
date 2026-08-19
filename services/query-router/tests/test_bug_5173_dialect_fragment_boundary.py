"""Bug-5173/F-006-02 regressions for canonical fragment rendering."""
from __future__ import annotations

from pathlib import Path

from src.rewrite.calendar_support import _semi_additive_agg
from src.rewrite.conditions import _render_condition
from src.rewrite.dialects import _transpile_to_dialect
from src.rewrite.joins import _coerce_join_pair
from test_render_boundary_static import (
    _dialect_call_violations,
    _is_boundary_module,
    _rewrite_module_paths,
    _transpile_write_violations,
)


def test_bug_5173_fragments_stay_canonical_until_sqlglot_boundary() -> None:
    """F-006-02 regression: fragment builders do not emit connector SQL.

    Test escape: direct fragment string construction previously invited
    connector-specific syntax drift. Guard: condition, join, and
    semi-additive fragments are canonical PostgreSQL and final target emission
    is exercised through the shared SQLGlot boundary. Tier: T3.
    """
    condition = _render_condition(
        '"f"."name"', "like", "100%", col_type="TEXT", like_escape="\\",
    )
    lhs, rhs = _coerce_join_pair(
        '"a"."date_key"', "DATE", '"b"."event_ts"', "TIMESTAMP",
    )
    semi_pg = _semi_additive_agg(
        "last_non_empty", '"f"."balance"', '"f"."event_date"', "postgres",
    )
    semi_bq = _semi_additive_agg(
        "last_non_empty", '"f"."balance"', '"f"."event_date"', "bigquery",
    )

    assert "ESCAPE" in condition
    assert "CAST(" in rhs
    assert "ARRAY_AGG" in semi_pg
    assert "OFFSET(0)" in semi_bq

    canonical = f"SELECT {condition}, {lhs} = {rhs}, {semi_pg}"
    non_semi_canonical = f"SELECT {condition}, {lhs} = {rhs}"
    for dialect in ("bigquery", "spark", "tsql"):
        rendered = _transpile_to_dialect(non_semi_canonical, dialect)
        assert rendered
    for dialect in ("bigquery", "redshift"):
        rendered = _transpile_to_dialect(canonical, dialect)
        assert rendered


def test_r4_lane_regression_semi_additive_filter_spelling_stays_canonical() -> None:
    """R4 regression: the AST semi-additive producer keeps the legacy
    ``FILTER (WHERE ...)`` spelling on the canonical postgres fragment.

    Test escape: the Bug-5173 AST rewrite rendered through SQLGlot's default
    postgres generator, which normalises ``FILTER (WHERE`` to ``FILTER(WHERE``
    and broke the established source-route string contract pinned by
    test_dax_time_variants. Guard: the canonical postgres fragment generator
    overrides filter_sql to restore the legacy spelling. Tier: T2.
    """
    semi_pg = _semi_additive_agg(
        "last_non_empty", '"f"."balance"', '"f"."event_date"', "postgres",
    )
    assert "FILTER (WHERE" in semi_pg
    assert "FILTER(WHERE" not in semi_pg


def test_bug_5173_static_guard_fails_closed_for_dynamic_render_calls(
    tmp_path: Path,
) -> None:
    """F-006-02 tool audit: dynamic calls are not an enumeration blind spot.

    Test escape: a literal-only scanner would miss a target held in a variable
    and allow a bypass. Guard: both keyword/positional ``.sql`` calls and a
    dynamic ``transpile(write=...)`` call are reported. Tier: T3.
    """
    candidate = tmp_path / "candidate.py"
    candidate.write_text(
        "target = choose_target()\n"
        "tree.sql(dialect=target)\n"
        "tree.sql(target)\n"
        "sqlglot.transpile(sql, read='postgres', write=target)\n",
        encoding="utf-8",
    )
    assert len(_dialect_call_violations(candidate)) == 2
    assert len(_transpile_write_violations(candidate)) == 1


def test_bug_5173_only_exact_package_relative_boundary_is_exempt(
    tmp_path: Path,
) -> None:
    """F-03 mutation: a nested/subpackage dialects.py remains in scope."""
    canonical = tmp_path / "dialects.py"
    nested = tmp_path / "nested" / "dialects.py"
    nested.parent.mkdir()
    canonical.write_text("tree.sql(dialect=target)\n", encoding="utf-8")
    nested.write_text("tree.sql(dialect=target)\n", encoding="utf-8")

    assert _rewrite_module_paths(tmp_path) == [canonical, nested]
    assert _is_boundary_module(canonical, tmp_path)
    assert not _is_boundary_module(nested, tmp_path)
    assert _dialect_call_violations(nested)


def test_bug_5173_transpile_guard_covers_positional_alias_and_opaque_shapes(
    tmp_path: Path,
) -> None:
    """F-03 mutations: every supported or opaque write shape fails closed.

    Test escape: the prior scanner inspected only ``write=`` and conventional
    function names, so positional, aliased, dynamic and starred target writes
    escaped enumeration. Guard: each mutation is reported, while literal
    PostgreSQL in the same positions remains canonical. Tier: T3.
    """
    candidate = tmp_path / "candidate.py"
    candidate.write_text(
        "import sqlglot as sg\n"
        "from sqlglot import transpile as convert\n"
        "emit = sg.transpile\n"
        "sg.transpile(sql, 'postgres', 'bigquery')\n"
        "sg.transpile(sql, 'postgres', target)\n"
        "convert(sql, 'postgres', 'spark')\n"
        "emit(sql, read='postgres', write=target)\n"
        "sg.transpile(sql, *args)\n"
        "convert(sql, **kwargs)\n"
        "sg.transpile(sql, 'postgres', 'postgres')\n",
        encoding="utf-8",
    )
    violations = _transpile_write_violations(candidate)
    assert len(violations) == 6, violations


def test_bug_5173_direct_render_guard_fails_closed_on_starred_shape(
    tmp_path: Path,
) -> None:
    candidate = tmp_path / "candidate.py"
    candidate.write_text(
        "tree.sql(*args)\n"
        "tree.sql(**kwargs)\n",
        encoding="utf-8",
    )
    assert len(_dialect_call_violations(candidate)) == 2


def test_bug_5173_static_guards_report_parse_failure(
    tmp_path: Path,
) -> None:
    """F-03 mutation: malformed modules fail closed instead of aborting scope."""
    candidate = tmp_path / "candidate.py"
    candidate.write_text("if broken(:\n", encoding="utf-8")
    direct = _dialect_call_violations(candidate)
    transpile = _transpile_write_violations(candidate)
    assert direct and "parse failure" in direct[0][1]
    assert transpile and "parse failure" in transpile[0][1]
