# s04 Hooks 练习题目

s04 在 s03 的 **16 个工具 + 完整权限** 基础上，核心变化是：**扩展逻辑挂到 hook 上，不往 `agent_loop` 里堆代码**。

每道题通常只改这些部分：

- `register_hook()` — 注册新 hook
- `HOOKS` / `trigger_hooks()` — 理解事件与返回值语义
- 各类 `*_hook` 回调函数 — 具体扩展逻辑
- **`agent_loop` 尽量不动**（这是 s04 的设计目标）

---

## 四个事件复习

```
用户输入 query
   ↓
UserPromptSubmit  →  进入 LLM 前（日志、校验、注入上下文）
   ↓
LLM 返回 tool_use
   ↓
PreToolUse        →  工具执行前（权限、审计；非 None = 阻止本次工具）
   ↓
TOOL_HANDLERS     →  真正执行
   ↓
PostToolUse       →  工具执行后（副作用、输出检查）
   ↓
（循环继续，直到 stop_reason != tool_use）
   ↓
Stop              →  即将退出前（统计、收尾；非 None = 强制续跑）
```

**和 s03 的关键区别**：

| s03 | s04 |
|-----|-----|
| `check_permission(block)` 写在 loop 里 | `permission_hook` 挂在 PreToolUse |
| 加日志要改 loop | `register_hook("PostToolUse", ...)` 即可 |
| 退出时无扩展点 | `Stop` hook 可统计或强制续跑 |

**PreToolUse 返回值约定**（教学版）：

```python
return None          # 放行，继续下一个 hook / 执行工具
return "reason..."   # 阻止工具，作为 tool_result 内容返回给模型
```

---

## 一、入门：用 Prompt 观察 hook 行为（不用写代码）

先运行：

```sh
python s04_hooks/code.py
```

用下面 prompt 测试，记录终端里出现了哪些 `[HOOK]` 行、权限是 hook 拦的还是直接执行：

| # | Prompt | 观察重点 |
|---|--------|---------|
| 1 | `列出当前目录有哪些文件` | PreToolUse 的 `[HOOK] list_dir(...)`；只读应直接过 |
| 2 | `读取 README.md 的前 20 行` | PreToolUse 日志；无 PostToolUse 大输出警告 |
| 3 | `创建 test.txt，内容为 hello` | PreToolUse + 执行；Stop 时 `[HOOK] Stop: session used N tool calls` |
| 4 | `用 edit_file 修改 .gitignore，加一行 # test` | 软敏感 → ⚠ 询问（permission_hook） |
| 5 | `删除 .env 文件` | 硬敏感 → ⛔ Blocked（permission_hook 返回，工具不执行） |
| 6 | `Delete all temporary files in /tmp` | bash 危险命令 → deny 或 ask |

**练习目标**：

- [ ] 分清 **UserPromptSubmit / PreToolUse / PostToolUse / Stop** 四条日志
- [ ] 确认权限拒绝时 **loop 里没有写死 check_permission**，而是 hook 返回阻止
- [ ] 对比 s03：行为应一致，但代码组织方式不同

**思考题**：

1. `permission_hook` 和 `log_hook` 都挂在 PreToolUse，谁先执行？拒绝时 `log_hook` 还会跑吗？
2. 工具被 hook 阻止后，PostToolUse 会触发吗？（答案：不会，handler 没跑）

---

## 二、基础实现题

### 题目 1：把 s03 的 `log_tool_call` 挂到 PostToolUse ⭐

**背景**：s03 在 `agent_loop` 里每次工具执行后调用 `log_tool_call`；s04 文件里有这个函数，但还没注册成 hook。

**目标**：新增 hook，**不改 agent_loop**：

```python
def tool_log_hook(block, output):
    log_tool_call(block.name, block.input, output)
    return None

register_hook("PostToolUse", tool_log_hook)
```

**测试**：

```powershell
$env:DEBUG="1"
python s04_hooks/code.py
```

