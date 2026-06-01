# s08 Context Compact 练习题目

s08 在 **s07 全套（skill + subagent + todo + hook + plan-only）** 基础上，核心新增是 **四层压缩管线**：

- **L3** `tool_result_budget` — 最后一条 user 消息里超大 tool_result 落盘（0 API）
- **L1** `snip_compact` — 消息条数 >50 时裁中间（0 API）
- **L2** `micro_compact` — 旧 tool_result 占位，保留最近 3 条（0 API）
- **L4** `compact_history` — 仍超限则 LLM 摘要（1 API）
- **应急** `reactive_compact` — API 报 `prompt_too_long` 时触发
- **工具** `compact` — 模型主动摘要

每道题通常只改这些部分：

- `CONTEXT_LIMIT` / `KEEP_RECENT` / `PERSIST_THRESHOLD` / `max_bytes`
- `snip_compact` / `micro_compact` / `tool_result_budget` / `compact_history`
- `agent_loop` 里压缩顺序与 `compact` 分支
- `.transcripts/` / `.task_outputs/tool-results/` 落盘路径

---

## 核心机制复习

```
每轮 agent_loop 开头（父 Agent）
   ↓
L3 tool_result_budget  → 最后一条 user 里 tool_result 总大小 >200KB → 落盘
   ↓
L1 snip_compact      → messages 条数 >50 → 留头 3 + 尾 47
   ↓
L2 micro_compact     → 除最近 3 条外，旧 tool_result >120 字符 → 占位符
   ↓
estimate_size > CONTEXT_LIMIT?
   ├─ Yes → L4 compact_history（写 transcript + LLM 摘要，替换 messages）
   └─ No  → 直接调 API
   ↓
API 失败 prompt_too_long?
   └─ Yes → reactive_compact（摘要 + 保留最后 5 条），最多重试 1 次
```

**关键洞察**：

| 问题 | 答案 |
|------|------|
| 压缩在什么时候跑？ | **每轮 LLM 调用前**（`agent_loop` 循环顶部） |
| 执行顺序能换吗？ | **不能随意换**：budget 必须在 micro 前，否则大内容先被占位符清掉无法落盘 |
| L1–L3 要调 LLM 吗？ | **不要**，纯结构/文本操作 |
| L4 会丢细节吗？ | **会**，活跃上下文只剩摘要；完整记录在 `.transcripts/` |
| 子 Agent 会压缩吗？ | **不会**，`spawn_subagent` 独立 messages，不跑 compact 管线 |
| `load_skill` 全文会被压吗？ | **会**，若之后又跑了更多工具，旧的 load 结果可能被 L2 占位 |
| `compact` 工具和 auto compact 区别？ | **模型主动** vs **estimate_size 超阈值自动**；都调 `compact_history` |

---

## 一、入门：用 Prompt 观察（不用写代码）

先运行：

```sh
python s08_context_compact/code.py
```

| # | Prompt | 观察重点 |
|---|--------|---------|
| 1 | `Read README.md, then read code.py, then read s01_agent_loop/README.md` | 连续 read 后，`.debug/session.jsonl` 里**最早**的 read result 是否变短/占位 |
| 2 | `Read every .py file in s08_context_compact/` | 是否出现 `<persisted-output>`？文件是否在 `.task_outputs/tool-results/` |
| 3 | 同一 session 里连续对话 15+ 轮（或故意读很多大文件） | 终端是否打印 `[auto compact]`？`.transcripts/` 是否新增 jsonl |
| 4 | `Use the compact tool — I'm running out of context` | 是否调用 `compact`？history 是否变成一条 `[Compacted]` 摘要 |
| 5 | 对比 s07：同一「load code-review + 多读几个文件」任务 | s08 下旧 tool_result 是否比 s07 更早变占位符 |

**练习目标**：分清 **四层各自解决什么问题**，以及 **0 API vs 1 API** 的代价。

---

## 二、基础理解题

### 题目 1：四层分别压什么？⭐

填空：

| 层 | 主要压缩对象 | API 成本 |
|----|-------------|----------|
| L1 snip | ? | ? |
| L2 micro | ? | ? |
| L3 budget | ? | ? |
| L4 auto | ? | ? |

**验收**：L1 压**整条 message**；L2 压**旧 tool_result 内容**；L3 压**单轮多条超大 result**；L4 压**整段对话**。

---

### 题目 2：为什么 budget 在 micro 前面？⭐⭐

**场景**：Agent 一次读了 5 个大文件，最后一条 user 消息里有 5 个各 80KB 的 tool_result。

**问题**：若顺序改成 `micro → budget`，会发生什么？

**答案应包含**：

- micro 会把旧 result 替换成 ~50 字符占位符
- budget 需要**完整内容**才能写入 `.task_outputs/tool-results/{tool_use_id}.txt`
- 顺序反了 → **磁盘上没全文，模型也只剩占位符**，无法 `read_file` 恢复

**你会练到**：README「顺序不能换」的原因。

---

### 题目 3：`KEEP_RECENT = 3` 的含义 ⭐

**问题**：对话里按时间顺序有 8 次 `read_file` 的 tool_result，L2 跑完后哪几条保留全文？

