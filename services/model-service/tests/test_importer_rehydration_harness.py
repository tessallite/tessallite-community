from __future__ import annotations

from tests.importer_rehydration_harness import importer_rehydration_cases


def test_importer_rehydration_harness_covers_ecosystem_importers():
    cases = importer_rehydration_cases()
    assert [case.name for case in cases] == [
        "dbt",
        "cube-with-join",
        "atscale",
        "yaml-roundtrip",
        "catalog",
    ]
    # After the B14/H23 import/export remediation every importer rehydrates
    # cleanly, so no case is allowed to carry an expected-failure marker.
    assert all(case.inject_project_connection for case in cases)


def test_importer_rehydration_cases_build_model_snapshots():
    for case in importer_rehydration_cases():
        snapshot = case.build_snapshot()

        assert snapshot.get("schema_version", 0) >= 1, case.name
        assert snapshot["model"]["slug"], case.name
        assert snapshot["tables"], case.name
        assert snapshot["columns"], case.name
        assert snapshot["dimensions"] or snapshot["measures"], case.name

        # Every fixed importer emits a placeholder data source the endpoint /
        # harness rebinds to a real project_connection_id; the snapshot itself
        # must not pre-bind one.
        assert snapshot["data_sources"], case.name
        assert all(
            "project_connection_id" not in source
            for source in snapshot["data_sources"]
        ), case.name
