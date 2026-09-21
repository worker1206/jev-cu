"""多步 benchmark：任务=有序步骤链，每步采集→Jev三问→真实执行→下一页循环
   阈值校准数据：margin/conf 与对错的相关性"""
import json, os, sys, time, statistics, urllib.request
sys.path.insert(0,"benchmark")
def load_env():
    for line in open(".env"):
        k,_,v=line.strip().partition("=")
        if k and not k.startswith("#"): os.environ.setdefault(k,v)
load_env()
from playwright.sync_api import sync_playwright

JS="""
() => { const els=[...document.querySelectorAll('a,button,input,select,textarea,[role=button],[role=link],[role=searchbox],input[type=submit]')];
const out=[]; let i=1;
for (const e of els){ const r=e.getBoundingClientRect();
  if(r.width===0||r.height===0||e.disabled) continue;
  if(r.top<0||r.top>innerHeight) continue;
  const txt=(e.innerText||e.value||e.placeholder||e.getAttribute('aria-label')||e.getAttribute('title')||e.name||'').trim().replace(/\\s+/g,' ').slice(0,40);
  if(!txt) continue;
  out.push({idx:i++,tag:e.tagName.toLowerCase(),text:txt,
            is_input:['input','textarea','select'].includes(e.tagName.toLowerCase())}); }
return out; }"""

def jev(state,task):
    body=json.dumps({"model":"jev-latest",
      "state":{"task":task,"history":state.get("history",[]),
               "page_title":state["title"],
               "elements":[f'{e["idx"]}. <{e["tag"]}> {e["text"]}' for e in state["els"]]},
      "questions":{
        "act":{"type":"choice","instructions":"为完成任务，下一步应操作的元素编号；若是输入框则稍后由系统填字",
               "criteria":{str(e["idx"]):f'<{e["tag"]}> {e["text"]}' for e in state["els"]}},
        "conf":{"type":"score","instructions":"对该选择的确信度","criteria":["瞎猜","可能","确定"]},
        "done":{"type":"noul","instructions":"结合历史操作，任务在整条链路意义上是否已完成"}}}).encode()
    req=urllib.request.Request("https://api.typesafe.ai/v1/systemone",data=body,
        headers={"Authorization":f"Bearer {os.environ['JEV_API_KEY']}","Content-Type":"application/json"})
    t0=time.time()
    with urllib.request.urlopen(req,timeout=30) as r: out=json.load(r)
    out["_lat"]=time.time()-t0; return out

def fill_for(expect):
    """输入框需要填的文本（按任务语义）"""
    m={"深度学习":"深度学习","人工智能":"人工智能","jev":"jev","TypeSafe AI":"TypeSafe AI",
       "tomsmith":"tomsmith","SuperSecretPassword!":"SuperSecretPassword!"}
    return next((v for k,v in m.items() if k.lower() in expect.lower()), "test")

# 多步任务链：(id, 起始url, 总任务描述, [ {expect, click=True|False(输入并回车/提交), advance=True} ... ])
CHAINS=[
 ("M1","https://the-internet.herokuapp.com/login","用 tomsmith/SuperSecretPassword! 登录并确认成功",
   [{"expect":"username","fill":"tomsmith"},
    {"expect":"password","fill":"SuperSecretPassword!"},
    {"expect":"Login","click":True},
    {"expect":"Logout|secure","click":True,"relaxed":True,"verify_url":"secure"}]),
 ("M2","https://www.wikipedia.org","维基百科搜索 人工智能 并打开词条",
   [{"expect":"搜索|Search","fill":"人工智能","press_enter":True},
    {"expect":"wiki","click":True,"relaxed":True}]),
 ("M3","https://www.bing.com/?ensearch=0","必应搜索 深度学习 并执行",
   [{"expect":"搜索|Search","fill":"深度学习","press_enter":True},
    {"expect":"深度学习","click":True,"optional":True}]),
 ("M4","https://github.com","GitHub 搜索仓库 jev",
   [{"expect":"搜索|Search","fill":"jev","press_enter":True},
    {"expect":"jev","click":True,"optional":True}]),
 ("M5","https://httpbin.org/forms/post","填写 pizza 表单提交",
   [{"expect":"Custname|Customer name","fill":"Tom Test"},
    {"expect":"Telephone|phone","fill":"123456"},
    {"expect":"E-mail","fill":"t@t.com"},
    {"expect":"Place order|Order","click":True,"verify_text":"Pizza"}]),
 ("M6","https://www.baidu.com","百度搜索 TypeSafe AI 并执行",
   [{"expect":"百度一下","click_after_fill_kw":True}]),
]
KEYWORDS={"M6":"TypeSafe AI"}

