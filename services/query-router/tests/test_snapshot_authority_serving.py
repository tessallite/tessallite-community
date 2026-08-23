"""L2 — the deployed snapshot is the SERVING authority, and it fails CLOSED.

Three registry issues (Bug-9200, Bug-9227, Bug-9397) reported three unrelated
symptoms — an undeployed calendar edit moving served numbers, a stale cached
result replaying, a parameter resolving to a draft default. They are one defect
shape, so they get one guard module:

    When a model carries a deploy pointer, the serving-path lookups NAMED BELOW
    resolve from that deployed snapshot; a lookup that cannot be resolved
    REFUSES. It never falls back to live/draft state and never returns a
    permissive default.

The sites this module locks:

  * ``_bind_query_parameters`` — the declared-parameter NAME set and the
    parameter DEFINITIONS, through ``_resolve_parameter_authority``;
  * the ``@``-namespace catalogues — named lists / named sets
    (``load_named_lists``) and Named Query definitions
    (``load_named_queries``), including their deploy-scoped caches;
  * the cached-result freshness gate —
    ``_cached_response_with_current_freshness`` and
    ``_cached_artifact_still_servable``;
  * ``evict_model_cache`` — every deploy-scoped query-router cache;
  * ``/named-objects`` — the deployed ``@``-object catalogue the SPA consumes.

L2-F4 — this is deliberately NOT stated as "every serving-path lookup". It is
not one: the COLUMN-LEVEL-SECURITY closure still walks LIVE ``Measure`` rows on
a deployed model, at ``src/api/routes.py::_kpi_cls_blocked_measure_ids`` and at
``src/routing/router.py``'s measure-lineage CLS check. A deployed measure is
therefore compiled from its DEPLOYED definition but CLS-checked against the
DRAFT — a potential column-security divergence. ``router.py`` is a protected
core engine and the fix belongs to the security track, so it is logged as an
intake item, not closed here. Claiming the universal invariant would be a
guarantee a future reader relies on, and it does not hold.

Each test below names the site it locks and states what it does pre-fix. The
fail-CLOSED half is tested explicitly at every site, because that is the half
that regresses: a fail-open is invisible in the happy path and every reviewer
who meets it is told it "avoids turning a blip into an outage".

Test escape: the parameter sites had unit coverage for the DEPLOYED-and-healthy
path only, so the two authorities' disagreement (snapshot values vs live names)
and the exception window were both unexercised. The cache site's escape was
worse — a test ASSERTED the fail-open. Guard: this module. Tier: T2.
"""
from __future__ import annotations

import ast
import contextlib
import importlib
import types
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from fastapi import HTTPException

from src.api import routes as _routes
from src.params.named_list_resolver import invalidate_named_list_cache
from src.routing.named_query_resolver import invalidate_named_query_cache
from src.semantic import snapshot_resolver as _snap

# The whole point of this module is what the REAL resolver does with a real
# snapshot — including refusing an unusable one — so it opts out of conftest's
# always-succeeds empty shape.
pytestmark = [pytest.mark.unit, pytest.mark.real_snapshot_resolver]

_MODEL_ID = "00000000-0000-0000-0000-0000000000a1"
_VERSION_ID = "00000000-0000-0000-0000-0000000000b1"


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_caches():
    """Every deploy-scoped cache this module touches, before and after.

    Without this the snapshot/named-list caches carry one test's deployment
    into the next and the fail-closed assertions pass for the wrong reason.
    """
    _snap.invalidate()
    _snap.invalidate_live_metadata()
    _snap.reset_request_pins()
    invalidate_named_list_cache()
    invalidate_named_query_cache()
    yield
    _snap.invalidate()
    _snap.invalidate_live_metadata()
    _snap.reset_request_pins()
    invalidate_named_list_cache()
    invalidate_named_query_cache()


def _snapshot(*, parameters=None, named_sets=None) -> dict:
    """A minimally-usable deployed snapshot.

    ``dimensions`` is present so ``_snapshot_has_shape`` accepts it — a
    snapshot carrying ONLY parameters is (correctly) not a usable semantic
    shape and would resolve DEPLOYED_SNAPSHOT_INVALID.
    """
    return {
        "dimensions": [{"id": "d-1", "name": "region"}],
        "model_parameters": parameters or [],
        "named_sets": named_sets or [],
    }


