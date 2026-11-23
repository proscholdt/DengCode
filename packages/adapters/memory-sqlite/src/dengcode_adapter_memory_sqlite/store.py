"""MemoryStorePort sobre SQLite: FTS5 + sqlite-vec + grafo de lineage.

Layout (docs/especificacoes/memoria.md, "Implementação padrão"):

- ``records``: todos os campos de ``MemoryRecord``; chave ``(workspace, id)``;
- ``records_fts``: FTS5 sobre título + corpo + entidades (``rowid = records.pk``);
- ``records_vec``: ``vec0`` do sqlite-vec, particionada por workspace, com o
  tipo como coluna de metadados (``rowid = records.pk``);
- ``edges``: arestas de lineage ``(workspace, source, target, via)``;
- ``meta``: chave/valor interno (dimensão dos vetores gravados).

Conexão: **uma conexão por store, protegida por um ``threading.Lock``**, com
cada operação rodando em ``asyncio.to_thread``. Motivos: (1) ``:memory:`` só
existe dentro de uma conexão; (2) o sqlite-vec precisa ser carregado em cada
conexão nova, o que custaria em toda operação; (3) o SQLite serializa escritas
de qualquer forma — o lock só evita uso concorrente do mesmo objeto. Em
arquivo usamos WAL + ``busy_timeout``, então outro processo (outra sessão do
mesmo workspace) lê enquanto esta escreve.

Isolamento: **toda** consulta filtra por ``scope.workspace``, mesmo o arquivo
sendo por workspace — não existe caminho de consulta entre workspaces.
``project``/``env`` do escopo restringem a visão: um registro sem projeto
(ou sem ambiente) vale para o workspace inteiro; um com projeto só aparece
para escopos sem projeto ou do mesmo projeto.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import math
import sqlite3
import threading
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar, Literal

import sqlite_vec

from dengcode_adapter_memory_sqlite.query import fts_match_expression
from dengcode_adapter_memory_sqlite.validator import check_metadata_only
from dengcode_core.errors import AdapterError, ConfigError, DengCodeError, ScopeError
from dengcode_core.ports.embedding import EmbeddingPort
from dengcode_core.ports.memory import MemoryHit, MemoryKind, MemoryRecord, Scope
from dengcode_core.ports.models import AdapterContext, AdapterInfo, LineageEdge, PortKind

MEMORY = ":memory:"
RRF_K = 60
ENTITY_DEPTH = 2
MIN_CANDIDATES = 20
MAX_KNN = 4096
BACKFILL_BATCH = 64

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS records (
    pk INTEGER PRIMARY KEY,
    workspace TEXT NOT NULL,
    id TEXT NOT NULL,
    project TEXT,
    env TEXT,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    entities TEXT NOT NULL DEFAULT '[]',
    source TEXT NOT NULL,
    confidence TEXT NOT NULL,
    schema_hash TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_ts REAL NOT NULL,
    UNIQUE (workspace, id)
);
CREATE INDEX IF NOT EXISTS records_ws_updated ON records (workspace, updated_ts DESC);
CREATE VIRTUAL TABLE IF NOT EXISTS records_fts USING fts5(
    title, body, entities, tokenize = 'unicode61 remove_diacritics 2'
);
CREATE TABLE IF NOT EXISTS edges (
    workspace TEXT NOT NULL,
    source TEXT NOT NULL,
    target TEXT NOT NULL,
    via TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (workspace, source, target, via)
);
CREATE INDEX IF NOT EXISTS edges_ws_source ON edges (workspace, lower(source));
CREATE INDEX IF NOT EXISTS edges_ws_target ON edges (workspace, lower(target));
"""

_COLUMNS = (
    "pk, workspace, id, project, env, kind, title, body, entities, source, confidence, "
    "schema_hash, created_at, updated_at, updated_ts"
)

Conn = sqlite3.Connection


