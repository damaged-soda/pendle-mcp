# Spec Delta: 参数语义与错误信息增强（Draft）

## ADDED Requirements

### R1: `pendle_convert_v2` 参数语义必须明确且可操作
- tool 描述中必须明确：
  - `slippage` 的单位为**小数比例**（例如 0.5% => 0.005）
  - `amounts_in` 为 **token 最小单位整数** 的字符串列表（base-10），禁止使用小数形式（例如 `"0.001"`）
  - `tokens_in` / `tokens_out` 为链上 token 地址（`0x...`），与 `chain_id` 对应链一致
- 描述中必须提供至少 1 个可复制的示例（含 slippage 与 amounts_in）。
- 描述中必须解释：`need_scale` 仅透传给 Pendle API，不负责把 `amounts_in` 的人类小数转换为整数。

### R2: `ids` 类参数的格式必须在 tool 描述中说明
- 对接 Pendle API 的 `ids`（如 `pendle_get_assets_all`、`pendle_get_asset_prices`、`pendle_get_markets_all`）必须在 tool 描述中说明：
  - `ids` 的元素格式为 `<chainId>-<address>`（例如 `"1-0x..."`、`"8453-0x..."`）
  - 仅传裸地址可能导致 Pendle API 4xx（以官方接口为准）

### R3: 非 2xx 错误必须可诊断
- 当 Pendle API 返回非 2xx 时，异常文本必须包含：
  - HTTP status code
  - 请求 URL（包含路径与 query）
  - 响应 body（若过长则截断；截断上限需在实现中固定，例如 2KB）

### R4: 常见输入错误需在本地拦截并给出纠错提示
- `pendle_convert_v2.amounts_in` 中任意元素如果不是 base-10 整数字符串（例如包含 `.`、科学计数法、空字符串），必须在本地抛出 `ValueError`（或等价的可见错误），并提示：
  - 这是“最小单位整数”
  - 给出示例（例如 decimals=18 的 `0.001` => `1000000000000000`）

## MODIFIED Requirements

### M1: `PendleApiError` 的字符串表示需携带上下文
- 当前仅返回固定文案（例如 “Pendle API returned non-2xx response”）的行为必须修改：
  - 在不破坏现有异常类型的前提下，把 status/url/detail 纳入最终可见的错误文本。

