# jev-cu

**Jev 做系统一判断，LLM 做系统二生成**——一个把「快判断」和「慢思考」显式分层的浏览器 computer-use 骨架。

- **系统一（Jev / TypeSafe System One）**：每个动作步一次 HTTP 调用，同时问三件事——
  选哪个元素（choice）、有多确信（score）、任务是否已完成（noul）。约 1 秒、约 900 tokens。
- **系统二（OpenAI 兼容 LLM）**：只在 Jev 的 **margin 低于阈值** 时接管判断，并负责需要生成文本的场合
  （要往输入框里填的字符串）。
- **安全门**：凡危险动作（提交/支付/删除/发送…）无条件要求人工确认，与置信度无关。

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

## 常见故障：`JEV_BASE_URL` 必须是 API 主机

`JEV_BASE_URL` 要写 **API 主机**（`https://api.typesafe.ai`），
不是网页控制台（`https://console.typesafe.ai`）。指错了会得到
`Jev API 返回 307: b'/login?returnTo=%2Fv1%2Fsystemone'`——服务端把请求当成未登录，
而 key 本身完全有效。一条命令即可分辨：

```bash
jev-cu doctor
# {"jev_base_url": "https://console.typesafe.ai", ...,
#  "diagnosis": ["JEV_BASE_URL 指向网页控制台而非 API 主机；应设为 https://api.typesafe.ai"]}
```

注意 `brain_jev` 的默认值本来就是正确的 API 主机，
只有显式设置了错误的 `JEV_BASE_URL` 才会走偏——所以要么改对，要么删掉这一行。

## 配置项（环境变量，见 `.env.example`）

| 变量 | 说明 |
|---|---|
| `JEV_API_KEY` | **必填**，Jev API key，只存在于工作区根 `.env` |
| `JEV_BASE_URL` / `JEV_MODEL` | 默认 `https://api.typesafe.ai` / `jev-latest` |
| `JEV_ROUTE_T` / `JEV_DONE_T` | 路由 / 完成阈值 |
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
examples/           纯本地安全门演练：local_order_form.html + danger_gate_demo.py
eval/               benchmark 脚本与两份实测报告
docs/ARCHITECTURE.md 架构、状态机、决策日志格式
.github/workflows/ci.yml  最小 CI（py3.9 + py3.11 跑 pytest）
```

## 测试

```bash
python3 -m pytest tests/ -q
```

63 个用例，全部 mock，**不访问真实 API**；真实浏览器用例在环境不可用时 `pytest.skip`，保证离线也全绿。

### 危险动作安全门演练（纯本地，不碰真实站点）

`examples/local_order_form.html` 是一张本地表单，点击「提交订单」只在页面内改状态文本，
不发任何网络请求、不跳转。演示脚本对同一张表单跑两遍完整主循环（真实 chromium）：

```bash
python3 examples/danger_gate_demo.py              # 脚本化决策，离线、确定性
python3 examples/danger_gate_demo.py --real-jev   # 用真实 Jev API 判断（需 JEV_API_KEY）
```

实测结果（8 项断言全过）：

| 场景 | 人工答复 | 结果 | 页面状态 |
|---|---|---|---|
| A | 拒绝 `n` | `status=declined_dangerous_action`，危险词命中「提交」 | 仍是「尚未提交」，按钮仍可用 |
| B | 批准 `y` | 动作真实执行 | 变为「已下单成功」，按钮被禁用 |

## 边界与安全

- Jev 只做判断，不生成文本；需要生成时升级 LLM。
- 危险动作（提交/支付/删除/发送/转账…）无条件人工确认；确认失败、空输入、异常一律视为拒绝。
- 不绕过验证码、不规避站点反爬；请遵守目标站点条款。
- 日志不记录任何凭证；`.env` 只在 `.gitignore` / `.env.example` 层面出现。
- 完成判定发生在动作**之前**：`done >= 0.50` 时不再执行任何动作。
  但 `status=finished` 只代表「Jev 判定完成」，关键结论请以 `final.url` / `final.title` 为准。

## 已知局限（如实）

- 样本量仍偏小（8 步 / 15 步），阈值置信区间宽，需多站点复测。
- 未覆盖：验证码、iframe、Canvas 应用、候选元素超过 30 个的长列表页面。
- 输入文本抽取依赖任务文本里的引号或「搜索 X」句式，复杂场景请用 `--value` 显式给出。
- **多目标任务会震荡**（T7 实测）：任务「搜索 X **并打开词条**」在 12 步内
  `done` 始终 ≤ 0.18，循环反复重填同一个搜索框、重复点「Search」按钮，
  始终不去点结果链接。拆成两个单目标任务（先搜索、再打开）即可正常终止：
  单目标任务「搜索 X」在第 2 步 `done=0.63 ≥ 0.50` 正常 `finished`。
  根因：候选元素被截断在 30 个（结果页链接被挤掉）+ 历史信息不足以表达"搜索已完成、该点结果了"。
  改进方向：分阶段子目标，或按任务显式指定"打开第 N 个结果"。

## License

MIT
