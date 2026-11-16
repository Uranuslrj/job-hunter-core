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

# 发简历的范围（本人 2026-11-16 新增要求）：
# 只发「我主动打过招呼（投过）」的岗位，HR 要简历才发；
# 对方自己找上门、我没主动打招呼的要简历，不发。
# 判断依据 = 台账 greeting_ledger.csv 里 sent='已发' 的 job_id 集合，
# 见 find_resume_requests 里的 _greeted_job_ids()。

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

def _greeted_job_ids():
    """读台账，返回「我已经主动打过招呼（sent=已发）」的 job_id 集合。

    这是「我主动投过没」的权威依据：只有这些岗位，HR 要简历才发；
    对方自己找上门、job_id 不在这个集合里的，不发。
    """
    import csv as _csv
    ledger = path_of("data/greeting_ledger.csv")
    ids = set()
    try:
        with open(ledger, encoding="utf-8") as f:
            for row in _csv.DictReader(f):
                if (row.get("sent") or "").strip() == "已发":
                    jid = (row.get("job_id") or "").strip()
                    if jid:
                        ids.add(jid)
    except FileNotFoundError:
        pass
    return ids


def find_resume_requests(convs):
    """从会话列表筛出「要简历、还没发」的会话。

    两路判定（2026-11-16 补）：
      1. 文字路：对方最后一条（theirs）带要简历词 → 直接命中。
      2. 空消息路：对方最后发言（theirs=True）但 lastMsg 为空 → 卡片式请求在
         列表接口里不返回文字（闪念科技实测），必须点进对话页补查。

    2026-11-16 新增（本人拍板）：只发「我主动打过招呼」的岗位。
    —— HR 自己找上门、我没主动打招呼（job_id 不在台账「已发」集合里）的，
    哪怕对方要简历也不发。判断依据 = 台账 sent='已发' 的 job_id 集合。
    返回 [(conv, last_text)] 清单。
    """
    greeted = _greeted_job_ids()
    out = []
    for c in convs:
        last = (c.get("last") or "")
        theirs = c.get("theirs")
        # 已经发过了：最后一条已是「已发送附件」系统提示，跳过
        if any(m in last for m in RESUME_SENT_MARKS):
            continue
        # 核心新增：只发我主动打过招呼的岗位。job_id 空或不在台账「已发」里 → 跳过
        jid = (c.get("job_id") or "").strip()
        if not jid or jid not in greeted:
            continue
        if theirs and _wants_resume(last):
            out.append((c, last))
        elif theirs and not last:
            # 空消息 + 对方最后发言 → 可能是卡片请求，进对话页补查
            out.append((c, ""))
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

# 卡片请求里的「同意」按钮：message-card-wrap 里的 card-btn，文字「同意」。
# 2026-11-16 实测（闪念科技）拿到真实 DOM：
#   真卡片按钮：div.message-card-wrap.boss-green > div.message-card-buttons > span.card-btn（文字「同意」/「拒绝」）
#   陷阱按钮：  div.message-tip-bar > div.respond-popover > div.op > span.btn.btn-agree（文字也「同意」）
# 所以必须锁死在「message-card-wrap 容器内」的「span.card-btn」，且文字严格 ==「同意」，
# 否则会点到 respond-popover 那组（class 是 btn-agree，不是 card-btn），可能点错历史请求。
DETECT_CARD_JS = r"""
(function(){
  // 只扫「当前会话聊天区」div.chat-conversation，避免 SPA 切换会话时
  // 其他会话残留的卡片被全页面扫描误判（2026-11-16 实测踩坑：国科云招
  // 会话里扫到了闪念科技的「要简历」卡片残留）。
  var area = document.querySelector('div.chat-conversation');
  if (!area) area = document;
  var cards = area.querySelectorAll('div.message-card-wrap');
  var out = [];
  for (var i = 0; i < cards.length; i++) {
    var card = cards[i];
    var title = '';
    var h3 = card.querySelector('.message-card-top-title, .message-card-top-text, h3');
    if (h3) title = (h3.innerText || '').trim();
    var btns = [];
    var bs = card.querySelectorAll('div.message-card-buttons span.card-btn, div.message-card-buttons button.card-btn');
    for (var j = 0; j < bs.length; j++) {
      var t = (bs[j].innerText || '').trim();
      if (t) btns.push(t);
    }
    // 只收「卡片里带『同意』按钮」的，且标题里含「简历」字样的附件简历请求卡片
    if (btns.indexOf('同意') >= 0 && title.indexOf('简历') >= 0) {
      out.push({title: title.slice(0,60), btns: btns});
    }
  }
  return JSON.stringify(out);
})()
"""

