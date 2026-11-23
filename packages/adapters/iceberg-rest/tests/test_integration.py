"""Contrato e comportamento do adapter contra o servidor de referência Iceberg REST.

As tabelas são criadas pela própria API REST (``POST .../tables``). Para ter um
snapshot com ``total-records`` sem escrever arquivos de dados, um commit
adiciona um snapshot sintético (o catálogo só guarda o ponteiro de metadados;
ele não lê a *manifest list*).
"""

from __future__ import annotations

import time
from typing import Any

import httpx2
import pytest

from dengcode_adapter_iceberg_rest.catalog import IcebergRestCatalog
from dengcode_core.errors import NotFoundError, TransientError
from dengcode_core.ports.models import AdapterContext, TableRef
from dengcode_testkit.contracts.data import CatalogContract

pytestmark = pytest.mark.integration

CTX = AdapterContext(workspace="teste")


def _post(url: str, path: str, body: dict[str, Any], *, ok: tuple[int, ...] = (200,)) -> Any:
    resp = httpx2.post(f"{url}{path}", json=body, timeout=30)
    assert resp.status_code in ok, resp.text[:300]
    return resp.json() if resp.content else None


def _ensure_namespace(url: str, *parts: str) -> None:
    _post(url, "/v1/namespaces", {"namespace": list(parts)}, ok=(200, 409))


def _create_table(
    url: str, namespace: str, name: str, schema: dict[str, Any], **extra: Any
) -> dict[str, Any]:
    httpx2.delete(f"{url}/v1/namespaces/{namespace}/tables/{name}", timeout=30)
    body = {"name": name, "schema": schema, **extra}
    return _post(url, f"/v1/namespaces/{namespace}/tables", body)["metadata"]


def _add_snapshot(url: str, namespace: str, name: str, location: str, total: int) -> None:
    snapshot = {
        "snapshot-id": 1,
        "sequence-number": 1,
        "timestamp-ms": int(time.time() * 1000),
        "manifest-list": f"{location}/metadata/snap-1-sintetico.avro",
        "summary": {"operation": "append", "total-records": str(total)},
        "schema-id": 0,
    }
    updates = [
        {"action": "add-snapshot", "snapshot": snapshot},
        {"action": "set-snapshot-ref", "ref-name": "main", "type": "branch", "snapshot-id": 1},
    ]
    _post(
        url, f"/v1/namespaces/{namespace}/tables/{name}", {"requirements": [], "updates": updates}
    )


CLIENTES_SCHEMA = {
    "type": "struct",
    "schema-id": 0,
    "fields": [
        {"id": 1, "name": "id", "type": "int", "required": False},
        {"id": 2, "name": "email", "type": "string", "required": False},
    ],
}


@pytest.fixture
def catalog(rest_url: str) -> IcebergRestCatalog:
    return IcebergRestCatalog.from_config({"url": rest_url, "catalog_name": "iceberg"}, CTX)


class TestIcebergRestCatalog(CatalogContract):
    @pytest.fixture
    def seeded_table(self, rest_url: str) -> TableRef:
        _ensure_namespace(rest_url, "vendas")
        metadata = _create_table(rest_url, "vendas", "clientes", CLIENTES_SCHEMA)
        _add_snapshot(rest_url, "vendas", "clientes", metadata["location"], 3)
        return TableRef(catalog="iceberg", schema="vendas", name="clientes")

    @pytest.fixture
    def seeded_schema(self) -> str:
        return "vendas"


async def test_describe_snapshot_and_metadata(catalog: IcebergRestCatalog, rest_url: str) -> None:
    _ensure_namespace(rest_url, "vendas")
    metadata = _create_table(rest_url, "vendas", "clientes", CLIENTES_SCHEMA)
    _add_snapshot(rest_url, "vendas", "clientes", metadata["location"], 3)
    info = await catalog.describe(TableRef(schema="vendas", name="clientes"))
    assert info.row_count == 3
    assert info.table_format == "iceberg"
    assert info.location == metadata["location"]
    assert info.ref == TableRef(catalog="iceberg", schema="vendas", name="clientes")


async def test_nested_namespace_and_types(catalog: IcebergRestCatalog, rest_url: str) -> None:
    _ensure_namespace(rest_url, "lago")
    _ensure_namespace(rest_url, "lago", "bronze")
    schema = {
        "type": "struct",
        "schema-id": 0,
        "fields": [
            {"id": 1, "name": "id", "type": "long", "required": True, "doc": "chave"},
            {"id": 2, "name": "valor", "type": "decimal(10, 2)", "required": False},
            {"id": 3, "name": "criado", "type": "timestamptz", "required": False},
            {
                "id": 4,
                "name": "tags",
                "type": {
                    "type": "list",
                    "element-id": 7,
                    "element": "string",
                    "element-required": False,
                },
                "required": False,
            },
            {
                "id": 5,
                "name": "endereco",
                "type": {
                    "type": "struct",
                    "fields": [{"id": 6, "name": "cidade", "type": "string", "required": False}],
                },
                "required": False,
            },
        ],
    }
    _create_table(
        rest_url,
        "lago%1Fbronze",
        "eventos",
        schema,
        properties={"comment": "eventos brutos"},
    )
    assert {"lago", "lago.bronze"} <= set(await catalog.list_schemas())
    tables = await catalog.list_tables("lago.bronze")
    assert [t.name for t in tables] == ["eventos"]
    assert tables[0].schema_ == "lago.bronze"
    info = await catalog.describe(TableRef(schema="lago.bronze", name="eventos"))
    assert [(c.name, c.data_type, c.nullable) for c in info.columns] == [
        ("id", "long", False),
        ("valor", "decimal(10, 2)", True),
        ("criado", "timestamptz", True),
        ("tags", "list<string>", True),
        ("endereco", "struct<cidade: string>", True),
    ]
    assert info.columns[0].description == "chave"
    assert info.description == "eventos brutos"
    assert info.row_count == 0  # sem snapshot: tabela vazia


async def test_missing_namespace_is_not_found(catalog: IcebergRestCatalog) -> None:
    with pytest.raises(NotFoundError, match="NoSuchNamespaceException"):
        await catalog.list_tables("namespace_que_nao_existe")


async def test_unreachable_catalog_is_transient() -> None:
    bad = IcebergRestCatalog.from_config({"url": "http://127.0.0.1:1", "timeout_s": 5}, CTX)
    with pytest.raises(TransientError):
        await bad.list_schemas()
