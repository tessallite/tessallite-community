"""WHERE-literal type normalization (bug intake 2026-07-07 variant-date-anchor).

``conditions.py`` used a local ``col_type.upper().split("(")[0]`` normalisation
plus a duplicate ``_TIMESTAMP_TYPES``/``_DATE_TYPES`` set. That kept the
trailing ``WITHOUT TIME ZONE`` / ``WITH TIME ZONE`` words, so a PostgreSQL
``timestamp without time zone`` column was NOT recognised as a timestamp and a
string literal rendered as a bare ``'...'`` instead of ``TIMESTAMP '...'``
(latent on PostgreSQL via implicit coercion; wrong on stricter dialects). The
normalisation now flows through the shared ``normalize_type_token`` so verbose
spellings and precision suffixes collapse to their leading token.

These tests assert the boundary behaviour (a verbose timestamp spelling types
correctly in the WHERE literal path) and guard the reconciliation: the type
families are the shared ``connector_qualify`` frozensets, not a local copy.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from shared.connector_qualify import (
    _DATE_TYPES as CQ_DATE_TYPES,
    _TIMESTAMP_TYPES as CQ_TIMESTAMP_TYPES,
)
from src.ir.logical_query import LogicalFilter
from src.rewrite.conditions import (
    _DATE_TYPES,
    _TIMESTAMP_TYPES,
    _render_condition,
    _render_value,
    _render_where,
    is_numeric_col_type,
)
from src.rewrite.dialects import _transpile_to_dialect


class TestTimestampLiteralTyping:
    @pytest.mark.parametrize(
        "col_type",
        [
            "timestamp without time zone",
            "TIMESTAMP WITHOUT TIME ZONE",
            "timestamp with time zone",   # normalizes to TIMESTAMP (first word)
            "TIMESTAMP(3)",
            "timestamp(6) without time zone",
            "TIMESTAMP",
        ],
    )
    def test_verbose_timestamp_spellings_render_timestamp_literal(self, col_type):
        # A string value against any timestamp spelling whose normalized form
        # is "TIMESTAMP" must render the PostgreSQL-canonical ``TIMESTAMP 'lit'``
        # form (transpiled per dialect downstream), not a bare string literal.
        # Note: "timestamp with time zone" normalizes to "TIMESTAMP" (first word
        # only via normalize_type_token), so it emits TIMESTAMP here.
        rendered = _render_value("2024-01-01 00:00:00", col_type)
        assert rendered == "TIMESTAMP '2024-01-01 00:00:00'"

    @pytest.mark.parametrize(
        "col_type",
        [
            "TIMESTAMPTZ",
        ],
    )
    def test_tz_aware_timestamp_renders_timestamptz_literal(self, col_type):
        # Bug-6618: tz-AWARE timestamp types stored as the short form
        # (TIMESTAMPTZ, TIMESTAMP_TZ) must render as ``TIMESTAMPTZ 'lit'`` so
        # sqlglot transpiles to ``CAST('x' AS TIMESTAMP)`` on BigQuery, not
        # ``CAST('x' AS DATETIME)`` which mismatches a tz-aware BQ column.
        rendered = _render_value("2024-01-01 00:00:00", col_type)
        assert rendered == "TIMESTAMPTZ '2024-01-01 00:00:00'"

    def test_render_condition_uses_timestamp_literal_for_verbose_spelling(self):
        sql = _render_condition(
            '"ts_col"', "eq", "2024-01-01 00:00:00",
            "timestamp without time zone",
        )
        assert sql == "\"ts_col\" = TIMESTAMP '2024-01-01 00:00:00'"

    def test_non_timestamp_string_still_plain_literal(self):
        # A text column keeps a plain quoted literal (no TIMESTAMP prefix).
        assert _render_value("hello", "text") == "'hello'"


class TestBug7918SourceDialectDecidesTzAwareness:
    """Bug-7918: the token ``TIMESTAMP`` names a DIFFERENT type per dialect, so
    tz-awareness is only decidable against the SOURCE dialect the column type
    was introspected from.

    BigQuery introspection persists ``field_type`` verbatim
    (``source_introspection.py`` -> ``"timestamp"``). That normalises to
    ``TIMESTAMP``, which the token-only check read as tz-NAIVE, emitting
    ``TIMESTAMP 'lit'`` -> BigQuery ``CAST('lit' AS DATETIME)`` against a
    tz-aware BigQuery TIMESTAMP column: "No matching signature for operator".
    """

    @pytest.mark.parametrize(
        "col_type, source_connector, expected",
        [
            # BigQuery: bare ``timestamp`` IS the absolute-instant type.
            ("timestamp", "bigquery", "TIMESTAMPTZ '2024-01-01 00:00:00'"),
            ("TIMESTAMP", "bigquery", "TIMESTAMPTZ '2024-01-01 00:00:00'"),
            ("TIMESTAMP(3)", "bigquery", "TIMESTAMPTZ '2024-01-01 00:00:00'"),
            # BigQuery ``datetime`` is the tz-NAIVE one — must NOT flip.
            ("datetime", "bigquery", "TIMESTAMP '2024-01-01 00:00:00'"),
            # PostgreSQL: bare ``timestamp`` is tz-naive; the verbose tz-aware
            # spelling must be recognised even though normalize_type_token
            # drops the qualifier words.
            ("timestamp", "postgresql", "TIMESTAMP '2024-01-01 00:00:00'"),
            (
                "timestamp with time zone",
                "postgresql",
                "TIMESTAMPTZ '2024-01-01 00:00:00'",
            ),
            (
                "timestamp without time zone",
                "postgresql",
                "TIMESTAMP '2024-01-01 00:00:00'",
            ),
            # Snowflake: bare ``timestamp`` defaults to TIMESTAMP_NTZ (naive).
            ("timestamp", "snowflake", "TIMESTAMP '2024-01-01 00:00:00'"),
            ("timestamp_tz", "snowflake", "TIMESTAMPTZ '2024-01-01 00:00:00'"),
            # SQL Server: ``timestamp`` is a ROWVERSION, never a tz-aware
            # instant — it must not be flipped by the BigQuery rule.
            ("timestamp", "sqlserver", "TIMESTAMP '2024-01-01 00:00:00'"),
            # Fails safe: unknown / absent source dialect keeps the
            # self-declaring-token behaviour that predates this fix.
            ("timestamp", None, "TIMESTAMP '2024-01-01 00:00:00'"),
            ("timestamptz", None, "TIMESTAMPTZ '2024-01-01 00:00:00'"),
            ("timestamp", "not-a-connector", "TIMESTAMP '2024-01-01 00:00:00'"),
        ],
    )
    def test_literal_tz_form_follows_the_source_dialect(
        self, col_type, source_connector, expected
    ):
        sql = _render_condition(
            '"ts_col"', "gte", "2024-01-01 00:00:00", col_type,
            source_connector=source_connector,
        )
        assert sql == f'"ts_col" >= {expected}'

    def test_bigquery_timestamp_column_transpiles_to_a_timestamp_cast(self):
        """The business outcome, not the intermediate spelling: the query
        BigQuery actually receives must compare TIMESTAMP to TIMESTAMP. The
        pre-fix rendering produced ``CAST(... AS DATETIME)``, which BigQuery
        rejects with "No matching signature for operator >=" — the filter never
        runs and the user gets an error instead of their rows."""
        fragment = _render_condition(
            '"ts_col"', "gte", "2024-01-01 00:00:00", "timestamp",
            source_connector="bigquery",
        )
        emitted = _transpile_to_dialect(f"SELECT 1 WHERE {fragment}", "bigquery")
        assert "CAST('2024-01-01 00:00:00' AS TIMESTAMP)" in emitted
        assert "DATETIME" not in emitted

    def test_bigquery_datetime_column_still_transpiles_to_a_datetime_cast(self):
        """Non-regression: a BigQuery DATETIME column is tz-naive and must keep
        the DATETIME cast, or the fix would break the column it did not target."""
        fragment = _render_condition(
            '"ts_col"', "gte", "2024-01-01 00:00:00", "datetime",
            source_connector="bigquery",
        )
        emitted = _transpile_to_dialect(f"SELECT 1 WHERE {fragment}", "bigquery")
        assert "CAST('2024-01-01 00:00:00' AS DATETIME)" in emitted

    def test_in_list_of_timestamps_uses_the_source_dialect_for_every_member(self):
        sql = _render_condition(
            '"ts_col"', "in",
            ["2024-01-01 00:00:00", "2024-02-01 00:00:00"],
            "timestamp", source_connector="bigquery",
        )
        assert sql == (
            '"ts_col" IN (TIMESTAMPTZ \'2024-01-01 00:00:00\', '
            'TIMESTAMPTZ \'2024-02-01 00:00:00\')'
        )

    def test_render_where_forwards_the_source_connector(self):
        """``_render_where`` is the choke point three of the four production
        WHERE builders go through; if it drops ``source_connector`` the fix is
        inert no matter how correct the renderer is."""
        rendered = _render_where(
            [LogicalFilter("event_ts", "gte", "2024-01-01 00:00:00")],
            {"event_ts": '"t"."event_ts"'},
            "postgresql",
            {"event_ts": "timestamp"},
            source_connector="bigquery",
        )
        assert rendered == "\"t\".\"event_ts\" >= TIMESTAMPTZ '2024-01-01 00:00:00'"

    def test_non_timestamp_types_pass_through_untouched(self):
        """The canonicaliser must not rewrite a type it was not asked about —
        a numeric column still renders a bare token, text still quotes."""
        assert _render_condition(
            '"n"', "eq", 19, "int64", source_connector="bigquery",
        ) == '"n" = 19'
        assert _render_condition(
            '"s"', "eq", "EMEA", "string", source_connector="bigquery",
        ) == "\"s\" = 'EMEA'"
        assert _render_condition(
            '"d"', "eq", "2024-01-01", "date", source_connector="bigquery",
        ) == "\"d\" = DATE '2024-01-01'"


class TestBug7918DialectSpecificTimestampTokens:
    """Bug-7918 guards source-reported timestamp variant spellings.

    SQL Server reports ``datetimeoffset`` / ``datetime2`` and Snowflake reports
    ``timestamp_ltz`` / ``timestamp_ntz``. Their normalized tokens must enter
    the shared timestamp family so the WHERE path emits a typed literal rather
    than a plain string. The assertion is on the rendered SQL literal, not on
    set membership or helper invocation.
    """

    @pytest.mark.parametrize(
        "source_connector,col_type,expected_literal",
        [
            (
                "sqlserver",
                "datetimeoffset",
                "TIMESTAMPTZ '2024-01-01 00:00:00'",
            ),
            (
                "sqlserver",
                "datetime2",
                "TIMESTAMP '2024-01-01 00:00:00'",
            ),
            (
                "snowflake",
                "timestamp_ltz",
                "TIMESTAMPTZ '2024-01-01 00:00:00'",
            ),
            (
                "snowflake",
                "timestamp_ntz",
                "TIMESTAMP '2024-01-01 00:00:00'",
            ),
        ],
    )
    def test_bug_7918_variant_tokens_render_aware_and_unaware_literals(
        self, source_connector, col_type, expected_literal
    ):
        rendered = _render_condition(
            '"ts_col"',
            "gte",
            "2024-01-01 00:00:00",
            col_type,
            source_connector=source_connector,
        )
        assert rendered == f'"ts_col" >= {expected_literal}'

        # Keep the direct value-rendering boundary pinned as well: these raw
        # source tokens must not regress to a plain quoted string before the
        # condition-level canonicalizer runs.
        assert _render_value(
            "2024-01-01 00:00:00", col_type.upper()
        ) == expected_literal


class TestBug7918EveryProductionCallSiteIsWired:
    """The ws2 branch's Bug-7918 attempt threaded the connector into
    ``_render_value`` but changed NO call site, so the fix could only ever fire
    from a unit test calling the renderer directly — the production WHERE
    builders all pass the PG-canonical ``"postgresql"``. This guard makes that
    failure mode impossible to reintroduce silently.

    Discovery scope: every ``.py`` file under ``src/``. It FAILS CLOSED — an
    unparseable file raises ``SyntaxError`` out of the test, and a call site
    without the keyword fails the assertion. Known blind spot, stated rather
    than hidden: a call made through an alias (``fn = _render_where; fn(...)``)
    or by splatting ``**kwargs`` is not matched by name. The minimum-count
    assertion is what catches a discovery collapse — if a refactor hides call
    sites from this scan, the count drops and the test goes red.
    """

    _RENDERERS = {"_render_where", "_render_condition"}

    def _call_sites(self):
        import ast

        src_root = Path(__file__).resolve().parents[1] / "src"
        sites = []
        for path in sorted(src_root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = (
                    func.id if isinstance(func, ast.Name)
                    else func.attr if isinstance(func, ast.Attribute)
                    else None
                )
                if name in self._RENDERERS:
                    sites.append((path.name, node.lineno, name, node))
        return sites

    def test_discovery_still_sees_every_known_call_site(self):
        sites = self._call_sites()
        # 5 production call sites at the time of writing: source_sql.py x3
        # (persona-star _render_condition + two _render_where), aggregate.py,
        # raw_sql.py; plus conditions.py's own internal _render_condition call.
        assert len(sites) >= 6, (
            "Bug-7918 call-site discovery collapsed — it found "
            f"{len(sites)} render call sites, so this guard is no longer "
            "proving anything. Fix the scan before trusting a green result."
        )

    def test_every_call_site_passes_source_connector(self):
        missing = [
            f"{filename}:{lineno} {name}()"
            for filename, lineno, name, node in self._call_sites()
            if "source_connector" not in {kw.arg for kw in node.keywords}
        ]
        assert not missing, (
            "Bug-7918: these WHERE-render call sites do not pass "
            "``source_connector``, so a source-native timestamp type is "
            "rendered against the wrong dialect there: " + ", ".join(missing)
        )


class TestTypeFamiliesShared:
    def test_condition_type_sets_are_the_shared_connector_qualify_sets(self):
        # Reconciliation guard: no local duplicate — the re-exported names are
        # the same frozensets the rest of the pipeline (source_sql.py) uses.
        assert _TIMESTAMP_TYPES is CQ_TIMESTAMP_TYPES
        assert _DATE_TYPES is CQ_DATE_TYPES

    def test_double_precision_recognised_as_numeric(self):
        # Multi-word PostgreSQL spelling now normalises to DOUBLE and is
        # recognised (the old ``.split("(")`` kept "DOUBLE PRECISION").
        assert is_numeric_col_type("double precision") is True
        assert is_numeric_col_type("NUMERIC(10,2)") is True
