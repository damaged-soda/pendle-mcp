# Spec Delta: Pendle API Resilience & Diagnostics

Date: 2026-01-19

## ADDED Requirements

### R1. 错误分类与结构化诊断信息
当调用 Pendle API 失败时，系统 MUST 产出 `PendleApiError`，并满足：
- MUST 包含 `status_code`（如可得）、`method`、`path`（不含 baseUrl）、`url`（最终 URL，含 query）。
- MUST 包含 `params`（与发送给 API 的 query params 等价），并对敏感/超长字段做脱敏或截断（例如 `additionalData`）。
- MUST 包含 `error_type`（枚举或等价字符串），至少覆盖：
  - `client_error`：4xx（不含 429），认为是参数/权限/资源问题（默认不重试）。
  - `rate_limited`：429（可重试，支持读取 `Retry-After`）。
  - `upstream_error`：5xx（可重试）。
  - `network_error`：连接超时、DNS、断连等（可重试）。
  - `invalid_json`：2xx 但响应无法解析为 JSON（默认不重试）。
- MUST 在异常文本中包含以上关键信息，且 `detail/response_text` 按上限截断。

### R2. 可配置的重试/退避策略（含 jitter）
`PendleApiClient.get_json` MUST：
- 对 `rate_limited` / `upstream_error` / `network_error` 进行重试（最多 `max_retries` 次）。
- 使用指数退避 + jitter；若存在 `Retry-After` 且可解析，优先使用其等待时间（并允许叠加最小 jitter）。
- 在最终失败的错误中 MUST 包含：
  - `attempts`（实际请求次数）
  - `retries_exhausted=true/false`（或等价表达）

### R3. 错误 detail 截断上限可配置
系统 MUST 支持通过环境变量配置错误 detail 的最大字符数（例如 `PENDLE_API_ERROR_DETAIL_MAX_CHARS`），并保持默认值与现状一致（2048）。

### R4. 健康检查 / 降级提示工具
系统 MUST 新增一个 MCP tool（建议名：`pendle_health`）用于快速诊断当前 Pendle API 可用性：
- MUST 返回每个检查项的 `name`、`ok`（bool）、`latency_ms`（如可得）。
- SHOULD 覆盖“最小参数”且代表性的 endpoints（例如 `/v1/chains`、以及 SDK/Markets/Prices 中较常用且已知不稳定的项）。
- 若某检查项失败，MUST 返回简短错误摘要（基于 `PendleApiError`，但避免输出过长内容）。

## MODIFIED Requirements

### M1. 错误可诊断性约定扩展
现有约定“非 2xx 错误包含 `status_code`/`url`/`detail`（截断）”保持不变，并扩展为同时包含 `path`/`params`/`error_type`/`attempts` 等定位信息。

### M2. 重试覆盖范围扩展
现有“仅对网络错误/5xx 重试”保持语义一致，但扩展支持对 429 进行退避重试，并采用带 jitter 的指数退避。

## DEFERRED（本次不做）
- D1. 链上 `RouterStatic` 兜底报价层（将引入链上数据源/eth_call 依赖，需要单独评审架构约束变更）。
- D2. 全量 tool 输出增加 `raw + human + decimals` 的统一格式（可能涉及多端点 schema 设计与向后兼容策略，先拆分提案）。

