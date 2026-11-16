# 一天一条命令 —— 把整个流程串起来
#
# 用法：
#   python daily.py               跑完整一天（翻账 → 跟进 → 循环攒100 → 发 → 收尾）
#   python daily.py --dry         全流程演练，一条都不真发
#   python daily.py --only follow 只跑翻账跟进这一段
#   python daily.py --only send   只发今天的新客
#   python daily.py --only accumulate  只跑「循环扫描攒够100」这一段
#   python daily.py --only fetch  只抓今天的岗位（旧单步，会顺手抓岗位描述）
#   python daily.py --only eval   看能不能评估了
#   python daily.py --limit 5     各段最多 5 条（小规模测试就用它）
#
# 顺序为什么是"先翻账、再抓今天的"（这条是他改的我原来的设想，改得对）：
#   跟进要占用当天有限的发送额度。先抓先投新岗位，额度用光了，
#   翻完账才发现有该跟的，就跟不成了。
#
# 2026-11-07 改：fetch+pipeline 合并成 accumulate —— 循环「扫描→打分→生成」，
#   攒够 100 份可投招呼语才进发送。不够就换城市继续扫（--resume 接着扫新城市）。
#
# 每一步都可以单独重跑，也可断点续跑 —— 状态记在 data/daily_state.json。

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from config_loader import path_of

PY = sys.executable
STATE = path_of("data/daily_state.json")

STEPS = ["env", "follow", "resume", "accumulate", "send", "report", "eval"]

# 2026-11-07 加：每天目标攒够多少份可投招呼语。不够就换城市继续扫，扫到够为止。
# 与 config/target_roles.yaml 的 delivery.daily_greet_cap=80 对齐。
# 2026-11-12 改：80 → 100，与 config/target_roles.yaml 的 delivery.daily_greet_cap=100 对齐。
TARGET_DAILY_SEND = 100
# 最多循环几轮扫描（防止供给枯竭时无限扫下去）。每轮扫一批城市（受 SCAN_BUDGET 闸），
# 15 城 ÷ 主城 5 城每轮 ≈ 3~4 轮扫完，6 轮是留余量。
MAX_SCAN_ROUNDS = 6

QUEUE_PATH = path_of("data/send_queue.json")


def count_queue():
    """数一下当前发送队列里有多少条可投（读 send_queue.json）。文件坏/不存在算 0。"""
    try:
        if not os.path.exists(QUEUE_PATH):
            return 0
        data = json.load(open(QUEUE_PATH, encoding="utf-8"))
        return len(data) if isinstance(data, list) else 0
    except Exception:
        return 0


def load_state():
    try:
        return json.load(open(STATE, encoding="utf-8"))
    except Exception:
        return {}


