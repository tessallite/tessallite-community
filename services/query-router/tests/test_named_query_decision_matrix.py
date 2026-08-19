"""NQ-5 decision-matrix guard — every materialised-vs-live decision cell of
``_handle_named_query_reference`` with KNOWN outcomes.

Bug-9019: the CLS-existence probe selected ``PersonaTagRestriction.id`` — a
column that does NOT exist on the composite-PK association table (its only
columns are ``persona_id`` and ``data_tag_id``) — so statement construction
raised ``AttributeError`` and the bare ``except Exception`` converted it to
``_cls_active = True`` for EVERY persona-scoped caller. The materialised fast
path — the performance deliverable of the Named Query feature — was therefore
DEAD for any persona caller: every such request silently diverted to live.
Fail-closed (results correct via the live fallback), so no leak or wrong
number, but the feature was non-functional.

The column fix (``.id`` -> ``.data_tag_id``, mirroring the two sibling probes
at routes.py:4033 and routes.py:1758) plus the narrowed handler
(``except SQLAlchemyError``) ACTIVATE a production path that had never
executed. This file pins every cell of the serve/live decision, including
cell 7 (persona, no restriction -> materialised), which failed on the
pre-fix code.

Test escape: the whole decision matrix was unguarded; the probe's statement-
construction failure was invisible because the masking handler converted it
into a plausible fail-closed constant, so no test could see that the
materialised path never ran for persona callers.
Guard: this file. Tier: T3 (activates a previously dead production path).
"""
from __future__ import annotations

import contextlib
import types
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import OperationalError

from shared.named_query.population_contract import (
    named_query_population_fingerprint,
)
from src.api import routes as _routes
from src.api.routes import ExecuteRequest, ExecuteResponse
from src.routing.named_query_resolver import NamedQueryDefinition
from src.security import CompiledPredicate, Principal

pytestmark = pytest.mark.unit

_MODEL_ID = uuid.uuid4()
_VERSION = uuid.uuid4()
_PROJECT_ID = uuid.uuid4()
_NQ_ID = uuid.uuid4()
_ARTIFACT_ID = uuid.uuid4()
_TARGET_ID = uuid.uuid4()
_PERSONA_ID = uuid.uuid4()

_PROJECTION_DEF = "SELECT * FROM acme"
_AGGREGATED_DEF = "SELECT branch_id, COUNT(*) AS n FROM acme GROUP BY 1"
_TABLE_REF = 'SELECT * FROM "public"."nq_acme_abc123"'
_PREDICATE = '"branch_id" = \'A\''


# ---------------------------------------------------------------------------
# Row fixtures (same duck-typed style as test_named_query_rls_compile_error.py)
# ---------------------------------------------------------------------------


def _definition(
    shape: str = "projection",
    definition_sql: str = _PROJECTION_DEF,
) -> NamedQueryDefinition:
    return NamedQueryDefinition(
        id=str(_NQ_ID),
        name="leads",
        definition_sql=definition_sql,
        output_columns=[{"name": "*", "type": "string"}],
        shape=shape,
    )


def _model_row() -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=_MODEL_ID,
        slug="acme",
        deployed_version_id=str(_VERSION),
        deploy_epoch=7,
        project_id=_PROJECT_ID,
    )


def _artifact_row(
    *,
    status: str = "fresh",
    built_for_version_id: object = str(_VERSION),
    manifest: dict | None = None,
    definition_sql: str = _PROJECTION_DEF,
    stamp_fingerprint: bool = True,
) -> types.SimpleNamespace:
    """A fresh artifact bound to the deployed version, never overdue, carrying the
    canonical-population contract fingerprint for ``definition_sql`` (NQ-2/Bug-9161
    corrected Phase 1: manifest version + live-build binding + contract
    fingerprint over the EXPANDED deployed definition) so the serve-side
    population gate admits it.

    ``manifest``: only the RLS-projection cell (cell 8) needs the security-proof
    manifest fields; they are merged on top of the population fields here.
    ``stamp_fingerprint=False`` simulates a LEGACY (pre-contract) artifact whose
    manifest predates the population contract -> the serve gate must refuse it.
    """
    _manifest: dict = {
        "manifest_version": 2,
        "build_refresh_run_id": "run-1",
    }
    if stamp_fingerprint:
        _manifest["row_definition_fingerprint"] = (
            named_query_population_fingerprint(
                model_id=_MODEL_ID,
                named_query_id=_NQ_ID,
                deployed_version_id=_VERSION,
                deploy_epoch=7,
                definition_sql=_expanded_definition_sql(definition_sql),
            )
        )
    if manifest:
        _manifest.update(manifest)
    return types.SimpleNamespace(
        id=_ARTIFACT_ID,
        named_query_id=str(_NQ_ID),
        status=status,
        built_for_version_id=built_for_version_id,
        built_for_epoch=7,
        last_refresh_at=datetime.now(timezone.utc),
        row_manifest=_manifest,
        active_refresh_run_id="run-1",
        physical_table_name="nq_acme_abc123",
        target_schema="public",
        target_id=str(_TARGET_ID),
    )


