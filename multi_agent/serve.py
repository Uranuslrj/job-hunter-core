# -*- coding: utf-8 -*-
"""多 Agent 运行看板 · 轻量后端

作用：给 dashboard.html 提供两个接口，让浏览器能读到 runs/ 里的日志：
  GET /api/runs                    列出所有运行日志文件名
  GET /api/run?file=<name>         返回某个日志的所有事件（JSON 数组）
  GET /                            （可选）跳转看板首页

用法：
  python multi_agent/serve.py
  然后浏览器打开 http://127.0.0.1:8080/dashboard.html

只读，不改任何日志。零依赖（只用 Python 标准库）。
"""

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
RUNS_DIR = os.path.join(HERE, "runs")
PORT = 8080


def list_runs():
    if not os.path.isdir(RUNS_DIR):
        return []
    return sorted(f for f in os.listdir(RUNS_DIR) if f.endswith(".jsonl"))


def read_run(name):
    # 只允许读 .jsonl，防止路径穿越
    if "/" in name or "\\" in name or not name.endswith(".jsonl"):
        return None
    p = os.path.join(RUNS_DIR, name)
    if not os.path.exists(p):
        return None
    events = []
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except Exception:
                continue
    return events


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path == "/api/runs":
            self._send(200, json.dumps(list_runs(), ensure_ascii=False))
            return

        if path == "/api/run":
            name = (qs.get("file") or [""])[0]
            events = read_run(name)
            if events is None:
                self._send(404, json.dumps({"error": "not found"}, ensure_ascii=False))
                return
            self._send(200, json.dumps(events, ensure_ascii=False))
            return

        # 静态文件：dashboard.html
        if path in ("/", "/dashboard.html"):
            hp = os.path.join(HERE, "dashboard.html")
            if os.path.exists(hp):
                with open(hp, "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
                return

        self._send(404, json.dumps({"error": "not found"}, ensure_ascii=False))

    def log_message(self, *a):
        pass


def main():
    srv = HTTPServer(("127.0.0.1", PORT), Handler)
    print("多 Agent 看板已启动：http://127.0.0.1:%d/dashboard.html" % PORT)
    print("按 Ctrl+C 停止。")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")


if __name__ == "__main__":
    main()
