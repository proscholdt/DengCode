from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from dengcode_core.errors import AuthError, RateLimitError, TransientError
from dengcode_core.llm.retry import (
    RetryPolicy,
    call_with_retry,
    parse_retry_after,
    stream_with_retry,
)


def _policy(sleeps: list[float], attempts: int = 5) -> RetryPolicy:
    async def fake_sleep(s: float) -> None:
        sleeps.append(s)

    return RetryPolicy(max_attempts=attempts, sleep=fake_sleep, rand=lambda: 1.0)


def test_delay_is_exponential_capped_and_respects_retry_after() -> None:
    p = RetryPolicy(rand=lambda: 1.0)
    assert [p.delay(n) for n in (1, 2, 3, 4)] == [1.0, 2.0, 4.0, 8.0]
    assert p.delay(10) == 30.0
    assert p.delay(1, retry_after_s=7) == 7
    assert RetryPolicy(rand=lambda: 0.0).delay(3) == 2.0  # jitter: metade


async def test_success_on_third_attempt() -> None:
    sleeps: list[float] = []
    calls = 0

    async def fn() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise RateLimitError("429", retry_after_s=0.5)
        return "ok"

    assert await call_with_retry(fn, _policy(sleeps)) == "ok"
    assert calls == 3
    assert sleeps == [0.5, 0.5]


async def test_non_transient_fails_immediately() -> None:
    sleeps: list[float] = []
    calls = 0

    async def fn() -> str:
        nonlocal calls
        calls += 1
        raise AuthError("401")

    with pytest.raises(AuthError):
        await call_with_retry(fn, _policy(sleeps))
    assert calls == 1
    assert sleeps == []


async def test_gives_up_after_max_attempts() -> None:
    sleeps: list[float] = []

    async def fn() -> str:
        raise TransientError("503")

    with pytest.raises(TransientError):
        await call_with_retry(fn, _policy(sleeps, attempts=3))
    assert len(sleeps) == 2


async def test_stream_not_retried_after_first_event() -> None:
    opened = 0

    async def open_stream() -> AsyncIterator[int]:
        nonlocal opened
        opened += 1
        yield 1
        raise TransientError("caiu no meio")

    got: list[int] = []

    async def consume() -> None:
        async for x in stream_with_retry(open_stream, _policy([])):
            got.append(x)

    with pytest.raises(TransientError):
        await consume()
    assert opened == 1
    assert got == [1]


async def test_stream_retried_before_first_event() -> None:
    opened = 0

    async def open_stream() -> AsyncIterator[int]:
        nonlocal opened
        opened += 1
        if opened < 3:
            raise TransientError("503")
        yield 42

    assert [x async for x in stream_with_retry(open_stream, _policy([]))] == [42]
    assert opened == 3


@pytest.mark.parametrize(
    ("raw", "expected"), [("3", 3.0), ("1.5", 1.5), ("x", None), (None, None), ("-2", 0.0)]
)
def test_parse_retry_after(raw: str | None, expected: float | None) -> None:
    assert parse_retry_after(raw) == expected
