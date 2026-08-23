"""Bug-8384: named-set serving is pinned to the DEPLOYED model snapshot.

Contract under test: for a DEPLOYED model, a served named set's DEFINITION
(expression, builder_definition, name, list_type, ...) comes from the deployed
snapshot while its GOVERNANCE (certification, ownership, replacement) is
overlaid from the live row. A live expression edit is invisible to BI surfaces
until model deploy; a set created since the last deploy is withheld; an invalid
deployed snapshot fails closed rather than falling back to the draft.

Why this matters as a leak and not merely as staleness: a named set's expression
IS query semantics. The gateway inlines it into the MDX it executes
(``xmla_server._inline_named_sets``) and advertises it in MDSCHEMA_SETS, so an
unsaved draft edit silently changed what Excel/Power BI/Tableau computed before
anyone clicked Deploy.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from shared.db.models import Model, ModelVersion, NamedSet
from src.named_set_deploy_resolver import (
    NamedSetSnapshotInvalidError,
    ResolvedNamedSet,
    build_served_named_set,
    resolve_served_named_sets,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)

DEPLOYED_EXPR = "{ TopCount([Customer].[Customer].Members, 10, [Measures].[Revenue]) }"
DRAFT_EXPR = "{ TopCount([Customer].[Customer].Members, 500, [Measures].[Margin]) }"


class _FakeDb:
    """Minimal async ``db.get`` stub keyed by (type name, id)."""

    def __init__(self, objects):
        self._by_key = {(type(o).__name__, str(o.id)): o for o in objects}

    async def get(self, model_cls, obj_id):
        return self._by_key.get((model_cls.__name__, str(obj_id)))


def _live_named_set(ns_id, model_id, **overrides):
    base = dict(
        id=ns_id,
        model_id=model_id,
        name="Top Customers",
        display_name="Top Customers",
        description="The ten biggest accounts.",
        display_folder="Sales",
        scope=1,
        expression=DEPLOYED_EXPR,
        dimensions="Customer",
        builder_definition={"type": "topN", "entity": "Customer", "count": 10},
        list_type="advanced_mdx",
        certification_status="certified",
        owner_user_id="owner@acme.com",
        replacement_id=None,
        created_at=NOW,
        updated_at=NOW,
    )
    base.update(overrides)
    return NamedSet(**base)


def _snapshot_named_set(ns_id, model_id, **overrides):
    """A serialised snapshot row (UUIDs as strings, as the serialiser stores)."""
    d = {
        "id": str(ns_id),
        "model_id": str(model_id),
        "name": "Top Customers",
        "display_name": "Top Customers",
        "description": "The ten biggest accounts.",
        "display_folder": "Sales",
        "scope": 1,
        "expression": DEPLOYED_EXPR,
        "dimensions": "Customer",
        "builder_definition": {"type": "topN", "entity": "Customer", "count": 10},
        "list_type": "advanced_mdx",
        # Governance values present in the snapshot must be IGNORED (live wins).
        "certification_status": "draft",
        "owner_user_id": "stale@acme.com",
    }
    d.update(overrides)
    return d


def _deployed_model(model_id, version_id, epoch=1):
    return Model(id=model_id, deployed_version_id=version_id, deploy_epoch=epoch)


def _version(version_id, model_id, ns_dicts):
    return ModelVersion(
        id=version_id,
        model_id=model_id,
        snapshot_json={
            "schema_version": "1.0",
            "measures": [{"id": str(uuid.uuid4()), "name": "Revenue"}],
            "named_sets": ns_dicts,
        },
    )


@pytest.mark.asyncio
async def test_undeployed_draft_edit_does_not_reach_the_served_definition():
    """THE leak: an in-progress expression edit must not change BI semantics."""
    ns_id, model_id, version_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    # Modeller has edited the set in the builder but has NOT deployed:
    # top 10 by Revenue became top 500 by Margin.
    live = _live_named_set(ns_id, model_id, expression=DRAFT_EXPR)
    snap = _snapshot_named_set(ns_id, model_id)
    model = _deployed_model(model_id, version_id, epoch=4)
    db = _FakeDb([model, _version(version_id, model_id, [snap])])

    resolved, withheld = await resolve_served_named_sets(db, model, [live])

    assert withheld == []
    assert len(resolved) == 1
    assert isinstance(resolved[0], ResolvedNamedSet)
    served = resolved[0].named_set
    assert served.expression == DEPLOYED_EXPR
    assert served.expression != DRAFT_EXPR
    assert resolved[0].deployed_version_id == version_id
    assert resolved[0].deploy_epoch == 4


@pytest.mark.asyncio
async def test_every_definition_field_comes_from_the_snapshot():
    """Not just ``expression`` — a rename or a builder_definition edit leaks too."""
    ns_id, model_id, version_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    live = _live_named_set(
        ns_id, model_id,
        name="Renamed In Draft",
        display_name="Renamed In Draft",
        description="draft description",
        display_folder="Draft Folder",
        scope=2,
        expression=DRAFT_EXPR,
        dimensions="Customer,Product",
        builder_definition={"type": "topN", "entity": "Customer", "count": 500},
        list_type="sql_fixed",
    )
    snap = _snapshot_named_set(ns_id, model_id)
    model = _deployed_model(model_id, version_id)
    db = _FakeDb([model, _version(version_id, model_id, [snap])])

    resolved, _ = await resolve_served_named_sets(db, model, [live])
    served = resolved[0].named_set

    assert served.name == "Top Customers"
    assert served.display_name == "Top Customers"
    assert served.description == "The ten biggest accounts."
    assert served.display_folder == "Sales"
    assert served.scope == 1
    assert served.expression == DEPLOYED_EXPR
    assert served.dimensions == "Customer"
    assert served.builder_definition == {
        "type": "topN", "entity": "Customer", "count": 10,
    }
    assert served.list_type == "advanced_mdx"


@pytest.mark.asyncio
async def test_governance_is_overlaid_from_the_live_row():
    """An admin must be able to deprecate/re-certify without a redeploy.

    The gateway's ``certification_status != "deprecated"`` catalogue filter reads
    this overlay, so sourcing certification from the snapshot would make a
    deprecation invisible until the next model deploy.
    """
    ns_id, model_id, version_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    replacement = uuid.uuid4()
    live = _live_named_set(
        ns_id, model_id,
        certification_status="deprecated",
        owner_user_id="newowner@acme.com",
        replacement_id=replacement,
    )
    snap = _snapshot_named_set(ns_id, model_id)  # snapshot says draft/stale owner
    model = _deployed_model(model_id, version_id)
    db = _FakeDb([model, _version(version_id, model_id, [snap])])

    resolved, _ = await resolve_served_named_sets(db, model, [live])
    served = resolved[0].named_set

    assert served.certification_status == "deprecated"
    assert served.owner_user_id == "newowner@acme.com"
    assert served.replacement_id == replacement
    # Identity always anchors on the live row.
    assert served.id == ns_id
    assert served.model_id == model_id


@pytest.mark.asyncio
async def test_set_created_since_last_deploy_is_withheld():
    ns_id, other_id = uuid.uuid4(), uuid.uuid4()
    model_id, version_id = uuid.uuid4(), uuid.uuid4()
    live = _live_named_set(ns_id, model_id)
    # The snapshot pins a DIFFERENT set, so this one was never deployed.
    snap_other = _snapshot_named_set(other_id, model_id, name="Other")
    model = _deployed_model(model_id, version_id)
    db = _FakeDb([model, _version(version_id, model_id, [snap_other])])

    resolved, withheld = await resolve_served_named_sets(db, model, [live])

    assert resolved == []
    assert withheld == [ns_id]


@pytest.mark.asyncio
async def test_undeployed_model_withholds_every_set_from_serving():
    ns_id, model_id = uuid.uuid4(), uuid.uuid4()
    live = _live_named_set(ns_id, model_id)
    model = Model(id=model_id, deployed_version_id=None, deploy_epoch=0)
    db = _FakeDb([model])

    resolved, withheld = await resolve_served_named_sets(db, model, [live])

    assert resolved == []
    assert withheld == [ns_id]


@pytest.mark.asyncio
async def test_empty_snapshot_fails_closed():
    ns_id, model_id, version_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    live = _live_named_set(ns_id, model_id)
    model = _deployed_model(model_id, version_id)
    empty_version = ModelVersion(
        id=version_id, model_id=model_id, snapshot_json={"schema_version": "1.0"},
    )
    db = _FakeDb([model, empty_version])

    with pytest.raises(NamedSetSnapshotInvalidError):
        await resolve_served_named_sets(db, model, [live])


@pytest.mark.asyncio
async def test_missing_version_row_fails_closed():
    ns_id, model_id, version_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    live = _live_named_set(ns_id, model_id)
    model = _deployed_model(model_id, version_id)
    db = _FakeDb([model])  # version row absent

    with pytest.raises(NamedSetSnapshotInvalidError):
        await resolve_served_named_sets(db, model, [live])


@pytest.mark.asyncio
async def test_version_row_belonging_to_another_model_fails_closed():
    ns_id, model_id, version_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    live = _live_named_set(ns_id, model_id)
    model = _deployed_model(model_id, version_id)
    foreign = _version(version_id, uuid.uuid4(), [_snapshot_named_set(ns_id, model_id)])
    db = _FakeDb([model, foreign])

    with pytest.raises(NamedSetSnapshotInvalidError):
        await resolve_served_named_sets(db, model, [live])


@pytest.mark.asyncio
async def test_batch_partitions_resolved_and_withheld():
    model_id, version_id = uuid.uuid4(), uuid.uuid4()
    kept_id, dropped_id = uuid.uuid4(), uuid.uuid4()
    kept = _live_named_set(kept_id, model_id, name="Kept")
    dropped = _live_named_set(dropped_id, model_id, name="Dropped")
    snap = _snapshot_named_set(kept_id, model_id, name="Kept")
    model = _deployed_model(model_id, version_id, epoch=2)
    db = _FakeDb([model, _version(version_id, model_id, [snap])])

    resolved, withheld = await resolve_served_named_sets(db, model, [kept, dropped])

    assert [r.named_set.name for r in resolved] == ["Kept"]
    assert withheld == [dropped_id]


@pytest.mark.asyncio
async def test_undeployed_rename_cannot_change_served_order():
    """Gateway substitution order is deployed state, not a live draft side effect."""
    model_id, version_id = uuid.uuid4(), uuid.uuid4()
    alpha_id, zulu_id = uuid.uuid4(), uuid.uuid4()
    live_alpha = _live_named_set(alpha_id, model_id, name="Zulu Draft")
    live_zulu = _live_named_set(zulu_id, model_id, name="Alpha Draft")
    snap_alpha = _snapshot_named_set(alpha_id, model_id, name="Alpha Deployed")
    snap_zulu = _snapshot_named_set(zulu_id, model_id, name="Zulu Deployed")
    model = _deployed_model(model_id, version_id)
    db = _FakeDb(
        [model, _version(version_id, model_id, [snap_alpha, snap_zulu])]
    )

    resolved, _ = await resolve_served_named_sets(
        db, model, [live_zulu, live_alpha]
    )

    assert [row.named_set.name for row in resolved] == [
        "Alpha Deployed",
        "Zulu Deployed",
    ]


def test_column_absent_from_a_legacy_snapshot_falls_back_to_live():
    """A snapshot predating a column must not crash serving."""
    ns_id, model_id = uuid.uuid4(), uuid.uuid4()
    live = _live_named_set(ns_id, model_id, list_type="sql_fixed")
    snap = _snapshot_named_set(ns_id, model_id)
    snap.pop("list_type")

    served = build_served_named_set(snap, live)

    assert served.list_type == "sql_fixed"
    assert served.expression == DEPLOYED_EXPR


def test_free_text_definition_field_is_not_coerced_to_a_uuid():
    """Bug-8384: coercion is driven by the ORM column type, not string shape.

    ``dimensions`` is a plain Text column. A value that happens to look like a
    UUID (or an ISO timestamp) must survive the snapshot round trip as a string
    — handing a ``UUID`` object to a ``str``-typed response field is a
    producer/consumer type mismatch that only shows up on the rare row whose
    text matches the shape.
    """
    ns_id, model_id = uuid.uuid4(), uuid.uuid4()
    uuid_shaped = "12345678-1234-1234-1234-123456789abc"
    live = _live_named_set(ns_id, model_id)
    snap = _snapshot_named_set(ns_id, model_id, dimensions=uuid_shaped)

    served = build_served_named_set(snap, live)

    assert served.dimensions == uuid_shaped
    assert isinstance(served.dimensions, str)


def test_uuid_typed_definition_field_is_rehydrated_from_its_string_form():
    """The type-aware coercion must still rebuild genuine UUID columns."""
    ns_id, model_id = uuid.uuid4(), uuid.uuid4()
    live = _live_named_set(ns_id, model_id)
    # ``id`` is a UUID column; the serialiser stores it as a string. It is an
    # identity field so it comes from the live row, which proves the identity
    # anchor rather than the coercion — assert the coercion directly instead.
    from shared.deploy_resolver_core import coerce_snapshot_column

    id_col = NamedSet.__table__.columns["id"]
    dims_col = NamedSet.__table__.columns["dimensions"]
    raw = str(uuid.uuid4())

    assert coerce_snapshot_column(raw, id_col) == uuid.UUID(raw)
    assert coerce_snapshot_column(raw, dims_col) == raw


# ---------------------------------------------------------------------------
# Partition-coverage guards (Opus deep review R1).
#
# ``build_served_row`` classifies by exclusion: anything not listed as
# governance or identity is treated as a snapshot-pinned DEFINITION field. That
# default is the safe direction for the deploy-authority invariant, but it means
# a NEW column added to the ORM is classified SILENTLY -- a new governance
# column would be wrongly pinned to the snapshot and an admin action on it would
# not take effect until a redeploy. These tests force the decision to be made
# explicitly at the moment the column is added.
# ---------------------------------------------------------------------------

_NAMED_SET_DEFINITION_FIELDS = {
    "name", "display_name", "description", "display_folder", "scope",
    "expression", "dimensions", "builder_definition", "list_type",
}


def test_named_set_column_partition_is_exhaustive():
    from src import named_set_deploy_resolver as nsr

    classified = (
        set(nsr._LIVE_GOVERNANCE_FIELDS)
        | set(nsr._IDENTITY_FIELDS)
        | _NAMED_SET_DEFINITION_FIELDS
    )
    actual = {c.name for c in NamedSet.__table__.columns}
    assert actual - classified == set(), (
        "A NamedSet column is unclassified. build_served_row would silently "
        "treat it as a snapshot-pinned DEFINITION field. Add it to "
        "_LIVE_GOVERNANCE_FIELDS (live-owned: an admin must be able to change "
        "it without a redeploy) or to _NAMED_SET_DEFINITION_FIELDS here."
    )
    assert classified - actual == set(), (
        "A classified field no longer exists on NamedSet -- the governance "
        "list has drifted from the ORM."
    )


# Named explicitly rather than counted: a count-based guard passes silently
# when one unclassified column is added and another removed, which is the
# enumeration blind spot CLAUDE.md treats as a first-class finding in any
# coverage tool. Naming them also tells the next author WHICH column drifted.
_KPI_DEFINITION_FIELDS = {
    "name", "display_name", "description", "display_folder",
    "value_measure_id", "goal_measure_id", "status_expression",
    "trend_expression", "status_graphic", "trend_graphic",
    "kpi_type", "expression", "calc_agg_mode", "inner_agg", "inner_grain",
    "outer_agg", "at_grain", "non_additive_agg", "carry_forward",
    "target_type", "target_value", "target_measure_id", "target_expression",
    "target_period", "presentation_type", "presentation_meta",
    "format_token", "format_custom",
    "direction",
    "trend_period", "trend_threshold", "trend_sparkline_periods",
    "unit_label", "null_display_value",
    "weight", "parent_kpi_id", "indicator_type", "evaluation_order",
    "time_dimension_id", "business_definition",
}


def test_kpi_column_partition_is_exhaustive():
    """The same guard for the other family sharing deploy_resolver_core."""
    from shared.db.models import KPI
    from src import kpi_deploy_resolver as kdr

    classified = (
        set(kdr._LIVE_GOVERNANCE_FIELDS)
        | set(kdr._IDENTITY_FIELDS)
        | _KPI_DEFINITION_FIELDS
    )
    actual = {c.name for c in KPI.__table__.columns}
    assert actual - classified == set(), (
        "A KPI column is unclassified. Every column not in "
        "_LIVE_GOVERNANCE_FIELDS/_IDENTITY_FIELDS is served from the DEPLOYED "
        "SNAPSHOT. Confirm the new column really is a definition field (a "
        "governance/lifecycle column pinned to the snapshot means an admin "
        "action does not take effect until redeploy), then list it in "
        "_KPI_DEFINITION_FIELDS here."
    )
    assert classified - actual == set(), (
        "A classified field no longer exists on KPI -- the governance list has "
        "drifted from the ORM."
    )


def test_columns_without_tables_snapshot_is_not_a_serving_authority():
    """Bug-8306 parity for the shared deploy-resolver core.

    ``snapshot_resolver._snapshot_has_shape`` (query-router) fails a
    columns-without-tables snapshot CLOSED, and ``deploy_resolver_core``'s
    docstring claims to mirror it. Without this clause the two authorities
    disagree on the SAME deployed snapshot: the query-router returns
    DEPLOYED_SNAPSHOT_INVALID for every query while /named-sets and /kpis
    return HTTP 200 with an empty list, so Excel shows an empty catalogue and
    no error instead of a diagnosable failure.
    """
    from shared.deploy_resolver_core import snapshot_has_shape

    malformed = {"columns": [{"id": "c1", "column_name": "amount"}]}
    assert snapshot_has_shape(malformed, "named_sets") is False
    assert snapshot_has_shape(malformed, "kpis") is False
    # A snapshot carrying both is a usable shape.
    assert snapshot_has_shape(
        {"columns": [{"id": "c1"}], "tables": [{"id": "t1"}]}, "named_sets"
    ) is True


@pytest.mark.parametrize("family", ["named_sets", "kpis"])
def test_wrong_type_snapshot_family_is_not_a_serving_authority(family):
    """Malformed family payloads must fail closed instead of becoming empty lists."""
    from shared.deploy_resolver_core import snapshot_has_shape

    assert snapshot_has_shape({family: "corrupt"}, family) is False
    assert snapshot_has_shape(
        {"measures": [{"id": "m"}], family: "corrupt"}, family
    ) is False
    assert snapshot_has_shape({family: ["corrupt"]}, family) is False


def test_accepted_shape_families_match_the_query_router_predicate():
    """The accepted-family list must not silently drift from the query-router.

    Two authorities disagreeing about whether the SAME deployed snapshot is
    usable is how a model ends up serving a catalogue while every query 503s (or
    vice versa). The ONE deliberate difference is the resolver's own family: a
    snapshot carrying only ``named_sets``/``kpis`` can answer the question THIS
    resolver is asked, while the query-router, which must plan SQL, cannot.
    """
    from shared.deploy_resolver_core import snapshot_has_shape

    for shared_family in ("measures", "dimensions", "hierarchies"):
        assert snapshot_has_shape({shared_family: [{"id": "x"}]}, "named_sets") is True, (
            f"{shared_family!r} is accepted by "
            "query-router snapshot_resolver._snapshot_has_shape but rejected "
            "here -- the two authorities have drifted."
        )
    # The deliberate difference, asserted so it stays deliberate.
    assert snapshot_has_shape({"named_sets": [{"id": "x"}]}, "named_sets") is True
    assert snapshot_has_shape({"kpis": [{"id": "x"}]}, "kpis") is True
    # A genuinely empty/placeholder snapshot is still not an authority.
    assert snapshot_has_shape({"schema_version": "1.0"}, "named_sets") is False


@pytest.mark.asyncio
async def test_membership_stays_live_driven_and_governance_is_never_snapshot_sourced():
    """Bug-8753 boundary guard: do NOT re-add snapshot-orphan serving here.

    Serving a row the deployed snapshot pins but which no longer exists live was
    implemented in review round 1 and deliberately reverted in round 2. An orphan
    has no live governance row, so its ``certification_status`` would have to come
    from the snapshot -- which makes DELETING a set an admin had already
    DEPRECATED silently un-deprecate it and put it back in the BI catalogue
    badged ``[Certified]`` (the gateway's only catalogue filter is
    ``certification_status != "deprecated"``). Doing it safely needs a governance
    tombstone kept at delete time, and the same decision has to cover KPIs, whose
    resolver is fed a list already pre-filtered by ``is_deployed``/certification.
    Both halves are one product decision: Bug-8753.

    If you are here because you want deleted-but-deployed sets to keep serving,
    close Bug-8753 first, then change this test deliberately.
    """
    ns_id, model_id, version_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    # Deployed while certified; the admin then deprecated it, then it was deleted.
    snap = _snapshot_named_set(ns_id, model_id, certification_status="certified")
    model = _deployed_model(model_id, version_id)
    db = _FakeDb([model, _version(version_id, model_id, [snap])])

    resolved, withheld = await resolve_served_named_sets(db, model, [])

    assert resolved == [], (
        "A named set with no live row must not be served from the snapshot: "
        "its governance would come from the snapshot too, resurrecting a "
        "deprecated set as certified. See Bug-8753."
    )
    assert withheld == []


# ---------------------------------------------------------------------------
# Adapter-discovery guard (Opus deep review R4).
#
# The two partition tests above are hand-enumerated PER FAMILY. Their discovery
# mechanism is <ORM>.__table__.columns, which is exhaustive for a family that is
# already listed and blind to one that is not -- so they fail OPEN on exactly the
# extension architecture_kpi-deploy-serving-authority.md invites ("adding a third
# family: drill-through sets, model parameters, ..."). A third adapter would ship
# with no partition coverage and could silently pin a GOVERNANCE column to the
# snapshot, so an admin deprecating that entity would see no effect until the next
# redeploy. This guard makes the enumeration itself fail closed.
# ---------------------------------------------------------------------------

import ast
from pathlib import Path

_SRC_DIR = Path(__file__).resolve().parent.parent / "src"

# Families with a column-partition exhaustiveness test in THIS file.
_COVERED_DEPLOY_RESOLVER_ADAPTERS = {
    "kpi_deploy_resolver",
    "named_set_deploy_resolver",
}


def _deploy_resolver_adapters() -> set[str]:
    """Every model-service src module that adapts ``deploy_resolver_core``."""
    found: set[str] = set()
    for path in _SRC_DIR.glob("*.py"):
        if path.stem == "deploy_resolver_core":
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            module = None
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
            elif isinstance(node, ast.Import):
                module = ",".join(alias.name for alias in node.names)
            if module and "deploy_resolver_core" in module:
                found.add(path.stem)
                break
    return found


def test_every_deploy_resolver_adapter_has_a_column_partition_guard():
    actual = _deploy_resolver_adapters()
    assert actual == _COVERED_DEPLOY_RESOLVER_ADAPTERS, (
        "A module adapts deploy_resolver_core without a column-partition "
        "exhaustiveness test in this file.\n"
        f"  discovered: {sorted(actual)}\n"
        f"  covered:    {sorted(_COVERED_DEPLOY_RESOLVER_ADAPTERS)}\n"
        "build_served_row classifies BY EXCLUSION: every column not named as "
        "governance or identity is served from the DEPLOYED SNAPSHOT. A new "
        "family therefore pins its governance columns to the snapshot silently, "
        "and an admin deprecating one of its rows would see no effect until the "
        "next redeploy. Add a partition test for the new adapter, then list it "
        "in _COVERED_DEPLOY_RESOLVER_ADAPTERS."
    )
