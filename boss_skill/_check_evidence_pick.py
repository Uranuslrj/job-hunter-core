"""模型「只选不写」接入后的实测：跑 N 条真实岗位，数出到底发生了什么。

回答四个问题（都是能数出来的，不靠感觉）：
  ① 每条招呼语实际问了几次模型、花了多少秒
  ② 过门率、字数有没有变差
  ③ 模型真的改了规则版的选择吗？改了几条
  ④ 模型失败（超时/乱回）时，是不是真的退回规则版了

用法：
  python boss_skill/_check_evidence_pick.py          # 默认 10 条
  python boss_skill/_check_evidence_pick.py 20       # 20 条
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import greeting
import evidence_pick
from greeting import draft


def load_jobs():
    p = os.path.join(ROOT, "data", "jobs_today.json")
    d = json.load(open(p, encoding="utf-8"))
    return [j for j in d
            if j.get("title") and j.get("company")
            and (j.get("jd_text") or "").strip()]


def run_batch(jobs, use_model):
    old = greeting._evidence_pick_on
    if not use_model:
        greeting._evidence_pick_on = lambda: False
    rows = []
    try:
        for j in jobs:
            evidence_pick.begin_greeting()
            t0 = time.time()
            g = draft(j)
            dt = time.time() - t0
            rows.append({
                "title": j.get("title"),
                "company": j.get("company"),
                "calls": evidence_pick.calls_used(),
                "sec": dt,
                "chars": (g or {}).get("chars"),
                "ok": bool((g or {}).get("ok")),
                "text": (g or {}).get("text") or "",
            })
    finally:
        greeting._evidence_pick_on = old
    return rows


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    jobs = load_jobs()[:n]
    print(f"样本：{len(jobs)} 条（全部来自 jobs_today.json，都有 JD 正文）")
    print(f"开关：evidence_pick.enabled = {greeting.EP_CFG.get('enabled')}，"
          f"环境变量 BOSS_EVIDENCE_PICK = {os.environ.get('BOSS_EVIDENCE_PICK') or '(未设)'}")
    print()

    print("① 先跑【模型版】（每条实际调几次模型、多久）...")
    t = time.time()
    mrows = run_batch(jobs, use_model=True)
    mt = time.time() - t
    print(f"   完成，总耗时 {mt:.0f} 秒")
    print()

    print("② 再跑【规则版】做对照（开关临时关掉，一次模型都不调）...")
    t = time.time()
    rrows = run_batch(jobs, use_model=False)
    rt = time.time() - t
    print(f"   完成，总耗时 {rt:.0f} 秒")
    print()

    print("=" * 76)
    print("逐条对比")
    print("=" * 76)
    print(f"{'岗位':<22}{'调模型':>6}{'秒':>7}{'字数(模型/规则)':>18}{'过门':>6}  差异")
    print("-" * 76)
    for m, r in zip(mrows, rrows):
        same = "一样" if m["text"] == r["text"] else "★ 不同"
        title = (m["title"] or "")[:20]
        print(f"{title:<22}{m['calls']:>6}{m['sec']:>7.1f}"
              f"{str(m['chars']) + ' / ' + str(r['chars']):>18}"
              f"{'✅' if m['ok'] else '❌':>6}  {same}")

    print()
    print("=" * 76)
    print("汇总")
    print("=" * 76)
    m_ok = sum(1 for r in mrows if r["ok"])
    r_ok = sum(1 for r in rrows if r["ok"])
    m_ch = [r["chars"] for r in mrows if r["chars"]]
    r_ch = [r["chars"] for r in rrows if r["chars"]]
    calls = sum(r["calls"] for r in mrows)
    secs = [r["sec"] for r in mrows]
    diff = sum(1 for m, r in zip(mrows, rrows) if m["text"] != r["text"])
    no_call = sum(1 for r in mrows if r["calls"] == 0)

    print(f"过门        模型版 {m_ok}/{len(mrows)}　规则版 {r_ok}/{len(rrows)}")
    if m_ch and r_ch:
        print(f"平均字数    模型版 {sum(m_ch)/len(m_ch):.1f}　规则版 {sum(r_ch)/len(r_ch):.1f}")
    print(f"模型调用    共 {calls} 次（平均每条 {calls/len(mrows):.1f} 次，"
          f"{no_call} 条一次没调）")
    print(f"单条耗时    平均 {sum(secs)/len(secs):.1f} 秒，最慢 {max(secs):.1f} 秒")
    print(f"结果差异    {diff} / {len(mrows)} 条与规则版不同")
    print()
    if diff:
        print("★ 有差异的样例（模型干了什么）：")
        for m, r in zip(mrows, rrows):
            if m["text"] != r["text"]:
                print(f"\n  【{m['title']} @ {m['company']}】")
                print(f"    规则版：{r['text'][:160]}...")
                print(f"    模型版：{m['text'][:160]}...")


if __name__ == "__main__":
    main()
