# Intent: 提升 MCP tool 可用性（参数语义 + 错误可诊断性）

## 背景
在实际使用 `pendle-mcp` 的过程中，agent 调用 `pendle_convert_v2` 时出现「Pendle API returned non-2xx response」的失败，但：

1) tool 描述不够明确，导致 agent 不知道 `slippage` / `amounts_in` 的单位与格式（例如把 `0.5%` 传成 `0.5`，把 `0.001` 这种人类可读小数直接传给 `amounts_in`）。
2) 当前错误信息缺少关键上下文（HTTP status code、请求 URL、响应 body/错误字段），agent 无法快速定位错误原因并自我纠错。

## 目标（Goals）
- 让 agent 仅通过 MCP tool 的描述就能正确构造请求（尤其是 `pendle_convert_v2`）。
- 在 Pendle API 返回非 2xx 时，MCP 返回的错误信息应包含足够上下文帮助 agent 自我修复（至少包含 status code、URL、响应 body 摘要）。
- 对常见的输入错误进行本地快速校验（例如 `amounts_in` 传入小数），在不发起网络请求的情况下给出可操作的错误提示。

## 非目标（Non-goals）
- 不新增任何“写入/执行交易”的能力（本项目仍保持只读与不广播交易）。
- 不改变 Pendle 官方 API 的业务逻辑与行为（仅改进本项目的参数说明/校验/错误包装）。
- 不引入重量级依赖或复杂的金额解析逻辑（例如自动把人类可读小数转换为最小单位整数）。

## 用户故事（User Stories）
- 作为 agent，我看到 `pendle_convert_v2` 的说明就知道：
  - `slippage` 用小数比例表示（例如 0.5% = 0.005）
  - `amounts_in` 必须是 token 最小单位的整数（字符串），不能传 `"0.001"`
- 作为开发者/使用者，当调用失败时我能直接看到：
  - 是 400/401/429/5xx 哪一种
  - 实际请求的 URL
  - Pendle API 响应 body（或其摘要）

