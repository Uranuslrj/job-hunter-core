# 第零层：把岗位抓下来
#
# 做法：连上你自己开着、自己扫码登录好的那个浏览器去读页面。
#   不用模拟登录、不用破解任何校验、不用任何反检测手段 ——
#   因为浏览器是真的、登录态是你自己的，读到的页面跟你肉眼看到的一模一样。
#   全程只读，不发任何东西。
#
# 用法：
#   python fetch_cdp.py check    看看本机浏览器在哪、调试端口通没通
#   python fetch_cdp.py scan     扫列表页（15 城 × 5 个搜索词）→ data/jobs_list.json
#   python fetch_cdp.py detail   对值得看的岗位打开详情页拿 JD 正文 → data/jobs_today.json
#   python fetch_cdp.py one      scan + detail 连着跑（第一次用这个）

import argparse
import json
import os
import random
import re
import sys
import time
import unicodedata
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config_loader import roles, path_of
import blocklist

CFG = roles()

# Boss 城市代码。⚠ 这组代码是常见的那套（和天气/招聘站点通用的 101xxxxx），
# 第一次跑完请核对一下抓回来的卡片上面的城市文字对不对。
# 就算代码写错也不会错投：下面 normalize 时用的是卡片自己写的城市文字，
# 真正把关的是 config/target_roles.yaml 里的 15 城白名单。
CITY_CODES = {
    "北京": "101010100", "上海": "101020100", "广州": "101280100", "深圳": "101280600",
    "杭州": "101210100", "武汉": "101200100", "长沙": "101250100", "南京": "101190100",
    "苏州": "101190400", "无锡": "101190200", "宁波": "101210400", "成都": "101270100",
    "西安": "101110100", "厦门": "101230200", "福州": "101230100",
}

# 默认搜这几个词。词太多会把请求量堆上去。
# ⚠️ 2026-11-05 本人拍板重新把「大模型」加回，并新增「AIGC」「算法产品」。
#   说明：11-01 曾删掉「大模型」——它是实测唯一反复触发 code=37 的高危词
#   （11-01 实测 5 个词，只有「大模型」瞬间 code=37）。本次 11-05 本人明知此风险
#   仍拍板三个词全加，接受可能再次触发风控。机器只执行不发挥，已记录风险。
#   「大模型」「算法产品」没有专属岗位族，搜出来的岗要靠模型读 JD 正文二次判断。
# ✅ 2026-11-06 补 FDE 等岗位族别名（本人要求「别名有的都补进搜索词」）：
#   之前只有「解决方案工程师」一个词捞 FDE，漏了交付/实施/客户成功/产品交付这些
#   叫法。补的词都是 config/target_roles.yaml 里 role_clusters 各族的 include 别名，
#   机器只铺别名、不发明新词。
DEFAULT_QUERIES = [
    "AI产品经理", "Agent产品经理", "AI应用产品经理",
    "AI产品开发", "AI产品实习生", "AI产品管培生", "Agent产品开发", "大模型产品开发",
    "AI产品运营", "AI应用运营", "AI社区运营",
    "解决方案工程师", "交付工程师", "实施顾问", "客户成功工程师", "AI产品交付",
    "AI项目运营", "AI项目管理", "PMO",
    "大模型", "AIGC", "算法产品",
    "AI应用开发", "大模型应用开发", "AI应用工程师", "Agent开发", "Prompt工程师",
]


def rotate_queries(queries):
    """每天换一个扫描顺序，打破「第一个词永远是 AI产品经理」的固定时序。

    用「今天是这一年的第几天」当种子做确定性洗牌：同一天内顺序稳定（方便
    --resume 断点续扫不串），跨天自动轮换。风控会看「每次扫描先碰哪个词」，
    固定排头是个可被识别的机械特征；轮换后每天第一个词都不一样。
    """
    if len(queries) <= 1:
        return list(queries)
    # 一年第几天 → 0..364，再 %len 得到「今天从第几个词开始排头」
    day_of_year = time.localtime().tm_yday
    start = day_of_year % len(queries)
    return queries[start:] + queries[:start]

SEARCH_URL = ("https://www.zhipin.com/web/geek/job"
              "?query={query}&city={city}&page={page}")

# ⚠ 2026-10-28 改版：不再抄页面卡片，改调页面同款接口。
# 原因：BOSS 把卡片上的薪资数字做了字形混淆（kanzhun-mix 字体，DOM 里是
# U+E032 这类私有区字符，抄出来全是"-K·薪"没有数字）。而页面自己就是从
# joblist.json 接口拿的数据，返回的是明文（salaryDesc="10-15K·13薪"），
# 且字段比卡片全：公司、行业、规模、福利、一周几天、HR 是否在线都有。
# 这是用户自己浏览器里发的同一个请求，读它和读页面是一回事。
JOBLIST_API = ("https://www.zhipin.com/wapi/zpgeek/search/joblist.json"
               "?scene=1&query={query}&city={city}&page={page}&pageSize=30")

JOBLIST_JS = """
(async () => {
  const url = __URL__;
  // 从 URL 里解出「这次到底搜的哪个词」，作为这个请求的"身份牌"，
  // 出错时跟着一起带回来。这样日志记的是「这个请求本身用了哪个词」，
  // 而不是外层循环走到哪一步（后者会把真凶记错，2026-11-01 教训）。
  let _q = "";
  try { _q = decodeURIComponent((url.match(/[?&]query=([^&]*)/) || [])[1] || ""); } catch (e) {}
  try {
    const r = await fetch(url, {credentials: "include"});
    const j = await r.json();
    if (j.code !== 0) {
      // 完整带回 BOSS 的原话：code + message + zpData 里的提示，别只截一个 code。
      return JSON.stringify({error: "code=" + j.code,
                             message: j.message || "",
                             zpDataMsg: (j.zpData && (j.zpData.message || j.zpData.msg)) || "",
                             query: _q});
    }
    const list = (j.zpData && j.zpData.jobList) || [];
    return JSON.stringify({count: list.length, jobs: list.map(x => ({
      _id: x.encryptJobId,
      title: x.jobName || "",
      city: x.cityName || "",
      district: x.areaDistrict || "",
      salary: x.salaryDesc || "",
      company: x.brandName || "",
      labels: (x.jobLabels || []).concat([x.jobExperience || "", x.jobDegree || ""]).filter(Boolean),
      boss_line: x.bossOnline ? "今日在线" : (x.bossTitle || ""),
      href: "/job_detail/" + x.encryptJobId + ".html",
      card_text: [
        (x.welfareList || []).slice(0, 8).join(" "),
        (x.skills || []).join(" "),
        [x.brandStageName, x.brandIndustry, x.brandScaleName].filter(Boolean).join(" "),
        x.daysPerWeekDesc || "",
        x.leastMonthDesc || ""
      ].filter(Boolean).join(" | "),
      // 下面这些是页面上不显示、但接口直接给的，正好是我们最想要的
      job_type: x.jobType || 0,
      days_per_week: x.daysPerWeekDesc || "",
      least_month: x.leastMonthDesc || "",
      welfare: x.welfareList || [],
      contacted: !!x.contact,          // 之前跟这家聊过没有 —— 去重直接用
      boss_name: x.bossName || "",
      boss_title: x.bossTitle || "",
      brand_industry: x.brandIndustry || "",
      brand_scale: x.brandScaleName || "",
      brand_stage: x.brandStageName || ""
    }))});
  } catch (e) { return JSON.stringify({error: String(e)}); }
})()
"""

ACTIVITY_MAP = CFG["hr_activity"]["field_map"]


# ─────────────────────────────────────────────────────────────
# 连浏览器
# ─────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────
# 2026-11-05 起：驱动层统一走 chrome-use 插件（不再开调试端口）。
# 调试端口是 code=37 环境异常的破绽（见 docs/插件路线落地方案）。
# 设 BOSS_DRIVER=plugin 时，所有「读页面/发接口」都走 chrome-use 插件，
# 驱动的就是你日常登录的 Chrome，BOSS 看到的是真实用户。
# ─────────────────────────────────────────────────────────────
USE_PLUGIN = os.environ.get("BOSS_DRIVER", "").strip().lower() == "plugin"


def _plugin():
    """懒加载 plugin_driver（避免没装 chrome-use 时 import 报错）。"""
    import plugin_driver as _pd
    return _pd


def port_open(port=None):
    # 插件模式不关心调试端口，直接探「插件能不能连上浏览器」
    return _plugin().current_url() is not None


# 插件模式下用的「哨兵标签页」。上层代码到处传 tab、调 evaluate(tab, js)，
# 我们让 evaluate/goto 忽略 tab，只认这个哨兵，这样所有调用点一行都不用改。
class _PluginTab:
    pass


_PLUGIN_TAB = _PluginTab()


# ─────────────────────────────────────────────────────────────
# 统一驱动入口：只走 chrome-use 插件（不开调试端口）
# send_cdp.py / fetch_cdp.py / chat_scan.py 的所有调用点都经这三个函数。
# ─────────────────────────────────────────────────────────────

def evaluate(tab, js, timeout=40):
    # 插件模式：在活动标签页执行 JS，返回 JS 表达式的值
    return _plugin().eval_js(js, timeout=timeout)


def goto(tab, url, wait=4.0):
    # 插件模式：open 到目标 URL，再等页面渲染
    _plugin().open_url(url, timeout=90)
    time.sleep(wait)


def cdp(tab, method, params, timeout=40):
    # 插件模式：把三类 CDP Input 调用翻译成 chrome-use 命令。
    # 只覆盖 send_cdp 用到的三种：点鼠标 / 写文字 / 按键，其余报错不静默。
    pd = _plugin()
    if method == "Input.dispatchMouseEvent":
        if (params or {}).get("type") == "mousePressed":
            # 只处理 mousePressed（点下去那一下），mouseReleased 交给 Chrome 自己
            x, y = int(params.get("x", 0)), int(params.get("y", 0))
            pd.click_coord(x, y, timeout=timeout)
        return None
    if method == "Input.insertText":
        pd.insert_text(params.get("text", "") or "", timeout=timeout)
        return None
    if method == "Input.dispatchKeyEvent":
        # 只处理 keyDown 的 Enter（keyUp/char 交给浏览器），避免重复触发
        if (params or {}).get("type") == "keyDown":
            k = (params or {}).get("key") or ""
            if k:
                pd.press_key(k, timeout=timeout)
        return None
    # 插件模式没覆盖的方法：打印一次，别静默丢
    print(f"  [plugin_driver] 未覆盖的 CDP 方法：{method}")
    return None


