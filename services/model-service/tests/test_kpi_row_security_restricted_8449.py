"""Bug-8449 / Bug-8427 — a row-security denial must not be reported as "no data".

Live evidence that motivated this (2026-07-29, running local stack, acme-demo):

* ``POST .../kpis/evaluate-adhoc`` for a plain single-measure Revenue definition
  on ``modelx`` returned HTTP 200 with ``value: null``, ``formatted_value:
  "N/A"``, ``trend_label: "Insufficient Data"`` and NO explanation.
* The router response for the very same compiled SQL
  (``SELECT SUM("Revenue") AS value FROM "modelx"``) carried
  ``reason = "Row security active (1 rule(s): __deny_all__) ..."`` and
  ``routed_sql = "... WHERE 0 = 1"``.
* On models with NO row-security rules the same endpoint returned the CORRECT
  known answer, byte-identical to the JDBC gateway (``modell``/``base_amount``
  -> 2894296330.29; ``inventory``/``cost_price`` -> 852672.8). So the KPI
  arithmetic was never wrong — only the explanation was missing.

Four outcomes must stay distinguishable, and this module asserts the exact
mapping for each rather than merely "an object came back":

  1. a scoped real value           -> value set, not restricted
  2. no rows (genuinely empty)     -> value None, NOT restricted, no lock badge
  3. deny-all aggregate NULL       -> value None, restricted, explicit label
  4. deny-all COUNT/COALESCE zero  -> value redacted, restricted, explicit label

Run from tessallite/services/model-service/:
    pytest tests/test_kpi_row_security_restricted_8449.py -q
"""
from __future__ import annotations

import types

import pytest

from src.api.kpis import (
    ROW_SECURITY_DENY_ALL_RULE_ID,
    ROW_SECURITY_RESTRICTED_LABEL,
    _absorb_security_rules,
    _apply_composite_result,
    _stamp_row_security_restriction,
    row_security_denied_all,
)
from shared.schemas.pydantic_models import KPIEvaluateResponse

# F-017-12: shim caller_has_role to the token-role decision for these mocked-db
# unit tests (see conftest.kpi_effective_role); the real binding behaviour is
# covered by test_kpi_draft_visibility.
pytestmark = pytest.mark.usefixtures("kpi_effective_role")


# ---------------------------------------------------------------------------
# The producer/consumer seam: what the router says -> what the sink records
# ---------------------------------------------------------------------------

def test_deny_all_sentinel_matches_the_compiler_and_the_router_contract():
    """Three copies of this string exist (compiler, router response contract,
    KPI consumer). If they drift, the consumer branch silently never fires and
    the bug returns with every test still green."""
    from shared.security.predicate_compiler import _deny_all_predicate

    assert ROW_SECURITY_DENY_ALL_RULE_ID in _deny_all_predicate().active_rule_ids


def test_absorb_records_the_routers_applied_rule_ids():
    sink: set[str] = set()
    _absorb_security_rules(
        {"rows": [{"value": None}], "security_rules_applied": ["__deny_all__"]}, sink,
    )
    assert sink == {"__deny_all__"}
    assert row_security_denied_all(sink) is True


def test_absorb_on_an_unrestricted_response_leaves_the_sink_empty():
    sink: set[str] = set()
    _absorb_security_rules({"rows": [{"value": 42}], "security_rules_applied": []}, sink)
    assert sink == set()
    assert row_security_denied_all(sink) is False


def test_absorb_tolerates_a_router_that_predates_the_field():
    """A rolling deploy can pair a new model-service with an old query-router.
    The absent field must read as "no rule reported", never crash."""
    sink: set[str] = set()
    _absorb_security_rules({"rows": [], "columns": []}, sink)
    assert sink == set()
    assert row_security_denied_all(sink) is False


def test_absorb_is_a_no_op_without_a_sink():
    _absorb_security_rules({"security_rules_applied": ["__deny_all__"]}, None)  # no raise
    assert row_security_denied_all(None) is False


def test_a_narrowing_rule_is_not_reported_as_denied():
    """An EMEA manager legitimately sees only EMEA rows. That value is CORRECT
    for them and must never be badged "restricted" — only the deny-all coverage
    predicate means "you are entitled to zero rows"."""
    sink: set[str] = set()
    _absorb_security_rules(
        {"security_rules_applied": ["9e97a9b5-27b0-4b83-95b5-45b16bc21706"]}, sink,
    )
    assert sink != set()
    assert row_security_denied_all(sink) is False


# ---------------------------------------------------------------------------
# The response mapping: the three outcomes, asserted by exact field values
# ---------------------------------------------------------------------------

def _resp(value=None, status_label=None, **fields):
    return KPIEvaluateResponse(
        value=value, status_label=status_label, **fields,
    )


def test_denied_and_no_value_is_labelled_restricted():
    out = _stamp_row_security_restriction(_resp(), {ROW_SECURITY_DENY_ALL_RULE_ID})
    assert out.row_security_restricted is True
    assert out.status_label == ROW_SECURITY_RESTRICTED_LABEL


def test_no_data_without_a_denial_is_not_labelled_restricted():
    """Outcome 2 — the honest empty slice. Mislabelling this would tell a user
    their permissions are wrong when the data simply is not there."""
    out = _stamp_row_security_restriction(_resp(), set())
    assert out.row_security_restricted is None
    assert out.status_label is None


def test_deny_all_redacts_a_non_null_scalar_including_count_zero():
    """COUNT and COALESCE return zero over the deny-all empty scan. That zero
    is a governance artifact, not an authoritative business measurement."""
    out = _stamp_row_security_restriction(
        _resp(value=0.0, value_str="0", formatted_value="0"),
        {ROW_SECURITY_DENY_ALL_RULE_ID},
    )
    assert out.value is None
    assert out.value_str is None
    assert out.formatted_value is None
    assert out.row_security_restricted is True
    assert out.status_label == ROW_SECURITY_RESTRICTED_LABEL


def test_a_scoped_value_without_deny_all_is_preserved():
    out = _stamp_row_security_restriction(_resp(value=852672.8), {"region-emea"})
    assert out.value == 852672.8
    assert out.row_security_restricted is None
    assert out.status_label is None


def test_deny_all_overrides_existing_derived_status_and_trend_fields():
    """Derived presentation must not reassert a redacted scalar."""
    out = _stamp_row_security_restriction(
        _resp(
            value=0.0,
            status=1,
            status_label="On Track",
            status_color="#008000",
            status_position=1.0,
            status_bands=[{"label": "On Track"}],
            target=10.0,
            goal=10.0,
            formatted_target="10",
            formatted_goal="10",
            formatted_variance="-10",
            trend=1,
            trend_label="Improving",
            trend_pct=0.1,
            trend_pct_normalised=0.1,
            trend_series=[{"period": "2026-08", "value": 0.0}],
        ),
        {ROW_SECURITY_DENY_ALL_RULE_ID},
    )
    assert out.row_security_restricted is True
    assert out.status_label == ROW_SECURITY_RESTRICTED_LABEL
    for field in (
        "value", "target", "goal", "formatted_target", "formatted_goal",
        "formatted_variance", "status", "status_color", "status_position",
        "status_bands", "trend", "trend_label", "trend_pct",
        "trend_pct_normalised", "trend_series",
    ):
        assert getattr(out, field) is None, field


def test_restricted_label_is_not_a_composite_child_error():
    """``_child_error_reason`` treats a null-valued child as ERRORED when its
    label starts with one of the fail-loud prefixes, which flips the composite
    parent to degraded/error. A permissions decision is not a broken input, so
    the restricted label must NOT match any of those prefixes."""
    from src.api.kpis import _CHILD_ERROR_LABEL_PREFIXES, _child_error_reason

    assert not ROW_SECURITY_RESTRICTED_LABEL.startswith(_CHILD_ERROR_LABEL_PREFIXES)
    restricted = _stamp_row_security_restriction(
        _resp(), {ROW_SECURITY_DENY_ALL_RULE_ID},
    )
    assert _child_error_reason(restricted) is None


def test_stamp_tolerates_a_none_response():
    assert _stamp_row_security_restriction(None, {ROW_SECURITY_DENY_ALL_RULE_ID}) is None


