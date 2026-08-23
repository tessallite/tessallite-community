"""Bug-6295 / Bug-7623 — imported model-version history must NEVER present
today's live shape (or any wrong shape) under an old version label.

Two contracts are locked here:

OLD-format (schema_version 1) bundles carry no per-version ``snapshot_json``, so
the source-tenant historical shapes are genuinely NOT in the bundle. The earlier
importer "fixed" the foreign-id problem (Bug-5354) by stamping the CURRENT live
model onto EVERY imported version row — a silent-correctness defect (a restore of
"version 1" then rehydrated today's shape under an old label). The honest-degrade
contract: such rows persist with ``snapshot_unavailable=True`` and a PLACEHOLDER
``snapshot_json = {}`` — never the live shape, never a foreign-id copy — with
metadata (number, summary) preserved, and the live model is never serialised to
fabricate a per-version shape.

NEW-format (schema_version 2+, Bug-7623) bundles DO carry each version's own
``snapshot_json``. The importer makes it target-portable (re-keys internal PKs,
rebinds connection ids, bakes the LLM remap into the scheduler FKs, strips
cross-tenant-only refs, stamps the imported model's final slug) and persists it
with ``snapshot_unavailable=False`` so a revert reproduces version N's REAL shape.
A version that cannot be rebound (its connection is gone, or it was already a
placeholder) still honest-degrades.

Each assertion fails if the corresponding behaviour is reverted.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.model_snapshot.rehydrator import insert_model_versions


def _capture_db():
    """Mock AsyncSession that records the values dict of every insert()."""
    db = AsyncMock()
    captured: list[dict] = []

    async def _execute(stmt):
        try:
            compiled = stmt.compile()
            captured.append(dict(compiled.params))
        except Exception:
            pass
        return MagicMock()

    db.execute = AsyncMock(side_effect=_execute)
    db._captured = captured
    return db


@pytest.mark.asyncio
async def test_imported_version_is_marked_unavailable_not_stamped_with_live_shape():
    model_id = uuid.uuid4()
    # The bundle version carries a FOREIGN-id snapshot (ids from another tenant).
    # In reality Bug-7623 strips snapshot_json from the bundle entirely; we keep
    # it here to prove the importer discards whatever the bundle carries.
    foreign_snapshot = {
        "columns": [{"id": "FOREIGN-COL-1"}],
        "measures": [{"id": "FOREIGN-MEAS-1", "source_column_id": "FOREIGN-COL-1"}],
    }
    bundle_versions = [{
        "id": str(uuid.uuid4()),
        "version_number": 3,
        "summary": "imported",
        "snapshot_json": foreign_snapshot,
    }]
    # A live snapshot the importer must NOT fabricate onto history rows.
    live_snapshot = {
        "columns": [{"id": "LIVE-COL-1"}],
        "measures": [{"id": "LIVE-MEAS-1", "source_column_id": "LIVE-COL-1"}],
    }
    db = _capture_db()
    with patch(
        "shared.model_snapshot.serialiser.snapshot_model",
        new=AsyncMock(return_value=live_snapshot),
    ) as mock_snap:
        remap = await insert_model_versions(model_id, bundle_versions, db)

    # The live model is NOT serialised to fabricate a per-version shape.
    mock_snap.assert_not_awaited()

    rows = [r for r in db._captured if "snapshot_json" in r]
    assert rows, "no model_version row captured"
    stored = rows[0]["snapshot_json"]
    # The row is marked unavailable with a PLACEHOLDER snapshot ...
    assert rows[0]["snapshot_unavailable"] is True
    assert stored == {}
    # ... and specifically NOT today's live shape (the reverted-bug behaviour) ...
    assert stored != live_snapshot
    # ... nor the bundle's foreign-id copy.
    assert stored != foreign_snapshot
    # Version metadata (number/summary) is preserved; the timeline stays intact.
    assert rows[0]["version_number"] == 3
    assert rows[0]["summary"] == "imported"
    # id remap returned for the inserted version.
    assert remap[bundle_versions[0]["id"]] == rows[0]["id"]


@pytest.mark.asyncio
async def test_every_imported_version_is_unavailable_and_live_never_serialised():
    """Many imported versions: none may carry a real snapshot, and the live
    model must not be serialised at all (the old code serialised once and copied
    it to every row — the exact silent-wrong behaviour)."""
    model_id = uuid.uuid4()
    versions = [
        {"id": str(uuid.uuid4()), "version_number": 1, "snapshot_json": {"columns": [{"id": "OLD1"}]}},
        {"id": str(uuid.uuid4()), "version_number": 2, "snapshot_json": {"columns": [{"id": "OLD2"}]}},
    ]
    db = _capture_db()
    with patch(
        "shared.model_snapshot.serialiser.snapshot_model",
        new=AsyncMock(return_value={"columns": [{"id": "LIVE-COL"}]}),
    ) as mock_snap:
        await insert_model_versions(model_id, versions, db)

    # No live serialisation happens — history is never fabricated from live.
    assert mock_snap.await_count == 0
    rows = [r for r in db._captured if "snapshot_json" in r]
    assert len(rows) == 2
    for r in rows:
        assert r["snapshot_unavailable"] is True
        assert r["snapshot_json"] == {}


@pytest.mark.asyncio
async def test_append_authentic_import_version_is_servable_above_placeholders():
    """Bug-6295 (deploy happy-path): the importer must append ONE authentic,
    servable version from the live shape, numbered above the imported
    placeholders, so deploy-latest resolves a real snapshot. Prove the appended
    row is snapshot_unavailable=False, carries the live snapshot, and takes
    version_number = max(existing)+1."""
    from shared.model_snapshot.rehydrator import append_authentic_import_version

    model_id = uuid.uuid4()
    live_snapshot = {"columns": [{"id": "LIVE-COL-1"}], "measures": []}

    db = _capture_db()

    # The version-number probe reads the current max (imported placeholders went
    # up to 7); the helper must number the authentic row at 8.
    async def _execute(stmt):
        try:
            compiled = stmt.compile()
            db._captured.append(dict(compiled.params))
        except Exception:
            pass
        result = MagicMock()
        result.scalar_one_or_none.return_value = 7
        return result

    db.execute = AsyncMock(side_effect=_execute)

    with patch(
        "shared.model_snapshot.serialiser.snapshot_model",
        new=AsyncMock(return_value=live_snapshot),
    ) as mock_snap:
        new_id = await append_authentic_import_version(
            model_id, db, summary="s", created_by="import"
        )

    mock_snap.assert_awaited_once_with(model_id, db)
    rows = [r for r in db._captured if "snapshot_json" in r]
    assert len(rows) == 1
    row = rows[0]
    # Authentic + servable: real live snapshot, not a placeholder.
    assert row["snapshot_unavailable"] is False
    assert row["snapshot_json"] == live_snapshot
    # Numbered above the imported placeholders so deploy-latest picks it.
    assert row["version_number"] == 8
    assert row["id"] == new_id


@pytest.mark.asyncio
async def test_insert_model_versions_empty_list_does_not_serialise():
    model_id = uuid.uuid4()
    db = _capture_db()
    with patch(
        "shared.model_snapshot.serialiser.snapshot_model",
        new=AsyncMock(),
    ) as mock_snap:
        remap = await insert_model_versions(model_id, [], db)
    assert remap == {}
    mock_snap.assert_not_awaited()


# ---------------------------------------------------------------------------
# Bug-7623 — NEW-format (v2+) bundles carry each version's own portable
# snapshot; the importer persists it (restorable), while OLD-format bundles and
# non-rebindable / placeholder versions honest-degrade (H2 preserved).
# ---------------------------------------------------------------------------


def test_bundle_version_gate_routes_new_vs_old_format():
    """The format-version gate must route v2+ to the per-version path and v1 /
    missing to the honest-degrade path."""
    from shared.model_snapshot.project_rehydrator import (
        _bundle_carries_version_snapshots,
    )

    assert _bundle_carries_version_snapshots({"schema_version": 2}) is True
    assert _bundle_carries_version_snapshots({"schema_version": 3}) is True
    assert _bundle_carries_version_snapshots({"schema_version": 1}) is False
    assert _bundle_carries_version_snapshots({}) is False
    # A malformed value never crashes the gate; it degrades to old-format.
    assert _bundle_carries_version_snapshots({"schema_version": "x"}) is False


def test_make_version_snapshot_portable_rekeys_rebinds_strips_and_slugs():
    """The portability helper routes the version snapshot through the live import
    machinery: re-keys internal PKs (no collision with the still-live source
    model in a same-tenant duplicate), remaps connection ids, bakes the LLM
    remap into the scheduler FKs, strips cross-tenant-only refs, and stamps the
    imported model's final slug — without mutating the source snapshot."""
    from shared.model_snapshot.rehydrator import _make_version_snapshot_portable

    src_conn, tgt_conn = str(uuid.uuid4()), str(uuid.uuid4())
    src_llm, tgt_llm = str(uuid.uuid4()), str(uuid.uuid4())
    stray_llm = str(uuid.uuid4())  # a scheduler LLM not imported into target
    src_model_id = str(uuid.uuid4())
    src_measure_id = str(uuid.uuid4())
    new_model_id = uuid.uuid4()
    snap = {
        "model": {
            "id": src_model_id, "slug": "source-slug", "seed": "source-seed",
            "llm_config_id": str(uuid.uuid4()),
        },
        "measures": [{"id": src_measure_id}],
        "data_sources": [
            {"id": str(uuid.uuid4()), "display_name": "pg",
             "project_connection_id": src_conn}
        ],
        "data_targets": [],
        "glossary_entries": [{"id": str(uuid.uuid4()),
                              "created_by": str(uuid.uuid4())}],
        "ai_scheduler_config": {
            "id": str(uuid.uuid4()),
            "llm_config_id": src_llm,
            "glossary_llm_config_id": stray_llm,
        },
    }
    portable = _make_version_snapshot_portable(
        snap,
        new_model_id=new_model_id,
        connection_mapping={src_conn: tgt_conn},
        llm_id_remap={src_llm: tgt_llm},
        model_slug="imported-slug",
    )
    assert portable is not None
    # Internal PKs re-keyed (finding 1): the source measure id must NOT survive.
    assert portable["measures"][0]["id"] != src_measure_id
    assert portable["model"]["id"] == str(new_model_id)
    # Connection rebound; cross-tenant-only refs stripped.
    assert portable["data_sources"][0]["project_connection_id"] == tgt_conn
    assert portable["model"]["llm_config_id"] is None
    assert portable["glossary_entries"][0]["created_by"] is None
    # Scheduler LLM baked (finding 2): known source -> target id; unknown cleared.
    assert portable["ai_scheduler_config"]["llm_config_id"] == tgt_llm
    assert portable["ai_scheduler_config"]["glossary_llm_config_id"] is None
    # Final slug stamped (finding 3).
    assert portable["model"]["slug"] == "imported-slug"
    # Source seed dropped so a revert keeps the destination seed (R2 finding 1).
    assert "seed" not in portable["model"]
    # Source snapshot left untouched.
    assert snap["data_sources"][0]["project_connection_id"] == src_conn
    assert snap["model"]["slug"] == "source-slug"
    assert snap["model"]["seed"] == "source-seed"
    assert snap["measures"][0]["id"] == src_measure_id


