# s07 Skill Loading 练习题目

s07 在 **s06 全套（18 工具 + subagent + todo + hook + plan-only）** 基础上，核心新增是 **两级技能加载**：

- **Layer 1**：启动时扫描 `skills/`，目录注入 `SYSTEM`（便宜，每轮都带）
- **Layer 2**：`load_skill(name)` 经 `tool_result` 注入全文（贵，按需）

每道题通常只改这些部分：

- `_scan_skills()` / `_parse_frontmatter()` / `SKILL_REGISTRY`
- `build_system()` / `list_skills()`
- `load_skill()` — 查找与返回值
- `TOOLS` / `TOOL_HANDLERS` — 注册 `load_skill`
- `SYSTEM` — 何时提示模型先 load 再执行

---

## 核心机制复习

```
启动时 _scan_skills()
   ↓
SKILL_REGISTRY[name] = { name, description, content }
   ↓
build_system() → SYSTEM 含 "Skills available: ..."
   ↓
每轮 API 调用都带目录（Layer 1）
   ↓
模型调用 load_skill("code-review")
   ↓
tool_result 注入完整 SKILL.md（Layer 2）
   ↓
后续轮次 messages 里仍携带该 tool_result（直到 compact/结束）
```

**关键洞察**：

| 问题 | 答案 |
|------|------|
| 技能全文在 system prompt 里吗？ | **不在**，只在 `load_skill` 的 tool_result 里 |
| 模型怎么知道有哪些技能？ | **SYSTEM 目录**（name + description） |
| `load_skill` 读磁盘路径吗？ | **不**，只查 `SKILL_REGISTRY`（防 path traversal） |
| 子 Agent 能 `load_skill` 吗？ | **不能**，`SUB_TOOLS` 无此工具 |
| plan-only 能 `load_skill` 吗？ | **不能**，`plan_only_hook` 只允许 todo 工具 |
| 运行中新增 SKILL.md 会生效吗？ | **不会**，`_scan_skills()` 只在启动时跑一次 |

---

## 一、入门：用 Prompt 观察（不用写代码）

先运行：

```sh
python s07_skill_loading/code.py
```

| # | Prompt | 观察重点 |
|---|--------|---------|
| 1 | `What skills are available?` | 模型是否**不调** `load_skill` 也能列出目录（来自 SYSTEM） |
| 2 | `Load the code-review skill and tell me its first checklist section` | 是否出现 `[HOOK] load_skill`？回答是否像 SKILL.md 内容 |
| 3 | `I need to do a code review — load the relevant skill first` | 是否先 `load_skill` 再 read/review |
| 4 | `load_skill("not-a-real-skill")` | 返回是否 `Skill not found` 且列出 Available |
| 5 | 对比：改 CSS 颜色 vs 代码审查任务 | 前者可能从不 load；后者应 load `code-review` |

**练习目标**：分清 **目录（每轮 SYSTEM）** 与 **全文（一次 tool_result）** 的 token 代价。

---

## 二、基础实现题

### 题目 1：读懂 `_scan_skills` ⭐

**目标**：打开 `skills/code-review/SKILL.md`，对照 registry 里存了什么。

**验收**：

- [ ] `name` 来自 frontmatter 的 `name:`（不是目录名时必须一致）
- [ ] `description` 来自 frontmatter
- [ ] `content` 是**整个文件**（含 frontmatter + 正文）

**你会练到**：扫描发生在 **harness 启动**，不是模型 read_file。

---

### 题目 2：`load_skill` 为何不用路径 ⭐⭐

**现状**：

```python
def load_skill(name: str) -> str:
    skill = SKILL_REGISTRY.get(name)
```

**思考题**：若改成 `read_file(f"skills/{name}/SKILL.md")` 有什么风险？

**答案应包含**：路径遍历（`../.env`）、任意文件读取、与 registry 不一致。

**你会练到**：**注册表 lookup** 是安全边界。

---

### 题目 3：frontmatter 解析边界 ⭐

**测试**：在 `skills/` 下临时建 `broken-skill/SKILL.md`：

```markdown
# No frontmatter
Just a title skill
```

重启 agent，观察 `SKILL_REGISTRY` 里 name/description  fallback 行为（目录名 / 首行）。

**你会练到**：`_parse_frontmatter` 是**最小实现**，不是完整 YAML 解析器。

---

### 题目 4：增强 `build_system` 引导 ⭐

**目标**：在 SYSTEM 里补充，例如：

