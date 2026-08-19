"""Offline unit tests for model parameter resolution (catalog shapes #106/#107).

Covers `src/params/resolver.py`:
  - `substitute_parameters` for each value type (string/number/boolean/
    multi_value/date_range).
  - `_coerce` type coercion and its error cases.
  - `resolve_parameters` precedence (persona > session var > default) and the
    required-parameter-missing rejection (#107).

All tests are deterministic and require no live database; `resolve_parameters`
is exercised against a minimal in-memory fake async session.
"""
from __future__ import annotations

import pytest

from src.params.resolver import (
    _coerce,
    apply_parameters,
    resolve_parameters,
    substitute_parameters,
)


# ---------------------------------------------------------------------------
# substitute_parameters — value rendering per type (#106)
# ---------------------------------------------------------------------------

def test_substitute_string_is_quoted():
    sql = "SELECT * FROM t WHERE region = @region"
    out = substitute_parameters(sql, {"@region": "EMEA"})
    assert out == "SELECT * FROM t WHERE region = 'EMEA'"


def test_substitute_string_escapes_single_quote():
    out = substitute_parameters("WHERE name = @name", {"@name": "O'Brien"})
    assert out == "WHERE name = 'O''Brien'"


def test_substitute_number_is_bare_literal():
    out = substitute_parameters("WHERE amount > @min", {"@min": 100})
    assert out == "WHERE amount > 100"


def test_substitute_boolean_renders_true_false():
    assert substitute_parameters("WHERE flag = @f", {"@f": True}) == "WHERE flag = TRUE"
    assert substitute_parameters("WHERE flag = @f", {"@f": False}) == "WHERE flag = FALSE"


def test_substitute_multi_value_expands_quoted_list():
    out = substitute_parameters(
        "WHERE region IN (@regions)", {"@regions": ["EMEA", "APAC"]}
    )
    assert out == "WHERE region IN ('EMEA', 'APAC')"


def test_substitute_date_range_renders_between_bounds():
    out = substitute_parameters(
        "WHERE d BETWEEN @range",
        {"@range": {"from": "2026-01-01", "to": "2026-12-31"}},
    )
    assert out == "WHERE d BETWEEN '2026-01-01' AND '2026-12-31'"


def test_substitute_unknown_token_left_untouched():
    out = substitute_parameters("WHERE x = @unknown", {"@known": "v"})
    assert out == "WHERE x = @unknown"


# ---------------------------------------------------------------------------
# AST-level binding — a ``@name`` lexeme inside a string literal / identifier
# is NOT a placeholder and must never be substituted (Bug-1105 / F-029-05).
# Binding addresses only spans the sqlglot lexer classifies as ``PARAMETER``;
# a ``@name`` inside a STRING token is invisible to it.
# ---------------------------------------------------------------------------

def test_substitute_param_name_inside_string_literal_is_not_bound():
    # The author's literal happens to contain the declared parameter's name.
    # Regex text replacement would corrupt it to ``'a'INTERNAL'.com'`` and the
    # SQL would fail to parse (the original Bug-1105 live failure). AST binding
    # leaves the literal exactly as written.
    sql = "SELECT count(*) FROM modelx WHERE customer_segment = 'a@segment.com'"
    out = substitute_parameters(sql, {"@segment": "INTERNAL"})
    assert out == sql


def test_substitute_binds_real_placeholder_but_not_literal_lookalike():
    # Same name appears both inside a literal (must stay) and as a real
    # placeholder (must bind).
    sql = "WHERE email = 'a@segment.com' AND seg = @segment"
    out = substitute_parameters(sql, {"@segment": "RETAIL"})
    assert out == "WHERE email = 'a@segment.com' AND seg = 'RETAIL'"


def test_substitute_lone_at_name_string_literal_not_bound():
    # A literal that is exactly ``'@segment'`` must survive untouched.
    sql = "WHERE handle = '@segment' AND seg = @segment"
    out = substitute_parameters(sql, {"@segment": "RETAIL"})
    assert out == "WHERE handle = '@segment' AND seg = 'RETAIL'"


# ---------------------------------------------------------------------------
# _coerce — type coercion and error cases (#106)
# ---------------------------------------------------------------------------

def test_coerce_string():
    assert _coerce("string", 42, "@p") == "42"


def test_coerce_number_int_and_float():
    assert _coerce("number", "100", "@p") == 100
    assert _coerce("number", "3.14", "@p") == 3.14


def test_coerce_number_invalid_raises():
    with pytest.raises(ValueError, match="expects a number"):
        _coerce("number", "abc", "@p")


