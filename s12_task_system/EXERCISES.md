# s12 Task System 练习题目

s12 在 **s11 全套（错误恢复 + prompt 组装 + memory + compact + skill + subagent + todo + hook）** 基础上，核心新增是 **持久化任务图**：

- **Task**：`id / subject / description / status / owner / blockedBy`
- **存储**：`.tasks/task_*.json`（与 s05 `current_todos.json` 共存）
- **依赖**：`blockedBy` + `can_start`（缺失依赖视为 blocked）
- **生命周期**：`pending → claim → in_progress → complete → completed`
- **工具**：`create_task` / `list_tasks` / `get_task` / `claim_task` / `complete_task`

每道题通常只改这些部分：

- `Task` / `create_task` / `save_task` / `load_task` / `list_tasks`
- `can_start` / `claim_task` / `complete_task`
- 5 个工具 schema + handlers
- identity / plan-only 里与 task 相关的提示

---

## 核心机制复习

```
create_task(subject, blockedBy=[...])
   ↓ 写 .tasks/task_*.json  status=pending
claim_task(id)  ← can_start? 全部依赖 completed
   ↓  status=in_progress, owner=agent
complete_task(id)
   ↓  status=completed
   └─ 扫描下游 pending：若 can_start → 报告 Unblocked
```

**关键洞察**：

| 问题 | 答案 |
|------|------|
| Task vs TodoWrite？ | Task **落盘 + blockedBy**；Todo 是会话内清单（`current_todos.json`） |
| 谁能 claim？ | 仅 `pending` 且 `can_start == True` |
| 缺失的依赖 ID？ | **当作 blocked**（`can_start` 返回 False） |
| complete 后下游自动变 in_progress？ | **否**，只报告 Unblocked；仍需 `claim_task` |
| 教学版有环检测吗？ | **无**，只做 `blockedBy` 检查 |
| 退出再开还在吗？ | `.tasks/task_*.json` **跨 session**；每条 prompt 只删 `current_todos.json` |

---

## 一、入门：终端观察（不用写代码）

先运行：

```sh
python s12_task_system/code.py
```

| # | Prompt（可分多轮） | 观察重点 |
|---|-------------------|---------|
| 1 | README 四步：建 schema / endpoints / tests / docs 依赖 | `.tasks/` 是否出现多个 `task_*.json`？ |
| 2 | `List all tasks` | ○ / ● / ✓ 与 status 是否一致？ |
| 3 | Claim 并 Complete 第一个无依赖任务 | 终端 `[claim]` / `[complete]` / `[unblocked]`？ |
| 4 | 再 `List tasks` | 谁从 blocked 变为可 claim？ |
| 5 | 退出 `q`，再开 s12，只问 `List tasks` | **跨 session** 任务是否还在？ |
| 6 | `只列计划：搭一个 blog API，不要执行` | 能否 `create_task`？`claim_task` 是否被 plan-only 拦住？ |

**练习目标**：分清 **创建 → 认领 → 完成 → 解锁**，以及与 TodoWrite 的持久性差异。

---

## 二、基础理解题

### 题目 1：Task 六个字段 ⭐

填表：

| 字段 | 类型 / 取值 | 用途 |
|------|------------|------|
| id | ? | ? |
| subject | ? | ? |
| description | ? | ? |
| status | ? | ? |
| owner | ? | ? |
| blockedBy | ? | ? |

---

### 题目 2：TodoWrite vs Task System ⭐⭐

填表：

| | TodoWrite (s05) | Task System (s12) |
|--|-----------------|-------------------|
| 文件 | `current_todos.json` | ? |
| 依赖 | 无 | ? |
| 跨 session | 每条 prompt 会 unlink | ? |
| 多 Agent | 无 | ? |
| 状态 | pending / in_progress / completed（清单项） | ? |

---

### 题目 3：`can_start` 三条规则 ⭐⭐

读源码，回答以下是否返回 True：

1. `blockedBy == []`
2. 依赖文件存在且 status=`in_progress`
3. `blockedBy` 含不存在的 id `task_missing`

---

### 题目 4：状态机 ⭐

画出 / 写出合法转移：

```
pending --?--> in_progress --?--> completed
```

