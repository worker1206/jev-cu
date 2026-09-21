# jev-cu

**Jev 做系统一判断，LLM 做系统二生成**——一个把「快判断」和「慢思考」显式分层的浏览器 computer-use 骨架。

- **系统一（Jev / TypeSafe System One）**：每个动作步一次 HTTP 调用，同时问三件事——
  选哪个元素（choice）、有多确信（score）、任务是否已完成（noul）。约 1 秒、约 900 tokens。
- **系统二（OpenAI 兼容 LLM）**：只在 Jev 的 **margin 低于阈值** 时接管判断，并负责需要生成文本的场合
  （要往输入框里填的字符串）。
- **安全门**：凡危险动作（提交/支付/删除/发送…）无条件要求人工确认，与置信度无关。

**两句话说完交付形态**：能力只以 **MCP（stdio）** 与 **SKILL.md** 两种协议对外暴露，
代码里不存在任何宿主分支（没有 `if host == ...` 之类判断）——**不绑定任何宿主**。
真实站点的实测数据见下面「实测数据」表（单步 87.5%、多步 93.3%、1.03s/步、913 tok/步）。

## 为什么用 margin，而不是 confidence

实测踩过的坑：出现过 `confidence=1.32` 但概率分布几乎平局（`margin≈0.02`）的失真样本。
因此弃权口径取 **choice.probabilities 的 top1 − top2 差**，`confidence` 只做记录、不参与路由。

```python
margin = sorted(probabilities.values(), reverse=True)[0] - [1]   # top1 - top2
```

## 实测数据（真实站点 · 真实 API）

| 指标 | 实测值 | 来源 |
|---|---|---|
| 单步决策准确率 | **7/8 = 87.5%** | `eval/REPORT.md`（增强属性采集后） |
| 多步链路步级准确率 | **14/15 = 93.3%** | `eval/CALIBRATION.md`（6 条任务链） |
| 平均单步延迟 | **1.03 s/步** | 同上 |
| 平均 input tokens | **≈ 913 tok/步** | 同上 |
| 唯一真错误步的 margin | **0.28** | 该步正是被弃权机制拦下的样本 |
| `margin >= 0.30` 的效果 | 保留 **87%** 自动步，Jev 侧准确率 **100%** | 阈值校准 |
| `done >= 0.50` 的效果 | 终点步 3/3 命中，中段 0/12 误报 | 阈值校准 |

结论：**单请求三问不显著增加成本**，单步可一次调用拿全决策，成本模型成立。

## 阈值（v0.2，实测校准）

| 阈值 | 值 | 行为 |
|---|---|---|
| `JEV_ROUTE_T` | `0.30` | `margin < 0.30` → 升级 LLM |
| `JEV_DONE_T` | `0.50` | `done >= 0.50` → 判定任务完成 |
| `CONF_T` | 不启用 | margin 已足够，少一个要校准的参数 |
| 安全门 | 无条件 | 危险动作一律人工确认 |

阈值可用环境变量覆盖，默认值与 `eval/CALIBRATION.md` 一致。

## 安装

```bash
pip install playwright typer      # mcp 需要 Python >= 3.10
python3 -m playwright install chromium
```

## 用法（CLI）

```bash
# 从工作区根 .env 读取 JEV_API_KEY（勿写进代码、勿提交）
jev-cu run "在维基百科搜索\"人工智能\"" --url https://www.wikipedia.org --max-steps 25

# 显式提供要输入的文本，可重复
jev-cu run "登录后台" --url https://example.com/login --value admin --value 's3cret'

# 只读某个会话的决策日志汇总
jev-cu status 20260921-120000-ab12cd

# 配置体检：探测 Jev 端点是否可达（只报状态，不打印凭证）
jev-cu doctor
```

### 要填进输入框的文本从哪来（`--value` 优先）

按优先级依次尝试，**第一优先是 `--value`，复杂任务请一律用它**：