def test_coerce_boolean_from_string():
    assert _coerce("boolean", "true", "@p") is True
    assert _coerce("boolean", "no", "@p") is False
    assert _coerce("boolean", True, "@p") is True


def test_coerce_boolean_accepts_full_closed_token_set():
    for token in ("true", "1", "yes", "TRUE", "Yes", " true "):
        assert _coerce("boolean", token, "@p") is True
    for token in ("false", "0", "no", "FALSE", "No", " false "):
        assert _coerce("boolean", token, "@p") is False


def test_coerce_boolean_rejects_unrecognised_string():
    """Bug-5890: a typo or unrecognised value must raise, not silently
    become False."""
    with pytest.raises(ValueError, match="expects a boolean"):
        _coerce("boolean", "maybe", "@p")
    with pytest.raises(ValueError, match="expects a boolean"):
        _coerce("boolean", "random", "@p")
    with pytest.raises(ValueError, match="expects a boolean"):
        _coerce("boolean", "", "@p")


def test_coerce_multi_value_splits_csv():
    assert _coerce("multi_value", "a, b ,c", "@p") == ["a", "b", "c"]
    assert _coerce("multi_value", ["x", "y"], "@p") == ["x", "y"]


def test_coerce_date_range_requires_from_to():
    rng = {"from": "2026-01-01", "to": "2026-02-01"}
    assert _coerce("date_range", rng, "@p") == rng


def test_coerce_date_range_invalid_raises():
    # A bare string with no recognised separator is not a date_range.
    with pytest.raises(ValueError, match="expects a date_range"):
        _coerce("date_range", "not-a-range", "@p")


def test_coerce_date_range_from_comma_string():
    # Bug-6413: a JDBC session variable arrives as a string; a two-date pair
    # separated by a comma must resolve to the {from,to} object.
    assert _coerce("date_range", "2024-01-01,2024-12-31", "@p") == {
        "from": "2024-01-01",
        "to": "2024-12-31",
    }


def test_coerce_date_range_from_dotdot_string():
    assert _coerce("date_range", "2024-01-01..2024-12-31", "@p") == {
        "from": "2024-01-01",
        "to": "2024-12-31",
    }


def test_coerce_date_range_from_json_string():
    assert _coerce(
        "date_range", '{"from": "2024-01-01", "to": "2024-12-31"}', "@p"
    ) == {"from": "2024-01-01", "to": "2024-12-31"}


def test_coerce_date_range_single_date_string_rejected():
    # A single date is not a range (no second bound) -> fail clearly.
    with pytest.raises(ValueError, match="expects a date_range"):
        _coerce("date_range", "2024-01-01", "@p")


def test_coerce_date_range_three_parts_rejected():
    with pytest.raises(ValueError, match="expects a date_range"):
        _coerce("date_range", "2024-01-01,2024-06-01,2024-12-31", "@p")


# F-029-02: date_range bounds must be ISO-8601 dates, ordered, and only from/to.


def test_coerce_date_range_non_iso_string_bounds_rejected():
    # 'banana,pear' has the right SHAPE but non-date bounds; without ISO
    # validation this became BETWEEN 'banana' AND 'pear' — a source error or
    # silently-wrong empty result presented as success.
    with pytest.raises(ValueError, match="not a valid ISO-8601 date"):
        _coerce("date_range", "banana,pear", "@p")


def test_coerce_date_range_non_string_bounds_rejected():
    with pytest.raises(ValueError, match="must be an ISO-8601 date string"):
        _coerce("date_range", {"from": 1, "to": 2}, "@p")


def test_coerce_date_range_inverted_rejected():
    with pytest.raises(ValueError, match="inverted"):
        _coerce("date_range", {"from": "2024-12-31", "to": "2024-01-01"}, "@p")


def test_coerce_date_range_extra_keys_rejected():
    with pytest.raises(ValueError, match="unexpected key"):
        _coerce(
            "date_range",
            {"from": "2024-01-01", "to": "2024-12-31", "tz": "UTC"},
            "@p",
        )


def test_coerce_date_range_missing_bound_rejected():
    with pytest.raises(ValueError, match="requires both 'from' and 'to'"):
        _coerce("date_range", {"from": "2024-01-01"}, "@p")


def test_coerce_date_range_accepts_iso_datetime_bounds():
    rng = {"from": "2024-01-01T00:00:00", "to": "2024-12-31T23:59:59"}
    assert _coerce("date_range", rng, "@p") == rng


# F-029-16 VERIFY (not a fix): a parameterized equality/IN filter must still be
# aggregate-matchable. Binding runs BEFORE parse (routes.py), so the matcher
# never sees a placeholder — it sees a plain literal predicate. This asserts the
# bound SQL is byte-identical to the equivalent hand-written literal query, which
# is what the matcher would receive; the matcher's route decision is a pure
# function of that SQL, so identical input => identical route. No matcher change
# is warranted (the sensitive-component guard holds).


