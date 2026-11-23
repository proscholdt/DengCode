"""Fixtures dos testes do adapter Iceberg REST.

Integração: um container ``apache/iceberg-rest-fixture`` por sessão (servidor
de referência da spec, catálogo em SQLite e warehouse no disco do container),
com heap pequeno. O container é sempre derrubado no fim; sem Docker, os testes
de integração são pulados.
"""

from __future__ import annotations

import time
from collections.abc import Iterator

import httpx2
import pytest

ICEBERG_REST_IMAGE = "apache/iceberg-rest-fixture:1.10.1"
STARTUP_TIMEOUT_S = 180


def _wait_until_ready(url: str) -> None:
    deadline = time.monotonic() + STARTUP_TIMEOUT_S
    last = "sem resposta"
    while time.monotonic() < deadline:
        try:
            resp = httpx2.get(f"{url}/v1/config", timeout=5)
            if resp.status_code == 200:
                return
            last = f"HTTP {resp.status_code}"
        except httpx2.HTTPError as exc:
            last = str(exc)
        time.sleep(1)
    raise TimeoutError(f"catálogo REST não ficou pronto em {STARTUP_TIMEOUT_S}s ({last})")


@pytest.fixture(scope="session")
def rest_url() -> Iterator[str]:
    """URL base de um catálogo Iceberg REST descartável."""
    from testcontainers.core.container import DockerContainer

    container = (
        DockerContainer(ICEBERG_REST_IMAGE)
        .with_exposed_ports(8181)
        .with_env("JAVA_TOOL_OPTIONS", "-Xmx256m")
        .with_kwargs(mem_limit="512m")
    )
    try:
        container.start()
    except Exception as exc:  # Docker ausente, parado ou sem permissão
        pytest.skip(f"Docker indisponível para testes de integração: {exc}")
    try:
        url = f"http://{container.get_container_host_ip()}:{container.get_exposed_port(8181)}"
        _wait_until_ready(url)
        yield url
    finally:
        container.stop()
