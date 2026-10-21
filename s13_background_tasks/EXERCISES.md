# s13 Background Tasks 练习题目

s13 在 **s12 全套（任务系统 + 错误恢复 + prompt 组装 + memory + compact + skill + subagent + todo + hook）** 基础上，核心新增是 **线程后台执行**：

- **判断**：`should_run_background` — 模型显式 `run_in_background` 优先，`is_slow_operation` 关键词启发式兜底
- **执行**：`start_background_task` — 包装成 worker 扔进 daemon 线程，返回 `bg_id`
- **状态**：`background_tasks`（内存字典，`bg_id → {tool_use_id, command, status}`）+ `background_results`
- **同步**：`background_lock = threading.Lock()` 保护两个字典
- **通知**：`collect_background_results` — 完成后以 `<task_notification>` 注入对话

每道题通常只改这些部分：

- `is_slow_operation` / `should_run_background`
- `start_background_task` / worker 函数
- `collect_background_results`
- agent_loop 的分发逻辑与通知注入
- bash 工具 schema / `run_bash` 的 `run_in_background` 参数

---

## 核心机制复习

```
LLM 调 bash（可能带 run_in_background: true）
   ↓
should_run_background(tool_name, tool_input)
   ├─ 显式 run_in_background == True → 后台
   └─ 否则 is_slow_operation 启发式（仅 bash + 关键词）→ 后台
   ↓ 后台
start_background_task(block)
   ├─ _bg_counter += 1 → bg_id = bg_0001
   ├─ background_tasks[bg_id] = {tool_use_id, command, status: "running"}
   ├─ threading.Thread(target=worker, daemon=True).start()
   └─ 返回 bg_id → 本轮先回占位 tool_result
   ↓ 之后某轮
collect_background_results()
   └─ 找出 status == "completed" 的 → pop → <task_notification> 注入
```

**关键洞察**：

| 问题 | 答案 |
|------|------|
| 谁决定进后台？ | 模型显式 `run_in_background` 优先；教学版关键词启发式兜底 |
| 后台任务 ID 长什么样？ | `bg_{counter:04d}`，如 `bg_0001` |
| 后台线程是 daemon 吗？ | 是，`daemon=True`，进程退出时线程跟着退出 |
| 结果何时可见？ | 完成后由 `collect_background_results` 在**后续轮**注入通知 |
| 通知复用原 `tool_use_id` 吗？ | **不复用**。原始 tool call 已回占位 result，通知是独立 text block |
| 后台任务跨 session 吗？ | **不跨**。纯内存字典，进程重启 `_bg_counter` 归零、任务全丢 |
| 慢操作被送去后台时 PostToolUse hook 会执行吗？ | **不会**，分发后 `continue` 跳过 hook 与 todo 计时 |

---

## 一、入门：终端观察（不用写代码）

先运行：

```sh
python s13_background_tasks/code.py
```

| # | Prompt（可分多轮） | 观察重点 |
|---|-------------------|---------|
| 1 | `Run pip list and find all Python files in this directory`（**不带** "background" 措辞） | hook 打印的是 `bash(['pip list'])` 还是 `bash(['pip list', True])`？无参数 → 启发式不含 `list` → **同步执行** |
| 2 | 继续问 `list files` | 后台结果是否以 `<task_notification>` 出现？ |
| 3 | `Run pip install requests and read package.json`（**不带** "background" 措辞） | 启发式命中 `pip install` → 自动进后台 `bash(['pip install requests', True])`，占位返回 + read_file 同步执行 |
| 4 | `Run pip list in the background, then glob for *.py`（**显式**请求） | 模型把 prompt 里的 "background" 翻译成 `run_in_background: true` → `bash(['pip list', True])`，`pip list` 也进后台（显式优先于启发式） |
| 5 | `git status` | 是否同步执行（不进后台）？ |
| 6 | 退出 `q` 再开，问 `run pip install in background, then ls` | 新会话 `bg_id` 是否又从 `bg_0001` 开始？ |

