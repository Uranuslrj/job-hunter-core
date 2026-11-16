# 第四层：写打招呼语 + 五道质量门
#
# 结构（三段一个都不能少）：
#   钩子 —— JD 锚点。证明我真的读了你的岗位，不是群发。
#   证据 —— 简历锚点。不说"我有洞察力"，给一件可验证的事。
#   提问 —— 给 HR 一个低成本回复的理由。
#
# 缺了双锚点，写出来一定是这样：
#   "您好，我对贵司的 AI 产品经理岗位很感兴趣，我学习能力强、有责任心，希望能有机会进一步沟通。"
#   这是群发，HR 一眼看穿。

import csv
import os
import re
from config_loader import greeting_spec, path_of

SPEC = greeting_spec()

# ⚠ 只启用简历里真有的经历。理由：招呼语里说的事，HR 翻简历找不到就会判你编的。
#   "系统学习过心理学与认知学"这句在四份简历里都没有出处 → 先停用，等本人确认后打开。
ANCHORS = {k: v for k, v in SPEC["resume_anchors"].items() if v.get("enabled", True)}
CLOSINGS = SPEC["closing"]["variants"]
PICK_RULES = SPEC["closing"]["pick_by_situation"]
SALUT = SPEC["salutation"]
REQUIRED = SPEC["required_discourse"]
SELF_INTRO = (SPEC.get("self_intro") or {}).get("text", "") if (SPEC.get("self_intro") or {}).get("enabled") else ""
AVAIL = (SPEC.get("availability") or {}).get("text", "") if (SPEC.get("availability") or {}).get("enabled") else ""
AVAIL_CFG = SPEC.get("availability") or {}
AVAIL_INTERN_ONLY = bool(AVAIL_CFG.get("intern_only"))
AVAIL_INTERN_EXTRA = (AVAIL_CFG.get("intern_extra") or "").strip()
HOOK_PHRASE = REQUIRED["career_hook_phrase"]

# ── 本人 2026-10-29 SOP 新增的几块（能力格式 / RAG 必带 GitHub / 求职动机 / 固定结尾）
#
# ⚠ 2026-11-13 本人拍板处置：yaml 里原本的 framework / identity（greeting_spec 那份）
#   两块死配置已删（招呼语生成从不读它们）。tech_stack 保留（本人留作人工参考）。
#   注意 target_roles.yaml 里的 identity.graduation_year 被 scorer.py 读，别删。
EVID_FMT = SPEC.get("evidence_format") or {}
ABILITY_CLAUSE = EVID_FMT.get("ability_clause") or {}
_CLAUSE_DEFAULT = ABILITY_CLAUSE.get("default") or "能独立完成{name}"
_CLAUSE_EXC = ABILITY_CLAUSE.get("exceptions") or {}
MUST_END_ABILITY = bool(EVID_FMT.get("must_end_with_ability"))
RAG_RULE = SPEC.get("rag_github_rule") or {}
RAG_ON = bool(RAG_RULE.get("enabled"))
# ⚠ 不读 triggered_by：实际判定是【这条锚点在 yaml 里挂了 github 链接】才算
#   （见 _lay_out 里的判断）。triggered_by 列了三个项目，但项目②③没作品集地址，
#   按它执行会给没作品集的项目也套上"已上传 GitHub"。两条规则冲突，已记 decision_pending。
RAG_TEXT = RAG_RULE.get("text") or ""
MOTIV = SPEC.get("motivation") or {}
MOTIV_PREFIX = MOTIV.get("prefix") or "最主要的是我"
# 结尾：本人 SOP 要求固定一句。fixed=true 时只读 fixed_text，不再按岗位挑变体。
CLOSING_FIXED = bool((SPEC.get("closing") or {}).get("fixed"))
CLOSING_FIXED_TEXT = (SPEC.get("closing") or {}).get("fixed_text") or ""

# ── 2026-10-29 晚 本人拍板：模型「只选不写」正式接入（配置见 yaml 的 evidence_pick）
#
#   模型只干一件事：在"这条能力用哪句证据"的白名单里挑一个编号。
#   正文永远是代码拿编号去档案里取的原文 —— 它没有写字的权力，
#   所以不可能编造经历和数据，也不可能把六段框架写歪。
#
#   开关三种状态（优先级从高到低）：
#     BOSS_EVIDENCE_PICK=off/on 环境变量  >  yaml 的 evidence_pick.enabled
#   关掉之后整条链路退化成纯规则版，行为跟接入之前一模一样。
EP_CFG = SPEC.get("evidence_pick") or {}

# ── 2026-10-30 本人新增：开头流水账结构（见 yaml 的 intro_flow）
#   ① 学历句后固定加「曾于<实习公司>AI产品经理岗位实习」
#   ② 经历段一律用「也/同时」接续，不再「我做过」另起一句
#   ③ 剧本杀项目先写总起句「我做过剧本杀DM门店AI运营项目，且已应用到门店」
INTRO_FLOW = SPEC.get("intro_flow") or {}
INTRO_FLOW_ON = bool(INTRO_FLOW.get("enabled"))
# ★ 2026-11-01 本人拍板：心理学软技能固定第二段。
#   开关在 yaml 的 soft_skill_narrative.fixed_second 里（默认开），
#   句文本用 psychology 锚点的 short（带 200+ 客户数据，省字数）。
_SSN = SPEC.get("soft_skill_narrative") or {}
FIXED_PSYCH_ON = bool(_SSN.get("fixed_second", True))
PSYCH_SHORT = (ANCHORS.get("psychology") or {}).get("short") or ""
INTRO_INTERN_LINE = (INTRO_FLOW.get("intern_line") or "").strip()
INTRO_CONNECTORS = [c for c in (INTRO_FLOW.get("connectors") or []) if c]
INTRO_RAG_OPENER = (INTRO_FLOW.get("rag_opener") or "").strip()
INTRO_RAG_ANCHORS = set(INTRO_FLOW.get("rag_opener_anchors") or [])

# ── 2026-11-01 本人新增：固定话术的同义变体池。
#   七处固定话术各配了几个语义等价、表述不同的变体，目的是打破「100 条一字不差」
#   的群发特征（群发是风控最醒目的信号之一）。
#   轮换规律不是随机，而是【按 job_id 确定性哈希】选一个下标：
#     · 同一个岗位，无论跑多少次、补发多少次，选出来的变体永远一样 → 人看着稳定，不穿帮；
#     · 不同岗位落到不同变体 → 一条条消息的固定段各不相同，不再清一色；
#     · 不依赖随机种子、不用记状态，天然可复现。
#   每处都保留原句（text 本身）作为兜底：变体池为空或 job 拿不到 id 时，退回原句。
_VARIANT_CFG = SPEC.get("fixed_variant_switch") or {}
VARIANT_ON = bool(_VARIANT_CFG.get("enabled", True))


def _pick_variant(variants, job, default_text):
    """从变体池里按 job_id 确定性挑一句；拿不到 id 或池子空 → 退回 default_text。

    variants: [{"id": "B1", "text": "..."}, ...]
    用 job_id 的稳定哈希映射到池内下标，保证同岗位永远同变体。
    """
    if not VARIANT_ON or not variants:
        return default_text
    jid = (job or {}).get("job_id") or (job or {}).get("id") or ""
    if not jid:
        return default_text
    h = 0
    for ch in str(jid):
        h = (h * 131 + ord(ch)) & 0xFFFFFFFF
    return variants[h % len(variants)].get("text") or default_text


def _pick_variant_id(variants, job):
    """和 _pick_variant 用同一套哈希，但只返回选中的变体 id（不返回 text）。

    这是 2026-11-11 为了「A/B 测试归因」加的：招呼语生成时选中的变体 id 要
    记进台账，才能按 id 分组统计「哪个变体回复率更高」。之前只回传 text，
    id 丢了，台账里的 salutation_variant 一直是写死的 'neutral'（断链）。
    返回 '' 表示没选到（变体池空 / 开关关 / 拿不到 job_id / 池里没 id 字段）。
    """
    if not VARIANT_ON or not variants:
        return ""
    jid = (job or {}).get("job_id") or (job or {}).get("id") or ""
    if not jid:
        return ""
    h = 0
    for ch in str(jid):
        h = (h * 131 + ord(ch)) & 0xFFFFFFFF
    return variants[h % len(variants)].get("id") or ""


# ── 三个「在内部函数里被引用」的变体，用模块级变量传递。
#   这些函数（_assemble / _lay_out / _all_evidence）拿不到 job，但项目是单线程顺序执行，
#   所以在 build 入口一次性算好本条的三处变体存这里，函数读取时「有变体用变体、否则回退原句」。
_CUR_INTERN = None      # ② 实习句变体
_CUR_RAG_OPENER = None  # ③ 剧本杀总起句变体
_CUR_RAG_TEXT = None    # ④ GitHub 话术变体
_CUR_FORCED_FID = None  # ★ 本条被「强制插队」选中的模块 id（按词选模块的结果）
_CUR_FALLBACK = False   # ★ 本条 JD 没有任何项目锚点命中（弱命中/无命中），走杀手锏顺序


def _set_current_variants(job):
    """在 build 入口按 job 算好三处内部变体，存入模块级变量。"""
    global _CUR_INTERN, _CUR_RAG_OPENER, _CUR_RAG_TEXT
    _CUR_INTERN = _pick_variant(INTRO_FLOW.get("intern_line_variants"),
                                job, INTRO_INTERN_LINE)
    _CUR_RAG_OPENER = _pick_variant(INTRO_FLOW.get("rag_opener_variants"),
                                    job, INTRO_RAG_OPENER)
    _CUR_RAG_TEXT = _pick_variant(RAG_RULE.get("text_variants"), job, RAG_TEXT)


def _set_forced_anchor(fid):
    """记录本条被强制选中的模块，供 _lay_out 排序时压过「带数字往前」规则。"""
    global _CUR_FORCED_FID
    _CUR_FORCED_FID = fid


def _set_fallback(on):
    """记录本条 JD 是否「没有任何项目锚点命中」——是则走杀手锏顺序。"""
    global _CUR_FALLBACK
    _CUR_FALLBACK = on


def _evp_intern():
    return _CUR_INTERN if _CUR_INTERN is not None else INTRO_INTERN_LINE


def _evp_rag_opener():
    return _CUR_RAG_OPENER if _CUR_RAG_OPENER is not None else INTRO_RAG_OPENER


def _evp_rag_text():
    return _CUR_RAG_TEXT if _CUR_RAG_TEXT is not None else RAG_TEXT


def _evidence_pick_on():
    """模型选证据是否启用。环境变量优先，方便不改文件做 A/B 对比。"""
    env = (os.environ.get("BOSS_EVIDENCE_PICK") or "").strip().lower()
    if env in ("off", "0", "false", "no"):
        return False
    if env in ("on", "1", "true", "yes"):
        return True
    return bool(EP_CFG.get("enabled"))


def ability_clause(name):
    """把 JD 能力名变成一句"对应能力"（本人 SOP 第 3 条要求的第三个成分）。

    例：工作流搭建 → "能独立完成工作流搭建"
    英文开头的名字前面补个空格，免得读成"能独立完成AI"这种黏在一起的怪句。
    """
    if not name:
        return ""
    if name in _CLAUSE_EXC:
        return _CLAUSE_EXC[name]
    sep = " " if (name[:1].isascii() and name[:1].isalnum()) else ""
    return _CLAUSE_DEFAULT.format(name=sep + name)


def _goal_from_job(job):
    """求职动机里的"岗位方向"从哪来。

    ① 岗位数据里有 family → 用它查 CAREER_LINE；
    ② 没有 family（实测 114 条全都是 None）→ 从岗位名里取，
       只取第一个括号/横杠之前的部分，免得把"AI产品经理-【医疗ai经验-双休】"
       整串塞进去。
    """
    fam = job.get("family")
    if fam and fam in CAREER_LINE:
        career = CAREER_LINE[fam]
        goal = career
        for p in ("长期想做", "长期想走", "长期想在"):
            if goal.startswith(p):
                goal = goal[len(p):].strip()
                break
        return goal.replace("这条路", "").strip()
    title = (job.get("title") or "").strip()
    head = re.split(r"[（(【\-—|/]", title)[0].strip()
    if head[:2].lower() == "ai":
        head = "AI" + head[2:]
    return head or "AI 应用"

