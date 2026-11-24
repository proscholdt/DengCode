# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false
# (os stubs do pyarrow.compute são incompletos; os tipos de retorno abaixo são explícitos)
"""ResultStore: resultado completo em Parquet; ao LLM vai só a prévia mascarada.

Ao LLM: schema, contagem, até 20 linhas e estatísticas simples por coluna.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import Any, cast

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from dengcode_core.privacy.pii import PiiMasker, is_pii_column

PREVIEW_ROWS = 20
MAX_CELL = 80


class ResultStore:
    def __init__(self, cache_dir: Path) -> None:
        self.dir = cache_dir / "results"

    def save(self, table: pa.Table, result_id: str | None = None) -> Path:
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.dir / f"{result_id or uuid.uuid4().hex[:12]}.parquet"
        pq.write_table(table, path)
        return path


def _cell(value: Any) -> str:
    if value is None:
        return "∅"
    text = str(value).replace("\n", " ").replace("|", "\\|")
    return text if len(text) <= MAX_CELL else text[: MAX_CELL - 1] + "…"


def markdown_table(table: pa.Table, rows: int = PREVIEW_ROWS) -> str:
    head = table.slice(0, rows)
    names = head.column_names
    if not names:
        return "(sem colunas)"
    lines = ["| " + " | ".join(names) + " |", "|" + "---|" * len(names)]
    for row in head.to_pylist():
        lines.append("| " + " | ".join(_cell(row[n]) for n in names) + " |")
    return "\n".join(lines)


def column_stats(
    table: pa.Table, pii_columns: Iterable[str] = (), masker: PiiMasker | None = None
) -> list[str]:
    """Nulos, distintos e min/max (nunca de colunas PII)."""
    masker = masker or PiiMasker()
    marked = {c.lower() for c in pii_columns}
    out: list[str] = []
    for field, col in zip(table.schema, table.columns, strict=True):
        parts = [f"nulos {col.null_count}"]
        pii = field.name.lower() in marked or is_pii_column(field.name)
        try:
            parts.append(f"distintos {pc.count_distinct(col).as_py()}")
            t = field.type
            if not pii and (
                pa.types.is_integer(t)
                or pa.types.is_floating(t)
                or pa.types.is_decimal(t)
                or pa.types.is_temporal(t)
            ):
                mm = cast(dict[str, Any], pc.min_max(col).as_py())
                parts.append(f"min {mm['min']} · max {mm['max']}")
                if not pa.types.is_temporal(t):
                    mean = cast(Any, pc.mean(col).as_py())
                    if mean is not None:
                        parts.append(f"média {float(mean):.4g}")
        except (pa.ArrowNotImplementedError, pa.ArrowInvalid, TypeError):
            pass
        if pii:
            parts.append("PII: valores omitidos")
        out.append(f"- {field.name} ({field.type}): " + ", ".join(parts))
    return out


def _json_cell(value: Any) -> Any:
    """Valor de célula seguro para JSON (datas, decimais e binários viram texto)."""
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    text = value.isoformat() if hasattr(value, "isoformat") else str(value)
    return text if len(text) <= MAX_CELL * 4 else text[: MAX_CELL * 4 - 1] + "…"


def preview_payload(
    table: pa.Table,
    *,
    masker: PiiMasker,
    pii_columns: Iterable[str] = (),
    rows: int = PREVIEW_ROWS,
) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    """Schema e prévia (até ``rows`` linhas, PII mascarada) em formato JSON, para as
    interfaces mostrarem uma grade. Nunca devolve valores crus de colunas PII."""
    masked = masker.table(table.slice(0, min(rows, PREVIEW_ROWS)), list(pii_columns))
    columns = [{"name": f.name, "type": str(f.type)} for f in table.schema]
    names = masked.column_names
    data = [{n: _json_cell(row[n]) for n in names} for row in masked.to_pylist()]
    return columns, data


def render_for_llm(
    table: pa.Table,
    *,
    total_rows: int | None,
    path: Path | None,
    masker: PiiMasker,
    pii_columns: Iterable[str] = (),
    preview_rows: int = PREVIEW_ROWS,
    note: str | None = None,
) -> str:
    pii = list(pii_columns)
    masked = masker.table(table.slice(0, preview_rows), pii)
    count = f"{total_rows}" if total_rows is not None else f"≥ {table.num_rows} (truncado)"
    lines = [
        f"linhas: {count}",
        "colunas: " + ", ".join(f"{f.name} ({f.type})" for f in table.schema),
    ]
    if note:
        lines.append(note)
    if table.num_rows:
        lines += [f"prévia (até {preview_rows} linhas, PII mascarada):", markdown_table(masked)]
        lines += ["estatísticas:", *column_stats(table, pii, masker)]
    if path is not None:
        lines.append(f"resultado completo salvo em: {path}")
    return "\n".join(lines)
