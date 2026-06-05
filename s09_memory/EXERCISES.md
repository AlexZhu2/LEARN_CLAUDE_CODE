# s09 Memory 练习题目

s09 在 **s08 全套（compact + skill + subagent + todo + hook）** 基础上，核心新增是 **持久记忆系统**：

- **Layer 1**：`MEMORY.md` 索引注入 `build_system()`（便宜，每轮都带）
- **Layer 2**：`load_memories()` side-query → 注入 `<relevant_memories>`（按需全文）
- **写入**：`extract_memories()` 每轮结束后从 `pre_compress` 快照提取
- **整理**：`consolidate_memories()` 文件数 ≥ 10 时 Dream 式合并

每道题通常只改这些部分：

- `write_memory_file` / `_rebuild_memory_index` / `read_memory_index`
- `select_relevant_memories` / `load_memories`
- `extract_memories` / `consolidate_memories`
- `build_system()` / `agent_loop` 里的 inject + extract 时机
- `.memory/` 目录结构

---

## 核心机制复习

```
每轮 agent_loop
   ↓
build_system() → SYSTEM 含 MEMORY.md 索引（Layer 1）
   ↓
load_memories(messages) → side-query 选文件 → <relevant_memories> 追加到 system
   ↓
pre_compress = snapshot(messages)   ← 压缩前快照，供 extract 用
   ↓
s08 压缩管线 → LLM → 工具循环
   ↓
stop_reason != tool_use → extract_memories(pre_compress) → consolidate_memories()
   ↓
.memory/*.md + MEMORY.md 更新（跨 session 保留）
```

**关键洞察**：

| 问题 | 答案 |
|------|------|
| 记忆全文在 SYSTEM 里吗？ | **不在**，索引在 SYSTEM；全文在 `<relevant_memories>` |
| 和 s07 skill 有何相似？ | 都是 **索引常驻 + 按需加载** |
| 和 s08 compact 关系？ | compact 丢细节；memory **不参与压缩**，落盘保留 |
| 为何用 pre_compress？ | micro/budget 会**原地改** tool_result，extract 需要完整对话 |
| 子 Agent 有 memory 吗？ | **无** inject/extract；记忆在父 Agent |
| extract 何时跑？ | 本轮 **真正结束**（非 tool_use 且 Stop hook 不 force） |

---

## 一、入门：多轮 Prompt 观察（不用写代码）

先运行：

```sh
python s09_memory/code.py
```

| # | Prompt（分多轮输入） | 观察重点 |
|---|---------------------|---------|
| 1 | `I prefer tabs over spaces. Remember that.` | 轮次结束后 `[Memory: extracted N]`？`.memory/` 有新 `.md`？ |
| 2 | `Create test.py with a hello function` | 是否用 tab（若模型遵守 memory） |
| 3 | `What are my indentation preferences?` | 是否不调工具也能答（索引或 inject） |
| 4 | `I also prefer single quotes for strings.` | 是否再提取一条 memory |
| 5 | 退出重开 s09，只问：`What do you remember about my coding style?` | **跨 session** 是否仍记得 |

**练习目标**：分清 **索引（每轮 SYSTEM）**、**按需 inject**、**轮末 extract** 三条路径。

---

## 二、基础理解题

### 题目 1：四类 memory type ⭐

填空：

| type | 回答什么 | 举例 |
|------|---------|------|
| user | ? | tab 缩进 |
| feedback | ? | 别 mock 数据库 |
| project | ? | auth 重写原因 |
| reference | ? | bug 在 Linear XXX |

---

### 题目 2：MEMORY.md vs `.memory/*.md` ⭐⭐

**问题**：

1. 索引里一行通常长什么样？
2. `write_memory_file` 后谁负责更新索引？
3. 为何不把 30 个 memory 全文都塞进 SYSTEM？

**你会练到**：和 s07 skill 相同的 **Layer 1 / Layer 2** 模式。

---

### 题目 3：`select_relevant_memories` 怎么选？⭐⭐

读源码，回答：

1. side-query 输入是什么？（catalog 格式）
2. LLM 失败时 fallback 是什么？
3. 最多选几条？（`max_items`）

**验收**：能说出「LLM 选 index，不是 embedding」。

---

### 题目 4：为何 extract 用 `pre_compress`？⭐⭐⭐

**场景**：连续 read 5 个大文件 → L2 micro 把旧 result 变占位符 → 轮末 extract。

**问题**：若 `extract_memories(messages)` 用**压缩后**的 messages，会丢什么？

**答案应包含**：用户偏好可能只在早期 user 消息里；tool_result 占位后 extract 看不到文件内容。

---

### 题目 5：`consolidate_memories` 何时触发？⭐

**问题**：

1. `CONSOLIDATE_THRESHOLD` 默认多少？
2. 触发后旧文件怎么处理？
3. 和 CC 的 Dream 四层门控比，教学版简化了什么？

---

## 三、实现与改动题

### 题目 6：手动写一条 memory ⭐

在 `.memory/` 下新建 `user-pref-tabs.md`（含 frontmatter），运行 agent，观察 `MEMORY.md` 是否自动出现链接。

**注意**：手建文件后需调 `_rebuild_memory_index()` 或再 `write_memory_file` 一次才更新索引——思考为何。

---

### 题目 7：`build_system()` 每轮重建 ⭐⭐

**对比 s08**：s08 用模块级 `SYSTEM`；s09 在 `agent_loop` 里 `system = build_system()`。

**问题**：新写入 memory 后，**同一 session 下一轮**模型能否在 SYSTEM 索引里看到？为何？

---

### 题目 8：memory 与 compact 协作 ⭐⭐⭐

