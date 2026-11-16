# 自动评估 —— 每周日晚由 daily.py 触发，平时也可以手动跑
#
# 触发条件（两个都满足才真的算，否则打印"还不到时候"退出）：
#   1. 台账 >= 60 条（60 条只许看量级；284 条才够下"翻倍"的结论，
#      这是用样本量公式算出来的，不是拍的 —— 见 knowledge/offer-workflow-metrics.md）
#   2. 「72 小时已读/回复」列已经填了的行占发出行的 8 成以上
#      （发出去不等于有结果，回复要等 72 小时）
#
# 输出：docs/迭代提案-YYYYMMDD.md —— 全部是提案，改配置仍然要人划勾。
# 这条是设计里定死的：评估只出提案，机器不擅自改 yaml。

import csv
import os
import re
import sys
from datetime import date
from math import sqrt

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config_loader import path_of

LEDGER = path_of("data/greeting_ledger.csv")
MIN_ROWS = 60
MIN_72H_RATIO = 0.8
# 门槛分两档（2026-11-11 本人拍板）：
#   每组 ≥ TREND_N 条 → 先看趋势（"哪个数字冒头了"，不算定论）
#   每组 ≥ VERDICT_N 条 → 才下结论（"可以把它调成默认值"的提案）
TREND_N = 15
VERDICT_N = 40
SAMPLE_FOR_CONCLUSION = 284

# 7 处同义变体的固定位置（跟 greeting.py 里 _vids 列表顺序严格一致），
# 对应 variant_tags 这一列里用逗号拼出来的 id 顺序。
# 顺序：self_intro / intern_line / rag_opener / text_variants / availability /
#       career(E组) / closing
VARIANT_SLOTS = [
    ("self_intro", "自我介绍句"),
    ("intern_line", "实习句"),
    ("rag_opener", "剧本杀总起句"),
    ("text_variants", "GitHub 句"),
    ("availability", "到岗句"),
    ("career", "求职动机"),
    ("closing", "结尾句"),
]


def wilson(k, n, z=1.96):
    if not n:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    ctr = p + z * z / (2 * n)
    half = z * sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return ((ctr - half) / d * 100, (ctr + half) / d * 100)


def load_rows():
    if not os.path.exists(LEDGER):
        return []
    return list(csv.DictReader(open(LEDGER, encoding="utf-8")))


def _pct(k, n):
    return f"{100 * k / n:.1f}% ({k}/{n})" if n else "—"


def _split_variants(tags):
    """把 variant_tags 列（逗号拼的 7 个 id）拆回 7 个槽位，返回 {槽名: id}。

    槽位顺序见 VARIANT_SLOTS。id 可能是 ''（这处没轮换，比如非实习岗的到岗句、
    industry 角度的求职动机）。空 id 不参与分组，避免「未填」混进来污染统计。
    """
    parts = (tags or "").split(",")
    out = {}
    for i, (slot, _name) in enumerate(VARIANT_SLOTS):
        v = parts[i].strip() if i < len(parts) else ""
        if v:
            out[slot] = v
    return out


