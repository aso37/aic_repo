# -*- coding: utf-8 -*-
"""修正版探针：用 st_ms/ed_ms 直接对比，分析漏报窗口的指标信号"""
import pandas as pd

win = pd.read_parquet("results/fault_windows.parquet")
alarms = pd.read_parquet("data/alarm.parquet")

# 1) 分类型覆盖率
for t, grp in win[win.data_type == "train"].groupby("anomaly_type"):
    hit = sum(
        len(alarms[(alarms.timestamp >= w.st_ms) & (alarms.timestamp <= w.ed_ms)
                   & (alarms.entity == w.service)]) > 0
        for _, w in grp.iterrows())
    print(f"{t!r:20s} coverage {hit}/{len(grp)}")

# 2) 每个类型取漏报窗口，对比根因实体 窗口 vs 基线(2h) 指标
for ftype in ["CPU", "DISK", "NETWORK", "MEMORY"]:
    grp = win[(win.data_type == "train") & (win.anomaly_type == ftype)]
    missed = [w for _, w in grp.iterrows()
              if len(alarms[(alarms.timestamp >= w.st_ms) & (alarms.timestamp <= w.ed_ms)
                            & (alarms.entity == w.service)]) == 0]
    if not missed:
        continue
    w = missed[0]
    day = pd.to_datetime(w.st_ms, unit="ms").strftime("%m%d")
    f = f"E:/aic_data/AIOps2021/aiops2021-2/{day}/metric/metric_{day}.csv"
    if not pd.io.common.file_exists(f):
        print(f"\n===== {ftype} missed: {w.service} @ day {day} — 数据文件不存在，跳过 =====")
        continue
    m = pd.read_csv(f)
    sub = m[m.cmdb_id == w.service]
    winm = sub[(sub.timestamp * 1000 >= w.st_ms) & (sub.timestamp * 1000 <= w.ed_ms)]
    base = sub[(sub.timestamp * 1000 >= w.st_ms - 7200 * 1000)
               & (sub.timestamp * 1000 < w.st_ms)]
    a = winm.groupby("kpi_name")["value"].mean()
    b = base.groupby("kpi_name")["value"].mean()
    cmp = pd.DataFrame({"win": a, "base": b}).dropna()
    cmp["ratio"] = (cmp.win / cmp.base.replace(0, float("nan"))).round(2)
    sig = cmp[(cmp.ratio >= 2) | (cmp.ratio <= 0.5) | (cmp.win >= 90)]
    print(f"\n===== {ftype} missed: {w.service} "
          f"{pd.to_datetime(w.st_ms, unit='ms')} rows_in_win={len(winm)} =====")
    print(sig.sort_values("ratio", ascending=False).head(12).to_string())
