# -*- coding: utf-8 -*-
"""
补发简历：扫出「HR 找我们要过简历、但我们还没发」的会话，逐个自动补发。

背景（本人 2026-11-16 拍板）：现有自动投递只发文字招呼语，附件简历这条链
没进主流程。HR 都开口要简历了，附件却一直躺在附件栏没发出去 —— 桶口开得再大，
桶底漏着也白搭。这个脚本就是补桶底：把手里所有「要过简历但没给」的 HR，
挨个把定制简历发过去。

完整流程（本人定的五步）：
  1. 翻对话：读会话列表，找出「HR 要过简历、我们还没发附件」的那些
  2. 定制简历：对每个这样的 HR/岗位，生成对应的定制简历 PDF
  3. 上传：看附件栏满没满（上限 3），满了删最旧一份，再上传新简历
  4. 发：进对话，判断对方是【卡片请求】还是【口头文字要】，各用对应方式发
  5. 循环：把所有「要过简历但没给」的 HR 挨个处理完

用法：
  python resume_follow.py scan           只扫，列出「要简历未给」的清单（只读）
  python resume_follow.py send           真发（逐条走完整五步）
  python resume_follow.py send --dry     演练：走完流程，最后不真发
  python resume_follow.py send --limit 5  只处理前 5 个

依赖（全走 chrome-use 插件，须 BOSS_DRIVER=plugin）：
  - 会话读取：走插件驱动注入接口请求（同 chat_scan 的接口，但用 PD.eval_js）
  - 定制/上传/发简历：复用 resume_send.py
  - 驱动：plugin_driver（chrome-use 插件）
"""

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import plugin_driver as PD
from config_loader import path_of

# 复用 resume_send 的定制简历 / 上传 / 发简历能力
import resume_send

CHAT_URL = "https://www.zhipin.com/web/geek/chat"

# 系统提示里「已发出附件简历」的几种文案（用来判断这个会话我们到底发过没有）
RESUME_SENT_MARKS = [
    "附件简历已发送", "对方已同意", "您的附件简历", "附件简历已发送给对方",
    "已发送给Boss", "已发送给boss",
]

# ─────────────────────────────────────────────────────────────
# 判定「是不是真的要简历」：用正面强词 + 排除否定，不用宽泛的「简历」单字。
# 2026-11-16 实测：只用「简历」会命中「暂不匹配」「暂不合适」这类婉拒句，
# 23 条里混进 10+ 条误判。真·要简历的句子特征是「发/给/分享 + 简历」的
# 祈使动作，婉拒句则是「不匹配/不合适/遗憾/关闭」的否定。
# ─────────────────────────────────────────────────────────────

# 正面强词：明确在「要」简历的动作
ASK_RESUME_STRONG = [
    "发一份", "发份", "发我", "发一下", "发个", "发过来", "发来",
    "简历给我", "简历发", "简历过来", "附件简历", "发简历", "给一份简历",
    "分享一份简历", "分享简历", "推我", "简历推", "发详细", "发份详细",
    "发您的简历", "发你的简历",
]

# 否定词：句子里出现这些，说明是婉拒/拒绝，不是要简历
NEGATE_WORDS = [
    "不匹配", "不合适", "不适合", "不符合", "不一致", "不相符", "不适用",
    "遗憾", "抱歉", "感谢关注", "感谢您的关注", "感谢你的关注", "感谢您对",
    "已关闭", "即将关闭", "收尾", "暂不接收", "暂不", "已招满", "招满",
    "不接收", "不考虑", "祝愿", "祝福", "早日找到", "后续有合适", "暂不匹配",
]


def _wants_resume(last_text):
    """判断对方最后一条是不是真的在要简历。

    规则：命中正面强词（发/给/分享简历的动作），且不含否定词。
    """
    t = (last_text or "")
    if any(w in t for w in NEGATE_WORDS):
        return False
    return any(w in t for w in ASK_RESUME_STRONG)