**对照逻辑**：第 1 行（无关键词 + 无显式 → 同步）与第 3 行（关键词命中 → 后台）对比启发式；第 4 行（无关键词 + 显式 → 后台）证明**显式请求优先**。注意 prompt 措辞里出现 "background"/"in background" 会诱导模型显式传参，观察启发式时必须避免这类词。

**练习目标**：分清 **显式请求 vs 启发式兜底**、**同步 vs 异步**、以及后台结果注入的时机。

---

## 二、基础理解题

### 题目 1：四个全局名字 ⭐

填表：

| 名字 | 类型 | 作用 |
|------|------|------|
| `background_tasks` | ? | ? |
| `background_results` | ? | ? |
| `background_lock` | ? | ? |
| `_bg_counter` | ? | ? |

---

### 题目 2：bg 任务状态机 ⭐

`background_tasks[bg_id]["status"]` 只有两种取值。写出：

1. 两个状态分别是什么？
2. `running` 在哪写入？`completed` 在哪写入（哪个线程）？
3. 任务完成后，记录什么时候从字典里**移除**（pop）？

---

### 题目 3：`should_run_background` 判断 ⭐⭐

读源码，判断以下工具调用会不会进后台：

| 调用 | 是否后台 | 原因 |
|------|---------|------|
| `bash("pip install torch")` | ? | ? |
| `bash("npm run build")` | ? | ? |
| `bash("git status")` | ? | ? |
| `bash("pip list")` | ? | ? |
| `bash("sleep 100")` | ? | ? |
| `read_file(path)` | ? | ? |

---

### 题目 4：启发式关键词 ⭐

`is_slow_operation` 只对哪个工具生效？列出 `slow_keywords` 里的关键词。

**追问**：`pip list` 与 `pip install` 命运不同，为什么？

---

### 题目 5：为什么通知不复用 `tool_use_id`？⭐⭐

README 说"通知不复用原始 tool_use_id"。

1. 如果复用同一个 `tool_use_id` 再发一个 `tool_result`，Messages API 那边会怎样？
2. 教学版用什么格式注入？谁作为新的 text block 和 tool_result 一起出现？

---

## 三、机制交叉题

### 题目 6：`daemon=True` 的意义 ⭐⭐

把 `threading.Thread(target=worker, daemon=True)` 改成 `daemon=False` 会怎样？

1. 主进程退出时后台线程会怎样？
2. 对教学版这种交互式 REPL 有什么影响？

---

### 题目 7：`background_lock` 到底保护什么？⭐⭐

worker 线程和主线程会同时碰哪些共享对象？

1. 若不加锁，`collect_background_results` 遍历 + worker 写入会出什么问题？
2. 教学版里 `_bg_counter += 1` 发生在主线程，是线程安全的吗？为什么？

---

### 题目 8：worker 抛异常会怎样？（小改代码）⭐⭐⭐

看 `start_background_task` 的 worker：

```python
def worker():
    result = execute_tool(block)
    with background_lock:
        background_tasks[bg_id]["status"] = "completed"
        background_results[bg_id] = result
```

**问题**：若 `execute_tool` 抛异常（例如后台 bash 命令自身崩溃、handler 不存在），会发生什么？任务永远停在什么状态？

**小改代码**：给 worker 加 `try/except`，把异常作为结果写回 `background_results`，并让状态仍置为 `completed`。

**验收**：造一个 `run_in_background: true` 且必然失败的调用，观察通知里能看到错误信息而不是任务卡死。

---

### 题目 9：后台分发跳过了哪些逻辑？⭐⭐

agent_loop 里后台分发后用 `continue` 跳过：

```python
if handler and should_run_background(block.name, block.input):
    bg_id = start_background_task(block)
    results.append(...)
    continue   # ← 跳过这里之后的所有代码
```

1. 被跳过的是哪两段逻辑（提示：hook 与 todo 计时）？
2. 这带来什么后果？危险命令拦截还在吗（`PreToolUse` 在分发之前还是之后）？
3. 若想让后台任务的 `PostToolUse` 也能执行，该把 `start_background_task` 的调用挪到哪？

---

### 题目 10：与 s12 任务系统结合 ⭐⭐