class SqliteMemoryStore:
    """Memória do workspace num arquivo SQLite, com busca híbrida (RRF)."""

    info: ClassVar[AdapterInfo] = AdapterInfo(
        name="sqlite", port=PortKind.MEMORY_STORE, version="0.1.0"
    )
    name = "sqlite"

    def __init__(
        self,
        path: Path | str = MEMORY,
        *,
        embedder: EmbeddingPort | None = None,
        vectors: bool = True,
        min_similarity: float = 0.3,
        logger: logging.Logger | None = None,
    ) -> None:
        self._path = str(path)
        self._embedder = embedder
        self._want_vectors = vectors and embedder is not None
        self._max_distance = 1.0 - min_similarity
        self._log = logger or logging.getLogger(__name__)
        self._lock = threading.Lock()
        self._conn: Conn | None = None
        self._vectors = False
        self._ready = False
        self._warned = False

    @classmethod
    def from_config(cls, cfg: dict[str, Any], ctx: AdapterContext) -> SqliteMemoryStore:
        embedder = ctx.peers.get("embedding")
        if embedder is not None and not isinstance(embedder, EmbeddingPort):
            raise ConfigError("o peer 'embedding' não implementa EmbeddingPort")
        return cls(
            _resolve_path(cfg.get("path"), ctx),
            embedder=embedder,
            vectors=bool(cfg.get("vectors", True)),
            min_similarity=float(cfg.get("min_similarity", 0.3)),
            logger=ctx.logger,
        )

    @property
    def path(self) -> str:
        return self._path

    @property
    def vector_search_enabled(self) -> bool:
        """``True`` quando a busca vetorial está ativa (após a primeira operação)."""
        return self._vectors

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
                self._ready = False

    # ------------------------------------------------------------------ porta
    async def upsert(self, rec: MemoryRecord) -> None:
        _check_scope(rec.scope)
        check_metadata_only(rec)
        await self._ensure_ready()
        vector: list[float] | None = None
        if self._vectors:
            vectors = await self._embed([_embed_text(rec.title, rec.body)])
            vector = vectors[0] if vectors else None
        await self._call(lambda conn: _write(conn, rec, vector, vectors_on=self._vectors))

    async def get(self, scope: Scope, id: str) -> MemoryRecord | None:
        _check_scope(scope)
        await self._ensure_ready()
        return await self._call(lambda conn: _get(conn, scope, id))

    async def list(
        self, scope: Scope, *, kinds: list[MemoryKind] | None = None, limit: int = 50
    ) -> list[MemoryRecord]:
        _check_scope(scope)
        await self._ensure_ready()
        return await self._call(lambda conn: _list(conn, scope, kinds, limit))

    async def delete(self, scope: Scope, id: str) -> None:
        _check_scope(scope)
        await self._ensure_ready()
        await self._call(lambda conn: _delete(conn, scope, id, vectors_on=self._vectors))

    async def search(
        self,
        scope: Scope,
        query: str,
        *,
        kinds: list[MemoryKind] | None = None,
        entities: list[str] | None = None,
        limit: int = 10,
    ) -> list[MemoryHit]:
        _check_scope(scope)
        await self._ensure_ready()
        if limit <= 0:
            return []
        qvec: list[float] | None = None
        if self._vectors and query.strip():
            vectors = await self._embed([query])
            qvec = vectors[0] if vectors else None
        plan = _SearchPlan(
            scope=scope,
            match=fts_match_expression(query),
            qvec=qvec,
            kinds=kinds,
            entities=entities,
            limit=limit,
            max_distance=self._max_distance,
        )
        return await self._call(plan.run)

    async def add_edges(self, scope: Scope, edges: list[LineageEdge]) -> None:
        _check_scope(scope)
        await self._ensure_ready()
        if edges:
            await self._call(lambda conn: _add_edges(conn, scope.workspace, edges))

    async def neighbors(
        self, scope: Scope, entity: str, *, direction: Literal["up", "down"], depth: int = 2
    ) -> list[LineageEdge]:
        _check_scope(scope)
        await self._ensure_ready()
        return await self._call(
            lambda conn: _neighbors(conn, scope.workspace, entity, direction, depth)
        )

    # -------------------------------------------------------------- infra
    async def _call[T](self, fn: Callable[[Conn], T]) -> T:
        return await asyncio.to_thread(self._run, fn)

    def _run[T](self, fn: Callable[[Conn], T]) -> T:
        with self._lock:
            try:
                return fn(self._connection())
            except sqlite3.Error as exc:
                raise AdapterError(f"memória sqlite ({self._path}): {exc}") from exc

    def _connection(self) -> Conn:
        if self._conn is None:
            if self._path != MEMORY:
                Path(self._path).parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self._path, check_same_thread=False)
            try:
                self._configure(conn)
            except BaseException:
                conn.close()
                raise
            self._conn = conn
        return self._conn

    def _configure(self, conn: Conn) -> None:
        conn.execute("PRAGMA busy_timeout = 5000")
        if self._path != MEMORY:
            conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(_SCHEMA)
        self._vectors = self._load_vectors(conn)
        if self._vectors:
            assert self._embedder is not None
            _ensure_vec_table(conn, self._embedder.dimensions, self._log)

    def _load_vectors(self, conn: Conn) -> bool:
        if self._embedder is None:
            self._fallback("memória sem adapter de embedding na stack")
            return False
        if not self._want_vectors:
            self._fallback("busca vetorial desativada na config")
            return False
        try:
            conn.enable_load_extension(True)
            try:
                sqlite_vec.load(conn)
            finally:
                conn.enable_load_extension(False)
        except (AttributeError, sqlite3.Error, OSError) as exc:
            # alguns Pythons (notadamente builds de macOS) vêm sem extensões
            self._fallback(f"sqlite-vec não carregou neste Python ({exc})")
            return False
        return True

    def _fallback(self, reason: str) -> None:
        """Um único aviso por store, mesmo que a conexão seja reaberta."""
        if not self._warned:
            self._warned = True
            self._log.warning("%s: a busca da memória usa só texto (FTS5)", reason)

    async def _ensure_ready(self) -> None:
        if self._ready:
            return
        await self._call(lambda conn: None)
        if self._vectors:
            await self._backfill()
        self._ready = True

    async def _backfill(self) -> None:
        """Gera vetores de registros que ainda não têm (fallback antigo, troca de modelo)."""
        missing = await self._call(_missing_vectors)
        for start in range(0, len(missing), BACKFILL_BATCH):
            batch = missing[start : start + BACKFILL_BATCH]
            vectors = await self._embed([text for _, _, _, text in batch])
            if vectors is None:
                return
            rows = [
                (pk, workspace, kind, vec)
                for (pk, workspace, kind, _), vec in zip(batch, vectors, strict=True)
            ]
            await self._call(functools.partial(_insert_vectors, rows=rows))

    async def _embed(self, texts: list[str]) -> list[list[float]] | None:
        """Vetores dos textos, ou ``None`` se o embedder falhar (cai para FTS5)."""
        assert self._embedder is not None
        try:
            vectors = await self._embedder.embed(texts)
        except DengCodeError as exc:
            self._log.warning("embedding falhou (%s): seguindo só com FTS5", exc)
            return None
        dims = self._embedder.dimensions
        if len(vectors) != len(texts) or any(len(v) != dims for v in vectors):
            self._log.warning("embedding devolveu dimensão inesperada (esperado %d)", dims)
            return None
        return vectors


