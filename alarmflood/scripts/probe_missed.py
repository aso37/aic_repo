# -*- coding: utf-8 -*-
"""⚠️ 已废弃（2026-10-14）—— 请勿运行，改用下面两个脚本

本脚本存在**时区 bug**：第 21–24 行用了 `int(w.st.timestamp())`。
`w.st` 是 naive datetime，`.timestamp()` 会按本机时区（UTC+8）转换，
导致查询窗口偏离真实故障窗 **8 小时**，结论不可信。

保留本文件仅用于记录排查过程。替代品：

    probe_missed2.py    —— 修正版（全程用 st_ms/ed_ms），每类型取 missed[0] 做示例
    probe_missed_all.py —— 全样本统计版，能支撑"该类型无可用信号"这类全覆盖结论

原说明：通用探针：对每个故障类型，取漏报窗口，对比根因实体全指标 窗口均值 vs 基线均值
"""
import pandas as pd

win = pd.read_parquet("results/fault_windows.parquet")
alarms = pd.read_parquet("data/alarm.parquet")
alarms["ts"] = pd.to_datetime(alarms.timestamp, unit="ms")

for ftype in ["CPU", "DISK", "NETWORK"]:
    grp = win[(win.data_type == "train") & (win.anomaly_type == ftype)]
    missed = [w for _, w in grp.iterrows()
              if len(alarms[(alarms.ts >= w.st) & (alarms.ts <= w.ed)
                            & (alarms.entity == w.service)]) == 0]
    if not missed:
        continue
    w = missed[0]
    day = w.st.strftime("%m%d")
    f = f"E:/aic_data/AIOps2021/aiops2021-2/{day}/metric/metric_{day}.csv"
    m = pd.read_csv(f)
    sub = m[m.cmdb_id == w.service]
    winm = sub[(sub.timestamp >= int(w.st.timestamp()))
               & (sub.timestamp <= int(w.ed.timestamp()))]
    base = sub[(sub.timestamp >= int(w.st.timestamp()) - 7200)
               & (sub.timestamp < int(w.st.timestamp()))]
    a = winm.groupby("kpi_name")["value"].mean()
    b = base.groupby("kpi_name")["value"].mean()
    cmp = pd.DataFrame({"win": a, "base": b}).dropna()
    cmp["ratio"] = (cmp.win / cmp.base.replace(0, float("nan"))).round(2)
    # 显著变化：比值>=2 或 <=0.5，或窗口内绝对值很大
    sig = cmp[(cmp.ratio >= 2) | (cmp.ratio <= 0.5) | (cmp.win >= 90)]
    print(f"\n===== {ftype} missed window: {w.service} {w.st} =====")
    print(sig.sort_values("ratio", ascending=False).head(12).to_string())