# ---------------------------------------------------------------------------
# The transport: _execute_via_router must fill the sink from a real payload
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_execute_via_router_populates_the_sink_from_the_response(monkeypatch):
    """End-to-end through the actual HTTP client seam, so a future refactor that
    drops the ``_absorb_security_rules`` call is caught here and not only by the
    (unit-level) helper tests above."""
    import src.api.kpis as kpis_mod

    payload = {
        "rows": [{"value": None}],
        "columns": ["value"],
        "route_type": "source",
        "reason": "Row security active (1 rule(s): __deny_all__)",
        "routed_sql": 'SELECT SUM("Revenue") AS "value" FROM "t" WHERE 0 = 1',
        "security_rules_applied": ["__deny_all__"],
    }

    class _Resp:
        status_code = 200

        def json(self):
            return payload

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **kw):
            return _Resp()

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _Client)

    sink: set[str] = set()
    out = await kpis_mod._execute_via_router(
        "model-1", 'SELECT SUM("Revenue") FROM "t"', "tok", security_sink=sink,
    )
    assert out["rows"] == [{"value": None}]
    assert row_security_denied_all(sink) is True


@pytest.mark.asyncio
async def test_evaluate_expression_via_sql_threads_the_sink(monkeypatch):
    """The value pathway must be untouched (still None) while the CAUSE reaches
    the caller — the exact property Bug-8427 was missing."""
    import src.api.kpis as kpis_mod

    captured: dict = {}

    async def _fake_exec(model_id, sql, bearer, timeout_s=30.0, persona_id=None,
                         security_sink=None):
        captured["sql"] = sql
        _absorb_security_rules({"security_rules_applied": ["__deny_all__"]}, security_sink)
        return {"rows": [{"value": None}], "columns": ["value"]}

    monkeypatch.setattr(kpis_mod, "_execute_via_router", _fake_exec)

    ctx = types.SimpleNamespace(calc_agg_mode="automatic")
    measure = types.SimpleNamespace(name="Revenue", default_agg="sum")
    sink: set[str] = set()
    value = await kpis_mod._evaluate_expression_via_sql(
        'sum(measure("Revenue"))', "model-1", "modelx", "tok",
        {"Revenue": measure}, ctx, security_sink=sink,
    )
    assert value is None, "a denied query yields no value — that part is correct"
    assert row_security_denied_all(sink) is True, (
        "but the caller must be able to learn WHY it is None"
    )
    assert "SUM" in captured["sql"].upper()


# ---------------------------------------------------------------------------
# Round-1 deep review, finding 3 — the PYTHON-EVALUATOR fallback.
#
# The SQL compiler bails (``_COMPILER_UNSUPPORTED``) for kpi() cross-references
# and other uncompilable expressions, and composite KPIs never take the SQL path
# at all. Those routes fetch measures through _get_measure_value /
# _batch_get_measure_values, which originally received no sink — so a deny-all
# on exactly those KPIs still rendered a bare "N/A". The sink is now threaded
# through the whole fallback chain; these pin each link.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_measure_value_populates_the_sink(monkeypatch):
    import src.api.kpis as kpis_mod

    async def _fake_exec(model_id, sql, bearer, timeout_s=30.0, persona_id=None,
                         security_sink=None):
        _absorb_security_rules({"security_rules_applied": ["__deny_all__"]}, security_sink)
        return {"rows": [{"v": None}]}

    monkeypatch.setattr(kpis_mod, "_execute_via_router", _fake_exec)
    sink: set[str] = set()
    out = await kpis_mod._get_measure_value(
        "model-1", "Revenue", "tok", "modelx", "sum", security_sink=sink,
    )
    assert out is None
    assert row_security_denied_all(sink) is True


@pytest.mark.asyncio
async def test_batch_get_measure_values_populates_the_sink(monkeypatch):
    import src.api.kpis as kpis_mod

    async def _fake_exec(model_id, sql, bearer, timeout_s=30.0, persona_id=None,
                         security_sink=None):
        _absorb_security_rules({"security_rules_applied": ["__deny_all__"]}, security_sink)
        return {"rows": [{"m0": None}]}

    monkeypatch.setattr(kpis_mod, "_execute_via_router", _fake_exec)
    measure = types.SimpleNamespace(name="Revenue", default_agg="sum")
    sink: set[str] = set()
    out = await kpis_mod._batch_get_measure_values(
        "model-1", ["Revenue"], "tok", "modelx", {"Revenue": measure},
        security_sink=sink,
    )
    assert out == {"Revenue": None}
    assert row_security_denied_all(sink) is True


@pytest.mark.asyncio
async def test_measure_provider_forwards_the_sink(monkeypatch):
    """The provider is what the Python pipeline actually calls. Its per-measure
    exception handler deliberately degrades to None, so without the sink a
    denial is completely invisible on this route."""
    import src.api.kpis as kpis_mod

    async def _fake_exec(model_id, sql, bearer, timeout_s=30.0, persona_id=None,
                         security_sink=None):
        _absorb_security_rules({"security_rules_applied": ["__deny_all__"]}, security_sink)
        return {"rows": [{"v": None}]}

    monkeypatch.setattr(kpis_mod, "_execute_via_router", _fake_exec)
    measure = types.SimpleNamespace(name="Revenue", default_agg="sum")
    sink: set[str] = set()
    provider = kpis_mod._build_measure_provider(
        "model-1", "modelx", "tok", {"Revenue": measure}, security_sink=sink,
    )
    assert await provider.get_measure_value("Revenue") is None
    assert row_security_denied_all(sink) is True


@pytest.mark.asyncio
async def test_measure_provider_replays_prefetch_rules_only_on_cache_use():
    """A batch prefetch's deny-all metadata belongs to KPIs that consume its
    values. A literal sibling never calls the provider and remains visible."""
    import src.api.kpis as kpis_mod

    sink: set[str] = set()
    provider = kpis_mod._build_measure_provider(
        "model-1",
        "modelx",
        "tok",
        {},
        measure_value_cache={"Orders": 0.0},
        measure_value_security_rules={ROW_SECURITY_DENY_ALL_RULE_ID},
        security_sink=sink,
    )

    assert sink == set()
    assert await provider.get_measure_value("Orders") == 0.0
    assert sink == {ROW_SECURITY_DENY_ALL_RULE_ID}


@pytest.mark.asyncio
async def test_measure_provider_replays_referenced_kpi_rules_on_cache_use():
    """A scalar kpi() cache must carry the referent's governance verdict."""
    import src.api.kpis as kpis_mod

    sink: set[str] = set()
    provider = kpis_mod._build_measure_provider(
        "model-1",
        "modelx",
        "tok",
        {},
        kpi_value_cache={"Restricted Composite": None},
        kpi_value_security_rules={
            "Restricted Composite": {ROW_SECURITY_DENY_ALL_RULE_ID},
        },
        security_sink=sink,
    )

    assert sink == set()
    assert await provider.get_kpi_value("Restricted Composite") is None
    assert sink == {ROW_SECURITY_DENY_ALL_RULE_ID}


def test_every_execute_via_router_call_in_kpis_passes_a_security_sink():
    """Enumeration guard (CLAUDE.md shared-primitive discipline).

    A NEW value-fetching call added to kpis.py without a ``security_sink=``
    silently reopens the Bug-8427 conflation on that route, and no behavioural
    test would notice because the route simply reports "no data" as before.
    Parses the module rather than grepping so a call spanning several lines is
    still seen.

    KNOWN BOUNDARIES, stated rather than implied (round-2 deep review, finding
    3 — the earlier docstring wrongly claimed this "fails closed on a shape it
    cannot classify"):

    * SCOPE is this module only. The other five hand-rolled ``/execute``
      clients in the codebase are tracked as Bug-8453, not here.
    * A call-site keyword check cannot see a denial dropped one LEVEL ABOVE
      ``_execute_via_router`` — e.g. a KPI whose expression is only
      ``kpi("...")`` references issues no direct call at all. That route is
      covered behaviourally by
      ``test_referenced_kpi_restriction_reaches_the_parents_sink``, not here.

    Within its scope it now fails closed on the two evasions it CAN see: an
    indirect reference to the function (alias / functools.partial), and a
    literal ``security_sink=None``, which satisfies a naive keyword check while
    being functionally identical to omitting the argument.
    """
    import ast
    import inspect

    import src.api.kpis as kpis_mod

    src = inspect.getsource(kpis_mod)
    tree = ast.parse(src)
    target = "_execute_via_router"

    missing: list[int] = []
    explicit_none: list[int] = []
    call_nodes: set[int] = set()

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not isinstance(fn, (ast.Name, ast.Attribute)):
            continue
        if (getattr(fn, "id", None) or getattr(fn, "attr", None)) != target:
            continue
        call_nodes.add(id(fn))
        kw = next((k for k in node.keywords if k.arg == "security_sink"), None)
        if kw is None:
            missing.append(node.lineno)
        elif isinstance(kw.value, ast.Constant) and kw.value.value is None:
            explicit_none.append(node.lineno)

    # Any reference to the name that is NOT the callee of a direct call — an
    # alias assignment or a functools.partial — routes around the check above.
    indirect: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == target and id(node) not in call_nodes:
            indirect.append(node.lineno)
        elif (
            isinstance(node, ast.Attribute)
            and node.attr == target
            and id(node) not in call_nodes
        ):
            indirect.append(node.lineno)
    # The def statement itself is not an indirect reference.
    fn_defs = {
        n.lineno for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == target
    }
    indirect = [ln for ln in indirect if ln not in fn_defs]

    assert missing == [], (
        "every _execute_via_router call in kpis.py must pass security_sink= so a "
        "row-security denial on that route is not reported as 'no data'; "
        f"missing at line(s) {missing}"
    )
    assert explicit_none == [], (
        "security_sink=None is the same as omitting it — a denial on this route "
        f"is invisible; line(s) {explicit_none}"
    )
    assert indirect == [], (
        "_execute_via_router is referenced indirectly (alias or partial) at "
        f"line(s) {indirect}; the call-site guard above cannot see calls made "
        "through such a reference, so this fails closed instead of passing "
        "silently. Call the function directly, or extend this guard."
    )


