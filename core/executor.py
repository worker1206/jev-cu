"""Playwright 执行器：按稳定编号操作元素，异常一律转结构化错误。

铁律：执行异常不得向上抛裸异常——外层循环要靠 "ok": false 决定是否重试/中止，
一旦抛出去，日志就断了，崩溃现场也留不下（上一轮实测丢过数据）。

结果字典结构（稳定契约）：
  {"ok": bool, "action": str, "idx": str, "label": str|None, "value": str|None,
   "error": None | {"type": str, "message": str},
   "url": str, "title": str, "waited_ms": int, "duration_ms": int}
"""
from __future__ import annotations

import time

from core import sensor

DEFAULT_WAIT_MS = 800          # 普通动作后的稳定等待
NAV_WAIT_MS = 2000             # 可能触发导航的动作后的等待
DIALOG_WAIT_MS = 1200          # 点击触发按钮到输入框出现的等待
DEFAULT_TIMEOUT_MS = 10000

DIALOG_INPUT_SELECTORS = (
    'input[type="search"]',
    'input[name="q"]',
    'input[name="wd"]',
    'input[name="query"]',
    'input[type="text"]',
    'input[type="search"][role="combobox"]',
    'textarea[name="q"]',
    'input[placeholder*="搜索" i]',
    'input[placeholder*="Search" i]',
)

KNOWN_ACTIONS = ("click", "fill", "press_enter", "dialog_search")


