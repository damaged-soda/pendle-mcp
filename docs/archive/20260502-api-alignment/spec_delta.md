# Specification Delta: Pendle 官方 API 对齐升级

> **目标 SOT 文件**: `./docs/sot/overview.md` 与 `./docs/sot/architecture.md`
> **基于意图**: `intent.md`

## 1. 变更摘要 (Synopsis)

对照 Pendle 官方 OpenAPI（2026-05-02 抓取）和实际 HTTP 探测结果，按 2026-05-02 用户拍板的"删旧 + 强制升级 + 不接 spendle_user"方案：

* **新增 4 个 MCP tool**：`pendle_get_market_historical_data_v3`、`pendle_get_merkle_rewards`、`pendle_get_spendle_data`、`pendle_get_user_pnl_gained_positions`。
* **修改 1 个 MCP tool 的端点指向（破坏性）**：`pendle_get_markets_all` 改为调 `/v2/markets/all`，新增 `order_by / skip / limit` 参数，响应结构由 `{markets:[…]}` 变为 `{total, limit, skip, results}`。
* **删除 3 个 MCP tool（破坏性）**：
  * `pendle_get_market_historical_data_v2` → 由 `_v3` 取代
  * `pendle_get_merkle_claimed_rewards` → 由 `pendle_get_merkle_rewards` 取代（新端点同时返回 claimed + claimable）
  * （`pendle_get_markets_all` 名称保留但路径与响应结构都变）
* **不变量补充**：architecture 显式列出"sPENDLE rewards 端点（`/v1/spendle/{address}`）的 `multiTokenProof` 含动作语义气味，本项目不接入"，并保留既有"只读"原则。
* **不接入清单**：列出 6 类构造 tx payload / POST 类端点不接入的理由（与现有"只读"不变量一致），新增 `/v1/spendle/{address}`。

## 2. 需求变更 (Requirements Delta)

### 🟢 ADDED Requirements (新增需求)

#### Requirement: `pendle_get_market_historical_data_v3` 工具

The system **SHALL** 提供 `pendle_get_market_historical_data_v3` MCP tool，调用 `GET /v3/{chainId}/markets/{address}/historical-data`，参数与 `_v2` 同名加新增 `include_apy_breakdown: bool | None`。`time_frame` 仍接受 `hour/day/week` 与别名 `1h/1d/1w`（本地规范化）。

##### Scenario: 默认调用

* **GIVEN**: 用户调用 `pendle_get_market_historical_data_v3(chain_id=1, address="0x...", time_frame="1h")`
* **WHEN**: client 把 `1h` 规范化为 `hour`，发起 `GET /v3/1/markets/0x.../historical-data?time_frame=hour`
* **THEN**: 返回 `{total, timestamp_start, timestamp_end, results: [...]}`

##### Scenario: 启用 apy breakdown

* **GIVEN**: 用户传 `include_apy_breakdown=True`
* **WHEN**: client 发起请求时附带 `includeApyBreakdown=true`
* **THEN**: 响应字段中包含 apy breakdown 子项

#### Requirement: `pendle_get_merkle_rewards` 工具

The system **SHALL** 提供 `pendle_get_merkle_rewards` MCP tool，调用 `GET /v1/dashboard/merkle-rewards/{user}`，返回 `{claimableRewards, claimedRewards}`。

##### Scenario: 同时拿到两类奖励

* **GIVEN**: 用户调用 `pendle_get_merkle_rewards(user="0x...")`
* **WHEN**: client 发 `GET /v1/dashboard/merkle-rewards/0x...`
* **THEN**: 返回对象包含两个 list：`claimableRewards`（待领取）和 `claimedRewards`（已领取）

#### Requirement: `pendle_get_spendle_data` 工具

The system **SHALL** 提供 `pendle_get_spendle_data` MCP tool，调用 `GET /v1/spendle/data`，无参，返回 sPENDLE 总量、APR、buyback、历史等聚合数据。

