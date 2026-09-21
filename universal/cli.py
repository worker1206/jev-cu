"""jev-cu 命令行入口（typer）。

  jev-cu run "在维基百科搜索\"人工智能\"" --url https://www.wikipedia.org --max-steps 25
  jev-cu status 20260921-120000-ab12cd

输出永远是 JSON（含 schema_version="1"）；缺 JEV_API_KEY 时返回结构化错误 + 退出码 2。
"""
from __future__ import annotations

import json
import os
from typing import List, Optional

import typer

from core import brain_jev
from core import loop as loop_mod

SCHEMA_VERSION = "1"
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFIG = 2

app = typer.Typer(add_completion=False,
                  help="Jev-driven 浏览器 computer-use：Jev 做系统一判断，LLM 只在低 margin 时兜底")


def load_env(start=None):
    """从 start 逐级向上寻找 .env，只 setdefault，绝不回显任何值。"""
    here = os.path.abspath(start or os.getcwd())
    while True:
        candidate = os.path.join(here, ".env")
        if os.path.isfile(candidate):
            try:
                with open(candidate, "r", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line or line.startswith("#") or "=" not in line:
                            continue
                        key, _, value = line.partition("=")
                        key = key.strip()
                        if key:
                            os.environ.setdefault(key, value.strip().strip('"').strip("'"))
            except OSError:
                return None
            return candidate
        parent = os.path.dirname(here)
        if parent == here:
            return None
        here = parent


def emit(payload, code=EXIT_OK):
    """统一 JSON 输出通道；用 typer.Exit 控制退出码。"""
    typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))
    raise typer.Exit(code=code)


@app.command()
def run(
    task: str = typer.Argument(..., help="任务描述，例如：在维基百科搜索\"人工智能\""),
    url: Optional[str] = typer.Option(None, "--url", help="起始 URL"),
    max_steps: int = typer.Option(25, "--max-steps", min=1, max=200, help="最大步数"),
    value: Optional[List[str]] = typer.Option(None, "--value", help="要输入的文本，可重复"),
    session_id: Optional[str] = typer.Option(None, "--session-id", help="指定会话号"),
    log_dir: str = typer.Option(".", "--log-dir", help="决策日志根目录"),
    headless: bool = typer.Option(True, "--headless/--no-headless", help="无头模式"),
):
    """执行一次浏览器任务，输出 JSON 结果。"""
    load_env()
    if not os.environ.get("JEV_API_KEY"):
        emit({
            "schema_version": SCHEMA_VERSION,
            "status": "error",
            "session_id": session_id or loop_mod.new_session_id(),
            "task": task,
            "error": {
                "type": "missing_api_key",
                "message": "缺少 JEV_API_KEY：请写入工作区根 .env（参考 .env.example）；"
                           "不要写进代码、不要提交",
            },
        }, EXIT_CONFIG)

    result = loop_mod.run(task=task, url=url, max_steps=max_steps, headless=headless,
                          log_dir=log_dir, session_id=session_id, values=value)
    code = EXIT_OK if result.get("status") in ("finished", "max_steps") else EXIT_ERROR
    emit(result, code)


@app.command()
def doctor(
    base_url: Optional[str] = typer.Option(None, "--base-url", help="覆盖 JEV_BASE_URL 做探测"),
    timeout: int = typer.Option(15, "--timeout", help="探测超时秒数"),
):
    """配置体检：检查环境变量与 Jev 端点连通性。只报状态，绝不打印凭证。"""
    import urllib.error
    import urllib.request

    api_host = brain_jev.DEFAULT_BASE_URL
    env_file = load_env()
    key = os.environ.get("JEV_API_KEY") or ""
    base = (base_url or os.environ.get("JEV_BASE_URL") or api_host).rstrip("/")

    report = {
        "schema_version": SCHEMA_VERSION,
        "env_file_found": bool(env_file),
        "jev_api_key": "present" if key else "missing",
        "jev_base_url": base,
        "recommended_api_host": api_host,
        "llm_fallback_configured": bool(os.environ.get("LLM_BASE_URL")
                                        and os.environ.get("LLM_API_KEY")),
        "probe": {"ok": False, "http": None, "location": None, "final_url": None,
                  "models": [], "error": None},
        "diagnosis": [],
    }

    if not key:
        report["diagnosis"].append("缺少 JEV_API_KEY：请写入工作区根 .env（参考 .env.example），不要提交")
        emit(report, EXIT_CONFIG)

    try:
        req = urllib.request.Request(base + "/v1/models",
                                     headers={"Authorization": "Bearer " + key})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            final_url = resp.geturl()
            report["probe"]["final_url"] = final_url
            report["probe"]["http"] = getattr(resp, "status", 200)
            body = resp.read().decode("utf-8", "replace")
            try:
                payload = json.loads(body)
            except ValueError:
                payload = None
                report["probe"]["error"] = "响应不是 JSON（前 120 字）：%s" % body[:120].strip()
            if isinstance(payload, dict):
                report["probe"]["ok"] = True
                report["probe"]["models"] = [m.get("name")
                                             for m in (payload.get("models") or [])]
            elif final_url and "/login" in final_url:
                report["diagnosis"].append(
                    "JEV_BASE_URL 指向网页控制台而非 API 主机；应设为 %s" % api_host)
    except urllib.error.HTTPError as exc:
        location = exc.headers.get("Location") if exc.headers else None
        report["probe"]["http"] = exc.code
        report["probe"]["location"] = location
        report["probe"]["error"] = "HTTP %s: %s" % (
            exc.code, exc.read()[:160].decode("utf-8", "replace"))
        if exc.code in (301, 302, 307, 308) and location and "/login" in location:
            report["diagnosis"].append(
                "JEV_BASE_URL 指向网页控制台而非 API 主机；应设为 %s" % api_host)
    except Exception as exc:
        report["probe"]["error"] = "%s: %s" % (type(exc).__name__, exc)

    if report["probe"]["ok"]:
        report["diagnosis"].append("配置正常：Jev 端点可达")
    elif not report["diagnosis"]:
        report["diagnosis"].append("Jev 端点探测失败：请核对网络与 JEV_BASE_URL")
    emit(report, EXIT_OK if report["probe"]["ok"] else EXIT_ERROR)


@app.command()
def status(
    session_id: str = typer.Argument(..., help="会话号（决策日志文件名）"),
    log_dir: str = typer.Option(".", "--log-dir", help="决策日志根目录"),
):
    """只读汇总某个会话的决策日志。"""
    emit(loop_mod.summarize_log(session_id, log_dir=log_dir))


def main():
    app()


if __name__ == "__main__":        # 支持 python3 -m universal.cli
    main()
