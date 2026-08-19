"""Bug-5878: JDBC startup ``database`` param parsing.

The dbname accepts <tenant>, <tenant>/<model>, or <tenant>/<project>/<model>.
Malformed values (empty segments, >3 parts) return None so the server can
reject the connection with a clear 3D000 message instead of leaking a
"project/model" string into the query-router's UUID parsing.
"""
from src.jdbc.server import _parse_database_param


class TestParseDatabaseParam:
    def test_tenant_only(self):
        assert _parse_database_param("acme-demo") == ("acme-demo", None, None)

    def test_tenant_and_model(self):
        assert _parse_database_param("acme-demo/modely") == (
            "acme-demo", None, "modely",
        )

    def test_tenant_project_model(self):
        assert _parse_database_param("acme-demo/project1/modely") == (
            "acme-demo", "project1", "modely",
        )

    def test_four_parts_rejected(self):
        assert _parse_database_param("a/b/c/d") is None

    def test_empty_middle_segment_rejected(self):
        assert _parse_database_param("acme-demo//modely") is None

    def test_trailing_slash_rejected(self):
        assert _parse_database_param("acme-demo/modely/") is None

    def test_leading_slash_rejected(self):
        assert _parse_database_param("/acme-demo/modely") is None
