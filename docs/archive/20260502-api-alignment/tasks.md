# Implementation Plan: Pendle 官方 API 对齐升级

> **关联 Spec**: `spec_delta.md`
> **执行状态**: [x] Pending -> [ ] In Progress -> [ ] Verification -> [ ] Ready to Archive

> **2026-05-02 用户拍板**：删旧、强制升级；不接 `pendle_get_spendle_user`；单 PR。

## Phase 0: Git 起点

* [ ] 从 master 切出分支：`feat/api-alignment-20260502`
  * 建议命令：
    ```bash
    git checkout -b feat/api-alignment-20260502
    ```

## Phase 1: 准备与脚手架 (Preparation)

* [ ] **Context Check**: 确认 `docmap.yaml` 中 `./src/pendle-mcp` 路径存在（已确认）。
* [ ] **Dependency**: 无新增依赖。
* [ ] **Read 现有源文件**：`pendle_api.py`、`server.py`、`tests/test_pendle_api_client.py` 完整再过一遍，确认插入位置和现有命名风格。

## Phase 2: 核心逻辑实现 (Core Implementation)

### 2A. `pendle_api.py` 修改 1 个 / 新增 4 个 / 删除 2 个 client 方法

* [ ] **修改** `get_markets_all` 端点指向：`/v1/markets/all` → `/v2/markets/all`，新增 `order_by / skip / limit` 参数（破坏性，响应结构由 `{markets:[...]}` 变为 `{total, limit, skip, results:[...]}`）
* [ ] **新增** `get_market_historical_data_v3` —— `GET /v3/{chainId}/markets/{address}/historical-data`，沿用 `_v2` 的 `time_frame` 规范化逻辑（提取 helper 复用），新增 `include_apy_breakdown`
* [ ] **新增** `get_merkle_rewards` —— `GET /v1/dashboard/merkle-rewards/{user}`
* [ ] **新增** `get_spendle_data` —— `GET /v1/spendle/data`
* [ ] **新增** `get_user_pnl_gained_positions` —— `GET /v1/pnl/gained/{user}/positions`
* [ ] **删除** `get_market_historical_data_v2`
* [ ] **删除** `get_merkle_claimed_rewards`

### 2B. `server.py` 修改 1 个 / 新增 4 个 / 删除 2 个 MCP tool

* [ ] **修改** `pendle_get_markets_all`：暴露新增 `order_by / skip / limit` 参数；docstring 反映新端点和分页响应结构
* [ ] **新增** `pendle_get_market_historical_data_v3`（含 `time_frame` alias 处理与原 `_v2` 一致）
* [ ] **新增** `pendle_get_merkle_rewards`（docstring 明确同时返回 `claimableRewards + claimedRewards`）
* [ ] **新增** `pendle_get_spendle_data`
* [ ] **新增** `pendle_get_user_pnl_gained_positions`
* [ ] **删除** `pendle_get_market_historical_data_v2`
* [ ] **删除** `pendle_get_merkle_claimed_rewards`

### 2C. `pendle_health` 扩展

* [ ] 把 `pendle_get_spendle_data` 加入默认无参 health 探测列表
* [ ] 同步剔除已删除 tool 对应的探测条目（如 `pendle_health` 内部硬编码引用旧 tool）

### 2D. `tests/test_pendle_api_client.py` 调整

* [ ] **修改** `get_markets_all` 测试：URL 断言改为 `/v2/markets/all`，新增 `order_by/skip/limit` 编码断言
* [ ] **新增** `get_market_historical_data_v3`：URL 是 `/v3/...`、`include_apy_breakdown=true` 编码正确、`time_frame="1h"` 规范化为 `hour`
* [ ] **新增** `get_merkle_rewards`：URL 是 `/v1/dashboard/merkle-rewards/{user}`
* [ ] **新增** `get_spendle_data`：URL 是 `/v1/spendle/data`
* [ ] **新增** `get_user_pnl_gained_positions`：URL 是 `/v1/pnl/gained/{user}/positions`
* [ ] **删除** `get_market_historical_data_v2` 与 `get_merkle_claimed_rewards` 对应测试用例

## Phase 3: 验证 (Verification)

* [ ] **Automated Tests**: `cd src/pendle-mcp && pytest -q`，要求全绿
* [ ] **Live probe**（可选）：本地起 server 用 `pendle_health` 触发一次，确认 `/v1/spendle/data` 入围且活体可达；至少抽 1 个新 tool 真实调用
* [ ] **Lint**: 当前仓无强制 lint，跳过；保持现有代码风格

## Phase 4: 文档归档 (Documentation Merge)

* [ ] **SOT Update — `docs/sot/overview.md`**：
  * "提供的 MCP tools" 段落：删除 `_v2 historical` / `merkle_claimed_rewards`；新增 4 个 tool；调整 `pendle_get_markets_all` 描述为分页版
  * "Tool 参数与错误约定" 段落更新 markets/all 分页说明、historical-data 改 v3
  * Last Updated 改为 2026-05-02
* [ ] **SOT Update — `docs/sot/architecture.md`**：
  * 不变量段落保持现有"只读、排除 POST 与 cancel/redeem/swap 类"原则；显式列出本期决策"sPENDLE 用户级 rewards 端点（`/v1/spendle/{address}`）含 proof 数据，按只读保守边界不接入"
  * Last Updated 改为 2026-05-02
* [ ] **Archive**：`mv docs/wip/20260502-api-alignment docs/archive/20260502-api-alignment`
* [ ] **Cleanup**：确认 `docs/wip/` 下无残留中间文件
* [ ] **Ready**：通知用户 PR 可推送

## Git 提交建议

```bash
# Phase 0
git checkout -b feat/api-alignment-20260502

# Phase 2 完成后
git add src/pendle-mcp/src/pendle_mcp/pendle_api.py \
        src/pendle-mcp/src/pendle_mcp/server.py \
        src/pendle-mcp/tests/test_pendle_api_client.py
git commit -m "feat(api)!: align with Pendle OpenAPI 2026-05-02

- markets_all now hits /v2/markets/all with paginated response
- replace historical_data_v2 with v3 (adds include_apy_breakdown)
- replace merkle_claimed_rewards with merkle_rewards (claimable+claimed)
- add spendle_data, user_pnl_gained_positions
- extend pendle_health probes with /v1/spendle/data

BREAKING CHANGE: removes pendle_get_market_historical_data_v2,
pendle_get_merkle_claimed_rewards, and changes the response shape
of pendle_get_markets_all to {total, limit, skip, results}."

# Phase 4
git add docs/sot/overview.md docs/sot/architecture.md \
        docs/archive/20260502-api-alignment
git rm -r docs/wip/20260502-api-alignment
git commit -m "docs(sot): record API alignment and archive 20260502 wip"

# 推送 + 开 PR
git push -u origin feat/api-alignment-20260502
gh pr create --fill
```
