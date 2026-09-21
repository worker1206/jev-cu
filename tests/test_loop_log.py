"""DecisionLog 落盘契约 + 主循环/执行器的结构化错误契约。全 mock，不访问真实 API 与网络。"""
import json
import os

from core import brain_jev, loop
from core.executor import Executor


# ---------------------------------------------------------------- 假对象
class FakeElement:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def click(self, timeout=None):
        self.calls.append("click")
        if self.error:
            raise self.error

    def fill(self, value, timeout=None):
        self.calls.append(("fill", value))
        if self.error:
            raise self.error

    def press(self, key, timeout=None):
        self.calls.append(("press", key))
        if self.error:
            raise self.error

    def is_visible(self):
        return True


class FakePage:
    """够用的假页面：executor 只用 query_selector / wait_for_timeout / title / url。"""

    def __init__(self, element=None, elements=None, title="Fake", url="https://fake.test/",
                 missing=False):
        # missing=True 用来模拟"编号在页面上不存在"；否则默认给一个可点击的假元素
        self.element = element if (element is not None or missing) else FakeElement()
        self.elements = elements or []
        self.waits = []
        self._title = title
        self.url = url

    def query_selector(self, selector):
        return self.element

    def query_selector_all(self, selector):
        return []

    def wait_for_timeout(self, ms):
        self.waits.append(ms)

    def wait_for_load_state(self, state, timeout=None):
        return None

    def title(self):
        return self._title

    def evaluate(self, js):
        return [dict(e) for e in self.elements]

    def goto(self, url, wait_until=None, timeout=None):
        self.url = url


def fixed_decision(**kw):
    data = dict(act="1", confidence=1.0, margin=0.9, done=0.0, source="jev")
    data.update(kw)
    return brain_jev.Decision(**data)


# ---------------------------------------------------------------- DecisionLog
def test_log_write_then_read_back_immediately(tmp_path):
    log = loop.DecisionLog("s1", base_dir=str(tmp_path))
    path = log.append({"step": 1, "phase": "act", "decision": {"act": "3"}})

    assert os.path.exists(path)
    records = loop.read_log(path)                 # append 返回即读回，说明已落盘
    assert len(records) == 1
    assert records[0]["decision"]["act"] == "3"


def test_log_append_does_not_overwrite(tmp_path):
    log = loop.DecisionLog("s2", base_dir=str(tmp_path))
    for step in (1, 2, 3):
        log.append({"step": step, "phase": "act"})

    with open(log.path, "r", encoding="utf-8") as fh:
        lines = [ln for ln in fh.read().splitlines() if ln.strip()]
    assert len(lines) == 3
    assert [json.loads(ln)["step"] for ln in lines] == [1, 2, 3]
    assert log.read_all()[-1]["step"] == 3


def test_log_path_is_under_jev_cu_dir(tmp_path):
    log = loop.DecisionLog("s3", base_dir=str(tmp_path))
    assert log.path == os.path.join(str(tmp_path), ".jev-cu", "log", "s3.jsonl")


def test_log_creates_missing_directories(tmp_path):
    log = loop.DecisionLog("deep", base_dir=str(tmp_path / "a" / "b"))
    log.append({"phase": "act", "step": 1})
    assert os.path.exists(log.path)


def test_read_log_tolerates_broken_line(tmp_path):
    path = tmp_path / "broken.jsonl"
    path.write_text('{"step": 1}\nnot json\n{"step": 2}\n', encoding="utf-8")
    records = loop.read_log(str(path))
    assert len(records) == 3
    assert records[0]["step"] == 1
    assert "_parse_error" in records[1]
    assert records[2]["step"] == 2


def test_read_log_missing_file_returns_empty(tmp_path):
    assert loop.read_log(str(tmp_path / "nope.jsonl")) == []


def test_summarize_log_not_found(tmp_path):
    summary = loop.summarize_log("ghost", log_dir=str(tmp_path))
    assert summary["status"] == "not_found"
    assert summary["steps"] == 0


def test_summarize_log_reports_terminal_status(tmp_path):
    log = loop.DecisionLog("s4", base_dir=str(tmp_path))
    log.append({"step": 1, "phase": "act", "intent": {"kind": "click", "idx": "1"},
                "decision": {"finished": True}, "execution": {"ok": True, "url": "u"}})
    log.append({"phase": "end", "status": "finished", "steps": 1})

    summary = loop.summarize_log("s4", log_dir=str(tmp_path))
    assert summary["status"] == "finished"
    assert summary["steps"] == 1
    assert summary["finished"] is True
    assert summary["last_step"]["intent"]["idx"] == "1"


# ---------------------------------------------------------------- 执行器错误契约
def test_executor_returns_structured_error_instead_of_raising():
    page = FakePage(element=FakeElement(error=RuntimeError("boom")))
    result = Executor(page).execute("click", "3", label="<button> Go")

    assert result["ok"] is False
    assert result["error"]["type"] == "RuntimeError"
    assert "boom" in result["error"]["message"]
    assert result["idx"] == "3"
    assert result["label"] == "<button> Go"


