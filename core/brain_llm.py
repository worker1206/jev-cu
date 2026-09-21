"""LLM 兜底客户端（系统二）：只在 Jev margin 过低时介入。

设计约束：
  * 返回与 brain_jev.Decision 完全同构的对象（source="llm"），上层循环零分支。
  * 传输层可注入（transport: callable(messages, model) -> str），测试全 mock。
  * 未配置 LLM_BASE_URL / LLM_API_KEY 时 available() 返回 False，
    主循环会原样沿用 Jev 的判断，并把"兜底不可用"如实写进决策日志。
  * 绝不打印、写盘或记录 key 明文。
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request

from core.brain_jev import DONE_T, Decision, compute_margin
# 重试口径**直接复用** brain_jev 的那一套：同一张退避表、同一个抖动比例、
# 同一个可重试判定函数。两条链路必须口径一致，否则"可重试"的含义会分裂。
from core.brain_jev import RETRY_JITTER, RETRY_SCHEDULE, backoff_seconds, is_retryable_status

DEFAULT_MODEL = "gpt-4o-mini"


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


LLM_RETRY_MAX = _env_int("LLM_RETRY_MAX", 3)   # 单次 LLM 调用最多重试次数

SYSTEM_PROMPT = (
    "你是网页操作决策器（系统二），只负责在候选元素中挑选下一步要操作的一个元素。"
    "只输出一个 JSON 对象，前后不要任何其他文字。"
    '格式：{"act": "<元素编号>", "confidence": 0到1的小数, "done": 0到1的小数, "reason": "简短理由"}'
)

REQUIRED_ENV = ("LLM_BASE_URL", "LLM_API_KEY")

# doctor 连通性探测用的最小请求：只要模型回一个 JSON，就说明链路可用
PROBE_MESSAGES = [
    {"role": "system", "content": "只输出一个 JSON 对象，前后不要任何其他文字。"},
    {"role": "user", "content": '请只输出 {"act":"1"}'},
]


class LlmError(RuntimeError):
    """带 HTTP 状态码的 LLM 调用错误。

    doctor 靠 status 区分：401/403 鉴权失败、429/5xx 服务不可用；无 status 多为响应结构异常。
    """

    def __init__(self, message, status=None, retries=0, budget_exceeded=False):
        super().__init__(message)
        self.status = None if status is None else int(status)
        self.retries = int(retries)
        self.budget_exceeded = bool(budget_exceeded)


def config():
    """读取 LLM 配置。只读环境变量，绝不回显 key。"""
    return {
        "base_url": (os.environ.get("LLM_BASE_URL") or "").strip().rstrip("/"),
        "api_key": (os.environ.get("LLM_API_KEY") or "").strip(),
        "model": (os.environ.get("LLM_MODEL") or "").strip() or DEFAULT_MODEL,
    }


def missing_env(cfg=None):
    """返回缺失的环境变量名列表（只返回名字，不含值）。"""
    cfg = cfg or config()
    keys = {"base_url": "LLM_BASE_URL", "api_key": "LLM_API_KEY"}
    return [keys[k] for k in ("base_url", "api_key") if not cfg.get(k)]


def available(cfg=None):
    """未配置 base_url + api_key 时返回 False。"""
    return not missing_env(cfg)


def build_messages(task, elements, history=None):
    """构造 OpenAI 兼容 messages。元素编号与 Jev 侧完全同源（sensor 快照）。"""
    history = history or []
    lines = ["任务：%s" % task, ""]
    if history:
        lines.append("已完成的操作：")
        lines.extend("  - %s" % h for h in history[-5:])
        lines.append("")
    lines.append("当前页面候选元素（编号即操作目标）：")
    for e in elements:
        suffix = " [可输入]" if e.get("is_input") else ""
        lines.append("  %s. <%s> %s%s" % (e.get("idx"), e.get("tag"), e.get("label", ""), suffix))
    lines.append("")
    lines.append("请只输出 JSON。")
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(lines)},
    ]


def _as_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def extract_json(text):
    """从可能带 markdown 围栏的解释性文本里抽出第一个 JSON 对象。"""
    if text is None or not str(text).strip():
        raise ValueError("LLM 返回为空")
    raw = str(text).strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", raw, re.S)
    if fence:
        raw = fence.group(1).strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("LLM 返回不含 JSON 对象：%s" % raw[:120])
    chunk = raw[start:end + 1]
    try:
        data = json.loads(chunk)
    except ValueError as exc:
        raise ValueError("LLM 返回的 JSON 无法解析：%s" % chunk[:120]) from exc
    if not isinstance(data, dict):
        raise ValueError("LLM 返回的 JSON 不是对象")
    return data


def parse_llm_answer(text, valid_ids=None):
    """把 LLM 文本解析成与 Jev 同构的 Decision(source="llm")。

    单一 confidence 折成两候选分布后复用 Jev 的 margin 口径：
    margin = confidence - (1 - confidence) = 2*confidence - 1（下限 0）。
    这样"margin 越高越可信"在整个系统里只有一套含义。
    """
    data = extract_json(text)
    act = str(data.get("act", "") or "").strip()
    conf = min(max(_as_float(data.get("confidence"), 0.0), 0.0), 1.0)
    done = min(max(_as_float(data.get("done"), 0.0), 0.0), 1.0)

    if valid_ids is not None and act:
        allowed = set(str(v) for v in valid_ids)
        if act not in allowed:
            # 越界编号会被执行器拒掉（element_not_found），此处显式降 margin，
            # 让日志一眼看出"LLM 给了不存在的元素"。
            conf = 0.0

    key = act or "?"
    probabilities = {key: conf, "__rest__": round(1.0 - conf, 6)}
    margin = 0.0 if not act else max(0.0, compute_margin(probabilities))

    decision = Decision(
        act=act,
        confidence=conf,
        margin=margin,
        done=done,
        probabilities=probabilities,
        source="llm",
    )
    decision.need_llm = False          # 已经升级过了，不再二次升级
    decision.finished = done >= DONE_T
    return decision


def http_transport(cfg=None, timeout=30, max_retries=None, sleep=None, budget=None, stats=None):
    """默认传输层：OpenAI 兼容 POST {base_url}/chat/completions。

    与 Jev 链路**同一套重试口径**：429/5xx/URLError 有界重试（默认 LLM_RETRY_MAX=3，
    退避 0.5s/1.5s/4.0s + ≤25% 抖动），4xx（除 429）不重试。
    budget 为跨步共享的 RetryBudget；已熔断时直接失败，不再发请求。
    stats 是可选的可变 dict，成功后回填 {"retries": n}（返回值是文本，只能这样带出来）。

    doctor 的连通性探测请传 max_retries=0（探测刻意保持单次尝试，见 probe_llm）。
    """
    cfg = cfg or config()
    nap = sleep or time.sleep
    limit = LLM_RETRY_MAX if max_retries is None else max(0, int(max_retries))

    def _post(messages, model):
        body = json.dumps(
            {"model": model, "messages": messages, "temperature": 0}
        ).encode("utf-8")
        req = urllib.request.Request(
            cfg["base_url"] + "/chat/completions",
            data=body,
            headers={
                "Authorization": "Bearer " + cfg["api_key"],
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)

    def _call(messages, model):
        if budget is not None and budget.tripped:
            raise LlmError("重试预算已熔断，拒绝发起新请求：%s"
                           % (budget.reason or "预算耗尽"), budget_exceeded=True)
        retries = 0
        while True:
            try:
                payload = _post(messages, model)
                break
            except urllib.error.HTTPError as exc:
                # HTTPError 是 URLError 的子类，必须先捕获
                try:
                    detail = exc.read()[:200]
                except Exception:
                    detail = b""
                if not is_retryable_status(exc.code) or retries >= limit:
                    raise LlmError("LLM API 返回 %s: %s%s" % (
                        exc.code, detail,
                        "（已重试 %d 次）" % retries if retries else ""),
                        status=exc.code, retries=retries) from exc
                if budget is not None and not budget.allow():
                    raise LlmError(
                        "LLM API 持续返回 %s，重试预算耗尽已熔断：%s"
                        % (exc.code, budget.reason or "预算耗尽"),
                        status=exc.code, retries=retries, budget_exceeded=True) from exc
                nap(backoff_seconds(retries))
                retries += 1
                if budget is not None:
                    budget.spend()
            except urllib.error.URLError as exc:
                if retries >= limit:
                    raise LlmError("LLM 网络不可达（已重试 %d 次）: %s" % (retries, exc.reason),
                                   retries=retries) from exc
                if budget is not None and not budget.allow():
                    raise LlmError(
                        "LLM 网络持续不可达，重试预算耗尽已熔断：%s"
                        % (budget.reason or "预算耗尽"),
                        retries=retries, budget_exceeded=True) from exc
                nap(backoff_seconds(retries))
                retries += 1
                if budget is not None:
                    budget.spend()

        try:
            text = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LlmError("LLM 响应结构异常: %s" % str(payload)[:200]) from exc
        if stats is not None:
            stats["retries"] = retries
        return text

    return _call


def ask(task, elements, history=None, transport=None, cfg=None, timeout=30, model=None,
        budget=None, sleep=None, max_retries=None):
    """调用 LLM 兜底。transport 可注入以便 mock：callable(messages, model) -> str。

    未注入 transport 时走 http_transport（带与 Jev 同口径的有界重试与预算熔断）；
    重试次数从 stats 回填到 `Decision.retries`（注入式 transport 不重试，记 0）。
    """
    cfg = cfg or config()
    stats = {}
    if transport is None:
        missing = missing_env(cfg)
        if missing:
            raise RuntimeError("LLM 兜底未配置：缺少 %s" % ", ".join(missing))
        transport = http_transport(cfg, timeout=timeout, max_retries=max_retries,
                                   sleep=sleep, budget=budget, stats=stats)
    messages = build_messages(task, elements, history)
    started = time.time()
    text = transport(messages, model or cfg["model"])
    decision = parse_llm_answer(text, valid_ids=[e.get("idx") for e in elements])
    decision.latency = round(time.time() - started, 3)
    decision.retries = int(stats.get("retries", 0))
    return decision
