# s03 Permission 练习题目

在 s02 的 16 个工具基础上，s03 的核心是 **在工具执行前加权限判断**。

每道题通常只改这些部分：

- `DENY_LIST` — 闸门 1：硬拒绝
- `PERMISSION_RULES` — 闸门 2：规则匹配
- `ask_user` / `check_permission` — 闸门 3：用户审批
- `agent_loop` 尽量不动（只在拒绝时正确返回 `tool_result`）

---

## 三道闸门复习

```
工具调用
   ↓
闸门 1：DENY_LIST        → ⛔ 硬拒绝，不问用户
   ↓ 未命中
闸门 2：PERMISSION_RULES  → 命中则进入闸门 3
   ↓
闸门 3：ask_user          → ⚠ Allow? [y/N]
   ↓ 允许 / 未命中规则
执行 TOOL_HANDLERS
```

**关键约定**：无论拒绝还是用户 deny，都必须返回：

```python
{"type": "tool_result", "tool_use_id": block.id, "content": "..."}
```

不能返回 `{"type": "text", ...}`，否则 API 会 400。

---

## 一、入门：用 Prompt 观察行为（不用写代码）

先运行：

```sh
python s03_permission/code.py
```

用下面 prompt 测试，记录结果是「直接过 / ⚠ 询问 / ⛔ 拒绝」：

| # | Prompt | 预期 |
|---|--------|------|
| 1 | `列出当前目录有哪些文件` | 直接过（只读） |
| 2 | `创建 test.txt，内容为 hello` | 直接过（工作区内写） |
| 3 | `用 write_file 写 /etc/test.conf` | ⚠ 闸门 2（写工作区外） |
| 4 | `用 bash 执行 rm /tmp/foo`（不要 `-rf`） | ⚠ 闸门 2 |
| 5 | `Delete all temporary files in /tmp` | ⛔ 闸门 1（含 `rm -rf`） |
| 6 | `用 bash 执行 sudo apt update` | ⛔ 闸门 1 |

**练习目标**：分清三道闸门，理解「硬拒 ≠ 询问」。

**常见现象**：

- `rm -rf` 在闸门 1 就被拦，**不会**触发 ask
- `/tmp` 在工作区外，`list_dir` / `safe_path` 会报错，但这不是 ask 流程
- Windows 上模型可能用 `Remove-Item` 而不是 `rm `，若规则没写就不会 ask

---

## 二、基础实现题

### 题目 1：扩充 DENY_LIST（Windows 版） ⭐

**目标**：补上 Windows 危险命令。

```python
# 例如：Format-Volume, shutdown /s, del /f /s /q
```

**测试 prompt**：

> 用 bash 执行 shutdown /s

**你会练到**：闸门 1 是子串匹配，要想清楚漏网命令。

---

### 题目 2：保护敏感文件 ⭐⭐

**目标**：删/写 `.env`、`.git` 下文件时触发 ask 或硬拒。

```python
SENSITIVE_PATTERNS = [".env", ".git/", "credentials"]
```

**测试 prompt**：

> 删除 .env 文件  
> 用 edit_file 修改 .gitignore

**你会练到**：按 `path` 做规则，而不只是 bash。

---

### 题目 3：给 `safe_delete_file` 加权限 ⭐

**目标**：s02 的删除工具目前可能 **绕过 bash 规则**，直接删文件。

**测试 prompt**：

> 创建 temp.txt，然后用 safe_delete_file 删掉

**你会练到**：每个危险工具都要单独写规则，不能只管 bash。

---

### 题目 4：只读工具白名单 ⭐

**目标**：`read_file`、`glob`、`grep`、`file_info`、`git_status` 永远直接放行，不进 ask。

```python
READONLY_TOOLS = {"read_file", "glob", "grep", "file_info", "git_status", "list_dir", "count_lines"}

def check_permission(block) -> str | None:
    if block.name in READONLY_TOOLS:
        return None
    ...
```