def _expanded_definition_sql(definition_sql: str) -> str:
    """What the handler derives from the deployed snapshot for a definition.

    The matrix's star definition expands to the snapshot's exposed fields;
    every other shape passes through unchanged.
    """
    from shared.named_query.star_expansion import (
        expand_named_query_star_definition,
    )

    return expand_named_query_star_definition(
        definition_sql, _deployed_snapshot(),
    )


def _deployed_snapshot() -> dict:
    """The deployed snapshot the handler's expansion + fingerprint read.

    One exposed dimension (``branch_id``) and one exposed plain measure
    (``amount``), so the matrix's star definition expands to
    ``SELECT "branch_id", "amount" FROM acme``.
    """
    return {
        "named_queries": [
            {
                "id": str(_NQ_ID),
                "name": "leads",
                "definition_sql": _PROJECTION_DEF,
                "shape": "projection",
            },
        ],
        "dimensions": [
            {
                "id": "d-branch",
                "name": "branch_id",
                "source_column_id": "c-branch",
            },
        ],
        "measures": [
            {
                "id": "m-amount",
                "name": "amount",
                "measure_type": "standard",
                "variant_kind": None,
                "source_column_id": "c-amount",
            },
        ],
        "columns": [
            {
                "id": "c-branch",
                "model_table_id": "t-1",
                "column_name": "branch_id",
                "is_hidden": False,
            },
            {
                "id": "c-amount",
                "model_table_id": "t-1",
                "column_name": "amount",
                "is_hidden": False,
            },
        ],
        "tables": [
            {
                "id": "t-1",
                "physical_name": "demo.sales",
                "alias": "sales",
            },
        ],
    }


def _proof_manifest() -> dict:
    """The manifest the pocket §5.1 proof requires: build-bound, v2, with the
    security column materialised case-sensitively."""
    return {
        "build_refresh_run_id": "run-1",
        "manifest_version": 2,
        "columns": [{"logical_name": "branch_id"}],
    }


def _policy_row() -> types.SimpleNamespace:
    # No cron, disabled policy -> never overdue.
    return types.SimpleNamespace(cron_expression=None, is_enabled=False)


def _target_row() -> types.SimpleNamespace:
    return types.SimpleNamespace(id=_TARGET_ID, model_id=_MODEL_ID)


def _persona(
    *,
    default_filters: dict | None = None,
    included_measure_ids: list | None = None,
    included_dimension_ids: list | None = None,
    included_hierarchy_ids: list | None = None,
) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=_PERSONA_ID,
        bypass_row_security=False,
        default_filters=default_filters or {},
        included_measure_ids=included_measure_ids or [],
        included_dimension_ids=included_dimension_ids or [],
        included_hierarchy_ids=included_hierarchy_ids or [],
    )


def _principal() -> Principal:
    return Principal(
        user_identity="analyst@acme-demo.com",
        roles=frozenset({"analyst"}),
    )


def _compiled_rls() -> CompiledPredicate:
    return CompiledPredicate(
        sql_expression=_PREDICATE,
        active_rule_ids=("rule-1",),
        security_dimension_columns=("branch_id",),
        mapping_source_ids=(),
        compile_connector="postgresql",
    )


class _ScalarOne:
    """A fake ``db.execute`` result whose ``scalar_one_or_none`` returns ``row``.

    Bug-8924 antipattern honoured: this is NOT a fake ``_ScalarResult`` and its
    ``scalars()`` never returns ``self`` — it returns a separate result object
    (no ``scalars()`` at all here), so call-order dispatch stays honest.
    """

    def __init__(self, row: object) -> None:
        self._row = row

    def scalar_one_or_none(self) -> object:
        return self._row


class _ScalarsAll:
    """A fake ``db.execute`` result whose ``.scalars().all()`` returns rows
    (the restriction-id queries use ``.scalars().all()``)."""

    def __init__(self, rows: list[object]) -> None:
        self._rows = list(rows)

    def scalars(self) -> "_ScalarsAll":
        return self

    def all(self) -> list[object]:
        return self._rows


class _ClsProbe:
    """A ``db.execute`` result for the PersonaTagRestriction probe.

    ``row`` non-None == the persona HAS a tag restriction (``_cls_active``).
    """

    def __init__(self, row: object | None) -> None:
        self._row = row

    def first(self) -> object | None:
        return self._row


class _ExecuteScript:
    """Deterministic call-order dispatcher for the handler's ``db.execute``.

    Each step is either a result object (returned as-is) or an exception
    instance (raised on that call). The handler's execute order is FIXED per
    decision branch (artifact select, policy select, CLS probe, observation
    Model select), so positional scripting is honest. Any call past the end of
    the script returns ``_ScalarOne(None)``.
    """

    def __init__(self, steps: list[object]) -> None:
        self._steps = list(steps)
        self.calls: list[object] = []

    async def __call__(self, stmt: object) -> object:
        self.calls.append(stmt)
        if len(self.calls) > len(self._steps):
            return _ScalarOne(None)
        step = self._steps[len(self.calls) - 1]
        if isinstance(step, BaseException):
            raise step
        return step


