"""Snapshot-authority fail-closed contract tests (Bug-8302 / Bug-8306 / Bug-8303).

These guard the deployed-snapshot-authority invariants that had NO regression
coverage (Bug-8303, the test escape). Each test asserts a business/security
contract that a revert of the fix would break:

Bug-8302 — a DEPLOYED model whose snapshot is corrupt/missing/empty raises
``DeployedSnapshotUnavailableError`` (a ``SemanticBindingError`` subclass), and
EVERY serving surface (``/query`` headless, ``/plugin/execute``, ``/execute``
REST) maps it to HTTP 503 (temporarily unavailable) — NOT 422 (bad query) and
NOT 409 (not deployed). The 503 catch MUST precede the ``SemanticBindingError``
catch (its base class) or the base handler would shadow it back to 422.

Bug-8306 — ``_snapshot_has_shape`` fails a snapshot closed when it carries
``columns`` but no ``tables`` (a malformed/legacy snapshot). Such a snapshot
must resolve to ``DEPLOYED_SNAPSHOT_INVALID`` so the binder 503s BEFORE source
SQL is assembled; otherwise ``_load_model_graph`` would read the LIVE physical
graph into production SQL (a narrowed F-013-01 re-open).

Bug-8303 (F-029-01) — ``resolve_parameters`` pins parameter definitions to the
DEPLOYED snapshot when ``deployed_params`` is supplied, ignoring the live ORM
(so a draft default/rename cannot alter production results before redeploy).

F-003-04 — physical-name binding is collision-poisoned: a physical column name
that backs more than one semantic object resolves to NONE (fail-closed), never
an arbitrary winner (which would risk wrong numbers).

Bug-8301 — admin ``simulate-as`` resolves the effective persona against the
SIMULATED identity's stated roles (persona fidelity) and can NEVER escalate a
simulated non-privileged user to a privileged/visibility-widening persona.
"""
from __future__ import annotations

import sys
import types
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

# Same collection-order guard as test_undeployed_model_http_409.py: another test
# may install stub ``shared.db.session`` / ``shared.schemas.*`` modules into
# ``sys.modules`` without a ``__file__``, which breaks re-import of the real
# modules through ``src.api.routes``. Skip in a polluted process; running this
# file alone still exercises every assertion.
_SHARED_DB_SESSION = sys.modules.get("shared.db.session")
_POLLUTED = _SHARED_DB_SESSION is not None and getattr(
    _SHARED_DB_SESSION, "__file__", None
) is None

pytestmark = pytest.mark.skipif(
    _POLLUTED,
    reason="sys.modules polluted by another test; run this file alone",
)

from src.ir.logical_query import (
    DeployedSnapshotUnavailableError,
    ModelNotDeployedError,
    SemanticBindingError,
)


# ===========================================================================
# Bug-8306 — _snapshot_has_shape / resolve_snapshot_authority fail-closed
# ===========================================================================

def test_snapshot_has_shape_columns_without_tables_fails_closed():
    """Bug-8306: a snapshot with ``columns`` but no ``tables`` is malformed and
    must NOT count as a usable shape — otherwise the binder serves a deployed
    model whose source SQL reads the LIVE physical graph."""
    from src.semantic.snapshot_resolver import _snapshot_has_shape

    assert _snapshot_has_shape({"columns": [{"id": "c1", "column_name": "amt"}]}) is False


def test_snapshot_has_shape_columns_with_tables_is_usable():
    from src.semantic.snapshot_resolver import _snapshot_has_shape

    snap = {
        "tables": [{"id": "t1", "physical_name": "sales"}],
        "columns": [{"id": "c1", "column_name": "amt", "model_table_id": "t1"}],
    }
    assert _snapshot_has_shape(snap) is True


def test_snapshot_has_shape_measures_only_is_usable():
    """A measures/dimensions-only snapshot (no physical columns) is still a
    usable semantic shape — the tightening must NOT regress this."""
    from src.semantic.snapshot_resolver import _snapshot_has_shape

    assert _snapshot_has_shape({"measures": [{"id": "m1", "name": "revenue"}]}) is True
    assert _snapshot_has_shape({"dimensions": [{"id": "d1", "name": "region"}]}) is True


def test_snapshot_has_shape_empty_snapshot_is_not_usable():
    from src.semantic.snapshot_resolver import _snapshot_has_shape

    assert _snapshot_has_shape({}) is False
    assert _snapshot_has_shape({"hierarchies": []}) is False


class _GetDB:
    """Minimal async session whose ``.get(cls, pk)`` returns a scripted object."""

    def __init__(self, version_obj):
        self._version = version_obj

    async def get(self, _cls, _pk):
        return self._version


# NOTE: the query-router ``conftest`` installs an AUTOUSE fixture that patches
# ``resolve_deployed_shape`` to a fixed empty shape for the whole test session,
# so the ``resolve_snapshot_authority`` tests below must patch that name back to
# the REAL implementation to exercise the fail-closed gate. We do that by
# importing the real callable directly from the module's dict is not possible
# (the attribute is replaced), so we reconstruct the real behaviour through the
# real ``_snapshot_has_shape`` + ``_build_shape`` in a thin stand-in. This keeps
# the test asserting the ACTUAL classification wiring, not the conftest stub.


async def _real_resolve_deployed_shape(model, db):
    """Faithful re-implementation of resolve_deployed_shape's decision (sans
    cache) so the authority tests exercise the REAL fail-closed gate rather than
    the conftest's always-non-None stub."""
    from src.semantic.snapshot_resolver import _snapshot_has_shape, _build_shape
    from shared.db.models import ModelVersion

    dvid = getattr(model, "deployed_version_id", None)
    if dvid is None:
        return None
    version = await db.get(ModelVersion, dvid)
    if version is None or not isinstance(getattr(version, "snapshot_json", None), dict):
        return None
    snapshot = version.snapshot_json
    if not _snapshot_has_shape(snapshot):
        return None
    return _build_shape(model.id, snapshot)


