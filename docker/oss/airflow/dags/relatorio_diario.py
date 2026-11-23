"""DAG de exemplo que termina com SUCESSO (stack local do DengCode).

Fluxo: calcular_vendas -> publicar_relatorio
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from airflow.sdk import dag, task

log = logging.getLogger(__name__)


@dag(
    dag_id="relatorio_diario",
    schedule="0 6 * * *",
    start_date=datetime(2026, 1, 1, tzinfo=UTC),
    catchup=False,
    tags=["exemplo", "dengcode"],
    default_args={"owner": "dados", "retries": 1},
    doc_md=__doc__,
)
def relatorio_diario() -> None:
    @task
    def calcular_vendas() -> dict[str, float]:
        vendas = {"sul": 1250.0, "sudeste": 4310.5, "nordeste": 980.25}
        log.info("vendas por região calculadas: %s", vendas)
        return vendas

    @task
    def publicar_relatorio(vendas: dict[str, float]) -> None:
        log.info("relatório publicado: total de R$ %.2f", sum(vendas.values()))

    publicar_relatorio(calcular_vendas())


relatorio_diario()
