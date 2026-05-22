# s02 工具练习题目

在 `code.py` 里已有 5 个基础工具（`bash`、`read_file`、`write_file`、`edit_file`、`glob`）的基础上，自己动手加新工具。

每道题的标准步骤：

1. 写 `run_xxx(...)` 函数，返回 **字符串**（成功信息或 `Error: ...`）
2. 涉及文件路径的操作走 `safe_path`
3. 在 `TOOLS` 里加 `name / description / input_schema`
4. 在 `TOOL_HANDLERS` 里注册
5. 用固定 prompt 手动测试，观察模型会不会优先用你的工具而不是 `bash`

---

## 一、文件类（最推荐）

### 题目 1：`grep` / `search_text` ⭐

**目标**：在指定目录下搜索文本，比 bash `grep` 更稳定、更省 token。

```python
def run_grep(pattern: str, path: str = ".", glob_pattern: str = "**/*") -> str:
    # 返回 "file:line: content" 格式，最多 N 条
```

**测试 prompt**：

> 搜索项目里所有包含 `TOOL_HANDLERS` 的文件，告诉我分别在第几行

**你会练到**：`safe_path`、递归遍历、结果截断（别一次返回 10 万行）

---

### 题目 2：`list_dir` ⭐

**目标**：列出目录内容（文件/文件夹、大小、是否目录）。

```python
def run_list_dir(path: str = ".", recursive: bool = False) -> str:
```

**测试 prompt**：

> 列出 s02_tool_use 目录下所有文件，并告诉我 code.py 有多大

**你会练到**：`Path.iterdir()`、`is_dir()`、格式化输出

**常见坑**：递归合并子目录结果时，子函数若返回 `str`，要用 `results.extend(sub.splitlines())`，**不要** `results.extend(sub)`——后者会把字符串按字符拆开。

---

### 题目 3：`file_info` ⭐

**目标**：查看单个文件的元信息，不用读全文。

```python
def run_file_info(path: str) -> str:
    # 返回：大小、修改时间、是否目录、行数（文本文件）
```

**测试 prompt**：

> 告诉我 README.md 有多少行、多大、最后修改时间

**你会练到**：`stat()`、按需读取（比 `read_file` 更轻量）

---

### 题目 4：`delete_file` ⭐⭐

**目标**：安全删除文件（不能删目录、不能逃出工作区）。

```python
def run_delete_file(path: str) -> str:
    # 只允许删文件，不允许删目录
    # 可选：禁止删 .env、.git 等敏感路径
```

**测试 prompt**：

> 创建一个 temp.txt，读一下，然后删掉它

**你会练到**：权限/安全边界（和 s03 的铺垫）

---

### 题目 5：`append_file` ⭐

**目标**：往文件末尾追加内容，不用每次 `write_file` 覆盖全文。

```python
def run_append_file(path: str, content: str) -> str:
```

**测试 prompt**：

> 创建 log.txt 写入第一行，再追加两行日志

---

## 二、增强现有工具

### 题目 6：给 `read_file` 加 `offset` + `limit` ⭐

**目标**：支持从第 N 行开始读，像 Cursor 的 Read 工具一样。

```python
def run_read(path: str, limit: int | None = None, offset: int = 1) -> str:
    # offset：起始行号，从 1 开始
    # limit：读多少行（不是「读到第 N 行」）
```

**测试 prompt**：

> 只读 s02_tool_use/code.py 第 140-170 行

**你会练到**：改 schema + 改函数签名，理解 `**block.input` 如何自动传参

**常见坑**：

- `read_text()` / `write_text()` 在 Windows 上要加 `encoding="utf-8"`
- 用户说的「第 140 行」是 1-based，代码里切片要用 `offset - 1`

---

### 题目 7：`edit_file` 支持 `replace_all` ⭐⭐

**目标**：一次替换所有匹配，或明确只替换一次。

```python
def run_edit(path: str, old_text: str, new_text: str, replace_all: bool = False) -> str:
```

**测试 prompt**：

> 把 code.py 里所有 `print(` 改成 `# print(`（先只改一处，再全部改）

---

## 三、代码 / 项目理解类

### 题目 8：`count_lines` ⭐

**目标**：统计某类文件的总行数。

```python
def run_count_lines(glob_pattern: str = "**/*.py") -> str:
```

**测试 prompt**：

> 统计这个项目里所有 .py 文件一共多少行

---

### 题目 9：`run_python` ⭐⭐

**目标**：安全运行 Python 代码片段（比 bash 更可控）。