# ─────────────────────────────────────────────────────────────
# 列表页：把卡片挖出来
# ─────────────────────────────────────────────────────────────

LIST_JS = r"""
(() => {
  const cards = [];
  const seen = new Set();
  const blocks = document.querySelectorAll(
    'li[class*="job-card"], div[class*="job-card"], .job-list li, ul[class*="job-list"] li');
  blocks.forEach(c => {
    const pick = (sels) => {
      for (const s of sels) {
        const e = c.querySelector(s);
        if (e && e.innerText && e.innerText.trim()) return e.innerText.trim();
      }
      return '';
    };
    // 标题：优先 .job-name（纯标题），别用 [class*="job-title"]——
    // 实测 .job-title 是外层 div，innerText 会把薪资一起包进去（串了）。
    const title = pick(['.job-name', '[class*="job-name"]', '[class*="job-title"]']);
    if (!title) return;
    const a = c.querySelector('a[href*="job_detail"], a[href*="job"], a');
    const href = a ? (a.getAttribute('href') || '') : '';
    // 从 href 里抽出 encryptJobId（形如 /job_detail/xxxx.html），
    // 拿不到再退回 data-jobid。这样 normalize 里的 job_id 才是干净的
    // 32 位加密 ID，不会带上 ".html" 后缀。
    let jobId = '';
    const m = href.match(/job_detail\/([^/?#]+)/);
    if (m) jobId = m[1].replace(/\.html$/, '');
    else jobId = (c.getAttribute('data-jobid') || '') || href;
    // 公司名：在 footer 的 gongsi 链接里（a[href*="gongsi"]），
    // 别用 [class*="name"]（会误命中 .job-name 标题）。
    const compA = c.querySelector('a[href*="gongsi"], a[href*="company"]');
    const company = compA ? (compA.innerText || '').trim()
                   : pick(['[class*="company-name"]', '[class*="company"]']);
    if (seen.has(title + (href.slice(-24)))) return;
    seen.add(title + (href.slice(-24)));
    cards.push({
      _id: jobId,
      title,
      area: pick(['[class*="job-area"]', '[class*="area"]']),
      salary: pick(['[class*="salary"]']),
      company,
      labels: Array.from(c.querySelectorAll('[class*="tag"], .tag-item'))
                .map(e => e.innerText.trim()).filter(Boolean).slice(0, 8),
      boss_line: pick(['[class*="boss"]', '[class*="info-desc"]']),
      href,
      card_text: (c.innerText || '').replace(/\n+/g, ' | ').slice(0, 600),
    });
  });
  return {
    count: cards.length,
    url: location.href,
    cards,
    body_head: (document.body ? document.body.innerText : '').slice(0, 400),
  };
})()
"""

DETAIL_JS = r"""
(() => {
  const pickAll = (sels) => {
    const out = [];
    for (const s of sels) {
      document.querySelectorAll(s).forEach(e => {
        const t = (e.innerText || '').trim();
        if (t && t.length > 10 && !out.includes(t)) out.push(t);
      });
      if (out.length) break;
    }
    return out;
  };
  const jdParts = pickAll([
    '[class*="job-sec-text"]', '.job-detail-section [class*="text"]',
    '[class*="job-detail"] [class*="desc"]', '[class*="desc"]'
  ]);
  const tags = Array.from(document.querySelectorAll('[class*="job-tags"] span, [class*="tag"]'))
                    .map(e => (e.innerText || '').trim()).filter(Boolean);
  const bodyText = document.body ? document.body.innerText : '';
  // 发布时间：页面上写的是"刚刚/今天/3天前/7日内"这类话
  const pm = bodyText.match(/(刚刚|今天|昨天|[0-9]+\s*(分钟|小时|天)前|[0-9]+\s*日内|[0-9]+-[0-9]+-[0-9]+)/);
  // 薪资：详情页是明文的（实测"15-25K·15薪"，标准字体无字形混淆），
  // 正好补上列表页 DOM 里被 kanzhun-mix 混淆掉的薪资数字。
  // ⚠ 2026-11-12 修 bug：原来第一个选择器 '[class*="job-salary"]' 会命中
  // 'similar-job-salary'（页面底部的「相似岗位推荐」），拿到的是别人家的薪资，
  // 不是当前岗位。必须排除 similar 前缀，只认当前岗位那个 span.salary。
  let salary = '';
  const cands = Array.from(document.querySelectorAll('span.salary, [class*="job-salary"]'));
  for (const e of cands) {
    const cls = e.className || '';
    if (cls.indexOf('similar') >= 0) continue;   // 跳过「相似岗位」区
    const t = (e.innerText || '').trim();
    if (t) { salary = t; break; }
  }
  if (!salary) {
    // 兜底：若 span.salary 没命中，退回不带 similar 的任意 salary 元素
    for (const e of Array.from(document.querySelectorAll('[class*="salary"]'))) {
      const cls = e.className || '';
      if (cls.indexOf('similar') >= 0) continue;
      const t = (e.innerText || '').trim();
      if (t) { salary = t; break; }
    }
  }
  return {
    url: location.href,
    jd: jdParts.join('\n').slice(0, 8000),
    tags: tags.slice(0, 20),
    pub: pm ? pm[0] : '',
    salary: salary,
    full: bodyText.slice(0, 15000),
  };
})()
"""


# ─────────────────────────────────────────────────────────────
# 把卡片整理成下游能吃掉的样子
# ─────────────────────────────────────────────────────────────

def pick_city(*texts):
    """从一堆文字里挑出 15 城中的一个。挑不出来返回 ''。"""
    cities = CFG["hard_filter"]["city"]["cities"]
    for t in texts:
        t = t or ""
        for c in cities:
            if c in t:
                return c
    return ""


def log_risk(what):
    """记一笔风控，供 inspect.py 每天统计"今天触发几次"。

    记的是日期 + 原因，不含任何账号信息。
    """
    try:
        d = path_of("data")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "risk_events.log")
        with open(p, "a", encoding="utf-8") as f:
            f.write("%s\t%s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), what))
    except Exception:
        pass     # 记日志失败绝不能影响主流程


# ─────────────────────────────────────────────────────────────
# 风控冷却：触发过环境异常后，冷却期内拒绝重跑，别在风控里打转
# ─────────────────────────────────────────────────────────────

# 冷却多久。单位秒。第一次触发 30 分钟 —— 太短没用（风控不会这么快解除），
# 太长又耽误正事。但不固定：见下面 COOLDOWN_BASE / COOLDOWN_MAX，
# 冷却时长会随"连续触发次数"指数翻倍，防止固定 30 分钟变成"每 30 分钟自动撞一次"。
COOLDOWN_BASE = 30 * 60          # 第一次触发：30 分钟
COOLDOWN_MAX = 6 * 60 * 60       # 封顶 6 小时（对应腾讯云实战的指数退避锁）
COOLDOWN_SECONDS = COOLDOWN_BASE  # 兼容旧调用：无次数时按第一次算
# 冷却时间戳文件。里面就写一个 epoch 秒数，表示"这之前都别扫"。
COOLDOWN_FILE = "data/risk_cooldown.txt"
# 连续触发次数文件。触发一次 +1，成功恢复（探测四层全过）清零。
COOLDOWN_COUNT_FILE = "data/risk_cooldown_count.txt"


def _cooldown_until():
    """读回"冷却到什么时候"。没有或已过期返回 0。"""
    try:
        p = path_of(COOLDOWN_FILE)
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                return float(f.read().strip() or "0")
    except Exception:
        pass
    return 0.0


def _strike_count():
    """读回"连续触发了几次风控"。0 表示没有。"""
    try:
        p = path_of(COOLDOWN_COUNT_FILE)
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                return int(float(f.read().strip() or "0"))
    except Exception:
        pass
    return 0


def _cooldown_for_strikes(strikes):
    """按连续触发次数算冷却时长：30 分 → 60 → 120 → 240 → 封顶 6 小时。

    核心洞见（blockguard 库）：每次在封禁期里重试，都可能延长封禁。
    所以连续撞得越多，越要等得久，而不是每次都固定 30 分钟。
    """
    dur = COOLDOWN_BASE
    for _ in range(max(0, strikes - 1)):
        dur *= 2
        if dur >= COOLDOWN_MAX:
            return COOLDOWN_MAX
    return min(dur, COOLDOWN_MAX)


def set_cooldown(seconds=None):
    """触发风控后调用：连续触发次数 +1，按翻倍后的时长记"冷却到 now+seconds"。

    传 seconds 表示强制指定（比如读到 Retry-After 头时）；不传则按次数自动翻倍。
    """
    try:
        d = path_of("data")
        os.makedirs(d, exist_ok=True)
        strikes = _strike_count() + 1
        with open(path_of(COOLDOWN_COUNT_FILE), "w", encoding="utf-8") as f:
            f.write(str(strikes))
        if seconds is None:
            seconds = _cooldown_for_strikes(strikes)
        with open(path_of(COOLDOWN_FILE), "w", encoding="utf-8") as f:
            f.write(str(time.time() + seconds))
    except Exception:
        pass     # 写冷却失败不能让主流程崩


def clear_cooldown():
    """确认风控已恢复后调用：清掉冷却时间戳和连续触发次数，回到干净状态。"""
    try:
        for f in (COOLDOWN_FILE, COOLDOWN_COUNT_FILE):
            p = path_of(f)
            if os.path.exists(p):
                os.remove(p)
    except Exception:
        pass


def cooldown_left():
    """还差多久能解禁。0 表示没在冷却期。"""
    left = _cooldown_until() - time.time()
    return max(0.0, left)


def in_cooldown():
    """当前是否处在风控冷却期内。"""
    return cooldown_left() > 0


def deobfuscate(s):
    """BOSS 会把一部分常用汉字换成"康熙部首"区的字符（看起来像乱码，例如 ⾝⾊⾃）。

    例：页面上是"一个人⾝上"，其实是"一个人身上"。
    这些字符在 Unicode 里有标准对应关系，用 NFKC 就能还原成正常汉字。

    只动康熙部首这一个区（U+2F00–U+2FDF），别的字符一律不碰 ——
    免得把全角标点之类的也改了。
    注意：薪资的数字是另一种手法（私有区字符），这个办法还原不了，
    所以薪资走接口拿明文，不走这里。
    """
    if not s:
        return s
    out = []
    for ch in s:
        c = ord(ch)
        if 0x2F00 <= c <= 0x2FDF:          # 康熙部首区
            n = unicodedata.normalize("NFKC", ch)
            out.append(n if n != ch else ch)
        else:
            out.append(ch)
    return "".join(out)


def parse_publish_days(raw):
    """把页面上"刚刚 / 今天 / 3天前 / 7日内"这类话换成天数。认不出来返回 None。"""
    if not raw:
        return None
    raw = raw.strip()
    if any(w in raw for w in ["刚刚", "今天", "分钟", "小时"]):
        return 0
    if "昨天" in raw:
        return 1
    m = re.search(r"(\d+)\s*天前", raw)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d+)\s*日内", raw)
    if m:
        return int(m.group(1))
    return None


