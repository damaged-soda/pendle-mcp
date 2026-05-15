"""On-chain calibration for Pendle hosted-API underlyingApy.

Pendle's hosted `underlyingApy` (and the wider family `baseApy` /
`ytFloatingApy` / `aggregatedApy` / `ytRoi`) is a short sliding-window
display value — it annualizes recent `SY.exchangeRate` growth, which makes
NAV-discrete / occasional-distribute underlyings look 2-6× their sustained
rate (see pendle-research finding 26). This module reads `SY.exchangeRate()`
at `latest` and at the largest block whose timestamp is at or before
`latest_ts - 30d` (found via JSON-RPC `eth_getBlockByNumber` binary search),
and emits an annualized 30d window APY (`u_actual_30d_chain`) callers can
use as ground truth.

Requirements:
- An archive RPC reachable via `RPC_URL_<chainid>` env var. Etherscan's
  `module=proxy` eth_call always returns latest state for historical blocks,
  so we deliberately do NOT fall back to it.

Approach:
- Bisect on block timestamps rather than carrying a chain-keyed block-time
  table — Pendle keeps adding chains (HyperEVM, Berachain, …) and any
  static cadence assumption would silently rot. Bisect is ~log2(latest)
  extra RPC calls (≈25-30 on major chains, ~2-3s wall time), in exchange
  for correctness across every chain Pendle ever lists and ±1-block target
  precision instead of the ±10-minute drift a fixed-cadence estimate gives.

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


async def _eth_get_block_timestamp(
    client: httpx.AsyncClient, rpc_url: str, block_number: int
) -> int:
    """Fetch the unix timestamp of a specific block (header-only, no txs)."""
    result = await _rpc_call(
        client,
        rpc_url,
        "eth_getBlockByNumber",
        [hex(block_number), False],
    )
    if result is None:
        raise _RpcError(f"block {block_number} not found")
    if not isinstance(result, dict):
        raise _RpcError(
            f"eth_getBlockByNumber returned unexpected shape: {type(result).__name__}"
        )
    ts_hex = result.get("timestamp")
    if not isinstance(ts_hex, str) or not ts_hex.startswith("0x"):
        raise _RpcError(f"eth_getBlockByNumber block timestamp not hex: {ts_hex!r}")
    return int(ts_hex, 16)


async def _find_block_at_or_before_timestamp(
    client: httpx.AsyncClient,
    rpc_url: str,
    *,
    target_ts: int,
    latest_block: int,
    latest_ts: int,
) -> int:
    """Largest block N in [1, latest_block] with `block(N).timestamp <= target_ts`.

    Plain bisect on `eth_getBlockByNumber` timestamps. Costs ~log2(latest_block)
    RPC calls (25-30 on major chains). Raises `_RpcError` if the chain doesn't
    have ≥30d of history (or its earliest blocks are unqueryable).

    We use block 1 — not block 0 — as the lower bound: some chains reject
    `eth_getBlockByNumber(0)` (HyperEVM returns "invalid block height: 0"),
    and using block 1 avoids that without affecting precision (block 0 is
    only useful when the target ts is at genesis, which is the
    chain-too-young case we want to fail out anyway).
    """
    if target_ts >= latest_ts:
        return latest_block

    # Fail-fast: if block 1 is itself younger than target_ts, the chain
    # doesn't have ≥30d of history. This also seeds the bisect bracket
    # with a known `lo_ts`, saving the cost of a redundant probe later.
    lo: int = 1
    lo_ts: int | None = None
    try:
        lo_ts = await _eth_get_block_timestamp(client, rpc_url, 1)
        if lo_ts > target_ts:
            raise _RpcError(
                f"chain too young: block 1 ts {lo_ts} > target ts {target_ts} "
                "(need ≥30d of history for calibration)"
            )
    except _RpcError as e:
        if "too young" in str(e):
            raise
        # Block 1 unqueryable (extremely rare); proceed without the seed,
        # bisect will probe higher blocks. lo_ts remains None.

    hi = latest_block
    while hi - lo > 1:
        mid = (lo + hi) // 2
        mid_ts = await _eth_get_block_timestamp(client, rpc_url, mid)
        if mid_ts <= target_ts:
            lo, lo_ts = mid, mid_ts
        else:
            hi = mid

    if lo_ts is None:
        raise _RpcError(
            f"chain too young or earliest blocks unqueryable: no block at or "
            f"before target ts {target_ts}"
        )
    return lo


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
    open and close a fresh client per call. Cost: ~log2(latest_block) + 4
    RPC calls (≈30 on major chains).
    """
    rpc_url = load_rpc_url(chain_id)
    if rpc_url is None:
        return None, f"RPC_URL_{chain_id} not configured, cannot calibrate"

    owns_client = http_client is None
    client = http_client or httpx.AsyncClient(timeout=rpc_timeout_seconds)
    try:
        try:
            latest_block = await _eth_block_number(client, rpc_url)
            latest_ts = await _eth_get_block_timestamp(client, rpc_url, latest_block)
            target_ts = latest_ts - _THIRTY_DAYS_SECONDS
            past_block = await _find_block_at_or_before_timestamp(
                client,
                rpc_url,
                target_ts=target_ts,
                latest_block=latest_block,
                latest_ts=latest_ts,
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
