# 批量首触：对队列里的岗位逐条「详情页点立即沟通 → 平台代发模板话 → 进会话补发真话术」
# 这是今天验证过的可行路径：A 型弹窗输入路径在某公司上失效，B 型代发+补发是通的
# 用法：python boss_skill/_batch_send.py --send   （不加 --send 只演练）
import argparse
import csv
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fetch_cdp as F
import send_cdp as S
import run as _run
from config_loader import path_of

LEDGER = path_of("data/greeting_ledger.csv")
QUEUE = path_of("data/send_queue.json")
PLATFORM_DEFAULT = "您好，看到您的招聘信息，我很感兴趣，希望可以进一步沟通。"

JS_CLICK = r"""
(() => {
  const want = ["立即沟通", "继续沟通"];
  const all = [...document.querySelectorAll('a,button,div,span')];
  for (const w of want) {
    for (const el of all) {
      if (((el.innerText || '').trim() === w) && (el.offsetWidth || el.offsetHeight)) {
        const b = el.getBoundingClientRect();
        return {x: b.x + b.width/2, y: b.y + b.height/2};
      }
    }
  }
  return null;
})()
"""

JS_POP = r"""
(() => {
  const d = document.querySelector('.greet-pop');
  if (!d || !d.offsetWidth) return null;
  const btn = [...d.querySelectorAll('button,a,[class*="btn"]')]
                .find(b => /留在此页|继续沟通/.test((b.innerText || '').trim()) && b.offsetWidth);
  if (btn) btn.click();
  return (d.innerText || '').slice(0, 80);
})()
"""


def load_ledger_rows():
    with open(LEDGER, encoding="utf-8") as f:
        return list(csv.DictReader(f)), list(_run.FIELDS)


def save_ledger(rows, fields):
    with open(LEDGER, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def mark(job_id, sent, verify, note=""):
    rows, fields = load_ledger_rows()
    for r in rows:
        if (r.get("job_id") or "").strip() == job_id:
            r["sent"] = sent
            r["verify"] = verify
            if note:
                old = (r.get("notes") or "").strip()
                r["notes"] = (old + "　" if old else "") + note
    save_ledger(rows, fields)


def send_one(tab, item, do_send):
    job_id = item["job_id"]
    text = item["greeting_text"].strip()
    company = item["company"]

    # 绑定：台账里必须有这条且未发
    rows, fields = load_ledger_rows()
    mine = [r for r in rows if (r.get("job_id") or "").strip() == job_id]
    if len(mine) != 1:
        return "skip", f"台账命中 {len(mine)} 条，不唯一"
    if mine[0]["sent"] == "已发":
        return "skip", "台账里已发"

    url = item.get("url") or ""
    full = url if url.startswith("http") else "https://www.zhipin.com" + url
    F.goto(tab, full, wait=6)

    box = F.evaluate(tab, JS_CLICK)
    if not box:
        return "skip", "详情页没有沟通按钮"
    F.cdp(tab, "Input.dispatchMouseEvent",
          {"type": "mouseMoved", "x": box["x"], "y": box["y"]})
    time.sleep(0.3)
    F.cdp(tab, "Input.dispatchMouseEvent",
          {"type": "mousePressed", "x": box["x"], "y": box["y"], "button": "left", "clickCount": 1})
    F.cdp(tab, "Input.dispatchMouseEvent",
          {"type": "mouseReleased", "x": box["x"], "y": box["y"], "button": "left", "clickCount": 1})
    time.sleep(5)

    pop = F.evaluate(tab, JS_POP)          # 关弹窗，顺便看它说什么

    # 不拿"点击前的会话列表"跟"点击后的"做对比 —— 2026-10-29 踩过：
    # Boss 的会话列表有刷新延迟，对比法会把"平台已经代发成功"误判成"点击没生效"，
    # 结果 3 条岗位停在"只发了模板话、真话术没补上"的半吊子状态。
    # 改成：去聊天页，按岗位 ID 唯一命中（跟 _resend.py 同一套），没命中就等一会再查。
    F.goto(tab, "https://www.zhipin.com/web/geek/chat", wait=5)
    conv = None
    for _ in range(3):
        hit = [c for c in S.friend_list(tab) if (c.get("job_id") or "") == job_id]
        if hit:
            conv = hit[0]
            break
        time.sleep(7)
    if conv is None:
        return "skip", "点完之后按岗位 ID 找不到会话（平台可能没代发，也可能列表还没刷出来）"
    last = (conv.get("last") or "").strip()
    if not S.sent_matches(PLATFORM_DEFAULT, last):
        if S.sent_matches(text, last):
            mark(job_id, "已发", "ok（平台代发+补发均已确认）")
            return "ok", "会话里已经有我们这句（可能之前发过），记账为已发"
        return "skip", f"新会话最后一句不是平台模板话（{last[:20]}）→ 不动"

    # 打开会话补发（上面已经在聊天页了，不用整页重新导航一遍）
    ok, why = S._open_conv(tab, conv, first_text=PLATFORM_DEFAULT)
    if not ok:
        return "halt", f"打不开会话：{why}"
    if not S.type_text(tab, text):
        return "halt", "找不到输入框"
    time.sleep(0.8)
    back = S.read_input(tab).strip()
    if not S.same_text(back, text):
        S.clear_input(tab)
        return "halt", f"回读对不上（预期 {text[:16]}… 实际 {back[:16]}…）→ 已清空"
    if not do_send:
        S.clear_input(tab)
        return "dry", f"演练通过（{company}｜{conv['boss']}）"
    if not S.click_send(tab):
        S.clear_input(tab)
        return "halt", "点不到发送按钮"
    time.sleep(4)
    now = [c for c in S.friend_list(tab) if c["uid"] == conv["uid"]]
    if now and S.sent_matches(text, now[0]["last"]):
        mark(job_id, "已发", "ok（平台代发+补发，发完回读一致）",
             f"{time.strftime('%m-%d %H:%M')} B型代发+补发。")
        return "ok", f"已发（{company}｜{conv['boss']}｜{conv['boss_title']}）"
    return "halt", "发完回读对不上，去页面人工确认"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--send", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()
    q = json.load(open(QUEUE, encoding="utf-8"))
    if args.limit:
        q = q[:args.limit]
    tab = F.require_tab()
    stat = {"ok": 0, "dry": 0, "skip": 0, "halt": 0}
    for i, item in enumerate(q, 1):
        try:
            res, why = send_one(tab, item, args.send)
        except Exception as e:
            res, why = "halt", f"异常：{str(e)[:80]}"
        stat[res] = stat.get(res, 0) + 1
        print(f"[{i}/{len(q)}] {res:4s} {item['company']}｜{item['title'][:22]} —— {why}", flush=True)
        if res == "halt":
            break
        time.sleep(8)
    print(f"\n小结：{stat}")


if __name__ == "__main__":
    main()
