# Tasks: Pendle API Resilience & Diagnostics

## Gate（必须先完成）
- [x] 用户审核并确认进入 IMPLEMENT 阶段

## IMPLEMENT
- [x] 设计 `PendleApiError` 扩展字段：`method/path/params/error_type/attempts/retries_exhausted`（保持向后兼容）
- [x] 在 `PendleApiClient.get_json` 引入 429/5xx/网络错误的指数退避 + jitter（支持 `Retry-After`）
- [x] 增加 `PENDLE_API_ERROR_DETAIL_MAX_CHARS` 环境变量并接入
- [x] 对高误用参数增加本地校验与更清晰的报错（例如 `slippage` 范围、`tokens_in/amounts_in` 长度一致）
- [x] 新增 MCP tool：`pendle_health`（并发检查代表性 endpoints，输出 ok/latency/error 摘要）

## VERIFY
- [x] 补充/更新单测覆盖：429 重试、Retry-After、params 脱敏、attempts 字段等
- [x] 本地跑 `pytest`（仅 mock transport，不依赖真实联网）

## ARCHIVE（用户验收通过后）
- [x] 将 `spec_delta.md` 合并进 `docs/sot/`（更新错误约定/重试策略/健康检查工具）
- [x] `mv docs/wip/20260119-api-resilience docs/archive/20260119-api-resilience`
