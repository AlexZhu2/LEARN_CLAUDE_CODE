# s06 Subagent 练习题目

s06 在 **s05 全套（18 工具 + todo + hook + plan-only）** 基础上，核心新增是 **`task` 工具** 和 **`spawn_subagent()` 上下文隔离**。

每道题通常只改这些部分：

- `spawn_subagent()` — 子循环、30 轮上限、返回值
- `SUB_TOOLS` / `SUB_HANDLERS` / `SUB_SYSTEM` — 子 Agent 能力边界
- `in_subagent` — 子 Agent 期间哪些 hook 参与
- `SYSTEM` — 引导父 Agent 何时 delegate
- s05 机制 — todo / nag / Stop 仍只在**父** `agent_loop` 里

---

## 核心机制复习

```
父 Agent messages=[用户历史...]
   ↓
（可选）todo_write 规划
   ↓
父直接 read/edit/bash ...  OR  task(description) → spawn_subagent
   ↓                              ↓
主 history 继续累积          子 messages=[description]  ← 全新
                              子只用 SUB_TOOLS（5 个）
                              中间 tool 过程丢弃
                              只 return 摘要文本
   ↓
父收到 task 的 tool_result（一段 summary）
```

**关键洞察**：

| 问题 | 答案 |
|------|------|
| 子 Agent 能看到父对话吗？ | **不能**，只有 `description` 字符串 |
| 子 Agent 写的文件父 Agent 能看到吗？ | **能**，共享 WORKDIR 文件系统 |
| 子 Agent 能再 spawn 子 Agent 吗？ | **不能**，`SUB_TOOLS` 无 `task` |
| 子 Agent 走 permission hook 吗？ | **走**，安全不隔离 |
| 子 Agent 会收到 `<current_todos>` 注入吗？ | **不会**（`in_subagent` 跳过 `todo_inject_hook`） |
| `task` 在 plan-only 模式能用吗？ | **不能**，`plan_only_hook` 只允许 todo 工具 |

---

## 一、入门：用 Prompt 观察（不用写代码）

先运行：

```sh
python s06_subagent/code.py
```

| # | Prompt | 观察重点 |
|---|--------|---------|
| 1 | `读取 s06_subagent/code.py 前 30 行` | 单步任务，可能**不调** `task` |
| 2 | `Use a subtask to find what testing framework this project uses` | 是否出现 `[Subagent spawned]` / `[Subagent done]`？ |
| 3 | 同上，观察终端 | 子工具是否以 `[sub] read_file: ...` 输出？ |
| 4 | `Delegate: summarize what s06_subagent/code.py does in 3 bullet points` | 主 history 里是否**没有**子 Agent 的中间 read 细节？ |
| 5 | README 第 3 条 prompt（创建 string_tools.py + 父 Agent 验证） | 文件是否在磁盘？父 Agent 是否用 read/bash 验证？ |

**练习目标**：分清「父对话上下文」和「子 Agent 临时上下文」；理解 **task 返回的是摘要，不是 messages 列表**。

---

## 二、基础实现题

### 题目 1：确认 SUB_TOOLS 白名单 ⭐

**目标**：读懂这三行，并解释为何没有 `grep` / `task` / `todo_write`：

```python
SUB_TOOL_NAMES = ("bash", "read_file", "write_file", "edit_file", "glob")
SUB_TOOLS = [t for t in TOOLS if t["name"] in SUB_TOOL_NAMES]
SUB_HANDLERS = {name: TOOL_HANDLERS[name] for name in SUB_TOOL_NAMES}
```

**测试**：在 `spawn_subagent` 里临时打印 `len(SUB_TOOLS)`，应为 **5**。

**你会练到**：子 Agent 能力是**显式裁剪**的，不是自动继承父工具。

---

### 题目 2：30 轮安全上限 ⭐

**现状**：`for _ in range(30)` 硬编码。

