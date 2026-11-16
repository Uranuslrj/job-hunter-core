# 自动巡检：每天四项指标自己算，自己判定，不用人盯
#
# 用法：python inspect.py
# 退出码：0 = 一切正常可继续；1 = 有该停的事（照屏幕提示做）
#
# 四个指标（对应 knowledge/offer-workflow-metrics.md 第 1 节）：
#   1 招呼语严格执行率  —— 低于 100% 立刻停，不可交易
#   2 风控触发率        —— 出现 1 次就停，等人工过验证码
#   3 爬取正确率        —— 低于 98% 停抓，先修解析
#   4 回复率            —— 只记录；样本量没到【不许下结论】
#
# 第 4 条为什么这么硬：基线 10% 时，60 条的 95% 置信区间是 4.7%–20.1%。
# 也就是说 60 条时，10% 和 20% 在数据上分不开 —— 这时候下结论就是自欺欺人。

import csv
import json
import os
import random
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config_loader import path_of

# ── 达标线（改这里就能调，不用动逻辑）──────────────────────────────
MIN_SAMPLE_FOR_CONCLUSION = 284   # 基线10%时检测"翻倍"所需的两组总数
FLOOR_EXEC_RATE = 1.00            # 执行率必须 100%，不可交易
FLOOR_ACCURACY = 0.98             # 爬取正确率
MAX_RISK_PER_DAY = 0              # 风控触发次数

CITIES = {"北京", "上海", "深圳", "杭州", "广州", "武汉", "长沙", "南京",
          "苏州", "无锡", "宁波", "成都", "西安", "厦门", "福州"}


