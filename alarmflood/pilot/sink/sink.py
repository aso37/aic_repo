# -*- coding: utf-8 -*-
"""
告警接收端（模拟企业 ITSM/工单系统的入口）
接收 Alertmanager webhook，逐条落盘为 JSONL —— 这就是"真实告警流"的证据文件。
只依赖标准库，无需 Flask。
"""
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

OUT = Path("/data/alerts.jsonl")
PORT = 9099


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")
        try:
            payload = json.loads(body)
        except Exception as e:
            print(f"[sink] bad payload: {e}", flush=True)
            return

        recv_ms = int(time.time() * 1000)
        n = 0
        OUT.parent.mkdir(parents=True, exist_ok=True)
        with OUT.open("a", encoding="utf-8") as f:
            for a in payload.get("alerts", []):
                f.write(json.dumps({
                    "recv_ms": recv_ms,
                    "status": a.get("status"),
                    "labels": a.get("labels", {}),
                    "annotations": a.get("annotations", {}),
                    "startsAt": a.get("startsAt"),
                    "endsAt": a.get("endsAt"),
                }, ensure_ascii=False) + "\n")
                n += 1
        if n:
            firing = [a for a in payload.get("alerts", []) if a.get("status") == "firing"]
            print(f"[sink] +{n} alerts (firing={len(firing)}) -> {OUT}", flush=True)

    def log_message(self, *args):  # 静音默认访问日志
        pass


if __name__ == "__main__":
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.touch(exist_ok=True)
    print(f"[sink] listening on 0.0.0.0:{PORT}, writing {OUT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
