r"""
锚点 × 证据来源 一致性校验（不虚构铁律的机器版）

招呼语里每一个【带数字的说法】，都必须在证据来源里能找到。
证据来源写在 config/greeting_spec.yaml 的 evidence_sources：
  · resumes/national/*.md  四份简历原件
  · D:\Codex\...            本人 2026-10-29 指认的 RAG 项目原件（技术细节以它为准）
"""

import glob
import io
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config_loader import greeting_spec, path_of

SPEC = greeting_spec()

# 证据来源：yaml 里配了就用 yaml 的，没配就退回只扫简历（向后兼容）
_srcs = (SPEC.get("evidence_sources") or {}).get("files") or ["resumes/national/*.md"]
paths = []
for pat in _srcs:
    if pat.startswith("D:/") or os.path.isabs(pat):
        paths += sorted(glob.glob(pat))
    else:
        paths += sorted(glob.glob(path_of(pat)))
if not paths:                                    # 兜底：至少扫简历
    paths = sorted(glob.glob(path_of("resumes/national/*.md")))

blob = ""
loaded = []
for p in paths:
    try:
        blob += io.open(p, encoding="utf-8", errors="replace").read()
        loaded.append(os.path.basename(p))
    except Exception as e:
        print(f"  ⚠ 读不到证据文件：{p}  ({e})")
blob = blob.replace("\n", "").replace(" ", "")

# 把证据里出现过的所有"数字片段"抽出来，用于比对
RESUME_NUMS = set(re.findall(r"[0-9][0-9./+%%%]{0,8}", blob))

print(f"证据来源 {len(loaded)} 份：{loaded}")
print("里面出现过的数字片段：", sorted(RESUME_NUMS))
print("=" * 78)

bad = 0
for key, a in SPEC["resume_anchors"].items():
    # 停用的锚点不会出现在招呼语里，查它只会报假警
    # （sales 已全禁，但里面有个简历里没有的日期，之前每跑必报一条"有问题"）
    if not a.get("enabled", True):
        continue
    text = a["text"]
    nums = re.findall(r"[0-9][0-9./+%%%]{0,8}", text)
    # 再抽一些"非数字但有事实含义"的专有名词
    words = [w for w in re.split(r"[，,、（）()【】\[\]/＋+＋·]", text) if len(w) >= 2]
    missing_n = [n for n in nums if n not in blob and n not in RESUME_NUMS]
    missing_w = [w for w in words
                 if any(ch in w for ch in "队人队元元") is False
                 and len(w) <= 12
                 and w not in blob]
    flag = "✗ 有问题" if missing_n else "OK"
    if missing_n:
        bad += 1
    print(f"{flag}  {key}: {text}")
    if missing_n:
        print(f"        ↳ 简历里找不到的数字：{missing_n}")
    if missing_w:
        print(f"        ↳ 简历里找不到的词组（人工判断）：{missing_w[:6]}")

print("=" * 78)
print(f"合计 {bad} 条锚点含简历里没有的数字")

# 另外单独查几条高危说法
# ✅ 2026-10-28 用户本人确认：心理学/认知学相关依据真实存在（简历未写明属正常），
#    这两条不再当"高危"，只报告存在性、不再要求简历出处。
USER_CONFIRMED = {"心理学", "认知学"}
print("\n高危说法单独查：")
for phrase in ["三下乡", "ReAct", "Agent", "60队", "60 队", "心理学", "认知学",
               "STAR", "CP26", "Coze", "DashScope", "OpenAI"]:
    mark = "有" if phrase in blob else "没有"
    tag = "（用户已确认真实，不要求简历出处）" if phrase in USER_CONFIRMED else ""
    print(f"  {mark}：{phrase}{tag}")
