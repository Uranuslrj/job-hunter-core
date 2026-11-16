# 补发一条：给【已经建好会话、但里面只有平台默认模板话】的那家，补上我们自己写的招呼语
#
# 跟正常首触的区别：
#   会话已经存在了（点「立即沟通」的时候平台替我们发了默认模板），
#   所以这里【不能再点一次立即沟通】，而是直接进聊天页找到这个人、打字、发。
#
# 为什么单独写一个脚本而不是用 send_cdp.py follow：
#   follow 要求"会话最后一句 = 台账里记的那句"，但这里会话里是平台模板话，
#   跟台账记的对不上 → follow 会直接跳过。所以走单独一条路。
#
# 用法：
#   python boss_skill/_resend.py --job-id xxx          演练（打字+回读，不发）
#   python boss_skill/_resend.py --job-id xxx --send   真发

import argparse
import csv
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fetch_cdp as F
import send_cdp as S
import run as _run
from config_loader import path_of

LEDGER = path_of("data/greeting_ledger.csv")
PLATFORM_DEFAULT = "您好，看到您的招聘信息，我很感兴趣，希望可以进一步沟通。"


def find_row(job_id):
    if not os.path.exists(LEDGER):
        return None, []
    with open(LEDGER, encoding="utf-8") as f:
        rd = csv.DictReader(f)
        rows = list(rd)
    for r in rows:
        if (r.get("job_id") or "").strip() == job_id:
            return r, rows
    return None, rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--job-id", required=True)
    ap.add_argument("--send", action="store_true")
    args = ap.parse_args()

    row, rows = find_row(args.job_id)
    if row is None:
        print(f"台账里找不到岗位 ID：{args.job_id}")
        return
    text = (row.get("greeting_text") or "").strip()
    if not text:
        print("台账里这条没有招呼语原文。")
        return

    print(f"公司：{row.get('company')}　岗位：{row.get('title')}　城市：{row.get('city')}")
    print(f"招呼语（{len(text)} 字）：")
    print(f"　{text}")
    print()

    tab = F.require_tab()
    if tab is None:
        return

    # ① 绑定：只认岗位 ID 完全相同
    convs = S.friend_list(tab)
    hit = [c for c in convs if (c.get("job_id") or "") == args.job_id]
    if len(hit) != 1:
        print(f"按岗位 ID 命中 {len(hit)} 个（不是唯一）→ 不敢发，停。")
        return
    conv = hit[0]
    print(f"会话：{conv['boss']}｜{conv['company']}｜{conv['boss_title']}")
    print(f"最后一条：{conv['last'][:40]}")
    if conv["theirs"]:
        print("对方已经回话了 → 交给你人工处理，机器不接话。")
        return
    # 会话里必须是我们当初那句或者平台模板话，二选一，否则说明找错人了
    # 用 sent_matches（前缀比对）而不是 same_text（逐字比对）：
    #   接口返回的 lastMsg 会被截断，逐字比必然对不上，会把"已经发过了"误判成"情况不明"
    last = (conv.get("last") or "").strip()
    if S.sent_matches(text, last):
        print("这句已经在这个会话里了，不重复发。")
        return
    if not S.sent_matches(PLATFORM_DEFAULT, last):
        print(f"会话最后一句既不是我们写的、也不是平台模板话（是「{last[:24]}」）→ 不敢发，停。")
        return

    # ② 打开会话
    F.goto(tab, "https://www.zhipin.com/web/geek/chat", wait=6)
    time.sleep(3)
    ok, why = S._open_conv(tab, conv, first_text=PLATFORM_DEFAULT)
    if not ok:
        print(f"打不开会话：{why}")
        return
    print(f"打开会话：{why}")

    # ③ 打字 + 逐字回读
    if not S.type_text(tab, text):
        print("找不到输入框，停。")
        return
    time.sleep(0.8)
    back = S.read_input(tab).strip()
    print(f"回读（{len(back)} 字）：{back[:60]}…")
    if not S.same_text(back, text):
        S.clear_input(tab)
        print(f"回读对不上，已清空，没发。\n　预期：{text[:30]}\n　实际：{back[:30]}")
        return

    if not args.send:
        S.clear_input(tab)
        print()
        print("演练通过，已清空输入框，一条都没发。确认没问题加 --send。")
        return

    # ④ 发 + 发完回读
    if not S.click_send(tab):
        S.clear_input(tab)
        print("点不到发送按钮，停。")
        return
    time.sleep(4)

    again = S.friend_list(tab)
    now = [c for c in again if c["uid"] == conv["uid"]]
    if not now:
        print("发完之后列表里找不到这个会话了 → 去页面上人工确认一下。")
        return
    if S.sent_matches(text, now[0]["last"]):
        print(f"✓ 已补发。会话最后一条：{now[0]['last'][:50]}")
        _mark(row, rows, args.job_id, "已补发")
    else:
        print(f"⚠ 发完回读对不上，去页面人工确认。会话最后一条：{now[0]['last'][:50]}")


def _mark(row, rows, job_id, tag):
    for r in rows:
        if (r.get("job_id") or "").strip() == job_id:
            r["sent"] = "已发"
            r["verify"] = "ok（补发，第二条）"
            old = (r.get("notes") or "").strip()
            r["notes"] = (old + "　" if old else "") + \
                         f"{time.strftime('%Y-%m-%d %H:%M')} 已补发（会话里第一条是平台模板话，我们这句是第二条）。"
    # 2026-11-12 修：表头统一用 run.FIELDS，不从旧文件 fieldnames 取（防字段漂移冲数据）
    fields = list(_run.FIELDS)
    with open(LEDGER, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    print(f"台账已更新 → {LEDGER}")


if __name__ == "__main__":
    main()
