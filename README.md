# pendle-mcp

`pendle-mcp` 是一个 stdio MCP server，把 Pendle 官方 API v2（`https://api-v2.pendle.finance/core`）封成只读查询 tools，供 Claude Code / Codex 等 MCP 客户端调用。

只暴露语义只读的 GET 端点，不签名、不广播链上交易；可能触发动作的 GET（cancel/redeem/swap、`/v1/spendle/{address}` 等）也不接入。

## 快速开始

```bash
conda activate pendle-mcp
cd src/pendle-mcp
pip install -e ".[dev]"
pytest
python -m pendle_mcp   # stdio 运行
```

代码入口：`src/pendle-mcp/src/pendle_mcp/`。

## 模块结构

- `pendle_mcp.server` —— FastMCP server（stdio）与 tools 定义
- `pendle_mcp.pendle_api` —— `PendleApiClient`（baseUrl / timeout / 重试 / 错误归一化）与 endpoint 封装
- `tests/test_pendle_api_client.py` —— mock HTTP 单测，不依赖真实网络

## 提供的 MCP tools

| 类别 | tools |
|------|-------|
| Chains | `pendle_get_chains` |
| Health | `pendle_health` |
| Markets | `pendle_get_markets_all`（分页 v2）、`pendle_get_markets_points_market`、`pendle_get_market_data_v2`、`pendle_get_market_historical_data_v3` |
| Assets / Prices | `pendle_get_assets_all`、`pendle_get_asset_prices`、`pendle_get_prices_ohlcv_v4` |
| Transactions | `pendle_get_user_pnl_transactions`、`pendle_get_market_transactions_v5` |
| PnL | `pendle_get_user_pnl_gained_positions` |
| Dashboard | `pendle_get_user_positions`、`pendle_get_merkle_rewards` |
| Limit Orders | `pendle_get_limit_orders_all_v2`、`pendle_get_limit_orders_archived_v2`、`pendle_get_limit_orders_book_v2`、`pendle_get_limit_orders_maker_limit_orders`、`pendle_get_limit_orders_taker_limit_orders` |
| SDK（查询/报价） | `pendle_get_supported_aggregators`、`pendle_get_market_tokens`、`pendle_get_swapping_prices`、`pendle_get_pt_cross_chain_metadata`、`pendle_convert_v2` |
| Ve / sPENDLE | `pendle_get_ve_pendle_data_v2`、`pendle_get_ve_pendle_market_fees_chart`、`pendle_get_spendle_data` |
| Statistics | `pendle_get_distinct_user_from_token` |

## 参数与错误约定

调用容易踩坑的几条：

- **`ids` 元素格式**：`<chainId>-<address>`（例如 `1-0x...`、`8453-0x...`）。仅传裸地址会返回错误或空结果。适用于 `pendle_get_assets_all` / `pendle_get_asset_prices` / `pendle_get_markets_all`。
- **`pendle_get_markets_all` 分页**：响应 `{total, limit, skip, results}`，API 端默认 `limit=20`；用 `skip / limit / order_by` 翻页排序。
- **`pendle_convert_v2`**：
  - `slippage` 是**比例小数**且范围 `[0, 1]`（0.5% → `0.005`，50% → `0.5`）。
  - `tokens_in` / `amounts_in` 长度必须一致；`tokens_out` 不能为空。
  - `amounts_in` 必须是输入 token **最小单位**的 base-10 **整数字符串**（decimals=18 时 `0.001` → `"1000000000000000"`），禁止传 `"0.001"`。
- **`time_frame` 别名**：`pendle_get_market_historical_data_v3` 和 `pendle_get_prices_ohlcv_v4` 接受 `1h / 1d / 1w`，本地规范化成 `hour / day / week` 再请求 API；非法值本地直接报错，不发起 HTTP。
- **`pendle_get_market_historical_data_v3.include_apy_breakdown`**：v3 新参，附加 APY 拆解字段。
- **`pendle_get_prices_ohlcv_v4.parse_results`**：默认 `false`；开启后把响应里 `results` 的 CSV 串解析成 `results_parsed`（结构化数组、字符串字段保精度），解析失败会带 `parse_error`。
- **`pendle_get_merkle_rewards`**：响应同时包含 `claimableRewards`（待领取）与 `claimedRewards`（已领取），取代旧的 `pendle_get_merkle_claimed_rewards`。

API 错误统一返回 `PendleApiError`，字段：`error_type / status_code / method / path / params / attempts / retries_exhausted / url / detail`。其中 `params / url` 会对敏感或超长字段（如 `additionalData`）脱敏 / 截断；`detail` 按上限截断（默认 2048，可用 `PENDLE_API_ERROR_DETAIL_MAX_CHARS` 调）。

## 配置（环境变量）

| 变量 | 默认 | 说明 |
|------|------|------|
| `PENDLE_API_BASE_URL` | `https://api-v2.pendle.finance/core` | API base URL |
| `PENDLE_API_TIMEOUT_SECONDS` | `20` | 单次请求超时 |
| `PENDLE_API_MAX_RETRIES` | `1` | 对网络错误 / 5xx / 429 的最大重试次数；429 支持 `Retry-After` |
| `PENDLE_API_RETRY_BACKOFF_SECONDS` | `0.2` | 指数退避基数 + jitter |
| `PENDLE_API_ERROR_DETAIL_MAX_CHARS` | `2048` | 错误 `detail` 字段截断上限 |

## 命名约定

- MCP tools 对外参数用 `snake_case`。
- 透传给 Pendle API 的 query / path 字段保持官方命名（`chainId`、`resumeToken`、`includeFeeBreakdown`、`includeApyBreakdown` 等）。