def test_f029_16_parameterized_equality_is_byte_identical_to_literal():
    param_sql = "SELECT region, SUM(sales) FROM t WHERE region = @region GROUP BY region"
    literal_sql = "SELECT region, SUM(sales) FROM t WHERE region = 'EMEA' GROUP BY region"
    assert substitute_parameters(param_sql, {"@region": "EMEA"}) == literal_sql


def test_f029_16_parameterized_in_list_is_byte_identical_to_literal():
    param_sql = "SELECT region, SUM(sales) FROM t WHERE region IN (@regions) GROUP BY region"
    literal_sql = (
        "SELECT region, SUM(sales) FROM t WHERE region IN ('EMEA', 'APAC') GROUP BY region"
    )
    assert (
        substitute_parameters(param_sql, {"@regions": ["EMEA", "APAC"]}) == literal_sql
    )


# ---------------------------------------------------------------------------
# resolve_parameters — precedence and required-missing (#106/#107)
# ---------------------------------------------------------------------------

class _FakeParam:
    def __init__(self, name, param_type, default_value=None, allowed_values=None):
        self.name = name
        self.param_type = param_type
        self.default_value = default_value
        self.allowed_values = allowed_values


class _FakeScalars:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return _FakeScalars(self._rows)


class _FakeDB:
    """Minimal async session stub: returns the configured parameter rows
    regardless of the query, mirroring the single SELECT in resolve_parameters."""

    def __init__(self, rows):
        self._rows = rows

    async def execute(self, _stmt):
        return _FakeResult(self._rows)


@pytest.mark.asyncio
async def test_resolve_persona_beats_session_and_default():
    db = _FakeDB([_FakeParam("@region", "string", default_value="GLOBAL")])
    resolved = await resolve_parameters(
        "m1",
        session_vars={"app.region": "APAC"},
        persona_filters={"@region": "EMEA"},
        db=db,
    )
    assert resolved == {"@region": "EMEA"}


@pytest.mark.asyncio
async def test_resolve_session_var_beats_default():
    db = _FakeDB([_FakeParam("@region", "string", default_value="GLOBAL")])
    resolved = await resolve_parameters(
        "m1",
        session_vars={"app.region": "APAC"},
        persona_filters={},
        db=db,
    )
    assert resolved == {"@region": "APAC"}


@pytest.mark.asyncio
async def test_resolve_falls_back_to_default():
    db = _FakeDB([_FakeParam("@region", "string", default_value="GLOBAL")])
    resolved = await resolve_parameters(
        "m1", session_vars={}, persona_filters={}, db=db
    )
    assert resolved == {"@region": "GLOBAL"}


@pytest.mark.asyncio
async def test_resolve_session_var_matches_case_insensitively():
    # Bug-6411: a mixed-case model parameter (@RegionCode) must be settable
    # over JDBC. The gateway folds ``SET app.RegionCode`` to ``app.regioncode``
    # (Postgres GUC semantics); before the fix the exact-case lookup
    # ``app.RegionCode`` never matched and the value silently fell back to the
    # default.
    db = _FakeDB([_FakeParam("@RegionCode", "string", default_value="GLOBAL")])
    resolved = await resolve_parameters(
        "m1",
        session_vars={"app.regioncode": "APAC"},
        persona_filters={},
        db=db,
    )
    assert resolved == {"@RegionCode": "APAC"}


@pytest.mark.asyncio
async def test_resolve_date_range_from_session_var_string():
    # Bug-6413: a date_range parameter set over JDBC arrives as a string and
    # must resolve to the {from,to} object.
    db = _FakeDB([_FakeParam("@window", "date_range", default_value=None)])
    resolved = await resolve_parameters(
        "m1",
        session_vars={"app.window": "2024-01-01,2024-12-31"},
        persona_filters={},
        db=db,
    )
    assert resolved == {"@window": {"from": "2024-01-01", "to": "2024-12-31"}}


@pytest.mark.asyncio
async def test_resolve_coerces_declared_type():
    db = _FakeDB([_FakeParam("@min", "number", default_value="100")])
    resolved = await resolve_parameters(
        "m1", session_vars={}, persona_filters={}, db=db
    )
    assert resolved == {"@min": 100}


@pytest.mark.asyncio
async def test_resolve_required_missing_raises():
    # Shape #107: required parameter with no persona / session / default value.
    db = _FakeDB([_FakeParam("@region", "string", default_value=None)])
    with pytest.raises(ValueError, match="Required parameter '@region'"):
        await resolve_parameters("m1", session_vars={}, persona_filters={}, db=db)


