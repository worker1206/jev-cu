# jev-cu

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


## 风险

- 真实 provider 覆盖薄（单模型、单类任务）；多目标任务会震荡（候选截断 30 + 历史窗口 3 步）。
- 上游 5xx/429/网络抖动会重试（3 次，退避 0.5/1.5/4.0 s），有 8 次/会话预算，超限熔断为 `upstream_unstable`。
- MCP 真实 stdio 握手未在 Python ≥ 3.10 环境验证。

## 安全边界

- **危险动作无条件人工确认**（提交/支付/删除/发送），逐个动作各自过门，与置信度无关。
- **凭证只走环境变量与 `.env`**：仓库、决策日志、git 历史不含 key；CI 有凭证自查硬门禁。
- **不绕过验证码、不规避反爬**；离线演示只监听 `127.0.0.1`、只操作本地 HTML。