# 60–120 是上一版的工程默认值（当时找不到任何带测量方法的来源）。
# 现在有了 Boomerang 4000 万封邮件的数据：50–125 词区间回复率全部 >50%，
# 而【低于 50 词比 125 词掉得还快，25 词的效果等同于 2000 词】—— 长度是有下限的。
# 50 英文词按信息密度折成中文大约 75–100 字（这个折算是估算，不是证据）。
# 2026-10-28 本人要求扩篇幅：80–140 → **100–160**。
_LEN = SPEC.get("length") or {}
LENGTH = [int(_LEN.get("min", 100)), int(_LEN.get("max", 160))]
SIM_MAX = 0.6          # ⚠ 2026-10-28 本人要求：相似度不再作为拦截条件，只留着显示
SIM_GATE = False       # True = 拦；False = 只算不算拦
PHRASE_MAX = 20          # JD 锚点最长字数（篇幅扩到 100-160 后放宽），再长会挤掉证据段

FORBIDDEN = []
for g in SPEC["quality_gate"]:
    if g["id"] == "forbidden":
        for _k, words in g["words"].items():
            FORBIDDEN.extend(words)

# ── 风控敏感词（第六道门，2026-10-31 加）────────────────────────────
# 跟上面的 FORBIDDEN（讨好奉承词，命中了只是"减分"）不是一回事：
# 这些是平台风控语义模型会抓的词，命中就可能触发限流甚至封号。
# 来源：knowledge/fengkong-solutions.md 3.1/3.2（tsight + 官方协议 + 真实案例）。
# 分两档：硬拦截（碰到就不过门）+ 引流词（站外联系方式，最危险）。
#
# ⚠ 选词铁律（项目记忆第十七条）：风控词不能选"会在正常表述里自然出现"的词。
#   「好评」曾误伤「好评率约90%」这句真实数据 → 改成「刷好评」这种动作形式；
#   「私下」保留（正常招呼语不会说"私下"）；「引流」保留（求职者不会说"引流"）。
RISK_SENSITIVE_HARD = ["兼职", "刷单", "刷好评", "私下", "引流", "押金",
                       "培训费", "日结", "副业", "垫付", "在家兼职"]
RISK_SENSITIVE_OFFPLATFORM = ["加我微信", "加微信", "扫码", "微信详聊",
                              "加我QQ", "加QQ", "私聊我", "留电话", "手机号联系"]

JD_STRONG = ["需求拆解", "需求分析", "用户洞察", "同理心", "共情", "诉求",
             "用户访谈", "用户研究", "用户体验", "PRD", "业务方", "场景梳理"]
JD_SECOND = ["AI", "大模型", "Agent", "AIGC", "RAG", "提示词", "模型"]

LABELS = ["岗位职责", "任职要求", "职位要求", "职位描述", "工作内容",
          "岗位要求", "职责描述", "岗位简介", "工作职责"]

CAREER_LINE = {
    "ai_product_manager": "长期想做 AI 产品经理",
    "fde": "长期想走 FDE / AI 交付这条路",
    "ai_product_ops": "长期想做 AI 产品运营",
    "ai_project_ops": "长期想走 AI 项目运营这条路",
    None: "长期想在 AI 应用这条路上做深",
}

# AI 类岗位优先拿项目经历当证据，别用服务员经历去申产品经理
# 2026-10-29 移到 yaml 的 anchor_pick（本人可改），这里留默认值兜底
_PICK = SPEC.get("anchor_pick") or {}
TECH_ANCHORS = set(_PICK.get("tech_anchors") or
                   ["project_rag", "project_intern", "project_workflow"])
TECH_BOOST = int(_PICK.get("tech_boost") or 2)

# ★ 2026-11-10 晚 本人拍板（做法一：真正合并）：DM 匹配、AI 反馈闭环不再是两个
#   独立项目，而是剧本杀项目（project_rag）下面的块。它们的内容已并入
#   project_rag 的 blocks / proves，所以这两个锚点不再作为独立经历输出。
#   这里列出来，是为了让 _ranked_anchors / 兜底排序 / 白名单里凡是引用它们的地方，
#   统一跳过或改指向 project_rag，避免它们再各立门户。
MERGED_INTO_RAG = {"project_dm_match", "project_review_loop"}

# 不算证据的"套话"。这些词在 JD 里几乎人人都有，
# 拿它们当命中，等于让<经历A>经历去投 FDE 工程师。
WEAK_PROVES = {"抗压", "结果导向", "团队协作", "晋升曲线", "多线程协调",
               "转化", "复购引导", "服务交付", "内容传播", "项目收尾",
               "商业决策", "流程设计", "质量意识", "状态机"}

# 2026-10-28 本人要求：别再把 JD 原句搬进招呼语。
# 原句说的是"我要什么"，搬过来等于把对方刚写的东西念一遍给他听，
# 不产生新信息还白占二三十字。这里把 JD 关键词翻译成"我会什么"的能力名。
ABILITY_NAMES = SPEC.get("ability_names") or {}
ABILITY_THEMES = SPEC.get("jd_ability_themes") or {}
ABIL_EVID = SPEC.get("ability_evidence_order") or {}

# ★ 2026-10-29 本人规则：说了同一组数据的证据不能同时出现（见 anchor_pick.mutually_exclusive）
#   例：psychology 和 ToC_service 都带 200+ / 90% / 50%，同时上会让 HR 看到两遍，像凑数。
MUTEX = [set(p) for p in (SPEC.get("anchor_pick", {}).get("mutually_exclusive") or [])
         if isinstance(p, (list, tuple))]

# ★ 2026-10-29 本人规则：软技能统一表述（见 yaml 的 soft_skill_narrative）
#   命中"沟通 / 需求拆解 / 洞察 / 跨角色协作"这类要求时：
#     ① 优先用 psychology 那条素材（已排进 needs / user 主题第一位）
#     ② 不许再拿<经历B>（coordination）/ <经历A>（sales）顶替
_SOFT = SPEC.get("soft_skill_narrative") or {}
_SOFT_CHK = _SOFT.get("checks") or {}
SOFT_TRIGGERS = [t for t in (_SOFT_CHK.get("trigger_words") or []) if t]
SOFT_BLOCK_IDS = [r.get("id") for r in (_SOFT.get("replaced_experiences") or [])
                  if r.get("id")]


def _soft_mode(job):
    """这条岗位有没有在要软技能（沟通能力 / 需求拆解 / 用户洞察 / 跨角色协作）。"""
    if not SOFT_TRIGGERS:
        return False
    blob = " ".join(str(job.get(f) or "") for f in ("title", "jd_text"))
    return any(t in blob for t in SOFT_TRIGGERS)


# ---------- 从 JD 里挑一句话当锚点 ----------

def _strip_unbalanced(s):
    """把半边括号剔掉：JD 里这种写法很常见
    ——「包括：……局部重绘）的技术演进」，切出来的片段会带着一个孤零零的右括号。"""
    out = []
    depth = 0
    for ch in s:
        if ch in "（(":
            depth += 1
            out.append(ch)
        elif ch in "）)":
            if depth > 0:
                depth -= 1
                out.append(ch)
            # 这一步就是关键：没有左括号配对的右括号，直接扔掉
        else:
            out.append(ch)
    if depth > 0:                       # 反过来：左括号开着没关，也一并扔掉
        out = [c for c in out if c not in "（("]
    return "".join(out)


def _clean_phrase(sent):
    """
    从 JD 原句里切出一段干净、短的、能证明"我读过你这条 JD"的话。
    目标长度 8-22 字：太短显得敷衍（如"需要同理心"），太长会把证据挤没了。
    """
    for lb in LABELS:
        if sent.startswith(lb):
            sent = sent[len(lb):].lstrip("：: ,， ")
            break
    # JD 里常见的编号和括号残留在片段首尾会很丑（例如「局部重绘）的技术演进」），
    # 一律剃掉开头列举符号 / 结尾没有配对的半边括号。
    sent = re.sub(r"\s*[/｜|]+\s*", "、", sent)   # JD 里常拿斜杠列举，先统一成顿号，免得切出半截
    sent = re.sub(r"^[\d]+[.、)．]\s*", "", sent.strip())
    sent = _strip_unbalanced(sent)
    sent = re.sub(r"^[（(【\[》《\"'“”‘’]+|[）)】\]》\"'“”‘’]+$", "", sent.strip())
    sent = sent.strip("：: ,，、；;。*·-— ")
    parts = [p.strip() for p in re.split(r"[，,、；;。:：]", sent) if len(p.strip()) >= 4]
    if not parts:
        return sent.strip()[:PHRASE_MAX]

    def rank(p):
        hit_strong = any(w in p for w in JD_STRONG)
        if not (8 <= len(p) <= PHRASE_MAX):
            return (0, -len(p))
        return (2 if hit_strong else 1, min(len(p), PHRASE_MAX))

    best = max(parts, key=rank)
    if rank(best)[0] == 0:                      # 没有长度合适的，取最接近的截断
        best = min(parts, key=lambda p: abs(len(p) - 14))
    # 兜底最后一刀：括号这东西怎么切都可能剩下半边，干脆全剔掉。
    # 比如「大模型的算法演进（diffusion 局部重绘）的边界」→
    # 留着残括号 HR 一眼看出机器挑的，剔掉反而更像人读出来的。
    best = re.sub(r"[（(）)【】\[\]《》]", "", _strip_unbalanced(best))
    return _soft_trim(best)


def _soft_trim(p, hard=None):
    """非要截断时，也要断在标点上 —— 宁可短两个字，也别把词切成半截。

    以前直接 best[:16]，切出来过「实现用户体验的提升和用户规模的增」这种半句话，
    HR 一眼看出是机器抓的。现在回退到最近的标点，找不到就再退两字。
    """
    hard = hard or PHRASE_MAX
    if len(p) <= hard:
        return p.strip("、，, ")
    cut = p[:hard]
    for i in range(len(cut) - 1, max(6, len(cut) - 8), -1):
        if cut[i] in "，,、；;：: （(、":
            return cut[:i].strip("、，, ")
    return cut[:hard - 2].strip("、，, ")


def _score_sent(s):
    return sum(3 for w in JD_STRONG if w in s) + sum(1 for w in JD_SECOND if w in s)


# 这些句子不算"锚点"：它们是入职条件，不是岗位在做什么。
# 拿"要求每周到岗 4 天"当共鸣点发出去，HR 一眼看出是机器挑的。
ANCHOR_STOP = ["到岗", "每周", "天/周", "一周", "学历", "应届", "薪资", "工资",
               "实习时长", "个月", "转正", "五险一金", "上班时间", "工作地点",
               "投递", "简历投递", "办公地点", "出差", "加班"]


def _jd_anchor(job, idx=0):
    """idx=0 要最贴 JD 的那句；跟进句传 idx=1、2…，要一个【没用过的】新锚点。"""
    jd = job.get("jd_text") or ""
    sents = [s.strip() for s in re.split(r"[\n。；!！?？]+", jd) if len(s.strip()) >= 5]
    if not sents:
        return None
    keep = [s for s in sents if not any(w in s for w in ANCHOR_STOP)]
    if keep:
        sents = keep          # 全被过滤掉（JD 只写了入职条件）才退回原样
    if idx >= len(sents):
        return None
    # 按"跟 JD 关键词对得上多少"排；全都没对上时退化成按句子长度排（跟原来一样）
    if any(_score_sent(s) > 0 for s in sents):
        ranked = sorted(sents, key=lambda s: -_score_sent(s))
    else:
        ranked = sorted(sents, key=lambda s: -len(s))
    return _clean_phrase(ranked[idx])


# ---------- 从简历里挑一条经历当证据 ----------

