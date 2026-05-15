"""Unit tests for the on-chain APY calibration module."""

from __future__ import annotations

import json
from typing import Any, Callable

import httpx
import pytest

from pendle_mcp.chain_apy import (
    compute_u_actual_30d_chain,
    estimate_blocks_back,
    known_chain_ids,
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


def test_estimate_blocks_back_mainnet_30d() -> None:
    # ETH ~12s blocks * 30 days = 216000 blocks
    assert estimate_blocks_back(1, 30 * 86400) == 216000


def test_estimate_blocks_back_arbitrum_30d() -> None:
    # Arb ~0.25s blocks * 30 days = 10368000 blocks
    assert estimate_blocks_back(42161, 30 * 86400) == 10368000


def test_estimate_blocks_back_unknown_chain_returns_none() -> None:
    assert estimate_blocks_back(99999999, 30 * 86400) is None


def test_known_chain_ids_includes_majors() -> None:
    ids = known_chain_ids()
    assert 1 in ids
    assert 42161 in ids
    assert 8453 in ids
    assert 56 in ids


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


def _make_rpc_transport(
    *,
    latest_block: int,
    rate_at: dict[int, int],
    expect_url: str,
    expect_sy: str,
) -> httpx.MockTransport:
    """Build a MockTransport that answers eth_blockNumber + eth_call(exchangeRate)
    JSON-RPC calls. `rate_at` maps block_number -> raw uint256 exchangeRate."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == expect_url
        payload = json.loads(request.content)
        method = payload.get("method")
        rpc_id = payload.get("id", 1)
        if method == "eth_blockNumber":
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": rpc_id, "result": hex(latest_block)},
            )
        if method == "eth_call":
            params = payload.get("params") or []
            assert len(params) == 2
            call, block_tag = params
            assert call.get("to") == expect_sy
            assert call.get("data") == "0x3ba0b9a9"
            assert isinstance(block_tag, str) and block_tag.startswith("0x")
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

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_compute_u_actual_30d_chain_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    rpc_url = "https://eth.example.com/v2/key"
    sy = "0xbf98480425a29197e5d99d003017f63a1e595d02"
    monkeypatch.setenv("RPC_URL_1", rpc_url)

    latest_block = 25099140
    past_block = latest_block - 216000  # 30d at 12s blocks
    # 4.07% annualized 30d window -> ratio = 1 + 0.0407 * 30 / 365 = 1.003345...
    rate_past = 10**18
    rate_now = int(rate_past * (1.0 + 0.0407 * 30.0 / 365.0))

    transport = _make_rpc_transport(
        latest_block=latest_block,
        rate_at={latest_block: rate_now, past_block: rate_past},
        expect_url=rpc_url,
        expect_sy=sy,
    )
    async with httpx.AsyncClient(transport=transport) as http_client:
        value, error = await compute_u_actual_30d_chain(
            chain_id=1, sy_address=sy, http_client=http_client
        )

    assert error is None
    assert value is not None
    assert value == pytest.approx(0.0407, abs=1e-4)


@pytest.mark.asyncio
async def test_compute_u_actual_30d_chain_no_rpc_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RPC_URL_1", raising=False)
    value, error = await compute_u_actual_30d_chain(chain_id=1, sy_address="0x" + "ab" * 20)
    assert value is None
    assert error == "RPC_URL_1 not configured, cannot calibrate"


@pytest.mark.asyncio
async def test_compute_u_actual_30d_chain_unknown_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RPC_URL_99999999", "https://nowhere.example.com")
    value, error = await compute_u_actual_30d_chain(
        chain_id=99999999, sy_address="0x" + "ab" * 20
    )
    assert value is None
    assert error is not None
    assert "block-time entry" in error


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
    rpc_url = "https://eth.example.com/v2/key"
    sy = "0x" + "ab" * 20
    monkeypatch.setenv("RPC_URL_1", rpc_url)

    latest_block = 25099140

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        rpc_id = payload.get("id", 1)
        if payload.get("method") == "eth_blockNumber":
            return httpx.Response(
                200, json={"jsonrpc": "2.0", "id": rpc_id, "result": hex(latest_block)}
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
