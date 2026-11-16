# 替你翻聊天列表：谁回你了、谁没回，自动填回台账
#
# 你原来要做的事：发完第 3 天，打开 BOSS 的聊天列表，一条条翻 100 个对话，
# 看哪个 HR 回了，回来在表格里打个勾。翻 90 个只为确认"还是没回"。
# 这个脚本替你翻：只读、不点任何东西、不发任何消息。
#
# 怎么判断"回没回"：看每个对话的**最后一条消息是谁说的**。
#   最后一条 = 你发的那句招呼语      → 没人回
#   最后一条 = 对方说的，且不是你那句 → 有人回了（把原文摘下来给你看）
#   对不上、看不明白                 → 标"拿不准"，列出来给你人工看
#
# 用法（第一次必须先跑 probe）：
#   python chat_scan.py probe    去聊天页看看能读到什么 → 存原文，不写台账
#   python chat_scan.py scan     读列表 → data/chat_list.json
#   python chat_scan.py match    跟台账对上，打出表格（不写）
#   python chat_scan.py match --apply   确认没问题再写回台账
#   python chat_scan.py follow   列出"发出满 3 天还没回、可以跟进了"的清单

import csv
import json
import os
import re
import sys
import time
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config_loader import path_of
from fetch_cdp import require_tab, evaluate, goto

# 2026-10-28 校准：下面这两个地址是打开聊天页抓真实请求看到的，页面自己就请求它们。
CHAT_URL = "https://www.zhipin.com/web/geek/chat"

# 真实可用的就这一个，其余全是猜的、已删。参数 pageSize 要带上，不带返回空 {}。
FRIEND_API = ("https://www.zhipin.com/wapi/zprelation/friend/"
              "getGeekFriendList.json?page=%d&pageSize=50")

# 接口不通就退回读页面文字。这些是常见的会话条目标签，probe 会告诉你哪个有货。
DOM_CANDIDATES = [
    "li[class*='chat']", "li[class*='friend']", "li[class*='session']",
    "li[class*='item']", "div[class*='chat-item']", "div[class*='session']",
    "ul[class*='list'] li",
]

RISK_WORDS = ["请登录", "扫码登录", "安全验证", "滑动验证", "操作过于频繁"]


# ─────────────────────────────────────────────────────────────
# 小工具
# ─────────────────────────────────────────────────────────────

def norm(s):
    """统一文字：全角转半角、删掉各种隐形空格。跟 filters.py 里那套是同一个理。"""
    s = unicodedata.normalize("NFKC", str(s or ""))
    return re.sub(r"[\s\u3000\u200b\u200c\u200d\ufeff]+", "", s).lower()


def open_chat(tab, wait=5.0):
    goto(tab, CHAT_URL, wait=wait)
    time.sleep(1.5)


def risk_hit(text):
    return [w for w in RISK_WORDS if w in (text or "")]


def page_text(tab):
    return evaluate(tab, "document.body.innerText || ''") or ""


# ─────────────────────────────────────────────────────────────
# 取数：先试接口，不行再读页面
# ─────────────────────────────────────────────────────────────

def try_apis(tab, verbose=True):
    """试真实接口（2026-10-28 已抓包确认），带回前 200 字看结构。"""
    js = """
    (async () => {
      const u = "__URL__";
      try {
        const r = await fetch(u, {credentials: "include"});
        const j = await r.json();
        const d = j.zpData || {};
        const list = d.result || [];
        return JSON.stringify({url: u, code: j.code, n: list.length,
                               keys: list.length ? Object.keys(list[0]) : [],
                               sample: JSON.stringify(list[0] || {}).slice(0, 400)});
      } catch (e) { return JSON.stringify({url: u, error: String(e)}); }
    })()
    """.replace("__URL__", FRIEND_API % 1)
    raw = evaluate(tab, js, timeout=40)
    try:
        item = json.loads(raw or "{}")
    except Exception:
        return None, []
    good = item if (item.get("code") == 0 and item.get("n")) else None
    if verbose:
        if item.get("error"):
            print(f"　× {item['error'][:60]}")
        elif good:
            print(f"　✓ 通了：{item['n']} 条会话")
            print(f"　字段：{', '.join(item['keys'][:18])}")
        else:
            print(f"　× code={item.get('code')} 返回为空（多半要带 pageSize，或没登录）")
    return good, [item]