@pytest.mark.asyncio
async def test_resolve_snapshot_authority_columns_without_tables_is_invalid():
    """Bug-8306 end-to-end: a DEPLOYED model whose snapshot carries columns but
    no tables classifies as DEPLOYED_SNAPSHOT_INVALID (fail closed, 503) — it
    must NEVER classify as DEPLOYED (which would serve) or UNDEPLOYED (which
    would read live). The gate is the real ``_snapshot_has_shape``."""
    from src.semantic.snapshot_resolver import (
        SnapshotAuthority,
        resolve_snapshot_authority,
    )

    model = types.SimpleNamespace(
        id=uuid.uuid4(), deployed_version_id=uuid.uuid4(), deploy_epoch=0,
    )
    version = types.SimpleNamespace(
        snapshot_json={"columns": [{"id": "c1", "column_name": "amt"}]}
    )
    with patch(
        "src.semantic.snapshot_resolver.resolve_deployed_shape",
        new=_real_resolve_deployed_shape,
    ):
        authority, shape = await resolve_snapshot_authority(model, _GetDB(version))

    assert authority is SnapshotAuthority.DEPLOYED_SNAPSHOT_INVALID
    assert shape is None


@pytest.mark.asyncio
async def test_resolve_snapshot_authority_valid_snapshot_is_deployed():
    from src.semantic.snapshot_resolver import (
        SnapshotAuthority,
        resolve_snapshot_authority,
    )

    model = types.SimpleNamespace(
        id=uuid.uuid4(), deployed_version_id=uuid.uuid4(), deploy_epoch=0,
    )
    version = types.SimpleNamespace(
        snapshot_json={
            "tables": [{"id": "t1", "physical_name": "sales"}],
            "columns": [{"id": "c1", "column_name": "amt", "model_table_id": "t1"}],
            "measures": [{"id": "m1", "name": "revenue"}],
        }
    )
    with patch(
        "src.semantic.snapshot_resolver.resolve_deployed_shape",
        new=_real_resolve_deployed_shape,
    ):
        authority, shape = await resolve_snapshot_authority(model, _GetDB(version))

    assert authority is SnapshotAuthority.DEPLOYED
    assert shape is not None


@pytest.mark.asyncio
async def test_resolve_snapshot_authority_undeployed_reads_live():
    from src.semantic.snapshot_resolver import (
        SnapshotAuthority,
        resolve_snapshot_authority,
    )

    model = types.SimpleNamespace(id=uuid.uuid4(), deployed_version_id=None)
    with patch(
        "src.semantic.snapshot_resolver.resolve_deployed_shape",
        new=_real_resolve_deployed_shape,
    ):
        authority, shape = await resolve_snapshot_authority(model, _GetDB(None))

    assert authority is SnapshotAuthority.UNDEPLOYED
    assert shape is None


# ===========================================================================
# Bug-8302 — 503 mapping on EVERY serving surface
# ===========================================================================

def _fake_tenant_db_gen(db):
    async def _gen(_tenant_id):
        yield db
    return _gen


def _make_current_user():
    from shared.auth.middleware import CurrentUser

    return CurrentUser(
        user_id="u@example.com",
        tenant_id="tenant-1",
        email="u@example.com",
        role="viewer",
    )


@pytest.mark.asyncio
async def test_execute_rest_maps_snapshot_unavailable_to_503():
    """routes.py:_handle_execute — the reference mapping."""
    from src.api.routes import _handle_execute, ExecuteRequest

    db = AsyncMock()
    err = DeployedSnapshotUnavailableError(
        "Model \"x\" is deployed but its deployed snapshot is unavailable"
    )
    req = ExecuteRequest(model_id="model-1", raw_query="SELECT 1", protocol="jdbc")
    with patch("src.api.routes.bind_query_to_model", new=AsyncMock(side_effect=err)):
        with pytest.raises(HTTPException) as exc:
            await _handle_execute(req, db, user_identity="u@example.com")
    assert exc.value.status_code == 503


@pytest.mark.asyncio
async def test_headless_query_maps_snapshot_unavailable_to_503():
    """Bug-8302: headless /query maps DeployedSnapshotUnavailableError -> 503,
    not 422. A revert (removing the 503 catch) would surface the base
    SemanticBindingError handler and return 422 — this asserts 503."""
    import src.api.headless as headless

    m_id, p_id = str(uuid.uuid4()), str(uuid.uuid4())
    body = headless.HeadlessQueryRequest(
        project_id=p_id, model_id=m_id, measures=["revenue"],
    )
    resp = types.SimpleNamespace(headers={})
    db = AsyncMock()
    err = DeployedSnapshotUnavailableError("snapshot unavailable")

    with (
        patch.object(headless, "_check_rate_limit", new=AsyncMock(return_value=100)),
        patch.object(headless, "get_tenant_db", new=_fake_tenant_db_gen(db)),
        patch.object(headless, "load_authorized_model", new=AsyncMock(return_value=None)),
        patch.object(headless, "resolve_execution_persona", new=AsyncMock(return_value=None)),
        patch.object(headless, "bind_query_to_model", new=AsyncMock(side_effect=err)),
    ):
        with pytest.raises(HTTPException) as exc:
            await headless.headless_query(body, resp, current_user=_make_current_user())

    assert exc.value.status_code == 503


