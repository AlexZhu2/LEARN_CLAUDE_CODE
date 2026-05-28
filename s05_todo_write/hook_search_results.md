# s05_todo_write: "hook" Search Results

## Matching Files and Lines

| File | Line(s) | Context |
|------|---------|---------|
| s05_todo_write/code.py | 3 | `s05: TodoWrite — s04 hooks + 16 tools + todo_write planning.` |
| s05_todo_write/code.py | 7 | `+ Full s04: permission hooks, session.jsonl, timing, 16 tools` |
| s05_todo_write/code.py | 85 | `#  FROM s03: Permission (check_permission used by permission_hook)` |
| s05_todo_write/code.py | 1094 | `#  NEW in s04: Hook System (s03 permission logic now via hooks)` |
| s05_todo_write/code.py | 1100 | `def register_hook(event: str, callback):` |
| s05_todo_write/code.py | 1104 | `def trigger_hooks(event: str, *args):` |
| s05_todo_write/code.py | 1112 | `def permission_hook(block):` |
| s05_todo_write/code.py | 1143 | `def log_hook(block):` |
| s05_todo_write/code.py | 1150 | `def large_output_hook(block, output):` |
| s05_todo_write/code.py | 1160 | `# UserPromptSubmit hook: log user input before it reaches the LLM` |
| s05_todo_write/code.py | 1161 | `def context_inject_hook(query: str):` |
| s05_todo_write/code.py | 1168 | `def plan_only_hook(block):` |
| s05_todo_write/code.py | 1179 | `# Stop hook: print summary when loop is about to exit` |
| s05_todo_write/code.py | 1180 | `def summary_hook(messages: list):` |
| s05_todo_write/code.py | 1222 | `def tool_log_hook(block, output):` |
| s05_todo_write/code.py | 1244 | `def bash_audit_hook(block):` |
| s05_todo_write/code.py | 1254 | `def pretool_use_time_hook(block):` |
| s05_todo_write/code.py | 1259 | `def posttool_use_time_hook(block, output):` |
| s05_todo_write/code.py | 1268 | `def todo_inject_hook(block, output):` |
| s05_todo_write/code.py | 1278 | `register_hook("UserPromptSubmit", context_inject_hook)` |
| s05_todo_write/code.py | 1279 | `register_hook("PreToolUse", log_hook)  # 可选：改到 permission 前` |
| s05_todo_write/code.py | 1280 | `register_hook("PreToolUse", plan_only_hook)` |
| s05_todo_write/code.py | 1281 | `register_hook("PreToolUse", permission_hook)` |
| s05_todo_write/code.py | 1282 | `register_hook("PreToolUse", bash_audit_hook)` |
| s05_todo_write/code.py | 1283 | `register_hook("PreToolUse", pretool_use_time_hook)` |
| s05_todo_write/code.py | 1284 | `register_hook("PostToolUse", large_output_hook)` |
| s05_todo_write/code.py | 1285 | `register_hook("PostToolUse", todo_inject_hook)` |
| s05_todo_write/code.py | 1286 | `register_hook("PostToolUse", tool_log_hook)` |
| s05_todo_write/code.py | 1287 | `register_hook("PostToolUse", posttool_use_time_hook)` |
| s05_todo_write/code.py | 1288 | `register_hook("Stop", summary_hook)` |
| s05_todo_write/code.py | 1292 | `#  agent_loop — s04 hooks + s05 nag reminder` |
| s05_todo_write/code.py | 1320 | `force = trigger_hooks("Stop", messages)` |
| s05_todo_write/code.py | 1332 | `blocked = trigger_hooks("PreToolUse", block)` |
| s05_todo_write/code.py | 1346 | `modified = trigger_hooks("PostToolUse", block, output)` |
| s05_todo_write/code.py | 1372 | `user_query = trigger_hooks("UserPromptSubmit", query)` |
| s05_todo_write/EXERCISES.md | 3 | `s05 在 **s04 全套 hook + 16 工具** 基础上，核心新增是 **`todo_write` 规划工具** 和 **nag reminder**。** |
| s05_todo_write/EXERCISES.md | 11 | `- s04 hook — 可选扩展（PostToolUse 注入 todo 等）` |
| s05_todo_write/EXERCISES.md | 164 | `## 三、进阶题（结合 s04 hook）` |
| s05_todo_write/EXERCISES.md | 170 | `**目标**：新增 hook，在 **每次非 todo_write 工具执行后**，把 `.tasks/current_todos.json` 内容追加到 tool_result（或单独注入 user 消息）：` |
| s05_todo_write/EXERCISES.md | 173 | `def todo_inject_hook(block, output):` |
| s05_todo_write/EXERCISES.md | 183 | `**注意**：需配合 `large_output_hook` 顺序；截断时可能跳过本 hook（s04 题目 13）。` |
| s05_todo_write/EXERCISES.md | 207 | `### 题目 9：Stop hook 检查未完成 todo ⭐⭐` |
| s05_todo_write/EXERCISES.md | 209 | `**目标**：`summary_hook` 增强 — 若 `current_todos.json` 里仍有 `pending` / `in_progress`，打印警告：` |
| s05_todo_write/EXERCISES.md | 217 | `**你会练到**：Stop hook 做 **规划完整性** 检查。` |
| s05_todo_write/EXERCISES.md | 241 | `**目标**：确认 `permission_hook` 不会误拦 `todo_write`（无 path 字段）。` |
| s05_todo_write/EXERCISES.md | 325 | `### 题目 14：todo_write 会被 hook 记进 session.jsonl 吗？` |
| s05_todo_write/EXERCISES.md | 327 | `**答案应包含**：PostToolUse 会记；PreToolUse 有 `[HOOK] todo_write(...)`；large_output 截断时后续 hook 行为。` |
| s05_todo_write/EXERCISES.md | 348 | `- [ ] s04 hook 仍正常（permission、session.jsonl）` |
| s05_todo_write/README.en.md | 27 | `The minimal hook structure from the previous chapter is preserved, focusing on the new `todo_write` tool and reminder mechanism. `todo_write` does no actual work, can't read files or run commands, it simply lets the Agent organize its thoughts before diving in.` |
| s05_todo_write/README.en.md | 152 | `- TaskCreated / TaskCompleted hooks (`TaskCreateTool.ts:80-129`, `TaskUpdateTool.ts:231-260`) for external system integration` |
| s05_todo_write/README.md | 27 | `保留上一章的最小 hook 结构，重点看新增的 `todo_write` 工具和 reminder 机制。`todo_write` 本身不做任何实际工作，不能读文件、不能跑命令，只是让 Agent 在动手之前先理清思路。` |
| s05_todo_write/README.md | 152 | `- TaskCreated / TaskCompleted hooks（`TaskCreateTool.ts:80-129`、`TaskUpdateTool.ts:231-260`）供外部系统集成` |
| s05_todo_write/images/todo-overview.en.svg | 53 | `<text x="400" y="110" fill="#166534" font-size="9" font-weight="600" text-anchor="middle">trigger_hooks</text>` |
| s05_todo_write/images/todo-overview.en.svg | 90 | `<text x="140" y="402" fill="#334155" font-size="10">s04 Preserved (loop, hooks, 5 base tools)</text>` |
| s05_todo_write/images/todo-overview.ja.svg | 53 | `<text x="400" y="110" fill="#166534" font-size="9" font-weight="600" text-anchor="middle">trigger_hooks</text>` |
| s05_todo_write/images/todo-overview.svg | 53 | `<text x="400" y="110" fill="#166534" font-size="9" font-weight="600" text-anchor="middle">trigger_hooks</text>` |

## README Files Found

Three README files exist:
- `s05_todo_write/README.md` (Chinese)
- `s05_todo_write/README.en.md` (English)
- `s05_todo_write/README.ja.md` (Japanese)

## README Typos / Issues

After reviewing all three README files, no obvious typos were found. All three documents are well-written and consistent. One minor observation: the `<!-- translation-sync: zh@v1, en@v1, ja@v1 -->` footer in `README.en.md` and `README.ja.md` shows `v1`, while `README.md` shows `zh@v1, en@v0, ja@v0`, suggesting the Chinese version may not have been updated after the English/Japanese translations were finalized. However, this is a metadata/sync marker, not a typo in the content itself.