def load_ledger():
    p = path_of("data/greeting_ledger.csv")
    if not os.path.exists(p):
        return []
    with open(p, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def reply_stats(rows):
    """回复率：只算真正发出去的那些"""
    sent = [r for r in rows if (r.get("sent") or "").strip() == "已发"]
    replied = [r for r in sent if (r.get("replied") or "").strip() not in ("", "0", "否")]
    # 分意愿：hireEZ 数据显示只有约 42% 的回复是真有意愿，
    # 只盯总回复率会被"谢谢不考虑"骗过去
    positive = [r for r in replied
                if any(w in (r.get("replied") or "") for w in ["面试", "约", "聊", "简历", "合适", "机会"])]
    return len(sent), len(replied), len(positive)


def exec_stats(rows):
    """执行率：回读文本与生成文本逐字一致的条数 / 已发条数"""
    sent = [r for r in rows if (r.get("sent") or "").strip() == "已发"]
    if not sent:
        return 0, 0, 1.0
    ok = [r for r in sent if (r.get("verify") or "").strip() in ("ok", "OK", "通过", "一致")]
    return len(ok), len(sent), len(ok) / len(sent)


def accuracy_stats(sample_n=20):
    """爬取正确率：从抓回来的岗位里随机抽 N 条，查关键字段对不对

    注意：必须读 jobs_today.json（详情页后的最终产物），不能读 jobs_list.json。
    jobs_list 是 scan 阶段的中间态，薪资数字此时还是被 BOSS 字形混淆的私有区字符
    （kanzhun-mix），本来就该是乱的；detail 阶段才会用详情页明文覆盖成真数字。
    拿中间态去抽查，正确率必然误报成 0%（2026-11-12 踩过，巡检天天喊"停抓"）。
    """
    p = path_of("data/jobs_today.json")
    if not os.path.exists(p):
        return None
    try:
        jobs = json.load(open(p, encoding="utf-8"))
    except Exception:
        return None
    if not jobs:
        return None
    sample = random.sample(jobs, min(sample_n, len(jobs)))
    bad = []
    for j in sample:
        why = []
        if (j.get("city") or "") not in CITIES:
            why.append("城市不在清单")
        # "面议"是真实的薪资写法（校招岗尤其常见：作业帮、理想、贝壳都这么写），
        # 不算解析错误。一开始我没排除它，结果巡检天天误报 ——
        # 误报多了人就会无视巡检，这比没有巡检更糟。
        sal = (j.get("salary_raw") or "").strip()
        if not sal:
            why.append("薪资为空")
        elif sal != "面议" and not any(ch.isdigit() for ch in sal):
            why.append(f"薪资没数字（{sal[:10]}）")
        if not (j.get("company") or "").strip():
            why.append("公司名为空")
        if not (j.get("url") or "").strip():
            why.append("链接为空")
        if why:
            bad.append((j.get("title", "")[:24], "；".join(why)))
    return len(sample) - len(bad), len(sample), (len(sample) - len(bad)) / len(sample), bad


def risk_count():
    """风控次数：从风控日志数今天有几条"""
    p = path_of("data/risk_events.log")
    if not os.path.exists(p):
        return 0, []
    today = date.today().isoformat()
    hits = []
    with open(p, encoding="utf-8") as f:
        for line in f:
            if line.startswith(today):
                hits.append(line.strip())
    return len(hits), hits


def main():
    rows = load_ledger()
    n_sent, n_replied, n_positive = reply_stats(rows)
    n_ok, n_sent2, exec_rate = exec_stats(rows)
    acc = accuracy_stats()
    n_risk, risk_lines = risk_count()

    print()
    print("=" * 68)
    print(f"  每日巡检　{date.today().isoformat()}")
    print("=" * 68)
    print()
    print(f"  已发出          {n_sent} 条")
    print(f"  收到回复        {n_replied} 条")
    if n_sent:
        print(f"  回复率          {n_replied / n_sent * 100:.1f}%"
              f"　（其中像真有意愿的 {n_positive} 条）")
    else:
        print("  回复率          —（还没发过）")
    print(f"  招呼语执行率    {exec_rate * 100:.1f}%（{n_ok}/{n_sent2} 条逐字一致）")
    if acc:
        ok, total, rate, bad = acc
        print(f"  爬取正确率      {rate * 100:.1f}%（抽查 {total} 条，{total - ok} 条有问题）")
        for t, why in bad[:5]:
            print(f"       × {t}　← {why}")
    else:
        print("  爬取正确率      —（还没抓过岗位）")
    print(f"  今天风控        {n_risk} 次")
    print()

    # ── 判定 ──────────────────────────────────────────────
    stop = []
    if n_sent2 and exec_rate < FLOOR_EXEC_RATE:
        stop.append(f"招呼语执行率 {exec_rate * 100:.1f}% < 100%　→　有话术没按原样发出去，停机逐条查")
    if n_risk > MAX_RISK_PER_DAY:
        stop.append(f"今天触发风控 {n_risk} 次　→　去浏览器手动过验证码，之后用 --resume 续跑")
    if acc and acc[2] < FLOOR_ACCURACY:
        stop.append(f"爬取正确率 {acc[2] * 100:.1f}% < 98%　→　先修解析，别继续抓（抓了也是错的）")

    if stop:
        print("  ⛔ 该停下处理：")
        for s in stop:
            print(f"     · {s}")
        print()
        print("=" * 68)
        return 1

    print("  ✓ 四项都在达标线内，可以继续。")
    print()
    if n_sent < MIN_SAMPLE_FOR_CONCLUSION:
        left = MIN_SAMPLE_FOR_CONCLUSION - n_sent
        print(f"  📊 回复率还差 {left} 条才够下结论（现在 {n_sent}/{MIN_SAMPLE_FOR_CONCLUSION}）。")
        print("     这段时间只记录，别改策略 —— 改了就分不清是策略生效还是行情变了。")
    else:
        print(f"  📊 样本够 {MIN_SAMPLE_FOR_CONCLUSION} 条了，现在才有资格判断改动有没有效。")
        print("     判断方式：拿同一时期的两组比，别拿这周和下周比。")
    print()
    print("=" * 68)
    return 0


if __name__ == "__main__":
    sys.exit(main())