@pytest.mark.asyncio
async def test_headless_query_still_maps_plain_binding_error_to_422():
    """Regression guard: a plain SemanticBindingError still maps to 422 so the
    two conditions stay distinguishable on the wire (the 503 catch must not
    swallow ordinary bind failures)."""
    import src.api.headless as headless

    m_id, p_id = str(uuid.uuid4()), str(uuid.uuid4())
    body = headless.HeadlessQueryRequest(
        project_id=p_id, model_id=m_id, measures=["revenue"],
    )
    resp = types.SimpleNamespace(headers={})
    db = AsyncMock()
    err = SemanticBindingError("Unknown column: 'foo'")

    with (
        patch.object(headless, "_check_rate_limit", new=AsyncMock(return_value=100)),
        patch.object(headless, "get_tenant_db", new=_fake_tenant_db_gen(db)),
        patch.object(headless, "load_authorized_model", new=AsyncMock(return_value=None)),
        patch.object(headless, "resolve_execution_persona", new=AsyncMock(return_value=None)),
        patch.object(headless, "bind_query_to_model", new=AsyncMock(side_effect=err)),
    ):
        with pytest.raises(HTTPException) as exc:
            await headless.headless_query(body, resp, current_user=_make_current_user())

    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_headless_query_maps_not_deployed_to_409():
    """The three cases must stay distinct: not-deployed -> 409 (not 503/422)."""
    import src.api.headless as headless

    m_id, p_id = str(uuid.uuid4()), str(uuid.uuid4())
    body = headless.HeadlessQueryRequest(
        project_id=p_id, model_id=m_id, measures=["revenue"],
    )
    resp = types.SimpleNamespace(headers={})
    db = AsyncMock()
    err = ModelNotDeployedError("Model is not deployed")

    with (
        patch.object(headless, "_check_rate_limit", new=AsyncMock(return_value=100)),
        patch.object(headless, "get_tenant_db", new=_fake_tenant_db_gen(db)),
        patch.object(headless, "load_authorized_model", new=AsyncMock(return_value=None)),
        patch.object(headless, "resolve_execution_persona", new=AsyncMock(return_value=None)),
        patch.object(headless, "bind_query_to_model", new=AsyncMock(side_effect=err)),
    ):
        with pytest.raises(HTTPException) as exc:
            await headless.headless_query(body, resp, current_user=_make_current_user())

    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_plugin_execute_maps_snapshot_unavailable_to_503():
    """Bug-8302: /plugin/execute maps DeployedSnapshotUnavailableError -> 503."""
    import src.api.plugin as plugin

    m_id, p_id = str(uuid.uuid4()), str(uuid.uuid4())
    body = plugin.PluginExecuteRequest(
        project_id=p_id, model_id=m_id, measures=["revenue"],
    )
    resp = types.SimpleNamespace(headers={})
    db = AsyncMock()
    err = DeployedSnapshotUnavailableError("snapshot unavailable")

    with (
        patch.object(plugin, "check_rate_limit", new=AsyncMock(return_value=100)),
        patch.object(plugin, "get_tenant_db", new=_fake_tenant_db_gen(db)),
        patch.object(plugin, "load_authorized_model", new=AsyncMock(return_value=None)),
        patch.object(plugin, "resolve_execution_persona", new=AsyncMock(return_value=None)),
        patch.object(plugin, "bind_query_to_model", new=AsyncMock(side_effect=err)),
    ):
        with pytest.raises(HTTPException) as exc:
            await plugin.plugin_execute(body, resp, current_user=_make_current_user())

    assert exc.value.status_code == 503


@pytest.mark.asyncio
async def test_plugin_execute_still_maps_plain_binding_error_to_422():
    import src.api.plugin as plugin

    m_id, p_id = str(uuid.uuid4()), str(uuid.uuid4())
    body = plugin.PluginExecuteRequest(
        project_id=p_id, model_id=m_id, measures=["revenue"],
    )
    resp = types.SimpleNamespace(headers={})
    db = AsyncMock()
    err = SemanticBindingError("Unknown column: 'foo'")

    with (
        patch.object(plugin, "check_rate_limit", new=AsyncMock(return_value=100)),
        patch.object(plugin, "get_tenant_db", new=_fake_tenant_db_gen(db)),
        patch.object(plugin, "load_authorized_model", new=AsyncMock(return_value=None)),
        patch.object(plugin, "resolve_execution_persona", new=AsyncMock(return_value=None)),
        patch.object(plugin, "bind_query_to_model", new=AsyncMock(side_effect=err)),
    ):
        with pytest.raises(HTTPException) as exc:
            await plugin.plugin_execute(body, resp, current_user=_make_current_user())

    assert exc.value.status_code == 422


# ===========================================================================
# Bug-8303 / F-029-01 — deployed_params pins to the snapshot, ignores live ORM
# ===========================================================================

class _LiveParam:
    def __init__(self, name, param_type, default_value=None, allowed_values=None):
        self.name = name
        self.param_type = param_type
        self.default_value = default_value
        self.allowed_values = allowed_values


class _LiveParamDB:
    """Async session stub returning LIVE ORM param rows for any SELECT."""

    def __init__(self, rows):
        self._rows = rows

    async def execute(self, _stmt):
        rows = self._rows

        class _Result:
            def scalars(self_inner):
                class _S:
                    def all(self_s):
                        return rows
                return _S()

        return _Result()


@pytest.mark.asyncio
async def test_deployed_params_pin_supersedes_live_default():
    """F-029-01: when deployed_params carry a default, resolve_parameters uses
    the DEPLOYED default and never the live ORM's (draft) default. A revert
    (reading live) would resolve 'DRAFT' instead of 'DEPLOYED'."""
    from src.params.resolver import resolve_parameters

    live = _LiveParamDB([_LiveParam("@region", "string", default_value="DRAFT")])
    deployed = [{"name": "@region", "param_type": "string", "default_value": "DEPLOYED"}]

    resolved = await resolve_parameters(
        "m1",
        session_vars={},
        persona_filters={},
        db=live,
        deployed_params=deployed,
    )
    assert resolved == {"@region": "DEPLOYED"}


@pytest.mark.asyncio
async def test_deployed_params_ignore_live_draft_addition():
    """A parameter present ONLY in the live draft (not the deployed snapshot)
    must not be resolvable when deployed_params pins the set — a draft addition
    cannot alter production before redeploy."""
    from src.params.resolver import resolve_parameters

    # Live ORM has @newparam with a default; deployed snapshot has only @region.
    live = _LiveParamDB([
        _LiveParam("@region", "string", default_value="DRAFT"),
        _LiveParam("@newparam", "string", default_value="LEAK"),
    ])
    deployed = [{"name": "@region", "param_type": "string", "default_value": "EMEA"}]

    resolved = await resolve_parameters(
        "m1",
        session_vars={},
        persona_filters={},
        db=live,
        deployed_params=deployed,
    )
    assert resolved == {"@region": "EMEA"}
    assert "@newparam" not in resolved


