# 一次性回填：把「历史已经判断过不该投」的岗位灌进永久不投池
#
# 用途：blocklist 是新加的，池子一开始是空的。但后台 data/ 里已经有大量
# 「打过分没过线 / 硬条件被砍」的历史岗位，不灌进去的话，下次扫描还是得
# 重新抓详情、重新打一遍分。
#
# 回填来源（三类）：
#   1. data/jobs_today.json 详情池 —— 有完整 JD，能真正跑 layer0/layer1 判定
#   2. 各队列文件（send_queue.json 等）—— 里面带 score 的，< 60 分记入
#   3. 台账 greeting_ledger.csv —— sent=未发 且 score<60 的记入
#
# 只读不删，纯往 blocklist.json 里加条目。跑完打印汇总。
#
# 用法：python backfill_blocklist.py [--dry]
#   --dry  只统计会灌进去多少条，不真写

import argparse
import csv
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import filters
import blocklist
from config_loader import path_of
import fetch_cdp


def backfill_from_detail_pool(dry):
    """详情池：有完整 JD，跑 layer0 + layer1 + 打分，把「该砍」的灌进去。"""
    p = path_of("data/jobs_today.json")
    if not os.path.exists(p):
        print("（没有 jobs_today.json，跳过详情池）")
        return 0
    jobs = json.load(open(p, encoding="utf-8"))
    if not isinstance(jobs, list):
        print("（jobs_today.json 结构不对，跳过）")
        return 0

    added = 0
    for j in jobs:
        jid = (j.get("job_id") or "").strip()
        company = (j.get("company") or "").strip()
        title = (j.get("title") or "").strip()
        if not jid and not (company and title):
            continue

        # 已经在池子里就不重复记
        if blocklist.is_blocked(j)[0]:
            continue

        reason = ""
        # ① 列表阶段硬条件（城市/排除词/标题总监算法）
        reason = fetch_cdp._hard_block_reason(j)
        # ② 有 JD 的，再跑 layer0（排除词/城市/薪资/身份/断网错误页）
        if not reason and (j.get("jd_text") or "").strip():
            ok, why = filters.layer0(j)
            if not ok:
                reason = "；".join(why)
        # ③ 还过硬的，只认「标题总监/算法」这一类稳定硬条件。
        #    「资深经验」「粗筛未命中岗位族」2026-11-15 本人拍板不进池——
        #    它们保留查看空间，照常抓详情打分，可能翻盘。
        if not reason:
            acted = filters.layer1(j)
            if not acted.get("passed") and acted.get("excluded_title"):
                reason = f"标题排除：{acted['excluded_title']}"

        if reason:
            if not dry:
                blocklist.add(j, reason)
            added += 1

    print(f"详情池回填：{added} 条（共 {len(jobs)} 条）")
    return added


def backfill_from_queues(dry):
    """各队列文件：带 score 的，score < 55（硬底线）记入。

    2026-11-15 本人拍板：只有低于硬底线 55 分的才永久拉黑；55~59.9 分
    （过了底线但没过 60 自动发线）的保留，它们下次还会被扫到重新打分，
    JD 变了就可能翻盘，不能永久拉黑。
    """
    files = ["send_queue.json", "send_queue_skipped.json", "send_queue_dead.json",
             "_candidates.json", "_unsent.json"]
    total = 0
    for name in files:
        p = path_of("data/" + name)
        if not os.path.exists(p):
            continue
        try:
            data = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, list):
            continue
        added = 0
        for j in data:
            if blocklist.is_blocked(j)[0]:
                continue
            score = j.get("score")
            try:
                score = float(score) if score not in (None, "") else None
            except (ValueError, TypeError):
                score = None
            if score is None or score >= 55:
                continue
            if not dry:
                blocklist.add(j, f"分数 {score} 分低于硬底线 55 分")
            added += 1
        if added:
            print(f"队列 {name} 回填：{added} 条")
        total += added
    return total


def backfill_from_ledger(dry):
    """台账：sent=未发 且 score<55 的记入。"""
    p = path_of("data/greeting_ledger.csv")
    if not os.path.exists(p):
        print("（没有台账，跳过）")
        return 0
    rows = list(csv.DictReader(open(p, encoding="utf-8")))
    added = 0
    for r in rows:
        if (r.get("sent") or "").strip() != "未发":
            continue
        if blocklist.is_blocked(r)[0]:
            continue
        score = r.get("score")
        try:
            score = float(score) if score not in (None, "") else None
        except (ValueError, TypeError):
            score = None
        if score is None or score >= 55:
            continue
        if not dry:
            blocklist.add(r, f"分数 {score} 分低于硬底线 55 分（台账未发）")
        added += 1
    print(f"台账回填：{added} 条")
    return added


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="只统计，不真写")
    args = ap.parse_args()

    print("回填永久不投池" + ("（--dry 只统计不写）" if args.dry else ""))
    print("=" * 60)
    n1 = backfill_from_detail_pool(args.dry)
    n2 = backfill_from_queues(args.dry)
    n3 = backfill_from_ledger(args.dry)
    print("=" * 60)
    print(f"合计 {n1 + n2 + n3} 条" + ("（未写入）" if args.dry else "，已写入"))
    if not args.dry:
        nj, nc = blocklist.size()
        print(f"池子现状：{nj} 个 job_id + {nc} 条公司+标题 → data/blocklist.json")


if __name__ == "__main__":
    main()
