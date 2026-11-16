# -*- coding: utf-8 -*-
"""列表岗招呼语生成器（独立脚本，不碰 greeting.py 主逻辑）。

背景：抓到的列表岗只有 title/company/boss 等，没有 jd_text 正文。
      greeting.build() 依赖 rank_jd_points（要 JD 正文匹配能力点），
      无 JD 时 plan 为空 → build 返回 None。

这里复用 greeting.py 里所有固定框架（称呼/学历句/实习句/动机/到岗/结尾），
只把「能力点→证据」这一段，替换成「按岗位标题关键词 → 取锚点证据」的轻量规则。
不改 greeting.py 任何一行，不影响现有 114 条回归。

差异化三要素（本人 2026-11-02 拍板）：
  1) 称呼：salutation_for 按 boss_name 区分（张总/刘姐姐）
  2) 岗位标题：按标题关键词挑不同锚点证据
  3) 公司名：动机句里可带（motivation_line 已支持 company）
"""

import json
import re
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import greeting as G


# ============ 剧本杀三模块（哈希轮换）+ 心理学 固定组合 ============
# 本人 2026-11-02 拍板：列表岗招呼语固定用「剧本杀项目 + 心理学」两块证据，
# 剧本杀项目从 3 个模块里按 job_id 哈希取余选 1 个（复用主逻辑 _pick_variant 的
# 同一套「余数」轮换思路），保证不同岗位错开、同一岗位永远同一模块。
RAG_MODULES = ["project_rag", "project_dm_match", "project_review_loop"]


def _hash_pick(items, job):
    """用 job_id 的稳定哈希取余选 items 里一个下标。

    跟 greeting._pick_variant 用的是同一个哈希算法（h*131+ord 取低32位），
    保证「同岗位永远同下标、不同岗位各自错开」。
    """
    jid = (job or {}).get("job_id") or (job or {}).get("id") or ""
    if not jid:
        return items[0]
    h = 0
    for ch in str(jid):
        h = (h * 131 + ord(ch)) & 0xFFFFFFFF
    return items[h % len(items)]


def _anchor_short(key):
    a = G.ANCHORS.get(key)
    if not a:
        return None
    s = (a.get("short") or a.get("text") or "").strip()
    return s


def _evidence_parts(job):
    """固定拼「剧本杀模块（哈希选）+ 心理学」两块证据。

    规则（本人 2026-11-02 定，只服务列表岗、不碰主逻辑）：
      1) 剧本杀模块用 job_id 哈希在 3 模块里轮换，总起句只加这一次；
      2) 剧本杀正文（挂了 github 的锚点）末尾追加 GitHub 话术（走 _evp_rag_text 哈希变体）；
      3) 心理学是能力句（say_as=ability），前缀用「我也」，不用「也做过」；
      4) 不硬塞能力句（列表岗没有 JD 能力点，ability_clause 需要的名字从 JD 来，这里没有）。
    """
    module_key = _hash_pick(RAG_MODULES, job)
    parts = []

    # 第一块：剧本杀模块（加总起句 + 末尾挂 GitHub 话术）
    a = G.ANCHORS.get(module_key) or {}
    s = _anchor_short(module_key)
    if s:
        body = s
        # 总起句走哈希变体（_evp_rag_opener），跟主逻辑同一套轮换。
        if G.INTRO_FLOW_ON and G.INTRO_RAG_OPENER:
            opener = G._evp_rag_opener()
            if opener and opener not in body:
                body = opener + "，" + body
        # GitHub 话术：只对挂了 github 的剧本杀锚点追加，走哈希变体。
        if G.RAG_ON and G.RAG_TEXT and a.get("github"):
            _rt = G._evp_rag_text()
            if _rt and _rt not in body:
                body = body + "，" + _rt
        parts.append("%s。" % body)

    # 第二块：心理学（能力句，前缀「我也」）
    # 本人 2026-11-02 拍板：列表岗一律用完整版（带 <数据1>/约<数据2>/约<数据3> 三个数字）。
    ps = (G.ANCHORS.get("psychology") or {}).get("text") or ""
    ps = ps.strip()
    if ps:
        parts.append("我也%s。" % ps)

    return parts, module_key