# ---------------------------------------------------------------------------
# Type-safe binding — metacharacter / injection proofs (F-029-01)
# A value containing quotes or semicolons must bind as a single literal and
# never alter the query structure (no new statement, no clause break-out).
# ---------------------------------------------------------------------------

def test_substitute_semicolon_value_stays_inside_literal():
    out = substitute_parameters(
        "WHERE region = @region",
        {"@region": "EMEA'; DROP TABLE sales; --"},
    )
    # The whole payload is contained in one single-quoted literal with the
    # embedded quote doubled. No bare semicolon escapes the literal.
    assert out == "WHERE region = 'EMEA''; DROP TABLE sales; --'"


def test_substitute_value_cannot_break_out_of_literal():
    # Classic break-out attempt: close the literal, OR 1=1.
    out = substitute_parameters(
        "WHERE region = @region",
        {"@region": "x' OR '1'='1"},
    )
    assert out == "WHERE region = 'x'' OR ''1''=''1'"
    # Exactly one opening and one closing structure: count of quote chars is
    # even and every interior quote is doubled.
    assert out.count("'") % 2 == 0


def test_substitute_multi_value_each_element_is_literal():
    out = substitute_parameters(
        "WHERE region IN (@regions)",
        {"@regions": ["EMEA", "x') OR (1=1"]},
    )
    assert out == "WHERE region IN ('EMEA', 'x'') OR (1=1')"


def test_substitute_number_metachars_rejected_at_coerce():
    # A number param can never carry SQL text: coercion fails first.
    with pytest.raises(ValueError, match="expects a number"):
        _coerce("number", "1; DROP TABLE t", "@min")


# ---------------------------------------------------------------------------
# apply_parameters — single pipeline entry point (F-029-01 wiring)
# ---------------------------------------------------------------------------

class _FakeFirstResult:
    def __init__(self, rows):
        self._rows = rows

    def first(self):
        return self._rows[0] if self._rows else None

    def scalars(self):
        return _FakeScalars(self._rows)


class _FakeApplyDB:
    """Returns the configured rows for both the existence ``limit(1)`` probe
    and the full SELECT inside resolve_parameters."""

    def __init__(self, rows):
        self._rows = rows

    async def execute(self, _stmt):
        return _FakeFirstResult(self._rows)


@pytest.mark.asyncio
async def test_apply_parameters_short_circuits_when_no_params():
    db = _FakeApplyDB([])
    sql = "SELECT * FROM t WHERE region = @region"
    out = await apply_parameters(
        model_id="m1", sql=sql, session_vars={"app.region": "EMEA"},
        persona_filters={}, db=db,
    )
    # No declared parameters -> SQL is returned untouched, @token preserved.
    assert out == sql


@pytest.mark.asyncio
async def test_apply_parameters_binds_session_var():
    db = _FakeApplyDB([_FakeParam("@region", "string", default_value="GLOBAL")])
    out = await apply_parameters(
        model_id="m1", sql="WHERE region = @region",
        session_vars={"app.region": "EMEA"}, persona_filters={}, db=db,
    )
    assert out == "WHERE region = 'EMEA'"


@pytest.mark.asyncio
async def test_apply_parameters_persona_beats_session_var():
    db = _FakeApplyDB([_FakeParam("@region", "string", default_value="GLOBAL")])
    out = await apply_parameters(
        model_id="m1", sql="WHERE region = @region",
        session_vars={"app.region": "APAC"},
        persona_filters={"@region": "EMEA"}, db=db,
    )
    # Persona default beats JDBC session var beats model default.
    assert out == "WHERE region = 'EMEA'"


@pytest.mark.asyncio
async def test_apply_parameters_session_var_metachars_safe():
    db = _FakeApplyDB([_FakeParam("@region", "string", default_value="GLOBAL")])
    out = await apply_parameters(
        model_id="m1", sql="WHERE region = @region",
        session_vars={"app.region": "EMEA'; DROP TABLE sales; --"},
        persona_filters={}, db=db,
    )
    assert out == "WHERE region = 'EMEA''; DROP TABLE sales; --'"