# ---------------------------------------------------------------------- helpers
def _resolve_path(raw: Any, ctx: AdapterContext) -> str:
    if raw is None or str(raw).strip() == "":
        if ctx.cache_dir is not None:
            return str(ctx.cache_dir.parent / "memory.db")
        return str(Path.home() / ".dengcode" / "workspaces" / ctx.workspace / "memory.db")
    text = str(raw).strip()
    if text == MEMORY:
        return MEMORY
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = (ctx.project_dir or Path.cwd()) / path
    return str(path.resolve())


def _check_scope(scope: Scope) -> None:
    # o Scope já valida; isto cobre objetos montados sem validação (model_construct)
    if not scope.workspace or not scope.workspace.strip():
        raise ScopeError("escopo de memória sem workspace")


def _embed_text(title: str, body: str) -> str:
    return f"{title}\n{body}"


def _ts(value: datetime) -> float:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.timestamp()


def _norm(vector: list[float]) -> float:
    return math.sqrt(sum(x * x for x in vector))


def _json_list(values: Iterable[Any]) -> str:
    return json.dumps(list(values), ensure_ascii=False)


def _scope_sql(scope: Scope, alias: str = "r") -> tuple[str, list[Any]]:
    sql = f"{alias}.workspace = ?"
    params: list[Any] = [scope.workspace]
    if scope.project is not None:
        sql += f" AND ({alias}.project IS NULL OR {alias}.project = ?)"
        params.append(scope.project)
    if scope.env is not None:
        sql += f" AND ({alias}.env IS NULL OR {alias}.env = ?)"
        params.append(scope.env)
    return sql, params


