"""Testes rápidos (sem Docker): config, erros, metadados e o cliente REST.

O catálogo é simulado com ``httpx2.MockTransport``.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from importlib.metadata import entry_points
from importlib.resources import files
from typing import Any
from urllib.parse import parse_qs

import httpx2
import pytest

from dengcode_adapter_iceberg_rest.catalog import IcebergRestCatalog, build_settings
from dengcode_adapter_iceberg_rest.metadata import current_schema, render_type, row_count
from dengcode_adapter_iceberg_rest.rest import encode_namespace, map_status
from dengcode_core.errors import (
    AdapterError,
    AuthError,
    ConfigError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
    TransientError,
    UnsupportedOperation,
)
from dengcode_core.ports.catalog import CatalogPort
from dengcode_core.ports.models import AdapterContext, PortKind, TableRef

CTX = AdapterContext(workspace="teste")
Handler = Callable[[httpx2.Request], httpx2.Response]

SCHEMA = {
    "type": "struct",
    "schema-id": 1,
    "fields": [
        {"id": 1, "name": "id", "type": "long", "required": True, "doc": "chave"},
        {"id": 2, "name": "email", "type": "string", "required": False},
        {
            "id": 3,
            "name": "tags",
            "type": {"type": "list", "element-id": 4, "element": "string"},
            "required": False,
        },
    ],
}
METADATA = {
    "format-version": 2,
    "location": "s3://lago/vendas/clientes",
    "current-schema-id": 1,
    "schemas": [{"type": "struct", "schema-id": 0, "fields": []}, SCHEMA],
    "current-snapshot-id": 20,
    "snapshots": [
        {"snapshot-id": 10, "summary": {"operation": "append", "total-records": "1"}},
        {"snapshot-id": 20, "summary": {"operation": "append", "total-records": "3"}},
    ],
    "properties": {"comment": "clientes ativos"},
}


def error_body(message: str, kind: str, code: int) -> str:
    return json.dumps(
        {"error": {"message": message, "type": kind, "code": code, "stack": ["at x.Y"]}}
    )


class FakeRest:
    """Catálogo REST de mentira: rotas fixas + registro das requisições."""

    def __init__(self, routes: dict[str, Any], *, config: dict[str, Any] | None = None) -> None:
        self.routes = routes
        self.config = config or {"defaults": {}, "overrides": {}}
        self.requests: list[httpx2.Request] = []

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        path = request.url.raw_path.decode().split("?", 1)[0]
        if path == "/v1/config":
            return httpx2.Response(200, json=self.config)
        query = parse_qs(request.url.query.decode())
        key = path + (
            "?" + "&".join(f"{k}={v[0]}" for k, v in sorted(query.items())) if query else ""
        )
        answer = self.routes.get(key, self.routes.get(path))
        if answer is None:
            return httpx2.Response(
                404, text=error_body(f"Not found: {path}", "NoSuchTableException", 404)
            )
        if isinstance(answer, httpx2.Response):
            return answer
        return httpx2.Response(200, json=answer)

    def catalog(self, **cfg: Any) -> IcebergRestCatalog:
        base = {"url": "http://rest.test", **cfg}
        return IcebergRestCatalog.from_config(
            base, CTX, transport_factory=lambda: httpx2.MockTransport(self.handle)
        )


# ------------------------------------------------------------------ configuração
def test_settings() -> None:
    s = build_settings(
        {"url": "http://localhost:8181/v1/", "warehouse": "lago", "token": "tok-secreto"}
    )
    assert s.url == "http://localhost:8181"
    assert s.warehouse == "lago"
    assert "tok-secreto" not in repr(s)
    catalog = IcebergRestCatalog.from_config({"url": "http://x", "credential": "id:segredo"}, CTX)
    assert "segredo" not in repr(catalog)


@pytest.mark.parametrize(
    ("cfg", "match"),
    [
        ({}, "url"),
        ({"url": "localhost:8181"}, "url"),
        ({"url": "http://x", "token": 123}, "token"),
        ({"url": "http://x", "timeout_s": 0}, "timeout_s"),
        ({"url": "http://x", "timeout_s": "abc"}, "timeout_s"),
        ({"url": "http://x", "warehouse": 1}, "warehouse"),
    ],
)
def test_settings_invalid(cfg: dict[str, Any], match: str) -> None:
    with pytest.raises(ConfigError, match=match):
        build_settings(cfg)


def test_entry_point_info_and_port() -> None:
    eps = {ep.name: ep.value for ep in entry_points(group="dengcode.adapters")}
    assert eps["iceberg_rest"] == "dengcode_adapter_iceberg_rest.catalog:IcebergRestCatalog"
    info = IcebergRestCatalog.info
    assert (info.name, info.port) == ("iceberg_rest", PortKind.CATALOG)
    assert info.knowledge_pack == "dengcode_adapter_iceberg_rest:knowledge/iceberg.md"
    module, _, rel = info.knowledge_pack.partition(":")
    assert files(module).joinpath(rel).read_text(encoding="utf-8").startswith("# ")
    catalog = IcebergRestCatalog.from_config({"url": "http://x"}, CTX)
    assert isinstance(catalog, CatalogPort)
    assert catalog.capabilities().supports_column_stats is False


# ------------------------------------------------------------------ erros
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (404, NotFoundError),
        (401, AuthError),
        (419, AuthError),
        (403, PermissionDeniedError),
        (429, RateLimitError),
        (500, TransientError),
        (503, TransientError),
        (400, AdapterError),
        (406, AdapterError),
    ],
)
def test_map_status(status: int, expected: type[Exception]) -> None:
    err = map_status(status, error_body("Table does not exist: a.b", "NoSuchTableException", 404))
    assert type(err) is expected
    assert "Table does not exist" in str(err)
    assert "at x.Y" not in str(err)  # sem stack trace do servidor


def test_map_status_plain_body_and_retry_after() -> None:
    err = map_status(429, "devagar", retry_after="3")
    assert isinstance(err, RateLimitError)
    assert err.retry_after_s == 3
    assert "devagar" in str(err)


def test_encode_namespace() -> None:
    assert encode_namespace(["vendas"]) == "vendas"
    assert encode_namespace(["vendas", "bronze"]) == "vendas%1Fbronze"
    assert encode_namespace(["a b"]) == "a%20b"


# ------------------------------------------------------------------ metadados
def test_render_type_nested() -> None:
    assert render_type("decimal(10, 2)") == "decimal(10, 2)"
    assert render_type({"type": "list", "element": "int"}) == "list<int>"
    assert render_type({"type": "map", "key": "string", "value": "long"}) == "map<string, long>"
    struct = {
        "type": "struct",
        "fields": [
            {"name": "rua", "type": "string"},
            {"name": "num", "type": {"type": "list", "element": "int"}},
        ],
    }
    assert render_type(struct) == "struct<rua: string, num: list<int>>"
    with pytest.raises(AdapterError):
        render_type(42)


def test_current_schema_versions() -> None:
    assert current_schema(METADATA) is SCHEMA
    legacy = {"format-version": 1, "schema": {"type": "struct", "fields": []}}
    assert current_schema(legacy) == {"type": "struct", "fields": []}
    with pytest.raises(AdapterError):
        current_schema({})


def test_row_count_cases() -> None:
    assert row_count(METADATA) == 3
    assert row_count({**METADATA, "current-snapshot-id": None}) == 0
    assert row_count({**METADATA, "current-snapshot-id": -1}) == 0
    assert row_count({**METADATA, "current-snapshot-id": 99}) is None
    no_total = {**METADATA, "snapshots": [{"snapshot-id": 20, "summary": {}}]}
    assert row_count(no_total) is None


# ------------------------------------------------------------------ cliente REST
async def test_describe_maps_metadata() -> None:
    fake = FakeRest({"/v1/namespaces/vendas/tables/clientes": {"metadata": METADATA}})
    info = await fake.catalog(catalog_name="iceberg").describe(
        TableRef(schema="vendas", name="clientes")
    )
    assert info.ref == TableRef(catalog="iceberg", schema="vendas", name="clientes")
    assert [(c.name, c.data_type, c.nullable, c.description) for c in info.columns] == [
        ("id", "long", False, "chave"),
        ("email", "string", True, None),
        ("tags", "list<string>", True, None),
    ]
    assert info.table_format == "iceberg"
    assert info.location == "s3://lago/vendas/clientes"
    assert info.row_count == 3
    assert info.description == "clientes ativos"
    assert len(info.schema_hash) == 16


async def test_prefix_from_config_and_nested_namespace() -> None:
    fake = FakeRest(
        {"/v1/lago/namespaces/vendas%1Fbronze/tables/pedidos": {"metadata": METADATA}},
        config={"defaults": {}, "overrides": {"prefix": "lago"}},
    )
    info = await fake.catalog(warehouse="s3://lago").describe(
        TableRef(schema="vendas.bronze", name="pedidos")
    )
    assert info.ref.schema_ == "vendas.bronze"
    config_req = fake.requests[0]
    assert config_req.url.path == "/v1/config"
    assert config_req.url.params["warehouse"] == "s3://lago"


async def test_explicit_prefix_wins() -> None:
    fake = FakeRest(
        {"/v1/meu/namespaces": {"namespaces": [["a"]]}, "/v1/meu/namespaces?parent=a": {}},
        config={"defaults": {}, "overrides": {"prefix": "outro"}},
    )
    assert await fake.catalog(prefix="meu").list_schemas() == ["a"]


async def test_list_schemas_walks_nested_and_paginates() -> None:
    fake = FakeRest(
        {
            "/v1/namespaces": {"namespaces": [["vendas"]], "next-page-token": "p2"},
            "/v1/namespaces?pageToken=p2": {"namespaces": [["rh"]]},
            "/v1/namespaces?parent=vendas": {"namespaces": [["vendas", "bronze"]]},
            "/v1/namespaces?parent=vendas\x1fbronze": {"namespaces": []},
            "/v1/namespaces?parent=rh": {"namespaces": []},
        }
    )
    assert await fake.catalog().list_schemas() == ["rh", "vendas", "vendas.bronze"]


async def test_list_schemas_with_server_ignoring_parent() -> None:
    # servidor sem suporte a "parent": devolve sempre a mesma lista completa
    fake = FakeRest({"/v1/namespaces": {"namespaces": [["a"], ["a", "b"]]}})
    assert await fake.catalog().list_schemas() == ["a", "a.b"]


async def test_list_tables_sorted() -> None:
    fake = FakeRest(
        {
            "/v1/namespaces/vendas/tables": {
                "identifiers": [
                    {"namespace": ["vendas"], "name": "pedidos"},
                    {"namespace": ["vendas"], "name": "clientes"},
                ]
            }
        }
    )
    tables = await fake.catalog().list_tables("vendas")
    assert [t.name for t in tables] == ["clientes", "pedidos"]
    assert tables[0].schema_ == "vendas"


async def test_missing_table_and_namespace_are_not_found() -> None:
    fake = FakeRest({})
    catalog = fake.catalog()
    with pytest.raises(NotFoundError, match="NoSuchTableException"):
        await catalog.describe(TableRef(schema="vendas", name="x"))
    with pytest.raises(NotFoundError):
        await catalog.list_tables("nao_existe")
    with pytest.raises(NotFoundError, match="namespace"):
        await catalog.describe(TableRef(name="sem_namespace"))


async def test_bearer_token_is_sent() -> None:
    fake = FakeRest({"/v1/namespaces": {"namespaces": []}})
    await fake.catalog(token="tok-1").list_schemas()
    assert all(r.headers["Authorization"] == "Bearer tok-1" for r in fake.requests)


async def test_oauth_client_credentials_and_refresh() -> None:
    issued: list[str] = []
    seen: list[str | None] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/v1/oauth/tokens":
            form = parse_qs(request.content.decode())
            assert form["grant_type"] == ["client_credentials"]
            assert form["client_id"] == ["app"]
            assert form["client_secret"] == ["segredo"]
            assert form["scope"] == ["catalog"]
            issued.append(f"tok-{len(issued) + 1}")
            return httpx2.Response(200, json={"access_token": issued[-1]})
        auth = request.headers.get("Authorization")
        seen.append(auth)
        if auth == "Bearer tok-1" and request.url.path == "/v1/namespaces":
            return httpx2.Response(401, text=error_body("expirado", "NotAuthorized", 401))
        if request.url.path == "/v1/config":
            return httpx2.Response(200, json={"defaults": {}, "overrides": {}})
        return httpx2.Response(200, json={"namespaces": []})

    catalog = IcebergRestCatalog.from_config(
        {"url": "http://rest.test", "credential": "app:segredo"},
        CTX,
        transport_factory=lambda: httpx2.MockTransport(handle),
    )
    assert await catalog.list_schemas() == []
    assert issued == ["tok-1", "tok-2"]  # renovou uma vez após o 401
    assert seen[-1] == "Bearer tok-2"


async def test_unauthorized_without_credential_is_auth_error() -> None:
    def deny(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(401, text=error_body("sem token", "NotAuthorizedException", 401))

    catalog = IcebergRestCatalog.from_config(
        {"url": "http://rest.test"}, CTX, transport_factory=lambda: httpx2.MockTransport(deny)
    )
    with pytest.raises(AuthError, match="sem token"):
        await catalog.list_schemas()


async def test_connection_error_is_transient() -> None:
    def refuse(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("recusada", request=request)

    catalog = IcebergRestCatalog.from_config(
        {"url": "http://rest.test"}, CTX, transport_factory=lambda: httpx2.MockTransport(refuse)
    )
    with pytest.raises(TransientError, match="conexão"):
        await catalog.list_schemas()


async def test_invalid_json_is_adapter_error() -> None:
    fake = FakeRest({"/v1/namespaces": httpx2.Response(200, text="<html>")})
    with pytest.raises(AdapterError, match="inválida"):
        await fake.catalog().list_schemas()


async def test_stats_and_lineage_unsupported() -> None:
    catalog = FakeRest({}).catalog()
    with pytest.raises(UnsupportedOperation):
        await catalog.column_stats(TableRef(schema="a", name="b"))
    with pytest.raises(UnsupportedOperation):
        await catalog.lineage(TableRef(schema="a", name="b"), direction="up")
