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
import datetime as dt
import os
import time
from dataclasses import dataclass, field
from typing import Any, Mapping

import httpx

# 30 days — the default lookback window for the on-chain calibration.
_THIRTY_DAYS = 30
_SECONDS_PER_DAY = 86400

# keccak256("exchangeRate()")[:4]. Standard Pendle SY interface; emits the
# accumulator that grows monotonically with underlying yield.
_EXCHANGE_RATE_SELECTOR = "0x3ba0b9a9"
_NAV_ORACLE_SELECTOR = "0x49d4640d"
_NAV_REPORTED_TOPIC = (
    "0x4b82b8834f3f7b776bcb5a777f77ea7aabcd427c3fa20fba6fb6887d99b0a17e"
)

_ZERO_ADDRESS = "0x" + "0" * 40

_DEFAULT_RPC_TIMEOUT_SECONDS = 15.0
_RPC_CAPABILITY_CACHE_TTL_SECONDS = 86400
_LOG_CHUNK_SIZE_BLOCKS = 10_000


@dataclass(frozen=True)
class ChainTruthResult:
    """Structured market chain-truth result for opportunity scanning.

    `status="ok"` means `value` is usable. All other statuses are explicit
    unknown/fail-closed states; callers must not treat them as zero yield.
    """

    value: float | None
    status: str
    method: str
    confidence: str
    error: str | None = None
    notes: str | None = None
    window_days: int | None = None
    effective_window_days: float | None = None
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "value": self.value,
            "status": self.status,
            "method": self.method,
            "confidence": self.confidence,
        }
        if self.error is not None:
            out["error"] = self.error
        if self.notes is not None:
            out["notes"] = self.notes
        if self.window_days is not None:
            out["window_days"] = self.window_days
        if self.effective_window_days is not None:
            out["effective_window_days"] = self.effective_window_days
        if self.diagnostics:
            out["diagnostics"] = self.diagnostics
        return out

    @classmethod
    def ok(
        cls,
        *,
        value: float,
        method: str,
        confidence: str,
        window_days: int,
        effective_window_days: float | None = None,
        notes: str | None = None,
        diagnostics: dict[str, Any] | None = None,
    ) -> "ChainTruthResult":
        return cls(
            value=value,
            status="ok",
            method=method,
            confidence=confidence,
            window_days=window_days,
            effective_window_days=effective_window_days,
            notes=notes,
            diagnostics=diagnostics or {},
        )

    @classmethod
    def fail(
        cls,
        *,
        status: str,
        method: str,
        error: str,
        confidence: str = "none",
        window_days: int | None = None,
        notes: str | None = None,
        diagnostics: dict[str, Any] | None = None,
    ) -> "ChainTruthResult":
        return cls(
            value=None,
            status=status,
            method=method,
            confidence=confidence,
            error=error,
            window_days=window_days,
            notes=notes,
            diagnostics=diagnostics or {},
        )


@dataclass(frozen=True)
class HistoricalStateCapability:
    trusted: bool
    status: str
    error: str | None = None
    diagnostics: dict[str, Any] = field(default_factory=dict)


class _RpcError(RuntimeError):
    """Internal — surfaced through `(value, error)` tuple, never raised out."""


class _BoundaryLogsReducedRange(RuntimeError):
    def __init__(
        self,
        first: Mapping[str, Any] | None,
        last: Mapping[str, Any] | None,
    ) -> None:
        super().__init__("eth_getLogs retry succeeded with reduced range")
        self.first = first
        self.last = last


_HISTORICAL_STATE_CAPABILITY_CACHE: dict[
    tuple[int, str, str], tuple[float, HistoricalStateCapability]
] = {}


def _rpc_url_env_name(chain_id: int) -> str:
    return f"RPC_URL_{chain_id}"


def load_rpc_urls(chain_id: int) -> list[str]:
    """Return RPC URLs from `RPC_URL_<chainid>`.

    A single URL is the common case. Comma-separated values express
    priority/fallback order for chains where different providers have
    different capabilities, e.g. archive-state vs event-log support.
    """
    raw = os.getenv(_rpc_url_env_name(chain_id))
    if raw is None:
        return []
    urls: list[str] = []
    for item in raw.split(","):
        stripped = item.strip()
        if stripped and stripped not in urls:
            urls.append(stripped)
    return urls


def load_rpc_url(chain_id: int) -> str | None:
    urls = load_rpc_urls(chain_id)
    if not urls:
        return None
    return urls[0]


