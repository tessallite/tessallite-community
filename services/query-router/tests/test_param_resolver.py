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


def test_coerce_multi_value_splits_csv():
    assert _coerce("multi_value", "a, b ,c", "@p") == ["a", "b", "c"]
    assert _coerce("multi_value", ["x", "y"], "@p") == ["x", "y"]


def test_coerce_date_range_requires_from_to():
    rng = {"from": "2026-01-01", "to": "2026-02-01"}
    assert _coerce("date_range", rng, "@p") == rng


def test_coerce_date_range_invalid_raises():
    with pytest.raises(ValueError, match="date_range object"):
        _coerce("date_range", "not-a-range", "@p")


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