| 优先级 | 来源 | 例子 |
|---|---|---|
| 1 | `--value`（可重复，按顺序消费） | `--value admin --value 's3cret'` |
| 2 | 任务文本里的引号内容 | `"在维基百科搜索\"人工智能\""` → `人工智能` |
| 3 | 任务文本里的「搜索 X」句式 | `"百度搜索 TypeSafe AI 并执行"` → `TypeSafe AI` |
| 4 | 都没有 → 该输入框按 click 处理（不填字） |

`--value` 是**位置无关的顺序队列**：第 1 个 `--value` 给第一个被选中的输入框，
第 2 个给第二个，以此类推。多字段表单（登录、收货地址、多步向导）必须逐个显式给出：

```bash
# 登录：用户名 + 密码，两个 --value 按被选中顺序消费
jev-cu run "登录后台并确认进入仪表盘" --url https://example.com/login \
         --value admin --value 's3cret'

# 收货表单：姓名 / 电话 / 地址
jev-cu run "填写收货信息" --url file:///tmp/order.html \
         --value 张三 --value 13800000000 --value "北京市海淀区"
```

注意 `--value` 只负责**文本**；「要不要回车提交」由元素类型决定
（输入框默认 fill 后回车）。危险动作另走安全门，见下节。

输出永远是 JSON，且必带 `schema_version: "1"`：

```json
{
  "schema_version": "1",
  "session_id": "20260921-120000-ab12cd",
  "status": "finished",
  "steps": 4,
  "log_path": "./.jev-cu/log/20260921-120000-ab12cd.jsonl",
  "llm_fallback": {"available": false, "used": 0, "missing": ["LLM_BASE_URL", "LLM_API_KEY"]},
  "final": {"url": "https://zh.wikipedia.org/wiki/人工智能", "title": "人工智能 - 维基百科"}
}
```

退出码：`0` 完成/到达步数上限 · `1` 运行期错误（含危险动作被人工拒绝） · `2` 配置缺失（如无 `JEV_API_KEY`，返回结构化错误而非堆栈）。

## 瞬态错误重试（5xx / 429 / 网络抖动）

Jev 的单步推理请求是**无状态幂等**的，所以服务端抖动可以安全重试；而 4xx 是请求本身的问题，
重试只会重复犯错——**一律不重试**。

| 情况 | 行为 |
|---|---|
| `503`（如 `no healthy upstream`）/ 其他 5xx / `429` | 自动重试 |
| `URLError`（DNS、连接重置、超时等网络抖动） | 自动重试 |
| `400/401/403/404/422` 等 4xx | **不重试**，立即失败 |
| 单次调用重试上限 | Jev 用 `JEV_RETRY_MAX`、LLM 用 `LLM_RETRY_MAX`，默认都是 **3** 次（总尝试 = 4） |
| 退避 | **0.5s → 1.5s → 4.0s**，每次再加 ≤25% 随机抖动（避免多进程同步重试） |
| session 级总预算 | `JEV_RETRY_BUDGET`，默认 **8** 次重试/任务，触顶即熔断 |

**两条链路共用同一套口径**：`brain_llm` 直接复用 `brain_jev` 的
`is_retryable_status` / `backoff_seconds` / `RETRY_SCHEDULE` / `RETRY_JITTER`
（同一个函数对象、同一张退避表），不存在"Jev 会重试、LLM 不会"的口径分裂。

重试次数如实落到两处：成功时 `Decision.retries`（随决策日志的 `decision.retries` 落盘），
失败时 `JevError.retries` / `LlmError.retries` + `.status`
（主循环写进 `phase="jev"` 或 `fallback.error` 的 `retries/status`）。

实测样例（2026-09-21，Jev 端真实 503 期间）：`retries=1`，退避后重试成功，
该步 `latency=12.06s`，任务最终 `done=0.62 → finished`。