def load_event_log_rpc_urls(chain_id: int) -> list[str]:
    """Return configured RPC URLs in priority order for event-log adapters.

    `RPC_URL_<chainid>` supports comma-separated fallbacks. Unlike historical
    state reads, event-log adapters may use endpoints that support
    `eth_getLogs` even if historical `eth_call` is untrusted.
    """
    return load_rpc_urls(chain_id)


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


def parse_chain_address(value: Any) -> str | None:
    """Parse either `<chainId>-<address>` or a bare 0x address."""
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if "-" in text:
        _, _, text = text.partition("-")
    if text.startswith("0x") and len(text) == 42:
        return text
    return None


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


async def _eth_get_code(
    client: httpx.AsyncClient,
    rpc_url: str,
    address: str,
    block_tag: str,
) -> str:
    result = await _rpc_call(client, rpc_url, "eth_getCode", [address, block_tag])
    if not isinstance(result, str) or not result.startswith("0x"):
        raise _RpcError(f"eth_getCode returned non-hex result: {result!r}")
    return result.lower()


async def _eth_call_selector(
    client: httpx.AsyncClient,
    rpc_url: str,
    address: str,
    selector: str,
    block_tag: str = "latest",
) -> str:
    result = await _rpc_call(
        client,
        rpc_url,
        "eth_call",
        [{"to": address, "data": selector}, block_tag],
    )
    if not isinstance(result, str) or not result.startswith("0x"):
        raise _RpcError(f"eth_call returned non-hex result: {result!r}")
    if result == "0x":
        raise _RpcError("eth_call returned empty data")
    return result.lower()


def _decode_address_word(result: str) -> str | None:
    payload = result[2:]
    if len(payload) < 64:
        return None
    address = "0x" + payload[-40:]
    if address == _ZERO_ADDRESS:
        return None
    return address.lower()


def _decode_uint256_word(data: str, index: int = 0) -> int:
    payload = data[2:] if data.startswith("0x") else data
    start = index * 64
    word = payload[start : start + 64]
    if len(word) != 64:
        raise _RpcError(f"cannot decode uint256 word {index} from {data!r}")
    return int(word, 16)


async def _eth_get_logs(
    client: httpx.AsyncClient,
    rpc_url: str,
    *,
    address: str,
    topic0: str,
    from_block: int,
    to_block: int,
) -> list[Mapping[str, Any]]:
    if from_block > to_block:
        return []
    result = await _rpc_call(
        client,
        rpc_url,
        "eth_getLogs",
        [
            {
                "address": address,
                "fromBlock": hex(from_block),
                "toBlock": hex(to_block),
                "topics": [topic0],
            }
        ],
    )
    if not isinstance(result, list):
        raise _RpcError(f"eth_getLogs returned unexpected shape: {type(result).__name__}")
    return [row for row in result if isinstance(row, Mapping)]


async def _eth_get_boundary_logs_chunked(
    client: httpx.AsyncClient,
    rpc_url: str,
    *,
    address: str,
    topic0: str,
    from_block: int,
    to_block: int,
    chunk_size: int = _LOG_CHUNK_SIZE_BLOCKS,
) -> tuple[Mapping[str, Any] | None, Mapping[str, Any] | None, int]:
    """Find first and last matching log without scanning the full window.

    HyperEVM public RPCs cap `eth_getLogs` ranges (dRPC free tier: 10000
    blocks; official RPC: 1000 blocks). NavOracle emits frequently, so probing
    from both boundaries finds the usable event pair with a handful of requests
    instead of scanning every chunk.
    """
    first: Mapping[str, Any] | None = None
    last: Mapping[str, Any] | None = None
    chunks_scanned = 0

    async def read_logs(start_block: int, end_block: int) -> list[Mapping[str, Any]]:
        nonlocal chunks_scanned
        chunks_scanned += 1
        try:
            return await _eth_get_logs(
                client,
                rpc_url,
                address=address,
                topic0=topic0,
                from_block=start_block,
                to_block=end_block,
            )
        except _RpcError as e:
            text = str(e).lower()
            if chunk_size > 1000 and ("1000" in text or "range" in text):
                smaller_first, smaller_last, smaller_chunks = (
                    await _eth_get_boundary_logs_chunked(
                        client,
                        rpc_url,
                        address=address,
                        topic0=topic0,
                        from_block=from_block,
                        to_block=to_block,
                        chunk_size=1000,
                    )
                )
                chunks_scanned += smaller_chunks
                # A private exception lets the outer function return the result
                # from the reduced-range retry without duplicating both loops.
                raise _BoundaryLogsReducedRange(smaller_first, smaller_last) from e
            raise

    start = from_block
    try:
        while start <= to_block and first is None:
            end = min(to_block, start + chunk_size - 1)
            logs = await read_logs(start, end)
            if logs:
                logs.sort(
                    key=lambda row: (
                        int(str(row.get("blockNumber", "0x0")), 16),
                        int(str(row.get("logIndex", "0x0")), 16),
                    )
                )
                first = logs[0]
            start = end + 1

        end = to_block
        while end >= from_block and last is None:
            start = max(from_block, end - chunk_size + 1)
            logs = await read_logs(start, end)
            if logs:
                logs.sort(
                    key=lambda row: (
                        int(str(row.get("blockNumber", "0x0")), 16),
                        int(str(row.get("logIndex", "0x0")), 16),
                    )
                )
                last = logs[-1]
            end = start - 1
    except _BoundaryLogsReducedRange as reduced:
        return reduced.first, reduced.last, chunks_scanned

    return first, last, chunks_scanned


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


