# -*- coding: utf-8 -*-
"""D2 探针：常驻告警搁置 + 新发告警聚类开窗 的参数实验"""
import pandas as pd
from pathlib import Path

ROOT = Path(__file__).parents[1]
a = pd.read_parquet(ROOT / "results" / "alarms_with_template.parquet")
a["day"] = a.timestamp // 86_400_000
fw = pd.read_parquet(ROOT / "results" / "fault_windows.parquet")
train = fw[fw.data_type == "train"]

for dthr in [3, 5, 10]:
    g = a.groupby(["entity", "alarm_id"]).agg(days=("day", "nunique"))
    standing = set(g[g.days >= dthr].index)
    a["standing"] = [(e, r) in standing for e, r in zip(a.entity, a.alarm_id)]
    eff = a[~a.standing].sort_values("timestamp")
    for gap_min in [10, 15]:
        GAP = gap_min * 60_000
        ts = eff.timestamp.values
        wid = [0]
        for i in range(1, len(ts)):
            wid.append(wid[-1] + (0 if ts[i] - ts[i-1] <= GAP else 1))
        eff2 = eff.assign(w=wid)
        wins = eff2.groupby("w").agg(st_ms=("timestamp", "min"),
                                     ed_ms=("timestamp", "max"),
                                     n=("timestamp", "size"),
                                     ne=("entity", "nunique"))
        hit = sum(((wins.st_ms <= w.ed_ms) & (wins.ed_ms >= w.st_ms - 600_000)).any()
                  for _, w in train.iterrows())
        hit_root = 0
        for _, w in train.iterrows():
            m = wins[(wins.st_ms <= w.ed_ms) & (wins.ed_ms >= w.st_ms - 600_000)]
            if len(m) == 0:
                continue
            st, ed = m.st_ms.min(), m.ed_ms.max()
            if ((eff2.timestamp >= st) & (eff2.timestamp <= ed) & (eff2.entity == w.service)).any():
                hit_root += 1
        print(f"days>={dthr} gap={gap_min}min: 常驻对={len(standing)} 有效告警={len(eff2)} "
              f"窗口数={len(wins)} 窗口召回={hit}/{len(train)}({hit/len(train)*100:.0f}%) "
              f"根因在窗内={hit_root}/{len(train)}({hit_root/len(train)*100:.0f}%)")