def _kinds_sql(kinds: list[MemoryKind] | None, alias: str = "r") -> tuple[str, list[Any]]:
    if not kinds:
        return "", []
    return f" AND {alias}.kind IN (SELECT value FROM json_each(?))", [
        _json_list(str(k) for k in kinds)
    ]


def _row_to_record(row: tuple[Any, ...]) -> MemoryRecord:
    (_, workspace, id_, project, env, kind, title, body, entities, source, confidence) = row[:11]
    schema_hash, created_at, updated_at = row[11], row[12], row[13]
    return MemoryRecord.model_validate(
        {
            "id": id_,
            "scope": {"workspace": workspace, "project": project, "env": env},
            "kind": kind,
            "title": title,
            "body": body,
            "entities": json.loads(entities),
            "source": source,
            "confidence": confidence,
            "schema_hash": schema_hash,
            "created_at": created_at,
            "updated_at": updated_at,
        }
    )


def _ensure_vec_table(conn: Conn, dims: int, log: logging.Logger) -> None:
    row = conn.execute("SELECT value FROM meta WHERE key = 'vec_dimensions'").fetchone()
    if row is not None and int(row[0]) != dims:
        log.info("dimensão dos vetores mudou (%s → %d): reindexando a memória", row[0], dims)
        conn.execute("DROP TABLE IF EXISTS records_vec")
    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS records_vec USING vec0("
        "workspace text partition key, kind text, "
        f"embedding float[{int(dims)}] distance_metric=cosine)"
    )
    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('vec_dimensions', ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (str(dims),),
    )
    conn.commit()


def _missing_vectors(conn: Conn) -> list[tuple[int, str, str, str]]:
    rows = conn.execute(
        "SELECT pk, workspace, kind, title, body FROM records "
        "WHERE pk NOT IN (SELECT rowid FROM records_vec) ORDER BY pk"
    ).fetchall()
    return [(pk, ws, kind, _embed_text(title, body)) for pk, ws, kind, title, body in rows]


def _insert_vectors(conn: Conn, rows: list[tuple[int, str, str, list[float]]]) -> None:
    with conn:
        for pk, workspace, kind, vector in rows:
            _put_vector(conn, pk, workspace, kind, vector)


def _put_vector(conn: Conn, pk: int, workspace: str, kind: str, vector: list[float]) -> None:
    conn.execute("DELETE FROM records_vec WHERE rowid = ?", (pk,))
    if _norm(vector) == 0.0:
        return  # vetor nulo não tem direção: o registro fica só no FTS
    conn.execute(
        "INSERT INTO records_vec (rowid, workspace, kind, embedding) VALUES (?, ?, ?, ?)",
        (pk, workspace, kind, sqlite_vec.serialize_float32(vector)),
    )