async def _check_historical_state_capability(
    client: httpx.AsyncClient,
    rpc_url: str,
    *,
    chain_id: int,
    probe_contract: str,
) -> HistoricalStateCapability:
    """Detect whether `eth_call`-style historical state reads are trustworthy.

    We use `eth_getCode(probe_contract, "0x1")` as a chain-agnostic pre-creation
    probe: Pendle SY / underlying vault contracts should not exist at block 1.
    A provider returning current bytecode at block 1 is silently serving latest
    state for historical calls and must not be used for SY exchangeRate windows.
    """
    cache_key = (chain_id, rpc_url, probe_contract.lower())
    now = time.time()
    cached = _HISTORICAL_STATE_CAPABILITY_CACHE.get(cache_key)
    if cached is not None:
        cached_at, capability = cached
        if now - cached_at < _RPC_CAPABILITY_CACHE_TTL_SECONDS:
            return capability

    try:
        latest_code, block1_code = await asyncio.gather(
            _eth_get_code(client, rpc_url, probe_contract, "latest"),
            _eth_get_code(client, rpc_url, probe_contract, "0x1"),
        )
    except (httpx.HTTPError, _RpcError) as e:
        capability = HistoricalStateCapability(
            trusted=False,
            status="untrusted_rpc",
            error=f"{type(e).__name__}: {e}",
            diagnostics={"probe_contract": probe_contract},
        )
    else:
        if latest_code in {"0x", "0x0"}:
            capability = HistoricalStateCapability(
                trusted=False,
                status="contract_revert",
                error="probe contract has no latest bytecode",
                diagnostics={"probe_contract": probe_contract},
            )
        elif block1_code not in {"0x", "0x0"}:
            capability = HistoricalStateCapability(
                trusted=False,
                status="untrusted_rpc",
                error=(
                    "historical eth_getCode returned bytecode at block 1; "
                    "provider is likely serving latest state for historical calls"
                ),
                diagnostics={
                    "probe_contract": probe_contract,
                    "latest_code_bytes": max(0, (len(latest_code) - 2) // 2),
                    "block1_code_bytes": max(0, (len(block1_code) - 2) // 2),
                },
            )
        else:
            capability = HistoricalStateCapability(
                trusted=True,
                status="ok",
                diagnostics={
                    "probe_contract": probe_contract,
                    "latest_code_bytes": max(0, (len(latest_code) - 2) // 2),
                    "block1_code": block1_code,
                },
            )

    _HISTORICAL_STATE_CAPABILITY_CACHE[cache_key] = (now, capability)
    return capability


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


async def compute_u_actual_chain(
    *,
    chain_id: int,
    sy_address: str,
    window_days: int = _THIRTY_DAYS,
    rpc_url: str | None = None,
    rpc_timeout_seconds: float = _DEFAULT_RPC_TIMEOUT_SECONDS,
    http_client: httpx.AsyncClient | None = None,
) -> tuple[float | None, str | None]:
    """Compute on-chain annualized APY for a Pendle SY token.

    Returns `(value, error)` — exactly one is `None`. `value` is a decimal
    (e.g. `0.0407` for 4.07%); annualization is linear:
    `(rate_now / rate_window_ago - 1) × 365 / window_days`.

    The `http_client` parameter is injectable for tests; in production we
    open and close a fresh client per call. Typical cost: 6-10 RPC calls
    (~1-2s wall-clock) thanks to Newton-style cadence estimation + the
    cadence-guided bisect described in this module's top docstring.
    """
    if window_days <= 0:
        return None, f"window_days must be positive, got {window_days}"

    effective_rpc_url = rpc_url or load_rpc_url(chain_id)
    if effective_rpc_url is None:
        return None, f"RPC_URL_{chain_id} not configured, cannot calibrate"

    owns_client = http_client is None
    client = http_client or httpx.AsyncClient(timeout=rpc_timeout_seconds)
    try:
        try:
            latest_block, latest_ts = await _eth_get_block_header(
                client, effective_rpc_url, "latest"
            )
            target_ts = latest_ts - window_days * _SECONDS_PER_DAY
            past_block = await _find_block_at_or_before_timestamp(
                client,
                effective_rpc_url,
                target_ts=target_ts,
                latest_block=latest_block,
                latest_ts=latest_ts,
            )
            rate_now_raw, rate_past_raw = await asyncio.gather(
                _eth_call_exchange_rate(
                    client, effective_rpc_url, sy_address, latest_block
                ),
                _eth_call_exchange_rate(
                    client, effective_rpc_url, sy_address, past_block
                ),
            )
        except (httpx.HTTPError, _RpcError) as e:
            return None, f"{type(e).__name__}: {e}"
    finally:
        if owns_client:
            await client.aclose()

    if rate_past_raw == 0:
        return None, (
            f"{window_days}d-ago exchange rate is zero (SY may not have been deployed)"
        )

    ratio = rate_now_raw / rate_past_raw
    apy = (ratio - 1.0) * 365.0 / window_days
    return apy, None


async def compute_u_actual_30d_chain(
    *,
    chain_id: int,
    sy_address: str,
    rpc_timeout_seconds: float = _DEFAULT_RPC_TIMEOUT_SECONDS,
    http_client: httpx.AsyncClient | None = None,
) -> tuple[float | None, str | None]:
    """Compute on-chain 30d annualized APY for a Pendle SY token.

    Backward-compatible wrapper kept for the existing `pendle_get_market_data_v2`
    calibration field.
    """
    return await compute_u_actual_chain(
        chain_id=chain_id,
        sy_address=sy_address,
        window_days=_THIRTY_DAYS,
        rpc_timeout_seconds=rpc_timeout_seconds,
        http_client=http_client,
    )


def _market_datetime(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


async def _try_read_nav_oracle(
    client: httpx.AsyncClient,
    rpc_url: str,
    *,
    vault_address: str,
) -> str | None:
    result: str | None = None
    for attempt in range(3):
        try:
            result = await _eth_call_selector(
                client, rpc_url, vault_address, _NAV_ORACLE_SELECTOR, "latest"
            )
            break
        except (httpx.HTTPError, _RpcError):
            if attempt == 2:
                return None
            await asyncio.sleep(0.2 * (attempt + 1))
    if result is None:
        return None
    return _decode_address_word(result)


def _decode_nav_reported(log: Mapping[str, Any]) -> tuple[int, int | None, int, int]:
    data = log.get("data")
    if not isinstance(data, str) or not data.startswith("0x"):
        raise _RpcError("NavReported log missing hex data")
    pps = _decode_uint256_word(data, 0)
    reported_ts: int | None = None
    try:
        reported_ts = _decode_uint256_word(data, 1)
    except _RpcError:
        reported_ts = None
    block_number_raw = log.get("blockNumber")
    log_index_raw = log.get("logIndex")
    if not isinstance(block_number_raw, str) or not block_number_raw.startswith("0x"):
        raise _RpcError("NavReported log missing blockNumber")
    if not isinstance(log_index_raw, str) or not log_index_raw.startswith("0x"):
        raise _RpcError("NavReported log missing logIndex")
    return pps, reported_ts, int(block_number_raw, 16), int(log_index_raw, 16)


async def _compute_navoracle_event_chain_truth(
    *,
    chain_id: int,
    market: Mapping[str, Any],
    window_days: int,
    rpc_timeout_seconds: float,
) -> ChainTruthResult | None:
    vault_address = parse_chain_address(market.get("underlyingAsset"))
    if vault_address is None:
        return None

    rpc_urls = load_event_log_rpc_urls(chain_id)
    if not rpc_urls:
        return ChainTruthResult.fail(
            status="adapter_required",
            method="navoracle_event",
            error=f"RPC_URL_{chain_id} not configured and no event-log fallback is known",
            window_days=window_days,
        )

    last_error: str | None = None
    for rpc_url in rpc_urls:
        async with httpx.AsyncClient(timeout=rpc_timeout_seconds) as client:
            nav_oracle = await _try_read_nav_oracle(
                client, rpc_url, vault_address=vault_address
            )
            if nav_oracle is None:
                continue
            try:
                latest_block, latest_ts = await _eth_get_block_header(
                    client, rpc_url, "latest"
                )
                market_created_at = _market_datetime(market.get("timestamp"))
                if market_created_at is not None:
                    start_ts = int(market_created_at.timestamp()) - _SECONDS_PER_DAY
                else:
                    start_ts = latest_ts - window_days * _SECONDS_PER_DAY
                target_ts = max(start_ts, latest_ts - window_days * _SECONDS_PER_DAY)
                start_block = await _find_block_at_or_before_timestamp(
                    client,
                    rpc_url,
                    target_ts=target_ts,
                    latest_block=latest_block,
                    latest_ts=latest_ts,
                )
                first_log, last_log, chunks_scanned = await _eth_get_boundary_logs_chunked(
                    client,
                    rpc_url,
                    address=nav_oracle,
                    topic0=_NAV_REPORTED_TOPIC,
                    from_block=start_block,
                    to_block=latest_block,
                )
            except (httpx.HTTPError, _RpcError) as e:
                last_error = f"{type(e).__name__}: {e}"
                continue

        if first_log is None or last_log is None:
            return ChainTruthResult.fail(
                status="insufficient_history",
                method="navoracle_event",
                error="NavOracle emitted no NavReported events in the scan window",
                window_days=window_days,
                diagnostics={"nav_oracle": nav_oracle, "chunks_scanned": chunks_scanned},
            )

        first_pps, first_reported_ts, first_block, first_log_index = _decode_nav_reported(
            first_log
        )
        last_pps, last_reported_ts, last_block, last_log_index = _decode_nav_reported(
            last_log
        )
        if first_block == last_block and first_log_index == last_log_index:
            return ChainTruthResult.fail(
                status="insufficient_history",
                method="navoracle_event",
                error="NavOracle emitted fewer than 2 NavReported events in the scan window",
                window_days=window_days,
                diagnostics={"nav_oracle": nav_oracle, "chunks_scanned": chunks_scanned},
            )
        if first_pps <= 0:
            return ChainTruthResult.fail(
                status="contract_revert",
                method="navoracle_event",
                error="first NavReported pps is zero",
                window_days=window_days,
                diagnostics={"nav_oracle": nav_oracle, "first_block": first_block},
            )

        if first_reported_ts is not None and last_reported_ts is not None:
            elapsed_seconds = last_reported_ts - first_reported_ts
        else:
            async with httpx.AsyncClient(timeout=rpc_timeout_seconds) as client:
                first_ts, last_ts = await asyncio.gather(
                    _eth_get_block_timestamp(client, rpc_url, first_block),
                    _eth_get_block_timestamp(client, rpc_url, last_block),
                )
            elapsed_seconds = last_ts - first_ts

        if elapsed_seconds <= 0:
            return ChainTruthResult.fail(
                status="insufficient_history",
                method="navoracle_event",
                error="NavReported event timestamps did not advance",
                window_days=window_days,
                diagnostics={"nav_oracle": nav_oracle},
            )

        effective_window_days = elapsed_seconds / _SECONDS_PER_DAY
        ratio = last_pps / first_pps
        apy = ratio ** (365.0 / effective_window_days) - 1.0
        confidence = "high" if effective_window_days >= 1.0 else "medium"
        return ChainTruthResult.ok(
            value=apy,
            method="navoracle_event",
            confidence=confidence,
            window_days=window_days,
            effective_window_days=effective_window_days,
            notes=(
                "Computed from NavOracle.NavReported event-log pps ratio; "
                "does not depend on historical eth_call."
            ),
            diagnostics={
                "vault": vault_address,
                "nav_oracle": nav_oracle,
                "first_pps": first_pps,
                "last_pps": last_pps,
                "first_block": first_block,
                "last_block": last_block,
                "first_log_index": first_log_index,
                "last_log_index": last_log_index,
                "chunks_scanned": chunks_scanned,
                "rpc_url_role": "primary"
                if rpc_url == (load_rpc_url(chain_id) or "")
                else "event_fallback",
            },
        )

    if last_error is not None:
        return ChainTruthResult.fail(
            status="contract_revert",
            method="navoracle_event",
            error=last_error,
            window_days=window_days,
        )
    return None


async def _compute_sy_exchange_rate_chain_truth(
    *,
    chain_id: int,
    sy_address: str,
    window_days: int,
    rpc_timeout_seconds: float,
) -> ChainTruthResult:
    rpc_urls = load_rpc_urls(chain_id)
    if not rpc_urls:
        return ChainTruthResult.fail(
            status="untrusted_rpc",
            method="sy_accumulator",
            error=f"RPC_URL_{chain_id} not configured, cannot calibrate",
            window_days=window_days,
        )

    failed_capabilities: list[dict[str, Any]] = []
    selected_rpc_url: str | None = None
    selected_capability: HistoricalStateCapability | None = None
    async with httpx.AsyncClient(timeout=rpc_timeout_seconds) as client:
        for rpc_url in rpc_urls:
            capability = await _check_historical_state_capability(
                client,
                rpc_url,
                chain_id=chain_id,
                probe_contract=sy_address,
            )
            if capability.trusted:
                selected_rpc_url = rpc_url
                selected_capability = capability
                break
            failed_capabilities.append(
                {
                    "rpc_url": rpc_url,
                    "status": capability.status,
                    "error": capability.error,
                    **capability.diagnostics,
                }
            )
    if selected_rpc_url is None or selected_capability is None:
        return ChainTruthResult.fail(
            status="untrusted_rpc",
            method="sy_accumulator",
            error="no configured RPC URL passed historical-state capability probe",
            window_days=window_days,
            diagnostics={"rpc_attempts": failed_capabilities},
        )

    value, error = await compute_u_actual_chain(
        chain_id=chain_id,
        sy_address=sy_address,
        window_days=window_days,
        rpc_url=selected_rpc_url,
        rpc_timeout_seconds=rpc_timeout_seconds,
    )
    if error is not None or value is None:
        status = "contract_revert"
        if error and ("too young" in error or "not have been deployed" in error):
            status = "insufficient_history"
        return ChainTruthResult.fail(
            status=status,
            method="sy_accumulator",
            error=error or "unknown SY exchangeRate calibration error",
            window_days=window_days,
            diagnostics=selected_capability.diagnostics
            | {"rpc_url": selected_rpc_url, "rpc_attempts": failed_capabilities},
        )
    return ChainTruthResult.ok(
        value=value,
        method="sy_accumulator",
        confidence="high",
        window_days=window_days,
        diagnostics=selected_capability.diagnostics
        | {"rpc_url": selected_rpc_url, "rpc_attempts": failed_capabilities},
    )


async def compute_chain_truth_for_market(
    *,
    chain_id: int,
    market: Mapping[str, Any],
    window_days: int = _THIRTY_DAYS,
    rpc_timeout_seconds: float = _DEFAULT_RPC_TIMEOUT_SECONDS,
) -> ChainTruthResult:
    """Compute market-level chain truth with protocol-aware adapters.

    Adapter order is deliberate: protocol-specific event adapters run before
    the generic SY accumulator, so markets like AVLT do not get flattened into
    a false "0%" result when SY.exchangeRate is not the actual yield path.
    """
    if window_days <= 0:
        return ChainTruthResult.fail(
            status="contract_revert",
            method="none",
            error=f"window_days must be positive, got {window_days}",
            window_days=window_days,
        )

    nav_result = await _compute_navoracle_event_chain_truth(
        chain_id=chain_id,
        market=market,
        window_days=window_days,
        rpc_timeout_seconds=rpc_timeout_seconds,
    )
    if nav_result is not None:
        return nav_result

    sy_address = parse_sy_address(market.get("sy"))
    if sy_address is None:
        return ChainTruthResult.fail(
            status="adapter_required",
            method="none",
            error=f"market sy field unparseable: {market.get('sy')!r}",
            window_days=window_days,
        )

    return await _compute_sy_exchange_rate_chain_truth(
        chain_id=chain_id,
        sy_address=sy_address,
        window_days=window_days,
        rpc_timeout_seconds=rpc_timeout_seconds,
    )
