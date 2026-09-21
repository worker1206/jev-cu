"""Jev 瞬态错误重试：5xx/429/URLError 有界重试 + 指数退避；4xx 不重试。全 mock，不访问网络。"""
import io
import urllib.error

import pytest

from core import brain_jev

ELEMENTS = [
    {"idx": 1, "tag": "input", "label": "搜索", "is_input": True},
    {"idx": 2, "tag": "button", "label": "Search", "is_input": False},
]

OK_PAYLOAD = {
    "answers": {
        "act": {"choice": "2", "confidence": 1.0, "probabilities": {"2": 0.9, "1": 0.1}},
        "conf": {"score": 1.0},
        "done": {"noul": 0.1},
    },
    "usage": {"input_tokens": 913, "output_tokens": 10},
}


def http_error(code, body=b"upstream boom"):
    return urllib.error.HTTPError("https://api.invalid/v1/systemone", code, "err",
                                  {}, io.BytesIO(body))


class Sender:
    """按脚本抛错或返回；记录调用次数与每次的请求头。

    脚本项若为 callable 则每次调用时现造（HTTPError 的 body 只能读一次，
    复用同一个实例会让第二次读到的 body 变成空）。
    """

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0
        self.headers = []

    def __call__(self, url, body, headers, timeout):
        self.calls += 1
        self.headers.append(dict(headers))
        item = self.script[min(self.calls - 1, len(self.script) - 1)]
        if callable(item):
            item = item()
        if isinstance(item, Exception):
            raise item
        return item


def ask(sender, slept, **kwargs):
    return brain_jev.ask("测试任务", ELEMENTS, api_key="test-key",
                         base_url="https://api.invalid", sender=sender,
                         sleep=slept.append, **kwargs)


# ---------------------------------------------------------------- 可重试分类
def test_retryable_status_classification():
    assert brain_jev.is_retryable_status(429) is True
    for code in (500, 502, 503, 504, 599):
        assert brain_jev.is_retryable_status(code) is True, code
    for code in (400, 401, 403, 404, 422):
        assert brain_jev.is_retryable_status(code) is False, code


def test_backoff_bounded_and_growing():
    assert 0.5 <= brain_jev.backoff_seconds(0) <= 0.625
    assert 1.5 <= brain_jev.backoff_seconds(1) <= 1.875
    assert 4.0 <= brain_jev.backoff_seconds(2) <= 5.0
    assert brain_jev.backoff_seconds(9) <= 5.0          # 超出计划表后封顶


# ---------------------------------------------------------------- 重试成功
def test_503_retried_then_succeeds():
    slept = []
    sender = Sender([http_error(503, b"no healthy upstream"), http_error(503), OK_PAYLOAD])

    decision = ask(sender, slept)

    assert decision.act == "2"
    assert decision.retries == 2
    assert sender.calls == 3
    assert len(slept) == 2
    assert 0.5 <= slept[0] <= 0.625
    assert 1.5 <= slept[1] <= 1.875
    assert slept[1] > slept[0]
    assert sender.headers[0]["Authorization"] == "Bearer test-key"


def test_429_is_retried_with_backoff():
    slept = []
    sender = Sender([http_error(429, b"slow down"), OK_PAYLOAD])

    decision = ask(sender, slept)

    assert decision.retries == 1
    assert sender.calls == 2
    assert 0.5 <= slept[0] <= 0.625


def test_url_error_is_retried_then_succeeds():
    slept = []
    sender = Sender([urllib.error.URLError("connection reset by peer"), OK_PAYLOAD])

    decision = ask(sender, slept)

    assert decision.retries == 1
    assert sender.calls == 2
    assert len(slept) == 1


# ---------------------------------------------------------------- 重试耗尽
def test_503_retries_exhausted_raises_jev_error():
    slept = []
    sender = Sender([lambda: http_error(503, b"no healthy upstream")])   # 永远 503

    with pytest.raises(brain_jev.JevError) as excinfo:
        ask(sender, slept)

    error = excinfo.value
    assert error.retries == brain_jev.RETRY_MAX == 3
    assert error.status == 503
    assert sender.calls == 4                 # 1 次首发 + 3 次重试
    assert len(slept) == 3
    assert 0.5 <= slept[0] <= 0.625
    assert 1.5 <= slept[1] <= 1.875
    assert 4.0 <= slept[2] <= 5.0
    assert "503" in str(error)
    assert "已重试 3 次" in str(error)
    assert "no healthy upstream" in str(error)


def test_url_error_exhausted_reports_network():
    slept = []
    sender = Sender([urllib.error.URLError("dns lookup failed")])

    with pytest.raises(brain_jev.JevError) as excinfo:
        ask(sender, slept)

    error = excinfo.value
    assert error.retries == 3
    assert error.status is None
    assert "网络不可达" in str(error)
    assert "dns lookup failed" in str(error)


