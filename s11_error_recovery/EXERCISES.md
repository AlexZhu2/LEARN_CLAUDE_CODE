# s11 Error Recovery 练习题目

s11 在 **s10 全套（prompt 组装 + memory + compact + skill + subagent + todo + hook）** 基础上，核心新增是 **LLM 调用的三类错误恢复**：

- **Path 1**：`stop_reason == "max_tokens"` → 升 8K→64K → continuation（最多 3 次）
- **Path 2**：`prompt_too_long` 异常 → `reactive_compact` 一次 → 再失败则退出
- **Path 3**：429 / 529 → `with_retry` 指数退避；连续 529 可切 `FALLBACK_MODEL_ID`

每道题通常只改这些部分：

- `RecoveryState` / `with_retry` / `retry_delay`
- `is_prompt_too_long_error` / `reactive_compact`
- `agent_loop` 里 try/except 与 `max_tokens` 分支顺序
- 常量：`ESCALATED_MAX_TOKENS`、`MAX_RETRIES`、`MAX_CONSECUTIVE_529` 等

---

## 核心机制复习

```
agent_loop 每圈
   ↓
s10 prompt 组装 + s08 压缩管线
   ↓
with_retry(LLM call)          ← Path 3：只吞 429/529
   ↓ 成功 / 其它异常 raise
外层 except                   ← Path 2：prompt_too_long → reactive compact
   ↓
if stop_reason == max_tokens  ← Path 1：升级 / continuation（不是异常）
   ↓
正常 append → tool_use / Stop / extract
```

**关键洞察**：

| 问题 | 答案 |
|------|------|
| `with_retry` 管什么？ | **仅** 429 / 529（临时故障） |
| 上下文过长谁管？ | 外层 `except` + `reactive_compact` |
| 输出截断谁管？ | `stop_reason == "max_tokens"`（API 元数据，不是模型写的） |
| 第一次升 token 为何不 append？ | 同一请求重试，避免把半截输出写进 history |
| 连续 529 切模型？ | `consecutive_529 >= 3` 且配置了 `FALLBACK_MODEL_ID` |
| 与 s08 auto compact 关系？ | auto 在 LLM **前**；reactive 在 API **报错后** |

---

## 一、入门：读代码观察（不用写代码）

先打开：

```sh
# 关键函数
# RecoveryState / with_retry / agent_loop 中 try/except / max_tokens 分支
python s11_error_recovery/code.py
```

| # | 观察点 | 你要确认 |
|---|--------|---------|
| 1 | `with_retry` 里 `raise` 在哪 | 非 429/529 会冒泡到外层 |
| 2 | 第一次 `max_tokens` 分支有没有 `messages.append` | **没有**，直接 `continue` |
| 3 | 第二次及以后 `max_tokens` | 先 append assistant，再 append `CONTINUATION_PROMPT` |
| 4 | `has_attempted_reactive_compact` | reactive 只允许一次 |
| 5 | `lambda mt=max_tokens, mdl=state.current_model` | 闭包捕获当前 max_tokens / 模型 |

**练习目标**：分清 **异常路径**（Path 2/3）与 **成功返回后的 stop_reason 路径**（Path 1）。

---

## 二、基础理解题

### 题目 1：三条路径填表 ⭐

| 路径 | 触发信号 | 处理位置 | 恢复动作 |
|------|---------|---------|---------|
| Path 1 输出截断 | ? | ? | ? |
| Path 2 上下文超限 | ? | ? | ? |
| Path 3 临时故障 | ? | ? | ? |

---

### 题目 2：`stop_reason == "max_tokens"` 是谁设的？⭐

**问题**：模型会在回复文本里写 `max_tokens` 吗？这个字段从哪来？

---

### 题目 3：第一次 escalate 为何不 append？⭐⭐

**场景**：`stop_reason == "max_tokens"` 且 `has_escalated == False`。

**问题**：

1. 代码做了什么？（改哪个变量？`continue` 后 messages 变了吗？）
2. 若此时把截断输出 append 进 messages，再升到 64K 重试，会有什么坏处？

---

### 题目 4：`with_retry` 会处理 `prompt_too_long` 吗？⭐⭐

读 `with_retry`：遇到非 429/529 时做什么？

**追问**：若把 `prompt_too_long` 也放进 `with_retry` 里盲目重试，会怎样？

---

### 题目 5：`retry_delay` 公式 ⭐

填空：

```
base = min(BASE_DELAY_MS * (2 ** attempt), ?) / 1000
delay = base + random.uniform(0, ?)
```