def _ranked_anchors(job):
    """按"跟这条 JD 对得上"的程度排序候选经历。第一条最合适。"""
    jd = (job.get("jd_text") or "") + " " + (job.get("title") or "")
    scored = []
    for k, a in ANCHORS.items():
        if k == "psychology":
            continue
        # ★ 2026-11-10 晚：DM 匹配 / 反馈闭环已并入 project_rag，不再作为独立经历命中。
        if k in MERGED_INTO_RAG:
            continue
        # 有些"证明点"几乎是每份 JD 都会顺手写一句的套话
        # （比如"抗压能力强"），拿它们当命中，结果就是给 FDE 工程师配<经历A>经历。
        # 这些词一律不算证据。
        hit = sum(1 for p in (a.get("proves") or [])
                  if p in jd and p not in WEAK_PROVES)
        if hit:
            if k in TECH_ANCHORS:
                hit += TECH_BOOST
            scored.append((hit, k, a))
    scored.sort(key=lambda x: (-x[0], len(x[2]["text"])))

    # ★ 2026-11-01 本人拍板：如果没有任何一个 project_* 项目锚点真正命中 JD
    #   （hit>0），说明这条 JD 没对上你的技术项目（弱命中/无命中），
    #   走杀手锏顺序（剧本杀项目→心理学→实习），不要走 theme 白名单硬塞 Coze 工作流。
    _has_project_hit = any(k.startswith("project_") and h > 0
                           for h, k, _a in scored)
    _set_fallback(not _has_project_hit)

    # ★ 2026-11-01 修 bug：_CUR_FORCED_FID 是模块级变量，只在「命中强制触发」时才被
    #   _set_forced_anchor 赋值，没命中时不会自动清零。上一个岗位命中 RAG/闭环触发词
    #   留下的值会污染下一个岗位，导致 _lay_out._pick_priority 把上一个岗位的模块
    #   误判成「本条被强制」→ 顶到最前，挤掉心理学。每次进 _ranked_anchors 都先清零。
    _set_forced_anchor(None)


    # JD 明确在找"同理心/洞察"这套东西时，心理学是最强的一张牌。
    # 但门槛要卡严：几乎所有 AI 岗 JD 都会顺口带一句"用户洞察"，
    # 门槛松了会导致每条消息都拿心理学当证据 —— 同一批发出去的话长得一模一样。
    # ★ 2026-11-01 本人拍板「拓宽心理学触发」：把「洞察/用户体验/用户需求/用户研究」
    #   这类用户侧词也纳入，但分层 —— 强词（同理心/共情）命中即触发；
    #   弱词（洞察/用户体验等）需命中 2 个以上才触发，避免「顺口带一句」就全变心理学。
    strong_empathy = any(w in jd for w in ["同理心", "共情", "共情力"])
    strong_words = ["同理心", "共情", "共情力"]
    weak_words = ["用户洞察", "洞察", "用户体验", "用户需求", "用户研究", "诉求", "用户访谈"]
    strong_hit = sum(1 for w in strong_words if w in jd)
    weak_hit = sum(1 for w in weak_words if w in jd)
    if (strong_empathy or strong_hit >= 3 or weak_hit >= 2) and "psychology" in ANCHORS:
        scored.insert(0, (99, "psychology", ANCHORS["psychology"]))

    # ★★ 2026-10-29 晚：本人"简历铁律"在招呼语这边的落地。
    #   铁律：JD 里出现与 RAG 剧本杀项目相关的要求（RAG、知识库、向量检索、LLM 应用、
    #   多轮对话、剧本/内容生成等），就必须把该项目写进对应位置。
    #
    #   为什么以前一直是 0 条：project_rag 的 proves 是"RAG/需求拆解/评测意识/工程健壮性"，
    #   这些词 JD 里几乎不会原样出现（实测某两家公司命中数都是 0），
    #   于是它掉进下面的"兜底池"被打乱轮换，从此再没被选中过。
    #   现在改成看【这类要求有没有被提到】，命中就强制插到第一位。
    #   ⚠ 必须放在心理学之后插：心理学那档是 99 分插在最前，RAG 这条要压在它前面。
    _ff = SPEC.get("anchor_pick", {}).get("force_first_when") or {}
    _fid = _ff.get("anchor")
    _forced_fid = _fid          # 默认还是 project_rag（兼容旧配置）
    _rag_forced = False
    _triggers = [t for t in (_ff.get("jd_triggers") or []) if t]
    _hit_trigger = [t for t in _triggers if t in jd]
    if _fid in ANCHORS and _hit_trigger:
        # ★ 2026-11-10 晚 本人拍板（合并）：命中触发词后固定提 project_rag。
        #   原来这里还会在三个模块（rag/dm_match/review_loop）里按命中词选一个
        #   模块提最前；现在三个模块已合并成 project_rag 一个，具体选哪块
        #   （RAG/知识库/评测/DM匹配/反馈闭环）由 _pick_block 统一按命中词挑，
        #   这里只负责把剧本杀项目本身顶到第一位。
        _forced_fid = _fid
        scored = [(h, k, a) for h, k, a in scored if k != _forced_fid]
        scored.insert(0, (999, _forced_fid, ANCHORS[_forced_fid]))
        _rag_forced = True
        _set_forced_anchor(_forced_fid)

    # ★ 2026-11-10 本人拍板（改）：剧本杀门店项目（project_rag 门店助手）【无条件】
    #   排在经历第一位，不再依赖「JD 命中 RAG 词」这个前提。
    #   之前只在命中触发词时插队，没命中时 project_rag 掉进兜底池按固定顺序排，
    #   结果被 review_loop / dm_match 或别的项目挤下去，GitHub 句也跟着挂错地方。
    #   现在无论 JD 是什么，project_rag 都顶到经历第一，GitHub 句固定跟它走。
    if "project_rag" in ANCHORS:
        scored = [(h, k, a) for h, k, a in scored if k != "project_rag"]
        scored.insert(0, (999, "project_rag", ANCHORS["project_rag"]))
        _set_forced_anchor("project_rag")

    # 兜底：把剩下没命中 JD 的经历按【由短到长】接在后面。
    # 这样就算最合适的那条太长塞不下，也能退而求其次用一句短的，
    # 而不是把句子截成半截 —— 概率不为零，实测就有岗位走到这个分支。
    # 兜底：有些 JD 一条对得上的经历都没有（比如纯运营岗对不上你的项目）。
    #   这时候按【由短到长】排，保证至少能挑出一句塞得下的。
    #   排序再用 job_id 打个乱稳定错开 —— 否则同一批里所有这种岗都会撞同一句。
    used_keys = {k for _, k, _ in scored}
    rest = [(0, k, a) for k, a in ANCHORS.items() if k not in used_keys]
    # ★ 2026-11-01 本人拍板：JD 一条都对应不上的时候，不再「由短到长乱挑」，
    #   而是固定按杀手锏顺序排：剧本杀项目 → 心理学 → 实习项目。
    #   因为这三样是本人最有分量的牌，没得选时就按这个顺序往外打。
    #   剧本杀项目 = 三模块（rag / dm_match / review_loop），内部仍按 text 由短到长
    #   （短的好塞进字数预算，长的作为「升级」候选）。
    # ★ 2026-11-10 晚 本人拍板（合并）：DM 匹配 / 反馈闭环已并入 project_rag，
    #   不再是独立项目，从兜底顺序里拿掉。剧本杀项目 = project_rag 一个模块，
    #   内部的具体块（BM25/DM匹配/反馈闭环）由 _pick_block 按 JD 命中挑。
    order = ["project_rag", "psychology", "project_intern"]
    # 三模块内部由短到长，其余保持配置顺序，最后兜底按原「tech 优先+由短到长」。
    def _fallback_rank(x):
        k = x[1]
        if k in order:
            return (0, order.index(k), 0)
        if k in TECH_ANCHORS:
            return (1, 0, len(x[2]["text"]))
        return (2, 0, len(x[2]["text"]))
    # ★ 2026-11-10 接 embedding（建议 5，两层都接）：
    #   兜底池里 hit=0 的经历，关键词一条没对上，但可能语义上沾边。
    #   比如音频评测岗：JD 写「音质评价」，project_rag 的「评测意识」块语义最贴，
    #   纯关键词却对不上 → 它被丢进兜底池按固定顺序排，永远轮不到。
    #   这里给「有 blocks 且最贴块的语义相似度够高」的经历加分，让它排前面。
    if _BLOCK_ENABLED and _EMBED_ENABLED:
        def _fallback_rank(x):
            h, k, a = x
            if k in order:
                base = (0, order.index(k), 0)
            elif k in TECH_ANCHORS:
                base = (1, 0, len(a["text"]))
            else:
                base = (2, 0, len(a["text"]))
            # 有 blocks 的经历：算「最贴块的 skill 与 JD 的语义相似度」，
            # 相似度够高（≥0.4）就把它排到同组最前（用负值压过同组其它）。
            bonus = 0.0
            for b in (a.get("blocks") or []):
                sk = (b.get("skill") or "").strip()
                if sk:
                    bonus = max(bonus, _sem_sim(sk, jd))
            if bonus >= 0.4:
                base = (base[0], base[1], -bonus)
            return base
    rest.sort(key=_fallback_rank)
    scored.extend(rest)

    # ★★ 2026-10-29 晚补：这才是"强制插队"当时没生效的真正原因。
    #   project_rag 确实排到第 1 了，但 _evidence_for 是按
    #   ability_evidence_order 白名单【逐条往下试】的：
    #     "ai"   主题 → workflow, intern, RAG, review_loop...
    #     "data" 主题 → bizsim, cap_data, RAG, dm_match...
    #   排在 RAG 前面的项目会把这条主题先吃掉，于是每个主题都轮不到 RAG
    #   —— 排第 1 名等于没排。
    #   修法：命中铁律时，把 RAG 提到【每个含它的主题】白名单的第一位，
    #   其余顺序原样不动（只提升优先级，不改变"谁能配哪条能力"）。
    if _rag_forced:
        for _th, _lst in list((ABIL_EVID or {}).items()):
            if _forced_fid in _lst:
                ABIL_EVID[_th] = [_forced_fid] + [k for k in _lst if k != _forced_fid]

    return scored


def hash_offset(job):
    """同一个岗位每次都得到同一个值（保证可复现），不同岗位各不一样（保证不撞车）。"""
    import hashlib
    seed = str(job.get("job_id") or job.get("title") or "")
    return int(hashlib.md5(seed.encode("utf-8")).hexdigest(), 16)


def _fit_evidence(text, budget):
    """
    把一条经历压到 budget 字以内 —— 但只用"整句丢弃"的方式，绝不截半个词。
    实在塞不下就返回 None，让上层换一条更短的经历。原因：
    把 "BM25 0.6 + Emb…" 这种截一半的句子发给 HR，比少说一句更难看。
    """
    if len(text) <= budget:
        return text
    # ⚠ 只在逗号/分号处切。顿号后面是并列的词（"知识缺失、检索偏差"），
    #   从那儿断开会劈出「将失败案例按知识缺失」这种半句。
    parts = [p.strip() for p in re.split(r"[，,；;]", text) if p.strip()]
    if not parts:
        return None
    # 先试着整句拼回去（不够就一次次丢掉最后一个分句）
    while len(parts) > 1:
        joined = "，".join(parts)
        if len(joined) <= budget:
            return joined
        parts.pop()
    # 只剩一个分句还超预算时：优先挑【含数字】的那一段塞进去。
    # 数字是这条话里最硬的东西，宁可砍掉周围的修饰也别丢掉数字。
    # ⚠ 2026-10-28 修了两个错：
    #   ① 排序键原来是 (1 if 含数字 else 0)，把带数字的排到了最后，跟注释正好相反，
    #      结果挑出来的是「降级方案齐备」这种没数字的碎片；
    #   ② 最后一步原来是"硬切到 budget 个字"，切出过「一致性校」「自建 200 道模拟评」
    #      这种把词劈成两半的句子 —— 比少说一句更难看。
    #   现在塞不下就返回 None，让上层换一条更短的经历。
    allp = [p.strip() for p in re.split(r"[，,；;]", text)
            if len(p.strip()) >= 6]
    for p in sorted(allp, key=lambda x: (0 if re.search(r"\d", x) else 1, -len(x))):
        if len(p) <= budget:
            return p
    return None


def _nums(s):
    """取出句子里所有的数字（含小数），用来判断"升级成更长的说法"有没有把数字弄丢。"""
    return set(re.findall(r"\d+(?:\.\d+)?", s or ""))


def _grow_evidence(text, budget):
    """想"换更完整的说法"时专用 —— 只能整句丢，绝不挑单个分句。

    为什么不让用 _fit_evidence：_fit_evidence 在塞不下时会挑【含数字的那个分句】
    硬塞进去。那一步会把项目名（主语）丢掉
    —— 实测切出过「验收要求独立章节展开才算有效证据、20 轮硬顶」，
    读起来是"我做过验收要求……"，主语没了，比短版难看得多。
    本人 SOP 第 3 条要求每条是「经历＋量化数据＋对应能力」，
    丢掉"经历"这个成分等于违反 SOP。所以这里放不下就返回 None，
    让上层保留原来那句完整的短版。
    """
    if len(text) <= budget:
        return text
    parts = [p.strip() for p in re.split(r"[，,；;]", text) if p.strip()]
    if not parts:
        return None
    # 一次次丢分句，直到拼回去能塞下。
    # ⚠ 丢的时候优先丢【不含数字】的分句 —— 数字是这条话里最硬的东西，
    #   本人 SOP 第一条就是"能用数字绝不写有相关经验"。
    #   以前是从尾部一路丢，实测把"200 道评估集、7/7 通过"丢光、只剩技术参数。
    while len(parts) > 1:
        joined = "，".join(parts)
        if len(joined) <= budget:
            return joined
        idx = None
        for i in range(len(parts) - 1, 0, -1):
            if not re.search(r"\d", parts[i]):
                idx = i
                break
        parts.pop(idx if idx is not None else -1)
    return None




