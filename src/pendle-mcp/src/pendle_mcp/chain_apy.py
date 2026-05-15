"""On-chain calibration for Pendle hosted-API underlyingApy.

Pendle's hosted `underlyingApy` (and the wider family `baseApy` /
`ytFloatingApy` / `aggregatedApy` / `ytRoi`) is a short sliding-window
display value — it annualizes recent `SY.exchangeRate` growth, which makes
NAV-discrete / occasional-distribute underlyings look 2-6× their sustained
rate (see pendle-research finding 26). This module reads `SY.exchangeRate()`
at `latest` and at `latest - 30d` via JSON-RPC and emits an annualized 30d
window APY (`u_actual_30d_chain`) callers can use as ground truth.

Requirements:
- An archive RPC reachable via `RPC_URL_<chainid>` env var. Etherscan's
  `module=proxy` eth_call always returns latest state for historical blocks,
  so we deliberately do NOT fall back to it.
- A block-time entry for the chain (see `_BLOCK_TIME_SECONDS`). Lookback
  block is estimated as `latest - round(30d / block_time)`; ±10min
  wall-clock drift over 30d shifts the APY by < 0.01pp.

The module exposes one entry point — `compute_u_actual_30d_chain` — that
returns `(value, error)` with exactly one populated.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

# 30 days in seconds — the lookback window for the on-chain calibration.
_THIRTY_DAYS_SECONDS = 30 * 86400

# keccak256("exchangeRate()")[:4]. Standard Pendle SY interface; emits the
# accumulator that grows monotonically with underlying yield.
_EXCHANGE_RATE_SELECTOR = "0x3ba0b9a9"

_DEFAULT_RPC_TIMEOUT_SECONDS = 15.0


# Mean block production cadence per chain (seconds/block). Used only to estimate
# the 30d-ago block; ±5% drift is invisible at APY precision because the window
# is multiplicative (30d apart vs 30d ± a few minutes is rounding noise).
_BLOCK_TIME_SECONDS: dict[int, float] = {
    1: 12.0,        # Ethereum mainnet
    10: 2.0,        # Optimism
    56: 3.0,        # BNB Chain
    137: 2.1,       # Polygon PoS
    146: 1.0,       # Sonic
    250: 1.0,       # Fantom Opera
    5000: 2.0,      # Mantle
    8453: 2.0,      # Base
    42161: 0.25,    # Arbitrum One
    81457: 2.0,     # Blast
    534352: 3.0,    # Scroll
}


class _RpcError(RuntimeError):
    """Internal — surfaced through `(value, error)` tuple, never raised out."""


def _rpc_url_env_name(chain_id: int) -> str:
    return f"RPC_URL_{chain_id}"


def load_rpc_url(chain_id: int) -> str | None:
    raw = os.getenv(_rpc_url_env_name(chain_id))
    if raw is None:
        return None
    stripped = raw.strip()
    return stripped or None


def parse_sy_address(sy_field: Any) -> str | None:
    """Pull the bare SY address out of a `<chainId>-<address>` Pendle id string."""
    if not isinstance(sy_field, str):
        return None
    if "-" not in sy_field:
        return None
    _, _, address = sy_field.partition("-")
    address = address.strip().lower()
    if not address.startswith("0x") or len(address) != 42:
        return None
    return address


def estimate_blocks_back(chain_id: int, seconds: int) -> int | None:
    block_time = _BLOCK_TIME_SECONDS.get(chain_id)
    if block_time is None or block_time <= 0:
        return None
    return int(round(seconds / block_time))


def known_chain_ids() -> list[int]:
    """Chains we can estimate a 30d-ago block for."""
    return sorted(_BLOCK_TIME_SECONDS.keys())


async def _rpc_call(
    client: httpx.AsyncClient, rpc_url: str, method: str, params: list[Any]
) -> Any:
    response = await client.post(
        rpc_url,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        headers={"Content-Type": "application/json"},
    )
    if response.status_code < 200 or response.status_code >= 300:
        raise _RpcError(
            f"RPC HTTP {response.status_code}: {response.text[:200]}"
        )
    try:
        data = response.json()
    except ValueError as e:
        raise _RpcError(f"RPC invalid JSON: {e}") from e
    if not isinstance(data, dict):
        raise _RpcError("RPC response not a JSON object")
    err = data.get("error")
    if err:
        raise _RpcError(f"RPC error: {err}")
    if "result" not in data:
        raise _RpcError("RPC response missing 'result'")
    return data["result"]


async def _eth_block_number(client: httpx.AsyncClient, rpc_url: str) -> int:
    result = await _rpc_call(client, rpc_url, "eth_blockNumber", [])
    if not isinstance(result, str) or not result.startswith("0x"):
        raise _RpcError(f"eth_blockNumber returned unexpected result: {result!r}")
    return int(result, 16)


async def _eth_call_exchange_rate(
    client: httpx.AsyncClient,
    rpc_url: str,
    sy_address: str,
    block_number: int,
) -> int:
    block_tag = hex(block_number)
    result = await _rpc_call(
        client,
        rpc_url,
        "eth_call",
        [{"to": sy_address, "data": _EXCHANGE_RATE_SELECTOR}, block_tag],
    )
    if not isinstance(result, str) or not result.startswith("0x"):
        raise _RpcError(f"eth_call returned non-hex result: {result!r}")
    payload = result[2:]
    if not payload:
        # Empty `0x` typically means the call reverted or the contract has no
        # `exchangeRate()` at this block (e.g. SY deployed after the lookback).
        raise _RpcError(
            "eth_call returned empty data (SY may not have been deployed at this block)"
        )
    try:
        return int(payload, 16)
    except ValueError as e:
        raise _RpcError(f"eth_call result is not hex: {result!r}") from e


async def compute_u_actual_30d_chain(
    *,
    chain_id: int,
    sy_address: str,
    rpc_timeout_seconds: float = _DEFAULT_RPC_TIMEOUT_SECONDS,
    http_client: httpx.AsyncClient | None = None,
) -> tuple[float | None, str | None]:
    """Compute on-chain 30d annualized APY for a Pendle SY token.

    Returns `(value, error)` — exactly one is `None`. `value` is a decimal
    (e.g. `0.0407` for 4.07%); annualization is linear:
    `(rate_now / rate_30d_ago - 1) × 365 / 30`.

    The `http_client` parameter is injectable for tests; in production we
    open and close a fresh client per call.
    """
    rpc_url = load_rpc_url(chain_id)
    if rpc_url is None:
        return None, f"RPC_URL_{chain_id} not configured, cannot calibrate"

    blocks_back = estimate_blocks_back(chain_id, _THIRTY_DAYS_SECONDS)
    if blocks_back is None:
        return None, (
            f"no block-time entry for chain {chain_id}; cannot estimate 30d-ago block"
        )

    owns_client = http_client is None
    client = http_client or httpx.AsyncClient(timeout=rpc_timeout_seconds)
    try:
        try:
            latest_block = await _eth_block_number(client, rpc_url)
            past_block = latest_block - blocks_back
            if past_block <= 0:
                return None, (
                    f"chain {chain_id} too young: latest={latest_block}, "
                    f"30d-ago block would be {past_block}"
                )
            rate_now_raw = await _eth_call_exchange_rate(
                client, rpc_url, sy_address, latest_block
            )
            rate_past_raw = await _eth_call_exchange_rate(
                client, rpc_url, sy_address, past_block
            )
        except (httpx.HTTPError, _RpcError) as e:
            return None, f"{type(e).__name__}: {e}"
    finally:
        if owns_client:
            await client.aclose()

    if rate_past_raw == 0:
        return None, "30d-ago exchange rate is zero (SY may not have been deployed)"

    ratio = rate_now_raw / rate_past_raw
    apy = (ratio - 1.0) * 365.0 / 30.0
    return apy, None