def guess_track(card=None, *texts):
    """判断是校招还是实习。

    接口给的字段比页面上那几个词准得多：
      · least_month（最少干几个月）/ days_per_week（一周几天）—— 有这两个一定是实习岗
      · 岗位名里带"实习" —— 也是实习
      · 标签里"在校/应届" —— 校招
    """
    card = card or {}
    if card.get("least_month") or card.get("days_per_week"):
        return "实习"
    blob = " ".join(t or "" for t in texts)
    if "实习" in blob:
        return "实习"
    if "校招" in blob or "应届" in blob:
        return "校招"
    return "未知"


def guess_activity(*texts):
    """把 Boss 写的活跃度文案换成配置的等级代号；认不出来按最保守的 d15 处理。"""
    blob = " ".join(t or "" for t in texts)
    for word, code in ACTIVITY_MAP.items():
        if word in blob:
            return code
    return CFG["hr_activity"].get("fallback_in_list", "d15")


def guess_salary_kind(raw):
    raw = raw or ""
    if "元/天" in raw or "元/日" in raw:
        return "daily"
    if "元/月" in raw or "K" in raw.upper() or "k" in raw:
        return "monthly"
    if "元/时" in raw or "小时" in raw:
        return "hourly"
    return ""


def normalize(card, city_fallback=""):
    area = card.get("area") or ""
    city = pick_city(area, card.get("card_text", ""))
    if not city:
        # ⚠ 不能用"这次搜的是哪个城市"来兜底：Boss 的城市只相当于一个排序偏好，
        #   搜北京照样会返回天津的岗（实测约 19%）。那样会把天津的岗标成北京的。
        #   卡片自己写了城市就信卡片，实在没写才用搜索城市。
        head = re.split(r"[·•\-–\s/]", area.strip())[0] if area.strip() else ""
        city = head or city_fallback
    labels = [deobfuscate(x) for x in (card.get("labels") or [])]
    card_text = deobfuscate(card.get("card_text") or "")
    welfare = [deobfuscate(w) for w in (card.get("welfare") or [])]
    blob = " ".join([card.get("title", ""), area, card_text, " ".join(labels), " ".join(welfare)])

    # 远程：接口直接给了福利标签，比在页面文字里找词准
    remote_words = ["远程", "线上办公", "居家办公", "不限地点", "在家办公"]
    remote_ok = any(w in t for t in welfare for w in remote_words) \
        or any(w in blob for w in remote_words)
    return {
        "job_id": (card.get("_id") or "").strip()[-32:],
        # 第一次扫到该岗位的日期（YYYY-MM-DD）。scan 写盘前统一补，详见 scan()。
        # 用途：5 天内未打分的岗位视为「过期」，当成没扫到过处理。
        "first_seen": "",
        "title": deobfuscate(card.get("title", "")),
        "company": deobfuscate(card.get("company", "")),
        "city": city,
        "remote_ok": remote_ok,
        "track": guess_track(card, *(labels + [card_text, card.get("title", "")])),
        "salary_raw": card.get("salary", ""),
        "salary_kind": guess_salary_kind(card.get("salary", "")),
        "hr_activity": guess_activity(card.get("boss_line", ""), card_text),
        "job_labels": labels,
        "publish_days": None,          # 列表接口没给，详情页补
        "jd_text": "",                 # 同上
        "url": card.get("href", ""),
        # 接口给了、页面上没有的几样
        "days_per_week": deobfuscate(card.get("days_per_week", "")),   # 一周几天
        "least_month": deobfuscate(card.get("least_month", "")),       # 最少干几个月
        # 之前聊过没（去重）。三态：True=明确聊过 / False=明确没聊过 / None=接口没给这个字段。
        # 不能静默当 False：接口漏给字段时，False 会把"已聊过"的岗当"没聊过"放行。
        # 只有 True 才会在 scan 阶段源头跳过；None 和 False 都放行进台账/按钮兜底。
        # 说明：LIST_JS 里已用 !!x.contact 转成布尔，正常只会是 True/False；
        #   None 表示这个 key 压根不存在（接口改版/该岗位类型不返回），显式区分开。
        "contacted": (True if card.get("contacted") in (True, 1, "1", "true")
                      else (False if card.get("contacted") in (False, 0, "0", "false")
                            else None)),
        "boss_name": deobfuscate(card.get("boss_name", "")),
        "boss_title": deobfuscate(card.get("boss_title", "")),
        "brand_industry": deobfuscate(card.get("brand_industry", "")),
        "brand_scale": deobfuscate(card.get("brand_scale", "")),
        "welfare": welfare[:12],
        "company_clean": deobfuscate(card.get("company", "")),
    }


# ─────────────────────────────────────────────────────────────
# scan：扫列表页
# ─────────────────────────────────────────────────────────────

# 无目的浏览：偶尔打开一个详情页只看不投，打破"搜索→抓→下一词"的线性路径。
# 2026-10-31 加，对应 fengkong-solutions.md 1.2「线性无波动操作」——
# 风控会算请求时序的熵值，真人会"点开看看再关掉"，爬虫不会。
# 2026-11-01 本人拍板 0.15 → 0.5：实测投 100 份简历只多花约 9 分钟（112→121 分钟），
# 用这点时间换"时序更乱、更像真人"，明显压掉线性路径这个风控特征，划算。
IDLE_BROWSE_PROB = 0.5

# 单次总量预算闸：一次 scan 里最多发多少个接口请求，数到上限就停。
# 防的是「一口气刷爆」——哪天手滑写成 --cities 全部 --pages 3（=315 次），
# 这条闸会把请求总数按死在 SCAN_BUDGET 以内，剩下的留到下次 --resume。
# 不是防「正常扫一轮」：2 城 × 7 词 × 1 页 = 14 次基础请求，加上无目的浏览
# 噪声和失败重试，真实大概 30~40 次，所以默认 60 留一倍余量。
# 2026-11-07 改：默认 60 → 85。目标改成「每天攒够 80 份可投」，主城（京沪深杭广）
# 5 城 × 17 词 = 85 次请求正好一次扫完主城；11-07 已把搜索改成「导航+读DOM」，
# 实测 17 词×2 城 34 页面零 code37，请求量不再是 code37 的主因。
# 2026-11-08 改：85 → 150。对照成熟项目（boss-zhipin-scraper 单次 10 页、zhaopin
# 平台「列表大页+详情小批」）：扫描（列表/翻页）是风险较低的一环，可以比「详情页」
# 放宽；150 次 ≈ 覆盖主城 5 城(85 次) + 再扫 3~4 城，配合 --resume 分多轮累积，
# 不用一轮硬扫全 15 城。详情页那 120 上限不动（那是真正的风控敏感环节）。
# 可用环境变量 BOSS_SCAN_BUDGET 临时覆盖，不用改代码。
SCAN_BUDGET = int(os.environ.get("BOSS_SCAN_BUDGET", "150"))

# 连续无新增就停翻页（2026-11-13 落地，对标 getJD/bosszp-crawler 的
# --dedupe-on-the-fly「连续两页无新增时停止当前关键词+城市的分页」）。
# Boss 列表按发布时间/推荐排序，翻得越深岗位越老。某页扫出的岗位若
# 「job_id 全在已抓清单里」= 这一页没带来任何新岗位，说明这个词+城市的
# 新岗位已经抓完了，再翻只会遇到更老的（已抓过/已下架），纯浪费请求。
# 连续 STOP_AFTER_NO_NEW_PAGES 页无新增就停这个词，跳到下一个词。
# 判定铁律（同 _load_seen_job_ids）：
#   · job_id 为空 → 不参与判定（没 id 没法确认老新，宁多翻不漏）。
#   · 只有「job_id 非空 且 全在已抓清单」才算「本页无新增」。
#     只要有一个 job_id 不在清单里，就算有新增，streak 归零继续翻。
STOP_AFTER_NO_NEW_PAGES = int(os.environ.get("BOSS_STOP_NO_NEW_PAGES", "2"))

# 静默限流感知（2026-11-10 重写判定逻辑）：
# 接口不报错、不弹验证码，只是悄悄返回 0 条。旧逻辑靠"连续 N 页空"判断，
# 把「真没岗位」「渲染慢」也误判成限流，导致扫到没货的词就白白冷却 119 分钟。
# 新逻辑改看「页面形态」：只有 Boss 主动把搜索页踢走（跳回聊天页/登录页/验证页）
# 才算真限流，停手记冷却；单纯没卡片（页面没跳）就当「没岗位」，跳过不冷却。
# 判断依据是 LIST_JS 返回的 url / body_head 两个字段（页面真实地址 + 正文开头）。