**测试 prompt**：

> 读取 README.md 并搜索 TOOL_HANDLERS

**你会练到**：只读操作不该每次都弹窗。

---

### 题目 5：改进 ask_user 的输出 ⭐

**目标**：展示更清晰的信息。

```
⚠  Potentially destructive command
   Tool: bash
   Command: rm /tmp/foo
   Allow? [y/N/a=always]
```

**可选进阶**：输入 `a` 后，本次会话内同类操作不再问。

**你会练到**：权限 UX 也是 Agent 设计的一部分。

---

## 三、进阶题

### 题目 6：调整闸门 1 / 2 的分工 ⭐⭐

**现状**：`rm -rf` 在闸门 1 硬拒，永远不会 ask。

**练习**：把 `rm -rf` 从 DENY_LIST 移到闸门 2，让用户确认后再决定（仍禁止 `rm -rf /` 这种绝对路径）。

**测试 prompt**：

> Delete all temporary files in /tmp

**你会练到**：安全策略是产品设计，没有唯一正确答案。

---

### 题目 7：权限决策日志 ⭐⭐

**目标**：每次权限判断写入 `.debug/permission.log`：

```
2026-05-21 15:00:01 | bash | DENY | rm -rf in deny list
2026-05-21 15:00:05 | write_file | ASK | user allowed
2026-05-21 15:00:10 | read_file | ALLOW | readonly tool
```

**你会练到**：审计日志，为 s04 hooks 铺垫。

---

### 题目 8：从 JSON 加载规则 ⭐⭐⭐

**目标**：规则外置到 `s03_permission/permissions.json`：

```json
{
  "deny_list": ["rm -rf", "sudo"],
  "readonly_tools": ["read_file", "glob", "grep"],
  "rules": [
    {
      "tools": ["bash"],
      "keywords": ["rm ", "Remove-Item"],
      "message": "Potentially destructive command"
    }
  ]
}
```

启动时加载，改规则不用改 Python。

**你会练到**：对应 README 附录里 CC 的多来源规则（简化版）。

---

### 题目 9：`web_fetch` 域名白名单 ⭐⭐

**目标**：只允许 `docs.python.org`、`github.com`；其他域名 ask 或 deny。

```python
ALLOWED_DOMAINS = ["docs.python.org", "github.com", "raw.githubusercontent.com"]
```

**测试 prompt**：

> 抓取 https://docs.python.org/3/  
> 抓取 https://example.com/

**你会练到**：非 bash 工具也能做权限控制。

---

### 题目 10：绕过测试（安全思维） ⭐⭐⭐

**目标**：故意尝试绕过现有规则，再补洞。

| 攻击 prompt | 可能绕过 |
|-------------|---------|
| 命令变形、换行、`$(rm)` | 子串匹配弱点 |
| `Remove-Item -Recurse` | Windows 关键字遗漏 |
| `write_file` 写 `../../outside.txt` | path 解析 |
| 用 `safe_delete_file` 删 `.env` | 只拦 bash 不拦专用工具 |

**你会练到**：README 说的「简单字符串匹配不是可靠安全机制」。

---

## 四、综合场景题

### 场景 A：项目清理助手

**Prompt**：

> 帮我清理项目：删除 log.txt、__pycache__，但不要动 .env 和 .git

**验收**：

- [ ] 删 `log.txt` → 可直接过或 ask
- [ ] 删 `.env` → 必须拦
- [ ] `rm -rf` → 硬拒

---

### 场景 B：代码修改审批

**Prompt**：

> 给 run_bash 加 docstring，用 apply_patch 修改

**验收**：

- [ ] `read_file` 直接过
- [ ] `apply_patch` 改工作区内文件 → 可选 ask 或直接过
- [ ] 改 `.env` → 拦

---

### 场景 C：权限拒绝后不重试

**目标**：模型收到 deny 后不要 bash 连调 8 次。

