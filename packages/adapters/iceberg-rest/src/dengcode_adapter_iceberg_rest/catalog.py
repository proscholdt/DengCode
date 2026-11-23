"""CatalogPort sobre um catálogo Iceberg REST (spec aberta, só leitura).

- ``list_schemas`` = namespaces (aninhados com ponto: ``vendas.bronze``);
- ``list_tables(schema)`` = tabelas do namespace;
- ``describe`` = schema atual dos metadados (``required`` → ``nullable``,
  ``doc`` → descrição), localização, comentário (``properties.comment``) e
  ``row_count`` pelo ``total-records`` do snapshot atual.

Um adapter = um catálogo REST: ``TableRef.catalog`` é ignorado na busca e, nas
respostas, vira o ``catalog_name`` da config (ex.: o nome do catálogo no Trino,
para o agente usar o mesmo nome na consulta).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar, Literal

from dengcode_adapter_iceberg_rest.metadata import columns_from_schema, current_schema, row_count
from dengcode_adapter_iceberg_rest.rest import (
    DEFAULT_OAUTH_SCOPE,
    IcebergRestClient,
    RestSettings,
    TransportFactory,
)
from dengcode_core.errors import AdapterError, ConfigError, NotFoundError, UnsupportedOperation
from dengcode_core.ports.models import (
    AdapterContext,
    AdapterInfo,
    CatalogCapabilities,
    ColumnStats,
    LineageEdge,
    PortKind,
    TableInfo,
    TableRef,
    schema_hash,
)

DEFAULT_TIMEOUT_S = 30


def _text(cfg: Mapping[str, Any], key: str) -> str | None:
    value = cfg.get(key)
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ConfigError(f"iceberg_rest: '{key}' deve ser texto")
    return value


def build_settings(cfg: Mapping[str, Any]) -> RestSettings:
    """Valida o bloco de config. Mensagens de erro nunca mostram token/credencial."""
    url = _text(cfg, "url")
    if not url:
        raise ConfigError("iceberg_rest: 'url' é obrigatória (ex.: http://localhost:8181)")
    if not url.startswith(("http://", "https://")):
        raise ConfigError("iceberg_rest: 'url' deve começar com http:// ou https://")
    url = url.rstrip("/").removesuffix("/v1")
    for secret in ("token", "credential"):
        if cfg.get(secret) not in (None, "") and not isinstance(cfg.get(secret), str):
            raise ConfigError(f"iceberg_rest: '{secret}' deve ser texto (valor omitido)")
    raw_timeout = cfg.get("timeout_s", DEFAULT_TIMEOUT_S)
    try:
        timeout = float(raw_timeout)
    except (TypeError, ValueError):
        raise ConfigError("iceberg_rest: 'timeout_s' deve ser um número") from None
    if timeout <= 0:
        raise ConfigError("iceberg_rest: 'timeout_s' deve ser maior que zero")
    return RestSettings(
        url=url,
        warehouse=_text(cfg, "warehouse"),
        prefix=_text(cfg, "prefix"),
        token=_text(cfg, "token"),
        credential=_text(cfg, "credential"),
        oauth2_server_uri=_text(cfg, "oauth2_server_uri"),
        scope=_text(cfg, "scope") or DEFAULT_OAUTH_SCOPE,
        timeout_s=timeout,
    )


def split_namespace(schema: str) -> list[str]:
    parts = [p for p in schema.split(".") if p]
    if not parts:
        raise NotFoundError(f"iceberg-rest: namespace inválido: {schema!r}")
    return parts


class IcebergRestCatalog:
    info: ClassVar[AdapterInfo] = AdapterInfo(
        name="iceberg_rest",
        port=PortKind.CATALOG,
        version="0.1.0",
        knowledge_pack="dengcode_adapter_iceberg_rest:knowledge/iceberg.md",
    )
    name = "iceberg_rest"

    def __init__(self, client: IcebergRestClient, *, catalog_name: str | None = None) -> None:
        self._client = client
        self._catalog_name = catalog_name

    def __repr__(self) -> str:  # nunca expõe token/credencial
        return f"{type(self).__name__}(url={self._client.settings.url!r})"

    @classmethod
    def from_config(
        cls,
        cfg: dict[str, Any],
        ctx: AdapterContext,
        *,
        transport_factory: TransportFactory | None = None,
    ) -> IcebergRestCatalog:
        client = IcebergRestClient(build_settings(cfg), transport_factory=transport_factory)
        return cls(client, catalog_name=_text(cfg, "catalog_name"))

    def capabilities(self) -> CatalogCapabilities:
        return CatalogCapabilities(
            supports_lineage=False, supports_column_stats=False, supports_tags=False
        )

    async def list_schemas(self, catalog: str | None = None) -> list[str]:
        namespaces = await self._client.list_namespaces()
        return sorted(".".join(ns) for ns in namespaces)

    async def list_tables(self, schema: str, *, catalog: str | None = None) -> list[TableRef]:
        identifiers = await self._client.list_tables(split_namespace(schema))
        refs = [
            TableRef(
                catalog=self._catalog_name,
                schema=".".join(str(p) for p in ident.get("namespace") or []) or schema,
                name=str(ident["name"]),
            )
            for ident in identifiers
            if ident.get("name")
        ]
        return sorted(refs, key=lambda r: r.name)

    async def describe(self, ref: TableRef) -> TableInfo:
        if not ref.schema_:
            raise NotFoundError(
                f"iceberg-rest: informe o namespace da tabela ({ref.qualified()}); "
                "ex.: vendas.pedidos"
            )
        namespace = split_namespace(ref.schema_)
        loaded = await self._client.load_table(namespace, ref.name)
        metadata = loaded.get("metadata")
        if not isinstance(metadata, Mapping):
            raise AdapterError("iceberg-rest: resposta sem 'metadata' ao carregar a tabela")
        columns = columns_from_schema(current_schema(metadata))
        properties: Mapping[str, Any] = metadata.get("properties") or {}
        comment = properties.get("comment")
        return TableInfo(
            ref=TableRef(catalog=self._catalog_name, schema=".".join(namespace), name=ref.name),
            columns=columns,
            table_format="iceberg",
            location=metadata.get("location"),
            row_count=row_count(metadata),
            description=str(comment) if comment else None,
            schema_hash=schema_hash(columns),
        )

    async def column_stats(
        self, ref: TableRef, columns: list[str] | None = None
    ) -> list[ColumnStats]:
        raise UnsupportedOperation(
            "o catálogo Iceberg REST não calcula estatísticas de coluna; "
            "calcule com o motor de consulta que lê estas tabelas"
        )

    async def lineage(
        self, ref: TableRef, *, direction: Literal["up", "down"], depth: int = 2
    ) -> list[LineageEdge]:
        raise UnsupportedOperation("o catálogo Iceberg REST não guarda lineage")
