"""Table-grained JDBC relation contract required by generated LookML."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.jdbc.server import PGWireServer  # noqa: E402
from src.jdbc.catalogue import CatalogueDB  # noqa: E402
from src.router_client import QueryRouterError, execute_query, fetch_model_metadata  # noqa: E402


@pytest.mark.asyncio
async def test_fetch_model_metadata_adds_table_scoped_looker_relations(monkeypatch) -> None:
    async def models(*_args, **_kwargs):
        return [{"id": "m1", "project_id": "p1", "slug": "7modelx", "description": "Model X"}]

    async def dimensions(*_args, **_kwargs):
        return [
            {"id": "d1", "name": "payment_id", "source_column_id": "c1", "is_hidden": True},
            {"id": "d2", "name": "payment_status", "source_column_id": "c2"},
            {"id": "d3", "name": "account_type_code", "source_column_id": "c3"},
        ]

    async def measures(*_args, **_kwargs):
        return [{"id": "me1", "name": "amount", "source_column_id": "c4", "default_agg": "sum"}]

    async def personas(*_args, **_kwargs):
        return []

    async def snapshot(*_args, **_kwargs):
        return {
            "tables": [
                {"id": "t1", "alias": "payment_transaction", "table_type": "fact", "row_count_estimate": 2500},
                {"id": "t2", "alias": "8dim_account_type", "table_type": "dimension", "row_count_estimate": 12},
            ],
            "columns": [
                {"id": "c1", "model_table_id": "t1", "is_primary_key": True},
                {"id": "c2", "model_table_id": "t1"},
                {"id": "c3", "model_table_id": "t2", "is_primary_key": True, "is_nullable": False},
                {"id": "c4", "model_table_id": "t1"},
            ],
            "joins": [
                {
                    "left_table_id": "t2",
                    "right_table_id": "t1",
                    "left_column_id": "c3",
                    "right_column_id": "c2",
                }
            ],
        }

    import src.router_client as client

    monkeypatch.setattr(client, "list_all_models_for_tenant", models)
    monkeypatch.setattr(client, "get_model_dimensions", dimensions)
    monkeypatch.setattr(client, "get_model_measures", measures)
    monkeypatch.setattr(client, "get_model_personas", personas)
    monkeypatch.setattr(client, "get_model_snapshot", snapshot)
    monkeypatch.setattr(client.settings, "LOOKER_GATEWAY_ENABLED", True)

    (
        names,
        columns,
        model_ids,
        _descriptions,
        _trust,
        _personas,
        include_hidden,
        query_names,
        foreign_keys,
        row_estimates,
        looker_relations,
        _project_slugs,
    ) = await fetch_model_metadata(None, "tenant", "token")

    fact_relation = "field_7modelx__payment_transaction"
    dimension_relation = "field_7modelx__field_8dim_account_type"
    assert fact_relation in names
    assert dimension_relation in names
    assert [item["name"] for item in columns[fact_relation]] == [
        "payment_id",
        "payment_status",
        "amount",
    ]
    assert [item["name"] for item in columns[dimension_relation]] == [
        "account_type_code",
    ]
    assert columns[fact_relation][0]["is_primary_key"] is True
    assert columns[dimension_relation][0]["is_primary_key"] is True
    base_key = next(column for column in columns["7modelx"] if column["name"] == "account_type_code")
    assert base_key["is_primary_key"] is True
    assert base_key["is_nullable"] is False
    assert model_ids[fact_relation] == "m1"
    assert include_hidden[fact_relation] is True
    assert query_names[fact_relation] == "7modelx"
    assert row_estimates["7modelx"] == 2500
    assert row_estimates[fact_relation] == 2500
    assert row_estimates[dimension_relation] == 12
    assert foreign_keys[fact_relation] == [
        {
            "column_name": "payment_status",
            "foreign_table_name": dimension_relation,
            "foreign_column_name": "account_type_code",
        }
    ]
    assert looker_relations == {fact_relation, dimension_relation}

    monkeypatch.setattr(client.settings, "LOOKER_GATEWAY_ENABLED", False)
    disabled = await fetch_model_metadata(None, "tenant", "token")
    assert fact_relation not in disabled[0]
    assert disabled[-2] == {fact_relation, dimension_relation}


def test_generated_relation_resolves_and_rewrites_to_canonical_model() -> None:
    server = PGWireServer()
    server._model_id = "m1"
    server._table_model_id = {
        "modelx__payment_transaction": "m1",
        "modelx__dim_account_type": "m1",
    }
    server._table_persona_id = {
        "modelx__payment_transaction": None,
        "modelx__dim_account_type": None,
    }
    server._table_include_hidden = {"modelx__payment_transaction": True}
    server._table_query_name = {
        "modelx__payment_transaction": "modelx",
        "modelx__dim_account_type": "modelx",
    }

    sql = (
        'SELECT f.payment_id FROM public."modelx__payment_transaction" AS f '
        'JOIN public."modelx__dim_account_type" AS d '
        "ON f.account_type = d.account_type_code "
        "WHERE f.source_name = 'modelx__payment_transaction'"
    )
    assert server._resolve_model_id_and_variant(sql) == ("m1", True, None)
    rewritten = server._rewrite_exposed_relations(sql)
    assert 'public."modelx" AS f' in rewritten
    assert 'public."modelx" AS d' in rewritten
    assert "'modelx__payment_transaction'" in rewritten

    qualified = (
        'SELECT "modelx__payment_transaction".payment_id '
        'FROM public."modelx__payment_transaction"'
    )
    rewritten_qualified = server._rewrite_exposed_relations(qualified)
    assert '"modelx".payment_id' in rewritten_qualified
    assert 'FROM public."modelx"' in rewritten_qualified


@pytest.mark.asyncio
async def test_gateway_preserves_feature_not_supported_sqlstate(monkeypatch) -> None:
    server = PGWireServer()
    server._jwt_token = "token"
    server._tenant_slug = "tenant"
    server._table_model_id = {"modelx": "m1"}
    server._table_include_hidden = {"modelx": False}
    server._table_persona_id = {"modelx": None}
    server._table_query_name = {}

    async def reject(*_args, **_kwargs):
        raise QueryRouterError("window unsupported", 422, sqlstate="0A000")

    class Writer:
        def __init__(self) -> None:
            self.payload = b""

        def write(self, payload: bytes) -> None:
            self.payload += payload

        async def drain(self) -> None:
            pass

    monkeypatch.setattr("src.jdbc.server.execute_query", reject)
    writer = Writer()
    await server._handle_user_query("SELECT ROW_NUMBER() OVER () FROM modelx", writer)

    assert b"C0A000\x00" in writer.payload
    assert b"Mwindow unsupported\x00" in writer.payload


@pytest.mark.asyncio
async def test_generated_relation_plaintext_query_is_rejected_before_forwarding(monkeypatch) -> None:
    monkeypatch.setattr("src.jdbc.server.settings.LOOKER_GATEWAY_ENABLED", True)
    server = PGWireServer()
    server._jwt_token = "token"
    server._tenant_slug = "tenant"
    server._table_model_id = {"modelx__payment_transaction": "m1"}
    server._table_include_hidden = {"modelx__payment_transaction": True}
    server._table_persona_id = {"modelx__payment_transaction": None}
    server._table_query_name = {"modelx__payment_transaction": "modelx"}
    server._looker_relations = {"modelx__payment_transaction"}

    async def should_not_forward(*_args, **_kwargs):
        raise AssertionError("plaintext Looker query must not be forwarded")

    class Writer:
        def __init__(self) -> None:
            self.payload = b""

        def write(self, payload: bytes) -> None:
            self.payload += payload

        async def drain(self) -> None:
            pass

    monkeypatch.setattr("src.jdbc.server.execute_query", should_not_forward)
    writer = Writer()
    await server._handle_user_query(
        "SELECT payment_id FROM public.modelx__payment_transaction",
        writer,
    )

    assert b"C08004\x00" in writer.payload
    assert b"require TLS" in writer.payload


@pytest.mark.asyncio
async def test_disabled_looker_relation_is_rejected_even_with_model_id(monkeypatch) -> None:
    monkeypatch.setattr("src.jdbc.server.settings.LOOKER_GATEWAY_ENABLED", False)
    server = PGWireServer()
    server._tls_active = True
    server._jwt_token = "token"
    server._tenant_slug = "tenant"
    server._model_id = "m1"
    server._looker_relations = {"modelx__payment_transaction"}

    async def should_not_forward(*_args, **_kwargs):
        raise AssertionError("disabled Looker query must not be forwarded")

    class Writer:
        def __init__(self) -> None:
            self.payload = b""

        def write(self, payload: bytes) -> None:
            self.payload += payload

        async def drain(self) -> None:
            pass

    monkeypatch.setattr("src.jdbc.server.execute_query", should_not_forward)
    writer = Writer()
    await server._handle_user_query(
        "SELECT payment_id FROM public.modelx__payment_transaction",
        writer,
    )

    assert b"C0A000\x00" in writer.payload
    assert b"disabled" in writer.payload


@pytest.mark.asyncio
async def test_router_client_reads_feature_sqlstate_envelope(monkeypatch) -> None:
    class Response:
        status_code = 422
        text = ""

        @staticmethod
        def json():
            return {
                "detail": {
                    "message": "window unsupported",
                    "error_type": "feature_not_supported",
                    "sqlstate": "0A000",
                }
            }

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def post(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr("src.router_client.httpx.AsyncClient", lambda **_kwargs: Client())
    with pytest.raises(QueryRouterError) as exc:
        await execute_query("m1", "SELECT 1", "tenant", "token")

    assert exc.value.detail == "window unsupported"
    assert exc.value.sqlstate == "0A000"


@pytest.mark.asyncio
async def test_extended_query_preserves_feature_not_supported_sqlstate(monkeypatch) -> None:
    server = PGWireServer()
    server._jwt_token = "token"
    server._tenant_slug = "tenant"
    server._table_model_id = {"modelx": "m1"}
    server._table_include_hidden = {"modelx": False}
    server._table_persona_id = {"modelx": None}
    server._table_query_name = {}

    async def reject(*_args, **_kwargs):
        raise QueryRouterError("window unsupported", 422, sqlstate="0A000")

    monkeypatch.setattr("src.jdbc.server.execute_query", reject)
    _columns, _rows, error = await server._execute_for_extended("SELECT ROW_NUMBER() OVER () FROM modelx")
    assert error == ("window unsupported", "0A000")


@pytest.mark.asyncio
async def test_gateway_forwards_declared_looker_client_kind(monkeypatch) -> None:
    monkeypatch.setattr("src.jdbc.server.settings.LOOKER_GATEWAY_ENABLED", True)
    server = PGWireServer()
    server._tls_active = True
    server._client_kind = "looker_cloud"
    server._jwt_token = "token"
    server._tenant_slug = "tenant"
    server._table_model_id = {"modelx": "m1"}
    server._table_include_hidden = {"modelx": False}
    server._table_persona_id = {"modelx": None}
    server._table_query_name = {}
    captured = {}

    async def capture(*_args, **kwargs):
        captured.update(kwargs)
        return {"columns": [], "rows": []}

    monkeypatch.setattr("src.jdbc.server.execute_query", capture)
    _columns, _rows, error = await server._execute_for_extended("SELECT payment_id FROM modelx")

    assert error is None
    assert captured["client_kind"] == "looker_cloud"


@pytest.mark.parametrize(
    ("sql", "message"),
    [
        (
            'WITH base AS (SELECT f.payment_id FROM public."modelx__payment_transaction" f '
            'JOIN public."modelx__dim_account_type" d ON f.account_type = d.account_type_code) '
            "SELECT payment_id FROM base",
            "multiple semantic relations",
        ),
        (
            'SELECT ROW_NUMBER() OVER (ORDER BY f.payment_id) FROM public."modelx__payment_transaction" f '
            'JOIN public."modelx__dim_account_type" d ON f.account_type = d.account_type_code',
            "symmetric aggregates not yet supported",
        ),
    ],
)
@pytest.mark.asyncio
async def test_complex_generated_relation_join_rejected_before_rewrite(monkeypatch, sql: str, message: str) -> None:
    server = PGWireServer()
    server._tls_active = True
    server._jwt_token = "token"
    server._tenant_slug = "tenant"
    server._table_model_id = {
        "modelx__payment_transaction": "m1",
        "modelx__dim_account_type": "m1",
    }
    server._table_include_hidden = {"modelx__payment_transaction": True}
    server._table_persona_id = {"modelx__payment_transaction": None}
    server._table_query_name = {
        "modelx__payment_transaction": "modelx",
        "modelx__dim_account_type": "modelx",
    }

    async def should_not_forward(*_args, **_kwargs):
        raise AssertionError("unsupported complex SQL must not be forwarded")

    monkeypatch.setattr("src.jdbc.server.execute_query", should_not_forward)
    _columns, _rows, error = await server._execute_for_extended(sql)

    assert error is not None
    assert error[1] == "0A000"
    assert message in error[0]


def test_declared_keys_are_exposed_to_looker_catalog_probes() -> None:
    names = ["modelx__payment_transaction", "modelx__dim_account_type"]
    columns = {
        "modelx__payment_transaction": [
            {"name": "payment_id", "is_primary_key": True},
            {"name": "account_type", "is_primary_key": False},
            {"name": "amount", "is_primary_key": False},
        ],
        "modelx__dim_account_type": [
            {"name": "account_type_code", "is_primary_key": True},
        ],
    }
    foreign_keys = {
        "modelx__payment_transaction": [
            {
                "column_name": "account_type",
                "foreign_table_name": "modelx__dim_account_type",
                "foreign_column_name": "account_type_code",
            }
        ]
    }
    cat = CatalogueDB(
        model_names=names,
        table_columns=columns,
        table_foreign_keys=foreign_keys,
    )

    # key_column_usage
    result = cat.execute("SELECT * FROM information_schema.key_column_usage")
    assert result is not None
    _, rows = result
    assert [row[2:6] for row in rows] == [
        ["modelx__payment_transaction", "modelx__payment_transaction_pkey", "payment_id", "1"],
        ["modelx__dim_account_type", "modelx__dim_account_type_pkey", "account_type_code", "1"],
    ]

    # pg_index
    result = cat.execute("SELECT * FROM pg_catalog.pg_index")
    assert result is not None
    _, index_rows = result
    assert len(index_rows) == 2
    assert all(row[4] == "t" for row in index_rows)

    # pg_constraint
    result = cat.execute("SELECT * FROM pg_constraint")
    assert result is not None
    _, constraint_rows = result
    assert [row[2] for row in constraint_rows] == ["p", "p", "f"]
    assert constraint_rows[-1][3:] == ["16384", "2", "16385", "1"]

    # referential_constraints (foreign keys)
    result = cat.execute("SELECT * FROM information_schema.referential_constraints")
    assert result is not None
    _, foreign_rows = result
    assert foreign_rows[0][2:] == [
        "modelx__payment_transaction",
        "modelx__payment_transaction_fkey_1",
        "account_type",
        "modelx__dim_account_type",
        "account_type_code",
        "NO ACTION",
        "NO ACTION",
    ]

    cat.close()

    # Disabled Looker relations → empty
    cat_disabled = CatalogueDB(
        model_names=names,
        table_columns=columns,
        looker_enabled=False,
        looker_relations=set(names),
    )
    result = cat_disabled.execute("SELECT * FROM pg_index")
    assert result is not None
    _, hidden_rows = result
    assert hidden_rows == []
    cat_disabled.close()
