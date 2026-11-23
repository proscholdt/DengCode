from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlite_vec

from dengcode_adapter_memory_sqlite.query import fts_match_expression, terms
from dengcode_adapter_memory_sqlite.store import SqliteMemoryStore
from dengcode_adapter_memory_sqlite.validator import looks_like_data_rows
from dengcode_core.errors import ConfigError, TransientError
from dengcode_core.ports.memory import MemoryKind, MemoryStorePort, Scope
from dengcode_core.ports.models import AdapterContext
from dengcode_testkit.contracts.memory import WS_A, WS_B, MemoryStoreContract, make_record, seed
from dengcode_testkit.fake_embedding import FakeEmbedding


def _broken_load(_conn: sqlite3.Connection) -> None:
    raise sqlite3.OperationalError("not authorized")


# ------------------------------------------------------------------ contratos
class TestSqliteFtsOnly(MemoryStoreContract):
    """Sem embedder: só FTS5 + grafo."""

    @pytest.fixture
    def store(self, tmp_path: Path) -> Iterator[SqliteMemoryStore]:
        s = SqliteMemoryStore(tmp_path / "memory.db")
        yield s
        s.close()


class TestSqliteHybrid(MemoryStoreContract):
    """FTS5 + sqlite-vec com FakeEmbedding."""

    @pytest.fixture
    def store(self, tmp_path: Path) -> Iterator[SqliteMemoryStore]:
        s = SqliteMemoryStore(tmp_path / "memory.db", embedder=FakeEmbedding())
        yield s
        s.close()


class TestSqliteInMemoryHybrid(MemoryStoreContract):
    @pytest.fixture
    def store(self) -> Iterator[SqliteMemoryStore]:
        s = SqliteMemoryStore(":memory:", embedder=FakeEmbedding())
        yield s
        s.close()


