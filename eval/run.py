"""mini benchmark：Jev 三问（act选元素 / conf确信度 / done完成度）每步决策实测"""
import json, time, statistics, sys
sys.path.insert(0, "benchmark")
from sensor import collect
from tasks import TASKS
from playwright.sync_api import sync_playwright
import urllib.request

source(".env") if False else None
# 读环境
def load_env():
    for line in open(".env"):
        k,_,v = line.strip().partition("=")
        if k and not k.startswith("#"): os.environ.setdefault(k,v)
import os
load_env()
KEY = os.environ["JEV_API_KEY"]

def jev(state, task):
    body = json.dumps({
      "model":"jev-latest",
      "state":{"task":task, "page_title":state["title"],
               "elements":[f'{e["idx"]}. <{e["tag"]}> {e["text"]}' for e in state["els"]]},
      "questions":{
        "act": {"type":"choice","instructions":"为完成任务，下一步应点击/操作的元素编号",
                "criteria":{str(e["idx"]):f'<{e["tag"]}> {e["text"]}' for e in state["els"]}},
        "conf":{"type":"score","instructions":"对该选择的确信度","criteria":["瞎猜","可能","确定"]},
        "done":{"type":"noul","instructions":"该页面状态下任务是否已经完成"}
      }}).encode()
    req = urllib.request.Request("https://api.typesafe.ai/v1/systemone", data=body,
        headers={"Authorization":f"Bearer {KEY}","Content-Type":"application/json"})
    t0=time.time()
    with urllib.request.urlopen(req, timeout=30) as r:
        out=json.load(r)
    out["_lat"]=time.time()-t0
    return out

rows=[]; lats=[]
with sync_playwright() as p:
    b=p.chromium.launch(headless=True)
    pg=b.new_page(viewport={"width":1280,"height":800})
    for t in TASKS:
        try:
            pg.goto(t["url"], wait_until="domcontentloaded", timeout=30000)
            pg.wait_for_timeout(1500)
        except Exception as e:
            print(f'{t["id"]} 导航失败: {e}'); continue
        title=pg.title()
        JS = """
        () => { const els=[...document.querySelectorAll('a,button,input,select,textarea,[role=button],[role=link],[role=searchbox],input[type=submit]')];
        const out=[]; let i=1;
        for (const e of els){ const r=e.getBoundingClientRect();
          if(r.width===0||r.height===0||e.disabled) continue;
          if(r.top<0||r.top>innerHeight) continue;
          const txt=(e.innerText||e.value||e.placeholder||e.getAttribute('aria-label')||'').trim().replace(/\\s+/g,' ').slice(0,40);
          out.push({idx:i++,tag:e.tagName.toLowerCase(),text:txt}); }
        return out; } """
        els=pg.evaluate(JS)[:30]
        if not els: print(f'{t["id"]} 无可交互元素'); continue
        state={"title":title,"els":els}
        try:
            ans=jev(state,t["task"])
        except Exception as e:
            print(f'{t["id"]} Jev调用失败: {e}'); continue
        a=ans["answers"]["act"]; c=ans["answers"]["conf"]; d=ans["answers"]["done"]
        chosen=next((e for e in els if str(e["idx"])==a["choice"]), None)
        probs=sorted(a["probabilities"].values(), reverse=True)
        margin=probs[0]-probs[1] if len(probs)>1 else 1.0
        hit = bool(chosen) and (t["expect"].lower() in chosen["text"].lower() or t["expect"].lower() in chosen["tag"])
        lats.append(ans["_lat"])
        rows.append({**t,"chosen_idx":a["choice"],"chosen_text":chosen["text"] if chosen else "?",
                     "hit":hit,"conf":round(c["score"],2),"margin":round(margin,3),
                     "done_p":d["noul"],"lat":round(ans["_lat"],2),
                     "tok":ans["usage"]["input_tokens"]})
        print(f'{t["id"]} 选{a["choice"]}<{chosen["text"][:20] if chosen else "?"}> 期望[{t["expect"]}] '
              f'{"✓" if hit else "✗"} conf={c["score"]:.2f} margin={margin:.2f} done={d["noul"]:.2f} {ans["_lat"]:.2f}s')
        pg.wait_for_timeout(800)
    b.close()

n=len(rows); ok=sum(r["hit"] for r in rows)
print("\n========== MINI BENCHMARK RESULT ==========")
print(f"任务数: {n}   命中: {ok}   准确率: {ok}/{n} = {ok/n*100:.1f}%" if n else "无数据")
print(f"弃权口径1(conf<1.0): {sum(1 for r in rows if r['conf']<1.0)}/{n}")
print(f"弃权口径2(margin<0.6): {sum(1 for r in rows if r['margin']<0.6)}/{n}")
print(f"平均延迟: {statistics.mean(lats):.2f}s   平均input_tokens: {statistics.mean(r['tok'] for r in rows):.0f}")
json.dump(rows, open("benchmark/result.json","w"), ensure_ascii=False, indent=1)
print("明细已写 benchmark/result.json")