- 做 code review / 安全审计前 **必须** `load_skill("code-review")`
- 构建 MCP 服务器前 **必须** `load_skill("mcp-builder")`
- 不要对简单 read 任务 load 所有 skill

**测试 prompt**：

> Review s07_skill_loading/code.py for security issues

**你会练到**：Layer 1 让模型**知道有什么**；SYSTEM 规则决定**何时 load**。

---

### 题目 5：`load_skill` 返回格式 ⭐

**现状**：返回原始 `skill["content"]`（含 frontmatter）。

**练习 A**：改为只返回 body（去掉 frontmatter），观察模型是否仍能理解。  
**练习 B**：返回时加前缀：

```text
<skill name="code-review">
...content...
</skill>
```

**你会练到**：tool_result 格式影响模型后续行为（与 s05 todo JSON 类似）。

---

## 三、进阶题（与 s06 / hook 交叉）

### 题目 6：子 Agent 为什么没有 `load_skill` ⭐⭐

**目标**：确认 `SUB_TOOL_NAMES` 与 `TOOLS` 差异；读 `SUB_SYSTEM` 无 skill 提示。

**思考题**：若子 Agent 也能 load_skill，上下文隔离会被怎样破坏？

**答案应包含**：子 Agent 应用父传入的 `description`；skill 全文应在**父** load 后写进 task 描述，或父自己执行。

**你会练到**：**知识注入点**要在架构上选一层（父 vs 子）。

---

### 题目 7：plan-only 与 `load_skill` ⭐⭐

**Prompt**：

> 先只列计划不要执行：load code-review skill 并规划审查 s07 的步骤

**期望**：

- [ ] `plan-only mode enabled`
- [ ] **不应** `load_skill`（被 `plan_only_hook` 拦）
- [ ] 只有 `todo_write` / `todo_read`

**你会练到**：plan-only 是父会话策略；load 算「执行前知识加载」，仍被拦。

---

### 题目 8：`load_skill` 与 `todo_inject_hook` ⭐⭐

**场景**：父 Agent 先 `todo_write`，再 `load_skill("code-review")`。

**问题**：`todo_inject_hook` 会不会把 `<current_todos>` 追加到 **load_skill 的 tool_result** 上？

**答案**：会（父 Agent、非 `todo_write`、非 `in_subagent`）。是否有必要对 `load_skill` 跳过 inject？

**可选实现**：

```python
if block.name in ("todo_write", "load_skill"):
    return None
```

**你会练到**：大段 skill 正文 + todo JSON 可能撑爆 context，hook 顺序要设计。

---

### 题目 9：`large_output_hook` 与 skill 全文 ⭐⭐

**背景**：单个 SKILL.md 可能 2000+ tokens；多个 skill 连续 load 更夸张。

**练习**：读 `skills/mcp-builder/SKILL.md` 大小；若超过 50k 字符，`large_output_hook` 会怎样？

**答案应包含**：截断后 `tool_log` / `todo_inject` 可能因短路跳过（s04/s05 题 14 同款）。

**你会练到**：Layer 2 **按需** 不等于 **无上限**；与 s08 compact 衔接。

---

### 题目 10：`load_skill` 会进 session.jsonl 吗 ⭐

**期望**：父 Agent 调用会；子 Agent 不会（`in_subagent` 跳过 `tool_log_hook`）。

**验收**：跑 prompt #2，打开 `.debug/session.jsonl` 找 `"name": "load_skill"`。

---

### 题目 11：启动后热加载 ⭐⭐⭐

**现状**：`_scan_skills()` 只在 import 时跑一次；运行中新建 `skills/foo/SKILL.md` **不生效**。

**目标**：实现 `reload_skills()` 工具或 UserPromptSubmit hook：

1. 重新 `_scan_skills()`
2. `global SYSTEM; SYSTEM = build_system()`（注意：已进行的 conversation 仍用旧 system 直到新 session）

**你会练到**：目录层是 **进程级缓存**；真实 CC 有多来源、多刷新策略。

---

### 题目 12：task + load_skill 协作 ⭐⭐⭐

**Prompt**：

> Load the code-review skill, then use a subtask to grep s07_skill_loading/code.py for "password" and summarize findings

**观察**：

- [ ] 父是否先 `load_skill`
- [ ] 子 Agent **不应**再 load（无工具）
- [ ] 父是否把审查要点写进 `task(description=...)`

**你会练到**：**知识在父 load，脏活在子 task** 是合理分工。

---

## 四、综合场景题

### 场景 A：按需 load（README 标准）

**Prompt**：

> I need to do a code review of s05_todo_write/code.py — load the relevant skill first, then review

**验收**：

