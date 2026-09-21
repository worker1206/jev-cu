"""Jev（TypeSafe System One）决策客户端。

Jev 只做判断，不做生成：
  choice -> 选元素编号      score -> 确信度      noul -> 完成概率(yes)
弃权信号用 choice.probabilities 的 top1-top2 差（margin）；
不使用 confidence 字段——实测出现过 1.32 分而 confidence=0.0 的失真样本。
"""
from __future__ import annotations

import json
import os
import random
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


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


ROUTE_T = _env_float("JEV_ROUTE_T", 0.30)   # margin 低于此值 -> 升级 LLM
DONE_T = _env_float("JEV_DONE_T", 0.50)     # done 概率高于此值 -> 任务完成

# 瞬态错误重试：Jev 推理请求是无状态幂等的，5xx/429/网络抖动可以安全重试。
# 4xx（除 429）是请求本身的问题，重试只会重复犯错 —— 一律不重试。
RETRY_MAX = _env_int("JEV_RETRY_MAX", 3)              # 最多重试次数（总尝试 = 1 + RETRY_MAX）
RETRY_SCHEDULE = (0.5, 1.5, 4.0)                      # 指数退避基数，实际值再加 ≤25% 抖动
RETRY_JITTER = 0.25                                   # 抖动比例，避免多进程同步重试
RETRYABLE_STATUS = (429,)


class JevError(RuntimeError):
    """带重试次数与 HTTP 状态码的 Jev 调用错误（便于日志与诊断分类）。"""

    def __init__(self, message, retries=0, status=None):
        super().__init__(message)
        self.retries = int(retries)
        self.status = status


def is_retryable_status(code):
    """429 与全部 5xx 可重试；其余 4xx 不重试。"""
    return code in RETRYABLE_STATUS or 500 <= int(code) < 600


def backoff_seconds(attempt):
    """第 attempt 次重试前的等待秒数（attempt 从 0 计），带抖动。"""
    base = RETRY_SCHEDULE[min(int(attempt), len(RETRY_SCHEDULE) - 1)]
    return round(base + random.uniform(0.0, base * RETRY_JITTER), 4)


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
    retries: int = 0

    def as_dict(self):
        return dict(act=self.act, confidence=round(self.confidence, 4),
                    margin=round(self.margin, 4), done=round(self.done, 4),
                    probabilities=self.probabilities, latency=round(self.latency, 3),
                    input_tokens=self.input_tokens, need_llm=self.need_llm,
                    finished=self.finished, source=self.source, retries=self.retries)


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


def post_json(url, body, headers, timeout):
    """默认传输层：POST 并解析 JSON。HTTP 错误抛 HTTPError，网络问题抛 URLError。"""
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def _read_error_body(exc):
    try:
        return exc.read()[:200].decode("utf-8", "replace")
    except Exception:
        return ""


def ask(task, elements, history=None, model=None, api_key=None, base_url=None, timeout=30,
        sender=None, sleep=None, max_retries=None):
    """调用 Jev。api_key 缺省读环境变量 JEV_API_KEY。

    瞬态错误（5xx / 429 / URLError 网络抖动）自动有界重试：最多 RETRY_MAX 次，
    退避 0.5s/1.5s/4.0s（带 ≤25% 抖动）。4xx（除 429）不重试。
    实际重试次数记录在 `Decision.retries`（成功）或 `JevError.retries`（失败）里。

    sender / sleep 可注入以便 mock：sender(url, body, headers, timeout) -> dict，
    sleep(seconds) -> None。
    """
    api_key = api_key or os.environ.get("JEV_API_KEY", "")
    if not api_key:
        raise RuntimeError("缺少 JEV_API_KEY（请写入 .env，勿硬编码进代码）")
    base = (base_url or os.environ.get("JEV_BASE_URL", DEFAULT_BASE_URL)).rstrip("/")
    body = json.dumps(build_request(task, elements, history, model)).encode("utf-8")
    url = base + "/v1/systemone"
    headers = {"Authorization": "Bearer " + api_key, "Content-Type": "application/json"}
    send = sender or post_json
    nap = sleep or time.sleep
    limit = RETRY_MAX if max_retries is None else max(0, int(max_retries))

    started = time.time()
    retries = 0
    while True:
        try:
            payload = send(url, body, headers, timeout)
            break
        except urllib.error.HTTPError as exc:
            # HTTPError 是 URLError 的子类，必须先捕获
            status = exc.code
            detail = _read_error_body(exc)
            if not is_retryable_status(status) or retries >= limit:
                raise JevError(
                    "Jev API 返回 %s: %s%s" % (status, detail,
                                               "（已重试 %d 次）" % retries if retries else ""),
                    retries=retries, status=status) from exc
            nap(backoff_seconds(retries))
            retries += 1
        except urllib.error.URLError as exc:
            if retries >= limit:
                raise JevError(
                    "Jev API 网络不可达（已重试 %d 次）: %s" % (retries, exc.reason),
                    retries=retries) from exc
            nap(backoff_seconds(retries))
            retries += 1

    decision = parse_answer(payload, latency=time.time() - started)
    decision.retries = retries
    return decision
