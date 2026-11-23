"""Conversão de linhas do psycopg em ``pyarrow.Table``.

O tipo Arrow de cada coluna vem do OID do PostgreSQL (``cursor.description``).
Tipos sem equivalente direto (json, uuid, inet, timetz, enums, geométricos...)
viram texto. Se a conversão para o tipo escolhido falhar (ex.: ``numeric``
com ``NaN``), a coluna também cai para texto — nunca se perde a linha.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pyarrow as pa

# OIDs fixos do catálogo do PostgreSQL (pg_type)
_BOOL, _BYTEA, _CHAR, _NAME, _INT8, _INT2, _INT4, _TEXT, _OID = 16, 17, 18, 19, 20, 21, 23, 25, 26
_JSON, _FLOAT4, _FLOAT8, _BPCHAR, _VARCHAR = 114, 700, 701, 1042, 1043
_DATE, _TIME, _TIMESTAMP, _TIMESTAMPTZ, _INTERVAL = 1082, 1083, 1114, 1184, 1186
_NUMERIC, _JSONB = 1700, 3802

_SCALAR: dict[int, pa.DataType] = {
    _BOOL: pa.bool_(),
    _BYTEA: pa.binary(),
    _CHAR: pa.string(),
    _NAME: pa.string(),
    _INT8: pa.int64(),
    _INT2: pa.int16(),
    _INT4: pa.int32(),
    _TEXT: pa.string(),
    _OID: pa.int64(),
    _FLOAT4: pa.float32(),
    _FLOAT8: pa.float64(),
    _BPCHAR: pa.string(),
    _VARCHAR: pa.string(),
    _DATE: pa.date32(),
    _TIME: pa.time64("us"),
    _TIMESTAMP: pa.timestamp("us"),
    _TIMESTAMPTZ: pa.timestamp("us", tz="UTC"),
    _INTERVAL: pa.duration("us"),
}

# OID do array → OID do elemento
_ARRAYS: dict[int, int] = {
    1000: _BOOL,
    1005: _INT2,
    1007: _INT4,
    1016: _INT8,
    1021: _FLOAT4,
    1022: _FLOAT8,
    1009: _TEXT,
    1015: _VARCHAR,
    1014: _BPCHAR,
    1182: _DATE,
    1115: _TIMESTAMP,
    1185: _TIMESTAMPTZ,
}

_MAX_DECIMAL_PRECISION = 38
_CONVERSION_ERRORS = (pa.ArrowException, TypeError, ValueError, OverflowError)


@dataclass(frozen=True)
class ColumnSpec:
    """O que interessa de uma coluna do ``cursor.description``."""

    name: str
    type_code: int
    type_display: str
    precision: int | None = None
    scale: int | None = None


def _decimal_scale(values: Sequence[Any]) -> int | None:
    """Maior escala entre os valores; ``None`` se houver NaN/Infinity."""
    scale = 0
    for value in values:
        if value is None:
            continue
        if not isinstance(value, Decimal) or not value.is_finite():
            return None
        exponent = value.as_tuple().exponent
        if isinstance(exponent, int) and exponent < 0:
            scale = max(scale, -exponent)
    return scale


def arrow_type(spec: ColumnSpec, values: Sequence[Any]) -> pa.DataType | None:
    """Tipo Arrow para a coluna, ou ``None`` quando ela deve virar texto."""
    if spec.type_code in _SCALAR:
        return _SCALAR[spec.type_code]
    if spec.type_code in _ARRAYS:
        return pa.list_(_SCALAR[_ARRAYS[spec.type_code]])
    if spec.type_code == _NUMERIC:
        if spec.precision and 0 < spec.precision <= _MAX_DECIMAL_PRECISION:
            return pa.decimal128(spec.precision, spec.scale or 0)
        # numeric sem precisão declarada (ex.: sum(bigint), avg(...)): escala
        # pelos próprios valores
        scale = _decimal_scale(values)
        if scale is None or scale > _MAX_DECIMAL_PRECISION:
            return None
        return pa.decimal128(_MAX_DECIMAL_PRECISION, scale)
    return None


def _as_text(value: Any) -> str | None:
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, bytes | bytearray | memoryview):
        return bytes(value).hex()
    if isinstance(value, dict | list | tuple):
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value)


def to_arrow_array(spec: ColumnSpec, values: Sequence[Any]) -> pa.Array:
    """Converte os valores de uma coluna; em último caso, para texto."""
    target = None if spec.type_code in (_JSON, _JSONB) else arrow_type(spec, values)
    if target is not None:
        try:
            return pa.array(values, type=target)
        except _CONVERSION_ERRORS:
            pass
    return pa.array([_as_text(v) for v in values], type=pa.string())


def rows_to_table(specs: Sequence[ColumnSpec], rows: Sequence[Sequence[Any]]) -> pa.Table:
    """Monta a tabela Arrow (com schema mesmo quando não há linhas)."""
    arrays = [to_arrow_array(spec, [row[i] for row in rows]) for i, spec in enumerate(specs)]
    return pa.Table.from_arrays(arrays, names=[spec.name for spec in specs])
