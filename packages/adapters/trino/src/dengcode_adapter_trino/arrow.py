"""Conversão das linhas JSON do protocolo do Trino em ``pyarrow.Table``.

O tipo Arrow vem do texto do tipo Trino (``columns[].type``, ex.:
``varchar(50)``, ``decimal(10,2)``, ``timestamp(6) with time zone``,
``array(integer)``). O cliente declara ``PARAMETRIC_DATETIME``, então os
valores de data/hora chegam com a precisão real (até picossegundos; o Arrow
guarda microssegundos — o excesso é truncado).

Tipos sem equivalente direto (``map``, ``row``, ``json``, ``uuid``,
``ipaddress``, intervalos, ``time with time zone``...) viram texto (``map`` e
``row`` em JSON). Se a conversão de uma coluna falhar, ela cai para texto —
nunca se perde a linha.
"""

from __future__ import annotations

import base64
import json
import math
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, timezone, tzinfo
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pyarrow as pa

Converter = Callable[[Any], Any]

_INTEGERS: dict[str, pa.DataType] = {
    "tinyint": pa.int8(),
    "smallint": pa.int16(),
    "integer": pa.int32(),
    "int": pa.int32(),
    "bigint": pa.int64(),
}
_FLOATS: dict[str, pa.DataType] = {"real": pa.float32(), "double": pa.float64()}
_SPECIAL_FLOATS = {"NaN": math.nan, "Infinity": math.inf, "-Infinity": -math.inf}
_TYPE = re.compile(r"^\s*([a-zA-Z_ ]+?)\s*(?:\((.*)\))?\s*(with time zone)?\s*$", re.DOTALL)
_FRACTION = re.compile(r"\.(\d+)")
_OFFSET = re.compile(r"^([+-])(\d{2}):?(\d{2})$")
_MAX_DECIMAL_PRECISION = 38
_CONVERSION_ERRORS = (pa.ArrowException, TypeError, ValueError, OverflowError, KeyError)


@dataclass(frozen=True)
class TrinoColumn:
    """Uma coluna do resultado: nome e tipo como o Trino descreve."""

    name: str
    type: str


@dataclass(frozen=True)
class ParsedType:
    base: str  # ex.: "varchar", "decimal", "timestamp", "array"
    args: str | None  # conteúdo entre parênteses, sem tocar (ex.: "10,2", "integer")
    with_time_zone: bool


def parse_type(text: str) -> ParsedType:
    """Separa ``timestamp(3) with time zone`` em base, argumentos e fuso."""
    match = _TYPE.match(text)
    if match is None:
        return ParsedType(base=text.strip().lower(), args=None, with_time_zone=False)
    base = " ".join(match.group(1).lower().split())
    with_tz = match.group(3) is not None
    if base.endswith(" with time zone"):  # sem parênteses: "timestamp with time zone"
        base, with_tz = base.removesuffix(" with time zone"), True
    return ParsedType(base=base, args=match.group(2), with_time_zone=with_tz)


# ------------------------------------------------------------------ conversores
def _float(value: Any) -> float:
    if isinstance(value, str):
        return _SPECIAL_FLOATS[value] if value in _SPECIAL_FLOATS else float(value)
    return float(value)


def _truncate_fraction(text: str) -> str:
    """Corta a fração de segundos em 6 dígitos (o datetime vai até microssegundos)."""
    return _FRACTION.sub(lambda m: "." + m.group(1)[:6], text, count=1)


def parse_date(value: str) -> date:
    return date.fromisoformat(value)


def parse_time(value: str) -> time:
    return time.fromisoformat(_truncate_fraction(value))


def parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(_truncate_fraction(value))


def _zone(name: str) -> tzinfo:
    if name in ("UTC", "Z", "GMT"):
        return UTC
    offset = _OFFSET.match(name)
    if offset:
        sign = -1 if offset.group(1) == "-" else 1
        delta = timedelta(hours=int(offset.group(2)), minutes=int(offset.group(3)))
        return timezone(sign * delta)
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"fuso horário desconhecido: {name}") from exc


