# s05 TodoWrite 练习题目

s05 在 **s04 全套 hook + 16 工具** 基础上，核心新增是 **`todo_write` 规划工具** 和 **nag reminder**。

每道题通常只改这些部分：

- `run_todo_write()` — 校验、写盘、返回值
- `SYSTEM` — 引导模型何时规划
- `rounds_since_todo` + `agent_loop` — 提醒机制
- `TOOLS` / `TOOL_HANDLERS` — 可选新增 `todo_read` 等
- s04 hook — 可选扩展（PostToolUse 注入 todo 等）

---

## 核心机制复习

```
用户 prompt
   ↓
（可选）todo_write — 列出 pending / in_progress / completed
   ↓
read / edit / bash ... — 真正执行
   ↓
（每轮 tool_use 后 rounds_since_todo += 1）
   ↓
连续 3 轮没调 todo_write → 注入 <reminder>Update your todos.</reminder>
   ↓
调 todo_write → rounds_since_todo 归零
```

**关键洞察**（README + 你问过的点）：

| 问题 | 答案 |
|------|------|
| `todo_write` 会读文件吗？ | **不会**，只写 `.tasks/current_todos.json` |
| 模型怎么知道当前 todo？ | 靠 **对话 memory** + 每次 **整包传入 todos** |
| 文件给谁看？ |  mainly **人类观察**（教学版） |

---

## 一、入门：用 Prompt 观察（不用写代码）

先运行：

```sh
python s05_todo_write/code.py
```

| # | Prompt | 观察重点 |
|---|--------|---------|
| 1 | `读取 s05_todo_write/example/hello.py` | 单步任务，**可能不调** todo_write |
| 2 | `Refactor s05_todo_write/example/hello.py: add type hints, docstrings, and a main guard` | 第一次工具是不是 todo_write？ |
| 3 | 同上，跑完后打开 `.tasks/current_todos.json` | status 有没有 pending → in_progress → completed |
| 4 | 多步任务中故意等 3 轮以上不更新 todo | 是否出现 `<reminder>Update your todos.</reminder>` |
| 5 | `q` 再开新 prompt | `rounds_since_todo` 是全局变量，**跨 prompt 是否仍累积？** |

**练习目标**：分清「规划工具」和「执行工具」；理解 todo 状态存在 **模型输入** 和 **json 文件** 两条线。

---

## 二、基础实现题

### 题目 1：`run_todo_write` 返回完整列表 ⭐

**现状**：只返回 `"Updated 3 tasks"`，模型容易「忘了」具体项。

**目标**：返回 JSON 字符串，便于模型下一轮对齐：

```python
return json.dumps({"updated": len(todos), "todos": todos}, ensure_ascii=False, indent=2)
```

**测试 prompt**：

> 列 3 步计划重构 hello.py，然后读文件

**验收**：

- [ ] tool_result 里能看到完整 todos
- [ ] `.tasks/current_todos.json` 与返回一致

**你会练到**：工具返回值也是 planning 上下文的一部分。

---

### 题目 2：只允许一个 `in_progress` ⭐⭐

**目标**：若传入多个 `in_progress`，返回 Error：

```python
if sum(1 for t in todos if t["status"] == "in_progress") > 1:
    return "Error: only one task may be in_progress at a time"
```

**测试 prompt**：

> 创建 todo：两步都标 in_progress（故意为难模型，或手改 tool 输入观察）

**你会练到**：规划状态机约束，避免模型并行混乱。

---

### 题目 3：调整 nag 阈值 ⭐

**现状**：`rounds_since_todo >= 3`

**练习 A**：改成 2，观察 reminder 是否更频繁。  
**练习 B**：改成 5，观察模型是否更容易「忘掉 todo」。

**你会练到**：reminder 是 **产品参数**，不是固定真理。

---

### 题目 4：新 prompt 重置计数器 ⭐⭐

**现状**：`rounds_since_todo` 是模块级全局变量，**同一进程内第二个 prompt 会继承上一轮的计数**。

**目标**：在 `__main__` 里每次用户输入新 query 前重置：

```python
global rounds_since_todo
rounds_since_todo = 0
```

