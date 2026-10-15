# -*- coding: utf-8 -*-
"""诊断：未被覆盖的 NETWORK 故障窗口 —— 查看根因实体当时的网络指标。

⚠️ 时间基准（重要，本脚本 2026-10-14 修正过一次 bug）
--------------------------------------------------------------------
- `fault_windows.parquet` 的 `st_ms` / `ed_ms` 是以数据自带 time 列为锚点推算的
  epoch（毫秒），这是**唯一可信的时间基准**。
- **严禁使用 `w.st.timestamp()`**：`st` 是 naive datetime，`.timestamp()` 会按
  本机时区（UTC+8）转换，实测与数据时钟差 8 小时 —— 原版脚本就是踩了这个坑，
  等于去查了一段正常时段的指标，从而得出"网络指标无指纹"的错误结论。
- metric CSV 的 `timestamp` 是同一数据时钟的 epoch **秒**。
- 本脚本的 datetime 比较与 epoch 查询**全部以 st_ms/ed_ms 为准**。

用法（在 alarmflood/ 目录下）：
    .venv\\Scripts\\python.exe scripts\\probe_network.py
"""
from collections import Counter
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).parents[1]
RAW_ROOT = Path("E:/aic_data/AIOps2021/aiops2021-2")

win = pd.read_parquet(ROOT / "results" / "fault_windows.parquet")
alarms = pd.read_parquet(ROOT / "data" / "alarm.parquet")

# ---- 1) 找出窗内根因实体完全没有告警的 NETWORK 故障 ----
net = win[(win.data_type == "train") & (win.anomaly_type == "NETWORK")]
missed = []
for _, w in net.iterrows():
    hit = alarms[(alarms.timestamp >= w.st_ms) & (alarms.timestamp <= w.ed_ms)
                 & (alarms.entity == w.service)]
    if len(hit) == 0:
        missed.append(w)

print(f"missed NETWORK windows: {len(missed)}/{len(net)}")
print("missed entities:", Counter(w.service for w in missed))

if not missed:
    raise SystemExit("没有未覆盖的 NETWORK 窗口，无需进一步诊断")

# ---- 2) 取第一个，查它的网络指标 ----
w = missed[0]
day = pd.to_datetime(w.st_ms, unit="ms").strftime("%m%d")
st_s, ed_s = int(w.st_ms) // 1000, int(w.ed_ms) // 1000
print(f"\nprobe window: {w.service}  {w.st} -> {w.ed}")
print(f"  数据时钟 day={day}  查询区间 epoch 秒 = [{st_s}, {ed_s}]")

f = RAW_ROOT / day / "metric" / f"metric_{day}.csv"
m = pd.read_csv(f)
print(f"  metric 文件: {f}  共 {len(m):,} 行, {m.kpi_name.nunique()} 个 KPI")

sub = m[(m.cmdb_id == w.service) & (m.timestamp >= st_s) & (m.timestamp <= ed_s)]
netk = [k for k in sub.kpi_name.unique()
        if "NET" in k.upper() or "TCP" in k.upper()]
print(f"  窗内该实体的 NET/TCP 类 KPI ({len(netk)}): {netk}")

if netk:
    piv = sub[sub.kpi_name.isin(netk)].pivot_table(
        index="timestamp", columns="kpi_name", values="value")
    print("\n=== 窗口内取值分布 ===")
    print(piv.describe().loc[["min", "mean", "max"]].T.to_string())

    base = m[(m.cmdb_id == w.service) & (m.timestamp < st_s - 3600)]
    print("\n=== 基线（故障前 1 小时以上，均值）===")
    print(base[base.kpi_name.isin(netk)]
          .groupby("kpi_name")["value"].mean().to_string())

# ---- 3) 顺带看该实体全部 KPI 的量纲，便于判断阈值是否合理 ----
print("\n=== 该实体全部 KPI 的 min/mean/max（判断量纲用）===")
allsub = m[m.cmdb_id == w.service]
print(allsub.groupby("kpi_name")["value"]
      .agg(["min", "mean", "max"]).round(3).to_string())
