# -*- coding: utf-8 -*-
"""诊断2：故障窗口内根因实体的 metric 是否出现数据断流（心跳丢失）"""
import pandas as pd

day = "0304"
f = f"E:/aic_data/AIOps2021/aiops2021-2/{day}/metric/metric_{day}.csv"
m = pd.read_csv(f, usecols=["timestamp", "cmdb_id", "kpi_name"])
svc = "Tomcat02"
sub = m[m.cmdb_id == svc].sort_values("timestamp")
ts = sorted(sub.timestamp.unique())
print(f"{svc} rows={len(sub)}, time range {ts[0]}~{ts[-1]}, "
      f"unique ts={len(ts)}")

# 19:22:00 = 1614854520 附近是否有数据
import datetime
w_st, w_ed = 1614854520, 1614854820
inwin = sub[(sub.timestamp >= w_st) & (sub.timestamp <= w_ed)]
print(f"rows in fault window 19:22~19:27: {len(inwin)}")
before = sub[(sub.timestamp >= w_st - 1800) & (sub.timestamp < w_st)]
print(f"rows in 30min before:  {len(before)}")

# 采样间隔分布
diffs = pd.Series(ts).diff().dropna()
print("sampling interval(s) value_counts:")
print(diffs.value_counts().head(10).to_string())
