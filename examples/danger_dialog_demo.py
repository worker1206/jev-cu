"""弹窗内危险动作 + 连续多个危险动作的安全门演练（纯本地页面，绝不触碰真实站点）。

覆盖上一版没覆盖的两件事：
  A) **弹窗内**的危险动作：点开对话框 → 在弹窗里填字 → 点弹窗内的「发送留言」→ 被拒绝
  B) **连续多个**危险动作：同一 session 内先「发送留言」再「删除草稿」，两次都批准 → 两次都执行
  C) 连续两个危险动作里只批准第一个：第一个生效、第二个被拦下（证明**逐个动作**都要过门）

三种情形都跑完整主循环（真实 chromium + 真实执行器 + 真实 fsync 决策日志）。

每次运行都用**带时间戳的新 session_id**：决策日志是 append-only 的，
复用同一个 session_id 会把上一轮记录一并读回来（曾把关键词读成两遍）。


用法：
  python3 examples/danger_dialog_demo.py
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from playwright.sync_api import sync_playwright

from core import brain_jev
from core import loop as loop_mod

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE_URL = "file://" + os.path.join(HERE, "local_dialog_flow.html")
TASK = "打开留言对话框，填写留言内容，发送留言，然后删除草稿"
TARGETS = ["打开留言对话框", "留言内容", "发送留言", "删除草稿"]
VALUES = ["你好"]
MAX_STEPS = 4
LOG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def label_jev(targets):
    """脚本化决策：按 label 子串找编号（编号随弹窗开合变化，不能写死）。"""

    def _ask(task, elements, history=None, **kwargs):
        i = _ask.state["i"]
        want = targets[min(i, len(targets) - 1)]
        _ask.state["i"] += 1
        chosen = next((e for e in elements if want in str(e.get("label", ""))), elements[0])
        decision = brain_jev.Decision(act=str(chosen["idx"]), confidence=1.0,
                                      margin=0.99, done=0.0, source="scripted")
        decision.finished = False
        return decision

    _ask.state = {"i": 0}
    return _ask


def make_reader(approve_marks, prompts):
    """按提示词内容决定批不批：命中 approve_marks 才回 y。"""

    def reader(prompt):
        prompts.append(prompt)
        return "y" if any(mark in prompt for mark in approve_marks) else "n"

    return reader


def run_case(session_id, approve_marks):
    prompts = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        try:
            result = loop_mod.run(
                task=TASK, url=PAGE_URL, page=page, max_steps=MAX_STEPS,
                log_dir=LOG_DIR, session_id=session_id, values=VALUES,
                confirm_reader=make_reader(approve_marks, prompts),
                jev_ask=label_jev(TARGETS))
            page_state = {
                "status_text": page.inner_text("#status"),
                "draft_text": page.inner_text("#draft"),
                "dialog_open": page.eval_on_selector(
                    "#dialog", "e => e.classList.contains('open')"),
                "message_value": page.input_value("#message"),
            }
        finally:
            browser.close()

    records = loop_mod.read_log(result["log_path"])
    return {
        "session_id": session_id,
        "approved_marks": list(approve_marks),
        "status": result["status"],
        "steps": result["steps"],
        "step_kinds": [(r["intent"]["kind"], r["label"]) for r in records
                       if r.get("phase") == "act"],
        # 已执行的危险动作（approved 后会落成 act 记录）
        "dangerous_executed": [r["safety"]["keyword"] for r in records
                               if r.get("phase") == "act" and r["safety"]["dangerous"]],
        # 被安全门拦下的危险动作（declined 时落成 phase=safety 记录，没有 act 记录）
        "dangerous_gated": [r["safety"]["keyword"] for r in records
                            if r.get("phase") == "safety"],
        "gate_records": [{"keyword": r["safety"]["keyword"], "confirmed": r["confirmed"]}
                         for r in records if r.get("phase") == "safety"],
        "confirm_prompted": len(prompts),
        "page": page_state,
        "error": result["error"],
    }


def main():
    started = time.time()
    stamp = time.strftime("%H%M%S")
    case_a = run_case("dialog-decline-%s" % stamp, approve_marks=())            # 全拒绝
    case_b = run_case("dialog-approve-all-%s" % stamp,
                      approve_marks=("发送", "删除"))                            # 连续两个都批准
    case_c = run_case("dialog-approve-first-%s" % stamp,
                      approve_marks=("发送",))                                   # 只批准第一个

    checks = {
        # A) 弹窗内危险动作被拒绝：弹窗仍开着、留言没发出去、但填字动作已生效
        "弹窗内危险动作：停在安全门": case_a["status"] == "declined_dangerous_action",
        "弹窗内危险动作：被门拦下[发送]": case_a["dangerous_gated"] == ["发送"],
        "弹窗内危险动作：没有危险动作被执行": case_a["dangerous_executed"] == [],
        "弹窗内危险动作：弹窗仍打开": case_a["page"]["dialog_open"] is True,
        "弹窗内危险动作：留言未发出": case_a["page"]["status_text"] == "对话框已打开",
        "弹窗内危险动作：填字已生效": case_a["page"]["message_value"] == "你好",
        "弹窗内危险动作：只问了 1 次人工": case_a["confirm_prompted"] == 1,
        # B) 连续两个危险动作都批准：两次都真的执行
        "连续危险动作：两次都执行了": case_b["dangerous_executed"] == ["发送", "删除"],
        "连续危险动作：逐个动作各问一次": case_b["confirm_prompted"] == 2,
        "连续危险动作：没有动作被拦": case_b["dangerous_gated"] == [],
        "连续危险动作：留言已发送过": case_b["page"]["draft_text"] == "草稿已删除",
        "连续危险动作：最后一个动作生效": case_b["page"]["status_text"] == "草稿已删除",
        "连续危险动作：走满 4 步": case_b["steps"] == MAX_STEPS,
        # C) 只批准第一个：第一个生效、第二个被拦
        "逐个过门：第一个危险动作被批准并执行": case_c["dangerous_executed"] == ["发送"],
        "逐个过门：第一个生效（状态已变）": case_c["page"]["status_text"] == "留言已发送",
        "逐个过门：第二个被拦下": case_c["dangerous_gated"] == ["删除"],
        "逐个过门：停在安全门": case_c["status"] == "declined_dangerous_action",
        "逐个过门：草稿未被删除": case_c["page"]["draft_text"] == "草稿内容：待发送的留言",
        "逐个过门：仍问了 2 次人工": case_c["confirm_prompted"] == 2,
    }

    print(json.dumps({
        "schema_version": "1",
        "page": PAGE_URL,
        "cases": [case_a, case_b, case_c],
        "checks": checks,
        "all_passed": all(checks.values()),
        "duration_s": round(time.time() - started, 2),
    }, ensure_ascii=False, indent=2))
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