def test_shared_pk_map_gives_live_shape_and_version_matching_ids():
    """R2 SECURITY (finding 2): when the live shape and a version snapshot share
    ONE pk map, a source id re-keys to the SAME new id in both — the id
    continuity a revert needs to re-attach preserved governance (CLS/RLS/KPI)
    and match preserved aggregates. Without the shared map the two would diverge
    and the CLS tag re-attach would drop links (masked columns exposed)."""
    from shared.model_snapshot.importer import prepare_snapshot_for_import
    from shared.model_snapshot.rehydrator import _make_version_snapshot_portable

    src_col = str(uuid.uuid4())
    src_model_id = str(uuid.uuid4())
    new_model_id = uuid.uuid4()
    shared: dict[str, str] = {}

    live_snap = {
        "model": {"id": src_model_id, "slug": "s", "seed": "seed"},
        "columns": [{"id": src_col}],
        "data_sources": [], "data_targets": [],
    }
    live_portable, _ = prepare_snapshot_for_import(
        live_snap, new_model_id=new_model_id,
        connection_mapping={}, shared_pk_map=shared,
    )
    version_portable = _make_version_snapshot_portable(
        {
            "model": {"id": src_model_id, "slug": "s", "seed": "seed"},
            "columns": [{"id": src_col}],
            "data_sources": [], "data_targets": [],
        },
        new_model_id=new_model_id,
        connection_mapping={},
        llm_id_remap={},
        model_slug="imported",
        shared_pk_map=shared,
    )
    assert version_portable is not None
    # Same source column id -> same new id in both (continuity preserved).
    assert (
        live_portable["columns"][0]["id"]
        == version_portable["columns"][0]["id"]
        != src_col
    )
    # Both point at the same target model id.
    assert (
        live_portable["model"]["id"]
        == version_portable["model"]["id"]
        == str(new_model_id)
    )