def test_every_governed_evaluation_wrapper_call_passes_a_security_sink():
    """Target/value wrappers must not drop governance above the router call.

    A direct ``_execute_via_router`` guard stays green when its immediate
    wrapper collects rule ids but an endpoint forgets to pass that wrapper its
    sink. Enumerate both wrappers so every value and target route remains wired.
    """
    import ast
    import inspect

    import src.api.kpis as kpis_mod

    tree = ast.parse(inspect.getsource(kpis_mod))
    targets = {"_evaluate_expression_via_sql", "_get_measure_value"}
    missing: list[tuple[str, int]] = []
    explicit_none: list[tuple[str, int]] = []
    direct_callees: set[int] = set()

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not isinstance(fn, (ast.Name, ast.Attribute)):
            continue
        name = getattr(fn, "id", None) or getattr(fn, "attr", None)
        if name not in targets:
            continue
        direct_callees.add(id(fn))
        kw = next((k for k in node.keywords if k.arg == "security_sink"), None)
        if kw is None:
            missing.append((name, node.lineno))
        elif isinstance(kw.value, ast.Constant) and kw.value.value is None:
            explicit_none.append((name, node.lineno))

    indirect: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        name = None
        if isinstance(node, ast.Name):
            name = node.id
        elif isinstance(node, ast.Attribute):
            name = node.attr
        if name in targets and id(node) not in direct_callees:
            indirect.append((name, node.lineno))

    fn_defs = {
        (node.name, node.lineno)
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in targets
    }
    indirect = [ref for ref in indirect if ref not in fn_defs]

    assert missing == [], f"governed wrapper calls missing security_sink=: {missing}"
    assert explicit_none == [], f"governed wrapper calls disable the sink: {explicit_none}"
    assert indirect == [], f"indirect governed-wrapper references evade the guard: {indirect}"


@pytest.mark.asyncio
async def test_referenced_kpi_restriction_reaches_the_parents_sink(monkeypatch):
    """Round-2 deep review, finding 2.

    A KPI whose expression is ONLY ``kpi("B")`` references makes no direct
    router call — ``extract_measure_names`` finds nothing and
    ``_batch_get_measure_values`` early-returns — so the AST guard above is
    structurally blind to it. The referenced KPI's restriction must still reach
    the parent's sink, or the parent renders a bare "N/A" under a deny-all.
    """
    import src.api.kpis as kpis_mod

    ref_kpi = types.SimpleNamespace(
        id="kpi-b", name="B", expression='sum(measure("Revenue"))',
        kpi_type=None, model_id="model-1",
        certification_status="certified",
        measure_ids=None, dimension_ids=None, business_definition=None,
    )

    async def _fake_single(*a, **kw):
        return _stamp_row_security_restriction(
            _resp(), {ROW_SECURITY_DENY_ALL_RULE_ID},
        )

    monkeypatch.setattr(kpis_mod, "_evaluate_single_kpi", _fake_single)
    monkeypatch.setattr(
        kpis_mod, "_kpi_visible_to_persona", lambda *a, **k: True,
    )

    sink: set[str] = set()
    cache = await kpis_mod._resolve_referenced_kpi_values(
        'kpi("B") * 1.05', _RefDb(ref_kpi), "model-1", "modelx", "tok", {},
        model=types.SimpleNamespace(
            deployed_version_id=None, fiscal_year_start_month=None,
            calendar_type=None,
        ),
        is_privileged=True,
        persona_id=None,
        allowed_measure_ids=None,
        name_to_id={"B": "kpi-b"},
        security_sink=sink,
    )
    assert cache == {"B": None}
    assert row_security_denied_all(sink) is True, (
        "a restricted referenced KPI must mark the referring KPI restricted too"
    )


@pytest.mark.asyncio
async def test_referenced_composite_restriction_reaches_the_parents_sink(monkeypatch):
    """A composite referenced through kpi() must restrict the referring KPI,
    even though the reference resolves to a null score and then continues."""
    import src.api.kpis as kpis_mod
    from src.kpi_composite import COMPOSITE_STATUS_RESTRICTED, CompositeResult

    ref_kpi = types.SimpleNamespace(
        id="composite-b", name="B", expression="literal(0)",
        kpi_type="composite", model_id="model-1",
        certification_status="certified",
        measure_ids=None, dimension_ids=None, business_definition=None,
    )
    captured: dict[str, object] = {}

    async def _restricted_composite(*args, **kwargs):
        captured["sink"] = kwargs.get("security_sink")
        return CompositeResult(
            composite_score=None,
            status=COMPOSITE_STATUS_RESTRICTED,
        )

    monkeypatch.setattr(
        kpis_mod, "_evaluate_composite_score", _restricted_composite,
    )
    monkeypatch.setattr(
        kpis_mod, "_kpi_visible_to_persona", lambda *a, **k: True,
    )

    async def _governed_composite(*args, **kwargs):
        return types.SimpleNamespace(row_security_restricted=True)

    monkeypatch.setattr(kpis_mod, "_evaluate_single_kpi", _governed_composite)

    sink: set[str] = set()
    cache = await kpis_mod._resolve_referenced_kpi_values(
        'kpi("B")', _RefDb(ref_kpi), "model-1", "modelx", "tok", {},
        model=types.SimpleNamespace(deployed_version_id=None),
        is_privileged=True,
        persona_id=None,
        allowed_measure_ids=None,
        name_to_id={"B": "composite-b"},
        security_sink=sink,
    )

    assert cache == {"B": None}
    assert captured["sink"] is not sink
    assert sink == {ROW_SECURITY_DENY_ALL_RULE_ID}


class _RefDb:
    """Async-session stub returning one referenced KPI for any lookup."""

    def __init__(self, kpi):
        self._kpi = kpi

    async def execute(self, *a, **kw):
        kpi = self._kpi

        class _Scalars:
            def all(self):
                return [kpi]

            def first(self):
                return kpi

        class _Result:
            def scalars(self):
                return _Scalars()

            def scalar_one_or_none(self):
                return kpi

        return _Result()

    async def get(self, *a, **kw):
        return None


# ---------------------------------------------------------------------------
# Reviewer follow-up: real single and batch composite orchestration.
# ---------------------------------------------------------------------------

def _with_count_measure(db, scalar_result_type, model_id):
    """Make the existing entity-aware test DB return one COUNT measure."""
    from unittest.mock import AsyncMock

    measure = types.SimpleNamespace(
        id="orders-measure",
        model_id=model_id,
        name="Orders",
        default_agg="count",
    )
    original_execute = db.execute.side_effect

    async def _execute(stmt, *args, **kwargs):
        descriptions = getattr(stmt, "column_descriptions", None)
        entity = descriptions[0].get("entity") if descriptions else None
        if getattr(entity, "__name__", "") == "Measure":
            return scalar_result_type([measure])
        return await original_execute(stmt, *args, **kwargs)

    db.execute = AsyncMock(side_effect=_execute)
    return db