def _db(*, snapshot=None, deployed=True, live_params=(), get_raises=None):
    """An async db whose ``get`` dispatches on the requested ORM class."""
    from shared.db.models import Model, ModelParameter, ModelVersion

    model = types.SimpleNamespace(
        id=uuid.UUID(_MODEL_ID),
        deployed_version_id=uuid.UUID(_VERSION_ID) if deployed else None,
        deploy_epoch=1,
    )
    version = types.SimpleNamespace(
        id=uuid.UUID(_VERSION_ID),
        model_id=uuid.UUID(_MODEL_ID),
        snapshot_json=snapshot,
    )

    async def _get(cls, _ident, *a, **kw):
        if get_raises is not None:
            raise get_raises
        if cls is Model:
            return model
        if cls is ModelVersion:
            return version
        return None

    scalars = MagicMock()
    scalars.all = MagicMock(
        return_value=[
            types.SimpleNamespace(
                name=p, param_type="string", default_value="LIVE", allowed_values=None,
            )
            for p in live_params
        ]
    )
    result = MagicMock()
    result.scalars = MagicMock(return_value=scalars)

    db = AsyncMock()
    db.get = AsyncMock(side_effect=_get)
    db.execute = AsyncMock(return_value=result)
    # Only ModelParameter is ever selected live on this path; assert it here so
    # a future live read of some other table cannot hide behind this stub.
    db._live_orm_class = ModelParameter
    return db


def _body(sql: str):
    return MagicMock(
        raw_query=sql,
        model_id=_MODEL_ID,
        protocol="jdbc",
        session_vars=None,
    )


# ---------------------------------------------------------------------------
# Bug-9397 F-029-01 — one authority for names AND values
# ---------------------------------------------------------------------------

async def test_bug_9397_a_draft_parameter_cannot_400_a_deployed_named_list() -> None:
    """The reported symptom, end to end at the binding seam.

    A modeller adds a DRAFT parameter ``@region`` to a model that already has a
    DEPLOYED named list ``@region``. Nothing is redeployed, so serving is
    unchanged and ``IN (@region)`` must keep expanding the deployed list.

    Pre-fix it raised 400 "matches both a model parameter and a named list":
    the collision check read the LIVE ``ModelParameter`` table while the values
    came from the snapshot, so an UNDEPLOYED edit broke DEPLOYED serving — the
    exact failure deploy pinning exists to prevent, inverted.
    """
    db = _db(
        snapshot=_snapshot(
            parameters=[],                       # deployed: no parameters
            named_sets=[{
                "name": "region",
                "list_type": "sql_fixed",
                "builder_definition": {
                    "type": "fixedMembers",
                    "data_type": "string",
                    "members": ["EMEA", "APAC"],
                },
            }],
        ),
        live_params=["@region"],                 # draft: a colliding parameter
    )
    body = _body("SELECT * FROM sales WHERE region IN (@region)")

    await _routes._bind_query_parameters(body, db, None)

    assert body.raw_query == "SELECT * FROM sales WHERE region IN ('EMEA', 'APAC')"


async def test_bug_9397_a_draft_parameter_is_not_a_declared_name() -> None:
    """The same authority split, seen from the other side.

    A parameter that exists ONLY in the draft must not be treated as declared:
    a query referencing it has to fail as an unknown placeholder, not resolve
    against a definition no deploy ever published.
    """
    db = _db(snapshot=_snapshot(parameters=[]), live_params=["@draft_only"])
    body = _body("SELECT * FROM sales WHERE x = @draft_only")

    with pytest.raises(HTTPException) as exc:
        await _routes._bind_query_parameters(body, db, None)

    assert exc.value.status_code == 400
    assert "@draft_only" in str(exc.value.detail)


async def test_an_undeployed_model_still_reads_its_live_parameters() -> None:
    """The fail-closed rule must not break AUTHORING.

    With no deploy pointer there is no snapshot to be authoritative, so live
    draft rows ARE the authority — the same undeployed fallback the binder and
    the calc-dependency loader use. A fix that refused here would make the
    model builder's preview unusable.
    """
    db = _db(deployed=False, live_params=["@region"])
    body = _body("SELECT * FROM sales WHERE region = @region")

    await _routes._bind_query_parameters(body, db, None)

    assert body.raw_query == "SELECT * FROM sales WHERE region = 'LIVE'"


