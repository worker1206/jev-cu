# Changelog

本文件记录 jev-cu 的版本变更。格式参考 Keep a Changelog，版本号遵循语义化版本。

## [0.1.0] - 2026-09-21

首个可用版本：Jev 做系统一判断、LLM 做系统二兜底，端到端可跑真实浏览器任务。

### Added

- **系统一（Jev）**：`core/brain_jev.py` —— 一次请求同时问三件事
  （`choice` 选元素 / `score` 确信度 / `noul` 完成度），约 1s、约 900 tokens。
- **系统二（LLM 兜底）**：`core/brain_llm.py` —— OpenAI 兼容；仅在 `margin < 0.30` 时接管；
  未配置时 `available()` 返回 `False`，主循环如实记录「兜底不可用」并沿用 Jev 判断。
- **元素快照**：`core/sensor.py` —— 给可交互元素打稳定编号 `data-jev-idx`，
  采集 `innerText/value/placeholder/aria-label/name/type/title`（input 可见文本常为空，
  不回落到这些属性就等于把模型喂成瞎子）。
- **安全门**：`core/safety.py` —— 中英危险动作识别（提交/支付/删除/发送/转账…），
  危险动作**无条件**人工确认；确认失败、空输入、异常一律视为拒绝。
- **执行器**：`core/executor.py` —— 按编号 `click` / `fill`（先 click 再填）/ `press_enter` /
  `dialog_search`；**任何异常都转成结构化错误，不向上抛裸异常**。
- **主循环**：`core/loop.py` —— 采集 → 判断 → 兜底 → 安全门 → 执行 → 完成判定 → 决策日志；
  完成判定发生在**动作之前**（避免在已完成的页面上多做一个动作把状态推偏）。
- **决策日志**：`.jev-cu/log/<session>.jsonl`，每步 `append → flush → os.fsync`，崩溃不丢数据。
- **瞬态错误重试**：5xx / 429 / `URLError` 有界重试（默认 3 次，退避 0.5s/1.5s/4.0s + ≤25% 抖动）；
  4xx（除 429）一律不重试。Jev 与 LLM 两条链路**共用同一套口径**。
- **重试总预算与熔断**：`JEV_RETRY_BUDGET`（默认 8 次/session），触顶即熔断，
  返回结构化 `status="upstream_unstable"` 并在日志写明熔断原因。
- **CLI**：`jev-cu run` / `jev-cu status` / `jev-cu doctor`，输出恒为 JSON（`schema_version: "1"`）。
- **诊断**：`doctor` 分类诊断并给分档退出码
  （0 ok / 1 unknown / 2 missing_key / 3 misconfigured_base_url / 4 auth_failed /
  5 service_unavailable / 6 network_unreachable），并探测 LLM 兜底连通性。
- **协议级接入（不绑定宿主）**：MCP（stdio）`browse` / `status`，恒返回 JSON、绝不抛异常；
  以及 `universal/SKILL.md`。代码中不存在任何宿主分支。
- **纯本地安全演练**：`examples/danger_gate_demo.py`（按钮型，8 项断言）与
  `examples/danger_dialog_demo.py`（弹窗内 + 连续多个危险动作，19 项断言），
  只使用 `examples/` 下的本地 HTML，不发网络请求、不跳转。
- **测试**：全 mock、离线可跑；真实浏览器用例在环境不可用时 `pytest.skip`。
- **LLM 兜底 wiring 自检（不需要任何 LLM key）**：`examples/llm_stub_demo.py`
  用标准库起一个只监听 127.0.0.1 的 OpenAI 兼容 stub，可离线（`--fake-jev`）或叠加真实 Jev 判断，
  验证升级链路、真实重试（`--fail-first N`）与越界编号（`--out-of-range`）；
  `examples/local_search_page.html` 为其本地页面。
- **CI**：`.github/workflows/ci.yml`（Python 3.9 + 3.11 跑 pytest / compileall / 凭证自查）。
- **文档**：`README.md`（含「首次配置（快速开始）」：`cp .env.example .env` → 自行填 key →
  `jev-cu doctor` 自查 → 第一条任务；并说明 LLM_* 为可选及其 base_url 版本段要求）、
  `docs/ARCHITECTURE.md`、`eval/` 下两份实测报告。
- **`.env` 加载策略**：优先级为「显式指定 > 仓库根 > cwd 及其上溯」，上溯在用户主目录处停止
  （不加载 `~/.env`）；已是环境变量的同名项不会被覆盖（`setdefault`）。
  `doctor` 输出 `env_file` 字段、`JEV_DEBUG=1` 时的调试日志，都**只记录路径字符串，绝不记录任何值**。