def _composite_governance_fixture():
    from .test_kpi_composite_indicators import _kpi

    parent = _kpi(
        name="governance_parent",
        kpi_type="composite",
        expression="literal(0)",
    )
    denied = _kpi(
        name="denied_count",
        expression='coalesce(measure("Orders"), literal(0))',
        parent_kpi_id=parent.id,
        target_value=100.0,
    )
    visible = _kpi(
        name="visible_literal",
        expression="literal(50)",
        parent_kpi_id=parent.id,
        target_value=100.0,
    )
    return parent, denied, visible


def _compiler_fallback_for(denied, visible, unsupported):
    async def _evaluate(expression, *args, **kwargs):
        if expression == denied.expression:
            return unsupported
        if expression == visible.expression:
            return 50.0
        return 0.0

    return _evaluate


async def _deny_all_count_router(
    model_id, sql, bearer, timeout_s=30.0, persona_id=None,
    security_sink=None,
):
    assert "COUNT" in sql.upper()
    _absorb_security_rules(
        {"security_rules_applied": [ROW_SECURITY_DENY_ALL_RULE_ID]},
        security_sink,
    )
    return {"rows": [{"m0": 0.0}]}


async def _security_only_on_target(expression, *args, security_sink=None, **kwargs):
    if expression == 'literal(7)':
        _absorb_security_rules(
            {"security_rules_applied": [ROW_SECURITY_DENY_ALL_RULE_ID]},
            security_sink,
        )
        return 0.0
    return 42.0


@pytest.mark.asyncio
async def test_single_target_expression_restriction_redacts_the_kpi(client):
    """The saved single-evaluate target wrapper must share the value sink."""
    from unittest.mock import AsyncMock, patch

    from .conftest import TEST_MODEL_ID, async_gen_from
    from .test_kpi_composite_indicators import PREFIX, _entity_db, _kpi, _model

    kpi = _kpi(name="target_expression_governance", expression="literal(42)")
    kpi.target_type = "expression"
    kpi.target_expression = 'literal(7)'
    db = _entity_db(_model(deployed=False), [], get_kpis=[kpi])

    with (
        patch("src.api.kpis.get_tenant_db", async_gen_from(db)),
        patch(
            "src.api.kpis.resolve_effective_persona",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "src.api.kpis._evaluate_expression_via_sql",
            side_effect=_security_only_on_target,
        ),
    ):
        response = await client.post(f"{PREFIX}/{kpi.id}/evaluate")

    assert response.status_code == 200
    data = response.json()
    assert data["kpi_id"] == str(kpi.id)
    assert data["value"] is None
    assert data["target"] is None
    assert data["row_security_restricted"] is True
    assert data["status_label"] == ROW_SECURITY_RESTRICTED_LABEL


@pytest.mark.asyncio
async def test_single_target_measure_restriction_redacts_the_kpi(client):
    """The saved target-measure wrapper must share the value sink."""
    from unittest.mock import AsyncMock, patch

    from .conftest import TEST_MODEL_ID, async_gen_from
    from .test_kpi_composite_indicators import (
        PREFIX,
        _ScalarResult,
        _entity_db,
        _kpi,
        _model,
    )

    kpi = _kpi(name="target_measure_governance", expression="literal(42)")
    kpi.target_type = "measure"
    kpi.target_measure_id = "orders-measure"
    db = _with_count_measure(
        _entity_db(_model(deployed=False), [], get_kpis=[kpi]),
        _ScalarResult,
        TEST_MODEL_ID,
    )

    async def _restricted_target(*args, security_sink=None, **kwargs):
        _absorb_security_rules(
            {"security_rules_applied": [ROW_SECURITY_DENY_ALL_RULE_ID]},
            security_sink,
        )
        return 0.0

    with (
        patch("src.api.kpis.get_tenant_db", async_gen_from(db)),
        patch(
            "src.api.kpis.resolve_effective_persona",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "src.api.kpis._evaluate_expression_via_sql",
            new_callable=AsyncMock,
            return_value=42.0,
        ),
        patch(
            "src.api.kpis._get_measure_value",
            side_effect=_restricted_target,
        ),
    ):
        response = await client.post(f"{PREFIX}/{kpi.id}/evaluate")

    assert response.status_code == 200
    data = response.json()
    assert data["value"] is None
    assert data["target"] is None
    assert data["row_security_restricted"] is True
    assert data["status_label"] == ROW_SECURITY_RESTRICTED_LABEL


@pytest.mark.asyncio
async def test_adhoc_target_expression_restriction_redacts_the_preview(client):
    """The ad-hoc target wrapper must contribute to the preview's sink."""
    from unittest.mock import AsyncMock, patch

    from .conftest import TEST_PROJECT_ID, async_gen_from
    from .test_kpi_composite_indicators import PREFIX, _entity_db, _model

    model = _model(deployed=False)
    model.project_id = TEST_PROJECT_ID
    db = _entity_db(model, [])

    with (
        patch("src.api.kpis.get_tenant_db", async_gen_from(db)),
        patch(
            "src.api.kpis.resolve_effective_persona",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "src.api.kpis._evaluate_expression_via_sql",
            side_effect=_security_only_on_target,
        ),
    ):
        response = await client.post(
            f"{PREFIX}/evaluate-adhoc",
            json={
                "expression": "literal(42)",
                "target_expression": 'literal(7)',
            },
        )

    assert response.status_code == 200
    data = response.json()
    assert data["value"] is None
    assert data["target"] is None
    assert data["row_security_restricted"] is True
    assert data["status_label"] == ROW_SECURITY_RESTRICTED_LABEL


def _restricted_composite_reference_fixture():
    from .test_kpi_composite_indicators import _kpi

    parent = _kpi(
        name="Restricted Composite",
        kpi_type="composite",
        expression="literal(0)",
    )
    child = _kpi(
        name="Denied Child",
        expression='coalesce(measure("Orders"), literal(0))',
        parent_kpi_id=parent.id,
        target_value=100.0,
    )
    consumer = _kpi(
        name="Composite Consumer",
        expression='coalesce(kpi("Restricted Composite"), literal(0))',
        target_value=100.0,
    )
    return parent, child, consumer


async def _restricted_batch_prefetch(
    model_id, measure_names, bearer, model_slug, measure_map,
    persona_id=None, security_sink=None,
):
    if security_sink is not None:
        security_sink.add(ROW_SECURITY_DENY_ALL_RULE_ID)
    return {name: 0.0 for name in measure_names}


def _batch_reference_fallback(unsupported):
    async def _evaluate(expression, *args, **kwargs):
        if "coalesce" in expression:
            return unsupported
        return 0.0

    return _evaluate


@pytest.mark.asyncio
async def test_batch_fresh_restricted_composite_reference_is_restricted(client):
    """A fresh composite must finalize before a coalesce(kpi(...), 0) user."""
    from unittest.mock import AsyncMock, patch

    import src.api.kpis as kpis_mod
    from src.kpi_cache import get_kpi_cache
    from .conftest import TEST_MODEL_ID, async_gen_from
    from .test_kpi_composite_indicators import PREFIX, _entity_db, _model

    parent, child, consumer = _restricted_composite_reference_fixture()
    all_kpis = [parent, child, consumer]
    db = _entity_db(_model(deployed=False), [all_kpis, []])
    get_kpi_cache().invalidate_model(TEST_MODEL_ID)

    with (
        patch("src.api.kpis.get_tenant_db", async_gen_from(db)),
        patch(
            "src.api.kpis.resolve_effective_persona",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "src.api.kpis._batch_get_measure_values",
            side_effect=_restricted_batch_prefetch,
        ),
        patch(
            "src.api.kpis._evaluate_expression_via_sql",
            side_effect=_batch_reference_fallback(
                kpis_mod._COMPILER_UNSUPPORTED,
            ),
        ),
    ):
        response = await client.post(
            f"{PREFIX}/evaluate-batch",
            json={"kpi_ids": [str(k.id) for k in all_kpis]},
        )

    assert response.status_code == 200
    by_id = {row["kpi_id"]: row for row in response.json()["results"]}
    for kpi in (parent, consumer):
        result = by_id[str(kpi.id)]
        assert result["value"] is None
        assert result["row_security_restricted"] is True
        assert result["status_label"] == ROW_SECURITY_RESTRICTED_LABEL


