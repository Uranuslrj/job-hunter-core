# 第一层和第二层过滤：用本地规则把明显不合适的岗位砍掉
#
# 为什么要本地先砍一刀：
#   给模型打分是要花钱要时间的。1000 条全喂给模型，其中 600 条一看就该扔。
#   本地规则跑 1000 条 = 几秒钟、零成本，先把垃圾挡在外面。

import re
import unicodedata
from config_loader import roles

ROLES = roles()


# ---------- 薪资换算 ----------

def _num(s):
    m = re.search(r"(\d+(?:\.\d+)?)", str(s or ""))
    return float(m.group(1)) if m else None


def _norm(s):
    """把文字统一成可比的样子：全角转半角、各种隐形空格删掉、英文转小写。

    为什么要这一步（2026-10-28 实测踩的坑）：
    有条岗位叫 `AI\\xa0产品运营实习生（FDE方向）`，AI 和产品中间那个空格
    是网页用的不换行空格，肉眼跟普通空格一模一样，程序眼里却是另一个字符 ——
    结果这条没匹配上"AI产品运营"，被当成无关岗位扔了，而它偏偏写着 FDE 方向。
    同理还有全角空格、`AI 产品经理` 里多一个空格之类，都是隐形杀手。
    """
    s = unicodedata.normalize("NFKC", str(s or ""))
    s = re.sub(r"[\s\u3000\u200b\u200c\u200d\ufeff]+", "", s)
    return s.lower()


def daily_salary(job):
    """把岗位薪资统一折算成'每天多少钱'，便于跟下限比较。"""
    raw = job.get("salary_raw") or ""
    kind = job.get("salary_kind") or ""
    n = _num(raw)
    if n is None:
        return None, "薪资字段缺失"
    if kind == "daily" or ("元/天" in raw or "元/日" in raw or "/天" in raw):
        return n, "日薪"
    if kind == "monthly" or ("元/月" in raw or "/月" in raw or "K" in raw.upper()):
        base = n * 1000 if n < 100 else n          # 写成 8K / 8 的情况按千处理
        days = ROLES["hard_filter"]["salary"]["workdays_per_month"]
        return base / days, "月薪折算"
    if "元/时" in raw or "元/小时" in raw:
        return n * 8, "时薪折算"
    return n, "未知单位"


# ---------- 第一层：硬性条件 ----------

def _city_ok(job, problems):
    cf = ROLES["hard_filter"]["city"]
    city = (job.get("city") or "").strip()
    remote = bool(job.get("remote_ok"))
    cities = cf["cities"]
    mode = cf.get("match_mode", "exact")

    if remote and ROLES["hard_filter"]["work_mode"].get("remote_accepted", True):
        return True
    if not city:
        problems.append("城市字段缺失")
        return False
    if mode == "exact":
        if city in cities:
            return True
        problems.append(f"城市不在清单里（{city}）")
        return False
    return True


def _salary_ok(job, problems):
    cf = ROLES["hard_filter"]["salary"]
    job_track = job.get("track") or "未知"

    # 校招不设下限
    if job_track == "校招":
        return True

    daily, note = daily_salary(job)
    if daily is None:
        # 配置里写的是"缺失不淘汰，改由打分降权"
        return True

    tier1 = cf["internship"]["tier1_cities"]
    city = (job.get("city") or "")
    if city in tier1:
        floor = cf["internship"]["tier1_daily_yuan"]
    else:
        floor = cf["internship"]["tier_other_daily_yuan"]

    if daily < floor:
        problems.append(f"日薪 {daily:.0f} 低于下限 {floor}（{note}）")
        return False
    return True