def _write(conn: Conn, rec: MemoryRecord, vector: list[float] | None, *, vectors_on: bool) -> None:
    scope = rec.scope
    with conn:
        conn.execute(
            "INSERT INTO records (workspace, id, project, env, kind, title, body, entities, "
            "source, confidence, schema_hash, created_at, updated_at, updated_ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (workspace, id) DO UPDATE SET project = excluded.project, "
            "env = excluded.env, kind = excluded.kind, title = excluded.title, "
            "body = excluded.body, entities = excluded.entities, source = excluded.source, "
            "confidence = excluded.confidence, schema_hash = excluded.schema_hash, "
            "updated_at = excluded.updated_at, updated_ts = excluded.updated_ts",
            (
                scope.workspace,
                rec.id,
                scope.project,
                scope.env,
                str(rec.kind),
                rec.title,
                rec.body,
                _json_list(rec.entities),
                rec.source,
                rec.confidence,
                rec.schema_hash,
                rec.created_at.isoformat(),
                rec.updated_at.isoformat(),
                _ts(rec.updated_at),
            ),
        )
        (pk,) = conn.execute(
            "SELECT pk FROM records WHERE workspace = ? AND id = ?", (scope.workspace, rec.id)
        ).fetchone()
        conn.execute("DELETE FROM records_fts WHERE rowid = ?", (pk,))
        conn.execute(
            "INSERT INTO records_fts (rowid, title, body, entities) VALUES (?, ?, ?, ?)",
            (pk, rec.title, rec.body, " ".join(rec.entities)),
        )
        if vectors_on:
            if vector is None:
                # sem vetor novo (embedder falhou): apaga o antigo; o backfill repõe depois
                conn.execute("DELETE FROM records_vec WHERE rowid = ?", (pk,))
            else:
                _put_vector(conn, pk, scope.workspace, str(rec.kind), vector)


def _get(conn: Conn, scope: Scope, id_: str) -> MemoryRecord | None:
    where, params = _scope_sql(scope)
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM records r WHERE {where} AND r.id = ?", [*params, id_]
    ).fetchone()
    return _row_to_record(row) if row else None


def _list(
    conn: Conn, scope: Scope, kinds: list[MemoryKind] | None, limit: int
) -> list[MemoryRecord]:
    if limit <= 0:
        return []
    where, params = _scope_sql(scope)
    kinds_sql, kinds_params = _kinds_sql(kinds)
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM records r WHERE {where}{kinds_sql} "
        "ORDER BY r.updated_ts DESC, r.pk DESC LIMIT ?",
        [*params, *kinds_params, limit],
    ).fetchall()
    return [_row_to_record(row) for row in rows]


def _delete(conn: Conn, scope: Scope, id_: str, *, vectors_on: bool) -> None:
    where, params = _scope_sql(scope)
    row = conn.execute(f"SELECT pk FROM records r WHERE {where} AND r.id = ?", [*params, id_])
    found = row.fetchone()
    if found is None:
        return
    (pk,) = found
    with conn:
        conn.execute("DELETE FROM records WHERE pk = ?", (pk,))
        conn.execute("DELETE FROM records_fts WHERE rowid = ?", (pk,))
        if vectors_on:
            conn.execute("DELETE FROM records_vec WHERE rowid = ?", (pk,))


def _add_edges(conn: Conn, workspace: str, edges: list[LineageEdge]) -> None:
    with conn:
        conn.executemany(
            "INSERT OR IGNORE INTO edges (workspace, source, target, via) VALUES (?, ?, ?, ?)",
            [(workspace, e.source, e.target, e.via or "") for e in edges],
        )