CLICK_AGREE_JS = r"""
(function(){
  // 只在「当前会话聊天区」内，找文字 ==「同意」的 span.card-btn
  // （排除 respond-popover 的 btn-agree 陷阱，排除其他会话残留）
  var area = document.querySelector('div.chat-conversation');
  if (!area) area = document;
  var cards = area.querySelectorAll('div.message-card-wrap');
  for (var i = 0; i < cards.length; i++) {
    var btns = cards[i].querySelectorAll('div.message-card-buttons span.card-btn, div.message-card-buttons button.card-btn');
    for (var j = 0; j < btns.length; j++) {
      var t = (btns[j].innerText || '').trim();
      if (t === '同意') {
        btns[j].click();
        return 'CLICKED';
      }
    }
  }
  return 'NOT_FOUND';
})()
"""


def _detect_card():
    """探测当前会话里有没有「附件简历请求卡片」。返回 dict 或 None。

    只认「message-card-wrap 里带『同意』按钮、且标题含『简历』」的卡片
    （2026-11-16 实测的精确结构）。排除 respond-popover 的 btn-agree 陷阱按钮。
    """
    raw = PD.eval_js(DETECT_CARD_JS, timeout=30)
    try:
        cards = json.loads(raw or "[]")
    except Exception:
        return None
    for c in cards:
        title = c.get("title") or ""
        btns = c.get("btns") or []
        if "简历" in title and "同意" in btns:
            return c
    return None


# 点「同意」→ 弹「请选择要发送的简历」弹窗 → 选中目标简历 → 点「发送」的连贯 JS。
# 2026-11-16 实测（闪念科技）彻底搞清楚真实机制：
#   1. 点「同意」后弹的不是「上传简历」框，而是「请选择要发送的简历」框（列出已上传
#      的 N 份简历，li.list-item + button.btn-confirm「发送」按钮，初始 disabled）。
#   2. 必须先点选一份 li.list-item（点 li 本身，Vue @click 绑在 li 上），
#      「发送」按钮才从 disabled 变亮。
#   3. 再点「发送」，简历才真正发给 HR。
#   4. 弹窗在 async JS 返回后会自动关，所以「点同意→选中→点发送」必须在一个
#      async JS 里连贯完成，不能拆成多次 eval_js。
#   5. 弹窗里可能有多个 dialog-wrap（deactive 是历史残留，active 是当前真弹窗），
#      必须认 active。
# 选中哪份：按「简历文件名」匹配（文件名已带公司名，如「李然举-闪念科技-AI产品经理.pdf」），
#   精确选中刚定制的那份，不盲目选第一份。匹配不到才退而选第一份（并返回警告）。
SEND_CARD_JS_TMPL = r"""
(async function(){
  var target = "__TARGET_NAME__";  // 要选中的简历文件名
  var out = {agree:false, dialog:false, selected:false, sent:false, detail:""};

  // 1. 点「同意」按钮（只在当前聊天区 message-card-wrap 内，排除 respond-popover 陷阱）
  var area = document.querySelector('div.chat-conversation') || document;
  var cards = area.querySelectorAll('div.message-card-wrap');
  for (var i = 0; i < cards.length; i++) {
    var btns = cards[i].querySelectorAll('div.message-card-buttons span.card-btn, div.message-card-buttons button.card-btn');
    for (var j = 0; j < btns.length; j++) {
      if ((btns[j].innerText || '').trim() === '同意') {
        btns[j].click();
        out.agree = true;
        break;
      }
    }
    if (out.agree) break;
  }
  if (!out.agree) { out.detail = 'NO_AGREE_BTN'; return JSON.stringify(out); }

  // 2. 等「请选择要发送的简历」弹窗出现（最多 8 秒）
  var dlg = null;
  for (var k = 0; k < 16; k++) {
    await new Promise(function(r){ setTimeout(r, 500); });
    var d = document.querySelector('div.dialog-wrap.active');
    if (d && d.querySelectorAll('li.list-item').length > 0) { dlg = d; break; }
  }
  if (!dlg) { out.detail = 'NO_SELECT_DIALOG'; return JSON.stringify(out); }
  out.dialog = true;

  // 3. 选中目标简历（按文件名匹配，精确选刚定制的那份）
  var items = dlg.querySelectorAll('li.list-item');
  var picked = null;
  for (var m = 0; m < items.length; m++) {
    var nameEl = items[m].querySelector('.resume-name');
    var name = nameEl ? (nameEl.innerText || '').trim() : '';
    if (name === target) { picked = items[m]; break; }
  }
  if (!picked) {
    // 匹配不到，退而选第一份（但记警告）
    picked = items[0];
    out.detail = 'NAME_NOT_MATCH_FALLBACK_FIRST';
  }
  picked.click();
  await new Promise(function(r){ setTimeout(r, 600); });
  out.selected = true;

  // 4. 确认「发送」按钮变亮（不再 disabled），点它
  var send = dlg.querySelector('button.btn-confirm');
  if (!send) { out.detail = 'NO_SEND_BTN'; return JSON.stringify(out); }
  if (send.disabled) {
    out.detail = 'SEND_STILL_DISABLED';
    return JSON.stringify(out);
  }
  send.click();
  await new Promise(function(r){ setTimeout(r, 1500); });
  out.sent = true;
  out.detail = 'SENT';
  return JSON.stringify(out);
})()
"""


