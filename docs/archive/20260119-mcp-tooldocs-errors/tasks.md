# Tasks: 提升 tool 描述与错误可诊断性

## IMPLEMENT Checklist

- [x] 更新 `pendle_convert_v2` 的 tool 描述：明确 `slippage` 与 `amounts_in` 的单位/格式，并给出示例
- [x] 更新 `ids` 相关 tools 的描述：明确 `<chainId>-<address>` 格式
- [x] 在 `pendle_convert_v2` 增加 `amounts_in` 本地格式校验（发现小数/非整数时给出纠错提示）
- [x] 改进 `PendleApiError` 的可见错误文本：包含 status code / URL / body 摘要
- [x] 补充/调整单测：覆盖 `amounts_in` 校验与错误文本格式
- [x] 运行 `pytest`（在 `src/pendle-mcp`）

## VERIFY Checklist

- [x] 用最小示例验证：传 `"0.001"` 会得到本地可读错误提示（无需请求 Pendle API）
- [x] 用 mock transport 验证：非 2xx 时错误信息包含 status/url/body