**练习 A**：改成模块常量 `SUBAGENT_MAX_TURNS = 30`。  
**练习 B**：故意改成 `3`，用需要多轮 read 的 prompt 触发 fallback 消息：

> `Subagent stopped after 30 turns without final answer.`

**你会练到**：子 Agent 必须有**熔断**，防止父 Agent 永远阻塞。

---

### 题目 3：`extract_text` 回退逻辑 ⭐⭐

**现状**：最后一轮若是 tool_result，可能没有 assistant 文本。

**目标**：阅读 `spawn_subagent` 末尾的 `extract_text` + 反向扫描 assistant 消息逻辑。

**思考题**：若子 Agent 第 30 轮仍以 `tool_use` 结束，用户会看到什么？

**你会练到**：**只回传结论** 需要可靠的文本提取，不能假设 `messages[-1]` 一定是 text。

---

### 题目 4：增强 SYSTEM — 何时 delegate ⭐

**目标**：在父 `SYSTEM` 里补充规则，例如：

- 探索/调研类（读很多文件找信息）→ 优先 `task`
- 单文件小改 → 父 Agent 直接做
- delegate 时在 `description` 里写清**路径、验收标准、不要做什么**

**测试 prompt**：

> Use a subtask to list every hook name registered in s06_subagent/code.py and what event each uses.

**你会练到**：**task 是产品决策**，靠 SYSTEM 塑造，不是模型自觉。

---

### 题目 5：子 Agent 也走 PostToolUse 的 `modified` ⭐

**现状**：`spawn_subagent` 已处理：

```python
modified = trigger_hooks("PostToolUse", block, output)
if modified is not None:
    output = modified
```

**练习**：故意让 `large_output_hook` 截断一个超大 read，观察子 Agent 的 `[sub]` 输出是否变短。

**你会练到**：子循环与父循环在 **hook 短路语义** 上一致（先返回 non-None 的 hook  wins）。

---

## 三、进阶题（hook + 隔离）

### 题目 6：`in_subagent` 隔离 todo 注入 ⭐⭐⭐

**背景**：若不跳过，子 Agent 的 tool_result 会带上父的 `<current_todos>`，破坏「干净上下文」。

**目标**：确认以下 hook 在 `in_subagent=True` 时行为符合注释：

| Hook | 子 Agent 期望 |
|------|---------------|
| `todo_inject_hook` | 跳过 |
| `plan_only_hook` | 跳过 |
| `tool_log_hook` | 跳过（不进 session.jsonl） |
| `permission_hook` | **保留** |
| `large_output_hook` | **保留** |

**验收 prompt**（父 Agent 先 todo_write 再 task）：

> 先 plan：调研 s05 的 hook 列表；然后用 subtask 读取 s06_subagent/code.py 的 register_hook 部分

检查：子 Agent 的 tool_result / `[sub]` 输出里**不应**出现 `<current_todos>`。

**你会练到**：**上下文隔离 = messages 隔离 + hook 作用域**，不是只换 system prompt。

---

### 题目 7：plan-only 与 task 冲突 ⭐⭐

**测试 prompt**：

> 先只列计划不要执行：用 subtask 调研项目结构

**期望**：

- [ ] `plan-only mode enabled`
- [ ] **不应**出现 `[Subagent spawned]`（`task` 被 `plan_only_hook` 拦截）
- [ ] 只有 `todo_write` / `todo_read`

**你会练到**：plan-only 是**父会话策略**；子 Agent 只在执行阶段 spawn。

---

### 题目 8：permission 在子 Agent 里仍生效 ⭐⭐

**测试**：让子 Agent 尝试写 `.env` 或工作区外路径（需模型配合或手改 tool 输入观察）。

**期望**：

- [ ] 子 Agent 收到 `Blocked: sensitive file` 或 permission denied
- [ ] 父 Agent 终端仍显示 `[HOOK]` / `⛔`（权限冒泡到同一终端）

**你会练到**：README 说的 **「安全策略不因上下文隔离而跳过」**。

---

