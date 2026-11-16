# 简历体检：用项目里现成的配置和经历库，自动查简历有没有"露馅"的地方
#
# 复用三处现有能力，不重新发明：
#   1 config/greeting_spec.yaml 的 resume_anchors —— 招呼语会提到的经历，简历里必须有依据
#   2 config/greeting_spec.yaml 的 quantified_formula —— 每条经历要有数字
#   3 config/target_roles.yaml 的 role_clusters —— 目标岗位族关键词，简历里得出现
#
# 用法：python resume_check.py            体检 resumes/original 和 resumes/optimized
#       python resume_check.py <目录>      体检指定目录

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config_loader import roles, greeting_spec

CFG = roles()
SPEC = greeting_spec()

# 招呼语里会讲、但简历里必须找得到依据的关键词
# （从 greeting_spec.yaml 的 resume_anchors 逐条挑出来的实体词）
# ⚠ 开源版已脱敏：下面 <> 里是你自己的经历实体，请替换成你自己的
#   关键词/数据/公司名，用于验证「招呼语里说的，简历里都能找到出处」。
ANCHOR_KEYS = {
    "ToC_service":      ["<服务客户数>", "<好评率>", "<复购率>", "<业务关键词>"],
    "sales":            ["<销售数据>", "<岗位名>", "<公司名>"],
    "coordination":     ["<数据>", "<称号>", "<数字>"],
    "bizsim":           ["<竞赛名>", "<奖项>", "<名次>", "<名次>"],
    "content":          ["<时长>", "<时长>", "<数据>", "<项目名>"],
    "project_rag":      ["<技术词>", "<指标>", "<指标>", "<分数>", "<项目名>"],
    "project_intern":   ["<数据>", "<数据>", "<分类>", "<公司名>", "<工具名>"],
    "project_workflow": ["<工具名>", "<子问题>", "多源检索", "人工确认"],
    "psychology":       ["心理学", "认知学", "认知"],
}

FAMILY_EXTRA = {
    "ai_product_manager": ["PRD", "需求", "原型", "验收", "状态", "流程", "竞品"],
    "fde":                ["API", "接口", "端到端", "降级", "集成", "交付", "Demo", "SQLite"],
    "ai_product_ops":     ["反馈", "运营", "知识库", "用户", "闭环", "指标", "复购"],
    "ai_project_ops":     ["SOP", "进度", "协调", "复盘", "风险", "验收", "测试"],
}

FAMILY_OF_FILE = {
    "AI产品经理": "ai_product_manager",
    "FDE":        "fde",
    "AI产品运营": "ai_product_ops",
    "AI项目运营": "ai_project_ops",
}

NUM_RE = re.compile(r"\d[\d,\.]*\s*(?:%|元|人|条|道|份|次|天|月|年|分|秒|/7|倍|万)?")

# 时间线检查：抓「2021.09 - 2024.06」「2021年9月 - 2024年6月」这类起止写法，
# 查两类问题：① 单段起止颠倒（开始 > 结束）② 多段经历时间重叠打架。
# 2026-10-31 加，对应风控「资料信息→简历时间线矛盾」这条（fengkong-solutions.md 4.1）。
TIME_RANGE_RE = re.compile(
    r"(\d{4})\s*[年.\-/]?\s*(\d{1,2})?\s*月?\s*[至到~\-—－]\s*"
    r"(\d{4})\s*[年.\-/]?\s*(\d{1,2})?\s*月?"
)


def _to_months(year, month):
    """把「年+月」折成从公元0年起的累计月数，方便比大小。月缺省按 12（年末）算。"""
    y = int(year)
    m = int(month) if month else 12
    return y * 12 + (m - 1)


def check_timeline(text):
    """返回 (问题列表)。问题列表每项是「区间A 和 区间B 打架」或「区间起止颠倒」的说明。"""
    issues = []
    spans = []
    for m in TIME_RANGE_RE.finditer(text):
        y1, mo1, y2, mo2 = m.group(1), m.group(2), m.group(3), m.group(4)
        # 年份要像真实年份（1990-2030），别把「200+」这类数据数字误抓成时间
        if not (1990 <= int(y1) <= 2030 and 1990 <= int(y2) <= 2030):
            continue
        s = _to_months(y1, mo1)
        e = _to_months(y2, mo2)
        raw = m.group(0)
        # ① 单段起止颠倒
        if s > e:
            issues.append(f"起止颠倒：{raw}（开始晚于结束）")
            continue
        # 时间跨度超过 12 年，多半是误抓或写错（实习/工作经历不该这么长）
        if e - s > 12 * 12:
            issues.append(f"跨度异常：{raw}（超过 12 年，可能写错）")
        spans.append((s, e, raw))
    # ② 多段经历重叠打架（重叠超过 3 个月才报，避免「7月结束/9月开始」这种正常衔接被误报）
    spans.sort()
    for i in range(len(spans) - 1):
        s1, e1, r1 = spans[i]
        s2, e2, r2 = spans[i + 1]
        if s2 <= e1 - 3:  # 前一段结束 3 个月以上，后一段才开始，才算打架
            issues.append(f"时间重叠：{r1} 和 {r2} 互相打架")
    return issues


