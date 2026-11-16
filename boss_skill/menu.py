# 投递流程菜单
#
# 为什么菜单用 python 写、不写在 bat 里：
#   cmd 读 bat 是分块缓冲的，切代码页的时机对不上。中文里有些字的 GBK 字节
#   恰好是 0x3A（半角冒号），cmd 会当成命令分隔符，后半句就变成一条"命令"被执行。
#   python 输出到控制台走宽字符接口，不受代码页影响，所以中文放这里最稳。

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable

MENU = [
    ("0", "检查浏览器连上没有", ["fetch_cdp.py", "check"]),
    ("1", "抓历史投递记录（去重用）", ["fetch_cdp.py", "history"]),
    ("2", "抓今天的岗位（约 15 分钟，中途停了可接着跑）", ["fetch_cdp.py", "one", "--resume"]),
    ("3", "生成投递清单 + 招呼语", ["run.py", "--auto"]),
    ("4", "探选择器（第一次必跑）", ["send_cdp.py", "probe"]),
    ("5", "演练，不真发（先看一遍）", ["send_cdp.py", "dry-run", "--limit", "3"]),
    ("6", "真发 10 条", ["send_cdp.py", "send", "--limit", "10"]),
    ("7", "每日巡检（四项指标自己判，不用你盯）", ["inspect.py"]),
    ("8", "★ 跑一整天（翻账→跟进→抓今天→发新客→收尾）", ["daily.py"]),
    ("9", "　└ 先演练一遍全程，一条都不真发", ["daily.py", "--dry", "--limit", "3"]),
    ("10", "翻聊天记录：更新台账里「回没回」", ["chat_scan.py", "match", "--apply"]),
    ("11", "给没回的补一句（跟进）", ["send_cdp.py", "follow"]),
    ("12", "评估：出提案，不改配置", ["evaluate.py"]),
]

HINT = {
    "0": "这是第一步。看到『已开』才算通。",
    "1": "去你后台把聊过 / 投过的抓回来，存到 data/raw/history_*.txt。",
    "2": "会扫 15 个城市 × 5 个搜索词。浏览器别关，别断网。",
    "3": "打分、写招呼语、过质量门，最后出 data/review_card.html。",
    "4": "验证我猜的按钮位置对不对。跑完把输出发我。",
    "5": "走完整流程，但最后一步不点发送。",
    "6": "今天只发 10 条。每条之间会随机歇 12-35 秒。",
    "7": "四项指标：执行率 100% / 风控 0 次 / 正确率 ≥98% / 回复率只看不下结论。",
    "8": "日常就按这个。顺序是「先翻账再抓今天」，错开会占用当天发送额度。",
    "9": "小规模测试先用这条：全程演练 3 条，一条都不真发。",
    "10": "翻台账这一步本来就归机器干，几分钟搞定。",
    "11": "跟进默认只发高/中把握；低把握列成清单等你勾。",
    "12": "样本不到 60 条（或结果还没落定）它会直接说『不到时候』。",
}


def show():
    print()
    print("  " + "=" * 56)
    print("     Boss 自动投递 —— 选一步")
    print("  " + "=" * 56)
    print()
    for k, label, _ in MENU:
        print(f"     {k}   {label}")
    print()
    print("     q   退出")
    print()
    if HINT:
        print("  " + "-" * 56)


def main():
    while True:
        show()
        c = input("     输入数字后回车（直接回车看说明）：").strip()

        if c.lower() == "q":
            return

        if c == "":
            print()
            print("     每一步是干什么的：")
            for k, label, _ in MENU:
                print(f"       {k}  {label}")
                print(f"          {HINT.get(k, '')}")
            input("\n     回车回到菜单 ...")
            continue

        hit = [m for m in MENU if m[0] == c]
        if not hit:
            print(f"     没有『{c}』这一步，重选。")
            continue

        _, label, args = hit[0]
        print()
        print("  " + "=" * 56)
        print(f"     正在跑：{label}")
        print("  " + "=" * 56)
        print()

        try:
            subprocess.run([PY] + args, cwd=HERE)
        except KeyboardInterrupt:
            print()
            print("     （你按了 Ctrl+C，回到菜单）")
            continue

        input("\n     这一步跑完了，回车回到菜单 ...")


if __name__ == "__main__":
    main()
