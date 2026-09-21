"""jev-cu 命令行入口（typer）。

  jev-cu run "在维基百科搜索\"人工智能\"" --url https://www.wikipedia.org --max-steps 25
  jev-cu status 20260921-120000-ab12cd
  jev-cu doctor          # 配置体检：分类诊断 + LLM 连通性探测

输出永远是 JSON（含 schema_version="1"）；缺 JEV_API_KEY 时返回结构化错误 + 退出码 2。

退出码：0 正常 / 1 未分类错误 / 2 缺 key（配置缺失） / 3 base_url 指向网页控制台 /
        4 鉴权失败(401,403) / 5 服务不可用(429,5xx) / 6 网络不可达(URLError)
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import List, Optional

import typer

from core import brain_jev, brain_llm
from core import loop as loop_mod

SCHEMA_VERSION = "1"
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFIG = 2          # 缺少 JEV_API_KEY
EXIT_MISCONFIG = 3       # base_url 指向网页控制台而非 API 主机
EXIT_AUTH = 4            # 401 / 403
EXIT_UNAVAILABLE = 5     # 429 / 5xx
EXIT_UNREACHABLE = 6     # 网络不可达（URLError）

CATEGORY_EXIT = {
    "ok": EXIT_OK,
    "missing_key": EXIT_CONFIG,
    "misconfigured_base_url": EXIT_MISCONFIG,
    "auth_failed": EXIT_AUTH,
    "service_unavailable": EXIT_UNAVAILABLE,
    "network_unreachable": EXIT_UNREACHABLE,
    "unknown": EXIT_ERROR,
}

# 测试注入点：callable(request, timeout) -> (http_status, final_url, body_bytes)
_PROBE_SENDER = None

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


# ---------------------------------------------------------------- 探测与分类诊断
def default_probe_sender(request, timeout):
    """默认探测传输层：返回 (http_status, final_url, body_bytes)。"""
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        return getattr(resp, "status", 200), resp.geturl(), resp.read()


def classify_http_status(code):
    """HTTP 状态码 → (category, exit_code)。"""
    code = int(code)
    if code in (401, 403):
        return "auth_failed", EXIT_AUTH
    if code == 429 or 500 <= code < 600:
        return "service_unavailable", EXIT_UNAVAILABLE
    return "unknown", EXIT_ERROR


def classify_exception(exc, final_url=""):
    """异常 → (category, exit_code)。"""
    if isinstance(exc, urllib.error.HTTPError):
        return classify_http_status(exc.code)
    if isinstance(exc, urllib.error.URLError):
        return "network_unreachable", EXIT_UNREACHABLE
    if final_url and "/login" in final_url:
        return "misconfigured_base_url", EXIT_MISCONFIG
    return "unknown", EXIT_ERROR


def probe_jev(base_url, api_key, timeout=15, sender=None):
    """探测 Jev 端点并归类。永远返回结构化 dict，绝不抛异常。"""
    send = sender or _PROBE_SENDER or default_probe_sender
    report = {"ok": False, "category": "unknown", "exit_code": EXIT_ERROR, "http": None,
              "final_url": None, "location": None, "models": [], "error": None, "hint": ""}
    misconfig_hint = ("JEV_BASE_URL 指向网页控制台而非 API 主机；应设为 %s"
                      % brain_jev.DEFAULT_BASE_URL)
    try:
        req = urllib.request.Request(
            base_url.rstrip("/") + "/v1/models",
            headers={"Authorization": "Bearer " + api_key})
        status, final_url, body = send(req, timeout)
        report["http"] = status
        report["final_url"] = final_url
        try:
            payload = json.loads(body.decode("utf-8", "replace"))
        except ValueError:
            report["error"] = "响应不是 JSON（前 120 字）：%s" % \
                body[:120].decode("utf-8", "replace").strip()
            if final_url and "/login" in final_url:
                report.update(category="misconfigured_base_url", exit_code=EXIT_MISCONFIG,
                              hint=misconfig_hint)
            else:
                report.update(category="unknown", exit_code=EXIT_ERROR,
                              hint="Jev 端点返回了非 JSON 内容，请核对 JEV_BASE_URL")
            return report
        report["models"] = [m.get("name") for m in (payload.get("models") or [])]
        report.update(ok=True, category="ok", exit_code=EXIT_OK,
                      hint="配置正常：Jev 端点可达")
        return report
    except urllib.error.HTTPError as exc:
        location = exc.headers.get("Location") if exc.headers else None
        report["location"] = location
        report["http"] = exc.code
        try:
            detail = exc.read()[:160].decode("utf-8", "replace")
        except Exception:
            detail = ""
        report["error"] = "HTTP %s: %s" % (exc.code, detail)
        if exc.code in (301, 302, 307, 308) and location and "/login" in location:
            report.update(category="misconfigured_base_url", exit_code=EXIT_MISCONFIG,
                          hint=misconfig_hint)
        else:
            category, code = classify_http_status(exc.code)
            hints = {
                "auth_failed": "鉴权失败：JEV_API_KEY 无效或已过期",
                "service_unavailable": "服务不可用：Jev 返回 %s，属服务端故障，可稍后重试"
                                       % exc.code,
            }
            report.update(category=category, exit_code=code,
                          hint=hints.get(category, "未分类错误：HTTP %s" % exc.code))
        return report
    except Exception as exc:
        category, code = classify_exception(exc)
        report["error"] = "%s: %s" % (type(exc).__name__, str(exc)[:200])
        report.update(category=category, exit_code=code,
                      hint=("网络不可达：请检查本机网络 / 代理 / DNS"
                            if category == "network_unreachable"
                            else "未分类错误：%s" % str(exc)[:160]))
        return report


def probe_llm(cfg=None, timeout=15, transport=None):
    """复用 brain_llm.http_transport 发一条最小请求并归类。绝不抛异常。

    分类：not_configured / ok / auth_failed(401,403) / service_unavailable(429,5xx)
          / unreachable(URLError) / bad_response（再细分 reason: no_json / missing_act）。

    探测刻意**保持单次尝试**（`max_retries=0`）：体检要如实反映"此刻通不通"，
    重试会把一次瞬时故障掩盖成"正常"，也会让 doctor 变慢。真实调用链才有重试。
    """
    cfg = cfg or brain_llm.config()
    missing = brain_llm.missing_env(cfg)
    report = {"configured": not missing, "ok": False, "category": "not_configured",
              "missing": missing, "model": cfg.get("model"), "http": None,
              "latency": None, "reply": None, "reason": None, "error": None, "hint": ""}
    if missing:
        report["hint"] = "LLM 兜底未配置（可选）：缺少 %s" % ", ".join(missing)
        return report

    send = transport or brain_llm.http_transport(cfg, timeout=timeout, max_retries=0)
    started = time.time()
    try:
        text = send(brain_llm.PROBE_MESSAGES, cfg["model"])
    except Exception as exc:
        status = getattr(exc, "status", None)
        if isinstance(exc, urllib.error.URLError) and not isinstance(exc, urllib.error.HTTPError):
            category = "unreachable"
            hint = "LLM 网络不可达：请检查 LLM_BASE_URL / 网络"
        elif status in (401, 403):
            category = "auth_failed"
            hint = "LLM 鉴权失败：LLM_API_KEY 无效或已过期"
        elif status is not None and (status == 429 or 500 <= status < 600):
            category = "service_unavailable"
            hint = "LLM 服务不可用：HTTP %s" % status
            report["http"] = status
        else:
            category = "unknown"
            hint = "LLM 探测失败：%s" % str(exc)[:160]
        report.update(category=category, hint=hint,
                      error="%s: %s" % (type(exc).__name__, str(exc)[:200]))
        return report

    report["latency"] = round(time.time() - started, 3)

    # 字段校验分两级：完全没有 JSON ≠ 有 JSON 但缺 act —— 两者处置完全不同，
    # 前者多半是协议/端点不对，后者多半是模型没按提示词输出（或不是决策模型）。
    try:
        data = brain_llm.extract_json(text)
    except ValueError as exc:
        report.update(category="bad_response", reason="no_json", error=str(exc)[:200],
                      hint="LLM 可达但没有返回 JSON（端点或协议不匹配，请核对 LLM_BASE_URL）")
        return report

    act = str(data.get("act", "") or "").strip()
    if not act:
        report.update(category="bad_response", reason="missing_act",
                      error="JSON 缺少 act 字段：%s" % json.dumps(data, ensure_ascii=False)[:160],
                      hint="LLM 返回的是 JSON 但缺少 act 字段（提示词不匹配，或该模型不是决策模型）")
        return report

    report.update(ok=True, category="ok", reason=None, reply=act, hint="LLM 兜底可达")
    return report


@app.command()
def doctor(
    base_url: Optional[str] = typer.Option(None, "--base-url", help="覆盖 JEV_BASE_URL 做探测"),
    timeout: int = typer.Option(15, "--timeout", help="探测超时秒数"),
):
    """配置体检：分类诊断 Jev 端点 + 探测 LLM 兜底连通性。只报状态，绝不打印凭证。"""
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
        "llm_fallback_configured": brain_llm.available(),
        "probe": {},
        "llm_probe": {},
        "diagnosis_code": None,
        "diagnosis": [],
    }

    if not key:
        report["probe"] = {"ok": False, "category": "missing_key", "exit_code": EXIT_CONFIG,
                           "http": None, "final_url": None, "location": None, "models": [],
                           "error": None, "hint": "缺少 JEV_API_KEY：请写入工作区根 .env"
                                                   "（参考 .env.example），不要提交"}
        report["diagnosis_code"] = "missing_key"
        report["diagnosis"].append(report["probe"]["hint"])
        report["llm_probe"] = probe_llm(timeout=timeout)
        emit(report, EXIT_CONFIG)

    report["probe"] = probe_jev(base, key, timeout=timeout)
    report["diagnosis_code"] = report["probe"]["category"]
    report["diagnosis"].append(report["probe"]["hint"])

    # LLM 是**可选兜底**：它的状态只作诊断提示，不改变 doctor 的退出码
    # （退出码始终跟随权威的 Jev 探测结果）。
    report["llm_probe"] = probe_llm(timeout=timeout)
    if report["llm_probe"]["category"] != "ok":
        report["diagnosis"].append("LLM 兜底：" + report["llm_probe"]["hint"])

    emit(report, report["probe"]["exit_code"])


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
