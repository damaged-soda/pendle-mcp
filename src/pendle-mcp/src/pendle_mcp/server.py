"""FastMCP server tools wrapping Pendle's hosted API.

Be careful with the APY fields Pendle's hosted API returns — `underlyingApy`,
`baseApy`, `ytFloatingApy`, `aggregatedApy`, `ytRoi` and friends are
short-sliding-window display values, not on-chain ground truth. They
annualize the *recent* `SY.exchangeRate` change, so NAV-discrete /
occasional-distribute underlyings (Midas weekly NAV pushes, Superform
distribute events, etc.) get systematically overstated by 2-6× until the
sliding window rolls past the jump. Continuous-accrual underlyings (Ethena
7d-vest, daily-pulse vaults) happen to track ground truth within ~15%, but
that's happenstance — the field semantics are the same. See
pendle-research finding 26 + `infra/underlying-apy-ground-truth.md` for
reference samples.

For ground truth, `pendle_get_market_data_v2` always attaches
`u_actual_30d_chain` (and the diagnostic `u_ui_vs_chain_ratio`) computed by
reading `SY.exchangeRate()` at `latest_block` and at the largest block whose
timestamp is at or before `latest_ts - 30d` (found via `eth_getBlockByNumber`
bisect on `RPC_URL_<chainid>`) — see `pendle_mcp.chain_apy`.
"""

from __future__ import annotations

import asyncio
import csv
import datetime as dt
import io
import time
from typing import Any, Mapping

from pendle_mcp.chain_apy import (
    compute_u_actual_30d_chain,
    compute_chain_truth_for_market,
    parse_sy_address,
)
from pendle_mcp.pendle_api import (
    PendleApiClient,
    PendleApiError,
    PendleAssetType,
    TransactionAction,
    TransactionType,
)

try:
    from mcp.server.fastmcp import FastMCP
except Exception as e:
    raise RuntimeError(
        "MCP Python SDK is required; install dependencies (e.g. `pip install -e \".[dev]\"`)."
    ) from e

mcp = FastMCP("pendle-mcp")

_OHLCV_RESULT_KEYS = ("time", "open", "high", "low", "close", "volume")

_PNL_GROUP_BY_ALLOWED = {"action", "tx_hash"}
_PNL_SUMMARY_DEFAULT_PAGE_SIZE = 100
# Hard upper bound on rows scanned by `pendle_get_user_pnl_summary`. Picked
# generously (≈100 active retail addresses' lifetime PnL) so it should never
# trip in normal use; if it does, the tool raises so callers can't be misled
# by a partial summary that *looks* complete.
_PNL_SUMMARY_HARD_CAP_ROWS = 10000
_MARKETS_ALL_PAGE_SIZE = 100
_NEW_MARKET_OPPORTUNITY_DEFAULT_CALIBRATION_CONCURRENCY = 2