def main(week_mode=True):
    rows = load_rows()
    sent_rows = [r for r in rows if (r.get("sent") or "").strip() == "已发"]
    n = len(sent_rows)
    print(f"台账里「已发」{n} 条")

    # ── 触发门槛 ──
    if n < MIN_ROWS:
        print(f"还不到评估的时候：已发 {n} 条，满 {MIN_ROWS} 条才跑第一次。")
        print("现在该做的事：继续发、继续填台账。评估本身不着急。")
        return 0
    # 什么叫"结果成熟"（这一行已经可以下判断了）：
    #   ① 台账里填了 72 小时列，或已经有回复记录
    #   ② 或者发出已经超过 7 天 —— 超过 7 天还没回就是没回，不用再等
    import time as _t
    def _parse(s):
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return _t.mktime(_t.strptime((s or "").strip(), fmt))
            except Exception:
                continue
        return None
    now = _t.time()
    filled72 = 0
    for r in sent_rows:
        if (r.get("read_72h") or "").strip() or (r.get("replied") or "").strip():
            filled72 += 1
            continue
        t0 = _parse(r.get("send_time") or "")
        if t0 and (now - t0) >= 7 * 86400:
            filled72 += 1
    ratio = filled72 / n
    if ratio < MIN_72H_RATIO:
        print(f"还不到评估的时候：{filled72}/{n}（{100 * ratio:.0f}%）的结果还没落定，"
              f"满 {100 * MIN_72H_RATIO:.0f}% 才算数。")
        print("（发出不满 7 天、也没有 72 小时记录的那些，还在等对方回复）")
        return 0

    lines = [f"# 迭代提案（{date.today().isoformat()}）", ""]
    lines.append("> 本文件全部是【提案】。改不改、改哪个，由人划勾；机器不擅自改配置。")
    lines.append(f"> 样本 {n} 条。"
                 + ("已达下结论标准（284 条）。" if n >= SAMPLE_FOR_CONCLUSION
                    else f"看量级没问题，下结论还差 {SAMPLE_FOR_CONCLUSION - n} 条。"))
    lines.append("")

    # ── 1. 总回复率 ──
    replied = [r for r in sent_rows if (r.get("replied") or "").strip()]
    good = [r for r in sent_rows if (r.get("ask_resume") or "").strip() == "1"]
    lo, hi = wilson(len(replied), n)
    lo2, hi2 = wilson(len(good), n)
    lines += ["## 1. 回复总览", "",
              f"- 任意回应：{_pct(len(replied), n)}，95% 置信区间 {lo:.1f}~{hi:.1f}%",
              f"- 好回复（要简历/约面试/追问到岗）：{_pct(len(good), n)}，95% 置信区间 {lo2:.1f}~{hi2:.1f}%",
              "",
              "> 只看「任意回应」会高估：里面混着「谢谢不考虑」。判断话术好坏一律看好回复。", ""]

    # ── 2. 变体对比 ──
    # 2026-11-11 重写：打分依据从「任意回应」换成「好回复（ask_resume）」，分组字段
    # 从旧的假字段（salutation_variant 写死 neutral / has_jd_anchor 写死无）换成
    # 新字段 variant_tags（7 处变体真实 id）。每个槽位单独一组，算「好回复率」。
    lines += ["## 2. 话术变体对比（好回复率）", "",
              "> 打分依据 = ask_resume（HR 要简历/约面试/追问到岗 = 好回复）。",
              "> 每组满 15 条先看趋势，满 40 条才下「调成默认」的结论。", ""]
    for slot, name in VARIANT_SLOTS:
        groups = {}
        for r in sent_rows:
            vid = _split_variants(r.get("variant_tags") or "").get(slot)
            if not vid:
                continue
            groups.setdefault(vid, []).append(r)
        if not groups:
            continue
        lines.append(f"### {name}（{slot}）")
        lines.append("| 变体 | 条数 | 好回复率 | 95% 置信 |")
        lines.append("|---|---|---|---|")
        tbl = []
        for vid, rs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            n = len(rs)
            good = sum(1 for r in rs if (r.get("ask_resume") or "").strip() == "1")
            lo, hi = wilson(good, n)
            tbl.append((vid, n, _pct(good, n), f"{lo:.1f}~{hi:.1f}%"))
        for vid, n, rate, ci in tbl:
            if n >= VERDICT_N:
                mark = ""
            elif n >= TREND_N:
                mark = "（够看趋势）"
            else:
                mark = "（样本太小，只列不看）"
            lines.append(f"| {vid} | {n} | {rate} | {ci} {mark} |")
        # 下结论：只有 ≥ VERDICT_N 的才比，且至少两个组都够格
        big = [t for t in tbl if t[1] >= VERDICT_N]
        if len(big) >= 2:
            best = max(big, key=lambda t: float(t[2].split("%")[0]))
            lines.append("")
            lines.append(f"→ 提案：{name}里「{best[0]}」好回复率最高"
                         f"（{best[2]}），可以考虑把它调成默认值。")
        elif len(big) == 1:
            lines.append("")
            lines.append(f"（{name}目前只有「{big[0][0]}」够 40 条，别的变体样本还不够，先不下结论）")
        else:
            # 没有组够 40 条，但可能有够 15 条的趋势信号
            trend = [t for t in tbl if t[1] >= TREND_N]
            if len(trend) >= 2:
                top = max(trend, key=lambda t: float(t[2].split("%")[0]))
                lines.append("")
                lines.append(f"（趋势：{name}里「{top[0]}」暂时领先，但样本都还没到 40 条，继续攒）")
        lines.append("")

    # ── 3. 跟进效果 ──
    followed = [r for r in sent_rows if (r.get("followup_sent") or "").strip()]
    if followed:
        f_replied = sum(1 for r in followed if (r.get("replied") or "").strip())
        notf = [r for r in sent_rows if not (r.get("followup_sent") or "").strip()]
        n_replied = sum(1 for r in notf if (r.get("replied") or "").strip())
        lines += ["## 3. 跟进有没有用", "",
                  f"- 跟过的：{_pct(f_replied, len(followed))}",
                  f"- 没跟的：{_pct(n_replied, len(notf))}",
                  "",
                  "> 注意先后顺序：跟过的都是「发出满 72 小时还没回」的，本来就难回。", ""]
    else:
        lines += ["## 3. 跟进有没有用", "", "还没开始跟，下周再看。", ""]

    # ── 4. 漏斗健康度 ──
    today_file = path_of("data/jobs_today.json")
    if os.path.exists(today_file):
        import json
        try:
            jobs = json.load(open(today_file, encoding="utf-8"))
            lines += ["## 4. 今日漏斗", "",
                      f"- 抓到 {len(jobs)} 条，其中带岗位描述 "
                      f"{sum(1 for j in jobs if len((j.get('jd_text') or '')) > 80)} 条",
                      "- 没描述的岗位会卡在打分那关 —— 这个数字掉下去就先修抓取，别调话术", ""]
        except Exception:
            pass

    out = path_of(f"docs/迭代提案-{date.today().isoformat()}.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print("\n".join(lines[:6]))
    print(f"\n提案写好了 → {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
