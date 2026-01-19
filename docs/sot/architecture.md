# 架构说明（SOT）

Last Updated: 2026-01-19

## 模块边界（按 repo 或关键模块描述）
- repo: `pendle-mcp`
  - `pendle_mcp.server`：FastMCP server（stdio）与 tools 定义
  - `pendle_mcp.pendle_api`：`PendleApiClient`（baseUrl/timeout/重试/错误归一化）与 endpoint 封装
  - `tests/test_pendle_api_client.py`：最小单测（mock HTTP，不依赖真实联网）

## 关键约束 / 不变量
- 数据源：仅 Pendle 官方 API v2（默认 baseUrl：`https://api-v2.pendle.finance/core`）
- 只读：仅调用语义只读的 GET endpoints；排除所有 POST 与语义上可能触发动作的 GET（例如 cancel/redeem/swap 类）。部分 GET 会返回报价/交易参数（如 `convert`），但本项目不签名、不广播链上交易。
- 参数约定：MCP tools 对外参数使用 snake_case；对 Pendle API 的 query/path 字段按官方命名透传（如 `chainId`、`resumeToken`、`includeFeeBreakdown`）
- `ids` 参数约定：对接 Pendle API 的 `ids`（如 assets/prices/markets）使用 `<chainId>-<address>` 作为元素格式（例如 `1-0x...`、`8453-0x...`）。
- `convert_v2` 入参校验：`amounts_in` 必须是最小单位 base-10 整数字符串；不符合则本地直接报错，不发起 API 请求。
- 错误可诊断性：`PendleApiError` 的异常文本包含 `status_code` / `url` / `detail`；其中 `detail` 过长会按固定上限截断（当前 2048 chars）。
- 配置入口（环境变量）：
  - `PENDLE_API_BASE_URL`
  - `PENDLE_API_TIMEOUT_SECONDS`（默认 20）
  - `PENDLE_API_MAX_RETRIES`（默认 1，仅对网络错误/5xx 重试）
  - `PENDLE_API_RETRY_BACKOFF_SECONDS`（默认 0.2，指数退避）

## 跨 repo 交互（如适用）
无（当前仅单 repo）
