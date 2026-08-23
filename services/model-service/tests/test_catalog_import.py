"""F-020-04 / F-020-E1: catalog import bundle-shape contract.

These guard the defect that made the catalog importer raise
SnapshotSchemaError on every call: the per-model snapshot omitted
schema_version and used a "sources" key the rehydrator never reads.

Bug-5266: slug-collision retry must use a SAVEPOINT so the placeholder
ProjectConnection flushed before the retry loop is not rolled back.

Bug-5561: catalog importer now delegates slug allocation to the shared
``insert_model_with_slug_retry`` from ``slug_utils.py``.

Bug-5724: SSRF defence-in-depth (IPv6-mapped, is_private, follow_redirects).
"""
from __future__ import annotations

import contextlib
import ipaddress
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import IntegrityError

from shared.model_snapshot.slug_utils import slug_with_suffix
from src.api.catalog_import import _catalog_to_bundle, _is_ssrf_blocked, _slugify


_TABLES = [
    {
        "name": "orders",
        "description": "fact table",
        "fields": [
            {"name": "amount", "data_type": "decimal(18,2)", "description": ""},
            {"name": "region", "data_type": "varchar", "description": ""},
            {"name": "qty", "data_type": "int", "description": ""},
        ],
    },
]


def test_bundle_snapshot_carries_schema_version_and_data_sources():
    model_id = "11111111-1111-1111-1111-111111111111"
    bundle, tbl, dim, meas = _catalog_to_bundle(
        _TABLES, model_id, "orders", "Orders",
    )
    snap = bundle["models"][0]
    # F-020-04: the per-model snapshot must carry schema_version (the
    # rehydrator raises SnapshotSchemaError without it).
    assert snap["schema_version"] == 2
    # It must use "data_sources" (read by the rehydrator), NOT "sources".
    assert "sources" not in snap
    assert len(snap["data_sources"]) == 1
    ds = snap["data_sources"][0]
    assert ds["source_type"] == "import_placeholder"
    # Tables bind to the placeholder source via source_id.
    assert snap["tables"][0]["source_id"] == ds["id"]


def test_numeric_columns_become_measures_rest_dimensions():
    model_id = "22222222-2222-2222-2222-222222222222"
    bundle, tbl, dim, meas = _catalog_to_bundle(
        _TABLES, model_id, "orders", "Orders",
    )
    assert tbl == 1
    assert meas == 2  # amount + qty
    assert dim == 1   # region
    snap = bundle["models"][0]
    assert all(m["default_agg"] == "sum" for m in snap["measures"])


def test_table_type_is_documented_not_unclassified():
    # F-020-23: table_type defaults to a documented value (dim_detail), not
    # the undocumented "unclassified" downstream consumers do not switch on.
    bundle, *_ = _catalog_to_bundle(
        _TABLES, "33333333-3333-3333-3333-333333333333", "orders", "Orders",
    )
    assert bundle["models"][0]["tables"][0]["table_type"] == "dim_detail"


def test_slug_headroom_keeps_suffix_under_64_chars():
    # F-020-19: a 64-char slug plus a collision suffix must not overflow the
    # String(64) column. Bug-5561: now uses the shared slug_with_suffix.
    long_slug = "a" * 64
    assert len(slug_with_suffix(long_slug, 12)) <= 64


def test_slugify_bounds_length_and_falls_back():
    assert _slugify("x" * 200) == "x" * 64
    assert _slugify("!!!") == "catalog_model"


def test_slugify_digit_leading_is_bi_safe():
    # Bug-7622: a catalog table/model named with a digit-leading name (e.g.
    # "123_orders") must slugify to a BI-safe slug, not one that later trips
    # validate_bi_safe_slug and 500s the endpoint.
    from shared.model_snapshot.slug_utils import validate_bi_safe_slug

    slug = _slugify("123_orders")
    assert slug == "_123_orders"
    validate_bi_safe_slug(slug)  # must not raise