async def test_l13_persona_at_does_not_reconstruct_bare_dimension_as_parameter() -> None:
    """L13-PERSONA-AT / Q2 Option B: only authored @ keys override params.

    A persona may intentionally carry both ``Region`` (dimension filter) and
    ``@Region`` (parameter override). Re-keying every bare key would make the
    dimension value silently win the parameter channel and violate the saved
    namespace contract.
    """
    db = _db(
        snapshot=_snapshot(
            parameters=[{
                "name": "@Region",
                "param_type": "string",
                "default_value": "GLOBAL",
                "allowed_values": None,
            }],
        ),
    )
    body = _body("SELECT * FROM sales WHERE region = @Region")
    persona = types.SimpleNamespace(
        model_id=uuid.UUID(_MODEL_ID),
        default_filters={"Region": "EMEA", "@Region": "APAC"},
    )
    apply = AsyncMock(return_value="SELECT * FROM sales WHERE region = 'APAC'")

    with (
        patch.object(_routes, "load_persona", new=AsyncMock(return_value=persona)),
        patch.object(_routes, "apply_parameters", new=apply),
    ):
        await _routes._bind_query_parameters(body, db, "persona-1")

    assert apply.await_args.kwargs["persona_filters"] == {"@Region": "APAC"}
    assert body.raw_query == "SELECT * FROM sales WHERE region = 'APAC'"


# ---------------------------------------------------------------------------
# Bug-9397 F-029-03 — the exception window fails CLOSED
# ---------------------------------------------------------------------------

async def test_bug_9397_a_db_fault_refuses_instead_of_using_live_defaults() -> None:
    """A DB fault while classifying the authority must REFUSE.

    Pre-fix the whole lookup sat inside ``except Exception: pass``. A fault in
    ``db.get(Model, ...)`` fired BEFORE the deploy pointer was known, so
    ``_deployed_params`` was still ``None`` — and ``None`` is the sentinel
    meaning "undeployed, read the LIVE ORM". A transient database error on a
    DEPLOYED model therefore served numbers computed from DRAFT parameter
    defaults, silently.

    "We could not read the model" is not "the model is undeployed".
    """
    db = _db(
        snapshot=_snapshot(parameters=[{
            "name": "@region", "param_type": "string",
            "default_value": "DEPLOYED", "allowed_values": None,
        }]),
        live_params=["@region"],
        get_raises=RuntimeError("connection reset"),
    )
    body = _body("SELECT * FROM sales WHERE region = @region")

    with pytest.raises(HTTPException) as exc:
        await _routes._bind_query_parameters(body, db, None)

    assert exc.value.status_code == 503
    # The draft default must not appear anywhere in the outcome.
    assert "LIVE" not in body.raw_query


async def test_bug_9397_an_unusable_deployed_snapshot_refuses() -> None:
    """A deploy pointer with an unreadable snapshot is DEPLOYED_SNAPSHOT_INVALID,
    not UNDEPLOYED. Serving its draft would leak unpublished parameter values to
    a BI client."""
    db = _db(snapshot=None, live_params=["@region"])   # pointer set, no snapshot
    body = _body("SELECT * FROM sales WHERE region = @region")

    with pytest.raises(HTTPException) as exc:
        await _routes._bind_query_parameters(body, db, None)

    assert exc.value.status_code == 503
    assert "LIVE" not in body.raw_query


async def test_the_deployed_default_is_what_gets_bound() -> None:
    """Positive control: the snapshot's value, not the draft's, reaches the SQL.

    Without this the fail-closed tests above could pass on a resolver that
    simply refuses everything.
    """
    db = _db(
        snapshot=_snapshot(parameters=[{
            "name": "@region", "param_type": "string",
            "default_value": "DEPLOYED", "allowed_values": None,
        }]),
        live_params=["@region"],
    )
    body = _body("SELECT * FROM sales WHERE region = @region")

    await _routes._bind_query_parameters(body, db, None)

    assert body.raw_query == "SELECT * FROM sales WHERE region = 'DEPLOYED'"


# ---------------------------------------------------------------------------
# Bug-9239 — the cache must never be a wider door than the matcher
# ---------------------------------------------------------------------------

def _pocket_row(status: str):
    row = types.SimpleNamespace(status=status, retired_at=None)
    result = MagicMock()
    result.one_or_none = MagicMock(return_value=row)
    return result