def test_make_version_snapshot_portable_returns_none_on_missing_connection():
    """A version whose source/target connection has no mapping cannot be
    rebound — the helper returns None so the caller honest-degrades it."""
    from shared.model_snapshot.rehydrator import _make_version_snapshot_portable

    snap = {
        "model": {"id": str(uuid.uuid4())},
        "data_sources": [
            {"id": str(uuid.uuid4()), "display_name": "pg",
             "project_connection_id": "GONE"}
        ],
        "data_targets": [],
    }
    assert _make_version_snapshot_portable(
        snap,
        new_model_id=uuid.uuid4(),
        connection_mapping={},
        llm_id_remap={},
        model_slug="x",
    ) is None


@pytest.mark.asyncio
async def test_new_format_version_persists_real_portable_snapshot():
    """A NEW-format version restores to version N's REAL shape (PKs re-keyed,
    connection rebound, cross-tenant refs stripped, slug stamped), NOT today's
    live shape."""
    model_id = uuid.uuid4()
    src_conn, tgt_conn = str(uuid.uuid4()), str(uuid.uuid4())
    src_measure_id = str(uuid.uuid4())
    version_shape = {
        "schema_version": 4,
        "model": {"id": str(uuid.uuid4()), "slug": "source-slug",
                  "llm_config_id": str(uuid.uuid4())},
        "measures": [{"id": src_measure_id}],
        "data_sources": [
            {"id": str(uuid.uuid4()), "display_name": "pg",
             "project_connection_id": src_conn}
        ],
        "data_targets": [],
        "glossary_entries": [{"id": str(uuid.uuid4()),
                              "created_by": str(uuid.uuid4())}],
    }
    todays_live_shape = {"measures": [{"id": str(uuid.uuid4())}]}
    versions = [{
        "id": str(uuid.uuid4()),
        "version_number": 1,
        "summary": "v1 real",
        "snapshot_json": version_shape,
        "snapshot_unavailable": False,
    }]
    db = _capture_db()
    with patch(
        "shared.model_snapshot.serialiser.snapshot_model",
        new=AsyncMock(return_value=todays_live_shape),
    ) as mock_snap:
        await insert_model_versions(
            model_id, versions, db,
            bundle_carries_version_snapshots=True,
            connection_mapping={src_conn: tgt_conn},
            model_slug="imported-slug",
        )
    # The live model is never serialised to fabricate a version shape.
    mock_snap.assert_not_awaited()
    rows = [r for r in db._captured if "snapshot_json" in r]
    assert len(rows) == 1
    stored = rows[0]["snapshot_json"]
    # Restorable: marked available, carries a real (non-empty) per-version shape.
    assert rows[0]["snapshot_unavailable"] is False
    assert stored["measures"] and stored != {}
    # Internal PK re-keyed (finding 1) — the source id must not survive.
    assert stored["measures"][0]["id"] != src_measure_id
    assert stored["model"]["id"] == str(model_id)
    # NOT today's live shape (the reverted-bug behaviour).
    assert stored != todays_live_shape
    # Connection rebound; cross-tenant refs stripped; final slug stamped.
    assert stored["data_sources"][0]["project_connection_id"] == tgt_conn
    assert stored["model"]["llm_config_id"] is None
    assert stored["glossary_entries"][0]["created_by"] is None
    assert stored["model"]["slug"] == "imported-slug"
    # Metadata preserved.
    assert rows[0]["version_number"] == 1
    assert rows[0]["summary"] == "v1 real"