def _send_card(pdf_path):
    """场景一（卡片式）：点「同意」→ 弹「选择简历」框 → 选中目标简历 → 点「发送」。

    2026-11-16 实测澄清（之前一直没搞对的地方）：
    - 点「同意」≠ 直接发，也不走「上传简历」框。
    - 真实链路是：点同意 → 弹「请选择要发送的简历」框（列出附件栏已有简历）→
      选中一份 → 点「发送」→ 才真正发出。
    - 弹窗在 JS 返回后自动关，所以整条链在一个 async JS 里连贯完成。
    """
    fname = os.path.basename(pdf_path)
    js = SEND_CARD_JS_TMPL.replace("__TARGET_NAME__", fname)
    raw = PD.eval_js(js, timeout=60)
    try:
        d = json.loads(raw or "{}")
    except Exception:
        return False, f"点同意发送返回无法解析：{raw}"
    if d.get("sent"):
        note = d.get("detail") or "SENT"
        return True, f"已点同意并发送（选中：{fname}）"
    if d.get("selected"):
        return False, f"已选中简历但发送失败：{d.get('detail')}"
    if d.get("dialog"):
        return False, f"弹窗已出现但选中/发送失败：{d.get('detail')}"
    return False, f"点同意后未进入发送流程：{d.get('detail') or raw}"


# 点「发简历」→ 弹「请选择要发送的简历」框 → 选中目标简历 → 点「发送」。
# 2026-11-16 本人确认：点「发简历」和点「同意」弹出的是同一个「请选择要发送的简历」
# 弹窗（列出附件栏已有简历 + 发送按钮），不是「上传简历」框。所以场景二和场景一
# 殊途同归，都是「选中已有简历 → 点发送」。
SEND_BUTTON_JS_TMPL = r"""
(async function(){
  var target = "__TARGET_NAME__";
  var out = {clicked:false, dialog:false, selected:false, sent:false, detail:""};

  // 1. 点「发简历」按钮（底部工具栏 div.toolbar-btn，文字「发简历」）
  var tb = document.querySelectorAll('div.toolbar-btn, [class*=toolbar] span, [class*=toolbar] div');
  var clicked = false;
  for (var i = 0; i < tb.length; i++) {
    if ((tb[i].innerText || '').trim() === '发简历') {
      tb[i].click();
      clicked = true;
      break;
    }
  }
  if (!clicked) { out.detail = 'NO_SEND_RESUME_BTN'; return JSON.stringify(out); }
  out.clicked = true;

  // 2. 等弹窗
  var dlg = null;
  for (var k = 0; k < 16; k++) {
    await new Promise(function(r){ setTimeout(r, 500); });
    var d = document.querySelector('div.dialog-wrap.active');
    if (d && d.querySelectorAll('li.list-item').length > 0) { dlg = d; break; }
  }
  if (!dlg) { out.detail = 'NO_SELECT_DIALOG'; return JSON.stringify(out); }
  out.dialog = true;

  // 3. 选中目标简历
  var items = dlg.querySelectorAll('li.list-item');
  var picked = null;
  for (var m = 0; m < items.length; m++) {
    var nameEl = items[m].querySelector('.resume-name');
    var name = nameEl ? (nameEl.innerText || '').trim() : '';
    if (name === target) { picked = items[m]; break; }
  }
  if (!picked) { picked = items[0]; out.detail = 'NAME_NOT_MATCH_FALLBACK_FIRST'; }
  picked.click();
  await new Promise(function(r){ setTimeout(r, 600); });
  out.selected = true;

  // 4. 点发送
  var send = dlg.querySelector('button.btn-confirm');
  if (!send) { out.detail = 'NO_SEND_BTN'; return JSON.stringify(out); }
  if (send.disabled) { out.detail = 'SEND_STILL_DISABLED'; return JSON.stringify(out); }
  send.click();
  await new Promise(function(r){ setTimeout(r, 1500); });
  out.sent = true;
  out.detail = 'SENT';
  return JSON.stringify(out);
})()
"""


