from shared.query_log_client_kinds import is_optimizer_workload


def test_real_user_and_app_origins_remain_eligible_including_kpi_and_null():
    for client_kind in (None, "plugin", "headless", "agent", "mcp", "looker_cloud", "kpi"):
        assert is_optimizer_workload(client_kind=client_kind, route_type="source")


def test_explicit_maintenance_origins_and_routes_are_ineligible():
    assert not is_optimizer_workload(client_kind="hierarchy_preview", route_type="source")
    for route_type in ("introspect", "kpi_metadata", "discover_members"):
        assert not is_optimizer_workload(client_kind=None, route_type=route_type)