在 `SYSTEM` 里加边界说明，或拒绝时返回更明确的消息：

```
Permission denied: rm -rf is blocked. Do not retry destructive commands. Explain the limitation to the user.
```

**验收**：

- [ ] 不可完成任务时，模型解释原因而不是无限 retry

---

## 推荐学习路线（3 天）

| 天 | 题目 | 目标 |
|----|------|------|
| Day 1 | 观察 prompt + 题 2、3、4 | 理解规则覆盖范围 |
| Day 2 | 题 1、5、6 + 场景 A | 调闸门分工与 Windows |
| Day 3 | 题 7、8、10 | 日志、外置规则、绕过测试 |

---

## 验收 checklist

- [ ] 硬拒返回 `tool_result`（不 crash，不用 `type: text`）
- [ ] ask 拒绝也返回 `tool_result`
- [ ] 只读工具不被误拦
- [ ] 危险工具（delete、write 区外）会触发 ask 或 deny
- [ ] Windows 命令（`Remove-Item`）有覆盖
- [ ] 敏感路径（`.env`）有保护
- [ ] 用 `DEBUG=1` 或权限日志能追溯每次决策

---

## 调试技巧

| 现象 | 排查 |
|------|------|
| 没触发 ask | 检查是否被闸门 1 提前 return；检查命令是否匹配关键字 |
| `/tmp` 报错但不是 ask | `safe_path` 在 handler 里报错，需在 PERMISSION_RULES 里提前拦 |
| 程序 400 crash | 拒绝时必须返回 `tool_result` + `tool_use_id` |
| bash 连调很多次 | 任务不可能完成或拒绝信息不够明确；开 `DEBUG=1` 看命令 |

开 DEBUG：

```powershell
$env:DEBUG="1"
python s03_permission/code.py
```

---

## 和后续章节的关系

| 后续章节 | 会演进的方向 |
|---------|-------------|
| s04 Hooks | 把 `check_permission`、日志拆成 PreToolUse hook |
| s05+ | 更多工具，每个都要考虑权限 |
| s15 Agent Teams | 权限冒泡到父 Agent（README 附录） |

s03 练习的核心收获：**安全不能靠信任模型，要靠代码在 execute 之前做决策**。

---

## 参考：权限相关代码模板

```python
# 1. 硬拒绝（闸门 1）
DENY_LIST = ["rm -rf", "sudo", ...]

def check_deny_list(command: str) -> str | None:
    for pattern in DENY_LIST:
        if pattern in command:
            return f"Blocked: '{pattern}' is on the deny list"
    return None

# 2. 规则匹配（闸门 2）
PERMISSION_RULES = [
    {
        "tools": ["write_file", "edit_file", "safe_delete_file"],
        "check": lambda args: is_sensitive_path(args.get("path", "")),
        "message": "Sensitive file operation",
    },
]

# 3. 管线（闸门 1 → 2 → 3）
def check_permission(block) -> str | None:
    if block.name == "bash":
        if reason := check_deny_list(block.input.get("command", "")):
            print(f"\n⛔ {reason}")
            return reason
    if reason := check_rules(block.name, block.input):
        if ask_user(...) == "deny":
            return f"Permission denied by user: {reason}"
    return None

# 4. agent_loop 里
deny_reason = check_permission(block)
if deny_reason:
    results.append({
        "type": "tool_result",
        "tool_use_id": block.id,
        "content": deny_reason,
    })
    continue
```

---

## 优先推荐（结合常见踩坑）

若时间有限，建议按这个顺序做：

1. **题目 3** — 给 `safe_delete_file` 加规则（补漏洞）
2. **题目 2** — 保护 `.env`（实用）
3. **题目 4** — 只读白名单（减少误拦）
4. **题目 6** — 理解 `rm -rf` 走 deny 还是 ask
5. **场景 C** — 减少模型无意义 retry