def _make_db(script: _ExecuteScript) -> MagicMock:
    db = MagicMock()
    db.get = AsyncMock(
        side_effect=lambda cls, _id: {
            "Model": _model_row(),
            "DataTarget": _target_row(),
            "ModelVersion": types.SimpleNamespace(
                id=_VERSION,
                model_id=_MODEL_ID,
                snapshot_json=_deployed_snapshot(),
            ),
        }.get(cls.__name__)
    )
    db.execute = MagicMock(side_effect=script)  # plain MagicMock: the
    # handler awaits the returned coroutine; an AsyncMock would not await a
    # callable-INSTANCE side_effect.
    db.execute_script = script
    return db


def _live_response() -> ExecuteResponse:
    return ExecuteResponse(
        rows=[{"branch_id": "A"}],
        columns=["branch_id"],
        route_type="source",
        reason="",
        aggregate_id=None,
        execution_ms=5,
        bytes_processed=100,
        rows_returned=1,
    )


# ---------------------------------------------------------------------------
# Driver — drives the REAL ``_handle_named_query_reference`` once per cell.
# ---------------------------------------------------------------------------


async def _run(
    script: _ExecuteScript,
    *,
    persona: object | None = None,
    principal: Principal | None = None,
    nq: NamedQueryDefinition | None = None,
    compiled_rls: CompiledPredicate | None = None,
    overdue: bool = False,
    proof_holds: bool | None = None,
    real_execute: bool = False,
) -> tuple[ExecuteResponse, _ExecuteScript, dict[str, AsyncMock]]:
    """Drive the REAL ``_handle_named_query_reference`` once per cell.

    ``real_execute=True`` (NQ2C-F1 cells): the live re-dispatch drives the REAL
    ``_handle_execute`` with the REAL persona gate and the REAL CLS check
    (``route_query`` runs for real) — only the execution/observation seams are
    stubbed. The mock of ``_handle_execute`` is what made the F1 regression
    invisible (the persona/CLS 403 on the expanded projection never fired), so
    these cells fail if the live body ever carries an unpermitted field again.
    """
    db = _make_db(script)
    live_response = _live_response()
    exec_mock = AsyncMock(return_value=([{"branch_id": "A"}], 123, ["branch_id"]))
    live_mock = AsyncMock(return_value=live_response)
    record_mock = AsyncMock()
    mocks = {"exec": exec_mock, "live": live_mock, "record": record_mock}
    patches: list[object] = [
        patch.object(
            _routes, "load_named_queries",
            new=AsyncMock(return_value={"@leads": nq or _definition()}),
        ),
        patch.object(
            _routes, "compile_row_security",
            new=AsyncMock(return_value=compiled_rls),
        ),
        patch(
            "shared.aggregate_connection.resolve_source_connection",
            new=AsyncMock(return_value=types.SimpleNamespace()),
        ),
        patch(
            "shared.source_executor.resolve_connector_type",
            new=AsyncMock(return_value="postgresql"),
        ),
        patch.object(
            _routes, "resolve_endpoint_connection",
            new=AsyncMock(
                return_value=types.SimpleNamespace(connection_type="postgresql")
            ),
        ),
        patch.object(_routes, "execute_on_connection", new=exec_mock),
        patch.object(_routes, "record_query_success", new=record_mock),
        # The generation-guard names are imported INSIDE the handler
        # (``from src.routing.named_query_generation_guard import ...``), so
        # they must be patched at their source module, not on ``_routes``.
        patch(
            "src.routing.named_query_generation_guard.assert_named_query_route_admissible",
            new=AsyncMock(return_value=types.SimpleNamespace()),
        ),
        patch(
            "src.routing.named_query_generation_guard.assert_named_query_generation_unchanged",
            new=AsyncMock(),
        ),
        patch(
            "src.routing.named_query_generation_guard.read_named_query_generation",
            new=AsyncMock(return_value=types.SimpleNamespace()),
        ),
    ]
    if real_execute:
        patches.extend(_real_execute_patches(mocks))
    else:
        patches.append(patch.object(_routes, "_handle_execute", new=live_mock))
    if overdue:
        patches += [
            patch(
                "shared.staleness_gate.resolve_overdue_grace_seconds",
                new=MagicMock(return_value=300.0),
            ),
            patch(
                "shared.staleness_gate.artifact_overdue",
                new=MagicMock(return_value=True),
            ),
        ]
    if proof_holds is not None:
        patches.append(
            patch.object(
                _routes, "projection_security_proof_holds",
                new=MagicMock(return_value=proof_holds),
            )
        )
    body = ExecuteRequest(
        model_id=str(_MODEL_ID), raw_query="SELECT * FROM @leads",
    )
    logical_query = types.SimpleNamespace(limit=None)
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        response = await _routes._handle_named_query_reference(
            db, body, logical_query,
            ref_name="leads",
            persona=persona,
            principal=principal,
            user_identity=(
                principal.user_identity if principal else "anon@acme-demo.com"
            ),
            tenant_id="acme-demo",
        )
    return response, script, mocks