### 题目 9：禁止递归 spawn ⭐⭐

**目标**：确认 `SUB_TOOLS` 无 `task`。若模型在子 Agent 内「幻想」调用 task，handler 不存在 → `Unknown: task`。

**可选增强**：在 `spawn_subagent` 内对 Unknown 工具返回更明确的：

```text
Error: subagents cannot spawn subagents (no task tool)
```

**你会练到**：教学版递归防护 = **工具面裁剪**，不是 runtime 检测 history。

---

### 题目 10：父 Stop hook vs 子 Agent ⭐⭐

**思考题**：

- 子 Agent 跑完 10 轮 read，父 Agent 的 pending todo 仍存在，Stop hook 何时触发？
- 子 Agent 的 10 轮会触发父的 `rounds_since_todo` nag 吗？

**答案应包含**：nag / Stop **只在** `agent_loop`；`spawn_subagent` 是同步函数，在父的一轮 `task` tool 执行期间子循环跑完，**不**单独 increment 父的 `rounds_since_todo`（除非父那一轮还调了别的工具）。

**你会练到**：父子是**嵌套调用关系**，不是两个并行 agent_loop。

---

### 题目 11：session.jsonl 记谁？ ⭐

**现状**：`tool_log_hook` 在 `in_subagent` 时跳过。

**练习 A**（观察）：跑一次带 task 的 prompt，看 `.debug/session.jsonl` 是否只有父工具、没有 `[sub]` 那些 read。  
**练习 B**（可选实现）：子 Agent 单独写 `.debug/subagent.jsonl`，字段加 `"scope": "subagent"`。

**你会练到**：**观测面**和**模型上下文**可以分开设计。

---

### 题目 12：给 SUB_SYSTEM 加验收指令 ⭐⭐

**目标**：强化 `SUB_SYSTEM`，例如：

```text
Return a concise summary with: findings, files changed, and anything unfinished.
Do not mention that you are a subagent.
```

**测试 prompt**（README #3）：

> Use a task to create s06_subagent/example/string_tools.py with slugify(text: str), then verify from parent

**验收**：

- [ ] 子 Agent 创建文件
- [ ] 返回摘要含函数行为说明
- [ ] 父 Agent 自己 read/run 验证（Verify todo）

---

## 四、综合场景题

### 场景 A：调研 vs 执行分离（标准 README 题）

**Prompt**：

> Use a subtask to find all files under s05_todo_write/ that mention "hook", summarize in a table. Then update your todos and fix any typo you find in s05 README if needed.

**验收**：

- [ ] 出现 `[Subagent spawned]` / `[Subagent done]`
- [ ] 主对话不膨胀为大量 grep 行（只有 task 摘要）
- [ ] 若父 Agent 改文件，改动在 WORKDIR 可见
- [ ] todo 有 Verify 步骤（s05 校验仍生效）

---

### 场景 B：该 delegate 还是不该

**Prompt**：

> edit s06_subagent/example/hello.py to add a docstring

**对比 Prompt**：

> Use a subtask to read every .py under s04_hooks/ and s05_todo_write/ and compare hook registration order

**验收**：

- [ ] 小改：**可能不** spawn 子 Agent
- [ ] 大调研：**应** spawn 子 Agent
- [ ] 能口头解释判断依据（文件数、上下文污染、父 todo 复杂度）

---

### 场景 C：子 Agent 写盘 + 父 Agent 验收

**Prompt**：

> Use a task to create s06_subagent/example/string_tools.py with slugify(text: str). After it returns, you must read the file and run a quick test yourself before marking Verify complete.

**验收**：

- [ ] 文件存在且可 import
- [ ] 父 Agent 不盲信子摘要，有 read/bash 验证
- [ ] `.tasks/current_todos.json` 最终全 completed

---

### 场景 D：熔断与错误传递

**练习**：临时设 `SUBAGENT_MAX_TURNS = 2`，prompt：

> Use a subtask to read s06_subagent/code.py entirely and list every function name

