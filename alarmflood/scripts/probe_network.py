# -*- coding: utf-8 -*-
"""临时诊断：找出未被覆盖的 NETWORK 故障窗口，查看根因实体当时的网络指标"""
import pandas as pd

win = pd.read_parquet("results/fault_windows.parquet")
alarms = pd.read_parquet("data/alarm.parquet")
alarms["ts"] = pd.to_datetime(alarms.timestamp, unit="ms")

net = win[(win.data_type == "train") & (win.anomaly_type == "NETWORK")]
missed = [w for _, w in net.iterrows()
          if len(alarms[(alarms.ts >= w.st) & (alarms.ts <= w.ed)
                        & (alarms.entity == w.service)]) == 0]
print(f"missed NETWORK windows: {len(missed)}/{len(net)}")

from collections import Counter
print("missed entities:", Counter(w.service for w in missed))

w = missed[0]
print("probe window:", w.service, w.st, "->", w.ed)
day = w.st.strftime("%m%d")
f = f"E:/aic_data/AIOps2021/aiops2021-2/{day}/metric/metric_{day}.csv"
m = pd.read_csv(f)
sub = m[(m.cmdb_id == w.service)
        & (m.timestamp >= int(w.st.timestamp()))
        & (m.timestamp <= int(w.ed.timestamp()))]
netk = [k for k in sub.kpi_name.unique() if "NET" in k.upper() or "TCP" in k.upper()]
piv = sub[sub.kpi_name.isin(netk)].pivot_table(index="timestamp", columns="kpi_name", values="value")
print(piv.describe().loc[["min", "mean", "max"]].T.to_string())

base = m[(m.cmdb_id == w.service) & (m.timestamp < int(w.st.timestamp()) - 3600)]
print("--- baseline (1h before, mean) ---")
print(base[base.kpi_name.isin(netk)].groupby("kpi_name")["value"].mean().to_string())
