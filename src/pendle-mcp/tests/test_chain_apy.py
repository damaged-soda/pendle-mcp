"""Unit tests for the on-chain APY calibration module."""

from __future__ import annotations

import json
from typing import Any, Callable

import httpx
import pytest

from pendle_mcp.chain_apy import (
    compute_u_actual_30d_chain,
    load_rpc_url,
    parse_sy_address,
)


def test_parse_sy_address_valid() -> None:
    sy = "1-0xb47cbf6697a6518222c7af4098a43aefe2739c8c"
    assert parse_sy_address(sy) == "0xb47cbf6697a6518222c7af4098a43aefe2739c8c"


def test_parse_sy_address_lowercases_mixed_case_input() -> None:
    sy = "42161-0xB47cBF6697A6518222C7AF4098A43AEFE2739C8C"
    assert parse_sy_address(sy) == "0xb47cbf6697a6518222c7af4098a43aefe2739c8c"


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "",
        "0xb47cbf6697a6518222c7af4098a43aefe2739c8c",  # missing chain prefix
        "1-",
        "1-0xshort",
        "1-not-an-address",
        42,
    ],
)
def test_parse_sy_address_rejects_bad_inputs(bad: Any) -> None:
    assert parse_sy_address(bad) is None


def test_load_rpc_url_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RPC_URL_1", "https://example.com/eth")
    assert load_rpc_url(1) == "https://example.com/eth"


def test_load_rpc_url_strips_whitespace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RPC_URL_1", "   https://example.com/eth   ")
    assert load_rpc_url(1) == "https://example.com/eth"


def test_load_rpc_url_empty_string_treated_as_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RPC_URL_1", "")
    assert load_rpc_url(1) is None


def test_load_rpc_url_returns_none_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RPC_URL_1", raising=False)
    assert load_rpc_url(1) is None


def _u256_hex(value: int) -> str:
    return "0x" + format(value, "064x")


def _make_chain_rpc_handler(
    *,
    latest_block: int,
    block_time_seconds: float,
    genesis_ts: int,
    sy_address: str,
    rate_at: dict[int, int],
    expect_url: str,
    call_log: list[str] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    """Build a synthetic-chain RPC handler:
    - `eth_blockNumber` always returns `latest_block`.
    - `eth_getBlockByNumber(N)` returns `{timestamp: genesis_ts + N * block_time}`.
    - `eth_call(SY, exchangeRate())` returns `rate_at[block_number]` (raw uint256).
    """

    def block_ts(n: int) -> int:
        return int(genesis_ts + n * block_time_seconds)

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == expect_url
        payload = json.loads(request.content)
        method = payload.get("method")
        rpc_id = payload.get("id", 1)
        if call_log is not None:
            call_log.append(method)
        if method == "eth_blockNumber":
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": rpc_id, "result": hex(latest_block)},
            )
        if method == "eth_getBlockByNumber":
            params = payload.get("params") or []
            block_tag, include_txs = params
            assert include_txs is False
            block_number = int(block_tag, 16)
            ts = block_ts(block_number)
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "result": {
                        "number": hex(block_number),
                        "timestamp": hex(ts),
                    },
                },
            )
        if method == "eth_call":
            params = payload.get("params") or []
            call, block_tag = params
            assert call.get("to") == sy_address
            assert call.get("data") == "0x3ba0b9a9"
            block_number = int(block_tag, 16)
            rate = rate_at.get(block_number)
            if rate is None:
                return httpx.Response(
                    200,
                    json={
                        "jsonrpc": "2.0",
                        "id": rpc_id,
                        "error": {"code": -32000, "message": f"no rate at {block_number}"},
                    },
                )
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": rpc_id, "result": _u256_hex(rate)},
            )
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": rpc_id,
                "error": {"code": -32601, "message": "method not handled"},
            },
        )

    return handler


@pytest.mark.asyncio
async def test_compute_u_actual_30d_chain_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """On a synthetic ETH-like chain (12s blocks), bisect should land on the
    block exactly 30d before latest, and the APY math should produce 4.07%."""
    rpc_url = "https://eth.example.com/v2/key"
    sy = "0xbf98480425a29197e5d99d003017f63a1e595d02"
    monkeypatch.setenv("RPC_URL_1", rpc_url)

    latest_block = 25099140
    block_time = 12.0
    genesis_ts = 1438269988  # ETH-ish genesis
    expected_past_block = latest_block - int(30 * 86400 / block_time)  # 216000 blocks

    rate_past = 10**18
    rate_now = int(rate_past * (1.0 + 0.0407 * 30.0 / 365.0))

    call_log: list[str] = []
    transport = httpx.MockTransport(
        _make_chain_rpc_handler(
            latest_block=latest_block,
            block_time_seconds=block_time,
            genesis_ts=genesis_ts,
            sy_address=sy,
            rate_at={latest_block: rate_now, expected_past_block: rate_past},
            expect_url=rpc_url,
            call_log=call_log,
        )
    )
    async with httpx.AsyncClient(transport=transport) as http_client:
        value, error = await compute_u_actual_30d_chain(
            chain_id=1, sy_address=sy, http_client=http_client
        )

    assert error is None
    assert value is not None
    assert value == pytest.approx(0.0407, abs=1e-4)

    # Sanity-check the call pattern: 1 eth_blockNumber + bisect probes + 2 eth_calls.
    assert call_log.count("eth_blockNumber") == 1
    assert call_log.count("eth_call") == 2
    # Bisect cost is bounded by log2(latest_block) + 1 (genesis check) + 1
    # (latest ts). On 25M blocks that's ≤ 27. Keep the bound loose.
    assert call_log.count("eth_getBlockByNumber") <= 30


