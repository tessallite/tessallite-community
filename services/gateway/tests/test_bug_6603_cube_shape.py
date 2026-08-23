"""Bug-6603: XMLA/Excel cube shape — dimension -> hierarchy -> level.

Fable diagnostic symptoms 1 & 2. Verifies the model -> cube mapping module
(``src/dax/cube_model.py``) and the MDSCHEMA builders that consume it produce
the correct SSAS cube shape:

1. A flat Tessallite dimension emits as an ATTRIBUTE hierarchy (HIERARCHY_ORIGIN
   = 2), not a spurious user-defined one-level hierarchy (origin 1).
2. A model user-defined hierarchy emits origin 1 with its ORDERED multi-levels.
3. A calendar hierarchy (dimension_kind == "time") carries is_time_dim through,
   so DIMENSION_TYPE = 1 (time) and its levels are time-typed
   (Year/Quarter/Month/Day), and stays persona-scoped by included_hierarchy_ids.

Reproduce-first note: before the fix, ``_rows_hierarchies`` hardcoded
HIERARCHY_ORIGIN = "1" for every dimension (so ``test_flat_dim_is_attribute_
hierarchy_origin_2`` would assert 2 against an emitted 1 and FAIL), and
``_build_discover_dimensions`` stripped id / is_time_dim / captions off
hierarchies (so the calendar time-typing and persona tests would FAIL).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.dax import cube_model
from src.dax.mdschema import (
    _rows_dimensions,
    _rows_hierarchies,
    _rows_levels,
    _rows_measuregroup_dimensions,
    _rows_md_properties,
    _rows_members,
)


# --- Fixtures mirroring the model-service payloads -------------------------

def _flat_dimension():
    return {
        "id": "dim-account",
        "name": "account_type",
        "display_name": "Account Type",
        "description": "Kind of account",
        "is_time_dim": False,
        "source_column_id": "col-1",
    }


def _user_hierarchy_def():
    return {
        "id": "hier-geo",
        "name": "Geography",
        "display_name": "Geography",
        "dimension_kind": "geo",
        "description": "Region rollup",
        "levels": [
            {"id": "l0", "ordinal": 0, "name": "Region"},
            {"id": "l1", "ordinal": 1, "name": "Country"},
            {"id": "l2", "ordinal": 2, "name": "City"},
        ],
    }


def _calendar_hierarchy_def():
    return {
        "id": "hier-cal",
        "name": "Order Calendar",
        "display_name": "Order Calendar",
        "dimension_kind": "time",
        "calendar_type": "standard",
        "levels": [
            {"ordinal": 0, "name": "Year", "time_unit": "year"},
            {"ordinal": 1, "name": "Quarter", "time_unit": "quarter"},
            {"ordinal": 2, "name": "Month", "time_unit": "month"},
            {"ordinal": 3, "name": "Day", "time_unit": "day"},
        ],
    }


# --- Mapping module --------------------------------------------------------

class TestBuildCubeDimensions:

    def test_flat_dim_marked_attribute_origin(self):
        dims = cube_model.build_cube_dimensions([_flat_dimension()], [])
        acc = next(d for d in dims if d["name"] == "account_type")
        assert acc["source"] == "dimension"
        assert acc["hierarchy_origin"] == cube_model.ORIGIN_ATTRIBUTE  # "2"

    def test_user_hierarchy_origin_and_levels(self):
        dims = cube_model.build_cube_dimensions([], [_user_hierarchy_def()])
        geo = next(d for d in dims if d["name"] == "Geography")
        assert geo["source"] == "hierarchy"
        assert geo["hierarchy_origin"] == cube_model.ORIGIN_USER_DEFINED  # "1"
        assert [lvl["name"] for lvl in geo["levels"]] == ["Region", "Country", "City"]
        # id preserved so included_hierarchy_ids scoping works.
        assert geo["id"] == "hier-geo"
        assert geo["hierarchy_id"] == "hier-geo"

    def test_calendar_hierarchy_is_time_typed(self):
        dims = cube_model.build_cube_dimensions([], [_calendar_hierarchy_def()])
        cal = next(d for d in dims if d["name"] == "Order Calendar")
        # dimension_kind == "time" -> is_time_dim derived True.
        assert cal["is_time_dim"] is True
        assert cal["hierarchy_origin"] == cube_model.ORIGIN_USER_DEFINED

    def test_hierarchy_named_like_dimension_merges(self):
        # A hierarchy sharing a dimension's name keeps the dimension identity
        # AND adopts the multi-level hierarchy shape (no clobber / id loss).
        dim = {**_flat_dimension(), "name": "Geography", "id": "dim-geo"}
        dims = cube_model.build_cube_dimensions([dim], [_user_hierarchy_def()])
        names = [d["name"] for d in dims]
        assert names.count("Geography") == 1
        geo = next(d for d in dims if d["name"] == "Geography")
        assert geo["source"] == "hierarchy"
        assert geo["dimension_id"] == "dim-geo"      # real dim id retained
        assert geo["hierarchy_id"] == "hier-geo"     # hierarchy id retained
        assert len(geo["levels"]) == 3

    def test_ordering_dimensions_before_hierarchies(self):
        dims = cube_model.build_cube_dimensions(
            [_flat_dimension()], [_user_hierarchy_def()],
        )
        assert [d["name"] for d in dims] == ["account_type", "Geography"]

    # --- Bug-6890: calendar grain-level dims must not duplicate as strays ---

    @staticmethod
    def _calendar_with_key_attributes():
        return {
            "id": "hier-upd-cal",
            "name": "updated_at Calendar",
            "display_name": "updated_at Calendar",
            "dimension_kind": "time",
            "type": "date_embedded",
            "levels": [
                {"ordinal": 0, "name": "Year", "time_unit": "year",
                 "key_attribute": {"name": "updated_at_calendar_year"}},
                {"ordinal": 1, "name": "Month", "time_unit": "month",
                 "key_attribute": {"name": "updated_at_calendar_month"}},
                {"ordinal": 2, "name": "Day", "time_unit": "day",
                 "key_attribute": {"name": "updated_at_calendar_day"}},
            ],
        }

    @staticmethod
    def _grain_dim(name: str, grain: str):
        return {
            "id": f"dim-{name}",
            "name": name,
            "display_name": f"{grain.title()} (updated_at Calendar)",
            "is_time_dim": True,
            "time_grain": grain,
            "is_hidden": False,
        }

    def test_time_hierarchy_level_dims_suppressed(self):
        """A grain-level dimension owned by a visible time hierarchy must not
        also appear as a standalone one-level hierarchy (the Excel stray
        'Year (updated_at Calendar)' duplicate)."""
        grain_dims = [
            self._grain_dim("updated_at_calendar_year", "year"),
            self._grain_dim("updated_at_calendar_month", "month"),
            self._grain_dim("updated_at_calendar_day", "day"),
        ]
        dims = cube_model.build_cube_dimensions(
            grain_dims + [_flat_dimension()],
            [self._calendar_with_key_attributes()],
        )
        names = [d["name"] for d in dims]
        assert "updated_at Calendar" in names
        assert "account_type" in names
        assert "updated_at_calendar_year" not in names
        assert "updated_at_calendar_month" not in names
        assert "updated_at_calendar_day" not in names

    def test_non_time_hierarchy_level_dims_kept(self):
        """Level attributes of a NON-time hierarchy stay browsable flat —
        suppression is scoped to calendar grain duplicates only."""
        hier = _user_hierarchy_def()
        hier["levels"][0]["key_attribute"] = {"name": "region"}
        region = {"id": "dim-region", "name": "region", "is_time_dim": False}
        dims = cube_model.build_cube_dimensions([region], [hier])
        assert "region" in [d["name"] for d in dims]

    def test_hidden_time_hierarchy_does_not_suppress(self):
        """A hidden calendar must not swallow its grain dims — they are then
        the only remaining access to those columns."""
        hier = self._calendar_with_key_attributes()
        hier["is_hidden"] = True
        grain = self._grain_dim("updated_at_calendar_year", "year")
        dims = cube_model.build_cube_dimensions([grain], [hier])
        assert "updated_at_calendar_year" in [d["name"] for d in dims]


class TestPersonaFilter:

    def test_dimension_allow_list_does_not_drop_hierarchies(self):
        dims = cube_model.build_cube_dimensions(
            [_flat_dimension()], [_calendar_hierarchy_def()],
        )
        # persona lists a dimension id but NO hierarchy ids: the calendar must
        # survive (Fable finding b) — it is governed by included_hierarchy_ids.
        persona = {"included_dimension_ids": ["dim-account"], "included_hierarchy_ids": []}
        kept = cube_model.filter_cube_dimensions_by_persona(dims, persona)
        names = {d["name"] for d in kept}
        assert "account_type" in names
        assert "Order Calendar" in names

    def test_hierarchy_allow_list_scopes_hierarchies(self):
        dims = cube_model.build_cube_dimensions(
            [_flat_dimension()],
            [_user_hierarchy_def(), _calendar_hierarchy_def()],
        )
        persona = {"included_dimension_ids": [], "included_hierarchy_ids": ["hier-geo"]}
        kept = cube_model.filter_cube_dimensions_by_persona(dims, persona)
        names = {d["name"] for d in kept}
        assert "Geography" in names            # allowed hierarchy
        assert "Order Calendar" not in names   # excluded hierarchy
        assert "account_type" in names         # attribute dim unaffected

    def test_empty_allow_lists_noop(self):
        dims = cube_model.build_cube_dimensions([_flat_dimension()], [_user_hierarchy_def()])
        persona = {"included_dimension_ids": [], "included_hierarchy_ids": []}
        kept = cube_model.filter_cube_dimensions_by_persona(dims, persona)
        assert len(kept) == len(dims)

    def test_merged_entry_kept_by_explicit_dimension_grant(self):
        # A hierarchy sharing a dimension's name merges to one entry carrying
        # both ids. A persona that explicitly grants the dimension keeps the
        # entry even when a non-empty hierarchy allow list omits the hierarchy.
        dim = {**_flat_dimension(), "name": "Geography", "id": "dim-geo"}
        dims = cube_model.build_cube_dimensions([dim], [_user_hierarchy_def()])
        persona = {
            "included_dimension_ids": ["dim-geo"],
            "included_hierarchy_ids": ["hier-other"],  # excludes hier-geo
        }
        kept = cube_model.filter_cube_dimensions_by_persona(dims, persona)
        assert any(d["name"] == "Geography" for d in kept)

    def test_dimension_grant_without_hierarchy_emits_flat_attribute(self):
        # F-3(b): when the persona grants the DIMENSION but a non-empty
        # included_hierarchy_ids omits the merged hierarchy, the entry must be
        # emitted as the FLAT attribute (source != "hierarchy", origin 2) so
        # member discovery goes through get_dimension_members — NOT the merged
        # hierarchy entry, which would route to get_hierarchy_preview and 404
        # for this persona (advertised-but-never-expands).
        dim = {**_flat_dimension(), "name": "Geography", "id": "dim-geo"}
        dims = cube_model.build_cube_dimensions([dim], [_user_hierarchy_def()])
        persona = {
            "included_dimension_ids": ["dim-geo"],
            "included_hierarchy_ids": ["hier-other"],  # excludes hier-geo
        }
        kept = cube_model.filter_cube_dimensions_by_persona(dims, persona)
        geo = next(d for d in kept if d["name"] == "Geography")
        assert geo["source"] != "hierarchy", (
            "granted dimension must be reachable via the flat member path, not "
            "the persona-404 hierarchy preview path (F-3b)"
        )
        assert geo.get("hierarchy_origin") == cube_model.ORIGIN_ATTRIBUTE
        assert geo.get("id") == "dim-geo"       # real dimension id for the fetch
        assert not geo.get("hierarchy_id")      # multi-level shape dropped

    def test_hierarchy_grant_keeps_multilevel_shape(self):
        # Control: when the hierarchy IS granted the entry stays a hierarchy.
        dim = {**_flat_dimension(), "name": "Geography", "id": "dim-geo"}
        dims = cube_model.build_cube_dimensions([dim], [_user_hierarchy_def()])
        persona = {
            "included_dimension_ids": ["dim-geo"],
            "included_hierarchy_ids": ["hier-geo"],  # hierarchy granted
        }
        kept = cube_model.filter_cube_dimensions_by_persona(dims, persona)
        geo = next(d for d in kept if d["name"] == "Geography")
        assert geo["source"] == "hierarchy"
        assert geo.get("hierarchy_id") == "hier-geo"


# --- End-to-end through the MDSCHEMA builders ------------------------------

class TestBuildersConsumeCubeShape:

    def test_flat_dim_hierarchy_origin_2(self):
        dims = cube_model.build_cube_dimensions([_flat_dimension()], [])
        rows = _rows_hierarchies("demo", dims)
        acc = next(r for r in rows if r["HIERARCHY_NAME"] == "account_type")
        assert acc["HIERARCHY_ORIGIN"] == "2"
        # Bug-6603 grouping: standalone attribute dims share the [Dimensions] group
        # node (DIMENSION_UNIQUE_NAME column), but the HIERARCHY_UNIQUE_NAME grammar
        # stays [attr].[attr] so member discovery / Execute are unaffected.
        assert acc["DIMENSION_UNIQUE_NAME"] == "[Dimensions]"
        assert acc["HIERARCHY_UNIQUE_NAME"] == "[account_type].[account_type]"

    def test_user_hierarchy_origin_1_multilevel(self):
        dims = cube_model.build_cube_dimensions([], [_user_hierarchy_def()])
        hrows = _rows_hierarchies("demo", dims)
        geo = next(r for r in hrows if r["HIERARCHY_NAME"] == "Geography")
        assert geo["HIERARCHY_ORIGIN"] == "1"
        lrows = _rows_levels("demo", dims, {})
        geo_levels = [r for r in lrows if r["HIERARCHY_UNIQUE_NAME"] == "[Geography].[Geography]"]
        assert [r["LEVEL_NAME"] for r in geo_levels] == ["(All)", "Region", "Country", "City"]
        assert [r["LEVEL_NUMBER"] for r in geo_levels] == ["0", "1", "2", "3"]

    def test_calendar_dimension_type_time_and_level_types(self):
        dims = cube_model.build_cube_dimensions([], [_calendar_hierarchy_def()])
        # Bug-6891: the calendar joins the [Hierarchies] group node; time typing
        # is asserted on the hierarchy ROW (DIMENSION_TYPE) and on LEVEL_TYPE.
        drows = _rows_dimensions("demo", dims, {})
        group = next(r for r in drows if r["DIMENSION_UNIQUE_NAME"] == "[Hierarchies]")
        assert group["DIMENSION_NAME"] == "Hierarchies"
        hrows = _rows_hierarchies("demo", dims)
        cal_h = next(r for r in hrows if r["HIERARCHY_NAME"] == "Order Calendar")
        assert cal_h["DIMENSION_TYPE"] == "1"
        # Levels time-typed via the calendar level names / time_unit.
        lrows = _rows_levels("demo", dims, {})
        by_level = {
            r["LEVEL_NAME"]: r
            for r in lrows
            if r["HIERARCHY_UNIQUE_NAME"] == "[Order Calendar].[Order Calendar]"
        }
        assert by_level["Year"]["LEVEL_TYPE"] == "20"
        assert by_level["Quarter"]["LEVEL_TYPE"] == "68"
        assert by_level["Month"]["LEVEL_TYPE"] == "132"
        assert by_level["Day"]["LEVEL_TYPE"] == "1028"

    def test_calendar_level_types_from_time_unit_when_names_differ(self):
        # Level NAMES that are not the canonical words still type correctly
        # because the authoritative time_unit drives LEVEL_TYPE.
        cal = _calendar_hierarchy_def()
        cal["levels"] = [
            {"ordinal": 0, "name": "FY", "time_unit": "year"},
            {"ordinal": 1, "name": "Per", "time_unit": "month"},
        ]
        dims = cube_model.build_cube_dimensions([], [cal])
        lrows = _rows_levels("demo", dims, {})
        by_level = {
            r["LEVEL_NAME"]: r
            for r in lrows
            if r["HIERARCHY_UNIQUE_NAME"] == "[Order Calendar].[Order Calendar]"
        }
        assert by_level["FY"]["LEVEL_TYPE"] == "20"
        assert by_level["Per"]["LEVEL_TYPE"] == "132"


# --- Bug-6603 field-list grouping (decision 2026-07-07) --------------------

def _second_flat_dimension():
    return {
        "id": "dim-region",
        "name": "region",
        "display_name": "Region",
        "description": "Sales region",
        "is_time_dim": False,
        "source_column_id": "col-2",
    }


class TestFieldListGrouping:
    """Standalone dims -> one [Dimensions] group; hierarchies keep own nodes;
    KPIs are a native group. Grouping is on the DIMENSION_UNIQUE_NAME column only —
    hierarchy/level/member unique names stay [Name].[Name] (Execute-safe)."""

    def test_group_unique_name_helper(self):
        flat = cube_model.build_cube_dimensions([_flat_dimension()], [])[0]
        assert cube_model.dimension_unique_name_for(flat) == "[Dimensions]"
        hier = cube_model.build_cube_dimensions([], [_user_hierarchy_def()])[0]
        # Bug-6891: hierarchies collapse into the shared [Hierarchies] group.
        assert cube_model.dimension_unique_name_for(hier) == "[Hierarchies]"

    def test_dimensions_rowset_collapses_standalone_into_one_group(self):
        dims = cube_model.build_cube_dimensions(
            [_flat_dimension(), _second_flat_dimension()],
            [_user_hierarchy_def(), _calendar_hierarchy_def()],
        )
        drows = _rows_dimensions("demo", dims, {})
        unames = [r["DIMENSION_UNIQUE_NAME"] for r in drows]
        # Exactly ONE standalone group node for the two attribute dims.
        assert unames.count("[Dimensions]") == 1
        group = next(r for r in drows if r["DIMENSION_UNIQUE_NAME"] == "[Dimensions]")
        assert group["DIMENSION_NAME"] == "Dimensions"
        assert group["DIMENSION_CAPTION"] == "Dimensions"
        assert group["DIMENSION_CARDINALITY"] == "2"  # two grouped attributes
        # Bug-6891: hierarchies collapse into ONE [Hierarchies] group node.
        assert unames.count("[Hierarchies]") == 1
        assert "[Geography]" not in unames
        assert "[Order Calendar]" not in unames
        # The Measures dimension is still present.
        assert "[Measures]" in unames
        # No per-attribute standalone dimension nodes leaked in.
        assert "[account_type]" not in unames
        assert "[region]" not in unames

    def test_group_absent_when_no_standalone_dims(self):
        dims = cube_model.build_cube_dimensions([], [_user_hierarchy_def()])
        drows = _rows_dimensions("demo", dims, {})
        unames = [r["DIMENSION_UNIQUE_NAME"] for r in drows]
        assert "[Dimensions]" not in unames
        assert "[Hierarchies]" in unames  # Bug-6891 group node
        assert "[Geography]" not in unames

    def test_hierarchies_rowset_groups_attributes_keeps_hier_grammar(self):
        dims = cube_model.build_cube_dimensions(
            [_flat_dimension(), _second_flat_dimension()], [_user_hierarchy_def()],
        )
        hrows = _rows_hierarchies("demo", dims)
        for attr in ("account_type", "region"):
            row = next(r for r in hrows if r["HIERARCHY_NAME"] == attr)
            assert row["DIMENSION_UNIQUE_NAME"] == "[Dimensions]"
            assert row["HIERARCHY_UNIQUE_NAME"] == f"[{attr}].[{attr}]"
            assert row["HIERARCHY_ORIGIN"] == "2"
        geo = next(r for r in hrows if r["HIERARCHY_NAME"] == "Geography")
        # Bug-6891: group key column; the hierarchy grammar is unchanged.
        assert geo["DIMENSION_UNIQUE_NAME"] == "[Hierarchies]"
        assert geo["HIERARCHY_UNIQUE_NAME"] == "[Geography].[Geography]"

    def test_members_rowset_uses_group_column_but_hier_grammar(self):
        dims = cube_model.build_cube_dimensions([_flat_dimension()], [])
        member_data = {
            "account_type": {
                "members": [{"name": "Checking", "ordinal": 0, "parent": ""}],
                "levels": ["account_type"],
                "members_by_level": {
                    0: [{"name": "Checking", "ordinal": 0, "parent": ""}]
                },
            }
        }
        mrows = _rows_members("demo", [], dims, {}, member_data)
        checking = next(r for r in mrows if r.get("MEMBER_NAME") == "Checking")
        assert checking["DIMENSION_UNIQUE_NAME"] == "[Dimensions]"
        assert checking["HIERARCHY_UNIQUE_NAME"] == "[account_type].[account_type]"
        assert checking["MEMBER_UNIQUE_NAME"] == "[account_type].[account_type].[Checking]"

    def test_measuregroup_dimensions_emits_group_node_once(self):
        # Guard for the round-1 dedup fix: N collapsed standalone attrs must yield
        # exactly ONE [Dimensions] measuregroup-dimension row, not N duplicates.
        dims = cube_model.build_cube_dimensions(
            [_flat_dimension(), _second_flat_dimension()], [_user_hierarchy_def()],
        )
        rows = _rows_measuregroup_dimensions("demo", dims, [])
        group_rows = [r for r in rows if r["DIMENSION_UNIQUE_NAME"] == "[Dimensions]"]
        assert len(group_rows) == 1
        # Bug-6891: hierarchies dedupe into one [Hierarchies] group row as well.
        hier_rows = [r for r in rows if r["DIMENSION_UNIQUE_NAME"] == "[Hierarchies]"]
        assert len(hier_rows) == 1

    def test_hidden_standalone_dim_no_dangling_reference(self):
        # Referential integrity: a hidden standalone dim must not emit rows in any
        # rowset that reference a DIMENSION_UNIQUE_NAME absent from MDSCHEMA_DIMENSIONS.
        hidden = {**_second_flat_dimension(), "name": "secret", "id": "dim-secret",
                  "is_hidden": True}
        dims = cube_model.build_cube_dimensions(
            [_flat_dimension(), hidden], [_user_hierarchy_def()],
        )
        drows = _rows_dimensions("demo", dims, {})
        advertised = {r["DIMENSION_UNIQUE_NAME"] for r in drows}
        other_rows = (
            _rows_hierarchies("demo", dims)
            + _rows_levels("demo", dims, {})
            + _rows_members("demo", [], dims, {}, {})
            + _rows_measuregroup_dimensions("demo", dims, [])
            + _rows_md_properties("demo", dims, [], {})
        )
        for r in other_rows:
            du = r.get("DIMENSION_UNIQUE_NAME")
            if du and du != "[Measures]":
                assert du in advertised, f"{du} dangles (not in MDSCHEMA_DIMENSIONS)"
        # The hidden dim's own bracket must appear nowhere.
        assert "[secret]" not in str(other_rows)

    def test_flat_time_dim_keeps_own_time_typed_node(self):
        # A flat is_time_dim dimension must NOT join the [Dimensions] group — the
        # group node is DIMENSION_TYPE 3 but a time dim emits type 1 on its
        # hierarchy row, so grouping it would make DIMENSIONS and HIERARCHIES
        # disagree for the same unique name. It keeps its own time-typed node.
        time_dim = {**_flat_dimension(), "name": "order_date", "id": "dim-od",
                    "is_time_dim": True}
        assert cube_model.is_standalone_attribute(time_dim) is False
        dims = cube_model.build_cube_dimensions(
            [time_dim, _second_flat_dimension()], [],
        )
        drows = _rows_dimensions("demo", dims, {})
        od = next(r for r in drows if r["DIMENSION_NAME"] == "order_date")
        assert od["DIMENSION_UNIQUE_NAME"] == "[order_date]"
        assert od["DIMENSION_TYPE"] == "1"
        hrows = _rows_hierarchies("demo", dims)
        od_h = next(r for r in hrows if r["HIERARCHY_NAME"] == "order_date")
        assert od_h["DIMENSION_UNIQUE_NAME"] == "[order_date]"
        assert od_h["DIMENSION_TYPE"] == "1"
        # the sibling flat non-time dim still groups under [Dimensions]
        region = next(r for r in hrows if r["HIERARCHY_NAME"] == "region")
        assert region["DIMENSION_UNIQUE_NAME"] == "[Dimensions]"

    def test_md_properties_skips_hidden_dim_on_hier_filter(self):
        # A HIERARCHY_UNIQUE_NAME filter naming a HIDDEN dim must not resurrect it
        # via the unknown-dim fallback with a dangling uname; a genuinely unknown
        # dim still gets the fallback.
        hidden = {**_flat_dimension(), "name": "secret", "id": "dim-secret",
                  "is_hidden": True}
        dims = cube_model.build_cube_dimensions([_flat_dimension(), hidden], [])
        hidden_rows = _rows_md_properties(
            "demo", dims, [],
            {"HIERARCHY_UNIQUE_NAME": ["[secret].[secret]"], "PROPERTY_TYPE": ["1"]},
        )
        assert hidden_rows == []
        ghost_rows = _rows_md_properties(
            "demo", dims, [],
            {"HIERARCHY_UNIQUE_NAME": ["[ghost].[ghost]"], "PROPERTY_TYPE": ["1"]},
        )
        assert any(r["DIMENSION_UNIQUE_NAME"] == "[ghost]" for r in ghost_rows)
