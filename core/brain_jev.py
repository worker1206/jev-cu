"""Jev（TypeSafe System One）决策客户端。

Jev 只做判断，不做生成：
  choice -> 选元素编号      score -> 确信度      noul -> 完成概率(yes)
弃权信号用 choice.probabilities 的 top1-top2 差（margin）；
不使用 confidence 字段——实测出现过 1.32 分而 confidence=0.0 的失真样本。
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

DEFAULT_BASE_URL = "https://api.typesafe.ai"
DEFAULT_MODEL = "jev-latest"


def _env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


ROUTE_T = _env_float("JEV_ROUTE_T", 0.30)   # margin 低于此值 -> 升级 LLM
DONE_T = _env_float("JEV_DONE_T", 0.50)     # done 概率高于此值 -> 任务完成


@dataclass
class Decision:
    act: str = ""
    confidence: float = 0.0
    margin: float = 0.0
    done: float = 0.0
    probabilities: dict = field(default_factory=dict)
    latency: float = 0.0
    input_tokens: int = 0
    need_llm: bool = False
    finished: bool = False
    source: str = "jev"

    def as_dict(self):
        return dict(act=self.act, confidence=round(self.confidence, 4),
                    margin=round(self.margin, 4), done=round(self.done, 4),
                    probabilities=self.probabilities, latency=round(self.latency, 3),
                    input_tokens=self.input_tokens, need_llm=self.need_llm,
                    finished=self.finished, source=self.source)


def compute_margin(probabilities):
    """top1 - top2 的概率差；只有一个候选时视为满分。"""
    values = sorted((float(v) for v in (probabilities or {}).values()), reverse=True)
    if not values:
        return 0.0
    if len(values) == 1:
        return 1.0
    return round(values[0] - values[1], 6)


def parse_answer(payload, latency=0.0):
    """解析 /v1/systemone 响应为 Decision。"""
    answers = (payload or {}).get("answers") or {}
    act = answers.get("act") or {}
    done = answers.get("done") or {}
    probs = act.get("probabilities") or {}
    decision = Decision(
        act=str(act.get("choice", "")),
        confidence=float(act.get("confidence", 0) or 0),
        margin=compute_margin(probs),
        done=float(done.get("noul", 0) or 0),
        probabilities={k: float(v) for k, v in probs.items()},
        latency=latency,
        input_tokens=int(((payload or {}).get("usage") or {}).get("input_tokens", 0) or 0),
    )
    decision.need_llm = decision.margin < ROUTE_T
    decision.finished = decision.done >= DONE_T
    return decision


def build_request(task, elements, history=None, model=None):
    """构造单请求三问：act(选元素) / conf(确信度) / done(完成度)。"""
    history = history or []
    state = {
        "task": task,
        "history": history[-3:],
        "elements": ["%s. <%s> %s" % (e["idx"], e["tag"], e["label"]) for e in elements],
    }
    return {
        "model": model or os.environ.get("JEV_MODEL", DEFAULT_MODEL),
        "state": state,
        "questions": {
            "act": {
                "type": "choice",
                "instructions": "为完成当前任务，下一步应操作的元素编号",
                "criteria": {str(e["idx"]): "<%s> %s" % (e["tag"], e["label"]) for e in elements},
            },
            "conf": {
                "type": "score",
                "instructions": "对该选择的确信度",
                "criteria": ["瞎猜", "可能", "确定"],
            },
            "done": {
                "type": "noul",
                "instructions": "结合历史操作，该任务是否已经完成",
            },
        },
    }


def ask(task, elements, history=None, model=None, api_key=None, base_url=None, timeout=30):
    """调用 Jev。api_key 缺省读环境变量 JEV_API_KEY。"""
    api_key = api_key or os.environ.get("JEV_API_KEY", "")
    if not api_key:
        raise RuntimeError("缺少 JEV_API_KEY（请写入 .env，勿硬编码进代码）")
    base = (base_url or os.environ.get("JEV_BASE_URL", DEFAULT_BASE_URL)).rstrip("/")
    body = json.dumps(build_request(task, elements, history, model)).encode("utf-8")
    req = urllib.request.Request(
        base + "/v1/systemone",
        data=body,
        headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
        method="POST",
    )
    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.load(resp)
    except urllib.error.HTTPError as exc:
        raise RuntimeError("Jev API 返回 %s: %s" % (exc.code, exc.read()[:200])) from exc
    return parse_answer(payload, latency=time.time() - started)