- **面向使用者的 `.env.example` 注释**：JEV_API_KEY 申请地址与留空后果、LLM_* 可选的后果、
  `JEV_BASE_URL` 必须是 API 主机的警告。

### 实测数据（真实站点 · 真实 API）

| 指标 | 实测值 |
|---|---|
| 单步决策准确率 | 7/8 = 87.5% |
| 多步链路步级准确率 | 14/15 = 93.3% |
| 平均单步延迟 | 1.03 s/步 |
| 平均 input tokens | ≈ 913 tok/步 |
| `margin >= 0.30` | 保留 87% 自动步，Jev 侧准确率 100% |
| `done >= 0.50` | 终点步 3/3 命中，中段 0/12 误报 |

### Fixed

- **越界编号曾被算成最高置信**：LLM 返回不在候选集里的编号时，旧实现只把 `confidence` 置 0，
  概率分布仍是 `{"999": 0.0, "__rest__": 1.0}`，`compute_margin` 取到 `1.0 - 0.0 = 1.0`
  ——**越界反而成了"最可信"**（注释声称降 margin，代码却相反）。
  现改为给出零概率质量分布，`margin` 归零，原始编号仍保留在 `act` 里便于排查。
  回归用例：`test_out_of_range_act_must_not_look_confident` 等。
- **越界编号的终态不再与"没给编号"混淆**：编号不在候选集里时交由执行器产出统一的
  `element_not_found`（连续 3 次 → `status="execution_failed"`）；
  只有 `act` 为空才是 `status="no_action"`。

### Known Limitations

如实列出本版本**没有**解决的问题（不要当成已完成）：

- **[T2] LLM 兜底：wiring 已验证，真实 provider 端到端待 key**（两段，别混为一谈）
  - **wiring 已用本地 stub 验证 ✅**：`examples/llm_stub_demo.py` 起一个只监听 127.0.0.1 的
    OpenAI 兼容 stub，跑通了**真实 HTTP 往返 + 真实 JSON 解析 + 真实重试 + 真实主循环 +
    真实 chromium 执行 + 真实 fsync 决策日志**；断言成立：`decision.source=="llm"`、
    `llm_fallback.used>0`、升级后 `need_llm==False`（不二次升级）、stub 首次回 503 时
    `Decision.retries==1`。见 `tests/test_llm_wiring.py`。
  - **真实 provider 端到端待 key ⏳**：本机未配置 `LLM_BASE_URL` / `LLM_API_KEY`，
    **没有**用任何真实 provider 验证过"低 margin → LLM 接管"。
    因此 T2 **尚未闭环**，stub 验证不能当作 T2 完成。
- **[T3] MCP 真实握手未验证**：`mcp` 包要求 Python ≥ 3.10，本机 3.9 装不上。
  已验证的是"模块可 import、工具恒返回 JSON 字符串、缺包时清晰提示并退出码 2"，
  **FastMCP 的真实 stdio 握手未验证**。
- **[T5] 阈值样本量小**：8 步 / 15 步两次实测，置信区间宽；
  未覆盖验证码、iframe、Canvas 应用。
- **[T9] 候选元素硬截断 30 个**（`collect_limit=30`）：长列表页（搜索结果页、商品列表）
  的目标元素会被挤出候选集。放宽会线性增加 token 成本，取舍未做。
- **[T10] 历史窗口仅 3 步**（`history[-3:]`）：长任务状态感知有限，
  3 条窗口对 10+ 步任务是否够用未验证。
- **[T7 残余] 多目标任务会震荡**：任务「搜索 X **并打开词条**」在 12 步内 `done` 始终 ≤0.18，
  循环反复重填同一个搜索框、重复点「Search」，始终不去点结果链接（根因是 T9 + T10 叠加）。
  拆成两个单目标任务即可正常终止（单目标任务第 2 步 `done=0.63` 正常 `finished`）。
- **[T14 残余] 安全演练覆盖面**：已覆盖按钮型 + 弹窗内 + 连续多个危险动作；
  未覆盖 iframe 内、以及"批准后动作本身失败"的路径。
- **[T13 残余]** doctor 的 LLM 探测为单次尝试（刻意如此），`bad_response` 只校验 `act` 字段存在性。
- **[T19] stub ≠ provider**：stub 只覆盖"形状正确"的响应；真实 provider 的模型行为
  （提示词遵从度、字段漂移、长上下文截断）未经任何验证。

### Notes

- 本版本**未** `git push`、未创建远端仓库。
- 凭证只存在于工作区根 `.env`（未纳入版本控制）；仓库内无任何真实 key。
