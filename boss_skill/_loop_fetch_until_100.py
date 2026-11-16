"""临时脚本：跳过扫描，直接循环抓存量详情 + 打分，攒够 100 份可投才停。

用法：
    BOSS_DRIVER=plugin python _loop_fetch_until_100.py

逻辑（对应 daily.py 的 step_fetch_until_enough，但去掉 scan 环节）：
    每轮：detail --resume --cap 120（抓存量详情）→ run.py --auto（打分+导出队列）
    → 数 send_queue.json 够不够 100
       - 够 100 → 停，报告
       - 不够但本轮有新增 → 再来一轮
       - 不够且没新增 → 供给枯竭，停，报告差多少
最多跑 MAX_ROUNDS 轮（默认 8 轮 = 最多 960 条详情）。
"""

import os
import subprocess
import sys
import json

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
TARGET = 100
CAP = 120
MAX_ROUNDS = 8

DATA_DIR = os.path.join(os.path.dirname(HERE), "data")
QUEUE = os.path.join(DATA_DIR, "send_queue.json")


def count_queue():
    if os.path.exists(QUEUE):
        try:
            return len(json.load(open(QUEUE, encoding="utf-8")))
        except Exception:
            return 0
    return 0


def run(cmd, label):
    print(f"\n{'=' * 60}")
    print(f"▶ {label}")
    print(f"{'=' * 60}")
    r = subprocess.run(cmd, cwd=HERE)
    return r.returncode


def main():
    prev = count_queue()
    print(f"起始可投队列：{prev} 条，目标 {TARGET} 条")

    for rnd in range(1, MAX_ROUNDS + 1):
        print(f"\n{'─' * 60}")
        print(f"第 {rnd} 轮：抓详情 {CAP} 条 → 打分")
        print(f"{'─' * 60}")

        # 1) 抓详情（跳过扫描，直接抓存量）
        rc_detail = run(
            [PY, "fetch_cdp.py", "detail", "--resume", "--cap", str(CAP)],
            f"抓存量详情（第 {rnd} 轮，上限 {CAP} 条）",
        )

        # 2) 打分 + 导出队列
        run(
            [PY, "run.py", "--input", os.path.join(DATA_DIR, "jobs_today.json"),
             "--no-ask", "--auto"],
            f"打分与生成招呼语（第 {rnd} 轮）",
        )

        cur = count_queue()
        added = cur - prev
        print(f"\n第 {rnd} 轮结束：队列 {cur} 条（本轮新增 {added} 条）")

        if cur >= TARGET:
            print(f"✓ 已攒够 {TARGET} 条，停止。")
            return 0
        if added <= 0:
            print(f"⚠ 本轮无新增（供给枯竭），停止。还差 {TARGET - cur} 条。")
            return 0
        print(f"还不够 {TARGET} 条，继续下一轮……")
        prev = cur

    print(f"\n⚠ 已跑满 {MAX_ROUNDS} 轮，队列 {count_queue()} 条，未到 {TARGET}。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
