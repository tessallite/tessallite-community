"""Shared harness for the hierarchy-preview route tests (Bug-9895).

The preview is a derivation over the persona model query: every statement it
issues goes to the query-router ``/execute`` path through
``hierarchies._execute_via_router``. These helpers stand in for the model
metadata the endpoint reads and record the SQL it routes, so each test file
asserts on BEHAVIOUR (which members, which bounding, which warning) rather than
restating the endpoint's internals.
"""
from __future__ import annotations

import contextlib
import types
import uuid
from unittest.mock import AsyncMock, patch

from .conftest import TEST_MODEL_ID, TEST_PROJECT_ID, async_gen_from, make_mock_db, make_model

PREFIX = f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}"

# Ordered level dimension names for the default three-level fake hierarchy.
LEVEL_DIMS = ["country_code", "city_name", "channel_name"]


def make_persona(
    *,
    bypass_rls: bool = False,
    included_hierarchy_ids=None,
    included_dimension_ids=None,
    persona_id: uuid.UUID | None = None,
):
    return types.SimpleNamespace(
        id=persona_id or uuid.uuid4(),
        model_id=TEST_MODEL_ID,
        name="Business",
        slug="business",
        bypass_row_security=bypass_rls,
        included_hierarchy_ids=included_hierarchy_ids or [],
        included_dimension_ids=included_dimension_ids or [],
        includes_hidden_columns=False,
        audience_roles=["member"],
        default_filters={},
    )


def make_levels(count: int = 3, names: list[str] | None = None):
    return [
        types.SimpleNamespace(
            id=uuid.uuid4(),
            ordinal=i,
            name=(names[i] if names else f"Level_{i}"),
            key_attribute_id=uuid.uuid4(),
            key_attribute_source="physical_column",
        )
        for i in range(count)
    ]


def make_resolved(table_name: str = "dim_region", row_count_estimate: int | None = 100):
    return types.SimpleNamespace(
        table=types.SimpleNamespace(
            id=uuid.uuid4(),
            physical_name=table_name,
            row_count_estimate=row_count_estimate,
        ),
        ref=types.SimpleNamespace(name="region_code", data_type="varchar"),
    )


class Routed:
    """Records every SQL statement the endpoint routes, and replays rows."""

    def __init__(self, rows_for=None):
        self.calls: list[dict] = []
        self._rows_for = rows_for or (lambda sql: [])

    async def __call__(self, model_id, sql, bearer, *, persona_id=None, timeout_s=60.0):
        self.calls.append(
            {"model_id": model_id, "sql": sql, "bearer": bearer, "persona_id": persona_id},
        )
        return self._rows_for(sql)

    @property
    def sqls(self) -> list[str]:
        return [c["sql"] for c in self.calls]

    @property
    def sample_sql(self) -> str:
        """The member sample. Level-count probes are unordered DISTINCT
        statements, so the sample is the one statement carrying ``ORDER BY``."""
        ordered = [s for s in self.sqls if "ORDER BY" in s]
        assert len(ordered) == 1, self.sqls
        return ordered[0]


def preview_patches(
    *,
    routed,
    persona,
    levels,
    hierarchy_id,
    caption_dim: str | None = None,
    level_dims: list[str | None] | None = None,
    resolve_attribute=None,
    excluded_attrs=None,
    hierarchy_name: str = "Geography Channel",
):
    """Every patch the preview route needs, as a list for :func:`entered`."""
    dims = level_dims if level_dims is not None else LEVEL_DIMS
    mock_db = make_mock_db()
    mock_db.get = AsyncMock(return_value=make_model(slug="modely"))
    dim_by_attr = {lv.key_attribute_id: dims[i] for i, lv in enumerate(levels)}

    async def _dim_name(db, *, model_id, attribute_id, source):
        return dim_by_attr.get(attribute_id)

    return [
        patch("src.api.hierarchies.get_tenant_db", async_gen_from(mock_db)),
        patch(
            "src.api.hierarchies.resolve_effective_persona",
            new=AsyncMock(return_value=persona),
        ),
        patch(
            "src.api.hierarchies._load_hierarchy_or_404",
            new=AsyncMock(
                return_value=types.SimpleNamespace(
                    id=hierarchy_id, name=hierarchy_name, model_id=TEST_MODEL_ID,
                ),
            ),
        ),
        patch("src.api.hierarchies._levels_for_hierarchy", new=AsyncMock(return_value=levels)),
        patch(
            "src.api.hierarchies._resolve_attribute",
            new=(
                resolve_attribute
                if resolve_attribute is not None
                else AsyncMock(side_effect=lambda *a, **k: make_resolved())
            ),
        ),
        patch("src.api.hierarchies._resolve_level_dimension_name", new=_dim_name),
        patch(
            "src.api.hierarchies._resolve_level_caption_dimension_name",
            new=AsyncMock(return_value=caption_dim),
        ),
        patch("src.api.hierarchies._execute_via_router", new=routed),
        patch(
            "src.api.hierarchies.get_excluded_level_attribute_ids",
            new=AsyncMock(return_value=excluded_attrs if excluded_attrs is not None else set()),
        ),
    ]


@contextlib.contextmanager
def entered(patches):
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        yield


async def get_preview(client, hierarchy_id, query: str):
    return await client.get(
        f"{PREFIX}/hierarchies/{hierarchy_id}/preview?{query}",
        headers={"Authorization": "Bearer test-token"},
    )
