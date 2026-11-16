"""
打分分母口径自检 —— 不写进交付物，只用来验证改动对不对。

新口径：JD 没提的维度不进分子也不进分母（真筛选项除外）。
老口径：JD 没提 = 一律 40 分，照常加权。

这个脚本把同一批岗位用两种口径各算一遍，打印：
  1. 两条 ≥自动发送线（scoring.thresholds.strong）的数量各是多少
  2. 分数整体移动了多少
  3. 一个手工算的例子，手算与代码是否对得上（防止公式写错自己都看不出来）
"""

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import filters
import scorer
from config_loader import path_of

OLD_NEUTRAL = 40


def score_old(job, layer1_result):
    """改之前的行为：JD 没提就 40 分，权重照算。"""
    jd = job.get("jd_text") or ""
    title = job.get("title") or ""
    sents = filters._split_jd(jd) + [title]
    family = layer1_result.get("family")
    total = 0.0
    for dim in scorer.DIMS:
        key = dim["key"]
        words = scorer.EVIDENCE_WORDS.get(key, [])
        if key == "role_match":
            if family:
                s = 90
            elif (layer1_result.get("insight_hits") or {}).get("insight"):
                s = 75
            else:
                s = 0
        elif key == "decomp_insight" and family is None:
            hits_map = layer1_result.get("insight_hits") or {}
            if hits_map.get("insight"):
                s = 100
            else:
                s, _e, _st = scorer._calc(sents, words)
        else:
            s, _e, st = scorer._calc(sents, words)
            if st == "no_evidence" and key not in scorer.STRICT_KEYS:
                s = OLD_NEUTRAL
        total += s * dim["weight"] / 100.0
    return round(total, 1)


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else path_of("data/jobs_today.json")
    if not os.path.exists(src):
        print("找不到数据文件：", src)
        return
    jobs = json.load(open(src, encoding="utf-8"))
    print(f"读进 {len(jobs)} 条岗位：{src}\n")

    rows = []
    for j in jobs:
        ok0, _why = filters.layer0(j)
        if not ok0:
            continue
        acted = filters.layer1(j)
        if not acted.get("passed"):
            continue
        j["family"] = acted.get("family")
        new = scorer.score_job(j, acted)
        rows.append((new["title"], new["company"], score_old(j, acted), new["score"], new["detail"]))

    rows.sort(key=lambda r: -r[3])
    _line = scorer.THRESH["strong"]   # 自动发送线（读配置，不再硬编码 85）
    strong_old = [r for r in rows if r[2] >= _line]
    strong_new = [r for r in rows if r[3] >= _line]

    print("=" * 78)
    print(f"过粗筛的岗位：{len(rows)} 条")
    print(f"  老口径 ≥{_line}（能自动发）：{len(strong_old)} 条")
    print(f"  新口径 ≥{_line}（能自动发）：{len(strong_new)} 条")
    if rows:
        avg_old = sum(r[2] for r in rows) / len(rows)
        avg_new = sum(r[3] for r in rows) / len(rows)
        print(f"  平均分：{avg_old:.1f} → {avg_new:.1f}（+{avg_new - avg_old:.1f}）")
    print("=" * 78)
    print("\n新口径前 12 名（职务 / 公司 / 老分 → 新分）：")
    for r in rows[:12]:
        counted = [k for k, d in r[4].items() if isinstance(d, dict) and d.get("counted")]
        skipped = [k for k, d in r[4].items() if isinstance(d, dict) and d.get("counted") is False]
        print(f"  {r[3]:6.1f}  {r[2]:6.1f}  {r[0][:22]:24s} {r[1][:14]:16s} 算:{','.join(counted)}  没算:{','.join(skipped)}")

    # ── 手算校验：挑前 3 条，用 detail 里的原始分手工重算一遍 ──
    # 注意：scorer 的最终分 = 摊分后的 base（封顶 100）+ bonus - penalty。
    # 手算必须把 detail 里的 bonus/penalty 也加减进去，否则和代码永远对不上，
    # 只会产生假警报（这是 2026-10-30 执行验证时抓出来的自检脚本自身过时）。
    print("\n手算校验（用 detail 里的原始分重算，应等于代码输出）：")
    for r in rows[:3]:
        num = den = 0.0
        for k, d in r[4].items():
            if isinstance(d, dict) and d.get("counted"):
                num += d["score"] * d["weight"]
                den += d["weight"]
        base = num / den if den else 0.0
        base = min(100.0, base)
        # bonus/penalty 存在 detail 里是带正负号的字符串列表，反解出数值
        def _sum_delta(items):
            tot = 0.0
            for s in items or []:
                m = re.search(r"([+-]\d+)$", str(s))
                if m:
                    tot += int(m.group(1))
            return tot
        delta = _sum_delta(r[4].get("bonus")) + _sum_delta(r[4].get("penalty"))
        hand = round(base + delta, 1)
        ok = abs(hand - r[3]) < 0.15
        print(f"  {'OK ' if ok else '✗✗ '} {r[0][:20]:22s} 手算 {hand} vs 代码 {r[3]}  (base={base:.1f} 加分罚分={delta:+.0f} 分母权重合计 {den})")

    # ── 极端检验：真筛选项缺失时，不许被抬高分 ──
    print("\n极端检验：一条 JD 里完全没提 AI —— 新口径应该依然把它压住")
    fake = {"job_id": "test", "title": "产品助理", "jd_text": "负责日常文档整理与会议安排。每周到岗 4 天。",
            "company": "测试公司", "hr_activity": "d3"}
    acted = filters.layer1(fake)
    if acted.get("passed"):
        s = scorer.score_job(fake, acted)
        d = s["detail"]
        print(f"  ai_core = {d['ai_core']['score']} 分（应为 0，且在分母内 counted={d['ai_core']['counted']}）")
        print(f"  总分 = {s['score']}（应该 <55 被丢掉）")


if __name__ == "__main__":
    main()
