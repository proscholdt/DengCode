"""Fixtures dos testes do adapter PostgreSQL.

- Loop de eventos: o psycopg assíncrono não roda no ``ProactorEventLoop``
  (padrão do asyncio no Windows), então os testes deste pacote usam um
  ``SelectorEventLoop`` via hook do pytest-asyncio — sem mexer na política
  global de event loop.
- Banco: um ``PostgresContainer`` (testcontainers) por sessão; sem Docker os
  testes de integração são pulados.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable, Iterator, Mapping
from typing import Any

import pytest

POSTGRES_IMAGE = "postgres:16-alpine"


def pytest_asyncio_loop_factories(
    config: pytest.Config, item: pytest.Item
) -> Mapping[str, Callable[[], asyncio.AbstractEventLoop]]:
    if sys.platform == "win32":
        return {"selector": asyncio.SelectorEventLoop}
    return {"default": asyncio.new_event_loop}


@pytest.fixture(scope="session")
def pg_config() -> Iterator[dict[str, Any]]:
    """Configuração do adapter apontando para um Postgres descartável."""
    try:
        from testcontainers.community.postgres import PostgresContainer
    except ImportError:  # testcontainers < 4.13
        from testcontainers.postgres import PostgresContainer

    container = PostgresContainer(POSTGRES_IMAGE, driver=None)
    try:
        container.start()
    except Exception as exc:  # Docker ausente, parado ou sem permissão
        pytest.skip(f"Docker indisponível para testes de integração: {exc}")
    try:
        yield {
            "host": container.get_container_host_ip(),
            "port": int(container.get_exposed_port(5432)),
            "dbname": container.dbname,
            "user": container.username,
            "password": container.password,
            "connect_timeout_s": 10,
        }
    finally:
        container.stop()
