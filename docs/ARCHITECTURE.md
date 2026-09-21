# jev-cu 架构

## 一句话

把网页操作拆成 **看（sensor）→ 想（Jev 系统一 / LLM 系统二）→ 拦（safety）→ 做（executor）→ 记（DecisionLog）**
五段，每段都可单独替换、单独测试。

```
                 ┌──────────────────────── core/loop.py ────────────────────────┐
                 │                                                              │
  page ──► sensor.collect ──► brain_jev.ask ──┐                               │
           (稳定编号快照)      (三问一次拿全)   │ margin < ROUTE_T              │
                                              ├──► brain_llm.ask（可注入传输层）│
                                              │    (系统二兜底)                │
                                              ▼                               │
                                        safety.describe ──► confirm(reader)   │
                                        (危险动作无条件人工确认)                │
                                              ▼                               │
                                        plan_intent ──► executor.execute       │
                                        (click/fill/press_enter/dialog_search) │
                                              ▼                               │
                                        DecisionLog.append (flush + fsync)     │
                 └──────────────────────────────────────────────────────────────┘
```

## 模块职责与不变量

| 模块 | 职责 | 不变量 |
|---|---|---|
| `core/sensor.py` | 把页面压成带稳定编号的元素列表 | 编号写入 `data-jev-idx`，采集与执行共用同一编号，杜绝"采集顺序 ≠ 点击顺序"的漂移 |
| `core/brain_jev.py` | 系统一：一次请求问 choice/score/noul | 弃权只看 `margin = top1 − top2`，不看 `confidence` |
| `core/brain_llm.py` | 系统二兜底：OpenAI 兼容 | 返回与 Jev 同构的 `Decision(source="llm")`；未配置时 `available() == False`；传输层可注入 |
| `core/safety.py` | 危险动作识别 + 人工确认 | 确认失败/异常/空输入一律视为**拒绝**；绝不自动放行 |
| `core/executor.py` | Playwright 真操作 | **任何异常都转成结构化错误，不向上抛裸异常** |
| `core/loop.py` | 编排 + 决策日志 | 每步 `append → flush → fsync`；`run()` 永远返回结构化结果 |
| `universal/cli.py` | 人用入口 | 输出必带 `schema_version`；缺 key → 退出码 2 |
| `universal/mcp_server.py` | 机器用入口（stdio） | 工具**永远返回 JSON 字符串**、绝不抛异常；无宿主分支 |

### 为什么执行器不抛异常

上一轮实测中进程崩溃丢过决策数据：一旦执行器把异常抛到主循环之外，
「崩在哪一步、崩在哪个元素」就无从查证。因此执行器统一返回：

```json
{"ok": false, "action": "click", "idx": "7",
 "error": {"type": "element_not_found", "message": "编号 7 在页面上不存在（快照可能已过期）"},
 "url": "...", "title": "...", "duration_ms": 12}
```

错误类型表：

| type | 含义 |
|---|---|
| `element_not_found` | 编号在当前页面不存在（快照过期 / 页面已跳转） |
| `dialog_input_not_found` | dialog 型搜索：点了触发按钮但没等到输入框 |
| `element_detached_after_action` | 文本已填入，但回车提交时元素脱离 DOM（页面已跳转）。**不重试**：编号是新页面重新分配的，按编号重按回车会误操作到别的元素（见下） |
| `unknown_action` | 主循环传了执行器不认识的动作 |
| `TimeoutError` / `Error` / 其他 | Playwright 原始异常类型名，原文进 `message`（截断 300 字） |

主循环遇到连续 3 次执行失败即中止并给出 `status="execution_failed"`。

### `element_detached_after_action` 为什么不自动重试

`data-jev-idx` 是**每次采集重新分配**的。fill 成功后如果页面已经跳转，
旧句柄按下回车会抛 "Element is not attached to the DOM"；此时若按编号重新取元素再按一次回车，
拿到的是**新页面**上恰好同号的另一个元素——那是一次真实的误操作（可能落到提交/删除按钮上）。
因此这里选择如实上报 + 下一步重新采集，而不是"重试一下看看"。

