# s14 Cron Scheduler 练习题目

s14 在 **s13 全套（后台任务 + 任务系统 + 错误恢复 + prompt + memory + compact + skill + subagent + todo + hook）** 基础上，核心新增是 **按时间表生产工作**：

- **CronJob**：`id / cron / prompt / recurring / durable`
- **四层模型**：Scheduler → Queue → Queue Processor → Consumer
- **匹配**：`cron_matches` 五段式 + DOM/DOW OR 语义
- **持久化**：`.scheduled_tasks.json`（durable）vs 内存（session-only）
- **工具**：`schedule_cron` / `list_crons` / `cancel_cron`
- **同步**：`cron_lock`（调度与队列）+ `agent_lock`（用户输入与定时交付互斥）

每道题通常只改这些部分：

- `CronJob` / `cron_matches` / `validate_cron`
- `schedule_job` / `cancel_job` / `save_durable_jobs` / `load_durable_jobs`
- `cron_scheduler_loop` / `consume_cron_queue` / `queue_processor_loop`
- agent_loop 开头的 cron 注入
- `run_agent_turn_locked` + `__main__` 里的 `agent_lock`

---

## 核心机制复习

```
schedule_cron(cron, prompt, recurring, durable)
   ↓ 写入 scheduled_jobs；durable → .scheduled_tasks.json
cron_scheduler_loop (daemon, 每 1s)
   ↓ cron_matches(now) 且本分钟未 fire → cron_queue.append(job)
queue_processor_loop (daemon, 每 0.2s)
   ↓ 队列非空 且 agent_lock 可拿 → run_agent_turn_locked()
agent_loop 每轮开头
   └─ consume_cron_queue() → 注入 "[Scheduled] {prompt}"
```

**关键洞察**：

| 问题 | 答案 |
|------|------|
| 谁触发定时工作？ | 调度线程，不需要用户按回车 |
| 四层各干什么？ | 判时间 / 传任务 / 趁空闲叫醒 / 注入 messages |
| durable 跨重启保留什么？ | **任务定义**；进程关掉调度就停，不是系统 crontab |
| 同一分钟会 fire 两次吗？ | **不会**，`_last_fired[job_id] = "YYYY-MM-DD HH:MM"` 防重 |
| Agent 忙时 fire 去哪？ | 进 `cron_queue` 等着；processor 拿不到锁就跳过；agent_loop 自己也会 consume |
| 用户输入和定时交付如何互斥？ | `agent_lock` |
| cron / bg / task 三种 ID？ | `cron_*` 调度任务；`bg_*` 后台执行；`task_*` 持久化 DAG |

---

## 一、入门：终端观察（不用写代码）

先运行：

```sh
python s14_cron_scheduler/code.py
```

> **Windows 注意**：prompt 里不要写 shell 的 `date`（会交互卡住 120s）。用  
> `python -c "from datetime import datetime; print(datetime.now())"`  
> 或 `powershell -Command Get-Date`。

| # | Prompt（可分多轮） | 观察重点 |
|---|-------------------|---------|
| 1 | `Schedule every 2 minutes: print current time with python -c "from datetime import datetime; print(datetime.now())"` | `[cron register]`？`.scheduled_tasks.json` 是否出现？ |
| 2 | `List all cron jobs` | 是否显示 recurring / durable？ |
| 3 | **不要打字**，等最多 2 分钟 | 是否出现 `[cron fire]` → `[queue processor]` → `[inject cron]`？ |
| 4 | 看到输出后立刻：`Cancel the recurring cron and list_crons` | 取消后是否还 fire？提示符是否恢复安静？ |
| 5 | `Schedule a one-shot in about 1 minute: print hello with python`（强调 one-shot / recurring=false） | fire 一次后 `list_crons` 是否为空？ |
| 6 | 先建 durable job，`q` 退出再开 s14 | 启动时是否 `[cron] loaded N durable job(s)`？ |

**练习目标**：分清 **注册 → fire → 入队 → 空闲交付 → 注入**，以及 durable 与 one-shot 的差异。做完观察后务必 cancel 掉 recurring job，否则 REPL 会被刷屏。

---

## 二、基础理解题

### 题目 1：CronJob 五个字段 ⭐

填表：

| 字段 | 类型 / 取值 | 用途 |
|------|------------|------|
| id | ? | ? |
| cron | ? | ? |
| prompt | ? | ? |
| recurring | ? | ? |
| durable | ? | ? |

