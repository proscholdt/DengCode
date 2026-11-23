# Especificação — Memória

## Escopo obrigatório

```python
class Scope(BaseModel):
    workspace: str  # obrigatório; ex.: "trabalho-1"
    project: str | None  # caminho/ID do repo
    env: str | None  # dev | prod
```

Toda leitura/escrita recebe `Scope`. Sem workspace → erro. Não existe consulta
"global" entre workspaces.

## Tipos de registro

```python
class MemoryKind(StrEnum):
    CATALOG_TABLE = "catalog_table"  # snapshot de TableInfo (metadados)
    COLUMN_STATS = "column_stats"
    GLOSSARY = "glossary"  # termo de negócio → definição
    KNOWN_ISSUE = "known_issue"  # "customer_id tem nulos desde 2024"
    INCIDENT = "incident"  # falha, causa, solução
    DECISION = "decision"


class MemoryRecord(BaseModel):
    id: str
    scope: Scope
    kind: MemoryKind
    title: str
    body: str  # markdown curto; NUNCA valores de linha ou PII
    entities: list[str]  # ex.: ["silver.orders", "silver.orders.customer_id"]
    source: str  # "catalog:unity", "agent", "user"
    confidence: Literal["verified", "proposed", "stale"]
    schema_hash: str | None
    created_at: datetime
    updated_at: datetime
```

## MemoryStorePort

```python
class MemoryStorePort(Protocol):
    async def upsert(self, rec: MemoryRecord) -> None: ...
    async def get(self, scope: Scope, id: str) -> MemoryRecord | None: ...
    async def search(
        self,
        scope: Scope,
        query: str,
        *,
        kinds: list[MemoryKind] | None = None,
        entities: list[str] | None = None,
        limit: int = 10,
    ) -> list[MemoryHit]: ...
    async def delete(self, scope: Scope, id: str) -> None: ...
    async def add_edges(self, scope: Scope, edges: list[LineageEdge]) -> None: ...
    async def neighbors(
        self, scope: Scope, entity: str, *, direction: Literal["up", "down"], depth: int = 2
    ) -> list[LineageEdge]: ...
```

## Implementação padrão (SQLite)

- Arquivo: `~/.dengcode/workspaces/<workspace>/memory.db`.
- Tabelas: `records`, `records_fts` (FTS5 sobre title+body+entities),
  `records_vec` (sqlite-vec), `edges` (grafo de lineage).
- Busca híbrida: score = combinação (Reciprocal Rank Fusion) de FTS5 +
  vetorial; filtro por `entities` usa o grafo (vizinhança até `depth`).
- Se `sqlite-vec` não carregar na plataforma, cai para só FTS5 com aviso.
- Adapter alternativo (fase 7+): Postgres + pgvector para times.

## Arquivos humanos

| Arquivo | Conteúdo | Quem edita |
| --- | --- | --- |
| `<repo>/.dengcode/PROJETO.md` | convenções, camadas, proibições, tabelas protegidas | humano; agente só propõe diff |
| `<repo>/.dengcode/incidents/*.md` | incidentes versionados (opcional) | agente propõe, humano aprova |
| `~/.dengcode/preferences.md` | preferências pessoais | usuário |

## Regras

1. **Pista, não verdade**: antes de agir com base num `CATALOG_TABLE`, compare
   `schema_hash` com o catálogo ao vivo; divergiu → marque `stale` e use o ao vivo.
2. **Só metadados**: um validador rejeita `body` com padrões de PII ou que
   pareça linha de dados (heurística: muitos valores separados por vírgula/tab).
3. **Sob demanda**: o prompt recebe só um resumo curto do workspace
   (nº de tabelas conhecidas, últimos incidentes por título). O resto via
   ferramentas `memory_search`, `memory_propose`.
4. **Escrita**: catálogo/estatísticas = automático (`verified`); o resto o
   agente cria como `proposed` e o usuário confirma (vira `verified`).
5. **Compactação da sessão**: ao atingir ~75% do contexto, o modelo `fast`
   resume as mensagens antigas preservando: decisões, arquivos alterados,
   pendências, SQLs executados (sem resultados).
