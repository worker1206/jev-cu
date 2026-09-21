"""安全门：危险动作识别 + 人工确认（human-in-the-loop）。

铁律：凡涉及 提交/支付/删除/发送/转账 之类的动作，无论 Jev/LLM 置信多高，
一律要求人工确认；确认失败（异常、空输入、非肯定答复）一律按"拒绝"处理。
reader 可注入（测试用假 reader），默认从 stdin 读取，绝不自动放行。
"""
from __future__ import annotations

import re
import sys

# 英文关键词：按词边界匹配，避免 "buyer" 命中 "buy" 之外的噪音
DANGEROUS_KEYWORDS_EN = (
    "submit", "pay", "payment", "purchase", "checkout", "place order", "buy",
    "delete", "remove", "send", "transfer", "unsubscribe", "cancel account",
    "drop table", "format disk", "wire money",
)

# 中文关键词：中文无词边界，用子串匹配
DANGEROUS_KEYWORDS_ZH = (
    "提交", "支付", "付款", "下单", "购买", "结算", "删除", "移除",
    "发送", "转账", "汇款", "注销", "退订", "清空", "打款",
)

DANGEROUS_KEYWORDS = tuple(DANGEROUS_KEYWORDS_EN) + tuple(DANGEROUS_KEYWORDS_ZH)

_EN_RE = re.compile(
    r"\b(?:%s)\b" % "|".join(re.escape(k) for k in DANGEROUS_KEYWORDS_EN), re.I
)

AFFIRMATIVE = ("y", "yes", "yeah", "ok", "okay", "true", "1",
               "是", "确认", "同意", "可以", "执行", "允许")


def match_dangerous(text):
    """返回命中的危险关键词；不危险返回 None。中英文都识别。"""
    if text is None:
        return None
    raw = str(text)
    if not raw.strip():
        return None
    hit = _EN_RE.search(raw.lower())
    if hit:
        return hit.group(0).lower()
    for kw in DANGEROUS_KEYWORDS_ZH:
        if kw in raw:
            return kw
    return None


def is_dangerous(text):
    """布尔版判定，便于日志/断言直接使用。"""
    return match_dangerous(text) is not None


def is_affirmative(answer):
    """肯定答复判定：空、None、n/no 一律 False（默认拒绝）。"""
    if answer is None:
        return False
    text = str(answer).strip().lower()
    if not text:
        return False
    return text in AFFIRMATIVE


def default_reader(prompt):
    """默认 reader：交互式询问。EOF/异常由 confirm() 兜住并视为拒绝。"""
    sys.stderr.write(prompt)
    sys.stderr.flush()
    return sys.stdin.readline()


def confirm(action, reader=None, prompt=None):
    """人工确认门。reader 可注入：callable(prompt) -> str。"""
    reader = reader or default_reader
    text = prompt or ("[安全门] 该动作疑似危险，需要人工确认：%s\n继续执行？[y/N] " % action)
    try:
        answer = reader(text)
    except Exception:
        return False
    return is_affirmative(answer)


def describe(action):
    """统一的安全门描述，供决策日志记录命中的关键词。"""
    kw = match_dangerous(action)
    return {"dangerous": kw is not None, "keyword": kw}
