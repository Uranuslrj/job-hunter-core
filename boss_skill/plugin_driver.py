# -*- coding: utf-8 -*-
"""chrome-use 插件驱动层：替代 fetch_cdp 的 CDP 调试端口连接。

为什么换（结论来自 docs/插件路线落地方案-恢复后照做-20261102.md）：
    code=37「环境异常」的根因是反爬 Cookie __zp_stoken__ 失效，而它失效的
    触发点是「调试端口」这个能被页面侧探测的自动化尾巴。chrome-use 走
    Native Messaging + Chrome 扩展，不开调试端口，驱动的是你日常登录的
    Chrome（真实档案、真实痕迹），BOSS 看到的是「一个正常用户」。

和 fetch_cdp 的关系：
    fetch_cdp 里所有「读页面/发接口」的活，本质都是往页面里注入一段 JS 然后
    取返回值（Runtime.evaluate）。chrome-use 的 `eval` 命令干的正是同一件事，
    只是从「调试端口」换成了「扩展 relay」。所以迁移 = 把 evaluate 换成 eval，
    逻辑层（招呼语/打分/台账/六段框架/config）一个字不动。

用法（供 token_diag.py 和后续迁移的脚本调用）：
    from plugin_driver import eval_js, open_url, read_url, current_url
    print(eval_js("document.title"))

铁律：这个模块只封装「驱动浏览器」的最小动作，不含任何业务逻辑、不投、不写账号。
"""

import json
import os
import subprocess
import tempfile

# chrome-use 可执行文件路径（安装时落在用户目录，已进 PATH，但这里写死保险）
# 开源版默认留空，让用户通过环境变量 CHROME_USE_BIN 指定自己机器的安装路径。
CHROME_USE = os.environ.get(
    "CHROME_USE_BIN",
    "chrome-use",   # 默认直接调 PATH 里的 chrome-use；找不到会提示设 CHROME_USE_BIN
)

# BOSS 需要登录的页面。诊断/迁移时先 open 到这一页，确认登录态。
BOSS_CHAT_URL = "https://www.zhipin.com/web/geek/chat"


def _run(args, timeout=60):
    """跑一条 chrome-use 命令，返回 (success: bool, data: dict)。

    chrome-use 的 --json 输出形如 {"success":true,"data":{...},"error":null,...}。
    这里统一解析成 (success, data)，让上层不用关心 CLI 细节。
    """
    cmd = [CHROME_USE] + args + ["--json"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return False, {"error": "chrome-use 命令超时（%ds）" % timeout}
    except FileNotFoundError:
        return False, {"error": "找不到 chrome-use，路径：%s" % CHROME_USE}
    except Exception as e:
        return False, {"error": "chrome-use 调用失败：%s" % e}

    out = (p.stdout or "").strip()
    if not out:
        return False, {"error": "chrome-use 无输出", "stderr": (p.stderr or "")[:200]}
    try:
        d = json.loads(out)
    except Exception:
        # 有些命令不带 --json 或输出不是 JSON，原样塞进 data
        return False, {"error": "chrome-use 输出非 JSON", "raw": out[:500]}
    if isinstance(d, dict) and d.get("success") is False:
        return False, {"error": d.get("error") or "chrome-use 返回 success=false"}
    return True, d


def eval_js(js, timeout=60):
    """在浏览器里执行一段 JS，返回求值结果（原始值，可能是 str/dict/...）。

    返回的是 JS 表达式的「值」。如果要拿 JSON 字符串，JS 里自己 return JSON.stringify(...)。
    失败返回 None 并打印原因（不抛异常，因为驱动层失败不该崩掉业务层）。
    """
    # 用临时文件传脚本，避免 shell 转义/中文/引号问题
    fd, path = tempfile.mkstemp(suffix=".js", prefix="cu_eval_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(js)
    except Exception as e:
        return None
    try:
        ok, d = _run(["eval", "--file", path], timeout=timeout)
    finally:
        try:
            os.remove(path)
        except Exception:
            pass
    if not ok:
        print("  [plugin_driver] eval 失败：", d.get("error", d))
        return None
    # chrome-use eval --json 的结果：data.result 是 JS 的返回值
    if isinstance(d, dict):
        return d.get("data", {}).get("result") if isinstance(d.get("data"), dict) else d.get("data")
    return d


def eval_json(js, timeout=60):
    """执行 JS 并期望它 return 一个 JSON 字符串，解析成 dict 返回。

    诊断脚本里几乎所有注入都 return JSON.stringify(...)，用这个最省心。
    """
    raw = eval_js(js, timeout=timeout)
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(str(raw))
    except Exception:
        return {"raw": raw}


def current_url(timeout=30):
    """当前活动标签页的 URL。"""
    return eval_js("window.location.href", timeout=timeout)


def open_url(url, timeout=90):
    """导航到指定 URL（chrome-use open）。返回是否成功。"""
    ok, d = _run(["open", url], timeout=timeout)
    return ok


def read_url(url=None, timeout=60, raw=False):
    """读页面文字（可选指定 URL，缺省读活动标签页）。返回文本或 None。"""
    args = ["read"]
    if raw:
        args.append("--raw")
    if url:
        args.append(url)
    ok, d = _run(args, timeout=timeout)
    if not ok:
        return None
    # read --json 返回 data 里可能是 result 或直接是文本
    if isinstance(d, dict):
        data = d.get("data", {})
        if isinstance(data, dict):
            return data.get("result") or data.get("text") or ""
        return str(data)
    return str(d)


def click_coord(x, y, timeout=60):
    """在视口坐标 (x,y) 处点一下（对应旧 CDP Input.dispatchMouseEvent 的点击）。

    chrome-use click 坐标用法是 `click <x> <y>`（两个独立参数），
    不能拼成 "x y" 一个字符串。
    """
    ok, d = _run(["click", str(int(x)), str(int(y))], timeout=timeout)
    return ok


def insert_text(text, timeout=60):
    """往当前聚焦的输入框一次性写入文字（对应旧 Input.insertText）。

    用 --stdin 传，避免 shell 转义/中文/引号/多行问题。
    """
    # keyboard inserttext --stdin 从标准输入读正文
    try:
        p = subprocess.run(
            [CHROME_USE, "keyboard", "inserttext", "--stdin", "--json"],
            input=text, capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        return False
    out = (p.stdout or "").strip()
    try:
        d = json.loads(out)
    except Exception:
        return False
    return bool(d) and d.get("success") is not False


def press_key(key, timeout=60):
    """按一个键（对应旧 Input.dispatchKeyEvent，例如 Enter）。"""
    ok, d = _run(["press", key], timeout=timeout)
    return ok


def is_boss_page():
    """当前活动标签页是不是 BOSS。"""
    u = current_url() or ""
    return "zhipin.com" in u


def ensure_boss_page():
    """确保当前活动标签页是 BOSS 页面；不是就 open 过去。返回 (ok, url)。"""
    u = current_url() or ""
    if "zhipin.com" in u:
        return True, u
    ok = open_url(BOSS_CHAT_URL)
    if not ok:
        return False, u
    # 导航后给页面一点渲染时间，再读一次真实 URL
    import time
    time.sleep(3)
    u2 = current_url() or ""
    return "zhipin.com" in u2, u2


if __name__ == "__main__":
    # 自测：连上当前 Chrome，打印活动标签页 URL 和标题
    print("当前活动标签页 URL：", current_url())
    print("标题：", eval_js("document.title"))