class _Savepoint:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@pytest.mark.parametrize(
    ("status", "servable"),
    [("fresh", True), ("stale", False), ("building", False)],
)
async def test_bug_9239_cached_pocket_admission_matches_the_matcher(
    status: str, servable: bool,
) -> None:
    """The cache replay admits exactly what ``pocket_matcher`` admits.

    The matcher selects ``PocketDefinition.status == "fresh"``. The cache
    replay accepted ``{"fresh", "stale"}``, so a pocket the scheduler flipped
    to ``stale`` on a failed refresh or a schema drift kept replaying its rows
    for the rest of the cache TTL — while the SAME pocket was refused on a
    miss. ``stale`` is the case that fails pre-fix.
    """
    cached = _routes.ExecuteResponse(
        rows=[{"n": 1}], columns=["n"], route_type="pocket",
        reason="pocket", aggregate_id=None, pocket_id=str(uuid.uuid4()),
        execution_ms=0, bytes_processed=0, rows_returned=1,
    )
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_pocket_row(status))
    db.begin_nested = MagicMock(side_effect=lambda: _Savepoint())

    assert await _routes._cached_artifact_still_servable(
        cached, db, model_id=uuid.uuid4(), route_type="pocket",
    ) is servable


# ---------------------------------------------------------------------------
# Bug-9393 — every deploy-scoped cache is on the eviction list
# ---------------------------------------------------------------------------

async def test_bug_9393_evict_model_cache_covers_the_named_object_caches() -> None:
    """``evict_model_cache`` must clear the named-list and Named-Query caches.

    Both key on the deploy pointer and self-heal on the next post-deploy query,
    so this is residual-window hygiene — but the list is hand-maintained, which
    is exactly how these two came to be the only deploy-scoped query-router
    caches missing from it. Pre-fix both assertions fail.
    """
    calls: list[str] = []
    with (
        patch.object(
            _routes, "invalidate_named_list_cache",
            new=MagicMock(side_effect=lambda mid: calls.append(f"list:{mid}")),
        ),
        patch.object(
            _routes, "invalidate_named_query_cache",
            new=MagicMock(side_effect=lambda mid: calls.append(f"query:{mid}")),
        ),
    ):
        await _routes.evict_model_cache(_MODEL_ID)

    assert f"list:{_MODEL_ID}" in calls, (
        "the deployed named-list cache is not evicted on deploy (Bug-9393)"
    )
    assert f"query:{_MODEL_ID}" in calls, (
        "the deployed Named-Query definition cache is not evicted on deploy"
    )


def test_bug_9393_the_eviction_list_is_derived_from_a_real_invalidator() -> None:
    """Anti-vacuous: the two names the test patches must be the real module-level
    invalidators, not attributes the patch created."""
    from src.params import named_list_resolver as _nl
    from src.routing import named_query_resolver as _nq

    assert _routes.invalidate_named_list_cache is _nl.invalidate_named_list_cache
    assert _routes.invalidate_named_query_cache is _nq.invalidate_named_query_cache


# ---------------------------------------------------------------------------
# L2-F3 — the eviction list is ENUMERATED, not just hand-maintained
# ---------------------------------------------------------------------------
#
# ``evict_model_cache``'s body carried a comment claiming
# ``test_evict_model_cache_covers_every_deploy_scoped_cache`` enumerates the
# list. It did not exist. The list happened to be complete, so nothing was
# broken — but the next contributor to add a deploy-scoped cache would have
# trusted a guard that was never written. This is that guard.
#
# It FAILS CLOSED on a shape it does not recognise (per the coverage-tool
# blind-spot rule): an ``invalidate*`` function whose first parameter is not
# ``model_id`` is neither assumed model-scoped nor silently ignored — it fails
# and demands a classification.

_SRC_ROOT = Path(__file__).resolve().parents[1] / "src"

# Module-level ``invalidate*`` functions that are deliberately NOT model-scoped.
# Every entry needs a reason. An unlisted zero-argument invalidator FAILS this
# test rather than being quietly skipped.
_NON_MODEL_SCOPED_INVALIDATORS: dict[tuple[str, str], str] = {
    ("src.routing.pocket_matcher", "invalidate_parsed_sql_cache"): (
        "keyed by SQL TEXT, not by model — a deploy cannot stale an entry, so "
        "it is correctly absent from evict_model_cache"
    ),
}


def _module_path_to_name(path: Path) -> str:
    return ".".join(path.relative_to(_SRC_ROOT.parent).with_suffix("").parts)