**追问**：为何要加 jitter（抖动）？

---

## 三、机制交叉题（s08 + s11）

### 题目 6：auto compact vs reactive compact ⭐⭐

填表：

| | auto compact（s08） | reactive compact（s11 Path 2） |
|--|-------------------|-------------------------------|
| 触发时机 | LLM **前** `estimate_size > CONTEXT_LIMIT` | ? |
| 触发条件 | 本地估算 | ? |
| 次数限制 | 每圈可跑 | ? |
| 合并版实现 | `compact_history`（LLM 摘要） | ? |

---

### 题目 7：Path 1 两阶段状态机 ⭐⭐⭐

画出 / 写出状态转移：

```
正常 (max_tokens=8K, has_escalated=False)
  --stop_reason=max_tokens--> ?
  --再 max_tokens--> ?
  --再 max_tokens（recovery_count 累加）--> ?
  --recovery_count 用尽--> ?
```

**验收**：能说出 escalate **只有一次**，continuation **最多 3 次**。

---

### 题目 8：529 切模型后，下一圈用哪个 model？⭐⭐

读 `with_retry` 与 `agent_loop` 里的 lambda。

**问题**：

1. `state.current_model` 何时被改成 `FALLBACK_MODEL`？
2. 切模型后，side-query（`select_relevant_memories` / `extract_memories`）用的是 `MODEL` 还是 `state.current_model`？这对吗？

---

### 题目 9：recovery 与 extract 的关系 ⭐⭐

**场景**：Path 1 continuation 进行中（已 append 半截 assistant + CONTINUATION_PROMPT）。

**问题**：此时会跑 `extract_memories` 吗？什么时候才会 extract？

---

### 题目 10：给 `with_retry` 加超时错误（小改代码）⭐⭐⭐

**目标**：把含 `"timeout"` / `"timed out"` 的异常也当作可重试（走退避），最多 `MAX_RETRIES` 次。

**你会练到**：扩展 Path 3 的判定，而不误伤 Path 2。

**验收**：

- [ ] timeout 会 sleep 重试
- [ ] `prompt_too_long` 仍 raise 到外层 compact
- [ ] 非临时错误仍立即 raise

---

## 四、与 s10 / CC 的设计题

### 题目 11：s10 vs s11 错误处理对比 ⭐

填表：

| | s10（合并版） | s11 |
|--|--------------|-----|
| 429/529 | ? | `with_retry` |
| prompt_too_long | 简单 reactive + raise | ? |
| max_tokens | ? | escalate + continuation |
| RecoveryState | 无 | ? |

---

### 题目 12：为何 Path 1 检查必须在 `messages.append` 之前？⭐⭐

对比错误写法：

```python
messages.append({"role": "assistant", "content": response.content})
if response.stop_reason == "max_tokens":
    ...
```

**问题**：第一次 escalate 时会出什么问题？

---

### 题目 13：教学版 vs CC 恢复能力 ⭐⭐

README「深入 CC」提到 CC 有 13+ reason code。

写 3 句：教学版覆盖了哪三类？刻意简化了什么？（连接错误、超时、认证缓存等任举 2 例）

---

### 题目 14：`CONTINUATION_PROMPT` 为何禁止道歉和复述？⭐

读常量文案。从「节省 token / 续写质量」各写 1 句。

---

## 五、场景验收

### 场景 A：正常路径（无错误）

**Prompt**：`List files in the current directory`

**验收**：

- [ ] 无 `[429]` / `[529]` / `[max_tokens]` / `[reactive compact]` 日志
- [ ] 正常 tool 循环结束
- [ ] s10 `[assembled]` / `[cache hit]` 仍可能出现

---

### 场景 B：理解 Path 1（读代码推演）

假设连续两次 `stop_reason == "max_tokens"`：

1. 第一次：打印什么？`messages` 长度变了吗？`max_tokens` 变成多少？
2. 第二次：打印什么？`messages` 末尾多了哪两类消息？

**验收**：能不看答案写出推演结果。

---

### 场景 C：Path 2 只一次

假设 API 连续两次抛 `prompt_too_long`：

1. 第一次：是否 `reactive_compact`？是否 `continue`？
2. 第二次：打印什么？是否 `return`？

---

## 六、进阶思考题

### 题目 15：该把 escalate 也放进 `with_retry` 吗？⭐⭐⭐

列出 **支持** / **反对** 各 2 条（相对当前「成功返回后看 stop_reason」设计）。

---