# ---------------------------------------------------------------------------
# Bug-5313: ``@`` inside a string literal must NOT trigger resolution of
# required parameters — only REAL placeholder spans should be resolved.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_parameters_skips_resolution_when_at_only_in_literal():
    """Bug-5313: a query containing ``@`` only inside a string literal
    (e.g. ``'user@company.com'``) must NOT resolve required parameters.
    Before the fix, the ``@`` fast-path matched and ``resolve_parameters``
    raised ParameterError for a required param with no default even though
    there was no real placeholder to substitute."""
    # Declare a required parameter with no default.
    db = _FakeApplyDB([_FakeParam("@region", "string", default_value=None)])
    sql = "SELECT * FROM t WHERE email = 'user@region.com'"
    # Before fix: raises ParameterError("Required parameter '@region'...")
    # After fix: returns the SQL untouched.
    out = await apply_parameters(
        model_id="m1", sql=sql, session_vars={}, persona_filters={}, db=db,
    )
    assert out == sql


@pytest.mark.asyncio
async def test_apply_parameters_unreferenced_required_param_does_not_block():
    """Bug-6410: a required parameter (no default / session / persona value)
    that the query does not reference must NOT block an otherwise-valid
    parameterised query. Only the referenced ``@seg`` is bound; the unused
    required ``@region`` is skipped rather than raising ParameterError."""
    db = _FakeApplyDB([
        _FakeParam("@region", "string", default_value=None),   # required, unused
        _FakeParam("@seg", "string", default_value="RETAIL"),  # referenced
    ])
    sql = "WHERE seg = @seg"
    out = await apply_parameters(
        model_id="m1", sql=sql, session_vars={}, persona_filters={}, db=db,
    )
    assert out == "WHERE seg = 'RETAIL'"


@pytest.mark.asyncio
async def test_apply_parameters_referenced_required_param_still_raises():
    """Guard against over-correction: a required parameter the query DOES
    reference must still fail loudly when it cannot be resolved."""
    db = _FakeApplyDB([_FakeParam("@region", "string", default_value=None)])
    with pytest.raises(ParameterError, match="Required parameter '@region'"):
        await apply_parameters(
            model_id="m1", sql="WHERE region = @region",
            session_vars={}, persona_filters={}, db=db,
        )


@pytest.mark.asyncio
async def test_apply_parameters_resolves_real_placeholder_with_literal_at():
    """Bug-5313 regression: when the SQL has BOTH a real @param placeholder
    AND an ``@`` inside a string literal, the real placeholder must still
    be resolved and bound."""
    db = _FakeApplyDB([_FakeParam("@region", "string", default_value="EMEA")])
    sql = "WHERE email = 'a@region.com' AND seg = @region"
    out = await apply_parameters(
        model_id="m1", sql=sql, session_vars={}, persona_filters={}, db=db,
    )
    assert out == "WHERE email = 'a@region.com' AND seg = 'EMEA'"


# ---------------------------------------------------------------------------
# allowed_values governance enforcement (F-029-02)
# A resolved value must be a member of the modeler's declared allowed list;
# multi_value validates every element. Fail-closed with ParameterError.
# ---------------------------------------------------------------------------

from src.params.resolver import ParameterError  # noqa: E402


@pytest.mark.asyncio
async def test_allowed_values_accepts_member_string():
    db = _FakeDB([
        _FakeParam("@region", "string", default_value=None,
                   allowed_values=["EMEA", "APAC"])
    ])
    resolved = await resolve_parameters(
        "m1", session_vars={"app.region": "EMEA"}, persona_filters={}, db=db
    )
    assert resolved == {"@region": "EMEA"}


@pytest.mark.asyncio
async def test_allowed_values_rejects_non_member_string():
    db = _FakeDB([
        _FakeParam("@region", "string", default_value=None,
                   allowed_values=["EMEA", "APAC"])
    ])
    with pytest.raises(ParameterError, match="not one of the allowed values"):
        await resolve_parameters(
            "m1", session_vars={"app.region": "ANY-VALUE"},
            persona_filters={}, db=db,
        )


@pytest.mark.asyncio
async def test_allowed_values_rejects_via_session_var_injection():
    # The exact F-029-02 attack: a JDBC client SETs an unlisted value.
    db = _FakeDB([
        _FakeParam("@region", "string", default_value="EMEA",
                   allowed_values=["EMEA", "APAC"])
    ])
    with pytest.raises(ParameterError):
        await resolve_parameters(
            "m1", session_vars={"app.region": "EVIL"},
            persona_filters={}, db=db,
        )


@pytest.mark.asyncio
async def test_allowed_values_number_normalises_string_list():
    # Declared list ["10","20"] (JSON text) must match a coerced numeric 10.
    db = _FakeDB([
        _FakeParam("@n", "number", default_value="10",
                   allowed_values=["10", "20"])
    ])
    resolved = await resolve_parameters(
        "m1", session_vars={}, persona_filters={}, db=db
    )
    assert resolved == {"@n": 10}


