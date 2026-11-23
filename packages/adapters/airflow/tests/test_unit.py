"""Testes do adapter Airflow com ``httpx2.MockTransport`` (sem rede).

As respostas de ``fixtures/airflow3.json`` foram gravadas de um Airflow 3 real
(``docker/oss/airflow.yaml``); as de ``fixtures/airflow2.json`` seguem a API
``/api/v1`` do Airflow 2.10.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from importlib.metadata import entry_points
from importlib.resources import files
from pathlib import Path
from typing import Any

import httpx2
import pytest

from dengcode_adapter_airflow.client import AirflowApi, Credentials
from dengcode_adapter_airflow.mapping import (
    map_status,
    render_log_content,
    split_task_label,
    tail,
    to_pipeline,
    to_run,
)
from dengcode_adapter_airflow.orchestrator import AirflowOrchestrator
from dengcode_core.errors import (
    AdapterError,
    AuthError,
    ConfigError,
    NotFoundError,
    RateLimitError,
    TransientError,
)
from dengcode_core.llm.retry import RetryPolicy
from dengcode_core.ports.models import AdapterContext, PortKind
from dengcode_testkit.contracts.orchestrator import OrchestratorContract

FIXTURES = Path(__file__).parent / "fixtures"
TOKEN = "jwt-gravado-de-teste"
BASIC = "Basic " + base64.b64encode(b"admin:admin").decode("ascii")
CTX = AdapterContext(workspace="teste")
V3_FAILED_RUN = "carga_pedidos/manual__2026-11-23T23:18:11.355919+00:00"


def load(name: str) -> dict[str, Any]:
    raw: dict[str, Any] = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return {k: v for k, v in raw.items() if not k.startswith("_")}


Handler = Callable[[httpx2.Request], httpx2.Response | None]


class FakeAirflow:
    """Servidor gravado: responde por ``"MÉTODO caminho"`` e confere a autenticação."""

    def __init__(self, routes: dict[str, Any], *, major: int) -> None:
        self.routes = dict(routes)
        self.major = major
        self.requests: list[httpx2.Request] = []
        self.handlers: list[Handler] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        for handler in self.handlers:
            resp = handler(request)
            if resp is not None:
                return resp
        path = request.url.path
        if not path.endswith("/version") and path != "/auth/token":
            expected = f"Bearer {TOKEN}" if self.major == 3 else BASIC
            if request.headers.get("authorization") != expected:
                return httpx2.Response(401, json={"detail": "Not authenticated"})
        route = self.routes.get(f"{request.method} {path}")
        if route is None:
            return httpx2.Response(404, json={"detail": f"{path} not found"})
        if "text" in route:
            return httpx2.Response(route["status"], text=route["text"])
        return httpx2.Response(route["status"], json=_page(route["json"], request))

    def paths(self, method: str = "GET") -> list[str]:
        return [r.url.path for r in self.requests if r.method == method]


def _page(body: Any, request: httpx2.Request) -> Any:
    """Aplica ``limit``/``offset`` às coleções gravadas, como o servidor real."""
    if not isinstance(body, dict) or "total_entries" not in body:
        return body
    offset = int(request.url.params.get("offset", "0"))
    limit = int(request.url.params.get("limit", "100"))
    return {k: v[offset : offset + limit] if isinstance(v, list) else v for k, v in body.items()}


async def _no_sleep(_: float) -> None:
    return None


def make(
    fake: FakeAirflow | Callable[[httpx2.Request], httpx2.Response],
    *,
    api_version: int | None = None,
    variant: str = "local",
    username: str | None = "admin",
    password: str | None = "admin",
) -> AirflowOrchestrator:
    client = httpx2.AsyncClient(
        base_url="http://airflow.test", transport=httpx2.MockTransport(fake)
    )
    api = AirflowApi(
        client,
        credentials=Credentials(username=username, password=password),
        variant=variant,
        api_version=api_version,
        retry=RetryPolicy(max_attempts=3, sleep=_no_sleep),
    )
    return AirflowOrchestrator(api)


@pytest.fixture
def fake3() -> FakeAirflow:
    return FakeAirflow(load("airflow3.json"), major=3)


@pytest.fixture
def fake2() -> FakeAirflow:
    return FakeAirflow(load("airflow2.json"), major=2)


# ------------------------------------------------------------ contratos
class TestAirflow3RecordedContract(OrchestratorContract):
    @pytest.fixture
    def orchestrator(self, fake3: FakeAirflow) -> AirflowOrchestrator:
        return make(fake3)

    @pytest.fixture
    def failing_pipeline_id(self) -> str:
        return "carga_pedidos"


class TestAirflow2RecordedContract(OrchestratorContract):
    @pytest.fixture
    def orchestrator(self, fake2: FakeAirflow) -> AirflowOrchestrator:
        return make(fake2)

    @pytest.fixture
    def failing_pipeline_id(self) -> str:
        return "carga_pedidos"


# ------------------------------------------------------------ metadados
def test_info_and_entry_point() -> None:
    info = AirflowOrchestrator.info
    assert (info.name, info.port) == ("airflow", PortKind.ORCHESTRATOR)
    assert info.knowledge_pack == "dengcode_adapter_airflow:knowledge/airflow.md"
    module, _, rel = info.knowledge_pack.partition(":")
    assert "DAG" in files(module).joinpath(rel).read_text(encoding="utf-8")
    eps = {ep.name: ep.value for ep in entry_points(group="dengcode.adapters")}
    assert eps["airflow"] == "dengcode_adapter_airflow.orchestrator:AirflowOrchestrator"
    caps = make(FakeAirflow({}, major=3)).capabilities()
    assert (caps.supports_trigger, caps.supports_code_definitions, caps.supports_backfill) == (
        True,
        True,
        False,
    )


# ------------------------------------------------------------ versão e auth
async def test_detects_airflow3_and_uses_jwt_once(fake3: FakeAirflow) -> None:
    orch = make(fake3)
    await orch.list_pipelines()
    await orch.list_runs("relatorio_diario")
    assert fake3.paths()[0] == "/api/v2/version"
    assert fake3.paths("POST") == ["/auth/token"]
    token_req = next(r for r in fake3.requests if r.url.path == "/auth/token")
    assert json.loads(token_req.content) == {"username": "admin", "password": "admin"}
    api_reqs = [r for r in fake3.requests if r.url.path.startswith("/api/v2/dags")]
    assert all(r.headers["authorization"] == f"Bearer {TOKEN}" for r in api_reqs)


async def test_detects_airflow2_and_uses_basic_auth(fake2: FakeAirflow) -> None:
    orch = make(fake2)
    pipelines = await orch.list_pipelines()
    assert fake2.paths()[:2] == ["/api/v2/version", "/api/v1/version"]
    assert fake2.paths("POST") == []
    assert {p.id for p in pipelines} == {"carga_pedidos", "relatorio_diario"}
    assert all(
        r.headers["authorization"] == BASIC
        for r in fake2.requests
        if r.url.path.startswith("/api/v1/dags")
    )


async def test_configured_version_skips_probe(fake2: FakeAirflow) -> None:
    await make(fake2, api_version=2).list_pipelines()
    assert not any(p.endswith("/version") for p in fake2.paths())


async def test_not_airflow_is_adapter_error() -> None:
    orch = make(FakeAirflow({}, major=3))
    with pytest.raises(AdapterError, match="api/v2/version"):
        await orch.list_pipelines()


async def test_version_probe_ignores_html_pages(fake2: FakeAirflow) -> None:
    fake2.routes["GET /api/v2/version"] = {"status": 200, "text": "<!doctype html><html></html>"}
    assert len(await make(fake2).list_pipelines()) == 2
    assert fake2.paths()[:2] == ["/api/v2/version", "/api/v1/version"]


async def test_version_probe_accepts_protected_endpoint(fake3: FakeAirflow) -> None:
    fake3.handlers.append(
        lambda r: (
            httpx2.Response(401, json={"detail": "x"}) if r.url.path == "/api/v2/version" else None
        )
    )
    assert len(await make(fake3).list_pipelines()) == 2


async def test_expired_token_is_renewed_once(fake3: FakeAirflow) -> None:
    state = {"expired": True}

    def expire_first(request: httpx2.Request) -> httpx2.Response | None:
        if request.url.path == "/api/v2/dags" and state["expired"]:
            state["expired"] = False
            return httpx2.Response(401, json={"detail": "Token expired"})
        return None

    fake3.handlers.append(expire_first)
    assert len(await make(fake3).list_pipelines()) == 2
    assert fake3.paths("POST") == ["/auth/token", "/auth/token"]


async def test_rejected_password_is_auth_error(fake3: FakeAirflow) -> None:
    fake3.routes["POST /auth/token"] = {"status": 401, "json": {"detail": "Invalid credentials"}}
    with pytest.raises(AuthError, match="usuário ou senha"):
        await make(fake3, password="errada").list_pipelines()


async def test_basic_variant_on_airflow3_skips_token(fake3: FakeAirflow) -> None:
    seen: list[str] = []

    def accept_basic(request: httpx2.Request) -> httpx2.Response | None:
        seen.append(request.headers.get("authorization", ""))
        if request.url.path == "/api/v2/dags":
            return httpx2.Response(200, json={"dags": [], "total_entries": 0})
        return None

    fake3.handlers.append(accept_basic)
    assert await make(fake3, variant="basic").list_pipelines() == []
    assert fake3.paths("POST") == []
    assert seen[-1] == BASIC


async def test_without_credentials_sends_no_auth() -> None:
    def server(request: httpx2.Request) -> httpx2.Response:
        assert "authorization" not in request.headers
        if request.url.path == "/api/v2/dags":
            return httpx2.Response(200, json={"dags": [], "total_entries": 0})
        return httpx2.Response(200, json={"version": "3.3.2"})

    assert await make(server, username=None, password=None).list_pipelines() == []


# ------------------------------------------------------------ erros
@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, AuthError),
        (403, AuthError),
        (404, NotFoundError),
        (429, RateLimitError),
        (500, TransientError),
        (503, TransientError),
        (400, AdapterError),
        (409, AdapterError),
    ],
)
async def test_status_mapping(status: int, error: type[Exception]) -> None:
    def server(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/auth/token":
            return httpx2.Response(201, json={"access_token": TOKEN})
        return httpx2.Response(
            status, json={"detail": "algo deu errado"}, headers={"Retry-After": "7"}
        )

    with pytest.raises(error) as info:
        await make(server, api_version=3).list_pipelines()
    assert type(info.value) is error
    assert "algo deu errado" in str(info.value)
    if isinstance(info.value, TransientError):
        assert info.value.retry_after_s == 7


@pytest.mark.parametrize(
    "exc", [httpx2.ConnectError("recusada"), httpx2.ReadTimeout("lento"), httpx2.ReadError("x")]
)
async def test_network_failures_are_transient(exc: Exception) -> None:
    def server(request: httpx2.Request) -> httpx2.Response:
        raise exc

    with pytest.raises(TransientError):
        await make(server, api_version=2).list_pipelines()


async def test_gets_are_retried_on_transient_errors(fake3: FakeAirflow) -> None:
    calls = {"n": 0}

    def flaky(request: httpx2.Request) -> httpx2.Response | None:
        if request.url.path == "/api/v2/dags":
            calls["n"] += 1
            if calls["n"] < 3:
                return httpx2.Response(503, json={"detail": "reiniciando"})
        return None

    fake3.handlers.append(flaky)
    assert len(await make(fake3).list_pipelines()) == 2
    assert calls["n"] == 3


async def test_trigger_is_never_retried(fake3: FakeAirflow) -> None:
    fake3.routes["POST /api/v2/dags/carga_pedidos/dagRuns"] = {
        "status": 503,
        "json": {"detail": "indisponível"},
    }
    with pytest.raises(TransientError):
        await make(fake3).trigger("carga_pedidos")
    assert fake3.paths("POST").count("/api/v2/dags/carga_pedidos/dagRuns") == 1


async def test_secret_is_not_in_repr_or_errors(fake3: FakeAirflow) -> None:
    fake3.routes["POST /auth/token"] = {"status": 401, "json": {"detail": "bad"}}
    orch = make(fake3, password="s3gr3d0-xyz")
    assert "s3gr3d0-xyz" not in repr(orch)
    with pytest.raises(AuthError) as info:
        await orch.list_pipelines()
    assert "s3gr3d0-xyz" not in str(info.value)


# ------------------------------------------------------------ DAGs e runs
async def test_pipelines_v3_mapping(fake3: FakeAirflow) -> None:
    by_id = {p.id: p for p in await make(fake3).list_pipelines()}
    carga = by_id["carga_pedidos"]
    assert carga.schedule == "0 0 * * *"
    assert carga.paused is False
    assert carga.definition_path == "carga_pedidos.py"


async def test_pipelines_v2_mapping(fake2: FakeAirflow) -> None:
    by_id = {p.id: p for p in await make(fake2).list_pipelines()}
    assert by_id["carga_pedidos"].schedule == "@daily"
    assert by_id["carga_pedidos"].definition_path == "/opt/airflow/dags/carga_pedidos.py"
    assert by_id["relatorio_diario"].schedule == "0 6 * * *"


def test_schedule_variants() -> None:
    def sched(**dag: Any) -> str | None:
        return to_pipeline({"dag_id": "d", **dag}).schedule

    assert sched(schedule_interval={"__type": "TimeDelta", "days": 1, "seconds": 0}) == (
        "a cada 1 day, 0:00:00"
    )
    assert sched(schedule_interval=None) is None
    assert sched(timetable_summary=None, timetable_description="Never, external triggers only") == (
        "Never, external triggers only"
    )
    assert sched(timetable_summary="@hourly", schedule_interval={"__type": "CronExpression"}) == (
        "@hourly"
    )
    assert to_pipeline({"dag_id": "d", "fileloc": "/opt/dags/d.py"}).definition_path == (
        "/opt/dags/d.py"
    )


async def test_pagination_follows_total_entries() -> None:
    def server(request: httpx2.Request) -> httpx2.Response:
        offset = int(request.url.params.get("offset", "0"))
        limit = int(request.url.params["limit"])
        # servidor com teto de página menor (50) do que o pedido (100)
        page = [
            {"dag_id": f"dag_{i:03d}"} for i in range(offset, min(offset + min(limit, 50), 120))
        ]
        return httpx2.Response(200, json={"dags": page, "total_entries": 120})

    pipelines = await make(server, api_version=2).list_pipelines()
    assert [p.id for p in pipelines] == [f"dag_{i:03d}" for i in range(120)]


async def test_list_runs_orders_newest_first_and_details_only_failures(fake3: FakeAirflow) -> None:
    runs = await make(fake3).list_runs("carga_pedidos", limit=5)
    req = next(r for r in fake3.requests if r.url.path.endswith("/carga_pedidos/dagRuns"))
    assert req.url.params["order_by"] == "-run_after"
    assert req.url.params["limit"] == "5"
    assert runs[0].run_id == V3_FAILED_RUN
    assert runs[0].failed_tasks == ["validar_pedidos", "carregar_pedidos"]
    ok = await make(fake3).list_runs("relatorio_diario")
    assert {r.status for r in ok} == {"success"}
    assert all(r.tasks == [] for r in ok)


async def test_list_runs_v2_order_and_unknown_dag(fake2: FakeAirflow) -> None:
    orch = make(fake2)
    await orch.list_runs("carga_pedidos")
    req = next(r for r in fake2.requests if r.url.path.endswith("/carga_pedidos/dagRuns"))
    assert req.url.params["order_by"] == "-execution_date"
    # Airflow 2 devolve lista vazia para DAG inexistente: o adapter confere a DAG
    fake2.routes["GET /api/v1/dags/fantasma/dagRuns"] = {
        "status": 200,
        "json": {"dag_runs": [], "total_entries": 0},
    }
    with pytest.raises(NotFoundError):
        await orch.list_runs("fantasma")


async def test_get_run_encodes_ids_and_maps_tasks(fake3: FakeAirflow) -> None:
    run = await make(fake3).get_run(V3_FAILED_RUN)
    assert run.pipeline_id == "carga_pedidos"
    assert run.status == "failed"
    assert {t.task_id: t.status for t in run.tasks} == {
        "extrair_pedidos": "success",
        "validar_pedidos": "failed",
        "carregar_pedidos": "failed",
    }
    raw = next(r for r in fake3.requests if r.url.path.endswith("+00:00"))
    assert b"%2B00%3A00" in raw.url.raw_path


async def test_get_run_requires_composite_id(fake3: FakeAirflow) -> None:
    with pytest.raises(AdapterError, match="<dag_id>/<dag_run_id>"):
        await make(fake3).get_run("manual__2026-11-23")


async def test_get_run_unknown_is_not_found(fake3: FakeAirflow) -> None:
    with pytest.raises(NotFoundError):
        await make(fake3).get_run("carga_pedidos/run_que_nao_existe")


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("success", "success"),
        ("failed", "failed"),
        ("upstream_failed", "failed"),
        ("running", "running"),
        ("up_for_retry", "running"),
        ("up_for_reschedule", "running"),
        ("deferred", "running"),
        ("restarting", "running"),
        ("queued", "queued"),
        ("scheduled", "queued"),
        ("skipped", "cancelled"),
        ("removed", "cancelled"),
        ("estado_novo", "unknown"),
    ],
)
def test_status_map(state: str, expected: str) -> None:
    assert map_status(state) == expected


def test_null_states() -> None:
    assert map_status(None) == "unknown"
    assert map_status(None, task=True) == "queued"


def test_to_run_with_retries_skips_and_mapped_tasks() -> None:
    run = to_run(
        {
            "dag_id": "d",
            "dag_run_id": "r1",
            "state": "running",
            "start_date": "2026-11-23T10:00:00Z",
        },
        [
            {"task_id": "a", "state": "success", "try_number": 1, "map_index": -1},
            {"task_id": "b", "state": "up_for_retry", "try_number": 1, "map_index": -1},
            {"task_id": "c", "state": "failed", "try_number": 2, "map_index": 0},
            {"task_id": "c", "state": "failed", "try_number": 1, "map_index": 1},
            {"task_id": "d", "state": "upstream_failed", "try_number": 0, "map_index": -1},
            {"task_id": "e", "state": "skipped", "try_number": 0},
            {"task_id": "f", "state": None, "try_number": 0},
        ],
    )
    assert run.run_id == "d/r1"
    assert run.status == "running"
    assert run.started_at is not None
    assert run.started_at.tzinfo is not None
    assert run.failed_tasks == ["c[0]", "c[1]", "d"]
    assert [t.status for t in run.tasks] == [
        "success",
        "running",
        "failed",
        "failed",
        "failed",
        "cancelled",
        "queued",
    ]
    assert split_task_label("c[1]") == ("c", 1)
    assert split_task_label("c") == ("c", None)


# ------------------------------------------------------------ logs
async def test_task_logs_v3_renders_events_with_traceback(fake3: FakeAirflow) -> None:
    logs = await make(fake3).task_logs(V3_FAILED_RUN, "validar_pedidos")
    assert "ValueError: falha de qualidade: coluna id_cliente com 12 nulos" in logs
    assert "Traceback (most recent call last):" in logs
    assert "ERROR - Task failed with exception" in logs
    req = next(r for r in fake3.requests if "/logs/" in r.url.path)
    assert req.url.path.endswith("/validar_pedidos/logs/1")
    assert req.url.params["full_content"] == "true"
    assert req.headers["accept"] == "application/json"


async def test_task_logs_tail(fake3: FakeAirflow) -> None:
    orch = make(fake3)
    full = await orch.task_logs(V3_FAILED_RUN, "validar_pedidos", tail_lines=0)
    last = await orch.task_logs(V3_FAILED_RUN, "validar_pedidos", tail_lines=4)
    assert last.splitlines() == full.splitlines()[-4:]
    assert tail("a\nb\nc", 2) == "b\nc"


async def test_task_logs_of_task_that_did_not_run(fake3: FakeAirflow) -> None:
    text = await make(fake3).task_logs(V3_FAILED_RUN, "carregar_pedidos")
    assert "upstream_failed" in text
    assert not any("/carregar_pedidos/logs" in p for p in fake3.paths())


async def test_task_logs_v2_plain_text(fake2: FakeAirflow) -> None:
    run = (await make(fake2).list_runs("carga_pedidos"))[0]
    logs = await make(fake2).task_logs(run.run_id, "validar_pedidos", tail_lines=50)
    assert "coluna id_cliente com 12 nulos" in logs
    req = next(r for r in fake2.requests if "/logs/" in r.url.path)
    assert req.headers["accept"] == "text/plain"


async def test_task_logs_v2_falls_back_to_previous_try(fake2: FakeAirflow) -> None:
    run = (await make(fake2).list_runs("carga_pedidos"))[0]
    _, dag_run_id = run.run_id.split("/", 1)
    base = f"GET /api/v1/dags/carga_pedidos/dagRuns/{dag_run_id}/taskInstances/validar_pedidos"
    ti = dict(fake2.routes[base]["json"])
    ti["try_number"] = 2  # Airflow 2 antigo: "próxima" tentativa numa task encerrada
    fake2.routes[base] = {"status": 200, "json": ti}
    logs = await make(fake2).task_logs(run.run_id, "validar_pedidos")
    assert "12 nulos" in logs
    log_paths = [p for p in fake2.paths() if "/logs/" in p]
    assert [p.rsplit("/", 1)[-1] for p in log_paths] == ["2", "1"]


async def test_task_logs_of_mapped_task(fake3: FakeAirflow) -> None:
    base = "/api/v2/dags/d/dagRuns/r1/taskInstances/t"
    fake3.routes[f"GET {base}/3"] = {
        "status": 200,
        "json": {"task_id": "t", "map_index": 3, "state": "failed", "try_number": 1},
    }
    fake3.routes[f"GET {base}/logs/1"] = {
        "status": 200,
        "json": {"content": [{"event": "erro no lote 3", "level": "error"}]},
    }
    logs = await make(fake3).task_logs("d/r1", "t[3]")
    assert logs == "ERROR - erro no lote 3"
    req = next(r for r in fake3.requests if "/logs/" in r.url.path)
    assert req.url.params["map_index"] == "3"


def test_render_log_content_shapes() -> None:
    assert render_log_content(None) == ""
    assert render_log_content("texto puro\nlinha 2") == "texto puro\nlinha 2"
    rendered = render_log_content(
        [
            {"event": "::group::Log message source details", "sources": ["/logs/a.log"]},
            {
                "timestamp": "2026-11-23T10:00:00Z",
                "level": "error",
                "event": "Task failed with exception",
                "filename": "task_runner.py",
                "lineno": 10,
                "dag_id": "d",
                "rows": 3,
                "error_detail": [
                    {
                        "exc_type": "KeyError",
                        "exc_value": "'id_cliente'",
                        "frames": [{"filename": "/dags/x.py", "lineno": 5, "name": "f"}],
                    }
                ],
            },
            "linha solta",
        ]
    )
    assert rendered.splitlines() == [
        "::group::Log message source details",
        "fontes: /logs/a.log",
        "[2026-11-23T10:00:00Z] {task_runner.py:10} ERROR - Task failed with exception rows=3",
        "Traceback (most recent call last):",
        '  File "/dags/x.py", line 5, in f',
        "KeyError: 'id_cliente'",
        "linha solta",
    ]


# ------------------------------------------------------------ trigger
QUEUED_RUN = {
    "dag_run_id": "manual__2026-11-23T23:16:58.344840+00:00",
    "dag_id": "carga_pedidos",
    "logical_date": None,
    "queued_at": "2026-11-23T23:16:58.351975Z",
    "start_date": None,
    "end_date": None,
    "run_after": "2026-11-23T23:16:58.344840Z",
    "run_type": "manual",
    "state": "queued",
    "triggered_by": "rest_api",
    "conf": {"data_ref": "2026-11-22"},
}


async def test_trigger_v3(fake3: FakeAirflow) -> None:
    fake3.routes["POST /api/v2/dags/carga_pedidos/dagRuns"] = {"status": 200, "json": QUEUED_RUN}
    run = await make(fake3).trigger("carga_pedidos", {"data_ref": "2026-11-22"})
    assert run.run_id == "carga_pedidos/manual__2026-11-23T23:16:58.344840+00:00"
    assert run.status == "queued"
    assert run.started_at is None
    req = next(r for r in fake3.requests if r.url.path.endswith("/dagRuns") and r.method == "POST")
    assert json.loads(req.content) == {"conf": {"data_ref": "2026-11-22"}, "logical_date": None}


async def test_trigger_v2(fake2: FakeAirflow) -> None:
    fake2.routes["POST /api/v1/dags/carga_pedidos/dagRuns"] = {"status": 200, "json": QUEUED_RUN}
    run = await make(fake2).trigger("carga_pedidos")
    assert run.status == "queued"
    req = next(r for r in fake2.requests if r.method == "POST")
    assert json.loads(req.content) == {"conf": {}}


async def test_trigger_unknown_dag(fake3: FakeAirflow) -> None:
    with pytest.raises(NotFoundError):
        await make(fake3).trigger("pipeline_que_nao_existe_xyz")


# ------------------------------------------------------------ configuração
def test_from_config() -> None:
    orch = AirflowOrchestrator.from_config(
        {"url": "http://localhost:8081/", "username": "admin", "password": "segredo-123"}, CTX
    )
    assert "localhost:8081" in repr(orch)
    assert "segredo-123" not in repr(orch)


@pytest.mark.parametrize(
    "cfg",
    [
        {},
        {"url": "http://x", "username": "admin"},
        {"url": "http://x", "variant": "mwaa"},
        {"url": "http://x", "variant": "outra"},
        {"url": "http://x", "api_version": "4"},
        {"url": "http://x", "api_version": "três"},
        {"url": "http://x", "variant": "basic"},
    ],
)
def test_from_config_errors(cfg: dict[str, Any]) -> None:
    with pytest.raises(ConfigError):
        AirflowOrchestrator.from_config(cfg, CTX)
