from __future__ import annotations

import sqlite3
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from dengcode_core.audit import AuditLog, AuditRecord
from dengcode_core.data.results import ResultStore, render_for_llm
from dengcode_core.privacy.pii import (
    PiiMasker,
    cnpj_valid,
    cpf_valid,
    is_pii_column,
    luhn_valid,
    mask_text,
)

CPF_OK = "529.982.247-25"
CNPJ_OK = "11.222.333/0001-81"
CARD_OK = "4111 1111 1111 1111"


def test_validators() -> None:
    assert cpf_valid(CPF_OK)
    assert cpf_valid("52998224725")
    assert not cpf_valid("111.111.111-11")
    assert not cpf_valid("529.982.247-26")
    assert cnpj_valid(CNPJ_OK)
    assert not cnpj_valid("11.222.333/0001-82")
    assert luhn_valid(CARD_OK)
    assert not luhn_valid("4111 1111 1111 1112")


@pytest.mark.parametrize(
    ("raw", "masked"),
    [
        ("contato: maria.silva@empresa.com.br", "contato: m***@e***.br"),
        (f"cpf {CPF_OK}", "cpf ***.***.***-25"),
        (f"cnpj {CNPJ_OK}", "cnpj **.***.***/****-81"),
        ("tel (11) 98765-4321", "tel (**) *****-4321"),
        ("tel +55 11 98765-4321", "tel +** ** *****-4321"),
        (f"cartão {CARD_OK}", "cartão **** **** **** 1111"),
    ],
)
def test_mask_preserves_format(raw: str, masked: str) -> None:
    assert mask_text(raw) == masked


@pytest.mark.parametrize(
    "text",
    [
        "pedido 1234567890",
        "valor 2179.60",
        "data 2026-09-24",
        "cpf inválido 529.982.247-26",
        "id 4111111111111112",
    ],
)
def test_does_not_mask_ids_amounts_dates_or_invalid_docs(text: str) -> None:
    assert mask_text(text) == text


def test_column_name_heuristics() -> None:
    assert is_pii_column("email_cliente")
    assert is_pii_column("CPF")
    assert is_pii_column("telefone_celular")
    assert is_pii_column("col", tags=["pii"])
    assert not is_pii_column("valor_total")


def test_mask_table_by_tag_name_and_detector() -> None:
    table = pa.table(
        {
            "id": [1, 2],
            "contato": ["ana@x.com", "nenhum"],
            "segredo": ["abc", None],
            "cpf": [CPF_OK, "000"],
        }
    )
    out = PiiMasker().table(table, pii_columns=["segredo"])
    assert out.column("id").to_pylist() == [1, 2]
    assert out.column("contato").to_pylist() == ["a***@x***.com", "nenhum"]
    assert out.column("segredo").to_pylist() == ["***", None]
    assert out.column("cpf").to_pylist() == ["***.***.***-25", "***"]


def test_render_for_llm_masks_and_hides_pii_stats(tmp_path: Path) -> None:
    table = pa.table(
        {"id": [1, 2, 3], "email": ["a@x.com", "b@y.com", None], "valor": [10.0, 20.0, 30.0]}
    )
    path = ResultStore(tmp_path).save(table, "r1")
    assert pq.read_table(path).num_rows == 3  # o Parquet guarda o resultado completo, local
    text = render_for_llm(table, total_rows=3, path=path, masker=PiiMasker())
    assert "a@x.com" not in text
    assert "a***@x***.com" in text
    assert "min 10.0 · max 30.0" in text
    assert "PII: valores omitidos" in text


def test_audit_is_append_only_and_masks_sql(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.db")
    log.append(
        AuditRecord(
            workspace="w",
            sql="SELECT * FROM c WHERE email = 'ana@x.com'",
            kind="read",
            decision="executed",
            rows=1,
        )
    )
    [rec] = log.list()
    assert "ana@x.com" not in rec.sql
    conn = sqlite3.connect(tmp_path / "audit.db")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("DELETE FROM audit")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("UPDATE audit SET decision = 'x'")
    conn.close()
