# pendle-mcp 补全 Pendle 官方 API（GET / 只读）技术变更计划（Plan）

## 0. 背景与目标（简短即可）
- 背景：`pendle-mcp` 已完成最小可用版本（接入 Pendle 官方 API v2），但当前仅覆盖少量核心 endpoints；需要在“只读（GET）”前提下补全官方 API 的查询能力。
- 目标：
  - 在 `pendle-mcp` 中补全 Pendle 官方 API v2（`/core/docs`）里 **只读 GET** endpoints 的 MCP tools 覆盖
  - 每个 tool 参数与官方 OpenAPI 对齐（避免拍脑袋），并提供最小单测覆盖（mock HTTP）
- 非目标：
  - 不实现 POST/交易创建类接口（例如限价单创建）
  - 不实现语义上可能触发链上动作/生成交易的接口（即使它是 GET，也视为非只读）
  - 不引入 `pendle_raw_request` 透传

## 1. 影响范围（必须）
- 影响的 repo（来自 docmap，可多项）：`pendle-mcp`
- 影响的模块/目录/文件（按 repo 分组列出即可）：
  - repo: `pendle-mcp`
    - `src/pendle_mcp/pendle_api.py`：补充 endpoint 封装方法（只读 GET）
    - `src/pendle_mcp/server.py`：新增 MCP tools（只读 GET）
    - `tests/*`：补充单测（mock HTTP；覆盖路径拼接、query 参数映射、错误处理）
- 外部可见变化（如适用：API/CLI/配置/数据格式）：
  - 新增一批 `pendle_get_*` MCP tools（只读查询）

## 2. 方案与改动点（必须）
说明：实现将按批次推进；每批次写入前需先列出本批次改动文件/影响并征求确认。

数据源与文档：
- Pendle 官方 API v2 docs（OpenAPI 内嵌于页面）：https://api-v2.pendle.finance/core/docs
- baseUrl：`https://api-v2.pendle.finance/core`

实现原则：
- 只读：仅调用语义只读的 GET endpoints（即使某些 action endpoint 设计成 GET，也不纳入本需求）
- 参数对齐：严格按 OpenAPI 的 query/path 参数实现（命名上对 MCP 使用 snake_case；对外请求保持官方字段）
- 统一 client：所有 HTTP 调用只通过 `PendleApiClient`，集中处理 timeout、最小重试、非 2xx 错误、JSON 解析错误

计划覆盖的只读 GET endpoints → MCP tools（按官方 tag 分组；已存在的标注“已实现”）：

- Chains
  - `pendle_get_chains` → `GET /v1/chains`（已实现）

- Markets
  - `pendle_get_markets_all` → `GET /v1/markets/all`（已实现）
  - `pendle_get_markets_points_market` → `GET /v1/markets/points-market`
  - `pendle_get_market_data_v2` → `GET /v2/{chainId}/markets/{address}/data`
  - `pendle_get_market_historical_data_v2` → `GET /v2/{chainId}/markets/{address}/historical-data`

- Assets / Prices
  - `pendle_get_assets_all` → `GET /v1/assets/all`（已实现）
  - `pendle_get_asset_prices` → `GET /v1/prices/assets`（已实现）
  - `pendle_get_prices_ohlcv_v4` → `GET /v4/{chainId}/prices/{address}/ohlcv`

- Transactions
  - `pendle_get_user_pnl_transactions` → `GET /v1/pnl/transactions`
  - `pendle_get_market_transactions_v5` → `GET /v5/{chainId}/transactions/{address}`

- Dashboard
  - `pendle_get_user_positions` → `GET /v1/dashboard/positions/database/{user}`
  - `pendle_get_merkle_claimed_rewards` → `GET /v1/dashboard/merkle-claimed-rewards/{user}`

- Limit Orders（只读/分析类）
  - `pendle_get_limit_orders_all_v2` → `GET /v2/limit-orders`
  - `pendle_get_limit_orders_archived_v2` → `GET /v2/limit-orders/archived`
  - `pendle_get_limit_orders_book_v2` → `GET /v2/limit-orders/book/{chainId}`
  - `pendle_get_limit_orders_maker_limit_orders` → `GET /v1/limit-orders/makers/limit-orders`
  - `pendle_get_limit_orders_taker_limit_orders` → `GET /v1/limit-orders/takers/limit-orders`