def _send_via_button(pdf_path):
    """场景二（口头要简历）：点「发简历」→ 弹「选择简历」框 → 选中 → 点「发送」。"""
    fname = os.path.basename(pdf_path)
    js = SEND_BUTTON_JS_TMPL.replace("__TARGET_NAME__", fname)
    raw = PD.eval_js(js, timeout=60)
    try:
        d = json.loads(raw or "{}")
    except Exception:
        return False, f"点发简历发送返回无法解析：{raw}"
    if d.get("sent"):
        return True, f"已点发简历并发送（选中：{fname}）"
    if d.get("selected"):
        return False, f"已选中简历但发送失败：{d.get('detail')}"
    if d.get("dialog"):
        return False, f"弹窗已出现但选中/发送失败：{d.get('detail')}"
    return False, f"点发简历后未进入发送流程：{d.get('detail') or raw}"


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

    # 1. 定制简历（先定制，后面上传附件栏 + 点发送都用它）
    pdf = resume_send.build_resume(job_title, company, job_type="intern")
    if not pdf:
        return "skip", f"{company}｜定制简历失败（岗位名：{job_title}）"

    if dry:
        # dry 不真上传不真发，但要知道会走哪条路，所以仍打开会话判断卡片/口头
        if "/web/geek/chat" not in (PD.current_url() or ""):
            PD.open_url(CHAT_URL)
            time.sleep(4)
        ok2, why = _open_conv(conv, job_title=jt)
        if not ok2:
            return "skip", f"{company}｜打开会话失败：{why}"
        card = _detect_card()
        if not last_text and not card:
            return "skip", f"{company}｜空消息且无简历卡片，跳过"
        mode = "卡片请求（点同意）" if card else "口头要（点发简历）"
        return "dry", f"{company}｜{mode}｜pdf={os.path.basename(pdf)}"

    # 2. 上传到附件栏（复用 resume_send.upload_resume，内部：导航简历页→判满删最旧→
    #    confirmAdd 保存进附件栏）。关键：点同意/发简历后弹的「请选择要发送的简历」框，
    #    列的正是附件栏已有的简历。所以必须先上传，弹窗里才有刚定制的这份可选中。
    ok = resume_send.upload_resume(pdf, confirm=False)
    if not ok:
        return "skip", f"{company}｜上传附件栏失败"
    # 上传会导航到简历页，发之前必须回聊天页重新打开会话
    if "/web/geek/chat" not in (PD.current_url() or ""):
        PD.open_url(CHAT_URL)
        time.sleep(4)
    ok2, why = _open_conv(conv, job_title=jt)
    if not ok2:
        return "skip", f"{company}｜上传后重新打开会话失败：{why}"

    # 3. 判断卡片 or 口头
    card = _detect_card()
    if not last_text and not card:
        return "skip", f"{company}｜空消息且无简历卡片，跳过"

    # 4. 风控再查一次（发之前）
    risk = _risk_words()
    if risk:
        return "halt", f"出现风控「{risk}」，停"

    # 5. 两种场景殊途同归：点入口按钮 → 弹「选择简历」框 → 选中刚上传的 → 点发送。
    if card:
        ok3, why3 = _send_card(pdf)
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