def test_slugify_symbol_only_falls_back_bi_safe():
    from shared.model_snapshot.slug_utils import validate_bi_safe_slug

    slug = _slugify("$$$")
    assert slug == "catalog_model"
    validate_bi_safe_slug(slug)  # must not raise


# ---------------------------------------------------------------------------
# Bug-5937: catalog import must require HTTPS by default (the request sends
# an Authorization bearer token to the supplied URL).
# ---------------------------------------------------------------------------


class TestValidateCatalogUrlTransportSecurity:
    def test_https_url_accepted_by_default(self):
        from src.api.catalog_import import _validate_catalog_url
        assert _validate_catalog_url("https://catalog.example.com") == "https://catalog.example.com"

    def test_http_url_rejected_by_default(self):
        from src.api.catalog_import import _validate_catalog_url
        with pytest.raises(ValueError, match="https"):
            _validate_catalog_url("http://catalog.example.com")

    def test_http_url_accepted_when_operator_opts_in(self, monkeypatch):
        from shared.config.settings import get_settings
        from src.api.catalog_import import _validate_catalog_url

        monkeypatch.setattr(get_settings(), "CATALOG_IMPORT_ALLOW_HTTP", True)
        assert _validate_catalog_url("http://catalog.internal") == "http://catalog.internal"

    def test_http_rejected_again_after_opt_in_disabled(self, monkeypatch):
        # Regression guard: the flag must be re-checked per call, not cached
        # from a prior enabled state.
        from shared.config.settings import get_settings
        from src.api.catalog_import import _validate_catalog_url

        monkeypatch.setattr(get_settings(), "CATALOG_IMPORT_ALLOW_HTTP", True)
        _validate_catalog_url("http://catalog.internal")
        monkeypatch.setattr(get_settings(), "CATALOG_IMPORT_ALLOW_HTTP", False)
        with pytest.raises(ValueError, match="https"):
            _validate_catalog_url("http://catalog.internal")

    def test_http_opt_in_does_not_bypass_ssrf_host_block(self, monkeypatch):
        # The transport-encryption flag must not weaken the separate
        # host/IP SSRF checks (_BLOCKED_HOSTS / _is_ssrf_blocked).
        from shared.config.settings import get_settings
        from src.api.catalog_import import _validate_catalog_url

        monkeypatch.setattr(get_settings(), "CATALOG_IMPORT_ALLOW_HTTP", True)
        with pytest.raises(ValueError, match="not allowed"):
            _validate_catalog_url("http://localhost")


# ---------------------------------------------------------------------------
# Bug-5266: slug-collision retry uses SAVEPOINT, not full rollback
# ---------------------------------------------------------------------------


def _make_import_db(*, fail_flush_count=0):
    """Mock AsyncSession with begin_nested() as an async context manager.

    ``fail_flush_count`` controls how many consecutive flush calls inside a
    savepoint raise IntegrityError before succeeding. Crucially, db.rollback
    is tracked so the test can assert it was NOT called (the savepoint
    handles the rollback internally).
    """
    db = AsyncMock()
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()

    flush_calls = {"n": 0}

    async def _flush():
        flush_calls["n"] += 1
        if flush_calls["n"] <= fail_flush_count:
            raise IntegrityError("INSERT", {}, Exception("duplicate slug"))

    db.flush = AsyncMock(side_effect=_flush)

    @contextlib.asynccontextmanager
    async def _begin_nested():
        yield None

    db.begin_nested = MagicMock(side_effect=lambda: _begin_nested())

    # db.execute returns for:
    #   1) select(ProjectConnection.id) → conn_q.first() returns a fake row
    #   2) select(Model.slug) → existing_q.all() returns []
    default_result = MagicMock()
    default_result.first.return_value = (uuid.uuid4(),)
    default_result.all.return_value = []
    db.execute = AsyncMock(return_value=default_result)
    db.get = AsyncMock(
        return_value=MagicMock(id=uuid.uuid4(), slug="test-project")
    )

    return db


