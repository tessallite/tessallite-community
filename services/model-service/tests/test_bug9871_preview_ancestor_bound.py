"""Bug-9871 -- the hierarchy preview drill is bounded by the full ancestor path.

``parent_key`` alone matched a level whose keys repeat under different ancestors
across all of them (children of month 9 = every September, 300 rows). The drill
must carry one equality per ancestor key ABOVE the parent as well.

Bug-9895 re-expressed the preview over the persona model query, so the bounding
is now an equality on each ancestor LEVEL'S DIMENSION in a routed
``SELECT DISTINCT`` rather than a CAST spliced into a physical-table scan. These
tests assert the BEHAVIOUR through the route, so the guard survives the change
of mechanism -- and it now covers the case the physical builder could not: a
hierarchy whose levels live in different tables, where the old code emitted
``ancestor_bound_unsupported`` and returned members from every ancestor.
"""
from __future__ import annotations

import uuid

import pytest

from ._hierarchy_preview_harness import (
    Routed,
    entered,
    get_preview,
    make_levels,
    make_persona,
    preview_patches,
)
from .conftest import client  # noqa: F401

pytestmark = pytest.mark.unit

# Year / Month / Day -- the calendar shape whose keys repeat under ancestors.
CAL_DIMS = ["cal_year", "cal_month", "cal_day"]


def _cal_patches(routed, hierarchy_id, **kw):
    return preview_patches(
        routed=routed,
        persona=make_persona(),
        levels=make_levels(3, names=["Year", "Month", "Day"]),
        hierarchy_id=hierarchy_id,
        level_dims=CAL_DIMS,
        **kw,
    )


@pytest.mark.asyncio
async def test_drill_bounds_by_every_ancestor_key(client):
    """Children of [2025].[9] filter on the YEAR as well as the month, so only
    the 30 days of September 2025 can match -- not every September."""
    hid = uuid.uuid4()
    routed = Routed(rows_for=lambda sql: [{"cal_day": "2025-09-01"}])
    with entered(_cal_patches(routed, hid)):
        resp = await get_preview(
            client, hid,
            "sample_size=100&expand_level=2&parent_key=9&ancestor_keys=2025",
        )

    assert resp.status_code == 200
    sql = routed.sample_sql
    assert "CAST(\"cal_year\" AS VARCHAR) = '2025'" in sql, sql
    assert "CAST(\"cal_month\" AS VARCHAR) = '9'" in sql, sql
    assert [m["key_value"] for m in resp.json()["members"]] == ["2025-09-01"]


@pytest.mark.asyncio
async def test_drill_without_ancestors_binds_only_the_parent(client):
    """A drill one level below the root has no ancestors above the parent, so
    exactly one equality is emitted."""
    hid = uuid.uuid4()
    routed = Routed(rows_for=lambda sql: [{"cal_month": "9"}])
    with entered(_cal_patches(routed, hid)):
        resp = await get_preview(
            client, hid, "sample_size=100&expand_level=1&parent_key=2025",
        )

    assert resp.status_code == 200
    sql = routed.sample_sql
    assert sql.count("CAST(") == 1, sql
    assert "CAST(\"cal_year\" AS VARCHAR) = '2025'" in sql, sql


@pytest.mark.asyncio
async def test_ancestor_key_literal_is_escaped_by_sqlglot(client):
    """F-016-19: a quote in an ancestor key is escaped by the literal builder."""
    hid = uuid.uuid4()
    routed = Routed(rows_for=lambda sql: [])
    with entered(_cal_patches(routed, hid)):
        resp = await get_preview(
            client, hid,
            "sample_size=100&expand_level=2&parent_key=9&ancestor_keys=20%2725",
        )

    assert resp.status_code == 200
    assert "'20''25'" in routed.sample_sql


@pytest.mark.asyncio
async def test_cross_table_drill_is_ancestor_bounded_too(client):
    """Bug-9895: levels in DIFFERENT tables are joined by the model query, so a
    cross-table drill is bounded by the whole ancestor path as well.

    The physical-scan builder could only bound ancestors that shared the child's
    table; anything else produced ``ancestor_bound_unsupported`` and served
    members from every ancestor. That warning is now unreachable on this path.
    """
    hid = uuid.uuid4()
    routed = Routed(rows_for=lambda sql: [{"channel_name": "ATM"}])
    patches = preview_patches(
        routed=routed,
        persona=make_persona(),
        levels=make_levels(3, names=["Country", "City", "Channel"]),
        hierarchy_id=hid,
        level_dims=["country_code", "city_name", "channel_name"],
    )
    with entered(patches):
        resp = await get_preview(
            client, hid,
            "sample_size=100&expand_level=2&parent_key=London&ancestor_keys=GB",
        )

    body = resp.json()
    assert resp.status_code == 200
    assert "ancestor_bound_unsupported" not in {w["type"] for w in body["warnings"]}
    sql = routed.sample_sql
    assert "CAST(\"country_code\" AS VARCHAR) = 'GB'" in sql, sql
    assert "CAST(\"city_name\" AS VARCHAR) = 'London'" in sql, sql
    assert [m["key_value"] for m in body["members"]] == ["ATM"]
