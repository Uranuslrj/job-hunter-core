"""把新版的打招呼语在真实岗位数据上全跑一遍，看整体质量。

从任意目录都能跑：路径按脚本自己的位置算。
"""
import sys, io, json, re, collections, os
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import filters, greeting, scorer

jobs = json.load(io.open(os.path.join(ROOT, 'data', 'jobs_today.json'),
                 encoding='utf-8'))
print('总岗位数:', len(jobs))

ok = 0
fail = collections.Counter()
chars = []
npts = collections.Counter()
samples = []

# 讨好奉承词检查（2026-10-29 晚本人定的规则：动机要理性，不许"真的很想去"）
BAN = [b for b in ((greeting.MOTIV.get('flattery_ban') or {}).get('forbid') or []) if b]
flattery = collections.Counter()
motiv_lines = collections.Counter()

# 软技能统一表述检查（2026-10-29 晚本人定的规则，见 yaml 的 soft_skill_narrative）
SSN = greeting.SPEC.get('soft_skill_narrative') or {}
CHK = SSN.get('checks') or {}
TRIG = [t for t in (CHK.get('trigger_words') or []) if t]
CHAIN_M = CHK.get('chain_markers') or {}
# ⛔ 2026-10-29 本人拍板：<经历A> / <经历B> 全禁，任何场景都不许出现
FORBID = [f for f in (CHK.get('forbid_always') or CHK.get('forbid_when_triggered') or []) if f]
psy_used = 0          # 用上心理学素材的条数
chain_broken = []     # 用了但因果链缺环的
wrong_evidence = []   # 触发了软技能却拿<经历A>/<经历B>顶替的
num_drift = []        # 数据被改写的（写成"近300"之类）
risk_counter = collections.Counter()   # 风控敏感词命中计数
risk_samples = []     # 命中风控敏感词的样例（公司+岗位+词）

for j in jobs:
    a = filters.layer1(j)
    if not a.get('passed'):
        continue
    j = dict(j)
    j['family'] = a.get('family')
    s = scorer.score_job(j, a)
    j['score'] = s.get('score')
    g = greeting.draft(j)
    if not g:
        fail['生成不出来'] += 1
        continue
    chars.append(g['chars'])
    npts[len(g.get('jd_points') or [])] += 1
    txt = g['text'] or ''
    for b in BAN:
        if b in txt:
            flattery[b] += 1
    for seg in txt.split('。'):
        if '最主要的是' in seg:
            motiv_lines[seg.strip()] += 1
    # ── 软技能统一表述（本人 2026-10-29 规则）──
    has_psy = '心理学' in txt
    if has_psy:
        psy_used += 1
        miss = [name for name, marks in CHAIN_M.items()
                if not any(m in txt for m in marks)]
        if miss:
            chain_broken.append((j.get('company', ''), ','.join(miss)))
        # 冻结数据只能原样出现，被写成别的数字就是改写
        # ⚠ 开源版脱敏：换成你自己的「不可改写」的简历关键数据
        for frozen in ('<数据1>', '<数据2>', '<数据3>'):
            if frozen not in txt and re.search(r'\d+\s*%', txt):
                num_drift.append((j.get('company', ''), frozen))
                break
    # 全禁检查：不分场景，出现就是违规
    for f in FORBID:
        if f in txt:
            wrong_evidence.append((j.get('company', ''), f))
            break

    # ── 风控敏感词（2026-10-31 加，对应 fengkong-solutions.md 3.1/3.2）──
    # 命中就说明这条招呼语会触发平台风控，必须查。跟上面的讨好词分开统计，
    # 因为后果不同：讨好词是"减分"，敏感词是"可能封号"。
    RISK_HARD = greeting.RISK_SENSITIVE_HARD
    RISK_OFF = greeting.RISK_SENSITIVE_OFFPLATFORM
    risk_hits = [w for w in RISK_HARD + RISK_OFF if w in txt]
    if risk_hits:
        risk_counter.update(risk_hits)
        risk_samples.append((j.get('company', ''), j.get('title', ''), ','.join(risk_hits)))
    if g.get('ok'):
        ok += 1
    else:
        for r in g.get('reasons') or []:
            fail[r] += 1
    if len(samples) < 4 and j.get('score', 0) >= scorer.THRESH["strong"]:
        samples.append((j.get('title'), j.get('company'), j.get('score'), g['text']))

print('过门:', ok, '/', len(chars))
print('平均字数:', round(sum(chars) / max(1, len(chars)), 1))
print('能力条数分布:', dict(npts))
print()
print('--- 不过门的原因 ---')
for r, c in fail.most_common():
    print(f'  {c:>3}  {r}')

print()
print(f'--- 讨好奉承词命中（应为 0，共查 {len(BAN)} 个禁用词）---')
if flattery:
    for b, c in flattery.most_common():
        print(f'  ✗ {c:>3}  {b}')
else:
    print('  ✓ 0 命中')

print()
print('--- 动机句重复度（同一句出现几次）---')
for s_, c in motiv_lines.most_common(5):
    print(f'  {c:>3}×  {s_[:60]}')
if len(motiv_lines) and max(motiv_lines.values()) > len(chars) * 0.4:
    print('  ⚠ 有一句占了多数 —— 模板感强，可考虑换角度或按岗位细分')

print()
print('--- 软技能统一表述（本人 2026-10-29 规则）---')
print(f'  用上心理学素材: {psy_used} 条')
if chain_broken:
    print(f'  ✗ 因果链缺环 {len(chain_broken)} 条:', chain_broken[:5])
else:
    print('  ✓ 因果链四环完整（专业背景→洞察需求→转化为产品方案→数据验证）')
if num_drift:
    print(f'  ✗ 数据被改写 {len(num_drift)} 条:', num_drift[:5])
else:
    print('  ✓ 冻结数据原样出现（<数据1> / 约 <数据2> / 约 <数据3>）')
if wrong_evidence:
    print(f'  ✗ 出现已全禁的<经历A>/<经历B> {len(wrong_evidence)} 条:', wrong_evidence[:5])
else:
    print('  ✓ <经历A>/<经历B> 0 出现（本人 2026-10-29 拍板全禁）')

print()
print('--- 风控敏感词（应为 0，命中即可能触发封号）---')
if risk_counter:
    print(f'  ✗ 命中风控敏感词 {sum(risk_counter.values())} 次:', dict(risk_counter))
    for c, t, w in risk_samples[:5]:
        print(f'     {c}｜{t}｜{w}')
else:
    print('  ✓ 0 命中（无兼职/刷单/引流/加微信等风控词）')

print()
print('--- 高分岗位样例 ---')
for t, c, s, txt in samples:
    print(f'[{s}] {t} @ {c}')
    print('   ', txt)
    print()
