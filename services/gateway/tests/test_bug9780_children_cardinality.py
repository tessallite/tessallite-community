"""Bug-9780 — CHILDREN_CARDINALITY must not contradict has_children."""
import pytest
from src.dax.mdx_execute import _build_multi_hierarchy_row_tuples
from src.dax.subtotal_engine import SUBTOTAL_GRAIN_PREFIX, SubtotalHierarchy, SubtotalLevel


def _h(name):
    return SubtotalHierarchy(
        hierarchy_name=name, mdx_dim_name=name, mdx_hier_name=name,
        levels=[SubtotalLevel(name=name, ordinal=0, dim_name=name)], axis=1,
    )


def _rows():
    """The nested-PivotTable shape: All/All, All/<each>, <each>/All, leaves."""
    out = []
    for a in ("__ALL__", "CREDIT", "CURRENT"):
        for b in ("__ALL__", "PIN", "OTP"):
            out.append({
                "account_type": None if a == "__ALL__" else a,
                "auth_method": None if b == "__ALL__" else b,
                SUBTOTAL_GRAIN_PREFIX + "account_type": -1 if a == "__ALL__" else 0,
                SUBTOTAL_GRAIN_PREFIX + "auth_method": -1 if b == "__ALL__" else 0,
            })
    return out


def test_all_member_reports_its_real_child_count():
    tuples = _build_multi_hierarchy_row_tuples(
        _rows(), [_h("account_type"), _h("auth_method")])
    for members in tuples:
        for m in members:
            if m["name"] == "All":
                assert int(m["children_cardinality"]) == 2, (
                    "the All member reported "
                    f"{m['children_cardinality']} children while carrying "
                    "has_children=True — a client reading CHILDREN_CARDINALITY "
                    "is told this rollup member is a leaf"
                )
                return
    pytest.fail("no All member was built")


def test_no_member_claims_children_while_reporting_zero():
    tuples = _build_multi_hierarchy_row_tuples(
        _rows(), [_h("account_type"), _h("auth_method")])
    for members in tuples:
        for m in members:
            if m.get("has_children"):
                assert int(m["children_cardinality"]) > 0, (
                    f"{m['uname']} says has_children=True but reports 0 children"
                )


def test_leaf_members_still_report_zero():
    tuples = _build_multi_hierarchy_row_tuples(
        _rows(), [_h("account_type"), _h("auth_method")])
    for members in tuples:
        for m in members:
            if not m.get("has_children"):
                assert int(m["children_cardinality"]) == 0
