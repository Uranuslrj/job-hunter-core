# -*- coding: utf-8 -*-
"""
看板数据汇总脚本：读后台各数据文件，汇总成 dashboard/dashboard_data.json 给前端渲染。

用法：
  python dashboard_data.py            # 生成真实数据 -> dashboard/dashboard_data.json
  python dashboard_data.py --sample   # 生成脱敏样例数据（作品集展示用）

看板模块所需数据一览：
  1. 各池子数量：详情池 / 发送队列 / 已跳过 / 死信 / 永久不投 / 候选 / 未发
  2. 台账指标：已发总数、回复率、已读率、要简历率、来源分布、发送趋势
  3. 最近运行历史：daily_state.history（哪几步成功、accumulate 多少条）
  4. TOP 岗位：_status.json（分数排序，含状态：立即沟通/继续沟通/已关闭）
"""
import os
import sys
import json
import csv
import time
import argparse
import collections

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA = os.path.join(ROOT, "data")
OUT_DIR = os.path.join(ROOT, "dashboard")


def _read_json(name):
    p = os.path.join(DATA, name)
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _read_csv(name):
    p = os.path.join(DATA, name)
    if not os.path.exists(p):
        return []
    try:
        with open(p, encoding="utf-8") as f:
            return list(csv.DictReader(f))
    except Exception:
        return []


def pool_counts():
    """各池子数量"""
    counts = {}
    for name in [
        "jobs_today.json",
        "send_queue.json",
        "send_queue_skipped.json",
        "send_queue_dead.json",
        "_candidates.json",
        "_unsent.json",
    ]:
        d = _read_json(name)
        counts[name] = len(d) if isinstance(d, (list, dict)) else 0

    bl = _read_json("blocklist.json") or {}
    counts["blocklist_job_ids"] = len(bl.get("job_ids", {})) if isinstance(bl, dict) else 0
    counts["blocklist_co_title"] = len(bl.get("co_title", {})) if isinstance(bl, dict) else 0
    return counts


def ledger_metrics():
    """台账指标"""
    rows = _read_csv("greeting_ledger.csv")
    if not rows:
        return {"total": 0}

    sent = [r for r in rows if (r.get("sent") or "").strip() == "已发"]
    unsent = [r for r in rows if (r.get("sent") or "").strip() == "未发"]

    # 回复：replied=1 为明确回复
    replied = [r for r in sent if (r.get("replied") or "").strip() == "1"]
    # 已读：read_72h=1 为72小时内已读
    read72 = [r for r in sent if (r.get("read_72h") or "").strip() == "1"]
    # 要简历：ask_resume=1
    ask_resume = [r for r in sent if (r.get("ask_resume") or "").strip() == "1"]

    # 来源分布
    src = collections.Counter((r.get("source") or "未知").strip() for r in sent)

    # 按来源算回复率
    src_detail = {}
    for s in ["机器", "沟通过", "历史"]:
        sub = [r for r in sent if (r.get("source") or "").strip() == s]
        sub_rep = [r for r in sub if (r.get("replied") or "").strip() == "1"]
        src_detail[s] = {
            "sent": len(sub),
            "replied": len(sub_rep),
            "rate": round(len(sub_rep) / len(sub) * 100, 1) if sub else 0,
        }

    # 按日期算发送趋势
    by_date = collections.Counter()
    for r in sent:
        t = (r.get("send_time") or "").strip()
        if t:
            by_date[t[:10]] += 1
    trend = [{"date": d, "count": c} for d, c in sorted(by_date.items())]

    return {
        "total": len(rows),
        "sent": len(sent),
        "unsent": len(unsent),
        "replied": len(replied),
        "reply_rate": round(len(replied) / len(sent) * 100, 1) if sent else 0,
        "read72": len(read72),
        "read_rate": round(len(read72) / len(sent) * 100, 1) if sent else 0,
        "ask_resume": len(ask_resume),
        "ask_resume_rate": round(len(ask_resume) / len(sent) * 100, 1) if sent else 0,
        "source_dist": dict(src),
        "source_detail": src_detail,
        "trend": trend[-30:],  # 最近30天
    }