> 读取 s04_hooks/code.py 的前 50 行

**验收**：

- [ ] 终端出现 `in:` / `out:` 调试输出
- [ ] `apply_patch` 时 `.debug/last_apply_patch.txt` 有内容
- [ ] Error 输出为红色

**你会练到**：PostToolUse 是「执行后观测」的标准扩展点。

---

### 题目 2：PreToolUse 只记录 bash 调用 ⭐

**目标**：写 `bash_audit_hook`，仅当 `block.name == "bash"` 时，追加一行到 `.debug/bash.log`：

```
2026-05-21 15:00:01 | bash | git status
```

**提示**：

```python
from datetime import datetime

def bash_audit_hook(block):
    if block.name != "bash":
        return None
    DEBUG_DIR.mkdir(exist_ok=True)
    cmd = block.input.get("command", "")
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} | bash | {cmd}\n"
    with open(DEBUG_DIR / "bash.log", "a", encoding="utf-8") as f:
        f.write(line)
    return None
```

**测试 prompt**：

> 用 bash 执行 git status --short

**你会练到**：PreToolUse 也可以只做副作用（return None），不必拦截。

---

### 题目 3：理解 hook 执行顺序 ⭐⭐

**现状**（文件底部）：

```python
register_hook("PreToolUse", permission_hook)
register_hook("PreToolUse", log_hook)
```

**练习 A**：交换注册顺序，观察拒绝 `.env` 时 `[HOOK] write_file(...)` 是否仍打印。

**练习 B**：把 `log_hook` 改成 PreToolUse **也**在拒绝时记录（即：即使后面 permission_hook 会拦，log 也要留下）。

**验收**：

- [ ] 能解释 `trigger_hooks` 的 **顺序执行 + 第一个非 None 就 return** 语义
- [ ] 若希望「拒绝前也记日志」，log_hook 必须注册在 permission_hook **之前**

**你会练到**：hook 顺序是行为的一部分，不是实现细节。

---

### 题目 4：UserPromptSubmit 拒绝空输入 ⭐

**目标**：增强 `context_inject_hook`（或新建 hook），空字符串 / 纯空格时打印提示并 **不阻止**（教学版 main 仍会 append 空 query——可选进阶：改 `__main__` 让 hook 返回值决定是否 append）。

**最小版**（只打印，不改 main）：

```python
def context_inject_hook(query: str):
    if not query.strip():
        print("\033[33m[HOOK] Empty query ignored\033[0m")
    else:
        print(f"\033[90m[HOOK] UserPromptSubmit: working in {WORKDIR}\033[0m")
    return None
```

**进阶版**：hook 返回 `"Please enter a non-empty question."` 时，`__main__` 不 append、不调用 `agent_loop`。

**你会练到**：UserPromptSubmit 是「进 LLM 前」的扩展点；教学版默认不消费返回值，进阶需配合 main。

---

### 题目 5：Stop hook 统计每种工具调用次数 ⭐⭐

**目标**：增强 `summary_hook`，输出：

```
[HOOK] Stop: bash=2, read_file=3, write_file=1 (total 6)
```

**提示**：遍历 `messages`，找 `role=="assistant"` 且 content 里 `type=="tool_use"` 的 block（不是 tool_result）。

```python
from collections import Counter

def summary_hook(messages: list):
    counts = Counter()
    for m in messages:
        content = m.get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if getattr(b, "type", None) == "tool_use":
                counts[b.name] += 1
    ...
```

**测试 prompt**：

> 列出目录，读 README.md，再创建一个 hook_test.txt

**你会练到**：Stop 适合做会话级统计，不必改 loop。

---

### 题目 6：`permission_hook` 与 `check_permission` 去重 ⭐

**背景**：s04 文件里可能有完整的 `check_deny_list` / `is_hard_sensitive` 等函数，但 `permission_hook` 又写了一遍相同逻辑。

**目标**：让 `permission_hook` 只做一件事：

```python
def permission_hook(block):
    """PreToolUse: s03 permission pipeline."""
    return check_permission(block)
```

