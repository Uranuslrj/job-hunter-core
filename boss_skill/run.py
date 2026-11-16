# 把整条流水线串起来
#   读岗位 → 硬过滤 → 粗筛 → 打分 → 写招呼语 → 过五道门 → 给你看 → 你按一下 → 记账
#
# 用法：
#   python run.py                      用 sample_jd/ 里的样例跑一遍自测（不联网）
#   python run.py --input jobs.json    用真实抓下来的岗位
#   python run.py --cap 30             今天最多发 30 条
#   python run.py --no-ask             只看，不问我（生成 HTML 卡片用）

import argparse
import csv
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import filters
import scorer
import greeting as greeter
import review_card as rc
import blocklist
from config_loader import roles, path_of

CFG = roles()
LEDGER = path_of(CFG and "data/greeting_ledger.csv")

FIELDS = ["send_time", "job_id", "company", "city", "title", "family", "score",
          "salutation_variant", "closing_variant", "word_count", "has_jd_anchor",
          # ✅ 2026-11-11 新增：本条招呼语真正用到的变体 id（逗号拼接），
          #   供 A/B 测试按 id 分组统计「要简历率」，判断哪个变体最好。
          #   之前只有 closing_variant 有真值，salutation_variant 是写死的 'neutral'。
          "variant_tags",
          "greeting_text", "role_cluster", "read_24h", "read_72h", "replied",
          # ✅ 2026-11-11 新增：「好回复」档（HR 找我要简历 / 约面试 / 问到岗等明确意向）。
          #   这是 A/B 测「哪个变体最好」的唯一打分依据（好回复=1，其余=0），
          #   由 chat_scan.py 的 classify_good() 判定后回填，不靠 notes 里 grep 关键词。
          "ask_resume",
          "notes",
          "sent", "verify",
          # ✅ 2026-10-28 新增：跟进（第二句）留痕。
          # followup_text 存原文，是为了以后能算"跟过的 vs 没跟过的"回复率差多少。
          "followup_sent", "followup_time", "followup_text",
          # ✅ 2026-10-28 新增：区分"机器发的"和"你以前手动发的"。
          # 手动记录是从聊天历史倒回来补的（chat_scan.py import-history），
          # 不带学习"模型优化"的属性，算回复率时要能拆开看。
          "source"]
QUEUE = path_of("data/send_queue.json")


def load_jobs(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, list) else data.get("jobs", [])


def run_pipeline(jobs, verbose=True):
    stats = {"total": len(jobs), "layer0_out": 0, "jd_bad": 0, "layer1_out": 0,
             "scored": 0, "below_floor": 0, "gate_failed": 0}
    reasons = {}

    alive = []
    for job in jobs:
        ok, why = filters.layer0(job)
        if not ok:
            # JD 抓取失败单独拎出来，别混进普通"硬过滤"里：
            # 它跟"岗位不匹配"是两码事，是"该重抓"的信号，得让人一眼看到。
            if any(w.startswith("JD 抓取失败") for w in why):
                stats["jd_bad"] += 1
            for w in why:
                reasons[w] = reasons.get(w, 0) + 1
            stats["layer0_out"] += 1
            continue
        acted = filters.layer1(job)
        if not acted["passed"]:
            stats["layer1_out"] += 1
            continue
        # 把认出来的岗位族写回 job，写招呼语时要用它来决定"长期想做什么"那句话
        job["family"] = acted["family"]
        s = scorer.score_job(job, acted)
        stats["scored"] += 1
        if s["below_floor"]:
            stats["below_floor"] += 1
            # ── 永久不投池：打过分、没过绝对底线的，记下来 ──
            # 这条已经开过详情页、打过一整遍分，结论是「不该投」。记进池子，
            # 下次扫到同一个 job_id（或同公司同标题）直接 pass，省详情和打分。
            blocklist.add(s, f"分数 {s['score']} 分低于底线 {scorer.THRESH['hard_floor']} 分")
            continue
        alive.append(s)
        s["_job"] = job

    ranked = scorer.rank(alive)
    for s in ranked:
        # 轮换起点：让同一批里的消息用不同经历当证据，别十条全一个样
        s["greeting"] = greeter.draft(s["_job"])
        if s["greeting"] and not s["greeting"].get("ok"):
            stats["gate_failed"] += 1

    if verbose:
        print()
        print("=" * 78)
        print(f"筛岗结果：共 {stats['total']} 条")
        print(f"  硬过滤砍掉　{stats['layer0_out']} 条")
        for w, n in sorted(reasons.items(), key=lambda x: -x[1]):
            print(f"　　　· {w} ×{n}")
        if stats["jd_bad"]:
            print(f"  ⚠ 其中 {stats['jd_bad']} 条是「JD 抓取失败」（详情页连接被终止，该重抓，不是真不匹配）")
        print(f"  粗筛没认出　{stats['layer1_out']} 条（不在 5 个岗位族里）")
        print(f"  打分后低于硬底线 {stats['below_floor']} 条")
        print(f"  剩下 {len(ranked)} 条，其中没过质量门的 {stats['gate_failed']} 条")
        print("=" * 78)
    return ranked