**测试**：

1. 第一个 prompt 跑 3 轮工具不调 todo
2. 输入第二个无关 prompt
3. 不应立刻收到 reminder

**你会练到**：规划状态作用域 — **per-session vs per-turn**。

---

### 题目 5：增强 SYSTEM 提示 ⭐

**目标**：在 SYSTEM 里补充规则，例如：

- 多步任务（≥2 步）**必须**先 `todo_write`
- 每完成一步 **必须**更新 todo
- 全部 completed 后再文字总结

**测试 prompt**：

> Create a Python package under s05_todo_write/example/demo_pkg with __init__.py, utils.py, and tests/test_utils.py

**你会练到**：SYSTEM 是规划行为的第一推动力（比 nag 更早）。

---

### 题目 6：给 `todo_write` 加校验 — 空列表 ⭐

**目标**：

```python
if not todos:
    return "Error: todos must not be empty"
```

**你会练到**：规划工具也要有输入校验，和 s02 工具一样。

---

## 三、进阶题（结合 s04 hook）

### 题目 7：PostToolUse 注入当前 todo ⭐⭐⭐

**背景**：模型不读盘，长对话后会丢 todo。

**目标**：新增 hook，在 **每次非 todo_write 工具执行后**，把 `.tasks/current_todos.json` 内容追加到 tool_result（或单独注入 user 消息）：

```python
def todo_inject_hook(block, output):
    if block.name == "todo_write":
        return None
    path = TASKS_DIR / "current_todos.json"
    if not path.exists():
        return None
    todos = path.read_text(encoding="utf-8")
    return output + f"\n\n<current_todos>\n{todos}\n</current_todos>"
```

**注意**：需配合 `large_output_hook` 顺序；截断时可能跳过本 hook（s04 题目 13）。

**你会练到**：**写盘 + 读回注入** 才是完整 planning loop。

---

### 题目 8：新增 `todo_read` 工具 ⭐⭐

**目标**：

```python
def run_todo_read() -> str:
    path = TASKS_DIR / "current_todos.json"
    if not path.exists():
        return "(no todos yet)"
    return path.read_text(encoding="utf-8")
```

注册到 `TOOLS` / `TOOL_HANDLERS`；SYSTEM 里写「不确定进度时用 todo_read」。

**你会练到**：显式读路径，对比「模型靠 memory」的脆弱性。

---

### 题目 9：Stop hook 检查未完成 todo ⭐⭐

**目标**：`summary_hook` 增强 — 若 `current_todos.json` 里仍有 `pending` / `in_progress`，打印警告：

```
[HOOK] Stop: ⚠ 2 todos incomplete
```

**可选进阶**：返回 force 消息，逼模型续跑更新 todo（类似 s04 题目 9）。

**你会练到**：Stop hook 做 **规划完整性** 检查。

---

### 题目 10：verification 步骤 ⭐⭐⭐

**背景**：CC 源码在「全 completed 但无 verification」时会 nudge。

**目标**：要求 todo 列表最后一步必须是：

```json
{"content": "Verify: run tests or re-read changed files", "status": "pending"}
```

在 `run_todo_write` 里校验，或 SYSTEM 里强制；全部 completed 时最后一项也必须是 completed。

**测试 prompt**：README 的 hello.py 重构任务。

**你会练到**：计划不仅要「做」，还要「验」。

---

### 题目 11：`todo_write` 权限 — 只读放行 ⭐

**目标**：确认 `permission_hook` 不会误拦 `todo_write`（无 path 字段）。

**测试**：敏感文件任务中仍应能更新 todo 状态。

**你会练到**：新工具接入要过 s03/s04 权限矩阵。

---

## 四、综合场景题

### 场景 A：三步重构（README 标准题）

**Prompt**：

> Refactor s05_todo_write/example/hello.py: add type hints, docstrings, and a main guard

**验收**：

- [ ] 首次或早期调用 `todo_write`
- [ ] 至少 3 个 todo 项
- [ ] 终端出现 `## Current Tasks`
- [ ] `.tasks/current_todos.json` 最终全 `completed`
- [ ] hello.py 确实被改好

