"""core/sensor 纯函数契约 + 采集 JS 属性覆盖；真实浏览器用例失败即 skip（保证离线可过）。"""
import pytest

from core import sensor

ELEMENTS = [
    {"idx": 1, "tag": "button", "label": "Search", "text": "Search",
     "placeholder": "", "aria_label": "", "name": "", "type": "", "title": "",
     "is_input": False},
    {"idx": 2, "tag": "input", "label": "搜索", "text": "", "placeholder": "搜索",
     "aria_label": "站内搜索", "name": "q", "type": "search", "title": "",
     "is_input": True},
]

# 采集 JS 必须覆盖这些属性，否则 input 的可见文本为空时会被喂成瞎子（历史 5 个假错误）
REQUIRED_JS_TOKENS = (
    "data-jev-idx", "innerText", "value",
    "placeholder", "aria-label", "name", "type", "title",
)


def test_to_state_text_is_numbered_lines():
    assert sensor.to_state_text(ELEMENTS) == "  1. <button> Search\n  2. <input> 搜索"


def test_to_state_text_empty():
    assert sensor.to_state_text([]) == ""


def test_build_criteria_keys_are_element_ids():
    criteria = sensor.build_criteria(ELEMENTS)
    assert set(criteria) == {"1", "2"}
    assert criteria["1"] == "<button> Search"
    assert criteria["2"] == "<input> 搜索"


def test_elements_js_covers_required_attributes():
    js = sensor.ELEMENTS_JS
    missing = [t for t in REQUIRED_JS_TOKENS if t not in js]
    assert not missing, "采集 JS 缺少属性：%s" % missing


def test_elements_js_marks_stable_index():
    assert "setAttribute('data-jev-idx'" in sensor.ELEMENTS_JS
    assert "removeAttribute('data-jev-idx')" in sensor.ELEMENTS_JS


def test_normalize_truncates_and_keeps_indexes_aligned():
    raw = [
        {"idx": 1, "tag": "a", "text": "一"},
        {"idx": 2, "tag": "input", "text": ""},
        {"idx": 3, "tag": "button", "text": "三"},
    ]
    out = sensor.normalize(raw, limit=2)
    assert [e["idx"] for e in out] == [1, 2]
    assert out[0]["is_input"] is False          # 缺失时按 tag 推断
    assert out[1]["is_input"] is True


def test_normalize_falls_back_to_placeholder_for_label():
    out = sensor.normalize([{"idx": 1, "tag": "input", "text": "", "placeholder": "搜索"}])
    assert out[0]["label"] == "搜索"


def test_normalize_does_not_mutate_input():
    raw = [{"idx": 1, "tag": "a", "text": "一"}]
    sensor.normalize(raw)
    assert raw == [{"idx": 1, "tag": "a", "text": "一"}]


def test_collect_and_resolve_on_real_browser_or_skip():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    try:
        ctx = sync_playwright().start()
        browser = ctx.chromium.launch(headless=True)
    except Exception as exc:                       # 只有环境不可用才 skip，断言失败仍算失败
        pytest.skip("真实浏览器不可用：%s" % exc)

    try:
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.set_content(
            "<button>Search</button>"
            "<input placeholder='站内搜索' name='q' type='search'>"
        )
        elements = sensor.collect(page)
        assert elements, "应采集到可交互元素"
        assert [e["idx"] for e in elements] == list(range(1, len(elements) + 1))
        assert any(e["is_input"] for e in elements)
        assert any(e["label"] == "站内搜索" for e in elements)

        handle = sensor.resolve(page, elements[0]["idx"])
        assert handle is not None, "resolve 必须能按编号取回真实元素"
        assert handle.evaluate("e => e.tagName.toLowerCase()") == elements[0]["tag"]
    finally:
        try:
            browser.close()
        finally:
            ctx.stop()