- [ ] `load_skill("code-review")` 出现
- [ ] 审查维度接近 SKILL.md（Security / Correctness / …）
- [ ] todo 含 Verify（s05 仍生效）
- [ ] 未 load 无关 skill（如 pdf）

---

### 场景 B：只问目录，不 load

**Prompt**：

> What skills do you have and when should I use each?

**验收**：

- [ ] 可能零次 `load_skill`
- [ ] 回答基于 SYSTEM 目录即可
- [ ] token 消耗明显低于场景 A

---

### 场景 C：错误 skill 名 + 纠正

**Prompt**：

> Load skill "code_review" and review hello_world.py

**验收**：

- [ ] 第一次 `load_skill` 失败（registry 是 `code-review`）
- [ ] 模型改用正确 name 或向用户说明

---

### 场景 D：多 skill 任务

**Prompt**：

> I want to build an MCP server for PDF processing — load both mcp-builder and pdf skills, outline a plan

**验收**：

- [ ] 两次 `load_skill`
- [ ] messages 变长（两段全文都在 history）
- [ ] 能解释为何不应把两个 SKILL 全文都塞进 SYSTEM

---

## 五、设计思考题（不写代码也可）

### 题目 13：Layer 1 vs Layer 2 的 token 账

估算：4 个 skill 目录各 ~50 tokens vs 各 load 全文 ~2000 tokens。  
何时只 Layer 1 够用？何时必须 Layer 2？

---

### 题目 14：skill 内容 vs system prompt

对比：

| 方式 | 优点 | 缺点 |
|------|------|------|
| 全文进 SYSTEM | 模型总能看到 | 每轮都贵 |
| load_skill → tool_result | 按需 | 进 history 后仍占上下文 |
| s08 compact 后丢弃 | 省 token | 可能丢细节 |

写 3–5 句说明 s07 在完整链路中的位置。

---

### 题目 15：与真实 CC Skill 工具的差异

README「深入 CC 源码」提到：CC 的 Skill 工具返回 `"Launching skill: ..."`，正文经 `newMessages` 注入；教学版合并为 tool_result。

这对你理解 **tool_result 也是上下文的一部分** 有什么启发？

---

### 题目 16：该给子 Agent 加 `load_skill` 吗？

列出 **支持** 与 **反对** 各 2 条。结合 `in_subagent`、SUB_SYSTEM、场景 C 的「父 load 子执行」作答。

---

## 推荐学习路线（2 天）

| 天 | 题目 | 目标 |
|----|------|------|
| Day 1 | 观察表 + 题 1–5 | 两级加载 + registry |
| Day 2 | 题 6–10 + 场景 A/B | hook 交叉 + 观测 |
| 可选 | 题 11–12 + 场景 D | 热加载 + task 协作 |

---

## 验收 checklist

- [ ] 能解释 Layer 1（SYSTEM 目录）与 Layer 2（load_skill）区别
- [ ] 知道 `load_skill` 走 registry 不走路径
- [ ] 子 Agent 无 load_skill；plan-only 父 Agent 也不能 load
- [ ] `load_skill` 会记入 session.jsonl（父）
- [ ] 多步任务仍可有 todo_write / task / Verify（s05/s06 保留）
- [ ] 能说出「运行中新建 skill 不生效」的原因

---

## 调试技巧

| 现象 | 排查 |
|------|------|
| 从不 load_skill | SYSTEM 是否够强；任务是否太简单 |
| Skill not found | frontmatter `name` 与调用名是否一致（连字符） |
| 目录为空 | `skills/` 是否在 WORKDIR；是否重启进程 |
| load 后回答仍泛化 | tool_result 是否太短/被 large_output 截断 |
| 子 Agent 重复调研 | 父是否应先在 task description 里写 skill 要点 |
| plan-only 仍 load | `plan_only_hook` 是否拦 load_skill |

---

## 和后续章节的关系

| 章节 | 关系 |
|------|------|
| s06 Subagent | 父 load + 子 task 分工 |
| s08 Context Compact | load 进来的大 tool_result 需要压缩/丢弃 |
| s12 Task System | 更重的任务状态机，skill 仍是知识层 |

s07 练习的核心收获：**知识别堆 system prompt；目录常挂、全文按需；registry 保安全。**

---

## 优先推荐（时间有限）

1. **观察表 #1–3** — 5 分钟建立两级加载直觉
2. **题目 2** — registry vs read_file 安全
3. **题目 7** — plan-only 与 load 边界
4. **场景 A** — README 标准验收
5. **题目 14** — 为 s08 铺垫