def _friend_list():
    """读出全部会话。返回 [{company,boss,last,mine,theirs,job_id,uid,...}]"""
    js = """
    (async () => {
      const all = [];
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
    raw = PD.eval_js(js, timeout=60)
    try:
        lst = json.loads(raw or "[]")
    except Exception:
        return []
    out = []
    for x in lst:
        uid = x.get("uid")
        lm = x.get("lastMessageInfo") or {}
        out.append({
            "company": x.get("brandName") or "",
            "boss": x.get("name") or "",
            "boss_title": x.get("title") or "",
            "last": x.get("lastMsg") or (lm.get("showText") or ""),
            "mine": lm.get("toId") == uid if uid else False,
            "theirs": lm.get("fromId") == uid if uid else False,
            "job_id": x.get("encryptJobId") or "",
            "encrypt_boss_id": x.get("encryptBossId") or "",
            "uid": uid,
        })
    return out


def _risk_words():
    """当前页面有没有风控字样（强信号）。返回命中的词或 None。"""
    txt = PD.read_url() or ""
    for w in ["请登录", "扫码登录", "滑动验证", "操作过于频繁",
              "系统检测到异常", "账号存在异常", "请完成验证"]:
        if w in txt:
            return w
    return None


# ─────────────────────────────────────────────────────────────
# 判定：这条会话是不是「要简历但还没给」
# ─────────────────────────────────────────────────────────────

def find_resume_requests(convs):
    """从会话列表筛出「要简历、还没发」的会话。

    判「要简历」：对方最后一条（theirs）里带要简历词。
    判「还没发」：会话最后一条不是系统「已发送附件」提示。
    返回 [(conv, reason)] 清单，reason 是识别依据。
    """
    out = []
    for c in convs:
        last = (c.get("last") or "")
        # 对方最后一条在要简历，而且这条不是系统「已发送」提示
        if c.get("theirs") and _wants_resume(last):
            # 排除「已经发过了」：最后一条若已经是发送成功的系统提示，跳过
            if any(m in last for m in RESUME_SENT_MARKS):
                continue
            out.append((c, last))
    return out


# ─────────────────────────────────────────────────────────────
# 打开指定会话（复用 resume_send 的匹配逻辑）
# ─────────────────────────────────────────────────────────────

def _open_conv(conv, job_title=""):
    """在会话列表里点开目标会话。返回 (True, 说明)/(False, 原因)。

    job_title 传真实岗位名（从 jobs_today 查到的），用于会话列表匹配时
    唯一命中（同一个 HR 常挂多个岗位，靠岗位名区分）。HR 职务不传（会串号）。
    """
    return resume_send._open_chat_by_match(
        boss=conv.get("boss", ""),
        company=conv.get("company", ""),
        job_title=job_title,
    )


# ─────────────────────────────────────────────────────────────
# 判断是「卡片请求」还是「口头要」，并分别发
# ─────────────────────────────────────────────────────────────

# 卡片请求里的「同意」按钮：message-card-wrap 里的 card-btn，文字「同意」
DETECT_CARD_JS = r"""
(function(){
  var cards = document.querySelectorAll('div.message-card-wrap');
  var out = [];
  for (var i = 0; i < cards.length; i++) {
    var card = cards[i];
    var title = '';
    var h3 = card.querySelector('h3.message-card-top-title, .message-card-top-title');
    if (h3) title = (h3.innerText || '').trim();
    var btns = [];
    card.querySelectorAll('.card-btn, span.card-btn, .message-card-buttons span, .message-card-buttons button').forEach(function(b){
      var t = (b.innerText || '').trim();
      if (t) btns.push({text: t, cls: (b.className || '').toString()});
    });
    if (title || btns.length) out.push({title: title.slice(0,60), btns: btns});
  }
  return JSON.stringify(out);
})()
"""

CLICK_AGREE_JS = r"""
(function(){
  var btns = document.querySelectorAll('.card-btn, span.card-btn, .message-card-buttons span, .message-card-buttons button');
  for (var i = 0; i < btns.length; i++) {
    var t = (btns[i].innerText || '').trim();
    if (t === '同意') {
      btns[i].click();
      return 'CLICKED:' + (btns[i].className || '').toString().slice(0, 30);
    }
  }
  return 'NOT_FOUND';
})()
"""


def _detect_card():
    """探测当前会话里有没有「附件简历请求卡片」以及上面的按钮。返回 dict 或 None。"""
    raw = PD.eval_js(DETECT_CARD_JS, timeout=30)
    try:
        cards = json.loads(raw or "[]")
    except Exception:
        return None
    for c in cards:
        # 卡片标题里带「简历」或按钮里有「同意」，都算附件简历请求卡片
        if "简历" in (c.get("title") or "") or any(b.get("text") == "同意" for b in c.get("btns", [])):
            return c
    return None


def _send_card():
    """场景一：点「同意」，把附件简历发出去。返回 (ok, 说明)。"""
    r = PD.eval_js(CLICK_AGREE_JS, timeout=30)
    if r and r.startswith("CLICKED"):
        return True, f"已点同意按钮（{r.split(':',1)[1] if ':' in r else ''}）"
    return False, f"没找到「同意」按钮（探测返回 {r}）"


def _send_via_button(pdf_path):
    """场景二：口头要简历 → 点「发简历」→ 上传 → 发出去。返回 (ok, 说明)。"""
    # 点「发简历」按钮
    r = PD.eval_js(resume_send.SEND_RESUME_CLICK_JS, timeout=30)
    if r != "CLICKED":
        return False, f"没点到「发简历」按钮（{r}）"
    time.sleep(2)
    # 弹窗出现后，调组件方法上传 PDF 并发送
    ok, d = resume_send._upload_via_component(pdf_path, action="confirmAddAction")
    if not ok:
        return False, f"上传失败：{d}"
    return True, f"已上传并触发发送：{d.get('fileName')}"


# ─────────────────────────────────────────────────────────────
# 处理单个会话：定制 → 上传 → 发
# ─────────────────────────────────────────────────────────────

# 岗位名权威来源：jobs_today.json（之前扫描抓过的岗位，含真实岗位名 title）。
# 会话接口的 title 字段是「HR 的职务」（招聘主管/总经理），不是岗位名，不能用。
_JOB_NAME_CACHE = None


def _load_job_name_by_id():
    """把 jobs_today.json 加载成 {encryptJobId: 岗位名}。只加载一次。"""
    global _JOB_NAME_CACHE
    if _JOB_NAME_CACHE is not None:
        return _JOB_NAME_CACHE
    p = path_of("data/jobs_today.json")
    m = {}
    try:
        if os.path.exists(p):
            data = json.load(open(p, encoding="utf-8"))
            for j in data:
                jid = j.get("job_id") or ""
                title = (j.get("title") or "").strip()
                if jid and title:
                    m[jid] = title
    except Exception:
        pass
    _JOB_NAME_CACHE = m
    return m


def _job_title_from_conv(conv):
    """拿这个会话的真实岗位名。

    权威来源是 jobs_today.json（按 encryptJobId 查）。查不到返回空字符串，
    由调用方兜底。绝不用 boss_title（那是 HR 职务，不是岗位名）。
    """
    jid = (conv.get("job_id") or "").strip()
    if not jid:
        return ""
    return _load_job_name_by_id().get(jid, "")


def process_one(conv, last_text, dry=False):
    """处理一个「要简历未给」的会话。返回 (结果, 说明)。结果 ∈ ok/skip/halt"""
    company = (conv.get("company") or "").strip()
    boss = (conv.get("boss") or "").strip()
    jt = _job_title_from_conv(conv)

    # 岗位名是定制简历的关键。查不到真实岗位名（jobs_today 里没有）时，
    # 用「AI产品实习生」兜底 —— 这是用户的主求职方向。但要记清楚是兜底。
    job_title = jt if jt else "AI产品实习生"
    used_fallback = not jt

    # 1. 定制简历
    pdf = resume_send.build_resume(job_title, company, job_type="intern")
    if not pdf:
        return "skip", f"{company}｜定制简历失败（岗位名：{job_title}）"

    # 2. 判附件空间 + 上传（满则删旧）
    ok, d = _ensure_attachment_space(dry)
    if not ok:
        return "skip", f"{company}｜附件空间处理失败：{d}"
    if not dry:
        ok, d = resume_send._upload_via_component(pdf)
        if not ok:
            return "skip", f"{company}｜上传失败：{d}"

    # 3. 打开目标会话（先确保在聊天页；用真实岗位名匹配，防串号）
    if "/web/geek/chat" not in (PD.current_url() or ""):
        PD.open_url(CHAT_URL)
        time.sleep(4)
    ok2, why = _open_conv(conv, job_title=jt)
    if not ok2:
        return "skip", f"{company}｜打开会话失败：{why}"

    # 4. 判断卡片 or 口头
    card = _detect_card()
    if dry:
        mode = "卡片请求（点同意）" if card else "口头要（点发简历）"
        return "dry", f"{company}｜{mode}｜pdf={os.path.basename(pdf)}"

    # 风控再查一次（发之前）
    risk = _risk_words()
    if risk:
        return "halt", f"出现风控「{risk}」，停"

    if card:
        ok3, why3 = _send_card()
    else:
        ok3, why3 = _send_via_button(pdf)

    if not ok3:
        return "skip", f"{company}｜发送失败：{why3}"
    return "ok", f"{company}｜已发（{'点同意' if card else '发简历按钮'}）"


def _ensure_attachment_space(dry=False):
    """确保附件栏有空间：满 3 份就删最旧一份。返回 (ok, 说明)。"""
    # 导航到简历页
    if "/web/geek/resume" not in (PD.current_url() or ""):
        PD.open_url(resume_send.RESUME_URL)
        time.sleep(4)
    cnt = resume_send._get_attach_count()
    if cnt is None:
        return True, "读不到计数，按未满处理"
    cur = cnt.get("cur", 0)
    maxn = cnt.get("max", resume_send.ATTACH_MAX)
    if cur < maxn:
        return True, f"附件栏 {cur}/{maxn}，有空间"
    # 满了，删最旧
    if dry:
        return True, "dry：附件栏已满，跳过删除"
    if not resume_send._delete_first_attachment():
        return False, "删最旧附件失败"
    PD.open_url(resume_send.RESUME_URL)
    time.sleep(3)
    return True, "已删最旧一份"


# ─────────────────────────────────────────────────────────────
# 主入口
# ─────────────────────────────────────────────────────────────

def cmd_scan():
    """只读扫描，列出要简历未给的清单。"""
    print("读会话列表，找「要简历但还没发」的……")
    PD.ensure_boss_page()
    risk = _risk_words()
    if risk:
        print(f"⛔ 出现风控「{risk}」，请先手动处理。")
        return

    convs = _friend_list()
    print(f"读到 {len(convs)} 条会话。")
    reqs = find_resume_requests(convs)
    print(f"\n要简历但还没发：{len(reqs)} 条\n")
    for i, (c, last) in enumerate(reqs, 1):
        print(f"  {i}. {c.get('company','')[:16]}｜{c.get('boss','')[:10]}｜"
              f"{last[:40]}")
    return reqs


def cmd_send(dry=False, limit=None):
    """真发（或演练）。逐条走完整五步。"""
    print("=" * 60)
    print("补发简历流程开始" + ("（演练，不真发）" if dry else "（真发）"))
    print("=" * 60)

    PD.ensure_boss_page()
    risk = _risk_words()
    if risk:
        print(f"⛔ 出现风控「{risk}」，停。")
        return

    convs = _friend_list()
    reqs = find_resume_requests(convs)
    print(f"要简历但还没发：{len(reqs)} 条\n")

    if limit:
        reqs = reqs[:limit]
        print(f"（只处理前 {limit} 条）\n")

    results = {"ok": 0, "skip": 0, "dry": 0, "halt": 0}
    for i, (c, last) in enumerate(reqs, 1):
        print(f"\n[{i}/{len(reqs)}] {c.get('company','')}｜{c.get('boss','')}")
        print(f"  对方说：{last[:50]}")
        res, why = process_one(c, last, dry=dry)
        results[res] = results.get(res, 0) + 1
        print(f"  → {res}：{why}")
        if res == "halt":
            print("⛔ 遇到风控，停止后续所有发送。")
            break

    print("\n" + "=" * 60)
    print(f"处理完成：成功 {results['ok']}，跳过 {results['skip']}，"
          f"演练 {results['dry']}，停机 {results['halt']}")
    print("=" * 60)
    return results


def main():
    p = argparse.ArgumentParser(description="补发简历：要简历未给的 HR 逐个自动发")
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("scan", help="只读扫描，列清单")

    s = sub.add_parser("send", help="真发")
    s.add_argument("--dry", action="store_true", help="演练不真发")
    s.add_argument("--limit", type=int, default=None, help="只处理前 N 条")

    args = p.parse_args()

    if args.cmd == "scan":
        cmd_scan()
    elif args.cmd == "send":
        cmd_send(dry=args.dry, limit=args.limit)
    else:
        p.print_help()


if __name__ == "__main__":
    main()