def _coerce_float(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    return 0.0


def _parse_iso_datetime(value: Any) -> dt.datetime | None:
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


def _days_between(now: dt.datetime, past: dt.datetime | None) -> float | None:
    if past is None:
        return None
    return (now - past).total_seconds() / 86400.0


def _days_until(future: dt.datetime | None, now: dt.datetime) -> float | None:
    if future is None:
        return None
    return (future - now).total_seconds() / 86400.0


def _extract_market_address(market: Mapping[str, Any]) -> str | None:
    address = market.get("address")
    if isinstance(address, str) and address.startswith("0x") and len(address) == 42:
        return address.lower()
    return None


def _market_float(market: Mapping[str, Any], key: str) -> float | None:
    details = market.get("details")
    if not isinstance(details, Mapping):
        return None
    value = details.get(key)
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _market_tvl_usd(market: Mapping[str, Any]) -> float | None:
    return _market_float(market, "totalTvl")


def _market_underlying_apy(market: Mapping[str, Any]) -> float | None:
    return _market_float(market, "underlyingApy")


def _market_implied_apy(market: Mapping[str, Any]) -> float | None:
    return _market_float(market, "impliedApy")


def _bps(value: float | None) -> float | None:
    if value is None:
        return None
    return value * 10_000.0


def _flatten_pnl_row(row: Mapping[str, Any]) -> dict[str, float]:
    pt_spent = (row.get("ptData") or {}).get("spent_v2") or {}
    yt_spent = (row.get("ytData") or {}).get("spent_v2") or {}
    lp_spent = (row.get("lpData") or {}).get("spent_v2") or {}
    profit = row.get("profit") or {}
    return {
        "spentUsd": _coerce_float(pt_spent.get("usd"))
        + _coerce_float(yt_spent.get("usd"))
        + _coerce_float(lp_spent.get("usd")),
        "spentAsset": _coerce_float(pt_spent.get("asset"))
        + _coerce_float(yt_spent.get("asset"))
        + _coerce_float(lp_spent.get("asset")),
        "spentEth": _coerce_float(pt_spent.get("eth"))
        + _coerce_float(yt_spent.get("eth"))
        + _coerce_float(lp_spent.get("eth")),
        "profitUsd": _coerce_float(profit.get("usd")),
        "profitAsset": _coerce_float(profit.get("asset")),
        "profitEth": _coerce_float(profit.get("eth")),
        "txValueAsset": _coerce_float(row.get("txValueAsset")),
    }


def _totals_from_pnl_rows(rows: list[Mapping[str, Any]]) -> dict[str, float]:
    """Sum profit + txValueAsset across all rows. Spent intentionally omitted —
    callers wanting capital-adjusted ROI should pull `groups[*].spent*` since
    spent isn't a single signed quantity at the top level (pt+yt+lp legs)."""
    totals = {
        "totalProfitUsd": 0.0,
        "totalProfitAsset": 0.0,
        "totalProfitEth": 0.0,
        "totalTxValueAsset": 0.0,
    }
    for row in rows:
        flat = _flatten_pnl_row(row)
        totals["totalProfitUsd"] += flat["profitUsd"]
        totals["totalProfitAsset"] += flat["profitAsset"]
        totals["totalProfitEth"] += flat["profitEth"]
        totals["totalTxValueAsset"] += flat["txValueAsset"]
    return totals


def _aggregate_pnl_rows(rows: list[Mapping[str, Any]], group_by: str) -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = {}
    sets: dict[str, dict[str, set[Any]]] = {}
    for row in rows:
        if group_by == "action":
            key = row.get("action") or ""
        else:  # tx_hash
            key = row.get("txHash") or ""
        bucket = buckets.get(key)
        if bucket is None:
            bucket = {
                "key": key,
                "count": 0,
                "profitUsd": 0.0,
                "profitAsset": 0.0,
                "profitEth": 0.0,
                "spentUsd": 0.0,
                "spentAsset": 0.0,
                "spentEth": 0.0,
                "txValueAsset": 0.0,
            }
            if group_by == "action":
                bucket["chainIds"] = []
                bucket["markets"] = []
            else:
                bucket["actions"] = []
                bucket["chainId"] = row.get("chainId")
                bucket["market"] = row.get("market")
                bucket["timestamp"] = row.get("timestamp")
            buckets[key] = bucket
            sets[key] = {
                "actions": set(),
                "chainIds": set(),
                "markets": set(),
            }
        flat = _flatten_pnl_row(row)
        bucket["count"] += 1
        for fk in (
            "profitUsd",
            "profitAsset",
            "profitEth",
            "spentUsd",
            "spentAsset",
            "spentEth",
            "txValueAsset",
        ):
            bucket[fk] += flat[fk]
        meta = sets[key]
        action = row.get("action")
        if action is not None:
            meta["actions"].add(action)
        chain_id = row.get("chainId")
        if chain_id is not None:
            meta["chainIds"].add(chain_id)
        market = row.get("market")
        if market is not None:
            meta["markets"].add(market)
        if group_by == "tx_hash":
            ts = row.get("timestamp")
            if ts and (bucket["timestamp"] is None or ts < bucket["timestamp"]):
                bucket["timestamp"] = ts

    for key, bucket in buckets.items():
        meta = sets[key]
        if group_by == "action":
            bucket["chainIds"] = sorted(meta["chainIds"])
            bucket["markets"] = sorted(meta["markets"])
        else:
            bucket["actions"] = sorted(meta["actions"])

    return sorted(buckets.values(), key=lambda b: b["count"], reverse=True)


def _parse_ohlcv_results_csv(results: str) -> list[dict[str, str]]:
    reader = csv.reader(io.StringIO(results))
    rows: list[dict[str, str]] = []
    header_skipped = False
    for row_index, row in enumerate(reader):
        normalized = [value.strip() for value in row]
        if not any(normalized):
            continue
        if not header_skipped and normalized and normalized[0].lower() in {"time", "timestamp"}:
            header_skipped = True
            continue
        if len(normalized) != len(_OHLCV_RESULT_KEYS):
            raise ValueError(
                f"Expected {len(_OHLCV_RESULT_KEYS)} columns but got {len(normalized)} at row {row_index}."
            )
        rows.append(dict(zip(_OHLCV_RESULT_KEYS, normalized, strict=True)))
    return rows


@mcp.tool()
async def pendle_get_chains() -> Any:
    """Get supported chain IDs. (GET /v1/chains)"""
    async with PendleApiClient.from_env() as client:
        return await client.get_chains()


@mcp.tool()
async def pendle_get_markets_all(
    *,
    chain_id: int | None = None,
    ids: list[str] | None = None,
    is_active: bool | None = None,
    order_by: str | None = None,
    skip: int | None = None,
    limit: int | None = None,
) -> Any:
    """Get whitelisted markets list with metadata across chains. (GET /v2/markets/all)

    Returns a paginated response: `{total, limit, skip, results: [...]}`.

    Notes:
    - `ids` items should be market IDs in the form `<chainId>-<address>` (e.g. `1-0x...`,
      `8453-0x...`). Passing a raw address may return an error or empty results.
    - Default API page size is 20; use `skip` and `limit` to page through `total`.
    """
    async with PendleApiClient.from_env() as client:
        return await client.get_markets_all(
            chain_id=chain_id,
            ids=ids,
            is_active=is_active,
            order_by=order_by,
            skip=skip,
            limit=limit,
        )


@mcp.tool()
async def pendle_get_markets_points_market(
    *,
    chain_id: int | None = None,
    is_active: bool | None = None,
) -> Any:
    """Get points market. (GET /v1/markets/points-market)"""
    async with PendleApiClient.from_env() as client:
        return await client.get_markets_points_market(
            chain_id=chain_id,
            is_active=is_active,
        )


async def _fetch_all_markets(
    client: PendleApiClient,
    *,
    chain_id: int,
    is_active: bool = True,
) -> list[Mapping[str, Any]]:
    markets: list[Mapping[str, Any]] = []
    skip = 0
    while True:
        page = await client.get_markets_all(
            chain_id=chain_id,
            is_active=is_active,
            skip=skip,
            limit=_MARKETS_ALL_PAGE_SIZE,
        )
        if not isinstance(page, Mapping):
            break
        results = page.get("results")
        if not isinstance(results, list) or not results:
            break
        markets.extend(row for row in results if isinstance(row, Mapping))
        if len(results) < _MARKETS_ALL_PAGE_SIZE:
            break
        total = page.get("total")
        if isinstance(total, int) and len(markets) >= total:
            break
        skip += len(results)
    return markets


def _prefilter_new_market_opportunity_candidates(
    markets: list[Mapping[str, Any]],
    *,
    now: dt.datetime,
    market_age_days: int,
    min_tvl_usd: float,
) -> tuple[list[Mapping[str, Any]], list[dict[str, Any]]]:
    candidates: list[Mapping[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for market in markets:
        address = _extract_market_address(market)
        name = market.get("name") if isinstance(market.get("name"), str) else None
        created_at = _parse_iso_datetime(market.get("timestamp"))
        age_days = _days_between(now, created_at)
        tvl_usd = _market_tvl_usd(market)
        sy_address = parse_sy_address(market.get("sy"))

        reason: str | None = None
        if address is None:
            reason = "bad_market_address"
        elif created_at is None or age_days is None:
            reason = "missing_or_bad_created_at"
        elif age_days > market_age_days:
            reason = "market_too_old"
        elif tvl_usd is None:
            reason = "missing_tvl"
        elif tvl_usd < min_tvl_usd:
            reason = "tvl_below_threshold"
        elif sy_address is None:
            reason = "missing_or_bad_sy"

        if reason is None:
            candidates.append(market)
        else:
            skipped.append(
                {
                    "market_id": address,
                    "market_name": name,
                    "reason": reason,
                    "market_age_days": age_days,
                    "tvl_usd": tvl_usd,
                }
            )
    return candidates, skipped


async def _build_new_market_opportunity_row(
    market: Mapping[str, Any],
    *,
    chain_id: int,
    now: dt.datetime,
    chain_truth_window_days: int,
    spread_threshold_bps: int,
    implied_discount_threshold_bps: int,
) -> dict[str, Any]:
    address = _extract_market_address(market)
    sy_address = parse_sy_address(market.get("sy"))
    created_at = _parse_iso_datetime(market.get("timestamp"))
    expiry = _parse_iso_datetime(market.get("expiry"))
    u_ui = _market_underlying_apy(market)
    implied = _market_implied_apy(market)
    tvl_usd = _market_tvl_usd(market)

    row: dict[str, Any] = {
        "market_id": address,
        "market_name": market.get("name"),
        "chain_id": chain_id,
        "created_at": created_at.isoformat().replace("+00:00", "Z")
        if created_at is not None
        else None,
        "market_age_days": _days_between(now, created_at),
        "expiry": expiry.isoformat().replace("+00:00", "Z") if expiry is not None else None,
        "ttm_days": _days_until(expiry, now),
        "sy": market.get("sy"),
        "u_ui_pendle": u_ui,
        "implied_apy": implied,
        "tvl_usd": tvl_usd,
        "is_new": market.get("isNew"),
        "is_prime": market.get("isPrime"),
        "is_volatile": market.get("isVolatile"),
        "categories": market.get("categoryIds"),
        "chain_truth_window_days": chain_truth_window_days,
        "spread_threshold_bps": spread_threshold_bps,
        "implied_discount_threshold_bps": implied_discount_threshold_bps,
        "trigger_reasons": [],
    }

    if sy_address is None:
        row["u_chain_long"] = None
        row["chain_truth"] = {
            "value": None,
            "status": "adapter_required",
            "method": "none",
            "confidence": "none",
            "error": f"market sy field unparseable: {market.get('sy')!r}",
        }
        row["chain_truth_status"] = "adapter_required"
        row["chain_truth_method"] = "none"
        row["chain_truth_confidence"] = "none"
        row["chain_truth_error"] = row["chain_truth"]["error"]
        row["is_opportunity"] = False
        return row

    chain_truth = await compute_chain_truth_for_market(
        chain_id=chain_id,
        market=market,
        window_days=chain_truth_window_days,
    )
    row["chain_truth"] = chain_truth.to_dict()
    row["u_chain_long"] = chain_truth.value
    row["chain_truth_status"] = chain_truth.status
    row["chain_truth_method"] = chain_truth.method
    row["chain_truth_confidence"] = chain_truth.confidence
    if chain_truth.status != "ok":
        row["chain_truth_error"] = chain_truth.error or chain_truth.status
        row["is_opportunity"] = False
        return row

    value = chain_truth.value
    if value is None:
        row["chain_truth_error"] = "chain truth returned ok status without a value"
        row["is_opportunity"] = False
        return row
    if value == 0 and u_ui is not None and u_ui > 0.001:
        row["chain_truth"]["status"] = "adapter_required"
        row["chain_truth"]["error"] = (
            "SY accumulator is flat while Pendle UI APY is positive; "
            "protocol-specific adapter required"
        )
        row["chain_truth_status"] = "adapter_required"
        row["chain_truth_error"] = row["chain_truth"]["error"]
        row["is_opportunity"] = False
        return row

    ui_spread = value - u_ui if value is not None and u_ui is not None else None
    implied_discount = value - implied if value is not None and implied is not None else None
    row["ui_spread_bps"] = _bps(ui_spread)
    row["implied_discount_bps"] = _bps(implied_discount)
    row["ui_vs_chain_ratio"] = (u_ui / value) if value and u_ui is not None else None
    row["implied_vs_chain_ratio"] = (implied / value) if value and implied is not None else None

    trigger_reasons: list[str] = []
    if row["ui_spread_bps"] is not None and row["ui_spread_bps"] >= spread_threshold_bps:
        trigger_reasons.append("ui_understates_chain_truth")
    if (
        row["implied_discount_bps"] is not None
        and row["implied_discount_bps"] >= implied_discount_threshold_bps
    ):
        trigger_reasons.append("market_implied_below_chain_truth")
    row["trigger_reasons"] = trigger_reasons
    row["is_opportunity"] = bool(trigger_reasons)
    return row


async def _detect_new_market_opportunities(
    *,
    chain_id: int,
    market_age_days: int,
    chain_truth_window_days: int,
    spread_threshold_bps: int,
    implied_discount_threshold_bps: int,
    min_tvl_usd: float,
    include_non_opportunities: bool,
    calibration_concurrency: int,
) -> dict[str, Any]:
    if market_age_days <= 0:
        raise ValueError(f"market_age_days must be positive; got {market_age_days}.")
    if chain_truth_window_days <= 0:
        raise ValueError(
            f"chain_truth_window_days must be positive; got {chain_truth_window_days}."
        )
    if spread_threshold_bps < 0:
        raise ValueError(f"spread_threshold_bps must be >= 0; got {spread_threshold_bps}.")
    if implied_discount_threshold_bps < 0:
        raise ValueError(
            "implied_discount_threshold_bps must be >= 0; "
            f"got {implied_discount_threshold_bps}."
        )
    if min_tvl_usd < 0:
        raise ValueError(f"min_tvl_usd must be >= 0; got {min_tvl_usd}.")
    if calibration_concurrency <= 0:
        raise ValueError(
            f"calibration_concurrency must be positive; got {calibration_concurrency}."
        )

    now = dt.datetime.now(dt.timezone.utc)
    async with PendleApiClient.from_env() as client:
        markets = await _fetch_all_markets(client, chain_id=chain_id, is_active=True)

    candidates, prefilter_skipped = _prefilter_new_market_opportunity_candidates(
        markets,
        now=now,
        market_age_days=market_age_days,
        min_tvl_usd=min_tvl_usd,
    )
    sem = asyncio.Semaphore(calibration_concurrency)

    async def run_one(market: Mapping[str, Any]) -> dict[str, Any]:
        async with sem:
            return await _build_new_market_opportunity_row(
                market,
                chain_id=chain_id,
                now=now,
                chain_truth_window_days=chain_truth_window_days,
                spread_threshold_bps=spread_threshold_bps,
                implied_discount_threshold_bps=implied_discount_threshold_bps,
            )

    rows = await asyncio.gather(*(run_one(market) for market in candidates))
    opportunities = [row for row in rows if row.get("is_opportunity")]
    opportunities.sort(
        key=lambda row: max(
            row.get("ui_spread_bps") or float("-inf"),
            row.get("implied_discount_bps") or float("-inf"),
        ),
        reverse=True,
    )
    unknown_candidates = [
        row for row in rows if row.get("chain_truth_status") not in {None, "ok"}
    ]
    non_opportunities = [row for row in rows if not row.get("is_opportunity")]

    result: dict[str, Any] = {
        "parameters": {
            "chain_id": chain_id,
            "market_age_days": market_age_days,
            "chain_truth_window_days": chain_truth_window_days,
            "spread_threshold_bps": spread_threshold_bps,
            "implied_discount_threshold_bps": implied_discount_threshold_bps,
            "min_tvl_usd": min_tvl_usd,
        },
        "snapshot_at": now.isoformat().replace("+00:00", "Z"),
        "summary": {
            "markets_scanned": len(markets),
            "prefilter_candidates": len(candidates),
            "opportunities": len(opportunities),
            "chain_truth_errors": sum(1 for row in rows if row.get("chain_truth_error")),
            "unknown_candidates": len(unknown_candidates),
            "prefilter_skipped": len(prefilter_skipped),
        },
        "opportunities": opportunities,
        "unknown_candidates": unknown_candidates,
    }
    if include_non_opportunities:
        result["non_opportunities"] = non_opportunities
        result["prefilter_skipped"] = prefilter_skipped
    return result


@mcp.tool()
async def pendle_detect_new_market_opportunities(
    *,
    chain_id: int = 1,
    market_age_days: int = 30,
    chain_truth_window_days: int = 90,
    spread_threshold_bps: int = 200,
    implied_discount_threshold_bps: int = 50,
    min_tvl_usd: float = 500_000,
    include_non_opportunities: bool = False,
    calibration_concurrency: int = _NEW_MARKET_OPPORTUNITY_DEFAULT_CALIBRATION_CONCURRENCY,
) -> Any:
    """Detect new Pendle markets where longer-window chain truth is not priced in.

    This is a manual scanner, not a cron/alarm tool. It fetches active markets,
    filters to young + liquid markets, computes each candidate's chain-truth
    APY via a protocol-aware adapter registry (default 90d), then flags markets where:

    - `u_chain_long - u_ui_pendle >= spread_threshold_bps`, or
    - `u_chain_long - implied_apy >= implied_discount_threshold_bps`.

    The second trigger is deliberately separate because the savUSD case can
    remain actionable even after Pendle's UI APY catches up: the trade only
    exists if the market's implied APY is still below the protocol forward rate.

    Chain-truth adapters currently include:
    - `sy_accumulator`: historical `SY.exchangeRate()` reads, only after a
      historical-state RPC probe passes.
    - `navoracle_event`: `NavReported` event-log pps ratio for Avalon/NavVault
      markets; this can work on chains where historical eth_call is unavailable
      but event logs are trustworthy.

    Rows that cannot be judged are surfaced in `unknown_candidates` with
    `chain_truth.status` (`untrusted_rpc`, `adapter_required`,
    `insufficient_history`, `contract_revert`) instead of being silently
    interpreted as 0% yield.

    Returns `{parameters, snapshot_at, summary, opportunities, unknown_candidates}`.
    Pass `include_non_opportunities=true` for calibration/debug output.
    """
    return await _detect_new_market_opportunities(
        chain_id=chain_id,
        market_age_days=market_age_days,
        chain_truth_window_days=chain_truth_window_days,
        spread_threshold_bps=spread_threshold_bps,
        implied_discount_threshold_bps=implied_discount_threshold_bps,
        min_tvl_usd=min_tvl_usd,
        include_non_opportunities=include_non_opportunities,
        calibration_concurrency=calibration_concurrency,
    )


@mcp.tool()
async def pendle_get_market_data_v2(
    *,
    chain_id: int,
    address: str,
    timestamp: str | None = None,
) -> Any:
    """Get latest/historical market data by address. (GET /v2/{chainId}/markets/{address}/data)

    APY field semantics — important:
    - `underlyingApy`, `underlyingInterestApy`, `underlyingRewardApy`, `ytFloatingApy`,
      `aggregatedApy`, `maxBoostedApy` and other Pendle-computed APYs are
      **short sliding-window display values**, not on-chain ground truth. Pendle
      annualizes the recent `SY.exchangeRate` change over a few-day window, so
      NAV-discrete / occasional-distribute underlyings get overstated 2-6×
      (e.g. Midas weekly-NAV mHYPER, Superform distribute superUSDC); continuous-
      accrual underlyings (Ethena sUSDe etc.) happen to track ground truth
      within ~15% but this is a property of the underlying, not the field.
    - For ground truth use `u_actual_30d_chain` (always attached below) or call
      `SY.exchangeRate()` yourself across `latest_block` vs the block at
      `latest_ts - 30d` (found via `eth_getBlockByNumber` timestamp bisect, not
      a fixed block-count offset — chain cadence varies).

    Extra fields this tool injects (always present, even when calibration fails):
    - `u_actual_30d_chain`: float | None — on-chain annualized 30d window APY,
      decimal (e.g. 0.0407 for 4.07%). Computed at `latest` block regardless of
      the `timestamp` arg — calibration is "now", not historical.
    - `u_ui_vs_chain_ratio`: float | None — `underlyingApy / u_actual_30d_chain`.
      Diagnostic: 1.0 ≈ UI matches chain; > 1.5 strongly suggests the underlying
      is event-pulsed and UI is in the post-pulse sliding-window tail.
    - `u_actual_chain_error`: str — present only when `u_actual_30d_chain` is
      None; explains why (missing `RPC_URL_<chainId>`, chain too young for a 30d
      window, contract revert, markets/all lookup failure, …).

    Calibration requires an archive RPC at `RPC_URL_<chainId>` env var
    (Etherscan's `module=proxy` eth_call always returns latest state for
    historical blocks, so we deliberately do not fall back to it). The 30d-ago
    block is located via Newton-style cadence estimation + cadence-guided
    bisect on `eth_getBlockByNumber` timestamps — typically 6-10 RPC calls
    (~1-2s wall-clock) on any chain Pendle lists (HyperEVM, Berachain, …)
    without a hardcoded block-time table.

    Caveat: this calibration assumes the SY's yield accrues into
    `SY.exchangeRate()` (the standard interest-bearing model). For SYs that
    instead distribute yield via separate reward tokens (e.g. some HyperEVM
    LST wrappers), `u_actual_30d_chain` may come back at ~0 even when the UI
    `underlyingApy` is non-trivial — that's not a calibration bug, it's the
    SY using a reward-distribution model the `exchangeRate()` accumulator
    doesn't capture.
    """
    market_id = f"{chain_id}-{address}"
    async with PendleApiClient.from_env() as client:
        # markets/all is best-effort — its only job is to give us the SY
        # address for chain calibration. A 429 / 5xx / network blip there
        # must not break the main data response; it becomes a calibration
        # error and gets surfaced via `u_actual_chain_error`.
        data, market_meta = await asyncio.gather(
            client.get_market_data_v2(
                chain_id=chain_id,
                address=address,
                timestamp=timestamp,
            ),
            client.get_markets_all(chain_id=chain_id, ids=[market_id]),
            return_exceptions=True,
        )
    if isinstance(data, BaseException):
        raise data

    return await _attach_chain_calibration(
        data=data,
        chain_id=chain_id,
        market_meta=market_meta,
    )


async def _attach_chain_calibration(
    *,
    data: Any,
    chain_id: int,
    market_meta: Any,
) -> Any:
    """Inject `u_actual_30d_chain` / `u_ui_vs_chain_ratio` (+ optional error) into a
    market data response. Defensive: never raises — surfaces failures via the
    diagnostic string so the caller still gets the Pendle data."""
    if not isinstance(data, dict):
        return data

    sy_address, sy_error = _extract_sy_address(market_meta)
    if sy_address is None:
        data["u_actual_30d_chain"] = None
        data["u_ui_vs_chain_ratio"] = None
        data["u_actual_chain_error"] = sy_error
        return data

    value, error = await compute_u_actual_30d_chain(
        chain_id=chain_id,
        sy_address=sy_address,
    )
    data["u_actual_30d_chain"] = value
    data["u_ui_vs_chain_ratio"] = _compute_ui_vs_chain_ratio(
        ui_value=data.get("underlyingApy"),
        chain_value=value,
    )
    if error is not None:
        data["u_actual_chain_error"] = error
    return data


def _extract_sy_address(market_meta: Any) -> tuple[str | None, str | None]:
    """Pull the SY address out of a `/v2/markets/all` response. Returns
    `(sy_address, error)`. Handles the case where `market_meta` is itself an
    exception (best-effort markets/all call failed in `asyncio.gather`)."""
    if isinstance(market_meta, BaseException):
        if isinstance(market_meta, PendleApiError):
            return None, f"markets/all lookup failed: {market_meta.summary()}"
        return None, (
            f"markets/all lookup failed: {type(market_meta).__name__}: {market_meta}"
        )
    if not isinstance(market_meta, Mapping):
        return None, "markets/all lookup returned unexpected shape"
    results = market_meta.get("results")
    if not isinstance(results, list) or not results:
        return None, "markets/all returned no matching market (sy unavailable)"
    first = results[0]
    if not isinstance(first, Mapping):
        return None, "markets/all result entry has unexpected shape"
    sy_field = first.get("sy")
    sy_address = parse_sy_address(sy_field)
    if sy_address is None:
        return None, f"markets/all sy field unparseable: {sy_field!r}"
    return sy_address, None


def _compute_ui_vs_chain_ratio(*, ui_value: Any, chain_value: float | None) -> float | None:
    if chain_value is None or chain_value == 0:
        return None
    if not isinstance(ui_value, (int, float)):
        return None
    return float(ui_value) / chain_value


@mcp.tool()
async def pendle_get_market_historical_data_v3(
    *,
    chain_id: int,
    address: str,
    time_frame: str | None = None,
    timestamp_start: str | None = None,
    timestamp_end: str | None = None,
    fields: list[str] | None = None,
    include_fee_breakdown: bool | None = None,
    include_apy_breakdown: bool | None = None,
) -> Any:
    """Get market time-series data by address. (GET /v3/{chainId}/markets/{address}/historical-data)

    Notes:
    - `time_frame` accepts `hour`/`day`/`week` and aliases `1h`/`1d`/`1w` (auto-normalized before request).
    - `include_apy_breakdown` (v3 only) attaches APY breakdown sub-fields to the response.

    APY field semantics — important: each time-series row's `underlyingApy` /
    `baseApy` / `ytFloatingApy` / `aggregatedApy` is the same short-sliding-window
    display value Pendle's `/v2/{chainId}/markets/{address}/data` returns at that
    timestamp. It is **not** the chain's ground-truth APY at that point —
    NAV-discrete / occasional-distribute underlyings get overstated 2-6× during
    the post-pulse window. Use this endpoint for UI-shape series; for ground
    truth call `SY.exchangeRate()` directly or use `pendle_get_market_data_v2`'s
    injected `u_actual_30d_chain` (latest-only).
    """
    async with PendleApiClient.from_env() as client:
        return await client.get_market_historical_data_v3(
            chain_id=chain_id,
            address=address,
            time_frame=time_frame,
            timestamp_start=timestamp_start,
            timestamp_end=timestamp_end,
            fields=fields,
            include_fee_breakdown=include_fee_breakdown,
            include_apy_breakdown=include_apy_breakdown,
        )


@mcp.tool()
async def pendle_get_assets_all(
    *,
    ids: list[str] | None = None,
    chain_id: int | None = None,
    skip: int | None = None,
    limit: int | None = None,
    asset_type: PendleAssetType | None = None,
) -> Any:
    """Get supported PT/YT/LP/SY assets metadata. (GET /v1/assets/all)

    Notes:
    - `ids` items should be asset IDs in the form `<chainId>-<address>` (e.g. `1-0x...`,
      `8453-0x...`). Passing a raw address may return an error or empty results.
    """
    async with PendleApiClient.from_env() as client:
        return await client.get_assets_all(
            ids=ids,
            chain_id=chain_id,
            skip=skip,
            limit=limit,
            asset_type=asset_type,
        )


@mcp.tool()
async def pendle_get_asset_prices(
    *,
    ids: list[str] | None = None,
    chain_id: int | None = None,
    skip: int | None = None,
    limit: int | None = None,
    asset_type: PendleAssetType | None = None,
) -> Any:
    """Get USD prices for assets. (GET /v1/prices/assets)

    Notes:
    - `ids` items should be asset IDs in the form `<chainId>-<address>` (e.g. `1-0x...`,
      `8453-0x...`). Passing a raw address may return an error or empty results.
    """
    async with PendleApiClient.from_env() as client:
        return await client.get_asset_prices(
            ids=ids,
            chain_id=chain_id,
            skip=skip,
            limit=limit,
            asset_type=asset_type,
        )


@mcp.tool()
async def pendle_get_prices_ohlcv_v4(
    *,
    chain_id: int,
    address: str,
    time_frame: str | None = None,
    timestamp_start: str | None = None,
    timestamp_end: str | None = None,
    parse_results: bool = False,
) -> Any:
    """Get PT / YT / LP historical price by address. (GET /v4/{chainId}/prices/{address}/ohlcv)

    Notes:
    - `time_frame` accepts `hour`/`day`/`week` and aliases `1h`/`1d`/`1w` (auto-normalized before request).
    - If `parse_results=true` and response has `results` as a CSV string, returns `results_parsed` as an array of
      `{time, open, high, low, close, volume}` string fields (keeps original `results` unchanged).
    """
    async with PendleApiClient.from_env() as client:
        data = await client.get_prices_ohlcv_v4(
            chain_id=chain_id,
            address=address,
            time_frame=time_frame,
            timestamp_start=timestamp_start,
            timestamp_end=timestamp_end,
        )
        if not parse_results:
            return data

        if not isinstance(data, Mapping):
            return {
                "raw": data,
                "results_parsed": None,
                "parse_error": "Unexpected response type; expected object.",
            }

        results = data.get("results")
        if not isinstance(results, str):
            return {
                **data,
                "results_parsed": None,
                "parse_error": "Response missing 'results' CSV string.",
            }

        try:
            parsed = _parse_ohlcv_results_csv(results)
        except Exception as e:
            return {
                **data,
                "results_parsed": None,
                "parse_error": f"{type(e).__name__}: {e}",
            }

        return {**data, "results_parsed": parsed, "parse_error": None}


@mcp.tool()
async def pendle_get_user_pnl_transactions(
    *,
    user: str,
    skip: int | None = None,
    limit: int | None = None,
    chain_id: int | None = None,
    market: str | None = None,
) -> Any:
    """Get user transactions by address. (GET /v1/pnl/transactions)

    Pure paginator: returns the raw API response `{total, results: [...]}`. Use
    `skip` / `limit` to page through `total`. For aggregated PnL across the
    user's full history, use `pendle_get_user_pnl_summary` instead — that tool
    scans every page and emits per-action / per-tx_hash summaries with totals.
    """
    async with PendleApiClient.from_env() as client:
        return await client.get_user_pnl_transactions(
            user=user,
            skip=skip,
            limit=limit,
            chain_id=chain_id,
            market=market,
        )


@mcp.tool()
async def pendle_get_user_pnl_summary(
    *,
    user: str,
    chain_id: int | None = None,
    market: str | None = None,
    group_by: str = "action",
    page_size: int | None = None,
) -> Any:
    """Aggregate PnL across the user's full history. (scans GET /v1/pnl/transactions)

    Scans every page (no soft cap, so the result is always complete) and
    aggregates by `action` (default) or `tx_hash`. Returns:

        {user, chainId, market, groupBy, scanned, pagesFetched,
         totalProfit{Usd,Asset,Eth}, totalTxValueAsset, groups}

    `total*` are summed across `scanned` rows — and `scanned` always equals the
    address's full PnL row count, since this tool refuses to truncate. Spent is
    omitted at the top level (pt+yt+lp legs aren't a single signed quantity);
    pull `groups[*].spent*` for capital-adjusted ROI.

    Each group carries `count`, summed `profit{Usd,Asset,Eth}`, summed
    `spent{Usd,Asset,Eth}` (pt+yt+lp legs combined), `txValueAsset`, plus
    mode-specific fields (`action`: `chainIds` / `markets`; `tx_hash`:
    `actions` / `chainId` / `market` / `timestamp`).

    Safety: raises ValueError if the address has more than
    `_PNL_SUMMARY_HARD_CAP_ROWS` (≈10000) rows — bypass via the raw
    `pendle_get_user_pnl_transactions` paginator with explicit `skip` / `limit`
    bounds.
    """
    if group_by not in _PNL_GROUP_BY_ALLOWED:
        raise ValueError(
            "group_by must be one of action / tx_hash. "
            f"Invalid group_by={group_by!r}."
        )
    page_size_eff = page_size if page_size is not None else _PNL_SUMMARY_DEFAULT_PAGE_SIZE
    if page_size_eff <= 0:
        raise ValueError(f"page_size must be positive; got {page_size_eff}.")

    rows: list[Mapping[str, Any]] = []
    offset = 0
    pages_fetched = 0
    async with PendleApiClient.from_env() as client:
        while True:
            page = await client.get_user_pnl_transactions(
                user=user,
                skip=offset,
                limit=page_size_eff,
                chain_id=chain_id,
                market=market,
            )
            pages_fetched += 1
            if not isinstance(page, Mapping):
                break
            results = page.get("results")
            if not isinstance(results, list) or not results:
                break
            rows.extend(results)
            if len(rows) > _PNL_SUMMARY_HARD_CAP_ROWS:
                raise ValueError(
                    f"user {user!r} has more than {_PNL_SUMMARY_HARD_CAP_ROWS} "
                    "PnL rows; the summary refuses to truncate. Fetch the raw "
                    "rows in batches via pendle_get_user_pnl_transactions("
                    "user=..., skip=..., limit=...) and aggregate client-side."
                )
            if len(results) < page_size_eff:
                break
            api_total = page.get("total")
            if isinstance(api_total, int) and len(rows) >= api_total:
                break
            offset += len(results)

    return {
        "user": user,
        "chainId": chain_id,
        "market": market,
        "groupBy": group_by,
        "scanned": len(rows),
        "pagesFetched": pages_fetched,
        **_totals_from_pnl_rows(rows),
        "groups": _aggregate_pnl_rows(rows, group_by),
    }


@mcp.tool()
async def pendle_get_market_transactions_v5(
    *,
    chain_id: int,
    address: str,
    transaction_type: TransactionType | None = None,
    min_value: float | None = None,
    tx_origin: str | None = None,
    action: TransactionAction | None = None,
    resume_token: str | None = None,
    limit: int | None = None,
    skip: int | None = None,
) -> Any:
    """Get market transactions by address. (GET /v5/{chainId}/transactions/{address})"""
    async with PendleApiClient.from_env() as client:
        return await client.get_market_transactions_v5(
            chain_id=chain_id,
            address=address,
            transaction_type=transaction_type,
            min_value=min_value,
            tx_origin=tx_origin,
            action=action,
            resume_token=resume_token,
            limit=limit,
            skip=skip,
        )


@mcp.tool()
async def pendle_get_user_positions(
    *,
    user: str,
    filter_usd: float | None = None,
) -> Any:
    """Get user positions by address. (GET /v1/dashboard/positions/database/{user})"""
    async with PendleApiClient.from_env() as client:
        return await client.get_user_positions(user=user, filter_usd=filter_usd)


@mcp.tool()
async def pendle_get_merkle_rewards(*, user: str) -> Any:
    """Get pending and claimed merkle rewards for a user. (GET /v1/dashboard/merkle-rewards/{user})

    Returns `{claimableRewards, claimedRewards}`: claimable items are not yet claimed,
    claimed items are historical records.
    """
    async with PendleApiClient.from_env() as client:
        return await client.get_merkle_rewards(user=user)


@mcp.tool()
async def pendle_get_spendle_data() -> Any:
    """Get aggregate sPENDLE staking data. (GET /v1/spendle/data)

    Returns total PENDLE staked, last-epoch APR, buyback amount, and historical breakdowns
    spanning sPENDLE and legacy vePENDLE positions over the last 12 epochs.
    """
    async with PendleApiClient.from_env() as client:
        return await client.get_spendle_data()


@mcp.tool()
async def pendle_get_user_pnl_gained_positions(*, user: str) -> Any:
    """Get a user's gained PnL across all market positions. (GET /v1/pnl/gained/{user}/positions)

    Returns `{total, positions}`: each position carries net gain, total spent, max capital,
    trading volume, and unclaimed rewards.
    """
    async with PendleApiClient.from_env() as client:
        return await client.get_user_pnl_gained_positions(user=user)


@mcp.tool()
async def pendle_get_limit_orders_all_v2(
    *,
    chain_id: int | None = None,
    limit: int | None = None,
    maker: str | None = None,
    yt: str | None = None,
    timestamp_start: str | None = None,
    timestamp_end: str | None = None,
    resume_token: str | None = None,
) -> Any:
    """Get all limit orders for analytics. (GET /v2/limit-orders)"""
    async with PendleApiClient.from_env() as client:
        return await client.get_limit_orders_all_v2(
            chain_id=chain_id,
            limit=limit,
            maker=maker,
            yt=yt,
            timestamp_start=timestamp_start,
            timestamp_end=timestamp_end,
            resume_token=resume_token,
        )


@mcp.tool()
async def pendle_get_limit_orders_archived_v2(
    *,
    chain_id: int | None = None,
    limit: int | None = None,
    maker: str | None = None,
    yt: str | None = None,
    timestamp_start: str | None = None,
    timestamp_end: str | None = None,
    resume_token: str | None = None,
) -> Any:
    """Get all archived limit orders for analytics. (GET /v2/limit-orders/archived)"""
    async with PendleApiClient.from_env() as client:
        return await client.get_limit_orders_archived_v2(
            chain_id=chain_id,
            limit=limit,
            maker=maker,
            yt=yt,
            timestamp_start=timestamp_start,
            timestamp_end=timestamp_end,
            resume_token=resume_token,
        )


@mcp.tool()
async def pendle_get_limit_orders_book_v2(
    *,
    chain_id: int,
    precision_decimal: int,
    market: str,
    limit: int | None = None,
    include_amm: bool | None = None,
) -> Any:
    """Get order book v2. (GET /v2/limit-orders/book/{chainId})"""
    async with PendleApiClient.from_env() as client:
        return await client.get_limit_orders_book_v2(
            chain_id=chain_id,
            precision_decimal=precision_decimal,
            market=market,
            limit=limit,
            include_amm=include_amm,
        )


@mcp.tool()
async def pendle_get_limit_orders_maker_limit_orders(
    *,
    chain_id: int,
    maker: str,
    skip: int | None = None,
    limit: int | None = None,
    yt: str | None = None,
    order_type: int | None = None,
    is_active: bool | None = None,
) -> Any:
    """Get user limit orders in market. (GET /v1/limit-orders/makers/limit-orders)"""
    async with PendleApiClient.from_env() as client:
        return await client.get_limit_orders_maker_limit_orders(
            chain_id=chain_id,
            maker=maker,
            skip=skip,
            limit=limit,
            yt=yt,
            order_type=order_type,
            is_active=is_active,
        )


@mcp.tool()
async def pendle_get_limit_orders_taker_limit_orders(
    *,
    chain_id: int,
    yt: str,
    order_type: int,
    skip: int | None = None,
    limit: int | None = None,
    sort_by: str | None = None,
    sort_order: str | None = None,
) -> Any:
    """Get limit orders to match by YT address. (GET /v1/limit-orders/takers/limit-orders)"""
    async with PendleApiClient.from_env() as client:
        return await client.get_limit_orders_taker_limit_orders(
            chain_id=chain_id,
            yt=yt,
            order_type=order_type,
            skip=skip,
            limit=limit,
            sort_by=sort_by,
            sort_order=sort_order,
        )


@mcp.tool()
async def pendle_get_supported_aggregators(*, chain_id: int) -> Any:
    """Get supported aggregators for a chain. (GET /v1/sdk/{chainId}/supported-aggregators)"""
    async with PendleApiClient.from_env() as client:
        return await client.get_supported_aggregators(chain_id=chain_id)


@mcp.tool()
async def pendle_get_market_tokens(*, chain_id: int, market: str) -> Any:
    """Get supported tokens for market. (GET /v1/sdk/{chainId}/markets/{market}/tokens)"""
    async with PendleApiClient.from_env() as client:
        return await client.get_market_tokens(chain_id=chain_id, market=market)


@mcp.tool()
async def pendle_get_swapping_prices(*, chain_id: int, market: str) -> Any:
    """Get real-time PT/YT swap price of a market. (GET /v1/sdk/{chainId}/markets/{market}/swapping-prices)"""
    async with PendleApiClient.from_env() as client:
        return await client.get_swapping_prices(chain_id=chain_id, market=market)


@mcp.tool()
async def pendle_get_pt_cross_chain_metadata(*, chain_id: int, pt: str) -> Any:
    """PT cross-chain metadata. (GET /v1/sdk/{chainId}/cross-chain-pt-metadata/{pt})"""
    async with PendleApiClient.from_env() as client:
        return await client.get_pt_cross_chain_metadata(chain_id=chain_id, pt=pt)


def _strip_convert_v2_tx_fields(response: Any) -> Any:
    """Drop tx calldata and encoded contract params from a convert v2 response.

    Removes per route:
    - `tx` (contains `tx.data` calldata, dominant size cost, and grows large for
      aggregator routes)
    - `contractParamInfo.contractCallParams` (encoded args, useless without tx
      execution)

    Keeps `contractParamInfo.method` / `contractParamInfo.contractCallParamsName`,
    `outputs`, `data`, and all top-level fields — that's what scanning callers
    need (amountOut, method, aggregatorType, priceImpact, fee, route count).
    """
    if not isinstance(response, dict):
        return response
    routes = response.get("routes")
    if not isinstance(routes, list):
        return response
    for route in routes:
        if not isinstance(route, dict):
            continue
        route.pop("tx", None)
        info = route.get("contractParamInfo")
        if isinstance(info, dict):
            info.pop("contractCallParams", None)
    return response


@mcp.tool()
async def pendle_convert_v2(
    *,
    chain_id: int,
    slippage: float,
    tokens_in: list[str],
    amounts_in: list[str],
    tokens_out: list[str],
    receiver: str | None = None,
    enable_aggregator: bool | None = None,
    aggregators: list[str] | None = None,
    redeem_rewards: bool | None = None,
    need_scale: bool | None = None,
    additional_data: str | None = None,
    use_limit_order: bool | None = None,
    include_tx: bool = False,
) -> Any:
    """Universal convert function. (GET /v2/sdk/{chainId}/convert)

    Parameter semantics:
    - `slippage` is a fraction (e.g. 0.5% -> 0.005; 50% -> 0.5).
    - `amounts_in` MUST be base-10 integer strings in the input token's smallest unit (e.g. wei).
      Do not pass decimals like `"0.001"`.
      Example (decimals=18): `0.001 * 10**18 = 1000000000000000` => `"1000000000000000"`.
    - `need_scale` is forwarded to the Pendle API. It does NOT auto-convert human-readable decimals
      in `amounts_in` to smallest-unit integers.
    - `include_tx` (default False) — when False, drop `routes[].tx` (calldata) and
      `routes[].contractParamInfo.contractCallParams` (encoded args) from the response.
      Set True only when you actually need to broadcast the tx; leaving it False
      keeps scanning / quoting responses small.
    """
    async with PendleApiClient.from_env() as client:
        response = await client.convert_v2(
            chain_id=chain_id,
            slippage=slippage,
            tokens_in=tokens_in,
            amounts_in=amounts_in,
            tokens_out=tokens_out,
            receiver=receiver,
            enable_aggregator=enable_aggregator,
            aggregators=aggregators,
            redeem_rewards=redeem_rewards,
            need_scale=need_scale,
            additional_data=additional_data,
            use_limit_order=use_limit_order,
        )
    if not include_tx:
        response = _strip_convert_v2_tx_fields(response)
    return response


@mcp.tool()
async def pendle_get_ve_pendle_data_v2() -> Any:
    """Get vePENDLE data. (GET /v2/ve-pendle/data)"""
    async with PendleApiClient.from_env() as client:
        return await client.get_ve_pendle_data_v2()


@mcp.tool()
async def pendle_get_ve_pendle_market_fees_chart(
    *,
    timestamp_start: str | None = None,
    timestamp_end: str | None = None,
) -> Any:
    """Get vePENDLE market fees chart. (GET /v1/ve-pendle/market-fees-chart)"""
    async with PendleApiClient.from_env() as client:
        return await client.get_ve_pendle_market_fees_chart(
            timestamp_start=timestamp_start,
            timestamp_end=timestamp_end,
        )


@mcp.tool()
async def pendle_get_distinct_user_from_token(
    *,
    token: str,
    chain_id: int | None = None,
) -> Any:
    """Get distinct user count from token. (GET /v1/statistics/get-distinct-user-from-token)"""
    async with PendleApiClient.from_env() as client:
        return await client.get_distinct_user_from_token(
            token=token,
            chain_id=chain_id,
        )


@mcp.tool()
async def pendle_health(
    *,
    chain_id: int | None = None,
    market_address: str | None = None,
    asset_address: str | None = None,
    time_frame: str | None = None,
    timestamp_start: str | None = None,
    timestamp_end: str | None = None,
) -> Any:
    """Health check Pendle API endpoints and show degraded status.

    By default, checks only endpoints that do not require parameters. Provide `chain_id` and
    addresses to check market/price endpoints as well.
    """

    async def run_check(name: str, coro: Any) -> dict[str, Any]:
        start = time.perf_counter()
        try:
            await coro
            ok = True
            error: str | None = None
        except PendleApiError as e:
            ok = False
            error = e.summary()
        except Exception as e:  # pragma: no cover - defensive
            ok = False
            error = f"{type(e).__name__}: {e}"
        latency_ms = int((time.perf_counter() - start) * 1000)
        return {"name": name, "ok": ok, "latency_ms": latency_ms, "error": error}

    async with PendleApiClient.from_env() as client:
        checks: dict[str, Any] = {
            "v1/chains": client.get_chains(),
            "v2/ve-pendle/data": client.get_ve_pendle_data_v2(),
            "v1/spendle/data": client.get_spendle_data(),
        }

        if chain_id is not None and market_address is not None:
            checks["v2/markets/data"] = client.get_market_data_v2(
                chain_id=chain_id, address=market_address
            )
            checks["v1/sdk/markets/tokens"] = client.get_market_tokens(
                chain_id=chain_id, market=market_address
            )
            checks["v1/sdk/markets/swapping-prices"] = client.get_swapping_prices(
                chain_id=chain_id, market=market_address
            )
            checks["v3/markets/historical-data"] = client.get_market_historical_data_v3(
                chain_id=chain_id,
                address=market_address,
                time_frame=time_frame,
                timestamp_start=timestamp_start,
                timestamp_end=timestamp_end,
            )

        if chain_id is not None and asset_address is not None:
            checks["v4/prices/ohlcv"] = client.get_prices_ohlcv_v4(
                chain_id=chain_id,
                address=asset_address,
                time_frame=time_frame,
                timestamp_start=timestamp_start,
                timestamp_end=timestamp_end,
            )

        results = await asyncio.gather(
            *(run_check(name, coro) for name, coro in checks.items())
        )

        results.sort(key=lambda item: item["name"])
        return {
            "checks": results,
            "base_url": str(client._client.base_url),
        }


def run() -> None:
    mcp.run()