def try_dom(tab, verbose=True):
    """接口全不通就退回读页面。把候选标签试一遍，返回抓到的条目文字。"""
    js = """
    (() => {
      const sels = __SELS__;
      const out = [];
      for (const s of sels) {
        const els = Array.from(document.querySelectorAll(s));
        const txt = els.map(e => (e.innerText || '').replace(/\\n+/g, ' | ').trim())
                       .filter(t => t.length > 3);
        out.push({sel: s, n: txt.length, sample: txt.slice(0, 5)});
      }
      return JSON.stringify(out);
    })()
    """
    raw = evaluate(tab, js.replace("__SELS__", json.dumps(DOM_CANDIDATES, ensure_ascii=False)))
    try:
        return json.loads(raw or "[]")
    except Exception:
        return []


# ─────────────────────────────────────────────────────────────
# 命令 1：probe —— 只探，不写任何东西
# ─────────────────────────────────────────────────────────────

def cmd_probe(tab):
    print("去聊天页看看能读到什么。全程只读，不点任何东西、不发任何消息。")
    print()
    open_chat(tab)

    txt = page_text(tab)
    rk = risk_hit(txt)
    if rk:
        print(f"⛔ 页面上出现：{'、'.join(rk)}")
        print("　停。请你在那个浏览器里手动处理完，再跑一次。")
        return
    if len(txt.strip()) < 80:
        print("⚠ 页面几乎是空的。可能没登录，也可能聊天页地址变了。")
        print("　你自己打开「消息」页面，把地址栏网址发我，我换成真实地址。")

    os.makedirs(path_of("data/raw"), exist_ok=True)
    p = path_of("data/raw/chat_page.txt")
    with open(p, "w", encoding="utf-8") as f:
        f.write(txt)
    print(f"整页原文已存 → {p}（{len(txt)} 字）")
    print()

    print("[1] 试接口（最准，能用就用它）")
    good, items = try_apis(tab)
    print()
    print("[2] 试读页面结构（接口不通用这个）")
    doms = try_dom(tab)
    for d in doms:
        flag = "✓" if d.get("n") else "×"
        print(f"　{flag} {d['sel']}　抓到 {d.get('n', 0)} 条")
        if d.get("n"):
            for s in d["sample"][:3]:
                print(f"　　　· {s[:80]}")
    print()

    p2 = path_of("data/raw/chat_probe.json")
    with open(p2, "w", encoding="utf-8") as f:
        json.dump({"api": items, "dom": doms}, f, ensure_ascii=False, indent=2)
    print(f"探测结果已存 → {p2}")
    print()
    print("把 data/raw/chat_probe.json 和 chat_page.txt 发我，")
    print("我照着真实结构写解析规则，之后 scan / match 就能全自动。")


# ─────────────────────────────────────────────────────────────
# 命令 2：scan —— 读出会话列表
# ─────────────────────────────────────────────────────────────

