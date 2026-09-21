"""元素快照采集：把页面压成带稳定编号的紧凑元素列表。

设计教训（来自 benchmark）：
  input 的可见文本常为空，必须回落到 placeholder / aria-label / name / title，
  否则模型再准也会被喂成瞎子（历史上因此产生 5 个"假错误"）。
"""
from __future__ import annotations

SELECTOR = (
    "a,button,input,select,textarea,[role=button],[role=link],"
    "[role=searchbox],[role=tab],input[type=submit]"
)

# 采集 JS：给每个可见可交互元素打上稳定编号 data-jev-idx
ELEMENTS_JS = r"""
() => {
  document.querySelectorAll('[data-jev-idx]').forEach(e => e.removeAttribute('data-jev-idx'));
  const sel = 'a,button,input,select,textarea,[role=button],[role=link],[role=searchbox],[role=tab],input[type=submit]';
  const out = [];
  let i = 1;
  for (const e of document.querySelectorAll(sel)) {
    const r = e.getBoundingClientRect();
    if (r.width === 0 || r.height === 0 || e.disabled) continue;
    if (r.top < 0 || r.top > window.innerHeight) continue;
    const tag = e.tagName.toLowerCase();
    const text = (e.innerText || '').trim().replace(/\s+/g, ' ').slice(0, 40);
    const value = ('value' in e && e.value) ? String(e.value).slice(0, 40) : '';
    const attrs = {
      placeholder: e.getAttribute('placeholder') || '',
      aria_label: e.getAttribute('aria-label') || '',
      name: e.getAttribute('name') || '',
      type: e.getAttribute('type') || '',
      title: e.getAttribute('title') || ''
    };
    const label = text || value || attrs.placeholder || attrs.aria_label
                  || attrs.title || attrs.name || '';
    if (!label) continue;
    e.setAttribute('data-jev-idx', String(i));
    out.push({
      idx: i++, tag: tag, label: label, text: text, value: value,
      placeholder: attrs.placeholder, aria_label: attrs.aria_label,
      name: attrs.name, type: attrs.type, title: attrs.title,
      is_input: ['input', 'textarea', 'select'].includes(tag)
    });
  }
  return out;
}
"""


def normalize(elements, limit: int = 30):
    """规范化 + 截断，保证编号连续（截断后编号仍与页面 data-jev-idx 一致）。"""
    out = []
    for e in elements[:limit]:
        item = dict(e)
        item.setdefault("label", item.get("text") or item.get("placeholder") or "")
        item.setdefault("is_input", item.get("tag") in ("input", "textarea", "select"))
        out.append(item)
    return out


def collect(page, limit: int = 30):
    """从 Playwright page 采集元素快照。"""
    return normalize(page.evaluate(ELEMENTS_JS), limit=limit)


def resolve(page, idx: str):
    """按编号取回真实元素句柄（依赖采集时写入的 data-jev-idx，保证编号稳定）。"""
    return page.query_selector('[data-jev-idx="%s"]' % str(idx))


def to_state_text(elements):
    """渲染成给 Jev 的紧凑状态文本：`编号. <tag> 标签`。"""
    return "\n".join("  %s. <%s> %s" % (e["idx"], e["tag"], e["label"]) for e in elements)


def build_criteria(elements):
    """choice 问题的 criteria：编号 -> 元素描述。"""
    return {str(e["idx"]): "<%s> %s" % (e["tag"], e["label"]) for e in elements}
