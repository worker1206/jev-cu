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

DEFAULT_MODEL = "gpt-4o-mini"

SYSTEM_PROMPT = (
    "你是网页操作决策器（系统二），只负责在候选元素中挑选下一步要操作的一个元素。"
    "只输出一个 JSON 对象，前后不要任何其他文字。"
    '格式：{"act": "<元素编号>", "confidence": 0到1的小数, "done": 0到1的小数, "reason": "简短理由"}'
)

REQUIRED_ENV = ("LLM_BASE_URL", "LLM_API_KEY")


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


def http_transport(cfg=None, timeout=30):
    """默认传输层：OpenAI 兼容 POST {base_url}/chat/completions。"""
    cfg = cfg or config()

    def _call(messages, model):
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
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.load(resp)
        except urllib.error.HTTPError as exc:
            raise RuntimeError("LLM API 返回 %s: %s" % (exc.code, exc.read()[:200])) from exc
        try:
            return payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("LLM 响应结构异常: %s" % str(payload)[:200]) from exc

    return _call


def ask(task, elements, history=None, transport=None, cfg=None, timeout=30, model=None):
    """调用 LLM 兜底。transport 可注入以便 mock：callable(messages, model) -> str。"""
    cfg = cfg or config()
    if transport is None:
        missing = missing_env(cfg)
        if missing:
            raise RuntimeError("LLM 兜底未配置：缺少 %s" % ", ".join(missing))
        transport = http_transport(cfg, timeout=timeout)
    messages = build_messages(task, elements, history)
    started = time.time()
    text = transport(messages, model or cfg["model"])
    decision = parse_llm_answer(text, valid_ids=[e.get("idx") for e in elements])
    decision.latency = round(time.time() - started, 3)
    return decision
