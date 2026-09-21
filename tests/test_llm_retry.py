"""LLM 兜底链路的重试：与 Jev **同一套口径**（退避表/抖动/可重试判定）。全 mock，不访问网络。"""
import io
import json
import urllib.error

import pytest

from core import brain_jev, brain_llm

OK_CONTENT = '{"act":"1","confidence":0.9,"done":0.1}'
OK_PAYLOAD = {"choices": [{"message": {"content": OK_CONTENT}}]}


def llm_cfg():
    return {"base_url": "https://llm.invalid/v1", "api_key": "test-llm-key",
            "model": "test-model"}


def http_error(code, body=b"upstream boom"):
    return urllib.error.HTTPError("https://llm.invalid/v1/chat/completions", code, "err",
                                  {}, io.BytesIO(body))


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def fake_urlopen(script):
    """按脚本返回响应或抛错；callable 项每次现造（HTTPError 的 body 只能读一次）。"""
    state = {"calls": 0}

    def _urlopen(request, timeout=None):
        state["calls"] += 1
        item = script[min(state["calls"] - 1, len(script) - 1)]
        if callable(item):
            item = item()
        if isinstance(item, Exception):
            raise item
        return FakeResponse(item)

    return _urlopen, state


def make_transport(monkeypatch, script, slept=None, **kwargs):
    opener, state = fake_urlopen(script)
    monkeypatch.setattr(brain_llm.urllib.request, "urlopen", opener)
    stats = {}
    send = brain_llm.http_transport(llm_cfg(), timeout=1,
                                    sleep=(slept.append if slept is not None else None),
                                    stats=stats, **kwargs)
    return send, state, stats


# ---------------------------------------------------------------- 与 Jev 口径一致
def test_retry_policy_is_literally_shared_with_jev():
    assert brain_llm.is_retryable_status is brain_jev.is_retryable_status
    assert brain_llm.backoff_seconds is brain_jev.backoff_seconds
    assert brain_llm.RETRY_SCHEDULE is brain_jev.RETRY_SCHEDULE
    assert brain_llm.RETRY_JITTER == brain_jev.RETRY_JITTER
    assert brain_llm.LLM_RETRY_MAX == brain_jev.RETRY_MAX == 3
    for code in (429, 500, 502, 503, 504):
        assert brain_llm.is_retryable_status(code) is True, code
    for code in (400, 401, 403, 404, 422):
        assert brain_llm.is_retryable_status(code) is False, code


def test_llm_schedule_matches_documented_values():
    assert brain_jev.RETRY_SCHEDULE == (0.5, 1.5, 4.0)
    assert 0.5 <= brain_llm.backoff_seconds(0) <= 0.625
    assert 1.5 <= brain_llm.backoff_seconds(1) <= 1.875
    assert 4.0 <= brain_llm.backoff_seconds(2) <= 5.0


# ---------------------------------------------------------------- 重试成功
def test_llm_503_retried_then_succeeds(monkeypatch):
    slept = []
    send, state, stats = make_transport(
        monkeypatch, [lambda: http_error(503, b"no healthy upstream"), OK_PAYLOAD], slept)

    text = send(brain_llm.PROBE_MESSAGES, "test-model")

    assert json.loads(text)["act"] == "1"
    assert state["calls"] == 2
    assert stats["retries"] == 1
    assert len(slept) == 1
    assert 0.5 <= slept[0] <= 0.625


def test_llm_429_is_retried(monkeypatch):
    slept = []
    send, state, stats = make_transport(
        monkeypatch, [lambda: http_error(429, b"slow down"), OK_PAYLOAD], slept)

    send(brain_llm.PROBE_MESSAGES, "test-model")

    assert state["calls"] == 2
    assert stats["retries"] == 1
    assert 0.5 <= slept[0] <= 0.625


def test_llm_url_error_is_retried(monkeypatch):
    slept = []
    send, state, stats = make_transport(
        monkeypatch, [urllib.error.URLError("connection reset"), OK_PAYLOAD], slept)

    send(brain_llm.PROBE_MESSAGES, "test-model")

    assert state["calls"] == 2
    assert stats["retries"] == 1


# ---------------------------------------------------------------- 重试耗尽
def test_llm_503_retries_exhausted(monkeypatch):
    slept = []
    send, state, _stats = make_transport(
        monkeypatch, [lambda: http_error(503, b"no healthy upstream")], slept)

    with pytest.raises(brain_llm.LlmError) as excinfo:
        send(brain_llm.PROBE_MESSAGES, "test-model")

    error = excinfo.value
    assert error.status == 503
    assert error.retries == brain_llm.LLM_RETRY_MAX == 3
    assert state["calls"] == 4
    assert len(slept) == 3
    assert 0.5 <= slept[0] <= 0.625
    assert 1.5 <= slept[1] <= 1.875
    assert 4.0 <= slept[2] <= 5.0
    assert "已重试 3 次" in str(error)
    assert "no healthy upstream" in str(error)