## 阈值与升级路径

| 条件 | 行为 |
|---|---|
| `margin >= 0.30` | 信任 Jev，直接执行 |
| `margin < 0.30` | 若 LLM 已配置 → 调 LLM 接管判断；未配置 → 沿用 Jev 判断，并在日志 `fallback` 字段如实记录 `not_configured` |
| `done >= 0.50` | 判定任务完成，主循环收尾 |
| 动作描述命中危险词 | 无条件 `safety.confirm()`；被拒绝 → `status="declined_dangerous_action"` |

LLM 的单值 `confidence` 会被折成两候选分布后复用 Jev 的 margin 口径
（`margin = 2 * confidence − 1`），保证「margin 越高越可信」在整个系统里只有一套含义。

**越界编号必须 margin = 0**：`act` 不在候选集里时（`parse_llm_answer(valid_ids=...)`），
分布取零概率质量 `{act: 0.0, "__rest__": 0.0}`，`margin` 自然算得 0。
早期实现只把 `confidence` 置 0，却留下 `"__rest__": 1.0`，于是 `margin` 变成 **1.0**
——"给了不存在的元素"反而成了最高置信，现已修掉并加了回归用例。
原始编号仍保留在 `act` 里，日志中一眼可见 LLM 到底给了什么。

## 主循环状态机

```
init → (goto URL) → 循环 {
    采集 → 无元素 → no_elements
    Jev 调用异常：
        重试预算熔断 → upstream_unstable（写 phase="jev" + status + 熔断原因）
        其他         → error(jev_call_failed)
    done >= 0.50（当前状态已判完成）→ 写 phase="done" 记录 → finished  ← 判定在动作之前
    act 为空（没给编号）→ no_action
    编号不在候选集（越界）→ 交给执行器 → element_not_found（连续 3 次 → execution_failed）
    危险动作且未确认 → declined_dangerous_action
    执行失败 ×3 → execution_failed
} → 写 phase="end" 记录 → 返回
到达 max_steps 未完成 → max_steps
顶层异常 → error(结构化，附 type/message)
```

### 完成判定必须在动作之前

Jev 的 `done` 问的是「**结合历史操作，该任务是否已经完成**」——问的是**当前**状态。
所以主循环在**执行动作前**就先看 `used.finished`；为真则写一条 `phase="done"` 记录、
`result["steps"] = step - 1`（本步没有执行动作）、`decided_finished_at_step = step` 后立即终止。

反例（T7 实测的真 bug）：旧实现先执行、后判断，于是在「已经搜到结果的页面」上又点了一次
空搜索，页面被推到空查询结果页，`done` 仍有 0.64 → `status=finished` 却与页面实际状态不符。
修正后同一条任务：step1 填字搜索（done=0.07）→ step2 判 done=0.63 直接终止，
`final.url` 停在真正的结果页 `Special:Search?search=人工智能`，`title` 为
「人工智能 - Search results - Wikipedia」。

**消费方注意**：`status=finished` 只代表「Jev 判定完成」，
关键结论请以 `final.url` / `final.title` 与实际页面状态为准（`snapshot` 已随每条记录落盘）。

## 决策日志格式（`.jev-cu/log/<session>.jsonl`）

一行一个 JSON 对象，追加写，**每行写完立即 `flush()` + `os.fsync()`**。

### `phase="act"`（核心记录，每步一条）