@pytest.mark.asyncio
async def test_batch_cached_restricted_composite_reference_is_restricted(client):
    """A cached restricted composite must restrict a fresh kpi() consumer."""
    from unittest.mock import AsyncMock, patch

    import src.api.kpis as kpis_mod
    from src.kpi_cache import get_kpi_cache
    from .conftest import TEST_MODEL_ID, async_gen_from
    from .test_kpi_composite_indicators import PREFIX, _entity_db, _model

    parent, child, consumer = _restricted_composite_reference_fixture()
    db = _entity_db(
        _model(deployed=False),
        [[parent], [child], [parent, consumer], [child]],
    )
    get_kpi_cache().invalidate_model(TEST_MODEL_ID)

    with (
        patch("src.api.kpis.get_tenant_db", async_gen_from(db)),
        patch(
            "src.api.kpis.resolve_effective_persona",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "src.api.kpis._batch_get_measure_values",
            side_effect=_restricted_batch_prefetch,
        ),
        patch(
            "src.api.kpis._evaluate_expression_via_sql",
            side_effect=_batch_reference_fallback(
                kpis_mod._COMPILER_UNSUPPORTED,
            ),
        ),
    ):
        warm = await client.post(
            f"{PREFIX}/evaluate-batch",
            json={"kpi_ids": [str(parent.id)]},
        )
        response = await client.post(
            f"{PREFIX}/evaluate-batch",
            json={"kpi_ids": [str(parent.id), str(consumer.id)]},
        )

    assert warm.status_code == 200
    assert warm.json()["results"][0]["row_security_restricted"] is True
    assert response.status_code == 200
    by_id = {row["kpi_id"]: row for row in response.json()["results"]}
    result = by_id[str(consumer.id)]
    assert result["value"] is None
    assert result["row_security_restricted"] is True
    assert result["status_label"] == ROW_SECURITY_RESTRICTED_LABEL


def _target_restricted_composite_fixture():
    from .test_kpi_composite_indicators import _kpi

    composite = _kpi(
        name="Target Restricted Composite",
        kpi_type="composite",
        expression="literal(999)",
    )
    composite.target_type = "expression"
    composite.target_expression = "literal(7)"
    child = _kpi(
        name="Visible Composite Child",
        expression="literal(50)",
        parent_kpi_id=composite.id,
        target_value=100.0,
    )
    consumer = _kpi(
        name="Target Restricted Consumer",
        expression=(
            'coalesce(kpi("Target Restricted Composite"), literal(0))'
        ),
        target_value=100.0,
    )
    return composite, child, consumer


def _target_restricted_expression_evaluator(unsupported):
    async def _evaluate(expression, *args, security_sink=None, **kwargs):
        if expression == "literal(999)":
            pytest.fail("a composite placeholder must never be evaluated")
        if expression == "literal(7)":
            if security_sink is not None:
                security_sink.add(ROW_SECURITY_DENY_ALL_RULE_ID)
            return 0.0
        if 'kpi("Target Restricted Composite")' in expression:
            return unsupported
        return {
            "literal(50)": 50.0,
            "literal(60)": 60.0,
            "literal(40)": 40.0,
        }.get(expression, 0.0)

    return _evaluate


@pytest.mark.asyncio
async def test_batch_composite_restricted_target_with_visible_child_is_restricted(client):
    """A target denial is authoritative even when every child is visible."""
    from unittest.mock import AsyncMock, patch

    import src.api.kpis as kpis_mod
    from src.kpi_cache import get_kpi_cache
    from .conftest import TEST_MODEL_ID, async_gen_from
    from .test_kpi_composite_indicators import PREFIX, _entity_db, _model

    composite, child, _ = _target_restricted_composite_fixture()
    db = _entity_db(_model(deployed=False), [[composite, child], []])
    get_kpi_cache().invalidate_model(TEST_MODEL_ID)

    with (
        patch("src.api.kpis.get_tenant_db", async_gen_from(db)),
        patch(
            "src.api.kpis.resolve_effective_persona",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "src.api.kpis._evaluate_expression_via_sql",
            side_effect=_target_restricted_expression_evaluator(
                kpis_mod._COMPILER_UNSUPPORTED,
            ),
        ),
    ):
        response = await client.post(
            f"{PREFIX}/evaluate-batch",
            json={"kpi_ids": [str(composite.id), str(child.id)]},
        )

    assert response.status_code == 200
    by_id = {row["kpi_id"]: row for row in response.json()["results"]}
    visible = by_id[str(child.id)]
    restricted = by_id[str(composite.id)]
    assert visible["value"] == 50.0
    assert visible["row_security_restricted"] is None
    assert restricted["value"] is None
    assert restricted["target"] is None
    assert restricted["row_security_restricted"] is True
    assert restricted["status_label"] == ROW_SECURITY_RESTRICTED_LABEL


@pytest.mark.asyncio
async def test_single_nested_composite_merges_target_restriction_with_visible_sibling(
    client,
):
    """Nested-child finalization must retain the child's own target denial."""
    from unittest.mock import AsyncMock, patch

    import src.api.kpis as kpis_mod
    from .conftest import async_gen_from
    from .test_kpi_composite_indicators import PREFIX, _entity_db, _kpi, _model

    grandparent = _kpi(
        name="Grandparent Composite",
        kpi_type="composite",
        expression="literal(999)",
    )
    nested = _kpi(
        name="Nested Target Restricted",
        kpi_type="composite",
        expression="literal(999)",
        parent_kpi_id=grandparent.id,
    )
    nested.target_type = "expression"
    nested.target_expression = "literal(7)"
    nested_leaf = _kpi(
        name="Nested Visible Leaf",
        expression="literal(60)",
        parent_kpi_id=nested.id,
        target_value=100.0,
    )
    sibling = _kpi(
        name="Grandparent Visible Sibling",
        expression="literal(40)",
        parent_kpi_id=grandparent.id,
        target_value=100.0,
    )
    db = _entity_db(
        _model(deployed=False),
        [[nested, sibling], [nested_leaf]],
        get_kpis=[grandparent],
    )

    with (
        patch("src.api.kpis.get_tenant_db", async_gen_from(db)),
        patch(
            "src.api.kpis.resolve_effective_persona",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "src.api.kpis._evaluate_expression_via_sql",
            side_effect=_target_restricted_expression_evaluator(
                kpis_mod._COMPILER_UNSUPPORTED,
            ),
        ),
    ):
        response = await client.post(f"{PREFIX}/{grandparent.id}/evaluate")

    assert response.status_code == 200
    data = response.json()
    assert data["value"] is None
    assert data["row_security_restricted"] is True
    assert data["status_label"] == ROW_SECURITY_RESTRICTED_LABEL
    assert data["composite_status"] == "restricted"


@pytest.mark.asyncio
async def test_batch_fresh_target_restricted_composite_restricts_kpi_consumer(client):
    """Fresh target governance must cross composite finalization into kpi()."""
    from unittest.mock import AsyncMock, patch

    import src.api.kpis as kpis_mod
    from src.kpi_cache import get_kpi_cache
    from .conftest import TEST_MODEL_ID, async_gen_from
    from .test_kpi_composite_indicators import PREFIX, _entity_db, _model

    composite, child, consumer = _target_restricted_composite_fixture()
    all_kpis = [composite, child, consumer]
    db = _entity_db(_model(deployed=False), [all_kpis, []])
    get_kpi_cache().invalidate_model(TEST_MODEL_ID)

    with (
        patch("src.api.kpis.get_tenant_db", async_gen_from(db)),
        patch(
            "src.api.kpis.resolve_effective_persona",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "src.api.kpis._evaluate_expression_via_sql",
            side_effect=_target_restricted_expression_evaluator(
                kpis_mod._COMPILER_UNSUPPORTED,
            ),
        ),
    ):
        response = await client.post(
            f"{PREFIX}/evaluate-batch",
            json={"kpi_ids": [str(k.id) for k in all_kpis]},
        )

    assert response.status_code == 200
    by_id = {row["kpi_id"]: row for row in response.json()["results"]}
    assert by_id[str(child.id)]["value"] == 50.0
    for kpi in (composite, consumer):
        result = by_id[str(kpi.id)]
        assert result["value"] is None
        assert result["row_security_restricted"] is True
        assert result["status_label"] == ROW_SECURITY_RESTRICTED_LABEL