##### Scenario: 默认

* **GIVEN**: 用户调用 `pendle_get_spendle_data()`
* **WHEN**: client 发 `GET /v1/spendle/data`
* **THEN**: 返回包含 `totalPendleStaked / totalStakedInSpendle / virtualSpendleFromVependle / lastEpochApr / lastEpochBuybackAmount / sPendleHistoricalData / vependleHistoricalData` 的对象

#### Requirement: `pendle_get_user_pnl_gained_positions` 工具

The system **SHALL** 提供 `pendle_get_user_pnl_gained_positions` MCP tool，调用 `GET /v1/pnl/gained/{user}/positions`，返回 `{total, positions}`。

##### Scenario: 默认

* **GIVEN**: 用户调用 `pendle_get_user_pnl_gained_positions(user="0x...")`
* **WHEN**: client 发 `GET /v1/pnl/gained/0x.../positions`
* **THEN**: 返回 `{total, positions: [...]}`，每个 position 含 net gain / total spent / max capital / trading volume / unclaimed rewards

#### Requirement: `pendle_health` 默认探测扩展

The system **SHALL** 在 `pendle_health` 默认无参探测列表中加入 `/v1/spendle/data`。

---

### 🟡 MODIFIED Requirements (修改需求)

#### Requirement: `pendle_get_markets_all` 端点重指向（破坏性）

> **OLD Behavior**: 调用 `GET /v1/markets/all`，参数 `chain_id / ids / is_active`，响应 `{markets: [...]}`（无分页）。
> **NEW Behavior**: 调用 `GET /v2/markets/all`，参数新增 `order_by / skip / limit`，响应改为 `{total, limit, skip, results: [...]}`（分页）。

##### Scenario: 不带参数

* **GIVEN**: 用户调用 `pendle_get_markets_all()`
* **WHEN**: client 发起 `GET /v2/markets/all`
* **THEN**: 返回 `{total, limit, skip, results: [...]}`，`results` 为 API 端默认页（默认 limit=20）

##### Scenario: 分页

* **GIVEN**: 用户调用 `pendle_get_markets_all(skip=20, limit=50, chain_id=1)`
* **WHEN**: client 发起 `GET /v2/markets/all?skip=20&limit=50&chainId=1`
* **THEN**: 响应中 `skip=20, limit=50`，且 `results` 全部为 `chainId=1` 的 market

##### Impact Analysis

* 受影响代码：`src/pendle_mcp/server.py` `pendle_get_markets_all` tool；`PendleApiClient.get_markets_all`
* 调用方影响：**破坏性**。响应结构由顶层 `markets` 变为 `results`，调用方需要适配。
* 数据迁移：无

---

### 🔴 REMOVED Requirements (移除需求)

#### Requirement: `pendle_get_market_historical_data_v2`

* **Reason**: 由 `pendle_get_market_historical_data_v3` 强制替代。`/v2/{chainId}/markets/{address}/historical-data` 已退出 Pendle OpenAPI 文档（虽然实测仍在线），按用户决策不保留过渡别名。
* **Impact**: 调用方必须改用 `_v3`。新方法签名兼容旧的，仅多一个可选 `include_apy_breakdown` 参数。

#### Requirement: `pendle_get_merkle_claimed_rewards`

* **Reason**: 由 `pendle_get_merkle_rewards` 强制替代。新端点同时返回 `claimableRewards + claimedRewards`，覆盖旧能力且语义更清晰。
* **Impact**: 调用方必须改用 `pendle_get_merkle_rewards`，并从返回值的 `claimedRewards` 字段读取等价数据。

#### Requirement: `PendleApiClient.get_market_historical_data_v2`、`PendleApiClient.get_merkle_claimed_rewards`

* **Reason**: 同上，client 层方法一并删除，避免代码遗留。
* **Impact**: 内部代码无其他调用方（已确认）。

---

### ⛔ EXPLICITLY OUT OF SCOPE (明确拒绝接入)

