# -*- coding: utf-8 -*-
"""把 resumes/还原版/ 的简历 md 转成「格式对齐原简历」的 PDF。
原简历排版参数（从用户提供的 PDF 提取）：
  - A4 单页，黑体 SimHei
  - 姓名 22pt / 职位 9pt / 栏目标题 10pt / 正文 7pt
  - 高密度，无边框分隔线，标题无下划线（原简历是纯文字线性排版）
本脚本用 Edge headless 打印，不依赖第三方库。
"""
import os
import re
import subprocess
import html as htmllib

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(BASE, "resumes", "还原版")
OUT = os.path.join(BASE, "resumes", "还原版_pdf")
EDGE = r"C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"


def md_to_html(md: str) -> str:
    """轻量 markdown -> html。保留原简历的线性排版：
    姓名(#)、职位、栏目标题、普通段落。加粗 -> strong。"""
    lines = md.split("\n")
    out = []
    first_line = True
    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            out.append("")
            continue

        def inline(s):
            s = htmllib.escape(s)
            s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
            return s

        # 第一行 = 姓名（最大标题）
        if first_line:
            out.append(f'<h1 class="name">{inline(line)}</h1>')
            first_line = False
            continue

        # 第二行 = 职位（次标题）
        if len(out) == 1 and not out[0] == "":
            pass

        out.append(f"<p>{inline(line)}</p>")

    return "\n".join(out)


CSS = """
@page { size: A4; margin: 10mm 12mm; }
* { box-sizing: border-box; }
body {
    font-family: "SimHei", "Microsoft YaHei", sans-serif;
    color: #000;
    font-size: 7pt;
    line-height: 1.28;
    margin: 0;
}
h1.name {
    font-size: 22pt;
    margin: 0 0 1pt;
    text-align: left;
    font-weight: bold;
    letter-spacing: 1pt;
}
p { margin: 0.6pt 0; }
"""


def wrap(html_body: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<style>{CSS}</style>
</head>
<body>
{html_body}
</body>
</html>"""


def main():
    os.makedirs(OUT, exist_ok=True)
    files = sorted(f for f in os.listdir(SRC) if f.endswith(".md"))
    print(f"共 {len(files)} 份，开始转换...\n")
    for f in files:
        md_path = os.path.join(SRC, f)
        with open(md_path, encoding="utf-8") as fp:
            md = fp.read()
        body = md_to_html(md)
        html_path = os.path.join(OUT, f.replace(".md", ".html"))
        pdf_path = os.path.join(OUT, f.replace(".md", ".pdf"))
        with open(html_path, "w", encoding="utf-8") as fp:
            fp.write(wrap(body))
        html_uri = "file:///" + html_path.replace("\\", "/")
        cmd = [
            EDGE, "--headless=new", "--disable-gpu",
            "--no-pdf-header-footer",
            "--print-to-pdf=" + pdf_path.replace("\\", "/"),
            html_uri,
        ]
        subprocess.run(cmd, capture_output=True, timeout=60)
        ok = os.path.exists(pdf_path) and os.path.getsize(pdf_path) > 0
        size = os.path.getsize(pdf_path) if ok else 0
        print(f"  [{'OK' if ok else 'FAIL'}] {f} -> {size} 字节")
    print("\n输出目录：", OUT)


if __name__ == "__main__":
    main()
