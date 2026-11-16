# -*- coding: utf-8 -*-
"""token 链路诊断：查清楚 code=37 到底断在 token 的哪一环。

背景（结论来自 docs/风控审计-多Agent-20261102.md）：
    BOSS 的 code=37「环境异常」，真实根因是反爬 Cookie `__zp_stoken__` 失效，
    不是账号被判死。这个 Cookie 由前端 JS 动态生成，跟「当前浏览器会话」绑定，
    过期后所有接口返回 code=37。

本脚本要回答的问题（四层，从轻到重）：
    L1  token 在不在？       —— 读 Cookie，看 __zp_stoken__ 有没有、有没有值
    L2  token 新不新？       —— 看 Cookie 的过期时间，判断是不是刚过期/根本没写
    L3  环境被标记没？       —— 读 navigator.webdriver + 调试端口泄露信号
    L4  接口层通不通？       —— 发一次最轻的会话列表请求，看返回 code

用法：
    python token_diag.py            # 只做 L1-L3（纯读，零风控风险）
    python token_diag.py --deep     # 加 L4（发一次轻接口请求）

⚠ 铁律：这个脚本只读、不写、不投、不搜。任何一层发现问题就停，绝不硬闯下一层。
   建议在账号恢复后、决定「要不要继续自动化」之前跑，先看清楚 token 断在哪。
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import plugin_driver as PD   # 2026-11-05 改：从 fetch_cdp(CDP调试端口) 换成 chrome-use 插件


# ─────────────────────────────────────────────────────────────
# L1：token 在不在
# ─────────────────────────────────────────────────────────────

JS_READ_COOKIE = """
(() => {
  const want = ["__zp_stoken__", "__g", "wp", "acw_tc", "zp_s_id"];
  const out = {};
  document.cookie.split(";").forEach(p => {
    const i = p.indexOf("=");
    if (i < 0) return;
    const k = p.slice(0, i).trim();
    const v = p.slice(i + 1).trim();
    if (want.indexOf(k) >= 0) {
      // Cookie 值可能很长，只记前 40 字 + 总长，不把完整 token 打进日志
      out[k] = { len: v.length, head: v.slice(0, 40) };
    }
  });
  // 除了 document.cookie，再数一下一共有多少个 cookie（辅助判断登录态在不在）
  out["_total_cookies"] = document.cookie.split(";").filter(s => s.trim()).length;
  return JSON.stringify(out);
})()
"""


def check_cookie_present():
    """L1：看关键 Cookie 在不在。返回 (结论, 详情 dict)。"""
    d = PD.eval_json(JS_READ_COOKIE)
    stoken = d.get("__zp_stoken__")
    total = d.get("_total_cookies", 0)

    if stoken and stoken.get("len", 0) > 0:
        return True, {"stoken_len": stoken["len"], "total": total}
    return False, {"stoken_len": 0, "total": total,
                   "说明": "__zp_stoken__ 不在 Cookie 里（要么没登录，要么登录态已被清）"}


# ─────────────────────────────────────────────────────────────
# L2：token 新不新（过期时间）
# ─────────────────────────────────────────────────────────────

JS_STOKEN_EXPIRY = """
(() => {
  // 从 Cookie 里找 __zp_stoken__，读它的「过期时间」。
  // 这个 Cookie 由前端 JS 动态算，30-60 分钟就过期；看它的过期时刻能判断
  // 「是正常过期了」还是「根本没被正确刷新」。
  const pairs = document.cookie.split(";").map(s => s.trim()).filter(Boolean);
  const line = pairs.find(p => p.startsWith("__zp_stoken__="));
  if (!line) return JSON.stringify({found: false});
  const i = line.indexOf("=");
  const v = line.slice(i + 1);
  // 一个 cookie 完整条目形如 "k=v; expires=...; path=..."
  // 但 document.cookie 读不到 expires，这里只能拿到值。所以换用「值本身」判断：
  //   stoken 的值是 base64 或 hex，长度有规律；这里只回报值长度和一个稳定指纹
  let hash = 0;
  for (let c = 0; c < v.length; c++) hash = (hash * 31 + v.charCodeAt(c)) >>> 0;
  return JSON.stringify({
    found: true,
    value_len: v.length,
    value_fingerprint: hash,
    // 有没有带 expires 无从得知，这里靠值长度给个粗判：
    //   BOSS 的 stoken 一般几十到一百多字符，太短可能是占位/失效态
    looks_valid: v.length >= 20
  });
})()
"""


def check_token_fresh():
    """L2：token 值是否像「有效」的（长度够）。返回 (结论, 详情)。"""
    d = PD.eval_json(JS_STOKEN_EXPIRY)
    if not d.get("found"):
        return False, {"说明": "读不到 __zp_stoken__ 的值"}
    if not d.get("looks_valid"):
        return False, {"说明": "token 值过短（可能已失效/被替换成占位）", **d}
    return True, d


# ─────────────────────────────────────────────────────────────
# L3：环境被标记没（webdriver + 调试端口泄露）
# ─────────────────────────────────────────────────────────────

JS_ENV_SIGNAL = """
(() => {
  const out = {
    webdriver: (navigator.webdriver === true),
    // 有没有 window.chrome（真 Chrome/Edge 有，headless/抹痕环境可能没有）
    has_chrome: !!window.chrome,
    // navigator.plugins 空不空（自动化环境常为空）
    plugins_count: (navigator.plugins ? navigator.plugins.length : -1),
    // languages 空不空
    languages: (navigator.languages || []).join(","),
    // 有没有被注入过自动化脚本的痕迹（常见变量名，出现即高度可疑）
    auto_vars: ["_phantom", "__nightmare", "callPhantom", "domAutomation"]
      .filter(k => typeof window[k] !== "undefined"),
  };
  return JSON.stringify(out);
})()
"""


def check_env_signal():
    """L3：读环境自动化信号。返回 (结论, 详情)。"""
    d = PD.eval_json(JS_ENV_SIGNAL)
    problems = []
    if d.get("webdriver"):
        problems.append("navigator.webdriver = true（自动化标记没抹掉）")
    if not d.get("has_chrome"):
        problems.append("window.chrome 不存在（可能是无头/抹痕环境）")
    if d.get("plugins_count") == 0:
        problems.append("navigator.plugins 为空（真浏览器不会这样）")
    if d.get("auto_vars"):
        problems.append("检测到自动化注入变量：" + ",".join(d["auto_vars"]))
    return (not problems), {"problems": problems, **d}


# ─────────────────────────────────────────────────────────────
# L4：接口层通不通（--deep 才做）
# ─────────────────────────────────────────────────────────────

FRIEND_API = ("https://www.zhipin.com/wapi/zprelation/friend/"
              "getGeekFriendList.json?page=1&pageSize=3")

JS_FRIEND_TEST = """
(async () => {
  try {
    const r = await fetch(__URL__, {credentials: "include"});
    const j = await r.json();
    const res = (j.zpData && j.zpData.result);
    return JSON.stringify({
      http_ok: r.ok,
      code: j.code,
      is_array: Array.isArray(res),
      count: Array.isArray(res) ? res.length : -1,
      msg: (j.message || "").slice(0, 80),
      retry_after: r.headers.get("retry-after") || null
    });
  } catch (e) {
    return JSON.stringify({error: String(e)});
  }
})()
""".replace("__URL__", json.dumps(FRIEND_API))


def check_api():
    """L4：发一次最轻的会话列表请求，看 code。返回 (结论, 详情)。"""
    d = PD.eval_json(JS_FRIEND_TEST)
    if d.get("error"):
        return False, {"说明": f"接口调用抛异常：{d['error']}"}
    code = d.get("code")
    if code == 0 and d.get("is_array"):
        return True, {"说明": f"接口正常，读到 {d.get('count')} 条会话", **d}
    if code == 37:
        return False, {"说明": "code=37 环境异常（token 失效/环境指纹不对），正是要查的那个", **d}
    if code == 36:
        return False, {"说明": "code=36 账号被标记（需手动验证），比 37 更严重", **d}
    if code == 32:
        return False, {"说明": "code=32 账号临时封禁", **d}
    if code == 1006:
        return False, {"说明": "code=1006 限流", **d}
    return False, {"说明": f"接口返回 code={code}（未分类），msg={d.get('msg','')}", **d}


# ─────────────────────────────────────────────────────────────
# 主流程：四层依次查，断了就停
# ─────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="token 链路诊断：查 code=37 断在哪一环")
    ap.add_argument("--deep", action="store_true",
                    help="加这一项才发接口请求（L4），默认只做纯读 L1-L3")
    args = ap.parse_args()

    print()
    print("  ===== token 链路诊断 =====")
    print("  目的：查清楚 code=37 到底断在 token 的哪一环")
    print("  铁律：只读不写，任何一层发现问题就停，不硬闯下一层")
    print()

    # 先连浏览器（chrome-use 插件，驱动你日常登录的 Chrome）
    url = PD.current_url() or ""
    if not url or url == "about:blank" or "zhipin" not in url:
        print("  ⚠ 当前活动标签页不是 BOSS（或为空页）。")
        print(f"     当前 URL：{url[:60] if url else '（读不到）'}")
        print("     尝试用插件打开 BOSS 聊天页 ...")
        ok, u2 = PD.ensure_boss_page()
        if not ok:
            print("  ✗ 无法打开 BOSS 页面。请先手动在 Chrome 里打开 zhipin.com 并确认登录，再跑。")
            return
        url = u2
        print(f"     已打开：{url[:60]}")

    # 确认当前页面是 BOSS，否则 Cookie 读的是别的站
    if "zhipin" not in url:
        print("  ✗ 当前页面仍不是 BOSS，先手动打开 zhipin.com 并登录，再跑。")
        return

    all_ok = True

    # ── L1 ──
    print("  [L1] 关键 Cookie 在不在 ...")
    ok, d = check_cookie_present()
    if not ok:
        print(f"  ✗ L1 不过：{d.get('说明')}")
        print("     → 结论：token 根本没写进 Cookie。要么没登录，要么登录态被清。")
        print("       先手动在浏览器里确认右上角有头像、是登录状态。")
        return
    print(f"  ✓ L1 通过：__zp_stoken__ 在（长度 {d.get('stoken_len')}，共 {d.get('total')} 个 Cookie）")

    # ── L2 ──
    print("  [L2] token 值像不像有效 ...")
    ok, d = check_token_fresh()
    if not ok:
        print(f"  ✗ L2 不过：{d.get('说明')}")
        print("     → 结论：token 值异常（过短/失效），可能被替换成占位态。")
        print("       这就是「刷新了也没用」的典型表现——环境被盯上，token 发出来就作废。")
        all_ok = False
    else:
        print(f"  ✓ L2 通过：token 值长度 {d.get('value_len')}，指纹 {d.get('value_fingerprint')}")

    # ── L3 ──
    print("  [L3] 环境自动化信号 ...")
    ok, d = check_env_signal()
    if not ok:
        print(f"  ✗ L3 不过：")
        for p in d.get("problems", []):
            print(f"      · {p}")
        print("     → 结论：环境层还有自动化痕迹。webdriver 只是其一，")
        print("       chrome-use 插件虽已去掉「调试端口」，但页面侧仍可能读到 webdriver 等信号。")
        all_ok = False
    else:
        print("  ✓ L3 通过：webdriver=false，环境层没有明显自动化注入痕迹")
        print("     （走 chrome-use 插件，不再有调试端口这个最大破绽）")

    if not args.deep:
        print()
        if all_ok:
            print("  ── 结论：L1-L3 全过。token 在、值正常、环境无明显痕迹。 ──")
            print("     想确认接口层到底还报不报 code=37，加 --deep 做一次轻接口探测。")
        else:
            print("  ── 结论：上面有 ✗，token 链路在上游就断了。 ──")
            print("     先把 ✗ 那层解决，再谈接口层。别急着 --deep。")
        return

    # ── L4 ──
    print("  [L4] 接口层通不通（1 次轻请求）...")
    ok, d = check_api()
    if not ok:
        print(f"  ✗ L4 不过：{d.get('说明')}")
        print("     → 结论：接口层仍然报错。")
        if d.get("code") == 37:
            print("       code=37 = 环境异常（token 失效/环境指纹不对）。")
            print("       这说明 L1-L3 看着正常，但「环境」这个维度还是没过 BOSS 的关。")
            print("       最可能的原因：调试端口 + 独立档案这个组合被识别。")
            print("       → 根治方向：插件路线（不开调试端口、用日常浏览器）。")
        all_ok = False
    else:
        print(f"  ✓ L4 通过：{d.get('说明')}")

    print()
    if all_ok:
        print("  ── 结论：四层全过，token 链路通畅，可以正常扫描/投递。 ──")
    else:
        print("  ── 结论：上面有 ✗。对照 ✗ 在哪一层，就知道该修什么： ──")
        print("     L1 不过 → 没登录 / 登录态被清 → 先手动登录")
        print("     L2 不过 → token 值失效 → 环境被盯上，token 发出来就作废")
        print("     L3 不过 → 环境有自动化痕迹 → 换插件路线根治")
        print("     L4 不过 → 接口仍报 37/36 → 停手，走插件路线，别硬刷")
    print()


if __name__ == "__main__":
    main()