def _real_execute_patches(
    mocks: dict[str, AsyncMock],
) -> list[object]:
    """Seams for driving the REAL ``_handle_execute`` on the live re-dispatch.

    The REAL binder binds the narrowed projection against the deployed shape
    (``_load_model`` / ``resolve_deployed_shape`` patched — the same seams the
    physical repro in test_nq2_bug9161_star_expansion.py drives), the REAL
    ``enforce_persona_gate`` and the REAL ``route_query`` (including the REAL
    ``_check_column_restrictions``) run, and only execution/observation/logging
    are stubbed. ``mocks["live_body"]`` captures the body that reached the
    helper, and ``mocks["decision"]`` the routed decision.
    """
    from src.semantic.snapshot_resolver import DeployedShape

    snap = _deployed_snapshot()

    def _row(cls, row: dict):
        valid = {c.name for c in cls.__table__.columns}
        return cls(**{k: v for k, v in row.items() if k in valid})

    from shared.db.models import Dimension, Measure

    dims = [_row(Dimension, d) for d in snap["dimensions"]]
    measures = [_row(Measure, m) for m in snap["measures"]]
    shape = DeployedShape(
        measures=measures,
        dimensions=dims,
        hidden_column_ids=set(),
        physical_columns_all={"branch_id", "amount"},
        physical_columns_visible={"branch_id", "amount"},
        hierarchy_rows=[],
        physical_column_ids={},
        attribute_relationships=[],
        dimensions_by_id={str(d.id): d for d in dims},
        columns_by_id={str(c["id"]): dict(c) for c in snap["columns"]},
        tables_by_id={str(t["id"]): dict(t) for t in snap["tables"]},
        join_rows=[],
        user_defined_attribute_rows=[],
        qualified_column_ids={},
        table_name_ids={},
    )
    model = types.SimpleNamespace(
        id=_MODEL_ID, slug="acme", display_name="acme",
        deployed_version_id=str(_VERSION), deploy_epoch=7,
    )

    async def _shape_loader(*a, **k):
        return shape

    captured: dict = {}

    async def _capture_body(body, db, persona_id):
        captured["raw_query"] = body.raw_query

    async def _fake_observation(*, bound, decision, db, user_identity,
                                tenant_id, persona, client_kind):
        routed_decision = types.SimpleNamespace(
            route_type="source",
            reason="force_route=source set on request; aggregate + pocket matchers bypassed",
            aggregate_id=None,
            pocket_id=None,
            rewritten_query="SELECT 1",
            target_dialect="postgres",
            security_rules_applied=[],
        )
        return ([{"v": 1}], 0, ["v"], None, 5, routed_decision)

    from src.api.routes import PipelineTrace

    _routes._cache.clear()
    mocks["live_body"] = captured
    return [
        patch.object(_routes, "_bind_query_parameters", new=AsyncMock(
            side_effect=_capture_body,
        )),
        patch(
            "src.semantic.binder._load_model",
            new=AsyncMock(return_value=model),
        ),
        patch(
            "src.semantic.binder.resolve_deployed_shape",
            new=AsyncMock(side_effect=_shape_loader),
        ),
        patch.object(
            _routes, "_evaluate_bound_field_compatibility",
            new=AsyncMock(return_value=None),
        ),
        patch.object(
            _routes, "execute_with_observation",
            new=AsyncMock(side_effect=_fake_observation),
        ),
        patch.object(
            _routes, "_build_trace",
            new=AsyncMock(return_value=PipelineTrace()),
        ),
    ]


def _assert_live_real(
    response: ExecuteResponse,
    mocks: dict[str, AsyncMock],
    skip_reason: str,
    expected_raw_query: str,
) -> None:
    """The NQ2C-F1 live assertion: the physical table is never read, the REAL
    ``_handle_execute`` ran (REAL persona gate + REAL CLS check) and did NOT
    403 — the live body carried exactly the persona/CLS-narrowed projection."""
    mocks["exec"].assert_not_awaited()
    assert response.route_type == "source"
    assert response.reason.startswith(
        f"Named Query @leads served live ({skip_reason})"
    )
    assert mocks["live_body"]["raw_query"] == expected_raw_query


