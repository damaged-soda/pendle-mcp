# Tasks: time_frame Aliases + OHLCV CSV Parsing

## Gate（必须先完成）
- [x] 用户审核并确认进入 IMPLEMENT 阶段

## IMPLEMENT
- [x] 在 `pendle_mcp.pendle_api` 增加 `time_frame` 规范化/校验逻辑，并接入 `get_market_historical_data_v2` / `get_prices_ohlcv_v4`
- [x] 为 `pendle_get_prices_ohlcv_v4` 增加 `parse_results` 参数，并实现 `results` CSV -> `results_parsed` 解析（保持默认兼容）
- [x] 更新 tool docstring（说明 `time_frame` 可用值与别名、`parse_results` 行为）

## VERIFY
- [x] 单测：`time_frame="1d"` 映射为 `"day"` 且不会触发 400
- [x] 单测：非法 `time_frame` 本地直接 `ValueError`（不发 HTTP）
- [x] 单测：OHLCV `results` CSV 解析（含 header/空行/列数异常）
- [x] 本地跑 `pytest`

## ARCHIVE（用户验收通过后）
- [x] 将 `spec_delta.md` 合并进 `docs/sot/`
- [x] `mv docs/wip/20260119-timeframe-ohlcv docs/archive/20260119-timeframe-ohlcv`