@pytest.mark.asyncio
async def test_no_deployed_params_reads_live_orm():
    """Undeployed model (deployed_params=None) reads the live ORM default —
    the fallback path must stay intact."""
    from src.params.resolver import resolve_parameters

    live = _LiveParamDB([_LiveParam("@region", "string", default_value="LIVEVAL")])
    resolved = await resolve_parameters(
        "m1", session_vars={}, persona_filters={}, db=live, deployed_params=None,
    )
    assert resolved == {"@region": "LIVEVAL"}


# ===========================================================================
# F-003-04 — physical-name binding collision poison (fail-closed)
# ===========================================================================

def test_build_shape_poisons_ambiguous_physical_name():
    """F-003-04: a physical column name that appears on TWO columns (across
    tables) is POISONED in physical_column_ids — it resolves to NO id, so the
    binder falls through to source rather than picking an arbitrary winner
    (wrong-number risk)."""
    from src.semantic.snapshot_resolver import _build_shape

    mid = uuid.uuid4()
    snapshot = {
        "tables": [
            {"id": "t1", "physical_name": "a", "alias": "a"},
            {"id": "t2", "physical_name": "b", "alias": "b"},
        ],
        # Two DIFFERENT columns share the physical name 'code' in different tables.
        "columns": [
            {"id": str(uuid.uuid4()), "column_name": "code", "model_table_id": "t1"},
            {"id": str(uuid.uuid4()), "column_name": "code", "model_table_id": "t2"},
            {"id": str(uuid.uuid4()), "column_name": "amount", "model_table_id": "t1"},
        ],
    }
    shape = _build_shape(mid, snapshot)

    # 'code' is ambiguous -> poisoned (absent from the trusted id map).
    assert "code" not in shape.physical_column_ids
    # 'amount' is unambiguous -> resolvable.
    assert "amount" in shape.physical_column_ids


# ===========================================================================
# Bug-8301 — simulate-as persona fidelity + non-escalation
# ===========================================================================

def test_persona_current_user_uses_simulated_roles_not_admin():
    """Bug-8301: with simulation active, persona resolution runs against the
    SIMULATED user's stated roles — the admin's own role is dropped so the
    simulated user is NOT treated as privileged."""
    from src.api._simulate import (
        persona_current_user_for_principal,
        simulate_headers_present,
    )
    from shared.security import Principal
    from shared.auth.middleware import CurrentUser
    from shared.security.persona_resolver import is_privileged_by_role

    admin = CurrentUser(
        user_id="admin@x.com", tenant_id="t1", email="admin@x.com",
        role="tenant_admin", roles=["tenant_admin"],
    )
    # Admin simulates a plain viewer.
    principal = Principal(user_identity="viewer@x.com", roles=frozenset({"viewer"}))
    assert simulate_headers_present("viewer@x.com", "viewer") is True

    sim_user = persona_current_user_for_principal(admin, principal, simulated=True)

    # The simulated user is a viewer for persona-entitlement purposes, NOT admin.
    assert not is_privileged_by_role(sim_user)
    assert sim_user.email == "viewer@x.com"
    assert "tenant_admin" not in (sim_user.roles or [])


def test_persona_current_user_preserves_privileged_simulated_role():
    """When the admin deliberately simulates ANOTHER privileged user, the
    privileged tier is honoured (fidelity) — but it comes from the SIMULATED
    roles, not inherited from the caller."""
    from src.api._simulate import persona_current_user_for_principal
    from shared.security import Principal
    from shared.auth.middleware import CurrentUser
    from shared.security.persona_resolver import is_privileged_by_role

    admin = CurrentUser(
        user_id="admin@x.com", tenant_id="t1", email="admin@x.com",
        role="system_admin", roles=["system_admin"],
    )
    principal = Principal(user_identity="mod@x.com", roles=frozenset({"modeler"}))
    sim_user = persona_current_user_for_principal(admin, principal, simulated=True)

    assert is_privileged_by_role(sim_user)  # modeler is privileged
    assert "modeler" in sim_user.roles


def test_persona_current_user_no_simulation_returns_real_user():
    """No simulate header -> the REAL caller is used unchanged (its role,
    persona_id/embed-lock all apply). A real non-simulated user must not be
    downgraded to a synthetic viewer."""
    from src.api._simulate import (
        persona_current_user_for_principal,
        simulate_headers_present,
    )
    from shared.security import Principal
    from shared.auth.middleware import CurrentUser

    user = CurrentUser(
        user_id="real@x.com", tenant_id="t1", email="real@x.com",
        role="tenant_admin", roles=["tenant_admin"],
    )
    principal = Principal.from_current_user(user)
    assert simulate_headers_present(None, None, None, None) is False

    out = persona_current_user_for_principal(user, principal, simulated=False)
    assert out is user


def test_simulate_privileged_roles_match_persona_resolver():
    """Bug-8301 (Fable R1 FINDING-1): the privileged-role set used to pick the
    simulated persona tier must be the SAME object the resolver treats as
    privileged, so a simulated identity's entitlement can never drift from a
    real login's. The literal was replaced by a shared import — pin it."""
    from src.api import _simulate
    from shared.security.persona_resolver import PRIVILEGED_ROLES

    assert _simulate._PERSONA_PRIVILEGED_ROLES is PRIVILEGED_ROLES


# ---------------------------------------------------------------------------
# Bug-8301 — ROUTE wiring: /execute + /explain pass the SIMULATED identity into
# persona resolution (producer-fixed-consumer-unwired guard; Fable R1 FINDING-3)
# ---------------------------------------------------------------------------

def _admin_user():
    from shared.auth.middleware import CurrentUser

    return CurrentUser(
        user_id="admin@x.com", tenant_id="t1", email="admin@x.com",
        role="tenant_admin", roles=["tenant_admin"],
    )