def _neighbors(
    conn: Conn, workspace: str, entity: str, direction: Literal["up", "down"], depth: int
) -> list[LineageEdge]:
    """BFS dirigida: ``up`` segue quem alimenta a entidade; ``down``, quem ela alimenta."""
    near, far = ("target", "source") if direction == "up" else ("source", "target")
    visited = {entity.lower()}
    frontier = [entity.lower()]
    seen_edges: set[tuple[str, str, str]] = set()
    out: list[LineageEdge] = []
    for _ in range(max(depth, 0)):
        if not frontier:
            break
        rows = conn.execute(
            f"SELECT source, target, via FROM edges WHERE workspace = ? "
            f"AND lower({near}) IN (SELECT value FROM json_each(?)) "
            "ORDER BY source, target, via",
            (workspace, _json_list(frontier)),
        ).fetchall()
        nxt: list[str] = []
        for source, target, via in rows:
            key = (source.lower(), target.lower(), via)
            if key in seen_edges:
                continue
            seen_edges.add(key)
            out.append(LineageEdge(source=source, target=target, via=via or None))
            other = (source if far == "source" else target).lower()
            if other not in visited:
                visited.add(other)
                nxt.append(other)
        frontier = nxt
    return out


def _expand_entities(conn: Conn, workspace: str, entities: list[str], depth: int) -> dict[str, int]:
    """Entidades pedidas + vizinhas no grafo (qualquer direção), com a distância."""
    dist = {e.strip().lower(): 0 for e in entities if e.strip()}
    frontier = list(dist)
    for level in range(1, depth + 1):
        if not frontier:
            break
        rows = conn.execute(
            "SELECT lower(source), lower(target) FROM edges WHERE workspace = ?1 "
            "AND (lower(source) IN (SELECT value FROM json_each(?2)) "
            "OR lower(target) IN (SELECT value FROM json_each(?2)))",
            (workspace, _json_list(frontier)),
        ).fetchall()
        current = set(frontier)
        nxt: list[str] = []
        for source, target in rows:
            for node, other in ((source, target), (target, source)):
                if node in current and other not in dist:
                    dist[other] = level
                    nxt.append(other)
        frontier = nxt
    return dist


def _entity_distance(record_entities: list[str], expanded: dict[str, int]) -> int | None:
    """Menor distância entre as entidades do registro e o conjunto expandido.

    Tabela e coluna se casam: ``silver.orders`` casa ``silver.orders.customer_id``
    (nos dois sentidos)."""
    best: int | None = None
    for raw in record_entities:
        entity = raw.lower()
        for target, d in expanded.items():
            related = entity.startswith(target + ".") or target.startswith(entity + ".")
            if entity == target or related:
                best = d if best is None else min(best, d)
    return best


