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
| `unknown_action` | 主循环传了执行器不认识的动作 |
| `TimeoutError` / `Error` / 其他 | Playwright 原始异常类型名，原文进 `message`（截断 300 字） |

主循环遇到连续 3 次执行失败即中止并给出 `status="execution_failed"`。

## 阈值与升级路径

| 条件 | 行为 |
|---|---|
| `margin >= 0.30` | 信任 Jev，直接执行 |
| `margin < 0.30` | 若 LLM 已配置 → 调 LLM 接管判断；未配置 → 沿用 Jev 判断，并在日志 `fallback` 字段如实记录 `not_configured` |
| `done >= 0.50` | 判定任务完成，主循环收尾 |
| 动作描述命中危险词 | 无条件 `safety.confirm()`；被拒绝 → `status="declined_dangerous_action"` |

LLM 的单值 `confidence` 会被折成两候选分布后复用 Jev 的 margin 口径
（`margin = 2 * confidence − 1`），保证「margin 越高越可信」在整个系统里只有一套含义。

## 主循环状态机

```
init → (goto url) → 循环 {
    采集 → 无元素 → no_elements
    Jev 调用异常 → error(jev_call_failed)
    决策不指向任何存在编号 → no_action
    危险动作且未确认 → declined_dangerous_action
    执行失败 ×3 → execution_failed
    执行成功且 done >= 0.50 → finished
} → 写 phase="end" 记录 → 返回
到达 max_steps 未完成 → max_steps
顶层异常 → error(结构化，附 type/message)
```

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

### 其他 phase

- `phase="sense"` + `status="no_elements"`：页面没有可交互元素。
- `phase="jev"` + `error`：Jev 调用失败（结构化错误原文）。
- `phase="plan"` + `status="no_action"`：决策编号不存在。
- `phase="safety"` + `status="declined_dangerous_action"`：人工拒绝，**此记录之后不会再有执行动作**。
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
