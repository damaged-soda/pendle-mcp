# pendle-mcp 初始化（官方 API 接入）技术变更计划（Plan）

## 0. 背景与目标（简短即可）
- 背景：当前项目处于未初始化阶段，`docs/` 与 `src/` 多为占位符；需要启动一个 `pendle-mcp` MCP Server，但数据源先接入 Pendle 官方 API v2。
- 目标：
  - 建立单 repo：`src/pendle-mcp/`，具备可安装、可构建、可运行的 MCP Server 骨架
  - 封装 Pendle 官方 API v2 的最小可用客户端，并以 MCP Tools 形式暴露给上层使用
  - 提供最小本地自测路径（不写部署/上线）
- 非目标：
  - 暂不接入链上 RPC/合约读数、Subgraph、多数据源聚合
  - 暂不做性能压测、生产级缓存/限流治理（只做基础超时与错误处理）

## 1. 影响范围（必须）
- 影响的 repo（来自 docmap，可多项）：`pendle-mcp`
- 影响的模块/目录/文件（按 repo 分组列出即可）：
  - repo: `pendle-mcp`
    - `pyproject.toml` / `src/pendle_mcp/*`：MCP server 与 Pendle API client
    - `tests/*`：最小单测（API client 的成功/失败/超时）
- 外部可见变化（如适用：API/CLI/配置/数据格式）：
  - 新增一个 MCP Server（stdio），提供 Pendle 数据查询类 tools（只读）
  - 配置：Pendle API Base URL、超时等（以环境变量为主）

## 2. 方案与改动点（必须）
说明：实现将按批次推进；每批次写入前需先列出本批次改动文件/影响并征求确认。

Pendle 官方 API 文档（参考）：https://api-v2.pendle.finance/core/docs#description/recommended-way-to-fetch-data

技术栈（建议，PLAN 闸门可调整）：
- Python 3.11+
- MCP SDK：Python 版 MCP SDK（优先使用官方/主流实现）
- HTTP client：`httpx`（统一处理 timeout、重试等）
- 测试：`pytest`（配合 mock HTTP）

按 repo 分组：

- repo: `pendle-mcp`
  - 改动点：
    - 初始化项目结构与脚本（build/start/test）
    - 增加 `PendleApiClient`：封装 baseUrl、timeout、重试/错误归一化（最小实现）
    - 增加 MCP Tools（首版先做“最小可用 + 可扩展”）：
      - `pendle_get_*`：围绕“推荐获取方式”提供 2~5 个核心查询工具（具体 endpoint 与参数以官方文档为准，避免拍脑袋）
    - 基础可观测性：请求错误信息、超时提示（不引入复杂日志系统）
  - 新增/修改的接口或数据结构：无（首版仅新增）
  - 关键逻辑说明：
    - 所有对 Pendle API 的调用通过统一 client，集中处理超时、JSON 解析、错误结构
    - MCP tools 层只做参数校验（schema）与 client 调用编排，不在 tool 内堆业务逻辑

## 3. 自测与验收口径（必须，可执行）
- 本地自测步骤（命令/操作）：
  - `cd src/pendle-mcp`
  - `conda activate pendle-mcp`
  - `pip install -e ".[dev]"`
  - `pytest`
  - `python -m pendle_mcp`（启动 MCP server；如需联调，可用 MCP Inspector/客户端调用 tools）
- 自测记录（2026-01-11）：
  - `pytest`：6 passed
  - 联网冒烟：`pendle_get_chains` 调用成功；`pendle_get_markets_all` 调用成功（返回包含 `markets` 字段）
- 关键用例清单：
  - 能启动 server 且不报错
  - 至少一个核心 tool 能返回结构化 JSON（在联网情况下调用官方 API 成功）
  - 传入非法参数时，能返回清晰的参数错误（而不是崩溃/无信息）
- 通过标准：
  - 单测通过：`pytest` 全绿（至少覆盖：成功响应解析、非 2xx 错误、超时/网络错误）
  - 联网冒烟通过（可选但推荐）：本地调用 1~2 个核心 tool 能拿到合理数据

交付摘要口径（固定）：实现完成后，必须输出交付摘要，包含：
- 实际改动清单（按 repo 列出关键文件/模块）
- 自测步骤与结果
- 对照本节“通过标准”的逐条结论
- 已知风险/未决事项（如有必须列出）

约束：用户验收通过前，不更新 SOT，不归档 WIP。

## 4. SOT 更新清单（必须）
用户验收通过后，要把“最终事实”沉淀到 SOT（至少文件级，V1 不要求精确到小节）：

- `docs/sot/overview.md`：补全项目是什么、repo 列表（`pendle-mcp`）、最小运行/自测命令
- `docs/sot/architecture.md`：补全模块边界（MCP server / tools / api client）、关键约束（只读、超时/错误策略、配置入口）

## 5. 完成后归档动作（固定）
实现完成并完成基本自测后：
1) 输出交付摘要并请求用户验收
2) 用户验收通过后，按第 4 节更新 SOT
3) 更新第 6 节检查单（必须全勾）
4) 将整个目录从 `docs/wip/20260111-pendle-mcp-init/` 移动到 `docs/archive/20260111-pendle-mcp-init/`

## 6. WIP 检查单（必须全勾才能归档）
- [x] plan.md 已确认（PLAN 闸门已通过）
- [x] 代码改动已完成（IMPLEMENT 完成）
- [x] 基本自测已完成（记录命令/步骤与结果）
- [x] 已输出交付摘要并且用户验收通过（VERIFY 闸门已通过）
- [x] SOT 已更新（按第 4 节执行；已更新：`docs/sot/overview.md`、`docs/sot/architecture.md`）
- [x] 已归档：wip → archive（目录已移动）