def run_history():
    """最近运行历史"""
    d = _read_json("daily_state.json")
    if not d:
        return {"last_start": None, "history": []}
    hist = d.get("history", [])
    # 只取最近 20 条，倒序（最新在前）
    recent = hist[-20:][::-1]
    return {
        "last_start": d.get("last_start"),
        "history": recent,
    }


def top_jobs(limit=15):
    """TOP 岗位（按分数）"""
    d = _read_json("_status.json")
    if not isinstance(d, list):
        return []
    # 按 score 降序
    d = sorted(d, key=lambda x: x.get("score", 0), reverse=True)
    out = []
    for j in d[:limit]:
        out.append({
            "title": j.get("title", ""),
            "company": j.get("company", ""),
            "score": j.get("score"),
            "status": j.get("status", ""),
        })
    return out


def drilldown():
    """各模块下钻明细：点数字卡片后看到的「公司 + 岗位」列表。"""
    out = {}

    # 已发岗位（台账 sent=已发）
    rows = _read_csv("greeting_ledger.csv")
    sent_rows = [r for r in rows if (r.get("sent") or "").strip() == "已发"]
    out["sent"] = [{
        "job_id": r.get("job_id", ""),
        "company": r.get("company", ""),
        "title": r.get("title", ""),
        "city": r.get("city", ""),
        "score": r.get("score", ""),
        "replied": (r.get("replied") or "").strip() == "1",
        "time": (r.get("send_time") or "")[:10],
    } for r in sent_rows]

    # 未发岗位
    unsent_rows = [r for r in rows if (r.get("sent") or "").strip() == "未发"]
    out["unsent"] = [{
        "job_id": r.get("job_id", ""),
        "company": r.get("company", ""),
        "title": r.get("title", ""),
        "city": r.get("city", ""),
        "score": r.get("score", ""),
    } for r in unsent_rows]

    # 发送队列
    sq = _read_json("send_queue.json") or []
    out["send_queue"] = [{
        "job_id": j.get("job_id", ""),
        "company": j.get("company", ""),
        "title": j.get("title", ""),
        "city": j.get("city", ""),
        "score": j.get("score", ""),
    } for j in (sq if isinstance(sq, list) else [])]

    # 永久不投（co_title 键是「公司\x01标题」，拆开）
    bl = _read_json("blocklist.json") or {}
    co_title = bl.get("co_title", {}) if isinstance(bl, dict) else {}
    out["blocklist"] = []
    for k, v in co_title.items():
        parts = k.split("\x01")
        out["blocklist"].append({
            "company": parts[0] if len(parts) > 0 else "",
            "title": parts[1] if len(parts) > 1 else "",
            "reason": v.get("reason", ""),
        })

    # 详情池（只取前 200 条，避免 json 太大）
    jt = _read_json("jobs_today.json") or []
    jt = jt if isinstance(jt, list) else []
    out["jobs_today"] = [{
        "job_id": j.get("job_id", ""),
        "company": j.get("company", ""),
        "title": j.get("title", ""),
        "city": j.get("city", ""),
    } for j in jt[:200]]

    # 已回复岗位（replied=1）
    out["replied"] = [{
        "job_id": r.get("job_id", ""),
        "company": r.get("company", ""),
        "title": r.get("title", ""),
        "city": r.get("city", ""),
        "time": (r.get("send_time") or "")[:10],
    } for r in sent_rows if (r.get("replied") or "").strip() == "1"]

    return out


def build(sample=False):
    if sample:
        return build_sample()

    data = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "sample": False,
        "pools": pool_counts(),
        "ledger": ledger_metrics(),
        "runs": run_history(),
        "top_jobs": top_jobs(),
        "drilldown": drilldown(),
    }
    return data