以下端点**不**新增对应 MCP tool，理由是与 SOT architecture.md 中"只读、排除 POST 与语义可能触发动作的 GET（cancel/redeem/swap 类）"不变量冲突：

| 端点 | 方法 | 拒绝理由 |
|---|---|---|
| `/v1/spendle/{address}` | GET | 返回的 `multiTokenProof` 含动作语义气味；用户判断"用处不大" |
| `/v3/sdk/{chainId}/convert` | POST | 违反 POST 排除原则；旧 v2 GET 仍可用 |
| `/v2/sdk/{chainId}/swap-pt-cross-chain` | GET | swap 类 tx payload 构造 |
| `/v1/sdk/{chainId}/redeem-interests-and-rewards` | GET | redeem 类 tx payload 构造 |
| `/v1/sdk/{chainId}/limit-order/cancel-all` | GET | cancel 类 tx payload 构造 |
| `/v1/sdk/{chainId}/limit-order/cancel-batch` | POST | cancel + POST 双重排除 |
| `/v1/sdk/{chainId}/limit-order/cancel-single` | GET | cancel 类 tx payload 构造 |
| `/v1/limit-orders/makers/limit-orders` | POST | 创建限价单 |
| `/v1/limit-orders/makers/generate-limit-order-data` | POST | 创建限价单数据 |
| `/v1/limit-orders/makers/generate-scaled-order-data` | POST | 创建限价单数据 |

## 3. 数据结构/API 变更 (Schema/API Changes)

### 修改 client 方法（`pendle_api.py`）

```python
class PendleApiClient:
    # 端点重指向：/v1/markets/all → /v2/markets/all（破坏性，响应结构变化）
    async def get_markets_all(
        self,
        *,
        chain_id: int | None = None,
        ids: list[str] | None = None,
        is_active: bool | None = None,
        order_by: str | None = None,
        skip: int | None = None,
        limit: int | None = None,
    ) -> Any: ...   # GET /v2/markets/all
```

### 新增 client 方法（`pendle_api.py`）

```python
class PendleApiClient:
    async def get_market_historical_data_v3(
        self,
        *,
        chain_id: int,
        address: str,
        time_frame: str | None = None,
        timestamp_start: str | None = None,
        timestamp_end: str | None = None,
        fields: list[str] | None = None,
        include_fee_breakdown: bool | None = None,
        include_apy_breakdown: bool | None = None,
    ) -> Any: ...   # GET /v3/{chainId}/markets/{address}/historical-data

    async def get_merkle_rewards(self, *, user: str) -> Any:
        ...   # GET /v1/dashboard/merkle-rewards/{user}

    async def get_spendle_data(self) -> Any:
        ...   # GET /v1/spendle/data

    async def get_user_pnl_gained_positions(self, *, user: str) -> Any:
        ...   # GET /v1/pnl/gained/{user}/positions
```

### 删除 client 方法（`pendle_api.py`）

* `get_market_historical_data_v2`
* `get_merkle_claimed_rewards`

### 新增对外 MCP tool 名称

* `pendle_get_market_historical_data_v3`
* `pendle_get_merkle_rewards`
* `pendle_get_spendle_data`
* `pendle_get_user_pnl_gained_positions`

### 删除对外 MCP tool 名称

* `pendle_get_market_historical_data_v2`
* `pendle_get_merkle_claimed_rewards`

### `pendle_health` 默认探测列表

* 新增条目：`pendle_get_spendle_data`（无参，路径 `/v1/spendle/data`）。
* 同步处理任何已删除工具对应的探测条目（如有）。

### 测试

* `tests/test_pendle_api_client.py` 新增 4 个 mock-based 测试用例（4 个新方法）。
* 修改 `get_markets_all` 现有测试用例，断言 URL 改为 `/v2/markets/all`、新增分页参数。
* 删除 `get_market_historical_data_v2` 与 `get_merkle_claimed_rewards` 的测试用例。
