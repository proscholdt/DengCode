"""AuditLog: toda query (executada, negada ou recusada) em SQLite append-only.

Sem valores de linha; o SQL é guardado com PII mascarada. Fica fora do prompt.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

from dengcode_core.privacy.pii import mask_text

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    workspace TEXT NOT NULL,
    stack TEXT,
    env TEXT,
    engine TEXT,
    sql TEXT NOT NULL,
    kind TEXT,
    decision TEXT NOT NULL,
    approved_by TEXT,
    estimated_cost TEXT,
    actual_cost TEXT,
    rows INTEGER,
    elapsed_ms INTEGER,
    error TEXT
);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit
BEGIN SELECT RAISE(ABORT, 'audit é append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit
BEGIN SELECT RAISE(ABORT, 'audit é append-only'); END;
"""

_COLUMNS = (
    "ts",
    "workspace",
    "stack",
    "env",
    "engine",
    "sql",
    "kind",
    "decision",
    "approved_by",
    "estimated_cost",
    "actual_cost",
    "rows",
    "elapsed_ms",
    "error",
)


class AuditRecord(BaseModel):
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))
    workspace: str
    stack: str | None = None
    env: str | None = None
    engine: str | None = None
    sql: str
    kind: str | None = None
    decision: str
    approved_by: str | None = None
    estimated_cost: str | None = None
    actual_cost: str | None = None
    rows: int | None = None
    elapsed_ms: int | None = None
    error: str | None = None


class AuditLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _conn(self) -> Generator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def append(self, rec: AuditRecord) -> None:
        data = rec.model_dump()
        data["ts"] = rec.ts.isoformat()
        data["sql"] = mask_text(rec.sql)
        data["error"] = mask_text(rec.error) if rec.error else None
        placeholders = ", ".join("?" for _ in _COLUMNS)
        with self._conn() as conn:
            conn.execute(
                f"INSERT INTO audit ({', '.join(_COLUMNS)}) VALUES ({placeholders})",
                [data[c] for c in _COLUMNS],
            )

    def list(self, *, since: datetime | None = None, limit: int = 100) -> list[AuditRecord]:
        sql = f"SELECT {', '.join(_COLUMNS)} FROM audit"
        params: list[object] = []
        if since is not None:
            sql += " WHERE ts >= ?"
            params.append(since.isoformat())
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [AuditRecord(**dict(zip(_COLUMNS, r, strict=True))) for r in rows]
