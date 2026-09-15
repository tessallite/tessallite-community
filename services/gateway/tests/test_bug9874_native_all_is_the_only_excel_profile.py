"""Bug-9874 -- the calculated-total Excel wire profile is retired.

Excel's wire contract is ONE paired thing (Bug-9772): ``ALL_MEMBER``
advertised in Discover, the native All member as every aggregate coordinate
on the Execute axis, and no intrinsic member-property rows. Six regressions
came from moving one element without the others, so no element is
individually overridable for Excel. With the legacy profile gone there is no
profile switch either: this file pins that Excel always gets native-all,
that the per-element emergency variables still cannot split it, that other
clients are untouched, and that the retired calculated Total never appears.
"""

from __future__ import annotations

import pytest

from src.dax.cube_model import (
    EXCEL_PROFILE_NATIVE_ALL,
    RollupWireMode,
    XmlaClientProfile,
    advertise_all_member,
    advertise_kpis_in_discover,
    advertise_member_properties,
    rollup_wire_mode,
    xmla_client_profile,
)
from src.dax.mdschema import _rows_md_properties, _rows_members
from src.dax.member_uname import canonical_member_uname

_EXCEL = {"SspropInitAppName": "Microsoft Office Excel"}
_OTHER = {"SspropInitAppName": "Some BI Client"}
_DIMS = [{"name": "account_type", "source_column_id": "c1"}]
_MEMBER_PROPS = {"CUBE_NAME": ["modely"], "PROPERTY_TYPE": ["1"],
                 "HIERARCHY_UNIQUE_NAME": ["[Dimensions].[account_type]"]}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch):
    for name in (
        "TESSALLITE_XMLA_EXCEL_PROFILE",
        "TESSALLITE_XMLA_ALL_MEMBER",
        "TESSALLITE_XMLA_SUPPRESS_ROLLUP_ALL",
    ):
        monkeypatch.delenv(name, raising=False)


def test_excel_profile_is_native_all_and_one_object():
    assert EXCEL_PROFILE_NATIVE_ALL == "native-all"
    p = xmla_client_profile(properties=_EXCEL)
    # Another test module reloads ``cube_model``; compare enum members by
    # value and the profile by its class name, never by object identity.
    assert type(p).__name__ == XmlaClientProfile.__name__
    assert (p.advertise_all_member, p.rollup_wire_mode.value, p.advertise_member_properties) == (
        True, RollupWireMode.NATIVE_ALL.value, False,
    )
    # Every accessor agrees with the profile: no second decision path.
    assert advertise_all_member(properties=_EXCEL) is p.advertise_all_member
    assert rollup_wire_mode(properties=_EXCEL).value == p.rollup_wire_mode.value
    assert advertise_member_properties(properties=_EXCEL) is p.advertise_member_properties
    assert advertise_kpis_in_discover(properties=_EXCEL) is p.advertise_kpis


@pytest.mark.parametrize("profile", [None, "native-all", "calculated-total", "calculated"])
@pytest.mark.parametrize("all_member", ["0", "1"])
@pytest.mark.parametrize("suppress", ["0", "1"])
def test_no_environment_value_moves_any_excel_element(
    monkeypatch: pytest.MonkeyPatch, profile, all_member, suppress,
):
    """The retired profile name is inert, and the per-element emergency
    variables that serve other clients still cannot split Excel's pair."""
    if profile is not None:
        monkeypatch.setenv("TESSALLITE_XMLA_EXCEL_PROFILE", profile)
    monkeypatch.setenv("TESSALLITE_XMLA_ALL_MEMBER", all_member)
    monkeypatch.setenv("TESSALLITE_XMLA_SUPPRESS_ROLLUP_ALL", suppress)
    p = xmla_client_profile(properties=_EXCEL)
    assert (p.advertise_all_member, p.rollup_wire_mode.value, p.advertise_member_properties) == (
        True, RollupWireMode.NATIVE_ALL.value, False,
    )


def test_other_clients_keep_native_all_and_member_properties():
    other = xmla_client_profile(properties=_OTHER)
    assert other.advertise_all_member is True
    assert other.rollup_wire_mode.value == RollupWireMode.NATIVE_ALL.value
    assert other.advertise_member_properties is True
    assert len(_rows_md_properties("modely", _DIMS, [], _MEMBER_PROPS, properties=_OTHER)) == 26


def test_excel_gets_no_intrinsic_member_property_rows():
    """The element the Bug-9772 bisect isolated: with the (All) level implicit,
    every enumerated property is a pivot-cache field Excel cannot serialise."""
    assert _rows_md_properties("modely", _DIMS, [], _MEMBER_PROPS, properties=_EXCEL) == []


def test_discover_carries_the_native_all_member_and_no_calculated_total():
    member_data = {"account_type": {"members": [
        {"name": "CREDIT", "caption": "CREDIT"}, {"name": "LOAN", "caption": "LOAN"},
    ]}}
    restr = {"CUBE_NAME": ["modely"], "HIERARCHY_UNIQUE_NAME": ["[Dimensions].[account_type]"]}
    rows = _rows_members("modely", [], _DIMS, restr, member_data, properties=_EXCEL)
    names = [r["MEMBER_NAME"] for r in rows]
    assert "All" in names
    assert "__TessalliteTotal__" not in names
    assert all(r["MEMBER_TYPE"] != "4" for r in rows)


def test_a_source_value_named_like_the_retired_token_is_an_ordinary_member():
    """The token no longer has a reserved meaning: it is a plain caption member,
    while a real ``All`` key still takes the key grammar."""
    assert canonical_member_uname("[D].[H]", "H", ["__TessalliteTotal__"], is_multi_level=False) == "[D].[H].[__TessalliteTotal__]"
    assert canonical_member_uname("[D].[H]", "H", ["All"], is_multi_level=False) == "[D].[H].&[All]"