@pytest.mark.asyncio
async def test_new_format_version_degrades_when_connection_unmapped():
    """A NEW-format version whose connection cannot be rebound honest-degrades
    to non-restorable rather than persist a snapshot whose revert would fail."""
    model_id = uuid.uuid4()
    version_shape = {
        "model": {"id": str(uuid.uuid4())},
        "data_sources": [
            {"id": str(uuid.uuid4()), "display_name": "pg",
             "project_connection_id": "GONE"}
        ],
        "data_targets": [],
    }
    versions = [{
        "id": str(uuid.uuid4()), "version_number": 2,
        "snapshot_json": version_shape,
    }]
    db = _capture_db()
    await insert_model_versions(
        model_id, versions, db,
        bundle_carries_version_snapshots=True,
        connection_mapping={},  # GONE has no mapping
    )
    rows = [r for r in db._captured if "snapshot_json" in r]
    assert rows[0]["snapshot_unavailable"] is True
    assert rows[0]["snapshot_json"] == {}


@pytest.mark.asyncio
async def test_new_format_preexisting_placeholder_stays_unavailable():
    """Even under NEW-format, a version already flagged snapshot_unavailable
    (history imported before this fix) stays a non-restorable placeholder."""
    model_id = uuid.uuid4()
    versions = [{
        "id": str(uuid.uuid4()), "version_number": 1,
        "snapshot_json": {}, "snapshot_unavailable": True,
    }]
    db = _capture_db()
    await insert_model_versions(
        model_id, versions, db,
        bundle_carries_version_snapshots=True,
        connection_mapping={},
    )
    rows = [r for r in db._captured if "snapshot_json" in r]
    assert rows[0]["snapshot_unavailable"] is True
    assert rows[0]["snapshot_json"] == {}