**答案**：**最后 3 条**保留；前 5 条若 content >120 字符 → 占位符。

**追问**：第 8 条只有 80 字符的错误信息，会被压吗？  
**答案**：**不会**（≤120 不替换）。

---

### 题目 4：`tool_result_budget` 只看哪条消息？⭐⭐

读 `tool_result_budget` 源码：

```python
last = messages[-1] if messages else None
```

**问题**：

1. 为什么只统计 **最后一条** user 消息？
2. 若倒数第二条里还有 500KB 旧 result，budget 会处理吗？

**答案应包含**：

- 典型模式：assistant tool_use → user 一批 tool_result；**最新一轮**最容易爆
- 更早的 result 由 **L2 micro** 处理，不是 budget 的职责

---

### 题目 5：`estimate_size` 的局限 ⭐

**现状**：`estimate_size(msgs) = len(str(msgs))`

**思考题**：这和真实 token 数有何差别？对 `[auto compact]` 触发时机有什么影响？

**答案应包含**：字符数 ≠ token；中文/代码/JSON 比例不同；教学版**故意简化**；可能过早或过晚触发 L4。

---

## 三、实现与改动题

### 题目 6：调参实验 ⭐⭐

**目标**：改常量并观察行为（改完记得改回或注释记录）：

| 常量 | 默认 | 实验值 | 预期 |
|------|------|--------|------|
| `KEEP_RECENT` | 3 | 1 | 几乎只保留最后 1 条 tool_result |
| `CONTEXT_LIMIT` | 50000 | 5000 | 更容易 `[auto compact]` |
| `PERSIST_THRESHOLD` | 30000 | 5000 | 更小文件也会落盘 |

**验收**：至少完成 1 项，写 2–3 句观察结论。

---

### 题目 7：`micro_compact` 白名单 ⭐⭐

**场景**：s07 题 8 — `load_skill` 返回 2000+ 字符后，L2 可能在后续轮次把它压成占位符。

**可选实现**：对 `load_skill` 的 tool_result **永不 micro 压缩**（需给 block 打标或按 tool_use 关联工具名 — 教学版可简化：content 以 `# ` 开头且来自 skill 的跳过）。

**思考题**：白名单 vs「按 output 大小跳过」哪个更好？

---

### 题目 8：`compact` 工具与 Stop hook ⭐⭐

**场景**：模型调用 `compact` 后，`messages` 被替换成摘要；磁盘上 todos 可能仍是 in_progress。

**问题**：

1. `compact` 会清 `.tasks/current_todos.json` 吗？
2. 压缩后 Stop hook 还会 nag pending todos 吗？

**答案**：compact **不**清 todo 文件；Stop hook **仍**可能 force 继续 — 摘要里应保留「剩余工作」。

---

### 题目 9：实现 transcript 检索工具（进阶）⭐⭐⭐

**现状**：L4 把完整对话写入 `.transcripts/`，但模型**没有工具**读回 transcript。

**目标**：新增 `read_transcript(path)` 或 `list_transcripts()`，让模型在摘要丢细节后能查原文。

**你会练到**：压缩 = **活跃上下文** vs **冷存储** 分离。

---

### 题目 10：`reactive_compact` vs `compact_history` ⭐⭐

对比两者返回值：

```python
# compact_history
return [{"role": "user", "content": f"[Compacted]\n\n{summary}"}]

# reactive_compact
return [{"role": "user", "content": f"[Reactive compact]\n\n{summary}"}, *messages[-5:]]
```

**问题**：为什么应急要多保留 **最后 5 条**？`MAX_REACTIVE_RETRIES = 1` 意味着什么？

---

## 四、与 s07 / hook 的交叉题

### 题目 11：`load_skill` + L2 + L4 完整链路 ⭐⭐⭐

**Prompt**：

> Load code-review skill, then read s08_context_compact/code.py and review it

**观察并回答**：

1. Layer 2 skill 正文何时进入 history？
2. 再 read 5 个文件后，code-review 的 tool_result 还在吗？
3. 若触发 `[auto compact]`，skill checklist 还在上下文吗？

**你会练到**：s07 题 14 的答案 — load 按需，**compact 负责事后腾地方**。

---

### 题目 12：子 Agent 为何不 compact？⭐⭐

**问题**：`spawn_subagent` 里直接 `client.messages.create`，不跑 L1–L4。这样设计合理吗？

**答案应包含**：

- 子上下文隔离、任务短、返回父 Agent 只有 summary
- 父 Agent 的 history 里只有 **一条** task tool_result（子 Agent 输出）
- 真正膨胀在**父** session，压缩管线放在父 `agent_loop`

---

### 题目 13：`large_output_hook` 与 L3 分工 ⭐⭐

| 机制 | 触发点 | 阈值 | 行为 |
|------|--------|------|------|
| `large_output_hook` | PostToolUse | 50000 字符 | 截断 + `...(truncated)` |
| L3 budget | 下轮 LLM 前 | 单条 >30000 落盘；一轮合计 >200KB | 落盘 + preview |

