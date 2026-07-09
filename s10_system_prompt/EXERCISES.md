# s10 System Prompt 练习题目

s10 在 **s09 全套（memory + compact + skill + subagent + todo + hook）** 基础上，核心新增是 **运行时组装 system prompt**：

- **PROMPT_SECTIONS**：按主题拆分的 prompt 片段字典
- **assemble_system_prompt(context)**：根据真实状态选 section、拼接字符串
- **get_system_prompt(context)**：`json.dumps` 做 cache key，避免重复拼接
- **update_context()**：从磁盘/注册表生成 context（不是从 messages 猜）

每道题通常只改这些部分：

- `PROMPT_SECTIONS` / `assemble_system_prompt` / `get_system_prompt`
- `update_context` / `_invalidate_prompt_cache`
- `agent_loop` 里 `get_system_prompt` + `load_memories` 的顺序
- `reload_skills` 与 cache 失效

---

## 核心机制复习

```
每轮 agent_loop（每一圈 tool 循环）
   ↓
update_context() → context 字典（工具列表、skill 目录、MEMORY.md 索引）
   ↓
get_system_prompt(context) → 拼稳定 section（可 [cache hit]）
   ↓
load_memories(messages) → <relevant_memories> 追加到 system（不进 cache key）
   ↓
s09 压缩管线 → LLM → 工具循环
   ↓
轮末 extract / consolidate（s09，不变）
```

**关键洞察**：

| 问题 | 答案 |
|------|------|
| `context` 是什么？ | **运行环境快照** dict，不是对话 history |
| `messages` 进 context 吗？ | **不进**；Layer 2 用 `load_memories(messages)` 单独追加 |
| 哪些 section 始终加载？ | `identity`、`tools`、`workspace` |
| 哪些 section 按需加载？ | `skills`（有 skill 时）、`memory_index` + `memory_behavior`（索引非空时） |
| cache 命中条件？ | `context` 序列化后 key 与上轮相同 |
| 与 s09 `build_system()` 关系？ | 同样内容，拆成 section + 按需 + 缓存 |
| 子 Agent 用组装 prompt 吗？ | **否**，仍用硬编码 `SUB_SYSTEM` |

---

## 一、入门：终端观察（不用写代码）

先运行：

```sh
python s10_system_prompt/code.py
```

| # | Prompt | 观察重点 |
|---|--------|---------|
| 1 | `Read README.md`（需多步 tool） | 第 2 圈起是否出现 `[cache hit]`？`[assembled] sections` 有哪些？ |
| 2 | 确认 `.memory/` 为空或删 `MEMORY.md` | sections 里**没有** `memory_index` |
| 3 | `Remember: I prefer tabs. Then create test.py` | extract 后下一轮 sections 是否出现 `memory_index, memory_behavior`？ |
| 4 | 同一 prompt 内连续 3+ 次 tool | 稳定 section 是否 cache hit？`load_memories` 仍每轮跑 |
| 5 | `reload_skills`（或改 `skills/` 后触发） | cache 是否失效、重新 `[assembled]`？ |

**练习目标**：分清 **context → 稳定 system** 与 **messages → 动态 memory 注入** 两条路径。

---

## 二、基础理解题

### 题目 1：`PROMPT_SECTIONS` 有哪些 key？⭐

填空：

| key | 加载策略 | 判断条件 |
|-----|---------|---------|
| identity | 始终 | — |
| tools | 始终 | `context["enabled_tools"]` |
| workspace | 始终 | `context["workspace"]` |
| skills | 按需 | `skill_catalog` 非空且 ≠ `(no skills found)` |
| memory_index | 按需 | ? |
| memory_behavior | 按需 | ? |

---

### 题目 2：`context` 里四个字段从哪来？⭐⭐

读 `update_context()`，填表：

| 字段 | 数据来源 |
|------|---------|
| `enabled_tools` | ? |
| `workspace` | ? |
| `skill_catalog` | ? |
| `memory_index` | ? |

**追问**：为何不用 `messages` 里的关键词判断要不要加载 memory section？

---

### 题目 3：`[cache hit]` 何时出现？⭐⭐

**场景**：一条 user 消息触发 bash → read_file → edit_file 三轮 tool。

**问题**：

1. 哪几轮可能 `[cache hit]`？
2. 什么操作会让 cache 失效？
3. `load_memories` 的结果变了吗？system 字符串一定相同吗？

---

### 题目 4：为何用 `json.dumps` 不用 `hash()`？⭐

README 提到两点原因，各写一句。

---

