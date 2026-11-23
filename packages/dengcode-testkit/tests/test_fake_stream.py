"""O FakeStreamCompute cumpre o contrato de StreamComputePort."""

from __future__ import annotations

import pytest

from dengcode_core.errors import NotFoundError
from dengcode_core.ports.stream import StreamComputePort, StreamJobInfo
from dengcode_testkit.contracts.stream import StreamComputeContract
from dengcode_testkit.fake_stream import FakeStreamCompute, FakeStreamJob


class TestFakeStreamComputeContract(StreamComputeContract):
    @pytest.fixture
    def stream_compute(self) -> FakeStreamCompute:
        return FakeStreamCompute()


def test_is_a_stream_compute_port() -> None:
    assert isinstance(FakeStreamCompute(), StreamComputePort)


async def test_default_has_one_failed_job_with_error_logs() -> None:
    fake = FakeStreamCompute()
    failed = [j for j in await fake.list_jobs() if j.status == "failed"]
    assert len(failed) == 1
    logs = await fake.job_logs(failed[0].id)
    assert "ERROR" in logs
    assert "OutOfMemoryError" in logs


async def test_logs_tail() -> None:
    fake = FakeStreamCompute()
    logs = await fake.job_logs("job-deduplica-eventos", tail_lines=2)
    assert len(logs.splitlines()) == 2
    assert logs.endswith("reinícios esgotados (3/3)")
    assert await fake.job_logs("job-deduplica-eventos", tail_lines=0) == ""


async def test_custom_jobs() -> None:
    job = FakeStreamJob(StreamJobInfo(id="j1", name="meu_job", status="unknown"), ["linha"])
    fake = FakeStreamCompute([job])
    assert [j.id for j in await fake.list_jobs()] == ["j1"]
    assert (await fake.job_status("j1")).status == "unknown"
    assert await fake.job_logs("j1") == "linha"
    with pytest.raises(NotFoundError):
        await fake.job_status("job-enriquece-pedidos")


def test_duplicate_ids_are_rejected() -> None:
    job = FakeStreamJob(StreamJobInfo(id="j1", name="a", status="running"))
    with pytest.raises(ValueError, match="repetido"):
        FakeStreamCompute([job, job])