后台任务完成通知 `<task_notification>` 是普通 text block，会进入 `messages`。

1. 它会被 memory 提取 / compact / Stop hook 处理吗？（作为普通 user 消息的一部分）
2. 通知里的 `<task_id>` 是 `bg_0001` 这种后台 ID，和 s12 的 `task_...` 冲突吗？语义上有什么区别？

---

## 四、与 CC / 设计题

### 题目 11：内存字典 vs `LocalShellTaskState` ⭐⭐

README「深入 CC」说教学版用内存字典，CC 用 `LocalShellTaskState`，输出重定向到文件。

写 3 句：教学版缺了什么能力？这些能力各需要什么（文件 IO？额外线程？）？

---

### 题目 12：Node.js 里没有真正的线程 ⭐

CC 运行在 Node.js/Bun 单线程事件循环。"后台"只是"不 await"。

**问题**：对比教学版的 Python daemon 线程，两种"后台"有什么本质区别？

---

### 题目 13：通知队列优先级 ⭐⭐

CC 的通知队列分 `next` > `later` 两个优先级（`messageQueueManager.ts`）。

1. 后台任务默认是哪个优先级？
2. `next` 大概用于什么场景？教学版的单条注入相当于哪种？

---

### 题目 14：停滞看门狗 ⭐⭐

CC 的后台 bash 任务有看门狗（`LocalShellTask.tsx`）：45 秒输出无增长就检测是否卡在交互式提示（`(y/n)`）。

**问题**：为什么需要这个？教学版后台 `npm install` 若遇到 `(y/n)` 交互会怎样？

---

## 五、场景验收

### 场景 A：后台 + 同步混合

**Prompt**：`Run pip list in the background and find all Python files in this directory`

> 注：这里的 "in the background" 是**有意**的显式请求——本场景验证的是混合流程，不是启发式。若想测启发式，参考观察表第 1/3 行（不带该措辞）。

**验收**：

- [ ] 出现 `[background] dispatched bg_0001` 及占位 tool_result
- [ ] 同一轮内 glob/list_dir 等**同步**工具照常执行并返回
- [ ] 后续某轮出现 `<task_notification>`，内含 `<status>completed</status>`

---

### 场景 B：等待期间干活

**Prompt**：`Run npm install (use run_in_background) and while waiting, read package.json`

**验收**：

- [ ] 第一轮：install 占位返回 + `read_file` 结果同时出现
- [ ] 第二轮（或更晚）：install 的 `<task_notification>` 与当前轮 tool_result 合在一条 user 消息
- [ ] 全程主循环没有因 install 阻塞

---

### 场景 C：与任务系统结合

**Prompt**：`Create a task to setup the project, then run pip list in the background, then list tasks`

**验收**：

- [ ] 既有 `create_task` / `list_tasks`（s12 能力保留）
- [ ] 又有 `bg_xxxx` 后台任务在跑
- [ ] 两种 ID 前缀 `task_` / `bg_` 互不干扰

---

## 六、进阶思考题

### 题目 15：启发式误判的代价 ⭐⭐⭐

`sleep 100` 不含任何关键词 → 启发式漏判（模型又没显式请求）→ 同步阻塞 100 秒。

1. 漏判的代价是什么？
2. 反方向：`make` 可能 1 秒完成，被判后台 → 结果延迟到下一轮。代价又是什么？
3. 你支持保留关键词启发式还是完全依赖显式参数？给 1 条理由。

---

### 题目 16：结果截断 ⭐⭐

`collect_background_results` 的通知只带 `summary = output[:200]`，完整结果在 `background_results[bg_id]` 里被 pop 丢弃。

**问题**：如果后台命令输出 10 万字符（比如 `npm run build` 的日志），agent 只见前 200 字符。你能想到什么改进方案？（至少 2 个，例如写入文件再让 agent 用 read_file。）

---

### 题目 17：后台失败没有传播路径 ⭐⭐⭐

worker 里没有 `try/except`，后台任务失败时状态永远 `running`，通知永远不出现。