@pytest.mark.asyncio
async def test_compute_u_actual_30d_chain_works_on_chain_without_predefined_block_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Chain 999 (HyperEVM) — not in any pre-baked block-time table. Calibration
    should still work because we bisect on actual block timestamps."""
    rpc_url = "https://hyperevm.example.com"
    sy = "0x" + "ab" * 20
    monkeypatch.setenv("RPC_URL_999", rpc_url)

    latest_block = 5_000_000
    block_time = 1.0
    genesis_ts = 1_700_000_000
    expected_past_block = latest_block - int(30 * 86400 / block_time)

    rate_past = 10**18
    rate_now = int(rate_past * (1.0 + 0.10 * 30.0 / 365.0))  # 10% APY

    transport = httpx.MockTransport(
        _make_chain_rpc_handler(
            latest_block=latest_block,
            block_time_seconds=block_time,
            genesis_ts=genesis_ts,
            sy_address=sy,
            rate_at={latest_block: rate_now, expected_past_block: rate_past},
            expect_url=rpc_url,
        )
    )
    async with httpx.AsyncClient(transport=transport) as http_client:
        value, error = await compute_u_actual_30d_chain(
            chain_id=999, sy_address=sy, http_client=http_client
        )

    assert error is None
    assert value == pytest.approx(0.10, abs=1e-3)


@pytest.mark.asyncio
async def test_compute_u_actual_30d_chain_no_rpc_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RPC_URL_1", raising=False)
    value, error = await compute_u_actual_30d_chain(chain_id=1, sy_address="0x" + "ab" * 20)
    assert value is None
    assert error == "RPC_URL_1 not configured, cannot calibrate"


@pytest.mark.asyncio
async def test_compute_u_actual_30d_chain_chain_too_young(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A chain whose genesis is within the last 30d should fail with a clear error."""
    rpc_url = "https://young.example.com"
    sy = "0x" + "ab" * 20
    monkeypatch.setenv("RPC_URL_42", rpc_url)

    # Genesis 10 days ago: 30d-ago target is before genesis.
    import time

    now_ts = int(time.time())
    genesis_ts = now_ts - 10 * 86400
    latest_block = 100_000

    transport = httpx.MockTransport(
        _make_chain_rpc_handler(
            latest_block=latest_block,
            block_time_seconds=10.0,
            genesis_ts=genesis_ts,
            sy_address=sy,
            rate_at={},
            expect_url=rpc_url,
        )
    )
    async with httpx.AsyncClient(transport=transport) as http_client:
        value, error = await compute_u_actual_30d_chain(
            chain_id=42, sy_address=sy, http_client=http_client
        )

    assert value is None
    assert error is not None
    assert "chain too young" in error


@pytest.mark.asyncio
async def test_compute_u_actual_30d_chain_rpc_error(monkeypatch: pytest.MonkeyPatch) -> None:
    rpc_url = "https://eth.example.com/v2/key"
    sy = "0x" + "ab" * 20
    monkeypatch.setenv("RPC_URL_1", rpc_url)

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": payload.get("id", 1),
                "error": {"code": -32000, "message": "boom"},
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        value, error = await compute_u_actual_30d_chain(
            chain_id=1, sy_address=sy, http_client=http_client
        )
    assert value is None
    assert error is not None
    assert "boom" in error


@pytest.mark.asyncio
async def test_compute_u_actual_30d_chain_empty_eth_call_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SY contract reverts (or wasn't deployed yet) → eth_call returns `0x`."""
    rpc_url = "https://eth.example.com/v2/key"
    sy = "0x" + "ab" * 20
    monkeypatch.setenv("RPC_URL_1", rpc_url)

    latest_block = 25099140
    block_time = 12.0
    genesis_ts = 1438269988

    def block_ts(n: int) -> int:
        return int(genesis_ts + n * block_time)

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        rpc_id = payload.get("id", 1)
        method = payload.get("method")
        if method == "eth_blockNumber":
            return httpx.Response(
                200, json={"jsonrpc": "2.0", "id": rpc_id, "result": hex(latest_block)}
            )
        if method == "eth_getBlockByNumber":
            block_tag = payload["params"][0]
            n = int(block_tag, 16)
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "result": {"number": hex(n), "timestamp": hex(block_ts(n))},
                },
            )
        # eth_call: empty 0x means contract reverted / does not implement.
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": rpc_id, "result": "0x"}
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        value, error = await compute_u_actual_30d_chain(
            chain_id=1, sy_address=sy, http_client=http_client
        )
    assert value is None
    assert error is not None
    assert "empty data" in error
