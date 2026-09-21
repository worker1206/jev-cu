"""主循环：采集 → Jev 判断 →（低 margin 时）LLM 兜底 → 安全门 → 执行 → done 判定 → 决策日志。

决策日志写 .jev-cu/log/<session>.jsonl，每步即写即存并 flush + fsync：
上一轮实测中进程崩溃丢过数据，这条不能省。
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid

from core import brain_jev, brain_llm, safety, sensor
from core.executor import Executor

SCHEMA_VERSION = "1"
LOG_DIRNAME = os.path.join(".jev-cu", "log")
MAX_CONSECUTIVE_ERRORS = 3

_OPEN_Q = "「『“‘\"'《"
_CLOSE_Q = "」』”’\"'》"
_QUOTE_RE = re.compile("[%s]([^%s%s]{1,80})[%s]" % (_OPEN_Q, _OPEN_Q, _CLOSE_Q, _CLOSE_Q))
_SEARCH_RE = re.compile(
    r"(?:搜索|查询|检索|search(?:\s+for)?)\s*([^，。；、？?！!]{1,40}?)"
    r"(?=\s*(?:并|然后|再|，|。|,|；|;|$))",
    re.I,
)


# ---------------------------------------------------------------- 决策日志
class DecisionLog:
    """每步即写即存的 JSONL 日志（append + flush + fsync）。"""

    def __init__(self, session_id, base_dir="."):
        self.session_id = str(session_id)
        self.base_dir = base_dir
        self.dir = os.path.join(base_dir, LOG_DIRNAME)
        self.path = os.path.join(self.dir, "%s.jsonl" % self.session_id)

    def append(self, record):
        os.makedirs(self.dir, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())      # 崩过，必须真正落盘
        return self.path

    def read_all(self):
        return read_log(self.path)


def read_log(path):
    """读回 jsonl。坏行不抛异常，包成 _parse_error 记录便于排查。"""
    out = []
    if not os.path.exists(path):
        return out
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError as exc:
                out.append({"_parse_error": str(exc), "_raw": line[:200]})
    return out


def log_path_for(session_id, log_dir="."):
    return os.path.join(log_dir, LOG_DIRNAME, "%s.jsonl" % session_id)


def new_session_id():
    return time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


# ---------------------------------------------------------------- 意图规划
def extract_value(task):
    """从任务文本里抽取要输入的文本：优先引号内容，其次"搜索 X"式表达。"""
    task = task or ""
    m = _QUOTE_RE.search(task)
    if m and m.group(1).strip():
        return m.group(1).strip()
    m = _SEARCH_RE.search(task)
    if m and m.group(1).strip():
        return m.group(1).strip()
    return ""


def has_input(elements):
    return any(e.get("is_input") for e in (elements or []))


def plan_intent(decision, element, elements=None, values=None, task="",
                enable_dialog_search=True):
    """把决策落成具体动作意图。

    输入框 → fill（先点后填，可选回车）；页面上没有任何输入框但任务带待输入文本
    → dialog_search（点触发按钮后出现的输入框，对应 benchmark 里 GitHub 模式）；
    其余 → click。
    """
    idx = str(decision.act)
    pending = list(values or [])
    value = pending[0] if pending else extract_value(task)

    if element and element.get("is_input"):
        if value:
            return {"kind": "fill", "idx": idx, "value": value, "submit": True}
        return {"kind": "click", "idx": idx, "value": None, "submit": False}
    if enable_dialog_search and value and not has_input(elements):
        return {"kind": "dialog_search", "idx": idx, "value": value, "submit": True}
    return {"kind": "click", "idx": idx, "value": None, "submit": False}


# ---------------------------------------------------------------- 主循环
def run(task, url=None, max_steps=25, headless=True, log_dir=".", session_id=None,
        page=None, values=None, confirm_reader=None, jev_ask=None, llm_ask=None,
        executor=None, collect_limit=30, on_step=None):
    """执行一次完整的浏览器任务。永远返回结构化结果，不向调用方抛裸异常。"""
    session_id = str(session_id or new_session_id())
    log = DecisionLog(session_id, base_dir=log_dir)
    jev_ask = jev_ask or brain_jev.ask
    llm_ask = llm_ask or brain_llm.ask
    llm_ready = brain_llm.available()

    result = {
        "schema_version": SCHEMA_VERSION,
        "session_id": session_id,
        "task": task,
        "url": url,
        "status": "init",
        "steps": 0,
        "log_path": log.path,
        "llm_fallback": {"available": llm_ready, "used": 0,
                         "missing": [] if llm_ready else brain_llm.missing_env()},
        "final": {"url": "", "title": ""},
        "error": None,
    }

    history = []
    own_browser = page is None
    playwright = browser = None
    closed = False
    try:
        if own_browser:
            from playwright.sync_api import sync_playwright
            playwright = sync_playwright().start()
            browser = playwright.chromium.launch(headless=headless)
            page = browser.new_page(viewport={"width": 1280, "height": 800})

        execu = executor or Executor(page)
        if url:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            execu.settle()

        result["status"] = "max_steps"
        consecutive_errors = 0
        for step in range(1, max_steps + 1):
            elements = sensor.collect(page, limit=collect_limit)
            if not elements:
                log.append({"ts": time.time(), "session_id": session_id, "step": step,
                            "phase": "sense", "status": "no_elements",
                            "snapshot": execu.snapshot()})
                result["status"] = "no_elements"
                break

            try:
                decision = jev_ask(task, elements, history)
            except Exception as exc:
                log.append({"ts": time.time(), "session_id": session_id, "step": step,
                            "phase": "jev", "error": {"type": "jev_call_failed",
                                                      "message": str(exc)[:300]}})
                result["status"] = "error"
                result["error"] = {"type": "jev_call_failed", "message": str(exc)[:300]}
                break

            fallback = None
            used = decision
            if decision.need_llm:
                if llm_ready:
                    try:
                        used = llm_ask(task, elements, history)
                        fallback = {"used": True, "ok": True, "source": "llm"}
                        result["llm_fallback"]["used"] += 1
                    except Exception as exc:
                        fallback = {"used": True, "ok": False,
                                    "error": {"type": type(exc).__name__,
                                              "message": str(exc)[:300]}}
                else:
                    fallback = {"used": False, "reason": "not_configured",
                                "missing": brain_llm.missing_env()}

            chosen = next((e for e in elements if str(e["idx"]) == str(used.act)), None)
            label = ("<%s> %s" % (chosen["tag"], chosen["label"])) if chosen else str(used.act)
            guard = safety.describe(label)
            confirmed = None

            if guard["dangerous"]:
                confirmed = safety.confirm(label, reader=confirm_reader)
                if not confirmed:
                    log.append({"ts": time.time(), "session_id": session_id, "step": step,
                                "phase": "safety", "status": "declined_dangerous_action",
                                "label": label, "safety": guard, "confirmed": False,
                                "snapshot": execu.snapshot()})
                    result["status"] = "declined_dangerous_action"
                    result["error"] = {"type": "human_declined",
                                       "message": "危险动作未获人工确认：%s" % label}
                    break

            if not used.act or chosen is None:
                log.append({"ts": time.time(), "session_id": session_id, "step": step,
                            "phase": "plan", "status": "no_action",
                            "decision": used.as_dict(), "label": label,
                            "snapshot": execu.snapshot()})
                result["status"] = "no_action"
                result["error"] = {"type": "no_action",
                                   "message": "决策未指向任何存在的元素编号：%r" % used.act}
                break

            intent = plan_intent(used, chosen, elements, values, task)
            execution = execu.execute(intent["kind"], intent["idx"],
                                      value=intent.get("value"), label=label,
                                      submit=intent.get("submit", True))
            record = {
                "ts": time.time(), "session_id": session_id, "step": step, "phase": "act",
                "task": task, "source": used.source, "decision": used.as_dict(),
                "fallback": fallback, "elements_count": len(elements),
                "label": label, "intent": intent,
                "safety": {"dangerous": guard["dangerous"], "keyword": guard["keyword"],
                           "confirmed": confirmed},
                "execution": execution,
                "snapshot": {"url": execution.get("url"), "title": execution.get("title")},
            }
            log.append(record)
            if on_step is not None:
                try:
                    on_step(record)
                except Exception:
                    pass

            result["steps"] = step
            history.append("step%d: %s %s -> %s" % (step, intent["kind"], label,
                                                    "ok" if execution["ok"] else "fail"))

            if not execution["ok"]:
                consecutive_errors += 1
                if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    result["status"] = "execution_failed"
                    result["error"] = execution.get("error")
                    break
                continue
            consecutive_errors = 0

            if used.finished:
                result["status"] = "finished"
                break

        snap = execu.snapshot()
        result["final"] = snap
        log.append({"ts": time.time(), "session_id": session_id, "phase": "end",
                    "status": result["status"], "steps": result["steps"], "snapshot": snap})
        closed = True
    except Exception as exc:
        result["status"] = "error"
        result["error"] = {"type": type(exc).__name__, "message": str(exc)[:300]}
        try:
            log.append({"ts": time.time(), "session_id": session_id, "phase": "end",
                        "status": "error",
                        "error": {"type": type(exc).__name__, "message": str(exc)[:300]}})
        except Exception:
            pass
    finally:
        try:
            if browser is not None:
                browser.close()
        except Exception:
            pass
        try:
            if playwright is not None:
                playwright.stop()
        except Exception:
            pass
        del closed
    return result


# ---------------------------------------------------------------- 会话状态
def summarize_log(session_id, log_dir="."):
    """给 CLI/MCP 的 status 用：只读汇总，不抛异常。"""
    session_id = str(session_id)
    path = log_path_for(session_id, log_dir)
    summary = {"schema_version": SCHEMA_VERSION, "session_id": session_id,
               "log_path": path, "status": "not_found", "steps": 0,
               "finished": False, "last_step": None, "parse_errors": 0}
    if not os.path.exists(path):
        return summary
    try:
        records = read_log(path)
    except Exception as exc:
        summary["status"] = "unreadable"
        summary["error"] = {"type": type(exc).__name__, "message": str(exc)[:200]}
        return summary

    acts = [r for r in records if r.get("phase") == "act"]
    ends = [r for r in records if r.get("phase") == "end"]
    last = acts[-1] if acts else None
    summary["status"] = ends[-1].get("status") if ends else "no_terminal_record"
    summary["steps"] = len(acts)
    summary["parse_errors"] = sum(1 for r in records if "_parse_error" in r)
    if last:
        summary["finished"] = bool((last.get("decision") or {}).get("finished"))
        summary["last_step"] = {
            "step": last.get("step"),
            "intent": last.get("intent"),
            "ok": bool((last.get("execution") or {}).get("ok")),
            "url": (last.get("execution") or {}).get("url"),
        }
    return summary
