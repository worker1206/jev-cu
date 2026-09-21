"""LLM 兜底 wiring 验证：本地 OpenAI 兼容 stub + 真实 HTTP + 真实主循环。

定性：**wiring 验证**，不是 T2 闭环——真实 provider 端到端仍待 LLM key。
本文件不访问任何真实 provider，也不需要真实 LLM key（stub 的 key 就是字面量 "stub"）。

无浏览器的用例（越界 margin、stub 形状）在任何环境都能跑；
涉及真实 chromium 的用例在浏览器不可用时 pytest.skip。
"""
import contextlib
import importlib.util
import json
import os
import urllib.request

import pytest

from core import brain_llm
from core import loop as loop_mod

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STUB_PATH = os.path.join(REPO_ROOT, "examples", "llm_stub_demo.py")

PROMPT_WITH_ELEMENTS = (
    "任务：在本地页面搜索\"人工智能\"\n\n"
    "当前页面候选元素（编号即操作目标）：\n"
    "  1. <input> 搜索 [可输入]\n"
    "  2. <button> 搜索\n"
    "请只输出 JSON。"
)


def _load_stub():
    """按文件路径加载 examples/llm_stub_demo.py（它 import 时无任何副作用）。"""
    spec = importlib.util.spec_from_file_location("llm_stub_demo", STUB_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


STUB = _load_stub()


@contextlib.contextmanager
def _page_or_skip():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    try:
        ctx = sync_playwright().start()
        browser = ctx.chromium.launch(headless=True)
    except Exception as exc:                      # 只有环境不可用才 skip
        pytest.skip("真实浏览器不可用：%s" % exc)

    try:
        yield browser.new_page(viewport={"width": 1280, "height": 800})
    finally:
        try:
            browser.close()
        finally:
            ctx.stop()


@contextlib.contextmanager
def _stub_env(monkeypatch, **kwargs):
    server = STUB.StubServer(**kwargs)
    server.start()
    try:
        monkeypatch.setenv("LLM_BASE_URL", server.base_url)
        monkeypatch.setenv("LLM_API_KEY", STUB.STUB_API_KEY)
        monkeypatch.setenv("LLM_MODEL", "stub")
        yield server
    finally:
        server.stop()


def _acts(log_path):
    return [r for r in loop_mod.read_log(log_path) if r.get("phase") == "act"]


# ---------------------------------------------------------------- stub 自身契约
def test_stub_server_binds_loopback_only():
    server = STUB.StubServer()
    server.start()
    try:
        assert server.host == "127.0.0.1", "stub 绝不能绑 0.0.0.0"
        assert server.port > 0
    finally:
        server.stop()
    assert server.host is None, "stop() 后端口应已释放"


def test_stub_serves_openai_compatible_shape():
    server = STUB.StubServer()
    server.start()
    try:
        body = json.dumps({"model": "stub",
                           "messages": [{"role": "user", "content": PROMPT_WITH_ELEMENTS}]}
                          ).encode("utf-8")
        request = urllib.request.Request(
            server.base_url + "/chat/completions", data=body,
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.load(response)

        content = payload["choices"][0]["message"]["content"]
        assert isinstance(content, str), "content 必须是 JSON **字符串**"
        parsed = json.loads(content)
        assert set(parsed) == {"act", "confidence", "done", "reason"}
        assert parsed["reason"] == "stub"
        assert parsed["act"] == "1"                  # 选中 label 含「搜索」的输入框
        assert server.requests and server.requests[0]["path"] == "/v1/chat/completions"
        # 只记录非敏感事实：不得记录请求头（不落凭证）
        assert all("headers" not in item for item in server.requests)
    finally:
        server.stop()


def test_stub_picks_search_element_then_first():
    elements = STUB.parse_elements(PROMPT_WITH_ELEMENTS)
    assert [e["idx"] for e in elements] == ["1", "2"]
    assert STUB.choose_element(elements)["idx"] == "1"
    # 没有「搜索/search」时退回第一个
    other = [{"idx": "7", "tag": "button", "label": "提交订单"},
             {"idx": "8", "tag": "a", "label": "返回"}]
    assert STUB.choose_element(other)["idx"] == "7"
    assert STUB.choose_element([]) is None


# ---------------------------------------------------------------- 越界编号：margin 必须为 0
@pytest.mark.parametrize("act,expected", [("999", 0.0), ("", 0.0), ("2", 0.8)])
def test_parse_llm_answer_margin_by_range(act, expected):
    decision = brain_llm.parse_llm_answer(
        '{"act":"%s","confidence":0.9,"done":0.15}' % act, valid_ids=[1, 2, 3])
    assert decision.margin == expected


def test_out_of_range_act_must_not_look_confident():
    """回归：越界编号曾算出 margin=1.0（最高置信）——修复后必须为 0。"""
    decision = brain_llm.parse_llm_answer('{"act":"999","confidence":0.9}', valid_ids=[1, 2, 3])
    assert decision.margin == 0.0
    assert decision.confidence == 0.0
    assert decision.probabilities == {"999": 0.0, "__rest__": 0.0}
    assert decision.act == "999"          # 原始编号保留，日志里能看出 LLM 给了什么


def test_out_of_range_checked_against_valid_ids_only_when_provided():
    """不传 valid_ids 时不做越界判定（保持向后兼容）。"""
    decision = brain_llm.parse_llm_answer('{"act":"999","confidence":0.9}')
    assert decision.margin == 0.8


# ---------------------------------------------------------------- 真实主循环 wiring
def test_llm_fallback_wiring_through_real_loop(tmp_path, monkeypatch):
    with _stub_env(monkeypatch) as server:
        with _page_or_skip() as page:
            result = loop_mod.run(task=STUB.DEFAULT_TASK, url=STUB.PAGE_URL, page=page,
                                  max_steps=1, log_dir=str(tmp_path), session_id="wiring",
                                  jev_ask=STUB.fake_jev_ask)
            filled = page.input_value("#q")

        acts = _acts(result["log_path"])

    assert len(acts) == 1
    record = acts[0]
    assert record["decision"]["source"] == "llm"          # 决策确实换成了 LLM
    assert record["decision"]["need_llm"] is False        # 升级后不再二次升级
    assert record["fallback"]["used"] is True
    assert record["label"] == "<input> 搜索"              # stub 按规则挑中的元素
    assert record["execution"]["ok"] is True
    assert filled == "人工智能"                            # 真实 chromium 里真填进去了
    assert result["llm_fallback"]["used"] == 1
    assert len(server.requests) == 1                      # 真实 HTTP 走了一次


def test_llm_fallback_records_real_retry_after_503(tmp_path, monkeypatch):
    """stub 首次回 503 → 真实重试路径生效 → Decision.retries == 1。"""
    with _stub_env(monkeypatch, fail_first=1) as server:
        with _page_or_skip() as page:
            result = loop_mod.run(task=STUB.DEFAULT_TASK, url=STUB.PAGE_URL, page=page,
                                  max_steps=1, log_dir=str(tmp_path), session_id="wiring-retry",
                                  jev_ask=STUB.fake_jev_ask)
        acts = _acts(result["log_path"])

    assert acts[0]["decision"]["retries"] == 1
    assert acts[0]["execution"]["ok"] is True
    assert len(server.requests) == 1                      # 503 那次不计入成功请求


def test_stub_out_of_range_zero_margin_and_element_not_found(tmp_path, monkeypatch):
    with _stub_env(monkeypatch, out_of_range=True):
        with _page_or_skip() as page:
            result = loop_mod.run(task=STUB.DEFAULT_TASK, url=STUB.PAGE_URL, page=page,
                                  max_steps=3, log_dir=str(tmp_path), session_id="wiring-oob",
                                  jev_ask=STUB.fake_jev_ask)
            filled = page.input_value("#q")

        acts = _acts(result["log_path"])

    assert acts, "越界编号也应留下 act 记录（不是静默跳过）"
    assert all(a["decision"]["act"] == STUB.OUT_OF_RANGE_IDX for a in acts)
    assert all(a["decision"]["margin"] == 0.0 for a in acts)          # margin 被压到 0
    assert all(a["decision"]["source"] == "llm" for a in acts)
    assert all((a["execution"].get("error") or {}).get("type") == "element_not_found"
               for a in acts)                                          # 执行器给出结构化错误
    assert result["status"] == "execution_failed"                      # 连续失败 → 不是成功
    assert result["status"] != "finished"
    assert filled == ""                                                # 没有真的操作到任何元素


def test_empty_act_still_reports_no_action(tmp_path, monkeypatch):
    """没给编号（act 为空）与给了越界编号是两种可区分的终态。"""
    def empty_act_jev(task, elements, history=None, **kwargs):
        decision = brain_llm.parse_llm_answer('{"act":"","confidence":0.9,"done":0.1}',
                                              valid_ids=[e["idx"] for e in elements])
        return decision

    with _page_or_skip() as page:
        result = loop_mod.run(task="x", url=STUB.PAGE_URL, page=page, max_steps=2,
                              log_dir=str(tmp_path), session_id="wiring-empty",
                              jev_ask=empty_act_jev)

    assert result["status"] == "no_action"
    records = loop_mod.read_log(result["log_path"])
    assert records[0]["phase"] == "plan"
    assert records[0]["status"] == "no_action"
