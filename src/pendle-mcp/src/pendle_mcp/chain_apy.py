"""On-chain calibration for Pendle hosted-API underlyingApy.

Pendle's hosted `underlyingApy` (and the wider family `baseApy` /
`ytFloatingApy` / `aggregatedApy` / `ytRoi`) is a short sliding-window
display value — it annualizes recent `SY.exchangeRate` growth, which makes
NAV-discrete / occasional-distribute underlyings look 2-6× their sustained
rate (see pendle-research finding 26). This module reads `SY.exchangeRate()`
at `latest_block` and at the largest block whose timestamp is at or before
`latest_ts - 30d` (located via Newton-style cadence-refinement on
`eth_getBlockByNumber` plus a narrow final bisect), and emits an annualized
30d window APY (`u_actual_30d_chain`) callers can use as ground truth.

Requirements:
- An archive RPC reachable via `RPC_URL_<chainid>` env var. Etherscan's
  `module=proxy` eth_call always returns latest state for historical blocks,
  so we deliberately do NOT fall back to it.

Approach (no chain-keyed block-time table — Pendle keeps adding chains and
any static cadence assumption would silently rot):

1. One call to `eth_getBlockByNumber("latest", false)` returns both
   `latest_block` and `latest_ts` — folding two old RPCs into one.
2. Newton-style estimate: probe assuming 12s/block; compute realized
   cadence between the probe and latest; refine and probe again. Converges
   to ~1-block precision on ETH (12s exact) and ~hundreds-of-blocks
   precision on Arb / Base / BSC after one refinement.
3. Cadence-guided bisect on the residual bracket: `mid = lo +
   (target_ts - lo_ts) / cadence` (Newton step inside the bisect) instead
   of the standard `mid = (lo + hi) / 2`. Converges in 1-5 probes even on
   brackets that are millions of blocks wide; degrades to standard bisect
   if cadence is wildly off (still advances ≥ 1 block per probe).
4. The two `SY.exchangeRate()` reads at `latest_block` and the resolved
   30d-ago block run in parallel via `asyncio.gather`.

Typical cost: 6 RPC calls on ETH-cadence chains, ~10 on faster ones like
Arbitrum — vs ~28-30 on a naive `[1, latest_block]` bisect.

The module exposes one entry point — `compute_u_actual_30d_chain` — that
returns `(value, error)` with exactly one populated.
"""

from __future__ import annotations

import asyncio
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


async def _eth_get_block_header(
    client: httpx.AsyncClient, rpc_url: str, block_tag: str
) -> tuple[int, int]:
    """Fetch (block_number, timestamp) for a block, addressed by hex tag or
    one of `"latest"` / `"earliest"` / `"pending"`. Header-only, no txs.

    Folds `eth_blockNumber` + `eth_getBlockByNumber` into a single RPC when
    `block_tag == "latest"`.
    """
    result = await _rpc_call(
        client,
        rpc_url,
        "eth_getBlockByNumber",
        [block_tag, False],
    )
    if result is None:
        raise _RpcError(f"block {block_tag} not found")
    if not isinstance(result, dict):
        raise _RpcError(
            f"eth_getBlockByNumber({block_tag}) returned unexpected shape: "
            f"{type(result).__name__}"
        )
    number_hex = result.get("number")
    ts_hex = result.get("timestamp")
    if not isinstance(number_hex, str) or not number_hex.startswith("0x"):
        raise _RpcError(
            f"eth_getBlockByNumber({block_tag}) block number not hex: {number_hex!r}"
        )
    if not isinstance(ts_hex, str) or not ts_hex.startswith("0x"):
        raise _RpcError(
            f"eth_getBlockByNumber({block_tag}) timestamp not hex: {ts_hex!r}"
        )
    return int(number_hex, 16), int(ts_hex, 16)


async def _eth_get_block_timestamp(
    client: httpx.AsyncClient, rpc_url: str, block_number: int
) -> int:
    """Fetch the unix timestamp of a specific block. Thin wrapper around
    `_eth_get_block_header` kept as a separate helper so the bisect loop
    only carries one piece of data through."""
    _, ts = await _eth_get_block_header(client, rpc_url, hex(block_number))
    return ts


