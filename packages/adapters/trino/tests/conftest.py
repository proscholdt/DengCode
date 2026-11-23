"""Fixtures dos testes do adapter Trino.

Integração: um container ``trinodb/trino`` por sessão (testcontainers), com os
catálogos de exemplo da imagem (``memory``, ``tpch``...). Heap pequeno (1 GB) e
limite de memória do container, porque a máquina de desenvolvimento é
compartilhada; o container é sempre derrubado no fim. Sem Docker, os testes de
integração são pulados.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any

import httpx2
import pytest

TRINO_IMAGE = "trinodb/trino:484"
STARTUP_TIMEOUT_S = 300


def _wait_until_ready(base_url: str) -> None:
    """Espera ``/v1/info`` responder com ``starting: false`` (o Trino demora a subir)."""
    deadline = time.monotonic() + STARTUP_TIMEOUT_S
    last = "sem resposta"
    while time.monotonic() < deadline:
        try:
            resp = httpx2.get(f"{base_url}/v1/info", timeout=5)
            if resp.status_code == 200 and resp.json().get("starting") is False:
                return
            last = f"HTTP {resp.status_code}: {resp.text[:100]}"
        except httpx2.HTTPError as exc:
            last = str(exc)
        time.sleep(2)
    raise TimeoutError(f"Trino não ficou pronto em {STARTUP_TIMEOUT_S}s ({last})")


@pytest.fixture(scope="session")
def trino_config() -> Iterator[dict[str, Any]]:
    """Configuração do adapter apontando para um Trino descartável."""
    from testcontainers.core.container import DockerContainer

    container = (
        DockerContainer(TRINO_IMAGE)
        .with_exposed_ports(8080)
        .with_env("JAVA_TOOL_OPTIONS", "-Xmx1G")
        .with_kwargs(mem_limit="1800m")
    )
    try:
        container.start()
    except Exception as exc:  # Docker ausente, parado ou sem permissão
        pytest.skip(f"Docker indisponível para testes de integração: {exc}")
    try:
        host = container.get_container_host_ip()
        port = int(container.get_exposed_port(8080))
        _wait_until_ready(f"http://{host}:{port}")
        yield {"host": host, "port": port, "catalog": "memory", "schema": "default"}
    finally:
        container.stop()