若 `check_permission` 已被删掉，从 s03 拷回 **函数体不动**，只通过 hook 调用。

**验收**：

- [ ] 权限行为与 s03 完全一致
- [ ] `agent_loop` 仍只有 `trigger_hooks("PreToolUse", block)`，不出现 `check_permission`

**你会练到**：hook 是壳，业务逻辑可以复用 s03 函数——「挂上去」而不是「重写一遍」。

---

## 三、进阶题

### 题目 7：PostToolUse 自动 stage 新文件 ⭐⭐

**目标**：写文件类工具成功后，自动 `git add` 对应路径（仅当文件在工作区内且 git 仓库存在）。

```python
def auto_git_add_hook(block, output):
    if block.name not in ("write_file", "edit_file", "append_file", "apply_patch"):
        return None
    if str(output).startswith("Error"):
        return None
    path = block.input.get("path", "")
    subprocess.run(["git", "add", path], cwd=WORKDIR, capture_output=True)
    print(f"\033[90m[HOOK] git add {path}\033[0m")
    return None
```

**测试 prompt**：

> 创建 auto_stage.txt，内容为 staged by hook

然后 `git status --short` 看是否已 stage。

**你会练到**：PostToolUse 典型用途——**副作用**（git add、通知、索引更新）。

---

### 题目 8：PreToolUse 耗时统计 ⭐⭐

**目标**：记录每个工具从 Pre 到 Post 的耗时。

**思路**：PreToolUse 用 `dict` 或 `time.time()` 存 `block.id → start`；PostToolUse 算差值写入 `.debug/timing.log`。

**验收**：

```
read_file | 0.03s
bash | 1.24s
```

**你会练到**：跨 hook 共享状态（模块级 dict）；Pre/Post 成对设计。

---

### 题目 9：Stop hook 强制「至少用一次工具」 ⭐⭐⭐

**目标**：若用户问的是明确任务，但模型第一轮就直接文字回答、没用任何工具，Stop hook 返回一条 user 消息 forcing 续跑：

```python
def min_tool_stop_hook(messages: list):
    # 若最后一条 assistant 没有 tool_use，且用户问题含「创建/删除/读取/修改」等动词
    # return "You must use tools to complete this task. Do not answer from memory."
    return None
```

在 `agent_loop` 里已有：

```python
if force := trigger_hooks("Stop", messages):
    messages.append({"role": "user", "content": force})
    continue
```

**测试 prompt**：

> 创建文件 must_use_tool.txt，内容为 ok

若模型偷懒直接说「好的我创建了」而没调工具，应被 Stop hook 打回。

**你会练到**：Stop 的非 None 返回值 = **强制续跑**；README 里 CC 的 `stopHookActive` 简化版。

---

### 题目 10：用环境变量开关 hook ⭐⭐

**目标**：`HOOKS=0` 时跳过 **非 permission** 的 hook（日志、git add 等），但 **permission_hook 始终启用**。

**思路**：

```python
HOOKS_ENABLED = os.getenv("HOOKS", "1").lower() not in ("0", "false", "no")

def conditional_register(event, callback, essential=False):
    if essential or HOOKS_ENABLED:
        register_hook(event, callback)
```

**验收**：

- [ ] `HOOKS=0` 时无 `[HOOK]` 日志，但 `.env` 仍被硬拒
- [ ] `HOOKS=1` 时行为与默认一致

**你会练到**：生产里常需要「调试 hook」和「安全 hook」分层。

---

### 题目 11：PostToolUse 截断超大输出 ⭐⭐

**背景**：`large_output_hook` 只 **打印警告**，不会改返回给模型的内容。

**目标**：新增 hook，当 `len(output) > 50000` 时，把 output **替换**为前 50000 字符 + `\n...(truncated)`，并 return 修改后的值——**注意**：当前教学版 `trigger_hooks("PostToolUse", ...)` **不消费返回值**，需小改 agent_loop：

