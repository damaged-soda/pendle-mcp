# 架构说明（SOT）

Last Updated: 2026-01-11

## 模块边界（按 repo 或关键模块描述）
- repo: `pendle-mcp`
  - `pendle_mcp.server`：FastMCP server（stdio）与 tools 定义
  - `pendle_mcp.pendle_api`：`PendleApiClient`（baseUrl/timeout/重试/错误归一化）与 endpoint 封装
  - `tests/test_pendle_api_client.py`：最小单测（mock HTTP，不依赖真实联网）

## 关键约束 / 不变量
- 数据源：仅 Pendle 官方 API v2（默认 baseUrl：`https://api-v2.pendle.finance/core`）
- 只读：首版 tools 仅调用 GET endpoints（`/v1/chains`、`/v1/markets/all`、`/v1/assets/all`、`/v1/prices/assets`）
- 配置入口（环境变量）：
  - `PENDLE_API_BASE_URL`
  - `PENDLE_API_TIMEOUT_SECONDS`（默认 20）
  - `PENDLE_API_MAX_RETRIES`（默认 1，仅对网络错误/5xx 重试）
  - `PENDLE_API_RETRY_BACKOFF_SECONDS`（默认 0.2，指数退避）

## 跨 repo 交互（如适用）
无（当前仅单 repo）