| 字段 | 类型 | 说明 |
|---|---|---|
| `ts` | float | Unix 时间戳 |
| `session_id` | str | 会话号（= 日志文件名） |
| `step` | int | 步序号，从 1 开始 |
| `phase` | str | `act` |
| `task` | str | 任务原文 |
| `source` | str | 本步决策来源：`jev` 或 `llm` |
| `decision` | obj | `Decision.as_dict()`：`act/confidence/margin/done/probabilities/latency/input_tokens/need_llm/finished/source` |
| `fallback` | obj \| null | `{"used": true, "ok": true}` / `{"used": false, "reason": "not_configured", "missing": [...]}` |
| `elements_count` | int | 本步候选元素数（>30 会被截断，是重要排查线索） |
| `label` | str | 被选中元素的 `<tag> label` |
| `intent` | obj | `{"kind": "click\|fill\|press_enter\|dialog_search", "idx": "7", "value": "人工智能", "submit": true}` |
| `safety` | obj | `{"dangerous": bool, "keyword": "提交"\|null, "confirmed": bool\|null}` |
| `execution` | obj | 执行器结果（见上「执行器」节结构表） |
| `snapshot` | obj | 动作后的 `{url, title}` |

### LLM 兜底（wiring + 真实 provider 均已验证）

- **真实 provider**：已用真实 key 跑通"强制升级 → LLM 接管 → 不二次升级"与
  "默认阈值 → 不调用 LLM"两段（证据见 CHANGELOG 的 Closed 段）。
  即 `brain_llm.ask` → `parse_llm_answer` → 主循环落地这条真实链路已闭环。
- **wiring（无凭证回归）**：`examples/llm_stub_demo.py` 提供只监听 127.0.0.1 的 OpenAI 兼容 stub，
  在**没有 LLM key** 的情况下也能验证真实 HTTP / 解析 / 重试 / 主循环。
  它验证的是**接线**，不能替代真实模型行为（覆盖仍薄，见 T19）。

### 其他 phase

- `phase="sense"` + `status="no_elements"`：页面没有可交互元素。
- `phase="jev"` + `error`：Jev 调用失败（结构化错误原文）。
- `phase="plan"` + `status="no_action"`：决策**没有给出编号**（`act` 为空）。
  注意"编号越界"不走这条路：它会在 `phase="act"` 记录里带 `element_not_found`，
  两者是刻意区分开的两种终态。
- `phase="safety"` + `status="declined_dangerous_action"`：人工拒绝，**此记录之后不会再有执行动作**。
- `phase="done"` + `status="finished"`：当前状态已判完成（`done >= DONE_T`），本步**没有执行动作**；
  含 `decision` / `fallback` / `snapshot`。`steps` 不因这条记录增加。
- `phase="end"`：会话终态，含 `status` 与最终 `snapshot`。**`status` 的权威来源**；缺此记录说明进程中途死亡（`summarize_log` 会报 `no_terminal_record`）。

读取侧容错：坏行不抛异常，包成 `{"_parse_error": ..., "_raw": ...}` 返回，
`summarize_log()` 统计 `parse_errors` 计数，便于发现"写了一半"的崩溃现场。

## 可测试性设计

- 决策大脑：`jev_ask` / `llm_ask` 都是可注入参数（默认真实实现）。
- 执行器：只依赖 `query_selector` / `wait_for_timeout` / `title` / `url`，可用假页面完整测试。
- 安全门：`confirm(reader=...)` 注入假 reader，测试无需 stdin。
- 传输层：`brain_llm.ask(transport=...)` 注入假传输，测试不碰网络。
- 真实浏览器用例：先取浏览器，取不到才 `pytest.skip`，**断言失败仍算失败**（不掩盖问题）。

## 不绑定宿主的做法

能力只以 **MCP 工具** 和 **SKILL.md** 两种协议对外呈现；
`core/` 与 `universal/` 中不存在任何针对具体宿主的条件分支或宿主标识判断，
宿主差异全部由「读环境变量」和「调用方传参」吸收。

## 瞬态错误重试

Jev 单步推理请求**无状态幂等**，因此服务端抖动可安全重试；4xx 是请求本身的问题，重试只会重复犯错。

