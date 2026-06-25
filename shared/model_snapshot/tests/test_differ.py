from shared.model_snapshot.differ import diff_snapshots


def _snap(tables=None, dimensions=None, measures=None):
    return {
        "tables": tables or [],
        "dimensions": dimensions or [],
        "measures": measures or [],
        "joins": [],
        "hierarchies": [],
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