**问题**：除了题目 8 的修复，还可以把失败做成显式通知（`<status>failed</status>`）。设计一个失败的 notification 格式，并说明 status 除了 `completed` 还该有哪些值。

---

### 题目 18：并发上限 ⭐⭐

CC 前台工具并发上限 10（`CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY`），后台 bash 无硬性限制。

**问题**：教学版如果一轮里模型发出 3 个 `run_in_background: true` 的 bash，会同时起 3 个线程。有什么风险？要不要加并发上限？

---

## 推荐学习路线（1–2 天）

| 天 | 题目 | 目标 |
|----|------|------|
| Day 1 | 观察表 + 题 1–5 | 四个全局名、判断逻辑、状态机、通知格式 |
| Day 2 | 题 6–9 + 场景 A/B | 线程语义、锁、worker 异常、循环集成 |
| 可选 | 题 10–12 + 场景 C | 与任务系统结合、CC 对比 |

---

## 验收 checklist

- [ ] 能说出 `should_run_background` 的两条路径及优先级
- [ ] 能解释为什么通知不复用 `tool_use_id`
- [ ] 知道 `daemon=True` 的作用
- [ ] 知道 `background_lock` 保护哪两个字典、在哪些线程间
- [ ] 能指出 worker 无 `try/except` 的失败场景
- [ ] 能演示"后台跑慢命令 + 同步继续干活"的完整流程
- [ ] 知道后台任务是内存态，跨 session 不保留

---

## 调试技巧

| 现象 | 排查 |
|------|------|
| 后台任务永远 `running`，通知不出现 | worker 是否抛异常？`execute_tool` 的 handler 是否存在？ |
| 通知注入没看到 | 纯文字轮现在也会在 return 前 collect；若仍没有，看 worker 是否抛异常（status 卡在 running）？compact 分支仍跳过 collect |
| `pip list` 没进后台 | 启发式关键词里没有 `list`，模型必须显式 `run_in_background` |
| 所有 bash 都进后台 | 命令是否命中关键词？模型是否总传 `run_in_background`？ |
| 新会话 bg_id 又从 `bg_0001` 开始 | 内存态字典 + `_bg_counter` 进程内计数，跨 session 必重置 |
| 后台命令输出看不到全貌 | 通知只带 `summary[:200]`，完整结果 pop 后已丢弃 |
| 后台 bash 卡在交互式提示 | 教学版无看门狗，命令可能永远挂着；用 `echo y \| ...` 或 `--yes` |

---

## 和前后章节的关系

| 章节 | 关系 |
|------|------|
| s12 Task System | 执行策略从"全同步"变"慢操作异步"；工具数量不变 |
| s11 Error Recovery | 后台任务的 API 调用仍走恢复路径；但 worker 里 tool 失败不会触发恢复 |
| s05 Todo | todo 计时在后台分发时被跳过，注意 `rounds_since_todo` 不再归零 |
| s06 Subagent | 后台任务里若调用 subagent 相关工具，需自行管理；教学版只把 bash 送后台 |
| s14 Cron Scheduler | 后台解决"慢"，s14 解决"定时" |

s13 练习的核心收获：**慢操作丢后台，占位先回，通知后到；判断靠显式请求 + 启发式兜底，安全靠锁，生命周期靠内存字典，跨 session 不保证。**

---

## 优先推荐（时间有限）

1. **观察表 #1–3** — 10 分钟建立"后台 vs 同步"直觉
2. **题目 3、5、7** — 判断逻辑 + 通知格式 + 锁
3. **场景 A/B** — 异步不阻塞的完整流程
4. **题目 8** — worker 异常修复（小改代码）
5. **题目 15** — 启发式误判的设计权衡

---

## 参考答案提示（自测用）

<details>
<summary>题目 1–5 简答</summary>

**题 1**：`background_tasks` = dict，`bg_id → {tool_use_id, command, status}`；`background_results` = dict，`bg_id → 输出字符串`；`background_lock` = threading.Lock，保护上面两个字典；`_bg_counter` = int，进程内自增生成 bg_id。