# ---------- 相似度 ----------

def _bigrams(text):
    return {text[i:i + 2] for i in range(len(text) - 1)}


def _max_similarity(text, recent):
    worst = 0.0
    for r in recent or []:
        b, rb = _bigrams(text), _bigrams(r)
        union = b | rb
        if not union:
            continue
        worst = max(worst, len(b & rb) / len(union))
    return worst


def load_recent(limit=20):
    path = path_of(SPEC["ledger"]["file"])
    if not os.path.exists(path):
        return []
    rows = []
    with open(path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            t = (r.get("greeting_text") or "").strip()
            if t:
                rows.append(t)
    return rows[-limit:]


# ---------- 称呼与结尾：都按这条岗位的情况来定，不轮换 ----------

def salutation_for(job):
    """本人 2026-10-29 SOP 第 1 条：
       男性 → 姓 + 总；女性 → 姓 + 姐姐；后接"您好"。
       拿不到名字 / 不是人名 → "您好"（不猜，猜错比不用更糟）。

    例：『刘女士』→ 刘姐姐您好；『张先生』→ 张总您好；『牛旭阳』→ 牛总您好。
    """
    name = (job.get("boss_name") or "").strip()
    suffix = SALUT.get("salutation_suffix") or "您好"
    if not name:
        return SALUT["default"]
    # ⚠ 有的"姓名"根本不是人名（真例：「呼中心」）。拿这种取首字会生成"呼总"，当场尴尬。
    #   名字里带这些词的，或明显过长的，一律回落"您好"。
    if any(w in name for w in ("中心", "团队", "招聘", "人事", "人力", "老师",
                               "HR", "hr", "部", "组", "专员")):
        return SALUT["default"]
    if len(name) > 4:
        return SALUT["default"]
    if "女士" in name:
        return name[0] + "姐姐" + suffix
    if "先生" in name and len(name) >= 3:
        return name[0] + "总" + suffix
    if 2 <= len(name) <= 4 and not any(c in name for c in ("女", "先", "士", "生")):
        return name[0] + "总" + suffix
    return SALUT["default"]


_RECENT_CLOSINGS = []   # 最近几条用过的结尾（只在本次运行内记）

def _closing_candidates(job):
    """按这条 JD 说了什么，给候选结尾排优先级（不轮换）。

    返回一串 id，第一个最贴这条岗位，后面的依次备用。
    """
    blob = " ".join([
        job.get("title") or "",
        job.get("jd_text") or "",
        " ".join(job.get("job_labels") or []),
    ])
    hits = [r["id"] for r in PICK_RULES
            if any(k and k in blob for k in (r.get("when_any") or []))]
    return hits or ["Z"]


def pick_closing(job, window=3):
    """按这条 JD 说了什么挑结尾，命中第一条就用它。

    加了一道闸：最近 3 条里已经用过的结尾，这一条先跳过，换下一个同样对得上号的。
    为什么：不设这道闸的话，97 条里有 61 条会被判成 D（AI 岗的 JD 几乎都带
    「Agent/工作流」这类词），结果就是几十家收到同一句话 —— 那跟群发没区别。
    顺延不是随机轮换：用哪个仍由这条岗位说了什么决定，只是不让一个变体连着刷屏。
    """
    cands = _closing_candidates(job)
    recent = set(_RECENT_CLOSINGS[-window:])
    for cid in cands:
        if cid not in recent:
            _RECENT_CLOSINGS.append(cid)
            return cid
    # 命中的都在最近用过 → 挑命中里最靠前的那个（实在没得挑才用 Z）
    cid = cands[0]
    _RECENT_CLOSINGS.append(cid)
    return cid


# ---------- 能力型补充（2026-10-28 本人放开：允许写"掌握/熟悉某项技术"） ----------

CAPS = SPEC.get("capability_pool", {})
CAP_ITEMS = [c for c in CAPS.get("items", [])
             if c.get("resume_backing") and not c.get("need_confirm")]
_CAP_NUM = re.compile(r"[0-9０-９%％]")     # 能力句里不许出现数字
_CAP_TIME = re.compile(r"20\d{2}|年\s*\d{1,2}\s*月|\d+\s*(个月|年|天)")


def _cap_safe(text):
    """能力句里一旦出现数字、时间、项目名/公司名 → 判编造。"""
    if not text:
        return True
    if _CAP_NUM.search(text) or _CAP_TIME.search(text):
        return False
    for a in ANCHORS.values():
        for frag in re.split(r"[，,、（）()]", a["text"]):
            f = frag.strip()
            if len(f) >= 4 and f in text:
                return False             # 把项目名/公司名混进能力句了
    return True


# ---------- 生成一条 ----------

_NUM = re.compile(r"[0-9０-９]")


def _kw_scores(job):
    """扫一遍 JD，给里头出现的每个关键词算【重视度】。

    重视度 = 基础分 × 出现次数 + 位置分
      - 基础分：需求拆解/同理心这类强信号 3 分，AI/大模型这类 2~3 分，模型这种泛词 1 分；
      - 出现次数：同一个词反复出现，说明对方真的很在意；
      - 位置分：出现在 JD 越靠前的位置，分越高（第一个词拿满 3 分，末尾接近 0）。
    """
    jd = " ".join([job.get("title") or "", job.get("jd_text") or "",
                   " ".join(job.get("job_labels") or [])])
    n = max(1, len(jd))
    scored = []
    for theme, kwmap in ABILITY_THEMES.items():
        for w, base in (kwmap or {}).items():
            c = jd.count(w)
            if c:
                scored.append((base * c + 3 * (1 - jd.index(w) / n), w, theme))
    scored.sort(key=lambda x: -x[0])
    return scored


def rank_jd_points(job, top=3):
    """先判断这条 JD【最重视哪三条能力】，按重视度从高到低返回。

    返回 [(能力名, 主题, 重视度分), ...]

    为什么同一主题只取一条：
      JD 里"AI、大模型、Agent"往往是一句话里连着写的，那其实是同一件事。
      算成三条，招呼语就会变成同一个意思换个词说三遍。
    """
    scored = _kw_scores(job)
    out, themes = [], set()
    for s, w, t in scored:
        if t in themes:
            continue
        themes.add(t)
        out.append((ABILITY_NAMES.get(w, w), t, round(s, 2)))
        if len(out) >= top:
            break
    # JD 只讲了一件事（主题不够凑数）→ 按分补同主题里排名靠后的词
    if len(out) < top:
        names = {o[0] for o in out}
        for s, w, t in scored:
            name = ABILITY_NAMES.get(w, w)
            if name in names:
                continue
            names.add(name)
            out.append((name, t, round(s, 2)))
            if len(out) >= top:
                break
    return out


def decompose_jd(job):
    """拆 JD（三层策略第一层）：显性关键词 + 隐含要求/硬约束。

    返回 {
      "explicit_keywords": [JD 原词...],   # 关键词对齐：驱动锚点选择 + 校验话术有没有对上
      "risks": [注意点...],                # 隐含要求/硬约束 → 风险&注意点（不进话术）
      "risk_ids": [命中的约束 id...],
    }

    两层各管什么（本人 2026-10-29 定）：
      - 显性关键词（表层命中）：JD 专业名词原样复用，让 HR 3 秒扫到对口词。
      - 风险&注意点（深层约束）：实习时长/转正/出勤/编程/学历/年限，
        这些读出来但【绝不写进话术】—— 把"我们可能不匹配"写进招呼语是自杀。
    """
    jd_full = " ".join([job.get("title") or "", job.get("jd_text") or ""])
    jd_req = " ".join([job.get("title") or "", _requirements_text(job)])
    dec = SPEC.get("jd_decompose") or {}

    # ① 显性关键词（JD 原词）：扫全文，技术词可能出现在职责或公司介绍里
    explicit = []
    ek = dec.get("explicit_keywords") or {}
    if isinstance(ek, dict):
        for group in ek.values():
            for w in group or []:
                if w and w in jd_full and w not in explicit:
                    explicit.append(w)
    else:
        for w in ek or []:
            if w and w in jd_full and w not in explicit:
                explicit.append(w)

    # ② 隐含要求 + 硬约束 → 风险&注意点：只扫"任职要求"段，
    #    别把公司介绍里的"创始人剑桥大学硕士"误当成对候选人的学历要求。
    risks, risk_ids = [], []
    for hc in dec.get("hard_constraints") or []:
        for kw in hc.get("keywords") or []:
            if kw and kw in jd_req:
                risks.append((hc.get("note") or "").replace("{match}", kw))
                risk_ids.append(hc.get("id") or "")
                break

    return {"explicit_keywords": explicit, "risks": risks, "risk_ids": risk_ids}


def _requirements_text(job):
    """抽 JD 里"任职要求"那段。隐含要求/硬约束只看这段，不含公司介绍。

    分两步切：
      ① 切到"任职要求/职位要求…"标签之后（前面是岗位职责，不算约束）；
      ② 再从要求段里切掉后面冒出来的公司介绍/福利/联系方式等杂讯
         —— 否则"创始人剑桥大学硕士"会被误当成对候选人的学历要求。
    """
    jd = job.get("jd_text") or ""
    for lb in ("任职要求", "职位要求", "岗位要求", "任职资格", "资格要求", "任职条件"):
        i = jd.find(lb)
        if i >= 0:
            jd = jd[i:]
            break
    for m in ("公司介绍", "【公司", "工作地点", "工作地址", "关于我们", "我们提供",
              "加入我们", "官网", "AppStore", "粉丝", "薪资待遇", "团队介绍",
              "企业文化", "联系方式"):
        j = jd.find(m)
        if j >= 0:
            jd = jd[:j]
            break
    return jd


# ── 2026-11-10 经历切块（本人 A 偏 B 折中方案）──
#   把「整段经历」拆成「项目名 + 多个能力块」，每个块是职责+数据+技能三件套。
#   匹配时按 JD 命中挑最贴的那个块，不再整坨塞。
#   开关：BOSS_BLOCK=on/off 环境变量 优先于 yaml 的 blocks.enabled。
#   默认【关】——关掉后完全走老的 text/short 逻辑，行为跟切块之前一模一样，
#   这是「效果不好随时退回」的保险。本人确认效果后再打开。
_BLOCK_CFG = SPEC.get("blocks") or {}
_BLOCK_ENABLED = False
_env_block = os.environ.get("BOSS_BLOCK", "").strip().lower()
if _env_block in ("on", "1", "true"):
    _BLOCK_ENABLED = True
elif _env_block in ("off", "0", "false"):
    _BLOCK_ENABLED = False
elif _BLOCK_CFG.get("enabled"):
    _BLOCK_ENABLED = True


# embedding 辅助排序开关（调研建议 5）。默认【关】——每条岗位多 1 次 jd 冷调用（约 0.7s），
# 会拖慢日常投递。确认效果后本人用 BOSS_EMBED=on 打开。退回：BOSS_EMBED=off。
#   打开时：没命中时用语义相似度挑最沾边的块（真 embedding 余弦 + n-gram 兜底）。
_EMBED_ENABLED = False
_env_embed = os.environ.get("BOSS_EMBED", "").strip().lower()
if _env_embed in ("on", "1", "true"):
    _EMBED_ENABLED = True
elif _env_embed in ("off", "0", "false"):
    _EMBED_ENABLED = False


def _sem_sim(short, long):
    """两段文本的语义相似度 0~1。真 embedding（余弦）+ n-gram 兜底，见 embed_client。

    short 通常是经历块的 skill 词（2~6 字），long 是 JD（几百字）。
    语义匹配补的是「同义盲区」：JD 写「音质评价」、skill 写「评测意识」，
    关键词对不上，但语义上说的是同一件事。
    """
    try:
        from embed_client import sim as _esim
        return _esim(short, long)
    except Exception:
        # embed_client 都 import 不进（极端情况）→ 退回纯字面「子串命中」
        return 1.0 if short and short in long else 0.0


def _pick_block(job, a):
    """从一条经历的 blocks 里挑最贴这条 JD 的那一块，拼成「项目名 + 职责 + 数据」一句。

    匹配分两档：
      ① 硬命中：块的 skill 词命中 JD（跟 proves 命中同口径），命中数最多的块排最前；
      ② 都没命中（同义改写场景）→ 用 n-gram 语义相似度算「哪个块 skill 最沾边」，
         挑相似度最高的那块。这是「embedding 辅助排序」（调研建议 5）的落地，
         弥补纯关键词的「同义盲区」。
    兜底：连语义都挑不出（无 blocks）→ 返回 None，交给上层走老 text/short 逻辑。
    返回一句拼好的话（不含项目名前缀，前缀由调用方决定）。
    """
    blocks = a.get("blocks") or []
    if not blocks:
        return None
    jd = (job.get("jd_text") or "") + " " + (job.get("title") or "")
    best, best_hit = None, -1
    for b in blocks:
        skill = (b.get("skill") or "").strip()
        hit = 0
        if skill:
            # skill 可能带「/」或「、」拆多个词，任一命中都算这个块命中
            for w in re.split(r"[/、]", skill):
                w = w.strip()
                if w and w in jd and w not in WEAK_PROVES:
                    hit += 1
        if hit > best_hit:
            best_hit = hit
            best = b
    # ① 有硬命中 → 直接用命中最多的块（老逻辑，不动）
    if best is not None and best_hit > 0:
        best = best
    else:
        # ② 都没命中 → embedding 辅助：按 skill 语义相似度挑最沾边的块
        if _EMBED_ENABLED:
            best = None
            best_sim = 0.0
            for b in blocks:
                skill = (b.get("skill") or "").strip()
                sim = _sem_sim(skill, jd)
                if sim > best_sim:
                    best_sim = sim
                    best = b
        if best is None:
            best = blocks[0]
    duty = (best.get("duty") or "").strip()
    data = (best.get("data") or "").strip()
    # 职责 + 数据拼成一句（数据是量化结果，接在职责后面）
    if duty and data:
        return f"{duty}，{data}"
    return duty or data or None


def _all_evidence(job, anchor_idx=0):
    """把手上所有能拿出来证明的事列成一张表：经历（9 条）+ 能力（6 条），按 id 索引。

    anchor_idx 只换经历的起点（第一条给最重要的能力用），能力句不参与轮换
    —— 能力句是"我会什么"，跟"先讲哪个"没关系。
    """
    out = {}
    ranked = _ranked_anchors(job)
    if anchor_idx:
        ranked = ranked[anchor_idx:] + ranked[:anchor_idx]
    # ★ 2026-11-01 本人拍板（改）：心理学从「固定第二段」改回「诉求洞察能力点的证据」。
    #   软技能岗（销售/洞察/沟通/需求拆解）时，把 short 直接指到完整版 text（带
    #   好评率 90% / 复购率 50%），让最强的两个数字上场；非软技能岗维持短版省字数。
    #   _soft_mode 只认 JD 原文里的软技能触发词，跟能力点排序互不依赖，安全。
    _soft = _soft_mode(job)
    github_done = False  # ★ 2026-11-05 修：GitHub 句只拼一次（三个模块锚点共用同一个作品集地址）
    for _h, key, a in ranked:
        # ★ 2026-10-29 晚：RAG 那句"已上传到 GitHub，并配了可访问的作品集"
        #   在这里就拼进【长版和短版】，不要在 _lay_out 里事后追加。
        #   原因：_grow_evidence 可能把正文换成 short 短版，短版没有那句 → 漏掉。
        #   只对 yaml 里真挂了 github 链接的锚点生效（现在只有 project_rag），
        #   项目②③没作品集地址，硬说"已上传 GitHub"等于张冠李戴。
        # ★ 2026-11-05 修：三个模块锚点（rag/dm_match/review_loop）都挂了同一个
        #   github 地址，同时被选中时之前每个都拼一次 → GitHub 句重复。
        #   现在只在第一个带 github 的锚点上拼一次。
        # ★ 2026-11-10 本人拍板（改）：GitHub 句【固定】挂在 project_rag（门店助手）
        #   这个主体锚点上，不再「谁排第一就挂谁」。之前 review_loop 排第一时，
        #   GitHub 句被挂到「反馈闭环」后面（子模块带了整个项目的开源声明，读着怪）。
        #   现在只认 project_rag，其他模块即使排前面也不抢这句。
        _txt, _sht = a["text"], (a.get("short") or a["text"])
        # ★ 2026-11-10 晚 本人拍板（改）：实习经历（project_intern）不管切块开关，
        #   都不用项目名前缀『<实习公司> AI 产品经理实习』，统一用 yaml 里配的
        #   block_lead 固定引导句「也在实习中做过」——
        #   省字数 + 避免跟开头固定句「曾于<实习公司>AI产品经理岗位实习」重复公司名。
        #   这里把 text/short 开头那句「『…实习』：」替换成「也在实习中做过：」，
        #   后续 _pick_block（切块开）或 _phrases_of/_grow_evidence（切块关）
        #   自然就从引导句后面取内容了。
        _lead = (a.get("block_lead") or "").strip()
        if _lead:
            for _v, _s in (("text", _txt), ("short", _sht)):
                _n = re.sub(r"^(『[^』]*』)\s*[:：]", _lead + "：", _s or "")
                if _v == "text":
                    _txt = _n
                else:
                    _sht = _n
        # ★ 2026-11-10 经历切块：开关开 + 这条经历有 blocks 时，按 JD 挑最贴的块，
        #   用「项目名：职责，数据」替换整段 text/short，实现精准命中。
        #   挑不出块（无 blocks / 开关关）→ 走下面老逻辑，行为不变（退回保险）。
        if _BLOCK_ENABLED:
            _blk = _pick_block(job, a)
            if _blk:
                # ★ 2026-11-10 晚 本人拍板：项目名前缀优先用【短版】（short 里的『...』），
                #   短版项目名更简洁（如『剧本杀门店 AI 助手』），完整版太长读着累。
                #   提取顺序：short 开头有『...』就用 short 的，否则退回 text 的。
                #   （有 block_lead 的锚点，前缀已在上面换成固定引导句，这里跳过项目名提取）
                _prefix = ""
                if _lead:
                    # ★ 2026-11-10 晚 本人拍板：实习经历切块时，引导句（block_lead）
                    #   要跟着每一块走，不是只挂在整段上。之前这里 _lead 存在时
                    #   _prefix 被置空 → 块正文直接裸着拼，引导句丢了，HR 分不清
                    #   这是实习经历还是剧本杀项目。现在把引导句当项目名前缀用，
                    #   拼成「也在『AI 产品经理实习』中做过：自建评估集…」。
                    _prefix = _lead
                else:
                    for _src in (a.get("short"), a.get("text")):
                        _m = re.match(r"^(『[^』]*』)\s*[:：]", _src or "")
                        if _m:
                            _prefix = _m.group(1)
                            break
                _blk_txt = f"{_prefix}：{_blk}" if _prefix else _blk
                _txt = _blk_txt
                _sht = _blk_txt
        if RAG_ON and RAG_TEXT and key == "project_rag" and not github_done:
            _rt = _evp_rag_text()
            if _rt and _rt not in _txt:
                _txt = _txt + "，" + _rt
            if _rt and _rt not in _sht:
                _sht = _sht + "，" + _rt
            github_done = True
        # ★ 软技能岗：心理学这条用完整版（standard），带全 90% / 50% 数据。
        if _soft and key == "psychology" and a.get("text"):
            _sht = a["text"]
        out[key] = {"id": key, "kind": "anchor",
                    "text": _txt, "short": _sht,
                    "num": 1 if _NUM.search(a["text"]) else 0}
    if CAPS.get("enabled"):
        for c in CAP_ITEMS:
            key = "cap_" + c["id"]
            out[key] = {"id": key, "kind": "cap",
                        "text": c["text"], "short": c.get("short") or c["text"],
                        "num": 1 if _NUM.search(c["text"]) else 0}
    return out


def _phrases_of(e, budget, hint=""):
    """从一条证据里挑一句塞得下的【完整】说法。

    优先顺序：① 现成的短版（人已经压缩过）；② 含数字的分句；③ 跟能力名说同一件事的分句。
    ⚠ 只在逗号/分号处收尾。顿号后面跟的是并列的词（"知识缺失、检索偏差"），
      从那儿断开会劈出「将失败案例按知识缺失」这种半句。
    """
    if len(e["short"]) <= budget:
        return e["short"]
    pool = []
    for src in (e["text"], e["short"]):
        for p in re.split(r"[，,；;]", src):
            p = p.strip()
            if len(p) >= 6:
                pool.append(p)
    seen, uniq = set(), []
    for c in pool:
        if c and c not in seen:
            seen.add(c)
            uniq.append(c)
    fits = [c for c in uniq if len(c) <= budget]
    if not fits:
        return None
    hint_words = [w for w in re.split(r"[\s、/]", hint or "") if len(w) >= 2]

    def key(x):
        return (0 if re.search(r"\d", x) else 1,
                0 if any(w in x for w in hint_words) else 1,
                -len(x))

    fits.sort(key=key)
    return fits[0]


def _evidence_for_rule(theme, ev, used, budget, hint="", block=()):
    """给某条能力挑一句能接住的证据，按 config 里排好的优先顺序找。

    block：这批证据本条不许用（软技能场景下<经历B> / <经历A>被 psychology 顶替）。

    为什么不直接在所有证据里挑"最长的/带数字的"：
      那样会出现「Agent 搭建」配到「能用 Python 和 SQL 取数」——
      字面好看，实际答非所问，HR 一眼看出是机器拼的。
    所以每条能力只能从【人排好的那几条】里挑，挑不到就整条不写。
    """
    # ★ 2026-11-01 本人拍板：JD 没有任何项目锚点命中（弱命中/无命中）时，
    #   不走 theme 白名单，直接按 _all_evidence 里排好的杀手锏顺序挑。
    #   否则会冒出「给销售岗硬塞 Coze 工作流」这种文不对题的结果。
    if _CUR_FALLBACK:
        # ★ 2026-11-01 本人拍板（改）：软技能主题（needs=诉求洞察 / user=用户洞察）
        #   在兜底模式下也要优先 psychology，不能让剧本杀三模块按杀手锏顺序把它挤掉。
        #   否则「诉求洞察」这条销售岗最核心的能力，会被 project_dm_match 顶替
        #   （心理学完整版带 90%/50% 是打销售岗最强的一张牌）。
        _soft_themes = {"needs", "user"}
        if theme in _soft_themes:
            e = ev.get("psychology")
            if e and e.get("kind") == "anchor" and "psychology" not in used \
                    and "psychology" not in block:
                body = _phrases_of(e, budget, hint)
                if body:
                    return body, e
        for key, e in ev.items():
            if e.get("kind") != "anchor" or key in used or key in block:
                continue
            if any(key in pair and (pair - {key}) & used for pair in MUTEX):
                continue
            body = _phrases_of(e, budget, hint)
            if body:
                return body, e
        return None, None

    for key in (ABIL_EVID.get(theme) or []):
        e = ev.get(key)
        if not e or key in used or key in block:
            continue
        # 跟已经用掉的那条说的是同一组数据 → 跳过（见 MUTEX 说明）
        if any(key in pair and (pair - {key}) & used for pair in MUTEX):
            continue
        body = _phrases_of(e, budget, hint)
        if body:
            return body, e
    # ★ 2026-11-01 本人拍板：JD 一条都对应不上的时候，theme 白名单也挑不中，
    #   就按 _all_evidence 里已经排好的杀手锏顺序（剧本杀项目 → 心理学 → 实习）
    #   往下挑。ev 是 dict，key 顺序正是 _ranked_anchors 的兜底顺序，
    #   所以这里直接按 ev 顺序遍历，即「杀手锏顺序」。
    for key, e in ev.items():
        if e.get("kind") != "anchor" or key in used or key in block:
            continue
        if any(key in pair and (pair - {key}) & used for pair in MUTEX):
            continue
        body = _phrases_of(e, budget, hint)
        if body:
            return body, e
    return None, None


def _evidence_for(theme, ev, used, budget, hint="", block=(), job=None):
    """挑证据的统一入口：开着模型就让模型选编号，其余情况全走规则版。

    ★ 2026-10-29 晚 本人拍板接入（配置 evidence_pick）。
      模型在这里只做「选」——从 _evidence_for_rule 会挑的那批候选里挑一个编号，
      正文仍由 _phrases_of 从档案原文生成，模型碰不到一个字。

    三层保险，任何一层不满足就静默退回规则版（绝不空着、绝不报错）：
      ① 开关关着 / 模型没接（backend=none）→ 直接规则版，零开销
      ② 候选只有一个 → 没什么可选的，不问模型（省一次调用）
      ③ 模型超时 / 乱回 / 抛异常 → 规则版，日志里能看到 ok=False
    """
    # ★ 2026-11-01 本人拍板：JD 没有任何项目锚点命中（弱命中/无命中）时，
    #   不走模型选编号，直接走规则版的杀手锏顺序。理由：没真正对上技术能力，
    #   让模型「选」也没意义，反而可能选偏；按人定好的杀手锏顺序最稳。
    if not _CUR_FALLBACK and _evidence_pick_on():
        try:
            from evidence_pick import pick_evidence
            body, e = pick_evidence(theme, ev, used, budget, hint, block,
                                    job=job, ability_name=hint)
            if body:
                return body, e
        except Exception:
            # 模块缺失、网络断、模型抽风 —— 一律不影响出稿，退回规则版
            pass
    return _evidence_for_rule(theme, ev, used, budget, hint, block)


def _psych_used_in_info(info):
    """心理学素材是否已经作为价值段被选中（若已选，固定第二段不再重复插）。"""
    return any((p.get("evid") or {}).get("id") == "psychology" for p in (info or []))


def _assemble(sal, intro, info, mid, closing, avail=""):
    """拼一整条招呼语。

    每条价值段现在是「我做过 + 项目名称 + 量化数据 + 核心动作 + 产出结果」，
    **前面不再挂「工作流搭建」这类能力标签**。

    为什么去掉标签：标签只是把 JD 里刚写过的"我们要什么"换个说法再念一遍。
    HR 比谁都清楚他要找什么样的人，重复一遍既占字又不产生新信息
    —— 他只知道"这人知道我们要什么"，还是不知道"这人做过什么"。

    去掉之后：省下的字数全给证据，而且直接报项目名，
    他自己会判断这个项目跟岗位对不对得上 —— 判断由他做出来的，比我们替他做出来的可信。

    每条的动词不一样：经历说"我做过"，能力说"我会用 / 我能"——
    顺着读是自然递进，不是硬凑的轮换。
    """
    # 本人 2026-10-29 SOP 第 3 条：每条证据 = 经历 ＋ 量化数据 ＋ **对应能力**
    # 对应能力挂在这条证据的末尾，让 HR 读完就能把它跟 JD 那条要求对上号。
    parts = []
    rag_opener_done = False  # ★ 剧本杀项目总起句只拼一次（本人规则：总起句绑整个项目，不是绑每个模块）
    for i, p in enumerate(info):
        pre = p["pre"] if "pre" in p else ("也做过" if INTRO_FLOW_ON else ("我做过" if i == 0 else "也做过"))
        body = p["body"]
        # ★ 2026-10-30 本人规则：剧本杀项目先写总起句「我做过剧本杀DM门店AI运营项目，
        #   且已应用到门店」，最后再接具体选中的能力。
        #   总起句自带「我做过」动词，所以这条的 pre 置空（不再叠「也做过」），
        #   否则会拼出「也做过我做过…」这种动词重复的病句。
        #   剧本杀项目是本人明确指定的唯一例外，允许它用「我做过」开头。
        #   2026-11-05 修：三个模块锚点（RAG/DM匹配/反馈闭环）同时命中时，
        #   之前每条都叠一次总起句 → 拼出重复段。现在只在第一个模块前拼一次。
        is_rag = bool(INTRO_FLOW_ON and INTRO_RAG_OPENER
                      and p["evid"]["id"] in INTRO_RAG_ANCHORS
                      and INTRO_RAG_OPENER not in body)
        if is_rag:
            pre = ""
            if not rag_opener_done:
                body = _evp_rag_opener() + "，" + body
                rag_opener_done = True
        # ★ 2026-11-10 晚 本人拍板：实习经历（project_intern）切块时用了固定引导句
        #   「也在实习中做过」，这句已经自带「也…做过」的动词，不能再叠「也做过」，
        #   否则会拼出「也做过也在实习中做过：…」这种动词重复的病句。
        _intern_anchor = ANCHORS.get(p["evid"]["id"]) or {}
        if (_intern_anchor.get("block_lead")
                and (p["body"] or "").startswith(_intern_anchor["block_lead"])):
            pre = ""
        if MUST_END_ABILITY:
            # ★ 2026-11-10 晚 本人修：cap 能力句（kind=="cap"）的 text 本身已经是
            #   "能把…整理成 PRD"这种完整的"对应能力"表达，末尾再叠一句
            #   ability_clause 就会重复（实测 PRD：cap_prd 的"能把调研到的信息整理成
            #   需求清单、优先级和 PRD" + ability_clause 的"能把调研结果整理成 PRD"
            #   → 一个"PRD"出现两次）。所以 cap 能力句不叠尾巴，只有经历锚点才叠。
            if p["evid"]["kind"] != "cap":
                clause = ability_clause(p.get("name"))
                if clause:
                    body = body + "，" + clause
        parts.append("%s%s。" % (pre, body))
    # ★ 2026-10-30 本人规则：学历句（intro）后固定接「曾于<实习公司>AI产品经理岗位实习」，
    #   这句加在 intro 之后、第一段经历之前。
    intern_part = ""
    if INTRO_FLOW_ON and INTRO_INTERN_LINE:
        intern_part = _evp_intern() + "。"
    # ★ 2026-11-01 本人拍板（改）：心理学从「固定第二段」改回「诉求洞察能力点的证据」，
    #   不再硬插固定句。它由 _lay_out 按 needs 主题正常选证据，软技能岗时带完整版
    #   （90% / 50%），排序靠「带数字往前」自动顶到最前。
    text = sal + intro + intern_part + "".join(parts) + mid + avail + closing
    # 整条话最后要收句号。SOP 给的固定结尾句本身不带句号，这里补上
    # （补的是标点，不动 SOP 规定的文字）。
    if text and not text.endswith(("。", "！", "？")):
        text += "。"
    return text


def _lay_out(plan, ev, room, soft=False, job=None):
    """按【重视度高→中→低】给三条能力各配一句证据。

    两条规矩：
      ① 要么说完整的（先上短版，放得下再换成更长的版本），要么整条不写 —— 绝不切半句；
      ② 总字数放不下，就从【最次要那条】开始整条丢掉。

    soft=True：本条岗位在要软技能 → <经历B> / <经历A>不许上（本人 2026-10-29 规则）。
    job：传给「挑证据」那一步用（模型选编号时要看岗位；铁律锁死也要看 JD）。
    """
    picks, used = [], set()
    block = tuple(SOFT_BLOCK_IDS) if soft else ()
    for name, theme, _s in plan:
        body, e = _evidence_for(theme, ev, used, room, hint=name, block=block,
                                job=job)
        if not body:
            continue
        # ★ 2026-11-05 修：GitHub 句统一在 _all_evidence 里拼（只拼一次），
        #   这里不再追加。之前这里也追加一次，导致两个模块锚点同时命中时
        #   _all_evidence 拼一个、这里又拼一个 → GitHub 句重复。
        #   _all_evidence 已经保证「带 github 的锚点，其 text 和 short 都拼好」，
        #   且整条招呼语里只出现一次，所以 _lay_out 拿到的 body 该带的都带了。
        used.add(e["id"])
        picks.append({"name": name, "theme": theme,
                      "evid": e, "body": body})

    # 第一条位置最值钱 —— HR 最先看到、看得最仔细。
    # 所以在"按 JD 重视度排序"之上再叠一层：带数字的真实经历往前排，
    # 相对顺序不变（Python 的 sort 是稳定的），只是把最硬的那条顶到第一位。
    # 装不下要丢时，也从队尾丢 —— 先丢软的能力句，保住硬证据。
    # ★ 2026-11-01 本人扩：命中「闭环/复核」这类词强制选了某个模块时（如
    #   project_review_loop），它必须排第一，不被「带数字」的 project_rag 挤下去
    #   —— 否则「按词选模块」等于白改（review_loop 不带数字，rag 带数字，老被挤掉）。
    def _pick_priority(p):
        if _CUR_FORCED_FID and p["evid"]["id"] == _CUR_FORCED_FID:
            return 0
        # ★ 2026-11-10 晚 本人拍板（改）：软技能岗心理学紧跟剧本杀之后排【第二】，
        #   不再排第一（返回 0）。因为剧本杀门店项目已「无条件排经历第一位」，
        #   软技能岗的心理学不该压过它，而是排它后面——两张牌都在最前，只是顺序对调。
        #   之前 psychology 返回 0 跟剧本杀并列，稳定排序下 psychology 先入 picks
        #   就排到了剧本杀前面（实测 6 条解决方案/BIP 岗心理学压过剧本杀）。
        if soft and p["evid"]["id"] == "psychology":
            return 1
        return 2 if re.search(r"\d", p["body"]) else 3

    picks.sort(key=_pick_priority)

    # 动词按最终顺序再定：经历说「我做过 / 也做过」，能力说「我… / 我也…」——
    # 能力句本身以「会用 / 能把 / 熟悉」开头，前面加个"我"才是人话
    # （说成"我做过会用 Figma"就别扭了）。
    for i, p in enumerate(picks):
        # ★ 有些锚点是"专业背景"而不是"一段经历"（比如心理学那条），
        #   yaml 里标了 say_as: ability → 前缀按能力句走（"我"而不是"我做过"），
        #   否则会拼出"我做过我系统学习过…"这种读不通的话（2026-10-29 实测踩到）。
        as_ability = (ANCHORS.get(p["evid"]["id"]) or {}).get("say_as") == "ability"
        is_cap = p["evid"]["kind"] == "cap" or as_ability
        if is_cap:
            p["pre"] = "我" if i == 0 else "我也"
        else:
            # ★ 2026-10-30 本人规则：学历句后已接「曾于…实习」这一句，
            #   所以后面所有经历段一律用「也做过」接续，第一条也不例外，
            #   不再出现「我做过」这种另起一句的写法。
            p["pre"] = "也做过" if INTRO_FLOW_ON else ("我做过" if i == 0 else "也做过")

    def total():
        # 每条 = 前缀 + 证据 + 「。」1 字 + （本人 SOP 2026-10-29）「，」+ 对应能力
        # ⚠ 2026-10-29 修：以前这里没算"对应能力"那句，导致预算少算 40~50 字，
        #   实际输出普遍 361~374 字、超出 360 上限（114 条里 45 条被拦）。
        s = 0
        for p in picks:
            s += len(p["pre"]) + len(p["body"]) + 1
            # ★ 2026-10-30 本人规则：剧本杀项目会额外加一句总起
            #   「我做过剧本杀DM门店AI运营项目，且已应用到门店」（在 _assemble 里加）。
            #   这里把它的长度也算进预算，否则实际输出会比预算多 20 多字，超字数门。
            if (INTRO_FLOW_ON and INTRO_RAG_OPENER
                    and p["evid"]["id"] in INTRO_RAG_ANCHORS
                    and INTRO_RAG_OPENER not in p["body"]):
                s += len(_evp_rag_opener()) + 1   # +1 是总起句后的逗号
            if MUST_END_ABILITY and p["evid"]["kind"] != "cap":
                c = ability_clause(p.get("name"))
                if c:
                    s += 1 + len(c)
        return s

    # ★ 2026-11-10 本人拍板：招呼语里证据（经历/能力）最少要 2 条，不能只剩 1 条。
    #   理由：只有 1 条证据的招呼语太单薄，像群发，撑不起"有料"的第一印象。
    #   做法：字数装不下时，丢到还剩 MIN_PICKS 条就停；2 条都放不下就判生成失败，
    #   （宁可这条不发，也不要发一条只剩 1 个证据的敷衍话术）。
    min_picks = int((SPEC.get("jd_points") or {}).get("min_points") or 2)
    while len(picks) > min_picks and total() > room:   # 装不下 → 丢最次要那条，但至少留 min_picks 条
        picks.pop()
    if not picks or len(picks) < min_picks:
        return []
    for p in picks:                          # 还有富余 → 换更完整的说法
        spare = room - total()
        if spare <= 2:
            break
        if p["body"].startswith(p["evid"].get("text") or "\0"):
            continue                         # 已经是长版全文，没有更长的了
        # ★★ 2026-10-29 晚（这是 GitHub 那句一直被弄丢的最后一道关）：
        #   带 github 的锚点【只能整版用】，不许拿 _grow_evidence 去截。
        #   原因：那句话钉在长版最末尾，任何"截到中途"的版本都没有它；
        #   而 _grow_evidence 只要返回一个更长的版本就会把带 GitHub 的短版换掉
        #   → 正文变成"长版前半截"，GitHub 和作品集一起消失（实测两家公司都这样）。
        #   整版用会多占字数，所以把这一步放到【其它锚点都长完】之后统一做，
        #   有富余就换成整版，没富余就保持短版 —— 两种都带 GitHub。
        if RAG_ON and RAG_TEXT and (ANCHORS.get(p["evid"]["id"]) or {}).get("github"):
            continue
        # ⚠ 用 _grow_evidence 而不是 _fit_evidence：后者会挑单个含数字的分句，
        #   把项目名（主语）丢掉，切出"我做过验收要求…"这种没头的话。
        _cand_budget = len(p["body"]) + spare
        longer = _grow_evidence(p["evid"]["text"], max(0, _cand_budget))
        if not longer or len(longer) <= len(p["body"]):
            continue
        # ⚠ 不许为了"变长"把数字弄丢。
        #   实测升级后切出过「BM25 0.6 + 向量 0.4 混合检索、Top-K 去重排序、ReAct Agent、
        #   Coze 工作流、OpenAI 兼容 API 集成（…接口降级）」——全是技术参数，
        #   而「200 道评估集、7/7 通过」被挤没了。
        #   本人 SOP 第一条就是"能用数字绝不写有相关经验"，数字丢了就不升级。
        if _nums(p["evid"].get("short")) - _nums(longer):
            continue
        p["body"] = longer

    # 最后一步：带 github 的锚点如果还有富余，换成【整版长文】（整版才带 GitHub 句）。
    # 换不下就保持短版 —— 短版同样带 GitHub 句，所以怎么都不会漏。
    for p in picks:
        a = ANCHORS.get(p["evid"]["id"]) or {}
        if not (RAG_ON and RAG_TEXT and a.get("github")):
            continue
        full = p["evid"]["text"]
        if len(full) <= len(p["body"]):
            continue
        if room - total() >= len(full) - len(p["body"]):
            p["body"] = full
    return picks


def _is_intern(job):
    """这条岗位是不是实习岗。

    ★ 2026-10-29 晚本人规则：不是实习 → 到岗句整句不写；是实习 → 追加"可随时到岗"。

    三路信号（命中任意一路即判实习），写在 yaml 的 availability.detect_intern.signals：
      1) days_per_week / least_month 有值 —— Boss 官方字段，只有实习岗才填（最可靠）
      2) salary_raw 含"元/天"或"/天"    —— 按天计薪基本等于实习
      3) 岗位名含实习/实习生/见习/intern  —— ⚠ 只查岗位名，不查 JD
    三路都没命中 → 非实习（宁可不写，不要写错）。
    """
    # 1) 结构化字段：最可靠
    if str(job.get("days_per_week") or "").strip() or str(job.get("least_month") or "").strip():
        return True
    # 2) 按天计薪
    sal = str(job.get("salary_raw") or "")
    if "元/天" in sal or "/天" in sal:
        return True
    # 3) 岗位名关键词（不查 JD —— 实测查 JD 会误判 7 条全职岗）
    d = AVAIL_CFG.get("detect_intern") or {}
    kw_signal = {}
    for s in (d.get("signals") or []):
        if s.get("id") == "岗位名关键词":
            kw_signal = s
            break
    fields = kw_signal.get("in_fields") or ["title"]
    kws = [str(k) for k in (kw_signal.get("keywords") or ["实习"]) if k]
    text = " ".join(str(job.get(f) or "") for f in fields)
    return any(k in text for k in kws)


def availability_line(job):
    """到岗句。非实习岗返回空串（整句不出现），实习岗在后面追加"可随时到岗"。"""
    if not AVAIL_CFG.get("enabled"):
        return ""
    if AVAIL_INTERN_ONLY and not _is_intern(job):
        return ""
    # ★ 2026-11-01 本人新增：到岗句走同义变体池（F1~F4，按 job_id 确定性挑）。
    #   变体是完整句（含"5 天/6 个月"），无需再拼"可随时到岗"（intern_extra 只挂在原句上）。
    base = _pick_variant((AVAIL_CFG.get("variants") or []), job, AVAIL)
    if _is_intern(job) and AVAIL_INTERN_EXTRA:
        return base + "，" + AVAIL_INTERN_EXTRA
    return base


def _industry_word(job):
    """行业前景句里的行业词：只认客观大类，绝不猜对方具体业务。

    词表在 config/greeting_spec.yaml 的 motivation.industry_words（本人可改）。
    猜错行业 = 编造，所以兜底一律回落到最宽的 "AI 应用"。
    """
    text = " ".join(str(job.get(k) or "")
                    for k in ("title", "jd_text", "labels", "company", "family"))
    low = text.lower()
    for row in (MOTIV.get("industry_words") or []):
        for kw in (row.get("match") or []):
            if str(kw).lower() in low:
                return row.get("word") or ""
    return MOTIV.get("industry_fallback") or "AI 应用"


def motivation_line(job):
    """本人 2026-10-29 SOP 第 5 条 + 当晚修订：动机 = "最主要的是我" + 理性理由。

    ★ 2026-10-29 晚本人亲自修订：**不得用"真的想去这家公司"这类讨好奉承**，
      要体现理性层面的价值判断 —— 二选一：
        ① 行业前景："最主要的是我认为{行业}行业十分具有前景"
        ② 长期规划："最主要的是我长期规划打算深耕{岗位}这个岗位"
      两个句式都写在 yaml 的 motivation.angle 里（本人可改），
      选哪个由 motivation.angle.default 决定，机器不自行发明第三种说法。
    """
    ang = MOTIV.get("angle") or {}
    which = ang.get("default") or "career"
    # ★ 2026-11-01 本人新增：长期规划版走同义变体池（E1~E4）。
    #   变体是「完整句」，不带 {goal} 变量（本人已确认照搬，接受丢掉具体岗位名的写法）。
    #   只有 career 角度才轮换；industry 角度保持原样（它本来就只认客观大类，无需轮换）。
    if which == "career":
        v = _pick_variant((ang.get("career_variants") or []), job, None)
        if v:
            return v
    tpl = (ang.get(which) or {}).get("tpl")
    if not tpl:                                  # 兜底：万一 yaml 里 angle 被改坏
        tpl = (MOTIV.get("templates") or {}).get("no_company")
    if not tpl:
        return MOTIV_PREFIX + "长期规划打算深耕" + _goal_from_job(job) + "这个岗位"

    goal = _goal_from_job(job)
    industry = _industry_word(job)
    # 英文/数字开头的词前面补个空格：不然读成"深耕AI 产品经理""认为AIGC行业"这种黏连怪句
    if goal[:1].isascii() and goal[:1].isalnum():
        goal = " " + goal
    if industry[:1].isascii() and industry[:1].isalnum():
        industry = " " + industry
    return tpl.format(goal=goal, industry=industry,
                      title=(job.get("title") or "").strip(),
                      company=(job.get("company") or "").strip())


def build(job, closing_id=None, anchor_idx=0):
    """拼一条打招呼语。

    结构（2026-10-28 改版）：
      称呼 + 固定自我介绍
      + 【JD 最重视的三条能力，按重视度高→中→低，每条配一句我们的证据】
      + 职业规划 + 到岗信息 + 反问结尾

    三点说明：
      1) 去掉了"双锚点"硬门槛（不再要求必须引用 JD 原句），省下的字全部给证据；
      2) 证据不再按位置死配（第1条给经历、第2条给能力句…），
         改成【这条能力我们拿什么接】—— 按 abilities 对照表挑，接不住就不写；
      3) 价值段【不写能力名标签】。标签只是把 JD 的要求换个说法念一遍，
         HR 知道自己要什么，重复一遍不产生新信息；直接报项目名，
         让他自己判断对不对得上 —— 他做出的判断比我们替他做的可信。

    anchor_idx 只是换起点：卡住时从排序里的下一条经历开始试。
    """
    ranked = _ranked_anchors(job)
    if not ranked:
        return None
    # ★ 2026-11-01 本人规则：先按 job 把三处「内部变体」（实习句/总起句/GitHub句）算好，
    #   供 _assemble / _lay_out / _all_evidence 这些拿不到 job 的函数读取。
    _set_current_variants(job)
    career = CAREER_LINE.get(job.get("family"), CAREER_LINE[None])

    sal = salutation_for(job)
    # ★ 2026-11-01 本人规则：自我介绍句走同义变体池（按 job_id 确定性挑），
    #   拿不到 id 或池空 → 退回原句 SELF_INTRO。
    intro_text = _pick_variant((SPEC.get("self_intro") or {}).get("variants"),
                               job, SELF_INTRO)
    intro = "，" + intro_text + "。" if intro_text else "，"
    # 到岗句跟职业规划句合成一句，中间用逗号 —— 拆成两句会把结尾挤掉。
    # ★ 2026-10-29 晚本人规则：到岗句只在【实习岗】出现，且后面追加"可随时到岗"；
    #   非实习岗整句不写，此时动机句自己收句号，避免留下半句。
    line = motivation_line(job)
    avail_txt = availability_line(job)
    if avail_txt:
        mid, avail = line, ("，" + avail_txt + "。")
    else:
        mid, avail = line + "。", ""

    jp = SPEC.get("jd_points") or {}
    topn = int(jp.get("max_points") or jp.get("rank_top") or 3)
    weights = jp.get("weights") or [0.40, 0.33, 0.27]
    plan = rank_jd_points(job, top=topn)

    ev = _all_evidence(job, anchor_idx=anchor_idx)

    # 结尾和三条价值段都在抢 LENGTH 那点字数。取舍按本人的话来：
    # 「把空间留给展示我们的价值的部分」→ 先看能放下几条【完整】证据；
    # 能放下的条数一样时，才优先用【按这条岗位情况挑中】的那个结尾
    # （所以不是随机轮换，兜底句 Z 只在别的都放不下时才轮到）。
    base = len(sal) + len(intro) + len(mid) + len(avail)
    # ★ 2026-10-30 本人规则：学历句后固定加「曾于…实习」句，长度要算进预算，
    #   否则 _lay_out 分给证据的字会偏多，最后整条超 360 被字数门拦下。
    if INTRO_FLOW_ON and INTRO_INTERN_LINE:
        base += len(_evp_intern()) + 1   # +1 是它后面的句号
    # ★ 2026-11-01 本人拍板（改）：心理学已改回「诉求洞察能力点的证据」，
    #   不再作为固定段硬插，故不再在这里预扣固定段长度（其字数由 _lay_out 正常预算）。
    # ★ 本条岗位在要软技能 → 素材走 psychology，禁用<经历B>/<经历A>。
    #   两个来源都看：① JD 原文里有这些词；② 挑出来的能力点名字里有
    #   （JD 写"跨团队沟通"，能力点叫"跨角色协作"，只查原文会漏 —— 实测漏了 4 条）。
    soft = _soft_mode(job) or any(
        any(t in (name or "") for t in SOFT_TRIGGERS) for name, _th, _s in plan)
    if CLOSING_FIXED:
        # 本人 SOP 第 7 条：结尾固定一句，不按岗位挑、不轮换、不区分性别。
        # ★ 2026-11-01 本人新增：固定结尾句也走同义变体池（G1~G4，按 job_id 确定性挑），
        #   拿不到 id 或池空 → 退回固定原句。
        closing = _pick_variant((SPEC.get("closing") or {}).get("fixed_variants"),
                                job, CLOSING_FIXED_TEXT or CLOSINGS[0]["text"])
        info = _lay_out(plan, ev, LENGTH[1] - base - len(closing), soft=soft,
                        job=job)
        if not info:
            return None
        cid = (SPEC.get("closing") or {}).get("fixed_id") or "Z"
    else:
        primary = _closing_candidates(job)[0]
        seen = []
        for cid in (([closing_id] if closing_id else []) + _closing_candidates(job) + ["Z"]):
            if cid not in seen:
                seen.append(cid)
        best = None
        for cid in seen:
            closing = next((c["text"] for c in CLOSINGS if c["id"] == cid), CLOSINGS[0]["text"])
            info = _lay_out(plan, ev, LENGTH[1] - base - len(closing), soft=soft,
                            job=job)
            if not info:
                continue
            score = (len(info), 1 if cid == primary else 0, -len(closing))
            if best is None or score > best[0]:
                best = (score, cid, closing, info)
        if best is None:
            return None
        _s, cid, closing, info = best
        _RECENT_CLOSINGS.append(cid)

    # 上面按比例切完，可能有某条被压成碎片。这里把碎片还原成完整的短版
    # （"团队销冠"这种断句太短，读着像话没说完），从最重要的那条开始补。
    # ⚠ 不再把短版升级成长版：短版是人压好的完整说法，升级只会把后两条挤没。
    for p in info:
        e = p["evid"]
        if len(e["short"]) <= len(p["body"]):
            continue
        grown = (len(_assemble(sal, intro, info, mid, closing, avail))
                 - len(p["body"]) + len(e["short"]))
        # ★ 2026-10-29 晚：同样的坑在这里还有第二份。
        #   _lay_out 里给 GitHub 句留了位置，但这一段把 body 换回 short 时
        #   又按"全长"算一遍，结果 short（带 GitHub）装不下 → 不换 →
        #   body 停在长版前半截（没 GitHub）。所以这里也要扣掉 GitHub 那句。
        if RAG_ON and RAG_TEXT and (ANCHORS.get(e["id"]) or {}).get("github") \
                and _evp_rag_text() not in e["short"]:
            grown += 1 + len(_evp_rag_text())
        if grown <= LENGTH[1]:
            p["body"] = e["short"]

    text = _assemble(sal, intro, info, mid, closing, avail)
    # ── 2026-11-11 本人拍板：把本条招呼语真正用到的变体 id 收集起来回传，
    #   供 run.py 记进台账，好做 A/B 测试归因（哪个变体「要简历率」更高）。
    #   之前只回传 text，id 丢了，台账的 salutation_variant 一直是写死的 'neutral'。
    #   这里每处都用 _pick_variant_id（和 _pick_variant 同一套 job_id 哈希）取 id，
    #   保证记的 id 和真正选中的那句严格一致。
    _vids = [
        _pick_variant_id((SPEC.get("self_intro") or {}).get("variants"), job),
        _pick_variant_id(INTRO_FLOW.get("intern_line_variants"), job),
        _pick_variant_id(INTRO_FLOW.get("rag_opener_variants"), job),
        _pick_variant_id(RAG_RULE.get("text_variants"), job),
        _pick_variant_id((SPEC.get("availability") or {}).get("variants"), job),
        # ★ 求职动机 E 组：只有 career 角度才轮换（industry 角度不轮换）。
        #   跟 motivation_line() 里的判断保持一致，避免记了一个根本没被选中的 id。
        (_pick_variant_id((MOTIV.get("angle") or {}).get("career_variants"), job)
         if (MOTIV.get("angle") or {}).get("default") == "career" else ""),
        cid,
    ]
    variant_ids = [v for v in _vids if v]
    return {
        "text": text,
        "closing_id": cid,
        "variant_ids": variant_ids,
        "jd_points": [p["name"] for p in info],
        "jd_themes": [p["theme"] for p in info],
        "resume_anchor": next((p["evid"]["id"] for p in info
                               if p["evid"]["kind"] == "anchor"), ""),
        "capability": next((p["evid"]["id"][4:] for p in info
                            if p["evid"]["kind"] == "cap"), ""),
        "capability_text": "",
    }


# ---------- 五道门 ----------

def check(greet, recent):
    reasons = []
    text = greet["text"]
    n = len(re.sub(r"\s", "", text))

    # ⚠ 2026-10-28 本人要求：去掉「双锚点」硬门槛，把字数让给价值展示。
    # 不再检查"有没有引用 JD 原话"，但仍要求有【简历证据】（见下一条）。
    if not greet.get("resume_anchor"):
        reasons.append("缺简历锚点")
    # ★ 2026-11-10 本人拍板：证据（经历/能力）最少 2 条，只有 1 条判不过门。
    min_picks = int((SPEC.get("jd_points") or {}).get("min_points") or 2)
    if len(greet.get("jd_points") or []) < min_picks:
        reasons.append(f"证据只有 {len(greet.get('jd_points') or [])} 条，少于 {min_picks} 条")
    if not (LENGTH[0] <= n <= LENGTH[1]):
        reasons.append(f"字数 {n}，超出 {LENGTH[0]}-{LENGTH[1]}")
    sim = _max_similarity(text, recent)
    if SIM_GATE and sim > SIM_MAX:
        reasons.append(f"和最近的招呼语太像（{sim:.2f}）")
    # 能力型补充句：只许"掌握/熟悉"，不许夹带数字、项目名、公司名、时间
    cap_text = greet.get("capability_text") or ""
    if cap_text and not _cap_safe(cap_text):
        reasons.append("能力补充句里夹带了数字或具体事实（只许说掌握/熟悉）")
    bad = [w for w in FORBIDDEN if w in text]
    if bad:
        reasons.append(f"命中禁用词：{'、'.join(bad)}")
    if "…" in text:
        reasons.append("句子被截断（证据段塞不下，应换更短的经历而不是截半句）")
    if HOOK_PHRASE not in text:
        reasons.append(f"缺『{HOOK_PHRASE}』引出的职业规划句")

    # ── 第六道门：风控敏感词（2026-10-31 加）────────────────────────
    # 命中就直接不过门，比上面的讨好词严格 —— 这些是可能触发封号的词，
    # 不是"写得不够好"，是"可能惹祸"。宁可这条发不出去，也不能发出去踩雷。
    risk_hit = [w for w in RISK_SENSITIVE_HARD if w in text]
    risk_hit += [w for w in RISK_SENSITIVE_OFFPLATFORM if w in text]
    if risk_hit:
        reasons.append(f"命中风控敏感词：{'、'.join(risk_hit)}")

    return (len(reasons) == 0), reasons, n, sim


# ---------- 跟进句（第二句） ----------

def _overlap_fragment(text, first):
    """找出第二句里有没有整段照抄第一句（≥8 个字连续一样就算）。

    为什么用这个而不是"相似度"：相似度 0.95 可能只是"少了一个不"字，
    看数字根本发现不了。逐字比对才靠得住 —— 跟发送端的回读校验一个理。
    """
    a = re.sub(r"\s", "", text or "")
    b = re.sub(r"\s", "", first or "")
    for n in range(12, 7, -1):
        for i in range(len(a) - n + 1):
            if a[i:i + n] in b:
                return a[i:i + n]
    return None


def build_followup(job, first_text, closing_id=None, attempt=0):
    """给没回你的 HR 补一句。

    三条硬规矩（来自 config/greeting_spec.yaml 的 followup.hard_rules）：
      1. 不复述第一句里的任何一句 → 用新 JD 锚点 + 另一条简历经历
      2. 长度不超过第一句的一半 → 长了就变催，不是补
      3. 同一家最多跟一次 → 由调用方查台账把关
    """
    fu = SPEC.get("followup") or {}
    if not fu.get("enabled"):
        return None

    first = re.sub(r"\s", "", first_text or "")
    if not first:
        return None

    ratio = fu.get("length_ratio", 0.5)
    maxlen = max(fu.get("length_floor", 30), int(len(first) * ratio))

    # 1) 换一个 JD 锚点：必须是第一句里没出现过的
    phrase = None
    for i in range(1, 6):
        p = _jd_anchor(job, i)
        if p and p not in first:
            phrase = p
            break

    openers = fu.get("openers") or ["您好，再补一句："]
    closings = fu.get("closings") or [{"id": "F1", "text": "方便的话我把简历发您。"}]
    # attempt 轮换：每次换一组（开场白 + 结尾），这样多试几次才真的在换东西。
    # 不轮换的话，试 10 次生成的都是同一句，attempts 形同虚设。
    spin = hash_offset(job) + attempt
    opener = openers[spin % len(openers)]
    cid = closing_id or closings[spin % len(closings)]["id"]
    closing = next((c["text"] for c in closings if c["id"] == cid), closings[0]["text"])

    head = f"{opener}JD 里「{phrase}」这块，" if phrase else opener
    budget = maxlen - len(head) - len(closing) - 1        # 1 是句号

    # 2) 换一条简历经历：首句用的是第 0 条，跟进换一条【没说过】的。
    # 先只在"跟这条 JD 对得上"的经历里挑；一条都对不上才退到其余的。
    # 顺序不能反 —— 反过来就会出现「JD 说要输出 PRD，我回一句<经历A>卖了多少」的笑话。
    ranked = _ranked_anchors(job)
    fresh = [x for x in ranked[1:] if x[0] > 0 and x[2]["text"][:8] not in first]
    if not fresh:
        fresh = [x for x in ranked[1:] if x[2]["text"][:8] not in first]
    # 短结尾更容易塞得下，所以按长度排，配合 attempt 一起轮换
    fresh = sorted(fresh, key=lambda x: len(x[2]["text"]))
    if fresh:
        off = spin % len(fresh)
        fresh = fresh[off:] + fresh[:off]

    evid_text = used_key = evid_hit = None
    for _h, k, a in fresh:
        cand = _fit_evidence(a["text"], budget)
        if cand and cand[:8] not in first:
            evid_text, used_key, evid_hit = cand, k, _h
            break
    if not evid_text:
        return None                      # 找不到新东西可说 → 宁可不跟

    # 既没有新 JD 锚点、这条经历又跟 JD 一条都对不上 → 标成"低把握"。
    # 不直接判失败：判失败就彻底不出了，你连看都看不到。
    # 标出来的意思是【机器不擅自决定】—— 低把握的留给人工看一眼再发。
    low_confidence = (phrase is None and not evid_hit)

    body = f"{head}{evid_text}。{closing}"

    return {"text": body, "closing_id": cid, "jd_anchor": phrase,
            "resume_anchor": used_key, "max_len": maxlen,
            "low_confidence": low_confidence}


def check_followup(greet, first_text, recent):
    reasons = []
    text = greet["text"]
    n = len(re.sub(r"\s", "", text))
    first = re.sub(r"\s", "", first_text or "")

    if n > greet.get("max_len", 60):
        reasons.append(f"太长：{n} 字，超过第一句的一半（{greet['max_len']}）")
    if n < 20:
        reasons.append(f"太短：{n} 字，说了等于没说")
    frag = _overlap_fragment(text, first)
    if frag:
        reasons.append(f"复述了第一句：「{frag}」")
    sim = _max_similarity(text, recent)
    if SIM_GATE and sim > SIM_MAX:
        reasons.append(f"和最近发过的太像（{sim:.2f}）")
    bad = [w for w in FORBIDDEN if w in text]
    if bad:
        reasons.append(f"命中禁用词：{'、'.join(bad)}")
    if "…" in text:
        reasons.append("句子被截断")
    return (len(reasons) == 0), reasons, n, sim


def draft_followup(job, first_text, attempts=10):
    recent = load_recent()
    last = None
    for i in range(attempts):
        g = build_followup(job, first_text, attempt=i)
        if not g:
            continue
        ok, reasons, n, sim = check_followup(g, first_text, recent)
        g.update({"ok": ok, "reasons": reasons, "chars": n, "similarity": round(sim, 2)})
        last = g
        if ok:
            return g
    return last


def draft(job, attempts=12, start=0):
    """
    反复生成直到过五道门。
    每次换一个结尾变体，塞不下就换一条更短的经历 —— 但绝不把一个词截一半。
    实在过不了就带着原因退回，绝不悄悄放行。
    """
    recent = load_recent()
    dec = decompose_jd(job)      # 拆 JD 一次，附加到最终结果（风险&注意点）
    # 本条招呼语的模型调用计数清零（见 evidence_pick.max_calls_per_greeting）
    if _evidence_pick_on():
        try:
            from evidence_pick import begin_greeting
            begin_greeting()
        except Exception:
            pass
    last = None
    for i in range(attempts):
        # 结尾由这条岗位说了什么决定（pick_closing），不再轮换；
        # 过不了门就换一条经历，而不是换结尾。
        g = build(job, closing_id=None, anchor_idx=start + i)
        if not g:
            continue
        ok, reasons, n, sim = check(g, recent)
        g.update({"ok": ok, "reasons": reasons, "chars": n, "similarity": round(sim, 2)})
        last = g
        if ok:
            break
    if last is not None:
        last["jd_decompose"] = dec
        last["risks"] = dec["risks"]
        last["explicit_keywords"] = dec["explicit_keywords"]
    return last