@pytest.mark.asyncio
async def test_allowed_values_number_rejects_out_of_set():
    db = _FakeDB([
        _FakeParam("@n", "number", default_value=None,
                   allowed_values=[10, 20])
    ])
    with pytest.raises(ParameterError):
        await resolve_parameters(
            "m1", session_vars={"app.n": "30"}, persona_filters={}, db=db
        )


@pytest.mark.asyncio
async def test_allowed_values_multi_value_validates_every_element():
    db = _FakeDB([
        _FakeParam("@regions", "multi_value", default_value=None,
                   allowed_values=["EMEA", "APAC", "AMER"])
    ])
    # All members -> ok.
    resolved = await resolve_parameters(
        "m1", session_vars={"app.regions": "EMEA,APAC"},
        persona_filters={}, db=db,
    )
    assert resolved == {"@regions": ["EMEA", "APAC"]}
    # One bad element -> reject the whole resolution.
    with pytest.raises(ParameterError):
        await resolve_parameters(
            "m1", session_vars={"app.regions": "EMEA,ROGUE"},
            persona_filters={}, db=db,
        )


@pytest.mark.asyncio
async def test_allowed_values_empty_list_imposes_no_restriction():
    db = _FakeDB([
        _FakeParam("@region", "string", default_value=None, allowed_values=[])
    ])
    resolved = await resolve_parameters(
        "m1", session_vars={"app.region": "anything"},
        persona_filters={}, db=db,
    )
    assert resolved == {"@region": "anything"}


@pytest.mark.asyncio
async def test_allowed_values_persona_filter_also_enforced():
    db = _FakeDB([
        _FakeParam("@region", "string", default_value=None,
                   allowed_values=["EMEA"])
    ])
    with pytest.raises(ParameterError):
        await resolve_parameters(
            "m1", session_vars={}, persona_filters={"@region": "APAC"}, db=db
        )


# ---------------------------------------------------------------------------
# Bug-7660: persona parameter override key shape contract
# The resolver expects persona_filters keyed by ``@name`` (matching the
# declared parameter name). The Persona ORM ``default_filters`` are keyed
# by bare dimension name (e.g. ``"Region"``). The route-level
# ``_bind_query_parameters`` must re-key to ``@``-prefixed form before
# passing into the resolver; a bare key never matches a ``@``-prefixed
# parameter.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persona_override_bare_key_does_not_match():
    """Bug-7660 contract: the resolver matches persona filters by ``@name``
    only. A bare-name key (the shape stored in Persona.default_filters)
    does NOT match, so the caller must re-key. This test documents the
    contract: without re-keying, the persona override is silently ignored
    and the model default is used instead."""
    db = _FakeDB([_FakeParam("@region", "string", default_value="GLOBAL")])
    resolved = await resolve_parameters(
        "m1",
        session_vars={},
        persona_filters={"region": "EMEA"},  # bare key, no @
        db=db,
    )
    # The bare key does NOT match @region -> falls back to default.
    assert resolved == {"@region": "GLOBAL"}


@pytest.mark.asyncio
async def test_persona_override_at_prefixed_key_matches():
    """Bug-7660 regression: after the route-level re-key, the resolver
    receives ``@``-prefixed keys and resolves persona overrides correctly."""
    db = _FakeDB([_FakeParam("@region", "string", default_value="GLOBAL")])
    resolved = await resolve_parameters(
        "m1",
        session_vars={},
        persona_filters={"@region": "EMEA"},  # correctly @-prefixed
        db=db,
    )
    assert resolved == {"@region": "EMEA"}


# ---------------------------------------------------------------------------
# Bug-7663: case-insensitive placeholder matching + unknown placeholder error
# Placeholder names in the query may differ in casing from the declared
# parameter name (e.g. ``@regioncode`` vs ``@RegionCode``). Matching must
# be case-insensitive, consistent with session-variable handling (Bug-6411).
# Unknown placeholders must raise a clear ParameterError instead of silently
# surviving into the parser.
# ---------------------------------------------------------------------------


def test_substitute_case_insensitive_binding():
    """Bug-7663: ``@regioncode`` in the query binds to resolved ``@RegionCode``."""
    sql = "SELECT * FROM t WHERE region = @regioncode"
    out = substitute_parameters(sql, {"@RegionCode": "EMEA"})
    assert out == "SELECT * FROM t WHERE region = 'EMEA'"


def test_substitute_mixed_case_multiple_placeholders():
    """Bug-7663: multiple placeholders with case variation all bind correctly."""
    sql = "WHERE region = @REGION AND seg = @Seg"
    out = substitute_parameters(sql, {"@region": "EMEA", "@seg": "RETAIL"})
    assert out == "WHERE region = 'EMEA' AND seg = 'RETAIL'"


