# 项目概览（SOT）

Last Updated: 2026-01-19

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

## Tool 参数与错误约定（重要）
- `ids`（如 `pendle_get_assets_all` / `pendle_get_asset_prices` / `pendle_get_markets_all`）：元素格式为 `<chainId>-<address>`（例如 `1-0x...`、`8453-0x...`）。仅传裸地址可能返回错误或空结果。
- `pendle_convert_v2.slippage`：使用**比例小数**（例如 0.5% => `0.005`；50% => `0.5`）。
- `pendle_convert_v2.amounts_in`：必须是输入 token **最小单位**的 base-10 **整数字符串**（例如 decimals=18 时，`0.001` => `1000000000000000`）。禁止传 `"0.001"` 这类小数。
- 非 2xx 错误：会包含 `status_code`、请求 `url`、以及响应 `detail`（过长会截断）以便快速定位问题。

## 本地开发最小路径（只到开发自测）
- `conda activate pendle-mcp`
- `cd src/pendle-mcp`
- `pip install -e ".[dev]"`
- `pytest`
- `python -m pendle_mcp`（stdio 运行）
