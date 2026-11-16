# 自动发送：把你选中的招呼语替你发出去
#
# ⚠ 这一层是整套东西里唯一不可逆的地方 —— 消息发过去就收不回来。
#   所以下面的六道检查一道都不能删。删任何一道，代价都是"把 A 公司的话发给 B 公司"。
#
# 六道检查（对应 docs/自动投递-改造设计.md 第二节）：
#   1 入口绑定：每条都从【这条岗位的详情页】点进去，绝不在聊天列表里找人
#   2 目标核对：点开对话框先读公司名/岗位名，跟台账对不上就不许输入
#   3 内容自洽：话里的岗位族是不是这条的、有没有别的公司名混进来
#   4 去重：90 天内同公司同岗位不重复投
#   5 回读校验：输进去之后把输入框的字读回来【逐字】比对，不一样就重输
#   6 发完回读：确认这条消息真的发给了这个人，对不上立刻停机
#
# 用法：
#   python send_cdp.py probe              探一次选择器（不点任何东西，先跑这个）
#   python send_cdp.py dry-run            演练：走完流程，最后一步不点发送
#   python send_cdp.py send               真发
#   python send_cdp.py send --limit 10    只发 10 条（第一天建议这样）

import argparse
import csv
import json
import os
import random
import re
import shutil
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fetch_cdp as F
import run as _run
from config_loader import roles, path_of

CFG = roles()
DELIV = CFG["delivery"]
LEDGER = path_of("data/greeting_ledger.csv")
SEEN_FILE = path_of("data/company_seen.txt")
QUEUE = path_of("data/send_queue.json")
QUEUE_PARK = path_of("data/send_queue_skipped.json")   # 被跳过太多次的、投不了的，挪到这里存着
SENDLOG = path_of("data/send_log.csv")

# ─────────────────────────────────────────────────────────────
# 跳过怎么办（2026-11-07 加）
#
# 为什么要有这个：以前"跳过"只是打印一行，队列文件一个字都不改。
# 结果是那几条投不了的（90 天投过、没地址、内容对不上）每轮都排在最前面、
# 每轮都跳过一遍，把当天额度白白吃掉 —— 11-07 那天 14 条额度里就有 3 条
# 是被三个"钉子户"吃的，人得手动去改 json 才发得出去。
#
# 现在的规矩，一句话：**跳过不吃额度，跳一次挪队尾，跳第二次就收走。**
#   ① 这一轮只按"发出去几条"算满没满，跳过不占名额（后面还会接着往下找）
#   ② 跳过一次 → 挪到队尾，下一轮排最后，给别人让路，也给它自己留一次机会
#   ③ 同一条跳第二次 → 挪出队列，写进 data/send_queue_skipped.json（留时间和原因）
#   ④ 有些原因一次都不用等，当场收走 —— 因为条件永远不会自己变好
# ─────────────────────────────────────────────────────────────
SKIP_PARK_AFTER = 2          # 跳够几次就收走

# 这些原因等不用等，第一次跳过就收走：条件不会自己变好，留着只是堵路
SKIP_PARK_IMMEDIATE = (
    "90 天内同公司同岗位投过",     # 时间没到，明天、后天都一样
    "没有这条岗位的详情页地址",     # 数据缺了，重跑一百次也还是没有
    "这句已经在这个会话里",         # 已经发过了
    "对方已经回话",                 # 人家回话了，该人工接手，不该机器再发
)


# ─────────────────────────────────────────────────────────────
# 防风控 v2（2026-11-03 下沉，2026-11-05 接入代码）：节奏参数集中在这里
# ─────────────────────────────────────────────────────────────

def greet_interval_seconds():
    """两条发送之间的等待秒数 —— 读 interval_heavy_tail 的重尾分布。

    重尾（heavy tail）：70% 短间隔 12-40 秒 + 30% 长间隔 60-300 秒，
    模拟真人"大部分时间正常、偶尔走神去干别的"。之前用旧的 interval_seconds
    固定区间 [12,35]，两种长度均匀混，缺"偶尔很长的停顿"这个真人特征。
    """
    ht = DELIV.get("interval_heavy_tail") or {}
    short_ratio = float(ht.get("short_ratio", 0.70))
    if random.random() < short_ratio:
        # 短间隔：12-40 秒，用对数正态让它集中在 20 秒附近
        return F.human_delay(22.0, spread=0.5)
    # 长间隔：60-300 秒（1-5 分钟），模拟真人去忙别的
    return random.uniform(60, 300)


def day_cap_today():
    """今天的单日上限 —— 固定返回 daily_greet_cap，不再隔天减量。

    2026-11-07 本人拍板：取消「周中满额、周五 8 折、周末 4 折」的隔天减量，
    每天固定投满 daily_greet_cap（=80）。节奏缓冲改由「分波发送 + 波间歇」提供，
    见 cmd_send 里的 wave 逻辑。
    """
    return int(DELIV.get("daily_greet_cap", 80))


def risk_code_verdict(code):
    """按 risk_code_policy 给接口返回的 code 分级，返回 (是否致命, 动作说明)。

    之前只认 37 一种，实际上 code 有分级（37 环境 / 36 账号被标记 /
    32 临时封禁 / 1006 限流），不同 code 处理不同。37 可能是 token 过期
    可刷新重试一次，36/32 要立即全停人工处理，1006 是限流当日不再发。
    """
    policy = DELIV.get("risk_code_policy") or {}
    code = str(code)
    if code not in policy:
        return (False, f"code={code} 未在分级表里，按普通失败处理")
    action = policy[code]
    if code in ("36", "32"):
        return (True, f"code={code} {action}")
    if code == "37":
        return (False, f"code=37 {action}")
    if code == "1006":
        return (True, f"code=1006 {action}")
    return (False, f"code={code} {action}")

# ── 选择器候选：真实页面长什么样我没见过，所以每种都列几个，probe 会告诉你哪个有货 ──
# 2026-10-28 已经在真页面上校准过（不再是猜的）：
#   输入框 textarea.input（最多 300 字）　发送按钮 .btn-sure-v2，文案是「提交」
#   空白时按钮带 disabled，打了字才解禁 —— 这本身就是一道保险
SEL = {
    # 2026-11-05 插件模式真页面校准（chrome-use 驱动日常 Chrome）：
    #   输入框真正可写的是 div.chat-input（contenteditable=true，752×79），
    #   textarea.input 是 0×0 隐藏框。所以 div.chat-input 提到最前。
    #   发送按钮是 button.btn-send（空内容时带 disabled，写文字后脱离）。
    "输入框": [
        "div.chat-input", "div[contenteditable='true']",
        "textarea.input", "#chat-input", "textarea.chat-input",
        ".chat-input textarea",
        "[class*='chat-input']", "textarea", "[contenteditable='true']",
    ],
    "发送按钮": ["button.btn-send", "button.btn-sure-v2", ".btn-send",
                 "[class*='btn-send']", "[class*='send-btn']",
                 "[class*='chat-op'] button", "[class*='input-box'] button"],
    # ⚠ 2026-10-29 probe 实测：上面这几个模糊选择器在现在的聊天页上【全部是 0】，
    #   A 型路径（弹窗里自己写）因此永远卡在"读不到抬头"。
    #   末尾这些是 JS_CONV_READ 里已经验证能读到东西的真类名（2026-10-28 真页面校准）。
    #   放在末尾 = 只有老的都找不到才轮到它们，不会改变现有行为。
    "会话头部": [".chat-header", "[class*='chat-header']", ".base-info",
                 "[class*='base-info']", "[class*='job-card-info']",
                 ".session-title", "[class*='session'] [class*='info']",
                 ".chat-conversation .base-info",
                 ".chat-conversation .name-text",
                 ".chat-conversation .base-title",
                 ".chat-conversation .position-name"],
    "最后一条消息": [".message-item", "[class*='message-item']", ".msg-content",
                     "[class*='msg-content']", ".chat-item", "[class*='bubble']",
                     ".chat-conversation .text-content", ".text-content"],
}
BTN_TEXTS = ["立即沟通", "继续沟通", "打招呼", "在线聊", "立即投递"]
# 详情页上「立即沟通」那几层壳，probe 用。
# 真正能点的是最里面那个 a.btn-startchat —— 外面两层 div 只是套它的盒子（2026-10-29 踩出来）。
SEL_DETAIL_BTN = ["a.btn-startchat", "a[class*='startchat']",
                  ".btn-startchat-wrap", ".btn-container"]
# ⛔ 原 SEND_TEXTS（提交/发送/send）已删：click_send() 里内联了这两个字，没人读这个常量
#    （2026-10-29 清理死代码时发现）

# 熔断：连着几条没过就停，不许"假装没发生继续发"
# 2026-11-06 改三处（本人拍板「123 全加」）：
#   ① 好事/中性不算连败（见 SOFT_SKIP_NOTES），只有真失败才 +1
#   ② 连续失败阈值 3 → 5
#   ③ 加「滑动窗口跳过率」闸：最近 LOOKBACK 条里，真失败超过 FAIL_RATIO 就停
MAX_CONSECUTIVE_FAIL = 5
FAIL_LOOKBACK = 10          # 滑动窗口：看最近多少条
FAIL_RATIO = 0.7            # 最近 10 条里真失败超过 70% 才熔断

# 「软跳过」：这些不是真失败，不该计连败、不该参与熔断。
#   - 对方已回话 → 是好事，交人工回复
#   - 已经发过/投过 → 是防重，不是失败
#   - 没有详情页地址 → 是数据问题，单列，不该反复触发熔断
#   - 会话最后一句不是平台模板话 → 多半是 HR 已经回复了，不该补发，不是失败
#   - 岗位已失效/职位已关闭 → 岗位本身没了，不是系统故障
SOFT_SKIP_NOTES = (
    "对方已经回话",
    "这句已经在这个会话里",
    "90 天内同公司同岗位投过",
    "没有这条岗位的详情页地址",
    "会话最后一句不是平台模板话",
    "岗位已失效",
)
INCLUDE_LOW = False      # 低把握的跟进要不要一起发（日常永远 False，命令行加 --include-low 才开）


# ─────────────────────────────────────────────────────────────
# 页面操作
# ─────────────────────────────────────────────────────────────

JS_CLICK_TEXT = """
(() => {
  const texts = %s;
  const all = Array.from(document.querySelectorAll('button, a, span, div[class*="btn"]'));
  const vis = e => e.offsetParent !== null;
  const hit = (e, t) => {
    const s = (e.innerText || '').trim();
    return s === t || (s.indexOf(t) === 0 && s.length < t.length + 4);
  };
  for (const t of texts) {
    let cands = all.filter(e => vis(e) && hit(e, t));
    if (!cands.length) continue;
    // ★ 2026-10-29 修：只点【最里面】那一个。
    //   Boss 的沟通按钮是三层套娃，innerText 全是「立即沟通」：
    //     div.btn-container  →  div.btn-startchat-wrap  →  a.btn-startchat（真能点的）
    //   父容器在文档里排前面，按老写法先撞上父容器，对 div 调 .click() 等于点空气
    //   —— 页面纹丝不动，后面读抬头自然读不到，整条卡死在"halt"。
    //   所以先把"肚子里还套着别的候选"的父层剔掉，剩下的就是叶子节点。
    cands = cands.filter(e => !cands.some(o => o !== e && e.contains(o)));
    const e = cands[0];
    e.click();
    return 'clicked:' + t + ' on ' + e.tagName + '.' + (e.className || '').toString().slice(0, 40);
  }
  return null;
})()
"""

JS_READ_HEADER = """
(() => {
  const sels = %s;
  const out = [];
  for (const s of sels) {
    document.querySelectorAll(s).forEach(e => {
      const t = (e.innerText || '').trim();
      if (t && t.length < 300 && !out.includes(t)) out.push(t);
    });
    if (out.length) break;
  }
  return out;
})()
"""

JS_LAST_MSG = """
(() => {
  const sels = %s;
  for (const s of sels) {
    const list = document.querySelectorAll(s);
    if (list.length) {
      const last = list[list.length - 1];
      const t = (last.innerText || '').trim();
      if (t) return {hit: s, text: t.slice(-500)};
    }
  }
  return null;
})()
"""