def human_delay(median_sec, spread=0.5):
    """模拟真人的操作等待：对数正态分布，不是均匀分布。

    真人点一下、停一下，不是"每个间隔一样随机"，而是"大部分时间集中在
    中位数附近，偶尔冒出很长的停顿（走神/喝水/回微信），偶尔快速连点"。
    学术上这叫对数正态（长尾）分布（arXiv:1607.02952 实测人类操作间隔）。

    参数：
      median_sec —— 中位数秒数（大部分等待都在这附近，会有一半比它短、一半比它长）
      spread     —— 波动系数，越大尾巴越长（默认 0.5，接近真人的波动幅度）

    内部换算：lognormvariate 的参数是对数域的均值/标准差，这里把中位数和
    波动系数换算过去，调用方只关心"大概等多久"，不用懂对数。
    返回值会 clamp 到 [median*0.2, median*5]，防止极端值太离谱。
    """
    import math
    mu = math.log(median_sec)          # 对数域均值 = ln(中位数)
    sigma = max(spread, 0.05)          # 对数域标准差，直接拿波动系数
    v = random.lognormvariate(mu, sigma)
    lo, hi = median_sec * 0.2, median_sec * 5.0
    return max(lo, min(hi, v))


def page_delay():
    """翻页间隔：22–38 秒的对数正态，且下限硬卡 22 秒。

    2026-11-07 改（v2 方案①）：一手实测「15 秒间隔连续第 3 次 search 就
    code37，30/60/90 秒均通过」，原中位 16 秒正踩在红线附近。中位提到 28、
    下限卡到 22，彻底离开红线区。代价是单页扫描时长约 +75%，换安全。
    """
    # 中位 28 秒；spread 0.30 尾巴适中，不会动不动 60 秒
    import math
    v = random.lognormvariate(math.log(28.0), 0.30)
    return max(22.0, min(38.0, v))


def _idle_browse(tab, verbose=True):
    """打开列表页第一个岗位的详情，停留几秒就返回，不做任何投递动作。

    目的：往请求序列里掺一点"真人无目的浏览"的噪声。失败就静默跳过，
    绝不因为这一步影响主流程（它本来就不是为了拿数据）。
    """
    try:
        # 选择器沿用项目里已验证能命中的（见上方 scan 抓取用的选择器）：
        # li/div[class*="job-card"] → 里面的 a[href*="job"] 或 [class*="job-name"]
        js = ("(() => { const c = document.querySelector('li[class*=\"job-card\"], "
              "div[class*=\"job-card\"]'); if (!c) return '{}'; "
              "const a = c.querySelector('a[href*=\"job\"], [class*=\"job-name\"], a'); "
              "if (!a) return '{}'; a.click(); return '{\"clicked\":1}'; })()")
        evaluate(tab, js, timeout=15)
        # 停留一个像人看简历的时长（对数正态，中位 11.5 秒），期间不干别的
        time.sleep(human_delay(11.5, spread=0.5))
        # 返回列表页（历史后退一步即可，不刷新）
        evaluate(tab, "history.back(); '{}'", timeout=15)
        if verbose:
            print("　↺ 穿插了一次无目的浏览（点开详情看了一眼就返回）")
    except Exception:
        # 这一步本来就是可有可无的噪声，失败无所谓，别让它打断主流程
        pass


def _absorb_contacted(job):
    """把「已聊过」的岗位沉淀进台账，作为后续判重的权威依据。

    为什么跳过之前要回写：contacted 是 scan 那一刻的接口快照，只在那一刻有效；
    如果只是 `continue` 跳过、什么都不留，等这轮扫描结束，这个"聊过"信号就没了，
    下次 HR 换皮重发（job_id 变）或手动投过时，台账三级匹配认不出来。

    做法：job_id 非空就写进 greeting_ledger.csv（sent=已发、source=contacted）；
    job_id 为空就退化成公司名写进 company_seen.txt（判重的第二道兜底）。
    与 chat_scan.py 的 import-contacted 口径一致：聊过 = 投过。
    """
    import csv as _csv
    try:
        # 复用 run.py 的 FIELDS 表头（表头唯一权威，见 run.FIELDS 注释）
        import run as _run
        fields = _run.FIELDS
    except Exception:
        return 0

    jid = (job.get("job_id") or "").strip()
    company = (job.get("company") or "").strip()

    added = 0
    ledger = path_of("data/greeting_ledger.csv")

    # 已有 job_id 去重（不重复追加）
    have = set()
    try:
        with open(ledger, encoding="utf-8") as f:
            have = {(row.get("job_id") or "").strip()
                    for row in _csv.DictReader(f) if (row.get("job_id") or "").strip()}
    except Exception:
        have = set()

    if jid and jid not in have:
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        row = {k: "" for k in fields}
        row.update({
            "send_time": now,
            "job_id": jid,
            "company": company,
            "city": job.get("city", ""),
            "title": job.get("title", ""),
            "family": job.get("family", ""),
            "notes": "扫描时接口 contacted=true 跳过｜来源：列表接口快照",
            "sent": "已发",
            "source": "contacted",
        })
        with open(ledger, "a", encoding="utf-8", newline="") as f:
            w = _csv.DictWriter(f, fieldnames=fields)
            if os.path.getsize(ledger) == 0:
                w.writeheader()
            w.writerow(row)
        added += 1

    # 无 job_id 的：退化成公司名兜底
    if not jid and company:
        seen_file = path_of("data/company_seen.txt")
        seen = set()
        try:
            with open(seen_file, encoding="utf-8") as f:
                for line in f:
                    s = line.strip()
                    if s and not s.startswith("#"):
                        seen.add(s)
        except Exception:
            seen = set()
        if company not in seen:
            with open(seen_file, "a", encoding="utf-8") as f:
                f.write(company + "\n")
    return added


