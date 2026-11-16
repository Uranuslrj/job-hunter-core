# 证据挑选：让模型只"选编号"，不写字
#
# 设计（本人 2026-10-29 拍板）：
#   规则照常跑完六段框架、拆 JD、挑能力点；
#   到【这条能力用哪句证据接】这一步，把白名单里的候选列给模型，
#   模型只回一个编号，代码拿编号去取原文 —— 它从头到尾没有写字的权力。
#
# 为什么这样最安全：
#   - 数字不可能编：正文是代码从档案里取的原文，模型没碰过一个字
#   - 六段框架不破坏：模型只影响"挑哪条"，不影响"怎么拼"
#   - 错了代价最小：最坏是挑了条没那么贴的，退回规则版即可
#
# 三道保险（缺一个都不许用）：
#   ① 模型返回的必须严格是候选编号之一，否则作废
#   ② 任何异常（超时/乱回/空）→ 一律退回规则版（白名单第一个）
#   ③ 走 client.chat → 自动过隐私脱敏层，公司名/人名在模型眼里是占位符
#
# 用法（greeting.py 里 _evidence_for 已经接上，正常不需要手动调）：
#   from evidence_pick import pick_evidence
#   body, e = pick_evidence(theme, ev, used, budget, hint, block, job, name)
#
# 开关（2026-10-29 晚本人拍板正式接入，配置在 config/greeting_spec.yaml 的
# evidence_pick 块，不在本文件）：
#   evidence_pick.enabled: true/false        ← 改这里就把整条链路切回纯规则版
#   环境变量 BOSS_EVIDENCE_PICK=off/on        ← 不改文件的临时开关，适合 A/B 对比

import re
import sys
import os
import json
import hashlib
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 从 greeting 借规则原件：候选白名单、互斥组、断句函数
# （这些都是本人定好的规则，本模块只借用不改）
from greeting import ABIL_EVID, MUTEX, _phrases_of
from model_client import client

# ── 决策缓存（2026-11-12 本人拍板新增）：固化「模型选编号」的结果 ──
# 问题：模型选编号受服务状态漂移（超时/熔断时回退规则版，失败又不写 model_cache），
#   导致同一批岗位两次跑结果不一致，最终队列条数 22 vs 100 漂移。
# 解法：模型选出的编号一旦确定，按「job_id + 能力点 + 候选集」固化到本地决策文件，
#   第二次跑直接读这个决策，不再重新问模型 → 彻底消除漂移。
# 回退：环境变量 BOSS_EVIDENCE_DETERMINISTIC=off 关闭本层，回到现状行为，零成本。
_DECISION_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "..", "data", "evidence_decision.json")


def _deterministic_on():
    """决策缓存是否开启。默认开；=off 关闭（回退用）。"""
    return os.environ.get("BOSS_EVIDENCE_DETERMINISTIC", "on").strip().lower() != "off"