**追问**：教学版能否从 `in_progress` 回到 `pending`？CC 在 teammate 退出时大致怎么做？

---

### 题目 5：`complete_task` 的 Unblocked 列表从哪来？⭐⭐

**问题**：

1. 扫描的是哪些任务？过滤条件是什么？
2. Unblocked 之后下游任务的 status 变成什么？还要再调什么工具？

---

## 三、机制交叉题

### 题目 6：DAG 推演 ⭐⭐⭐

任务关系：

```
schema
  ├─→ endpoints → tests
  └─→ docs
```

初始全部 `pending`。按顺序执行：

1. `claim(schema)` → `complete(schema)`  
2. 此时哪些可 claim？哪些仍 blocked？  
3. `complete(endpoints)` 后谁被 Unblocked？

---

### 题目 7：错误 claim / complete ⭐⭐

对每种情况写返回大概含义：

1. claim 一个已是 `in_progress` 的任务  
2. claim 依赖未完成的任务  
3. complete 一个仍是 `pending` 的任务  
4. claim 不存在的 id  

---

### 题目 8：plan-only 与 task 工具 ⭐⭐

读 `plan_only_hook` 与 `PROMPT_SECTIONS["plan_only"]`。

**问题**：下列哪些在 plan-only 下允许？哪些拒绝？

`create_task` / `list_tasks` / `get_task` / `claim_task` / `complete_task` / `bash` / `todo_write`

---

### 题目 9：与 s11 错误恢复的独立性 ⭐

README 说 tasks 与 with_retry 是独立层。

**问题**：在 `agent_loop` 里，`claim_task` 失败（被 blocked）会触发 Path 1/2/3 吗？为什么？

---

### 题目 10：禁止自依赖（小改代码）⭐⭐⭐

**目标**：`create_task` / `claim_task` 前检查：若 `blockedBy` 含自身 id（创建时还没有 id，可检查 claim），或创建后 `blockedBy` 引用自己——实现一个简化版：

在 `claim_task` 中：若 `task_id in task.blockedBy`，拒绝 claim 并返回明确错误。

**验收**：手写 JSON 造一个自依赖任务，claim 被拒绝。

---

## 四、与 CC / 设计题

### 题目 11：教学版 vs CC Task 字段 ⭐⭐

README「深入 CC」提到 `blocks`、`activeForm`、`metadata`。

写 3 句：教学版省略了什么？`blockedBy` alone 够表达双向依赖吗？

---

### 题目 12：为何 missing dep = blocked？⭐⭐

**问题**：若缺失依赖当作「已满足」会有什么危险？当作 blocked 又有什么代价？

---

### 题目 13：ID 生成方式 ⭐

教学版：`task_{timestamp}_{rand4}`。CC 用递增 + highwatermark。

各写 1 句优缺点。

---

### 题目 14：何时用 Todo，何时用 Task？⭐⭐

给三个场景各选一个：

1. 「先读 README，再改一行 typo」  
2. 「后端：建表 → API → 测试，明天继续」  
3. 「plan only，先拆 5 步，今天不执行」  

---

## 五、场景验收

### 场景 A：README 标准四步

按 README「试一下」1–4 执行。

**验收**：

- [ ] `.tasks/` 有 ≥4 个 `task_*.json`
- [ ] complete schema 后 endpoints/docs 出现在 Unblocked 或变为可 claim
- [ ] tests 在 endpoints 完成前 claim 失败

---

### 场景 B：跨 session

1. Session 1：创建依赖任务，只 complete schema  
2. 退出 `q`  
3. Session 2：`List tasks` → claim endpoints  

**验收**：

- [ ] schema 仍 completed  
- [ ] endpoints 可 claim  
- [ ] `current_todos.json` 被 unlink 不影响 `task_*.json`

---

### 场景 C：plan-only + task

**Prompt**：`只列计划：实现登录功能，分出依赖任务，不要执行`

**验收**：

- [ ] 出现 `create_task` / `list_tasks`  
- [ ] 不出现 `claim_task` / `bash`（或被 hook 拦住）  

---

## 六、进阶思考题

### 题目 15：该不该做环检测？⭐⭐⭐