def parse_timestamp_tz(value: str) -> datetime:
    """``2026-01-31 10:00:00.000 America/Sao_Paulo`` → datetime em UTC."""
    stamp, _, zone = value.rpartition(" ")
    if not stamp:
        raise ValueError(f"timestamp com fuso sem fuso: {value!r}")
    local = parse_timestamp(stamp).replace(tzinfo=_zone(zone))
    return local.astimezone(UTC)


def _binary(value: Any) -> bytes:
    return base64.b64decode(value)


def _as_text(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)


def _decimal_type(args: str | None) -> pa.DataType:
    if not args:
        return pa.decimal128(_MAX_DECIMAL_PRECISION, 0)
    parts = [p.strip() for p in args.split(",")]
    precision = int(parts[0])
    scale = int(parts[1]) if len(parts) > 1 else 0
    if precision > _MAX_DECIMAL_PRECISION:
        return pa.decimal256(precision, scale)
    return pa.decimal128(precision, scale)


def scalar_mapping(trino_type: str) -> tuple[pa.DataType, Converter] | None:
    """Tipo Arrow + conversor de valor para tipos com equivalente direto."""
    parsed = parse_type(trino_type)
    base = parsed.base
    if base == "boolean":
        return pa.bool_(), bool
    if base in _INTEGERS:
        return _INTEGERS[base], int
    if base in _FLOATS:
        return _FLOATS[base], _float
    if base == "decimal":
        return _decimal_type(parsed.args), Decimal
    if base in ("varchar", "char"):
        return pa.string(), str
    if base == "varbinary":
        return pa.binary(), _binary
    if base == "date":
        return pa.date32(), parse_date
    if base == "time" and not parsed.with_time_zone:
        return pa.time64("us"), parse_time
    if base == "timestamp":
        if parsed.with_time_zone:
            return pa.timestamp("us", tz="UTC"), parse_timestamp_tz
        return pa.timestamp("us"), parse_timestamp
    return None


def column_mapping(trino_type: str) -> tuple[pa.DataType, Converter]:
    """Tipo Arrow + conversor de uma coluna; o que não tem equivalente vira texto."""
    scalar = scalar_mapping(trino_type)
    if scalar is not None:
        return scalar
    parsed = parse_type(trino_type)
    if parsed.base == "array" and parsed.args:
        element = scalar_mapping(parsed.args)
        if element is not None:
            arrow_type, convert = element

            def convert_list(values: Any) -> list[Any]:
                return [None if v is None else convert(v) for v in values]

            return pa.list_(arrow_type), convert_list
    return pa.string(), _as_text


def to_arrow_array(trino_type: str, values: Sequence[Any]) -> pa.Array:
    """Converte uma coluna; se algo falhar, a coluna inteira vira texto."""
    arrow_type, convert = column_mapping(trino_type)
    if arrow_type == pa.string() and convert is _as_text:
        return pa.array([None if v is None else _as_text(v) for v in values], type=pa.string())
    try:
        converted = [None if v is None else convert(v) for v in values]
        return pa.array(converted, type=arrow_type)
    except _CONVERSION_ERRORS:
        return pa.array([None if v is None else _as_text(v) for v in values], type=pa.string())


def rows_to_table(columns: Sequence[TrinoColumn], rows: Sequence[Sequence[Any]]) -> pa.Table:
    """Monta a tabela Arrow; sem linhas, mantém nomes e tipos das colunas."""
    if not columns:
        return pa.table({})
    arrays = [to_arrow_array(col.type, [row[i] for row in rows]) for i, col in enumerate(columns)]
    names = [col.name for col in columns]
    if len(set(names)) == len(names):
        return pa.Table.from_arrays(arrays, names=names)
    # nomes repetidos (ex.: SELECT 1, 1): o Arrow aceita via schema explícito
    schema = pa.schema([pa.field(n, a.type) for n, a in zip(names, arrays, strict=True)])
    return pa.Table.from_arrays(arrays, schema=schema)