---

### 题目 2：四层模型 ⭐⭐

把下列组件标到对应层（1–4）：

`cron_scheduler_loop` / `cron_queue` / `queue_processor_loop` / `consume_cron_queue`（在 agent_loop 里）

各用一句话：这一层**不做什么**（例如 Scheduler 不直接调 LLM）。

---

### 题目 3：五段式 cron 读法 ⭐

写出下列表达式的含义：

1. `*/2 * * * *`
2. `0 9 * * *`
3. `0 9 * * 1-5`
4. `30 8 1 * *`
5. `0 0 * * 0`

---

### 题目 4：`cron_matches` 与 DOM/DOW OR ⭐⭐

读 `cron_matches`。下列是否匹配？假设时间为 **2026-10-19（周一）09:00**：

1. `0 9 * * *`
2. `0 9 19 * *`
3. `0 9 * * 1`（cron 星期：0=周日，1=周一）
4. `0 9 20 * 1`（DOM=20 且 DOW=周一，两者都约束）
5. `0 10 * * *`

**追问**：第 4 题为什么是 OR 而不是 AND？

---

### 题目 5：`_last_fired` 防重 ⭐⭐

调度线程每秒轮询。若没有 `_last_fired`，`*/2` 的任务在匹配的那一分钟内大约会 fire 多少次？

`minute_marker` 为什么用 `"YYYY-MM-DD HH:MM"` 而不是只记 `HH:MM`？

---

## 三、机制交叉题

### 题目 6：`cron_lock` vs `agent_lock` ⭐⭐

填表：

| 锁 | 保护什么 | 谁拿锁 |
|----|---------|--------|
| `cron_lock` | ? | ? |
| `agent_lock` | ? | ? |

**追问**：用户正在输入（拿着 `agent_lock`）时到点了，fire 的 job 去哪了？何时被消费？

---

### 题目 7：durable vs session-only ⭐⭐

1. durable 写入哪个文件？
2. 进程 `q` 退出后，调度线程还在跑吗？
3. 下次启动 `load_durable_jobs` 恢复的是「立刻补跑错过的触发」还是「从现在起继续按表调度」？

---

### 题目 8：one-shot 生命周期 ⭐⭐

`recurring=False` 的 job 在 fire 之后会发生什么？（看 `cron_scheduler_loop` 里 `if not job.recurring`）

若它同时是 durable，磁盘文件会怎样？

---

### 题目 9：与 s13 后台任务的区别 ⭐⭐

填表：

| | s13 Background | s14 Cron |
|--|----------------|----------|
| 触发者 | ? | ? |
| 典型 ID | `bg_*` | ? |
| 跨重启 | ? | ? |
| 注入格式 | `<task_notification>` | ? |

---

### 题目 10：Stop hook + 定时交付（小观察）⭐⭐⭐

cron 交付后若 Stop hook 发现还有 pending todos，会 force 再跑一轮。

**问题**：这会让「每次定时触发」比你想象的更长吗？结合你终端里出现的  
`[HOOK] Stop: all todos are completed...` 解释一下。

---

## 四、与 CC / 设计题

### 题目 11：进程内调度的边界 ⭐⭐

README 说：durable 只保留定义，不是系统 crontab。

写 2 句：什么场景必须用系统 crontab / systemd timer？教学版进程内调度够用的场景是什么？

---

### 题目 12：队列积压 ⭐⭐

Agent 被一个慢 bash（120s timeout）拖住时，`*/2` 的 job 可能在队列里积了几个？

agent_loop 开头的 `consume_cron_queue` 会一次吃掉几个？对 LLM 来说像什么？（提示：多条 `[Scheduled]` user 消息）

---

### 题目 13：REPL 被刷屏 ⭐

你遇到过：recurring durable job 每 2 分钟 fire，提示符被输出冲掉。

列出 **至少 2 个**缓解办法（cancel / one-shot / 加大间隔 / Ctrl+C 删 json …）。

---

### 题目 14：非法 cron ⭐

`validate_cron("*/0 * * * *")` 或 `"0 25 * * *"` 会怎样？  
`load_durable_jobs` 遇到非法表达式会怎样？（拖垮启动还是跳过？）

---

## 五、场景验收

### 场景 A：最小快乐路径（Windows 安全）

**Prompt**：

```
Schedule every 2 minutes: run python -c "from datetime import datetime; print(datetime.now())"
```

**验收**：