### 重试总预算与熔断（`status="upstream_unstable"`）

单次重试上限拦不住 **503 风暴**：每一步各自退避重试 ≈ 6s，25 步任务最坏能多等 150s 还在失败。
因此增加 **session 级重试总预算**：累计重试次数达到 `JEV_RETRY_BUDGET`（默认 8）即熔断——

1. 停止重试，不再发起任何新请求（硬闸门，`RetryBudget.tripped` 一置位就拒绝发送）；
2. 主循环以 `status="upstream_unstable"` 收尾（**不是** `error`），
   把"上游不稳定"与"我的请求有问题"分开；
3. 决策日志写 `phase="jev"`、`status="upstream_unstable"`，并在 `error.message`
   与 `retry_budget` 里写明熔断原因（累计次数 / 上限）；
4. 结果 JSON 顶层带 `retry_budget: {limit, spent, tripped, reason}`。

## 故障诊断：`jev-cu doctor`

`doctor` 不只是把错误文本打出来，而是**分类诊断**并给出对应退出码：

| 退出码 | `diagnosis_code` | 含义与处置 |
|---|---|---|
| `0` | `ok` | 正常，端点可达（会列出 `models`） |
| `1` | `unknown` | 未分类错误 |
| `2` | `missing_key` | 缺 `JEV_API_KEY`：写入工作区根 `.env` |
| `3` | `misconfigured_base_url` | `JEV_BASE_URL` 指向网页控制台而非 API 主机 |
| `4` | `auth_failed` | `401/403`：key 无效或已过期 |
| `5` | `service_unavailable` | `429/5xx`：**服务端故障**，稍后重试即可（代码已自动重试） |
| `6` | `network_unreachable` | `URLError`：本机网络 / 代理 / DNS 问题 |

`doctor` 还会**探测 LLM 兜底连通性**（复用 `brain_llm.http_transport` 发一条最小请求，
要求模型只回 `{"act":"1"}`），分类为
`not_configured` / `ok` / `auth_failed`(401,403) / `service_unavailable`(429,5xx) /
`unreachable` / `bad_response`。
`bad_response` 还会给出 `reason`，把两种"回复不对"分开（处置完全不同）：

| `reason` | 含义 | 提示方向 |
|---|---|---|
| `no_json` | 回来的压根不是 JSON | 端点/协议不匹配，核对 `LLM_BASE_URL` |
| `missing_act` | 是 JSON，但缺 `act` 字段 | 提示词不匹配，或该模型不是决策模型 |

探测**刻意保持单次尝试**（`max_retries=0`）：体检要如实反映"此刻通不通"，
重试会把一次瞬时故障掩盖成"正常"，也会让 doctor 变慢；有界重试只属于真实调用链。

注意：**LLM 是可选兜底，它的状态只作诊断提示，不改变 doctor 的退出码**（退出码始终跟随权威的 Jev 探测）。

### 最常见的坑：`JEV_BASE_URL` 必须是 API 主机

`JEV_BASE_URL` 要写 **API 主机**（`https://api.typesafe.ai`），
不是网页控制台（`https://console.typesafe.ai`）。指错了会得到
`Jev API 返回 307: b'/login?returnTo=%2Fv1%2Fsystemone'`——服务端把请求当成未登录，
而 key 本身完全有效。`doctor` 会直接判为 `misconfigured_base_url`（退出码 3）。

注意 `brain_jev` 的默认值本来就是正确的 API 主机，
只有显式设置了错误的 `JEV_BASE_URL` 才会走偏——所以要么改对，要么删掉这一行。

## 首次配置（快速开始）

**本仓库不含任何 API key**；key 只存在于你自己的 `.env` 里，`请勿提交 .env`（已在 `.gitignore` 中）。