# 页面上出现这些字，说明登录掉了 / 被风控了 → 立刻停
#
# ⚠ 2026-10-29 删掉「异常」两个字：岗位描述里到处都是它
#   （某科技公司那条写的是"异常兜底"），结果正常岗位被判成风控、整批投递当场停。
#   一个词只要在 JD 正文里可能自然出现，就不能拿它当风控信号。
#
# ⚠ 2026-11-07 又栽了一次，所以把词表拆成「强信号 / 弱信号」两档：
#   某信息安全公司的公司介绍里写着产品线「……安全评估系统、
#   **安全验证平台**、高级威胁分析预警系统……」→ 「安全验证」命中，
#   正常岗位被连停两轮，白记一次冷却。同一个坑第二次踩，说明光"挑不常见词"
#   不够，得看它出现在什么语境。
#
# 强信号：只可能出现在验证页/登录页，业务正文里不会这么说 → 命中即停
RISK_WORDS_HARD = [
    "滑动验证", "请完成验证", "操作过于频繁",
    "系统检测到异常", "账号存在异常", "扫码登录",
]
# 弱信号：可能是业务词（公司介绍、产品名、JD 里的技术名词）→ 还要确认页面
#   真的是验证页才停，见 risk_check / _looks_like_verify_page
RISK_WORDS_SOFT = ["安全验证", "请登录"]
RISK_WORDS = RISK_WORDS_HARD + RISK_WORDS_SOFT      # 兼容旧引用

# ─────────────────────────────────────────────────────────────
# 弹窗 / 投递上限提示（2026-11-02 补，对应 B 站视频踩坑「弹窗关闭后检测」）
#
# 问题：点「立即沟通」后，BOSS 可能弹一个「今日打招呼次数已达上限」之类的提示。
#   老逻辑不管弹窗，直接去读抬头/读会话，读不到就报 halt「页面改版」——
#   其实是到上限了，程序自己不知道，白白把「上限」当成故障，还可能连着误判。
# 修法：**先检测再操作**。任何"读页面"之前，先看看页面上有没有这些上限提示，
#   有就当场认出来、停下并说清楚"是到上限了，不是页面坏了"。
#
# 为什么单独列一份、不塞进 RISK_WORDS：RISK_WORDS 是"账号被风控/掉登录"，
#   要立刻熔断；而"上限"是**正常的一天发满了**，不需要熔断，只要"今天到此为止"。
#   两者后果不同，混在一起会让"正常到上限"被当成"账号出问题"。
# ⚠ 措辞只收"投递/打招呼次数"这类上限提示自己的话，别收「异常」「频繁」
#   （那俩已经在 RISK_WORDS 里，且「频繁」也可能出现在别处）。
LIMIT_WORDS = [
    "今日打招呼次数", "打招呼次数已达", "今日投递", "投递次数已达",
    "已达今日上限", "今日上限", "次数已用完", "已用完今日",
    "今日沟通次数", "沟通次数已达", "达到今日上限", "每日上限",
    "今日剩余次数为 0", "今日剩余为 0",
]


def _js(template, arr):
    return template % json.dumps(arr, ensure_ascii=False)


def page_text(tab):
    return F.evaluate(tab, "(document.body ? document.body.innerText : '').slice(0, 1200)") or ""


def _report_hard_risk(word):
    """页面出现风控字样时，记一笔风控 + 记冷却。**幂等**，同一次风控只记一次。

    为什么必须记（2026-11-07 实测踩的坑）：
      第 5 条（某信息安全公司）撞「安全验证」后系统确实停手了，但没记账 ——
      冷却文件还是空的。这时候人一重跑，会立刻再撞同一个验证，
      把连续触发次数从 1 推到 2、3，冷却从 30 分钟翻倍到几小时。

    为什么必须先判 in_cooldown()：
      F.set_cooldown() 每调一次就「次数 +1、时长翻倍」。而 risk_check 每条
      发送前都会调一次（light_patrol 里），页面一直挂着验证的话，
      不判重就会一次运行里翻倍好几次，把自己锁死一整天。
    """
    if F.in_cooldown():
        return                      # 已经在冷却期里，不重复记、不重复翻倍
    F.log_risk(f"发送环节 页面出现「{word}」")
    F.set_cooldown()


def _looks_like_verify_page(tab):
    """当前页面到底是不是「验证页/风控页」，而不是正常业务页里顺嘴提了句业务词。

    两个判据，命中任一就算真验证页（返回 True）：
      1) DOM 里有验证容器：captcha/verify/validate 类名，或指向验证域名的 iframe
      2) 整页正文很短（< 300 字）—— 正常岗位详情页至少一两千字，
         被换成验证页时只剩一句"请完成安全验证"

    为什么需要它：某信息安全公司的公司介绍里写着产品「安全验证平台」，
    光看文字命中最容易误杀。看"页面是不是正常业务页"才靠谱。
    """
    js = """(function(){
      var el = document.querySelector('[class*="captcha"],[class*="verify"],[class*="validate"],[id*="captcha"],[id*="verify"]');
      var ifr = document.querySelectorAll('iframe');
      var badFrame = false;
      for (var i=0;i<ifr.length;i++){
        var src = ifr[i].src || '';
        if (src.indexOf('captcha') >= 0 || src.indexOf('verify') >= 0 || src.indexOf('passport') >= 0) badFrame = true;
      }
      var t = document.body ? document.body.innerText : '';
      return JSON.stringify({
        has_verify_dom: !!el,
        bad_frame: badFrame,
        len: t.length
      });
    })()"""
    try:
        d = json.loads(F.evaluate(tab, js) or "{}")
    except Exception:
        return False            # 读不出来就当不是验证页（宁可放过，别乱停）
    if d.get("has_verify_dom") or d.get("bad_frame"):
        return True
    return int(d.get("len") or 0) < 300


def risk_check(tab):
    """页面上出现风控字样并且**确实是验证页**时，返回命中的词；否则 None。

    ★ 2026-11-07 分两档（同一天第二次踩「风控词撞业务词」这个坑）：
      强信号（RISK_WORDS_HARD）命中即停。
      弱信号（RISK_WORDS_SOFT，比如"安全验证"）还要过 _looks_like_verify_page：
        某信息安全公司的公司介绍里写着「安全验证平台」，那是它的产品名，
        页面完全正常（岗位在招、20-30K、JD 齐全）→ 不该停。
    """
    t = page_text(tab)
    hard = next((w for w in RISK_WORDS_HARD if w in t), None)
    soft = next((w for w in RISK_WORDS_SOFT if w in t), None)
    if not hard and not soft:
        return None
    if hard:
        _report_hard_risk(hard)     # 强信号：业务正文里不会这么说话，直接算
        return hard
    # 弱信号：先确认页面真的是验证页
    if _looks_like_verify_page(tab):
        _report_hard_risk(soft)
        return soft
    return None                     # 正常业务页里出现业务词 → 放行


def limit_check(tab):
    """先检测再操作：页面上有没有「今日投递/打招呼次数上限」提示。

    返回命中的那句文案；没有返回 None。必须在读抬头/读会话之前调用，
    否则弹窗一旦被后续操作关掉，上限提示就被自己吞了（视频踩的坑）。
    """
    t = page_text(tab)
    for w in LIMIT_WORDS:
        if w in t:
            return w
    return None


def wait_render(tab, what="会话列表", max_rounds=6, per_round=2.0):
    """等页面真正渲染出来再往下走（对应视频踩坑「SPA 渲染延迟要 8-12 秒」）。

    BOSS 是单页应用，点了跳转后内容是 JS 慢慢填出来的，干等 3 秒常常抓到旧内容
    或一片空白。这里不是干等，而是"每 per_round 秒看一眼目标元素有没有出来，
    出来了就立刻继续，最多看 max_rounds 轮"。比写死 8 秒更快也更稳：
    快的时候 2 秒就走，慢的时候也最多等 12 秒，不误判、不空等。

    返回 True=渲染出来了 / False=等到最后还是没有（调用方自己决定怎么办）。
    """
    for _ in range(max_rounds):
        hit = F.evaluate(tab, """
        (() => {
          if (!document.body) return 0;
          const t = document.body.innerText || '';
          // 会话列表 / 聊天区 / 输入框，三者出现任意一个就算"页面起来了"
          const has = document.querySelector('div.user-list') ||
                      document.querySelector('.chat-conversation') ||
                      Array.from(document.querySelectorAll('textarea, [contenteditable=true]'))
                        .some(e => { const r = e.getBoundingClientRect(); return r.width > 40 && r.height > 20; });
          return (has ? 1 : 0);
        })()""", timeout=20)
        if hit:
            return True
        time.sleep(per_round)
    return False


def find_chat_btn(tab, max_rounds=5, per_round=2.0):
    """轮询找「立即沟通/继续沟通」按钮，找到了就点，返回 (是否点到, 说明)。

    2026-11-06 修：原来点进详情页后干等 5 秒、一次性找按钮，找不到就跳过。
    BOSS 详情页是 SPA 动态加载，「继续沟通」按钮要查历史沟通状态，慢的时候
    5 秒还没渲染出来 → 被误判成「没找到按钮」。实测（17:14 那批）后面连续
    5 条都是这个错，前面 17 条正常 —— 就是「越到后面页面越慢 + 零重试」。
    这里改成跟 wait_render 一样的轮询：每 per_round 秒找一次，最多 max_rounds 轮。
    返回说明带上「职位已关闭」这种明确信号，让上层能区分「真没按钮」和「岗位关了」。
    """
    # 先探一次页面有没有「职位已关闭 / 已停止招聘」这种明确信号
    closed = F.evaluate(tab, """
    (() => {
      const t = document.body.innerText || '';
      if (t.indexOf('职位已关闭') >= 0) return '职位已关闭';
      if (t.indexOf('已停止招聘') >= 0) return '已停止招聘';
      if (t.indexOf('该职位已下架') >= 0) return '职位已下架';
      return '';
    })()""", timeout=20)
    if closed:
        return False, f"岗位已失效：{closed}"

    for _ in range(max_rounds):
        clicked = F.evaluate(tab, _js(JS_CLICK_TEXT, BTN_TEXTS), timeout=30)
        if clicked:
            return True, clicked
        time.sleep(per_round)
    return False, "轮询多次仍没找到沟通按钮"


def probe_btn_text(tab, words):
    """【只读】探测页面上沟通按钮的文字，返回命中的文字，不点任何东西。

    用途：区分「立即沟通」（新岗位，没聊过）和「继续沟通」（已沟通过，job_id
    可能变了）。Boss 的岗位 job_id 会变 —— HR 下架重发/改标题后，新 job_id
    指向的还是同一个 HR 同一个会话，详情页按钮会从「立即沟通」变成「继续沟通」。
    靠这个信号判断「到底聊过没」，比台账的 job_id 精确匹配更可靠（job_id 会变，
    但按钮不会骗人）。"""
    js = """
    (() => {
      const words = %s;
      const all = Array.from(document.querySelectorAll('a, button, span, div[class*="btn"]'));
      const vis = e => e.offsetParent !== null;
      for (const t of words) {
        const hit = all.find(e => vis(e) && (e.innerText || '').trim() === t);
        if (hit) return t;
      }
      return null;
    })()
    """ % json.dumps(words, ensure_ascii=False)
    return F.evaluate(tab, js, timeout=20)


# ─────────────────────────────────────────────────────────────
# 会话状态：用接口读，不猜页面结构
#
# 为什么不用"看页面上最后一条消息是谁说的"：
# 2026-10-28 实测发现，点「立即沟通」有可能被平台当成"一键打招呼"，
# 直接把 BOSS 那句默认招呼语「您好，看到您的招聘信息，我很感兴趣…」发出去。
# 那天就发生了（14:51 发给作业帮）。页面上有没有这条、长什么样都要猜，
# 但接口里写得明明白白 —— 所以这一层一律走接口，且每次点击前后各查一次。
# ─────────────────────────────────────────────────────────────

FRIEND_API = ("https://www.zhipin.com/wapi/zprelation/friend/"
              "getGeekFriendList.json?page=%d&pageSize=50")

