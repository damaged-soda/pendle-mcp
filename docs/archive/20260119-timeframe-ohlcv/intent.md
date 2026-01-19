# Intent: time_frame 本地校验/别名映射 + OHLCV CSV 可选解析

Date: 2026-01-19

## 背景
用户在实际调用 `pendle_get_prices_ohlcv_v4` / `pendle_get_market_historical_data_v2` 时，容易按“常见写法”传入 `time_frame="1d"`（或 `1h/1w`），但 Pendle API 实际要求为 `day/hour/week`，导致 400。

虽然当前错误信息已足够可诊断，但该类错误可以通过**本地校验与别名映射**在请求前避免，提升体验。

此外，`pendle_get_prices_ohlcv_v4` 的 `results` 字段在部分场景为 CSV 字符串；若能提供可选解析为结构化数组（time/open/high/low/close/volume），将更易用。

## 目标（Goals）
- `time_frame` 体验优化：接受 `1d/1h/1w` 并自动映射到 `day/hour/week`；对非法值在本地直接报错并给出可选值提示。
- OHLCV 解析：为 `pendle_get_prices_ohlcv_v4` 增加可选参数，将 `results` CSV 解析为结构化数组，并保持向后兼容（默认仍返回原始 JSON）。

## 非目标（Non-Goals）
- 不引入新数据源或链上兜底能力。
- 不改变现有 tools 默认返回结构（仅在显式开启解析参数时增加解析结果字段）。
- 不对所有 endpoint 做通用 schema 重写（仅针对 `time_frame` 与 OHLCV `results` 的体验改进）。

## 验收标准（Acceptance）
- 传入 `time_frame="1d"` 与 `time_frame="day"` 行为一致，且实际发往 Pendle API 的 query 为 `time_frame=day`。
- 传入非法 `time_frame` 时，本地 `ValueError` 信息明确列出允许值与别名映射（不发起 HTTP 请求）。
- `pendle_get_prices_ohlcv_v4(parse_results=True)` 时，若响应存在 `results` CSV 字符串，则返回额外字段 `results_parsed`（数组），且 `results` 原字段保持不变。