### 题目 16：reactive compact 后要不要 `_invalidate_prompt_cache()`？⭐⭐

**场景**：Path 2 compact 了 messages，`continue` 回到循环开头，会重新 `update_context` + `get_system_prompt`。

**问题**：有必要在 compact 后立刻 invalidate 吗？为什么？

---

### 题目 17：指数退避上限为何是 32s？⭐

`min(..., 32000)` 毫秒。写 2 句：太大 / 太小各有什么问题？

---

## 推荐学习路线（1–2 天）

| 天 | 题目 | 目标 |
|----|------|------|
| Day 1 | 观察表 + 题 1–5 | 三路径分工、max_tokens、with_retry |
| Day 2 | 题 6–9 + 场景 B/C | 与 s08 compact 交叉、状态机 |
| 可选 | 题 10–12 + 题 15 | 改代码 + 设计权衡 |

---

## 验收 checklist

- [ ] 能画出三路径：谁在 `with_retry` 内、谁在外层 except、谁看 `stop_reason`
- [ ] 知道 `max_tokens` 是 API 字段，不是模型输出
- [ ] 能解释第一次 escalate 不 append 的原因
- [ ] 知道 reactive compact 只一次，失败则退出
- [ ] 能写出退避公式与 jitter 的作用
- [ ] 能对比 auto compact（调用前）与 reactive（报错后）
- [ ] 知道 continuation 期间不会 extract，真正结束才 extract

---

## 调试技巧

| 现象 | 排查 |
|------|------|
| 一碰 529 就崩 | `with_retry` 是否包住了 `messages.create`？异常文案是否含 `529`/`overloaded`？ |
| 升到 64K 仍截断却不续写 | `has_escalated` 是否已 True？是否误在 escalate 分支就 `return`？ |
| continuation 死循环 | `recovery_count` / `MAX_RECOVERY_RETRIES` |
| compact 两次仍超限却一直重试 | `has_attempted_reactive_compact` 是否生效？ |
| 切了 fallback 但请求仍用旧模型 | lambda 是否捕获 `state.current_model`？ |
| 误把 prompt_too_long 当 429 重试 | `with_retry` 是否错误匹配了 `"too many tokens"`？ |

---

## 和前后章节的关系

| 章节 | 关系 |
|------|------|
| s08 Compact | auto/L4 在调用前；s11 reactive 在 API 报错后 |
| s10 System Prompt | 组装与 cache 保留；恢复后 `continue` 会重新组装 |
| s09 Memory | extract 仍在真正结束时；continuation 中途不 extract |
| s12 Task System | 恢复就绪后，下一章做任务系统 |

s11 练习的核心收获：**错误要分类处理——临时故障重试，上下文改输入再试，输出截断先加配额再续写；别对所有错误一视同仁地 sleep。**

---

## 优先推荐（时间有限）

1. **观察表 #1–3** — 10 分钟建立三路径直觉  
2. **题目 3、4** — escalate 不 append + with_retry 边界  
3. **题目 7** — Path 1 状态机  
4. **场景 B/C** — 纸上推演  
5. **题目 15** — 设计权衡  

---

## 参考答案提示（自测用）

<details>
<summary>题目 1–5 简答</summary>

**题 1**：Path1=`stop_reason==max_tokens` / 成功返回后 / 升 token 或 continuation；Path2=异常文案含 prompt too long / 外层 except / reactive compact 一次；Path3=429或529 / `with_retry` 内 / 退避（+可选切模型）。

**题 2**：不是模型写的；是 Anthropic API 响应里的 `stop_reason` 枚举。

**题 3**：只设 `max_tokens=64000`、`has_escalated=True`，`continue`，messages 不变。若先 append 半截，history 会留下残缺 assistant，浪费 token 且干扰续写。

**题 4**：不会；非 429/529 直接 `raise`。盲目重试同一超长 prompt 只会反复失败、浪费配额。

**题 5**：32000；`base * 0.25`。jitter 避免多客户端同一时刻重试（惊群）。

</details>

<details>
<summary>题目 6–7、12 简答</summary>

**题 6**：reactive 在 API 报错后；条件是 `is_prompt_too_long_error`；只一次；合并版用 LLM `reactive_compact`（摘要+尾部），不是 stub 的纯 trim。

**题 7**：8K → escalate 到 64K（不 append）→ 仍截断则 append+continuation（最多 3）→ 用尽则 return。

**题 12**：先 append 再检查，第一次 escalate 也会把半截输出写进 messages，违背「同一请求重试」设计。

</details>