def _assert_live(
    response: ExecuteResponse,
    mocks: dict[str, AsyncMock],
    skip_reason: str,
) -> None:
    """The LIVE branch: definition re-dispatched through the ONE central live
    helper as a FRESH canonical ExecuteRequest (expanded deployed definition,
    force_route="source", protocol="jdbc", dialect="postgres",
    include_hidden=False, no session vars / caption dimensions — Bug-9173/F6),
    and the physical table never read."""
    mocks["live"].assert_awaited_once()
    mocks["exec"].assert_not_awaited()
    mocks["record"].assert_not_awaited()
    assert response.route_type == "source"
    assert response.reason == f"Named Query @leads served live ({skip_reason})"
    _live_body = mocks["live"].await_args.args[0]
    assert isinstance(_live_body, ExecuteRequest)
    assert _live_body.force_route == "source"
    assert _live_body.protocol == "jdbc"
    assert _live_body.dialect == "postgres"
    assert _live_body.include_hidden is False
    assert _live_body.session_vars is None
    assert _live_body.caption_dimensions is None
    assert _live_body.model_id == str(_MODEL_ID)


def _assert_materialised(
    response: ExecuteResponse,
    mocks: dict[str, AsyncMock],
    *,
    sql_fragment: str,
    predicate: str | None = None,
) -> str:
    """The MATERIALISED branch: physical table read through the executor."""
    mocks["exec"].assert_awaited_once()
    mocks["live"].assert_not_awaited()
    mocks["record"].assert_awaited_once()
    assert response.route_type == "named_query"
    assert response.reason == "Named Query @leads served materialised (status=fresh)"
    sql: str = mocks["exec"].await_args.args[0]
    assert sql_fragment in sql
    if predicate is not None:
        assert predicate in sql, "compiled RLS predicate must be injected"
    else:
        assert _PREDICATE not in sql
    assert response.routed_sql == sql
    return sql


# ---------------------------------------------------------------------------
# The matrix. Call order in every script: artifact, policy, [CLS probe],
# [observation Model on the materialised branch].
# ---------------------------------------------------------------------------


async def test_cell_1a_no_artifact_serves_live() -> None:
    response, _, mocks = await _run(
        _ExecuteScript([_ScalarOne(None), _ScalarOne(_policy_row())]),
    )
    _assert_live(response, mocks, "no_artifact")


async def test_cell_1b_artifact_not_fresh_serves_live() -> None:
    response, _, mocks = await _run(
        _ExecuteScript(
            [_ScalarOne(_artifact_row(status="building")),
             _ScalarOne(_policy_row())],
        ),
    )
    _assert_live(response, mocks, "no_artifact")


async def test_cell_2_version_gate_failure_serves_live() -> None:
    # The artifact was built for a DIFFERENT model version than the deployed
    # pointer: the rows would not be byte-identical to a re-run of the
    # definition, so the gate fails closed to live.
    response, _, mocks = await _run(
        _ExecuteScript(
            [
                _ScalarOne(
                    _artifact_row(built_for_version_id=str(uuid.uuid4()))
                ),
                _ScalarOne(_policy_row()),
            ],
        ),
    )
    _assert_live(response, mocks, "version_gate")


async def test_cell_3_overdue_artifact_serves_live() -> None:
    response, _, mocks = await _run(
        _ExecuteScript(
            [_ScalarOne(_artifact_row()), _ScalarOne(_policy_row())],
        ),
        overdue=True,
    )
    _assert_live(response, mocks, "overdue")


async def test_cell_4_rls_active_aggregated_shape_serves_live() -> None:
    # A pre-aggregated table cannot be row-filtered after the fact without
    # wrong numbers — live fallback re-aggregates under the consumer's filter.
    response, _, mocks = await _run(
        _ExecuteScript(
            [
                _ScalarOne(_artifact_row(definition_sql=_AGGREGATED_DEF)),
                _ScalarOne(_policy_row()),
            ],
        ),
        principal=_principal(),
        compiled_rls=_compiled_rls(),
        nq=_definition(shape="aggregated", definition_sql=_AGGREGATED_DEF),
    )
    _assert_live(response, mocks, "rls_aggregated_live")


async def test_cell_5_cls_restriction_present_serves_live() -> None:
    """The R1-critical cell, now through the REAL gates (NQ2C-F1): a persona
    WITH a tag restriction must keep ``_cls_active`` True (the probe returned a
    row) and serve LIVE — CLS column projection of a shared cache is v1
    live-only. The live body is the CLS-NARROWED projection (the restricted
    ``branch_id`` removed), and the REAL ``_handle_execute`` — REAL persona
    gate, REAL ``route_query`` with the REAL ``_check_column_restrictions`` —
    must NOT 403 on it. Before NQ2C-F1 the live body was the FULL expanded
    projection and the real CLS check blocked ``branch_id`` (403); the
    ``_handle_execute`` mock hid that regression. The physical materialised
    table is never read."""
    _tag = uuid.uuid4()
    script = _ExecuteScript(
        [
            _ScalarOne(_artifact_row()),
            _ScalarOne(_policy_row()),
            _ClsProbe(types.SimpleNamespace(data_tag_id=_tag)),
            # Handler-side narrowing: restriction tags -> restricted columns.
            _ScalarsAll([types.SimpleNamespace(data_tag_id=str(_tag))]),
            _ScalarsAll(["c-branch"]),
            # Real route_query CLS check: the same two queries again.
            _ScalarsAll([types.SimpleNamespace(data_tag_id=str(_tag))]),
            _ScalarsAll(["c-branch"]),
            # Dialect resolution: no touched-table rows -> no source ->
            # postgres fallback.
            _ScalarsAll([]),
            _ScalarOne(None),
        ],
    )
    response, executed, mocks = await _run(
        script, persona=_persona(), real_execute=True,
    )
    # Explicit ``_cls_active`` assertion: the probe WAS issued and returned a
    # restriction row -> ``_cls_active`` True -> live.
    assert "persona_tag_restrictions" in str(executed.calls[2])
    # The narrowing's restriction enumeration (calls 3-4) and the REAL
    # route_query CLS check (calls 5-6) must both have run. The trailing
    # dialect-resolution queries are consumed only when the conftest's
    # autouse dialect stub is not active, so their count is not pinned.
    assert "persona_tag_restrictions" in str(executed.calls[3])
    assert "data_tag_columns" in str(executed.calls[4])
    assert "persona_tag_restrictions" in str(executed.calls[5])
    assert "data_tag_columns" in str(executed.calls[6])
    _assert_live_real(
        response, mocks, "cls_or_default_filters_live",
        expected_raw_query='SELECT "amount" FROM acme',
    )