def write_ledger(items):
    path = LEDGER
    fresh = not os.path.exists(path) or os.path.getsize(path) == 0
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    # 2026-11-12 修：记账前先把台账里已有的 job_id 读出来做去重。
    # 之前是无脑追加 —— 同一个岗位今天扫到记一行、明天 --resume 又记一行、
    # 打分过线就记账（哪怕没发），导致台账累计到 1141 行，而真实「沟通过」只有
    # 497 个岗位，数字虚高、判重和回复率分母全被污染。
    have = set()
    if not fresh:
        try:
            with open(path, encoding="utf-8") as f:
                have = {(row.get("job_id") or "").strip()
                        for row in csv.DictReader(f) if (row.get("job_id") or "").strip()}
        except Exception:
            have = set()

    rows = []
    for it in items:
        jid = (it.get("job_id") or "").strip()
        # 同一个 job_id 已经记过就不重复追加（更新已有行由 mark_ledger 负责）
        if jid and jid in have:
            continue
        if jid:
            have.add(jid)
        g = it.get("greeting") or {}
        rows.append({
            "send_time": now,
            "job_id": jid,
            "company": it.get("company", ""),
            "city": it.get("city", ""),
            "title": it.get("title", ""),
            "family": it.get("family") or "insight",
            "score": it.get("score", ""),
            "salutation_variant": "neutral",
            "closing_variant": g.get("closing_id", ""),
            "word_count": g.get("chars", ""),
            "has_jd_anchor": "有" if g.get("jd_anchor") else "无",
            "variant_tags": ",".join(g.get("variant_ids") or []),
            "greeting_text": (g.get("text") or "").replace("\n", " "),
            "role_cluster": "",
            "read_24h": "", "read_72h": "", "replied": "", "ask_resume": "",
            "notes": "；".join(g.get("risks") or []),
            "sent": "未发", "verify": "",
            "followup_sent": "", "followup_time": "", "followup_text": "",
            "source": "机器",
        })
    with open(path, "a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if fresh:
            w.writeheader()
        w.writerows(rows)
    return len(rows)


def _norm_city(c):
    """城市归一：去掉「市」后缀，空值归 None。"""
    if not c:
        return None
    c = c.strip()
    return c[:-1] if c.endswith("市") else c


def _already_sent_filter(item, days=90):
    """导出名单前的前置去重：90 天内同公司同岗位投过没。

    2026-11-11 加：原来这份逻辑只在 send_cdp.py 发送阶段才查，导出队列这步
    完全不筛，导致「已投过的岗位」反复混进待投名单。这里把同样的判定提前到
    导出时做，让名单源头就是干净的。
    2026-11-11 再改：同岗位判定升级成三级精度（job_id / 公司+标题+城市），
    既挡住 HR 重发的旧岗，又不错杀「公司在不同城市各招一个同名岗」。
    """
    company = (item.get("company") or "").strip()
    title = (item.get("title") or "").strip()
    job_id = (item.get("job_id") or "").strip()
    city = _norm_city(item.get("city") or "")
    if not company:
        return False

    if os.path.exists(LEDGER):
        try:
            with open(LEDGER, encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    if (row.get("sent") or "") != "已发":
                        continue
                    row_jid = (row.get("job_id") or "").strip()
                    row_co = (row.get("company") or "").strip()
                    row_ti = (row.get("title") or "").strip()
                    row_city = _norm_city(row.get("city") or "")

                    # ① job_id 精确匹配
                    if job_id and row_jid == job_id:
                        return True
                    # 公司和标题都对不上，肯定不是同一个坑
                    if row_co != company or row_ti != title:
                        continue
                    # ② 城市都齐 → 必须城市也同；任一缺城市 → 保守算同一个坑
                    if city and row_city:
                        if city == row_city:
                            return True
                    else:
                        return True
        except Exception:
            pass

    # 手动投过的公司名（company_seen.txt）
    # 2026-11-12 本人拍板：放开「互相包含」的模糊匹配（原「某公司简称」能挡「某公司全称」所有岗，
    #   大公司会投很多不同岗位，被误伤太多）。改成只拦「公司名完全相等」这一档精确匹配。
    seen_file = path_of("data/company_seen.txt")
    if os.path.exists(seen_file):
        try:
            with open(seen_file, encoding="utf-8") as f:
                for line in f:
                    seen = line.strip()
                    if not seen or seen.startswith("#"):
                        continue
                    if seen == company:
                        return True
        except Exception:
            pass
    return False


def export_queue(items, path=QUEUE):
    """
    把选中的条目导出成队列，交给 send_cdp.py 去发。
    队列里必须带 url —— 自动发送时每条都要从这条岗位自己的详情页点进去，
    绝不能去聊天列表里找人找会话，那是张冠李戴的头号来源。

    2026-11-11 加：导出时就做两道滤网 ——
    ① 已投过（90 天窗口，见 _already_sent_filter）
    ② 已关闭（无 url 或带下架标记）
    这样待投名单源头就是干净的，不再「导出脏名单、发送时再逐条跳」。
    """
    queue = []
    skipped_sent = 0
    skipped_closed = 0
    for it in items:
        g = it.get("greeting") or {}
        if not g.get("ok"):
            continue
        job = it.get("_job") or {}

        if _already_sent_filter(it):
            skipped_sent += 1
            continue
        url = (job.get("url") or "").strip()
        if not url or job.get("closed") or job.get("offline"):
            skipped_closed += 1
            continue

        queue.append({
            "job_id": it.get("job_id", ""),
            "company": it.get("company", ""),
            "title": it.get("title", ""),
            "city": it.get("city", ""),
            "family": it.get("family") or "",
            "score": it.get("score", ""),
            "greeting_text": (g.get("text") or "").strip(),
            "url": url,
        })
    if skipped_sent or skipped_closed:
        print(f"导出时过滤：已投过 {skipped_sent} 条、已下架/无地址 {skipped_closed} 条")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(queue, f, ensure_ascii=False, indent=1)
    return queue


def reconcile(days=5, verbose=True):
    """扫描前的兜底：把后台「未投递 + 未下架 + 未过期」的历史岗位统一补打分，
    结果合并进 send_queue.json，跟今天新扫的一起发。

    背景（本人 2026-11-14 拍板）：扫描流程每次从 BOSS 重新搜，从不回头补「上次
    扫到但没发出去」的旧岗位。旧岗只能靠下次重扫撞见才回来，撞不见就永远漏掉。
    本函数就是补这个洞 —— 把后台还活着的旧岗翻出来再打一遍分，产出「当前可投总数」。

    三条判定（按顺序，命中任一就排除）：
      ① 已投过   —— 台账 sent=已发，或命中三级判重（job_id / 公司+标题+城市 / 公司名）
      ② 已下架   —— 无 url，或带 closed/offline 标记
      ③ 已过期   —— first_seen 距今超过 days 天（5 天）仍未打分，当成没扫到过，作废
      ④ 历史遗留 —— 没有 first_seen 的旧数据（加字段前抓的），真实状态本地判不准，跳过

    数据源：data/jobs_today.json（详情池，带 JD 正文，能真正打分）。
    返回：(可投数, 已投数, 已下架数, 已过期数, 历史遗留数)
    """
    import time as _t
    from datetime import datetime as _dt

    pool_path = path_of("data/jobs_today.json")
    if not os.path.exists(pool_path):
        if verbose:
            print("　（没有 jobs_today.json，跳过补打分）")
        return 0, 0, 0, 0, 0

    jobs = load_jobs(pool_path)
    if not jobs:
        if verbose:
            print("　（jobs_today.json 为空，跳过补打分）")
        return 0, 0, 0, 0, 0

    # 已投 job_id 集合（读台账，只认 sent=已发）
    sent_ids = set()
    if os.path.exists(LEDGER):
        try:
            with open(LEDGER, encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    if (row.get("sent") or "") == "已发":
                        jid = (row.get("job_id") or "").strip()
                        if jid:
                            sent_ids.add(jid)
        except Exception:
            pass

    # 现有队列的 job_id，避免重复加入
    queue_ids = set()
    if os.path.exists(QUEUE):
        try:
            queue_ids = {(q.get("job_id") or "").strip()
                         for q in json.load(open(QUEUE, encoding="utf-8"))
                         if (q.get("job_id") or "").strip()}
        except Exception:
            pass

    today = _dt.now()
    candidates = []
    n_sent = n_closed = n_expired = n_legacy = 0
    for j in jobs:
        jid = (j.get("job_id") or "").strip()
        # 已在现有队列里 → 跳过（不用重复补）
        if jid and jid in queue_ids:
            continue
        # ① 已投过（job_id 精确；公司+标题+城市 走 _already_sent_filter 内部）
        if jid and jid in sent_ids:
            n_sent += 1
            continue
        if _already_sent_filter(j):
            n_sent += 1
            continue
        # ② 已下架
        url = (j.get("url") or "").strip()
        if not url or j.get("closed") or j.get("offline"):
            n_closed += 1
            continue
        # ③ 已过期 / 历史遗留（无 first_seen 的旧数据不参与补打分）
        #    有 first_seen → 超 5 天未打分就当没扫到，作废。
        #    无 first_seen → 2026-11-15 修：不再一刀切判「历史遗留」跳过。
        #      原来这里把「没烙 first_seen 的详情池」全部跳过，导致昨天抓的 1533 条
        #      里 1237 条可投岗位凭空消失（字段缺失 ≠ 数据无效，它们有完整 url+JD）。
        #      新逻辑：无 first_seen 时，用 jobs_today.json 的文件修改时间兜底当首见时间，
        #      只要岗位本身还有 url 且没过期窗口，就正常参与补打分。
        fs = (j.get("first_seen") or "").strip()
        if not fs:
            # 用详情池文件的修改时间兜底，当作这批数据的「首见时间」。
            # 这样「有完整数据的旧详情池」能被正常补打分，而不是被误当历史遗留丢弃。
            try:
                pool_mtime = _dt.fromtimestamp(os.path.getmtime(pool_path))
                seen_dt = pool_mtime
            except Exception:
                n_legacy += 1
                continue
        else:
            try:
                seen_dt = _dt.strptime(fs[:10], "%Y-%m-%d")
            except ValueError:
                seen_dt = None
        if seen_dt is not None and (today - seen_dt).days > days:
            n_expired += 1
            continue
        candidates.append(j)

    if verbose:
        print()
        print("=" * 78)
        print("补打分兜底：把后台还活着的旧岗翻出来再打一遍分")
        print(f"  详情池 {len(jobs)} 条 → 排除 已投 {n_sent} / 已下架 {n_closed} / 已过期 {n_expired} / 历史遗留 {n_legacy}")
        print(f"  剩 {len(candidates)} 条「未投 + 未下架 + 未过期」，补打分中……")

    if not candidates:
        if verbose:
            print("　（没有可补打分的岗位）")
        return 0, n_sent, n_closed, n_expired, n_legacy

    ranked = run_pipeline(candidates)
    # 只保留招呼语过质量门的
    good = [s for s in ranked if (s.get("greeting") or {}).get("ok")]

    # 合并进现有队列（去重：同 job_id 已在队列里的不重复加）
    merged = []
    for s in good:
        jid = (s.get("job_id") or "").strip()
        if jid and jid in queue_ids:
            continue
        queue_ids.add(jid)
        job = s.get("_job") or {}
        g = s.get("greeting") or {}
        merged.append({
            "job_id": s.get("job_id", ""),
            "company": s.get("company", ""),
            "title": s.get("title", ""),
            "city": s.get("city", ""),
            "family": s.get("family") or "",
            "score": s.get("score", ""),
            "greeting_text": (g.get("text") or "").strip(),
            "url": job.get("url", ""),
        })

    # 读回现有队列，追加，写回
    existing = []
    if os.path.exists(QUEUE):
        try:
            existing = json.load(open(QUEUE, encoding="utf-8"))
        except Exception:
            existing = []
    total = existing + merged
    with open(QUEUE, "w", encoding="utf-8") as f:
        json.dump(total, f, ensure_ascii=False, indent=1)

    if verbose:
        print(f"  补打分过线 {len(merged)} 条，并入发送队列 → 现共 {len(total)} 份可投")
        print("=" * 78)
    return len(merged), n_sent, n_closed, n_expired, n_legacy


def default_input():
    """岗位来源：优先用真抓回来的，没有才退回 sample_jd 的演示数据。

    2026-10-29 修：菜单第 3 项和 daily 的打分步骤都是跑 `run.py --auto` 不带 --input，
    老默认却是 sample_jd/jobs.json —— 队列里全是"某智能科技公司"这种演示岗位，
    没有详情页地址，真发时三条全被"没有 url"拦下（白跑一轮还烧了熔断计数）。
    现在按【文件修改时间】挑 data/ 里最新的那份，谁新用谁。
    """
    here = os.path.dirname(os.path.abspath(__file__))
    cands = []
    for name in ("jobs_today.json", "jobs_new.json", "jobs_list.json"):
        p = os.path.normpath(os.path.join(here, "..", "data", name))
        if os.path.exists(p) and os.path.getsize(p) > 10:
            cands.append((os.path.getmtime(p), p))
    if cands:
        return max(cands)[1]
    return os.path.join(here, "sample_jd", "jobs.json")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default=None,
                    help="不填就自动用 data/ 里最新抓的那份岗位文件")
    ap.add_argument("--cap", type=int, default=None)
    ap.add_argument("--no-ask", action="store_true")
    ap.add_argument("--auto", action="store_true",
                    help="不问我，直接按机器勾的（≥strong 阈值，默认 60 分）导出队列，给自动发送用")
    ap.add_argument("--reconcile", action="store_true",
                    help="补打分兜底：把后台未投+未下架+未过期的旧岗补打分并入队列，输出可投总数")
    ap.add_argument("--reconcile-days", type=int, default=5,
                    help="补打分时的过期天数门槛（默认 5 天）")
    args = ap.parse_args()
    if args.reconcile:
        n, s, c, e, legacy = reconcile(days=args.reconcile_days)
        print()
        print(f"补打分完成：可投 +{n} 条，已投排除 {s} 条，已下架 {c} 条，已过期 {e} 条，历史遗留 {legacy} 条")
        return
    if args.input is None:
        args.input = default_input()
    print(f"岗位来源：{args.input}")

    cap = args.cap or CFG["delivery"]["daily_greet_cap"]
    jobs = load_jobs(args.input)
    ranked = run_pipeline(jobs)
    if not ranked:
        print()
        print("今天一条合格的都没有。要么岗位还不够多，要么筛得太严。")
        return

    top = ranked[:cap]
    rc.render_terminal(top, CFG, cap)

    html = rc.render_html(top, CFG, path_of("data/review_card.html"))
    print(f"卡片已经生成：{html}（想慢慢看就双击打开）")

    auto_mode = CFG["delivery"].get("mode") == "auto_cdp"

    if args.no_ask and not args.auto:
        return

    if args.auto or (auto_mode and args.no_ask):
        # 不打扰你：按机器勾的走
        chosen = [it for it in top
                  if (it.get("greeting") or {}).get("ok") and rc.default_checked(it, CFG)][:cap]
        if not chosen:
            print("今天没有过默认线的，机器不敢自作主张发。")
            return
    else:
        chosen = rc.ask(top, CFG, cap)

    if not chosen:
        print("今天不投。")
        return

    n = write_ledger(chosen)
    q = export_queue(chosen)
    print()
    print(f"已记账 {n} 条 → data/greeting_ledger.csv")
    print(f"发送队列 {len(q)} 条 → data/send_queue.json")

    if auto_mode:
        print()
        print("自动发送已开。下一步：")
        print("　第一次务必先跑　python send_cdp.py probe　　　（探选择器，不点任何东西）")
        print("　再跑　　　　　　python send_cdp.py dry-run　　 （演练，最后一步不点发送）")
        print("　确认没问题　　　python send_cdp.py send --limit 10")
    else:
        print("接下来：打开 Boss，把这些招呼语发给对应的 HR（手动粘 —— 见 README 第六节）。")
        print("想让机器替你发，把 config/target_roles.yaml 的 delivery.mode 改成 auto_cdp。")
    print("发出去之后，每天回来把 read_24h / read_72h / replied 三列填上 —— 这三列是整套系统唯一的老师。")


if __name__ == "__main__":
    main()