def scan(tab, cities, queries, pages=1, wait=4.0, verbose=True, resume=False):
    """扫列表页。

    resume=True 时接着上次跑：读回已有的 jobs_list.json，
    把已经扫过的「城市+搜索词」跳过去。被风控打断后靠它续上，不用从头再来。
    """
    # ⛔ 冷却闸门：如果上次触发过风控、现在还在冷却期，直接拒绝重跑。
    #    在风控里打转只会让账号被盯得更久，不如等它自然过去。
    left = cooldown_left()
    if left > 0:
        print(f"  ⛔ 还在风控冷却期（还差 {int(left // 60)} 分 {int(left % 60)} 秒）。")
        print(f"     先别刷了，等冷却过去再跑。想看剩余时间可跑 fetch_cdp.py cooldown")
        return []

    out = path_of("data/jobs_list.json")
    got, miss = [], []
    skipped_contacted = []   # 已聊过（contacted=True）源头跳过的岗位，仅统计用
    skipped_blocklist = []   # 永久不投池命中的岗位，仅统计用
    n = 0
    halted = False
    done = set()
    # 已抓过详情的 job_id 清单：连续无新增停翻页时拿来判「这页有没有新岗位」。
    # 只加载一次，别每页重读文件。空清单 = 不去重（第一次跑还没攒任何详情）。
    seen_ids = _load_seen_job_ids()
    # 连续失败退避：失败一次等 10 秒，第二次 20，第三次 40…… 别快速重试
    _backoff = 0
    # 总量预算计数器：这次 scan 一共发了多少个接口请求（见 SCAN_BUDGET）
    sent = 0
    if resume and os.path.exists(out):
        try:
            old = json.load(open(out, encoding="utf-8"))
            got = list(old)
            for j in old:
                if j.get("source_city") and j.get("source_query"):
                    done.add((j["source_city"], j["source_query"]))
            if verbose:
                print(f"（接着上次跑：已有 {len(old)} 条，{len(done)} 组扫过了，会跳过）")
        except Exception:
            pass
    for city in cities:
        if halted:
            break
        code = CITY_CODES.get(city)
        if not code:
            miss.append(f"{city}（没有城市代码）")
            continue
        for q in queries:
            if halted:
                break
            if (city, q) in done:
                if verbose:
                    print(f"　· {city} / {q} 上次扫过了，跳过")
                continue
            no_new_streak = 0   # 连续无新增页计数（见 STOP_AFTER_NO_NEW_PAGES 注释）
            for page in range(1, pages + 1):
                if halted:
                    break
                # ⛔ 总量预算闸：发请求前先数数，到上限就停。
                #    剩下的城市/词/页都不扫了，留到下次 --resume 接着跑。
                if sent >= SCAN_BUDGET:
                    halted = True
                    if verbose:
                        print()
                        print(f"  ⛔ 本次已达请求上限 {SCAN_BUDGET} 次，停止。")
                        print(f"     剩下的下次 --resume 接着跑（可设 BOSS_SCAN_BUDGET 临时调上限）。")
                    break
                # 2026-11-07 改：搜索不再「注入 fetch 调接口」，改为「导航到搜索结果页
                # URL + 读页面 SPA 自己渲染出的 DOM 卡片」。根因（见
                # research/boss-bot-detection/搜索改造方案-注入改原生加载.md）：
                # BOSS 能识别注入的 XHR（永远 code37），而页面自己发的请求能成功。
                # 导航到搜索页 = 请求由页面原生发起，我们只读渲染结果，规避根因。
                url = SEARCH_URL.format(query=urllib.parse.quote(q),
                                        city=code, page=page)
                sent += 1
                try:
                    goto(tab, url, wait=4.0)
                    raw = evaluate(tab, LIST_JS, timeout=30) or "{}"
                    data = json.loads(raw) if isinstance(raw, str) else (raw or {})
                except Exception as e:
                    if verbose:
                        print(f"　× {city} / {q} 抓取失败：{e}")
                    # 失败指数退避 + 随机抖动：连着失败越等越久，
                    # 抖动(0.5~1.0)防"多个请求同时失败又同时重试"的同步风暴。
                    _backoff = 10 if _backoff == 0 else min(_backoff * 2, 120)
                    time.sleep(_backoff * random.uniform(0.5, 1.0))
                    continue
                _backoff = 0   # 成功一次就重置退避
                if "error" in data:
                    err = data["error"]
                    real_q = data.get("query") or q
                    msg = data.get("message") or ""
                    zpmsg = data.get("zpDataMsg") or ""
                    miss.append(f"{city} / {real_q}（{err}）")
                    # ⛔ 熔断：BOSS 回"环境异常"(code=37) 是要弹验证码的前兆。
                    if "37" in err or "异常" in err or "验证" in err or "captcha" in err.lower():
                        halted = True
                        detail = "；".join(x for x in [err, msg, zpmsg] if x)
                        log_risk(f"{city} / {real_q}　{detail}")
                        set_cooldown()
                        if verbose:
                            _mins = int(cooldown_left() // 60)
                            print()
                            print("  ⛔ 触发风控（环境异常），立刻停手，不再继续请求。")
                            print("     做法：到那个浏览器里手动打开 BOSS 随便点两下，")
                            print("     如果弹验证码就过一下。等页面恢复正常再重跑。")
                            print(f"     已记冷却 {_mins} 分钟（连续触发次数越高，冷却越久），")
                            print("     这期间重跑会被直接拦下。")
                        break
                    _backoff = 5 if _backoff == 0 else min(_backoff * 2, 60)
                    time.sleep(_backoff * random.uniform(0.5, 1.0))
                    continue
                jobs = data.get("cards", [])
                n += 1
                if not jobs:
                    # 页面没读到卡片，先看"页面形态"再分类，别一锅烩（2026-11-10 改）：
                    #   A. 真没岗位  —— 搜索页好好开着，就是没结果（无害，跳过不冷却）
                    #   B. 被跳走了  —— 导航到搜索页后，Boss 主动把页面踢回聊天页/登录页
                    #                    （这才是真限流前兆，必须停手记冷却）
                    #   C. 渲染慢    —— 4 秒不够，卡片还没渲染出来（多等再读一次）
                    # 旧逻辑只数"连续几页空"就判限流，把 A/C 也当成 B 误杀，
                    # 才出现"扫到没货的词"白白冷却 119 分钟。改成先看页面跳没跳。
                    cur_url = (data.get("url") or "").lower()
                    body = (data.get("body_head") or "").lower()
                    # 跳走信号：地址不再是搜索页，而是聊天页/登录页/账号页/验证页
                    jumped = any(k in cur_url for k in
                                 ("/geek/chat", "login", "passport", "account", "captcha", "verify"))
                    # 被踢回聊天页的强特征：正文里有聊天页特有的字样
                    jumped = jumped or any(k in body for k in ("在线沟通", "立即沟通", "沟通过的", "谁看过我"))
                    if jumped:
                        # B：Boss 主动把页面踢走了 = 真限流，停手记冷却
                        halted = True
                        log_risk(f"{city} / {q}　搜索页被跳走（当前 url={cur_url[:60]}），疑似真限流")
                        set_cooldown()
                        if verbose:
                            print()
                            print("  ⛔ 搜索页被跳走了（Boss 主动动作，疑似真限流）。")
                            print(f"     当前页面已变成：{cur_url[:60]}")
                            print(f"     已记冷却 {int(cooldown_left() // 60)} 分钟。")
                        break
                    # 没跳走 = 还在搜索页，可能是"真没岗"或"渲染慢"。
                    # 先多等一会儿再读一次，排除渲染慢（C）。
                    time.sleep(3.0)
                    raw2 = evaluate(tab, LIST_JS, timeout=30) or "{}"
                    data2 = json.loads(raw2) if isinstance(raw2, str) else (raw2 or {})
                    jobs2 = data2.get("cards", [])
                    if jobs2:
                        # 第二次读到了 = 刚才是渲染慢，不是没岗，正常处理这批卡片
                        jobs = jobs2
                    else:
                        # 两次都空、且页面没跳走 = 真没岗位（A）。跳过不冷却。
                        miss.append(f"{city} / {q} 第{page}页（无岗位，跳过）")
                        if verbose:
                            print(f"　· {city} / {q} 第{page}页：无岗位（没被限流，继续）")
                        continue
                # 走到这里说明 jobs 非空（直接读到，或二次读补上了），正常处理
                # 城市代码校准：卡片每条都带地区，拿来核对码表
                wrong = [j["city"] for j in jobs if j.get("city") and j["city"] != city]
                if wrong and verbose:
                    uniq_wrong = sorted(set(wrong))
                    print(f"　! {city} 的码可能不准：这页混进了 {uniq_wrong[:5]}")
                # 本页扫出的 job_id（取 _id 后 32 位，与 normalize 同口径），
                # 供「连续无新增停翻页」判定用，省得再 normalize 一遍。
                page_job_ids = []
                for c in jobs:
                    job = normalize(c, city_fallback=city)
                    job["source_query"] = q
                    job["source_city"] = city
                    # ── 建议1 落地：源头跳过「已聊过」的岗位 ──
                    # 只有 contacted 明确是 True 才跳过（None=字段缺失、False=没聊过，
                    # 都放行进后面的台账三级匹配 / 按钮探测兜底，保证不漏）。
                    if job.get("contacted") is True:
                        _absorb_contacted(job)   # 跳过前先沉淀进台账，别让信号消失
                        skipped_contacted.append(job.get("company") or job.get("title") or job.get("job_id"))
                        continue
                    # ── 永久不投池：列表阶段直接 pass ──
                    # ① 先查池子：上次已经判断过「不该投」的，这次直接跳过，
                    #    连详情页都不开，省最贵的请求。
                    hit, why = blocklist.is_blocked(job)
                    if hit:
                        skipped_blocklist.append(job.get("company") or job.get("title") or job.get("job_id"))
                        continue
                    # ② 再看「列表阶段就能判的硬条件」：命中就既跳过、又写进池子，
                    #    这样下次扫到直接 pass，不用重复判断。
                    hard = _hard_block_reason(job)
                    if hard:
                        blocklist.add(job, hard)
                        skipped_blocklist.append(job.get("company") or job.get("title") or job.get("job_id"))
                        continue
                    got.append(job)
                    page_job_ids.append(job.get("job_id") or "")
                if verbose:
                    print(f"　· {city} / {q} 第{page}页：{len(jobs)} 条")
                # 实时心跳：告知前端"正在扫描 {city}/{q} 第{page}页"
                try:
                    import live_status
                    live_status.touch(step="fetch", running=True,
                                      done=page, total=pages,
                                      company=city, title=q,
                                      msg=f"扫描 {city} {q} 第{page}/{pages}页，累计 {len(got)} 条")
                except Exception:
                    pass
                # ── 连续无新增就停翻页（对标 getJD）──
                # 数这一页里有多少「新岗位」（job_id 非空且不在已抓清单）。
                # 一个都没有 → no_new_streak +1；连续 STOP_AFTER_NO_NEW_PAGES 页
                # 都没有新岗位，就停这个词的翻页，跳到下一个词。
                if seen_ids:
                    # 有非空 job_id 且都不在已抓清单 → 本页无新岗位
                    nonempty = [jid for jid in page_job_ids if jid]
                    new_in_page = sum(1 for jid in nonempty if jid not in seen_ids)
                    if nonempty and new_in_page == 0:
                        no_new_streak += 1
                        if verbose:
                            print(f"　　↳ 本页无新岗位（连续 {no_new_streak} 页）")
                        if no_new_streak >= STOP_AFTER_NO_NEW_PAGES:
                            if verbose:
                                print(f"　　⏹ 连续 {STOP_AFTER_NO_NEW_PAGES} 页无新岗位，"
                                      f"停翻 {city}/{q}，跳下一个词")
                            break
                    else:
                        no_new_streak = 0
                # 翻页间隔 22~38 秒（见 page_delay 注释），别刷太快。
                time.sleep(page_delay())
                # 偶尔穿插一次"无目的浏览"，打破线性路径（详见 _idle_browse 注释）
                if random.random() < IDLE_BROWSE_PROB:
                    _idle_browse(tab, verbose=verbose)

    # 同一个岗位去重（职位名 + 公司）
    uniq, seen = [], set()
    today = time.strftime("%Y-%m-%d")
    for j in got:
        key = (j["title"], j["company"], j["city"])
        if key in seen:
            continue
        seen.add(key)
        # 第一次扫到就烙上日期；resume 读回旧数据时已有 first_seen 就保留，
        # 这样「这个岗到底躺了几天」能从头算起，供 5 天过期清理用。
        if not j.get("first_seen"):
            j["first_seen"] = today
        uniq.append(j)

    out = path_of("data/jobs_list.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(uniq, f, ensure_ascii=False, indent=1)

    if verbose:
        print()
        print(f"扫完 {n} 个页面，拿到 {len(got)} 条，去掉重复剩 {len(uniq)} 条 → data/jobs_list.json")
        if skipped_contacted:
            print(f"源头跳过已聊过的岗位 {len(skipped_contacted)} 条（已回写台账，不重复投）")
        if skipped_blocklist:
            print(f"永久不投池跳过 {len(skipped_blocklist)} 条（含硬条件新入库，下次直接 pass）")
        if miss:
            print(f"没抓到的（{len(miss)} 组）：")
            for m in miss[:6]:
                print(f"　· {m}")
            if halted:
                print("　（中途遇到风控停下的 —— 手动过完验证码后重跑，会接着从断点继续）")
            else:
                print("　多半是页面结构变了，把 data/raw/debug_*.txt 发我，我改选择器。")
    return uniq


# ─────────────────────────────────────────────────────────────
# detail：要不要值得开详情页
# ─────────────────────────────────────────────────────────────

def _family_keywords():
    kws = []
    for fam in CFG["role_clusters"]:
        if fam.get("detect_in_jd_only"):
            continue
        kws.extend(fam.get("include", []))
    return kws


def worth_detail(job):
    """
    列表页信息太少，但开详情页是最贵的一步（一次一个请求）。
    所以先把明显没戏的挡住：城市不对、排除词、名字跟 5 个岗位族一点不沾。
    """
    cities = CFG["hard_filter"]["city"]["cities"]
    if job["city"] and job["city"] not in cities and not job["remote_ok"]:
        return False, "城市不在清单"
    blob = " ".join([job["title"], " ".join(job["job_labels"])])
    for w in CFG.get("exclude_keywords", []):
        if w in blob:
            return False, f"排除词：{w}"
    t = job["title"]
    if any(k.lower() in t.lower() for k in _family_keywords()):
        return True, "岗位名命中目标族"
    if any(k in t for k in ["AI", "大模型", "产品", "运营", "项目"]):
        return True, "名称沾边，值得看 JD"
    return False, "名称完全不沾边"


def _hard_block_reason(job):
    """列表阶段就能判的「永久不投」硬条件，命中返回原因，没命中返回 ""。

    这些条件只看列表接口给的信息（标题/公司/城市/标签/薪资），不用开详情页，
    所以能在扫描时就定生死，命中即写进永久不投池，下次直接 pass。

    注意：这里刻意只收「明确、稳定、不会变」的硬条件 ——
      · 城市不在清单（城市基本不会变）
      · 命中排除词（"地推""课程顾问"这类，岗位本质就排除）
      · 标题带"总监/算法"（title_exclude）
    不收「资深经验」「粗筛没命中岗位族」—— 这两类 2026-11-15 本人拍板「保留查看
    空间」，因为：
      · 资深经验岗可能换个 JD 写法（比如改成"经验不限、可招实习"）就翻盘；
      · 粗筛没命中可能是搜索词/分类规则不够全，岗位本身可能是好岗。
    所以它们不进永久池，照常抓详情、照常打分，只是会被打分环节正常拦下（留痕）。
    也不收「分数低」—— 分数要抓过详情、打过才知道，且 JD 改描述可能翻盘，
    那类由 run.py 打分环节回写（见 blocklist 的 job_id 精确匹配那条路）。
    """
    blob_title = (job.get("title") or "") + " " + " ".join(job.get("job_labels") or [])
    # 排除词（"地推""课程顾问"等）
    for w in CFG.get("exclude_keywords", []):
        if w in blob_title:
            return f"排除词：{w}"
    # 标题带总监/算法
    for w in CFG.get("title_exclude", []):
        if w in (job.get("title") or ""):
            return f"标题排除：{w}"
    # 城市不在清单（非远程）
    cities = CFG["hard_filter"]["city"]["cities"]
    if job.get("city") and job["city"] not in cities and not job.get("remote_ok"):
        return f"城市不在清单：{job['city']}"
    return ""


def _load_seen_job_ids():
    """聚合「已经抓过详情 / 已经发过 / 已经聊过」的 job_id 清单。

    抓详情是最贵的一步（一次一个请求）。同一个岗位会从不同关键词/城市/时间
    反复冒出来，如果每次都不看清单就直接点进去，等于同一个岗位反复抓好几遍。

    判重铁律：**只认 job_id 精确相等，其余一律放行**。
      · job_id 一模一样 → 说明这个岗位确实抓过/发过，跳过是安全的，不会漏投。
      · job_id 不一样（哪怕公司名一样、标题很像）→ 一律放行去抓。
        因为 job_id 会变（HR 改标题/重发岗位），靠公司名/标题相似度拦会误杀
        同一家公司的新岗位，那才是真漏投。这里绝不做公司名/标题去重。

    三个来源（都有 job_id 才算）：
      1. jobs_today.json —— 详情池里已抓过的（含已发、已跳过、已过滤的）。
      2. greeting_ledger.csv —— 台账里记过 job_id 的（已发 + contacted 跳过的）。
      3. jobs_list.json 本轮扫到的，会在 enrich_details 内实时并入（见下方）。
    """
    seen = set()

    # 1) jobs_today.json：已抓过详情的
    p = path_of("data/jobs_today.json")
    if os.path.exists(p):
        try:
            old = json.load(open(p, encoding="utf-8"))
            if isinstance(old, list):
                for x in old:
                    jid = (x.get("job_id") or "").strip()
                    if jid:
                        seen.add(jid)
        except Exception:
            pass

    # 2) greeting_ledger.csv：已发过 / 已聊过（contacted）的
    lp = path_of("data/greeting_ledger.csv")
    if os.path.exists(lp):
        try:
            import csv as _csv
            with open(lp, encoding="utf-8") as f:
                for row in _csv.DictReader(f):
                    jid = (row.get("job_id") or "").strip()
                    if jid:
                        seen.add(jid)
        except Exception:
            pass

    return seen


def enrich_details(tab, jobs, cap=None, wait=4.0, verbose=True, resume=False):
    cap = cap or CFG["hr_activity"]["detail_lookup_daily_cap"]
    # 今天日期（YYYY-MM-DD），写进每条岗位的 first_seen。
    # 补漏洞（2026-11-15）：first_seen 之前在 scan 写、在 detail 从不写，
    #   导致 jobs_today.json 全都没这个字段 → 补打分把它当「历史遗留」跳过，
    #   「后台可投总数」永远算不准。这里在详情入库时补上，字段闭环。
    today = time.strftime("%Y-%m-%d")
    todo, skipped = [], 0
    for j in jobs:
        ok, _why = worth_detail(j)
        if ok:
            todo.append(j)
        else:
            skipped += 1

    # ── 抓详情前 job_id 精确去重（2026-11-13 落地）──
    # 之前只在 resume=True 时才读 jobs_today.json 去重，默认跑会重复抓。
    # 现在无条件建一张「已抓过 job_id」清单，把已抓的踢掉，只抓真没抓过的。
    # 判重只认 job_id 精确相等（见 _load_seen_job_ids 注释），绝不漏投。
    import filters
    kept = []
    out = path_of("data/jobs_today.json")
    done_ids = _load_seen_job_ids()
    bad_ids = set()
    # 旧的错误页条目（当初存的是断网报错文字，不是真 JD）要重抓：
    # 它虽然 job_id 在清单里，但不能算「已抓过」，否则永远漏投。
    # 把它们从 seen 里剔除，并丢掉旧错误页条目，等重抓成功再写回。
    if os.path.exists(out):
        try:
            old = json.load(open(out, encoding="utf-8"))
            if isinstance(old, list):
                for x in old:
                    jid = x.get("job_id")
                    if not jid:
                        kept.append(x)
                        continue
                    bad, _why = filters.jd_invalid(x)
                    if bad:
                        bad_ids.add(jid)
                        done_ids.discard(jid)   # 错误页不算已抓，放它重抓
                    else:
                        kept.append(x)
        except Exception:
            pass

    # 本轮清单里 job_id 为空的，不参与去重（没 id 没法精确判，放行去抓，
    # 后续靠 worth_detail 和 layer0 兜底）。有 id 且在 seen 里的 → 跳过。
    todo = [j for j in todo if (j.get("job_id") or "").strip() not in done_ids]

    todo = todo[:cap]

    if verbose:
        dup_skipped = len(done_ids) - len(bad_ids)
        print(f"列表里 {len(jobs)} 条，先看名字符不合：{len(todo)} 条值得开详情页")
        if done_ids:
            print(f"（job_id 去重：已抓过 {dup_skipped} 条直接跳过，"
                  f"含错误页重抓 {len(bad_ids)} 条；上限 {cap}）")
        else:
            print(f"（上限 {cap}；这一步一次只能看一条岗位，是最费请求的一步）")

    def _save():
        """边抓边存。原来只在最后落盘 —— 中途遇到风控停手会把已抓的全丢了。"""
        with open(out, "w", encoding="utf-8") as f:
            json.dump(kept, f, ensure_ascii=False, indent=1)

    for i, j in enumerate(todo, 1):
        url = j.get("url") or ""
        if url and url.startswith("/"):
            url = "https://www.zhipin.com" + url
        if not url:
            continue
        try:
            goto(tab, url, wait)
        except Exception as e:
            if verbose:
                print(f"　× [{i}] {j['title']} 失败：{e}")
            time.sleep(2)
            continue
        # 2026-11-11 根因修复：goto 详情页后，先看「页面有没有被 302 弹回首页」。
        # 已下架/失效的岗位，BOSS 会把 job_detail 页 302 跳回首页（实测：还在招的岗
        # 能进、下架的岗 302 回 zhipin.com/）。之前没查这一步，直接把「首页文字」当
        # JD 存进了 jobs_today.json（就是那 122 条错误页），分数被压低、静默漏投。
        # 判断标准：读 current_url，若不是 job_detail 开头，就是被弹走了 → 跳过不存。
        cur = _plugin().current_url() or ""
        if "job_detail" not in cur:
            if verbose:
                print(f"　× [{i}/{len(todo)}] {j['title'][:20]} 岗位已下架（详情页 302 回首页），跳过")
            time.sleep(human_delay(3.0, spread=0.5))
            continue
        d = evaluate(tab, DETAIL_JS) or {}

        jd = deobfuscate(d.get("jd", ""))
        full = deobfuscate(d.get("full", ""))
        tags = [deobfuscate(t) for t in (d.get("tags") or [])]
        j["jd_text"] = jd if len(jd) > 40 else full   # 正文选择器没命中就用整页文字兜底
        j["job_labels"] = list(dict.fromkeys(j["job_labels"] + tags))[:12]
        j["hr_activity"] = guess_activity(full[:1500], j.get("hr_activity", ""))
        # 薪资：详情页是明文，补上列表页 DOM 里被字形混淆掉的数字。
        # 只有列表页没拿到「含数字的薪资」时才用详情页的覆盖（有数字才可信）。
        sal = deobfuscate(d.get("salary", ""))
        if sal and re.search(r"[0-9]", sal):
            j["salary_raw"] = sal
            j["salary_kind"] = guess_salary_kind(sal)
        if any(w in full for w in ["远程", "线上办公", "居家办公", "不限地点"]):
            j["remote_ok"] = True
        j["track"] = guess_track({}, *(tags + [full[:800]])) if j["track"] == "未知" else j["track"]
        j["publish_raw"] = deobfuscate(d.get("pub", ""))
        j["publish_days"] = parse_publish_days(j["publish_raw"])
        j.pop("source_query", None)
        # 补 first_seen（2026-11-15）：详情入库时统一烙「首次见到」日期。
        #   · 已有 first_seen（scan 阶段已写）→ 保留，不覆盖，让「躺了几天」从头算。
        #   · 没有 → 用今天补上。这样 jobs_today.json 里每条都有 first_seen，
        #     补打分（reconcile）才能用它算过期，而不是误判「历史遗留」。
        if not j.get("first_seen"):
            j["first_seen"] = today

        ok, why = filters.layer0(j)
        if ok:
            kept.append(j)
        if verbose:
            flag = "留" if ok else "砍"
            print(f"　[{i}/{len(todo)}] {flag}　{j['city']}　{j['title'][:22]}　{j['company'][:16]}"
                  + ("" if ok else f"　← {'; '.join(why)}"))
        # 实时心跳：告知前端"正在抓详情第 i/total 条"
        try:
            import live_status
            live_status.touch(step="fetch", running=True,
                              done=i, total=len(todo),
                              company=j.get("company", ""),
                              title=j.get("title", ""),
                              msg=f"抓详情 {i}/{len(todo)} {j.get('title','')}")
        except Exception:
            pass
        # 详情页是风控最敏感的一环，保持中位节奏。
        # 2026-11-13 由 10 秒放缓到 16 秒：连抓多轮（一天 >120 条）时降低高频被抓概率。
        time.sleep(human_delay(16.0, spread=0.5))
        if i % 10 == 0:
            _save()
            if verbose:
                print(f"　　　（已存 {len(kept)} 条 → data/jobs_today.json）")

    _save()

    if verbose:
        print()
        print(f"详情页看完 {len(todo)} 条，硬过滤后剩 {len(kept)} 条 → data/jobs_today.json")
        print("下一步：python run.py --input ../data/jobs_today.json")
    return kept


# ─────────────────────────────────────────────────────────────
# 入口
# ─────────────────────────────────────────────────────────────

def require_tab():
    # 插件模式：确认能连上浏览器即可，返回哨兵（evaluate/goto 会忽略它）
    if not port_open():
        print()
        print("连不上浏览器（chrome-use 插件）。请确认：")
        print("  1. 你日常登录 BOSS 的 Chrome 正开着")
        print("  2. chrome-use 扩展已装好、已连接")
        return None
    return _PLUGIN_TAB


def cmd_check(_a):
    """自检。退出码给 bat 用：0 = 通了，1 = 没开。"""
    print("驱动：chrome-use 插件（不开调试端口）")
    url = _plugin().current_url() or ""
    print(f"插件连接：{'已连上 ✓' if url else '没连上 X'}")
    if not url:
        print()
        print("请确认你日常登录 BOSS 的 Chrome 开着、chrome-use 扩展已连接。")
        sys.exit(1)
    print(f"当前标签页：{url[:70]}")
    if "zhipin" not in url:
        print()
        print("[注意] 当前标签页不是 BOSS。请在 Chrome 里打开 zhipin.com 并确认登录。")
    sys.exit(0)


def cmd_history(tab):
    """读你自己后台的『已经跟谁聊过 / 已经投过哪些』，存成去重表的原料。

    为什么要这一步：台账现在是空的，90 天去重第一天根本不起作用。
    如果你以前手动投过，机器再投一遍，同一个 HR 看到同一个人来两次 ——
    这比没投过还糟。
    """
    # 候选页面。BOSS 改版过好几次，URL 不一定对，全试一遍，抓到的原文都存下来
    targets = [
        ("沟通列表（聊过的人）", "https://www.zhipin.com/web/chat/index"),
        ("我的投递", "https://www.zhipin.com/web/geek/deliver"),
        ("投递记录", "https://www.zhipin.com/web/geek/joblist"),
        ("求职管理", "https://www.zhipin.com/web/geek/index"),
    ]

    print("去你自己后台抓历史记录。全程只读，不点任何东西。")
    print()

    got = []
    for i, (name, url) in enumerate(targets, 1):
        print(f"[{i}/{len(targets)}] {name}　{url}")
        goto(tab, url, wait=5.0)
        time.sleep(1.5)

        # 先看看这个页面到底是不是我们要的（会不会被弹回登录页）
        info = evaluate(tab, """
            (() => {
              const t = (document.body.innerText || '').trim();
              const risk = ['请登录','扫码登录','安全验证','滑动验证','操作过于频繁']
                           .filter(w => t.includes(w));
              return JSON.stringify({
                url: location.href,
                len: t.length,
                risk: risk,
                head: t.slice(0, 400)
              });
            })()
        """)
        try:
            d = json.loads(info or "{}")
        except Exception:
            d = {}

        if d.get("risk"):
            print(f"　⛔ 页面出现：{'、'.join(d['risk'])} —— 停。请你手动处理完再跑。")
            break

        head = (d.get("head") or "").replace("\n", " ")
        print(f"　当前 URL：{d.get('url','')[:70]}")
        print(f"　正文 {d.get('len',0)} 字　开头：{head[:150]}")

        if d.get("len", 0) < 80:
            print("　（这页几乎是空的，可能 URL 不对，跳过）")
            print()
            continue

        # 把整页原文存下来，供我们照着提炼
        raw = evaluate(tab, "document.body.innerText || ''")
        p = path_of(f"data/raw/history_{i}.txt")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write((raw or ""))
        print(f"　✓ 原文已存 → {p}")
        got.append((name, url, p, d.get("len", 0)))
        print()

        time.sleep(random.uniform(2.0, 3.5))

    print("=" * 60)
    if not got:
        print("一个都没抓到。两种可能：")
        print("　1. 登录掉了 —— 在那个浏览器里确认一下首页还是不是登录状态")
        print("　2. 这些 URL 都不对 —— 你自己打开「我的投递」页面，")
        print("　　把地址栏的网址复制发我，我换成真实地址")
        return

    print(f"抓到 {len(got)} 个页面：")
    for name, url, p, n in got:
        print(f"　· {name}（{n} 字）→ {p}")
    print()
    print("下一步：把 data/raw/history_*.txt 发我，我照着真实内容提炼成去重表。")
    print("在那之前，第一天 10 条的量很小，可以先跑 —— 但**你自己得看一眼卡片**，")
    print("看到眼熟的公司名就手动去掉。")


def cmd_bootstrap(_a):
    """已废弃：驱动层已统一走 chrome-use 插件，不再需要开调试端口。

    保留这个函数只是为了让 `fetch_cdp.py bootstrap` 命令不报「未知命令」。
    真要启动浏览器，打开你日常登录 BOSS 的 Chrome、确保 chrome-use 扩展已连接即可。
    """
    print("驱动层已切到 chrome-use 插件，无需再开调试端口。")
    print("请确认：你日常登录 BOSS 的 Chrome 开着、chrome-use 扩展已连接。")
    print("然后直接跑 python fetch_cdp.py check 验证连接。")


# ─────────────────────────────────────────────────────────────
# probe：安全探测风控是否已结束
# ─────────────────────────────────────────────────────────────
#
# 思想：用「最轻的动作」试探，而不是重新扫描（重动作）。
#   分级探测，从轻到重，任何一级看到风控信号就立刻停，绝不硬闯下一级。
#   就算风控还没结束，这些轻动作也不会加重它 —— 就像轻轻敲门听有没有人，
#   而不是直接砸门。

# 风控信号词。只认风控页自己的措辞，别的词一律不算（避免误报）。
RISK_WORDS = ["请登录", "扫码登录", "安全验证", "滑动验证",
              "操作过于频繁", "系统检测到异常", "环境异常", "验证码"]

# 聊天页：比岗位列表页轻得多，几乎不耗什么算力，是探测的第一落脚点。
CHAT_URL = "https://www.zhipin.com/web/chat/index"
# 会话列表接口：比 joblist 轻（只拉"跟谁聊过"，不触发搜索风控）。
FRIEND_LIST_API = "https://www.zhipin.com/wapi/zprelation/friend/getGeekFriendList.json?page=1&pageSize=5"


def _retry_after_seconds(headers):
    """从响应头里读 Retry-After / X-RateLimit-Reset，转成"还要等多少秒"。

    让目标告诉你它要你等多久，而不是自己拍脑袋固定 30 分钟。
    读不到返回 None。
    """
    if not headers:
        return None
    ra = headers.get("retry-after") or headers.get("Retry-After")
    if ra:
        try:
            # Retry-After 可能是秒数（"120"），也可能是日期（"Wed, 21 Oct ..."）
            if ra.strip().isdigit():
                return int(ra.strip())
            import email.utils as _eu
            t = _eu.parsedate_to_datetime(ra)
            return max(0, int((t.timestamp() - time.time())))
        except Exception:
            pass
    rr = headers.get("x-ratelimit-reset") or headers.get("X-RateLimit-Reset")
    if rr:
        try:
            return max(0, int(float(rr)) - int(time.time()))
        except Exception:
            pass
    return None


def _page_snapshot(tab, url, wait=6.0):
    """打开一个页面，读它的标题、正文长度、有没有风控信号词、有没有登录痕迹。

    返回 dict，绝不在这个函数里发任何写动作 —— 纯读。
    """
    goto(tab, url, wait=wait)
    info = evaluate(tab, """
        (() => {
          const t = (document.body ? document.body.innerText : '') || '';
          const risk = %s.filter(w => t.includes(w));
          // 登录痕迹：页面上有没有"已登录"的迹象（头像、用户名、消息、打招呼等）
          const loginHint = /头像|用户名|消息|沟通|打招呼|我的|退出|在线/.test(t.slice(0, 2000));
          return JSON.stringify({
            url: location.href,
            len: t.length,
            risk: risk,
            login_hint: loginHint,
            head: t.slice(0, 200)
          });
        })()
    """ % json.dumps(RISK_WORDS))
    try:
        return json.loads(info or "{}")
    except Exception:
        return {}


def cmd_probe(tab, deep=False):
    """探测风控状态。默认只做 L0/L1（纯读页面），--deep 才做 L2/L3（发接口请求）。"""
    print()
    print("  ===== 风控安全探测 =====")
    print()

    # ⛔ 前置闸门：如果还在冷却期，连探测都不做，直接告诉你等多久。
    left = cooldown_left()
    if left > 0:
        print(f"  ⛔ 还在风控冷却期（还差 {int(left // 60)} 分 {int(left % 60)} 秒）。")
        print("     冷却期内别探测 —— 现在去敲门只会让风控盯得更久。")
        print(f"     建议：等 {int(left // 60)} 分钟后再跑 probe。")
        return

    # ── L0：能不能打开页面 ──
    print("  [L0] 打开聊天页（最轻动作，只是翻个页）...")
    d = _page_snapshot(tab, CHAT_URL)
    if d.get("risk"):
        print(f"  ⛔ 页面出现风控信号：{'、'.join(d['risk'])}")
        print("     → 结论：风控还没结束。立刻停，不做任何后续请求。")
        _mark_probe_risk("L0", d)
        return
    if d.get("len", 0) < 80:
        print("  ⚠ 页面几乎是空的（可能没登录或页面结构变了），无法判断。")
        print("     → 结论：无法确认。建议先手动在浏览器里看一眼 BOSS 页面状态。")
        return
    print(f"  ✓ L0 通过：页面正常（{d.get('len', 0)} 字，URL 正常）")

    # ── L1：登录态还在不在 ──
    print("  [L1] 检查登录态（看有没有登录痕迹）...")
    if not d.get("login_hint"):
        print("  ⚠ 没看到明显的登录痕迹，可能是登录掉了。")
        print("     → 建议：在浏览器里确认右上角还有没有你的头像，没登录就先扫码。")
        return
    print("  ✓ L1 通过：登录态正常")

    if not deep:
        print()
        print("  ── 结论：页面能正常打开、登录态还在，风控大概率已结束。 ──")
        print("     （本次只做了纯读探测，没有发任何接口请求，零风控风险。）")
        print("     想再确认接口层是否恢复，可加 --deep 做一次最小接口探测。")
        return

    # ── L2：会话列表接口还理不理你（比 joblist 轻）──
    print("  [L2] 探会话列表接口（1 次轻请求）...")
    js = """
        (async () => {
          try {
            const r = await fetch(__URL__, {credentials: "include"});
            const j = await r.json();
            // getGeekFriendList 成功返回 zpData.result（数组，可能为空），没有 code 字段。
            const res = (j.zpData && j.zpData.result);
            const list = Array.isArray(res) ? res : [];
            return JSON.stringify({
              ok: Array.isArray(res),
              count: list.length,
              msg: (j.message||'').slice(0,80),
              retryAfter: r.headers.get('retry-after') || r.headers.get('Retry-After') || null,
              rateReset: r.headers.get('x-ratelimit-reset') || r.headers.get('X-RateLimit-Reset') || null
            });
          } catch (e) { return JSON.stringify({error: String(e)}); }
        })()
    """.replace("__URL__", json.dumps(FRIEND_LIST_API))
    try:
        r2 = json.loads(evaluate(tab, js, timeout=20) or "{}")
    except Exception as e:
        r2 = {"error": str(e)}
    if r2.get("error"):
        print(f"  ⚠ 接口调用失败：{r2['error']}（可能是网络或浏览器问题，不是风控信号）")
        return
    if not r2.get("ok"):
        msg = r2.get("msg", "")
        if any(w in msg for w in ["异常", "频繁", "验证", "登录", "captcha"]):
            print(f"  ⛔ 接口返回异常：msg={msg}")
            print("     → 结论：风控/登录态有问题。立刻停。")
            _mark_probe_risk("L2", r2)
            return
        print(f"  ⚠ 接口返回不是预期的 result 结构（{msg or '无 msg'}），拿不到会话数据。")
        print("     → 结论：接口层异常，建议再等等，别急着扫描。")
        return
    print(f"  ✓ L2 通过：会话列表接口返回正常（读到 {r2.get('count')} 条会话）")

    # ── L3：真正的岗位搜索（历史上触发 code=37 的那类，最重，最谨慎）──
    print("  [L3] 探一次岗位搜索（只 1 页、只 1 个词，最小试探）...")
    api = JOBLIST_API.format(query=urllib.parse.quote("AI产品经理"),
                             city=CITY_CODES.get("北京", "101010100"), page=1)
    js3 = """
        (async () => {
          try {
            const r = await fetch(__URL__, {credentials: "include"});
            const j = await r.json();
            const list = (j.zpData && j.zpData.jobList) || [];
            return JSON.stringify({
              code: j.code, count: list.length, msg: (j.message||'').slice(0,80),
              retryAfter: r.headers.get('retry-after') || r.headers.get('Retry-After') || null,
              rateReset: r.headers.get('x-ratelimit-reset') || r.headers.get('X-RateLimit-Reset') || null
            });
          } catch (e) { return JSON.stringify({error: String(e)}); }
        })()
    """.replace("__URL__", json.dumps(api))
    try:
        r3 = json.loads(evaluate(tab, js3, timeout=30) or "{}")
    except Exception as e:
        r3 = {"error": str(e)}
    if r3.get("error"):
        print(f"  ⚠ 搜索接口调用失败：{r3['error']}")
        return
    if r3.get("code") != 0:
        msg = r3.get("msg", "")
        if "37" in str(r3.get("code")) or any(w in msg for w in ["异常", "频繁", "验证", "captcha"]):
            print(f"  ⛔ 搜索接口触发风控：code={r3.get('code')} msg={msg}")
            print("     → 结论：风控还没结束。立刻停，并记冷却。")
            log_risk(f"probe L3　code={r3.get('code')} {msg}")
            # 优先信 Retry-After 头（让 BOSS 告诉你要等多久），没有才按次数翻倍
            ra = _retry_after_seconds({
                "retry-after": r3.get("retryAfter"),
                "x-ratelimit-reset": r3.get("rateReset"),
            })
            if ra:
                set_cooldown(seconds=ra)
                print(f"     BOSS 返回 Retry-After，按它要求冷却 {int(ra // 60)} 分钟。")
            else:
                set_cooldown()
            return
        print(f"  ⚠ 搜索接口返回 code={r3.get('code')}（{msg}）")
        return
    print(f"  ✓ L3 通过：搜索接口返回 {r3.get('count')} 条，风控已结束。")
    print()
    print("  ── 结论：四层全部通过，风控已彻底结束，可以正常扫描。 ──")
    # 四层全过 = 确认恢复，清掉连续触发次数，回到干净状态
    clear_cooldown()


def _mark_probe_risk(level, detail):
    """探测到风控时记一笔，并进入冷却，避免反复探测。"""
    log_risk(f"probe {level}　{json.dumps(detail, ensure_ascii=False)[:120]}")
    # 优先信 Retry-After 头（如果 detail 里带了），没有才按次数翻倍
    if isinstance(detail, dict):
        ra = _retry_after_seconds({
            "retry-after": detail.get("retryAfter"),
            "x-ratelimit-reset": detail.get("rateReset"),
        })
        if ra:
            set_cooldown(seconds=ra)
            return
    set_cooldown()


def load_list():
    p = path_of("data/jobs_list.json")
    if not os.path.exists(p):
        print("没有 data/jobs_list.json，先跑 python fetch_cdp.py scan")
        return None
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def main():
    ap = argparse.ArgumentParser(description="从你自己的浏览器里读 BOSS 岗位")
    ap.add_argument("cmd", choices=["check", "scan", "detail", "one", "history", "bootstrap", "cooldown", "probe"])
    ap.add_argument("--deep", action="store_true",
                    help="probe 专用：加这一项才做接口层探测（L2/L3），默认只做纯读页面")
    ap.add_argument("--cities", default="")
    ap.add_argument("--queries", default="")
    ap.add_argument("--pages", type=int, default=1)
    ap.add_argument("--cap", type=int, default=None)
    ap.add_argument("--wait", type=float, default=4.0)
    ap.add_argument("--resume", action="store_true",
                    help="接着上次跑：跳过已经扫过的「城市+搜索词」")
    ap.add_argument("--input", default="",
                    help="detail/one 专用：指定要开详情页的岗位清单文件（默认 jobs_list.json）")
    args = ap.parse_args()

    if args.cmd == "check":
        cmd_check(args)
        return

    if args.cmd == "cooldown":
        left = cooldown_left()
        if left > 0:
            print(f"还在风控冷却期，还差 {int(left // 60)} 分 {int(left % 60)} 秒解禁。")
        else:
            print("当前不在冷却期，可以正常扫。")
        return

    # bootstrap 命令保留但已退化：驱动走插件，无需开调试端口
    if args.cmd == "bootstrap":
        cmd_bootstrap(args)
        return

    tab = require_tab()
    if tab is None:
        return

    if args.cmd == "history":
        cmd_history(tab)
        return

    if args.cmd == "probe":
        cmd_probe(tab, deep=args.deep)
        return

    cities = [c.strip() for c in args.cities.split(",") if c.strip()] \
        or CFG["hard_filter"]["city"]["cities"]
    queries = [q.strip() for q in args.queries.split(",") if q.strip()] or DEFAULT_QUERIES
    # 用户没显式指定 --queries 时，每天轮换一下扫描顺序（打破固定时序特征）
    if not args.queries.strip():
        queries = rotate_queries(queries)

    if args.cmd in ("scan", "one"):
        # 2026-11-07 改：取消「每批只扫前 2 城」的限制，改为「主城优先、全城扫」。
        #   背景：原来只扫 2 城是防「注入 fetch 被识别」的 code37（2 城×N 词请求量小）。
        #   但 11-07 已把搜索改成「导航+读DOM」（页面原生请求），实测 17 词×2 城
        #   34 个页面全程零 code37，code37 的绝对主因是注入 fetch，不是请求量。
        #   现在目标改为「每天攒够 80 份可投招呼语」，需要一次扫全城才够量，
        #   所以放开限制。主城（京沪深杭广）排最前：万一中途撞风控，主城也扫完了。
        #   显式指定了 --cities 就按指定的顺序来，不做主城重排。
        _explicit = bool(args.cities.strip())
        if not _explicit:
            tier1 = ["北京", "上海", "深圳", "杭州", "广州"]
            head = [c for c in tier1 if c in cities]
            tail = [c for c in cities if c not in tier1]
            cities = head + tail
        print(f"开始扫：{len(cities)} 城 × {len(queries)} 个词 × {args.pages} 页")
        print(f"（主城优先：{'、'.join(cities[:5]) if len(cities) > 5 else '、'.join(cities)}）")
        print(f"（大概要 {len(cities)*len(queries)*args.pages*8/60:.0f} 分钟，别关浏览器）")
        jobs = scan(tab, cities, queries, args.pages, args.wait, resume=args.resume)
    else:
        if args.input:
            ip = args.input if os.path.isabs(args.input) else path_of(args.input)
            if not os.path.exists(ip):
                print(f"--input 指定的清单不存在：{ip}")
                return
            with open(ip, encoding="utf-8") as f:
                jobs = json.load(f)
        else:
            jobs = load_list()
        if jobs is None:
            return

    if args.cmd in ("detail", "one"):
        if jobs:
            enrich_details(tab, jobs, args.cap, args.wait, resume=args.resume)


if __name__ == "__main__":
    main()
