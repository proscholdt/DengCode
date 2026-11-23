"""OrchestratorPort para o Airflow (API REST estável, Airflow 2 e 3).

Identificadores (ver ``mapping``): run = ``"<dag_id>/<dag_run_id>"``;
task mapeada = ``"<task_id>[<map_index>]"``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any, ClassVar
from urllib.parse import quote

import httpx2

from dengcode_adapter_airflow.client import AirflowApi, ApiFlavor, Credentials
from dengcode_adapter_airflow.mapping import (
    render_log_content,
    split_run_id,
    split_task_label,
    tail,
    to_pipeline,
    to_run,
)
from dengcode_core.errors import ConfigError, NotFoundError
from dengcode_core.llm.retry import RetryPolicy
from dengcode_core.ports.models import AdapterContext, AdapterInfo, PortKind
from dengcode_core.ports.orchestrator import OrchestratorCapabilities, PipelineInfo, RunInfo

PAGE_SIZE = 100
"""Itens por página (o teto padrão da API do Airflow é 100)."""
_DETAIL_CONCURRENCY = 4


def _seg(value: str) -> str:
    """Escapa um segmento de caminho (run ids têm ``:`` e ``+``)."""
    return quote(value, safe="")


class AirflowOrchestrator:
    """DAGs, runs, task instances, logs e disparo via API REST do Airflow."""

    info: ClassVar[AdapterInfo] = AdapterInfo(
        name="airflow",
        port=PortKind.ORCHESTRATOR,
        version="0.1.0",
        knowledge_pack="dengcode_adapter_airflow:knowledge/airflow.md",
    )
    name = "airflow"

    def __init__(self, api: AirflowApi) -> None:
        self._api = api

    @classmethod
    def from_config(cls, cfg: dict[str, Any], ctx: AdapterContext) -> AirflowOrchestrator:
        url = cfg.get("url")
        if not url:
            raise ConfigError("airflow: 'url' é obrigatória (ex.: http://localhost:8081)")
        username = _opt_str(cfg.get("username"))
        password = _opt_str(cfg.get("password"))
        if (username is None) != (password is None):
            raise ConfigError("airflow: informe username e password juntos")
        client = httpx2.AsyncClient(
            base_url=str(url).rstrip("/"),
            timeout=float(cfg.get("timeout_s", 30.0)),
            verify=bool(cfg.get("verify_tls", True)),
        )
        api = AirflowApi(
            client,
            credentials=Credentials(username=username, password=password),
            variant=str(cfg.get("variant") or "local"),
            api_version=_api_version(cfg.get("api_version")),
            retry=RetryPolicy(max_attempts=int(cfg.get("max_attempts", 3))),
        )
        ctx.logger.debug(
            "orquestrador airflow configurado",
            extra={"url": api.base_url, "variant": cfg.get("variant") or "local"},
        )
        return cls(api)

    def __repr__(self) -> str:
        return f"AirflowOrchestrator({self._api!r})"

    async def aclose(self) -> None:
        await self._api.aclose()

    def capabilities(self) -> OrchestratorCapabilities:
        return OrchestratorCapabilities(
            supports_trigger=True, supports_code_definitions=True, supports_backfill=False
        )

    # ------------------------------------------------------------- leitura
    async def list_pipelines(self) -> list[PipelineInfo]:
        flavor = await self._api.flavor()
        dags = await self._paginate(f"{flavor.prefix}/dags", "dags", what="lista de DAGs")
        return [to_pipeline(d) for d in dags]

    async def list_runs(self, pipeline_id: str, *, limit: int = 20) -> list[RunInfo]:
        """Runs mais recentes primeiro; task instances só dos runs com falha."""
        flavor = await self._api.flavor()
        path = f"{flavor.prefix}/dags/{_seg(pipeline_id)}/dagRuns"
        runs = await self._paginate(
            path,
            "dag_runs",
            params={"order_by": flavor.run_order_by},
            max_items=max(1, limit),
            what=f"DAG '{pipeline_id}'",
        )
        if not runs:
            await self._get_dag(flavor, pipeline_id)  # 404 vira NotFoundError
            return []
        runs = runs[: max(1, limit)]
        sem = asyncio.Semaphore(_DETAIL_CONCURRENCY)

        async def summarize(run: dict[str, Any]) -> RunInfo:
            if str(run.get("state")).lower() != "failed":
                return to_run(run)
            async with sem:
                tis = await self._task_instances(flavor, pipeline_id, str(run["dag_run_id"]))
            return to_run(run, tis)

        return list(await asyncio.gather(*(summarize(r) for r in runs)))

    async def get_run(self, run_id: str) -> RunInfo:
        """Run ``"<dag_id>/<dag_run_id>"`` com todas as task instances."""
        dag_id, dag_run_id = split_run_id(run_id)
        flavor = await self._api.flavor()
        run, tis = await asyncio.gather(
            self._api.get_json(self._run_path(flavor, dag_id, dag_run_id), what=f"run '{run_id}'"),
            self._task_instances(flavor, dag_id, dag_run_id),
        )
        return to_run(run, tis)

    async def task_logs(self, run_id: str, task_id: str, *, tail_lines: int = 500) -> str:
        """Log da última tentativa da task, cortado nas últimas ``tail_lines`` linhas."""
        dag_id, dag_run_id = split_run_id(run_id)
        name, map_index = split_task_label(task_id)
        flavor = await self._api.flavor()
        ti_path = f"{self._run_path(flavor, dag_id, dag_run_id)}/taskInstances/{_seg(name)}"
        if map_index is not None:
            ti_path = f"{ti_path}/{map_index}"
        what = f"task '{task_id}' do run '{run_id}'"
        ti = await self._api.get_json(ti_path, what=what)
        try_number = ti.get("try_number")
        if not isinstance(try_number, int) or try_number < 1:
            return _not_executed(task_id, ti.get("state"))
        log_base = f"{self._run_path(flavor, dag_id, dag_run_id)}/taskInstances/{_seg(name)}/logs"
        params: dict[str, Any] = {"full_content": "true"}
        if map_index is not None:
            params["map_index"] = map_index
        try:
            text = await self._log_text(flavor, f"{log_base}/{try_number}", params, what)
        except NotFoundError:
            if try_number < 2:
                raise
            # Airflow 2 antigo devolve o *próximo* try_number em tasks encerradas
            text = await self._log_text(flavor, f"{log_base}/{try_number - 1}", params, what)
        return tail(text, tail_lines)

    # ------------------------------------------------------------- escrita
    async def trigger(self, pipeline_id: str, params: dict[str, Any] | None = None) -> RunInfo:
        """Cria um run com ``conf = params`` (escrita remota; nunca repetido)."""
        flavor = await self._api.flavor()
        body: dict[str, Any] = {"conf": dict(params or {})}
        if flavor.trigger_needs_logical_date:
            body["logical_date"] = None
        run = await self._api.post_json(
            f"{flavor.prefix}/dags/{_seg(pipeline_id)}/dagRuns",
            body,
            what=f"DAG '{pipeline_id}'",
        )
        return to_run(run)

    # ------------------------------------------------------------- util
    @staticmethod
    def _run_path(flavor: ApiFlavor, dag_id: str, dag_run_id: str) -> str:
        return f"{flavor.prefix}/dags/{_seg(dag_id)}/dagRuns/{_seg(dag_run_id)}"

    async def _get_dag(self, flavor: ApiFlavor, dag_id: str) -> dict[str, Any]:
        return await self._api.get_json(
            f"{flavor.prefix}/dags/{_seg(dag_id)}", what=f"DAG '{dag_id}'"
        )

    async def _task_instances(
        self, flavor: ApiFlavor, dag_id: str, dag_run_id: str
    ) -> list[Mapping[str, Any]]:
        path = f"{self._run_path(flavor, dag_id, dag_run_id)}/taskInstances"
        items = await self._paginate(path, "task_instances", what=f"run '{dag_id}/{dag_run_id}'")
        return list(items)

    async def _log_text(
        self, flavor: ApiFlavor, path: str, params: Mapping[str, Any], what: str
    ) -> str:
        if flavor.json_logs:
            body = await self._api.get_json(path, params=params, what=what)
            return render_log_content(body.get("content"))
        return await self._api.get_text(path, params=params, what=what)

    async def _paginate(
        self,
        path: str,
        key: str,
        *,
        what: str,
        params: Mapping[str, Any] | None = None,
        max_items: int | None = None,
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        while max_items is None or len(items) < max_items:
            page_size = PAGE_SIZE if max_items is None else min(PAGE_SIZE, max_items - len(items))
            query = {**(params or {}), "limit": page_size, "offset": len(items)}
            body = await self._api.get_json(path, params=query, what=what)
            page = body.get(key)
            if not isinstance(page, list) or not page:
                break
            items.extend(p for p in page if isinstance(p, dict))
            total = body.get("total_entries")
            if isinstance(total, int):
                if len(items) >= total:
                    break
            elif len(page) < page_size:  # sem total: página incompleta é a última
                break
        return items


def _not_executed(task_id: str, state: object) -> str:
    """Texto no lugar do log de uma task sem nenhuma tentativa."""
    if state == "upstream_failed":
        return (
            f"(a task '{task_id}' não executou porque uma task a montante falhou "
            "(upstream_failed); veja os logs da task que falhou primeiro)"
        )
    return f"(a task '{task_id}' ainda não executou nenhuma tentativa; estado: {state})"


def _api_version(value: object) -> int | None:
    if value is None or value == "" or str(value).lower() == "auto":
        return None
    try:
        return int(str(value))
    except ValueError as exc:
        raise ConfigError(
            f"airflow: api_version deve ser 2, 3 ou auto (recebido {value!r})"
        ) from exc


def _opt_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None
