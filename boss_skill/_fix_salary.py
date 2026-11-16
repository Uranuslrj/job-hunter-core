# -*- coding: utf-8 -*-
"""一次性修复：把 jobs_today.json 里薪资乱码（私用区字符）的岗位重抓一遍。

背景（2026-11-12）：
    scan 走 DOM 后列表页薪资是字形混淆的私用区字符（kanzhun-mix），
    detail 阶段本该用详情页明文覆盖，但 DETAIL_JS 的薪资选择器会先命中
    similar-job-salary（相似岗位区），导致少数岗位覆盖失败、薪资仍乱码。
    乱码会让 daily_salary 解析不出数字 → 触发 salary_missing -5 分惩罚，
    让本该靠前的岗位沉底。

用法：
    python _fix_salary.py --dry      只列出要修的，不动
    python _fix_salary.py            真抓（开详情页重读薪资，回写 jobs_today.json）

铁律：只读详情页、只写薪资相关字段，不投、不发、不碰台账。
"""
import argparse
import json
import sys
import time

sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))

import fetch_cdp as fc
from config_loader import path_of


def _is_bad_salary(j):
    sal = (j.get("salary_raw") or "").strip()
    if not sal:
        return False
    if sal == "面议":
        return False
    return not any(ch.isdigit() for ch in sal)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    out = path_of("data/jobs_today.json")
    jobs = json.load(open(out, encoding="utf-8"))
    bad = [j for j in jobs if _is_bad_salary(j)]
    if args.limit:
        bad = bad[: args.limit]
    if not bad:
        print("没有薪资乱码的岗位，无需修复。")
        return

    print(f"共 {len(bad)} 条薪资乱码，{'（dry，不抓）' if args.dry else '开始重抓'}：")
    for j in bad:
        print(f"  · {j.get('title','')[:22]}｜{j.get('company','')[:12]}｜{j.get('url','')[:50]}")

    if args.dry:
        return

    tab = fc._PLUGIN_TAB
    fixed = 0
    for i, j in enumerate(bad, 1):
        url = j.get("url") or ""
        if url.startswith("/"):
            url = "https://www.zhipin.com" + url
        if not url:
            print(f"  × [{i}] {j.get('title','')[:20]} 无 url，跳过")
            continue
        try:
            fc.goto(tab, url, wait=4.0)
            d = fc.evaluate(tab, fc.DETAIL_JS, timeout=40) or {}
            sal = fc.deobfuscate(d.get("salary", ""))
            has_digit = bool(sal) and bool(fc.re.search(r"[0-9]", sal))
            if has_digit:
                j["salary_raw"] = sal
                j["salary_kind"] = fc.guess_salary_kind(sal)
                fixed += 1
                print(f"  ✓ [{i}] {j.get('title','')[:20]} → {sal}")
            else:
                print(f"  ! [{i}] {j.get('title','')[:20]} 详情页薪资仍没数字（{sal[:12] if sal else '空'}），保留原值")
        except Exception as e:
            print(f"  × [{i}] {j.get('title','')[:20]} 抓取失败：{e}")
        time.sleep(fc.human_delay(8.0, spread=0.5) if hasattr(fc, "human_delay") else 8)

    json.dump(jobs, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n修好 {fixed} 条，回写 {out}。")


if __name__ == "__main__":
    main()
