from __future__ import annotations

import asyncio
import csv
import io
import time
from typing import Any, Mapping

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


def _coerce_float(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    return 0.0


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


@mcp.tool()
async def pendle_get_market_data_v2(
    *,
    chain_id: int,
    address: str,
    timestamp: str | None = None,
) -> Any:
    """Get latest/historical market data by address. (GET /v2/{chainId}/markets/{address}/data)"""
    async with PendleApiClient.from_env() as client:
        return await client.get_market_data_v2(
            chain_id=chain_id,
            address=address,
            timestamp=timestamp,
        )


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
