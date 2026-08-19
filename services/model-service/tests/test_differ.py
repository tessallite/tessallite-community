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
    # The differ covers every snapshot category (the old 8-entry list
    # silently ignored ~20) — assert against the canonical list so a new
    # category is never dropped from the diff again.
    from shared.model_snapshot.differ import DIFF_CATEGORIES

    result = diff_snapshots(_snap(), _snap())
    assert set(result.keys()) == set(DIFF_CATEGORIES)
    # The original core categories must always remain present.
    core = {"tables", "dimensions", "measures", "joins", "hierarchies",
            "aggregates", "pockets", "personas"}
    assert core <= set(result.keys())


# --- Bug-5916: singleton/dict snapshot keys must diff too -----------------


def test_singleton_model_field_change_detected():
    old = _snap()
    old["model"] = {"id": "mx", "display_name": "Sales Model", "default_timezone": "UTC"}
    new = _snap()
    new["model"] = {"id": "mx", "display_name": "Sales Model", "default_timezone": "America/New_York"}
    result = diff_snapshots(old, new)
    assert "model" in result
    assert result["model"]["changes"]["default_timezone"] == {
        "from": "UTC", "to": "America/New_York",
    }


def test_singleton_model_alias_map_change_detected():
    old = _snap()
    old["model_alias_map"] = {"warehouse": "sales_wh"}
    new = _snap()
    new["model_alias_map"] = {"warehouse": "sales_wh_v2"}
    result = diff_snapshots(old, new)
    assert result["model_alias_map"]["changes"]["warehouse"] == {
        "from": "sales_wh", "to": "sales_wh_v2",
    }


def test_singleton_refresh_sla_config_change_detected():
    old = _snap()
    old["refresh_sla_config"] = {"max_staleness_hours": 24}
    new = _snap()
    new["refresh_sla_config"] = {"max_staleness_hours": 6}
    result = diff_snapshots(old, new)
    assert result["refresh_sla_config"]["changes"]["max_staleness_hours"] == {
        "from": 24, "to": 6,
    }


def test_singleton_ai_scheduler_config_change_detected():
    old = _snap()
    old["ai_scheduler_config"] = {"enabled": False}
    new = _snap()
    new["ai_scheduler_config"] = {"enabled": True}
    result = diff_snapshots(old, new)
    assert result["ai_scheduler_config"]["changes"]["enabled"] == {
        "from": False, "to": True,
    }


def test_singleton_model_settings_change_detected():
    old = _snap()
    old["model_settings"] = {"query_cache_ttl_seconds": 300}
    new = _snap()
    new["model_settings"] = {"query_cache_ttl_seconds": 900}
    result = diff_snapshots(old, new)
    assert result["model_settings"]["changes"]["query_cache_ttl_seconds"] == {
        "from": 300, "to": 900,
    }


def test_singleton_identical_values_produce_no_diff_entry():
    old = _snap()
    old["model"] = {"id": "mx", "display_name": "Sales Model"}
    new = _snap()
    new["model"] = {"id": "mx", "display_name": "Sales Model"}
    result = diff_snapshots(old, new)
    assert "model" not in result


def test_singleton_excludes_metadata_noise_fields():
    # schema_version/exported_at churn on every save and are not meaningful
    # model content changes; they must not trigger a false-positive diff.
    old = _snap()
    old["model_settings"] = {"schema_version": 3, "exported_at": "2026-01-01T00:00:00Z"}
    new = _snap()
    new["model_settings"] = {"schema_version": 3, "exported_at": "2026-07-02T00:00:00Z"}
    result = diff_snapshots(old, new)
    assert "model_settings" not in result


def test_monotonic_counters_are_excluded_from_the_model_category_only():
    """Bug-7982 R7 (review round 5, O1) — the exclusion is category-scoped.

    deploy_epoch / data_epoch / dependency_revision no longer travel in the
    snapshot, so diffing a pre-exclusion version against a post-exclusion one
    would show a phantom "model.data_epoch: 7 -> None" that is not a model
    change (round 4, O4). But model_settings is an arbitrary USER-keyed dict:
    a setting a modeller literally named ``data_epoch`` is real content and
    must still diff. Excluding by field name globally swallowed it silently.

    Mutation proof: revert to ``extra_exclude=_MODEL_ONLY_EXCLUDE`` for every
    category -> RED here, all other differ tests still green.
    """
    old = _snap()
    old["model"] = {"id": "mx", "data_epoch": 7, "deploy_epoch": 3,
                    "dependency_revision": 11}
    old["model_settings"] = {"data_epoch": "nightly", "deploy_epoch": 1,
                             "dependency_revision": "a"}
    new = _snap()
    new["model"] = {"id": "mx"}
    new["model_settings"] = {"data_epoch": "hourly", "deploy_epoch": 2,
                             "dependency_revision": "b"}
    result = diff_snapshots(old, new)
    assert "model" not in result, (
        "a monotonic counter dropped from the snapshot must not diff as a "
        f"model change: {result.get('model')}"
    )
    assert result["model_settings"]["changes"]["data_epoch"] == {
        "from": "nightly", "to": "hourly"}
    assert result["model_settings"]["changes"]["deploy_epoch"] == {"from": 1, "to": 2}
    assert result["model_settings"]["changes"]["dependency_revision"] == {
        "from": "a", "to": "b"}