def _discover_invalidators() -> tuple[list[tuple[str, str]], list[str], int]:
    """AST-scan the query-router ``src`` tree for module-level invalidators.

    Returns ``(model_scoped, unrecognised, files_scanned)``. Only MODULE-LEVEL
    definitions count: a nested helper is not part of any module's public
    invalidation surface.
    """
    model_scoped: list[tuple[str, str]] = []
    unrecognised: list[str] = []
    files = sorted(_SRC_ROOT.rglob("*.py"))
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not node.name.startswith("invalidate"):
                continue
            module = _module_path_to_name(path)
            args = list(node.args.posonlyargs) + list(node.args.args)
            key = (module, node.name)
            if args and args[0].arg == "model_id":
                model_scoped.append(key)
            elif not args and not node.args.vararg and (
                key in _NON_MODEL_SCOPED_INVALIDATORS
            ):
                continue
            else:
                unrecognised.append(
                    f"{module}.{node.name}"
                    f"({', '.join(a.arg for a in args)})"
                )
    return model_scoped, unrecognised, len(files)


def test_the_invalidator_scanner_actually_scans() -> None:
    """Anti-blind-spot: a scanner that silently found nothing would make the
    coverage test below vacuously green. Pin a floor on both the files walked
    and the invalidators discovered."""
    model_scoped, _, files_scanned = _discover_invalidators()
    assert files_scanned >= 30, (
        f"the AST scan walked only {files_scanned} files under {_SRC_ROOT} — "
        "the discovery mechanism is broken, not the code it verifies"
    )
    assert len(model_scoped) >= 9, (
        f"only {len(model_scoped)} model-scoped invalidators discovered: "
        f"{model_scoped}. If one was legitimately removed, lower this floor "
        "deliberately; do not let the scanner quietly stop finding them."
    )


def test_every_invalidator_has_a_recognised_shape() -> None:
    """Fail CLOSED. A new ``invalidate*`` function must either take ``model_id``
    first (model-scoped -> must be on the eviction list) or be registered in
    ``_NON_MODEL_SCOPED_INVALIDATORS`` with a reason. Anything else fails here
    rather than being assumed harmless."""
    _, unrecognised, _ = _discover_invalidators()
    assert not unrecognised, (
        "unclassified cache invalidator(s): "
        + ", ".join(unrecognised)
        + ". Either take ``model_id`` as the first parameter and add the call "
        "to routes.evict_model_cache, or register the symbol in "
        "_NON_MODEL_SCOPED_INVALIDATORS with the reason it is not "
        "deploy-scoped."
    )


async def test_evict_model_cache_covers_every_deploy_scoped_cache() -> None:
    """THE enumeration the comment in ``evict_model_cache`` promised.

    Every module-level ``invalidate*(model_id, ...)`` in the query-router ``src``
    tree is patched and ``evict_model_cache`` is run once; each one must have
    been called. Adding a new deploy-scoped cache and forgetting to wire its
    invalidator now fails here instead of serving stale governed content for a
    cache TTL after a deploy.
    """
    model_scoped, unrecognised, _ = _discover_invalidators()
    assert not unrecognised, unrecognised

    mocks: dict[tuple[str, str], MagicMock] = {}
    patches: list[object] = []
    for module_name, fn_name in model_scoped:
        module = importlib.import_module(module_name)
        real = getattr(module, fn_name)
        mock = MagicMock()
        mocks[(module_name, fn_name)] = mock
        patches.append(patch.object(module, fn_name, new=mock))
        # ``routes`` may hold its own module-level binding, imported before any
        # patch on the defining module could reach it. Patch that binding too,
        # but ONLY when it is the same object (never shadow an unrelated name).
        if getattr(_routes, fn_name, None) is real:
            patches.append(patch.object(_routes, fn_name, new=mock))

    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        await _routes.evict_model_cache(_MODEL_ID)

    missing = sorted(
        f"{module}.{fn}" for (module, fn), mock in mocks.items()
        if not mock.called
    )
    assert not missing, (
        "deploy-scoped cache invalidator(s) missing from "
        "routes.evict_model_cache: " + ", ".join(missing)
    )


# ---------------------------------------------------------------------------
# Bug-9219 / Bug-9224 — the deployed @-object catalogue L13 consumes
# ---------------------------------------------------------------------------
#
# Both frontend halves were blocked on the same missing thing: no
# snapshot-authoritative way to ask "which @-objects can a query on this model
# actually resolve?". The SPA's only source was model-service's LIVE draft CRUD,
# which describes the DRAFT model — so a picker built on it offers objects
# queries cannot resolve and hides ones they can. That is the same authority
# split this module exists to close, so the catalogue is served from the
# DEPLOYED SNAPSHOT and refuses when it cannot read one.


