"""CP-12 F-018-01 / G-018-01 / G-104-02 / F-018-17: the shipped acme-demo bundle
must carry at least one sql_fixed named list on modely that a SQL query can
reference as IN (@List) and have expand to typed literals (aggregate-matchable).

Both demo tenants seed modely from this bundle, so proving expansion here proves
the sold SQL named-list journey on acme-demo AND the Community demo tenant.
"""
from __future__ import annotations

import json
from pathlib import Path

from src.params.named_list_resolver import _extract_lists_from_snapshot, expand_named_lists

BUNDLE = Path(__file__).resolve().parents[3] / "seeds" / "acme-demo" / "project.json"


def _modely_lists():
    bundle = json.loads(BUNDLE.read_text(encoding="utf-8-sig"))
    modely = next(m for m in bundle["models"] if m["model"]["slug"] == "modely")
    return _extract_lists_from_snapshot({"named_sets": modely["named_sets"]})


def test_demolist_is_a_seeded_sql_fixed_list():
    lists = _modely_lists()
    assert "@demolist" in lists, "modely must ship a sql_fixed @DemoList"
    dl = lists["@demolist"]
    assert dl.list_type == "sql_fixed"
    assert dl.members == ["LON", "NYC", "PAR"]


def test_demolist_expands_to_typed_literals():
    """IN (@DemoList) must expand to typed string literals with the placeholder
    gone — this is the 200-serving path the acceptance checks over JDBC."""
    lists = _modely_lists()
    sql = "SELECT region_code, COUNT(*) FROM modely WHERE region_code IN (@DemoList) GROUP BY region_code"
    out, audit = expand_named_lists(sql, lists, dialect="postgres")
    assert "@DemoList" not in out
    for member in ("LON", "NYC", "PAR"):
        assert f"'{member}'" in out, f"expected typed literal for {member} in {out!r}"
    assert audit, "expansion must record an audit entry"


def test_topn_sql_fixed_list_also_expands():
    lists = _modely_lists()
    assert "@topregionsbyvalue" in lists
    out, _ = expand_named_lists(
        "SELECT * FROM modely WHERE region_code IN (@TopRegionsByValue)",
        lists, dialect="postgres",
    )
    assert "@TopRegionsByValue" not in out
    assert "'LON'" in out