```bash
# 1) 生成配置文件（.env.example 里全是空占位，没有任何真实值）
cp .env.example .env

# 2) 由你自己填入 JEV_API_KEY
#    申请地址：https://console.typesafe.ai
#    编辑 .env，把 JEV_API_KEY= 后面填上你自己的 key
#    （留空时 CLI 会报 missing_api_key，doctor 也会判 missing_key）

# 3) 自查：确认端点、鉴权、网络都对
jev-cu doctor          # 正常 → exit 0，并列出可用 models

# 4) 跑第一条任务（推荐：单目标任务，实测 2 步即达 finished）
jev-cu run '在维基百科搜索"人工智能"' --url https://www.wikipedia.org --max-steps 6

# 只想验证链路通不通，也可以用 example.com 这类占位页：
# 它会真的打开并点进链接，但页面本身没有"任务已完成"的语义，通常停在 max_steps（属正常）
jev-cu run "打开 example.com 并读取页面标题" --url https://example.com --max-steps 5
```

`doctor` 的退出码就是"哪一类故障"的答案（详见「故障诊断」节）：
`0` 正常 · `2` 缺 key · `3` base_url 指错（指到网页控制台） · `4` 鉴权失败(401/403) ·
`5` 服务不可用(5xx) · `6` 网络不可达。

### `.env` 从哪读（优先级与安全）

- **优先级**：显式指定 > **仓库根 `.env`** > 当前工作目录（及其上溯）。
  仓库根优先，避免你在别处执行命令时误加载无关目录的 `.env`。
- **不会加载 `~/.env`**：上溯在用户主目录处停止，主目录里的同名变量不会被静默带进来。
- **只记路径、不记值**：`doctor` 输出的 `env_file` 字段与调试日志只写**路径字符串**；
  设 `JEV_DEBUG=1` 可看到实际加载了哪个 `.env`（依然只有路径，没有任何值）。
- 已是环境变量的同名项**不会被 `.env` 覆盖**（`setdefault` 语义）。

### LLM 兜底是**可选**的

`LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` 三个变量**都可以不填**：
不填时低 margin 的步骤**沿用 Jev 的判断**，决策日志如实记 `not_configured`，
`doctor` 也会提示 `not_configured`（不影响退出码）。

要启用兜底就填上，注意 **base_url 必须自带版本段**（如 `/v1`，不要只写域名根）：

```bash
# OpenAI 官方
LLM_BASE_URL=https://api.openai.com/v1
LLM_API_KEY=<你自己的 key>
LLM_MODEL=gpt-4o-mini

# 自建/第三方 OpenAI 兼容网关
LLM_BASE_URL=https://your-gateway.example.com/v1
LLM_API_KEY=<你自己的 key>
LLM_MODEL=<该网关支持的模型名>
```

## 配置项（环境变量，见 `.env.example`）

| 变量 | 说明 |
|---|---|
| `JEV_API_KEY` | **必填**，Jev API key，只存在于工作区根 `.env` |
| `JEV_BASE_URL` / `JEV_MODEL` | 默认 `https://api.typesafe.ai` / `jev-latest` |
| `JEV_ROUTE_T` / `JEV_DONE_T` | 路由 / 完成阈值 |
| `JEV_RETRY_MAX` | Jev 单次调用最大重试次数，默认 `3` |
| `JEV_RETRY_BUDGET` | session 级重试总预算，默认 `8` 次；触顶熔断为 `upstream_unstable` |
| `LLM_RETRY_MAX` | LLM 单次调用最大重试次数，默认 `3`（与 Jev 同口径） |
| `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` | 系统二兜底；未配置则 `available() == False`，主循环如实记录「兜底不可用」并沿用 Jev 判断 |

## 协议级接入（不绑定任何宿主）

能力只通过两个**协议**暴露，代码里没有任何宿主分支（不存在 `if host == ...` 之类的判断）：

1. **MCP（stdio）**：`jev-cu-mcp` 提供 `browse(task, start_url, max_steps)` 与 `status(session_id)`。
   两个工具**永远返回 JSON 字符串、绝不抛异常**；`mcp` 包缺失时给出清晰提示并以退出码 2 结束。
