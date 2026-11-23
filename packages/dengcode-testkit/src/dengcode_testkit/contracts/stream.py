"""Contratos de StreamPort e StreamComputePort.

Cada adapter herda a classe numa ``Test...`` e fornece as fixtures descritas
no docstring de cada contrato. Os contratos só leem: quem semeia os dados
(tópico, mensagens, grupo) é a fixture, contra containers locais.
"""

from __future__ import annotations

import pytest

from dengcode_core.errors import NotFoundError
from dengcode_core.ports.stream import StreamComputePort, StreamJobInfo, StreamPort

SAMPLE_KEYS = frozenset({"partition", "offset", "timestamp", "key", "value"})
"""Chaves que todo item de ``StreamPort.sample`` deve trazer."""

MISSING_TOPIC = "dc_contrato_topico_inexistente"
MISSING_JOB = "dc-contrato-job-inexistente"


class StreamContract:
    """Fixtures: ``stream``; ``topic_with_messages`` (nome de um tópico com pelo
    menos 3 mensagens JSON); ``consumer_group`` (grupo com offset gravado em
    ``topic_with_messages`` atrás do fim, ou seja, com lag > 0)."""

    async def test_topic_is_listed_with_partitions(
        self, stream: StreamPort, topic_with_messages: str
    ) -> None:
        topics = {t.name: t for t in await stream.list_topics()}
        assert topic_with_messages in topics
        assert topics[topic_with_messages].partitions >= 1
        assert all(not name.startswith("__") for name in topics)

    async def test_sample_returns_documented_dicts(
        self, stream: StreamPort, topic_with_messages: str
    ) -> None:
        items = await stream.sample(topic_with_messages, max_messages=10, timeout_s=10)
        assert 3 <= len(items) <= 10
        for item in items:
            assert set(item) >= SAMPLE_KEYS
            assert isinstance(item["partition"], int)
            assert isinstance(item["offset"], int)
            assert item["offset"] >= 0
        assert any(isinstance(item["value"], dict | list) for item in items)

    async def test_sample_respects_max_messages(
        self, stream: StreamPort, topic_with_messages: str
    ) -> None:
        items = await stream.sample(topic_with_messages, max_messages=2, timeout_s=10)
        assert len(items) <= 2

    async def test_sample_does_not_move_committed_offsets(
        self, stream: StreamPort, topic_with_messages: str, consumer_group: str
    ) -> None:
        before = await stream.consumer_lag(topic_with_messages, consumer_group)
        await stream.sample(topic_with_messages, max_messages=50, timeout_s=10)
        after = await stream.consumer_lag(topic_with_messages, consumer_group)
        assert after == before

    async def test_consumer_lag_of_lagging_group(
        self, stream: StreamPort, topic_with_messages: str, consumer_group: str
    ) -> None:
        if not stream.capabilities().supports_consumer_lag:
            pytest.skip("adapter sem lag de consumidor")
        lag = await stream.consumer_lag(topic_with_messages, consumer_group)
        assert lag
        assert all(isinstance(p, int) and isinstance(v, int) and v >= 0 for p, v in lag.items())
        assert sum(lag.values()) > 0

    async def test_unknown_topic_is_not_found(self, stream: StreamPort) -> None:
        with pytest.raises(NotFoundError):
            await stream.sample(MISSING_TOPIC, max_messages=1, timeout_s=2)
        with pytest.raises(NotFoundError):
            await stream.consumer_lag(MISSING_TOPIC, "dc-contrato-grupo")
        # ler não pode criar o tópico (auto.create.topics.enable)
        assert MISSING_TOPIC not in {t.name for t in await stream.list_topics()}

    async def test_schema_respects_capabilities(
        self, stream: StreamPort, topic_with_messages: str
    ) -> None:
        if not stream.capabilities().supports_schema_registry:
            assert await stream.schema(topic_with_messages) is None
            return
        found = await stream.schema(topic_with_messages)
        assert found is None or isinstance(found, dict)


class StreamComputeContract:
    """Fixture: ``stream_compute`` (com pelo menos um job)."""

    async def test_list_jobs(self, stream_compute: StreamComputePort) -> None:
        jobs = await stream_compute.list_jobs()
        assert jobs
        assert all(isinstance(j, StreamJobInfo) for j in jobs)
        assert len({j.id for j in jobs}) == len(jobs)

    async def test_job_status_matches_listing(self, stream_compute: StreamComputePort) -> None:
        for job in await stream_compute.list_jobs():
            status = await stream_compute.job_status(job.id)
            assert status.id == job.id
            assert status.name == job.name
            assert status.status == job.status

    async def test_unknown_job_is_not_found(self, stream_compute: StreamComputePort) -> None:
        with pytest.raises(NotFoundError):
            await stream_compute.job_status(MISSING_JOB)
        with pytest.raises(NotFoundError):
            await stream_compute.job_logs(MISSING_JOB)

    async def test_job_logs_are_text(self, stream_compute: StreamComputePort) -> None:
        job = (await stream_compute.list_jobs())[0]
        logs = await stream_compute.job_logs(job.id, tail_lines=50)
        assert isinstance(logs, str)
        assert len(logs.splitlines()) <= 50