@pytest.mark.asyncio
async def test_execute_route_resolves_persona_against_simulated_identity():
    """A revert of routes.py to ``current_user=current_user`` (the historic
    Bug-8301 bug) would resolve persona as the admin — this asserts the persona
    resolver receives the SIMULATED viewer identity when simulate headers are
    set on /execute."""
    import src.api.routes as routes

    body = routes.ExecuteRequest(model_id="model-1", raw_query="SELECT 1", protocol="jdbc")
    request = types.SimpleNamespace(headers={})
    db = AsyncMock()
    captured = {}

    async def _capture_persona(db_, *, current_user, model_id, requested_persona_id):
        captured["email"] = current_user.email
        captured["roles"] = list(current_user.roles or [])
        captured["role"] = current_user.role
        return None

    with (
        patch.object(routes, "get_tenant_db", new=_fake_tenant_db_gen(db)),
        patch.object(routes, "load_authorized_model", new=AsyncMock(return_value=None)),
        patch.object(routes, "resolve_execution_persona", new=_capture_persona),
        patch.object(routes, "_handle_execute", new=AsyncMock(return_value="ok")),
        patch.object(routes, "enforce_model_scope", new=lambda *a, **k: None),
    ):
        out = await routes.execute_query(
            body,
            request=request,
            current_user=_admin_user(),
            x_simulate_principal="viewer@x.com",
            x_simulate_roles="viewer",
            x_simulate_groups=None,
            x_simulate_claims=None,
        )

    assert out == "ok"
    # Persona resolution saw the SIMULATED viewer, NOT the admin.
    assert captured["email"] == "viewer@x.com"
    assert captured["roles"] == ["viewer"]
    assert captured["role"] == "viewer"


@pytest.mark.asyncio
async def test_execute_route_resolves_persona_as_admin_without_simulation():
    """Without simulate headers, /execute must resolve persona as the REAL
    caller (unchanged behaviour) — the synthetic-viewer path must not fire."""
    import src.api.routes as routes

    body = routes.ExecuteRequest(model_id="model-1", raw_query="SELECT 1", protocol="jdbc")
    request = types.SimpleNamespace(headers={})
    db = AsyncMock()
    captured = {}

    async def _capture_persona(db_, *, current_user, model_id, requested_persona_id):
        captured["email"] = current_user.email
        captured["role"] = current_user.role
        return None

    with (
        patch.object(routes, "get_tenant_db", new=_fake_tenant_db_gen(db)),
        patch.object(routes, "load_authorized_model", new=AsyncMock(return_value=None)),
        patch.object(routes, "resolve_execution_persona", new=_capture_persona),
        patch.object(routes, "_handle_execute", new=AsyncMock(return_value="ok")),
        patch.object(routes, "enforce_model_scope", new=lambda *a, **k: None),
    ):
        await routes.execute_query(
            body,
            request=request,
            current_user=_admin_user(),
            x_simulate_principal=None,
            x_simulate_roles=None,
            x_simulate_groups=None,
            x_simulate_claims=None,
        )

    assert captured["email"] == "admin@x.com"
    assert captured["role"] == "tenant_admin"


@pytest.mark.asyncio
async def test_explain_route_resolves_persona_against_simulated_identity():
    """Same wiring guard for /explain."""
    import src.api.routes as routes

    body = routes.ExecuteRequest(model_id="model-1", raw_query="SELECT 1", protocol="jdbc")
    db = AsyncMock()
    captured = {}

    async def _capture_persona(db_, *, current_user, model_id, requested_persona_id):
        captured["email"] = current_user.email
        captured["roles"] = list(current_user.roles or [])
        return None

    with (
        patch.object(routes, "get_tenant_db", new=_fake_tenant_db_gen(db)),
        patch.object(routes, "load_authorized_model", new=AsyncMock(return_value=None)),
        patch.object(routes, "resolve_execution_persona", new=_capture_persona),
        patch.object(routes, "_handle_explain", new=AsyncMock(return_value="ok")),
        patch.object(routes, "enforce_model_scope", new=lambda *a, **k: None),
    ):
        await routes.explain_query(
            body,
            current_user=_admin_user(),
            x_simulate_principal="viewer@x.com",
            x_simulate_roles="viewer",
            x_simulate_groups=None,
            x_simulate_claims=None,
        )

    assert captured["email"] == "viewer@x.com"
    assert captured["roles"] == ["viewer"]


@pytest.mark.asyncio
async def test_headless_query_maps_rewrite_stage_snapshot_unavailable_to_503():
    """Bug-7981 R2 review: DeployedSnapshotUnavailableError raised at the
    REWRITE stage (inside route_query -> rewrite_for_source ->
    _load_model_graph) must map to 503 like the bind-stage catch, not fall
    into the generic ``except ValueError`` -> 422.

    Test escape: the Bug-8302 contract tests only drove the BIND stage; the
    join-graph loader gained a rewrite-stage raise in Bug-7981, which the
    generic ValueError handler mislabelled as a client error. Guard: this
    test. Tier: T1 producer/consumer contract.
    """
    import src.api.headless as headless

    m_id, p_id = str(uuid.uuid4()), str(uuid.uuid4())
    body = headless.HeadlessQueryRequest(
        project_id=p_id, model_id=m_id, measures=["revenue"],
    )
    resp = types.SimpleNamespace(headers={})
    db = AsyncMock()
    bound = types.SimpleNamespace(
        model=types.SimpleNamespace(project_id=p_id, id=m_id),
        deployed_shape=object(),
    )
    err = DeployedSnapshotUnavailableError(
        "The deployed snapshot for this model carries no physical table graph"
    )

    with (
        patch.object(headless, "_check_rate_limit", new=AsyncMock(return_value=100)),
        patch.object(headless, "get_tenant_db", new=_fake_tenant_db_gen(db)),
        patch.object(headless, "load_authorized_model", new=AsyncMock(return_value=None)),
        patch.object(headless, "resolve_execution_persona", new=AsyncMock(return_value=None)),
        patch.object(headless, "bind_query_to_model", new=AsyncMock(return_value=bound)),
        patch.object(headless, "route_query", new=AsyncMock(side_effect=err)),
    ):
        with pytest.raises(HTTPException) as exc:
            await headless.headless_query(body, resp, current_user=_make_current_user())

    assert exc.value.status_code == 503


