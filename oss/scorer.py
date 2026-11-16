# 第三层：给岗位打分
#
# 为什么是几个维度分开打，而不是直接问"这条匹配吗 1-100"：
#   因为总分会掩盖真相。一条岗位可能"岗位匹配"满分，但"要求契合度"其实很低，
#   问总分它会给个体面的 75 分混进来。拆开之后你能直接看见哪一项拉胯。
#
# 硬规则：每个维度都必须附 JD 原句作为证据。没有证据 = 0 分。
#   原因：模型会编。它看着岗位名，就能顺着编出一句显然不存在的 JD 描述。

import re
from config_loader import roles
from filters import daily_salary, _split_jd

ROLES = roles()
DIMS = ROLES["scoring"]["dimensions"]
THRESH = ROLES["scoring"]["thresholds"]

NEUTRAL = 40

# 每个维度去 JD 里找什么词
# 成长空间分三档（强/中/弱），打分时档位高的词权重高：
#   强 = 有明确带教/培养机制；中 = 有上升通道；弱 = 泛泛的"成长"字样。
EVIDENCE_WORDS = {
    "role_match":      ["产品经理", "产品运营", "项目运营", "项目经理", "FDE",
                        "解决方案", "交付", "项目实施", "PMO"],
    "growth_stage":    {
        "strong": ["带教", "导师", "mentor", "一对一", "师傅带", "手把手",
                   "培养计划", "管培生", "导师制"],
        "mid":    ["晋升通道", "晋升路径", "轮岗", "转正", "留用", "校招",
                   "培训体系", "新人培训"],
        "weak":   ["成长", "发展空间", "学习机会"],
    },
    "industry_platform": {
        "industry":  ["人工智能", "大模型", "AI", "AIGC", "Agent", "云计算",
                      "新能源", "半导体", "芯片", "生物医药", "机器人",
                      "数据", "SaaS", "互联网"],
        "platform":  ["独角兽", "上市公司", "融资", "A轮", "B轮", "C轮", "D轮",
                      "行业领先", "头部", "龙头"],
    },
    "pay_value":      ["接受远程", "远程", "线上办公", "弹性", "不限天数",
                       "可线上", "居家", "五险一金", "补充医疗", "房补", "餐补"],
}


def _calc(sents, words, cap=100):
    """
    按"命中几种词 + 命中几句"给分。
    words 可以是平铺列表（不分档），也可以是 {strong/mid/weak: [...]} 分档结构。
    分档时：强档词每个计 3 分、中档 2 分、弱档 1 分；平铺列表沿用旧口径。
    只认真实出现在 JD 里的句子，找不到就是 0（宁可错杀，不接受印象分）。
    """
    # 归一化：把分档结构拍平成 (词, 档位) 列表，平铺列表统一记"平"档
    if isinstance(words, dict):
        flat = []
        for tier, ws in words.items():
            for w in ws:
                flat.append((w, tier))
    else:
        flat = [(w, "flat") for w in words]

    hits = []
    for s in sents:
        for w, tier in flat:
            if w in s:
                hits.append({"word": w, "tier": tier, "sentence": s[:60]})
                break
    if not hits:
        return 0, [], "no_evidence"

    # 分档打分：强=3 / 中=2 / 弱=1，其余档位（如 industry/platform）按中档 2 分算；
    # 平铺列表沿用旧公式（按命中种类数）
    if any(h["tier"] != "flat" for h in hits):
        tier_score = {"strong": 3, "mid": 2, "weak": 1}
        points = sum(tier_score.get(h["tier"], 2) for h in hits)
        # 基础 30 分起步，每个有效信号累加，封顶 100
        base = min(100, 30 + points * 10)
    else:
        kinds = len({h["word"] for h in hits})
        base = min(100, 40 + kinds * 20 + (len(hits) - 1) * 8)
    return min(base, cap), hits[:3], "ok"