### 题目 5：s09 vs s10 prompt 对比 ⭐⭐

填表：

| | s09 | s10 |
|--|-----|-----|
| 生成方式 | `build_system()` 一大段 | ? |
| 每轮调用 | 每圈 `build_system()` | ? |
| 缓存 | 无 | ? |
| memory 索引 | 在 SYSTEM 字符串里 | 哪个 section？ |
| Layer 2 全文 | `load_memories` 追加 | 变了吗？ |

---

## 三、机制交叉题（s09 + s10）

### 题目 6：memory 在 system 里分几层？⭐⭐

**问题**：

1. `memory_index` section 放什么？
2. `<relevant_memories>` 从哪来？在 cache 里吗？
3. 为何不把 `load_memories` 放进 `update_context`？

---

### 题目 7：`reload_skills` 之后 prompt 怎么变？⭐⭐

读 `reload_skills()` 和 `_invalidate_prompt_cache()`。

**问题**：改 `skills/foo/SKILL.md` 后调用 `reload_skills`，下一轮 `[assembled]` 会多/少哪个 section？为何必须 invalidate cache？

---

### 题目 8：新 user 输入前为何 `_invalidate_prompt_cache()`？⭐⭐

读 `__main__` 里每条 prompt 前的逻辑。

**问题**：若上一轮 `extract_memories` 写了新 memory，不 invalidate 会怎样？

---

### 题目 9：子 Agent 的 system prompt ⭐

**问题**：`spawn_subagent` 用 `SUB_SYSTEM` 还是 `get_system_prompt`？为何？

---

### 题目 10：加 `plan_only` section（小改代码）⭐⭐⭐

**目标**：当 `plan_only_mode == True` 时，在 `assemble_system_prompt` 里追加一段：

> "Plan-only mode: use todo_write/todo_read only; do not execute other tools."

**你会练到**：条件 section + context 字段扩展 + cache key 变化。

**验收**：

- [ ] 含「只列计划」的 prompt 后 `[assembled]` 出现新 section
- [ ] 下一条普通 prompt 该 section 消失

---

## 四、与 s09 / CC 的设计题

### 题目 11：教学版 cache vs CC API prompt cache ⭐⭐

README「深入 CC 源码」提到 CC 有 `SYSTEM_PROMPT_DYNAMIC_BOUNDARY`。

写 3 句：教学版 `get_system_prompt` 缓存的是什么？和 CC API 级 prompt cache 差在哪？

---

### 题目 12：静态 section vs 动态 section ⭐⭐

按教学版实现分类：

| 内容 | 静态 / 动态 | 理由 |
|------|------------|------|
| identity | ? | ? |
| tools 列表 | ? | ? |
| skill catalog | ? | ? |
| memory_index | ? | ? |
| `<relevant_memories>` | ? | ? |

---

### 题目 13：该把 `enabled_tools` 写死在 PROMPT_SECTIONS 吗？⭐⭐

**问题**：现在 tools section 从 `TOOL_HANDLERS.keys()` 生成。若写死 `"bash, read_file, write_file"` 会有什么问题？（提示：s10 合并版实际工具数量）

---

### 题目 14：`update_context` 的 `context` 参数为何未使用？⭐

见代码 `update_context(context, messages)`。

**问题**：这是 bug 还是设计？`messages` 去哪了？

---

## 五、场景验收

### 场景 A：README 三板斧（观察组装）

1. `Read the file README.md`
2. 手动或让 agent 确保 `.memory/MEMORY.md` 有内容
3. 再问：`What memories are available?`

**验收**：

- [ ] 见过 `[assembled] sections: identity, tools, workspace`
- [ ] 有 memory 时 sections 含 `memory_index, memory_behavior`
- [ ] 多步 tool 时见过 `[cache hit]`

---

### 场景 B：cache + memory 交叉

1. Session 开始：无 `.memory/MEMORY.md` → 发一条需 2+ tool 的请求
2. `Remember: always use type hints.` → 等 extract
3. 再问：`Add a function to test.py`（观察 cache 与 memory section）

**验收**：

- [ ] extract 后下一轮 `[assembled]` 出现 memory section
- [ ] 新 memory 未改变时，同轮后续 tool 可 cache hit

---

### 场景 C：s09 memory + s10 prompt 协作

**Prompt**：

> Load code-review skill and review test.py — use my tab preference from memory

**验收**：

- [ ] `load_skill` 出现
- [ ] system 里曾有 skill / memory 相关 section 或 inject
- [ ] 审查体现 memory 偏好（若模型遵守）