def test_executor_reports_missing_element():
    result = Executor(FakePage(missing=True)).execute("click", "9")
    assert result["ok"] is False
    assert result["error"]["type"] == "element_not_found"


def test_executor_reports_unknown_action():
    result = Executor(FakePage(element=FakeElement())).execute("teleport", "1")
    assert result["ok"] is False
    assert result["error"]["type"] == "unknown_action"


def test_executor_reports_dialog_input_not_found():
    result = Executor(FakePage(element=FakeElement())).execute("dialog_search", "1", value="jev")
    assert result["ok"] is False
    assert result["error"]["type"] == "dialog_input_not_found"


def test_executor_success_shape_and_fill_clicks_first():
    element = FakeElement()
    page = FakePage(element=element, title="T", url="https://fake.test/x")
    result = Executor(page).execute("fill", "2", value="人工智能", label="<input> 搜索")

    assert result["ok"] is True
    assert result["error"] is None
    assert result["url"] == "https://fake.test/x"
    assert result["title"] == "T"
    assert element.calls[0] == "click"              # fill 前必须先 click 聚焦
    assert ("fill", "人工智能") in element.calls
    assert page.waits, "每个动作后都要等待稳定"


# ---------------------------------------------------------------- 主循环（假页面 + 假 Jev）
def test_loop_runs_one_step_and_logs_each_step(tmp_path):
    elements = [{"idx": 1, "tag": "button", "label": "Search", "is_input": False}]
    page = FakePage(elements=elements)

    def fake_jev(task, els, history=None, **kw):
        return fixed_decision(act="1", done=0.6, finished=True)

    result = loop.run(task="搜索", url="https://fake.test/", page=page, max_steps=3,
                      log_dir=str(tmp_path), session_id="sess-a", jev_ask=fake_jev)

    assert result["schema_version"] == "1"
    assert result["status"] == "finished"
    assert result["steps"] == 1

    records = loop.read_log(result["log_path"])
    assert [r["phase"] for r in records] == ["act", "end"]
    assert records[0]["decision"]["act"] == "1"
    assert records[0]["execution"]["ok"] is True
    assert records[0]["elements_count"] == 1

    summary = loop.summarize_log("sess-a", log_dir=str(tmp_path))
    assert summary["status"] == "finished"
    assert summary["steps"] == 1


def test_loop_stops_when_human_declines_dangerous_action(tmp_path):
    elements = [{"idx": 1, "tag": "button", "label": "提交订单", "is_input": False}]
    page = FakePage(elements=elements)
    prompts = []

    def reader(prompt):
        prompts.append(prompt)
        return "n"

    result = loop.run(task="下单", page=page, max_steps=3, log_dir=str(tmp_path),
                      session_id="sess-b", confirm_reader=reader,
                      jev_ask=lambda task, els, history=None, **kw: fixed_decision(act="1"))

    assert result["status"] == "declined_dangerous_action"
    assert result["error"]["type"] == "human_declined"
    assert prompts, "危险动作必须走人工确认"
    assert page.waits == [], "被拒绝后不得执行任何动作"

    records = loop.read_log(result["log_path"])
    assert records[0]["phase"] == "safety"
    assert records[0]["safety"]["keyword"] == "提交"


def test_loop_records_llm_fallback_decision(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "https://llm.invalid/v1")
    monkeypatch.setenv("LLM_API_KEY", "test-not-a-real-key")
    elements = [{"idx": 1, "tag": "button", "label": "Next", "is_input": False}]
    page = FakePage(elements=elements)

    def low_margin_jev(task, els, history=None, **kw):
        decision = fixed_decision(act="1")
        decision.need_llm = True            # margin 低于阈值 → 触发兜底
        return decision

    def fake_llm(task, els, history=None, **kw):
        return fixed_decision(act="1", source="llm", done=0.9, finished=True)

    result = loop.run(task="翻页", page=page, max_steps=2, log_dir=str(tmp_path),
                      session_id="sess-c", jev_ask=low_margin_jev, llm_ask=fake_llm)

    assert result["status"] == "finished"
    record = loop.read_log(result["log_path"])[0]
    assert record["decision"]["source"] == "llm"
    assert record["fallback"]["used"] is True
    assert result["llm_fallback"]["used"] == 1


def test_loop_returns_structured_error_when_jev_call_fails(tmp_path):
    elements = [{"idx": 1, "tag": "button", "label": "Go", "is_input": False}]
    page = FakePage(elements=elements)

    def broken_jev(task, els, history=None, **kw):
        raise RuntimeError("JEV_API_KEY 缺失")

    result = loop.run(task="x", page=page, max_steps=2, log_dir=str(tmp_path),
                      session_id="sess-d", jev_ask=broken_jev)

    assert result["status"] == "error"
    assert result["error"]["type"] == "jev_call_failed"
    assert "JEV_API_KEY" in result["error"]["message"]