@pytest.mark.asyncio
async def test_slug_collision_retry_preserves_placeholder_connection():
    """Bug-5266 / Bug-5561: when a slug collision triggers an IntegrityError
    on the model flush, the shared ``insert_model_with_slug_retry`` must use
    a SAVEPOINT (begin_nested) so that the placeholder ProjectConnection
    flushed earlier is NOT rolled back.

    The catalog importer now delegates to the shared utility; this test
    verifies the SAVEPOINT contract through that utility directly.
    """
    from shared.model_snapshot.slug_utils import insert_model_with_slug_retry

    db = _make_import_db(fail_flush_count=1)

    existing_slugs: set[str] = set()
    model, candidate = await insert_model_with_slug_retry(
        db,
        project_id=uuid.uuid4(),
        base_slug="test_model",
        existing_slugs=existing_slugs,
        display_name="Test Model",
    )

    # begin_nested was called twice: once for the failed attempt, once for
    # the successful one.
    assert db.begin_nested.call_count == 2
    # The critical assertion: db.rollback() must NOT have been called.
    # The savepoint handles the IntegrityError rollback internally.
    db.rollback.assert_not_awaited()
    # The model was created with some slug
    assert candidate is not None


# ---------------------------------------------------------------------------
# Bug-5724: SSRF defence-in-depth — _is_ssrf_blocked
# ---------------------------------------------------------------------------


class TestIsSSRFBlocked:
    """Bug-5724: validate the defence-in-depth IP classification function."""

    def test_global_ipv4_allowed(self):
        assert _is_ssrf_blocked(ipaddress.ip_address("8.8.8.8")) is None

    def test_loopback_blocked(self):
        result = _is_ssrf_blocked(ipaddress.ip_address("127.0.0.1"))
        assert result is not None
        assert "loopback" in result

    def test_private_rfc1918_blocked(self):
        for addr in ("10.0.0.1", "172.16.0.1", "192.168.1.1"):
            result = _is_ssrf_blocked(ipaddress.ip_address(addr))
            assert result is not None, f"{addr} should be blocked"
            assert "private" in result

    def test_link_local_blocked(self):
        # 169.254.169.254 (cloud metadata endpoint) is both private and
        # link-local; the function blocks it regardless of which check fires.
        result = _is_ssrf_blocked(ipaddress.ip_address("169.254.169.254"))
        assert result is not None

    def test_multicast_blocked(self):
        result = _is_ssrf_blocked(ipaddress.ip_address("224.0.0.1"))
        assert result is not None

    def test_ipv6_mapped_loopback_blocked(self):
        """DNS rebinding via IPv6-mapped IPv4 loopback must be caught."""
        result = _is_ssrf_blocked(ipaddress.ip_address("::ffff:127.0.0.1"))
        assert result is not None
        assert "loopback" in result

    def test_ipv6_mapped_private_blocked(self):
        """DNS rebinding via IPv6-mapped RFC1918 must be caught."""
        result = _is_ssrf_blocked(ipaddress.ip_address("::ffff:192.168.1.1"))
        assert result is not None
        assert "private" in result

    def test_ipv6_mapped_link_local_blocked(self):
        """Cloud metadata endpoint via IPv6-mapped link-local must be caught."""
        result = _is_ssrf_blocked(ipaddress.ip_address("::ffff:169.254.169.254"))
        assert result is not None

    def test_global_ipv6_allowed(self):
        # Google's public DNS IPv6 address
        assert _is_ssrf_blocked(ipaddress.ip_address("2001:4860:4860::8888")) is None

    def test_ipv6_loopback_blocked(self):
        result = _is_ssrf_blocked(ipaddress.ip_address("::1"))
        assert result is not None
        assert "loopback" in result

    def test_ipv6_link_local_blocked(self):
        result = _is_ssrf_blocked(ipaddress.ip_address("fe80::1"))
        assert result is not None


def test_ssrf_safe_client_disables_redirects():
    """Bug-5724: the SSRF-safe client must not follow HTTP redirects."""
    from src.api.catalog_import import _ssrf_safe_client

    client = _ssrf_safe_client()
    assert client.follow_redirects is False