**Prompt 序列**：

1. 多轮对话 + 偏好「用 tab」
2. 故意触发 `[auto compact]`
3. 问：`What indentation do I prefer?`

**观察**：答案来自 **memory 文件** 还是 **compact 摘要**？看 `.memory/` 和 transcript。

---

### 题目 9：降低 `CONSOLIDATE_THRESHOLD` 实验 ⭐⭐

临时改为 `3`，连续输入 3 条不同偏好，观察是否出现 `[Memory: consolidated ...]`。

**验收**：写 2–3 句 consolidate 前后 `.memory/` 文件数变化。

---

### 题目 10：给 memory 加 `read_memory` 工具（进阶）⭐⭐⭐

**目标**：新增工具让模型主动读 `.memory/foo.md`（带路径校验，仅允许 `.memory/` 下）。

**你会练到**：索引 + 按需加载 vs 模型主动 read 的边界。

---

## 四、与 s07 / s08 的交叉题

### 题目 11：Memory vs Skill 双层对比 ⭐⭐

填表：

| | s07 Skill | s09 Memory |
|--|-----------|------------|
| 目录 | `skills/` | `.memory/` |
| 索引 | SYSTEM catalog | `MEMORY.md` |
| 按需加载 | `load_skill` | `load_memories` |
| 谁写入 | 人维护 SKILL.md | `extract_memories` 自动 |
| 跨 compact | skill 可被 L2 压（除非白名单） | **文件不参与 compact** |

---

### 题目 12：compact 后 memory 还在吗？⭐⭐

**问题**：L4 `compact_history` 替换 messages 后，`.memory/` 里文件会消失吗？extract 本轮还会跑吗？

---

### 题目 13：Stop hook 与 extract 顺序 ⭐⭐

**场景**：todo 仍有 pending，Stop hook force 继续。

**问题**：此轮会 `extract_memories` 吗？为什么？

**答案**：Stop force → `continue`，**不** extract；只有真正 `return` 才 extract。

---

## 五、场景验收

### 场景 A：README 标准四步（记忆累积）

按 README「试一下」1–4 步执行。

**验收**：

- [ ] `.memory/` 至少 1 个 `.md`
- [ ] `MEMORY.md` 有链接行
- [ ] 终端出现过 `[Memory: extracted ...]`
- [ ] 第 3 步能答出 tab 偏好

---

### 场景 B：跨 session

1. Session 1：`Remember: always use type hints in Python.`
2. 退出 `q`
3. Session 2：`What Python style do I prefer?`

**验收**：

- [ ] 不依赖 session history，仍能从 `.memory/` 回答

---

### 场景 C：memory + skill

**Prompt**：

> Load code-review skill and review test.py — but use my indentation preference from memory

**验收**：

- [ ] `load_skill` 出现
- [ ] 审查/编辑时体现 memory 中的缩进偏好

---

## 六、设计思考题

### 题目 14：Memory vs Session Memory（CC）

README「深入 CC 源码」对比 User Memory 与 Session Memory。

写 3–5 句：教学版 s09 实现了哪一种？s08 compact 摘要扮演什么角色？

---

### 题目 15：side-query 的成本

每轮 `select_relevant_memories` 可能多 1 次 LLM 调用。

**问题**：为何不用 embedding？何时 fallback 关键词匹配够用？

---

### 题目 16：该让 extract 在 compact 之后跑吗？

列出 **支持** / **反对** 各 2 条（相对当前「pre_compress + 轮末 extract」设计）。

---

## 推荐学习路线（2 天）

| 天 | 题目 | 目标 |
|----|------|------|
| Day 1 | 观察表 + 题 1–5 | 存储/加载/提取/整理 |
| Day 2 | 题 6–8 + 场景 A/B | pre_compress + compact 交叉 |
| 可选 | 题 9–11 + 场景 C | consolidate + skill 协作 |

---

## 验收 checklist

- [ ] 能解释 MEMORY.md 索引 vs `.memory/*.md` 全文
- [ ] 能解释 `load_memories` side-query 流程
- [ ] 知道 extract 用 pre_compress 的原因
- [ ] 知道 consolidate 阈值与 Dream 简化
- [ ] 能对比 s07 skill 与 s09 memory 的同构设计
- [ ] 知道 memory 不参与 s08 compact 管线

---

## 调试技巧

| 现象 | 排查 |
|------|------|
| 从不 extract | 是否每轮都 tool_use 未结束？Stop hook force？ |
| `.memory/` 空 | extract LLM 返回 `[]`；看 dialogue 是否太短 |
| 索引不更新 | 手建文件未 rebuild；`write_memory_file` 才会 rebuild |
| 跨 session 不记得 | 文件是否在 `.memory/`？新 session 是否新 WORKDIR？ |
| 偏好未遵守 | inject 未选中；side-query 失败且 keyword 未命中 |
| consolidate 不触发 | 文件数 < `CONSOLIDATE_THRESHOLD` |

---

## 和前后章节的关系

| 章节 | 关系 |
|------|------|
| s08 Compact | 丢细节；memory 是 **不参与压缩** 的长期层 |
| s10 System Prompt | `build_system()` 将演进为分段组装 |
| s07 Skill | 同构「索引 + 按需加载」 |

s09 练习的核心收获：**压缩管会话上下文，文件管跨会话知识；索引常挂、全文按需、轮末提取、定期整理。**

---

## 优先推荐（时间有限）

1. **观察表 #1–3** — 10 分钟建立 memory 直觉
2. **题目 4** — pre_compress 为何必要
3. **场景 A** — README 四步验收
4. **场景 B** — 跨 session
5. **题目 11** — 与 s07 skill 对照