def save_state(s):
    try:
        json.dump(s, open(STATE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    except Exception:
        pass


def run(cmd, label, allow_fail=False):
    """跑一条子命令，返回成没成功。输出实时打出来，出问题能当场看见。"""
    print(f"\n{'─' * 60}\n▶ {label}\n{'─' * 60}")
    t0 = time.time()
    # 加 -u 让子进程无缓冲输出：不这样，子进程往管道写的东西会被全缓冲，
    # 扫描/发送这种长任务会 1 分多钟看不到任何输出，还以为卡死了（2026-11-07 踩到）。
    p = subprocess.run([PY, "-u"] + cmd, cwd=HERE, encoding="utf-8", errors="replace")
    sec = int(time.time() - t0)
    print(f"↳ {label}：退出码 {p.returncode}，耗时 {sec} 秒")
    if p.returncode != 0 and not allow_fail:
        return False
    return True


def step_env():
    """S0 环境检查：插件连没连上、登录还在不在、配置文件有没有写坏。"""
    print("检查浏览器插件连接……")
    ok = run(["boss_skill/fetch_cdp.py", "check"] if False else
             [os.path.join(HERE, "fetch_cdp.py"), "check"], "浏览器连接", allow_fail=True)
    if not ok:
        print("连不上浏览器插件。确认 chrome-use 插件已启用、BOSS 已登录，再回来。")
    # 配置能不能读出来（改坏了早点知道，别等发送的时候才炸）
    # 2026-10-29 改：原来开 3 个子进程各 import 一次，纯重复 ——
    #   下面每个步骤真跑的时候还会各自 import 一遍，坏了一样会报错，
    #   这 3 次只是白跑（每次约 0.6 秒，一天省约 2 秒）。
    #   现在合并成一个子进程一次 import 三个，只留"有没有写坏"这一个信号。
    r = subprocess.run(
        [PY, "-c", f"import sys; sys.path.insert(0,r'{HERE}'); import filters, greeting, scorer"],
        capture_output=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        print(f"⚠ 模块导入失败：{r.stderr.strip()[:200]}")
        return False
    print("配置与模块都正常")
    return ok


def step_follow(dry, limit, include_low):
    """S1+S2 翻账 → 意图分流 → 跟进"""
    if not run([os.path.join(HERE, "chat_scan.py"), "match", "--apply"], "翻账（更新台账回没回）",
               allow_fail=True):
        print("翻账没跑成（多半是浏览器没开），先跳过跟进。")
        return False
    cmd = [os.path.join(HERE, "send_cdp.py"), "follow"]
    if not dry:
        cmd.append("--send")
    if include_low:
        cmd.append("--include-low")
    if limit:
        cmd += ["--limit", str(limit)]
    return run(cmd, "跟进（高/中把握自动发，低把握只列清单）", allow_fail=True)


def step_resume(dry, limit):
    """S2.5 补发简历：扫出「要简历但没发」的 HR，逐个定制→上传→发。

    放在 follow 之后、accumulate 之前：先把「对方已主动要简历」的高意向机会
    补上附件简历（最靠近 offer 的一步），再去扫新岗位攒文字招呼语。
    """
    cmd = [os.path.join(HERE, "resume_follow.py"), "send"]
    if dry:
        cmd.append("--dry")
    if limit:
        cmd += ["--limit", str(limit)]
    return run(cmd, "补发简历（要简历未给的 HR 逐个自动发）", allow_fail=True)


def step_fetch(resume=False):
    """S3 抓今天的岗位 + 岗位描述"""
    cmds = [([os.path.join(HERE, "fetch_cdp.py"), "scan"], "扫描岗位列表")]
    cmds.append(([os.path.join(HERE, "fetch_cdp.py"), "detail"] + (["--resume"] if resume else []),
                 "抓岗位描述（这步决定当天能产出几条）"))
    ok = True
    for cmd, label in cmds:
        ok = run(cmd, label, allow_fail=True) and ok
    return ok


def step_pipeline(limit):
    """S4 打分 → 生成招呼语 → 导出队列"""
    cmd = [os.path.join(HERE, "run.py"), "--auto"]
    if limit:
        cmd += ["--cap", str(limit)]       # run.py 的截断参数是 --cap，不是 --limit
    return run(cmd, "打分与生成招呼语", allow_fail=True)


def step_fetch_until_enough(target=TARGET_DAILY_SEND, resume=False, limit=None,
                            max_rounds=MAX_SCAN_ROUNDS):
    """循环「扫描 → 打分 → 生成招呼语」，攒够 target 份可投才停。

    2026-11-07 本人拍板：不再设投递数量上限，改为「每天攒够 80 份可投招呼语，
    不够就换城市继续扫，再打分再生成，直到够 80 份才进入发送环节」。

    每轮做三件事：
      1. scan（第一轮从头扫，之后 --resume 接着扫新城市/新词）
      2. detail（抓岗位描述，这步决定当天能产出几条）
      3. pipeline（run.py --auto 打分 + 生成招呼语 + 导出队列）

    每轮后数一下 data/send_queue.json 有多少条：
      - 够 target → 停，进入发送
      - 不够但本轮有新增 → 再来一轮（换城市继续扫）
      - 不够且本轮没新增 → 供给枯竭，停，如实报告还差多少

    返回值：攒到的条数（不够 target 也返回实际值，让上层知道差多少）。
    """
    print(f"\n{'═' * 60}")
    print(f"▶ 循环攒招呼语：目标 {target} 份可投，不够就换城市继续扫")
    print(f"{'═' * 60}")

    # ── 补打分兜底（2026-11-14 本人拍板，扫描前自动跑一次）──
    # 扫描流程每次从 BOSS 重新搜，从不回头补「上次扫到但没发出去」的旧岗位。
    # 这里在扫描前先把后台「未投+未下架+未过期」的旧岗补打分、并入队列，
    # 让旧账结清，再扫新岗位补足目标。打分是纯本地计算，秒级，不拖慢流程。
    run([os.path.join(HERE, "run.py"), "--reconcile"],
        "补打分兜底（结清后台旧账，并入队列）", allow_fail=True)

    prev_queue = count_queue()
    # 传了 limit（小规模测试）→ 只跑一轮就停，不循环攒够 target。
    # 否则会"不够就换城市再扫"，多轮扫描照样拖时间、撞超时。
    effective_rounds = 1 if limit else max_rounds
    for rnd in range(1, effective_rounds + 1):
        print(f"\n—— 第 {rnd} 轮扫描 → 打分 → 生成 ——")

        # 1) 扫描（第一轮从头，之后 --resume 接着扫新城市）
        #    --limit 小规模测试时：限 1 城 × 1 词 × 1 页，几十秒跑完。
        #    只限 --pages 不够——scan 耗时 = 城市数×词数×页数，光限页数还是 15城×27词 要 54 分钟。
        scan_cmd = [os.path.join(HERE, "fetch_cdp.py"), "scan"]
        if limit:
            scan_cmd += ["--cities", "北京", "--queries", "大模型", "--pages", "1"]
        if rnd > 1:
            scan_cmd.append("--resume")
        ok_scan = run(scan_cmd, f"扫描岗位（第 {rnd} 轮）", allow_fail=True)

        # 2) 抓岗位描述（始终 --resume：保留已抓过的，接着抓没抓的，累加进 jobs_today）
        #    detail 里 resume 逻辑 = 读已有 jobs_today 的 job_id，跳过它们，抓剩下的累加保存。
        #    这样多轮循环才能真的「累积」，而不是每轮覆盖前 120 条。
        #    --limit 小规模测试时：detail 只抓 limit 条描述（--cap），配合 scan 的 1 页
        detail_cmd = [os.path.join(HERE, "fetch_cdp.py"), "detail", "--resume"]
        if limit:
            detail_cmd += ["--cap", str(limit)]
        ok_detail = run(detail_cmd, f"抓岗位描述（第 {rnd} 轮）", allow_fail=True)

        # 3) 打分 + 生成招呼语 + 导出队列
        ok_pipe = step_pipeline(limit)

        # 数队列
        cur_queue = count_queue()
        added = cur_queue - prev_queue
        print(f"\n　第 {rnd} 轮结束：队列现在 {cur_queue} 份（本轮新增 {added} 份）")

        if cur_queue >= target:
            print(f"　✓ 已攒够 {target} 份，停止扫描，进入发送环节。")
            return cur_queue

        if added <= 0:
            print(f"　⚠ 本轮没有新增可投岗位（供给可能枯竭），停止扫描。")
            print(f"　还差 {target - cur_queue} 份到目标 {target}，今天先按 {cur_queue} 份发。")
            return cur_queue

        # 不够但还有新增 → 继续下一轮（换城市扫）
        print(f"　还不够 {target} 份，换城市继续扫下一轮……")

    print(f"\n　⚠ 已经扫了 {max_rounds} 轮，仍只攒到 {count_queue()} 份。")
    print(f"　可能 15 城的供给确实填不满 {target}，建议手动加城市或放宽岗位族。")
    return count_queue()


def step_send(dry, limit):
    """S5 发今天的新客"""
    cmd = [os.path.join(HERE, "send_cdp.py"), "send" if not dry else "dry-run"]
    if limit:
        cmd += ["--limit", str(limit)]
    return run(cmd, "发送今日新客（每条门口有六道检查）", allow_fail=True)


def step_report():
    """S6+S7 收尾：每日巡检 + 到条件就评估"""
    run([os.path.join(HERE, "inspect.py")], "每日巡检（四项指标）", allow_fail=True)
    # 评估：只看今天要不要跑 —— 真够不够 60 条由 evaluate.py 自己判
    if datetime.now().weekday() == 6:          # 周日
        step_eval()
    return True


def step_eval():
    """评估 + 出提案。够不够 60 条由 evaluate.py 自己判断，不够它会说还差多少。"""
    return run([os.path.join(HERE, "evaluate.py")], "评估（出提案，不改配置）", allow_fail=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="全程演练，一条都不真发")
    ap.add_argument("--limit", type=int, default=None, help="每段最多几条")
    ap.add_argument("--only", choices=STEPS + ["fetch", "pipeline"],
                    help="只跑某一段（fetch/pipeline 是旧的单步，保留供调试）")
    ap.add_argument("--include-low", action="store_true",
                    help="低把握的跟进也一起发（默认只列清单）")
    ap.add_argument("--resume", action="store_true", help="抓描述时接着上次的断点跑")
    args = ap.parse_args()

    st = load_state()
    st["last_start"] = datetime.now().isoformat(timespec="seconds")
    st.setdefault("history", [])

    plan = [args.only] if args.only else STEPS
    if args.only == "env":
        plan = ["env"]
    results = {}
    try:
        import live_status  # 实时心跳，旁路，失败不影响主流程
    except Exception:
        live_status = None
    for s in plan:
        if live_status:
            live_status.touch(step=s, running=True,
                              msg=f"开始：{live_status.STEP_CN.get(s, s)}")
        if s == "env":
            results[s] = step_env()
        elif s == "follow":
            results[s] = step_follow(args.dry, args.limit, args.include_low)
        elif s == "resume":
            results[s] = step_resume(args.dry, args.limit)
        elif s == "fetch":
            results[s] = step_fetch(args.resume)
        elif s == "pipeline":
            results[s] = step_pipeline(args.limit)
        elif s == "accumulate":
            # 循环扫描攒够 80（不够换城市继续扫）
            results[s] = step_fetch_until_enough(
                target=TARGET_DAILY_SEND, resume=args.resume, limit=args.limit)
        elif s == "send":
            results[s] = step_send(args.dry, args.limit)
        elif s == "report":
            results[s] = step_report()
        elif s == "eval":
            results[s] = step_eval()

    st["last_result"] = results
    st["history"].append({"t": st["last_start"], "steps": plan, "result": results})
    st["history"] = st["history"][-14:]
    save_state(st)
    if live_status:
        live_status.idle()

    print(f"\n{'═' * 60}\n今天到这儿了\n{'═' * 60}")
    for s in plan:
        flag = "✓" if results.get(s) else "!"
        print(f"  {flag} {s}")
    if args.only in (None, "follow", "send") and not args.dry:
        print("\n记得一件事：台账的『已读/回复』列由 chat_scan 自动翻，"
              "但『对方明显有兴趣』这类消息要你自己回 —— 那是最靠近 offer 的一步。")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