def test_substitute_unknown_placeholder_raises_when_declared_names_provided():
    """Bug-7663: an unknown placeholder raises a clear ParameterError when
    declared_names is provided, instead of silently surviving into the parser."""
    with pytest.raises(ParameterError, match="Unknown parameter placeholder"):
        substitute_parameters(
            "WHERE x = @typo",
            {"@known": "v"},
            declared_names={"@known"},
        )


def test_substitute_unknown_placeholder_lists_declared_names():
    """Bug-7663: the error message lists the declared parameters."""
    with pytest.raises(ParameterError, match="@region"):
        substitute_parameters(
            "WHERE x = @regiom",
            {"@region": "EMEA"},
            declared_names={"@region"},
        )


def test_substitute_unknown_placeholder_still_silent_without_declared_names():
    """Backward compatibility: without declared_names, unknown tokens are
    left untouched (original behavior for direct callers)."""
    out = substitute_parameters("WHERE x = @unknown", {"@known": "v"})
    assert out == "WHERE x = @unknown"


@pytest.mark.asyncio
async def test_resolve_case_insensitive_referenced_names():
    """Bug-7663: referenced_names from query spans use query casing; the
    resolver must match case-insensitively against declared param names."""
    db = _FakeDB([_FakeParam("@RegionCode", "string", default_value="GLOBAL")])
    # The query uses ``@regioncode`` (all lower), declared is ``@RegionCode``.
    resolved = await resolve_parameters(
        "m1",
        session_vars={},
        persona_filters={},
        db=db,
        referenced_names={"@regioncode"},  # query casing
    )
    assert resolved == {"@RegionCode": "GLOBAL"}


@pytest.mark.asyncio
async def test_resolve_persona_filter_case_insensitive():
    """Bug-7663: persona filters keyed with different casing still match."""
    db = _FakeDB([_FakeParam("@RegionCode", "string", default_value="GLOBAL")])
    resolved = await resolve_parameters(
        "m1",
        session_vars={},
        persona_filters={"@regioncode": "EMEA"},
        db=db,
    )
    assert resolved == {"@RegionCode": "EMEA"}


@pytest.mark.asyncio
async def test_apply_parameters_case_insensitive_binding():
    """Bug-7663 end-to-end: ``@regioncode`` in query binds to declared
    ``@RegionCode`` through the full apply_parameters pipeline."""
    db = _FakeApplyDB([_FakeParam("@RegionCode", "string", default_value="GLOBAL")])
    out = await apply_parameters(
        model_id="m1", sql="WHERE region = @regioncode",
        session_vars={}, persona_filters={}, db=db,
    )
    assert out == "WHERE region = 'GLOBAL'"


@pytest.mark.asyncio
async def test_apply_parameters_unknown_placeholder_raises():
    """Bug-7663: an unrecognised placeholder in the pipeline raises a clear
    ParameterError instead of reaching the parser as a raw ``@token``."""
    db = _FakeApplyDB([_FakeParam("@region", "string", default_value="EMEA")])
    with pytest.raises(ParameterError, match="Unknown parameter placeholder"):
        await apply_parameters(
            model_id="m1", sql="WHERE x = @regiom AND y = @region",
            session_vars={}, persona_filters={}, db=db,
        )


# ---------------------------------------------------------------------------
# Bug-8068 — lossless JDBC multi-value encoding.
#
# Wrong-rows guard. Every JDBC session variable arrives as a string, and the
# only multi-value encoding was "split on every comma", so a filter on the real
# member "New York, NY" silently became the two members "New York" and "NY".
# A JSON-array form now round-trips any member; the legacy comma list is
# untouched for values that contain no commas.
# ---------------------------------------------------------------------------

from src.params.resolver import _render  # noqa: E402


def test_multi_value_json_array_preserves_embedded_commas():
    """The reported defect: one business value must stay ONE filter member."""
    assert _coerce("multi_value", '["New York, NY","Paris"]', "@cities") == [
        "New York, NY", "Paris",
    ]
    # And it must render as two literals, not three.
    assert _render(["New York, NY", "Paris"], "postgres") == "'New York, NY', 'Paris'"


def test_multi_value_legacy_comma_form_is_unchanged():
    """Backward compatibility: a non-JSON string still comma-splits exactly as
    before, so existing SET app.<name> session variables keep working."""
    assert _coerce("multi_value", "EMEA,APAC", "@r") == ["EMEA", "APAC"]
    assert _coerce("multi_value", "a, b ,c", "@r") == ["a", "b", "c"]


