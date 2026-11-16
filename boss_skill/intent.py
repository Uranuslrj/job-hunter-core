# 回复意图分类（分四档）—— 翻账时对"有人回话"的条目做判定
#
# 分流规则（docs/全自动工作流-设计v1.md 第 3 节）：
#   A 真有兴趣   → 提醒人工回（离 offer 最近的一步，不外包给机器）
#   B 客套婉拒   → 关闭，永不跟进（跟了招人烦）
#   C 系统通知 / 招聘方广告 → 视同没回，进跟进候选
#   D 没回       → 进跟进候选（72 小时后）
#   待看         → 机器分不出来，列给人
#
# 模型的位置：先跑规则版（chat_scan.classify_reply），分不出来的"待看"
# 才交给小模型再看一遍；模型也分不出来，还是列给人。
# 为什么不让模型打头阵：九成回复规则一秒就能定，模型是给剩下那一成兜底的。

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from model_client import client

# 规则版的本体在 chat_scan 里（GOOD_YES / POLITE_NO / SPAM_NO / AUTO_MSG 四组词），
# 这里只做包装，词表只维护一处。
from chat_scan import classify_reply as _rule_classify

# 分类结果 → 动作
ACTION = {
    "真有兴趣": "A_提醒人工回",
    "客套婉拒": "B_关闭不跟",
    "招聘方广告": "C_视同没回",
    "系统通知": "C_视同没回",
    "没回": "D_进跟进候选",
    "待看": "E_列给人工",
}

LLM_PROMPT = (
    "你是求职助手。下面是一位 HR 对求职者打招呼的回复，请判断 HR 的真实意图，"
    "只输出以下五个词之一：真有兴趣 / 客套婉拒 / 系统通知 / 招聘方广告 / 待看。\n"
    "判断标准：约面试、要简历、加微信、问细节 = 真有兴趣；"
    "祝顺利、不合适、暂无坑位 = 客套婉拒；"
    "已收到达人才库、模板化长文 = 系统通知；"
    "推销课程/培训/其他岗位 = 招聘方广告；其余 = 待看。\n"
    "回复原文：「{text}」"
)


def classify(text, use_model=True):
    """返回 (分类, 动作, 用没用模型)。动作见 ACTION。"""
    label = _rule_classify(text)
    used_model = False
    if label == "待看" and use_model:
        out = client.chat(
            [{"role": "user", "content": LLM_PROMPT.format(text=(text or "")[:300])}],
            tag="intent", temperature=0.0, max_tokens=10)
        if out:
            out = out.strip().strip("「」\"' ")
            if out in ACTION:
                label, used_model = out, True
    return label, ACTION.get(label, "E_列给人工"), used_model


def classify_batch(items, use_model=True):
    """items: [{company, text, ...}]。返回同样的列表，加上 intent / action 字段。
    给 daily.py 翻账阶段用：一次进来，出去就是分好类的。"""
    out = []
    for it in items:
        label, action, used = classify(it.get("text") or "", use_model=use_model)
        d = dict(it)
        d["intent"] = label
        d["action"] = action
        d["intent_by_model"] = used
        out.append(d)
    return out