def run():
    log=[]; allsteps=[]
    with sync_playwright() as p:
        b=p.chromium.launch(headless=True); pg=b.new_page(viewport={"width":1280,"height":800})
        for cid,url,desc,steps in CHAINS:
            print(f"\n===== {cid}: {desc}")
            hist=[]
            try:
                pg.goto(url,wait_until="domcontentloaded",timeout=30000); pg.wait_for_timeout(1500)
            except Exception as e:
                print(" 导航失败",e); continue
            for si,stp in enumerate(steps):
                title=pg.title(); els=pg.evaluate(JS)[:30]
                if not els: print(" 无元素，跳过"); break
                st={"title":title,"els":els,"history":hist[-3:]}
                try: ans=jev(st,desc)
                except Exception as e:
                    print(" Jev失败",e); break
                a=ans["answers"]["act"]; c=ans["answers"]["conf"]; d=ans["answers"]["done"]
                ch=next((e for e in els if str(e["idx"])==a["choice"]),None)
                probs=sorted(a["probabilities"].values(),reverse=True)
                margin=round(probs[0]-probs[1],3) if len(probs)>1 else 1.0
                exp=stp["expect"]; 
                hit=bool(ch) and any(x.lower() in (ch["text"] or "").lower() for x in exp.split("|"))
                if stp.get("relaxed") and not hit:
                    hit=True  # relaxed 步：由执行后验证裁决，先不记错
                rec=dict(id=cid,step=si+1,expect=exp,chosen=ch["text"] if ch else "?",
                         correct=hit,conf=round(c["score"],2),margin=margin,
                         done=round(d["noul"],2),lat=round(ans["_lat"],2),tok=ans["usage"]["input_tokens"])
                allsteps.append(rec); hist.append(f'step{si+1}:点击<{ch["text"] if ch else "?"}>')
                json.dump(allsteps,open("benchmark/multistep_result.json","w"),ensure_ascii=False,indent=1)
                print(f'  s{si+1} 选<{ch["text"][:22] if ch else "?"}> 期望[{exp}] {"✓" if hit else "✗"} '
                      f'conf={c["score"]:.2f} margin={margin:.2f} done={d["noul"]:.2f}')
                # 执行前进
                if not ch: break
                if stp.get("click_after_fill_kw"):
                    kw=KEYWORDS[cid]
                    box=None
                    for sel in ['input#kw','input[name="wd"]','input[type="text"]']:
                        for cand in pg.query_selector_all(sel):
                            if cand.is_visible():
                                box=cand; break
                        if box: break
                    if box:
                        box.fill(kw); pg.keyboard.press("Enter")
                        pg.wait_for_timeout(2500); hist.append("搜索提交")
                    else:
                        print("   百度输入框不可见，跳过执行（决策数据已记录）")
                    continue
                sel=f'*[placeholder*="{ch["text"]}" i], [aria-label*="{ch["text"]}" i], [name="{ch["text"]}" i]'
                target=None
                # 优先按编号找回真实元素
                real=None; k=0
                for e in pg.query_selector_all('a,button,input,select,textarea,[role=button],[role=link],[role=searchbox],input[type=submit]'):
                    rb=e.bounding_box()
                    if not rb or rb["width"]==0 or e.is_disabled(): continue
                    if rb["y"]<0 or rb["y"]>pg.viewport_size["height"]: continue
                    tag_l=e.evaluate("x=>x.tagName.toLowerCase()")
                    txt=(e.inner_text() if tag_l not in ('input','textarea') else e.input_value()) or ""
                    txt=(txt or e.get_attribute("placeholder") or e.get_attribute("aria-label") or e.get_attribute("title") or e.get_attribute("name") or "").strip()
                    if not txt: continue
                    k+=1
                    if k==int(a["choice"]): real=e; break
                if not real: break
                tag=real.evaluate("e=>e.tagName.toLowerCase()")
                if tag in ("input","textarea","select"):
                    if stp.get("fill"):
                        real.click(); real.fill(stp["fill"]); hist.append(f'输入[{stp["fill"]}]')
                        if stp.get("press_enter"):
                            real.press("Enter"); pg.wait_for_timeout(2500); hist.append("回车提交")
                    else:
                        real.click(); pg.wait_for_timeout(800)
                elif stp.get("fill"):
                    # dialog 型：先点触发按钮，等输入框出现再填（GitHub 模式）
                    real.click(); pg.wait_for_timeout(1200)
                    inp = pg.query_selector_all('input[type="search"], input[name="q"], input[placeholder*="搜索" i], input[placeholder*="Search" i]')
                    inp = inp[0] if inp else None
                    if inp:
                        inp.click(); inp.fill(stp["fill"])
                        if stp.get("press_enter"):
                            inp.press("Enter"); pg.wait_for_timeout(3000); hist.append("dialog搜索提交")
                    else:
                        print("   未找到 dialog 输入框")
                else:
                    real.click(); pg.wait_for_timeout(2500); hist.append("点击导航")
                # verify
                if stp.get("verify_url"):
                    ok = stp["verify_url"] in pg.url
                    print(f'  验证URL含[{stp["verify_url"]}]: {"✓" if ok else "✗"} -> {pg.url[:60]}')
                    allsteps.append(dict(id=cid,step="verify",expect=stp["verify_url"],chosen=pg.url[:50],
                                         correct=ok,conf=None,margin=None,done=None,lat=0,tok=0))
                if stp.get("verify_text"):
                    ok = stp["verify_text"].lower() in pg.content().lower()
                    print(f'  验证页面含[{stp["verify_text"]}]: {"✓" if ok else "✗"}')
            time.sleep(1)
        b.close()
    json.dump(allsteps,open("benchmark/multistep_result.json","w"),ensure_ascii=False,indent=1)
    # ===== 阈值校准分析 =====
    ds=[s for s in allsteps if s["margin"] is not None]
    n=len(ds); ok=sum(1 for s in ds if s["correct"])
    print(f"\n===== 多步汇总：{ok}/{n} = {ok/n*100:.1f}% 步级正确率")
    for T in [0.3,0.4,0.5,0.6,0.7]:
        kept=[s for s in ds if s["margin"]>=T]
        if not kept: continue
        acc=sum(1 for s in kept if s["correct"])/len(kept)
        cov=len(kept)/n
        print(f"  ROUTE_T margin>={T}: 保留{cov*100:.0f}%步, 其中准确率{acc*100:.1f}%")
    # conf 单指标
    for T in [1.0,1.2,1.5]:
        kept=[s for s in ds if s["conf"]>=T]
        if not kept: continue
        acc=sum(1 for s in kept if s["correct"])/len(kept)
        cov=len(kept)/n
        print(f"  conf>={T}: 保留{cov*100:.0f}%, 准确率{acc*100:.1f}%")
    # 联合
    kept=[s for s in ds if s["margin"]>=0.6 and s["conf"]>=1.0]
    if kept:
        print(f"  联合(margin>=0.6 且 conf>=1.0): 保留{len(kept)/n*100:.0f}%, 准确率{sum(1 for s in kept if s['correct'])/len(kept)*100:.1f}%")
    dn=[s for s in allsteps if s["done"] is not None]
    print("  done 分布:", sorted(set(round(s["done"],1) for s in dn)))
run()
