# 「永久不投池」—— 把曾经 pass 掉的岗位记下来，下次扫描直接跳过
#
# 为什么要有这个池子（2026-11-15 本人提出）：
#   后台每次扫描都是「重新从 BOSS 搜」，搜到一批岗位 → 硬过滤 → 粗筛 → 打分 →
#   没过线的就静默丢掉。这些「打过分但没过线 / 硬条件不符被砍」的岗位，砍完就没了，
#   下次扫描又会被重新搜到、重新抓详情、重新打一遍分 —— 纯浪费最贵的详情页请求。
#
#   这个池子把「已经判断过、确实不该投」的岗位记下来，下次在【列表阶段】就认出来、
#   直接跳过，连详情页都不用开。
#
# 池子存两类键，分别应对两种「岗位会换皮」的情况：
#   1. job_id 精确匹配  —— job_id 没变，直接命中
#   2. 公司名 + 标题 精确匹配 —— job_id 会变（HR 改标题/重发岗位，见 MEMORY 二十三之二），
#      但同一个坑「公司 + 岗位名」通常不变，用这俩兜住
#
# 铁律（跟 _load_seen_job_ids 的判重方向相反，别搞混）：
#   · 这里只记「明确判断过、确实不该投」的岗位，所以可以拦。
#   · 判「已投过」要靠 job_id 精确匹配（宁漏不误杀）；判「不投」这里放得略宽，
#     因为多拦一个「本来也不该投」的岗位，代价只是少投一个不该投的，可接受。
#
# 存储：data/blocklist.json，结构：
#   {
#     "job_ids": {"<job_id>": {"reason": "...", "when": "2026-11-15"}},
#     "co_title": {"<company>\u0001<title>": {"reason": "...", "when": "..."}}
#   }

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config_loader import path_of

BLOCKLIST_PATH = path_of("data/blocklist.json")

# 分隔符：公司名和标题拼成一个键时用的分隔符。用控制符 \u0001 当分隔，
# 因为公司名/标题里几乎不可能出现这个字符，避免「公司A+标题B」和「公司AB+标题」
# 拼出来是同一个键。
KEY_SEP = "\u0001"


def _load():
    """读回池子。文件不存在或坏了就返回空池，绝不让读失败影响主流程。"""
    try:
        if os.path.exists(BLOCKLIST_PATH):
            with open(BLOCKLIST_PATH, encoding="utf-8") as f:
                d = json.load(f)
                if isinstance(d, dict):
                    return d
    except Exception:
        pass
    return {"job_ids": {}, "co_title": {}}


def _save(d):
    """写回池子。失败静默（记不了池子不能挡住扫描主流程）。"""
    try:
        with open(BLOCKLIST_PATH, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def _co_title_key(company, title):
    return (company or "").strip() + KEY_SEP + (title or "").strip()


def add(job, reason):
    """把一条岗位记进池子。job 可以是 dict（带 job_id/company/title），
    也可以是已经归一好的 (job_id, company, title) 三元组。

    reason 写清楚为什么 pass（排除词/城市/资深经验/分数低等），方便以后复盘
    或手动放行某一条。
    """
    if isinstance(job, dict):
        jid = (job.get("job_id") or "").strip()
        company = (job.get("company") or "").strip()
        title = (job.get("title") or "").strip()
    else:
        jid, company, title = (list(job) + ["", "", ""])[:3]
        jid, company, title = (jid or "").strip(), (company or "").strip(), (title or "").strip()

    # 两个键都空就没法记，直接放弃（这种情况本身也判不了重）
    if not jid and not (company and title):
        return False

    d = _load()
    when = time.strftime("%Y-%m-%d")
    if jid:
        d["job_ids"][jid] = {"reason": reason, "when": when}
    if company and title:
        d["co_title"][_co_title_key(company, title)] = {"reason": reason, "when": when}
    _save(d)
    return True


def is_blocked(job):
    """判断一条岗位是不是已经在池子里。返回 (是否命中, 原因) 或 (False, "")。

    job 可以是 dict，也可以是 (job_id, company, title) 三元组。
    """
    if isinstance(job, dict):
        jid = (job.get("job_id") or "").strip()
        company = (job.get("company") or "").strip()
        title = (job.get("title") or "").strip()
    else:
        jid, company, title = (list(job) + ["", "", ""])[:3]
        jid, company, title = (jid or "").strip(), (company or "").strip(), (title or "").strip()

    d = _load()
    if jid and jid in d.get("job_ids", {}):
        return True, d["job_ids"][jid].get("reason", "")
    if company and title:
        key = _co_title_key(company, title)
        if key in d.get("co_title", {}):
            return True, d["co_title"][key].get("reason", "")
    return False, ""


def size():
    """池子现在有多少条（供体检/汇报用）。"""
    d = _load()
    # 用 job_id 数 + 公司标题数（两者可能重叠，这里只算个量级）
    return len(d.get("job_ids", {})), len(d.get("co_title", {}))


if __name__ == "__main__":
    n_job, n_co = size()
    print(f"永久不投池：{n_job} 个 job_id + {n_co} 条公司+标题")