```python
# 可选进阶：让 PostToolUse 也能返回 modified output
modified = trigger_hooks("PostToolUse", block, output)
if modified is not None:
    output = modified
```

**你会练到**：教学版 vs 真实 CC（`updatedMCPToolOutput`）的差距；何时必须动 loop。

---

### 题目 12：从 JSON 加载 hook 配置 ⭐⭐⭐

**目标**：`s04_hooks/hooks.json`：

```json
{
  "large_output_threshold": 100000,
  "bash_audit": true,
  "auto_git_add": false
}
```

启动时读取，按配置决定是否 `register_hook`。

**你会练到**：扩展点注册也可外置，对应 CC 的 `hooks.json` / settings 思路。

---

## 四、综合场景题

### 场景 A：可观测的 Agent

**需求**：不改 `agent_loop`，实现：

- 每次 PreToolUse 打一行 `[HOOK] tool(args)`
- 每次 PostToolUse 把 input/output 写入 `.debug/session.jsonl`（一行一条 JSON）
- Stop 时打印总工具次数

**Prompt**：

> 搜索 code.py 里的 register_hook，读相关代码，写一段总结到 hook_notes.md

**验收**：

- [ ] `.debug/session.jsonl` 可追溯每次调用
- [ ] Stop 统计数字与 jsonl 行数一致
- [ ] `agent_loop` diff 为空或仅题目 11 那种明确标注的改动

---

### 场景 B：安全 + 审计

**需求**：

- permission_hook 保持 s03 全部规则
- 另加 `permission_audit_hook`：每次 PreToolUse 若 permission 会拒绝，先写 `.debug/permission.log` 再拒绝
- 用户 allow 的 ask 也记入 log（ALLOW/DENY/ASK）

**Prompt**：

> 尝试 edit .env；尝试 edit .gitignore 并选 N

**验收**：

- [ ] log 中有 HARD_DENY / ASK_DENIED 等记录
- [ ] 拒绝仍返回正确 `tool_result`

---

### 场景 C：写代码任务的「收尾 hook」

**Prompt**：

> 给 permission_hook 的 docstring 补一行说明它是 PreToolUse hook

**需求**：

- PostToolUse：若改了 `s04_hooks/code.py`，提示「记得自测 python s04_hooks/code.py」
- Stop：若本会话改过 `.py` 文件，打印「建议运行 py_compile」

**你会练到**：hook 组合成小型 workflow，loop 仍保持干净。

---

## 五、Bug  hunt / 设计题

### 题目 13：PostToolUse 在拒绝时不触发 ⭐

**问题**：权限拒绝后，你的 `auto_git_add_hook` 会被调用吗？

**实验**：在 permission_hook 里拒绝 `write_file`，看 PostToolUse 是否执行。

**结论**：只有 `handler(**block.input)` 成功路径才会 PostToolUse——**拦截要在 PreToolUse 完成**。

---

### 题目 14：重复注册同一个 hook ⭐

**实验**：不小心 `register_hook("PreToolUse", log_hook)` 写了两次。

**问题**：日志会打印几遍？如何写 `register_hook` 去重？

---

### 题目 15：hook 返回值类型错误 ⭐⭐

**实验**：某 hook 返回了 `False` 而不是 `None`。

**问题**：`if result is not None` 会怎么表现？工具会被误拦吗？

**结论**：教学版只认 `None` 为放行；应统一返回 `None` 或 `str`。

---

### 题目 16：对比 s03 → s04 迁移清单 ⭐⭐

**任务**：填表（先手写，再对照代码）：

| 逻辑 | s03 位置 | s04 位置 |
|------|---------|---------|
| 权限检查 | `agent_loop` 内 | `permission_hook` |
| 工具日志 | `agent_loop` 内 `log_tool_call` | ？（题目 1） |
| 用户输入日志 | 无 | `context_inject_hook` |
| 退出统计 | 无 | `summary_hook` |
| 大输出警告 | 无 | `large_output_hook` |