def score_job(job, layer1_result):
    jd = job.get("jd_text") or ""
    title = job.get("title") or ""
    sents = _split_jd(jd) + [title]

    family = layer1_result.get("family")

    detail = {}
    total = 0.0
    w_sum = 0.0          # ← 分子只累加"JD 里真的有信息"的那些维度的权重

    for dim in DIMS:
        key = dim["key"]
        words = EVIDENCE_WORDS.get(key, [])

        # ── 岗位匹配度：直接用粗筛的结论，不再数关键词 ──
        # 岗位名已经在目标族里了，那就是匹配，不需要再从 JD 里找一遍。
        if key == "role_match":
            kw = layer1_result.get("keyword") or ""
            if family:
                if kw and ("×" in kw):
                    # 双词交叉命中（如 "AI×产品"），比纯关键词略弱
                    s, ev, status = 92, [{"word": "双词交叉命中",
                                          "sentence": f"标题里 AI 词 × 岗位词 同时出现（{kw}）"}], "ok"
                else:
                    # 标题直接命中目标族关键词，最精准
                    s, ev, status = 100, [{"word": "目标岗位族命中",
                                           "sentence": f"标题命中目标族关键词（{kw or family}）"}], "ok"
            else:
                s, ev, status = 0, [], "no_evidence"

        else:
            s, ev, status = _calc(sents, words)
            # JD 没提 ≠ 这条不行。这些维度是"信息缺失"，按中性处理。
            if status == "no_evidence":
                s, ev, status = NEUTRAL, [{"word": "JD 未提及", "sentence": "JD 没写，按中性处理，不扣"}], "no_info"

        no_info = (status == "no_info")

        detail[key] = {
            "name": dim["name"],
            "weight": dim["weight"],
            "score": s,
            "evidence": ev,
            "status": status,
            "counted": not no_info,      # False = 这一维 JD 没提，没进分母
        }

        # ── 分母口径：JD 没提的维度不算进分母 ──
        # 以前的做法：JD 没提 = 记 40 分，照常按权重算。听起来很温和，实际是罚款：
        #   cost(权重15) 和 growth_stage(权重20) 在绝大多数 JD 里都找不到，
        #   于是这两项永远只拿 40 分，白白扣分，让真正的好岗位被"JD 没写字"压低了。
        # 现在的做法：JD 没提 = 这一维整个退场，分子分母都不算，
        #   剩下的维度按权重重新摊成 100 分。JD 没提就不再对个人不利。
        if no_info:
            continue
        total += s * dim["weight"] / 100.0
        w_sum += dim["weight"]

    # 按"真正算进来"的权重重新摊成百分制
    if w_sum > 0:
        base_score = total * 100.0 / w_sum
        total = base_score
    else:
        base_score = total = 0.0

    # 基础分先封顶到 100（封顶只对"加权摊分"这一层，不吞掉后面的加减分）
    total = min(100.0, total)

    # ── 加减分：全部从配置读，代码按 key 分发判定，不再硬编码 ──
    #   好处：调加减分规则只改 config/target_roles.yaml 的 bonus/penalty，
    #   不用动这个文件。cond 字段只是给人看的说明，程序认的是 key。
    blob = jd + " " + title
    act = job.get("hr_activity") or ""
    grad_year = str(ROLES.get("identity", {}).get("graduation_year") or "")
    # 先把一次算出来的中间信号准备好，供各 key 判定用
    try:
        pd_ = job.get("publish_days")
        pdays = int(pd_) if pd_ not in (None, "") else None
    except (ValueError, TypeError):
        pdays = None
    daily, note = daily_salary(job)
    scale = (job.get("brand_scale") or "").strip()

    bonus_applied = []
    for b in ROLES["scoring"].get("bonus", []):
        key = b.get("key", "")
        sc = b["score"]
        hit = False
        if key == "accept_grad":
            hit = any(k in blob for k in ["应届", grad_year])
        elif key == "has_mentor":
            hit = any(k in blob for k in ["带教", "导师", "mentor"])
        elif key == "hr_active_d3":
            hit = act in ("now", "today", "d3")
        if hit:
            total += sc
            bonus_applied.append(f"{b.get('cond', key)} {sc:+d}")

    penalty_applied = []
    for p in ROLES["scoring"].get("penalty", []):
        key = p.get("key", "")
        sc = p["score"]
        hit = False
        if key == "salary_missing":
            hit = (daily is None)
        elif key == "company_tiny_fde":
            hit = (scale == "0-20人" and layer1_result.get("family") == "fde")
        elif key == "hr_inactive_d15":
            hit = (act == "d15")
        elif key == "hr_inactive_d7":
            hit = (act in ("d7", "d10"))
        elif key == "stale_listing":
            hit = (pdays is not None and pdays >= 30)
        if hit:
            total += sc
            penalty_applied.append(f"{p.get('cond', key)} {sc:+d}")

    if bonus_applied:
        detail["bonus"] = bonus_applied
    if penalty_applied:
        detail["penalty"] = penalty_applied
    if daily is not None:
        detail["daily_salary"] = round(daily, 1)

    # 最终只做下限保护（分数可为负的极端情况兜底），上限不再压回 100，
    # 让加分项（尤其"接受应届 +5"这种你最看重的）在高分岗上真正生效。
    total = max(0.0, total)

    if total >= THRESH["strong"]:
        band = "strong"
    else:
        # 2026-11-08：删除 neutral 档（原 neutral=70 > strong=60 倒挂）。
        # 现在只剩两档：≥strong 默认发；<strong 但 ≥hard_floor 留给人工看。
        band = "neutral" if total >= THRESH["hard_floor"] else "drop"

    return {
        "job_id": job.get("job_id"),
        "title": title,
        "company": job.get("company"),
        "city": job.get("city"),
        "family": layer1_result.get("family"),
        "score": round(total, 1),
        "base_score": round(base_score, 1),
        "band": band,
        "below_floor": total < THRESH["hard_floor"],
        "detail": detail,
    }


def rank(scored, pick=None):
    """按分数从高到低排，取前 pick 条。低于硬底线的不进队。"""
    alive = [s for s in scored if not s["below_floor"]]
    alive.sort(key=lambda x: -x["score"])
    return alive[:pick] if pick else alive