JS_FRIEND_LIST = """
(async () => {
  const all = [];
  for (let p = 1; p <= 5; p++) {
    // 页码必须拼进去。写死 page=1 会把同一页读五遍，104 条会算成 500 条。
    const u = "https://www.zhipin.com/wapi/zprelation/friend/"
              + "getGeekFriendList.json?page=" + p + "&pageSize=50";
    const r = await fetch(u, {credentials: 'include'});
    const j = await r.json();
    const res = (j.zpData && j.zpData.result) || [];
    all.push(...res);
    if (res.length < 50) break;
  }
  return JSON.stringify(all);
})()
"""

PLATFORM_DEFAULT = "您好，看到您的招聘信息，我很感兴趣，希望可以进一步沟通。"


def friend_list(tab):
    """读出全部会话。返回 [{company, boss, last, ts, mine, theirs, job_id, uid}]"""
    raw = F.evaluate(tab, JS_FRIEND_LIST.replace("__URL__", FRIEND_API % 1), timeout=60)
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
            "msg_id": lm.get("msgId") or 0,
            "ts": x.get("lastTS") or lm.get("msgTime") or 0,
            "mine": lm.get("toId") == uid if uid else False,
            "theirs": lm.get("fromId") == uid if uid else False,
            "job_id": x.get("encryptJobId") or "",
            "uid": uid,
        })
    return out


def _probe_once(tab):
    """探一次接口，返回 (code, msg, has_data)。code=None 表示没读到。
    has_data=True 表示 zpData.result 真的是数组（能读到会话），是比 code 更硬的信号。"""
    js = """
    (async () => {
      try {
        const r = await fetch("__URL__", {credentials: 'include'});
        const j = await r.json();
        const arr = (j.zpData && j.zpData.result);
        return JSON.stringify({
          code: j.code,
          msg: (j.message || '').slice(0,60),
          has_data: Array.isArray(arr)
        });
      } catch (e) { return JSON.stringify({error: String(e)}); }
    })()
    """.replace("__URL__", FRIEND_API % 1)
    raw = F.evaluate(tab, js, timeout=30)
    try:
        d = json.loads(raw or "{}")
    except Exception:
        return None, "没读到接口返回", False
    if d.get("error"):
        return None, f"接口异常：{d['error']}", False
    return d.get("code"), (d.get("msg") or ""), bool(d.get("has_data"))


def api_code_probe(tab):
    """发前探一次接口 code，命中分级表就停。返回 (code, 说明)。

    2026-11-06 修：之前页面跳转时 fetch 会读到脏 code（实测 code=7），
    而 7 不在 risk_code_policy 分级表里，属于「脏读」不是真登录失效。
    修法：未知 code 先重试一次（间隔 2 秒），重试后：
      - 若 has_data=True（真能读到会话）→ 认定登录没问题，code 作废返回 0
      - 若还是读不到数据 → 才当真的异常处理，返回那个 code
    这样既能保住 36/32/1006/37 的真分级拦截，又不会被页面跳转的脏读吓停。
    """
    code, msg, has_data = _probe_once(tab)
    # 正常或读到数据：直接放行
    if code == 0 or has_data:
        return code, msg
    # code=None（没读到）或未知 code：重试一次
    time.sleep(2)
    code2, msg2, has_data2 = _probe_once(tab)
    if has_data2:
        return 0, "（第一次读到脏 code，重试确认登录正常）"
    return code2, msg2


def _recover_token(tab):
    """code37 时：重载 BOSS 页让页面脚本重新生成 stoken，再探一次。

    2026-11-07 加（v2 方案③）。一手实测结论（can4hou6joeng4/boss-agent-cli
    platform-risk.md）：code37 命中后恢复，第 1 步必须先重载业务页让页面
    脚本重新生成 stoken，再读新 stoken——不复位页面读到的仍是失效 token。

    返回 (恢复后 code, 说明)。第 1 步（重载）不能省。
    """
    try:
        cur = F._plugin().current_url() or ""
        if cur and "zhipin.com" in cur:
            F.goto(tab, cur, wait=5)   # 重载当前 BOSS 页
        else:
            F.goto(tab, "https://www.zhipin.com/web/geek/chat", wait=5)
    except Exception as e:
        return None, f"重载失败：{e}"
    time.sleep(2)
    code, msg = api_code_probe(tab)
    return code, msg


# ⛔ 原 find_conv() 已删（2026-10-29 清理死代码时发现）：
#   ① 全项目没人调用（现在的路径一律按 job_id 唯一命中，见 process_one / send_in_existing_conv）
#   ② 它内部有"按公司名回退匹配"分支 —— 那正是串号头号来源
#      （历史招呼语有 9 条一字不差，靠公司名猜必错），留着迟早被误用。
#   ③ 它的 ID 判断还写错了：`a and b in c or c in b` 的优先级跟想的不一样。
# 以后要找会话，一律用：按 job_id 完全相同去 friend_list() 里过滤。


def same_text(a, b):
    """逐字比：去掉空格标点之后还一样才算一样。
    为什么不让"差不多就行"：相似度 0.95 可能只是"少了一个不"字。"""
    import unicodedata
    def clean(s):
        s = unicodedata.normalize("NFKC", str(s or ""))
        return re.sub(r"[\s\u3000\u200b\uff0c,\u3002.\uff1b;!！?？]+", "", s)
    return clean(a) == clean(b)


# ─────────────────────────────────────────────────────────────
# 六道检查
# ─────────────────────────────────────────────────────────────

FAMILY_WORDS = {
    "ai_product_manager": ["AI 产品经理", "AI产品经理", "产品经理"],
    # fde 不在这张表里：2026-11-01 本人拍板动机句"丢掉岗位族名"（泛化成"深耕这个方向"），
    # 导致话里不再出现 FDE/AI交付/解决方案，被下面的强校验误拦。
    # 这里不再对 fde 做族名词强校验，靠"混进别的公司名"那道检查防串号。
    "ai_product_ops": ["AI 产品运营", "产品运营", "AI 运营", "运营"],
    "ai_project_ops": ["AI 项目运营", "项目运营", "AI 运营", "运营"],
    "ai_insight_ops": ["AI 产品", "需求"],
}


def verify_text(item, text, queue):
    """检查 3：这条招呼语是不是属于这条岗位。返回 (通过?, 原因)"""
    fam = item.get("family") or ""
    words = FAMILY_WORDS.get(fam, [])
    if words and not any(w in text for w in words):
        # 第 5 族（靠 JD 正文认出来的）名字里没有 AI，放过
        if fam != "ai_insight_ops":
            return False, f"话里没提到这个岗位族（{fam}）"

    # 有没有别的公司名混进来 —— 这是"生成时串了"最明显的征兆
    mine = (item.get("company") or "").strip()
    if mine and len(mine) >= 3:
        for other in queue:
            oc = (other.get("company") or "").strip()
            if oc and oc != mine and len(oc) >= 3 and oc in text:
                return False, f"话里混进了别的公司名：{oc}"

    if not text.strip():
        return False, "招呼语是空的"
    return True, ""