- [ ] `[cron register]` + `.scheduled_tasks.json` 有该 job
- [ ] 空闲等待后出现 `[cron fire]` → `[queue processor]` → `[inject cron]`
- [ ] 工具在数秒内完成（**不是** 120s timeout）
- [ ] 验完后 `cancel` 掉，REPL 恢复安静

---

### 场景 B：one-shot

**Prompt**：`Create a one-shot reminder in about 1 minute to print hello via python`

**验收**：

- [ ] `recurring=False`（或 list 里显示 one-shot）
- [ ] fire 一次后 list_crons 为空（或该 id 消失）
- [ ] 若曾 durable，fire 后 `.scheduled_tasks.json` 不再包含它

---

### 场景 C：跨 session durable

1. Schedule durable recurring（间隔宜 ≥5 分钟，或验完立刻 cancel）
2. `q` 退出
3. 再开 s14

**验收**：

- [ ] 启动有 `[cron] loaded ... durable job(s)`
- [ ] 调度线程重新开始轮询（不是「补跑所有错过的分钟」）

---

### 场景 D：与 s12/s13 共存

**Prompt**：`Create a task "check time", then schedule a one-shot cron in 1 minute to claim and complete that task if pending`

**验收**：

- [ ] 同时出现 `task_*` 与 `cron_*`
- [ ] 定时触发后能走 claim/complete（或合理解释为何 plan-only/依赖挡住）

---

## 六、进阶思考题

### 题目 15：该不该补跑错过的触发？⭐⭐⭐

进程关了 1 小时再开，durable `*/5` 任务：

- 方案 A：只从「现在」继续匹配  
- 方案 B：把错过的触发全部补进队列  

支持 / 反对各写 2 条。教学版选的是哪个？

---

### 题目 16：Scheduler 要不要直接调 agent_loop？⭐⭐

当前 Scheduler 只入队。若改成 fire 时直接 `agent_loop(...)`：

支持 1 条 / 反对 2 条（提示：锁、重入、与用户输入抢跑）。

---

### 题目 17：`agent_lock.acquire(blocking=False)` ⭐⭐

queue processor 用非阻塞获取。若改成 `blocking=True` 会怎样？

---

### 题目 18：cron prompt 里写死危险命令 ⭐⭐⭐

durable job 的 prompt 是「运行 `rm -rf /`」。重启后自动 loaded。

**问题**：PreToolUse / permission_hook 还能拦住吗？durable 文件本身是否该校验 prompt？写 3 句设计意见。

---

## 推荐学习路线（1–2 天）

| 天 | 题目 | 目标 |
|----|------|------|
| Day 1 | 观察表 + 题 1–5 | CronJob、四层、cron 读法、防重 |
| Day 2 | 题 6–9 + 场景 A/B | 两把锁、durable、与 s13 对比 |
| 可选 | 题 10–12 + 场景 C | Stop hook、积压、跨 session |

---

## 验收 checklist

- [ ] 能默写四层模型及数据流向
- [ ] 能读懂常用五段式 cron
- [ ] 知道 `_last_fired` 防的是「同一分钟内每秒重复 fire」
- [ ] 能区分 durable 定义持久 vs 调度进程存活
- [ ] 知道 `agent_lock` 保护用户输入与定时交付
- [ ] 能解释 Windows 上 `date` 为何不适合做演示命令
- [ ] 做完 recurring 练习会主动 cancel，避免刷屏

---

## 调试技巧

| 现象 | 排查 |
|------|------|
| 一直刷屏找不到输入处 | 是否有 recurring durable 还在？`cancel` 或删 `.scheduled_tasks.json` 后重启 |
| `date` 超时 120s | Windows 交互式 `date`；改用 python / `Get-Date` |
| 注册了但不 fire | cron 表达式是否对？是否还在同一分钟已 fire 过？系统时间？ |
| fire 了但没 `[queue processor]` | Agent 是否正拿着 `agent_lock`？看是否稍后在 agent_loop 内 `[inject cron]` |
| 启动无 loaded | 文件是否 durable？路径是否工作区根目录 `.scheduled_tasks.json`？JSON 是否合法？ |
| one-shot 还在 list 里 | fire 是否已发生？`recurring` 是否其实是 True？ |
| 同一次交付注入两次 | 慢工具跨过下一个 tick，队列积压；见题目 12 |
| 非法 cron 仍进磁盘 | `schedule_job` 是否先 `validate_cron`？手改 json 后靠 `load_durable_jobs` 跳过 |

