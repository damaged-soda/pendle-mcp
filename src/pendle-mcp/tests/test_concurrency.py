import asyncio

import httpx
import pytest

import pendle_mcp.pendle_api as pendle_api
from pendle_mcp.pendle_api import (
    DEFAULT_PENDLE_API_MAX_CONCURRENCY,
    DEFAULT_PENDLE_API_MAX_RETRIES,
    PendleApiClient,
)


@pytest.fixture(autouse=True)
def _reset_concurrency_state():
    pendle_api._reset_global_concurrency_state()
    yield
    pendle_api._reset_global_concurrency_state()


def test_default_max_retries_is_3() -> None:
    # Layer A: bumped from 1 to 3 to absorb short 429 bursts
    assert DEFAULT_PENDLE_API_MAX_RETRIES == 3


def test_default_max_concurrency_is_4() -> None:
    assert DEFAULT_PENDLE_API_MAX_CONCURRENCY == 4


def test_read_env_concurrency_limit_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PENDLE_API_MAX_CONCURRENCY", raising=False)
    assert pendle_api._read_env_concurrency_limit() == DEFAULT_PENDLE_API_MAX_CONCURRENCY


def test_read_env_concurrency_limit_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PENDLE_API_MAX_CONCURRENCY", "7")
    assert pendle_api._read_env_concurrency_limit() == 7


def test_read_env_concurrency_limit_invalid_int(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PENDLE_API_MAX_CONCURRENCY", "not-a-number")
    with pytest.raises(ValueError, match="must be an integer"):
        pendle_api._read_env_concurrency_limit()


def test_read_env_concurrency_limit_below_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PENDLE_API_MAX_CONCURRENCY", "0")
    with pytest.raises(ValueError, match=">= 1"):
        pendle_api._read_env_concurrency_limit()


@pytest.mark.asyncio
async def test_concurrency_cap_bounds_inflight_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Cap at 3 so we can verify the semaphore enforces the bound when 10 are
    # fired concurrently.
    monkeypatch.setenv("PENDLE_API_MAX_CONCURRENCY", "3")

    inflight = 0
    peak = 0

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal inflight, peak
        inflight += 1
        peak = max(peak, inflight)
        # Hold the slot long enough that any unbounded fan-out would race past.
        await asyncio.sleep(0.02)
        inflight -= 1
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    async with PendleApiClient(
        base_url="https://api-v2.pendle.finance/core", transport=transport
    ) as client:
        await asyncio.gather(*(client.get_chains() for _ in range(10)))

    assert peak == 3, f"expected peak inflight 3, got {peak}"


@pytest.mark.asyncio
async def test_semaphore_held_across_retry_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retrying request must keep its slot through backoff sleeps so the
    cooldown is real — otherwise other inflight requests barge in and
    re-trigger 429."""
    monkeypatch.setenv("PENDLE_API_MAX_CONCURRENCY", "1")

    # Two requests fired concurrently with a slot count of 1: the second
    # must NOT start until the first (including its retry attempts) is done.
    inflight = 0
    peak = 0
    call_count = {"n": 0}

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal inflight, peak
        inflight += 1
        peak = max(peak, inflight)
        call_count["n"] += 1
        # First two attempts get 429 (with no Retry-After so we use backoff),
        # third returns 200. This forces the retry path with sleeps.
        n = call_count["n"]
        await asyncio.sleep(0.005)
        try:
            if n <= 2:
                return httpx.Response(429, json={"error": "rate"})
            return httpx.Response(200, json={"ok": True})
        finally:
            inflight -= 1

    transport = httpx.MockTransport(handler)
    async with PendleApiClient(
        base_url="https://api-v2.pendle.finance/core",
        transport=transport,
        max_retries=3,
        retry_backoff_seconds=0.01,
    ) as client:
        await asyncio.gather(client.get_chains(), client.get_chains())

    assert peak == 1, f"expected peak inflight 1, got {peak}"


@pytest.mark.asyncio
async def test_semaphore_singleton_across_clients(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two PendleApiClient instances on the same loop must share the
    process-wide semaphore (the whole point of the global cap)."""
    monkeypatch.setenv("PENDLE_API_MAX_CONCURRENCY", "2")

    inflight = 0
    peak = 0

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal inflight, peak
        inflight += 1
        peak = max(peak, inflight)
        await asyncio.sleep(0.02)
        inflight -= 1
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)

    async def fire_one() -> None:
        # Each call instantiates its own client — mirroring how MCP tool
        # calls work today (PendleApiClient.from_env() per call).
        async with PendleApiClient(
            base_url="https://api-v2.pendle.finance/core", transport=transport
        ) as client:
            await client.get_chains()

    await asyncio.gather(*(fire_one() for _ in range(8)))

    assert peak == 2, f"expected peak inflight 2 across separate clients, got {peak}"


@pytest.mark.asyncio
async def test_semaphore_recreated_for_new_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Module-level semaphore is loop-bound; a different loop must get a
    fresh semaphore instead of erroring on cross-loop usage."""
    monkeypatch.setenv("PENDLE_API_MAX_CONCURRENCY", "2")

    sem1 = pendle_api._get_concurrency_semaphore()
    assert sem1 is pendle_api._get_concurrency_semaphore()  # same loop = same sem


def test_from_env_picks_up_new_max_retries_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PENDLE_API_MAX_RETRIES", raising=False)
    client = PendleApiClient.from_env()
    try:
        assert client._max_retries == 3
    finally:
        # Ensure the http client gets cleaned up
        import asyncio as _asyncio

        _asyncio.run(client.aclose())