class _SearchPlan:
    """Busca híbrida dentro de uma thread: três listas ranqueadas + RRF."""

    def __init__(
        self,
        *,
        scope: Scope,
        match: str | None,
        qvec: list[float] | None,
        kinds: list[MemoryKind] | None,
        entities: list[str] | None,
        limit: int,
        max_distance: float,
    ) -> None:
        self.scope = scope
        self.match = match
        self.qvec = qvec
        self.kinds = kinds
        self.entities = [e for e in (entities or []) if e.strip()]
        self.limit = limit
        self.max_distance = max_distance
        self.pool = max(limit * 4, MIN_CANDIDATES)

    def run(self, conn: Conn) -> list[MemoryHit]:
        candidates: set[int] | None = None
        ranked: dict[str, list[int]] = {}
        if self.entities:
            graph = self._graph(conn)
            candidates = set(graph)
            if not candidates:
                return []
            ranked["grafo"] = graph[: self.pool]
        if self.match is not None:
            ranked["fts"] = self._fts(conn, candidates)
        if self.qvec is not None:
            ranked["vetor"] = self._vector(conn, self.qvec, candidates)
        return self._fuse(conn, ranked)

    def _filters(self, alias: str = "r") -> tuple[str, list[Any]]:
        where, params = _scope_sql(self.scope, alias)
        kinds_sql, kinds_params = _kinds_sql(self.kinds, alias)
        return where + kinds_sql, [*params, *kinds_params]

    def _graph(self, conn: Conn) -> list[int]:
        expanded = _expand_entities(conn, self.scope.workspace, self.entities, ENTITY_DEPTH)
        where, params = self._filters()
        rows = conn.execute(
            f"SELECT r.pk, r.entities, r.updated_ts FROM records r WHERE {where} "
            "AND r.entities != '[]'",
            params,
        ).fetchall()
        scored: list[tuple[int, float, int]] = []
        for pk, entities, updated_ts in rows:
            d = _entity_distance(json.loads(entities), expanded)
            if d is not None:
                scored.append((d, -updated_ts, pk))
        scored.sort()
        return [pk for _, _, pk in scored]

    def _fts(self, conn: Conn, candidates: set[int] | None) -> list[int]:
        where, params = self._filters()
        cand_sql = ""
        if candidates is not None:
            cand_sql = " AND r.pk IN (SELECT value FROM json_each(?))"
            params.append(_json_list(sorted(candidates)))
        try:
            rows = conn.execute(
                "SELECT r.pk FROM records_fts f JOIN records r ON r.pk = f.rowid "
                f"WHERE records_fts MATCH ? AND {where}{cand_sql} "
                "ORDER BY bm25(records_fts, 5.0, 1.0, 2.0) LIMIT ?",
                [self.match, *params, self.pool],
            ).fetchall()
        except sqlite3.OperationalError:
            # a expressão é montada só com palavras; isto é só uma rede de segurança
            return []
        return [pk for (pk,) in rows]

    def _vector(self, conn: Conn, qvec: list[float], candidates: set[int] | None) -> list[int]:
        if _norm(qvec) == 0.0:
            return []
        kinds_sql = ""
        params: list[Any] = [sqlite_vec.serialize_float32(qvec), min(MAX_KNN, self.pool * 8)]
        params.append(self.scope.workspace)
        if self.kinds:
            marks = ", ".join("?" for _ in self.kinds)
            kinds_sql = f" AND kind IN ({marks})"
            params.extend(str(k) for k in self.kinds)
        rows = conn.execute(
            "SELECT rowid, distance FROM records_vec WHERE embedding MATCH ? AND k = ? "
            f"AND workspace = ?{kinds_sql} ORDER BY distance",
            params,
        ).fetchall()
        near = [pk for pk, distance in rows if distance <= self.max_distance]
        if candidates is not None:
            near = [pk for pk in near if pk in candidates]
        if not near:
            return []
        # project/env do escopo (e o workspace, de novo) conferidos na tabela principal
        where, wparams = _scope_sql(self.scope)
        allowed = {
            pk
            for (pk,) in conn.execute(
                f"SELECT r.pk FROM records r WHERE {where} "
                "AND r.pk IN (SELECT value FROM json_each(?))",
                [*wparams, _json_list(near)],
            ).fetchall()
        }
        return [pk for pk in near if pk in allowed][: self.pool]

    def _fuse(self, conn: Conn, ranked: dict[str, list[int]]) -> list[MemoryHit]:
        """Reciprocal Rank Fusion: score = Σ 1 / (k + posição), posição a partir de 1."""
        scores: dict[int, float] = {}
        matched: dict[int, list[str]] = {}
        for source in ("fts", "vetor", "grafo"):
            for position, pk in enumerate(ranked.get(source, []), start=1):
                scores[pk] = scores.get(pk, 0.0) + 1.0 / (RRF_K + position)
                matched.setdefault(pk, []).append(source)
        if not scores:
            return []
        where, params = _scope_sql(self.scope)
        rows = conn.execute(
            f"SELECT {_COLUMNS} FROM records r WHERE {where} "
            "AND r.pk IN (SELECT value FROM json_each(?))",
            [*params, _json_list(scores)],
        ).fetchall()
        by_pk = {row[0]: row for row in rows}
        order = sorted(by_pk, key=lambda pk: (-scores[pk], -by_pk[pk][14], -pk))
        return [
            MemoryHit(record=_row_to_record(by_pk[pk]), score=scores[pk], matched_by=matched[pk])
            for pk in order[: self.limit]
        ]