@pytest.mark.asyncio
async def test_batch_cached_target_restricted_composite_restricts_kpi_consumer(client):
    """Cached merged governance must survive the later composite rebuild."""
    from unittest.mock import AsyncMock, patch

    import src.api.kpis as kpis_mod
    from src.kpi_cache import get_kpi_cache
    from .conftest import TEST_MODEL_ID, async_gen_from
    from .test_kpi_composite_indicators import PREFIX, _entity_db, _model

    composite, child, consumer = _target_restricted_composite_fixture()
    db = _entity_db(
        _model(deployed=False),
        [[composite], [child], [composite, consumer], [child]],
    )
    get_kpi_cache().invalidate_model(TEST_MODEL_ID)

    with (
        patch("src.api.kpis.get_tenant_db", async_gen_from(db)),
        patch(
            "src.api.kpis.resolve_effective_persona",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "src.api.kpis._evaluate_expression_via_sql",
            side_effect=_target_restricted_expression_evaluator(
                kpis_mod._COMPILER_UNSUPPORTED,
            ),
        ),
    ):
        warm = await client.post(
            f"{PREFIX}/evaluate-batch",
            json={"kpi_ids": [str(composite.id)]},
        )
        response = await client.post(
            f"{PREFIX}/evaluate-batch",
            json={"kpi_ids": [str(composite.id), str(consumer.id)]},
        )

    assert warm.status_code == 200
    assert warm.json()["results"][0]["row_security_restricted"] is True
    assert response.status_code == 200
    by_id = {row["kpi_id"]: row for row in response.json()["results"]}
    for kpi in (composite, consumer):
        result = by_id[str(kpi.id)]
        assert result["value"] is None
        assert result["row_security_restricted"] is True
        assert result["status_label"] == ROW_SECURITY_RESTRICTED_LABEL


@pytest.mark.asyncio
async def test_single_kpi_reference_to_target_restricted_composite_is_redacted(client):
    """The real resolver must govern a referenced composite before publishing it."""
    from unittest.mock import AsyncMock, patch

    import src.api.kpis as kpis_mod
    from src.kpi_cache import get_kpi_cache
    from .conftest import TEST_MODEL_ID, async_gen_from
    from .test_kpi_composite_indicators import PREFIX, _entity_db, _model

    composite, child, consumer = _target_restricted_composite_fixture()
    db = _entity_db(
        _model(deployed=False), [[composite], [child]], get_kpis=[consumer],
    )
    get_kpi_cache().invalidate_model(TEST_MODEL_ID)

    with (
        patch("src.api.kpis.get_tenant_db", async_gen_from(db)),
        patch("src.api.kpis.resolve_effective_persona", new_callable=AsyncMock, return_value=None),
        patch(
            "src.api.kpis._evaluate_expression_via_sql",
            side_effect=_target_restricted_expression_evaluator(
                kpis_mod._COMPILER_UNSUPPORTED,
            ),
        ),
    ):
        response = await client.post(f"{PREFIX}/{consumer.id}/evaluate")

    assert response.status_code == 200
    result = response.json()
    assert result["value"] is None
    assert result["target"] is None
    assert result["row_security_restricted"] is True
    assert result["status_label"] == ROW_SECURITY_RESTRICTED_LABEL


@pytest.mark.asyncio
async def test_adhoc_kpi_reference_to_target_restricted_composite_is_redacted(client):
    """Ad-hoc fallback uses the same governed composite reference resolver."""
    from unittest.mock import AsyncMock, patch

    import src.api.kpis as kpis_mod
    from .conftest import TEST_PROJECT_ID, async_gen_from
    from .test_kpi_composite_indicators import PREFIX, _entity_db, _model

    composite, child, _ = _target_restricted_composite_fixture()
    model = _model(deployed=False)
    model.project_id = TEST_PROJECT_ID
    db = _entity_db(model, [[composite], [child]])

    with (
        patch("src.api.kpis.get_tenant_db", async_gen_from(db)),
        patch("src.api.kpis.resolve_effective_persona", new_callable=AsyncMock, return_value=None),
        patch(
            "src.api.kpis._evaluate_expression_via_sql",
            side_effect=_target_restricted_expression_evaluator(
                kpis_mod._COMPILER_UNSUPPORTED,
            ),
        ),
    ):
        response = await client.post(
            f"{PREFIX}/evaluate-adhoc",
            json={"expression": 'kpi("Target Restricted Composite")'},
        )

    assert response.status_code == 200
    result = response.json()
    assert result["value"] is None
    assert result["target"] is None
    assert result["row_security_restricted"] is True
    assert result["status_label"] == ROW_SECURITY_RESTRICTED_LABEL


@pytest.mark.asyncio
async def test_single_composite_compiler_fallback_restricts_count_child(client):
    """The real single-composite orchestration must not renormalise a visible
    literal sibling over a deny-all COUNT/COALESCE fallback child."""
    from unittest.mock import AsyncMock, patch

    import src.api.kpis as kpis_mod
    from .conftest import TEST_MODEL_ID, async_gen_from
    from .test_kpi_composite_indicators import (
        PREFIX,
        _ScalarResult,
        _entity_db,
        _model,
    )

    parent, denied, visible = _composite_governance_fixture()
    db = _with_count_measure(
        _entity_db(_model(deployed=False), [[denied, visible]], get_kpis=[parent]),
        _ScalarResult,
        TEST_MODEL_ID,
    )

    with (
        patch("src.api.kpis.get_tenant_db", async_gen_from(db)),
        patch(
            "src.api.kpis.resolve_effective_persona",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "src.api.kpis._evaluate_expression_via_sql",
            side_effect=_compiler_fallback_for(
                denied, visible, kpis_mod._COMPILER_UNSUPPORTED,
            ),
        ),
        patch(
            "src.api.kpis._execute_via_router",
            side_effect=_deny_all_count_router,
        ),
    ):
        response = await client.post(f"{PREFIX}/{parent.id}/evaluate")

    assert response.status_code == 200
    data = response.json()
    assert data["value"] is None
    assert data["row_security_restricted"] is True
    assert data["status_label"] == ROW_SECURITY_RESTRICTED_LABEL
    assert data["composite_status"] == "restricted"


def _child_target_kpi_governance_fixture():
    from .test_kpi_composite_indicators import _kpi

    parent = _kpi(
        name="Child Target Composite",
        kpi_type="composite",
        expression="literal(999)",
    )
    restricted_goal = _kpi(
        name="Restricted Child Goal",
        expression="literal(13)",
        target_value=100.0,
    )
    governed_child = _kpi(
        name="Governed Target Child",
        expression="literal(50)",
        parent_kpi_id=parent.id,
    )
    governed_child.target_type = "expression"
    governed_child.target_expression = (
        'coalesce(kpi("Restricted Child Goal"), literal(0))'
    )
    visible_sibling = _kpi(
        name="Visible Target Sibling",
        expression="literal(40)",
        parent_kpi_id=parent.id,
        target_value=100.0,
    )
    consumer = _kpi(
        name="Child Target Composite Consumer",
        expression='coalesce(kpi("Child Target Composite"), literal(0))',
        target_value=100.0,
    )
    return parent, restricted_goal, governed_child, visible_sibling, consumer


def _child_target_kpi_evaluator(unsupported):
    async def _evaluate(expression, *args, security_sink=None, **kwargs):
        if expression == "literal(999)":
            pytest.fail("a composite placeholder must never be evaluated")
        if expression == "literal(13)":
            if security_sink is not None:
                security_sink.add(ROW_SECURITY_DENY_ALL_RULE_ID)
            return 0.0
        if 'kpi("' in expression:
            return unsupported
        return {"literal(50)": 50.0, "literal(40)": 40.0}.get(expression, 0.0)

    return _evaluate