def _catalogue_snapshot() -> dict:
    return _snapshot(
        parameters=[
            {"name": "@region", "param_type": "string",
             "default_value": "EMEA", "allowed_values": ["EMEA", "APAC"],
             "display_name": "Region", "description": "Sales region"},
            # No default: the caller MUST supply a value or the query is
            # refused, and the SPA has to know that to mark the field required.
            {"name": "@AsOfDate", "param_type": "date", "default_value": None},
        ],
        named_sets=[
            {"name": "TopRegions", "list_type": "sql_fixed",
             "builder_definition": {"type": "topN", "data_type": "string",
                                    "members": ["LON", "NYC"]}},
            # The acme-demo shape: an MDX EXPRESSION set with no member list.
            {"name": "Top 5 Countries by Revenue", "list_type": "dynamic_top_n",
             "builder_definition": None},
            # A dynamic SQL list that has never been refreshed-and-redeployed.
            {"name": "PendingList", "list_type": "sql_fixed",
             "builder_definition": {"type": "topN", "data_type": "string",
                                    "members": []}},
        ],
    )


async def test_bug_9219_the_catalogue_says_which_named_sets_sql_can_expand() -> None:
    """The machine-readable answer to "@Top5Countries returns 400".

    An MDX-expression set genuinely cannot be expanded by a SQL query — it
    stores ``TopCount(...)``, not a member list — so the honest contract is to
    publish that verdict as a STABLE CODE the client can translate, instead of
    every client rediscovering it from a 400 body.
    """
    db = _db(snapshot=_catalogue_snapshot())

    result = await _routes._build_deployed_named_objects(_MODEL_ID, db)

    by_name = {n.name: n for n in result.named_sets}
    assert by_name["TopRegions"].sql_usable is True
    assert by_name["TopRegions"].member_count == 2
    assert by_name["TopRegions"].unusable_reason is None

    mdx = by_name["Top 5 Countries by Revenue"]
    assert mdx.sql_usable is False
    assert mdx.unusable_reason == _routes.NAMED_OBJECT_MDX_ONLY

    # An unrefreshed dynamic list is a DIFFERENT problem with a different fix
    # (refresh + redeploy), so it gets a different code — collapsing the two
    # would send the user to the wrong remedy.
    pending = by_name["PendingList"]
    assert pending.sql_usable is False
    assert pending.unusable_reason == _routes.NAMED_OBJECT_NO_MEMBERS


async def test_bug_9224_the_catalogue_publishes_the_session_var_override_key() -> None:
    """The SPA could not override a parameter at all; this is what unblocks it.

    ``session_var_key`` is PUBLISHED rather than left for the client to
    assemble: the gateway lower-cases the key (Postgres GUC semantics), so a
    client that built ``app.<AuthoredCase>`` from the parameter name would
    silently never match and the override would appear to do nothing.
    """
    db = _db(snapshot=_catalogue_snapshot())

    result = await _routes._build_deployed_named_objects(_MODEL_ID, db)

    by_name = {p.name: p for p in result.parameters}
    assert set(by_name) == {"region", "AsOfDate"}

    region = by_name["region"]
    assert region.canonical_name == "@region"
    assert region.session_var_key == "app.region"
    assert region.param_type == "string"
    assert region.default_value == "EMEA"
    assert region.allowed_values == ["EMEA", "APAC"]
    assert region.has_default is True

    # A mixed-case name still yields a LOWER-CASED key — the whole point.
    as_of = by_name["AsOfDate"]
    assert as_of.canonical_name == "@AsOfDate"
    assert as_of.session_var_key == "app.asofdate"
    assert as_of.has_default is False