async def test_cell_6_persona_default_filters_serve_live() -> None:
    response, _, mocks = await _run(
        _ExecuteScript(
            [
                _ScalarOne(_artifact_row()),
                _ScalarOne(_policy_row()),
                _ClsProbe(None),
            ],
        ),
        persona=_persona(default_filters={"branch": "NYC"}),
    )
    _assert_live(response, mocks, "cls_or_default_filters_live")


async def test_cell_7_persona_no_restriction_serves_materialised() -> None:
    """THE Bug-9019 cell: a persona with NO tag restriction, no RLS, no
    default filters, fresh artifact, version ok, not overdue -> MATERIALISED.
    This cell was DEAD before the fix (``_cls_active`` was constant-True, so
    every persona caller diverted to ``cls_or_default_filters_live``).

    Explicit ``_cls_active`` assertion, side two: the probe WAS issued and
    returned NO row -> ``_cls_active`` False -> the else-branch serves the
    physical table."""
    script = _ExecuteScript(
        [
            _ScalarOne(_artifact_row()),
            _ScalarOne(_policy_row()),
            _ClsProbe(None),
            _ScalarOne(_model_row()),
        ],
    )
    response, executed, mocks = await _run(script, persona=_persona())
    assert len(executed.calls) == 4
    assert "persona_tag_restrictions" in str(executed.calls[2])
    _assert_materialised(response, mocks, sql_fragment=_TABLE_REF)


async def test_cell_8a_rls_projection_proof_holds_serves_materialised_with_predicate() -> None:
    """RLS active + projection shape + the pocket §5.1 proof holds -> the
    physical table serves WITH the compiled predicate injected per scan."""
    response, _, mocks = await _run(
        _ExecuteScript(
            [
                _ScalarOne(_artifact_row(manifest=_proof_manifest())),
                _ScalarOne(_policy_row()),
                _ScalarOne(_model_row()),
            ],
        ),
        principal=_principal(),
        compiled_rls=_compiled_rls(),
        nq=_definition(shape="projection", definition_sql=_PROJECTION_DEF),
    )
    _assert_materialised(
        response, mocks, sql_fragment=_TABLE_REF, predicate=_PREDICATE,
    )


async def test_r001_cell_8a_materialised_served_when_owners_populated() -> None:
    """R-001 regression at the Named-Query materialised site (routes.py:4873) —
    the SAME single-materialised-scan exposure as the pocket/aggregate sites.

    Under production config ``compile_row_security`` populates
    ``security_column_owners`` with the security column's real SOURCE owner. The
    NQ physical result table (``nq_acme_abc123``, a single ``SELECT *`` scan) is
    never that source owner, so the owner-not-scanned fail-closed guard
    (F-007-05/Bug-8896) would reject the query with a 403. A single materialised
    scan makes a bare ``WHERE <col> = ...`` unambiguous, so the materialised
    table MUST serve WITH the predicate injected, not 403."""
    import dataclasses

    compiled = dataclasses.replace(
        _compiled_rls(),
        security_column_owners=(("branch_id", "source_leads"),),
    )
    response, _, mocks = await _run(
        _ExecuteScript(
            [
                _ScalarOne(_artifact_row(manifest=_proof_manifest())),
                _ScalarOne(_policy_row()),
                _ScalarOne(_model_row()),
            ],
        ),
        principal=_principal(),
        compiled_rls=compiled,
        nq=_definition(shape="projection", definition_sql=_PROJECTION_DEF),
    )
    _assert_materialised(
        response, mocks, sql_fragment=_TABLE_REF, predicate=_PREDICATE,
    )


