# Specification Delta: <变更标题>

> **目标 SOT 文件**: `./docs/sot/<对应文件名>.md`
> **基于意图**: `intent.md`

## 1. 变更摘要 (Synopsis)

## 2. 需求变更 (Requirements Delta)

### 🟢 ADDED Requirements (新增需求)

#### Requirement: <需求名称>

The system **SHALL** <系统行为描述>.

##### Scenario: <场景名称> (Gherkin 风格)

* **GIVEN**: <前置条件>
* **WHEN**: <触发动作>
* **THEN**: <预期结果>

---

### 🟡 MODIFIED Requirements (修改需求)

#### Requirement: <原需求 ID 或名称>

> **OLD Behavior**: <旧的逻辑>
> **NEW Behavior**: <新的逻辑>

##### Impact Analysis (影响分析)

* 受影响的代码模块: `src/...`
* 是否需要数据迁移: Yes/No

---

### 🔴 REMOVED Requirements (移除需求)

#### Requirement: <被删除的需求>

* **Reason**: <删除原因>

## 3. 数据结构/API 变更 (Schema/API Changes)typescript

// Example Interface Change
interface User {
// ADDED
lastLoginAt: Date;
}