---

### 场景 B：做着做着偏了

**Prompt**：

> 把 s05_todo_write/example 下所有 .py 改成 snake_case，然后跑测试

**模拟偏航**：若模型开始只修测试、忘了 rename，观察 nag 能否拉回。

**验收**：

- [ ] 出现 reminder 或模型主动 re-todo
- [ ] todo 内容仍包含 rename 相关步骤

---

### 场景 C：规划 vs 执行分离

**Prompt**：

> 先只列计划不要执行：为 demo_pkg 创建 package 结构

然后：

> 现在执行第 1 步

**验收**：

- [ ] 第一轮只有 todo_write / 极少量工具
- [ ] 第二轮按 todo 逐步执行

**你会练到**：`todo_write` **不增加执行能力**，只增加节奏控制。

---

## 五、设计思考题（不写代码也可）

### 题目 12：为什么 todo 写盘但模型不读？

写 3–5 句，对比：

- 教学版写 `.tasks/current_todos.json` 的目的
- 真实 CC V1（内存 AppState）vs V2（文件 + 依赖图）

---

### 题目 13：`rounds_since_todo += 1` 的时机对吗？

**现状**：每个 **tool_use 轮次** +1，不论该轮调了几个工具。

**思考**：

- 一轮里调 3 个工具（read + edit + bash）算 1 还是 3？
- 若模型一轮只 `todo_write` + `read_file`，算不算「更新了 todo」？

对照你的 `code.py` 1177–1230 行，说清当前语义。

---

### 题目 14：todo_write 会被 hook 记进 session.jsonl 吗？

**答案应包含**：PostToolUse 会记；PreToolUse 有 `[HOOK] todo_write(...)`；large_output 截断时后续 hook 行为。

---

## 推荐学习路线（3 天）

| 天 | 题目 | 目标 |
|----|------|------|
| Day 1 | 观察 prompt + 题 1、5、6 | 理解规划工具语义 |
| Day 2 | 题 2、3、4 + 场景 A | 状态机 + nag + 作用域 |
| Day 3 | 题 7 或 8 + 题 9、10 | 读回注入 + 完整性 |

---

## 验收 checklist

- [ ] 多步任务会先或早期出现 `todo_write`
- [ ] `.tasks/current_todos.json` 与终端 `## Current Tasks` 一致
- [ ] status 会随进度变化（不是一直 pending）
- [ ] 3 轮不更新会 nag（题 3 可调阈值）
- [ ] 新 prompt 不会误继承 reminder（题 4）
- [ ] s04 hook 仍正常（permission、session.jsonl）
- [ ] 能解释「写盘 ≠ 模型自动读盘」

---

## 调试技巧

| 现象 | 排查 |
|------|------|
| 从不调 todo_write | 加强 SYSTEM；任务是否太简单（单步 read） |
| todo 全 pending 就结束 | 模型没更新 status；看 tool_result 是否信息太少（题 1） |
| reminder 太烦 | 阈值 3→5；或题 4 重置 scope |
| reminder 从不出现 | 模型每轮都调 todo_write；或 rounds 被误 reset |
| json 与终端不一致 | 同一次 `run_todo_write` 应同时写两处，查是否报错中断 |

---

## 和后续章节的关系

| 章节 | 关系 |
|------|------|
| s04 Hooks | PostToolUse 注入 todo（题 7）；Stop 检查（题 9） |
| s06 Subagent | 大任务拆子 Agent，todo 不够用时升级 |
| s12 Task System | 文件持久化 + blockedBy + 多工具，TodoWrite V2 |

s05 练习的核心收获：**规划是独立能力，靠工具 + 提示 + reminder 塑造行为，不靠「模型自觉」。**

---

## 优先推荐（时间有限）

1. **题目 1** — 返回完整 todos（立刻改善模型记忆）
2. **题目 4** — 修复全局 `rounds_since_todo`（常见坑）
3. **题目 7 或 8** — 解决「写盘不读盘」
4. **场景 A** — README 标准验收
5. **题目 13** — 搞清 nag 计数语义（面试爱问）