def test_llm_url_error_exhausted(monkeypatch):
    slept = []
    send, state, _stats = make_transport(
        monkeypatch, [urllib.error.URLError("dns fail")], slept)

    with pytest.raises(brain_llm.LlmError) as excinfo:
        send(brain_llm.PROBE_MESSAGES, "test-model")

    assert excinfo.value.retries == 3
    assert excinfo.value.status is None
    assert "网络不可达" in str(excinfo.value)


# ---------------------------------------------------------------- 不重试
@pytest.mark.parametrize("code", [400, 401, 403, 404, 422])
def test_llm_4xx_is_not_retried(monkeypatch, code):
    slept = []
    send, state, _stats = make_transport(monkeypatch, [lambda: http_error(code)], slept)

    with pytest.raises(brain_llm.LlmError) as excinfo:
        send(brain_llm.PROBE_MESSAGES, "test-model")

    assert excinfo.value.status == code
    assert excinfo.value.retries == 0
    assert state["calls"] == 1
    assert slept == []
    assert "已重试" not in str(excinfo.value)


def test_llm_max_retries_zero_single_attempt(monkeypatch):
    slept = []
    send, state, _stats = make_transport(monkeypatch, [lambda: http_error(503)], slept,
                                         max_retries=0)

    with pytest.raises(brain_llm.LlmError):
        send(brain_llm.PROBE_MESSAGES, "test-model")

    assert state["calls"] == 1
    assert slept == []


# ---------------------------------------------------------------- 预算熔断
def test_llm_budget_trips_and_blocks_further_requests(monkeypatch):
    budget = brain_jev.RetryBudget(limit=2)
    slept = []
    send, state, _stats = make_transport(monkeypatch, [lambda: http_error(503)], slept,
                                         budget=budget)

    with pytest.raises(brain_llm.LlmError) as excinfo:
        send(brain_llm.PROBE_MESSAGES, "test-model")

    assert excinfo.value.budget_exceeded is True
    assert excinfo.value.status == 503
    assert budget.tripped is True
    assert budget.spent == 2
    assert state["calls"] == 3          # 首发 + 2 次预算内重试

    # 熔断后再调用：一次请求都不发
    send2, state2, _stats2 = make_transport(monkeypatch, [OK_PAYLOAD], [], budget=budget)
    with pytest.raises(brain_llm.LlmError) as excinfo2:
        send2(brain_llm.PROBE_MESSAGES, "test-model")

    assert state2["calls"] == 0
    assert excinfo2.value.budget_exceeded is True
    assert "熔断" in str(excinfo2.value)


def test_llm_budget_allows_retries_within_limit(monkeypatch):
    budget = brain_jev.RetryBudget(limit=8)
    slept = []
    send, state, stats = make_transport(monkeypatch, [lambda: http_error(503), OK_PAYLOAD],
                                        slept, budget=budget)

    send(brain_llm.PROBE_MESSAGES, "test-model")

    assert state["calls"] == 2
    assert budget.spent == 1
    assert budget.tripped is False


# ---------------------------------------------------------------- ask() 集成
def test_llm_ask_records_retries_on_decision(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "https://llm.invalid/v1")
    monkeypatch.setenv("LLM_API_KEY", "test-llm-key")
    monkeypatch.setenv("LLM_MODEL", "test-model")
    opener, state = fake_urlopen([lambda: http_error(503), OK_PAYLOAD])
    monkeypatch.setattr(brain_llm.urllib.request, "urlopen", opener)
    slept = []

    decision = brain_llm.ask("任务", [{"idx": 1, "tag": "button", "label": "Go",
                                       "is_input": False}], sleep=slept.append)

    assert decision.source == "llm"
    assert decision.act == "1"
    assert decision.retries == 1
    assert state["calls"] == 2


def test_llm_ask_injected_transport_records_zero_retries(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "https://llm.invalid/v1")
    monkeypatch.setenv("LLM_API_KEY", "test-llm-key")

    decision = brain_llm.ask("任务", [{"idx": 1, "tag": "button", "label": "Go",
                                       "is_input": False}],
                             transport=lambda messages, model: OK_CONTENT)

    assert decision.act == "1"
    assert decision.retries == 0