**问题**：同一条 60KB 的 bash 输出，会先经历什么？两者会冲突吗？

---

### 题目 14：`todo_inject_hook` 与 compact ⭐

**问题**：L2 把旧 tool_result 换成占位符后，`<current_todos>` 还会被 inject 到新 result 上吗？

**答案**：**会**（父 Agent、非 todo_write/load_skill 等跳过列表）；但**旧** result 上的 todo 快照随占位符一起「逻辑上丢失」。

---

## 五、场景验收

### 场景 A：连续 read（L2）

**Prompt**：

> Read these files one by one: README.md, code.py, EXERCISES.md, s07_skill_loading/code.py, s06_subagent/code.py

**验收**：

- [ ] 至少 2 条较早 read 的 session.jsonl output 含 `Earlier tool result compacted` 或明显变短
- [ ] 最近 3 次 read 仍有实质内容

---

### 场景 B：大输出落盘（L3）

**Prompt**：

> Run bash to print the first 50000 characters of code.py (or read a very large file)

**验收**：

- [ ] tool_result 含 `<persisted-output>`
- [ ] `.task_outputs/tool-results/` 下有对应 `{tool_use_id}.txt`

---

### 场景 C：主动 compact

**Prompt**：

> We've been working a while. Use the compact tool to summarize our conversation so far, then tell me what you still remember about my original goal.

**验收**：

- [ ] 出现 `compact` 工具调用
- [ ] 下一轮 context 以 `[Compacted]` 为主
- [ ] 模型仍能说出摘要中的目标（可能丢细节）

---

### 场景 D：skill + compact（跨章）

**Prompt**：

> Load mcp-builder skill, read skills/mcp-builder/SKILL.md again via read_file, then compact

**验收**：

- [ ] 能解释 load_skill 与 read_file **两条路径**进 history
- [ ] compact 后 SKILL 全文不在活跃 context，但 transcript 在磁盘

---

## 六、设计思考题（不写代码也可）

### 题目 15：便宜的先跑，贵的后跑

用 3–5 句话解释：为何 L1–L3 都是 0 API，L4 才调 LLM？若先跑 L4 有什么问题？

---

### 题目 16：压缩后丢什么不可接受？

列举 **3 类** 不应仅依赖 L4 摘要保留的信息，并说明 s09 Memory 可能如何补位。

---

### 题目 17：与真实 Claude Code 的差异

README「深入 CC 源码」提到：CC 有 `contextCollapse`、post-compact 恢复最近文件、精确 token 阈值。

教学版省略了哪些？核心设计思想是否保留？

---

### 题目 18：该让子 Agent 也跑 micro_compact 吗？

列出支持 / 反对各 2 条。

---

## 推荐学习路线（2 天）

| 天 | 题目 | 目标 |
|----|------|------|
| Day 1 | 观察表 + 题 1–5 | 四层分工 + 顺序 |
| Day 2 | 题 6–8 + 场景 A/B | 调参 + compact 工具 |
| 可选 | 题 9–11 + 场景 C/D | s07 衔接 + transcript |

---

## 验收 checklist

- [ ] 能画出 budget → snip → micro → auto 顺序
- [ ] 能解释 L2「最近 3 条」与 120 字符阈值
- [ ] 知道 L3 落盘路径与 `<persisted-output>` 含义
- [ ] 知道 L4 写 transcript、messages 变摘要
- [ ] 知道 reactive 比 auto 多保留 5 条 tail
- [ ] 子 Agent 不跑压缩；父 Agent 跑
- [ ] 能说明 load_skill 大 result 与 compact 的关系（s07→s08）

---

## 调试技巧

| 现象 | 排查 |
|------|------|
| 从不 auto compact | `CONTEXT_LIMIT` 是否太大；`estimate_size` 是否偏小 |
| 落盘文件空/不存在 | budget 是否在 micro **之后**误调顺序；`PERSIST_THRESHOLD` |
| compact 后模型「失忆」 | 正常；查 `.transcripts/`；摘要 prompt 是否保留 remaining work |
| reactive 后仍报错 | 已重试 1 次；tail 5 条仍太大 |
| skill 内容消失 | L2 占位或 L4 摘要；是否需白名单或重新 load_skill |
| session.jsonl 与 messages 不一致 | compact **原地改** messages；jsonl 是 hook 日志快照 |

---

## 和前后章节的关系

| 章节 | 关系 |
|------|------|
| s07 Skill Loading | load 进来的大 tool_result 是 compact 的主要「燃料」 |
| s09 Memory | 压缩丢细节后，选择性长期记忆 |
| s11（错误恢复） | reactive 重试用尽后的完整恢复策略 |

s08 练习的核心收获：**上下文总会满；先廉价清理结构，再昂贵摘要；顺序保证可恢复；活跃 context ≠ 永久档案。**

---

## 优先推荐（时间有限）

1. **观察表 #1–2** — 10 分钟看 L2/L3 效果
2. **题目 2** — 理解 budget 必须在 micro 前
3. **题目 3** — 理解 KEEP_RECENT
4. **场景 A** — 验证 micro_compact
5. **题目 11** — 串起 s07 + s08