**题 2**：`running` / `completed`。`running` 在主线程 `start_background_task` 里写入；`completed` 在 worker（后台线程）执行完 `execute_tool` 后写入。pop 发生在 `collect_background_results`（主线程）。

**题 3**：`pip install torch` → 后台（关键词 `pip install`）；`npm run build` → 后台（关键词 `build`）；`git status` → 否；`pip list` → 否（无关键词）；`sleep 100` → 否（无关键词，需显式）；`read_file` → 否（`is_slow_operation` 只对 bash）。

**题 4**：只对 bash。关键词：install, build, test, deploy, compile, docker build, pip install, npm install, cargo build, pytest, make。`pip install` 含 "pip install" 命中；`pip list` 不含任何关键词。

**题 5**：一个 `tool_use` 只能配对**一个** `tool_result`（Messages API 工具配对语义）；重复/后到的 result 会悬空。教学版用独立的 `<task_notification>` text block 注入，和当前轮的 tool_result 一起组成 user 消息。

</details>

<details>
<summary>题目 6–10 简答</summary>

**题 6**：`daemon=False` 时主线程退出，程序会等后台线程结束才退出；交互式 REPL 退出 `q` 可能卡住等待跑完。

**题 7**：worker 写 `background_tasks[bg_id]["status"]`、`background_results[bg_id]`；主线程遍历 / pop。不加锁会读写竞争（遍历中 dict 变长 / 部分完成的数据）。`_bg_counter += 1` 只发生在主线程（agent_loop 单线程），没有竞争。

**题 8**：`execute_tool` 抛异常 → 锁后的赋值永远不执行 → status 永远 `running`、结果字典为空 → 通知永不注入。修复：worker 包 try/except，except 里写错误信息到 `background_results` 并置 `completed`（或新增 `failed` 状态）。

**题 9**：跳过 `trigger_hooks("PostToolUse", ...)` 与 `if block.name == "todo_write": rounds_since_todo = 0`。`PreToolUse` 在分发前执行，危险命令拦截仍然有效。若想执行 PostToolUse，需把分发改在 hook 之后、按同步流程结构改造（例如后台 worker 完成后回调执行 hook）。

**题 10**：是，通知作为普通 user text 参与 memory 提取 / compact / Stop hook。`bg_xxxx` 与 `task_xxxx` 前缀不同，分别指向后台执行记录与持久化任务，互不冲突。

</details>

<details>
<summary>题目 11–14 简答</summary>

**题 11**：教学版缺：停止任务、读取任务进行中的增量输出、跨重启保留。这些需要文件重定向（stdout/stderr → 文件）、任务注册表/进程句柄管理、输出文件轮询。

**题 12**：Python 用 OS 线程（GIL 内并行受限但可并发 IO）；Node 事件循环单线程，慢命令靠"不 await"把进程扔后台、用文件重定向做异步通知，没有额外线程。

**题 13**：后台任务默认 `later`（不阻塞用户输入）。`next` 用于需要立刻让模型看到的事件。教学版单条注入，无优先级区分。

**题 14**：后台命令卡在 `(y/n)` 等交互提示时，输出停止增长，看门狗检测到 45s 停滞可终止/提醒。教学版无看门狗，命令可能永久挂起直到进程退出。

</details>

<details>
<summary>题目 15–18 简答</summary>

**题 15**：漏判 → 同步阻塞数分钟，agent 空转烧 token；误判 → 快命令结果延迟一轮，且占位/通知多占消息。可保留启发式但收紧关键词，或完全依赖显式参数（CC 做法）。

**题 16**：a) 完整输出写 `.tasks/outputs/bg_xxxx.log`，通知里带文件路径，agent 可 `read_file`；b) 通知带更长摘要 + 关键行（error/warning）；c) 支持后续轮按 bg_id 拉取增量输出。

**题 17**：`<status>failed</status>` + `<error>...</error>`；status 建议 `running / completed / failed / cancelled` 四态。

**题 18**：多线程同时跑 bash 共享 `cwd=WORKDIR`、并发输出到同一终端；无上限可能打爆系统资源。可加一个信号量限制并发数。

</details>
