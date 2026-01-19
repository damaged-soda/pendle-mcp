# Spec Delta: time_frame Aliases + OHLCV CSV Parsing

Date: 2026-01-19

## ADDED Requirements

### R1. `time_frame` 规范化（别名映射 + 本地校验）
系统 MUST 在发起 Pendle API 请求前，对以下 endpoints 的 `time_frame` 入参进行规范化：
- `pendle_get_market_historical_data_v2.time_frame`
- `pendle_get_prices_ohlcv_v4.time_frame`

规范化规则 MUST 满足：
- 接受 `hour/day/week`（大小写不敏感），并传给 Pendle API 时使用小写规范值。
- 接受别名：`1h -> hour`、`1d -> day`、`1w -> week`（大小写不敏感）。
- 对任何不在上述集合中的值，MUST 在本地抛出 `ValueError`，并包含：
  - 允许值：`hour/day/week`
  - 支持别名：`1h/1d/1w`
  - 示例：`time_frame="1d"` 会被规范化为 `"day"`
- 若 `time_frame` 为 `None`，MUST 不做处理并保持现状（不传该 query 参数）。

### R2. OHLCV `results` CSV 的可选结构化解析
系统 MUST 为 `pendle_get_prices_ohlcv_v4` 增加一个可选参数（建议名：`parse_results: bool`）：
- 默认值 MUST 为 `False`（保持当前向后兼容行为）。
- 当 `parse_results=True` 且响应存在 `results` 字段且其类型为字符串时：
  - MUST 将其按 CSV 解析为结构化数组 `results_parsed`；
  - 每一行 MUST 映射为对象，字段为：`time/open/high/low/close/volume`（值保持字符串，避免精度问题）。
  - MUST 忽略空行，并 SHOULD 自动跳过可能存在的 header 行（例如第一行包含 `time` 字样）。
- 当 `parse_results=True` 但无法解析（缺字段/非字符串/列数不匹配等）时：
  - MUST 仍返回原始响应；
  - SHOULD 返回 `results_parsed: null` 并附带 `parse_error` 简述原因（避免 silent failure）。

## MODIFIED Requirements
无（本次均为向后兼容的新增能力）