```
brain_jev.ask()
  └─ 循环：post_json() 失败时判断 is_retryable_status()
       ├─ 429 / 5xx   → 退避 0.5s → 1.5s → 4.0s（各加 ≤25% 抖动）后重试，最多 RETRY_MAX=3 次
       ├─ URLError    → 同上（DNS / 连接重置 / 超时等网络抖动）
       └─ 其他 4xx    → 立即失败，不重试、不等待
```

- 成功：`Decision.retries` 记录实际重试次数（随决策日志 `decision.retries` 落盘）。
- 失败：抛 `JevError(message, retries, status)`；主循环把 `retries` / `status` 写进
  `phase="jev"` 记录的 `error` 字段，日志里能区分"服务端 5xx"与"请求本身有问题"。
- 注入点：`ask(sender=..., sleep=..., max_retries=..., budget=...)`，测试全 mock、不等待真实时间。

### 两条链路共用同一套口径

`brain_llm` **直接 import** `brain_jev` 的 `is_retryable_status` / `backoff_seconds` /
`RETRY_SCHEDULE` / `RETRY_JITTER`（同一个函数对象、同一张退避表），单次上限各自用
`JEV_RETRY_MAX` / `LLM_RETRY_MAX`（默认都是 3）。测试里有专门的"口径一致性"断言
（`brain_llm.is_retryable_status is brain_jev.is_retryable_status`），防止两边漂移。

### session 级总预算与熔断

单次上限拦不住 503 风暴（每步各退避重试 ≈ 6s，25 步最坏多等 150s 仍在失败）。
`RetryBudget` 跨步共享、累计计数：

```
每次"真的要重试一次"→ budget.spend()
重试前 → budget.allow()  失败即抛 budget_exceeded=True 的错误
进入 ask() 前 → budget.tripped 为真则**直接失败，不发请求**（硬闸门）
```

触顶（`JEV_RETRY_BUDGET`，默认 8）后主循环给 `status="upstream_unstable"`，
决策日志的 `phase="jev"` 记录带 `status` 与 `retry_budget`，结果 JSON 顶层也带
`retry_budget: {limit, spent, tripped, reason}`——"上游不稳定"与"我的请求有问题"因此可区分。

## 诊断分类（`jev-cu doctor`）

`probe_jev()` / `probe_llm()` 永远返回结构化 dict 且**绝不抛异常**，分类与退出码：

| category | 退出码 | 触发条件 |
|---|---|---|
| `ok` | 0 | `GET /v1/models` 返回可解析 JSON |
| `unknown` | 1 | 其他（含返回非 JSON 且不像登录页） |
| `missing_key` | 2 | 无 `JEV_API_KEY`（未发起请求） |
| `misconfigured_base_url` | 3 | 响应落在 `/login`（含跟随重定向后的 `final_url`，或 307+`Location`） |
| `auth_failed` | 4 | 401 / 403 |
| `service_unavailable` | 5 | 429 / 5xx |
| `network_unreachable` | 6 | `URLError` |

LLM 探测复用 `brain_llm.http_transport`（注入点 `transport`），要求模型只回 `{"act":"1"}`，
分类 `not_configured` / `ok` / `auth_failed` / `service_unavailable` / `unreachable` / `bad_response`；
`bad_response` 再用 `reason` 区分 `no_json`（压根不是 JSON → 端点/协议不对）与
`missing_act`（是 JSON 但缺 `act` → 提示词不匹配或不是决策模型），两者 hint 不同。

探测**刻意单次尝试**（`max_retries=0`）：体检要如实反映"此刻通不通"，重试会把瞬时故障
掩盖成正常；有界重试只属于真实调用链。
**LLM 是可选兜底，其状态不改变 doctor 退出码**——退出码始终跟随权威的 Jev 探测结果。
`http_transport` 把 `HTTPError` 统一换成带 `status` 的 `LlmError`，这是 LLM 分类能区分鉴权/5xx 的前提。
