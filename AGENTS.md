# 文档驱动 AI 编码规则（V1）

## 0）空间分离（必须遵守）
- 文档（定义空间）：./docs/
- 代码（实现空间）：./src/（每个 ./src/<repo> 可为独立 Git 仓库；禁止执行任何 git 命令）
- 仓库映射（单一事实源）：./docmap.yaml（先读它再行动，不得猜 repo 路径）

## 1）文档结构（目录即状态）
- SOT（当前事实，唯一权威）：./docs/sot/
- WIP（进行中变更）：./docs/wip/YYYYMMDD-topic/
  - 每个 WIP 需求目录必须包含：plan.md（含检查单）
- Archive（已完成归档）：./docs/archive/YYYYMMDD-topic/
  - 仅在“用户验收通过 + SOT 已更新 + 检查单全勾选”后才允许移动整个 WIP 目录到此处
- 模板：./docs/_templates/plan.md

## 2）状态锚点（防遗忘，必须）
- 每次回复开头必须输出“状态锚点”，格式固定如下（不省略）：
  - WIP：<当前需求目录路径；若尚未创建则写“无”>
  - 阶段：PLAN | IMPLEMENT | VERIFY | SOT | ARCHIVE
  - 下一步：<一句话说明你接下来要做什么>

## 3）会话启动默认动作（尽量静默）
当在项目根目录启动 Codex 时，按顺序执行：
1) 读取 ./docs/README.md
2) 读取 ./docs/sot/index.md 及其列出的 SOT 文件
3) 读取 ./docmap.yaml；列出可用 repo，并检查 ./src/<repo> 目录是否存在
   - 若 repo 目录不存在：提示用户自行 clone 到 ./src/；不得执行 git
4) 扫描 ./docs/wip/，列出当前进行中的需求目录（如有）
5) 若对话很长或易偏航：优先通过重新读取当前 WIP 的 plan.md 来恢复上下文（必要时使用 Codex 的 /compact）,恢复后必须对照 plan.md 第 6 节检查单，确认当前阶段与下一步。


## 4）任何“变更请求”的强制流程
当用户提出任何会改变行为/代码的请求时，必须按顺序执行：

1) 创建需求目录：./docs/wip/YYYYMMDD-topic/

2) 基于 ./docs/_templates/plan.md 创建并填写：./docs/wip/YYYYMMDD-topic/plan.md（包含检查单）

3) 请求用户审核并确认 plan.md（PLAN 闸门）
- 未确认前不得改代码

4) 实现阶段（IMPLEMENT，必须分批确认写入）
- 在写入任何代码/大量修改前，先输出“本批次变更计划”（将改哪些 repo/文件、做什么、影响/风险），征求用户确认后才允许写入
- 写入只发生在 ./src/<repo>/

5) 自测与交付摘要（VERIFY，验收闸门）
- 完成实现与基本自测后，必须输出“交付摘要”，包含：
  - 实际改动清单（按 repo 列出关键文件/模块）
  - 自测步骤与结果
  - 对照 plan.md 第 3 节“通过标准”的逐条结论
  - 已知风险/未决事项（如有必须明示）
- 询问用户是否“验收通过，允许更新 SOT 并归档”
- 未通过则保持 WIP，不得更新 SOT/归档，按反馈继续迭代

6) 收尾关卡（FINISH GATE）与归档（SOT + ARCHIVE）
- 仅在用户验收通过后，才允许：
  1) 按 plan.md 第 4 节更新 ./docs/sot/*（反映当前真实实现）
  2) 更新 plan.md 第 6 节检查单：将所有必选项勾为完成（必须全勾）
  3) 将整个需求目录从 ./docs/wip/YYYYMMDD-topic/ 移动到 ./docs/archive/YYYYMMDD-topic/

## 5）写入与安全闸门
- 写入/移动文件前，先用简短列表总结计划（路径 + 意图），并征求用户确认
- 严禁把代码写入 ./docs/；严禁把文档写入 ./src/
- V1 默认不引入/维护部署、上线、CI/CD、runbook 等文档（除非用户明确要求）
- 禁止执行任何 git 操作（clone/init/commit/push 等均不做）