class Executor:
    """把"编号 + 动作"翻译成真实浏览器操作，并回读动作后的状态。"""

    def __init__(self, page, wait_ms=DEFAULT_WAIT_MS, nav_wait_ms=NAV_WAIT_MS,
                 dialog_wait_ms=DIALOG_WAIT_MS, timeout_ms=DEFAULT_TIMEOUT_MS,
                 dialog_selectors=None):
        self.page = page
        self.wait_ms = wait_ms
        self.nav_wait_ms = nav_wait_ms
        self.dialog_wait_ms = dialog_wait_ms
        self.timeout_ms = timeout_ms
        self.dialog_selectors = tuple(dialog_selectors or DIALOG_INPUT_SELECTORS)

    # ---------------- 状态回读 ----------------
    def snapshot(self):
        """动作后回读 url/title，日志里能看出页面到底有没有变化。"""
        return {"url": self._url(), "title": self._title()}

    def _url(self):
        try:
            return str(getattr(self.page, "url", "") or "")
        except Exception:
            return ""

    def _title(self):
        try:
            return str(self.page.title() or "")
        except Exception:
            return ""

    def _wait(self, ms):
        try:
            self.page.wait_for_timeout(ms)
            return int(ms)
        except Exception:
            return 0

    def _wait_nav(self):
        """等待 + 尽力等 load state；load state 失败不算动作失败。"""
        waited = self._wait(self.nav_wait_ms)
        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=self.timeout_ms)
        except Exception:
            pass
        return waited

    def settle(self, ms=None):
        """对外暴露的稳定等待，供主循环在导航后使用。"""
        return self._wait(self.nav_wait_ms if ms is None else ms)

    # ---------------- 结果构造 ----------------
    def _ms(self, started):
        return int((time.time() - started) * 1000) if started else 0

    def _ok(self, action, idx, label=None, value=None, waited=0, started=None):
        snap = self.snapshot()
        return {
            "ok": True, "action": action, "idx": str(idx), "label": label, "value": value,
            "error": None, "url": snap["url"], "title": snap["title"],
            "waited_ms": int(waited or 0), "duration_ms": self._ms(started),
        }

    def _error(self, action, idx, etype, message, started=None, label=None, value=None, waited=0):
        snap = self.snapshot()
        return {
            "ok": False, "action": action, "idx": str(idx), "label": label, "value": value,
            "error": {"type": str(etype), "message": str(message)[:300]},
            "url": snap["url"], "title": snap["title"],
            "waited_ms": int(waited or 0), "duration_ms": self._ms(started),
        }

    # ---------------- 元素解析 ----------------
    def element(self, idx):
        """按采集时写下的 data-jev-idx 取回真实元素句柄。"""
        return sensor.resolve(self.page, idx)

    def _find_dialog_input(self):
        for sel in self.dialog_selectors:
            try:
                candidates = self.page.query_selector_all(sel) or []
            except Exception:
                continue
            for cand in candidates:
                try:
                    if cand.is_visible():
                        return cand
                except Exception:
                    continue
        return None

    # ---------------- 动作 ----------------
    def click(self, idx, label=None):
        started = time.time()
        try:
            el = self.element(idx)
            if el is None:
                return self._error("click", idx, "element_not_found",
                                   "编号 %s 在页面上不存在（快照可能已过期）" % idx,
                                   started, label=label)
            el.click(timeout=self.timeout_ms)
            waited = self._wait_nav()
            return self._ok("click", idx, label=label, waited=waited, started=started)
        except Exception as exc:
            return self._error("click", idx, type(exc).__name__, exc, started, label=label)

    def fill(self, idx, value, label=None, submit=True):
        """先 click 聚焦再 fill（部分框架不点不激活输入框），可选回车提交。"""
        started = time.time()
        try:
            el = self.element(idx)
            if el is None:
                return self._error("fill", idx, "element_not_found",
                                   "编号 %s 在页面上不存在（快照可能已过期）" % idx,
                                   started, label=label, value=str(value))
            el.click(timeout=self.timeout_ms)
            el.fill(str(value), timeout=self.timeout_ms)
            waited = self._wait(self.wait_ms)
            if submit:
                try:
                    el.press("Enter", timeout=self.timeout_ms)
                except Exception as exc:
                    # 填充成功后元素因页面跳转脱离 DOM。**绝不能**按编号重新按一次回车：
                    # 编号是新页面重新分配的，会指向另一个元素 —— 那是误操作。
                    # 如实上报为独立错误类型，让主循环下一步重新采集。
                    if "not attached" in str(exc):
                        return self._error(
                            "fill", idx, "element_detached_after_action",
                            "文本已填入，但回车提交时元素已脱离 DOM（页面已跳转）：%s"
                            % str(exc)[:160], started, label=label, value=str(value),
                            waited=waited)
                    raise
                waited += self._wait_nav()
            return self._ok("fill", idx, label=label, value=str(value),
                            waited=waited, started=started)
        except Exception as exc:
            return self._error("fill", idx, type(exc).__name__, exc, started,
                               label=label, value=str(value))

    def press_enter(self, idx, label=None):
        started = time.time()
        try:
            el = self.element(idx)
            if el is None:
                return self._error("press_enter", idx, "element_not_found",
                                   "编号 %s 在页面上不存在（快照可能已过期）" % idx,
                                   started, label=label)
            el.press("Enter", timeout=self.timeout_ms)
            waited = self._wait_nav()
            return self._ok("press_enter", idx, label=label, waited=waited, started=started)
        except Exception as exc:
            return self._error("press_enter", idx, type(exc).__name__, exc, started, label=label)

    def dialog_search(self, trigger_idx, value, label=None):
        """dialog 型搜索：点触发按钮 → 等输入框 → 填字 → 回车（GitHub 模式）。"""
        started = time.time()
        try:
            trigger = self.element(trigger_idx)
            if trigger is None:
                return self._error("dialog_search", trigger_idx, "element_not_found",
                                   "编号 %s 在页面上不存在（快照可能已过期）" % trigger_idx,
                                   started, label=label, value=str(value))
            trigger.click(timeout=self.timeout_ms)
            waited = self._wait(self.dialog_wait_ms)
            box = self._find_dialog_input()
            if box is None:
                return self._error("dialog_search", trigger_idx, "dialog_input_not_found",
                                   "点击触发元素后未找到可见输入框",
                                   started, label=label, value=str(value), waited=waited)
            box.click(timeout=self.timeout_ms)
            box.fill(str(value), timeout=self.timeout_ms)
            box.press("Enter", timeout=self.timeout_ms)
            waited += self._wait_nav()
            return self._ok("dialog_search", trigger_idx, label=label, value=str(value),
                            waited=waited, started=started)
        except Exception as exc:
            return self._error("dialog_search", trigger_idx, type(exc).__name__, exc, started,
                               label=label, value=str(value))

    def execute(self, action, idx, value=None, label=None, submit=True):
        """分派 + 兜底：任何漏网异常都变成结构化错误，绝不向上抛。"""
        try:
            if action == "click":
                return self.click(idx, label=label)
            if action == "fill":
                return self.fill(idx, value, label=label, submit=submit)
            if action == "press_enter":
                return self.press_enter(idx, label=label)
            if action == "dialog_search":
                return self.dialog_search(idx, value, label=label)
            return self._error(action, idx, "unknown_action",
                               "未知动作：%s（支持 %s）" % (action, "/".join(KNOWN_ACTIONS)),
                               time.time(), label=label)
        except Exception as exc:
            return self._error(action, idx, type(exc).__name__, exc, time.time(), label=label)