@pytest.mark.asyncio
async def test_headless_query_rewrite_stage_plain_value_error_still_422():
    """Regression guard: the new 503 catch must not swallow an ordinary
    rewrite-stage ValueError, which stays a 422 client error."""
    import src.api.headless as headless

    m_id, p_id = str(uuid.uuid4()), str(uuid.uuid4())
    body = headless.HeadlessQueryRequest(
        project_id=p_id, model_id=m_id, measures=["revenue"],
    )
    resp = types.SimpleNamespace(headers={})
    db = AsyncMock()
    bound = types.SimpleNamespace(
        model=types.SimpleNamespace(project_id=p_id, id=m_id),
        deployed_shape=object(),
    )

    with (
        patch.object(headless, "_check_rate_limit", new=AsyncMock(return_value=100)),
        patch.object(headless, "get_tenant_db", new=_fake_tenant_db_gen(db)),
        patch.object(headless, "load_authorized_model", new=AsyncMock(return_value=None)),
        patch.object(headless, "resolve_execution_persona", new=AsyncMock(return_value=None)),
        patch.object(headless, "bind_query_to_model", new=AsyncMock(return_value=bound)),
        patch.object(
            headless, "route_query",
            new=AsyncMock(side_effect=ValueError("no route for this query")),
        ),
    ):
        with pytest.raises(HTTPException) as exc:
            await headless.headless_query(body, resp, current_user=_make_current_user())

    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_plugin_execute_maps_rewrite_stage_snapshot_unavailable_to_503():
    """Companion for /plugin/execute - identical except-chain shape."""
    import src.api.plugin as plugin

    m_id, p_id = str(uuid.uuid4()), str(uuid.uuid4())
    body = plugin.PluginExecuteRequest(
        project_id=p_id, model_id=m_id, measures=["revenue"],
    )
    resp = types.SimpleNamespace(headers={})
    db = AsyncMock()
    bound = types.SimpleNamespace(
        model=types.SimpleNamespace(project_id=p_id, id=m_id),
        deployed_shape=object(),
    )
    err = DeployedSnapshotUnavailableError(
        "The deployed snapshot for this model carries no physical table graph"
    )

    with (
        patch.object(plugin, "check_rate_limit", new=AsyncMock(return_value=100)),
        patch.object(plugin, "get_tenant_db", new=_fake_tenant_db_gen(db)),
        patch.object(plugin, "load_authorized_model", new=AsyncMock(return_value=None)),
        patch.object(plugin, "resolve_execution_persona", new=AsyncMock(return_value=None)),
        patch.object(plugin, "bind_query_to_model", new=AsyncMock(return_value=bound)),
        patch.object(plugin, "route_query", new=AsyncMock(side_effect=err)),
    ):
        with pytest.raises(HTTPException) as exc:
            await plugin.plugin_execute(body, resp, current_user=_make_current_user())

    assert exc.value.status_code == 503


@pytest.mark.asyncio
async def test_plugin_execute_rewrite_stage_plain_value_error_still_422():
    """Regression guard for /plugin/execute - see the headless companion."""
    import src.api.plugin as plugin

    m_id, p_id = str(uuid.uuid4()), str(uuid.uuid4())
    body = plugin.PluginExecuteRequest(
        project_id=p_id, model_id=m_id, measures=["revenue"],
    )
    resp = types.SimpleNamespace(headers={})
    db = AsyncMock()
    bound = types.SimpleNamespace(
        model=types.SimpleNamespace(project_id=p_id, id=m_id),
        deployed_shape=object(),
    )

    with (
        patch.object(plugin, "check_rate_limit", new=AsyncMock(return_value=100)),
        patch.object(plugin, "get_tenant_db", new=_fake_tenant_db_gen(db)),
        patch.object(plugin, "load_authorized_model", new=AsyncMock(return_value=None)),
        patch.object(plugin, "resolve_execution_persona", new=AsyncMock(return_value=None)),
        patch.object(plugin, "bind_query_to_model", new=AsyncMock(return_value=bound)),
        patch.object(
            plugin, "route_query",
            new=AsyncMock(side_effect=ValueError("no route for this query")),
        ),
    ):
        with pytest.raises(HTTPException) as exc:
            await plugin.plugin_execute(body, resp, current_user=_make_current_user())

    assert exc.value.status_code == 422


# ===========================================================================
# Bug-8515 — routes.py rewrite-stage 503 mapping (the last unfixed surfaces)
#
# Test escape: the Bug-8302 tests only drove the BIND stage of routes.py, and
# the Bug-7981 rewrite-stage tests above only covered /headless/query and
# /plugin/execute — api/routes.py was skipped at the time because the file
# carried another lane's uncommitted work. Every ``route_query`` caller in
# routes.py therefore still mislabelled a blocked deployment as a 422/502
# client error. Guard: the tests below, one per caller. Tier: T1 contract.
# ===========================================================================

_SNAPSHOT_ERR_TEXT = (
    "The deployed snapshot for this model carries no physical table graph"
)


def _rewrite_stage_bound(model_id: str, fingerprint: str):
    """A bound query good enough to reach the route stage of routes.py."""
    return types.SimpleNamespace(
        model=types.SimpleNamespace(
            id=model_id, project_id="p1", deployed_version_id="v1", slug="m",
        ),
        logical_query=types.SimpleNamespace(
            protocol="jdbc",
            raw_query='SELECT SUM("Revenue") FROM "t"',
            query_fingerprint=fingerprint,
            limit=None,
            grain=[],
        ),
        resolved_measures=[],
        resolved_dimensions=[],
        resolved_filters=[],
        deployed_shape=object(),
    )


