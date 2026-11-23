"""Decodificação das mensagens amostradas (chave, valor, timestamp) para tipos JSON.

Valor: JSON quando possível, senão texto UTF-8; binário vira ``"base64:<...>"``.
Mensagens no formato do Schema Registry (byte mágico 0 + id de 4 bytes) com
corpo JSON têm o cabeçalho removido; Avro/Protobuf binário cai no marcador base64.
"""

from __future__ import annotations

import base64
import json
import re
from datetime import UTC, datetime
from typing import Any

MAX_VALUE_BYTES = 64 * 1024
"""Teto de bytes decodificados por chave/valor; o excedente é cortado."""

BASE64_PREFIX = "base64:"
_WIRE_HEADER = 5  # byte mágico 0 + id do schema (4 bytes, big-endian)
# caracteres de controle (exceto tab e quebras de linha): UTF-8 válido, mas é binário
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _base64(raw: bytes) -> str:
    cut = raw[:MAX_VALUE_BYTES]
    suffix = "" if len(cut) == len(raw) else f"…[truncado: {len(raw)} bytes]"
    return BASE64_PREFIX + base64.b64encode(cut).decode("ascii") + suffix


def _text(raw: bytes) -> str | None:
    """Texto UTF-8 (cortado no teto) ou ``None`` se for binário."""
    if len(raw) <= MAX_VALUE_BYTES:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None
        return None if _CONTROL.search(text) else text
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        # corte no meio de um caractere multibyte não conta como binário
        if exc.start < MAX_VALUE_BYTES:
            return None
    head = raw[:MAX_VALUE_BYTES].decode("utf-8", errors="ignore")
    if _CONTROL.search(head):
        return None
    return head + f"…[truncado: {len(raw)} bytes]"


def _json(text: str) -> tuple[bool, Any]:
    stripped = text.lstrip()
    if not stripped or stripped[0] not in '{["-0123456789tfn':
        return False, None
    try:
        return True, json.loads(text)
    except ValueError:
        return False, None


def decode_key(raw: bytes | None) -> str | None:
    """Chave como texto (sem interpretar JSON: ``"123"`` continua texto)."""
    if raw is None:
        return None
    text = _text(raw)
    return text if text is not None else _base64(raw)


def decode_value(raw: bytes | None) -> Any:
    """Valor como JSON, texto ou marcador base64 (nesta ordem)."""
    if raw is None:
        return None
    if len(raw) > _WIRE_HEADER and raw[0] == 0 and len(raw) <= MAX_VALUE_BYTES:
        body = _text(raw[_WIRE_HEADER:])
        if body is not None:
            ok, parsed = _json(body)
            if ok:
                return parsed
    text = _text(raw)
    if text is None:
        return _base64(raw)
    if len(raw) <= MAX_VALUE_BYTES:
        ok, parsed = _json(text)
        if ok:
            return parsed
    return text


def iso_timestamp(ms: int | None) -> str | None:
    """Timestamp do Kafka (ms desde a época) em ISO 8601 UTC; negativo = ausente."""
    if ms is None or ms < 0:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, tz=UTC).isoformat(timespec="milliseconds")
    except (OverflowError, OSError, ValueError):
        return None