**验收**：

- [ ] 子 Agent 提前停止
- [ ] 父 Agent 收到 fallback 或 partial summary
- [ ] 父 Agent 应决定：重试 task / 自己读 / 报告失败

---

## 五、设计思考题（不写代码也可）

### 题目 13：隔离了什么，没隔离什么？

写 5–8 句，对比教学版 s06 与 README「深入 CC 源码」：

- messages[] 隔离
- 文件系统 / WORKDIR 共享
- `readFileState` 克隆（真实 CC）
- todo / session.jsonl 通过 hook 共享或隔离

---

### 题目 14：`spawn_subagent` 与 `agent_loop` 有多像？

**答案应包含**：

- 相同：PreToolUse → handler → PostToolUse(modified)
- 不同：SYSTEM、TOOLS、循环上限、Stop/nag/plan-only、返回值 vs 改 history

---

### 题目 15：为什么 task 是 tool 而不是函数调用？

对比：

```python
# A: 模型决定调用
task(description="...")

# B: 代码里直接 spawn_subagent("...")
```

从 **模型可见性、tool_result 格式、与 todo 规划关系** 回答。

---

### 题目 16：若允许子 Agent 也有 `todo_write` 会怎样？

讨论：规划在父还是子？`.tasks/current_todos.json` 谁写？Stop hook 检查谁的 todo？

---

## 推荐学习路线（2–3 天）

| 天 | 题目 | 目标 |
|----|------|------|
| Day 1 | 观察表 + 题 1、2、4 | 理解 task / SUB_TOOLS / 摘要回传 |
| Day 2 | 题 6、7、8、10 + 场景 A | hook 作用域 + 安全 + 父子嵌套 |
| Day 3 | 题 11、12 + 场景 C、D | 观测、验收、熔断 |

---

## 验收 checklist

- [ ] 能解释「子 messages 新开、父 history 不存中间 read」
- [ ] 知道 SUB_TOOLS 只有 5 个，且无 task
- [ ] 子 Agent 工具仍被 permission 拦截
- [ ] `in_subagent` 时无 `<current_todos>` 注入
- [ ] plan-only 下不能 spawn subagent
- [ ] task 结果只是字符串摘要，父 Agent 需自行验证（配合 s05 Verify）
- [ ] s05 机制仍正常：todo_write、nag、Stop、session.jsonl（父工具）

---

## 调试技巧

| 现象 | 排查 |
|------|------|
| 从不 spawn subagent | 加强 SYSTEM；任务是否太小；模型偏好直接 read |
| 子 Agent 空摘要 | 看 `extract_text` 回退；是否 30 轮耗尽 |
| 子 Agent 仍看到 todo | `in_subagent` 是否为 True；`todo_inject_hook` 是否漏改 |
| plan-only 仍 spawn | `plan_only_hook` 是否拦 `task` |
| 父 history 仍很长 | 模型可能没用 task，在主 loop 里 read 太多 |
| 子写文件父找不到 | 路径是否相对 WORKDIR；是否写在子 description 指定路径 |

---

## 和后续章节的关系

| 章节 | 关系 |
|------|------|
| s05 TodoWrite | 父 Agent 规划；子 Agent 不持 todo |
| s07 Skill Loading | 子 Agent 可按需加载 skill 文档（扩展题） |
| s13 Team | 异步 subagent / 多 Agent 协作 |
| s12 Task System | Todo V2 + 依赖图，与 task 工具互补 |

s06 练习的核心收获：**大任务拆出去，用独立上下文换注意力；共享磁盘，不共享对话；安全与验收仍在 Harness 层约束。**

---

## 优先推荐（时间有限）

1. **观察表 #2–4** — 5 分钟建立直觉
2. **题目 6** — `in_subagent` / todo 注入（你刚修过的点）
3. **题目 10** — 父子 nag/Stop 边界（面试爱问）
4. **场景 C** — README 标准验收
5. **题目 14** — 对比两个 loop 异同