@pytest.mark.asyncio
@pytest.mark.parametrize("request_kind", ["parent", "consumer"])
async def test_single_composite_child_target_kpi_denial_redacts_parent_and_consumer(
    client,
    request_kind,
):
    """A child target dependency cannot be hidden by coalesce or a sibling."""
    from unittest.mock import AsyncMock, patch

    import src.api.kpis as kpis_mod
    from src.kpi_cache import get_kpi_cache
    from .conftest import TEST_MODEL_ID, async_gen_from
    from .test_kpi_composite_indicators import PREFIX, _entity_db, _model

    parent, goal, child, sibling, consumer = _child_target_kpi_governance_fixture()
    if request_kind == "parent":
        requested = parent
        kpi_results = [[child, sibling], [goal]]
    else:
        requested = consumer
        kpi_results = [[parent], [child, sibling], [goal]]
    db = _entity_db(
        _model(deployed=False),
        kpi_results,
        get_kpis=[requested],
    )
    get_kpi_cache().invalidate_model(TEST_MODEL_ID)

    with (
        patch("src.api.kpis.get_tenant_db", async_gen_from(db)),
        patch("src.api.kpis.resolve_effective_persona", new_callable=AsyncMock, return_value=None),
        patch("src.api.kpis._kpi_outer_cache_allowed", new_callable=AsyncMock, return_value=False),
        patch(
            "src.api.kpis._evaluate_expression_via_sql",
            side_effect=_child_target_kpi_evaluator(kpis_mod._COMPILER_UNSUPPORTED),
        ),
    ):
        response = await client.post(f"{PREFIX}/{requested.id}/evaluate")

    assert response.status_code == 200
    result = response.json()
    assert result["value"] is None
    assert result["target"] is None
    assert result["row_security_restricted"] is True
    assert result["status_label"] == ROW_SECURITY_RESTRICTED_LABEL
    if request_kind == "parent":
        assert result["composite_status"] == "restricted"


@pytest.mark.asyncio
async def test_batch_prefetch_preserves_restriction_and_literal_sibling(client):
    """Batch prefetch must carry deny-all metadata with the cached COUNT value,
    while leaving a literal sibling visible and failing the parent closed."""
    from unittest.mock import AsyncMock, patch

    import src.api.kpis as kpis_mod
    from .conftest import TEST_MODEL_ID, async_gen_from
    from .test_kpi_composite_indicators import (
        PREFIX,
        _ScalarResult,
        _entity_db,
        _model,
    )

    parent, denied, visible = _composite_governance_fixture()
    all_kpis = [parent, denied, visible]
    db = _with_count_measure(
        _entity_db(_model(deployed=False), [all_kpis, [denied, visible]]),
        _ScalarResult,
        TEST_MODEL_ID,
    )

    with (
        patch("src.api.kpis.get_tenant_db", async_gen_from(db)),
        patch(
            "src.api.kpis.resolve_effective_persona",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "src.api.kpis._evaluate_expression_via_sql",
            side_effect=_compiler_fallback_for(
                denied, visible, kpis_mod._COMPILER_UNSUPPORTED,
            ),
        ),
        patch(
            "src.api.kpis._execute_via_router",
            side_effect=_deny_all_count_router,
        ),
    ):
        response = await client.post(
            f"{PREFIX}/evaluate-batch",
            json={"kpi_ids": [str(k.id) for k in all_kpis]},
        )

    assert response.status_code == 200
    by_id = {row["kpi_id"]: row for row in response.json()["results"]}
    denied_result = by_id[str(denied.id)]
    visible_result = by_id[str(visible.id)]
    parent_result = by_id[str(parent.id)]

    assert denied_result["value"] is None
    assert denied_result["row_security_restricted"] is True
    assert visible_result["value"] == 50.0
    assert visible_result["row_security_restricted"] is None
    assert parent_result["value"] is None
    assert parent_result["row_security_restricted"] is True
    assert parent_result["composite_status"] == "restricted"


# ---------------------------------------------------------------------------
# Round-2 deep review, finding 1 — BATCH/SCORECARD composite parity.
#
# The Scorecard renders evaluate-batch, whose composite third pass rebuilds
# parents from ``eval_cache``. The cache must carry the restricted bit into
# ``ChildScore`` so the shared composite engine rejects a partial score and the
# shared response mapper redacts it identically on single and batch paths.
# ---------------------------------------------------------------------------

def test_composite_result_with_any_restricted_child_redacts_parent_response():
    from src.kpi_composite import ChildScore, evaluate_composite

    result = evaluate_composite(
        [
            ChildScore(
                "restricted", "Restricted", None, 100,
                weight=1.0, restricted=True,
            ),
            ChildScore("visible", "Visible", 50, 100, weight=1.0),
        ],
    )
    out = _apply_composite_result(
        _resp(value=50.0, formatted_value="50", status=1), result,
    )

    assert result.status == "restricted"
    assert result.composite_score is None
    assert out.row_security_restricted is True
    assert out.value is None
    assert out.formatted_value is None
    assert out.status is None
    assert out.status_label == ROW_SECURITY_RESTRICTED_LABEL
    assert out.composite_status == "restricted"


def test_composite_result_without_restriction_preserves_real_score():
    from src.kpi_composite import ChildScore, evaluate_composite

    result = evaluate_composite(
        [ChildScore("visible", "Visible", 50, 100, weight=1.0)],
    )
    out = _apply_composite_result(_resp(value=50.0), result)

    assert result.status == "ok"
    assert out.value == 50.0
    assert out.row_security_restricted is None
    assert out.composite_status == "ok"


# ---------------------------------------------------------------------------
# Fresh reviewer follow-up: kpi() references in TARGET expressions.
# ---------------------------------------------------------------------------

def _target_reference_sql(unsupported, *, restrict_target: bool = False):
    async def _evaluate(expression, *args, security_sink=None, **kwargs):
        if expression == 'kpi("Z Target")':
            return unsupported
        if expression == "literal(80)":
            if restrict_target and security_sink is not None:
                security_sink.add(ROW_SECURITY_DENY_ALL_RULE_ID)
            return 80.0
        if expression == "literal(50)":
            return 50.0
        return 0.0

    return _evaluate


@pytest.mark.asyncio
async def test_single_kpi_target_reference_uses_python_fallback(client):
    from unittest.mock import AsyncMock, patch

    import src.api.kpis as kpis_mod
    from .conftest import async_gen_from
    from .test_kpi_composite_indicators import PREFIX, _entity_db, _kpi, _model

    kpi = _kpi(name="Single Target Consumer", expression="literal(50)")
    kpi.target_type = "expression"
    kpi.target_expression = 'kpi("Z Target")'
    db = _entity_db(_model(deployed=False), [], get_kpis=[kpi])

    with (
        patch("src.api.kpis.get_tenant_db", async_gen_from(db)),
        patch("src.api.kpis.resolve_effective_persona", new_callable=AsyncMock, return_value=None),
        patch("src.api.kpis._kpi_outer_cache_allowed", new_callable=AsyncMock, return_value=False),
        patch(
            "src.api.kpis._resolve_referenced_kpi_values",
            new_callable=AsyncMock,
            return_value={"Z Target": 80.0},
        ),
        patch(
            "src.api.kpis._evaluate_expression_via_sql",
            side_effect=_target_reference_sql(kpis_mod._COMPILER_UNSUPPORTED),
        ),
    ):
        response = await client.post(f"{PREFIX}/{kpi.id}/evaluate")

    assert response.status_code == 200
    assert response.json()["value"] == 50.0
    assert response.json()["target"] == 80.0


@pytest.mark.asyncio
async def test_adhoc_kpi_target_reference_propagates_governance(client):
    from unittest.mock import AsyncMock, patch

    import src.api.kpis as kpis_mod
    from .conftest import TEST_PROJECT_ID, async_gen_from
    from .test_kpi_composite_indicators import PREFIX, _entity_db, _model

    model = _model(deployed=False)
    model.project_id = TEST_PROJECT_ID
    db = _entity_db(model, [])

    async def _restricted_reference(*args, security_sink=None, **kwargs):
        if security_sink is not None:
            security_sink.add(ROW_SECURITY_DENY_ALL_RULE_ID)
        return {"Z Target": 80.0}

    with (
        patch("src.api.kpis.get_tenant_db", async_gen_from(db)),
        patch("src.api.kpis.resolve_effective_persona", new_callable=AsyncMock, return_value=None),
        patch(
            "src.api.kpis._resolve_referenced_kpi_values",
            side_effect=_restricted_reference,
        ),
        patch(
            "src.api.kpis._evaluate_expression_via_sql",
            side_effect=_target_reference_sql(kpis_mod._COMPILER_UNSUPPORTED),
        ),
    ):
        response = await client.post(
            f"{PREFIX}/evaluate-adhoc",
            json={
                "expression": "literal(50)",
                "target_expression": 'kpi("Z Target")',
            },
        )

    assert response.status_code == 200
    data = response.json()
    assert data["value"] is None
    assert data["target"] is None
    assert data["row_security_restricted"] is True
    assert data["status_label"] == ROW_SECURITY_RESTRICTED_LABEL


