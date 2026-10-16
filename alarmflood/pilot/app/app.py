# -*- coding: utf-8 -*-
"""
试点微服务（真实运行，不是模拟器）

- ROLE=db ：提供 /work 做真实 CPU 计算；/debug/stress/cpu 可注入 CPU 饱和故障
- ROLE=api：后台持续调用下游 PEER(db) 的 /work；下游变慢 → 自身延迟/错误率上升
            （这就是"一处故障引发上下游连环告警"的真实因果链，非人为拼凑）

指标用 prometheus_client 暴露，由 Prometheus 真实抓取、真实触发告警规则。
"""
import os
import time
import threading

import psutil
import requests
from flask import Flask, jsonify, request
from prometheus_client import (CONTENT_TYPE_LATEST, Counter, Gauge, Histogram,
                               generate_latest)

SVC = os.getenv("SVC_NAME", "svc")
ROLE = os.getenv("ROLE", "db")
PEER = os.getenv("PEER", "")
PORT = 8000

app = Flask(__name__)

g_cpu = Gauge("svc_cpu_percent", "进程CPU使用率(相对单核，可>100)", ["svc", "role"])
g_mem_mb = Gauge("svc_memory_mb", "进程内存占用MB", ["svc", "role"])
g_mem_pct = Gauge("svc_memory_percent", "进程内存占系统百分比", ["svc", "role"])
g_up = Gauge("svc_up", "服务存活", ["svc", "role"])
g_stress = Gauge("svc_stress_active", "故障注入生效中", ["svc", "kind"])
c_req = Counter("svc_requests_total", "请求总数", ["svc", "role"])
c_err = Counter("svc_errors_total", "错误总数", ["svc", "role"])
h_lat = Histogram("svc_request_duration_seconds", "请求耗时", ["svc", "role"],
                  buckets=(0.01, 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10))

g_up.labels(SVC, ROLE).set(1)
_MEM_HOLD = []


# ---------- 指标自采集 ----------
def metric_updater():
    p = psutil.Process()
    p.cpu_percent(None)  # prime
    while True:
        g_cpu.labels(SVC, ROLE).set(p.cpu_percent(None))
        rss = p.memory_info().rss / 1024 / 1024
        g_mem_mb.labels(SVC, ROLE).set(rss)
        g_mem_pct.labels(SVC, ROLE).set(p.memory_percent())
        time.sleep(1)


# ---------- 业务逻辑 ----------
def _burn(seconds: float):
    end = time.time() + seconds
    while time.time() < end:
        pass


@app.route("/work")
def work():
    t0 = time.time()
    c_req.labels(SVC, ROLE).inc()
    try:
        if ROLE == "db":
            _burn(0.02)                       # 数据库侧真实计算 20ms
        else:
            if PEER:
                requests.get(PEER + "/work", timeout=2)
            _burn(0.005)
    except Exception:
        c_err.labels(SVC, ROLE).inc()
        h_lat.labels(SVC, ROLE).observe(time.time() - t0)
        return jsonify(svc=SVC, ok=False, err="downstream_fail"), 500
    h_lat.labels(SVC, ROLE).observe(time.time() - t0)
    return jsonify(svc=SVC, ok=True)


@app.route("/metrics")
def metrics():
    return generate_latest(), 200, {"Content-Type": CONTENT_TYPE_LATEST}


@app.route("/")
def root():
    return jsonify(svc=SVC, role=ROLE, peer=PEER, up=True)


# ---------- 故障注入端点 ----------
@app.route("/debug/stress/cpu")
def stress_cpu():
    """注入 CPU 饱和：起 N 个线程空转到 deadline"""
    threads = int(request.args.get("threads", 4))
    seconds = int(request.args.get("seconds", 180))

    def burn():
        g_stress.labels(SVC, "cpu").set(1)
        _burn(seconds)
        g_stress.labels(SVC, "cpu").set(0)

    for _ in range(threads):
        threading.Thread(target=burn, daemon=True).start()
    return jsonify(svc=SVC, injected="cpu", threads=threads, seconds=seconds)


@app.route("/debug/stress/mem")
def stress_mem():
    """注入内存占用：申请 MB 并持有到 deadline"""
    mb = int(request.args.get("mb", 500))
    seconds = int(request.args.get("seconds", 180))
    buf = bytearray(mb * 1024 * 1024)
    _MEM_HOLD.append(buf)
    g_stress.labels(SVC, "mem").set(1)

    def release():
        time.sleep(seconds)
        try:
            _MEM_HOLD.remove(buf)
        except ValueError:
            pass
        del buf
        g_stress.labels(SVC, "mem").set(0)

    threading.Thread(target=release, daemon=True).start()
    return jsonify(svc=SVC, injected="mem", mb=mb, seconds=seconds)


@app.route("/debug/stop")
def stop():
    """容器自杀：用于注入 SvcDown 类故障（可选剧本）"""
    def kill():
        time.sleep(1)
        os._exit(1)
    threading.Thread(target=kill, daemon=True).start()
    return jsonify(svc=SVC, stopping=True)


# ---------- 后台真实流量 ----------
def traffic():
    """每个容器持续请求自身 /work，使延迟指标是端到端真实测量"""
    url = f"http://127.0.0.1:{PORT}/work"
    while True:
        try:
            requests.get(url, timeout=5)
        except Exception:
            pass
        time.sleep(0.3)


if __name__ == "__main__":
    threading.Thread(target=metric_updater, daemon=True).start()
    threading.Thread(target=traffic, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT, threaded=True)
