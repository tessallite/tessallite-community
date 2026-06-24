"""E1 / F-002-17 — XMLA DRILLTHROUGH RETURN + Order() observability.

Both are documented, deliberate policies (server-curated drill columns;
fail-loud Order() rejection). The fix here is to make each VISIBLE instead of
silent/opaque, without changing the behaviour:

  * a DRILLTHROUGH with an explicit RETURN clause emits a SOAP <Warning> that
    the RETURN columns were ignored (columns are still server-curated);
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
# F-002-17 part 1 — DRILLTHROUGH RETURN clause emits a warning
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_drillthrough_return_clause_emits_warning():
    mdx = (
        "DRILLTHROUGH SELECT {[Measures].[Sales]} ON 0 FROM [Model] "
        "WHERE ([Region].[Country].&[France]) RETURN [Region].[City]"
    )
    parsed = parse_mdx(mdx)
    assert parsed.return_columns  # the RETURN clause was parsed

    drill_options = [{"hierarchy_id": "h1"}]
    drill_result = {
        "columns": ["order_id", "amount"],
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
    ):
        _xml, warnings = await drillthrough_handler.handle_drillthrough(
            parsed=parsed,
            tenant_slug="acme",
            jwt_token="jwt",
            measures_meta=[],
            dimensions_meta=[],
            hierarchy_defs=[],
        )

    assert any("RETURN" in w for w in warnings)


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
            new=AsyncMock(return_value=[{"hierarchy_id": "h1"}]),
        ),
        patch.object(
            drillthrough_handler, "_fetch_drill_through",
            new=AsyncMock(return_value=drill_result),
        ),
    ):
        _xml, warnings = await drillthrough_handler.handle_drillthrough(
            parsed=parsed,
            tenant_slug="acme",
            jwt_token="jwt",
            measures_meta=[],
            dimensions_meta=[],
            hierarchy_defs=[],
        )

    assert not any("RETURN" in w for w in warnings)
