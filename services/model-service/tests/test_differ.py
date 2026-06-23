from shared.model_snapshot.differ import diff_snapshots


def _snap(tables=None, dimensions=None, measures=None, joins=None, hierarchies=None):
    return {
        "tables": tables or [],
        "dimensions": dimensions or [],
        "measures": measures or [],
        "joins": joins or [],
        "hierarchies": hierarchies or [],
        "aggregates": [],
        "pockets": [],
        "personas": [],
    }


def test_no_diff_when_identical():
    snap = _snap(tables=[{"id": "t1", "alias": "sales"}])
    result = diff_snapshots(snap, snap)
    assert result["tables"]["added"] == []
    assert result["tables"]["removed"] == []
    assert result["tables"]["changed"] == []


def test_added_table_detected():
    old = _snap(tables=[])
    new = _snap(tables=[{"id": "t1", "alias": "sales", "display_name": "Sales"}])
    result = diff_snapshots(old, new)
    assert len(result["tables"]["added"]) == 1
    assert result["tables"]["added"][0]["alias"] == "sales"


def test_removed_dimension_detected():
    old = _snap(dimensions=[{"id": "d1", "slug": "region"}])
    new = _snap(dimensions=[])
    result = diff_snapshots(old, new)
    assert len(result["dimensions"]["removed"]) == 1
    assert result["dimensions"]["removed"][0]["slug"] == "region"


def test_changed_measure_detected():
    old = _snap(measures=[{"id": "m1", "slug": "revenue", "default_aggregation": "sum"}])
    new = _snap(measures=[{"id": "m1", "slug": "revenue", "default_aggregation": "avg"}])
    result = diff_snapshots(old, new)
    assert len(result["measures"]["changed"]) == 1
    ch = result["measures"]["changed"][0]
    assert ch["slug"] == "revenue"
    assert "default_aggregation" in ch["changes"]
    assert ch["changes"]["default_aggregation"] == {"from": "sum", "to": "avg"}


def test_empty_both_snapshots_returns_empty_diffs():
    result = diff_snapshots({}, {})
    for cat in result.values():
        assert cat["added"] == []
        assert cat["removed"] == []
        assert cat["changed"] == []


def test_multiple_field_changes_all_reported():
    old = _snap(measures=[{"id": "m1", "slug": "rev", "agg": "sum", "format": "usd"}])
    new = _snap(measures=[{"id": "m1", "slug": "rev", "agg": "avg", "format": "eur"}])
    result = diff_snapshots(old, new)
    ch = result["measures"]["changed"][0]
    assert "agg" in ch["changes"]
    assert "format" in ch["changes"]
    assert ch["changes"]["agg"] == {"from": "sum", "to": "avg"}
    assert ch["changes"]["format"] == {"from": "usd", "to": "eur"}


def test_items_without_id_field_are_skipped():
    old = _snap(tables=[{"alias": "orphan"}])  # no 'id' key
    new = _snap(tables=[{"alias": "orphan"}])
    result = diff_snapshots(old, new)
    # Items without id are excluded from all diff sets
    assert result["tables"]["added"] == []
    assert result["tables"]["removed"] == []
    assert result["tables"]["changed"] == []


def test_added_and_removed_in_same_category():
    old = _snap(tables=[{"id": "t1", "alias": "old_table"}])
    new = _snap(tables=[{"id": "t2", "alias": "new_table"}])
    result = diff_snapshots(old, new)
    assert len(result["tables"]["added"]) == 1
    assert len(result["tables"]["removed"]) == 1
    assert result["tables"]["changed"] == []


def test_unchanged_item_not_reported_as_changed():
    snap = _snap(measures=[{"id": "m1", "slug": "cost", "agg": "sum"}])
    result = diff_snapshots(snap, snap)
    assert result["measures"]["changed"] == []


def test_all_categories_present_in_result():
    result = diff_snapshots(_snap(), _snap())
    expected = {"tables", "dimensions", "measures", "joins", "hierarchies", "aggregates", "pockets", "personas"}
    assert set(result.keys()) == expected