2. **SKILL.md**：`universal/SKILL.md` 描述何时使用、用法、边界与安全约束，任何支持该约定的宿主都可装载。

```bash
jev-cu-mcp        # stdio，等待 MCP 客户端握手
```

## 目录

```
core/sensor.py      元素快照：打稳定编号 data-jev-idx，采集 innerText/value/placeholder/aria-label/name/type/title
core/brain_jev.py   系统一：三问请求、margin 计算、Decision
core/brain_llm.py   系统二兜底：OpenAI 兼容，传输层可注入
core/safety.py      危险动作识别（中英）+ 人工确认门（reader 可注入）
core/executor.py    Playwright 执行：按编号 click/fill/press_enter/dialog_search，异常转结构化错误
core/loop.py        主循环 + DecisionLog（每步 flush + fsync）
universal/cli.py    typer CLI（run / status / doctor）
universal/mcp_server.py  FastMCP stdio server（browse / status）
examples/           纯本地安全门演练：local_order_form.html + local_dialog_flow.html
                    + danger_gate_demo.py（按钮型）+ danger_dialog_demo.py（弹窗内/连续危险动作）
tests/              140 个 mock 用例（含真实浏览器用例，环境不可用时 skip）
eval/               benchmark 脚本与两份实测报告
docs/ARCHITECTURE.md 架构、状态机、重试与熔断、诊断分类、决策日志格式
CHANGELOG.md        版本变更与已知局限
.github/workflows/ci.yml  最小 CI（py3.9 + py3.11 跑 pytest + compileall + 凭证自查）
```

## 测试

```bash
python3 -m pytest tests/ -q
```

63 个用例，全部 mock，**不访问真实 API**；真实浏览器用例在环境不可用时 `pytest.skip`，保证离线也全绿。
（当前实测 140 passed / 0 skipped，明细见 `CHANGELOG.md`。）

### 危险动作安全门演练（纯本地，不碰真实站点）

两个演示脚本都用本地 HTML，页面内副作用只改文本/class，**不发网络请求、不跳转**：

```bash
python3 examples/danger_gate_demo.py              # 按钮型：拒绝 vs 批准（8 项断言）
python3 examples/danger_gate_demo.py --real-jev   # 用真实 Jev API 判断（需 JEV_API_KEY）
python3 examples/danger_dialog_demo.py            # 弹窗内 + 连续多个危险动作（19 项断言）
```

### LLM 兜底 wiring 自检（**不需要任何 LLM key**）

`examples/llm_stub_demo.py` 用 Python 标准库起一个**只监听 127.0.0.1** 的 OpenAI 兼容 stub
（`POST /v1/chat/completions`），把 LLM 兜底整条链路跑通：真实 HTTP 往返、真实 JSON 解析、
真实重试、真实主循环、真实 chromium 执行、真实 fsync 决策日志。

```bash
python3 examples/llm_stub_demo.py                 # 真实 Jev 判断 + stub 兜底（需 JEV_API_KEY）
python3 examples/llm_stub_demo.py --fake-jev      # 完全离线：不调 Jev，注入低 margin 信号
python3 examples/llm_stub_demo.py --fail-first 1  # stub 首次回 503 → 验证真实重试
python3 examples/llm_stub_demo.py --out-of-range  # stub 回越界编号 999 → 验证降 margin
```

**定性说明**：这是 **wiring 验证**，**不是 T2 闭环**——stub 只证明"链路与形状"，
真实 provider 的模型行为仍待用户提供 key 后验证。stub 不落任何凭证、结束即释放端口，
demo 用的 `LLM_API_KEY` 就是字面量 `stub`。

**按钮型**（`examples/local_order_form.html`，8/8 通过）：