async def _drive_execute_route_stage(side_effect):
    """Drive the REAL ``_handle_execute`` down to its route stage."""
    import src.api.routes as routes

    body = routes.ExecuteRequest(
        model_id="model-8515",
        raw_query='SELECT SUM("Revenue") FROM "t"',
        protocol="jdbc",
    )
    # The result cache is module-level and survives across tests; a stale hit
    # would return before the route stage and make the guard assert nothing.
    routes._cache.clear()
    bound = _rewrite_stage_bound("model-8515", "fp-8515")

    with (
        patch.object(routes, "_bind_query_parameters", new=AsyncMock()),
        patch.object(routes, "bind_query_to_model", new=AsyncMock(return_value=bound)),
        patch.object(
            routes, "_evaluate_bound_field_compatibility",
            new=AsyncMock(return_value=None),
        ),
        patch.object(
            routes, "resolve_target_dialect_for_bound",
            new=AsyncMock(return_value="postgres"),
        ),
        patch.object(routes, "compile_row_security", new=AsyncMock(return_value=None)),
        patch.object(routes, "route_query", new=AsyncMock(side_effect=side_effect)),
    ):
        with pytest.raises(HTTPException) as exc:
            await routes._handle_execute(
                body,
                db=AsyncMock(),
                user_identity="u@example.com",
                principal=None,
                tenant_id="tenant-1",
            )
    return exc.value


@pytest.mark.asyncio
async def test_execute_maps_rewrite_stage_snapshot_unavailable_to_503():
    """Bug-8515: /execute is the JDBC/XMLA gateway's own path. A blocked
    deployment surfacing from ``route_query`` must be 503 (deployment
    temporarily unusable, retry/repair), not the 422 the generic ValueError
    handler produced — DeployedSnapshotUnavailableError subclasses ValueError,
    so a revert (deleting the 503 catch) silently returns 'your query is
    invalid' to every BI client."""
    exc = await _drive_execute_route_stage(
        DeployedSnapshotUnavailableError(_SNAPSHOT_ERR_TEXT)
    )
    assert exc.status_code == 503
    assert _SNAPSHOT_ERR_TEXT in str(exc.detail)


@pytest.mark.asyncio
async def test_execute_rewrite_stage_plain_value_error_still_422():
    """The 503 catch must not swallow an ordinary rewrite-stage ValueError."""
    exc = await _drive_execute_route_stage(ValueError("no route for this query"))
    assert exc.status_code == 422


@pytest.mark.asyncio
async def test_execute_rewrite_stage_not_deployed_error_still_422():
    """ModelNotDeployedError raised at the REWRITE stage keeps its historical
    422 here (the 409 mapping is a BIND-stage contract): the new 503 catch must
    be narrow to DeployedSnapshotUnavailableError, not to its base classes."""
    exc = await _drive_execute_route_stage(ModelNotDeployedError("not deployed"))
    assert exc.status_code == 422


@pytest.mark.asyncio
async def test_explain_maps_rewrite_stage_snapshot_unavailable_to_503():
    """Bug-8515 shared-primitive sweep: /explain calls the same ``route_query``
    primitive, so the Explorer's route-plan panel must show 'deployment
    unavailable', not 'invalid query'."""
    import src.api.routes as routes

    body = routes.ExecuteRequest(
        model_id="model-8515e",
        raw_query='SELECT SUM("Revenue") FROM "t"',
        protocol="jdbc",
    )
    bound = _rewrite_stage_bound("model-8515e", "fp-8515e")
    err = DeployedSnapshotUnavailableError(_SNAPSHOT_ERR_TEXT)

    with (
        patch.object(routes, "_bind_query_parameters", new=AsyncMock()),
        patch.object(routes, "bind_query_to_model", new=AsyncMock(return_value=bound)),
        patch.object(
            routes, "_evaluate_bound_field_compatibility",
            new=AsyncMock(return_value=None),
        ),
        patch.object(routes, "route_query", new=AsyncMock(side_effect=err)),
    ):
        with pytest.raises(HTTPException) as exc:
            await routes._handle_explain(body, AsyncMock())

    assert exc.value.status_code == 503


@pytest.mark.asyncio
async def test_explain_rewrite_stage_plain_value_error_still_422():
    """Regression guard for /explain — see the 503 companion above."""
    import src.api.routes as routes

    body = routes.ExecuteRequest(
        model_id="model-8515e",
        raw_query='SELECT SUM("Revenue") FROM "t"',
        protocol="jdbc",
    )
    bound = _rewrite_stage_bound("model-8515e", "fp-8515e")

    with (
        patch.object(routes, "_bind_query_parameters", new=AsyncMock()),
        patch.object(routes, "bind_query_to_model", new=AsyncMock(return_value=bound)),
        patch.object(
            routes, "_evaluate_bound_field_compatibility",
            new=AsyncMock(return_value=None),
        ),
        patch.object(
            routes, "route_query",
            new=AsyncMock(side_effect=ValueError("no route for this query")),
        ),
    ):
        with pytest.raises(HTTPException) as exc:
            await routes._handle_explain(body, AsyncMock())

    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_discover_members_maps_rewrite_stage_snapshot_unavailable_to_503():
    """Bug-8515 shared-primitive sweep: member discovery is an XMLA/Excel
    surface. A 422 there tells Excel the member request was malformed when in
    fact the deployment needs repair."""
    import src.api.routes as routes

    body = routes.DiscoverMembersRequest(
        model_id=str(uuid.uuid4()), dimension_name="Region",
    )
    bound = _rewrite_stage_bound(body.model_id, "fp-8515d")
    bound.resolved_dimensions = [types.SimpleNamespace(name="Region")]
    err = DeployedSnapshotUnavailableError(_SNAPSHOT_ERR_TEXT)

    with (
        patch.object(routes, "bind_query_to_model", new=AsyncMock(return_value=bound)),
        patch.object(
            routes, "_augment_discover_with_display_column",
            new=AsyncMock(return_value=None),
        ),
        patch.object(routes, "route_query", new=AsyncMock(side_effect=err)),
    ):
        with pytest.raises(HTTPException) as exc:
            await routes._handle_discover_members(
                body, AsyncMock(), current_user=_make_current_user(),
            )

    assert exc.value.status_code == 503


