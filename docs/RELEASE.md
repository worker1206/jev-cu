# jev-cu 发布清单（v0.1.0）

本文件是**给发布者照做的操作清单**。

- 本文件**不含任何真实仓库地址或凭证**：`<URL>` 由发布者替换。
- **本清单尚未执行**：仓库当前**没有配置远端**，也**没有 push 过任何内容**。
- 本窗口（开发窗口）**不会执行任何 push 动作**。

---

## 0. 一句话状态

| 项 | 值 |
|---|---|
| 本地分支 | `main` |
| tag | `v0.1.0`（annotated，指向发布提交，**已冻结**） |
| 远端 | **无**（`git remote -v` 为空） |
| 已 push | **否** |
| 后续改动 | 一律走 `v0.1.1`，`v0.1.0` 不再移动 |

---

## 1. 发布前检查（本地全绿才继续）

```bash
cd jev-cu

# 1.1 工作树必须干净、tag 必须指向 HEAD
git status --short            # 期望：无输出
git rev-list -n1 v0.1.0       # 期望：等于下面这条
git rev-parse HEAD

# 1.2 测试与语法
python3 -m pytest tests/ -q   # 期望：161 passed（有浏览器时）
python3 -m compileall -q core universal tests examples

# 1.3 无凭证回归（不需要任何 key）
python3 examples/llm_stub_demo.py --fake-jev              # 期望：all_passed=true
python3 examples/llm_stub_demo.py --out-of-range --fake-jev  # 期望：all_passed=true

# 1.4 确认 .env 从未被跟踪
git ls-files | grep -x ".env" && echo "危险：.env 被跟踪" || echo ".env 未被跟踪 ✓"

# 1.5 凭证自查（把真实 key 读进变量但**不打印**；只输出命中计数）
KEY=$(grep -E '^JEV_API_KEY=' ../.env | cut -d= -f2- | tr -d '"'"'"' \r')
grep -rIF -- "$KEY" . | wc -l    # 期望：0
git log -p --all | grep -cF -- "$KEY"   # 期望：0
```

> 本地若装了 playwright，`pytest` 为 161 passed / 0 skipped；
> 若**没有**浏览器，则为 **156 passed / 5 skipped**（5 条浏览器用例自行 skip，不是失败）。

---

## 2. 添加远端并首次推送（**由发布者执行**）

```bash
cd jev-cu

# 2.1 添加远端（<URL> 换成你自己的仓库地址）
git remote add origin <URL>
git remote -v                 # 核对 origin 指向正确

# 2.2 推送主分支（首次带 -u 建立跟踪）
git push -u origin main

# 2.3 推送版本 tag
git push origin v0.1.0
```

注意事项：

- 若远端仓库是**已初始化**的（自带 README / LICENSE / CI 等），
  直接 push 会被拒。此时先 `git pull --rebase origin main` 再 push；
  确实需要覆盖远端初始提交时用 `git push --force-with-lease -u origin main`
  （**谨慎**：`--force-with-lease` 在别人已推送时会失败，这是保护，不要改用 `--force`）。
- **不要**推送 `.env`：它不在版本控制内，且 `.gitignore` 已覆盖；推送前可用第 1.4 步复核。
- tag 是 annotated tag（`git tag -n5` 可看到说明），`git push origin v0.1.0` 会把它推过去。

---

## 3. CI 预期矩阵

工作流：`.github/workflows/ci.yml`，触发于 `push` / `pull_request` / `workflow_dispatch`，
`permissions: contents: read`（只读，最小权限）。

| 步骤 | py3.9 | py3.11 | 说明 |
|---|---|---|---|
| `actions/checkout@v4` + `setup-python@v5` | ✅ | ✅ | |
| 安装必需依赖（`pytest` `typer`） | ✅ | ✅ | |
| 安装可选 `mcp`（`\|\| echo notice`） | ⚠️ 预期失败 | ✅ | `mcp` 要求 Python ≥3.10；失败**不**中断 job |
| 安装 `playwright` + chromium（`continue-on-error`） | 视网络 | 视网络 | 装不上则浏览器用例自行 skip |
| `python -m pytest tests/ -q` | ✅ | ✅ | 有浏览器 161 passed；无浏览器 156 passed / 5 skipped |
| `python -m compileall -q core universal tests` | ✅ | ✅ | |
| 凭证自查 | ✅ 必须绿 | ✅ 必须绿 | 见下 |

**凭证自查步骤（硬门禁）**，任一命中即 job 失败：

1. 仓库内不得存在 `.env`（`test ! -f .env`）；
2. `core universal tests examples docs` 中不得出现 `sk-<20+>` 或 `Bearer <30+>` 形态的硬编码凭证。

也就是说：**两个 job × 上述步骤全绿（py3.9 的 mcp 安装是唯一预期告警）**即视为 CI 通过。

---

## 4. 发布后核对

- [ ] GitHub Actions 两个 job（`pytest (py3.9)`、`pytest (py3.11)`）均为绿。
- [ ] 仓库 tag 列表出现 `v0.1.0`，指向与本地 `git rev-parse HEAD` 相同的提交。
- [ ] 仓库文件树中**没有** `.env`；用 GitHub 搜索 `sk-` / `Bearer ` 复核无命中。
- [ ] README 顶部可见：实测数据表（单步 87.5% / 多步 93.3% / 1.03s/步 / 913 tok/步）
      与「只以 MCP 与 SKILL.md 两种协议暴露、不绑定任何宿主」表述。
- [ ] `CHANGELOG.md` 的 `### Closed` 段含 T2 闭环证据，`### Known Limitations` 与代码现状一致。

---

## 5. 回滚

- **尚未 push 时**：本地 `git tag -d v0.1.0` 即可（本窗口已按"最后移一次后冻结"处理）。
- **已 push 且尚未有人使用**：`git push origin :refs/tags/v0.1.0` 删除远端 tag，
  修正后重新打 tag 再 push。
- **已有人使用**：**不要**移动或删除 `v0.1.0`，直接发 `v0.1.1`。
- 无论哪种情况，**不要**用 `git push --force` 覆盖 `main`。

---

## 6. 发布之后

- `v0.1.0` **冻结**：任何代码/文档改动都走 `v0.1.1`（`CHANGELOG.md` 顶部新增 `## [0.1.1]` 段）。
- 已知局限（T3/T5/T7/T9/T10/T12/T13/T14/T15/T17/T18/T19/T20/T21/T22）随版本继续跟踪，
  其中 T2 已在 `### Closed` 段标记闭环。