def build_list_greeting(job):
    """给只有 title 没有 jd_text 的列表岗生成一条招呼语。

    返回 {'text': ..., 'anchor_group': ...} 或 None。

    所有「固定话术」段（实习句/总起句/GitHub句/学历句/动机句/到岗句/结尾句）
    全部走主系统同一套 job_id 哈希变体轮换，跟 greeting.build 口径一致。
    """
    # 0) 先按本条 job 把三处「内部变体」（实习句/总起句/GitHub句）算好，
    #    存入主系统模块级变量 _CUR_*，这样 _evp_intern/_evp_rag_opener/_evp_rag_text
    #    才能取到「对应当前这条岗位」的正确变体（而不是残留的旧值或退回固定值）。
    G._set_current_variants(job)

    # 1) 称呼（复用）
    sal = G.salutation_for(job)

    # 2) 学历句（哈希轮换，跟主逻辑 1341 行一致）
    intro_text = G._pick_variant((G.SPEC.get("self_intro") or {}).get("variants"),
                                 job, G.SELF_INTRO)
    intro = "，" + intro_text + "。" if intro_text else "，"

    # 3) 实习句（哈希轮换，走 _evp_intern，此时 _CUR_INTERN 已按本条 job 算好）
    intern_part = ""
    if G.INTRO_FLOW_ON and G.INTRO_INTERN_LINE:
        intern_part = G._evp_intern() + "。"

    # 4) 证据句（固定：剧本杀模块哈希轮换 + 心理学）
    ev_parts, module_key = _evidence_parts(job)
    evidence = "".join(ev_parts)

    # 5) 动机句 + 到岗句（复用，内部各自走哈希）
    line = G.motivation_line(job)
    avail_txt = G.availability_line(job)
    if avail_txt:
        mid = line + "，" + avail_txt + "。"
    else:
        mid = line + "。"

    # 6) 结尾（哈希轮换，跟主逻辑 1381 行一致）
    if G.CLOSING_FIXED:
        closing = G._pick_variant((G.SPEC.get("closing") or {}).get("fixed_variants"),
                                  job, G.CLOSING_FIXED_TEXT or G.CLOSINGS[0]["text"])
    else:
        closing = G.CLOSINGS[0]["text"]

    text = sal + intro + intern_part + evidence + mid + closing
    if text and not text.endswith(("。", "！", "？")):
        text += "。"

    return {"text": text, "anchor_group": module_key}


def main():
    jobs_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "jobs_list.json")
    jobs_path = os.path.normpath(jobs_path)
    with open(jobs_path, encoding="utf-8") as f:
        jobs = json.load(f)

    out = []
    ok = 0
    for job in jobs:
        r = build_list_greeting(job)
        if r and r["text"]:
            ok += 1
            out.append({
                "job_id": job.get("job_id"),
                "title": job.get("title"),
                "company": job.get("company"),
                "city": job.get("city"),
                "salary": job.get("salary_raw"),
                "boss_name": job.get("boss_name"),
                "boss_title": job.get("boss_title"),
                "url": job.get("url"),
                "anchor_group": r["anchor_group"],
                "greeting": r["text"],
            })
        else:
            out.append({
                "job_id": job.get("job_id"),
                "title": job.get("title"),
                "company": job.get("company"),
                "city": job.get("city"),
                "salary": job.get("salary_raw"),
                "boss_name": job.get("boss_name"),
                "boss_title": job.get("boss_title"),
                "url": job.get("url"),
                "anchor_group": "failed",
                "greeting": "",
            })

    out_path = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "list_greetings.json"))
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print(f"生成完成：{ok}/{len(jobs)} 条成功")
    print(f"输出：{out_path}")

    # 打印几条样例
    print("\n===== 样例（前 5 条）=====")
    for x in out[:5]:
        print(f"\n【{x['title']} | {x['company']} | {x['city']}】")
        print(f"  分组: {x['anchor_group']}")
        print(f"  {x['greeting']}")


if __name__ == "__main__":
    main()
