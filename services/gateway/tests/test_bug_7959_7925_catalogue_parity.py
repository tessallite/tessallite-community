"""Bug-7959 + Bug-7925: catalogue parity tests.

Bug-7959: glossary effective descriptions must be deployment-pinned and
consistent across JDBC and XMLA.  The deployed snapshot now carries
``effective_description`` on dimension and measure rows (additive field).
JDBC consumes it directly from the snapshot.  XMLA overlays the deployed
snapshot values onto the live metadata via
``_overlay_deployed_effective_descriptions``.

Bug-7925: MDSCHEMA_SETS must NOT advertise sql_fixed named lists because
_inline_named_sets unconditionally skips them at execution time.  The
discovery-to-execution contract test asserts that every advertised set is
executable (sql_fixed absent from the catalogue, MDX sets still present).
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.dax.mdschema import _rows_sets
from src.dax.xmla_server import _overlay_deployed_effective_descriptions


def _u() -> str:
    return str(uuid.uuid4())


# -----------------------------------------------------------------------
# Bug-7959: XMLA deployed-snapshot effective_description overlay
# -----------------------------------------------------------------------


class TestOverlayDeployedEffectiveDescriptions:
    """Verify that the XMLA metadata overlay replaces live descriptions
    with deployed-snapshot-pinned descriptions."""

    def test_overlay_replaces_live_effective_description(self):
        dim_id = _u()
        meas_id = _u()
        measures = [
            {"id": meas_id, "name": "Revenue", "effective_description": "LIVE glossary text"},
        ]
        dimensions = [
            {"id": dim_id, "name": "Region", "effective_description": "LIVE dim glossary"},
        ]
        deployed_snapshot = {
            "dimensions": [
                {"id": dim_id, "name": "Region", "effective_description": "DEPLOYED dim glossary"},
            ],
            "measures": [
                {"id": meas_id, "name": "Revenue", "effective_description": "DEPLOYED meas glossary"},
            ],
        }
        _overlay_deployed_effective_descriptions(measures, dimensions, deployed_snapshot)
        assert dimensions[0]["effective_description"] == "DEPLOYED dim glossary"
        assert measures[0]["effective_description"] == "DEPLOYED meas glossary"

    def test_overlay_keeps_live_when_snapshot_lacks_field(self):
        """Pre-fix snapshots have no effective_description; the overlay must
        not clobber the live value with None."""
        dim_id = _u()
        dimensions = [
            {"id": dim_id, "name": "Region", "effective_description": "LIVE text"},
        ]
        deployed_snapshot = {
            "dimensions": [
                {"id": dim_id, "name": "Region"},
            ],
        }
        _overlay_deployed_effective_descriptions([], dimensions, deployed_snapshot)
        assert dimensions[0]["effective_description"] == "LIVE text"

    def test_overlay_handles_empty_snapshot(self):
        dim_id = _u()
        dimensions = [
            {"id": dim_id, "name": "Region", "effective_description": "LIVE text"},
        ]
        _overlay_deployed_effective_descriptions([], dimensions, {})
        assert dimensions[0]["effective_description"] == "LIVE text"

    def test_overlay_handles_none_snapshot(self):
        dimensions = [{"id": _u(), "effective_description": "live"}]
        _overlay_deployed_effective_descriptions([], dimensions, None)
        assert dimensions[0]["effective_description"] == "live"

    def test_overlay_only_matches_by_id(self):
        """A dimension present in live but absent from the deployed snapshot
        keeps its live effective_description."""
        live_id = _u()
        snap_id = _u()
        dimensions = [
            {"id": live_id, "name": "Region", "effective_description": "LIVE text"},
        ]
        deployed_snapshot = {
            "dimensions": [
                {"id": snap_id, "name": "Region", "effective_description": "SNAP text"},
            ],
        }
        _overlay_deployed_effective_descriptions([], dimensions, deployed_snapshot)
        assert dimensions[0]["effective_description"] == "LIVE text"

    def test_before_deploy_stability(self):
        """Editing a glossary entry after deploy must NOT change the served
        descriptions.  The deployed snapshot carries the pinned values; the
        overlay replaces the live (post-edit) values with the snapshot."""
        dim_id = _u()
        # Live metadata reflects a post-deploy glossary edit:
        dimensions = [
            {"id": dim_id, "name": "Region", "effective_description": "EDITED after deploy"},
        ]
        # Deployed snapshot retains the description at deploy time:
        deployed_snapshot = {
            "dimensions": [
                {"id": dim_id, "name": "Region", "effective_description": "Definition at deploy time"},
            ],
        }
        _overlay_deployed_effective_descriptions([], dimensions, deployed_snapshot)
        assert dimensions[0]["effective_description"] == "Definition at deploy time"

    def test_post_deploy_parity_jdbc_xmla(self):
        """Both JDBC and XMLA must serve the same effective_description
        from the deployed snapshot.

        JDBC reads the snapshot directly and uses
        ``dim.get("effective_description") or dim.get("description")``.
        XMLA overlays the deployed snapshot onto live metadata.
        Given the same deployed snapshot, both paths produce the same value.
        """
        dim_id = _u()
        meas_id = _u()
        deployed_snapshot = {
            "dimensions": [
                {"id": dim_id, "name": "Region", "description": "raw dim",
                 "effective_description": "Deployed glossary for Region"},
            ],
            "measures": [
                {"id": meas_id, "name": "Revenue", "description": "raw meas",
                 "effective_description": "Deployed glossary for Revenue"},
            ],
        }

        # JDBC path: reads from deployed snapshot directly.
        jdbc_dim_desc = (
            deployed_snapshot["dimensions"][0].get("effective_description")
            or deployed_snapshot["dimensions"][0].get("description")
            or ""
        )
        jdbc_meas_desc = (
            deployed_snapshot["measures"][0].get("effective_description")
            or deployed_snapshot["measures"][0].get("description")
            or ""
        )

        # XMLA path: live metadata with possibly different descriptions,
        # then overlay from the same deployed snapshot.
        xmla_dimensions = [
            {"id": dim_id, "name": "Region", "effective_description": "LIVE glossary (stale)"},
        ]
        xmla_measures = [
            {"id": meas_id, "name": "Revenue", "effective_description": "LIVE glossary (stale)"},
        ]
        _overlay_deployed_effective_descriptions(
            xmla_measures, xmla_dimensions, deployed_snapshot,
        )
        xmla_dim_desc = (
            xmla_dimensions[0].get("effective_description")
            or xmla_dimensions[0].get("description")
            or ""
        )
        xmla_meas_desc = (
            xmla_measures[0].get("effective_description")
            or xmla_measures[0].get("description")
            or ""
        )

        assert jdbc_dim_desc == xmla_dim_desc == "Deployed glossary for Region"
        assert jdbc_meas_desc == xmla_meas_desc == "Deployed glossary for Revenue"


# -----------------------------------------------------------------------
# Bug-7925: MDSCHEMA_SETS discovery-to-execution contract
# -----------------------------------------------------------------------


class TestMdschemaSetsDiscoveryContract:
    """Verify that MDSCHEMA_SETS only advertises sets that MDX execution
    can actually consume (sql_fixed is filtered out)."""

    def test_sql_fixed_excluded_from_discovery(self):
        named_sets = [
            {
                "name": "TopRegions",
                "display_name": "Top Regions",
                "list_type": "sql_fixed",
                "description": "SQL-only fixed member list",
                "expression": "",
                "display_folder": "",
                "scope": 1,
                "dimensions": "",
                "certification_status": "certified",
            },
        ]
        rows = _rows_sets("TestCatalog", named_sets)
        assert len(rows) == 0, "sql_fixed sets must not appear in MDSCHEMA_SETS"

    def test_mdx_sets_retained_in_discovery(self):
        named_sets = [
            {
                "name": "TopProducts",
                "display_name": "Top Products",
                "list_type": "mdx",
                "description": "MDX set expression",
                "expression": "TopCount([Product].Members, 5, [Measures].[Revenue])",
                "display_folder": "",
                "scope": 1,
                "dimensions": "[Product]",
                "certification_status": "certified",
            },
        ]
        rows = _rows_sets("TestCatalog", named_sets)
        assert len(rows) == 1
        assert rows[0]["SET_NAME"] == "TopProducts"

    def test_mixed_types_only_mdx_retained(self):
        """When the model has both sql_fixed and MDX sets, only MDX sets
        appear in the XMLA discovery catalogue."""
        named_sets = [
            {
                "name": "SQLFixed",
                "display_name": "SQL Fixed",
                "list_type": "sql_fixed",
                "description": "SQL-only",
                "expression": "",
                "display_folder": "",
                "scope": 1,
                "dimensions": "",
            },
            {
                "name": "MDXSet",
                "display_name": "MDX Set",
                "list_type": "mdx",
                "description": "MDX set",
                "expression": "{[Product].[All Products]}",
                "display_folder": "",
                "scope": 1,
                "dimensions": "[Product]",
            },
            {
                "name": "AnotherSQLFixed",
                "display_name": "Another SQL Fixed",
                "list_type": "sql_fixed",
                "description": "Another SQL-only",
                "expression": "",
                "display_folder": "",
                "scope": 1,
                "dimensions": "",
            },
        ]
        rows = _rows_sets("TestCatalog", named_sets)
        names = [r["SET_NAME"] for r in rows]
        assert "MDXSet" in names
        assert "SQLFixed" not in names
        assert "AnotherSQLFixed" not in names
        assert len(rows) == 1

    def test_none_list_type_retained(self):
        """Named sets without a list_type (legacy/MDX default) must still
        appear in discovery."""
        named_sets = [
            {
                "name": "LegacySet",
                "display_name": "Legacy",
                "list_type": None,
                "description": "Legacy MDX set",
                "expression": "{[Region].Members}",
                "display_folder": "",
                "scope": 1,
                "dimensions": "[Region]",
            },
        ]
        rows = _rows_sets("TestCatalog", named_sets)
        assert len(rows) == 1
        assert rows[0]["SET_NAME"] == "LegacySet"

    def test_certified_marker_preserved_on_mdx_sets(self):
        """Bug-6264 governance marker must still work for non-sql_fixed sets."""
        named_sets = [
            {
                "name": "CertifiedSet",
                "display_name": "Certified Set",
                "list_type": "mdx",
                "description": "A great set",
                "expression": "{[Product].Members}",
                "display_folder": "",
                "scope": 1,
                "dimensions": "[Product]",
                "certification_status": "certified",
            },
        ]
        rows = _rows_sets("TestCatalog", named_sets)
        assert len(rows) == 1
        assert rows[0]["SET_DESCRIPTION"].startswith("[Certified]")

    def test_every_advertised_set_is_executable(self):
        """Discovery-to-execution contract: every set returned by
        _rows_sets has a non-sql_fixed list_type, meaning _inline_named_sets
        will attempt to inline it (empty expressions are handled separately
        by the inliner's expression check, not by type exclusion)."""
        named_sets = [
            {"name": "A", "list_type": "mdx", "expression": "expr", "display_name": "A",
             "display_folder": "", "scope": 1, "dimensions": ""},
            {"name": "B", "list_type": "sql_fixed", "expression": "", "display_name": "B",
             "display_folder": "", "scope": 1, "dimensions": ""},
            {"name": "C", "list_type": None, "expression": "expr", "display_name": "C",
             "display_folder": "", "scope": 1, "dimensions": ""},
            {"name": "D", "list_type": "topN", "expression": "expr", "display_name": "D",
             "display_folder": "", "scope": 1, "dimensions": ""},
        ]
        rows = _rows_sets("TestCatalog", named_sets)
        advertised_names = {r["SET_NAME"] for r in rows}
        # sql_fixed must not be advertised
        assert "B" not in advertised_names
        # All others are executable (the inliner handles empty expressions)
        assert "A" in advertised_names
        assert "C" in advertised_names
        assert "D" in advertised_names
