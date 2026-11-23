"""Contrato de MemoryStorePort (docs/especificacoes/memoria.md).

O adapter herda ``MemoryStoreContract`` numa classe ``Test...`` e fornece a
fixture ``store``: um ``MemoryStorePort`` vazio e gravável. O mesmo store é
usado com escopos de workspaces diferentes — o teste de isolamento confere
que um workspace nunca enxerga registros do outro.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from dengcode_core.errors import PermissionDeniedError, ScopeError
from dengcode_core.ports.memory import MemoryKind, MemoryRecord, MemoryStorePort, Scope
from dengcode_core.ports.models import LineageEdge

WS_A = Scope(workspace="trabalho-1", project="repo-vendas", env="dev")
WS_B = Scope(workspace="trabalho-2", project="repo-vendas", env="dev")

_BASE = datetime(2026, 1, 10, 12, 0, tzinfo=UTC)


def make_record(
    scope: Scope,
    id: str,
    *,
    title: str,
    body: str = "",
    kind: MemoryKind = MemoryKind.KNOWN_ISSUE,
    entities: list[str] | None = None,
    minutes: int = 0,
) -> MemoryRecord:
    """Registro de teste com ``updated_at`` controlado (``minutes`` após uma base fixa)."""
    when = _BASE + timedelta(minutes=minutes)
    return MemoryRecord(
        id=id,
        scope=scope,
        kind=kind,
        title=title,
        body=body,
        entities=entities or [],
        source="agent",
        confidence="proposed",
        created_at=when,
        updated_at=when,
    )


async def seed(store: MemoryStorePort, scope: Scope = WS_A) -> None:
    """Quatro registros variados num workspace."""
    await store.upsert(
        make_record(
            scope,
            "issue-nulos",
            title="customer_id com nulos em silver.orders",
            body="Desde março de 2024 a coluna customer_id chega nula em ~3% dos pedidos.",
            entities=["silver.orders", "silver.orders.customer_id"],
            minutes=1,
        )
    )
    await store.upsert(
        make_record(
            scope,
            "glossario-receita",
            title="Receita líquida",
            body="Receita bruta menos impostos, devoluções e descontos comerciais.",
            kind=MemoryKind.GLOSSARY,
            entities=["gold.receita_diaria"],
            minutes=2,
        )
    )
    await store.upsert(
        make_record(
            scope,
            "incidente-carga",
            title="Falha na DAG carga_pedidos",
            body="A carga falhou por timeout no extrator; aumentamos o timeout para 30 minutos.",
            kind=MemoryKind.INCIDENT,
            entities=["raw.pedidos"],
            minutes=3,
        )
    )
    await store.upsert(
        make_record(
            scope,
            "decisao-particao",
            title="Particionar silver.orders por data",
            body="Decidimos particionar por order_date para reduzir o custo das consultas.",
            kind=MemoryKind.DECISION,
            entities=["silver.orders"],
            minutes=4,
        )
    )


def ids(items: list[MemoryRecord]) -> list[str]:
    return [r.id for r in items]


class MemoryStoreContract:
    """Fixture: ``store`` (MemoryStorePort vazio e gravável)."""

    # -------------------------------------------------------------- CRUD
    async def test_upsert_get_round_trip(self, store: MemoryStorePort) -> None:
        rec = make_record(
            WS_A,
            "r1",
            title="Tabela silver.orders",
            body="Colunas: order_id, customer_id, order_date.",
            kind=MemoryKind.CATALOG_TABLE,
            entities=["silver.orders"],
        ).model_copy(update={"schema_hash": "abc123", "confidence": "verified"})
        await store.upsert(rec)
        assert await store.get(WS_A, "r1") == rec

    async def test_upsert_replaces_existing(self, store: MemoryStorePort) -> None:
        await store.upsert(make_record(WS_A, "r1", title="Versão antiga", minutes=1))
        await store.upsert(make_record(WS_A, "r1", title="Versão nova", minutes=2))
        got = await store.get(WS_A, "r1")
        assert got is not None
        assert got.title == "Versão nova"
        assert ids(await store.list(WS_A)) == ["r1"]

    async def test_get_missing_returns_none(self, store: MemoryStorePort) -> None:
        assert await store.get(WS_A, "nao-existe") is None

    async def test_list_newest_first_with_kinds_and_limit(self, store: MemoryStorePort) -> None:
        await seed(store)
        assert ids(await store.list(WS_A)) == [
            "decisao-particao",
            "incidente-carga",
            "glossario-receita",
            "issue-nulos",
        ]
        only = await store.list(WS_A, kinds=[MemoryKind.GLOSSARY, MemoryKind.KNOWN_ISSUE])
        assert ids(only) == ["glossario-receita", "issue-nulos"]
        assert ids(await store.list(WS_A, limit=2)) == ["decisao-particao", "incidente-carga"]

    async def test_delete(self, store: MemoryStorePort) -> None:
        await seed(store)
        await store.delete(WS_A, "incidente-carga")
        assert await store.get(WS_A, "incidente-carga") is None
        assert "incidente-carga" not in ids(await store.list(WS_A))
        hits = await store.search(WS_A, "timeout extrator")
        assert "incidente-carga" not in [h.record.id for h in hits]
        await store.delete(WS_A, "incidente-carga")  # idempotente

    # -------------------------------------------------------------- busca
    async def test_search_finds_portuguese_words_in_title_and_body(
        self, store: MemoryStorePort
    ) -> None:
        await seed(store)
        by_title = await store.search(WS_A, "receita líquida")
        assert by_title
        assert by_title[0].record.id == "glossario-receita"
        assert "fts" in by_title[0].matched_by
        by_body = await store.search(WS_A, "devoluções e impostos")
        assert by_body[0].record.id == "glossario-receita"
        accentless = await store.search(WS_A, "devolucoes")
        assert accentless[0].record.id == "glossario-receita"
        assert by_title[0].score > 0

    async def test_search_respects_kinds(self, store: MemoryStorePort) -> None:
        await seed(store)
        hits = await store.search(WS_A, "silver.orders", kinds=[MemoryKind.DECISION])
        assert [h.record.id for h in hits] == ["decisao-particao"]

    async def test_search_respects_limit(self, store: MemoryStorePort) -> None:
        await seed(store)
        assert len(await store.search(WS_A, "silver orders pedidos receita", limit=2)) <= 2

    async def test_search_never_raises_on_odd_queries(self, store: MemoryStorePort) -> None:
        await seed(store)
        for query in ['"', "AND OR NOT", "title:x*", "(", "NEAR(a b", "*", "", "   ", "a'b\"c"]:
            await store.search(WS_A, query)

    async def test_search_without_match_is_empty(self, store: MemoryStorePort) -> None:
        await seed(store)
        assert await store.search(WS_A, "xilofone") == []

    # ---------------------------------------------------------- isolamento
    async def test_workspace_isolation(self, store: MemoryStorePort) -> None:
        await seed(store, WS_A)
        await store.upsert(
            make_record(
                WS_B,
                "so-no-b",
                title="Receita líquida no workspace B",
                body="Definição diferente: receita bruta menos impostos.",
                kind=MemoryKind.GLOSSARY,
                entities=["silver.orders"],
            )
        )
        await store.add_edges(WS_B, [LineageEdge(source="raw.pedidos", target="silver.orders")])
        hits_a = await store.search(WS_A, "receita líquida impostos")
        assert hits_a
        assert all(h.record.scope.workspace == WS_A.workspace for h in hits_a)
        hits_b = await store.search(WS_B, "receita líquida impostos")
        assert [h.record.id for h in hits_b] == ["so-no-b"]
        assert await store.get(WS_A, "so-no-b") is None
        assert await store.get(WS_B, "issue-nulos") is None
        assert ids(await store.list(WS_B)) == ["so-no-b"]
        assert "so-no-b" not in ids(await store.list(WS_A))
        by_entity = await store.search(WS_B, "", entities=["silver.orders"])
        assert [h.record.id for h in by_entity] == ["so-no-b"]
        assert await store.neighbors(WS_A, "silver.orders", direction="up") == []

    async def test_same_id_in_two_workspaces_is_independent(self, store: MemoryStorePort) -> None:
        await store.upsert(make_record(WS_A, "mesmo-id", title="Versão do A"))
        await store.upsert(make_record(WS_B, "mesmo-id", title="Versão do B"))
        await store.delete(WS_B, "mesmo-id")
        got = await store.get(WS_A, "mesmo-id")
        assert got is not None
        assert got.title == "Versão do A"

    async def test_empty_workspace_scope_is_rejected(self, store: MemoryStorePort) -> None:
        with pytest.raises(ScopeError):
            Scope(workspace="  ")
        # mesmo um Scope montado sem validação é recusado pelo store
        bad = Scope.model_construct(workspace="", project=None, env=None)
        with pytest.raises(ScopeError):
            await store.search(bad, "pedidos")
        with pytest.raises(ScopeError):
            await store.list(bad)
        with pytest.raises(ScopeError):
            await store.upsert(make_record(WS_A, "x", title="x").model_copy(update={"scope": bad}))

    # ---------------------------------------------------------- grafo
    async def test_neighbors_and_idempotent_edges(self, store: MemoryStorePort) -> None:
        edges = [
            LineageEdge(source="raw.pedidos", target="silver.orders", via="dbt"),
            LineageEdge(source="silver.orders", target="gold.receita_diaria", via="dbt"),
            LineageEdge(source="gold.receita_diaria", target="painel.vendas"),
        ]
        await store.add_edges(WS_A, edges)
        await store.add_edges(WS_A, edges)  # idempotente
        down = await store.neighbors(WS_A, "raw.pedidos", direction="down", depth=2)
        assert down == edges[:2]
        down_all = await store.neighbors(WS_A, "raw.pedidos", direction="down", depth=5)
        assert down_all == edges
        up = await store.neighbors(WS_A, "painel.vendas", direction="up", depth=1)
        assert up == [edges[2]]
        assert await store.neighbors(WS_A, "raw.pedidos", direction="up") == []

    async def test_entity_search_expands_through_edges(self, store: MemoryStorePort) -> None:
        await seed(store)
        await store.add_edges(
            WS_A,
            [
                LineageEdge(source="raw.pedidos", target="silver.orders"),
                LineageEdge(source="silver.orders", target="gold.receita_diaria"),
            ],
        )
        hits = await store.search(WS_A, "", entities=["gold.receita_diaria"])
        found = [h.record.id for h in hits]
        # o próprio (distância 0) vem primeiro; vizinhos até 2 saltos também entram
        assert found[0] == "glossario-receita"
        assert {"issue-nulos", "decisao-particao", "incidente-carga"} <= set(found)
        assert all("grafo" in h.matched_by for h in hits)
        # entidade sem relação nenhuma: nada
        assert await store.search(WS_A, "", entities=["outro.schema.tabela"]) == []

    async def test_entity_filter_restricts_text_search(self, store: MemoryStorePort) -> None:
        await seed(store)
        # "receita" casa o glossário no texto, mas ele não toca a entidade pedida
        hits = await store.search(
            WS_A, "receita silver orders", entities=["silver.orders.customer_id"]
        )
        # coluna casa a própria tabela (e vice-versa): os dois registros de silver.orders
        assert {h.record.id for h in hits} == {"issue-nulos", "decisao-particao"}

    # --------------------------------------------------------- validador
    @pytest.mark.parametrize(
        "body",
        [
            "Cliente reclamou: o e-mail dele é maria.souza@empresa.com.br",
            "CPF de exemplo que veio do log: 529.982.247-25",
        ],
    )
    async def test_pii_is_rejected(self, store: MemoryStorePort, body: str) -> None:
        with pytest.raises(PermissionDeniedError, match="metadados"):
            await store.upsert(make_record(WS_A, "pii", title="Problema", body=body))
        assert await store.get(WS_A, "pii") is None

    async def test_pii_in_title_is_rejected(self, store: MemoryStorePort) -> None:
        with pytest.raises(PermissionDeniedError):
            await store.upsert(make_record(WS_A, "pii", title="Erro de joao@x.com"))

    @pytest.mark.parametrize(
        "body",
        [
            "Amostra:\n1,Ana,SP,120.50\n2,Bruno,RJ,99.90\n3,Carla,MG,10.00\n",
            "id;nome;uf;valor\n1;Ana;SP;10\n2;Bruno;RJ;20\n",
            "1\tAna\tSP\t10\n2\tBruno\tRJ\t20\n3\tCarla\tMG\t30\n",
            "| id | nome |\n|---|---|\n| 1 | Ana |\n| 2 | Bruno |\n| 3 | Carla |\n",
        ],
    )
    async def test_data_rows_are_rejected(self, store: MemoryStorePort, body: str) -> None:
        with pytest.raises(PermissionDeniedError, match="metadados"):
            await store.upsert(make_record(WS_A, "linhas", title="Amostra", body=body))

    async def test_metadata_body_is_accepted(self, store: MemoryStorePort) -> None:
        body = (
            "customer_id tem ~3% de nulos desde 2024-03.\n"
            "Causa: o extrator ignora clientes sem cadastro, ver ticket DADOS-42.\n"
            "Contorno: filtrar nulos na silver e reprocessar a partição."
        )
        await store.upsert(make_record(WS_A, "ok", title="Nulos em customer_id", body=body))
        assert await store.get(WS_A, "ok") is not None