# ---------------------------------------------------------------- 不重试
@pytest.mark.parametrize("code", [400, 401, 403, 404, 422])
def test_4xx_is_not_retried(code):
    slept = []
    sender = Sender([http_error(code, b"bad request")])

    with pytest.raises(brain_jev.JevError) as excinfo:
        ask(sender, slept)

    assert excinfo.value.status == code
    assert excinfo.value.retries == 0
    assert sender.calls == 1                 # 一次都不重试
    assert slept == []                       # 也不退避等待
    assert "已重试" not in str(excinfo.value)


def test_max_retries_zero_disables_retry():
    slept = []
    sender = Sender([http_error(503)])

    with pytest.raises(brain_jev.JevError):
        ask(sender, slept, max_retries=0)

    assert sender.calls == 1
    assert slept == []


def test_max_retries_one_allows_single_retry():
    slept = []
    sender = Sender([http_error(503), OK_PAYLOAD])

    assert ask(sender, slept, max_retries=1).retries == 1
    assert sender.calls == 2


# ---------------------------------------------------------------- 其他契约
def test_missing_key_raises_before_any_request(monkeypatch):
    monkeypatch.delenv("JEV_API_KEY", raising=False)

    def forbidden(*args, **kwargs):
        raise AssertionError("缺 key 时不应发起任何请求")

    with pytest.raises(RuntimeError) as excinfo:
        brain_jev.ask("t", ELEMENTS, api_key="", sender=forbidden)
    assert "JEV_API_KEY" in str(excinfo.value)


def test_retries_visible_in_decision_dict():
    slept = []
    decision = ask(Sender([http_error(503), OK_PAYLOAD]), slept)
    dumped = decision.as_dict()
    assert dumped["retries"] == 1

    clean = ask(Sender([OK_PAYLOAD]), [])
    assert clean.as_dict()["retries"] == 0


# ---------------------------------------------------------------- T12：重试总预算 + 熔断
def test_retry_budget_default_is_eight():
    assert brain_jev.RETRY_BUDGET == 8
    assert brain_jev.RetryBudget().limit == 8
    assert brain_jev.RetryBudget().tripped is False


def test_budget_allows_retries_within_limit():
    slept = []
    budget = brain_jev.RetryBudget(limit=8)
    sender = Sender([lambda: http_error(503), OK_PAYLOAD])

    decision = ask(sender, slept, budget=budget)

    assert decision.retries == 1
    assert budget.spent == 1
    assert budget.tripped is False
    assert budget.as_dict() == {"limit": 8, "spent": 1, "tripped": False, "reason": None}


def test_budget_accumulates_across_calls():
    budget = brain_jev.RetryBudget(limit=8)
    for _ in range(3):
        ask(Sender([lambda: http_error(503), OK_PAYLOAD]), [], budget=budget)

    assert budget.spent == 3            # 跨步累计，而不是每次调用各自计数
    assert budget.tripped is False


def test_budget_trips_and_marks_error_as_budget_exceeded():
    slept = []
    budget = brain_jev.RetryBudget(limit=2)
    sender = Sender([lambda: http_error(503, b"no healthy upstream")])

    with pytest.raises(brain_jev.JevError) as excinfo:
        ask(sender, slept, budget=budget)

    error = excinfo.value
    assert error.budget_exceeded is True
    assert budget.tripped is True
    assert budget.spent == 2
    assert sender.calls == 3            # 首发 1 次 + 预算内 2 次重试，第 3 次重试前熔断
    assert len(slept) == 2
    assert "预算" in str(error)
    assert "熔断" in str(error)
    assert budget.as_dict()["reason"] and "预算上限" in budget.as_dict()["reason"]


def test_circuit_breaker_stops_sending_requests_after_trip():
    budget = brain_jev.RetryBudget(limit=2)
    with pytest.raises(brain_jev.JevError):
        ask(Sender([lambda: http_error(503)]), [], budget=budget)
    assert budget.tripped is True

    def forbidden(*args, **kwargs):
        raise AssertionError("熔断后不应再发请求")

    with pytest.raises(brain_jev.JevError) as excinfo:
        ask(forbidden, [], budget=budget)

    assert excinfo.value.budget_exceeded is True
    assert excinfo.value.retries == 0
    assert "熔断" in str(excinfo.value)


def test_budget_zero_trips_immediately_without_any_request():
    slept = []
    budget = brain_jev.RetryBudget(limit=0)
    sender = Sender([OK_PAYLOAD])
    assert budget.tripped is True

    with pytest.raises(brain_jev.JevError) as excinfo:
        ask(sender, slept, budget=budget)

    assert sender.calls == 0
    assert slept == []
    assert excinfo.value.budget_exceeded is True


def test_budget_trips_on_url_error_too():
    slept = []
    budget = brain_jev.RetryBudget(limit=1)
    sender = Sender([urllib.error.URLError("dns fail")])

    with pytest.raises(brain_jev.JevError) as excinfo:
        ask(sender, slept, budget=budget)

    assert excinfo.value.budget_exceeded is True
    assert budget.tripped is True
    assert "网络" in str(excinfo.value)


def test_no_budget_keeps_previous_behaviour():
    slept = []
    decision = ask(Sender([lambda: http_error(503), OK_PAYLOAD]), slept)
    assert decision.retries == 1        # 不传 budget 时不熔断、行为不变
