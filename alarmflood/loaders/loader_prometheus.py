# -*- coding: utf-8 -*-
"""
loader_prometheus.py —— L2 自建试点（云服务器 Docker 监控栈）告警接入

这是"新数据源 = 一个新 loader，下游零改动"这条架构承诺的现场证据：
Alertmanager webhook 落盘的 JSONL → 统一五列 alarm.parquet，
M1/M2/M3/M5 全部无需改代码即可运行。

输入：data/pilot/alerts.jsonl   （从云服务器 scp 回来）
输出：data/alarm_pilot.parquet  （统一五列）
       data/fault_windows_pilot.parquet （真值：注入的故障窗口）
"""
import json
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).parents[1]
SRC = ROOT / "data" / "pilot" / "alerts.jsonl"
GT = ROOT / "data" / "pilot" / "injections.json"
OUT_ALARM = ROOT / "data" / "alarm_pilot.parquet"
OUT_FAULT = ROOT / "results" / "fault_windows_pilot.parquet"


def load_alerts() -> pd.DataFrame:
    rows = []
    for line in SRC.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        a = json.loads(line)
        lab = a.get("labels", {})
        ann = a.get("annotations", {})
        entity = lab.get("svc") or lab.get("instance") or "unknown"
        rows.append({
            "timestamp": a["recv_ms"],          # 通知到达时间（体现重复报警）
            "entity": entity,
            "alarm_id": lab.get("alertname", "UNKNOWN"),
            "content": ann.get("summary", ""),
            "severity": int(lab.get("severity", 3)),
            "status": a.get("status"),
            "startsAt": a.get("startsAt"),
        })
    df = pd.DataFrame(rows).sort_values("timestamp").reset_index(drop=True)
    OUT_ALARM.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(OUT_ALARM, index=False)
    firing = df[df.status == "firing"]
    print(f"[loader-pilot] {len(df)} 条告警记录（firing={len(firing)}）"
          f" / {df.entity.nunique()} 个实体 / {df.alarm_id.nunique()} 条规则触发")
    print(f"[loader-pilot] 时间跨度 {(df.timestamp.max()-df.timestamp.min())/60000:.1f} 分钟"
          f" → {OUT_ALARM}")
    return df


def load_groundtruth() -> pd.DataFrame:
    gt = json.loads(GT.read_text(encoding="utf-8"))
    df = pd.DataFrame([{
        "id": 1,
        "service": gt["root_entity"],
        "anomaly_type": gt["anomaly_type"],
        "st_ms": gt["st_ms"],
        "ed_ms": gt["ed_ms"],
        "data_type": "train",
    }])
    OUT_FAULT.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(OUT_FAULT, index=False)
    print(f"[loader-pilot] 真值故障窗口：root={gt['root_entity']} "
          f"type={gt['anomaly_type']} → {OUT_FAULT}")
    return df


if __name__ == "__main__":
    load_alerts()
    load_groundtruth()