async def test_bug9493_sigil_and_bare_names_remain_distinct_catalogue_identities() -> None:
    """Bug-9493 + R2-PCR-002: ``@Region`` and legacy ``Region`` stay distinct
    and fail closed for overrides.

    Pre-fix code stripped the leading ``@`` for both ``name`` and
    ``session_var_key``, so two persisted parameters became one SPA field with
    one binding key. The catalogue must retain exact identity. Because
    supported SQL can only address ``@Name`` placeholders, both colliding rows
    must be ``sql_usable=false`` — advertising a usable ``app.legacy.*``
    override would be a silent no-op (R2-PCR-002).
    """
    db = _db(snapshot=_snapshot(parameters=[
        {"name": "@Region", "param_type": "string", "default_value": "EMEA",
         "display_name": "Region (sigil)"},
        {"name": "Region", "param_type": "string", "default_value": "APAC",
         "display_name": "Region (legacy)"},
    ]))

    result = await _routes._build_deployed_named_objects(_MODEL_ID, db)

    by_canonical = {p.canonical_name: p for p in result.parameters}
    assert set(by_canonical) == {"@Region", "Region"}
    assert by_canonical["@Region"].name == "Region"
    assert by_canonical["@Region"].session_var_key == "app.region"
    assert by_canonical["Region"].name == "Region"
    assert by_canonical["Region"].session_var_key == "app.legacy.region"
    # Distinct binding keys so identities remain auditable.
    assert len({p.session_var_key for p in result.parameters}) == 2
    # R2-PCR-002: neither colliding row is a usable SQL override.
    assert by_canonical["@Region"].sql_usable is False
    assert by_canonical["Region"].sql_usable is False
    assert by_canonical["@Region"].unusable_reason == _routes.PARAMETER_SIGIL_BARE_COLLISION
    assert by_canonical["Region"].unusable_reason == _routes.PARAMETER_SIGIL_BARE_COLLISION


async def test_r2_pcr_002_non_colliding_parameters_remain_sql_usable() -> None:
    """A lone ``@Region`` (no bare twin) must stay editable."""
    db = _db(snapshot=_snapshot(parameters=[
        {"name": "@Region", "param_type": "string", "default_value": "EMEA"},
    ]))

    result = await _routes._build_deployed_named_objects(_MODEL_ID, db)

    assert len(result.parameters) == 1
    assert result.parameters[0].sql_usable is True
    assert result.parameters[0].unusable_reason is None
    assert result.parameters[0].session_var_key == "app.region"


async def test_the_catalogue_reports_the_deploy_pointer_it_was_built_from() -> None:
    """A client caches the catalogue and refetches when the pointer moves —
    the same key every deploy-scoped cache in this service uses."""
    db = _db(snapshot=_catalogue_snapshot())

    result = await _routes._build_deployed_named_objects(_MODEL_ID, db)

    assert result.deployed_version_id == _VERSION_ID
    assert result.model_id == _MODEL_ID


async def test_an_undeployed_model_has_an_empty_catalogue() -> None:
    """Nothing is resolvable on an undeployed model, which is the truth. It must
    NOT fall back to listing the draft objects — that is the exact mismatch
    that makes a picker offer tokens queries then reject."""
    db = _db(deployed=False)

    result = await _routes._build_deployed_named_objects(_MODEL_ID, db)

    assert result.parameters == []
    assert result.named_sets == []
    assert result.named_queries == []
    assert result.deployed_version_id is None


async def test_an_unreadable_deployment_refuses_rather_than_reporting_empty() -> None:
    """A deployed model whose snapshot cannot be read must 503.

    An empty catalogue here would read to the SPA as "this model has no
    parameters" — a confident wrong answer, which is worse than an outage.
    """
    db = _db(snapshot=None)

    with pytest.raises(HTTPException) as exc:
        await _routes._build_deployed_named_objects(_MODEL_ID, db)

    assert exc.value.status_code == 503


async def test_l2_f12_a_transient_fault_in_a_catalogue_loader_is_a_typed_503() -> None:
    """L2-F12 + L2-F11 in one cell.

    F12: both ``@``-namespace loaders caught only their own typed error, so a
    transient DATABASE fault escaped as an untyped 500 — out of the very route
    whose parameter authority was rewritten one function above (Bug-9397) to
    stop doing exactly that.

    F11: the refusal must not reuse the query-path message. This route carries
    no query, and telling a catalogue caller that "this query's model
    parameters" could not be resolved describes something that does not exist.
    """
    db = _db(snapshot=_snapshot())

    with patch.object(
        _routes, "load_named_lists",
        new=AsyncMock(side_effect=RuntimeError("connection reset")),
    ):
        with pytest.raises(HTTPException) as exc:
            await _routes._build_deployed_named_objects(_MODEL_ID, db)

    assert exc.value.status_code == 503
    assert exc.value.detail == (
        _routes._DEPLOY_AUTHORITY_CATALOGUE_UNAVAILABLE_DETAIL
    )
    assert "this query's" not in str(exc.value.detail)