---

## 六、进阶思考题

### 题目 15：该把 `<relevant_memories>` 放进 context 吗？⭐⭐⭐

列出 **支持放进 context** / **反对** 各 2 条（相对当前「cache 后追加」设计）。

---

### 题目 16：新增 `compact` section 提示 ⭐⭐

s08 有 `compact` 工具。是否应在 `PROMPT_SECTIONS` 加一段说明何时主动 compact？

写 2 句理由（加 or 不加）。

---

### 题目 17：extract 之后要不要自动 `_invalidate_prompt_cache()`？⭐⭐

**场景**：轮末 `extract_memories` 更新 `MEMORY.md`，同一 `agent_loop` 已 `return`。

**问题**：当前在**下一条** user 输入前 invalidate。若在 `extract` 后立刻 invalidate 有无必要？

---

## 推荐学习路线（1–2 天）

| 天 | 题目 | 目标 |
|----|------|------|
| Day 1 | 观察表 + 题 1–5 | section、context、cache |
| Day 2 | 题 6–9 + 场景 A/B | 与 s09 memory 交叉 |
| 可选 | 题 10–12 + 场景 C | 改代码 + CC 对比 |

---

## 验收 checklist

- [ ] 能解释 `context` 四个字段及数据来源
- [ ] 能说出始终加载 vs 按需加载的 section
- [ ] 知道 `load_memories` 不进 cache 的原因
- [ ] 能解释 `[cache hit]` 与 `_invalidate_prompt_cache` 时机
- [ ] 能对比 s09 `build_system` 与 s10 组装等价关系
- [ ] 知道子 Agent 仍用 `SUB_SYSTEM`
- [ ] 能区分教学版字符串 cache 与 CC API prompt cache

---

## 调试技巧

| 现象 | 排查 |
|------|------|
| 从不 `[cache hit]` | 每轮 context 是否变？（memory_index、skill 更新） |
| memory section 不出现 | `MEMORY.md` 是否空？`read_memory_index()` 返回值 |
| reload 后仍旧 catalog | 是否调了 `reload_skills`？cache invalidate 了吗？ |
| extract 后下轮仍无 memory section | `__main__` 是否 `_invalidate_prompt_cache`？文件是否写入 `.memory/` |
| `[assembled]` 缺 skills | `skills/` 空？catalog 为 `(no skills found)` |
| 偏好未遵守 | 先查 s09 Layer 2 inject；与 s10 section 无关时仍是 side-query 问题 |

---

## 和前后章节的关系

| 章节 | 关系 |
|------|------|
| s09 Memory | `memory_index` / `memory_behavior` 是 section；Layer 2 仍 `load_memories` 追加 |
| s08 Compact | 压 `messages`，不压 `PROMPT_SECTIONS`；extract 仍用 pre_compress |
| s07 Skill | `skills` section 按需加载；`reload_skills` 触发 cache 失效 |
| s11 Error Recovery | prompt 组装就绪后，下一章处理 API/上下文错误 |

s10 练习的核心收获：**prompt 是配置单拼出来的——稳定环境进 context + cache，对话相关 memory 动态追加；加能力加 section，不必重写整段 SYSTEM。**

---

## 优先推荐（时间有限）

1. **观察表 #1、#3** — 10 分钟建立 assembled / cache hit 直觉
2. **题目 2、6** — context 与 memory 两层
3. **题目 5** — s09→s10 对照
4. **场景 A** — README 验收
5. **题目 15** — 理解为何不把 inject 放进 cache

---

## 参考答案提示（自测用）

<details>
<summary>题目 1–5 简答</summary>

**题 1**：`memory_index` / `memory_behavior` 均在 `memory_index` 非空时加载。

**题 2**：`TOOL_HANDLERS`；`WORKDIR`；`list_skills()`；`read_memory_index()`。不用关键词——要反映磁盘真实状态。

**题 3**：第 2 圈起若 context 不变可 hit；`reload_skills`、新 memory、`__main__` 每条 prompt 前 invalidate；`load_memories` 每轮可能变，追加后 system 字符串可不同。

**题 4**：`hash()` 进程随机化不稳定；嵌套结构不可 hash。

**题 5**：`PROMPT_SECTIONS` + assemble；`get_system_prompt(context)`；`json.dumps` cache；`memory_index` section；Layer 2 不变。

</details>

<details>
<summary>题目 14 简答</summary>

有意设计：`context` 每次从真实状态重算；`messages` 交给 `load_memories`，避免每轮对话变化导致 cache 失效。

</details>