def test_multi_value_json_array_round_trips_awkward_members():
    """Quotes, whitespace, Unicode and an empty string must survive intact —
    these are the members a delimiter convention cannot represent."""
    raw = '["O\'Brien & Co, Ltd", "  padded  ", "Zürich", "", "a\\"b"]'
    assert _coerce("multi_value", raw, "@m") == [
        "O'Brien & Co, Ltd", "  padded  ", "Zürich", "", 'a"b',
    ]


def test_multi_value_json_numbers_normalise_to_strings():
    """Both wire forms must resolve identically: ["10"] and [10] filter the
    same, matching the legacy split's string element type and the string
    element type allowed_values enforcement uses."""
    assert _coerce("multi_value", '[10, 20]', "@ids") == ["10", "20"]
    assert _coerce("multi_value", "10,20", "@ids") == ["10", "20"]


def test_multi_value_rejects_non_scalar_members():
    """A nested array/object/bool/null is not a filter member. Passing one
    through rendered str(obj) as a SQL literal — a member matching nothing."""
    for raw in ('[{"a": 1}]', '[["x"]]', "[true]", "[null]"):
        with pytest.raises(ParameterError, match="must be strings or numbers"):
            _coerce("multi_value", raw, "@m")
    # Same validation on the persona/list channel, not only the JDBC string one.
    with pytest.raises(ParameterError, match="must be strings or numbers"):
        _coerce("multi_value", [{"a": 1}], "@m")


def test_multi_value_malformed_json_array_fails_loudly():
    """Any array delimiter commits the wire value to strict JSON parsing.

    A truncated opening or closing delimiter must not fall back to the legacy
    comma splitter and silently turn one embedded-comma member into two.
    """
    for raw in (
        '["New York, NY", Paris]',
        '["New York, NY", "Paris"',
        '"New York, NY", "Paris"]',
    ):
        with pytest.raises(ParameterError, match="not valid JSON"):
            _coerce("multi_value", raw, "@m")


@pytest.mark.asyncio
async def test_jdbc_partial_json_array_is_rejected_end_to_end():
    """Malformed structured session input must fail at parameter resolution
    instead of producing corrupted SQL members."""
    db = _FakeApplyDB([_FakeParam("@cities", "multi_value", default_value=None)])

    with pytest.raises(ParameterError, match="not valid JSON"):
        await apply_parameters(
            model_id="m1", sql="WHERE city IN (@cities)",
            session_vars={"app.cities": '["New York, NY", "Paris"'},
            persona_filters={}, db=db,
        )


def test_multi_value_empty_is_rejected_at_both_boundaries():
    """Bug-7665: an empty multi_value used to render ``IN ()`` — a syntax error
    surfaced as an unrelated database message."""
    with pytest.raises(ParameterError, match="at least one value"):
        _coerce("multi_value", "[]", "@m")
    with pytest.raises(ParameterError, match="at least one value"):
        _coerce("multi_value", [], "@m")
    with pytest.raises(ParameterError, match="no valid SQL"):
        _render([], "postgres")


def test_boolean_rejects_non_string_non_bool():
    """Bug-7439: the fallback used Python truthiness, so 2 -> True and [] ->
    False. A malformed persona default then silently inverted a row filter."""
    for bad in (2, 0, [], {}, None, 1.5):
        with pytest.raises(ParameterError, match="expects a boolean"):
            _coerce("boolean", bad, "@flag")
    # Real booleans and the documented string tokens still work.
    assert _coerce("boolean", True, "@flag") is True
    assert _coerce("boolean", "no", "@flag") is False


@pytest.mark.asyncio
async def test_jdbc_session_var_json_array_binds_as_one_member_end_to_end():
    """End-to-end through apply_parameters on the real JDBC channel: the
    gateway forwards ``SET app.cities = '["New York, NY","Paris"]'`` verbatim as
    a string, and the bound SQL must contain exactly two literals."""
    db = _FakeApplyDB([_FakeParam("@cities", "multi_value", default_value=None)])
    out = await apply_parameters(
        model_id="m1", sql="WHERE city IN (@cities)",
        session_vars={"app.cities": '["New York, NY","Paris"]'},
        persona_filters={}, db=db,
    )
    assert out == "WHERE city IN ('New York, NY', 'Paris')"


@pytest.mark.asyncio
async def test_jdbc_session_var_legacy_comma_form_still_binds():
    db = _FakeApplyDB([_FakeParam("@cities", "multi_value", default_value=None)])
    out = await apply_parameters(
        model_id="m1", sql="WHERE city IN (@cities)",
        session_vars={"app.cities": "Paris,Berlin"},
        persona_filters={}, db=db,
    )
    assert out == "WHERE city IN ('Paris', 'Berlin')"
