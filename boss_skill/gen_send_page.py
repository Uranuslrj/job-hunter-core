# -*- coding: utf-8 -*-
"""把 list_greetings.json 渲染成手动投递网页。"""
import json
import os

BASE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.normpath(os.path.join(BASE, "..", "data", "list_greetings.json"))
OUT = os.path.normpath(os.path.join(BASE, "..", "manual_send.html"))

with open(SRC, encoding="utf-8") as f:
    jobs = json.load(f)

GROUP_LABEL = {
    "project_rag": "剧本杀·RAG检索",
    "project_dm_match": "剧本杀·DM匹配",
    "project_review_loop": "剧本杀·反馈闭环",
    "ai_pm": "AI产品经理",
    "ai_ops": "AI产品运营",
    "solution": "解决方案/售前",
    "data": "数据",
    "fallback": "其他",
}

def esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

cards = []
for x in jobs:
    url_full = "https://www.zhipin.com" + (x.get("url") or "")
    g = x.get("anchor_group") or "fallback"
    label = GROUP_LABEL.get(g, "其他")
    salary = x.get("salary") or "面议"
    city = x.get("city") or ""
    boss = x.get("boss_name") or ""
    boss_title = x.get("boss_title") or ""
    greeting = x.get("greeting") or ""
    title = x.get("title") or ""
    company = x.get("company") or ""

    cards.append(f'''
    <div class="card" data-group="{esc(g)}">
      <div class="card-head">
        <a class="job-title" href="{esc(url_full)}" target="_blank" rel="noopener">{esc(title)}</a>
        <span class="tag">{esc(label)}</span>
      </div>
      <div class="meta">
        <span class="company">{esc(company)}</span>
        <span class="sep">·</span>
        <span class="city">{esc(city)}</span>
        <span class="sep">·</span>
        <span class="salary">{esc(salary)}</span>
        <span class="sep">·</span>
        <span class="boss">{esc(boss)}{("（" + esc(boss_title) + "）") if boss_title else ""}</span>
      </div>
      <div class="greeting-box">
        <div class="greeting-text">{esc(greeting)}</div>
        <button class="copy-btn" data-text="{esc(greeting)}">复制</button>
      </div>
    </div>''')

html = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>BOSS 手动投递助手</title>
<style>
  :root {{
    --bg: #1e1f22;
    --card: #2a2b30;
    --text: #e6e6e6;
    --muted: #9a9ba0;
    --accent: #4a90d9;
    --accent-2: #3a7cc2;
    --border: #3a3b40;
    --tag: #3a4a5a;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, "PingFang SC", "Microsoft YaHei", "Segoe UI", sans-serif;
    padding: 24px;
    line-height: 1.6;
  }}
  .header {{
    max-width: 960px;
    margin: 0 auto 24px;
  }}
  .header h1 {{
    font-size: 22px;
    font-weight: 600;
    margin-bottom: 6px;
  }}
  .header p {{
    color: var(--muted);
    font-size: 14px;
  }}
  .filters {{
    max-width: 960px;
    margin: 0 auto 20px;
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }}
  .filter-btn {{
    background: var(--card);
    color: var(--text);
    border: 1px solid var(--border);
    padding: 7px 14px;
    border-radius: 18px;
    cursor: pointer;
    font-size: 13px;
    transition: all .15s;
  }}
  .filter-btn:hover {{ border-color: var(--accent); }}
  .filter-btn.active {{
    background: var(--accent);
    border-color: var(--accent);
    color: #fff;
  }}
  .container {{
    max-width: 960px;
    margin: 0 auto;
    display: flex;
    flex-direction: column;
    gap: 14px;
  }}
  .card {{
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 16px 18px;
  }}
  .card-head {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 12px;
    margin-bottom: 6px;
  }}
  .job-title {{
    color: var(--text);
    font-size: 16px;
    font-weight: 600;
    text-decoration: none;
  }}
  .job-title:hover {{ color: var(--accent); text-decoration: underline; }}
  .tag {{
    flex-shrink: 0;
    background: var(--tag);
    color: var(--text);
    font-size: 12px;
    padding: 2px 10px;
    border-radius: 12px;
    white-space: nowrap;
  }}
  .meta {{
    color: var(--muted);
    font-size: 13px;
    margin-bottom: 12px;
  }}
  .meta .sep {{ margin: 0 4px; opacity: .5; }}
  .meta .salary {{ color: #e0a458; }}
  .greeting-box {{
    position: relative;
    background: var(--bg);
    border-radius: 8px;
    padding: 12px 14px;
  }}
  .greeting-text {{
    font-size: 14px;
    color: var(--text);
    white-space: pre-wrap;
    word-break: break-all;
    padding-right: 60px;
  }}
  .copy-btn {{
    position: absolute;
    top: 8px;
    right: 8px;
    background: var(--accent);
    color: #fff;
    border: none;
    padding: 5px 14px;
    border-radius: 6px;
    cursor: pointer;
    font-size: 13px;
    transition: background .15s;
  }}
  .copy-btn:hover {{ background: var(--accent-2); }}
  .copy-btn.copied {{ background: #4caf6d; }}
  .empty {{ color: var(--muted); text-align: center; padding: 40px; }}
</style>
</head>
<body>
  <div class="header">
    <h1>BOSS 手动投递助手</h1>
    <p>共 {len(jobs)} 个岗位 · 点岗位名跳转 BOSS 详情页 · 点「复制」拿招呼语 · 手动粘贴发送</p>
  </div>
  <div class="filters">
    <button class="filter-btn active" data-filter="all">全部 ({len(jobs)})</button>
    <button class="filter-btn" data-filter="project_rag">剧本杀·RAG检索</button>
    <button class="filter-btn" data-filter="project_dm_match">剧本杀·DM匹配</button>
    <button class="filter-btn" data-filter="project_review_loop">剧本杀·反馈闭环</button>
  </div>
  <div class="container">
    {''.join(cards)}
  </div>
  <script>
    // 分组筛选
    const btns = document.querySelectorAll('.filter-btn');
    btns.forEach(b => {{
      b.addEventListener('click', () => {{
        btns.forEach(x => x.classList.remove('active'));
        b.classList.add('active');
        const f = b.dataset.filter;
        document.querySelectorAll('.card').forEach(c => {{
          c.style.display = (f === 'all' || c.dataset.group === f) ? '' : 'none';
        }});
      }});
    }});
    // 复制
    document.querySelectorAll('.copy-btn').forEach(btn => {{
      btn.addEventListener('click', async () => {{
        const text = btn.dataset.text;
        try {{
          await navigator.clipboard.writeText(text);
        }} catch (e) {{
          const ta = document.createElement('textarea');
          ta.value = text;
          document.body.appendChild(ta);
          ta.select();
          document.execCommand('copy');
          document.body.removeChild(ta);
        }}
        btn.textContent = '已复制';
        btn.classList.add('copied');
        setTimeout(() => {{
          btn.textContent = '复制';
          btn.classList.remove('copied');
        }}, 1200);
      }});
    }});
  </script>
</body>
</html>'''

with open(OUT, "w", encoding="utf-8") as f:
    f.write(html)

print(f"网页已生成：{OUT}")
print(f"岗位数：{len(jobs)}")
