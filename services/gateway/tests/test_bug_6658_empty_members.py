"""Bug-6658 (F-002-04): restore zero-fact axis members when NON EMPTY is absent.

SSAS renders every axis-level member — including members with no facts — unless
NON EMPTY prunes them. The fact-driven GROUP BY only returns members present in
facts, so a plain (no NON EMPTY) axis previously dropped zero-activity members.
These tests pin the gateway restoration: absent members are unioned into the
result with empty cells when NON EMPTY is absent, and pruned when it is present.
"""
import pytest

from src.dax import xmla_server
from src.dax.xmla_server import _mdx_axis_has_non_empty, _restore_empty_axis_members


def test_mdx_axis_has_non_empty_detects_keyword():
    mdx_ne = (
        "SELECT {[Measures].[Amount]} ON COLUMNS, "
        "NON EMPTY {[Product].[Product].[Product].Members} ON ROWS FROM [demo]"
    )
    mdx_plain = (
        "SELECT {[Measures].[Amount]} ON COLUMNS, "
        "{[Product].[Product].[Product].Members} ON ROWS FROM [demo]"
    )
    assert _mdx_axis_has_non_empty(mdx_ne, 1) is True
    assert _mdx_axis_has_non_empty(mdx_plain, 1) is False
    # The columns axis in both has no NON EMPTY.
    assert _mdx_axis_has_non_empty(mdx_ne, 0) is False


def test_mdx_axis_has_non_empty_rows_first_order():
    """Opus R1 F3: when MDX lists ROWS before COLUMNS, the detector must
    correctly isolate each axis's NON EMPTY, never leaking the ROWS keyword
    into the COLUMNS fragment."""
    # NON EMPTY on ROWS only, ROWS listed first.
    mdx_rows_ne = (
        "SELECT NON EMPTY {[Product].[Product].Members} ON ROWS, "
        "{[Measures].[Amount]} ON COLUMNS FROM [demo]"
    )
    assert _mdx_axis_has_non_empty(mdx_rows_ne, 1) is True
    assert _mdx_axis_has_non_empty(mdx_rows_ne, 0) is False  # bug: was True

    # NON EMPTY on COLUMNS only, ROWS listed first.
    mdx_cols_ne = (
        "SELECT {[Product].[Product].Members} ON ROWS, "
        "NON EMPTY {[Measures].[Amount]} ON COLUMNS FROM [demo]"
    )
    assert _mdx_axis_has_non_empty(mdx_cols_ne, 0) is True
    assert _mdx_axis_has_non_empty(mdx_cols_ne, 1) is False


def _members(*keys):
    return {"members": [{"key_value": k, "caption": k} for k in keys], "levels": []}


@pytest.mark.asyncio
async def test_empty_members_restored_without_non_empty(monkeypatch):
    """A plain (no NON EMPTY) flat axis restores members with no facts as empty
    (NULL-cell) rows so planners see zero-activity members."""
    mdx = (
        "SELECT {[Measures].[Amount]} ON COLUMNS, "
        "{[Product].[Product].[Product].Members} ON ROWS FROM [demo]"
    )
    columns = ["Product", "Amount"]
    rows = [{"Product": "P1", "Amount": 10}]  # only P1 has facts

    async def fake_members(model_id, dim, tenant_slug, jwt_token, persona_id=None):
        assert dim == "Product"
        return _members("P1", "P2", "P3")

    monkeypatch.setattr(xmla_server, "get_dimension_members", fake_members)

    out = await _restore_empty_axis_members(
        dax_statement=mdx,
        columns=columns,
        rows=rows,
        dimensions_meta=[{"name": "Product"}],
        measures_meta=[{"name": "Amount", "default_agg": "sum"}],
        dim_names={"Product"},
        hierarchy_level_dim_map={},
        hierarchy_default_dim_map={},
        model_id="m1",
        tenant_slug="demo",
        jwt_token="tok",
    )
    products = sorted(str(r.get("Product")) for r in out)
    assert products == ["P1", "P2", "P3"]
    # The restored members carry NULL measure cells (empty), not a fabricated 0.
    p2 = next(r for r in out if r["Product"] == "P2")
    p3 = next(r for r in out if r["Product"] == "P3")
    assert p2["Amount"] is None
    assert p3["Amount"] is None
    # The fact row is untouched.
    p1 = next(r for r in out if r["Product"] == "P1")
    assert p1["Amount"] == 10