async def test_cell_8b_rls_projection_proof_fails_serves_live() -> None:
    """Anything unproven -> live fallback (the proof is the gate: a security
    column missing from the manifest must never serve materialised)."""
    response, _, mocks = await _run(
        _ExecuteScript(
            [_ScalarOne(_artifact_row()), _ScalarOne(_policy_row())],
        ),
        principal=_principal(),
        compiled_rls=_compiled_rls(),
        nq=_definition(shape="projection", definition_sql=_PROJECTION_DEF),
        proof_holds=False,
    )
    _assert_live(response, mocks, "security")


async def test_cell_9_cls_probe_db_error_fails_closed_serves_live() -> None:
    """A GENUINE DB/operational error on the probe must still fail closed
    (``_cls_active`` True -> live), never serve the unprojected cache.

    NQ2C-F1 update: the narrowing's restriction enumeration hits the SAME
    DB error (step 4) and fails closed to the typed 403 — no field can be
    proven permitted, and the live CLS check would hit the identical error,
    so the unclassified 500 is converted to the existing OBJECT_NOT_AVAILABLE
    refusal. The materialised cache is never served either way.

    Exercises the narrowed ``except SQLAlchemyError`` branches (no pragma
    exemption needed)."""
    script = _ExecuteScript(
        [
            _ScalarOne(_artifact_row()),
            _ScalarOne(_policy_row()),
            OperationalError("SELECT", {}, Exception("connection refused")),
            OperationalError("SELECT", {}, Exception("connection refused")),
        ],
    )
    with pytest.raises(HTTPException) as exc_info:
        await _run(script, persona=_persona())
    assert exc_info.value.status_code == 403
    assert exc_info.value.detail["error_code"] == "OBJECT_NOT_AVAILABLE"


async def test_cell_10_cls_probe_coding_error_surfaces_not_masked() -> None:
    """Bug-9019 fix 2: a CODING error (AttributeError etc.) must now SURFACE
    instead of being silently converted to a plausible fail-closed constant.

    With the pre-fix ``except Exception`` this AttributeError was swallowed
    into ``_cls_active = True`` and the request silently diverted to live —
    the exact masking that hid the dead materialised path."""
    script = _ExecuteScript(
        [
            _ScalarOne(_artifact_row()),
            _ScalarOne(_policy_row()),
            AttributeError("PersonaTagRestriction has no attribute 'id'"),
        ],
    )
    with pytest.raises(AttributeError, match="no attribute"):
        await _run(script, persona=_persona())


@pytest.mark.parametrize(
    ("allow_list_field", "expected_body"),
    [
        # included_measure_ids only: measures restricted away (the star branch
        # would hide them); the exposed dim stays.
        ("included_measure_ids", 'SELECT "branch_id" FROM acme'),
        # included_dimension_ids allowing the snapshot's dim: the dim stays;
        # the measure is dropped (a projected plain measure binds as a
        # measure-as-dimension and the dimension allow-list's deny branch
        # would 403 it).
        ("included_dimension_ids", 'SELECT "branch_id" FROM acme'),
        # included_hierarchy_ids only: the matrix snapshot's dim carries no
        # hierarchy (a hierarchy-less dim is kept, exactly like enforce_persona),
        # so the body is the full exposure — the cell still proves the REAL
        # gate does not 403 a hierarchy allow-list principal.
        ("included_hierarchy_ids",
         'SELECT "branch_id", "amount" FROM acme'),
    ],
)
async def test_cell_11_persona_allow_list_forces_live(
    allow_list_field: str, expected_body: str,
) -> None:
    """NQ1R1-F1 / Bug-9167 activation guard, now through the REAL gates
    (NQ2C-F1): a persona carrying ANY populated include list must NEVER be
    served from the shared materialised cache — the live branch applies the
    persona gate. The live body is the persona-NARROWED projection, and the
    REAL ``_handle_execute`` (REAL ``enforce_persona_gate``) must NOT 403 on
    it. Before NQ2C-F1 the live body was the FULL expanded projection and the
    real gate's deny branch 403'd on the disallowed field; the ``_handle_execute``
    mock hid that regression. Red before the allow-list term is added to the
    live-forcing branch; the reason label is diagnostics — the physical-table
    non-read is the security contract."""
    steps: list[object] = [
        _ScalarOne(_artifact_row()), _ScalarOne(_policy_row()),
        _ClsProbe(None),
    ]
    if allow_list_field == "included_dimension_ids":
        # enforce_persona_gate resolves excluded hierarchy-level attrs when the
        # dimension allow-list is populated.
        steps.append(_ScalarsAll([]))
    steps += [
        _ScalarsAll([]),   # real route_query CLS probe: no tag restrictions
        _ScalarsAll([]),   # dialect: no touched-table rows
        _ScalarOne(None),  # dialect: no DataSource -> postgres fallback
    ]
    script = _ExecuteScript(steps)
    _allow_value: object = [str(uuid.uuid4())]
    if allow_list_field == "included_dimension_ids":
        _allow_value = ["d-branch"]  # the snapshot's exposed dimension id
    response, _, mocks = await _run(
        script,
        persona=_persona(**{allow_list_field: _allow_value}),
        real_execute=True,
    )
    mocks["exec"].assert_not_awaited()
    assert response.route_type == "source"
    assert "served live" in response.reason
    _assert_live_real(
        response, mocks, "persona_allow_list_live", expected_body,
    )


