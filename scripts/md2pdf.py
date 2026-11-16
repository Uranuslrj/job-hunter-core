# -*- coding: utf-8 -*-
"""把 resumes/final/ 下的简历 markdown 转成 PDF。
用 Edge headless 打印，不依赖任何第三方库，中文排版稳定。
"""
import os
import re
import subprocess
import html as htmllib

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(BASE, "resumes", "final")
OUT = os.path.join(BASE, "resumes", "final_pdf")
EDGE = r"C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"


def md_to_html(md: str) -> str:
    """轻量 markdown -> html。只处理简历里出现的结构：
    一级标题(#)、二级标题(##)、三级标题(###)、普通段落、列表项(-)、加粗(**x**)。
    """
    lines = md.split("\n")
    out = []
    in_list = False
    first = True

    def close_list():
        nonlocal in_list
        if in_list:
            out.append("</ul>")
            in_list = False

    for raw in lines:
        line = raw.rstrip()

        # 空行：结束列表
        if not line.strip():
            close_list()
            out.append("")
            continue

        # 内联：加粗
        def inline(s):
            s = htmllib.escape(s)
            s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
            return s

        # 标题
        if line.startswith("### "):
            close_list()
            out.append(f'<h3>{inline(line[4:])}</h3>')
            continue
        if line.startswith("## "):
            close_list()
            out.append(f'<h2>{inline(line[3:])}</h2>')
            continue
        if line.startswith("# "):
            close_list()
            out.append(f'<h1>{inline(line[2:])}</h1>')
            continue

        # 列表项
        m = re.match(r"^\s*[-*]\s+(.*)$", line)
        if m:
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{inline(m.group(1))}</li>")
            continue

        # 普通段落
        close_list()
        out.append(f"<p>{inline(line)}</p>")

    close_list()
    return "\n".join(out)


CSS = """
@page { size: A4; margin: 12mm 16mm; }
* { box-sizing: border-box; }
body {
    font-family: "Microsoft YaHei", "PingFang SC", "Noto Sans CJK SC", sans-serif;
    color: #1a1a1a;
    font-size: 12.5px;
    line-height: 1.45;
    margin: 0;
}
h1 {
    font-size: 22px;
    margin: 0 0 2px;
    text-align: center;
    letter-spacing: 1px;
}
h2 {
    font-size: 14px;
    margin: 14px 0 6px;
    padding-bottom: 3px;
    border-bottom: 1.5px solid #333;
    letter-spacing: 1px;
}
h3 {
    font-size: 13px;
    margin: 10px 0 4px;
    font-weight: 600;
}
p { margin: 3px 0; }
ul { margin: 3px 0 3px 0; padding-left: 18px; }
li { margin: 2px 0; }
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
    print(f"共 {len(files)} 份简历，开始转换...\n")

    for f in files:
        md_path = os.path.join(SRC, f)
        with open(md_path, encoding="utf-8") as fp:
            md = fp.read()
        body = md_to_html(md)
        html_path = os.path.join(OUT, f.replace(".md", ".html"))
        pdf_path = os.path.join(OUT, f.replace(".md", ".pdf"))
        with open(html_path, "w", encoding="utf-8") as fp:
            fp.write(wrap(body))

        # Edge headless 打印 PDF
        html_uri = "file:///" + html_path.replace("\\", "/")
        cmd = [
            EDGE,
            "--headless=new",
            "--disable-gpu",
            "--no-pdf-header-footer",
            "--print-to-pdf=" + pdf_path.replace("\\", "/"),
            html_uri,
        ]
        r = subprocess.run(cmd, capture_output=True, timeout=60)
        ok = os.path.exists(pdf_path) and os.path.getsize(pdf_path) > 0
        status = "OK" if ok else "FAIL"
        size = os.path.getsize(pdf_path) if ok else 0
        print(f"  [{status}] {f}  ->  {size} 字节")

    print("\n转换完成。输出目录：", OUT)


if __name__ == "__main__":
    main()
