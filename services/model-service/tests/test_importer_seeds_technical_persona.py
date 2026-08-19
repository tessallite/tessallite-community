"""Bug-6138: importer-created models must also get the canonical Technical
persona.

`create_model` seeds the technical persona, but the format importers (Cube,
dbt, AtScale, catalog, YAML) and the native model import create model rows
directly via ``insert_model_with_slug_retry`` + ``rehydrate_into_live`` — they
bypass ``create_model``. The external-format mappers emit only an ``everyone``
persona, so without an explicit seed those models have the same inert
hidden-columns technical catalog the bug is about.

This is a wiring-contract regression guard: it fails if the seed call is dropped
from any importer's model-creation path. A full end-to-end drive of each importer
(valid source bundle + live rehydrate) is disproportionate for guarding the
single call; the call site + argument are pinned here instead.
"""
from __future__ import annotations

import importlib
import inspect
import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from .conftest import async_gen_from, make_mock_db

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "module_path,expected_call",
    [
        ("src.api.cube_import", "await seed_technical_persona(db, new_model_id)"),
        ("src.api.dbt_import", "await seed_technical_persona(db, new_model_id)"),
        ("src.api.atscale_import", "await seed_technical_persona(db, new_model_id)"),
        ("src.api.catalog_import", "await seed_technical_persona(db, new_model_id)"),
        ("src.api.yaml_export", "await seed_technical_persona(db, new_model_id)"),
        ("src.api.import_export", "await seed_technical_persona(tenant_db, new_model_id)"),
        (
            "src.api.project_import_export",
            "await seed_technical_persona(tenant_db, UUID(str(new_model_id)))",
        ),
    ],
)
def test_importer_seeds_technical_persona_for_new_models(module_path, expected_call):
    mod = importlib.import_module(module_path)
    assert hasattr(mod, "seed_technical_persona"), (
        f"{module_path} must import seed_technical_persona"
    )
    src = inspect.getsource(mod)
    assert expected_call in src, (
        f"{module_path} must call `{expected_call}` so importer-created models "
        "get the canonical Technical persona (Bug-6138)"
    )


class _FakeUpload:
    """Minimal UploadFile stand-in: yields its bytes once, then EOF."""

    def __init__(self, data: bytes, filename: str = "model.yml"):
        self._data = data
        self._sent = False
        self.filename = filename

    async def read(self, _n: int = -1) -> bytes:
        if self._sent:
            return b""
        self._sent = True
        return self._data


@pytest.mark.asyncio
async def test_cube_import_seeds_persona_per_model_before_commit():
    """Behavioral guard (stronger than the source-string check): drive the real
    cube import endpoint with a two-model bundle and confirm the Technical
    persona is seeded ONCE PER MODEL and BEFORE the commit. This catches the two
    placement regressions a source-string check misses: a seed call moved after
    ``db.commit()`` (the flush would be lost), or dedented out of the per-model
    loop (only the last model seeded)."""
    from src.api import cube_import as mod

    events: list[tuple] = []

    db = make_mock_db()
    db.get = AsyncMock(return_value=types.SimpleNamespace(id=uuid.uuid4()))
    conn_result = MagicMock()
    conn_result.first.return_value = (uuid.uuid4(),)
    existing_result = MagicMock()
    existing_result.all.return_value = []
    db.execute = AsyncMock(side_effect=[conn_result, existing_result])

    async def _commit():
        events.append(("commit",))

    db.commit = AsyncMock(side_effect=_commit)

    async def _seed(_session, model_id):
        events.append(("seed", model_id))

    seed_spy = AsyncMock(side_effect=_seed)

    async def _insert(*_a, **kw):
        return types.SimpleNamespace(id=kw.get("new_model_id")), "slug"

    def _model_snap(slug):
        return {"model": {"slug": slug, "display_name": slug}, "data_sources": []}

    map_result = types.SimpleNamespace(
        bundle={"models": [_model_snap("m1"), _model_snap("m2")]},
        warnings=[],
    )

    with (
        patch.object(mod, "enforce_demo_source_locked", MagicMock()),
        patch.object(mod, "parse_cube_yaml", MagicMock(return_value={})),
        patch.object(mod, "map_cube_to_tessallite", MagicMock(return_value=map_result)),
        patch.object(mod, "get_tenant_db", async_gen_from(db)),
        patch.object(mod, "prepare_snapshot_for_import", MagicMock(return_value=({"model": {}}, []))),
        patch.object(mod, "insert_model_with_slug_retry", AsyncMock(side_effect=_insert)),
        patch.object(mod, "rehydrate_into_live", AsyncMock()),
        patch.object(mod, "seed_technical_persona", seed_spy),
    ):
        resp = await mod.import_cube_models(
            project_id=uuid.uuid4(),
            file=_FakeUpload(b"cubes: []"),
            current_user=types.SimpleNamespace(tenant_id="t1", email="a@b", user_id="u"),
        )

    assert resp.models_created == 2
    # One seed per model, both distinct, and all strictly before the commit.
    assert [e[0] for e in events] == ["seed", "seed", "commit"]
    seed_ids = [e[1] for e in events if e[0] == "seed"]
    assert len(set(seed_ids)) == 2
