"""Memória no motor: resumo do workspace para o prompt e sincronização de catálogo."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from dengcode_core.errors import DengCodeError
from dengcode_core.memory_records import table_record
from dengcode_core.ports.catalog import CatalogPort
from dengcode_core.ports.memory import MemoryKind, MemoryRecord, MemoryStorePort, Scope
from dengcode_core.ports.models import ColumnStats

log = logging.getLogger(__name__)


def _stats_line(s: ColumnStats) -> str:
    nulls = f"{s.null_fraction:.1%}" if s.null_fraction is not None else "?"
    return f"- {s.column}: nulos {nulls}, distintos {s.distinct_count}"


async def workspace_summary(memory: MemoryStorePort, scope: Scope) -> str:
    tables = await memory.list(scope, kinds=[MemoryKind.CATALOG_TABLE], limit=1000)
    incidents = await memory.list(scope, kinds=[MemoryKind.INCIDENT], limit=5)
    issues = await memory.list(scope, kinds=[MemoryKind.KNOWN_ISSUE], limit=5)
    lines = [f"- tabelas conhecidas: {len(tables)}"]
    if incidents:
        lines.append(
            "- incidentes recentes: "
            + "; ".join(f"{r.title} ({r.updated_at:%Y-%m-%d})" for r in incidents)
        )
    if issues:
        lines.append("- problemas conhecidos: " + "; ".join(r.title for r in issues))
    lines.append("Use `memory_search` para detalhes (memória é pista: confira ao vivo).")
    return "\n".join(lines)


@dataclass
class SyncReport:
    tables: int = 0
    changed: int = 0
    stats: int = 0
    errors: int = 0


async def sync_catalog(
    catalog: CatalogPort,
    memory: MemoryStorePort,
    scope: Scope,
    *,
    source: str,
    max_tables: int = 2000,
) -> SyncReport:
    """Grava CATALOG_TABLE e COLUMN_STATS (só frações e contagens) como ``verified``."""
    report = SyncReport()
    stats_ok = catalog.capabilities().supports_column_stats
    for schema in await catalog.list_schemas():
        for ref in await catalog.list_tables(schema):
            if report.tables >= max_tables:
                return report
            try:
                info = await catalog.describe(ref)
            except DengCodeError as exc:
                log.info("sync: %s falhou: %s", ref.qualified(), exc)
                report.errors += 1
                continue
            new = table_record(info, scope, source)
            old = await memory.get(scope, new.id)
            if old is not None and old.schema_hash != new.schema_hash:
                report.changed += 1
            await memory.upsert(new)
            report.tables += 1
            if not stats_ok:
                continue
            try:
                stats = await catalog.column_stats(ref)
            except DengCodeError:
                continue
            q = info.ref.qualified().lower()
            await memory.upsert(
                MemoryRecord(
                    id=f"stats:{q}",
                    scope=scope,
                    kind=MemoryKind.COLUMN_STATS,
                    title=f"estatísticas de {q}",
                    body="\n".join(_stats_line(s) for s in stats),
                    entities=[q],
                    source=f"catalog:{source}",
                    confidence="verified",
                    schema_hash=info.schema_hash,
                )
            )
            report.stats += 1
    return report