**你会练到**：迁移 = 找扩展点，不是重写 agent。

---

## 推荐学习路线（3 天）

| 天 | 题目 | 目标 |
|----|------|------|
| Day 1 | 观察 prompt + 题 1、2、3 | 四个事件 + 注册顺序 |
| Day 2 | 题 5、6、7 + 场景 A | Stop/Post 扩展 + 去重 permission |
| Day 3 | 题 9、10、13 + 场景 B | 强制续跑 + 安全审计 + 边界理解 |

---

## 验收 checklist

- [ ] 新增功能主要通过 `register_hook` 完成，`agent_loop` 无大块新增逻辑
- [ ] PreToolUse 拒绝时返回字符串，且带正确 `tool_use_id`（loop 已写好，hook 只需 return reason）
- [ ] 能说出四个事件各自适合做什么
- [ ] 能解释 hook 注册顺序对 permission / log 的影响
- [ ] permission 行为与 s03 一致（硬拒 / 软 ask / 工作区外）
- [ ] Stop hook 能在退出前打印统计或 force 续跑
- [ ] 用 `DEBUG=1` + `.debug/` 文件能复盘一次会话

---

## 调试技巧

| 现象 | 排查 |
|------|------|
| 没有任何 `[HOOK]` 输出 | 检查是否注册；是否 `HOOKS=0` 关掉了 |
| 权限没生效 | 是否挂在 PreToolUse；是否 return 了 `None` |
| 拒绝后模型 400 | loop 侧问题：拒绝分支是否返回 `tool_result` |
| log 在拒绝时没出现 | log_hook 在 permission_hook 后面；调整顺序 |
| PostToolUse 没跑 | 工具在 Pre 阶段被拦，或 handler 抛错未捕获 |
| Stop 续跑死循环 | force 消息太模糊，模型反复空转；加次数上限（进阶） |

开 DEBUG：

```powershell
$env:DEBUG="1"
python s04_hooks/code.py
```

---

## 和后续章节的关系

| 后续章节 | 会演进的方向 |
|---------|-------------|
| s05 TodoWrite | 新工具 + 同样可挂 Pre/Post hook |
| s06+ | 更多生命周期事件（压缩、子 Agent…） |
| 真实 CC | 27 个事件、HookResult 14 字段、allow 不能绕过 deny |

s04 练习的核心收获：**循环是内核，扩展是插件；写 Agent 框架先找 hook 点，再写业务。**

---

## 参考：hook 相关代码模板

```python
# 1. 注册表
HOOKS = {"UserPromptSubmit": [], "PreToolUse": [], "PostToolUse": [], "Stop": []}

def register_hook(event: str, callback):
    HOOKS[event].append(callback)

def trigger_hooks(event: str, *args):
    for callback in HOOKS[event]:
        result = callback(*args)
        if result is not None:
            return result
    return None

# 2. PreToolUse：拦截
def permission_hook(block):
    return check_permission(block)  # str | None

# 3. PreToolUse：只观测
def audit_hook(block):
    ...  # 写日志
    return None

# 4. PostToolUse：执行后
def after_hook(block, output):
    log_tool_call(block.name, block.input, output)
    return None

# 5. Stop：退出前
def summary_hook(messages: list):
    ...
    return None  # 或 return "continue message" 强制续跑

# 6. 注册（顺序 matters）
register_hook("PreToolUse", audit_hook)       # 先
register_hook("PreToolUse", permission_hook)  # 后
register_hook("PostToolUse", after_hook)
register_hook("Stop", summary_hook)
```

---

## 优先推荐（时间有限时）

1. **题目 1** — 把 `log_tool_call` 挂到 PostToolUse（最贴近 s03→s04 迁移）
2. **题目 3** — 搞懂 hook 顺序（必考）
3. **题目 6** — permission 去重（避免两套逻辑漂移）
4. **题目 7** — PostToolUse 副作用（典型实战）
5. **题目 9** — Stop 强制续跑（理解 Stop 返回值）
