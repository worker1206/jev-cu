"""用**本地 OpenAI 兼容 stub** 验证 LLM 兜底的 wiring（不依赖任何真实 provider，不需要 LLM key）。

定性（务必读清）：
  这是 **wiring 验证** —— 真实 HTTP 往返 + 真实 JSON 解析 + 真实重试 + 真实主循环
  + 真实 chromium 执行 + 真实 fsync 决策日志。
  **它不是 T2 闭环**：真实 provider 的端到端仍待用户提供 LLM key。

安全：
  * stub 只监听 127.0.0.1（临时端口），不落任何凭证，结束即 shutdown + server_close；
  * demo 里使用的 LLM_API_KEY 是字面量 "stub"，绝不读取、也绝不写入任何真实 key；
  * stub 刻意**不记录请求头**（Authorization 之类一律不留），只记录 path/model/选中的编号。

用法：
  python3 examples/llm_stub_demo.py                 # 真实 Jev 判断 + stub 兜底（需 JEV_API_KEY）
  python3 examples/llm_stub_demo.py --fake-jev      # 完全离线（不调 Jev，注入低 margin 信号）
  python3 examples/llm_stub_demo.py --fail-first 1  # 让 stub 先回一次 503，验证真实重试
  python3 examples/llm_stub_demo.py --out-of-range  # 让 stub 回越界编号 999，验证降 margin + element_not_found
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import brain_jev
from core import loop as loop_mod

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE_URL = "file://" + os.path.join(HERE, "local_search_page.html")
LOG_DIR = os.path.dirname(HERE)

STUB_HOST = "127.0.0.1"                 # 只监听回环地址
STUB_PATH = "/v1/chat/completions"
STUB_API_KEY = "stub"                   # 字面量占位，不是任何真实凭证
DEFAULT_TASK = '在本地页面搜索"人工智能"'
PREFER_LABELS = ("搜索", "search")
OUT_OF_RANGE_IDX = "999"
# 等价注入：把路由阈值抬到 1.0 以上，任何 margin（上限就是 1.0）都必然触发升级。
#
# 为什么不用 0.95？因为阈值必须**高于目标页上 Jev 的实际 margin** 才会升级，而各页差异极大：
#   · 本 demo 的极简本地页（2 个候选元素）Jev margin ≈ 0.98 / 1.0 → 0.95 **不会**升级
#     （我最初照抄 0.95，demo 因此静默走了 Jev 自己的判断，排查了一轮）；
#   · 真实维基百科搜索页 Jev margin ≈ 0.80 / 0.85 → 0.95 **确实**会升级。
# 经验规则：强制升级阈值取「该页最大观测 margin 之上」；margin 上限为 1.0，故 1.01 必然升级。
FORCED_ROUTE_T = 1.01

_ACT_LINE_RE = re.compile(r"^\s*(\d+)\.\s*<([^>]+)>\s*(.*)$")
_INPUT_SUFFIX_RE = re.compile(r"\s*\[可输入\]\s*$")


def parse_elements(prompt_text):
    """从 prompt 里解析出候选元素 `编号. <tag> label`。"""
    out = []
    for line in (prompt_text or "").splitlines():
        match = _ACT_LINE_RE.match(line)
        if not match:
            continue
        label = _INPUT_SUFFIX_RE.sub("", match.group(3)).strip()
        out.append({"idx": match.group(1), "tag": match.group(2), "label": label})
    return out


def choose_element(elements, prefer=PREFER_LABELS):
    """优先挑 label 含「搜索/search」的元素，否则挑第一个。"""
    for element in elements:
        low = element["label"].lower()
        if any(mark.lower() in low for mark in prefer):
            return element
    return elements[0] if elements else None


def stub_decision_body(payload, state):
    """stub 的决策内容：{"act","confidence","done","reason"}。"""
    messages = payload.get("messages") or []
    prompt = "\n".join(str(m.get("content", "")) for m in messages)
    chosen = choose_element(parse_elements(prompt))
    if state.get("out_of_range"):
        act = OUT_OF_RANGE_IDX
    else:
        act = chosen["idx"] if chosen else "1"
    return {"act": act, "confidence": 0.9, "done": 0.15, "reason": "stub"}


def make_handler(state):
    class StubHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _send_json(self, code, obj):
            data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length).decode("utf-8") if length else "{}"
            try:
                payload = json.loads(raw)
            except ValueError:
                payload = {}
            state["seen"] += 1
            if state["seen"] <= state["fail_first"]:
                self._send_json(503, {"error": {"message": "no healthy upstream"}})
                return
            if self.path.rstrip("/") != STUB_PATH:
                self._send_json(404, {"error": {"message": "stub 只实现 %s" % STUB_PATH}})
                return
            content = json.dumps(stub_decision_body(payload, state), ensure_ascii=False)
            # 只记录非敏感事实：**不记录任何请求头**（Authorization 一律不留）
            state["requests"].append({"path": self.path, "model": payload.get("model"),
                                      "act": json.loads(content)["act"]})
            self._send_json(200, {"choices": [{"message": {"content": content}}],
                                  "usage": {"prompt_tokens": 1, "completion_tokens": 1}})

        def do_GET(self):
            self._send_json(404, {"error": {"message": "stub 只实现 POST %s" % STUB_PATH}})

        def log_message(self, *args):        # 静音，别污染 demo 输出
            pass

    return StubHandler


class StubServer:
    """本地 OpenAI 兼容 stub：只绑 127.0.0.1 的临时端口。"""

    def __init__(self, fail_first=0, out_of_range=False):
        self.state = {"seen": 0, "requests": [], "fail_first": int(fail_first),
                      "out_of_range": bool(out_of_range)}
        self._httpd = None
        self.port = None

    def start(self):
        self._httpd = ThreadingHTTPServer((STUB_HOST, 0), make_handler(self.state))
        self.port = self._httpd.server_address[1]
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()
        return self.port

    @property
    def host(self):
        """实际绑定的地址（应为 127.0.0.1，绝不能是 0.0.0.0）。"""
        return self._httpd.server_address[0] if self._httpd is not None else None

    @property
    def base_url(self):
        return "http://%s:%d/v1" % (STUB_HOST, self.port)

    @property
    def requests(self):
        return list(self.state["requests"])

    def stop(self):
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None


def fake_jev_ask(task, elements, history=None, **kwargs):
    """离线模式：注入"低 margin"信号，强制走兜底（不调 Jev、不需要 key）。"""
    decision = brain_jev.Decision(act="", confidence=0.0, margin=0.05, done=0.0,
                                 source="jev-fake")
    decision.need_llm = True
    return decision


def run_demo(fail_first=0, out_of_range=False, fake_jev=False, max_steps=None,
             task=DEFAULT_TASK, headless=True, session_id=None):
    from playwright.sync_api import sync_playwright

    if not fake_jev:
        from universal.cli import load_env      # 只 setdefault；绝不回显任何值
        load_env()

    server = StubServer(fail_first=fail_first, out_of_range=out_of_range)
    port = server.start()
    # 指向本地 stub。先设好，随后 load_env 的 setdefault 不会覆盖它们。
    os.environ["LLM_BASE_URL"] = server.base_url
    os.environ["LLM_API_KEY"] = STUB_API_KEY
    os.environ["LLM_MODEL"] = "stub"

    # 决策日志是 append-only：若复用同一个 session_id，日志里会有上一轮的记录。
    # 先量一下已有行数，只回读**本次运行**产生的那一段（避免把历史决策算进来）。
    session_id = session_id or ("stub-%s" % time.strftime("%H%M%S"))
    log_path = loop_mod.log_path_for(session_id, LOG_DIR)
    _before = []
    if os.path.exists(log_path):
        with open(log_path, "r", encoding="utf-8") as _handle:
            _before = [line.strip() for line in _handle if line.strip()]
    log_offset = len(_before)

    saved_route_t = brain_jev.ROUTE_T
    if not fake_jev:
        brain_jev.ROUTE_T = FORCED_ROUTE_T     # 等价注入，仅本次进程生效
    if max_steps is None:
        max_steps = 3 if out_of_range else 2

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=headless)
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            try:
                result = loop_mod.run(
                    task=task, url=PAGE_URL, page=page, max_steps=max_steps,
                    log_dir=LOG_DIR, session_id=session_id,
                    jev_ask=(fake_jev_ask if fake_jev else None))
                page_state = {"url": page.url, "title": page.title(),
                              "search_value": page.input_value("#q"),
                              "status_text": page.inner_text("#status")}
            finally:
                browser.close()
    finally:
        brain_jev.ROUTE_T = saved_route_t
        server.stop()

    raw_lines = []
    if os.path.exists(result["log_path"]):
        with open(result["log_path"], "r", encoding="utf-8") as handle:
            raw_lines = [line.strip() for line in handle if line.strip()]
    raw_lines = raw_lines[log_offset:]                 # 只保留本次运行的行
    records = loop_mod.read_log(result["log_path"])[log_offset:]
    acts = [r for r in records if r.get("phase") == "act"]

    return {"port": port, "mode": "离线(fake-jev)" if fake_jev else "真实 Jev + stub 兜底",
            "stub_requests": server.requests, "result": result, "page": page_state,
            "raw_log_lines": raw_lines, "acts": acts, "max_steps": max_steps}


def main():
    parser = argparse.ArgumentParser(description="本地 stub 验证 LLM 兜底 wiring（不需要 LLM key）")
    parser.add_argument("--fail-first", type=int, default=0, help="前 N 次返回 503 以验证重试")
    parser.add_argument("--out-of-range", action="store_true", help="返回越界编号 999")
    parser.add_argument("--fake-jev", action="store_true", help="完全离线：不调 Jev")
    parser.add_argument("--no-headless", action="store_true")
    parser.add_argument("--session-id", default=None)
    args = parser.parse_args([a for a in sys.argv[1:]])

    run = run_demo(fail_first=args.fail_first, out_of_range=args.out_of_range,
                   fake_jev=args.fake_jev, headless=not args.no_headless,
                   session_id=args.session_id)
    result, acts = run["result"], run["acts"]
    total_retries = sum(int((a.get("decision") or {}).get("retries", 0)) for a in acts)
    sources = sorted({a.get("decision", {}).get("source") for a in acts})
    escalated_need_llm = [a["decision"]["need_llm"] for a in acts]

    if args.out_of_range:
        checks = {
            "越界编号的 margin 被压到 0": all(a["decision"]["margin"] == 0.0 for a in acts) and bool(acts),
            "决策来源是 llm": sources == ["llm"],
            "执行器报 element_not_found": all(
                (a["execution"].get("error") or {}).get("type") == "element_not_found"
                for a in acts) and bool(acts),
            "未被误判为成功": result["status"] != "finished" and result["status"] == "execution_failed",
            "没有真的点到任何元素": run["page"]["search_value"] == "",
        }
        summary = {"status": result["status"], "steps": result["steps"],
                   "margins": [a["decision"]["margin"] for a in acts],
                   "error_types": [(a["execution"].get("error") or {}).get("type") for a in acts]}
    else:
        checks = {
            "决策来源是 llm": sources == ["llm"],
            "llm_fallback.used > 0": result["llm_fallback"]["used"] > 0,
            "升级后不再二次升级(need_llm=False)": all(v is False for v in escalated_need_llm),
            "执行真的成功了": all(a["execution"]["ok"] for a in acts) and bool(acts),
            "LLM 选中的输入框被真实填入": run["page"]["search_value"] == "人工智能",
            "stub 真的收到了 HTTP 请求": len(run["stub_requests"]) >= 1,
            "选中的是含[搜索]的元素": all("搜索" in (a.get("label") or "")
                                          or "search" in (a.get("label") or "").lower()
                                          for a in acts),
        }
        if args.fail_first:
            checks["发生过真实重试(retries>0)"] = total_retries > 0
        summary = {"status": result["status"], "steps": result["steps"],
                   "llm_fallback_used": result["llm_fallback"]["used"],
                   "margin": [a["decision"]["margin"] for a in acts],
                   "need_llm": escalated_need_llm, "total_retries": total_retries,
                   "labels": [a.get("label") for a in acts]}

    payload = {
        "schema_version": "1",
        "定性": "wiring 验证（真实 HTTP/解析/重试/主循环）；不是 T2 闭环，真实 provider 待 key",
        "mode": run["mode"],
        "stub": {"bind": STUB_HOST, "port": run["port"], "path": STUB_PATH,
                 "api_key_used": STUB_API_KEY, "requests": run["stub_requests"]},
        "log_path": result["log_path"],
        "summary": summary,
        "checks": checks,
        "all_passed": all(checks.values()),
        "log_raw": run["raw_log_lines"],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
