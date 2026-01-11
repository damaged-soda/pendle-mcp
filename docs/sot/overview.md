# 项目概览（SOT）

Last Updated: 2026-01-11

## 项目是什么
`pendle-mcp` 是一个 MCP Server（stdio），数据源接入 Pendle 官方 API v2（`https://api-v2.pendle.finance/core`），对外提供只读（GET）数据查询类 tools（排除 POST 与语义上可能触发动作的 GET）。

## Repo 列表与职责（与 docmap 对齐）
- `pendle-mcp`：Python MCP server + Pendle API client（入口：`src/pendle-mcp/src/pendle_mcp/`）

## 提供的 MCP tools（按类别）
- Chains：`pendle_get_chains`
- Markets：`pendle_get_markets_all`、`pendle_get_markets_points_market`、`pendle_get_market_data_v2`、`pendle_get_market_historical_data_v2`
- Assets / Prices：`pendle_get_assets_all`、`pendle_get_asset_prices`、`pendle_get_prices_ohlcv_v4`
- Transactions：`pendle_get_user_pnl_transactions`、`pendle_get_market_transactions_v5`
- Dashboard：`pendle_get_user_positions`、`pendle_get_merkle_claimed_rewards`
- Limit Orders：`pendle_get_limit_orders_all_v2`、`pendle_get_limit_orders_archived_v2`、`pendle_get_limit_orders_book_v2`、`pendle_get_limit_orders_maker_limit_orders`、`pendle_get_limit_orders_taker_limit_orders`
- SDK（查询/报价类）：`pendle_get_supported_aggregators`、`pendle_get_market_tokens`、`pendle_get_swapping_prices`、`pendle_get_pt_cross_chain_metadata`、`pendle_convert_v2`
- Ve Pendle：`pendle_get_ve_pendle_data_v2`、`pendle_get_ve_pendle_market_fees_chart`
- Statistics：`pendle_get_distinct_user_from_token`

## 本地开发最小路径（只到开发自测）
- `conda activate pendle-mcp`
- `cd src/pendle-mcp`
- `pip install -e ".[dev]"`
- `pytest`
- `python -m pendle_mcp`（stdio 运行）