# 抓详情页时浏览器可能"连接被终止"，jd_text 里写进去的不是岗位描述，
# 而是一整段 Chrome 的错误页文字（ERR_CONNECTION_CLOSED / ERR_NETWORK_CHANGED
# / ERR_INTERNET_DISCONNECTED 等）。这种"假 JD"会被打分环节当成"JD 里没有 AI
# 关键词"→ AI 相关维度归 0 → 分数被凭空压低 → 静默淘汰。它跟"岗位真的不匹配"
# 是两码事：后者该淘汰，前者是"该重抓"，混在一起就变成隐性漏投，还查不出来
# （2026-11-11 定位，实测 744 条里 122 条是各种断网错误页）。
#
# 这些信号绝不会出现在正常 JD 里（JD 是招聘描述，不会有"ERR_"错误码、
# "检查代理服务器""运行网络诊断"这种话），所以命中即判"JD 抓取失败"。
# "ERR_" 是 Chrome 所有网络错误码的固定前缀（CONNECTION_CLOSED / NETWORK_CHANGED
# / TIMED_OUT / NAME_NOT_RESOLVED ...），用它能一次性兜住所有变体，不用逐个列。
JD_INVALID_SIGNALS = [
    "ERR_",                       # Chrome 网络错误码统一前缀，兜住所有变体
    "无法访问此网站", "您的连接已中断", "意外终止了连接", "终止了连接",
    "检测到了网络变化", "请试试以下办法",
    "检查代理服务器和防火墙", "检查网线", "运行 Windows 网络诊断",
]


def jd_invalid(job):
    """JD 是不是"抓取失败的错误页文字"。是就返回 (True, 原因)，不是返回 (False, "")。"""
    jd = (job.get("jd_text") or "").strip()
    if not jd:
        return False, "JD 为空"
    for sig in JD_INVALID_SIGNALS:
        if sig in jd:
            return True, f"JD 抓取失败（命中「{sig}」）"
    return False, ""


def _text_ok(job, problems):
    jd = job.get("jd_text") or ""
    title = job.get("title") or ""
    blob = jd + " " + title + " " + " ".join(job.get("job_labels") or [])

    for w in ROLES.get("exclude_keywords", []):
        if w in blob:
            problems.append(f"命中排除词：{w}")
            return False

    # 工作强度硬门槛：JD 命中大小周/单休/996 等明确高强度词，直接淘汰（不打分）
    for sig in ROLES["hard_filter"].get("intensity_signals", []):
        if sig in blob:
            problems.append(f"工作强度不符：{sig}")
            return False

    for sig in ROLES["hard_filter"].get("school_only_signals", []):
        if sig in blob:
            # "需要全职到岗"这条只在你投实习赛道时启用，校招禁用
            if sig == "需要全职到岗" and (job.get("track") == "校招"):
                continue
            problems.append(f"身份不符：{sig}")
            return False

    return True


def layer0(job):
    """返回 (是否通过, 淘汰原因列表)"""
    problems = []
    # 第一道：JD 是不是抓取失败的错误页。是就直接淘汰，并单独标注原因，
    # 别让它装成"正常 JD"走完打分、再被"分数低"静默淘汰（那样查不出漏投）。
    bad, why = jd_invalid(job)
    if bad:
        return False, [why]
    if not _text_ok(job, problems):
        return False, problems
    if not _city_ok(job, problems):
        return False, problems
    if not _salary_ok(job, problems):
        return False, problems
    return True, problems


# ---------- 第二层：关键词粗筛（决定喂给模型的是什么） ----------

def _split_jd(jd):
    """把 JD 拆成句子，用来作为打分证据。"""
    parts = re.split(r"[\n。；!！?？\n\r]+", jd or "")
    return [p.strip() for p in parts if len(p.strip()) >= 4]


def _pairs_hit(fam, ntitle):
    """双条件匹配：标题里"AI 类词"和"岗位词"各出现一个才算命中。

    为什么需要它（2026-10-28 实测）：一条条列穷举不完。
    「AI 产品实习生」出现 5 次全都漏了，因为列表里只有"AI产品经理"。
    改成两组词交叉后，不管是"AI影视产品经理"还是"AI/数字人产品经理"都能进来，
    而"半导体解决方案专家""液冷系统方案工程师"这种没有 AI 词的照样进不来。
    """
    p = fam.get("include_pairs")
    if not p:
        return None
    a = [w for w in p.get("ai_words", []) if _norm(w) in ntitle]
    b = [w for w in p.get("role_words", []) if _norm(w) in ntitle]
    if a and b:
        return f"{a[0]}×{b[0]}"
    return None


def title_blocked(job):
    """标题里有"总监""算法"这类词的，直接淘汰。返回命中的那个词（没有则返回 None）。

    为什么单独拎成一个函数：以前这段只写在 match_family 里，
    只对"靠标题认出来的前四个岗位族"生效。第 5 族（名字里没 AI、靠 JD 正文
    里 AI 词和洞察词同时出现才认出来）不看标题，于是绕过这条规则
    —— 实测就有一条「行业解决方案总监」被放进候选，还拿到 89.2 分。
    投实习/校招的人不可能拿到总监岗，这条现在对整个粗筛生效。
    """
    ntitle = _norm(job.get("title") or "")
    for w in ROLES.get("title_exclude", []):
        if nw := _norm(w):
            if nw in ntitle:
                return w
    return None