async def test_l2_f12_a_transient_fault_in_the_named_query_loader_is_a_typed_503() -> None:
    """The same widening on the second loader. Fixing one and not the other is
    the shared-primitive half-fix this repo keeps finding."""
    db = _db(snapshot=_snapshot())

    with patch.object(
        _routes, "load_named_queries",
        new=AsyncMock(side_effect=RuntimeError("connection reset")),
    ):
        with pytest.raises(HTTPException) as exc:
            await _routes._build_deployed_named_objects(_MODEL_ID, db)

    assert exc.value.status_code == 503
    assert exc.value.detail == (
        _routes._DEPLOY_AUTHORITY_CATALOGUE_UNAVAILABLE_DETAIL
    )


async def test_l2_f11_the_authority_503_is_re_flavoured_for_the_catalogue() -> None:
    """The shared ``_resolve_parameter_authority`` 503 reaches this route too.
    Its message names "this query's model parameters"; the catalogue caller
    never sent a query, so the detail must be the catalogue one."""
    db = _db(snapshot=None)

    with pytest.raises(HTTPException) as exc:
        await _routes._build_deployed_named_objects(_MODEL_ID, db)

    assert exc.value.status_code == 503
    assert exc.value.detail == (
        _routes._DEPLOY_AUTHORITY_CATALOGUE_UNAVAILABLE_DETAIL
    )


async def test_the_catalogue_publishes_named_query_columns_as_name_type_pairs() -> None:
    """``output_columns`` is a list of ``{"name", "type"}`` dicts at the
    producer (``shared.schemas.domains.governance_advanced.NamedQueryOutputColumn``),
    NOT a list of strings.

    Found while reviewing this lane's own new endpoint: the first draft did
    ``[str(c) for c in output_columns]``, which publishes the literal text
    ``"{'name': 'branch_id', 'type': 'string'}"`` as a column name — a picker
    built on it would offer that string to the user. Producer/consumer field
    alignment is exactly the rule this violates, so it gets a guard.
    """
    snap = _snapshot()
    snap["named_queries"] = [{
        "id": "11111111-1111-1111-1111-111111111111",
        "name": "leads",
        "definition_sql": "SELECT * FROM acme",
        "shape": "projection",
        "output_columns": [
            {"name": "branch_id", "type": "string"},
            {"name": "amount", "type": "number"},
            # Legacy snapshots may hold a bare string.
            "legacy_col",
            # Unnamed entries are dropped, never published as an empty column.
            {"type": "string"},
        ],
    }]
    db = _db(snapshot=snap)

    result = await _routes._build_deployed_named_objects(_MODEL_ID, db)

    assert len(result.named_queries) == 1
    cols = result.named_queries[0].output_columns
    assert [c.name for c in cols] == ["branch_id", "amount", "legacy_col"]
    assert [c.type for c in cols] == ["string", "number", None]
    assert result.named_queries[0].sql_usable is True


async def test_a_named_query_with_no_definition_is_reported_unusable() -> None:
    """A snapshot row with an empty ``definition_sql`` cannot serve, so the
    catalogue must not advertise it as available."""
    snap = _snapshot()
    snap["named_queries"] = [{
        "id": "22222222-2222-2222-2222-222222222222",
        "name": "broken", "definition_sql": "", "shape": "projection",
    }]
    db = _db(snapshot=snap)

    result = await _routes._build_deployed_named_objects(_MODEL_ID, db)

    assert result.named_queries[0].sql_usable is False
    assert (
        result.named_queries[0].unusable_reason
        == _routes.NAMED_OBJECT_DEFINITION_MISSING
    )


async def test_a_duplicate_named_set_key_refuses_the_catalogue() -> None:
    """The named-list loader fails closed on a duplicate lowercase key
    (Bug-7927). The catalogue must inherit that refusal rather than report a
    partial list — a query against the same model would be refused too, so a
    catalogue that succeeded would disagree with serving.
    """
    db = _db(snapshot=_snapshot(named_sets=[
        {"name": "Region", "list_type": "sql_fixed",
         "builder_definition": {"type": "fixedMembers", "data_type": "string",
                                "members": ["A"]}},
        {"name": "region", "list_type": "sql_fixed",
         "builder_definition": {"type": "fixedMembers", "data_type": "string",
                                "members": ["B"]}},
    ]))

    with pytest.raises(HTTPException) as exc:
        await _routes._build_deployed_named_objects(_MODEL_ID, db)

    assert exc.value.status_code == 503
