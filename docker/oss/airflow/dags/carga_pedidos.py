"""DAG de exemplo que FALHA de propósito (stack local do DengCode).

Simula uma carga de pedidos em que a validação de qualidade encontra nulos
na coluna ``id_cliente``. Serve para o agente investigar a falha lendo runs
e logs ("por que a DAG carga_pedidos falhou?").

Fluxo: extrair_pedidos -> validar_pedidos (falha) -> carregar_pedidos (upstream_failed)
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from airflow.sdk import dag, task

log = logging.getLogger(__name__)

TOTAL_LINHAS = 40


@dag(
    dag_id="carga_pedidos",
    schedule="@daily",
    start_date=datetime(2026, 1, 1, tzinfo=UTC),
    catchup=False,
    tags=["exemplo", "dengcode"],
    default_args={"owner": "dados", "retries": 0},
    doc_md=__doc__,
)
def carga_pedidos() -> None:
    @task
    def extrair_pedidos() -> list[dict[str, object]]:
        pedidos: list[dict[str, object]] = []
        for i in range(1, TOTAL_LINHAS + 1):
            id_cliente = None if i % 10 in (1, 4, 7) else 1000 + i
            pedidos.append({"id_pedido": i, "id_cliente": id_cliente, "valor": 25.0 * i})
        log.info("extraídos %d pedidos da origem vendas.pedidos_raw", len(pedidos))
        return pedidos

    @task
    def validar_pedidos(pedidos: list[dict[str, object]]) -> list[dict[str, object]]:
        nulos = sum(1 for p in pedidos if p["id_cliente"] is None)
        log.info("validando %d linhas: regra id_cliente NOT NULL", len(pedidos))
        if nulos:
            raise ValueError(
                f"falha de qualidade: coluna id_cliente com {nulos} nulos em "
                f"{len(pedidos)} linhas (esperado 0); verifique o join com vendas.clientes"
            )
        return pedidos

    @task
    def carregar_pedidos(pedidos: list[dict[str, object]]) -> None:
        log.info("carregando %d pedidos em analytics.fato_pedidos", len(pedidos))

    carregar_pedidos(validar_pedidos(extrair_pedidos()))


carga_pedidos()