async def test_cell_11e_allow_list_narrowing_to_nothing_reproduces_the_cls_403() -> None:
    """NQ2C-F1 empty-narrowed case: persona allow-lists that exclude EVERY
    exposed field (the dimension list names nothing on the model, the measure
    list too) must reproduce the EXISTING CLS 403 — ``OBJECT_NOT_AVAILABLE`` /
    "No columns are available" (the router's star-fully-restricted shape) —
    NOT the expansion's ValueError, which would read as a definition error."""
    script = _ExecuteScript([
        _ScalarOne(_artifact_row()), _ScalarOne(_policy_row()),
        _ClsProbe(None),
    ])
    with pytest.raises(HTTPException) as exc_info:
        await _run(
            script,
            persona=_persona(
                included_dimension_ids=[str(uuid.uuid4())],
                included_measure_ids=[str(uuid.uuid4())],
            ),
        )
    assert exc_info.value.status_code == 403
    assert exc_info.value.detail["error_code"] == "OBJECT_NOT_AVAILABLE"
    assert "No columns are available" in exc_info.value.detail["message"]


async def test_cell_11d_allow_list_preempts_rls_projection_proof() -> None:
    """NQ1R2-O1: ordering guard. Same state as cell 8a (RLS active, projection
    shape, pocket 5.1 proof HOLDS -> normally materialised WITH the injected
    predicate) but the persona also carries an allow-list. The allow-list branch
    (routes.py persona_allow_list_live) must sit ABOVE the projection branch: a
    row predicate does not narrow COLUMNS, so serving here would return the shared
    cache's full column set to a persona forbidden to read it. Cell 11 drives no
    RLS and so cannot observe a reordering below the projection branch."""
    script = _ExecuteScript([
        _ScalarOne(_artifact_row(manifest=_proof_manifest())),
        _ScalarOne(_policy_row()),
        _ClsProbe(None),          # persona present -> the CLS probe IS issued
        _ScalarOne(_model_row()),
    ])
    response, _, mocks = await _run(
        script,
        principal=_principal(),
        compiled_rls=_compiled_rls(),
        nq=_definition(shape="projection", definition_sql=_PROJECTION_DEF),
        persona=_persona(included_measure_ids=[str(uuid.uuid4())]),
    )
    _assert_live(response, mocks, "persona_allow_list_live")


async def test_cell_fp_mismatch_legacy_artifact_serves_live() -> None:
    """NQ-2/Bug-9161 population-contract gate: a fresh, version-current artifact
    whose manifest carries NO canonical-population fingerprint -- a LEGACY
    pre-contract artifact, or one built by a superseded compiler -- must be
    refused and fall back to live, no unsafe grandfathering. State is otherwise
    would-be materialised (no persona, no RLS/CLS/filters/allow-list); only the
    missing fingerprint diverts it to live."""
    script = _ExecuteScript(
        [_ScalarOne(_artifact_row(stamp_fingerprint=False)),
         _ScalarOne(_policy_row())],
    )
    response, _, mocks = await _run(script)
    mocks["exec"].assert_not_awaited()
    _assert_live(response, mocks, "population_contract_mismatch")


async def test_cell_fp_wrong_fingerprint_serves_live() -> None:
    """A manifest whose fingerprint does not match the fingerprint derived from
    the deployed snapshot (definition edited, hidden flag flipped, different
    build pointer) is refused -> live, never the stale population."""
    wrong = named_query_population_fingerprint(
        model_id=_MODEL_ID,
        named_query_id=_NQ_ID,
        deployed_version_id=_VERSION,
        deploy_epoch=7,
        definition_sql="SELECT something_else FROM acme",
    )
    script = _ExecuteScript(
        [_ScalarOne(_artifact_row(
            manifest={"row_definition_fingerprint": wrong},
        )), _ScalarOne(_policy_row())],
    )
    response, _, mocks = await _run(script)
    mocks["exec"].assert_not_awaited()
    _assert_live(response, mocks, "population_contract_mismatch")


async def test_live_body_is_the_expanded_deployed_definition() -> None:
    """The live dispatch compiles the EXPANDED deployed definition (star ->
    explicit exposed-field projection), NOT the raw star — the projection that
    makes the canonical source compile join the model's declared relations
    instead of collapsing to the anchor table (the star-collapse gotcha)."""
    script = _ExecuteScript(
        [_ScalarOne(_artifact_row(stamp_fingerprint=False)),
         _ScalarOne(_policy_row())],
    )
    _, _, mocks = await _run(script)
    _live_body = mocks["live"].await_args.args[0]
    assert _live_body.raw_query == _expanded_definition_sql(_PROJECTION_DEF)
    assert "*" not in _live_body.raw_query.replace("acme", "")
