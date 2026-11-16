# 第五层：审阅卡 + 你按一次确认
#
# 为什么这一步不能省：
#   这是整套系统里唯一由你签字的地方。机器负责捞岗、筛岗、打分、写稿；
#   发出去这个动作必须有人按一下 —— 因为它收不回来。
#   一个 HR 你这辈子只会打扰一次，话术张冠李戴、简历投到黑名单公司，损失不可逆。
#
# 你的操作只有三种（都在一条命令行里完成）：
#   all       —— 按机器建议的全发
#   1,3,5     —— 只发这几条
#   n / 回车  —— 今天不发

import os


def _tier_of(score, cfg):
    for t in cfg["review_ui"]["tiering"]:
        if ">=" in t["score"] and score >= int(t["score"].replace(">=", "")):
            return t
    return None


def default_checked(item, cfg):
    for t in cfg["review_ui"]["tiering"]:
        if ">=" in t["score"]:
            if item["score"] >= int(t["score"].replace(">=", "")):
                return t["action"] == "默认勾选"
    return False


def render_terminal(items, cfg, cap):
    strong = cfg["scoring"]["thresholds"]["strong"]
    print()
    print("=" * 78)
    print(f"今天挑出来 {len(items)} 条（分数从高到低）　今日发送上限 {cap} 条")
    print("  ✓ = 机器建议发（分数 {} 以上）　· = 建议你过一眼".format(strong))
    print("=" * 78)
    for i, it in enumerate(items, 1):
        g = it.get("greeting") or {}
        mark = "√" if default_checked(it, cfg) else "·"
        flag = "" if g.get("ok") else "　! 质量门未过"
        print(f"[{i:>2}] {mark} {it['score']:>5.1f}　{it['title']}　{it['company']}　{it['city']}{flag}")
        pairs = " ".join(
            f"{v['name']}{v['score']}"
            for v in it["detail"].values()
            if isinstance(v, dict) and "score" in v
        )
        print(f"      六维：{pairs}")
        if it.get("family") is None:
            print("      岗位族：靠 JD 正文认出来的（名字里带不带 AI 都有）")
        t = (g.get("text") or "").strip()
        if t:
            print(f"      招呼语（{g.get('chars', len(t))} 字）：{t}")
            if not g.get("ok"):
                print(f"      ! {'; '.join(g.get('reasons', []))}")
        risks = g.get("risks") or []
        if risks:
            print(f"      ⚠ 注意：{'；'.join(risks)}")
        kws = g.get("explicit_keywords") or []
        if kws:
            print(f"      JD 命中词：{'、'.join(kws[:8])}")
        print()

    print("-" * 78)
    if any(not (it.get("greeting") or {}).get("ok") for it in items):
        n_bad = sum(1 for it in items if not (it.get("greeting") or {}).get("ok"))
        print(f"注意：有 {n_bad} 条没过质量门，上面标了 ⚠，建议别勾它们。")


def ask(items, cfg, cap):
    """返回最终要发的条目列表"""
    checked = [i for i, it in enumerate(items, 1) if default_checked(it, cfg)]
    default_ids = [i for i in checked][:cap]

    while True:
        try:
            ans = input(f"输入 all / 编号(如 1,3,5) / n 后回车　[默认发勾选的 {len(default_ids)} 条]：").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return []
        if ans == "":
            return [items[i - 1] for i in default_ids]
        low = ans.lower()
        if low in ("n", "no", "none", "0"):
            return []
        if low == "all":
            return [it for it in items if (it.get("greeting") or {}).get("ok")][:cap]
        try:
            ids = [int(x) for x in re_split(ans)]
        except ValueError:
            print("看不懂，重来。可以输 all、编号（逗号隔开）、或者 n。")
            continue
        bad = [i for i in ids if i < 1 or i > len(items)]
        if bad:
            print(f"编号越界：{bad}")
            continue
        return [items[i - 1] for i in ids][:cap]


def re_split(s):
    import re as _re
    return [x for x in _re.split(r"[,\s，、]+", s) if x]