@pytest.mark.asyncio
async def test_discover_members_rewrite_stage_plain_value_error_still_422():
    """Regression guard for member discovery — see the 503 companion above."""
    import src.api.routes as routes

    body = routes.DiscoverMembersRequest(
        model_id=str(uuid.uuid4()), dimension_name="Region",
    )
    bound = _rewrite_stage_bound(body.model_id, "fp-8515d")
    bound.resolved_dimensions = [types.SimpleNamespace(name="Region")]

    with (
        patch.object(routes, "bind_query_to_model", new=AsyncMock(return_value=bound)),
        patch.object(
            routes, "_augment_discover_with_display_column",
            new=AsyncMock(return_value=None),
        ),
        patch.object(
            routes, "route_query",
            new=AsyncMock(side_effect=ValueError("no route for this query")),
        ),
    ):
        with pytest.raises(HTTPException) as exc:
            await routes._handle_discover_members(
                body, AsyncMock(), current_user=_make_current_user(),
            )

    assert exc.value.status_code == 422


def _aggregate_fallback_decision():
    from src.ir.logical_query import RouteDecision

    return RouteDecision(
        route_type="aggregate",
        rewritten_query="SELECT 1 FROM agg_t",
        reason="aggregate",
        aggregate_id=str(uuid.uuid4()),
    )


async def _drive_fallback_rerewrite(rewrite_side_effect, failure_log):
    """Drive ``execute_with_observation`` into its missing-table fallback."""
    import src.api.routes as routes

    bound = _rewrite_stage_bound(str(uuid.uuid4()), "fp-8515f")
    with (
        patch.object(routes, "resolve_filter_anchors", new=AsyncMock(return_value={})),
        patch.object(routes, "audit_filters_present"),
        patch.object(
            routes, "execute_routed_query",
            new=AsyncMock(side_effect=Exception('relation "agg_t" does not exist')),
        ),
        patch.object(
            routes, "rewrite_for_source",
            new=AsyncMock(side_effect=rewrite_side_effect),
        ),
        patch.object(routes, "_log_query_failure", new=failure_log),
    ):
        with pytest.raises(HTTPException) as exc:
            await routes.execute_with_observation(
                bound=bound,
                decision=_aggregate_fallback_decision(),
                db=AsyncMock(),
                user_identity="u@example.com",
                tenant_id="tenant-1",
            )
    return exc.value


@pytest.mark.asyncio
async def test_fallback_rerewrite_snapshot_unavailable_maps_to_503_and_logs_typed():
    """Bug-8515: the missing-cache-table fallback re-rewrite goes through the
    same join-graph loader, so it can fail closed on an unusable deployed
    snapshot too. Bug-7808's generic branch reported that as a 502 'could not
    be rewritten for the source connection — check the model configuration',
    which blames the model for a deployment problem. Assert the typed 503 AND
    that the Bug-7808 QueryLog contract still fires, with an accurate
    error_type (Bug-8520)."""
    failure_log = AsyncMock()
    exc = await _drive_fallback_rerewrite(
        DeployedSnapshotUnavailableError(_SNAPSHOT_ERR_TEXT), failure_log,
    )
    assert exc.status_code == 503
    assert _SNAPSHOT_ERR_TEXT in str(exc.detail)
    assert failure_log.await_count == 1
    assert "snapshot_unavailable" in failure_log.await_args.args


@pytest.mark.asyncio
async def test_fallback_rerewrite_other_failure_still_502_routing_error():
    """Regression guard: the new 503 catch must not absorb the ordinary
    Bug-7808 rewrite failure, which stays a 502 logged as routing_error."""
    failure_log = AsyncMock()
    exc = await _drive_fallback_rerewrite(
        TypeError("rewriter blew up"), failure_log,
    )
    assert exc.status_code == 502
    assert "routing_error" in failure_log.await_args.args


@pytest.mark.asyncio
async def test_discover_members_pocket_reroute_snapshot_unavailable_maps_to_503():
    """Bug-8515 shared-primitive sweep: the Bug-8392 pocket-generation recovery
    re-enters ``route_query`` from INSIDE an except handler, so it is a fifth,
    easily-missed caller of the same primitive. Its Bug-7808 branch mapped an
    unusable deployed snapshot to a 502 "Failed to query members"; assert the
    typed 503 and the accurate QueryLog error_type (Bug-8520)."""
    import src.api.routes as routes
    from src.ir.logical_query import RouteDecision
    from src.routing.pocket_generation_guard import PocketGenerationChangedError

    body = routes.DiscoverMembersRequest(
        model_id=str(uuid.uuid4()), dimension_name="Region",
    )
    bound = _rewrite_stage_bound(body.model_id, "fp-8515p")
    bound.resolved_dimensions = [types.SimpleNamespace(name="Region")]
    pocket_decision = RouteDecision(
        route_type="pocket",
        rewritten_query="SELECT DISTINCT region FROM pkt_t",
        reason="pocket",
        pocket_id=str(uuid.uuid4()),
    )
    err = DeployedSnapshotUnavailableError(_SNAPSHOT_ERR_TEXT)
    failure_log = AsyncMock()

    with (
        patch.object(routes, "bind_query_to_model", new=AsyncMock(return_value=bound)),
        patch.object(
            routes, "_augment_discover_with_display_column",
            new=AsyncMock(return_value=None),
        ),
        patch.object(
            routes, "route_query",
            new=AsyncMock(side_effect=[pocket_decision, err]),
        ),
        patch.object(routes, "resolve_filter_anchors", new=AsyncMock(return_value={})),
        patch.object(routes, "audit_filters_present"),
        patch.object(
            routes, "execute_routed_query",
            new=AsyncMock(side_effect=PocketGenerationChangedError("generation moved")),
        ),
        patch.object(routes, "_log_query_failure", new=failure_log),
    ):
        with pytest.raises(HTTPException) as exc:
            await routes._handle_discover_members(
                body, AsyncMock(), current_user=_make_current_user(),
            )

    assert exc.value.status_code == 503
    assert _SNAPSHOT_ERR_TEXT in str(exc.value.detail)
    assert failure_log.await_count == 1
    assert "snapshot_unavailable" in failure_log.await_args.args
