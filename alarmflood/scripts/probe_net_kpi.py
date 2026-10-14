# -*- coding: utf-8 -*-
"""诊断3：Tomcat02 在故障窗口内外的网络类指标对比，找可用的告警信号"""
import pandas as pd

day = "0304"
f = f"E:/aic_data/AIOps2021/aiops2021-2/{day}/metric/metric_{day}.csv"
m = pd.read_csv(f)
svc = "Tomcat02"
sub = m[m.cmdb_id == svc]
netk = [k for k in sub.kpi_name.unique() if "NET" in k.upper() or "TCP" in k.upper()]
print("net/tcp kpis:", netk)

w_st, w_ed = 1614854520, 1614854820
win = sub[(sub.timestamp >= w_st) & (sub.timestamp <= w_ed)]
base = sub[(sub.timestamp >= w_st - 7200) & (sub.timestamp < w_st)]

print("\n=== window mean vs baseline(2h) mean ===")
a = win[win.kpi_name.isin(netk)].groupby("kpi_name")["value"].mean()
b = base[base.kpi_name.isin(netk)].groupby("kpi_name")["value"].mean()
cmp = pd.DataFrame({"window": a, "base": b})
cmp["ratio"] = (cmp.window / cmp.base).round(2)
print(cmp.to_string())

# 也看看 CPU 等
osk = [k for k in sub.kpi_name.unique()
       if k.startswith("OSLinux-CPU") or "MEMUsed" in k or "DISKPercentBusy" in k]
a2 = win[win.kpi_name.isin(osk)].groupby("kpi_name")["value"].mean()
b2 = base[base.kpi_name.isin(osk)].groupby("kpi_name")["value"].mean()
print("\n=== cpu/mem/disk ===")
print(pd.DataFrame({"window": a2, "base": b2}).round(2).to_string())
