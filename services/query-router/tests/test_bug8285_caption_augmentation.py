"""Bug-8285: /execute member-caption projection (PRODUCER half).

The XMLA gateway sends ``ExecuteRequest.caption_dimensions`` for every axis
dimension that declares a display column. ``_augment_execute_with_caption_columns``
must, for each such dimension that actually resolved, append a companion
``<dim>__caption`` resolved dimension + passthrough SELECT so the source rewriter
projects ``<display_col> AS "<dim>__caption"``. The gateway consumer
(``mdx_execute._normalize_member_captions``) then renders UName=key,
Caption=display.

These tests pin the producer contract at the augmentation boundary (no live DB):
the alias name, that the display column drives the synthetic dimension, and that
un-requested / display-less dimensions are left untouched. The alias suffix MUST
stay ``__caption`` to match the gateway consumer end to end.
"""
from types import SimpleNamespace

import pytest

from src.api import routes


class _FakeDB:
    """Minimal async DB whose ``get`` returns a ModelColumn-like object for any
    display_column_id (or None for a missing id passed in ``missing``)."""

    def __init__(self, missing: set | None = None):
        self._missing = missing or set()

    async def get(self, _model, ident):
        if ident in self._missing:
            return None
        return SimpleNamespace(id=ident, column_name="product_name")


def _bound(dims, grain=None):
    # The execute pivot path always carries a GROUP BY; the augmentation only
    # captions dimensions that are in ``grain`` (so the display column can be
    # added to GROUP BY without faulting). Default the grain to the dim names.
    grain = grain if grain is not None else [d.name for d in dims]
    return SimpleNamespace(
        resolved_dimensions=list(dims),
        logical_query=SimpleNamespace(select_expressions=[], grain=list(grain)),
    )


def _dim(name, display_column_id=None):
    return SimpleNamespace(name=name, display_column_id=display_column_id)


@pytest.mark.asyncio
async def test_augment_projects_caption_column_for_requested_dim():
    bound = _bound([_dim("product_code", display_column_id="disp-1")])
    added = await routes._augment_execute_with_caption_columns(
        bound, _FakeDB(), ["product_code"]
    )
    assert added == ["product_code__caption"]
    # A synthetic resolved dimension named "<dim>__caption" was appended, bound
    # to the DISPLAY column so the rewriter selects it (and GROUP BYs it).
    caption_dims = [
        d for d in bound.resolved_dimensions if d.name == "product_code__caption"
    ]
    assert len(caption_dims) == 1
    assert caption_dims[0].source_column_id == "disp-1"
    # A passthrough SELECT expression carries the same alias so the column is a
    # standalone SELECT item, not GROUP-BY-only.
    ses = bound.logical_query.select_expressions
    assert len(ses) == 1
    assert ses[0].classification == "passthrough"
    assert ses[0].raw_text == "product_code__caption"
    assert ses[0].inner_column == "product_code__caption"
    # CRITICAL: the alias must ALSO be in grain so the source rewriter emits it
    # into GROUP BY. Omitting this makes the display an ungrouped column under a
    # GROUP BY -> hard source-DB error (Bug-8285 review finding 1).
    assert "product_code__caption" in bound.logical_query.grain


@pytest.mark.asyncio
async def test_dim_not_in_grain_is_skipped():
    # product_code declares a display column and is requested, but it is NOT part
    # of the query's GROUP BY grain — projecting a grouped caption would fault or
    # multiply rows, so it must be skipped.
    bound = _bound(
        [_dim("product_code", display_column_id="disp-1")], grain=["other_dim"]
    )
    added = await routes._augment_execute_with_caption_columns(
        bound, _FakeDB(), ["product_code"]
    )
    assert added == []
    assert bound.logical_query.select_expressions == []


@pytest.mark.asyncio
async def test_no_group_by_grain_is_noop():
    bound = _bound([_dim("product_code", display_column_id="disp-1")], grain=[])
    added = await routes._augment_execute_with_caption_columns(
        bound, _FakeDB(), ["product_code"]
    )
    assert added == []


@pytest.mark.asyncio
async def test_alias_suffix_matches_gateway_consumer_contract():
    """The projected alias MUST be ``<dim>__caption`` — the exact column name the
    gateway consumer builds (mdx_execute._member_caption_col). If this drifts the
    caption column is never matched and the pivot silently shows raw keys."""
    bound = _bound([_dim("region_code", display_column_id="disp-9")])
    added = await routes._augment_execute_with_caption_columns(
        bound, _FakeDB(), ["region_code"]
    )
    assert added == ["region_code" + routes._EXECUTE_CAPTION_SUFFIX]
    assert routes._EXECUTE_CAPTION_SUFFIX == "__caption"


@pytest.mark.asyncio
async def test_dim_without_display_column_is_skipped():
    bound = _bound([_dim("product_code", display_column_id=None)])
    added = await routes._augment_execute_with_caption_columns(
        bound, _FakeDB(), ["product_code"]
    )
    assert added == []
    assert bound.logical_query.select_expressions == []


@pytest.mark.asyncio
async def test_dim_not_requested_is_skipped():
    bound = _bound([_dim("product_code", display_column_id="disp-1")])
    # product_code declares a display column but is NOT in caption_dimensions.
    added = await routes._augment_execute_with_caption_columns(
        bound, _FakeDB(), ["some_other_dim"]
    )
    assert added == []


@pytest.mark.asyncio
async def test_missing_display_column_row_is_skipped():
    bound = _bound([_dim("product_code", display_column_id="disp-gone")])
    added = await routes._augment_execute_with_caption_columns(
        bound, _FakeDB(missing={"disp-gone"}), ["product_code"]
    )
    assert added == []


@pytest.mark.asyncio
async def test_no_signal_is_noop():
    bound = _bound([_dim("product_code", display_column_id="disp-1")])
    added = await routes._augment_execute_with_caption_columns(bound, _FakeDB(), None)
    assert added == []
    assert bound.logical_query.select_expressions == []