@pytest.mark.asyncio
async def test_old_format_degrades_even_with_snapshot_present():
    """An OLD-format bundle (default flag) degrades every version even when a
    snapshot_json rode along — the H2 contract for pre-existing backups."""
    model_id = uuid.uuid4()
    versions = [{
        "id": str(uuid.uuid4()), "version_number": 1,
        "snapshot_json": {"measures": [{"id": "OLD"}]},
    }]
    db = _capture_db()
    await insert_model_versions(model_id, versions, db)  # old-format default
    rows = [r for r in db._captured if "snapshot_json" in r]
    assert rows[0]["snapshot_unavailable"] is True
    assert rows[0]["snapshot_json"] == {}


# ---------------------------------------------------------------------------
# Bug-8032 (sol C1 -> B3) — a NEW-format history snapshot whose entity families
# are structurally malformed must NOT be offered as restorable.
# ---------------------------------------------------------------------------
#
# Bundle validation is shallow: it checks that a version row carries a snapshot,
# not that each family inside it is a list of records. A history snapshot with a
# valid non-empty ``measures`` list and ``dimensions: {}`` therefore persisted
# ``snapshot_unavailable=False``, and ``revert_to_version`` would install it as
# the model's DEPLOY POINTER. Every downstream reader then treats the falsey
# mapping as "this version deploys no dimensions" instead of "this snapshot is
# malformed" — the AI advisor reads six families and structurally validated one,
# so it ran, and paid for an LLM call, against a vocabulary that was never there.


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "family, bad_value",
    [
        ("dimensions", {}),
        ("dimensions", {"region": {"id": "d"}}),
        ("hierarchies", "none"),
        ("columns", [["not", "a", "dict"]]),
    ],
)
async def test_bug_8032_malformed_history_family_is_not_restorable(family, bad_value):
    """A malformed family makes the version non-restorable, not silently empty.

    Honest-degrade is the answer this function already gives a snapshot it
    cannot rebind. Withdrawing the restorable claim keeps revert — and therefore
    the deploy pointer — away from a snapshot no reader can interpret, while the
    timeline row itself survives.
    """
    model_id = uuid.uuid4()
    src_conn, tgt_conn = str(uuid.uuid4()), str(uuid.uuid4())
    version_shape = {
        "schema_version": 4,
        "model": {"id": str(uuid.uuid4()), "slug": "source-slug"},
        "measures": [{"id": str(uuid.uuid4()), "name": "revenue"}],
        family: bad_value,
        "data_sources": [
            {"id": str(uuid.uuid4()), "display_name": "pg",
             "project_connection_id": src_conn}
        ],
        "data_targets": [],
    }
    versions = [{
        "id": str(uuid.uuid4()), "version_number": 1, "summary": "v1",
        "snapshot_json": version_shape, "snapshot_unavailable": False,
    }]
    db = _capture_db()
    await insert_model_versions(
        model_id, versions, db,
        bundle_carries_version_snapshots=True,
        connection_mapping={src_conn: tgt_conn},
        model_slug="imported-slug",
    )
    rows = [r for r in db._captured if "snapshot_json" in r]
    assert len(rows) == 1
    assert rows[0]["snapshot_unavailable"] is True, (
        f"a history snapshot with a malformed {family!r} family was stored as "
        "restorable; revert would install it as the deploy pointer"
    )
    assert rows[0]["snapshot_json"] == {}
    # The timeline is not lost — only the restorable claim is withdrawn.
    assert rows[0]["version_number"] == 1
    assert rows[0]["summary"] == "v1"
