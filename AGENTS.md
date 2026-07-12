# 子仓面包屑

这个仓属于个人 fleet。**完整上下文先读 `/Users/leavan/work/personal/fleet/AGENTS.md` 与 `/Users/leavan/work/personal/fleet/STATE.md`**；本文件只放 pendle-mcp 的局部约定。（同目录下也有等价的 `CLAUDE.md`。）

## 本仓职责

CLI + stdio MCP server，把 Pendle 官方 API v2 封成只读查询能力（市场 / 资产 / 价格 / OHLCV / 持仓 / PnL / limit order / vePENDLE 等）。本机以 `pendle` CLI 暴露（wrapper 在 `~/ns/personal/bin/pendle`，凭据从 0600 secret.env 注入；skill 正本在 `~/ns/personal/skills/pendle/`）；MCP 注册已于 2026-07-07 退役（决策见 `~/work/charter/TOOLING.md`），server 代码保留。

代码、模块结构、tool 清单、参数约定、环境变量见 [README.md](README.md)。

## 工作约定

- **普通 Python 库**，没有专门的 doc-driven workflow —— 直接改代码、跑测试、提 PR 就行。
- 任何改动开新分支 + PR 给用户 review，不直接动 `main`。
- 文档语言中文；JSON key、代码标识符、API 字段名按官方英文。
- 测试：`cd src/pendle-mcp && pytest`。
- API 对齐变化（新端点、参数语义改动）直接改代码 + 更新 README 的 tools 表 + 参数约定章节，不再生成 intent / spec_delta / tasks 三件套。
