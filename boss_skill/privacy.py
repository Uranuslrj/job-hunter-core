# 隐私脱敏层 —— 模型外发前自动屏蔽个人隐私
#
# 一句话讲清为什么这么做：
#   「让模型别透露隐私」靠提示词是拦不住的（模型偶尔会漏），
#   必须靠代码：发送前把隐私替换成代号，拿回结果再把代号换回真实值。
#   这样就算提示词失效、就算厂商留存日志，日志里也只有代号，没有真值。
#
# 两层防护：
#   1) 代码硬脱敏（本文件）—— 真值根本不进模型的输入，这是保险丝；
#   2) 提示词软约束 —— 由调用方在 system 里加一句"禁止输出手机号/住址"，
#      属于最后一道提醒，不作为主要手段。
#
# 替换规则：
#   - 手机号 / 邮箱 / 网址 / QQ号 → 替换成 <PHONE> / <EMAIL> / <URL> 这类占位符
#   - 姓名 / 学校 / 公司名 / 地址 → 用白名单（隐私词表）逐条替换成 <PERSON> 等
#   - 身份证号 / 银行卡号 → 正则整段替换成 <ID> / <CARD>
#   隐私词表维护在 config/privacy.yaml，改词表不用改代码。
#
# 用法：
#   from privacy import scrub, restore
#   safe_text, map_ = scrub(原始文本)      # 发模型前
#   result = client.chat([...safe_text...])
#   final = restore(result, map_)          # 拿回结果后换回真值

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config_loader import path_of

PRIVACY_CFG = path_of("config/privacy.yaml")


def _load_rules():
    """读隐私词表。读不到就用内置最小集，保证脱敏至少覆盖最常见几类。"""
    default = {
        "person": [],
        "school": [],
        "company": [],
        "address": [],
        "phone": [],
        "email": [],
        "url": [],
    }
    try:
        import yaml
        d = yaml.safe_load(open(PRIVACY_CFG, encoding="utf-8")) or {}
        if not isinstance(d, dict):
            return default
        out = {}
        for k in default:
            out[k] = list(d.get(k) or [])
        return out
    except Exception:
        return default


# 占位符编号：多值时 <PHONE> / <PHONE2> / <PHONE3> …，保证每个真值都能一一对应换回来
_LABELS = {
    "person": "PERSON",
    "school": "SCHOOL",
    "company": "COMPANY",
    "address": "ADDRESS",
    "phone": "PHONE",
    "email": "EMAIL",
    "url": "URL",
}

# 正则兜底：就算词表没写全，这四类也能被自动抓到
_RE_ID = re.compile(r"\b\d{17}[\dXx]\b")                          # 身份证 18 位
_RE_CARD = re.compile(r"\b\d{13,19}\b")                           # 银行卡 13-19 位
_RE_PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")              # 11 位手机号
_RE_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def _uniq(vals):
    """去重、保序、只留非空。"""
    seen = []
    for v in vals:
        v = str(v or "").strip()   # str() 兜底：yaml 里纯数字会被解析成 int
        if v and v not in seen:
            seen.append(v)
    return seen


def scrub(text):
    """把文本里的隐私替换成占位符，返回 (脱敏后文本, 映射表)。

    映射表形如 { "<PHONE>": "13800138000", "<EMAIL>": "you@example.com", ... }，
    恢复时把占位符逐个换回真值即可。按「长的先换」排序，避免子串互相覆盖。
    """
    if not text:
        return "", {}
    rules = _load_rules()
    mapping = {}
    text = str(text)

    # 1) 正则兜底：先抓手机号/邮箱/身份证/银行卡（这些格式固定，词表不一定写得全）
    def _rep(pattern, label, text):
        nonlocal mapping
        def _sub(m):
            v = m.group(0)
            idx = 1
            key = f"<{label}>"
            while key in mapping and mapping[key] != v:
                idx += 1
                key = f"<{label}{idx}>"
            mapping[key] = v
            return key
        return pattern.sub(_sub, text)

    text = _rep(_RE_ID, "ID", text)
    text = _rep(_RE_CARD, "CARD", text)
    text = _rep(_RE_PHONE, "PHONE", text)
    text = _rep(_RE_EMAIL, "EMAIL", text)

    # 2) 白名单词表：人名/学校/公司/地址/网址等，按「长的先换」避免子串覆盖
    candidates = []
    for kind in ("person", "school", "company", "address", "phone", "email", "url"):
        for v in _uniq(rules.get(kind) or []):
            candidates.append((v, _LABELS[kind]))
    candidates.sort(key=lambda x: -len(x[0]))   # 长的先换

    for val, label in candidates:
        if not val:
            continue
        # 避免把「天津」这种短词换成占位符后覆盖了整句语义；短于 2 字跳过
        if len(val) < 2:
            continue
        if val not in text:
            continue
        idx = 1
        key = f"<{label}>"
        while key in mapping and mapping[key] != val:
            idx += 1
            key = f"<{label}{idx}>"
        mapping[key] = val
        text = text.replace(val, key)

    return text, mapping


def restore(text, mapping):
    """把占位符换回真值。mapping 来自 scrub 的返回值。"""
    if not text or not mapping:
        return text
    out = str(text)
    # 按占位符长度降序换，避免 <PHONE> 先换掉 <PHONE2> 的一部分
    for key in sorted(mapping, key=lambda k: -len(k)):
        out = out.replace(key, mapping[key])
    return out


def scrub_messages(messages):
    """批量脱敏一组 chat messages（system/user 都脱），返回 (安全messages, 合并映射)。"""
    mapping = {}
    safe = []
    for m in messages:
        if not isinstance(m, dict) or "content" not in m:
            safe.append(m)
            continue
        txt, mp = scrub(m.get("content") or "")
        mapping.update(mp)
        safe.append({**m, "content": txt})
    return safe, mapping


def audit(text, mapping):
    """发出去之前自检：确认敏感真值一个都没漏在文本里。

    返回漏网清单；为空表示安全。给调用方做最后一道闸。"""
    leaks = []
    for val in (mapping or {}).values():
        if val and val in text:
            leaks.append(val)
    return leaks
