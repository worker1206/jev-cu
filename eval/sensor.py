"""传感器层：把页面压成带编号的紧凑元素列表（jev-cu 的 state 采集核心）"""
from playwright.sync_api import sync_playwright

JS = """
() => {
  const els = [...document.querySelectorAll('a,button,input,select,textarea,[role=button],[role=link],[role=searchbox],[role=tab]')];
  const out = [];
  let i = 1;
  for (const e of els) {
    const r = e.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    if (e.disabled || e.getAttribute('aria-hidden') === 'true') continue;
    const txt = (e.innerText || e.value || e.placeholder || e.getAttribute('aria-label') || e.getAttribute('title') || '').trim().replace(/\\s+/g,' ').slice(0, 40);
    const vis = r.top >= 0 && r.top < innerHeight;
    out.push({idx: i++, tag: e.tagName.toLowerCase(), text: txt,
              visible: vis, x: Math.round(r.x), y: Math.round(r.y)});
  }
  return out;
}
"""

def collect(url, limit=25):
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        pg = b.new_page(viewport={"width":1280,"height":800})
        pg.goto(url, wait_until="domcontentloaded", timeout=30000)
        pg.wait_for_timeout(1200)
        els = [e for e in pg.evaluate(JS) if e["visible"]][:limit]
        title = pg.title()
        b.close()
        return title, els

if __name__ == "__main__":
    t, els = collect("https://example.com")
    print(t); [print(e) for e in els]
