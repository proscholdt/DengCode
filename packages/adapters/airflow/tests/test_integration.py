"""Contrato e comportamento contra um Airflow 3 real (``docker/oss/airflow.yaml``).

Sobe o compose num projeto próprio e numa porta livre, espera as DAGs de
exemplo, dispara ``carga_pedidos`` (que falha de propósito) e roda o contrato.
Sempre derruba tudo com ``down -v`` no fim. Sem Docker, os testes são pulados.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import subprocess
import time
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from dengcode_adapter_airflow.orchestrator import AirflowOrchestrator
from dengcode_core.errors import AuthError, DengCodeError
from dengcode_core.ports.models import AdapterContext
from dengcode_testkit.contracts.orchestrator import OrchestratorContract

pytestmark = pytest.mark.integration

COMPOSE_FILE = Path(__file__).resolve().parents[4] / "docker" / "oss" / "airflow.yaml"
CTX = AdapterContext(workspace="teste")
READY_TIMEOUT_S = 240
RUN_TIMEOUT_S = 300


@dataclass(frozen=True)
class AirflowStack:
    config: dict[str, Any]
    triggered_run_id: str


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    probe = subprocess.run(["docker", "info"], capture_output=True, check=False, timeout=30)
    return probe.returncode == 0


def _compose(project: str, port: int, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "DENGCODE_AIRFLOW_PORT": str(port)}
    return subprocess.run(
        ["docker", "compose", "-p", project, "-f", str(COMPOSE_FILE), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        env=env,
        timeout=600,
    )


async def _prepare(config: dict[str, Any]) -> str:
    """Espera as DAGs, dispara ``carga_pedidos`` e espera a falha."""
    orch = AirflowOrchestrator.from_config(config, CTX)
    try:
        deadline = time.monotonic() + READY_TIMEOUT_S
        while True:
            try:
                ids = {p.id for p in await orch.list_pipelines()}
                if {"carga_pedidos", "relatorio_diario"} <= ids:
                    break
            except DengCodeError:
                pass
            if time.monotonic() > deadline:
                raise AssertionError("as DAGs de exemplo não apareceram no Airflow")
            await asyncio.sleep(3)
        run = await orch.trigger("carga_pedidos", {"origem": "teste-integracao"})
        deadline = time.monotonic() + RUN_TIMEOUT_S
        while (await orch.get_run(run.run_id)).status not in ("failed", "success"):
            if time.monotonic() > deadline:
                raise AssertionError(f"o run {run.run_id} não terminou a tempo")
            await asyncio.sleep(3)
        return run.run_id
    finally:
        await orch.aclose()


@pytest.fixture(scope="session")
def airflow_stack() -> Iterator[AirflowStack]:
    if not _docker_available():
        pytest.skip("Docker indisponível para testes de integração")
    port = _free_port()
    project = f"dengcode-airflow-it-{os.getpid()}"
    try:
        up = _compose(project, port, "up", "-d", "--wait", "--wait-timeout", str(READY_TIMEOUT_S))
        if up.returncode != 0:
            logs = _compose(project, port, "logs", "--tail", "80").stdout
            pytest.fail(f"falha ao subir o Airflow:\n{up.stderr}\n{logs}")
        config = {
            "url": f"http://localhost:{port}",
            "username": "admin",
            "password": "admin",
            "timeout_s": 30,
        }
        yield AirflowStack(config=config, triggered_run_id=asyncio.run(_prepare(config)))
    finally:
        _compose(project, port, "down", "-v", "--remove-orphans")


@pytest.fixture
async def orchestrator(airflow_stack: AirflowStack) -> AsyncIterator[AirflowOrchestrator]:
    orch = AirflowOrchestrator.from_config(airflow_stack.config, CTX)
    yield orch
    await orch.aclose()


class TestAirflowContract(OrchestratorContract):
    @pytest.fixture
    def failing_pipeline_id(self) -> str:
        return "carga_pedidos"


async def test_triggered_run_failed_in_validation(
    orchestrator: AirflowOrchestrator, airflow_stack: AirflowStack
) -> None:
    run = await orchestrator.get_run(airflow_stack.triggered_run_id)
    assert run.status == "failed"
    assert run.failed_tasks == ["validar_pedidos", "carregar_pedidos"]
    statuses = {t.task_id: t.status for t in run.tasks}
    assert statuses["extrair_pedidos"] == "success"
    logs = await orchestrator.task_logs(run.run_id, "validar_pedidos")
    assert "coluna id_cliente com 12 nulos" in logs
    assert "Traceback" in logs
    assert "upstream_failed" in await orchestrator.task_logs(run.run_id, "carregar_pedidos")


async def test_pipelines_have_schedule_and_file(orchestrator: AirflowOrchestrator) -> None:
    by_id = {p.id: p for p in await orchestrator.list_pipelines()}
    assert by_id["relatorio_diario"].schedule == "0 6 * * *"
    assert by_id["carga_pedidos"].definition_path == "carga_pedidos.py"
    assert by_id["carga_pedidos"].paused is False


async def test_wrong_password_is_auth_error(airflow_stack: AirflowStack) -> None:
    orch = AirflowOrchestrator.from_config({**airflow_stack.config, "password": "errada"}, CTX)
    try:
        with pytest.raises(AuthError):
            await orch.list_pipelines()
    finally:
        await orch.aclose()