async def _find_block_at_or_before_timestamp(
    client: httpx.AsyncClient,
    rpc_url: str,
    *,
    target_ts: int,
    latest_block: int,
    latest_ts: int,
) -> int:
    """Largest block N in [1, latest_block] with `block(N).timestamp <= target_ts`.

    Uses Newton-style cadence refinement (≤2 probes) to land near the
    answer, then bisects the residual bracket. Typical cost: 4-8 RPC calls
    vs ~25 from a flat bisect. Raises `_RpcError` if the chain doesn't have
    ≥30d of history (or its earliest blocks are unqueryable).

    Note: we use block 1 — not block 0 — as the lower bound. Some chains
    reject `eth_getBlockByNumber(0)` (HyperEVM returns
    "invalid block height: 0"), and block 0 is only useful when the target
    ts is at genesis, which is the chain-too-young case we want to fail out
    on anyway.
    """
    if target_ts >= latest_ts:
        return latest_block

    # All sampled (block, ts) pairs. We keep them so we can pick the
    # tightest bracket for the final bisect.
    samples: list[tuple[int, int]] = [(latest_block, latest_ts)]

    # Newton iterations: assume 12s/block, sample, then refine using
    # realized cadence. ≤2 iterations is enough — on ETH (12s exact) the
    # first probe lands within 1 block; on Arb/Base/BSC the realized
    # cadence is so different that one refinement nails it.
    cadence = 12.0
    last_estimate: int | None = None
    for _ in range(2):
        offset_blocks = int((latest_ts - target_ts) / cadence)
        estimate = max(1, latest_block - offset_blocks)
        if estimate >= latest_block:
            break
        if last_estimate is not None and abs(estimate - last_estimate) <= 2:
            break
        last_estimate = estimate
        ts = await _eth_get_block_timestamp(client, rpc_url, estimate)
        samples.append((estimate, ts))
        span_blocks = latest_block - estimate
        span_seconds = latest_ts - ts
        if span_blocks <= 0 or span_seconds <= 0:
            break
        cadence = span_seconds / span_blocks

    # Pick the tightest [lo, hi] bracket from samples.
    lo_block: int | None = None
    lo_ts: int | None = None
    hi_block = latest_block
    for block, ts in samples:
        if ts <= target_ts:
            if lo_block is None or block > lo_block:
                lo_block, lo_ts = block, ts
        else:
            if block < hi_block:
                hi_block = block

    # If no probe landed at-or-before target, fail-fast on block 1
    # (re-using its ts if Newton already sampled it).
    if lo_block is None:
        ts1: int | None = next((t for b, t in samples if b == 1), None)
        if ts1 is None:
            try:
                ts1 = await _eth_get_block_timestamp(client, rpc_url, 1)
            except _RpcError as e:
                raise _RpcError(
                    f"chain too young or earliest blocks unqueryable: cannot read "
                    f"block 1 ({e})"
                ) from e
        if ts1 > target_ts:
            raise _RpcError(
                f"chain too young: block 1 ts {ts1} > target ts {target_ts} "
                "(need ≥30d of history for calibration)"
            )
        lo_block, lo_ts = 1, ts1

    # Cadence-guided bisect: instead of standard mid = (lo + hi) / 2, pick
    # `mid = lo + (target_ts - lo_ts) / cadence` so we probe directly at the
    # estimated target. With a consistent cadence this converges in 1-2
    # iterations even on a [probe1, latest] bracket that's millions of blocks
    # wide; with a wrong cadence guess it degrades to standard bisect (since
    # we always advance by at least 1 block). Updates `cadence` each round
    # with the realized (latest_ts - mid_ts) / (latest_block - mid).
    while hi_block - lo_block > 1:
        if cadence > 0 and lo_ts is not None:
            offset_blocks = int((target_ts - lo_ts) / cadence)
            mid = max(lo_block + 1, min(hi_block - 1, lo_block + offset_blocks))
        else:
            mid = (lo_block + hi_block) // 2
        mid_ts = await _eth_get_block_timestamp(client, rpc_url, mid)
        if mid_ts <= target_ts:
            lo_block, lo_ts = mid, mid_ts
            span_blocks = latest_block - mid
            span_seconds = latest_ts - mid_ts
            if span_blocks > 0 and span_seconds > 0:
                cadence = span_seconds / span_blocks
        else:
            hi_block = mid

    return lo_block


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
    open and close a fresh client per call. Typical cost: 6-10 RPC calls
    (~1-2s wall-clock) thanks to Newton-style cadence estimation + the
    cadence-guided bisect described in this module's top docstring.
    """
    rpc_url = load_rpc_url(chain_id)
    if rpc_url is None:
        return None, f"RPC_URL_{chain_id} not configured, cannot calibrate"

    owns_client = http_client is None
    client = http_client or httpx.AsyncClient(timeout=rpc_timeout_seconds)
    try:
        try:
            latest_block, latest_ts = await _eth_get_block_header(
                client, rpc_url, "latest"
            )
            target_ts = latest_ts - _THIRTY_DAYS_SECONDS
            past_block = await _find_block_at_or_before_timestamp(
                client,
                rpc_url,
                target_ts=target_ts,
                latest_block=latest_block,
                latest_ts=latest_ts,
            )
            rate_now_raw, rate_past_raw = await asyncio.gather(
                _eth_call_exchange_rate(client, rpc_url, sy_address, latest_block),
                _eth_call_exchange_rate(client, rpc_url, sy_address, past_block),
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
