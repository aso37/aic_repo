# -*- coding: utf-8 -*-
"""网络故障信号探查：哪个指标能识别网络型故障？

这是本次规则校准的**关键证据脚本**（对应 results/experiments.md 的「已结案」块）：
  A) FIN-WAIT / CLOSE-WAIT 的整体非零率（即误报率）
  B) 按实体拆分，看哪些实体会出现 FIN-WAIT > 0
  C) 若加规则「FIN-WAIT > 0」，能为多少故障窗提供证据（并对照其他故障类型）

**结论**：TCP-FIN-WAIT 的非零样本仅占 1.18%，却在 **71%** 的网络故障窗内出现，
而对 CPU/DISK/MEMORY/JVM 命中 **0%** —— 故用它替换原 `ens160 NETBandwidthUtil`。

用法（在 alarmflood/ 目录下）
    $env:AIOPS_RAW = "<AIOps2021 数据集目录：其下应有 aiops21_groundtruth.csv 与 aiops2021-2/>"
    .venv\\Scripts\\python.exe scripts\\probe_net_signals.py
"""
import os
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).parents[1]
RAW = Path(os.environ.get("AIOPS_RAW", "E:/aic_data/AIOps2021")) / "aiops2021-2"
R = ROOT
FIN = "OSLinux-OSLinux_NETWORK_NETWORK_TCP-FIN-WAIT"
CW = "OSLinux-OSLinux_NETWORK_NETWORK_TCP-CLOSE-WAIT"

print("=" * 88)
print("A) 整体误报率：FIN-WAIT / CLOSE-WAIT 非零的样本占比（10 天全量）")
print("=" * 88)
days = ["0304","0305","0306","0307","0309","0310","0312","0323","0324","0325"]
tot = fn = cw = 0
for d in days:
    f = RAW / d / "metric" / f"metric_{d}.csv"
    if not f.exists():
        continue
    m = pd.read_csv(f, usecols=["cmdb_id","kpi_name","value"])
    for kpi, cnt_name in ((FIN, "fin"), (CW, "cw")):
        s = m.loc[m.kpi_name == kpi, "value"]
        if s.empty:
            continue
        tot += len(s) if cnt_name == "fin" else 0
        if cnt_name == "fin":
            fn += int((s > 0).sum())
        else:
            cw += int((s > 0).sum())
print(f"FIN-WAIT  非零样本 {fn:,} / {tot:,} = {fn/max(tot,1)*100:.4f}%")
print(f"CLOSE-WAIT 非零样本 {cw:,} / {tot:,} = {cw/max(tot,1)*100:.4f}%")

print()
print("=" * 88)
print("B) 按实体拆分（0304）：哪些实体会出现 FIN-WAIT>0")
print("=" * 88)
m = pd.read_csv(RAW / "0304" / "metric" / "metric_0304.csv", usecols=["cmdb_id","kpi_name","value"])
s = m[m.kpi_name == FIN]
g = s.groupby("cmdb_id")["value"].agg(n="size", nonzero=lambda x: int((x > 0).sum()), mx="max")
g["rate%"] = (g.nonzero / g.n * 100).round(3)
print(g[g.nonzero > 0].sort_values("nonzero", ascending=False).to_string())
print(f"\n（全表共 {len(g)} 个实体有该指标，其中 {(g.nonzero>0).sum()} 个出现过非零）")

print()
print("=" * 88)
print("C) 若加规则『FIN-WAIT > 0』，会为多少故障窗提供证据？（对照：现有规则的覆盖率）")
print("=" * 88)
win = pd.read_parquet(R / "results" / "fault_windows.parquet")
train = win[win.data_type == "train"]
from collections import Counter
res = Counter(); tot_by_type = Counter()
for _, w in train.iterrows():
    d = pd.to_datetime(w.st_ms, unit="ms").strftime("%m%d")
    f = RAW / d / "metric" / f"metric_{d}.csv"
    if not f.exists():
        continue
    mm = pd.read_csv(f, usecols=["timestamp","cmdb_id","kpi_name","value"])
    sub = mm[(mm.cmdb_id == w.service) & (mm.kpi_name == FIN)]
    if sub.empty:
        continue
    ts = sub["timestamp"] * 1000
    inw = sub[(ts >= w.st_ms) & (ts <= w.ed_ms) & (sub.value > 0)]
    t = w.anomaly_type
    tot_by_type[t] += 1
    if len(inw) > 0:
        res[t] += 1
print(f"{'类型':<14} {'有 FIN-WAIT>0 证据':>18} / {'窗口数':>6}   命中率")
for t in sorted(tot_by_type, key=lambda x: -tot_by_type[x]):
    print(f"{t:<14} {res.get(t,0):>18} / {tot_by_type[t]:>6}   {res.get(t,0)/tot_by_type[t]*100:.0f}%")