def experience_blocked(job):
    """JD/标签里要「3年以上经验」的资深正职岗，直接淘汰。返回命中词（没有则 None）。

    2026-10-31 加，踩的坑：Inspire Group 这条 5-10 年经验的资深正职岗，
    因为岗位族/AI 核心/需求拆解这几个维度都高，被打到 90.3 分混进了投递队列，
    结果点开一看是 30-50K 的 5 年经验岗，应届实习投了必被秒拒。

    规则（避免误杀）：只有当岗位同时满足
      1. JD 或标签里出现资深经验信号（5-10年/3-5年/资深/专家…），
      2. 且【没有】任何应届友好信号（实习/应届/在校/经验不限/学历不限/可招实习…）
    才拦截。这样「经验不限」「可招实习生」的岗位不会被误伤。
    """
    blob = _norm((job.get("jd_text") or "") + " " + (job.get("title") or "")
                 + " " + " ".join(job.get("job_labels") or []))
    senior = ROLES.get("experience_senior_signals", [])
    junior = ROLES.get("experience_junior_signals", [])

    # 先看有没有应届友好信号，有就放行（比如标题明写"可招实习生"的）
    if any(_norm(w) and _norm(w) in blob for w in junior):
        return None

    # 没有友好信号，再查有没有资深经验信号
    for w in senior:
        if (nw := _norm(w)) and nw in blob:
            return w
    return None


def match_family(job):
    """判断这条岗位属于哪个岗位族。返回 (族 id, 命中词) 或 (None, None)"""
    title = job.get("title") or ""
    jd = job.get("jd_text") or ""
    blob = title + " " + jd
    ntitle, nblob = _norm(title), _norm(blob)

    # 标题里有"总监""算法"这类词的，直接淘汰（实习/校招投不上，纯算法也不是你要的方向）
    if title_blocked(job):
        return None, None

    for fam in ROLES["role_clusters"]:
        hit = None
        for kw in fam.get("include", []):
            if _norm(kw) in ntitle or _norm(kw) in nblob:
                hit = kw
                break
        if hit is None:
            hit = _pairs_hit(fam, ntitle)

        if hit:
            return fam["id"], hit
    return None, None


def family_city_ok(fid, job):
    """「岗位族 × 城市」交叉筛选：某个族若配置了 cities，则只投该族在这些城市的岗。

    返回 (是否通过, 原因)。没配置 cities 的族一律通过（跟随全局城市清单）。
    """
    fam = None
    for f in ROLES["role_clusters"]:
        if f.get("id") == fid:
            fam = f
            break
    if not fam:
        return True, ""

    cities = fam.get("cities") or []
    # 留空 = 不限城市，跟随 hard_filter.city 全局清单（已在 layer0 判断过）
    if not cities:
        return True, ""

    city = (job.get("city") or "").strip()
    if city in cities:
        return True, ""
    return False, f"岗位族 {fam.get('label', fid)} 限投城市 {cities}，岗位城市为 {city}"


def layer1(job):
    """
    返回 dict：
      passed       是否通过
      family       岗位族 id
      keyword      命中词
    """
    # 第一道先看标题：总监/算法一类直接出局。
    bad = title_blocked(job)
    if bad:
        return {"passed": False, "family": None, "keyword": None, "excluded_title": bad}

    # 第二道看经验年限：3 年以上经验的资深正职岗，应届实习投不上，直接出局。
    # （放在 title 之后、match_family 之前，跟 title 一样对整个粗筛生效）
    exp = experience_blocked(job)
    if exp:
        return {"passed": False, "family": None, "keyword": None, "excluded_exp": exp}

    fid, kw = match_family(job)

    if fid is None:
        return {"passed": False, "family": None, "keyword": None}

    # 岗位族 × 城市交叉筛选
    city_ok, city_why = family_city_ok(fid, job)
    if not city_ok:
        return {"passed": False, "family": fid, "keyword": kw, "excluded_city": city_why}

    return {"passed": True, "family": fid, "keyword": kw}
