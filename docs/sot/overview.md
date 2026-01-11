# 项目概览（SOT）

Last Updated: 2026-01-11

## 项目是什么
`pendle-mcp` 是一个 MCP Server（stdio），数据源接入 Pendle 官方 API v2（`https://api-v2.pendle.finance/core`），对外提供只读数据查询类 tools。

## Repo 列表与职责（与 docmap 对齐）
- `pendle-mcp`：Python MCP server + Pendle API client（入口：`src/pendle-mcp/src/pendle_mcp/`）

## 本地开发最小路径（只到开发自测）
- `conda activate pendle-mcp`
- `cd src/pendle-mcp`
- `pip install -e ".[dev]"`
- `pytest`
- `python -m pendle_mcp`（stdio 运行）
