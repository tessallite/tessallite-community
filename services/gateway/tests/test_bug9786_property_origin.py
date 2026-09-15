"""Bug-9786 — intrinsic properties were advertised as USER-DEFINED, corrupting saves.

Saving a PivotTable failed with "Damage to the file was so extensive that repairs
were not possible", and the recovered workbook reopened as plain values with no
PivotTable. Saving a MEASURE-ONLY sheet worked; adding one dimension broke it.

That difference is the whole diagnosis. With no dimension on the axis Excel never
asks for member properties. With one, it issues

    MDSCHEMA_PROPERTIES  HIERARCHY_UNIQUE_NAME=[Dimensions].[account_type]
                         PROPERTY_TYPE=1

as the LAST request before each failed save, and got back 13 properties per level
-- MEMBER_KEY, MEMBER_CAPTION, PARENT_UNIQUE_NAME, DISPLAY_INFO and the rest --
every one of them marked ``PROPERTY_ORIGIN = 1``, MD_PROPERTY_ORIGIN_USER_DEFINED.

Those are INTRINSIC properties that every OLAP provider has. Declaring them
user-defined tells the client the cube carries 13 CUSTOM member properties on
every level (2,419 rows across this model's 186 levels), with names that collide
with the client's own intrinsic ones. Excel wrote them into the PivotTable cache
and produced a file it could not read back.

Per the MDPROP_ORIGIN enumeration: 1 = USER_DEFINED, 2 = SYSTEM_ENABLED
(intrinsic), 4 = SYSTEM_INTERNAL.
"""

import pytest

from src.dax.mdschema import _rows_md_properties

CUBE = "modely"
DIMS = [
    {"name": "account_type", "source_column_id": "c1"},
    {"name": "channel_name", "source_column_id": "c2"},
]


def _rows(prop_type, hier=None):
    """Restrictions are LIST-valued, matching what the SOAP layer produces —
    the builder ignores scalar values, so a scalar here would silently test
    nothing."""
    restr = {"CUBE_NAME": [CUBE], "PROPERTY_TYPE": [prop_type]}
    if hier:
        restr["HIERARCHY_UNIQUE_NAME"] = [hier]
    return _rows_md_properties(CUBE, DIMS, [], restr)


class TestIntrinsicPropertiesAreNotUserDefined:
    def test_member_properties_are_system_enabled(self):
        rows = _rows("1")
        assert rows, "no member property rows were produced"
        bad = [r for r in rows if str(r.get("PROPERTY_ORIGIN")) == "1"]
        assert not bad, (
            f"{len(bad)} intrinsic member properties still claim "
            "PROPERTY_ORIGIN=1 (USER_DEFINED); a client will try to cache them "
            "as custom member properties"
        )
        assert all(str(r.get("PROPERTY_ORIGIN")) == "2" for r in rows)

    def test_cell_properties_are_system_enabled(self):
        """VALUE, FORMAT_STRING, BACK_COLOR and friends are equally intrinsic —
        the same wrong constant was used at both sites, so both are pinned."""
        rows = _rows("2")
        assert rows
        assert all(str(r.get("PROPERTY_ORIGIN")) == "2" for r in rows)

    def test_the_exact_request_excel_made_before_each_failed_save(self):
        rows = _rows("1", "[Dimensions].[account_type]")
        assert rows, "the request that preceded the failure returned nothing"
        assert all(str(r.get("PROPERTY_ORIGIN")) == "2" for r in rows)

    @pytest.mark.parametrize("name", [
        "MEMBER_KEY", "MEMBER_VALUE", "MEMBER_NAME", "MEMBER_UNIQUE_NAME",
        "MEMBER_CAPTION", "LEVEL_UNIQUE_NAME", "LEVEL_NUMBER",
        "PARENT_UNIQUE_NAME", "HIERARCHY_UNIQUE_NAME", "MEMBER_TYPE",
        "MEMBER_ORDINAL", "CHILDREN_CARDINALITY", "DISPLAY_INFO",
    ])
    def test_each_advertised_member_property_is_a_known_intrinsic_one(self, name):
        """If a genuinely user-defined property is ever added, this test fails and
        forces the origin to be decided per property rather than blanket-set."""
        rows = [r for r in _rows("1") if r.get("PROPERTY_NAME") == name]
        assert rows, f"{name} is no longer advertised"
        assert all(str(r.get("PROPERTY_ORIGIN")) == "2" for r in rows)

    def test_no_property_claims_the_internal_origin(self):
        """4 = SYSTEM_INTERNAL means 'not for clients'; advertising a property
        clients are expected to use under that origin would hide it."""
        for pt in ("1", "2"):
            assert all(str(r.get("PROPERTY_ORIGIN")) != "4" for r in _rows(pt))
