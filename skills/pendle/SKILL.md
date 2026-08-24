---
name: pendle
description: >-
  Pendle 官方 API v2 只读查询：市场清单与数据(含链上 30d APY 校准)、PT/YT/LP
  资产与价格、OHLCV、用户持仓与 PnL 聚合、limit order、vePENDLE/sPENDLE、
  swap 报价(convert)、新市场机会扫描。查 Pendle 市场/收益率/持仓、评估 PT/YT
  交易、扫新市场机会时使用。
argument-hint: <subcommand>
user-invocable: true
allowed-tools: Bash(pendle:*)
---

# Pendle API v2 只读 CLI

使用 `pendle` CLI(personal 域)查询 Pendle 官方 API v2 只读数据。凭据(代理 +
`RPC_URL_<chainid>`)由 wrapper 从 0600 文件注入,不要把 RPC URL / key 写进命令
行、URL 或文件。只读:不签名、不广播交易。

## CLI 摘要

```bash
pendle <subcommand> [options]   # 全部输出 JSON;pendle --help 看环境变量与全表
```

| 子命令 | 用途 |
|---|---|
| `get-chains` | 支持的链 ID 列表 |
| `health` | API 端点健康检查 |
| `get-markets-all` | 市场清单(分页 `{total,limit,skip,results}`,`--ids` 用 `<chainId>-<address>`) |
| `get-markets-points-market` | 积分市场清单 |
| `get-market-data-v2` | 单市场数据;响应始终附 `u_actual_30d_chain` 链上 30d APY 校准 |
| `get-market-historical-data-v3` | 市场时间序列(`--time-frame hour|day|week|1h|1d|1w`) |
| `detect-new-market-opportunities` | 扫年轻+有流动性的市场,找链上真相未被定价的机会 |
| `get-assets-all` / `get-asset-prices` | PT/YT/LP/SY 资产元数据 / USD 价格(`--asset-type`) |
| `get-prices-ohlcv-v4` | 资产 OHLCV(`--parse-results` 把 CSV 解析成结构化行) |
| `get-user-pnl-transactions` | 用户 PnL 交易原始翻页器 |
| `get-user-pnl-summary` | 用户全历史 PnL 聚合(`--group-by action|tx_hash`,绝不截断) |
| `get-market-transactions-v5` | 市场交易流(`--transaction-type`/`--action`/`--min-value`) |
| `get-user-pnl-gained-positions` | 用户各持仓已实现收益 |
| `get-user-positions` / `get-merkle-rewards` | 用户持仓 / merkle 奖励(claimable+claimed) |
| `get-limit-orders-all-v2` / `-archived-v2` | limit order 全量 / 归档(分析用) |
| `get-limit-orders-book-v2` | 订单簿(`--precision-decimal` 必填,`--include-amm`) |
| `get-limit-orders-maker-limit-orders` / `-taker-limit-orders` | maker 挂单 / taker 可吃单 |
| `get-supported-aggregators` / `get-market-tokens` / `get-swapping-prices` / `get-pt-cross-chain-metadata` | SDK 查询面 |
| `convert-v2` | 万能 swap 报价(默认剥掉 calldata,广播才 `--include-tx`) |
| `get-ve-pendle-data-v2` / `get-ve-pendle-market-fees-chart` / `get-spendle-data` | vePENDLE / sPENDLE |
| `get-distinct-user-from-token` | 持有某 token 的去重用户数 |
| `list` / `show` / `call` | 内省:列 tool、看签名、按 tool 名传 JSON kwargs 直调 |

## 常用例子

```bash
# 链 ID 清单
pendle get-chains

# 主网活跃市场按 TVL 排序取前 10
pendle get-markets-all --chain-id 1 --is-active --order-by 'totalTvl:-1' --limit 10

# 单市场数据 + 链上 30d APY 校准(u_actual_30d_chain / u_ui_vs_chain_ratio)
pendle get-market-data-v2 --chain-id 1 --address 0x34280882267ffa6383b363e278b027be083bbe3b

# 资产价格(--ids 元素必须是 <chainId>-<address>)
pendle get-asset-prices --ids '["1-0xb253eff1104802b97ac7e3ac9fdd73aece295a2c"]'

# PT 日线 OHLCV 并解析 CSV
pendle get-prices-ohlcv-v4 --chain-id 1 --address 0x... --time-frame 1d --parse-results

# 用户全历史 PnL 聚合(按 action)
pendle get-user-pnl-summary --user 0x... --group-by action

# swap 报价:slippage 是比例小数,amounts-in 是最小单位整数字符串
pendle convert-v2 --chain-id 1 --slippage 0.005 \
  --tokens-in '["0xcbc72d92b2dc8187414f6734718563898740c0bc"]' \
  --amounts-in '["1000000000000000000"]' \
  --tokens-out '["0xb253eff1104802b97ac7e3ac9fdd73aece295a2c"]' --enable-aggregator

# 扫主网 30 天内新市场机会(需 archive RPC)
pendle detect-new-market-opportunities --chain-id 1 --min-tvl-usd 250000
```

## 注意

- **APY 字段语义**:API 返回的 `underlyingApy`/`aggregatedApy` 等是短 sliding-window
  展示值,不是链上真相;NAV-discrete 类 underlying 会被高估 2-6×。要真相看
  `get-market-data-v2` 附带的 `u_actual_30d_chain`(≈UI 一致时 `u_ui_vs_chain_ratio`≈1)。
- 数组参数(`--ids`/`--fields`/`--tokens-in`/`--amounts-in`/`--tokens-out`/`--aggregators`)
  必须是 JSON 数组字符串;`--ids` 元素格式 `<chainId>-<address>`,裸地址会空结果。
- `convert-v2`:`--slippage` 是 `[0,1]` 比例小数;`--amounts-in` 必须是最小单位
  base-10 整数字符串(decimals=18 时 `0.001` → `"1000000000000000"`)。
- 链上校准(`get-market-data-v2`/`detect-new-market-opportunities`)需要该链
  `RPC_URL_<chainid>` 指向 archive 节点,wrapper 已注入;没配的链会返回
  `u_actual_chain_error` 而不是失败。
- 默认 `None` 的布尔 flag 是三态:`--is-active`/`--no-is-active`,不传就不发该参数。
- 内核与参数语义详见 `~/work/personal/pendle-mcp/README.md`。