| 场景 | 人工答复 | 结果 | 页面状态 |
|---|---|---|---|
| A | 拒绝 `n` | `status=declined_dangerous_action`，危险词命中「提交」 | 仍是「尚未提交」，按钮仍可用 |
| B | 批准 `y` | 动作真实执行 | 变为「已下单成功」，按钮被禁用 |

**弹窗内 + 连续多个**（`examples/local_dialog_flow.html`，19/19 通过）：
先点开对话框、在弹窗里填字，再操作弹窗内的「发送留言」，紧接着「删除草稿」——

| 场景 | 人工答复 | 已执行的危险动作 | 被拦下的 | 页面终态 |
|---|---|---|---|---|
| A 全拒绝 | 拒绝 | 无 | 「发送」 | 弹窗仍打开、留言未发出、**已填的字仍在**（`message=你好`） |
| B 连续全批准 | 批准两次 | 「发送」→「删除」 | 无 | `草稿已删除`，且**逐个动作各问一次人工**（2 次） |
| C 只批准第一个 | 第二次拒绝 | 「发送」 | 「删除」 | `留言已发送`，但草稿**完好未删** |

C 是"逐个过门"的关键证据：安全门是**每个动作各自**过门，不是一次批准就永久放行。

## 边界与安全

- Jev 只做判断，不生成文本；需要生成时升级 LLM。
- 危险动作（提交/支付/删除/发送/转账…）无条件人工确认；确认失败、空输入、异常一律视为拒绝。
- 不绕过验证码、不规避站点反爬；请遵守目标站点条款。
- 日志不记录任何凭证；`.env` 只在 `.gitignore` / `.env.example` 层面出现。
- 完成判定发生在动作**之前**：`done >= 0.50` 时不再执行任何动作。
  但 `status=finished` 只代表「Jev 判定完成」，关键结论请以 `final.url` / `final.title` 为准。

## 已知局限（如实）

- **[T5] 阈值样本量小**：8 步 / 15 步两次实测，置信区间宽，需多站点复测；
  未覆盖验证码、iframe、Canvas 应用。
- **[T9] 候选元素硬截断在 30 个**（`loop.run(collect_limit=30)`）：长列表页（搜索结果页、
  商品列表）里的目标元素会被挤出候选集。实测结果页 `elements_count` 稳定钉在 30。
  放宽会线性增加 token 成本，取舍未做——需要"结果页专用采集策略"或分页采集。
- **[T10] 历史窗口只有 3 步**（`brain_jev.build_request` 的 `history[-3:]`）：长任务的状态感知
  有限。本轮已让每步历史带上「键入值 + 落点 URL」，但仍未验证 3 条窗口对 10+ 步任务是否够用。
- **[T7 残余] 多目标任务会震荡**：任务「搜索 X **并打开词条**」在 12 步内
  `done` 始终 ≤ 0.18，循环反复重填同一个搜索框、重复点「Search」按钮，
  始终不去点结果链接。拆成两个单目标任务（先搜索、再打开）即可正常终止：
  单目标任务「搜索 X」在第 2 步 `done=0.63 ≥ 0.50` 正常 `finished`。
  根因即上面 T9 + T10 两条叠加。改进方向：分阶段子目标，或按任务显式指定"打开第 N 个结果"。
- **[T2] LLM 兜底：wiring 已验证 / 真实 provider 待 key**（两段，别混为一谈）
  - **wiring 已验证**：本地 stub（`python3 examples/llm_stub_demo.py`）跑通真实 HTTP + 真实解析
    + 真实重试 + 真实主循环 + 真实 chromium 执行；断言 `source=="llm"`、`llm_fallback.used>0`、
    `need_llm==False`、stub 首次 503 时 `Decision.retries==1`。
  - **真实 provider 端到端待 key**：未用任何真实 provider 验证过"低 margin → LLM 接管"，
    所以 **T2 未闭环**，stub 验证不能当作完成。

## License

MIT
