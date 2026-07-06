"""`pendle` CLI — invoke pendle-mcp tool kernels directly, no MCP client needed.

Each MCP tool `pendle_<name>` maps to a subcommand `<name>` with underscores
turned into hyphens (e.g. `pendle_get_market_data_v2` -> `get-market-data-v2`).
Tool keyword parameters map to `--kebab-case` flags; list parameters take JSON
array strings. The CLI imports the same async tool functions the MCP server
exposes, so it exercises the full tool path: argument validation, API client,
response post-processing — everything except the FastMCP transport layer.

The legacy `list` / `show` / `call` generic subcommands are kept for
introspection and ad-hoc JSON-kwargs invocation.
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import sys
from typing import Any, Callable

from pendle_mcp import server
from pendle_mcp.pendle_api import (
    PendleApiError,
    PendleAssetType,
    TransactionAction,
    TransactionType,
)

_ENV_EPILOG = """\
environment:
  PENDLE_API_BASE_URL                Pendle API base URL (default https://api-v2.pendle.finance/core).
  PENDLE_API_TIMEOUT_SECONDS         per-request timeout in seconds (default 20).
  PENDLE_API_MAX_RETRIES             max retries on network errors / 5xx / 429 (default 3).
  PENDLE_API_RETRY_BACKOFF_SECONDS   exponential backoff base + jitter (default 0.2).
  PENDLE_API_MAX_CONCURRENCY         process-wide outbound concurrency cap (default 4).
  PENDLE_API_ERROR_DETAIL_MAX_CHARS  error `detail` truncation limit (default 2048).
  RPC_URL_<chainid>                  archive JSON-RPC endpoint for on-chain APY calibration
                                     (e.g. RPC_URL_1). Comma-separated fallback list supported.

Full variable list and parameter semantics: README.md in the repo root.
All commands print JSON to stdout; errors go to stderr with exit code 1
(unknown tool / bad arguments exit 2).
"""

_TIME_FRAMES = ["hour", "day", "week", "1h", "1d", "1w"]


# ---------------------------------------------------------------------------
# argument helpers
# ---------------------------------------------------------------------------


def _json_str_array(name: str) -> Callable[[str], list[str]]:
    def parse(raw: str) -> list[str]:
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise argparse.ArgumentTypeError(
                f"{name} must be valid JSON: {exc}"
            ) from exc
        if not isinstance(value, list) or not all(
            isinstance(item, str) for item in value
        ):
            raise argparse.ArgumentTypeError(
                f"{name} must be a JSON array of strings, e.g. '[\"1-0x...\"]'"
            )
        return value

    return parse


def _add_paging(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--skip", type=int, help="Rows to skip (offset).")
    parser.add_argument("--limit", type=int, help="Max rows to return.")


def _add_tristate_flag(
    parser: argparse.ArgumentParser, flag: str, help_text: str
) -> None:
    """Add --flag / --no-flag pair; unset means 'do not send the parameter'."""
    parser.add_argument(
        flag,
        action=argparse.BooleanOptionalAction,
        default=None,
        help=help_text,
    )


def _enum_or_none(enum_cls: Any, value: str | None) -> Any:
    return enum_cls(value) if value is not None else None


# ---------------------------------------------------------------------------
# output / error rendering
# ---------------------------------------------------------------------------


def _format_pendle_api_error(err: PendleApiError) -> dict[str, Any]:
    return {
        "error": "PendleApiError",
        "message": str(err),
        "error_type": err.error_type,
        "status_code": err.status_code,
        "method": err.method,
        "path": err.path,
        "params": dict(err.params) if err.params else None,
        "attempts": err.attempts,
        "retries_exhausted": err.retries_exhausted,
        "url": err.url,
        "detail": err.detail,
    }


def _run_tool(coro: Any) -> int:
    try:
        result = asyncio.run(coro)
    except PendleApiError as e:
        print(
            json.dumps(_format_pendle_api_error(e), indent=2, default=str),
            file=sys.stderr,
        )
        return 1
    except Exception as e:  # noqa: BLE001 - CLI boundary
        print(f"Error: {e}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    return 0


def _cmd_tool(args: argparse.Namespace) -> int:
    return _run_tool(args.run(args))


# ---------------------------------------------------------------------------
# legacy generic commands (list / show / call)
# ---------------------------------------------------------------------------


def _discover_tools() -> dict[str, Callable[..., Any]]:
    return {
        name: obj
        for name, obj in vars(server).items()
        if name.startswith("pendle_") and inspect.iscoroutinefunction(obj)
    }


def _format_annotation(annotation: Any) -> str:
    if annotation is inspect.Parameter.empty:
        return "Any"
    return inspect.formatannotation(annotation)


def _format_signature(func: Callable[..., Any]) -> str:
    sig = inspect.signature(func)
    lines: list[str] = []
    for param in sig.parameters.values():
        if param.kind != inspect.Parameter.KEYWORD_ONLY:
            continue
        ann = _format_annotation(param.annotation)
        default = (
            "" if param.default is inspect.Parameter.empty else f" = {param.default!r}"
        )
        lines.append(f"  {param.name}: {ann}{default}")
    return "\n".join(lines) if lines else "  (no parameters)"


def _read_json_args(args_json: str | None) -> dict[str, Any]:
    if args_json is None or args_json == "-":
        text = sys.stdin.read()
    else:
        text = args_json
    if not text.strip():
        return {}
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError(
            f"args must be a JSON object (got {type(parsed).__name__})"
        )
    return parsed


def cmd_list(_args: argparse.Namespace) -> int:
    for name in sorted(_discover_tools()):
        print(name)
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    tools = _discover_tools()
    func = tools.get(args.tool)
    if func is None:
        print(f"unknown tool: {args.tool}", file=sys.stderr)
        return 2
    print(args.tool)
    if func.__doc__:
        print()
        print(inspect.cleandoc(func.__doc__))
    print()
    print("parameters (all keyword-only):")
    print(_format_signature(func))
    return 0


def cmd_call(args: argparse.Namespace) -> int:
    tools = _discover_tools()
    func = tools.get(args.tool)
    if func is None:
        print(f"unknown tool: {args.tool}", file=sys.stderr)
        return 2
    try:
        kwargs = _read_json_args(args.json)
    except (json.JSONDecodeError, ValueError) as e:
        print(f"invalid JSON args: {e}", file=sys.stderr)
        return 2
    try:
        result = asyncio.run(func(**kwargs))
    except PendleApiError as e:
        print(
            json.dumps(_format_pendle_api_error(e), indent=2, default=str),
            file=sys.stderr,
        )
        return 1
    except TypeError as e:
        print(f"argument error: {e}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    return 0


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pendle",
        description=(
            "Pendle official API v2 read-only CLI: chains, markets, assets, "
            "prices/OHLCV, user positions & PnL, limit orders, vePENDLE/sPENDLE, "
            "swap quoting (convert), and new-market opportunity scanning. "
            "Read-only: never signs or broadcasts transactions."
        ),
        epilog=_ENV_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # -- Chains / Health ----------------------------------------------------

    p = sub.add_parser(
        "get-chains",
        help="List supported chain IDs",
        description="Get supported chain IDs. (GET /v1/chains)",
    )
    p.set_defaults(func=_cmd_tool, run=lambda a: server.pendle_get_chains())

    p = sub.add_parser(
        "health",
        help="Health-check Pendle API endpoints",
        description=(
            "Health-check Pendle API endpoints and show degraded status. By default only "
            "parameterless endpoints are checked; pass --chain-id plus --market-address / "
            "--asset-address to also check market data / tokens / swapping-prices / "
            "historical-data / OHLCV endpoints."
        ),
    )
    p.add_argument("--chain-id", type=int, help="Chain ID for market/asset checks.")
    p.add_argument("--market-address", help="Market address for market endpoint checks.")
    p.add_argument("--asset-address", help="Asset address for the OHLCV check.")
    p.add_argument("--time-frame", choices=_TIME_FRAMES, help="Time frame for series checks.")
    p.add_argument("--timestamp-start", help="Series start timestamp (ISO8601).")
    p.add_argument("--timestamp-end", help="Series end timestamp (ISO8601).")
    p.set_defaults(
        func=_cmd_tool,
        run=lambda a: server.pendle_health(
            chain_id=a.chain_id,
            market_address=a.market_address,
            asset_address=a.asset_address,
            time_frame=a.time_frame,
            timestamp_start=a.timestamp_start,
            timestamp_end=a.timestamp_end,
        ),
    )

    # -- Markets ------------------------------------------------------------

    p = sub.add_parser(
        "get-markets-all",
        help="List whitelisted markets (paginated v2)",
        description=(
            "Get whitelisted markets list with metadata across chains. (GET /v2/markets/all)\n"
            "Returns {total, limit, skip, results}; API default page size is 20.\n"
            "`--ids` items must be `<chainId>-<address>` (e.g. '[\"1-0x...\"]'), not bare addresses.\n"
            "Example:\n"
            "  pendle get-markets-all --chain-id 1 --is-active --order-by 'totalTvl:-1' --limit 10"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--chain-id", type=int, help="Filter by chain ID.")
    p.add_argument(
        "--ids",
        type=_json_str_array("--ids"),
        help="JSON array of market IDs `<chainId>-<address>`, e.g. '[\"1-0x...\"]'.",
    )
    _add_tristate_flag(p, "--is-active", "Filter to active (or --no-is-active: inactive) markets.")
    p.add_argument("--order-by", help="Sort key, e.g. 'totalTvl:-1'.")
    _add_paging(p)
    p.set_defaults(
        func=_cmd_tool,
        run=lambda a: server.pendle_get_markets_all(
            chain_id=a.chain_id,
            ids=a.ids,
            is_active=a.is_active,
            order_by=a.order_by,
            skip=a.skip,
            limit=a.limit,
        ),
    )

    p = sub.add_parser(
        "get-markets-points-market",
        help="List points markets",
        description="Get points market. (GET /v1/markets/points-market)",
    )
    p.add_argument("--chain-id", type=int, help="Filter by chain ID.")
    _add_tristate_flag(p, "--is-active", "Filter to active (or --no-is-active: inactive) markets.")
    p.set_defaults(
        func=_cmd_tool,
        run=lambda a: server.pendle_get_markets_points_market(
            chain_id=a.chain_id,
            is_active=a.is_active,
        ),
    )

    p = sub.add_parser(
        "get-market-data-v2",
        help="Market data by address, with on-chain 30d APY calibration",
        description=(
            "Get latest/historical market data by address. (GET /v2/{chainId}/markets/{address}/data)\n"
            "Pendle APY fields (underlyingApy etc.) are short sliding-window display values, not\n"
            "chain ground truth. The response always carries `u_actual_30d_chain` (on-chain 30d\n"
            "annualized APY at the current latest block; needs RPC_URL_<chainid>, archive node),\n"
            "`u_ui_vs_chain_ratio`, and `u_actual_chain_error` when calibration failed.\n"
            "Example:\n"
            "  pendle get-market-data-v2 --chain-id 1 --address 0x..."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--chain-id", type=int, required=True, help="Chain ID.")
    p.add_argument("--address", required=True, help="Market address (0x-prefixed).")
    p.add_argument("--timestamp", help="Historical timestamp (ISO8601); omit for latest.")
    p.set_defaults(
        func=_cmd_tool,
        run=lambda a: server.pendle_get_market_data_v2(
            chain_id=a.chain_id,
            address=a.address,
            timestamp=a.timestamp,
        ),
    )

    p = sub.add_parser(
        "get-market-historical-data-v3",
        help="Market time-series data by address",
        description=(
            "Get market time-series data by address. (GET /v3/{chainId}/markets/{address}/historical-data)\n"
            "`--time-frame` accepts hour/day/week and aliases 1h/1d/1w. Per-row APY fields are the\n"
            "same sliding-window display values as the UI — not chain ground truth."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--chain-id", type=int, required=True, help="Chain ID.")
    p.add_argument("--address", required=True, help="Market address (0x-prefixed).")
    p.add_argument("--time-frame", choices=_TIME_FRAMES, help="hour/day/week (aliases 1h/1d/1w).")
    p.add_argument("--timestamp-start", help="Series start timestamp (ISO8601).")
    p.add_argument("--timestamp-end", help="Series end timestamp (ISO8601).")
    p.add_argument(
        "--fields",
        type=_json_str_array("--fields"),
        help="JSON array of field names to return, e.g. '[\"impliedApy\",\"tvl\"]'.",
    )
    _add_tristate_flag(p, "--include-fee-breakdown", "Attach fee breakdown sub-fields.")
    _add_tristate_flag(p, "--include-apy-breakdown", "Attach APY breakdown sub-fields (v3 only).")
    p.set_defaults(
        func=_cmd_tool,
        run=lambda a: server.pendle_get_market_historical_data_v3(
            chain_id=a.chain_id,
            address=a.address,
            time_frame=a.time_frame,
            timestamp_start=a.timestamp_start,
            timestamp_end=a.timestamp_end,
            fields=a.fields,
            include_fee_breakdown=a.include_fee_breakdown,
            include_apy_breakdown=a.include_apy_breakdown,
        ),
    )

    p = sub.add_parser(
        "detect-new-market-opportunities",
        help="Scan young markets where chain-truth APY is not priced in",
        description=(
            "Detect new Pendle markets where longer-window chain-truth APY is not priced in.\n"
            "Manual scanner: fetches active markets, filters young + liquid ones, computes\n"
            "chain-truth APY via protocol-aware adapters (needs RPC_URL_<chainid>, archive node),\n"
            "then flags rows by UI spread / implied discount thresholds. Unjudgeable rows land in\n"
            "`unknown_candidates` with a chain_truth.status instead of silently reading as 0%.\n"
            "Example:\n"
            "  pendle detect-new-market-opportunities --chain-id 1 --min-tvl-usd 250000"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--chain-id", type=int, default=1, help="Chain ID to scan (default 1).")
    p.add_argument("--market-age-days", type=int, default=30, help="Max market age in days (default 30).")
    p.add_argument(
        "--chain-truth-window-days", type=int, default=90,
        help="Chain-truth APY window in days (default 90).",
    )
    p.add_argument(
        "--spread-threshold-bps", type=int, default=200,
        help="Trigger when chain truth exceeds UI APY by this many bps (default 200).",
    )
    p.add_argument(
        "--implied-discount-threshold-bps", type=int, default=50,
        help="Trigger when chain truth exceeds implied APY by this many bps (default 50).",
    )
    p.add_argument("--min-tvl-usd", type=float, default=500_000, help="Min market TVL in USD (default 500000).")
    p.add_argument(
        "--include-non-opportunities", action="store_true",
        help="Also return non-triggering rows and prefilter skip reasons (calibration/debug).",
    )
    p.add_argument(
        "--calibration-concurrency", type=int, default=2,
        help="Concurrent archive-RPC calibrations (default 2).",
    )
    p.set_defaults(
        func=_cmd_tool,
        run=lambda a: server.pendle_detect_new_market_opportunities(
            chain_id=a.chain_id,
            market_age_days=a.market_age_days,
            chain_truth_window_days=a.chain_truth_window_days,
            spread_threshold_bps=a.spread_threshold_bps,
            implied_discount_threshold_bps=a.implied_discount_threshold_bps,
            min_tvl_usd=a.min_tvl_usd,
            include_non_opportunities=a.include_non_opportunities,
            calibration_concurrency=a.calibration_concurrency,
        ),
    )

    # -- Assets / Prices ------------------------------------------------------

    def _add_assets_args(ap: argparse.ArgumentParser) -> None:
        ap.add_argument(
            "--ids",
            type=_json_str_array("--ids"),
            help="JSON array of asset IDs `<chainId>-<address>`, e.g. '[\"1-0x...\"]'.",
        )
        ap.add_argument("--chain-id", type=int, help="Filter by chain ID.")
        _add_paging(ap)
        ap.add_argument(
            "--asset-type",
            choices=[e.value for e in PendleAssetType],
            help="Filter by asset type.",
        )

    p = sub.add_parser(
        "get-assets-all",
        help="List supported PT/YT/LP/SY assets metadata",
        description=(
            "Get supported PT/YT/LP/SY assets metadata. (GET /v1/assets/all)\n"
            "`--ids` items must be `<chainId>-<address>`, not bare addresses."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_assets_args(p)
    p.set_defaults(
        func=_cmd_tool,
        run=lambda a: server.pendle_get_assets_all(
            ids=a.ids,
            chain_id=a.chain_id,
            skip=a.skip,
            limit=a.limit,
            asset_type=_enum_or_none(PendleAssetType, a.asset_type),
        ),
    )

    p = sub.add_parser(
        "get-asset-prices",
        help="USD prices for assets",
        description=(
            "Get USD prices for assets. (GET /v1/prices/assets)\n"
            "`--ids` items must be `<chainId>-<address>`, not bare addresses."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_assets_args(p)
    p.set_defaults(
        func=_cmd_tool,
        run=lambda a: server.pendle_get_asset_prices(
            ids=a.ids,
            chain_id=a.chain_id,
            skip=a.skip,
            limit=a.limit,
            asset_type=_enum_or_none(PendleAssetType, a.asset_type),
        ),
    )

    p = sub.add_parser(
        "get-prices-ohlcv-v4",
        help="PT/YT/LP historical OHLCV price by asset address",
        description=(
            "Get PT / YT / LP historical price by address. (GET /v4/{chainId}/prices/{address}/ohlcv)\n"
            "`--time-frame` accepts hour/day/week and aliases 1h/1d/1w. With --parse-results the\n"
            "CSV `results` string is additionally parsed into `results_parsed` rows\n"
            "({time, open, high, low, close, volume}, string fields keep precision)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--chain-id", type=int, required=True, help="Chain ID.")
    p.add_argument("--address", required=True, help="Asset address (PT/YT/LP, 0x-prefixed).")
    p.add_argument("--time-frame", choices=_TIME_FRAMES, help="hour/day/week (aliases 1h/1d/1w).")
    p.add_argument("--timestamp-start", help="Series start timestamp (ISO8601).")
    p.add_argument("--timestamp-end", help="Series end timestamp (ISO8601).")
    p.add_argument(
        "--parse-results", action="store_true",
        help="Parse the CSV `results` string into structured `results_parsed` rows.",
    )
    p.set_defaults(
        func=_cmd_tool,
        run=lambda a: server.pendle_get_prices_ohlcv_v4(
            chain_id=a.chain_id,
            address=a.address,
            time_frame=a.time_frame,
            timestamp_start=a.timestamp_start,
            timestamp_end=a.timestamp_end,
            parse_results=a.parse_results,
        ),
    )

    # -- Transactions / PnL ---------------------------------------------------

    p = sub.add_parser(
        "get-user-pnl-transactions",
        help="Raw user PnL transactions (pure paginator)",
        description=(
            "Get user transactions by address. (GET /v1/pnl/transactions)\n"
            "Pure paginator: returns the raw {total, results} page. For an aggregated\n"
            "full-history view use `get-user-pnl-summary` instead."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--user", required=True, help="User address (0x-prefixed).")
    _add_paging(p)
    p.add_argument("--chain-id", type=int, help="Filter by chain ID.")
    p.add_argument("--market", help="Filter by market address.")
    p.set_defaults(
        func=_cmd_tool,
        run=lambda a: server.pendle_get_user_pnl_transactions(
            user=a.user,
            skip=a.skip,
            limit=a.limit,
            chain_id=a.chain_id,
            market=a.market,
        ),
    )

    p = sub.add_parser(
        "get-user-pnl-summary",
        help="Aggregate PnL across a user's full history",
        description=(
            "Aggregate PnL across the user's full history (scans GET /v1/pnl/transactions).\n"
            "Scans every page — the result is always complete — and aggregates by action\n"
            "(default) or tx_hash. Raises instead of truncating past ~10000 rows; fall back to\n"
            "the raw `get-user-pnl-transactions` paginator in that case.\n"
            "Example:\n"
            "  pendle get-user-pnl-summary --user 0x... --group-by action"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--user", required=True, help="User address (0x-prefixed).")
    p.add_argument("--chain-id", type=int, help="Filter by chain ID.")
    p.add_argument("--market", help="Filter by market address.")
    p.add_argument(
        "--group-by", choices=["action", "tx_hash"], default="action",
        help="Aggregation key (default action).",
    )
    p.add_argument(
        "--page-size", type=int,
        help="Internal pagination granularity (default 100); does not change completeness.",
    )
    p.set_defaults(
        func=_cmd_tool,
        run=lambda a: server.pendle_get_user_pnl_summary(
            user=a.user,
            chain_id=a.chain_id,
            market=a.market,
            group_by=a.group_by,
            page_size=a.page_size,
        ),
    )

    p = sub.add_parser(
        "get-market-transactions-v5",
        help="Market transactions by address",
        description="Get market transactions by address. (GET /v5/{chainId}/transactions/{address})",
    )
    p.add_argument("--chain-id", type=int, required=True, help="Chain ID.")
    p.add_argument("--address", required=True, help="Market address (0x-prefixed).")
    p.add_argument(
        "--transaction-type",
        choices=[e.value for e in TransactionType],
        help="Filter by transaction type.",
    )
    p.add_argument("--min-value", type=float, help="Min transaction value (USD).")
    p.add_argument("--tx-origin", help="Filter by tx origin address.")
    p.add_argument(
        "--action",
        choices=[e.value for e in TransactionAction],
        help="Filter by action.",
    )
    p.add_argument("--resume-token", help="Resume token from a previous page.")
    _add_paging(p)
    p.set_defaults(
        func=_cmd_tool,
        run=lambda a: server.pendle_get_market_transactions_v5(
            chain_id=a.chain_id,
            address=a.address,
            transaction_type=_enum_or_none(TransactionType, a.transaction_type),
            min_value=a.min_value,
            tx_origin=a.tx_origin,
            action=_enum_or_none(TransactionAction, a.action),
            resume_token=a.resume_token,
            limit=a.limit,
            skip=a.skip,
        ),
    )

    p = sub.add_parser(
        "get-user-pnl-gained-positions",
        help="User's gained PnL across all market positions",
        description=(
            "Get a user's gained PnL across all market positions. (GET /v1/pnl/gained/{user}/positions)"
        ),
    )
    p.add_argument("--user", required=True, help="User address (0x-prefixed).")
    p.set_defaults(
        func=_cmd_tool,
        run=lambda a: server.pendle_get_user_pnl_gained_positions(user=a.user),
    )

    # -- Dashboard ------------------------------------------------------------

    p = sub.add_parser(
        "get-user-positions",
        help="User positions by address",
        description="Get user positions by address. (GET /v1/dashboard/positions/database/{user})",
    )
    p.add_argument("--user", required=True, help="User address (0x-prefixed).")
    p.add_argument("--filter-usd", type=float, help="Hide positions below this USD value.")
    p.set_defaults(
        func=_cmd_tool,
        run=lambda a: server.pendle_get_user_positions(
            user=a.user,
            filter_usd=a.filter_usd,
        ),
    )

    p = sub.add_parser(
        "get-merkle-rewards",
        help="Pending and claimed merkle rewards for a user",
        description=(
            "Get pending and claimed merkle rewards for a user. "
            "(GET /v1/dashboard/merkle-rewards/{user}) "
            "Returns {claimableRewards, claimedRewards}."
        ),
    )
    p.add_argument("--user", required=True, help="User address (0x-prefixed).")
    p.set_defaults(
        func=_cmd_tool,
        run=lambda a: server.pendle_get_merkle_rewards(user=a.user),
    )

    # -- Limit Orders -----------------------------------------------------------

    def _add_limit_orders_analytics_args(ap: argparse.ArgumentParser) -> None:
        ap.add_argument("--chain-id", type=int, help="Filter by chain ID.")
        ap.add_argument("--limit", type=int, help="Max rows to return.")
        ap.add_argument("--maker", help="Filter by maker address.")
        ap.add_argument("--yt", help="Filter by YT address.")
        ap.add_argument("--timestamp-start", help="Start timestamp (ISO8601).")
        ap.add_argument("--timestamp-end", help="End timestamp (ISO8601).")
        ap.add_argument("--resume-token", help="Resume token from a previous page.")

    p = sub.add_parser(
        "get-limit-orders-all-v2",
        help="All limit orders (analytics)",
        description="Get all limit orders for analytics. (GET /v2/limit-orders)",
    )
    _add_limit_orders_analytics_args(p)
    p.set_defaults(
        func=_cmd_tool,
        run=lambda a: server.pendle_get_limit_orders_all_v2(
            chain_id=a.chain_id,
            limit=a.limit,
            maker=a.maker,
            yt=a.yt,
            timestamp_start=a.timestamp_start,
            timestamp_end=a.timestamp_end,
            resume_token=a.resume_token,
        ),
    )

    p = sub.add_parser(
        "get-limit-orders-archived-v2",
        help="Archived limit orders (analytics)",
        description="Get all archived limit orders for analytics. (GET /v2/limit-orders/archived)",
    )
    _add_limit_orders_analytics_args(p)
    p.set_defaults(
        func=_cmd_tool,
        run=lambda a: server.pendle_get_limit_orders_archived_v2(
            chain_id=a.chain_id,
            limit=a.limit,
            maker=a.maker,
            yt=a.yt,
            timestamp_start=a.timestamp_start,
            timestamp_end=a.timestamp_end,
            resume_token=a.resume_token,
        ),
    )

    p = sub.add_parser(
        "get-limit-orders-book-v2",
        help="Limit order book for a market",
        description=(
            "Get order book v2. (GET /v2/limit-orders/book/{chainId})\n"
            "Example:\n"
            "  pendle get-limit-orders-book-v2 --chain-id 1 --market 0x... --precision-decimal 4"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--chain-id", type=int, required=True, help="Chain ID.")
    p.add_argument("--precision-decimal", type=int, required=True, help="Price precision decimals.")
    p.add_argument("--market", required=True, help="Market address (0x-prefixed).")
    p.add_argument("--limit", type=int, help="Max levels to return.")
    _add_tristate_flag(p, "--include-amm", "Include AMM liquidity in the book.")
    p.set_defaults(
        func=_cmd_tool,
        run=lambda a: server.pendle_get_limit_orders_book_v2(
            chain_id=a.chain_id,
            precision_decimal=a.precision_decimal,
            market=a.market,
            limit=a.limit,
            include_amm=a.include_amm,
        ),
    )

    p = sub.add_parser(
        "get-limit-orders-maker-limit-orders",
        help="A maker's limit orders",
        description="Get user limit orders in market. (GET /v1/limit-orders/makers/limit-orders)",
    )
    p.add_argument("--chain-id", type=int, required=True, help="Chain ID.")
    p.add_argument("--maker", required=True, help="Maker address (0x-prefixed).")
    _add_paging(p)
    p.add_argument("--yt", help="Filter by YT address.")
    p.add_argument("--order-type", type=int, help="Order type (numeric).")
    _add_tristate_flag(p, "--is-active", "Filter to active (or --no-is-active: inactive) orders.")
    p.set_defaults(
        func=_cmd_tool,
        run=lambda a: server.pendle_get_limit_orders_maker_limit_orders(
            chain_id=a.chain_id,
            maker=a.maker,
            skip=a.skip,
            limit=a.limit,
            yt=a.yt,
            order_type=a.order_type,
            is_active=a.is_active,
        ),
    )

    p = sub.add_parser(
        "get-limit-orders-taker-limit-orders",
        help="Limit orders matchable by a taker (by YT)",
        description="Get limit orders to match by YT address. (GET /v1/limit-orders/takers/limit-orders)",
    )
    p.add_argument("--chain-id", type=int, required=True, help="Chain ID.")
    p.add_argument("--yt", required=True, help="YT address (0x-prefixed).")
    p.add_argument("--order-type", type=int, required=True, help="Order type (numeric).")
    _add_paging(p)
    p.add_argument("--sort-by", help="Sort key.")
    p.add_argument("--sort-order", help="Sort order (asc/desc).")
    p.set_defaults(
        func=_cmd_tool,
        run=lambda a: server.pendle_get_limit_orders_taker_limit_orders(
            chain_id=a.chain_id,
            yt=a.yt,
            order_type=a.order_type,
            skip=a.skip,
            limit=a.limit,
            sort_by=a.sort_by,
            sort_order=a.sort_order,
        ),
    )

    # -- SDK (quoting) ----------------------------------------------------------

    p = sub.add_parser(
        "get-supported-aggregators",
        help="Supported swap aggregators for a chain",
        description="Get supported aggregators for a chain. (GET /v1/sdk/{chainId}/supported-aggregators)",
    )
    p.add_argument("--chain-id", type=int, required=True, help="Chain ID.")
    p.set_defaults(
        func=_cmd_tool,
        run=lambda a: server.pendle_get_supported_aggregators(chain_id=a.chain_id),
    )

    p = sub.add_parser(
        "get-market-tokens",
        help="Supported tokens for a market",
        description="Get supported tokens for market. (GET /v1/sdk/{chainId}/markets/{market}/tokens)",
    )
    p.add_argument("--chain-id", type=int, required=True, help="Chain ID.")
    p.add_argument("--market", required=True, help="Market address (0x-prefixed).")
    p.set_defaults(
        func=_cmd_tool,
        run=lambda a: server.pendle_get_market_tokens(chain_id=a.chain_id, market=a.market),
    )

    p = sub.add_parser(
        "get-swapping-prices",
        help="Real-time PT/YT swap prices of a market",
        description="Get real-time PT/YT swap price of a market. (GET /v1/sdk/{chainId}/markets/{market}/swapping-prices)",
    )
    p.add_argument("--chain-id", type=int, required=True, help="Chain ID.")
    p.add_argument("--market", required=True, help="Market address (0x-prefixed).")
    p.set_defaults(
        func=_cmd_tool,
        run=lambda a: server.pendle_get_swapping_prices(chain_id=a.chain_id, market=a.market),
    )

    p = sub.add_parser(
        "get-pt-cross-chain-metadata",
        help="PT cross-chain metadata",
        description="PT cross-chain metadata. (GET /v1/sdk/{chainId}/cross-chain-pt-metadata/{pt})",
    )
    p.add_argument("--chain-id", type=int, required=True, help="Chain ID.")
    p.add_argument("--pt", required=True, help="PT address (0x-prefixed).")
    p.set_defaults(
        func=_cmd_tool,
        run=lambda a: server.pendle_get_pt_cross_chain_metadata(chain_id=a.chain_id, pt=a.pt),
    )

    p = sub.add_parser(
        "convert-v2",
        help="Universal convert / swap quote",
        description=(
            "Universal convert function. (GET /v2/sdk/{chainId}/convert)\n"
            "Parameter semantics:\n"
            "- --slippage is a fraction in [0, 1] (0.5% -> 0.005; 50% -> 0.5).\n"
            "- --amounts-in items MUST be base-10 integer strings in the input token's smallest\n"
            "  unit (decimals=18: 0.001 -> \"1000000000000000\"); never pass \"0.001\".\n"
            "- --tokens-in / --amounts-in lengths must match; --tokens-out must be non-empty.\n"
            "- Without --include-tx, routes[].tx (calldata) and encoded contract params are\n"
            "  dropped to keep quotes small; pass --include-tx only to broadcast.\n"
            "Example:\n"
            "  pendle convert-v2 --chain-id 1 --slippage 0.005 \\\n"
            "    --tokens-in '[\"0xcbc72d92b2dc8187414f6734718563898740c0bc\"]' \\\n"
            "    --amounts-in '[\"1000000000000000000\"]' \\\n"
            "    --tokens-out '[\"0xb253eff1104802b97ac7e3ac9fdd73aece295a2c\"]' \\\n"
            "    --enable-aggregator"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--chain-id", type=int, required=True, help="Chain ID.")
    p.add_argument("--slippage", type=float, required=True, help="Fraction in [0, 1], e.g. 0.005 for 0.5%%.")
    p.add_argument(
        "--tokens-in", type=_json_str_array("--tokens-in"), required=True,
        help="JSON array of input token addresses.",
    )
    p.add_argument(
        "--amounts-in", type=_json_str_array("--amounts-in"), required=True,
        help="JSON array of smallest-unit integer strings, aligned with --tokens-in.",
    )
    p.add_argument(
        "--tokens-out", type=_json_str_array("--tokens-out"), required=True,
        help="JSON array of output token addresses (non-empty).",
    )
    p.add_argument("--receiver", help="Receiver address; defaults to the API's placeholder.")
    _add_tristate_flag(p, "--enable-aggregator", "Allow external aggregator routing.")
    p.add_argument(
        "--aggregators", type=_json_str_array("--aggregators"),
        help="JSON array of aggregator names (see get-supported-aggregators).",
    )
    _add_tristate_flag(p, "--redeem-rewards", "Redeem rewards during conversion.")
    _add_tristate_flag(p, "--need-scale", "Forwarded to the API; does NOT auto-scale --amounts-in.")
    p.add_argument("--additional-data", help="Extra data fields to request, e.g. 'impliedApy'.")
    _add_tristate_flag(p, "--use-limit-order", "Route through the limit order book.")
    p.add_argument(
        "--include-tx", action="store_true",
        help="Keep routes[].tx calldata and encoded contract params in the response.",
    )
    p.set_defaults(
        func=_cmd_tool,
        run=lambda a: server.pendle_convert_v2(
            chain_id=a.chain_id,
            slippage=a.slippage,
            tokens_in=a.tokens_in,
            amounts_in=a.amounts_in,
            tokens_out=a.tokens_out,
            receiver=a.receiver,
            enable_aggregator=a.enable_aggregator,
            aggregators=a.aggregators,
            redeem_rewards=a.redeem_rewards,
            need_scale=a.need_scale,
            additional_data=a.additional_data,
            use_limit_order=a.use_limit_order,
            include_tx=a.include_tx,
        ),
    )

    # -- Ve / sPENDLE / Statistics ------------------------------------------------

    p = sub.add_parser(
        "get-ve-pendle-data-v2",
        help="vePENDLE data",
        description="Get vePENDLE data. (GET /v2/ve-pendle/data)",
    )
    p.set_defaults(func=_cmd_tool, run=lambda a: server.pendle_get_ve_pendle_data_v2())

    p = sub.add_parser(
        "get-ve-pendle-market-fees-chart",
        help="vePENDLE market fees chart",
        description="Get vePENDLE market fees chart. (GET /v1/ve-pendle/market-fees-chart)",
    )
    p.add_argument("--timestamp-start", help="Start timestamp (ISO8601).")
    p.add_argument("--timestamp-end", help="End timestamp (ISO8601).")
    p.set_defaults(
        func=_cmd_tool,
        run=lambda a: server.pendle_get_ve_pendle_market_fees_chart(
            timestamp_start=a.timestamp_start,
            timestamp_end=a.timestamp_end,
        ),
    )

    p = sub.add_parser(
        "get-spendle-data",
        help="Aggregate sPENDLE staking data",
        description=(
            "Get aggregate sPENDLE staking data. (GET /v1/spendle/data) "
            "Total PENDLE staked, last-epoch APR, buyback amount, and 12-epoch history."
        ),
    )
    p.set_defaults(func=_cmd_tool, run=lambda a: server.pendle_get_spendle_data())

    p = sub.add_parser(
        "get-distinct-user-from-token",
        help="Distinct user count holding a token",
        description="Get distinct user count from token. (GET /v1/statistics/get-distinct-user-from-token)",
    )
    p.add_argument("--token", required=True, help="Token address (0x-prefixed).")
    p.add_argument("--chain-id", type=int, help="Filter by chain ID.")
    p.set_defaults(
        func=_cmd_tool,
        run=lambda a: server.pendle_get_distinct_user_from_token(
            token=a.token,
            chain_id=a.chain_id,
        ),
    )

    # -- legacy generic commands -----------------------------------------------

    p_list = sub.add_parser(
        "list",
        help="List all MCP tool function names (introspection)",
    )
    p_list.set_defaults(func=cmd_list)

    p_show = sub.add_parser(
        "show",
        help="Show an MCP tool's signature and docstring (introspection)",
    )
    p_show.add_argument("tool")
    p_show.set_defaults(func=cmd_show)

    p_call = sub.add_parser(
        "call",
        help="Call an MCP tool by name with raw JSON kwargs",
    )
    p_call.add_argument("tool")
    p_call.add_argument(
        "--json",
        help="JSON object of kwargs. Use '-' or omit to read from stdin.",
        default=None,
    )
    p_call.set_defaults(func=cmd_call)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