def load_seen():
    """已投过的公司名清单。

    为什么单独存一份：台账只记机器投过的。你手动投过的那些机器不知道，
    不查就会再投一遍 —— 同一个 HR 看到同一个人来两次，比没投过还糟。
    """
    if not os.path.exists(SEEN_FILE):
        return set()
    out = set()
    with open(SEEN_FILE, encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if s and not s.startswith("#"):
                out.add(s)
    return out


def _parse_send_time(s):
    """把台账 send_time 解析成 datetime，解析不了返回 None。

    格式统一是 "%Y-%m-%d %H:%M"，但老数据可能带秒或别的写法，宽松解析。
    """
    if not s:
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    # 兜底：只取日期部分
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


def _within_days(ts, days):
    """ts 距今天是否在 days 天以内。ts 是 None 时保守返回 True（当近期投过）。"""
    if ts is None:
        return True
    return (datetime.now() - ts).days < days


def _norm_city(c):
    """城市归一：去掉「市」后缀，空值归 None。"""
    if not c:
        return None
    c = c.strip()
    return c[:-1] if c.endswith("市") else c


def _same_job(item, row):
    """判断台账里这一行是不是「同一个坑」。

    三级精度，从最严到最宽：
    ① job_id 完全相同 → 绝对同一个坑（最可靠）
    ② 公司 + 标题 + 城市 三者都同 → 同一个 HR 重发的老岗位（挡住）
       —— 加「城市」是为了不错杀「公司在不同城市各招一个同名岗」，
          比如顺丰在上海和杭州各挂一个「消费品行业解决方案」，这俩不是同一个坑。
    ③ 公司 + 标题 同、但城市缺一个 → 退化成旧的粗匹配（保守挡住，别漏重投）

    只有三级都不命中，才算「不是同一个坑」。
    """
    row_jid = (row.get("job_id") or "").strip()
    row_co = (row.get("company") or "").strip()
    row_ti = (row.get("title") or "").strip()
    row_city = _norm_city(row.get("city") or "")

    job_id = (item.get("job_id") or "").strip()
    company = (item.get("company") or "").strip()
    title = (item.get("title") or "").strip()
    city = _norm_city(item.get("city") or "")

    # ① job_id 精确匹配
    if job_id and row_jid == job_id:
        return True

    # ③ 只有公司+标题都同才往下看（公司和标题都不对，肯定不是同一个坑）
    if row_co != company or row_ti != title:
        return False

    # ② 城市都齐 → 必须城市也同；任一缺城市 → 保守算同一个坑
    if city and row_city:
        return city == row_city
    return True


def already_sent(item, days=90):
    """检查 4：90 天内同公司（同岗位）投过没有。

    2026-11-11 改：真正按「时间窗口」算，而不是「投过就永久排除」。
    - 台账里有 send_time 的，看它距今是否 < days 天；
    - send_time 为空的旧记录，保守处理（当 90 天内投过，继续排除），
      宁可少投、绝不重投。
    2026-11-11 再改：同岗位判定从「公司+标题」升级成「job_id / 公司+标题+城市」，
    既挡住重发的旧岗，又不错杀公司在不同城市各招的同名岗。
    """
    company = (item.get("company") or "").strip()
    if not company:
        return False

    # ① 台账：机器自己投过的
    if os.path.exists(LEDGER):
        with open(LEDGER, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if (row.get("sent") or "") != "已发":
                    continue
                if _same_job(item, row):
                    ts = _parse_send_time(row.get("send_time") or "")
                    if _within_days(ts, days):
                        return True

    # ② 历史名单：你手动投过的，或者从后台抓回来的（这些没有时间戳，保守算近期）
    # 2026-11-12 本人拍板：放开「互相包含」的模糊匹配（大公司会投很多不同岗位，
    #   全称简称互相包含会误伤太多）。改成只拦「公司名完全相等」这一档。
    for seen in load_seen():
        if seen == company:
            return True
    return False


def is_closed(item):
    """检查 5：岗位是否已下架/关闭。

    数据来源是 fetch 阶段留下的信号（下架岗位详情页会 302 弹回首页，
    enrich_details 里已经按这个跳过不存）。这里兜底：岗位数据里带了下架标记，
    或压根没有 url 可点（下架岗位通常拿不到详情页地址），都算「已关闭」。
    """
    if item.get("closed") or item.get("offline"):
        return True
    url = (item.get("url") or "").strip()
    return not url


# ─────────────────────────────────────────────────────────────
# 检查 2：目标核对（看对方是不是这个人）
# ─────────────────────────────────────────────────────────────

def verify_target(item, header_lines):
    blob = " ".join(header_lines)
    company = (item.get("company") or "").strip()
    title = (item.get("title") or "").strip()

    if company and company not in blob:
        # 全名对不上时，逐层脱衣服再比：
        #   "深圳市腾讯计算机系统有限公司" → 去城市前缀 → 去公司后缀 → "腾讯计算机系统"
        #   页面上常常只写简称（"腾讯"），所以两边都往中间凑
        core = re.sub(r"^(北京|上海|广州|深圳|杭州|天津|南京|武汉|成都|厦门|福州|西安|苏州|无锡|宁波|长沙)市?", "", company)
        core = re.sub(r"(股份)?(有限)?(责任)?公司$|集团$|(（.*?）|\(.*?\))", "", core).strip()
        matched = False
        if core:
            for line in header_lines:
                line = line.strip()
                # 页面上写了 core 的前几个字（"云和网络" 对上 "云和网络技术"）
                if core in line or (len(core) >= 4 and core[:4] in line):
                    matched = True
                    break
                # 页面上只写了简称（"腾讯" 对上 "腾讯计算机系统"）
                if len(line) >= 2 and line in core:
                    matched = True
                    break
        if not matched:
            return False, f"对话框里的公司名对不上（台账：{company}　简称：{core}）"

    if title:
        # 岗位名不要求一字不差，但得有重合
        tt = re.sub(r"[（(].*?[)）]", "", title)
        hit = sum(1 for ch in set(tt) if ch in blob)
        if len(set(tt)) >= 4 and hit / len(set(tt)) < 0.5:
            return False, f"对话框里的岗位名对不上（台账：{title}）"
    return True, ""


# ─────────────────────────────────────────────────────────────
# 输入 + 回读（2026-10-28 定稿）
#
# 当天踩坑史，全记在这儿防止再犯：
#   1. 页面上真正的输入框是【可见的那个】——聊天页里同时存在
#      隐藏的 textarea.input（0×0）和可见的 div.chat-input。
#      对隐藏的那个写值，按钮会亮，但点发送永远没反应。
#      → 一切输入都认"看得见"的那个。
#   2. 对隐藏框写值 + input 事件这条路已废弃。
#   3. 发送：聊天页右下角写着「按Enter键发送」→ 真按 Enter 就能发；
#      打招呼弹窗里则是一个「提交」按钮。两种都支持，按钮优先。
#   4. 会话列表接口里的 lastMsg 会被截断（大约 50 字），所以发完
#      对账不能比全文，要比"接口给的那截"是不是我们这句的开头。
# ─────────────────────────────────────────────────────────────

JS_VISIBLE_INPUT = """
(() => {
  const els = Array.from(document.querySelectorAll('textarea, [contenteditable=true]'));
  for (const e of els) {
    const r = e.getBoundingClientRect();
    if (r.width > 40 && r.height > 20)
      return JSON.stringify({tag: e.tagName, cls: (e.className||'').toString(),
                             x: Math.round(r.left + r.width/2),
                             y: Math.round(r.top + r.height/2)});
  }
  return null;
})()
"""

JS_READ_VISIBLE = """
(() => {
  const els = Array.from(document.querySelectorAll('textarea, [contenteditable=true]'));
  for (const e of els) {
    const r = e.getBoundingClientRect();
    if (r.width > 40 && r.height > 20)
      return (e.tagName === 'TEXTAREA' || e.tagName === 'INPUT') ? (e.value || '') : (e.innerText || '');
  }
  return null;
})()
"""


def _focus_visible_input(tab):
    meta = F.evaluate(tab, JS_VISIBLE_INPUT, timeout=20)
    if not meta:
        return None
    p = json.loads(meta)
    for t in ("mousePressed", "mouseReleased"):
        F.cdp(tab, "Input.dispatchMouseEvent",
              {"type": t, "x": p["x"], "y": p["y"], "button": "left", "clickCount": 1})
    time.sleep(0.5)
    return p


def type_text(tab, text):
    """真鼠标点进可见输入框 → CDP insertText 写入。
    返回 True/False（写没写进去由回读判断，这里只管尽力写）。"""
    if not _focus_visible_input(tab):
        return False
    F.cdp(tab, "Input.insertText", {"text": text})
    time.sleep(0.8)
    return True


def read_input(tab):
    return (F.evaluate(tab, JS_READ_VISIBLE, timeout=20) or "").strip()


def clear_input(tab):
    if not _focus_visible_input(tab):
        return
    F.evaluate(tab, """
    (() => {
      const els = Array.from(document.querySelectorAll('textarea, [contenteditable=true]'));
      for (const e of els) {
        const r = e.getBoundingClientRect();
        if (r.width > 40 && r.height > 20) {
          e.focus();
          document.execCommand('selectAll', false, null);
          document.execCommand('delete', false, null);
          e.dispatchEvent(new Event('input', {bubbles: true}));
          return 1;
        }
      }
      return 0;
    })()""", timeout=20)
    time.sleep(0.4)


def sent_matches(full, api_last):
    """发完对账。接口的 lastMsg 会截断，所以允许"接口那句是我们这句的开头"。
    另外接口会把箭头之类的字符转成网页实体（→ 变 &rarr;），先还原再比。"""
    import html as _html
    a = re.sub(r"[\s\u3000\u200b]+", "", _html.unescape(str(full or "")))
    b = re.sub(r"[\s\u3000\u200b]+", "", _html.unescape(str(api_last or "")))
    if not b:
        return False
    if a == b:
        return True
    return len(b) >= 20 and a.startswith(b)


def click_send(tab):
    """发送。按钮优先（打招呼弹窗的「提交」），找不到就按 Enter（聊天页）。"""
    js = """
    (() => {
      const inp = Array.from(document.querySelectorAll('textarea, [contenteditable=true]'))
        .find(e => { const r = e.getBoundingClientRect(); return r.width > 40 && r.height > 20; });
      if (!inp) return null;
      let box = inp;
      for (let i = 0; i < 6 && box; i++) {
        for (const e of box.querySelectorAll('div[class*=btn], button, a')) {
          const t = (e.innerText || '').trim();
          if (t !== '提交' && t !== '发送') continue;
          if ((e.className || '').toString().includes('disabled')) continue;
          if (e.hasAttribute('disabled')) continue;
          const r = e.getBoundingClientRect();
          if (r.width < 10 || r.height < 10) continue;
          e.click();
          return 'clicked:' + t + ':' + (e.className || '').toString().slice(0, 30);
        }
        box = box.parentElement;
      }
      return null;
    })()
    """
    r = F.evaluate(tab, js, timeout=20)
    if r:
        return True
    # 没有按钮（聊天页）→ 按 Enter
    if _focus_visible_input(tab):
        for t in ("keyDown", "char", "keyUp"):
            ev = {"type": t, "key": "Enter", "code": "Enter",
                  "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13}
            if t == "char":
                ev["text"] = "\r"
            F.cdp(tab, "Input.dispatchKeyEvent", ev)
        time.sleep(0.5)
        return True
    return False


# ─────────────────────────────────────────────────────────────
# 事中轻量巡检：每发 10 条查一次，出问题当场刹车
#
# 跟 inspect.py（事后算总账）的分工：那边每天跑一次、算四个指标；
# 这里只查"能不能继续发"这一件事，两秒钟完事：
#   1) 当前页面有没有出现风控字样
#   2) 今天的风控日志有没有新增
#   3) 本轮执行率 —— 连着多条没过（熔断线）就停
# 只读本地文件和当前页面，不碰任何接口 —— 巡检自己不能变成风控诱因。

def light_patrol(tab, sent_ok, attempts, consec_fail):
    """返回 None = 继续发；返回字符串 = 停下来的原因。"""
    risk = risk_check(tab)
    if risk:
        return f"页面出现「{risk}」"
    try:
        p = path_of("data/risk_events.log")
        if os.path.exists(p):
            txt = open(p, encoding="utf-8").read().strip()
            today = time.strftime("%Y-%m-%d")
            # 只要今天的日志里出现「发送类」风控记录就停（哪怕只有一条）。
            # 排除「静默限流」——那是扫描环节的疑似信号（连续几页刷不出卡片），
            # 跟发送招呼语无关，之前把它算进来导致第 10 条被扫描限流误刹车。
            for line in txt.splitlines()[-10:]:
                if not line.startswith(today):
                    continue
                if "静默限流" in line:
                    continue
                return "今天的风控日志里有发送类新记录"
    except Exception:
        pass
    if attempts >= 5 and sent_ok == 0:
        return "连续 5 条一条都没发出去，先停下来查原因"
    return None


# ─────────────────────────────────────────────────────────────
# 单条流程
# ─────────────────────────────────────────────────────────────

def process_one(tab, item, queue, dry=False, verbose=True):
    """返回 (结果, 说明)。结果 ∈ ok / dry / skip / halt"""
    company = item.get("company") or ""
    title = item.get("title") or ""
    text = (item.get("greeting_text") or "").strip()
    url = item.get("url") or ""

    # 检查 3：内容自洽（不用开页面就能查，先查）
    ok, why = verify_text(item, text, queue)
    if not ok:
        return "skip", f"内容自洽没过：{why}"

    # 检查 4：去重
    if already_sent(item):
        return "skip", "90 天内同公司同岗位投过"

    # 检查 5：岗位是否已下架/关闭
    if is_closed(item):
        return "skip", "岗位已下架或没有详情页地址"

    if not url:
        return "skip", "没有这条岗位的详情页地址"

    # 检查 1：入口绑定 —— 从这条岗位自己的详情页点进去
    F.goto(tab, url if url.startswith("http") else "https://www.zhipin.com" + url, wait=5)

    risk = risk_check(tab)
    if risk:
        return "halt", f"页面出现「{risk}」，停"

    # ★ 2026-11-12 加（判重漏网 bug）：点按钮前先【只读】探测按钮是「立即沟通」
    #   还是「继续沟通」。「继续沟通」= 这个岗位已经沟通过（job_id 变了，台账可能
    #   还记成未发）→ 直接跳过，绝不重投。这比台账 job_id 精确匹配更可靠，
    #   因为 job_id 会变，但 Boss 按钮不会骗人。
    btn_word = probe_btn_text(tab, ["继续沟通", "立即沟通", "打招呼", "在线聊", "立即投递"])
    if btn_word == "继续沟通":
        return "skip", (f"详情页按钮是「继续沟通」→ 这个岗位已经沟通过"
                        f"（job_id 变了，台账可能没跟上），不重复投")

    clicked, why = find_chat_btn(tab)
    if not clicked:
        return "skip", why
    time.sleep(random.uniform(3.0, 5.0))

    # ★ 2026-11-02 补（视频踩坑「弹窗关闭后检测」）：点完「立即沟通」先查上限弹窗，
    #   再谈读会话/读抬头。顺序不能反——先关弹窗再检测，上限提示会被自己关掉，
    #   程序就把「到上限」误判成「页面改版」，连着 halt（send_log 里
    #   「读不到对话框的抬头」这条，很可能有一部分就是这么来的）。
    limit_hit = limit_check(tab)
    if limit_hit:
        return "halt", f"页面提示「{limit_hit}」—— 今天投递/打招呼到上限了，别再发，停"

    # 2026-10-29 改掉原来的"点前拍快照、点后再拍一张做对比"：
    #   Boss 的会话列表有刷新延迟，对比法会把"平台已经代发成功"判成"点击没生效"，
    #   然后既补发不了、也不记账，岗位停在"只发了模板话"的半吊子状态（踩过 3 条）。
    # 现在先按【B 型】处理：平台代发 → 会话已建 → 按岗位 ID 唯一命中 → 进聊天页补发。
    # 找不到会话才退回【A 型】：弹窗还开着，直接在弹窗里打字（下面检查 2 + 检查 5）。
    # ⚠ 2026-10-29 加大等待：原来只等 2 轮 ×5 秒 = 10 秒。实测元外通那条
    #   会话其实已经建好了，可接口 10 秒内还没吐出来 → 程序以为"平台没代发"，
    #   退回 A 型去弹窗里打字，而弹窗早关了 → 报"读不到抬头"、整批停在第 12 条。
    #   现在 4 轮 ×5 秒 = 20 秒。多等这 10 秒，换的是不误判。
    hit = []
    for _ in range(4):
        hit = [r for r in friend_list(tab)
               if (r.get("job_id") or "") == str(item.get("job_id") or "")]
        if hit:
            break
        time.sleep(5.0)
    if hit:
        who = hit[0]
        last_now = (who.get("last") or "").strip()
        if sent_matches(text, last_now):
            return "skip", "这句已经在这个会话里了，不重复发"
        # 只有"平台替我发了那句默认模板话"这一种情况能补救：
        #   会话已经建好、人是按岗位 ID 对上的，去聊天页把真话术作为第二条补上。
        #   别的（冒出系统提示、冒出别人）一律不放过，直接跳过。
        if not sent_matches(PLATFORM_DEFAULT, last_now):
            return "skip", (f"会话最后一句不是平台模板话（是「{last_now[:24]}」）"
                            f"—— 看不出是平台代发的，不敢动，跳过。")
        return send_in_existing_conv(tab, item, text, who, dry)

    # 走到这儿 = 平台没代发，是【A 型】：新岗位首触，点完「立即沟通」页面已经跳到聊天页
    # 检查 2：目标核对
    #   ★ 2026-11-12 修（漏投 bug）：原来用 JS_READ_HEADER（详情页选择器）读抬头，
    #   但点完「立即沟通」后页面已经进了聊天页，那些详情页选择器（.chat-header 等）
    #   在聊天页上全是 0 —— SEL 注释里早就写「A 型路径永远卡在读不到抬头」。
    #   正确应该用 conv_read()（聊天页真类名 .chat-conversation 那套），
    #   跟 B 型补发路径 _open_conv 用的是同一套，已在真页面校准过。
    info = conv_read(tab)
    header = (info.get("heads") or []) if info else []
    if not header:
        return "skip", "读不到对话框的抬头（公司名/岗位名），不敢发，跳过这条"
    ok, why = verify_target(item, header)
    if not ok:
        return "skip", f"目标核对没过：{why}"

    # 检查 5：输入 + 逐字回读
    for attempt in (1, 2):
        if not type_text(tab, text):
            return "halt", "找不到输入框，停（多半页面改版了）"
        back = read_input(tab).strip()
        if back == text:
            break
        if attempt == 2:
            clear_input(tab)
            return "halt", f"输入框回读两次都对不上，停（预期「{text[:20]}…」实际「{back[:20]}…」）"
        time.sleep(1.0)
    else:
        return "halt", "输入框回读失败"

    if dry:
        clear_input(tab)
        return "dry", f"演练通过（对方：{' / '.join(header[:2])[:40]}）"

    if not click_send(tab):
        return "halt", "点不到发送按钮，停"
    time.sleep(random.uniform(2.0, 3.5))

    # 检查 6：发完回读 —— 用会话接口对账（接口那句可能被截断，用 sent_matches 比）
    time.sleep(1.5)
    rows_now = friend_list(tab)
    jid = str(item.get("job_id") or "")
    hit = [c for c in rows_now if jid and (c.get("job_id") or "") == jid]
    if hit:
        if not sent_matches(text, hit[0]["last"]):
            return "halt", (f"发完回读对不上，停（会话最后一条是"
                            f"「{(hit[0]['last'] or '')[:40]}」）")
    else:
        # 新会话还没出现在列表里（接口有延迟），退回页面读最后一条
        last = F.evaluate(tab, _js(JS_LAST_MSG, SEL["最后一条消息"])) or {}
        sent = (last.get("text") or "").strip()
        if text not in sent and not sent_matches(text, sent):
            return "halt", f"发完回读对不上，停（最后一条是「{sent[:40]}」）"

    return "ok", f"已发给 {' / '.join(header[:2])[:40]}"


def send_in_existing_conv(tab, item, text, conv_hint, dry=False):
    """在一个【已经存在的】会话里把我们写的招呼语发出去。

    什么时候会走到这儿：
      点「立即沟通」的时候，平台没弹输入框，而是直接替我们发了那句默认模板话
      「您好，看到您的招聘信息，我很感兴趣…」。会话是建起来了，但我们想说的
      一个字都没出去。
      这时候正确的做法不是"这条算了"，而是顺着这个已经建好的会话补一句 ——
      桶已经提到井边了，别把水倒掉。

    安全上跟首触那边一样严：
      1 只认岗位 ID 完全相同（不靠公司名猜，历史招呼语有 9 条一字不差）
      2 会话最后一句必须是平台模板话（说明还没人回、也没重复发过）
      3 打字之后逐字回读比对
      4 发完再用会话接口对一遍账
    """
    jid = str(item.get("job_id") or "")

    F.goto(tab, "https://www.zhipin.com/web/geek/chat", wait=6)
    wait_render(tab, "聊天页会话列表")

    risk = risk_check(tab)
    if risk:
        return "halt", f"聊天页出现「{risk}」，停"

    # 重新读一遍会话列表，不直接用传进来的那条（点完到这儿可能过了好几秒）
    convs = friend_list(tab)
    hit = [c for c in convs if (c.get("job_id") or "") == jid]
    if len(hit) != 1:
        return "skip", (f"按岗位 ID 命中 {len(hit)} 个会话（不是唯一）"
                        f"→ 不敢靠公司名猜，跳过")
    conv = hit[0]

    if conv["theirs"] and not conv["mine"]:
        return "skip", f"「{conv['company']}」对方已经回话了，交给人工处理"

    last = (conv.get("last") or "").strip()
    # 注意用 sent_matches（前缀比对）而不是 same_text（逐字比对）：
    #   接口返回的 lastMsg 会被截断（大约 50 字），而我们这句快 200 字，
    #   逐字比必然对不上，会把"已经发过了"误判成"情况不明"
    if sent_matches(text, last):
        return "skip", "这句已经在这个会话里了，不重复发"
    if not sent_matches(PLATFORM_DEFAULT, last):
        return "skip", (f"会话最后一句既不是平台模板话、也不是我们要发的这句"
                        f"（是「{last[:24]}」）→ 情况不明，跳过")

    opened, why = _open_conv(tab, conv, first_text=PLATFORM_DEFAULT,
                             job_title=item.get("title"))
    if not opened:
        return "skip", f"打不开会话：{why}"

    # 打字 + 逐字回读
    for attempt in (1, 2):
        if not type_text(tab, text):
            return "halt", "找不到输入框，停"
        back = read_input(tab).strip()
        if back == text:
            break
        if attempt == 2:
            clear_input(tab)
            return "halt", f"回读两次都对不上，停（实际「{back[:20]}…」）"
        time.sleep(1.0)
    else:
        return "halt", "回读失败，停"

    if dry:
        clear_input(tab)
        return "dry", ("演练通过：平台已代发模板话（第一条）"
                       f"，我们这句会作为第二条补上　抬头：{why}")

    if not click_send(tab):
        clear_input(tab)
        return "halt", "点不到发送按钮，停"
    time.sleep(random.uniform(2.5, 4.0))

    # 发完对账
    again = friend_list(tab)
    now = [c for c in again if c["uid"] == conv["uid"]]
    if not now:
        return "halt", "发完之后列表里找不到这个会话了，去页面人工确认"
    if not sent_matches(text, now[0]["last"]):
        return "halt", f"发完回读对不上，停（会话最后一条「{now[0]['last'][:30]}」）"
    return "ok", (f"已补发成功：平台模板话是第一条，我们这句是第二条"
                  f"（抬头：{why}）")


# ─────────────────────────────────────────────────────────────
# 跟进：给"发出去没人理"的那些补一句，不再悲崔
#
# 三条硬规矩（跟 research 结论一致）：
#   1 不复述：逐字比对，出现第一句任意连续 8 个字就废
#   2 有新东西：换一个没用过的 JD 锚点，或换一条没说过的简历经历
#   3 长度不超过第一句的一半：长了就成了催，不是补
#
# 用法：
#   python send_cdp.py follow             列出能跟进的清单 + 演练（默认不发！）
#   python send_cdp.py follow --send      真发（第一次别用，先看清单）
#   python send_cdp.py follow --limit 5   只处理 5 条
# ─────────────────────────────────────────────────────────────

FOLLOW_MIN_HOURS = 72      # 发出去 72 小时之内不催，太急
FOLLOW_MAX_DAYS = 7        # 超过 7 天不跟了，那时候人家早招到人了


def load_ledger():
    if not os.path.exists(LEDGER):
        return [], list(_run.FIELDS)
    with open(LEDGER, encoding="utf-8") as f:
        rd = csv.DictReader(f)
        rows = list(rd)
    # 2026-11-12 修：表头统一用 run.FIELDS（唯一权威），不再从读进来的文件取。
    # 之前用 rd.fieldnames，字段演进（加 variant_tags/ask_resume）后，旧文件表头
    # 缺新列，写回时 DictWriter 字段漂移 → 大量行被读成空 job_id → 读改写时被冲掉。
    return rows, list(_run.FIELDS)


def load_jobs_with_jd():
    """跟进句要靠 JD 找新锚点，所以必须能拿到岗位描述"""
    jobs = []
    for p in ("data/jobs_today.json", "data/jobs_follow.json", "data/jobs_list.json"):
        fp = path_of(p)
        if os.path.exists(fp):
            with open(fp, encoding="utf-8") as f:
                jobs.extend(json.load(f))
    return jobs


def follow_candidates(verbose=True):
    """哪些可以跟进：发过了、还没人回、没跟过、发出 3 天以上但不超 7 天"""
    rows, _ = load_ledger()
    jobs = load_jobs_with_jd()
    by_id = {}
    for j in jobs:
        for k in (j.get("job_id"), j.get("encryptJobId"), j.get("url")):
            if k:
                by_id[str(k)] = j

    now = time.time()
    out = []
    for r in rows:
        if (r.get("sent") or "") != "已发":
            continue
        if (r.get("replied") or "").strip():
            continue
        if (r.get("followup_sent") or "").strip():
            continue
        if not (r.get("greeting_text") or "").strip():
            continue
        t0 = _parse_time(r.get("send_time") or "")
        if t0 is None:
            continue
        age_h = (now - t0) / 3600
        if age_h < FOLLOW_MIN_HOURS:
            continue
        if age_h > FOLLOW_MAX_DAYS * 24:
            continue
        job = by_id.get(str(r.get("job_id") or ""), {})
        out.append({"row": r, "job": job, "age_h": age_h})
    return out


def _parse_time(s):
    s = (s or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return time.mktime(time.strptime(s, fmt))
        except Exception:
            continue
    return None


def process_follow(tab, cand, dry=True, verbose=True):
    """跟进一条。返回 (结果, 说明)"""
    row, job = cand["row"], cand["job"]
    first_text = (row.get("greeting_text") or "").strip()

    # ① 绑定校验：找到这条会话，并且确认"最后一条就是我们当初发的那句"
    #    跟进是在老会话里操作，最容易张冠李戴，所以这里比首触那边更严：
    #    —— 只认【岗位 ID 完全相同】。
    #    为什么不用公司名去猜：这批历史招呼语有 9 条是一模一样的原话，
    #    靠公司名（甚至靠"最后一句对得上"）都可能对到别的公司头上。
    convs = friend_list(tab)
    jid = (row.get("job_id") or "").strip()
    hit = [c for c in convs if jid and (c.get("job_id") or "") == jid]
    if len(hit) != 1:
        return "skip", (f"按岗位 ID 找不到唯一会话（{row.get('company')}，"
                        f"命中 {len(hit)} 个）→ 不敢靠公司名猜，跳过")
    conv = hit[0]
    if conv["theirs"] and not conv["mine"]:
        return "skip", f"「{conv['company']}」对方已经回话了，交给人工处理"
    if not same_text(conv["last"], first_text):
        return "skip", (f"会话最后一句跟台账记的对不上，不敢跟。"
                        f"会话里是「{(conv['last'] or '')[:20]}」，台账记的是「{first_text[:20]}」")

    # ② 生成跟进句：找不到新东西就不跟
    import greeting
    try:
        fu = greeting.draft_followup(job, first_text) if job else None
    except Exception as e:
        return "skip", f"生成跟进句失败：{e}"
    if not fu or not (fu.get("text") or "").strip():
        return "skip", "找不到能说的新东西（JD 锚点和经历都用完了）→ 宁可不跟"

    text = fu["text"]

    # ③ 内容自洽：跟首句那边的检查 3 一样，只是换成跟进句
    ok, why = verify_text({**row, "greeting_text": text}, text, [])
    if not ok:
        return "skip", f"跟进句内容自洽没过：{why}"

    if dry:
        return "dry", f"[{'算了' if fu.get('low_confidence') else '可用'}] {text}"

    # ④ 打开这个会话 → 打字 → 逐字回读 → 发
    F.goto(tab, "https://www.zhipin.com/web/geek/chat", wait=6)
    wait_render(tab, "聊天页会话列表")

    risk = risk_check(tab)
    if risk:
        return "halt", f"聊天页出现「{risk}」，停"

    opened, why = _open_conv(tab, conv, first_text=first_text,
                             job_title=row.get("title"))
    if not opened:
        return "skip", f"打不开会话：{why}"

    info0 = conv_read(tab)
    n0 = info0.get("n") or 0

    if not type_text(tab, text):
        return "halt", "找不到输入框，停"
    time.sleep(0.8)
    back = read_input(tab).strip()
    if not same_text(back, text):
        clear_input(tab)
        return "halt", f"回读对不上，停（预期「{text[:16]}…」实际「{back[:16]}…」）"

    # 打字期间不许冒出新消息 —— 冒出来说明平台替我们做了动作
    info1 = conv_read(tab)
    if n0 and (info1.get("n") or 0) > n0:
        clear_input(tab)
        return "halt", f"打字期间会话里多出了 {(info1.get('n') or 0) - n0} 条消息，先算了，停"

    if not click_send(tab):
        clear_input(tab)
        return "halt", "点不到发送按钮，停"
    time.sleep(random.uniform(2.5, 4.0))

    # ⑤ 发完再确认：会话最后一条必须就是刚刚这句
    again = friend_list(tab)
    now_conv = [c for c in again if c["uid"] == conv["uid"]]
    if not now_conv:
        return "halt", "发完之后在列表里找不到这个会话了，停"
    if not now_conv or not sent_matches(text, now_conv[0]["last"]):
        return "halt", (f"发完回读对不上，停（会话最后一条是"
                        f"「{(now_conv[0]['last'] if now_conv else '')[:30]}」）")
    return "ok", text


JS_CONV_ITEMS = r"""
(() => {
  const arr = [];
  document.querySelectorAll('div.user-list li').forEach((e, i) => {
    arr.push([i, (e.innerText || '').replace(/[\r\n]+/g, ' ').slice(0, 140)]);
  });
  return JSON.stringify(arr);
})()
"""

JS_CLICK_ITEM = r"""
(() => {
  const list = document.querySelectorAll('div.user-list li');
  const el = list[__IDX__];
  if (!el) return 'NO_ITEM';
  const target = el.querySelector('.friend-content') || el;
  target.scrollIntoView({block: 'center'});
  target.click();
  return 'CLICKED';
})()
"""

JS_CONV_READ = r"""
(() => {
  const conv = document.querySelector('.chat-conversation');
  if (!conv) return null;

  // 抬头：2026-10-28 在真页面上逐个试出来的精确类名，别再用 [class*=name] 这种模糊匹配
  //  （模糊匹配会把右侧「竞争者PK情况」卡片的名字也捞进来，害得抬头对不上直接熔断）
  const heads = [];
  ['.name-text', '.base-title', '.position-name', '.base-info']
    .forEach(s => conv.querySelectorAll(s).forEach(e => {
      const t = (e.innerText || '').trim();
      if (t && t.length < 60 && heads.indexOf(t) < 0) heads.push(t);
    }));
  // 公司名在抬头里是个没有类名的裸 span，单独补一次
  const bar = conv.querySelector('.base-info') || conv;
  const first = bar.querySelectorAll('span');
  if (first.length >= 2) {
    const maybe = (first[1].innerText || '').trim();
    if (maybe && maybe.length < 40 && heads.indexOf(maybe) < 0) heads.push(maybe);
  }

  // 消息：只认 .text-content —— 这是气泡里那行字，别的卡片都不带这个类名
  const msgs = [];
  conv.querySelectorAll('.text-content').forEach(e => {
    const t = (e.innerText || '').trim();
    if (t) msgs.push(t.slice(0, 300));
  });

  // 输入框：必须是【看得见】的那个。聊天页里 textarea.input 是 0×0 的隐藏框，
  //  只判断"存不存在"会被它骗过去，写进去按钮会亮但永远发不出去 → 要看尺寸
  const inp = Array.from(document.querySelectorAll('textarea, [contenteditable=true]'))
    .find(e => { const r = e.getBoundingClientRect(); return r.width > 40 && r.height > 20; });

  return JSON.stringify({heads: heads.slice(0, 8),
                         last: msgs.length ? msgs[msgs.length - 1] : null,
                         n: msgs.length,
                         has_input: !!inp,
                         input_cls: inp ? (inp.className || '').toString().slice(0, 40) : null});
})()
"""


def conv_items(tab):
    """读会话列表的可见条目（用于定位要点的那一个）。"""
    for _ in range(3):
        raw = F.evaluate(tab, JS_CONV_ITEMS, timeout=30)
        if raw:
            try:
                return json.loads(raw)
            except Exception:
                return []
        time.sleep(2)
    return []


def conv_read(tab):
    """读当前打开的会话：抬头名字、最后一条、消息条数、有没有输入框。"""
    for _ in range(3):
        raw = F.evaluate(tab, JS_CONV_READ, timeout=30)
        if raw and raw != "null":
            try:
                return json.loads(raw)
            except Exception:
                return {}
        time.sleep(2)
    return {}


def _open_conv(tab, conv, first_text=None, job_title=None):
    """在会话列表里点开某一个对话。

    点之前先在 Python 侧把"要点哪一个"挑出来（按 HR 名字 + 公司名 + 岗位名匹配），
    点完立刻回读抬头核对 —— 点错了就当场停，绝不打字。
    返回 (True, 说明) / (False, 原因)
    """
    boss = (conv.get("boss") or "").strip()
    comp = (conv.get("company") or "").strip()
    btitle = (conv.get("boss_title") or "").strip()
    jt = (job_title or "").strip()

    items = conv_items(tab)
    if not items:
        return False, "读不到会话列表"

    idx = None
    how = ""
    # 四档匹配，从严到宽：HR名+公司名+岗位名 → HR名+公司名 → HR名+职位名 → 只认 HR 名
    # ★ 2026-10-29 加最严那一档：同一个 HR 常常挂着好几个岗位，
    #   列表里会并排出现两行「刘女士　上海阶跃星辰」，光靠人名+公司名分不清该点哪行
    #   （实测就这么撞上过，程序报"命中 2 个，取第一个（有风险）"）。
    #   岗位名在列表行里是有的，加上它就能唯一命中。
    for nick, need in (("HR名+公司名+岗位名", [boss, comp, jt]),
                       ("HR名+公司名", [boss, comp]),
                       ("HR名+职位名", [boss, btitle]),
                       ("只认HR名", [boss])):
        need = [x for x in need if x and len(x) >= 2]
        if len(need) < (2 if nick != "只认HR名" else 1):
            continue
        hits = [(i, t) for i, t in items if all(x in t for x in need)]
        if len(hits) == 1:
            idx, how = hits[0][0], f"{nick}唯一命中"
            break
        if hits and idx is None:
            idx, how = hits[0][0], f"{nick}命中 {len(hits)} 个，取第一个（有风险）"
    if idx is None:
        hits = [(i, t) for i, t in items if comp and comp[:6] in t]
        if hits:
            idx, how = hits[0][0], "退回按公司名匹配（最不准，谨慎）"

    if idx is None:
        return False, f"列表里找不到这家（{boss}｜{comp}）"

    r = F.evaluate(tab, JS_CLICK_ITEM.replace("__IDX__", str(idx)), timeout=30)
    if r != "CLICKED":
        return False, f"点不动这个会话：{r}"
    time.sleep(3.0)

    info = conv_read(tab)
    if not info:
        return False, "点开之后读不到聊天区"
    heads = info.get("heads") or []
    joined = " ".join(heads)
    # 抬头里必须有 HR 名字，才算点对了人
    if boss and boss not in joined:
        return False, f"点开的不是这个人（抬头是「{joined[:30]}」，要找「{boss}」）"
    # 抬头里还得有岗位名 —— 上面那两行同名的情况，人名对了也可能点错行。
    # 岗位名在列表里可能被截断，所以先比全名、再比前 4 个字，两个都对不上才算错。
    if jt and len(jt) >= 3 and not (jt in joined or jt[:4] in joined):
        return False, f"点开的不是这个岗位（抬头是「{joined[:30]}」，要找「{jt}」）"
    if not info.get("has_input"):
        return False, "点开了但没有输入框，不敢打字"
    if first_text:
        last = (info.get("last") or "").strip()
        if last and not same_text(last, first_text) and first_text[:12] not in last:
            return False, (f"打开后最后一条不是我们当初那句，"
                           f"不敢接话（会话里是「{last[:24]}」）")
    return True, f"打开成功（{how}；抬头：{joined[:24]}）"


# ─────────────────────────────────────────────────────────────
# 跟进分级：高 / 中 / 低
#
# 判断只用硬规则，不调模型（延迟不划算，而且错了不可逆）：
#   高 = 有新 JD 锚点【并且】有新简历证据（跟 JD 对得上）
#   中 = 有新 JD 锚点，但没有对得上的经历
#   低 = 两边都没新东西，或者被标了低把握
#         低把握的机器不擅自发 —— 列成清单，人勾了才发。
#         为什么不全砍：砍掉的话跟进只覆盖两成多，跟进本来就是靠覆盖堆出来的。

def grade_followup(job, first_text):
    """返回 (等级, 理由, 生成结果)。生成不出来返回 ("低", …)"""
    import greeting
    try:
        fu = greeting.draft_followup(job, first_text) if job else None
    except Exception as e:
        return "低", f"生成失败：{e}", None
    if not fu or not (fu.get("text") or "").strip():
        return "低", "找不到能说的新东西", None
    if fu.get("low_confidence"):
        return "低", "新锚点或新经历跟这条 JD 都对不上（低把握）", fu
    has_anchor = bool((fu.get("jd_anchor") or "").strip())
    has_evid = bool((fu.get("resume_anchor") or "").strip())
    if has_anchor and has_evid:
        return "高", "有新 JD 锚点 + 一条对得上的经历", fu
    if has_anchor:
        return "中", "有新 JD 锚点，但没有对得上的新经历", fu
    return "低", "两边都没有新东西", fu


def write_pending_list(cands_with_grade):
    """把低把握的写成清单，等人勾。"""
    import csv, os
    p = path_of(f"docs/跟进待确认-{time.strftime('%Y%m%d')}.md")
    lines = ["# 跟进待确认清单（低把握，机器没擅自发）", "",
             "> 你勾哪几条（比方回我「发 1 和 3」），我就发哪几条。", "",
             "| 序号 | 公司 | 岗位 | 为什么不自动发 | 打算发的话 |", "|---|---|---|---|---|"]
    for i, (c, g, reason, fu) in enumerate(cands_with_grade, 1):
        lines.append(f"| {i} | {(c['row'].get('company') or '')[:14]} | "
                     f"{(c['row'].get('title') or '')[:16]} | {reason} | "
                     f"{(fu or {}).get('text', '')[:60]} |")
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return p


def cmd_follow(tab, do_send, limit):
    cands = follow_candidates()
    if not cands:
        print("没有可以跟进的。（要么台账里没有「已发且没回」的，要么都还没到 72 小时）")
        return
    print(f"能跟进的 {len(cands)} 条（发出 3 天以上、7 天以内、没回、没跟过）：")
    print()
    todo = cands[:limit] if limit else cands

    # 先给每条定级（这一步不碰页面，纯本地算）
    graded, low, picked = [], [], None
    for c in todo:
        g, reason, fu = grade_followup(c["job"], (c["row"].get("greeting_text") or "").strip())
        graded.append((c, g, reason, fu))
        if g == "低":
            low.append((c, g, reason, fu))
    if low and not do_send:
        print(f"其中 {len(low)} 条是【低把握】：新锚点和经历跟这条 JD 都对不上 —— "
              f"机器不擅自发，只列清单。")
        print("（真要发的话加 --include-low）")

    if low and not INCLUDE_LOW:
        todo = [t for t in graded if t[1] != "低"]
        p = write_pending_list(low)
        print(f"低把握清单 → {p}")
        if not todo:
            print("剩下的没有能自动发的了。清单你勾完回来我再发。")
            return
    else:
        todo = graded

    dry_n = ok_n = skip_n = 0
    for i, (c, g, reason, fu) in enumerate(todo, 1):
        r = c["row"]
        res, note = process_follow(tab, c, dry=not do_send)
        flag = {"ok": "✓ 已跟进", "dry": "· 演练", "skip": "× 跳过", "halt": "⛔ 停"}[res]
        print(f"[{i}/{len(todo)}] {flag}［{g}把握］　{r.get('company','')[:14]}　{r.get('title','')[:18]}")
        print(f"　　　{note}")
        if res == "ok":
            ok_n += 1
            _mark_followup(r.get("job_id", ""), time.strftime("%Y-%m-%d %H:%M:%S"), note)
        elif res == "dry":
            dry_n += 1
        elif res == "skip":
            skip_n += 1
        else:
            break
        # 每 10 条停 15 秒做一次轻量巡检
        if i % 10 == 0 and do_send:
            ok_so_far = ok_n
            problem = light_patrol(tab, ok_so_far, i, skip_n)
            if problem:
                print()
                print(f"⛔ 事中巡检在第 {i} 条亮红灯：{problem}，停。")
                break
            time.sleep(15)

        if res != "dry" and i < len(todo):
            time.sleep(greet_interval_seconds())
    print()
    print(f"跟进发出 {ok_n} 条，演练 {dry_n} 条，跳过 {skip_n} 条")
    if not do_send:
        print()
        print("上面是演练，一条都没真发。确认话没问题，加 --send 才会真发。")


def _mark_followup(job_id, when, text):
    rows, fields = load_ledger()
    if not rows:
        return
    for r in rows:
        if (r.get("job_id") or "") == job_id:
            r["followup_sent"] = "已跟"
            r["followup_time"] = when
            r["followup_text"] = text
    with open(LEDGER, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


# ─────────────────────────────────────────────────────────────
# 台账
# ─────────────────────────────────────────────────────────────

def append_log(rows):
    fresh = not os.path.exists(SENDLOG) or os.path.getsize(SENDLOG) == 0
    with open(SENDLOG, "a", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        if fresh:
            w.writerow(["time", "job_id", "company", "title", "result", "note"])
        w.writerows(rows)


def mark_ledger(job_id, sent, verify, item=None):
    """把发送结果回写到台账，这样 60 条基线才统计得准。

    2026-11-11 加：发成「已发」时回填 send_time（真实发送时间戳）。
    之前 send_time 只在 run.py 记账时写了个「记账时间」，发送后从不更新，
    导致 1141 条台账 send_time 全空/全是记账时间，「90 天窗口」没法算。

    2026-11-15 修（治本）：原来只「更新」不「新增」——发送成功的岗位若不在台账里
    （run.py 没记它，比如直接进的队列），mark_ledger 找不到对应行就静默跳过，
    结果「发了但台账里永远没有这条」，判重和回复率分母被污染。
    现在找不到行时，用 item 里的岗位信息补一行进去，杜绝孤儿记录。
    """
    if not os.path.exists(LEDGER):
        return
    with open(LEDGER, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    # 2026-11-12 修：表头统一用 run.FIELDS，不再从 rows[0].keys() 取。
    # 旧文件表头缺新列时，字段漂移会把大量行冲掉（本次台账被清到 1 行的根因之一）。
    fields = list(_run.FIELDS)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    hit = False
    for r in rows:
        if (r.get("job_id") or "") == job_id:
            hit = True
            r["sent"] = sent
            r["verify"] = verify
            if sent == "已发":
                r["send_time"] = now
    # 找不到行：新增一行，保证「发过的岗位一定在台账里」。
    # 字段能拿多少拿多少；拿不到的留空，绝不编造。
    if not hit and item:
        g = (item.get("greeting_text") or "").replace("\n", " ")
        rows.append({
            "send_time": now,
            "job_id": job_id,
            "company": item.get("company") or "",
            "city": item.get("city") or "",
            "title": item.get("title") or "",
            "family": item.get("family") or "",
            "score": item.get("score") or "",
            "salutation_variant": "",
            "closing_variant": "",
            "word_count": str(len(g)) if g else "",
            "has_jd_anchor": "",
            "variant_tags": "",
            "greeting_text": g,
            "role_cluster": "",
            "read_24h": "", "read_72h": "", "replied": "", "ask_resume": "",
            "notes": "",
            "sent": sent, "verify": verify,
            "followup_sent": "", "followup_time": "", "followup_text": "",
            "source": "机器",
        })
    with open(LEDGER, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


# ─────────────────────────────────────────────────────────────
# 三个命令
# ─────────────────────────────────────────────────────────────

def cmd_probe(tab, queue):
    """探选择器，不点任何东西。

    ⚠ 2026-10-29 改：原来只打开岗位详情页就数一遍，可【输入框 / 发送按钮 /
      会话头部 / 最后一条消息】这四个都只在聊起来之后才存在 —— 详情页上数必然全是 0，
      探了等于没探（真出问题时它会告诉你"一切正常"）。
      现在分两处探：详情页只探「沟通按钮」，聊天那四个去聊天页探。
    """
    print("探一次：不点任何东西，只数页面上有什么")
    print()

    if queue:
        item = queue[0]
        url = item.get("url") or ""
        if url:
            F.goto(tab, url if url.startswith("http")
                   else "https://www.zhipin.com" + url, wait=5)
            print(f"① 岗位详情页：{item.get('title')}　{item.get('company')}")
            btns = F.evaluate(tab, """
            (() => Array.from(document.querySelectorAll('button, a, span, div[class*="btn"]'))
                .map(e => (e.innerText||'').trim())
                .filter(t => t && t.length < 10)
                .slice(0, 40))()""") or []
            print("　页面上的短按钮文案（确认沟通按钮叫什么）：")
            print("　　" + "　".join(dict.fromkeys(btns)))
            for s in SEL_DETAIL_BTN:
                n = F.evaluate(tab, f"document.querySelectorAll({json.dumps(s)}).length")
                print(f"　{'有货 ✓' if n else '　空'}　{n} 个　{s}")
            print()

    F.goto(tab, "https://www.zhipin.com/web/geek/chat", wait=6)
    time.sleep(3)
    print("② 聊天页：这里才有输入框 / 发送按钮 / 会话头部 / 最后一条消息")
    print()
    for name, sels in SEL.items():
        print(f"【{name}】")
        for s in sels:
            n = F.evaluate(tab, f"document.querySelectorAll({json.dumps(s)}).length")
            mark = "有货 ✓" if n else "　空"
            print(f"　{mark}　{n} 个　{s}")
        print()
    print("把上面这段输出发我，我照着把选择器改准。")


def sent_today():
    today = time.strftime("%Y-%m-%d")
    if not os.path.exists(SENDLOG):
        return False
    with open(SENDLOG, encoding="utf-8") as f:
        for row in csv.reader(f):
            if row and row[0].startswith(today):
                return True
    return False


def sent_today_count():
    """数今天已经真发出去了几条（只看 send_log.csv 里 result=ok 的记录）。

    用来卡「单日投递上限」（daily_greet_cap）。之前只有 sent_today() 判
    「今天发没发过」，没有「发了几条」，导致 daily_greet_cap 这个配置
    其实没有代码读它 —— 分两轮跑 send，一天照样能超上限（2026-11-01 修）。
    """
    today = time.strftime("%Y-%m-%d")
    if not os.path.exists(SENDLOG):
        return 0
    n = 0
    with open(SENDLOG, encoding="utf-8", newline="") as f:
        for row in csv.reader(f):
            # 行格式：time, job_id, company, title, result, note
            if row and row[0].startswith(today) and len(row) >= 5 and row[4] == "ok":
                n += 1
    return n


def first_of_day_ok(todo):
    """
    每天第一条给你看一眼（约 30 秒）。
    这是整套自动化里唯一还留着的一只眼睛 —— 一天里只要有一次是真的，
    剩下 99 条出错的概率才有人兜着。
    """
    if DELIV.get("human_gate") != "first_of_day":
        return True
    if sent_today():
        return True
    it = todo[0]
    print()
    print("=" * 70)
    print("今天第一条，按配置要你看一眼（30 秒）：")
    print()
    print(f"　公司：{it.get('company')}　　城市：{it.get('city')}")
    print(f"　岗位：{it.get('title')}　　　分数：{it.get('score')}")
    print(f"　链接：{it.get('url')}")
    print()
    print(f"　招呼语：{it.get('greeting_text')}")
    print("=" * 70)
    try:
        ans = input("发给这个人？[y 回车=发 / n=今天不发]　").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return ans in ("", "y", "yes")


def load_parked():
    """读归档文件（跳太多收走的那些）。文件坏了就当空的，不能因为归档挂了就不发。"""
    try:
        with open(QUEUE_PARK, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except FileNotFoundError:
        return []
    except Exception as e:
        print(f"（归档文件读不动，先按空的算：{e}）")
        return []


def _handle_skip(item, queue, note):
    """跳过一条时，决定是「挪队尾」还是「收走」，并动队列。

    规矩（见文件头 48-70 行注释）：
      - 命中 SKIP_PARK_IMMEDIATE 的原因（90 天投过 / 没地址 / 已发过 / 对方回话）
        → 条件永远不会自己变好，当场收走，不再占位。
      - 其余原因 → skip_count +1；攒够 SKIP_PARK_AFTER 次收走，否则挪到队尾
        （给自己留一次机会，也给别的岗位让路）。
    """
    immediate = note.startswith(SKIP_PARK_IMMEDIATE)
    if immediate:
        park_item(item, note)
        _remove_from_queue(queue, item)
        return

    item["skip_count"] = item.get("skip_count", 0) + 1
    item["last_skip_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    item["last_skip_note"] = (note or "")[:120]

    if item["skip_count"] >= SKIP_PARK_AFTER:
        park_item(item, f"连跳 {item['skip_count']} 次：{note}")
        _remove_from_queue(queue, item)
    else:
        # 挪队尾：先从当前位置拿掉，再 append 到末尾。
        _remove_from_queue(queue, item)
        queue.append(item)
        print(f"　　→ 第 {item['skip_count']} 次跳过，挪到队尾（下轮排最后）")


def _remove_from_queue(queue, item):
    """按对象身份（is）从队列里摘掉一条（原地改）。

    为什么用 is 不用 job_id：todo 是 queue 的切片，元素是同一个 dict 引用，
    用 is 摘绝对准，不会误删 job_id 为空或重复的其他条目。
    """
    for idx, q in enumerate(queue):
        if q is item:
            queue.pop(idx)
            return


def park_item(item, reason, verbose=True):
    """把一条挪出队列、记进归档。同一个岗位只留一条记录（重跑不会重复堆）。"""
    rec = {k: v for k, v in item.items()
           if k not in ("skip_count", "last_skip_at", "last_skip_note")}
    rec["parked_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    rec["parked_reason"] = (reason or "")[:160]
    parked = [p for p in load_parked()
              if (p.get("job_id") or "") != (item.get("job_id") or "")]
    parked.append(rec)
    try:
        tmp = QUEUE_PARK + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(parked, f, ensure_ascii=False, indent=1)
        os.replace(tmp, QUEUE_PARK)
    except Exception as e:
        print(f"（写归档失败，这条会留在队列里：{e}）")
        return
    if verbose:
        print(f"　　→ 收走：「{item.get('company')}」{reason[:40]}（记进 data/send_queue_skipped.json）")


def save_queue(items):
    """把队列写回去。先留一份 .bak-last 再整体覆盖，写坏了还能捡回来。"""
    try:
        if os.path.exists(QUEUE):
            shutil.copyfile(QUEUE, QUEUE + ".bak-last")
        tmp = QUEUE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=1)
        os.replace(tmp, QUEUE)
        return True
    except Exception as e:
        print(f"⛔ 写回队列失败：{e}")
        print("　队列文件还是旧的，下次重跑会重新处理同一批（不会漏发，只是多做一遍）。")
        return False


def cmd_send(tab, queue, dry, limit):
    # ★ 2026-11-07 加：冷却闸门。发送环节撞风控（比如「安全验证」）后会记冷却，
    #   冷却期内直接拒跑 —— 不然重跑会立刻再撞同一个验证，把连续触发次数
    #   从 1 推到 2、3，冷却时长翻倍到几小时。dry 演练不碰风控，放行。
    if not dry and F.in_cooldown():
        left = F.cooldown_left()
        print(f"⛔ 还在风控冷却期，剩 {int(left // 60)} 分 {int(left % 60)} 秒，先别发。")
        print("　冷却期内重跑只会再撞一次，把冷却时长翻倍。")
        print("　做法：到浏览器里手动打开 BOSS，有验证就过一下，等恢复后再跑。")
        return

    # ★ 2026-10-29 加：先把【台账里已经记成"已发"】的剔掉。
    #   原来是整条队列原样再跑一遍，发过的条目会被重新打开、重新判一遍，
    #   白白耗掉"连败 3 次就停"的额度 —— 实测就是这么停在第 3 条的。
    try:
        done = set()
        if os.path.exists(LEDGER):
            with open(LEDGER, encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    if (row.get("sent") or "") == "已发":
                        done.add(row.get("job_id") or "")
        pending = [it for it in queue if (it.get("job_id") or "") not in done]
        if len(pending) < len(queue):
            print(f"队列 {len(queue)} 条，其中 {len(queue) - len(pending)} 条台账已记『已发』，跳过不再处理")
        queue = pending
    except Exception as e:
        print(f"（读台账过滤已发失败，按原队列跑：{e}）")

    if not queue:
        print("队列里都是已发过的了，没有要发的。")
        return

    cap = limit or DELIV.get("per_session_cap", 30)

    # ★ 2026-11-01 加：单日投递上限（daily_greet_cap），真正卡住「一天不超过 N 条」。
    #   之前 daily_greet_cap 写了但没代码读它，分两轮跑 send 就超了。现在发之前
    #   先数今天已经发了几条，把本轮截断到「还差多少到上限」。
    # ★ 2026-11-07 改：取消隔天减量，每天固定投满 daily_greet_cap（=80）。
    #   节奏缓冲改由「分波发送 + 波间歇」提供（见主循环里的 wave 逻辑）。
    day_cap = day_cap_today()
    already = sent_today_count()
    room = day_cap - already
    if room <= 0 and not dry:
        print(f"今天已经发了 {already} 条，达到单日上限 {day_cap} 条，今天到此为止。")
        return

    # ★ 2026-11-07 改：额度按「真发出去几条」算，不按「点开几条」算。
    #   以前是先把队列切前 N 条再跑，前 N 条里只要有几条投不了（跳过），
    #   那几条额度就白扔了。现在跳过的让出名额，接着往下找 —— 只要队列里还有
    #   能发的，今天就能发满，不用人再去手动改 json。
    budget = cap if dry else min(cap, max(room, 0))
    if budget <= 0:
        print("今天没有可发的额度了。")
        return
    if not dry and room < cap:
        print(f"单日上限 {day_cap} 条，今天已发 {already} 条，本轮最多发 {budget} 条")

    # 跳过的也要开一次页面才判得出来，所以再给个总条数上限：
    # 万一整队都是投不了的，别一直往下翻到天亮。
    max_attempt = min(len(queue), max(budget * 3, budget + 5))

    if not dry and not first_of_day_ok(queue[:budget]):
        print("今天不发。")
        return

    # code 分级闸：发之前探一次接口 code，命中 36/32/1006 就停（37 单独提示）
    if not dry:
        code, cmsg = api_code_probe(tab)
        policy = DELIV.get("risk_code_policy") or {}
        if code is not None and str(code) != "0" and str(code) in policy:
            fatal, verdict = risk_code_verdict(code)
            print(f"⛔ 接口返回 {verdict}" + (f"（{cmsg}）" if cmsg else ""))
            if fatal:
                print("　按 risk_code_policy 全停，今天不再发。请人工检查账号状态。")
                return
            # code=37 分两类（2026-11-07 改，v2 方案②③）：
            #   token 过期类 → 重载页面重生成 stoken 再重试一次，可自动恢复
            #   环境风险类 → 重试仍 37 才判定，停发
            print("　code=37：先重载 BOSS 页重生成 token，再重试一次……")
            code2, msg2 = _recover_token(tab)
            if code2 is not None and str(code2) == "37":
                print("　重试后仍是 code=37，判定为环境风险，今天先停，请跑 token_diag.py --deep 定位。")
                return
            if code2 is not None and str(code2) != "0":
                print(f"　重试后 code={code2}（{msg2}），仍异常，今天先停。")
                return
            print("　重载后恢复正常，继续发送。")
        if code is not None and str(code) != "0" and str(code) not in policy:
            # 未知 code（不在分级表，如脏读的 7）：api_code_probe 已重试过仍读不到数据，
            # 说明页面可能没稳定。再等 3 秒重探一次，仍异常才提示但不硬发。
            time.sleep(3)
            code2, cmsg2 = api_code_probe(tab)
            if code2 is not None and str(code2) != "0":
                print(f"⛔ 接口连续返回异常 code={code2}，为稳妥起见今天先停，请人工检查。")
                return
            print(f"　（第一次探到未知 code={code}，重探已恢复正常，继续）")

    # 本轮要处理的条目：整条队列，但最多只翻 max_attempt 条（防止整队都投不了时翻到天亮）。
    #   跳过的条目会在下面被挪队尾/收走，所以这里直接切片就够了，不用再复制。
    todo = queue[:max_attempt]

    print(f"{'演练' if dry else '真发'}：队列 {len(queue)} 条，本轮 {len(todo)} 条")
    print(f"每条间隔重尾分布（70% 短 12-40 秒 + 30% 长 1-5 分钟），故意慢")
    print()

    results, consec_fail = [], 0
    for i, item in enumerate(todo, 1):
        res, note = process_one(tab, item, queue, dry=dry)
        results.append((item, res, note))
        flag = {"ok": "✓ 已发", "dry": "· 演练", "skip": "× 跳过", "halt": "⛔ 停"}[res]
        print(f"[{i}/{len(todo)}] {flag}　{item.get('company','')[:14]}　{item.get('title','')[:20]}　{note}")

        # 实时心跳：告知前端"正在发第 i/total 条、当前哪家公司哪个岗位"
        try:
            import live_status
            live_status.touch(step="send", running=True,
                              done=i, total=len(todo),
                              company=item.get("company", ""),
                              title=item.get("title", ""),
                              msg=f"{flag} {item.get('company','')} {item.get('title','')}")
        except Exception:
            pass

        if res == "ok":
            mark_ledger(item.get("job_id", ""), "已发", "ok", item=item)
            consec_fail = 0
        elif res == "dry":
            # dry 演练通过：不记账、不计连败，继续演练下一条。
            # （之前漏了这个分支，dry 落到 else 里直接 break，演练只跑一条就停）
            consec_fail = 0
        elif res == "skip":
            mark_ledger(item.get("job_id", ""), "未发", note[:40], item=item)
            # ① 软跳过（好事/防重/缺地址）不计连败，只有真失败才 +1
            if not note.startswith(SOFT_SKIP_NOTES):
                consec_fail += 1

            # ★ 2026-11-07 补：跳过要动队列（挪队尾/收走），不能再让它一直堵队头。
            #   以前这里只打印一行、队列一个字不改，那几个投不了的"钉子户"
            #   每轮都排最前、每轮都跳过一遍，白白吃掉当天额度。
            #   规矩（见文件头 48-70 行注释）：跳过不吃额度；跳一次挪队尾；
            #   跳第二次收走；有些原因（90 天投过/没地址等）一次就收走。
            #   只在真发时动队列 —— dry 演练不改任何文件。
            if not dry:
                _handle_skip(item, queue, note)
        else:
            mark_ledger(item.get("job_id", ""), "未发", note[:40], item=item)
            break

        # ② 连续真失败闸：连着 MAX_CONSECUTIVE_FAIL 条真失败才停
        if consec_fail >= MAX_CONSECUTIVE_FAIL:
            print()
            print(f"连着 {consec_fail} 条真失败，停。先看看是岗位问题还是页面问题。")
            break

        # ③ 滑动窗口跳过率闸：最近 LOOKBACK 条里真失败占比超 FAIL_RATIO 才停
        if i >= FAIL_LOOKBACK:
            window = results[-FAIL_LOOKBACK:]
            real_fail = sum(1 for _, r, n in window
                            if r == "skip" and not n.startswith(SOFT_SKIP_NOTES))
            if real_fail / FAIL_LOOKBACK >= FAIL_RATIO:
                print()
                print(f"最近 {FAIL_LOOKBACK} 条里真失败 {real_fail} 条（≥{int(FAIL_RATIO*100)}%），"
                      f"可能不是岗位问题而是系统问题，停。")
                break

        # 每 10 条停 15 秒做一次轻量巡检（事后报警 → 事中刹车）
        if i % 10 == 0 and not dry:
            ok_so_far = sum(1 for _, r, _ in results if r == "ok")
            problem = light_patrol(tab, ok_so_far, i, consec_fail)
            if problem:
                print()
                print(f"⛔ 事中巡检在第 {i} 条亮红灯：{problem}，停。")
                break
            time.sleep(15)

        if i < len(todo):
            time.sleep(greet_interval_seconds())

        # ★ 2026-11-07 加：分波发送。每发满 wave_size 条，就歇 wave_rest_seconds 秒，
        #   把「一口气灌 80 条」拆成「20 条一波、波间歇 30 分钟」，模拟真人分段投。
        #   只在真发时歇；dry 演练跳过（演练不碰风控，也没必要等 30 分钟）。
        if not dry and res == "ok":
            _ok_so_far = sum(1 for _, r, _ in results if r == "ok")
            wave_size = int(DELIV.get("wave_size", 20))
            wave_rest = int(DELIV.get("wave_rest_seconds", 1800))
            if wave_size > 0 and _ok_so_far % wave_size == 0 and i < len(todo):
                mins = wave_rest // 60
                print()
                print(f"⛱ 已发满 {_ok_so_far} 条（第 {_ok_so_far // wave_size} 波），"
                      f"歇 {mins} 分钟再发下一波。")
                time.sleep(wave_rest)

    append_log([(time.strftime("%Y-%m-%d %H:%M"), it.get("job_id", ""),
                 it.get("company", ""), it.get("title", ""), r, n[:60])
                for it, r, n in results])

    # ★ 2026-11-07 补：本轮动过队列（跳过被挪队尾/收走），把队列写回磁盘。
    #   dry 演练不动队列（_handle_skip 里已经拦了），这里写回的是"发过要剔除"
    #   之前那段逻辑改过的 pending 队列，也一起落盘。
    if not dry:
        save_queue(queue)

    ok_n = sum(1 for _, r, _ in results if r == "ok")
    print()
    print(f"本轮：发出 {ok_n} 条，跳过 {sum(1 for _,r,_ in results if r=='skip')} 条"
          f"，{'演练 ' + str(sum(1 for _,r,_ in results if r=='dry')) + ' 条' if dry else ''}")
    print(f"明细 → data/send_log.csv　发送结果已回写台账")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["probe", "dry-run", "send", "follow"])
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--send", action="store_true", help="follow 专用：不加这条只演练")
    ap.add_argument("--include-low", action="store_true",
                    help="follow 专用：低把握的也一起发（默认只列清单）")
    ap.add_argument("--pick", default="",
                    help="follow 专用：只发清单里的第几条，比如 1,3,5")
    args = ap.parse_args()

    if DELIV.get("mode") != "auto_cdp":
        print("配置里 delivery.mode 不是 auto_cdp，自动发送没开。")
        print("确认要开的话，把 config/target_roles.yaml 里的 mode 改成 auto_cdp。")
        return

    global INCLUDE_LOW
    if getattr(args, "pick", ""):
        INCLUDE_LOW = True                     # 人明确点了名 → 这些就是被勾过的
    elif getattr(args, "include_low", False):
        INCLUDE_LOW = True

    tab = F.require_tab()
    if tab is None:
        return

    if args.cmd == "follow":
        cmd_follow(tab, args.send, args.limit)
        return


    if not os.path.exists(QUEUE):
        print("没有 data/send_queue.json。先跑：python run.py --auto")
        return
    with open(QUEUE, encoding="utf-8") as f:
        queue = json.load(f)
    if not queue:
        print("队列是空的，今天没有要发的。")
        return

    if args.cmd == "probe":
        cmd_probe(tab, queue)
    else:
        cmd_send(tab, queue, dry=(args.cmd == "dry-run"), limit=args.limit)


if __name__ == "__main__":
    main()