def parse_from_api(tab):
    """读会话列表。字段名是 2026-10-28 抓真实数据核过的，不是猜的。

    核心判断：lastMessageInfo.toId == 这个人的 uid → 最后一条是我们发的（对方还没回）
              lastMessageInfo.fromId == 这个人的 uid → 最后一条是对方发的（有回应）
    为什么这么判：一条会话里只有两个人的 id，我发出去的消息一定是"发给他"，
    所以他回的消息一定是"他发出去"。104 条实测全部能判，没有"判不出来"的。
    """
    js = """
    (async () => {
      const all = [];
      // 2026-11-12 改：不再写死 5 页，翻到返回不足 50 条才停（= 拉全）。
      // 之前 5 页封顶 250 条，而真实「沟通过」有 497 条，后半截被漏掉。
      // 加保险丝：最多 30 页（1500 条），防接口异常时无限翻页。
      for (let p = 1; p <= 30; p++) {
        const u = "https://www.zhipin.com/wapi/zprelation/friend/"
                  + "getGeekFriendList.json?page=" + p + "&pageSize=50";
        const r = await fetch(u, {credentials: "include"});
        const j = await r.json();
        const res = (j.zpData && j.zpData.result) || [];
        all.push(...res);
        if (res.length < 50) break;
      }
      return JSON.stringify(all);
    })()
    """
    raw = evaluate(tab, js, timeout=60)
    try:
        lst = json.loads(raw or "[]")
    except Exception:
        return None
    if not lst:
        return None

    out = []
    for x in lst:
        uid = x.get("uid")
        lm = x.get("lastMessageInfo") or {}
        mine = lm.get("toId") == uid if uid else False
        theirs = lm.get("fromId") == uid if uid else False
        out.append({
            "boss": x.get("name") or "",
            "boss_title": x.get("title") or "",
            "company": x.get("brandName") or "",
            "last": x.get("lastMsg") or (lm.get("showText") or ""),
            "time": x.get("lastTime") or "",
            "ts": x.get("lastTS") or lm.get("msgTime") or 0,
            "unread": x.get("unreadMsgCount") or 0,
            "mine": mine,
            "theirs": theirs,
            "encrypt_job_id": x.get("encryptJobId") or "",
            "encrypt_boss_id": x.get("encryptBossId") or "",
            "uid": uid,
        })
    return out


