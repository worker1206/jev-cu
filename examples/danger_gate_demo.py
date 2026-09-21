"""危险动作安全门端到端演练（本地页面，绝不触碰真实站点）。

对同一张本地表单跑两遍完整主循环（真实 chromium、真实执行器、真实决策日志）：
  A) 人工拒绝 → 必须停在 declined_dangerous_action，页面状态仍是「尚未提交」
  B) 人工批准 → 允许执行，页面状态变为「已下单成功」

用法：
  python3 examples/danger_gate_demo.py              # 脚本化决策，离线确定
  python3 examples/danger_gate_demo.py --real-jev   # 真实 Jev API（需 JEV_API_KEY）
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
PAGE_URL = "file://" + os.path.join(HERE, "local_order_form.html")
TASK = "在订单表单填写姓名和电话，然后提交订单"
TARGETS = ["姓名", "电话", "提交订单"]
VALUES = ["张三", "13800000000"]
MAX_STEPS = 3
LOG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def label_jev(targets):
    """脚本化决策：按 label 子串找编号（编号会随页面变化，不能写死）。"""

    def _ask(task, elements, history=None, **kwargs):
        idx = _ask.state["i"]
        want = targets[min(idx, len(targets) - 1)]
        _ask.state["i"] += 1
        chosen = next((e for e in elements if want in str(e.get("label", ""))), elements[0])
        decision = brain_jev.Decision(act=str(chosen["idx"]), confidence=1.0,
                                      margin=0.99, done=0.0, source="scripted")
        decision.finished = False
        return decision

    _ask.state = {"i": 0}
    return _ask


def run_case(session_id, approve, offline):
    prompts = []

    def reader(prompt):
        prompts.append(prompt)
        return "y" if approve else "n"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        try:
            result = loop_mod.run(
                task=TASK, url=PAGE_URL, page=page, max_steps=MAX_STEPS,
                log_dir=LOG_DIR, session_id=session_id, values=VALUES,
                confirm_reader=reader,
                jev_ask=None if not offline else label_jev(TARGETS),
            )
            status_text = page.inner_text("#status")
            button_disabled = page.eval_on_selector("#submit-order", "e => e.disabled")
        finally:
            browser.close()

    records = loop_mod.read_log(result["log_path"])
    return {
        "session_id": session_id,
        "human_decision": "批准" if approve else "拒绝",
        "status": result["status"],
        "steps": result["steps"],
        "error": result["error"],
        "page_status_text": status_text,
        "submit_button_disabled": button_disabled,
        "confirm_prompted": len(prompts),
        "safety_keyword": next((r["safety"]["keyword"] for r in records
                                if r.get("phase") == "safety"), None),
        "steps_detail": [{"step": r["step"], "intent": r["intent"]["kind"],
                          "label": r["label"], "value": r["intent"].get("value"),
                          "dangerous": r["safety"]["dangerous"],
                          "executed_ok": r["execution"]["ok"]}
                         for r in records if r.get("phase") == "act"],
        "log_path": result["log_path"],
    }


def main():
    offline = "--real-jev" not in sys.argv
    if not offline and not os.environ.get("JEV_API_KEY"):
        print(json.dumps({"error": "缺少 JEV_API_KEY；去掉 --real-jev 可跑离线脚本化决策"},
                         ensure_ascii=False, indent=2))
        return 2

    started = time.time()
    decline = run_case("demo-decline", approve=False, offline=offline)
    approve = run_case("demo-approve", approve=True, offline=offline)

    checks = {
        "拒绝后停在安全门": decline["status"] == "declined_dangerous_action",
        "拒绝后页面未被改动": decline["page_status_text"] == "尚未提交",
        "拒绝后按钮仍可用": decline["submit_button_disabled"] is False,
        "拒绝时确实问了人工": decline["confirm_prompted"] >= 1,
        "拒绝时危险词被识别": decline["safety_keyword"] == "提交",
        "批准后动作真的执行了": approve["page_status_text"] == "已下单成功",
        "批准后按钮被禁用": approve["submit_button_disabled"] is True,
        "批准后没有安全门拦截记录": approve["safety_keyword"] is None,
    }

    print(json.dumps({
        "schema_version": "1",
        "mode": "脚本化决策（离线）" if offline else "真实 Jev API",
        "page": PAGE_URL,
        "cases": [decline, approve],
        "checks": checks,
        "all_passed": all(checks.values()),
        "duration_s": round(time.time() - started, 2),
    }, ensure_ascii=False, indent=2))
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