def _nested_target_governance_sql(unsupported):
    """Emulate an unsupported nested target whose leaf is denied by RLS."""
    async def _evaluate(expression, *args, security_sink=None, **kwargs):
        if expression in {'kpi("B Target")', 'kpi("C Restricted")'}:
            return unsupported
        if expression == "literal(90)":
            if security_sink is not None:
                security_sink.add(ROW_SECURITY_DENY_ALL_RULE_ID)
            return 90.0
        if expression == "literal(80)":
            return 80.0
        if expression == "literal(50)":
            return 50.0
        return 0.0

    return _evaluate


def _nested_target_governance_fixture():
    from .test_kpi_composite_indicators import _kpi

    restricted = _kpi(name="C Restricted", expression="literal(90)")
    nested = _kpi(name="B Target", expression="literal(80)")
    nested.target_type = "expression"
    nested.target_expression = 'kpi("C Restricted")'
    consumer = _kpi(name="A Consumer", expression="literal(50)")
    consumer.target_type = "expression"
    consumer.target_expression = 'kpi("B Target")'
    return consumer, nested, restricted


@pytest.mark.asyncio
async def test_single_nested_target_dependency_propagates_row_security(client):
    """A target reference must resolve a referenced KPI's target dependencies."""
    from unittest.mock import AsyncMock, patch

    import src.api.kpis as kpis_mod
    from .conftest import async_gen_from
    from .test_kpi_composite_indicators import PREFIX, _entity_db, _model

    consumer, nested, restricted = _nested_target_governance_fixture()
    db = _entity_db(
        _model(deployed=False), [[nested], [restricted]], get_kpis=[consumer],
    )

    with (
        patch("src.api.kpis.get_tenant_db", async_gen_from(db)),
        patch("src.api.kpis.resolve_effective_persona", new_callable=AsyncMock, return_value=None),
        patch("src.api.kpis._kpi_outer_cache_allowed", new_callable=AsyncMock, return_value=False),
        patch(
            "src.api.kpis._evaluate_expression_via_sql",
            side_effect=_nested_target_governance_sql(kpis_mod._COMPILER_UNSUPPORTED),
        ),
    ):
        response = await client.post(f"{PREFIX}/{consumer.id}/evaluate")

    assert response.status_code == 200
    data = response.json()
    assert data["value"] is None
    assert data["target"] is None
    assert data["row_security_restricted"] is True
    assert data["status_label"] == ROW_SECURITY_RESTRICTED_LABEL


@pytest.mark.asyncio
async def test_adhoc_nested_target_dependency_propagates_row_security(client):
    """Ad-hoc target fallback shares the nested target governance contract."""
    from unittest.mock import AsyncMock, patch

    import src.api.kpis as kpis_mod
    from .conftest import TEST_PROJECT_ID, async_gen_from
    from .test_kpi_composite_indicators import PREFIX, _entity_db, _model

    _, nested, restricted = _nested_target_governance_fixture()
    model = _model(deployed=False)
    model.project_id = TEST_PROJECT_ID
    db = _entity_db(model, [[nested], [restricted]])

    with (
        patch("src.api.kpis.get_tenant_db", async_gen_from(db)),
        patch("src.api.kpis.resolve_effective_persona", new_callable=AsyncMock, return_value=None),
        patch(
            "src.api.kpis._evaluate_expression_via_sql",
            side_effect=_nested_target_governance_sql(kpis_mod._COMPILER_UNSUPPORTED),
        ),
    ):
        response = await client.post(
            f"{PREFIX}/evaluate-adhoc",
            json={
                "expression": "literal(50)",
                "target_expression": 'kpi("B Target")',
            },
        )

    assert response.status_code == 200
    data = response.json()
    assert data["value"] is None
    assert data["target"] is None
    assert data["row_security_restricted"] is True
    assert data["status_label"] == ROW_SECURITY_RESTRICTED_LABEL


def _target_reference_batch_fixture():
    from .test_kpi_composite_indicators import _kpi

    target = _kpi(name="Z Target", expression="literal(80)")
    consumer = _kpi(name="A Consumer", expression="literal(50)")
    consumer.target_type = "expression"
    consumer.target_expression = 'kpi("Z Target")'
    return consumer, target


@pytest.mark.asyncio
async def test_batch_auto_loads_and_orders_target_only_kpi_dependency(client):
    from unittest.mock import AsyncMock, patch

    import src.api.kpis as kpis_mod
    from src.kpi_cache import get_kpi_cache
    from .conftest import TEST_MODEL_ID, async_gen_from
    from .test_kpi_composite_indicators import PREFIX, _entity_db, _model

    consumer, target = _target_reference_batch_fixture()
    db = _entity_db(_model(deployed=False), [[consumer], [target]])
    get_kpi_cache().invalidate_model(TEST_MODEL_ID)

    with (
        patch("src.api.kpis.get_tenant_db", async_gen_from(db)),
        patch("src.api.kpis.resolve_effective_persona", new_callable=AsyncMock, return_value=None),
        patch("src.api.kpis._kpi_outer_cache_allowed", new_callable=AsyncMock, return_value=False),
        patch(
            "src.api.kpis._evaluate_expression_via_sql",
            side_effect=_target_reference_sql(kpis_mod._COMPILER_UNSUPPORTED),
        ),
    ):
        response = await client.post(
            f"{PREFIX}/evaluate-batch",
            json={"kpi_ids": [str(consumer.id)]},
        )

    assert response.status_code == 200
    assert len(response.json()["results"]) == 1
    assert response.json()["results"][0]["value"] == 50.0
    assert response.json()["results"][0]["target"] == 80.0


@pytest.mark.asyncio
async def test_batch_target_kpi_restriction_reaches_consumer(client):
    from unittest.mock import AsyncMock, patch

    import src.api.kpis as kpis_mod
    from src.kpi_cache import get_kpi_cache
    from .conftest import TEST_MODEL_ID, async_gen_from
    from .test_kpi_composite_indicators import PREFIX, _entity_db, _model

    consumer, target = _target_reference_batch_fixture()
    db = _entity_db(_model(deployed=False), [[consumer], [target]])
    get_kpi_cache().invalidate_model(TEST_MODEL_ID)

    with (
        patch("src.api.kpis.get_tenant_db", async_gen_from(db)),
        patch("src.api.kpis.resolve_effective_persona", new_callable=AsyncMock, return_value=None),
        patch("src.api.kpis._kpi_outer_cache_allowed", new_callable=AsyncMock, return_value=False),
        patch(
            "src.api.kpis._evaluate_expression_via_sql",
            side_effect=_target_reference_sql(
                kpis_mod._COMPILER_UNSUPPORTED,
                restrict_target=True,
            ),
        ),
    ):
        response = await client.post(
            f"{PREFIX}/evaluate-batch",
            json={"kpi_ids": [str(consumer.id)]},
        )

    assert response.status_code == 200
    result = response.json()["results"][0]
    assert result["value"] is None
    assert result["target"] is None
    assert result["row_security_restricted"] is True
    assert result["status_label"] == ROW_SECURITY_RESTRICTED_LABEL


@pytest.mark.asyncio
async def test_batch_cached_target_dependency_resolves_for_later_consumer(client):
    from unittest.mock import AsyncMock, patch

    import src.api.kpis as kpis_mod
    from src.kpi_cache import get_kpi_cache
    from .conftest import TEST_MODEL_ID, async_gen_from
    from .test_kpi_composite_indicators import PREFIX, _entity_db, _model

    consumer, target = _target_reference_batch_fixture()
    db = _entity_db(
        _model(deployed=False),
        [[target], [consumer], [target]],
    )
    get_kpi_cache().invalidate_model(TEST_MODEL_ID)

    with (
        patch("src.api.kpis.get_tenant_db", async_gen_from(db)),
        patch("src.api.kpis.resolve_effective_persona", new_callable=AsyncMock, return_value=None),
        patch("src.api.kpis._kpi_outer_cache_allowed", new_callable=AsyncMock, return_value=True),
        patch(
            "src.api.kpis._evaluate_expression_via_sql",
            side_effect=_target_reference_sql(kpis_mod._COMPILER_UNSUPPORTED),
        ),
    ):
        warm = await client.post(
            f"{PREFIX}/evaluate-batch",
            json={"kpi_ids": [str(target.id)]},
        )
        response = await client.post(
            f"{PREFIX}/evaluate-batch",
            json={"kpi_ids": [str(consumer.id)]},
        )

    assert warm.status_code == 200
    assert warm.json()["results"][0]["value"] == 80.0
    assert response.status_code == 200
    result = response.json()["results"][0]
    assert result["value"] == 50.0
    assert result["target"] == 80.0