def cmd_scan(tab):
    print("读聊天列表。全程只读。")
    print()
    open_chat(tab)
    txt = page_text(tab)
    rk = risk_hit(txt)
    if rk:
        print(f"⛔ 出现：{'、'.join(rk)} —— 停，请你手动处理完再来。")
        return

    rows = parse_from_api(tab)
    if rows:
        print(f"接口读到了 {len(rows)} 个对话")
    else:
        print("接口没读到，退回读页面文字（可能没那么准，先给你看）")
        rows = []
        for line in txt.split("\n"):
            line = line.strip()
            if len(line) >= 4:
                rows.append({"boss": "", "company": "", "last": line,
                             "time": "", "unread": 0, "mine": False})

    p = path_of("data/chat_list.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    print(f"已存 → {p}")
    print()
    for r in rows[:15]:
        who = f"{r['boss']}（{r['company']}）" if r["boss"] else "（页面行）"
        tail = r["last"][:46]
        print(f"　· {who}　{tail}")
    if len(rows) > 15:
        print(f"　……还有 {len(rows)-15} 条")
    print()
    print("下一步：跑 python chat_scan.py match 跟台账对上。")


# ─────────────────────────────────────────────────────────────
# 命令：import-history —— 把聊天历史倒回台账
#
# 为什么要这一步：台账是空的（机器一条没发过），但你手动已经聊了 100 多个。
# 这会带来两个问题：
#   ① 不知道"以前的招呼语到底有多少人回"，基线得从 0 开始攒；
#   ② 机器不知道你已经跟谁聊过，可能会重复打招呼 —— 同一个 HR 看到同一个
#      人来第二次，比没打过招呼还糟。
# 所以把历史倒回来：既补基线，也补去重名单。
#
# 用法：python chat_scan.py import-history
# ─────────────────────────────────────────────────────────────

# 对方回话的分类。规则来自 2026-10-28 真实数据里能看到的原话，
# 分不出来的就标"待看"，列给人 —— 机器的判断永远替不了你自己看一眼。
POLITE_NO = ["并不完全匹配", "并不完全吻合", "不完全匹配", "不太合适", "不太匹配",
             "不太吻合", "抱歉", "遗憾", "再去,", "后续有合适机会", "有机会再合作",
             "不够", "不匹配", "暂时不合适", "暂时没有", "暂时不符", "暂无合适",
             "位置已满", "已经招到", "谢谢你", "谢谢关注", "谢谢您的关注",
             "祝您早日找到", "岗位已关闭", "已停止招聘"]
AUTO_MSG = ["简历已顺利抵达人才库", "感谢您的投递", "人才库", "自动回复",
            "對方已同意", "对方已同意", "附件简历已发送"]
SPAM_NO = ["是否正为寻找一份理想工作", "快来和我聊聊", "我们正在寻觅"]
GOOD_YES = ["发一份简历", "发一份你的简历", "发份简历", "简历给我", "您的简历过来",
            "加您微信", "加个微信", "微信方便吗", " AI 视频面试", "视频面试",
            "面试邀请", "感兴趣聊聊", "感兴趣的话", "还在看机会", "方便的话聊聊",
            "约一下", "继续沟通", "什么时候方便", "方便沟通吗", "电话沟通",
            "可以聊聊", "发过来看", "投递简历过来", "详细介绍一下", "做个笔试",
            "安排面试", "约面试"]


# ── 2026-11-11 本人拍板：把「要简历」单独提出来当一个最高优先级的信号。
#   判定标准只有一条 —— HR 找你要简历，就算这条招呼语达到了目标（好回复）。
#   这比原来「真有兴趣 / 客套婉拒」的语义分类更干净：要简历是硬动作，不是软态度。
#   为什么单列：它是 A/B 测「哪个变体最好」的直接打分依据（要简历=1，其余=0），
#   必须和其它分类分开，不能混在「真有兴趣」里被稀释。
ASK_RESUME = ["简历", "发份", "发一份", "发我", "附上简历", "附件简历",
              "发过来看", "投递简历过来", "简历给我", "您的简历", "看看简历",
              "发下简历", "发个简历", "把简历", "先看看简历", "发简历"]


# ── 2026-11-11 本人拍板：把「好回复」定义成一个档，而不是只认「要简历」一种。
#   实际 HR 的意向表达有好几种，都比「客套婉拒」硬，都该算好回复：
#     ① 直接要简历（ASK_RESUME）
#     ② 约面试 / 约时间聊聊（"我们约个时间""安排面试""视频面试"）
#     ③ 追问到岗 / 到岗时间 / 实习时长（"什么时候能到岗""能实习多久"）
#   这三种都是 HR 愿意往下推进的硬信号，跟「谢谢不考虑」是两回事。
#   所以 A/B 打分的唯一依据从「要简历」扩成「好回复」：上面任一种命中 = 1。
GOOD_EXTRA = ["约个时间", "约时间", "约一下", "安排面试", "约面试", "面试邀请",
              "视频面试", "AI视频面试", "什么时候能到岗", "何时到岗", "能到岗",
              "什么时候到岗", "到岗时间", "能实习多久", "实习多久", "实习时长",
              "什么时候方便", "方便沟通", "电话沟通", "加您微信", "加个微信",
              "微信方便吗", "做个笔试", "继续沟通", "可以聊聊", "详细介绍一下"]


def classify_reply(text):
    """一句话把它归类：要简历 / 真有兴趣 / 客套婉拒 / 系统自动回复 / 广告 / 待看"""
    t = text or ""
    # 系统自动回复优先于一切：BOSS 会在 HR 收到简历时自动弹「简历已顺利抵达人才库」
    # 这类话，里面也带「简历」二字，若不先排除，会被下面的「要简历」误判成好回复。
    if any(w in t for w in AUTO_MSG):
        return "系统通知"
    # 最高优先级：HR 要简历 = 好回复（本人 2026-11-11 定，A/B 归因的唯一目标信号）
    if any(w in t for w in ASK_RESUME):
        return "要简历"
    if any(w in t for w in GOOD_YES):
        return "真有兴趣"
    if any(w in t for w in POLITE_NO):
        return "客套婉拒"
    if any(w in t for w in SPAM_NO):
        return "招聘方广告"
    if any(w in t for w in AUTO_MSG):
        return "系统通知"
    return "待看"


def classify_good(text):
    """判断这条 HR 回复是不是「好回复」—— A/B 测「哪个变体最好」的唯一打分依据。

    好回复 = 要简历 / 约面试 / 追问到岗等明确意向（=1）；其余（客套婉拒/广告/待看/
    系统通知）=0。比原来只认「要简历」宽一档，但比「任意回应」严：客套婉拒不算。
    """
    t = text or ""
    # 系统自动回复（"简历已抵达人才库"）先排除，别让机器自己的动作被算成好回复
    if any(w in t for w in AUTO_MSG):
        return False
    if any(w in t for w in ASK_RESUME):
        return True
    if any(w in t for w in GOOD_EXTRA):
        return True
    # GOOD_YES 里「发一份简历」这类其实和 ASK_RESUME 重叠，但「约面试」等意向词
    # 也在这里，一并算好回复，避免漏掉没进 GOOD_EXTRA 的意向表达。
    if any(w in t for w in GOOD_YES):
        return True
    return False


def cmd_import_history(tab, days=60):
    rows = parse_from_api(tab)
    if not rows:
        print("读不到会话列表。")
        return

    now = time.time() * 1000
    recent = [r for r in rows if (r.get("ts") or 0) > 0
              and (now - r["ts"]) / 86400000 <= days]

    ours = [r for r in recent if r.get("mine")]
    theirs = [r for r in recent if r.get("theirs")]

    print(f"{days} 天内的会话 {len(recent)} 条")
    print(f"　最后一条是我们发的（对方没回）：{len(ours)} 条")
    print(f"　最后一条是对方发的（有回应）　：{len(theirs)} 条")
    print()

    # 对方那 22 条到底是什么意思 —— 这才是有用的信息
    buckets = {}
    for r in theirs:
        k = classify_reply(r.get("last") or "")
        buckets.setdefault(k, []).append(r)
    print("对方回话的性质：")
    for k, v in sorted(buckets.items(), key=lambda x: -len(x[1])):
        print(f"　{k}：{len(v)} 条")
    print()
    for k, v in buckets.items():
        print(f"【{k}】")
        for r in v[:8]:
            print(f"　· {r['company'][:12]}｜{(r.get('last') or '')[:52]}")
        if len(v) > 8:
            print(f"　……还有 {len(v)-8} 条")
        print()

    # 写台账：只写"我们主动打招呼、而且没内容会串"的那些
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import run as _run
    p = path_of("data/greeting_ledger.csv")
    have = set()
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            have = {(row.get("job_id") or "") for row in csv.DictReader(f)}

    added = 0
    with open(p, "a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=_run.FIELDS)
        if os.path.getsize(p) == 0:
            w.writeheader()
        for r in ours:
            jid = r.get("encrypt_job_id") or ""
            if jid in have:
                continue
            have.add(jid)
            t = time.strftime("%Y-%m-%d %H:%M:%S",
                              time.localtime((r["ts"] or 0) / 1000))
            w.writerow({
                "send_time": t, "job_id": jid, "company": r.get("company", ""),
                "city": "", "title": r.get("boss_title", ""), "family": "",
                "score": "", "salutation_variant": "", "closing_variant": "",
                "word_count": len(r.get("last") or ""), "has_jd_anchor": "",
                "greeting_text": r.get("last", ""), "role_cluster": "",
                "read_24h": "", "read_72h": "", "replied": "", "ask_resume": "",
                "notes": f"历史导入｜HR：{r.get('boss','')}",
                "sent": "已发", "verify": "", "followup_sent": "", "followup_time": "",
                "followup_text": "", "source": "历史",
            })
            added += 1
        # 对方回过的也写一条，replied 标记上，这样基线才算得出来
        for r in theirs:
            jid = r.get("encrypt_job_id") or ""
            if jid in have:
                continue
            have.add(jid)
            t = time.strftime("%Y-%m-%d %H:%M:%S",
                              time.localtime((r["ts"] or 0) / 1000))
            kind = classify_reply(r.get("last") or "")
            w.writerow({
                "send_time": t, "job_id": jid, "company": r.get("company", ""),
                "city": "", "title": r.get("boss_title", ""), "family": "",
                "score": "", "salutation_variant": "", "closing_variant": "",
                "word_count": "", "has_jd_anchor": "",
                "greeting_text": "", "role_cluster": "",
                "read_24h": "", "read_72h": "",
                "replied": "1",
                "ask_resume": "1" if classify_good(r.get("last") or "") else "0",
                "notes": f"历史导入｜{kind}｜{(r.get('last') or '')[:40]}",
                "sent": "已发", "verify": "", "followup_sent": "", "followup_time": "",
                "followup_text": "", "source": "历史",
            })
            added += 1
    print(f"台账里写进去 {added} 条（source=历史）→ data/greeting_ledger.csv")
    print()
    print("提醒：这些是**倒推**出来的，不是机器当天发的。")
    print("　· send_time 是这条会话最后一次动的时间，不是我们发招呼语的时间")
    print("　· 岗位名写的是 HR 的职务（比如'招聘经理'），不是岗位，因为我们没抓那些岗位的 JD")
    print("　· 算回复率时用 source=历史 的这些；机器自己发的会标 source=机器")


# ─────────────────────────────────────────────────────────────
# 命令：import-contacted —— 全量拉「沟通过」岗位，全写「已投」
# ─────────────────────────────────────────────────────────────
# 2026-11-12 本人拍板：BOSS 后台「沟通过」= 我主动发出过招呼语、建立了会话的岗位，
# 就是「投递过」的意思（本人默认主动找我的不会去）。所以把沟通过列表全量拉回来，
# 全部写成台账 sent=已发，作为「不重复投递」的权威判重依据。
#
# 跟 cmd_import_history 的区别：
#   - 不限 60 天，全量（拉到底为止）
#   - 不看 mine/theirs（谁回没回都算投过）
#   - 只要 encrypt_job_id 非空就写；空的退化成公司名写进 company_seen.txt 兜底

def cmd_import_contacted(tab):
    rows = parse_from_api(tab)
    if not rows:
        print("读不到会话列表（可能没登录，或接口又变了）。")
        return

    print(f"接口拉回 {len(rows)} 条会话（沟通过的全部）。")
    print()

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import run as _run

    p = path_of("data/greeting_ledger.csv")
    have = set()
    if os.path.exists(p) and os.path.getsize(p) > 0:
        with open(p, encoding="utf-8") as f:
            have = {(row.get("job_id") or "").strip() for row in csv.DictReader(f)}

    added = 0
    skipped_dup = 0
    no_jid = 0
    no_jid_companies = []

    # 先全量内存里过一遍，再一次性追加，避免反复开关文件
    new_rows = []
    for r in rows:
        jid = (r.get("encrypt_job_id") or "").strip()
        company = (r.get("company") or "").strip()
        if not jid:
            no_jid += 1
            if company:
                no_jid_companies.append(company)
            continue
        if jid in have:
            skipped_dup += 1
            continue
        have.add(jid)
        ts = (r.get("ts") or 0)
        t = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts / 1000)) if ts else ""
        new_rows.append({
            "send_time": t, "job_id": jid, "company": company,
            "city": "", "title": r.get("boss_title", ""), "family": "",
            "score": "", "salutation_variant": "", "closing_variant": "",
            "word_count": len(r.get("last") or ""), "has_jd_anchor": "",
            "greeting_text": "", "role_cluster": "",
            "read_24h": "", "read_72h": "", "replied": "", "ask_resume": "",
            "notes": f"沟通过导入｜HR：{r.get('boss','')}",
            "sent": "已发", "verify": "", "followup_sent": "", "followup_time": "",
            "followup_text": "", "source": "沟通过",
        })

    if new_rows:
        with open(p, "a", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=_run.FIELDS)
            if os.path.getsize(p) == 0:
                w.writeheader()
            w.writerows(new_rows)
    added = len(new_rows)

    # 无 job_id 的：退化成公司名，写进 company_seen.txt（判重的第二道兜底）
    seen_file = path_of("data/company_seen.txt")
    seen_companies = set()
    if os.path.exists(seen_file):
        with open(seen_file, encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if s and not s.startswith("#"):
                    seen_companies.add(s)
    seen_added = 0
    with open(seen_file, "a", encoding="utf-8") as f:
        for c in no_jid_companies:
            if c and c not in seen_companies:
                f.write(c + "\n")
                seen_companies.add(c)
                seen_added += 1

    print(f"台账新增 {added} 条（source=沟通过，sent=已发）。")
    print(f"　重复跳过（job_id 已在台账）{skipped_dup} 条")
    print(f"　无 job_id（岗位已下架/接口没给）{no_jid} 条"
          f"　其中 {seen_added} 个公司名已兜底写进 company_seen.txt")
    print()
    print("提醒：这些是「沟通过=投递过」的权威账本，比本地台账更全。")
    print("　以后判重会优先认 job_id；查无 job_id 的靠公司名兜底。")

# ─────────────────────────────────────────────────────────────
# 命令 3：match —— 跟台账对上，判断回没回
# ─────────────────────────────────────────────────────────────

def load_ledger():
    p = path_of("data/greeting_ledger.csv")
    if not os.path.exists(p):
        return [], None
    with open(p, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return rows, p


def judge(row, chats):
    """判断这条台账有没有回复。返回 (结论, 依据)

    判定顺序（2026-11-05 改，优先用接口的 mine/theirs 字段，不再只靠字符串匹配）：
      1. 公司名对上 → 定位到对话
      2. 接口字段 theirs=True（最后一条是对方发的）→ 有回复，摘对方原话
      3. 接口字段 mine=True（最后一条是我们发的）→ 没回
      4. 字段缺失才退回「招呼语前 12 字是否还在最后一条里」兜底
    为什么要优先用 mine/theirs：之前只比字符串，我方自己发的跟进话术
    （"打扰一下补个更直接的点…"）会被当成对方回复，正向回复率被污染。
    mine/theirs 是接口给的、最权威的"最后一条谁发的"信号，104 条实测全判得出来。
    """
    mine_head = (row.get("greeting_text") or "").strip()
    head = mine_head[:12]
    cands = []
    for c in chats:
        blob = norm((c.get("company") or "") + (c.get("boss") or "") + (c.get("last") or ""))
        if norm(row.get("company") or "") and norm(row["company"]) in blob:
            cands.append(c)
    if not cands:
        return "拿不准", "聊天列表里没找到这家公司的对话"

    if len(cands) > 1:
        # 同一公司好几个对话：先看有没有 mine/theirs 明确对上的
        for c in cands:
            if c.get("theirs"):
                return "有回复", (c.get("last") or "")[:60]
        for c in cands:
            if c.get("mine") and head and norm(head) in norm(c.get("last") or ""):
                return "没回", f"最后一条还是你发的那句（找到 {len(cands)} 个对话，取对得上的那个）"
        return "拿不准", f"这家公司有 {len(cands)} 个对话，对不上你发的是哪句"

    c = cands[0]
    last = (c.get("last") or "").strip()
    # 优先用接口字段：对方发的 → 有回复；我们发的 → 没回
    if c.get("theirs"):
        return "有回复", last[:60] if last else "对方回了（内容没读到）"
    if c.get("mine"):
        return "没回", "最后一条是你发的（接口 mine 字段判定）"
    # 字段缺失，退回字符串匹配兜底
    if head and norm(head) in norm(last):
        return "没回", "最后一条还是你发的那句"
    if last:
        return "有回复", last[:60]
    return "拿不准", "没读到最后一条消息"


def cmd_match(tab, apply=False):
    rows, path = load_ledger()
    if not rows:
        print("台账是空的（一行都没有），没东西可对。")
        print("先发出去几条，过两天再来跑这个。")
        return

    chats = []
    p = path_of("data/chat_list.json")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            chats = json.load(f)
    if not chats:
        print("还没读过聊天列表，先去读一次……")
        cmd_scan(tab)
        with open(p, encoding="utf-8") as f:
            chats = json.load(f)

    print(f"台账 {len(rows)} 条　聊天列表 {len(chats)} 条")
    print()
    n_reply = n_none = n_unknown = 0
    results = []
    for r in rows:
        # 台账 sent 字段真实取值是「已发/未发」，不是「1/True」（2026-11-05 修）
        if (r.get("sent") or "").strip() != "已发":
            continue
        verdict, why = judge(r, chats)
        results.append((r, verdict, why))
        if verdict == "有回复":
            n_reply += 1
        elif verdict == "没回":
            n_none += 1
        else:
            n_unknown += 1

    print("判定结果：")
    print(f"　有回复 {n_reply} 条　没回 {n_none} 条　拿不准 {n_unknown} 条")
    print()
    for r, v, why in results:
        mark = {"有回复": "★", "没回": "·", "拿不准": "?"}[v]
        print(f" {mark} [{v}] {r.get('company','')[:16]}｜{r.get('title','')[:18]}")
        print(f"　　依据：{why}")

    if not apply:
        print()
        print("（上面只是算给你看，台账一个字没改。要写回去就加 --apply）")
        return

    for r, v, why in results:
        if v == "有回复":
            r["replied"] = "1"
            r["read_72h"] = "1"
            # 2026-11-05 修：把对方原话喂给 classify_reply，把「真有兴趣/客套婉拒/…」
            # 标签写进 notes。之前只写「自动判定有回复」，evaluate.py 靠 notes 里
            # 的「真有兴趣」算正向回复率，结果机器发的记录恒为 0（断链）。
            kind = classify_reply(why)
            r["notes"] = (r.get("notes", "") + f" |{kind}：{why[:40]}").strip(" |")
            # 2026-11-11 新增：好回复档单独落一列，供 A/B 按变体算「要简历/意向率」。
            #   好回复=1（要简历/约面试/追问到岗），其余=0。不再靠 notes 里 grep。
            r["ask_resume"] = "1" if classify_good(why) else "0"
        elif v == "没回":
            r["replied"] = "0"
            r["read_72h"] = "0"
            r["ask_resume"] = "0"
        else:
            r["notes"] = (r.get("notes", "") + " |待人工确认：" + why[:40]).strip(" |")

    with open(path, "w", encoding="utf-8", newline="") as f:
        # 2026-11-12 修：表头统一用 run.FIELDS（唯一权威），不再用 rows[0].keys()。
        # 之前 rows 是从磁盘读的，旧文件表头缺新列（variant_tags/ask_resume）时，
        # 写回会字段漂移，把大量行冲掉。
        import run as _run
        w = csv.DictWriter(f, fieldnames=_run.FIELDS)
        w.writeheader()
        w.writerows(rows)
    print()
    print(f"已写回台账 → {path}")
    print(f"　有回复 {n_reply} 条标成 1，没回 {n_none} 条标成 0，"
          f"拿不准 {n_unknown} 条留在备注里等你人工看。")


# ─────────────────────────────────────────────────────────────
# 命令 4：follow —— 谁可以跟进了
# ─────────────────────────────────────────────────────────────

def cmd_follow():
    rows, _ = load_ledger()
    if not rows:
        print("台账是空的。")
        return
    todo = []
    for r in rows:
        if (r.get("sent") or "").strip() != "已发":
            continue
        if (r.get("replied") or "").strip() == "1":
            continue
        if (r.get("followup_sent") or "").strip():
            continue
        todo.append(r)
    print(f"发出去还没回、也没跟进过的：{len(todo)} 条")
    print()
    now = time.time()
    for r in todo:
        st = (r.get("send_time") or "")[:16]
        print(f"　· {st}　{r.get('company','')[:16]}｜{r.get('title','')[:20]}")
    if todo:
        print()
        print("这 {n} 条就是跟进候选。等你确认话术后我再动手发。".format(n=len(todo)))
    print()
    print("提示：跟进只对『发出满 3 天还没回』的发，同一家最多跟一次。")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "probe"
    apply = "--apply" in sys.argv

    if cmd == "follow":          # 只看台账，不用连浏览器
        cmd_follow()
        return

    tab = require_tab()
    if not tab:
        sys.exit(1)

    if cmd == "probe":
        cmd_probe(tab)
    elif cmd == "scan":
        cmd_scan(tab)
    elif cmd == "match":
        cmd_match(tab, apply)
    elif cmd == "import-history":
        cmd_import_history(tab)
    elif cmd == "import-contacted":
        cmd_import_contacted(tab)
    else:
        print("用法：python chat_scan.py probe | scan | match [--apply] | follow | import-history | import-contacted")


if __name__ == "__main__":
    main()