```python
def run_python(code: str) -> str:
    # 用 subprocess 跑 python -c，或写到临时文件再跑
    # 加 timeout、禁止 import os/subprocess 等（可选进阶）
```

**测试 prompt**：

> 写一段 Python 计算 1 到 100 的和，并运行它

**常见坑**：Windows 上 subprocess 要指定 `encoding="utf-8", errors="replace"`，否则中文输出可能乱码或报错。

---

### 题目 10：`git_status` ⭐⭐

**目标**：只看 git 状态，不用模型拼 bash 命令。

```python
def run_git_status() -> str:
    # subprocess: git status --short
```

**测试 prompt**：

> 告诉我当前仓库有哪些未提交的改动

---

## 四、稍难一点（做完上面再做）

### 题目 11：`apply_patch` ⭐⭐⭐

**目标**：接收 unified diff 或简单的「搜索替换块」，一次改多处。

```python
def run_apply_patch(path: str, patch: str) -> str:
```

**测试 prompt**：

> 给 run_bash 函数加 docstring，用 patch 方式修改

**你会练到**：比 `edit_file` 更接近真实 Agent 的改代码方式

---

### 题目 12：`web_fetch` ⭐⭐⭐

**目标**：抓取 URL 正文（需网络）。

```python
def run_web_fetch(url: str) -> str:
    # 用 urllib 或 requests，限制返回长度
    # 只允许 http:// 和 https://，禁止 file://
```

**测试 prompt**：

> 读取 https://docs.python.org/3/library/pathlib.html 的开头，总结 Path 能做什么

**常见坑**：URL 校验应写成 `url.startswith("http://") or url.startswith("https://")`，不要写成 `not startswith("http") or not startswith("https")`——后者会把 `http://` 误判为非法。

---

### 题目 13：`json_read` ⭐⭐

**目标**：读 JSON 并格式化输出某个 key。

```python
def run_json_read(path: str, key: str | None = None) -> str:
```

**测试 prompt**：

> 如果项目里有 package.json / pyproject.toml，读取并解释依赖

---

## 推荐学习路线（3 天）

| 天 | 做哪几题 | 目标 |
|----|---------|------|
| Day 1 | 2 → 3 → 6 | 熟悉 schema + handler + safe_path |
| Day 2 | 1 → 5 → 8 | 遍历、搜索、结果截断 |
| Day 3 | 4 → 9 或 10 | 安全边界 + subprocess |

---

## 验收标准（自己打分）

- [ ] 模型能**选对工具**（例如「搜索」用 grep 而不是 bash）
- [ ] 非法路径被拦住（`../../etc/passwd`）
- [ ] 输出不会爆 context（大结果要截断，如 `[:50000]`）
- [ ] 错误是可读的字符串，不会让 agent loop 崩溃
- [ ] 文件读写统一 `encoding="utf-8"`（Windows 必查）

---

## 调试技巧

agent_loop 里有两路终端输出，别搞混：

| 输出 | 来源 | 含义 |
|------|------|------|
| `> read_file` | `agent_loop` 打印工具名 | debug，不是最终答案 |
| `print(output[:200])` | 工具返回值预览 | 只显示前 200 字符 |
| 模型格式化后的回复 | `print(block.text)` | **这才是给用户的答案** |

日常使用时可以去掉 handler 里的 `print("使用了...")` 和 `print(output[:200])`，终端会更干净。

---

## 和后续章节的关系

这些题在 s02 做都很合适。后面章节会专门讲：

| 后续章节 | 会正式实现的工具/机制 |
|---------|---------------------|
| s03 | 权限门控（approve/deny） |
| s05 | `todo_write` |
| s06 | `task`（子 agent） |
| s07 | `load_skill` |

建议 **`todo_write`、`task`、`load_skill` 留到对应章节**；在 s02 先把「文件 + 搜索 + 元信息 + 安全删除」练熟。

---

## 参考：加一个工具的模板

```python
# 1. 实现函数
def run_xxx(arg1: str, arg2: int = 10) -> str:
    try:
        # ... 逻辑 ...
        return "成功信息"
    except Exception as e:
        return f"Error: {e}"

# 2. 注册到 TOOLS
{"name": "xxx", "description": "...",
 "input_schema": {"type": "object",
                  "properties": {"arg1": {"type": "string"}, "arg2": {"type": "integer"}},
                  "required": ["arg1"]}},

# 3. 注册到 TOOL_HANDLERS
"xxx": run_xxx,
```

循环本身不用改——这就是 s02 的核心思想：**加工具 = 加函数 + 加 schema + 加映射**。