def _decision_load():
    try:
        with open(_DECISION_FILE, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _decision_save(d):
    try:
        os.makedirs(os.path.dirname(_DECISION_FILE), exist_ok=True)
        with open(_DECISION_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def _decision_key(job, ability_name, cands):
    """决策缓存的 key：同一岗位 + 同一能力点 + 同一批候选 → 同一个决策。"""
    jid = ""
    if isinstance(job, dict):
        jid = job.get("job_id") or job.get("job_name") or ""
    cand_ids = [k for k, _ in cands]
    raw = json.dumps({"jid": jid, "ability": ability_name, "cands": cand_ids},
                     ensure_ascii=False, sort_keys=True)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()

# 最多给模型看几个候选（太多它反而乱选，也费钱）
MAX_CANDIDATES = 6

# 一条招呼语最多问模型几次（含过不了门时的重试）。
# 为什么要有这个：draft() 过不了五道门会重试，重试又调 _lay_out，
# 不设上限的话一条话术可能被反复问十几次 —— 既费钱又拖慢发送链路。
MAX_CALLS_PER_GREETING = 10
_calls = 0


def _cfg():
    """读 yaml 的 evidence_pick 配置（读不到就用本文件的默认值）。"""
    try:
        from greeting import SPEC
        return SPEC.get("evidence_pick") or {}
    except Exception:
        return {}


def begin_greeting():
    """draft() 开头调一次：把本条招呼语的模型调用计数清零。"""
    global _calls
    _calls = 0


def calls_used():
    return _calls


def _budget_left():
    cap = int(_cfg().get("max_calls_per_greeting") or MAX_CALLS_PER_GREETING)
    return _calls < cap


def _rule_candidates(theme, ev, used, budget, hint, block):
    """按原规则列所有能用的候选，返回 [(id, body), ...]。

    过滤条件跟 _evidence_for 一字不差：白名单顺序、已用、block、互斥组、断句。
    """
    cands = []
    for key in (ABIL_EVID.get(theme) or []):
        e = ev.get(key)
        if not e or key in used or key in block:
            continue
        # 跟已用掉的那条说的是同一组数据 → 跳过
        if any(key in pair and (pair - {key}) & used for pair in MUTEX):
            continue
        body = _phrases_of(e, budget, hint)
        if body:
            cands.append((key, body))
    return cands


def _ask_model(cands, job, ability_name):
    """把候选列给模型，只要一个编号。返回 id 或 None（代表别用它的）。"""
    global _calls
    jd_title = (job.get("title") or job.get("job_name") or "").strip()
    company = (job.get("company") or job.get("company_name") or "").strip()

    lines = []
    for i, (key, body) in enumerate(cands, 1):
        lines.append(f"{i}. 编号[{key}] 内容：{body}")
    cand_block = "\n".join(lines)

    prompt = (
        "你在帮一位求职者写打招呼语。现在要给一个能力点挑一句最贴切的证据。\n"
        f"岗位：{jd_title}"
        + (f"（{company}）" if company else "") + "\n"
        f"要证明的能力：{ability_name}\n"
        "\n"
        "可选证据如下：\n" + cand_block + "\n"
        "\n"
        "请只输出一个编号，不要解释、不要标点、不要其他任何文字。\n"
        "编号格式示例：" + cands[0][0] + "\n"
    )

    _calls += 1
    out = client.chat([{"role": "user", "content": prompt}],
                      tag="evidence_pick",
                      temperature=float(_cfg().get("temperature") or 0.0),
                      max_tokens=40,
                      # 选编号是"一句话的活"，不该等 60 秒。
                      # 实测日志里有多条卡满 60000ms，三次一叠加能卡 3 分钟。
                      timeout=float(_cfg().get("timeout_seconds") or 12))
    if not out:
        return None

    # 严格清洗：只认候选里真实存在的编号
    text = (out or "").strip().strip("。.,，、\"'「」（）()[]【】 ")
    valid = {k for k, _ in cands}
    if text in valid:
        return text
    # 模型可能加了前缀/解释，从里面抠出第一个出现的合法编号
    m = re.search(r"[A-Za-z_][A-Za-z0-9_]*", text)
    if m and m.group(0) in valid:
        return m.group(0)
    # 也可能回的是序号（1/2/3）
    if text.isdigit():
        n = int(text)
        if 1 <= n <= len(cands):
            return cands[n - 1][0]
    return None


def _rag_forced(job):
    """判断 RAG 铁律是否触发，返回被强制插队的锚点 id（没触发返回 None）。

    跟 greeting._ranked_anchors 里那段判断用同一份配置（anchor_pick.force_first_when），
    保证两边口径一致 —— 那边负责把 project_rag 排到队首，这边负责不让模型把它换掉。
    """
    try:
        from greeting import SPEC, ANCHORS
        ff = (SPEC.get("anchor_pick") or {}).get("force_first_when") or {}
        fid = ff.get("anchor")
        if not fid or fid not in ANCHORS:
            return None
        jd = (job.get("jd_text") or "") + " " + (job.get("title") or "")
        if any(t and t in jd for t in (ff.get("jd_triggers") or [])):
            return fid
    except Exception:
        pass
    return None


def pick_evidence(theme, ev, used, budget, hint="", block=(), job=None,
                  ability_name="", use_model=True):
    """给一条能力挑证据。

    返回 (body, e)，跟原 _evidence_for 的返回格式一致，可以直接替换。
    任何情况下都不会因为模型出问题而返回坏东西 —— 兜底永远是规则版。
    """
    cands = _rule_candidates(theme, ev, used, budget, hint, block)
    if not cands:
        return None, None

    # ★ 2026-10-29 补的硬约束（踩到才发现的）：
    #   RAG 铁律在 _ranked_anchors 里只是"排到队首"，模型完全可以在选题这一步
    #   把它换成别的（实测阶跃星辰那条：JD 有「Agent/大模型/评测」，模型却把
    #   project_rag 换成了 Coze 工作流 → 剧本杀项目和 GitHub 那句全没了）。
    #   所以第一条能力点（used 为空）必须锁死用铁律指定的那条，不许模型改。
    forced = _rag_forced(job or {})
    if not used:
        # ① 铁律触发：第一段必须是铁律指定的那条（通常就是 project_rag）
        if forced and _cfg().get("lock_when_forced", True):
            for k, b in cands:
                if k == forced:
                    return b, ev[k]
        # ② 铁律没触发：第一个能力槽仍按规则版走，模型只许改后面的槽。
        #    ⚠ 注意这里锁的是「能力槽」，不是「最终显示顺序」：
        #      _lay_out 最后会按"正文里有没有数字"重排，带数字的往前顶。
        #      所以锁住第一个槽 ≠ 锁住第一段话（实测「中科创达」：
        #      第一槽是"原型绘制"，而排到最前面的那段是第三槽挑的 RAG）。
        # 这么锁的用处：最看重的那条能力不给模型改（确定性最高、少一次调用），
        # 顺带也少花一点延迟。
        if _cfg().get("lock_first", True):
            return cands[0][1], ev[cands[0][0]]

    # 只有一个候选，没什么可选的，不必问模型（省一次调用）
    if len(cands) == 1 or not use_model:
        return cands[0][1], ev[cands[0][0]]

    # 本条招呼语的调用额度用完了 → 剩下的能力点全走规则版（防费用/延迟失控）
    if not _budget_left():
        return cands[0][1], ev[cands[0][0]]

    cap = int(_cfg().get("max_candidates") or MAX_CANDIDATES)
    cands_slice = cands[:cap]

    # ★ 决策缓存（2026-11-12 本人拍板）：先查有没有已经固化过的决策。
    #   有就直接复用，不再重新问模型 —— 这是消除漂移的核心。
    if _deterministic_on():
        key = _decision_key(job, ability_name or hint, cands_slice)
        store = _decision_load()
        if key in store:
            prev = store[key].get("chosen")
            if prev in ev:
                body = next((b for k, b in cands if k == prev), None)
                if body:
                    return body, ev[prev]
            # 缓存的编号现在不可用了（候选变了）→ 当作没有，往下走

    chosen = None
    try:
        chosen = _ask_model(cands_slice, job or {}, ability_name or hint)
    except Exception:
        chosen = None

    # 模型没回 / 回了不认识的编号 → 退回规则版（白名单里排最前的）
    if chosen is None or chosen not in ev:
        # 决策缓存开启时：把「这次走了回退、选的是规则版第一个」也固化下来，
        # 避免「这次回退、下次模型恢复又选别的」来回摆动。
        if _deterministic_on():
            _decision_save({**_decision_load(),
                            _decision_key(job, ability_name or hint, cands_slice):
                                {"chosen": cands[0][0], "fallback": True,
                                 "ts": time.time()}})
        return cands[0][1], ev[cands[0][0]]

    # 模型成功选出编号 → 固化决策，下次直接复用
    if _deterministic_on():
        _decision_save({**_decision_load(),
                        _decision_key(job, ability_name or hint, cands_slice):
                            {"chosen": chosen, "fallback": False,
                             "ts": time.time()}})

    body = next((b for k, b in cands if k == chosen), None)
    if not body:
        return cands[0][1], ev[cands[0][0]]
    return body, ev[chosen]


def pick_evidence_report(theme, ev, used, budget, hint="", block=(), job=None,
                         ability_name=""):
    """调试用：把"规则选了谁 / 模型选了谁"都返回，便于对比。

    locked=True 表示这一条被 RAG 铁律锁死（模型问了也不许改），此时 model 字段
    直接等于 rule —— 跟 pick_evidence 的真实行为保持一致，避免看着自相矛盾。
    """
    cands = _rule_candidates(theme, ev, used, budget, hint, block)
    if not cands:
        return {"cands": [], "rule": None, "model": None, "used_model": False,
                "locked": False}
    rule_pick = cands[0][0]

    forced = _rag_forced(job or {})
    locked_id = next((k for k, _ in cands if k == forced),
                     None) if (forced and not used
                               and _cfg().get("lock_when_forced", True)) else None
    if not locked_id and not used and _cfg().get("lock_first", True):
        # 第一段默认锁规则版（理由见 pick_evidence 里的注释）
        locked_id = rule_pick
    if locked_id:
        return {"cands": [k for k, _ in cands], "rule": rule_pick,
                "model": locked_id, "used_model": False, "changed": False,
                "locked": True}

    chosen = None
    if _budget_left():
        try:
            cap = int(_cfg().get("max_candidates") or MAX_CANDIDATES)
            chosen = _ask_model(cands[:cap], job or {}, ability_name or hint)
        except Exception:
            chosen = None
    return {
        "cands": [k for k, _ in cands],
        "rule": rule_pick,
        "model": chosen,
        "used_model": chosen is not None and chosen in ev,
        "changed": chosen is not None and chosen in ev and chosen != rule_pick,
        "locked": False,
    }