def build_sample():
    """脱敏样例数据（作品集展示用）"""
    return {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "sample": True,
        "pools": {
            "jobs_today.json": 1533,
            "send_queue.json": 250,
            "send_queue_skipped.json": 10,
            "send_queue_dead.json": 7,
            "_candidates.json": 197,
            "_unsent.json": 47,
            "blocklist_job_ids": 139,
            "blocklist_co_title": 0,
        },
        "ledger": {
            "total": 581,
            "sent": 530,
            "unsent": 51,
            "replied": 108,
            "reply_rate": 20.4,
            "read72": 17,
            "read_rate": 3.2,
            "ask_resume": 3,
            "ask_resume_rate": 0.6,
            "source_dist": {"机器": 197, "沟通过": 233, "历史": 100},
            "source_detail": {
                "机器": {"sent": 197, "replied": 17, "rate": 8.6},
                "沟通过": {"sent": 233, "replied": 68, "rate": 29.2},
                "历史": {"sent": 100, "replied": 23, "rate": 23.0},
            },
            "trend": [
                {"date": "2026-11-08", "count": 23},
                {"date": "2026-11-12", "count": 5},
                {"date": "2026-11-15", "count": 4},
            ],
        },
        "runs": {
            "last_start": "2026-11-15T15:33:47",
            "history": [
                {"t": "2026-11-15T15:33:47", "steps": ["send"], "result": {"send": True}},
                {"t": "2026-11-15T14:28:48", "steps": ["send"], "result": {"send": True}},
                {"t": "2026-11-12T17:15:25", "steps": ["env", "follow", "accumulate", "send", "report", "eval"], "result": {"env": True, "follow": True, "accumulate": 1, "send": True, "report": True, "eval": True}},
                {"t": "2026-11-08T08:00:00", "steps": ["env", "follow", "accumulate", "send", "report", "eval"], "result": {"env": True, "follow": True, "accumulate": 23, "send": True, "report": True, "eval": True}},
            ],
        },
        "top_jobs": [
            {"title": "AI 产品实习生", "company": "某科技公司", "score": 98.7, "status": "继续沟通"},
            {"title": "AIGC 产品实习生", "company": "某数字科技", "score": 94.2, "status": "已关闭"},
            {"title": "AI Agent 产品经理", "company": "某 AI 公司", "score": 88.7, "status": "继续沟通"},
            {"title": "AI 应用产品经理", "company": "某医疗科技", "score": 86.5, "status": "立即沟通"},
            {"title": "解决方案工程师", "company": "某空间科技", "score": 82.8, "status": "已关闭"},
        ],
        "drilldown": {
            "sent": [
                {"company": "某科技公司", "title": "AI 产品实习生", "city": "北京", "score": "98.7", "replied": True, "time": "2026-11-15"},
                {"company": "某 AI 公司", "title": "AI Agent 产品经理", "city": "深圳", "score": "88.7", "replied": False, "time": "2026-11-15"},
                {"company": "某医疗科技", "title": "AI 应用产品经理", "city": "杭州", "score": "86.5", "replied": True, "time": "2026-11-12"},
            ],
            "unsent": [
                {"company": "某空间科技", "title": "解决方案工程师", "city": "上海", "score": "82.8"},
                {"company": "某数字科技", "title": "AIGC 产品实习生", "city": "深圳", "score": "94.2"},
            ],
            "send_queue": [
                {"company": "某科技公司", "title": "AI 产品实习生", "city": "北京", "score": "98.7"},
                {"company": "某 AI 公司", "title": "AI Agent 产品经理", "city": "深圳", "score": "88.7"},
            ],
            "blocklist": [
                {"company": "某公司", "title": "解决方案总监", "reason": "标题排除：总监"},
                {"company": "某科技", "title": "算法产品经理", "reason": "标题排除：算法"},
                {"company": "某公司", "title": "售前解决方案", "reason": "分数 36.5 分低于底线 55 分"},
            ],
            "jobs_today": [
                {"company": "某科技公司", "title": "AI 产品实习生", "city": "北京"},
                {"company": "某 AI 公司", "title": "AI Agent 产品经理", "city": "深圳"},
            ],
            "replied": [
                {"company": "某科技公司", "title": "AI 产品实习生", "city": "北京", "time": "2026-11-15"},
                {"company": "某医疗科技", "title": "AI 应用产品经理", "city": "杭州", "time": "2026-11-12"},
            ],
        },
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample", action="store_true", help="生成脱敏样例数据")
    args = parser.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)
    data = build(sample=args.sample)
    out = os.path.join(OUT_DIR, "dashboard_data.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"已生成 -> {out}")
    print(f"  类型: {'样例(脱敏)' if args.sample else '真实数据'}")
    print(f"  池子: {data['pools']}")
    if not args.sample:
        m = data["ledger"]
        print(f"  台账: 已发 {m['sent']} 回复率 {m['reply_rate']}%")


if __name__ == "__main__":
    main()