class TestSqliteVectorExtensionMissing(MemoryStoreContract):
    """Extensão indisponível (ex.: Python de macOS sem load_extension): cai para FTS5."""

    @pytest.fixture
    def store(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[SqliteMemoryStore]:
        monkeypatch.setattr(sqlite_vec, "load", _broken_load)
        s = SqliteMemoryStore(tmp_path / "memory.db", embedder=FakeEmbedding())
        yield s
        s.close()


# ------------------------------------------------------------------ vetores
@pytest.fixture
def hybrid(tmp_path: Path) -> Iterator[SqliteMemoryStore]:
    s = SqliteMemoryStore(tmp_path / "memory.db", embedder=FakeEmbedding())
    yield s
    s.close()


async def test_vector_only_match_is_reported(hybrid: SqliteMemoryStore) -> None:
    await hybrid.upsert(make_record(WS_A, "r1", title="Atraso no pedido", body="Carga lenta."))
    # "pedidos" não casa "pedido" no FTS (prefixo), mas o vetor aproxima os dois
    hits = await hybrid.search(WS_A, "pedidos")
    assert hybrid.vector_search_enabled
    assert [h.record.id for h in hits] == ["r1"]
    assert hits[0].matched_by == ["vetor"]


async def test_hybrid_hit_reports_both_sources(hybrid: SqliteMemoryStore) -> None:
    await seed(hybrid)
    hits = await hybrid.search(WS_A, "receita líquida")
    assert hits[0].record.id == "glossario-receita"
    assert hits[0].matched_by == ["fts", "vetor"]
    # RRF: primeiro lugar nas duas listas → 2 / (60 + 1)
    assert hits[0].score == pytest.approx(2 / 61)


async def test_embeds_title_and_body_and_query(tmp_path: Path) -> None:
    fake = FakeEmbedding()
    store = SqliteMemoryStore(tmp_path / "m.db", embedder=fake)
    await store.upsert(make_record(WS_A, "r1", title="Título", body="Corpo"))
    await store.search(WS_A, "consulta")
    assert fake.calls == [["Título\nCorpo"], ["consulta"]]
    store.close()


async def test_vector_search_is_workspace_isolated(hybrid: SqliteMemoryStore) -> None:
    await hybrid.upsert(make_record(WS_B, "b1", title="Atraso no pedido"))
    assert await hybrid.search(WS_A, "pedidos") == []
    assert [h.record.id for h in await hybrid.search(WS_B, "pedidos")] == ["b1"]


async def test_vector_kinds_filter(hybrid: SqliteMemoryStore) -> None:
    await hybrid.upsert(make_record(WS_A, "r1", title="Atraso no pedido"))
    await hybrid.upsert(make_record(WS_A, "r2", title="Atraso no pedido", kind=MemoryKind.INCIDENT))
    hits = await hybrid.search(WS_A, "pedidos", kinds=[MemoryKind.INCIDENT])
    assert [h.record.id for h in hits] == ["r2"]


async def test_project_scope_sees_workspace_wide_records(hybrid: SqliteMemoryStore) -> None:
    geral = Scope(workspace="trabalho-1")
    outro = Scope(workspace="trabalho-1", project="repo-financeiro")
    await hybrid.upsert(make_record(geral, "geral", title="Convenção de nomes de pedidos"))
    await hybrid.upsert(make_record(WS_A, "vendas", title="Pedidos duplicados", minutes=1))
    assert {r.id for r in await hybrid.list(geral)} == {"geral", "vendas"}
    assert {r.id for r in await hybrid.list(WS_A)} == {"geral", "vendas"}
    assert [r.id for r in await hybrid.list(outro)] == ["geral"]
    assert [h.record.id for h in await hybrid.search(outro, "pedidos")] == ["geral"]
    assert await hybrid.get(outro, "vendas") is None


async def test_embedder_failure_keeps_record_and_falls_back(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    fake = FakeEmbedding(fail_with=TransientError("ollama fora do ar"))
    store = SqliteMemoryStore(tmp_path / "m.db", embedder=fake)
    with caplog.at_level(logging.WARNING):
        await store.upsert(make_record(WS_A, "r1", title="Atraso no pedido"))
        hits = await store.search(WS_A, "pedido")
    assert [h.record.id for h in hits] == ["r1"]
    assert hits[0].matched_by == ["fts"]
    assert "embedding falhou" in caplog.text
    store.close()
    # quando o embedder volta, o registro sem vetor é reindexado ao reabrir
    fake.fail_with = None
    again = SqliteMemoryStore(tmp_path / "m.db", embedder=fake)
    hits = await again.search(WS_A, "pedidos")
    assert [h.matched_by for h in hits] == [["vetor"]]
    again.close()


async def test_dimension_change_reindexes(tmp_path: Path) -> None:
    first = SqliteMemoryStore(tmp_path / "m.db", embedder=FakeEmbedding(dimensions=32))
    await first.upsert(make_record(WS_A, "r1", title="Atraso no pedido"))
    first.close()
    second = SqliteMemoryStore(tmp_path / "m.db", embedder=FakeEmbedding(dimensions=128))
    hits = await second.search(WS_A, "pedidos")
    assert [h.matched_by for h in hits] == [["vetor"]]
    second.close()


# ------------------------------------------------------------------ fallback
async def test_extension_failure_warns_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(sqlite_vec, "load", _broken_load)
    store = SqliteMemoryStore(tmp_path / "m.db", embedder=FakeEmbedding())
    with caplog.at_level(logging.WARNING):
        await seed(store)
        hits = await store.search(WS_A, "receita")
        store.close()
        await store.search(WS_A, "receita")  # reabre a conexão: sem novo aviso
    assert not store.vector_search_enabled
    assert hits[0].matched_by == ["fts"]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "sqlite-vec" in warnings[0].getMessage()
    store.close()


async def test_vectors_disabled_by_config(tmp_path: Path) -> None:
    fake = FakeEmbedding()
    store = SqliteMemoryStore(tmp_path / "m.db", embedder=fake, vectors=False)
    await seed(store)
    await store.search(WS_A, "receita")
    assert fake.calls == []
    assert not store.vector_search_enabled
    store.close()


# ------------------------------------------------------------------ arquivo
async def test_two_instances_on_same_file_stay_isolated(tmp_path: Path) -> None:
    path = tmp_path / "compartilhado.db"
    a = SqliteMemoryStore(path, embedder=FakeEmbedding())
    b = SqliteMemoryStore(path, embedder=FakeEmbedding())
    await seed(a, WS_A)
    await b.upsert(make_record(WS_B, "b1", title="Receita líquida do B"))
    assert [h.record.id for h in await b.search(WS_B, "receita")] == ["b1"]
    assert all(h.record.scope.workspace == WS_A.workspace for h in await a.search(WS_A, "receita"))
    assert await b.get(WS_B, "glossario-receita") is None
    a.close()
    b.close()


async def test_persists_across_instances_with_wal(tmp_path: Path) -> None:
    path = tmp_path / "m.db"
    first = SqliteMemoryStore(path)
    await seed(first)
    first.close()
    second = SqliteMemoryStore(path)
    assert len(await second.list(WS_A)) == 4
    second.close()
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert {"records", "records_fts", "edges"} <= tables


async def test_vec_table_is_created_with_embedder_dimension(tmp_path: Path) -> None:
    path = tmp_path / "m.db"
    store = SqliteMemoryStore(path, embedder=FakeEmbedding(dimensions=48))
    await store.upsert(make_record(WS_A, "r1", title="Atraso no pedido"))
    store.close()
    conn = sqlite3.connect(path)
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'records_vec'").fetchone()[0]
    conn.close()
    assert "float[48]" in sql


# ------------------------------------------------------------------ config
def test_from_config_default_path_is_workspace_dir(tmp_path: Path) -> None:
    cache = tmp_path / "workspaces" / "trabalho-1" / "cache"
    fake = FakeEmbedding()
    ctx = AdapterContext(workspace="trabalho-1", cache_dir=cache, peers={"embedding": fake})
    store = SqliteMemoryStore.from_config({}, ctx)
    assert Path(store.path) == tmp_path / "workspaces" / "trabalho-1" / "memory.db"
    assert store._embedder is fake


def test_from_config_memory_and_relative_path(tmp_path: Path) -> None:
    ctx = AdapterContext(workspace="t", project_dir=tmp_path)
    assert SqliteMemoryStore.from_config({"path": ":memory:"}, ctx).path == ":memory:"
    rel = SqliteMemoryStore.from_config({"path": "dados/mem.db"}, ctx)
    assert Path(rel.path) == (tmp_path / "dados" / "mem.db").resolve()


def test_from_config_rejects_invalid_embedding_peer() -> None:
    ctx = AdapterContext(workspace="t", peers={"embedding": object()})
    with pytest.raises(ConfigError):
        SqliteMemoryStore.from_config({}, ctx)


async def test_from_config_creates_file_on_first_use(tmp_path: Path) -> None:
    cache = tmp_path / "ws" / "cache"
    store = SqliteMemoryStore.from_config({}, AdapterContext(workspace="ws", cache_dir=cache))
    assert not (tmp_path / "ws" / "memory.db").exists()
    await store.list(Scope(workspace="ws"))
    assert (tmp_path / "ws" / "memory.db").exists()
    store.close()


def test_satisfies_port() -> None:
    store: MemoryStorePort = SqliteMemoryStore()
    assert isinstance(store, MemoryStorePort)
    assert SqliteMemoryStore.info.name == "sqlite"


# ------------------------------------------------------------------ unidades
@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("receita líquida", '"receita"* OR "líquida"*'),
        ('a"b OR NOT c:*', '"not"*'),
        ("silver.orders customer_id", '"silver"* OR "orders"* OR "customer"* OR "id"'),
        ("de para o que", None),
        ("", None),
    ],
)
def test_fts_expression(query: str, expected: str | None) -> None:
    assert fts_match_expression(query) == expected


def test_terms_dedupe() -> None:
    assert terms("Pedido pedido PEDIDO") == ["pedido"]


@pytest.mark.parametrize(
    "text",
    [
        "Colunas: order_id (bigint), customer_id (bigint, 3% nulos), order_date (date).",
        "Decidimos usar a, b, c e d.\nRevisar em março.",
        "| coluna | tipo |\n|---|---|\n| id | int |\n",
    ],
)
def test_metadata_text_is_not_data(text: str) -> None:
    assert not looks_like_data_rows(text)
