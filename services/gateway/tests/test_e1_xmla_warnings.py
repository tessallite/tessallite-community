"""E1 — XMLA DRILLTHROUGH RETURN projection + Order() observability.

  * Wave C #4: a DRILLTHROUGH RETURN clause is HONOURED as an ordered
    post-projection of the curated, persona-authorised drill result; an
    unavailable/blocked requested column faults the whole request (supersedes
    the earlier F-002-17 "RETURN ignored + warning" policy).
  * Order() rejection carries an actionable message (sort in the BI client;
    the data is correct) rather than a bare "cannot translate" fault.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from src.dax.mdx_validators import check_unsupported_mdx_constructs
from src.dax import drillthrough_handler
from src.dax.ts_mdx_parser import parse_mdx


# --------------------------------------------------------------------------
# F-002-17 part 2 — Order() rejection is actionable
# --------------------------------------------------------------------------

def test_order_rejection_is_actionable():
    with pytest.raises(ValueError) as exc:
        check_unsupported_mdx_constructs(
            "Order({[Region].[Country].Members}, [Measures].[Sales], DESC)",
            "ROWS axis",
        )
    msg = str(exc.value)
    assert "Order()" in msg
    # Tells the user the data is fine and to sort client-side.
    assert "sort" in msg.lower()
    assert "client" in msg.lower()


def test_order_still_rejected_loud():
    # The policy is unchanged: Order() must still raise (not be silently
    # dropped), because dropping it would change row order / Top-N rows.
    with pytest.raises(ValueError, match="Order"):
        check_unsupported_mdx_constructs(
            "Order([Date].[Year].Members, [Measures].[Sales], BDESC)",
            "COLUMNS axis",
        )


# --------------------------------------------------------------------------
# Wave C #4 — DRILLTHROUGH RETURN is HONOURED as a post-projection of the
# curated, persona-authorised result. Supersedes F-002-17 (RETURN ignored +
# warning). An unavailable/blocked requested column faults the WHOLE request.
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_drillthrough_return_unavailable_column_faults():
    # RETURN names a column the curated result does not contain (unknown or
    # CLS-omitted) → the whole request is refused, never a partial projection.
    mdx = (
        "DRILLTHROUGH SELECT {[Measures].[Sales]} ON 0 FROM [Model] "
        "WHERE ([Region].[Country].&[France]) RETURN [Region].[City]"
    )
    parsed = parse_mdx(mdx)
    assert parsed.return_columns

    drill_options = ([{"hierarchy_id": "h1"}], None)
    drill_result = {
        "columns": ["order_id", "amount"],  # City is NOT here
        "rows": [{"order_id": "1", "amount": "100"}],
        "page": {"has_more": False},
        "hierarchy_path": [],
    }

    with (
        patch.object(drillthrough_handler, "_extract_measure_name", return_value="Sales"),
        patch.object(drillthrough_handler, "_find_measure", return_value={"id": "m1"}),
        patch.object(drillthrough_handler, "_extract_grouping_levels", return_value=[]),
        patch.object(
            drillthrough_handler, "_fetch_drill_options",
            new=AsyncMock(return_value=drill_options),
        ),
        patch.object(
            drillthrough_handler, "_fetch_drill_through",
            new=AsyncMock(return_value=drill_result),
        ),
        pytest.raises(drillthrough_handler.DrillThroughResolutionError),
    ):
        await drillthrough_handler.handle_drillthrough(
            parsed=parsed,
            tenant_slug="acme",
            jwt_token="jwt",
            measures_meta=[],
            dimensions_meta=[{"name": "city"}, {"name": "region"}],
            hierarchy_defs=[],
        )


@pytest.mark.asyncio
async def test_drillthrough_return_projects_available_subset_in_order():
    # RETURN names available columns → the result is projected to exactly those,
    # in the requested order (region-then-city here, reversed vs curated order).
    mdx = (
        "DRILLTHROUGH SELECT {[Measures].[amount]} ON 0 FROM [Model] "
        "RETURN [Region].[City], [Measures].[amount]"
    )
    parsed = parse_mdx(mdx)
    assert parsed.return_columns

    drill_result = {
        "columns": ["order_id", "amount", "city", "region"],
        "rows": [
            {"order_id": "1", "amount": "100", "city": "Paris", "region": "EMEA"},
            {"order_id": "2", "amount": "50", "city": "Lyon", "region": "EMEA"},
        ],
        "page": {"has_more": False},
        "hierarchy_path": [],
    }
    with (
        patch.object(drillthrough_handler, "_extract_measure_name", return_value="amount"),
        patch.object(drillthrough_handler, "_find_measure", return_value={"id": "m1"}),
        patch.object(drillthrough_handler, "_extract_grouping_levels", return_value=[]),
        patch.object(
            drillthrough_handler, "_fetch_drill_options",
            new=AsyncMock(return_value=([{"hierarchy_id": "h1"}], None)),
        ),
        patch.object(
            drillthrough_handler, "_fetch_drill_through",
            new=AsyncMock(return_value=drill_result),
        ),
    ):
        xml, warnings, _next = await drillthrough_handler.handle_drillthrough(
            parsed=parsed,
            tenant_slug="acme",
            jwt_token="jwt",
            measures_meta=[{"id": "m1", "name": "amount"}],
            dimensions_meta=[{"name": "city"}, {"name": "region"}],
            hierarchy_defs=[],
        )

    # Only the requested columns are present, in the requested order.
    city_pos = xml.find('name="city"')
    amount_pos = xml.find('name="amount"')
    assert city_pos != -1 and amount_pos != -1
    assert city_pos < amount_pos
    assert 'name="region"' not in xml
    assert 'name="order_id"' not in xml
    # Values are carried through the projection.
    assert "<city>Paris</city>" in xml
    assert not any("RETURN" in w for w in warnings)


@pytest.mark.asyncio
async def test_drillthrough_without_return_has_no_return_warning():
    mdx = (
        "DRILLTHROUGH SELECT {[Measures].[Sales]} ON 0 FROM [Model] "
        "WHERE ([Region].[Country].&[France])"
    )
    parsed = parse_mdx(mdx)
    assert not parsed.return_columns

    drill_result = {
        "columns": ["order_id"],
        "rows": [{"order_id": "1"}],
        "page": {"has_more": False},
        "hierarchy_path": [],
    }
    with (
        patch.object(drillthrough_handler, "_extract_measure_name", return_value="Sales"),
        patch.object(drillthrough_handler, "_find_measure", return_value={"id": "m1"}),
        patch.object(drillthrough_handler, "_extract_grouping_levels", return_value=[]),
        patch.object(
            drillthrough_handler, "_fetch_drill_options",
            new=AsyncMock(return_value=([{"hierarchy_id": "h1"}], None)),
        ),
        patch.object(
            drillthrough_handler, "_fetch_drill_through",
            new=AsyncMock(return_value=drill_result),
        ),
    ):
        _xml, warnings, _next = await drillthrough_handler.handle_drillthrough(
            parsed=parsed,
            tenant_slug="acme",
            jwt_token="jwt",
            measures_meta=[],
            dimensions_meta=[],
            hierarchy_defs=[],
        )

    assert not any("RETURN" in w for w in warnings)