列出 **支持加环检测** / **反对（教学版省略）** 各 2 条。

---

### 题目 16：complete 时自动 claim 下游？⭐⭐

当前只报告 Unblocked。若改为自动 `claim` 第一个 unblocked：

支持 2 条 / 反对 2 条。

---

### 题目 17：Stop hook 该不该看 `task_*.json`？⭐⭐

现在 `summary_hook` 主要看 `current_todos.json`。

**问题**：若还有 `in_progress` 的持久化 task，Stop force 是否该参考？写 2 句利弊。

---

## 推荐学习路线（1–2 天）

| 天 | 题目 | 目标 |
|----|------|------|
| Day 1 | 观察表 + 题 1–5 | 字段、存储、can_start、状态机 |
| Day 2 | 题 6–9 + 场景 A/B | DAG 推演、plan-only、与 s11 边界 |
| 可选 | 题 10–12 + 场景 C | 改代码 + CC 对比 |

---

## 验收 checklist

- [ ] 能默写 `pending → in_progress → completed` 及两个动作
- [ ] 能解释 `can_start` 对缺失依赖的处理
- [ ] 知道 complete **不**自动改下游 status
- [ ] 能对比 TodoWrite 与 Task 的持久性
- [ ] 知道 plan-only 允许哪些 task 工具
- [ ] 能手推 schema/endpoints/tests/docs 解锁顺序
- [ ] 知道任务系统与错误恢复是独立层

---

## 调试技巧

| 现象 | 排查 |
|------|------|
| claim 总是 Blocked | `blockedBy` id 是否打错？依赖是否 completed？文件是否存在？ |
| complete 后无 Unblocked | 下游 `blockedBy` 是否指向刚完成的 id？下游是否 already in_progress？ |
| 跨 session 任务丢了 | 是否误删整个 `.tasks/`？是否只剩 `current_todos.json`？ |
| plan-only 仍 claim | `is_plan_only_query` 是否命中？hook 是否注册？ |
| JSON 乱码 | `ensure_ascii=False` + utf-8 读写是否一致？ |
| list_tasks 空 | glob 是否 `task_*.json`？id 前缀是否 `task_`？ |

---

## 和前后章节的关系

| 章节 | 关系 |
|------|------|
| s05 Todo | 会话内清单；s12 是落盘 + 依赖的并行系统 |
| s11 Error Recovery | 独立层：tool 返回字符串错误 ≠ API 恢复路径 |
| s06 Subagent | `owner` 为多 agent 预留；教学版默认 `agent` |
| s13 Background Tasks | 有了任务图后，慢任务可进后台 |

s12 练习的核心收获：**大目标拆成有依赖的小任务，落盘才能跨会话恢复；claim 守门，complete 解锁，下游不会自动开工。**

---

## 优先推荐（时间有限）

1. **观察表 #1–4** — 10 分钟建立 DAG 直觉  
2. **题目 3、5、6** — can_start + Unblocked + 推演  
3. **场景 A/B** — 落盘与跨 session  
4. **题目 8** — plan-only  
5. **题目 15** — 环检测设计权衡  

---

## 参考答案提示（自测用）

<details>
<summary>题目 1–5 简答</summary>

**题 1**：id=`task_时间戳_随机`；subject 标题；description 详情；status=`pending|in_progress|completed`；owner 认领者；blockedBy 上游 id 列表。

**题 2**：`task_*.json`；`blockedBy`；跨 session 保留；`owner` 字段；三态 pending→in_progress→completed。

**题 3**：True；False；False。

**题 4**：claim；complete。教学版无 release；CC 可 unassign 并重置 pending。

**题 5**：所有 pending 且有 blockedBy 且 `can_start` 的任务的 subject；status **仍是 pending**，需再 `claim_task`。

</details>

<details>
<summary>题目 6–8 简答</summary>

**题 6**：complete(schema) 后 endpoints、docs 可 claim，tests 仍 blocked；complete(endpoints) 后 Unblocked 含 tests。

**题 7**：cannot claim（非 pending）；Blocked by: [...]；cannot complete（非 in_progress）；Task not found。

**题 8**：允许 create/list/get/todo_*；拒绝 claim/complete/bash。

</details>
