"""core/brain_jev 单测：全 mock，不访问真实 API。

重点守住三条实测教训：
  1. 弃权口径必须是 margin（top1-top2），不是 confidence；
  2. margin 阈值边界（>= 0.30 不升级）；
  3. done 阈值边界（>= 0.50 判完成）。
"""
from core import brain_jev

ELEMENTS = [
    {"idx": 1, "tag": "input", "label": "搜索", "is_input": True},
    {"idx": 2, "tag": "button", "label": "Search", "is_input": False},
    {"idx": 3, "tag": "a", "label": "登录", "is_input": False},
]


def payload(act="2", probs=None, conf=1.0, done=0.0, tokens=913):
    return {
        "answers": {
            "act": {"choice": act, "confidence": conf, "probabilities": probs or {"2": 0.9, "1": 0.1}},
            "conf": {"score": conf},
            "done": {"noul": done},
        },
        "usage": {"input_tokens": tokens, "output_tokens": 0},
    }


def test_compute_margin_top1_minus_top2():
    assert brain_jev.compute_margin({"a": 0.9, "b": 0.6, "c": 0.1}) == 0.3
    assert brain_jev.compute_margin({"a": 0.5, "b": 0.5}) == 0.0


def test_compute_margin_single_and_empty():
    assert brain_jev.compute_margin({"only": 0.42}) == 1.0
    assert brain_jev.compute_margin({}) == 0.0
    assert brain_jev.compute_margin(None) == 0.0


def test_parse_answer_reads_three_answers():
    decision = brain_jev.parse_answer(payload(act="2", probs={"2": 0.8, "1": 0.2},
                                             done=0.7, tokens=1234), latency=1.03)
    assert decision.act == "2"
    assert decision.margin == 0.6
    assert decision.done == 0.7
    assert decision.input_tokens == 1234
    assert decision.latency == 1.03
    assert decision.source == "jev"
    assert decision.need_llm is False
    assert decision.finished is True


def test_routing_uses_margin_not_confidence():
    """confidence=1.32 但概率几乎平局（实测失真样本）→ 必须升级 LLM。"""
    decision = brain_jev.parse_answer(payload(probs={"1": 0.51, "2": 0.50}, conf=1.32))
    assert decision.confidence == 1.32
    assert decision.margin < brain_jev.ROUTE_T
    assert decision.need_llm is True


def test_need_llm_threshold_boundary():
    at_threshold = brain_jev.parse_answer(payload(probs={"1": 0.9, "2": 0.6}))   # margin = 0.30
    below = brain_jev.parse_answer(payload(probs={"1": 0.79, "2": 0.50}))        # margin = 0.29
    assert at_threshold.margin == 0.3
    assert at_threshold.need_llm is False
    assert below.need_llm is True


def test_finished_threshold_boundary():
    assert brain_jev.parse_answer(payload(done=0.50)).finished is True
    assert brain_jev.parse_answer(payload(done=0.49)).finished is False
    assert brain_jev.parse_answer(payload(done=0.11)).finished is False


def test_build_request_asks_three_questions_at_once():
    body = brain_jev.build_request("搜索人工智能", ELEMENTS, history=["step1: 点击<搜索>"])
    assert set(body["questions"]) == {"act", "conf", "done"}
    assert body["questions"]["act"]["type"] == "choice"
    assert body["questions"]["conf"]["type"] == "score"
    assert body["questions"]["done"]["type"] == "noul"
    assert set(body) == {"model", "state", "questions"}


def test_criteria_keys_equal_element_ids():
    body = brain_jev.build_request("搜索人工智能", ELEMENTS)
    assert set(body["questions"]["act"]["criteria"]) == {"1", "2", "3"}
    assert body["questions"]["act"]["criteria"]["2"] == "<button> Search"


def test_state_carries_elements_and_history():
    body = brain_jev.build_request("搜索人工智能", ELEMENTS,
                                   history=["a", "b", "c", "d", "e"])
    assert body["state"]["task"] == "搜索人工智能"
    assert "1. <input> 搜索" in body["state"]["elements"]
    assert body["state"]["history"] == ["c", "d", "e"]     # 只带最近 3 步


def test_as_dict_is_json_safe():
    import json
    decision = brain_jev.parse_answer(payload(done=0.5))
    dumped = json.loads(json.dumps(decision.as_dict(), ensure_ascii=False))
    assert dumped["source"] == "jev"
    assert dumped["finished"] is True
