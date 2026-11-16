# 第三层：给岗位打分
#
# 为什么是六个维度分开打，而不是直接问"这条匹配吗 1-100"：
#   因为总分会掩盖真相。一条岗位可能"AI 相关性"满分、薪资满分，
#   但你最看重的"要求需求拆解/同理心洞察"压根没提 —— 问总分它会给个体面的 75 分混进来。
#   拆开之后你能直接看见"'需求拆解'这项 0 分"，这条就该死。
#
# 硬规则：每个维度都必须附 JD 原句作为证据。没有证据 = 0 分。
#   原因：模型会编。它看着岗位名叫"AI 产品经理"，就能顺着编出一句显然不存在的 JD 描述。

import re
from config_loader import roles
from filters import daily_salary, _split_jd

ROLES = roles()
DIMS = ROLES["scoring"]["dimensions"]
THRESH = ROLES["scoring"]["thresholds"]

NEUTRAL = 40

# 这两个是真筛选项：JD 里没有，就是这条岗不达标，按 0 分。
# 其余三项（要求契合/成长/性价比）属于"JD 没写"，不代表不好，见下方 NEUTRAL。
STRICT_KEYS = {"ai_core", "decomp_insight"}

# 每个维度去 JD 里找什么词
EVIDENCE_WORDS = {
    "role_match":      ["产品经理", "产品运营", "项目运营", "项目经理", "FDE",
                        "解决方案", "交付", "项目实施", "PMO"],
    "ai_core":         ["AI", "大模型", "LLM", "Agent", "AIGC", "RAG", "提示词",
                        "人工智能", "GPT", "模型能力", "Prompt"],
    "requirement_fit": ["应届", "2027", "在校", "实习生", "本科", "无经验要求",
                        "零经验", "接受应届生", "大三", "大四"],
    "decomp_insight":  ["需求分析", "需求拆解", "用户洞察", "同理心", "用户访谈",
                        "诉求", "用户研究", "共情", "用户体验", "同理",
                        "业务方", "需求文档", "PRD", "场景梳理"],
    "growth_stage":    ["带教", "导师", "mentor", "培养计划", "成长", "手把手",
                        "转正", "留用", "校招", "管培"],
    "cost":            ["接受远程", "远程", "线上办公", "弹性", "不限天数",
                        "可线上", "居家"],
}


def _calc(sents, words, cap=100):
    """
    按"命中几种词 + 命中几句"给分。
    只认真实出现在 JD 里的句子，找不到就是 0（宁可错杀，不接受印象分）。
    """
    hits = []
    for s in sents:
        for w in words:
            if w in s:
                hits.append({"word": w, "sentence": s[:60]})
                break
    if not hits:
        return 0, [], "no_evidence"

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

        # ── 岗位族匹配：直接用粗筛的结论，不再数关键词 ──
        # 岗位名已经在目标族里了，那就是匹配，不需要再从 JD 里找一遍。
        # 2026-10-30 加梯度：这一项权重最高（25%），但以前只有 90/75/0 三档，
        # 精准命中（标题里有 AI 词）和勉强命中（靠 JD 正文认出来）拉不开差距，
        # 让最大的权重白搭。现在按"命中质量"分档：
        #   - 标题直接命中目标族关键词 → 100（最精准）
        #   - 标题靠"AI×岗位"双词交叉命中 → 92
        #   - 只在 JD 正文里命中 → 80
        #   - 第 5 族（靠 JD 里 AI+洞察两组词）→ 75
        if key == "role_match":
            kw = layer1_result.get("keyword") or ""
            # 第 5 族：名字里没 AI，只能靠 JD 正文两组词认出来 → 最弱
            if family is None and (layer1_result.get("insight_hits") or {}).get("insight"):
                s, ev, status = 75, [{"word": "第5族 JD 正文命中",
                                      "sentence": "名字里没有 AI，靠 JD 里 AI+洞察两组词认出"}], "ok"
            elif family:
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

        # ── 第 5 族在"需求拆解"这一项天然成立（它就是靠这组词才被认出来的）──
        elif key == "decomp_insight" and family is None:
            hits_map = layer1_result.get("insight_hits") or {}
            if hits_map.get("insight"):
                ev = [{"word": w, "sentence": "(JD 正文命中)"} for w in hits_map["insight"][:2]]
                s, status = 100, "ok"
            else:
                s, ev, status = _calc(sents, words)

        else:
            s, ev, status = _calc(sents, words)
            # JD 没提 ≠ 这条不行。这些维度是"信息缺失"，按中性处理。
            # （而 ai_core / decomp_insight 是真筛选项，缺失就是 0 —— 见 STRICT_KEYS）
            if status == "no_evidence" and key not in STRICT_KEYS:
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

        # ── 分母口径（2026-10-28 用户拍板）：JD 没提的维度不算进分母 ──
        # 以前的做法：JD 没提 = 记 40 分，照常按权重算。听起来很温和，实际是罚款：
        #   cost(权重10) 和 growth_stage(权重10) 在绝大多数 JD 里都找不到，
        #   于是这两项永远只拿 40 分，白白扣掉 12 分。四项全满分也只有 85.5 分。
        #   （注：85.5 是旧口径的上限，不是发送阈值；发送线见 scoring.thresholds.strong）
        #   结果：真正的好岗位被"JD 没写字"这件事压到了自动发送线以下。
        # 现在的做法：JD 没提 = 这一维整个退场，分子分母都不算，
        #   剩下的维度按权重重新摊成 100 分。JD 没提就不再对个人不利。
        # 例外：ai_core / decomp_insight 是真筛选项（STRICT_KEYS），
        #   JD 没提就 0 分，且必须留在分母里 —— 否则"这岗位根本没提 AI"也能拿高分。
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