def family_keywords(fam_id):
    out = []
    for f in CFG["role_clusters"]:
        if f["id"] == fam_id:
            out += f.get("include", [])
            out += f.get("require_any_signal", [])
    return out


def count_numbers(text):
    return len(NUM_RE.findall(text))


def check_header(text):
    issues = []
    cities = CFG["hard_filter"]["city"]["cities"]
    head = text[:220]
    found = [c for c in cities if c in head]
    if len(found) < 10:
        issues.append(f"期望城市只写了 {len(found)} 个（要投 {len(cities)} 个）")
    if "随时到岗" not in text:
        issues.append("正文里没有「随时到岗」（只在文件名里有，HR 看不到）")
    if not re.search(r"(可实习|能实习|实习)\s*\d+\s*(个月|月)", text):
        issues.append("没写能实习几个月")
    if not re.search(r"每周\s*\d+\s*天|一周\s*\d+\s*天|远程", text):
        issues.append("没写每周能到岗几天 / 是否可远程")
    if "天津" in head:
        issues.append("现居城市与学校所在城市矛盾（请按你的实际情况改）")
    if "远程" not in head and "线上办公" not in head:
        issues.append("抬头没提远程/线上办公（远程岗能绕开地理限制）")
    return issues


def check_one(path):
    name = os.path.splitext(os.path.basename(path))[0]
    text = open(path, encoding="utf-8").read()
    fam = FAMILY_OF_FILE.get(name, "")

    print()
    print("=" * 74)
    print(f"【{name}】　{len(text)} 字　带单位的数字 {count_numbers(text)} 个")
    print("=" * 74)

    hi = check_header(text)
    print(f"\n① 抬头：{'没问题' if not hi else str(len(hi)) + ' 处要改'}")
    for h in hi:
        print(f"　 ✗ {h}")

    print("\n② 招呼语会提到的经历，简历里有没有依据：")
    missing = []
    for aid, keys in ANCHOR_KEYS.items():
        hit = [k for k in keys if k in text]
        if not hit:
            missing.append(aid)
            print(f"　 ✗ {aid:<18} 一个关键词都没有 → 招呼语说了会露馅")
        elif len(hit) < max(2, len(keys) // 2):
            print(f"　 ~ {aid:<18} 只命中 {len(hit)}/{len(keys)}：{hit}")
    if not missing:
        print("　 ✓ 全部有依据")

    if fam:
        kws = family_keywords(fam) + FAMILY_EXTRA.get(fam, [])
        hit = [k for k in kws if k in text]
        print(f"\n③ {fam} 岗位族关键词：命中 {len(hit)}/{len(kws)}")
        miss = [k for k in kws if k not in text]
        if miss:
            print(f"　 缺：{'、'.join(miss[:14])}")

    tl = check_timeline(text)
    print(f"\n④ 时间线一致性：{'没问题' if not tl else str(len(tl)) + ' 处打架'}")
    for t in tl:
        print(f"　 ✗ {t}")
    return missing


def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    d = sys.argv[1] if len(sys.argv) > 1 else os.path.join(root, "resumes")

    targets = []
    # 默认只扫 final/ 成品（当前真正在用的实习版/校招版）。
    # original/optimized 是历史版本，旧文件带一堆早已修掉的假警报（天津/缺心理学等），
    # 混着扫会一直误导人。要复查历史版本，显式传目录参数即可。
    for sub in ("final", "optimized", "original", ""):
        p = os.path.join(d, sub) if sub else d
        if not os.path.isdir(p):
            continue
        if os.path.isdir(os.path.join(d, "final")):
            # 有 final 目录时，只扫 final，不再往下扫旧版本
            break
        for f in sorted(os.listdir(p)):
            if f.endswith(".md"):
                targets.append(os.path.join(p, f))
    if not targets and os.path.isdir(os.path.join(d, "final")):
        for f in sorted(os.listdir(os.path.join(d, "final"))):
            if f.endswith(".md"):
                targets.append(os.path.join(d, "final", f))
    if not targets:
        print(f"没找到简历文件：{d}")
        return

    print(f"体检 {len(targets)} 份简历")
    all_missing = {}
    for f in targets:
        m = check_one(f)
        if m:
            all_missing[os.path.basename(f)] = m

    print()
    print("=" * 74)
    if all_missing:
        print("⚠ 招呼语会提到、但简历里找不到的（改之前先看这些）：")
        for fn, aids in all_missing.items():
            print(f"　{fn}：{aids}")
    else:
        print("✓ 四份简历都能接住招呼语里说的事")


if __name__ == "__main__":
    main()