- SDK（只读查询类）
  - `pendle_get_supported_aggregators` → `GET /v1/sdk/{chainId}/supported-aggregators`
  - `pendle_get_market_tokens` → `GET /v1/sdk/{chainId}/markets/{market}/tokens`
  - `pendle_get_swapping_prices` → `GET /v1/sdk/{chainId}/markets/{market}/swapping-prices`
  - `pendle_get_pt_cross_chain_metadata` → `GET /v1/sdk/{chainId}/cross-chain-pt-metadata/{pt}`
  - `pendle_convert_v2` → `GET /v2/sdk/{chainId}/convert`

- Ve Pendle
  - `pendle_get_ve_pendle_data_v2` → `GET /v2/ve-pendle/data`
  - `pendle_get_ve_pendle_market_fees_chart` → `GET /v1/ve-pendle/market-fees-chart`

- Statistics
  - `pendle_get_distinct_user_from_token` → `GET /v1/statistics/get-distinct-user-from-token`

明确排除（不做）：
- 所有 POST endpoints（例如 `/v1/limit-orders/makers/limit-orders` 等）
- 语义上可能生成交易/触发动作的 GET endpoints：
  - `GET /v1/sdk/{chainId}/limit-order/cancel-all`
  - `GET /v1/sdk/{chainId}/limit-order/cancel-single`
  - `GET /v1/sdk/{chainId}/redeem-interests-and-rewards`
  - `GET /v2/sdk/{chainId}/swap-pt-cross-chain`

批次建议（可按实现难度/优先级调整）：
- Batch 1：Markets / Prices / Transactions（新增 5~6 个 tools）
- Batch 2：Dashboard / Limit Orders（新增 5~6 个 tools）
- Batch 3：SDK / Ve Pendle / Statistics（新增 6~7 个 tools）

## 3. 自测与验收口径（必须，可执行）
- 本地自测步骤（命令/操作）：
  - `cd src/pendle-mcp`
  - `conda activate pendle-mcp`
  - `pip install -e ".[dev]"`
  - `pytest`
- 自测记录（2026-01-11）：
  - `python -m compileall -q src && pytest`：27 passed
  - MCP 联调：`list_tools` 返回 25 个 tools；`call_tool pendle_get_ve_pendle_data_v2` 调用成功（HTTP 200）
- 关键用例清单：
  - 新增的每个 tool 在 mock HTTP 下能正确拼接 path 与 query 参数
  - 非 2xx、网络错误、JSON 错误能返回清晰错误（不崩溃）
  - `codex mcp` 联调：能列出 tools，且至少 1~2 个新 tool 在联网情况下调用成功（可选）
- 通过标准：
  - 单测通过：`pytest` 全绿
  - Tools 覆盖：计划覆盖的 tool 全部可被 MCP 列出（名称匹配）

交付摘要口径（固定）：实现完成后，必须输出交付摘要，包含：
- 实际改动清单（按 repo 列出关键文件/模块）
- 自测步骤与结果
- 对照本节“通过标准”的逐条结论
- 已知风险/未决事项（如有必须列出）

约束：用户验收通过前，不更新 SOT，不归档 WIP。

## 4. SOT 更新清单（必须）
用户验收通过后，要把“最终事实”沉淀到 SOT（至少文件级，V1 不要求精确到小节）：

- `docs/sot/overview.md`：补充“提供的 MCP tools 列表（或分类）”与本地最小使用说明
- `docs/sot/architecture.md`：补充新增模块/接口边界（如增加了更多 API client 方法、参数/枚举）

## 5. 完成后归档动作（固定）
实现完成并完成基本自测后：
1) 输出交付摘要并请求用户验收
2) 用户验收通过后，按第 4 节更新 SOT
3) 更新第 6 节检查单（必须全勾）
4) 将整个目录从 `docs/wip/20260111-pendle-api-get-coverage/` 移动到 `docs/archive/20260111-pendle-api-get-coverage/`

## 6. WIP 检查单（必须全勾才能归档）
- [x] plan.md 已确认（PLAN 闸门已通过）
- [x] 代码改动已完成（IMPLEMENT 完成）
- [x] 基本自测已完成（记录命令/步骤与结果）
- [x] 已输出交付摘要并且用户验收通过（VERIFY 闸门已通过）
- [x] SOT 已更新（按第 4 节执行；已更新：`docs/sot/overview.md`、`docs/sot/architecture.md`）
- [x] 已归档：wip → archive（目录已移动）