CSS = """
body{font-family:"Microsoft YaHei",system-ui,sans-serif;max-width:900px;margin:24px auto;
     padding:0 16px;line-height:1.7;color:#222;background:#fff}
h1{font-size:20px;font-weight:500;margin:0 0 4px}
.sub{color:#666;font-size:13px;margin-bottom:20px}
.card{border:1px solid #e3e3e3;border-radius:10px;padding:14px 16px;margin-bottom:14px;background:#fafafa}
.card.on{border-color:#7CB342;background:#f6fbef}
.meta{font-size:13px;color:#555;margin-bottom:8px}
.score{font-weight:500;color:#333}
.msg{background:#fff;border:1px solid #eee;border-radius:6px;padding:10px 12px;
     font-size:14px;margin-top:8px;white-space:pre-wrap}
.dims{font-size:12px;color:#777}
.warn{color:#B35C1E;font-size:12px;margin-top:6px}
label{font-size:13px;color:#444}
.copy{margin-top:8px;padding:4px 12px;font-size:12px;border:1px solid #ccc;
      border-radius:5px;background:#fff;cursor:pointer;color:#444}
.copy:hover{border-color:#7CB342;color:#4b7a1f}
"""


def render_html(items, cfg, out_path):
    strong = cfg["scoring"]["thresholds"]["strong"]
    n_checked = sum(1 for it in items if default_checked(it, cfg))
    parts = [
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>",
        f"<title>今日候选 {len(items)} 条</title><style>{CSS}</style></head><body>",
        f"<h1>今日候选 {len(items)} 条</h1>",
        f"<div class='sub'>绿色框 = 机器勾选的建议发，共 {n_checked} 条。"
        f"阈值：{strong} 分以上默认勾选。</div>",
    ]
    for i, it in enumerate(items, 1):
        on = default_checked(it, cfg)
        g = it.get("greeting") or {}
        det = it["detail"]
        pairs = "　".join(
            f"{v['name']} {v['score']}" for v in det.values()
            if isinstance(v, dict) and "score" in v
        )
        parts.append(f"<div class='card{' on' if on else ''}'>")
        parts.append(f"<div class='meta'><b>[{i}]</b> <span class='score'>{it['score']} 分</span>"
                     f"　{it['title']}　{it['company']}　{it['city']}</div>")
        parts.append(f"<div class='dims'>{pairs}</div>")
        ev = []
        for v in det.values():
            if isinstance(v, dict) and v.get("evidence"):
                for e in v["evidence"]:
                    ev.append(f"{v['name']}：{e['sentence']}")
        if ev:
            parts.append(f"<div class='dims'>证据：{'；'.join(ev[:3])}</div>")
        if g.get("text"):
            parts.append(f"<div class='msg'>{g['text']}</div>")
            parts.append(f"<div class='dims'>{g.get('chars')} 字　相似度 {g.get('similarity')}　"
                         f"结尾变体 {g.get('closing_id')}</div>")
            parts.append(f"<button class='copy' onclick='cp(this)'>复制这条招呼语</button>")
        risks = g.get("risks") or []
        if risks:
            parts.append(f"<div class='warn'>⚠ 风险&注意：{'；'.join(risks)}</div>")
        kws = g.get("explicit_keywords") or []
        if kws:
            parts.append(f"<div class='dims'>JD 命中词：{'、'.join(kws[:10])}</div>")
        if not g.get("ok"):
            parts.append(f"<div class='warn'>质量门未通过：{'; '.join(g.get('reasons', []))}</div>")
        parts.append("</div>")
    parts.append("</body></html>")

    # 复制按钮：点一下把上面那条招呼语放进剪贴板，省得在框里拖选
    script = """
<script>
function cp(btn){
  let el = btn.previousElementSibling;
  while(el && el.className.indexOf('msg') === -1){ el = el.previousElementSibling; }
  if(!el) return;
  const txt = el.innerText;
  const done = () => { btn.textContent = '已复制 ✓';
                       setTimeout(()=>{ btn.textContent = '复制这条招呼语'; }, 1500); };
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(txt).then(done);
  } else {
    const ta = document.createElement('textarea');
    ta.value = txt; ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.appendChild(ta); ta.select();
    try { document.execCommand('copy'); done(); } catch(e) { btn.textContent = '复制失败，手动选'; }
    document.body.removeChild(ta);
  }
}
</script>"""
    parts.append(script)

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("".join(parts))
    return out_path
