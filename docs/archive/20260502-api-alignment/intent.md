# Intent: Pendle 官方 API 对齐升级（v2→v3、sPENDLE、merkle-rewards 语义修正）

> **状态**: DRAFT (草稿)
> **创建日期**: 2026-05-02
> **对应 Issue**: 无（自查发现，详见对话记录）

## 1. 背景与动机 (Context & Motivation)

* **现状**:
  * 当前 `pendle-mcp` 端点清单基于 2026-01 写入，对照官方 OpenAPI（`https://api-v2.pendle.finance/core/docs`）2026-05-02 抓取的 31 个端点，已经落后 1~3 个 minor 版本。
  * 实测：旧端点（`/v1/markets/all`、`/v2/.../historical-data`、`/v2/sdk/{chainId}/convert`、`/v1/dashboard/merkle-claimed-rewards/{user}`、`/v2/ve-pendle/data`、`/v1/ve-pendle/market-fees-chart` 等）大多还在线返回 200，但 OpenAPI 文档不再列出，属于"未公开承诺、随时可下线"的灰色区。
* **问题**:
  * 几个高频端点已有更新版本（v2→v3 historical-data；v1→v2 markets 分页版；新 `merkle-rewards` 同时返回 claimable + claimed）。
  * 完全没接到的新数据线：sPENDLE（取代 vePENDLE 的新质押产品）、`/v1/pnl/gained/{user}/positions`（用户已实现盈亏聚合）。
  * 现有 `pendle_get_merkle_claimed_rewards` 字段名是 `claimedRewards`，与新端点的 `claimableRewards`（待领取）字段语义完全不同——目前 tool 描述写"all merkle claimed rewards"易让模型以为查的是可领取奖励，存在**调用方误用风险**。
* **价值**:
  * 跟上文档现状，避免某天旧端点被关闭直接静默 404。
  * 暴露 sPENDLE 数据，配合现有 vePENDLE 工具形成完整质押视图。
  * 修正 merkle-rewards 语义混淆，给上层 LLM 调用提供明确的"可领取 vs 已领取"区分。

## 2. 核心目标 (Goals)

> **2026-05-02 用户拍板**：旧端点 tool 直接**删除并强制升级**（不保留兼容别名）；`pendle_get_spendle_user` **不接入**（用处不大，且 `multiTokenProof` 名字过于贴近"动作语义"边界）。

1. [ ] **historical-data**：新增 `pendle_get_market_historical_data_v3` 调 `/v3/{chainId}/markets/{address}/historical-data`，参数加 `include_apy_breakdown`；**删除** `pendle_get_market_historical_data_v2`。
2. [ ] **markets/all 分页版**：用 `pendle_get_markets_all` 直接重指向 `GET /v2/markets/all`（参数支持 `order_by/skip/limit`，响应 `{total, limit, skip, results}`）；不另起 `_v2` 后缀的 tool 名，旧 v1 路径直接淘汰。
3. [ ] **merkle-rewards**：新增 `pendle_get_merkle_rewards` 调 `/v1/dashboard/merkle-rewards/{user}`，返回 `{claimableRewards, claimedRewards}`；**删除** `pendle_get_merkle_claimed_rewards`。
4. [ ] **sPENDLE**：新增 `pendle_get_spendle_data`（`/v1/spendle/data`）。**`pendle_get_spendle_user` 不接入。**
5. [ ] **PnL gained**：新增 `pendle_get_user_pnl_gained_positions` 调 `/v1/pnl/gained/{user}/positions`。
6. [ ] **health check**：把 `pendle_get_spendle_data`（无参）加入 `pendle_health` 默认探测列表；剔除已删除工具对应的探测条目（如有）。
7. [ ] **SOT 同步**：更新 `docs/sot/overview.md` 工具清单、`docs/sot/architecture.md` 端点列表与不变量；归档本 wip。
8. [ ] **测试**：为新 client 方法补 mock 单测；删除已退役方法对应的测试。

## 3. 非目标 (Non-Goals / Out of Scope)

按 SOT 不变量"只读、排除 POST 与语义可能触发动作的 GET（cancel/redeem/swap）"，本次**明确不接入**：

* [ ] `POST /v3/sdk/{chainId}/convert`（新版 convert，方法变 POST）—— 旧 v2 GET 仍可用，保留。
* [ ] `GET /v2/sdk/{chainId}/swap-pt-cross-chain`（构造跨链 swap 链上 tx payload）。
* [ ] `GET /v1/sdk/{chainId}/redeem-interests-and-rewards`（构造 redeem tx payload）。
* [ ] `GET /v1/sdk/{chainId}/limit-order/cancel-{all,batch,single}`（取消限价单 tx payload；其中 cancel-batch 还是 POST）。
* [ ] `POST /v1/limit-orders/makers/limit-orders` / `generate-limit-order-data` / `generate-scaled-order-data`（创建限价单）。

不涉及：
* [ ] 修改现有 25 个 tool 的对外签名（除新增可选参数和文档）。
* [ ] 修改 PendleApiClient 的重试 / 错误归一化逻辑。
* [ ] 引入新的 `httpx` 之外的依赖。

## 4. 用户故事 (User Stories)

* **As a** LLM agent 调用 pendle-mcp 来分析 Pendle 数据
* **I want to** 拿到完整、最新版本的 markets/historical-data 字段（含 apy breakdown），以及 sPENDLE 质押全貌、用户已实现 PnL 视图，并且能明确区分"可领取 vs 已领取"的 merkle 奖励
* **So that** 我的分析既不会因为字段缺失而失真，也不会因为 tool 描述含糊导致把"已领取"误读为"可领取"

## 5. 风险评估 (Risks)

* **新增 5 个 tool 后，工具数量从 26 增到 31**，模型可能在"应该用 v2 还是 v3 markets"上花费额外 token。缓解：在两个 v2 tool 的 docstring 顶部明确互斥导引（例如"如果你需要分页，用 `_v2`；否则用旧版"）。
* **sPENDLE `/v1/spendle/{address}` 返回 `multiTokenProof`**，这是供上层签名调用 claim 的 merkle proof。技术上是只读 GET（仅获取数据、不签名/不广播），但 `multiTokenProof` 名字带"proof"易被误读成"动作类"。缓解：docstring 明确"仅返回 proof 数据；本 tool 不签名、不广播任何交易"，并在 SOT architecture 不变量段落里把 sPENDLE rewards 单独列出做澄清。
* **旧 `_v2` historical-data 端点**仍在线但已不在 OpenAPI 文档里。如果未来某天关闭，旧 tool 会突然 404。缓解：docstring 引导迁移到 `_v3`；同时在 `pendle_health` 把 v2 端点也加入定期巡检（探测到 404 即降级，不影响其他 tool）。
* **测试只 mock，不打真实 API**。本提案中所有"实测 200/404"结论已在对话上下文里完成，不会写进 CI；CI 仍然完全 offline。