@pytest.mark.asyncio
async def test_non_empty_axis_is_not_restored(monkeypatch):
    """With NON EMPTY present the member domain is NOT fetched; zero-fact members
    stay pruned (NON EMPTY is an explicit pruning operation)."""
    mdx = (
        "SELECT {[Measures].[Amount]} ON COLUMNS, "
        "NON EMPTY {[Product].[Product].[Product].Members} ON ROWS FROM [demo]"
    )
    rows = [{"Product": "P1", "Amount": 10}]
    called = {"n": 0}

    async def fake_members(*a, **k):
        called["n"] += 1
        return _members("P1", "P2", "P3")

    monkeypatch.setattr(xmla_server, "get_dimension_members", fake_members)

    out = await _restore_empty_axis_members(
        dax_statement=mdx,
        columns=["Product", "Amount"],
        rows=rows,
        dimensions_meta=[{"name": "Product"}],
        measures_meta=[{"name": "Amount", "default_agg": "sum"}],
        dim_names={"Product"},
        hierarchy_level_dim_map={},
        hierarchy_default_dim_map={},
        model_id="m1",
        tenant_slug="demo",
        jwt_token="tok",
    )
    assert called["n"] == 0
    assert [r["Product"] for r in out] == ["P1"]


@pytest.mark.asyncio
async def test_multi_dim_axis_left_fact_driven(monkeypatch):
    """A multi-dimension axis needs the full member cross-product; the gateway
    does not synthesise it (fail-safe) — the domain fetch is skipped."""
    mdx = (
        "SELECT {[Measures].[Amount]} ON COLUMNS, "
        "{[Product].[Product].Members * [Region].[Region].Members} ON ROWS "
        "FROM [demo]"
    )
    rows = [{"Product": "P1", "Region": "R1", "Amount": 10}]
    called = {"n": 0}

    async def fake_members(*a, **k):
        called["n"] += 1
        return _members("P1", "P2")

    monkeypatch.setattr(xmla_server, "get_dimension_members", fake_members)

    out = await _restore_empty_axis_members(
        dax_statement=mdx,
        columns=["Product", "Region", "Amount"],
        rows=rows,
        dimensions_meta=[{"name": "Product"}, {"name": "Region"}],
        measures_meta=[{"name": "Amount", "default_agg": "sum"}],
        dim_names={"Product", "Region"},
        hierarchy_level_dim_map={},
        hierarchy_default_dim_map={},
        model_id="m1",
        tenant_slug="demo",
        jwt_token="tok",
    )
    assert called["n"] == 0
    assert out == rows


@pytest.mark.asyncio
async def test_member_fetch_failure_keeps_fact_rows(monkeypatch):
    """A member-domain fetch failure degrades to the fact rows (best effort),
    never faults the pivot."""
    mdx = (
        "SELECT {[Measures].[Amount]} ON COLUMNS, "
        "{[Product].[Product].[Product].Members} ON ROWS FROM [demo]"
    )
    rows = [{"Product": "P1", "Amount": 10}]

    async def failing_members(*a, **k):
        raise RuntimeError("router down")

    monkeypatch.setattr(xmla_server, "get_dimension_members", failing_members)

    out = await _restore_empty_axis_members(
        dax_statement=mdx,
        columns=["Product", "Amount"],
        rows=rows,
        dimensions_meta=[{"name": "Product"}],
        measures_meta=[{"name": "Amount", "default_agg": "sum"}],
        dim_names={"Product"},
        hierarchy_level_dim_map={},
        hierarchy_default_dim_map={},
        model_id="m1",
        tenant_slug="demo",
        jwt_token="tok",
    )
    assert out == rows