---

## 和前后章节的关系

| 章节 | 关系 |
|------|------|
| s13 Background | 解决「慢」；s14 解决「定时」。都用 daemon 线程，但触发源不同 |
| s12 Task System | `task_*` 是工作流节点；`cron_*` 是闹钟。可组合：到点再 claim 任务 |
| s11 Error Recovery | 定时交付仍走完整 agent_loop / with_retry |
| s15 Agent Teams | 单 Agent 定时不够时，再上多 Agent 协作 |

s14 练习的核心收获：**调度与执行解耦——线程只负责到点入队，空闲时再叫醒 Agent；durable 存的是定义不是闹钟进程；练习完记得关掉 recurring，不然 REPL 会被自己的闹钟刷屏。**

---

## 优先推荐（时间有限）

1. **观察表 #1–4** — 注册 / 自动交付 / cancel 恢复安静  
2. **题目 2、5、6** — 四层 + 防重 + 两把锁  
3. **场景 A/B** — Windows 安全路径 + one-shot  
4. **题目 7** — durable 边界  
5. **题目 15** — 要不要补跑错过的触发  

---

## 参考答案提示（自测用）

<details>
<summary>题目 1–5 简答</summary>

**题 1**：id=`cron_六位随机`；cron=五段表达式；prompt=触发时注入的用户消息；recurring=是否周期；durable=是否写 `.scheduled_tasks.json`。

**题 2**：1 Scheduler=`cron_scheduler_loop`（不调 LLM）；2 Queue=`cron_queue`（不判断时间）；3 Processor=`queue_processor_loop`（不解析 cron）；4 Consumer=`consume_cron_queue`（不轮询时钟）。

**题 3**：每 2 分钟；每天 09:00；工作日 09:00；每月 1 号 08:30；每周日 00:00。

**题 4**：是；是；是（周一）；是（OR：DOW 匹配即可）；否。第 4 题标准 cron：DOM 与 DOW 都约束时任一满足即可。

**题 5**：约 60 次（每秒一次）。带日期避免跨天同一钟点误判为「本分钟已 fire」。

</details>

<details>
<summary>题目 6–10 简答</summary>

**题 6**：`cron_lock` 保护 `scheduled_jobs`/`cron_queue`/`_last_fired`，调度线程与 consume 端使用；`agent_lock` 保护「同时只有一轮 agent」，用户 REPL 与 queue processor 使用。忙时 fire 进队列；processor 跳过；当前或下一轮 agent_loop 开头 consume。

**题 7**：`.scheduled_tasks.json`；否，daemon 随进程退出；从现在起继续匹配，教学版不补跑错过的触发。

**题 8**：从 `scheduled_jobs` pop 掉；若 durable 则 `save_durable_jobs` 更新磁盘。

**题 9**：用户/模型工具调用 vs 调度线程；`bg_*` vs `cron_*`；内存不跨 vs durable 可跨；`<task_notification>` vs `[Scheduled] {prompt}`。

**题 10**：会。Stop force 会延长本轮，定时触发可能不止「跑一条命令就结束」。

</details>

<details>
<summary>题目 11–14 简答</summary>

**题 11**：应用关闭也要准时跑 → 系统 crontab；开发时 Agent 在线、会话内提醒/周期检查 → 进程内调度够用。

**题 12**：最多大约每 2 分钟积 1 个（120s 大约积 1 个，边界上可能 1–2）；`consume` 一次清空队列；LLM 可能连续看到多条 `[Scheduled]`。

**题 13**：cancel；改 one-shot；间隔改 `*/5` 或更大；Ctrl+C 后删 json。

**题 14**：`schedule_job` 返回错误字符串；load 时 print skipping，不拖垮启动。

</details>

<details>
<summary>题目 15–18 简答</summary>

**题 15**：教学版是 A。B 可能雪崩式补跑；A 可能漏掉关键检查。

**题 16**：反对：与用户输入重入、难用锁、调度线程阻塞在 LLM 上会耽误其他 job。入队更干净。

**题 17**：processor 会卡住等用户回合结束，失去「轮询跳过」的简单性；也可能和设计预期不符地长时间堵在 acquire。

**题 18**：工具执行仍受 PreToolUse 拦截；但自动 loaded 的恶意 prompt 仍会反复尝试。应对：load 时审核、敏感词拒绝、durable 默认 false、或签名/白名单命令。

</details>
