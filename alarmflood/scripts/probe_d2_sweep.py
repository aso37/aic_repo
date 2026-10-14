# -*- coding: utf-8 -*-
"""D2 探针2：细扫 (days阈值, 聚类间隔) 并输出窗口质量"""
import pandas as pd
from pathlib import Path

ROOT = Path(__file__).parents[1]
a = pd.read_parquet(ROOT / "results" / "alarms_with_template.parquet")
a["day"] = a.timestamp // 86_400_000
fw = pd.read_parquet(ROOT / "results" / "fault_windows.parquet")
train = fw[fw.data_type == "train"]


def sweep(dthr, gap_min, look_fwd=0, max_dur_min=60):
    g = a.groupby(["entity", "alarm_id"]).agg(days=("day", "nunique"))
    standing = set(g[g.days >= dthr].index)
    eff = a[~a.apply(lambda r: (r.entity, r.alarm_id) in standing, axis=1)].sort_values("timestamp")
    GAP = gap_min * 60_000
    MAXDUR = max_dur_min * 60_000
    ts = eff.timestamp.values
    wid = [0]
    for i in range(1, len(ts)):
        # 间隔超 GAP，或窗口已超时长上限 → 切新窗口
        same = (ts[i] - ts[i-1] <= GAP) and (ts[i] - ts[wid[len(wid)-0] if False else 0] <= 0)
        wid.append(wid[-1])
    # 重新用累计起点判断
    wid = []
    start_idx = 0
    for i in range(len(ts)):
        if i == 0:
            wid.append(0)
            continue
        cluster_start = ts[start_idx]
        if ts[i] - ts[i-1] <= GAP and ts[i] - cluster_start <= MAXDUR:
            wid.append(wid[-1])
        else:
            wid.append(wid[-1] + 1)
            start_idx = i
    eff = eff.assign(w=wid)
    wins = eff.groupby("w").agg(st_ms=("timestamp", "min"), ed_ms=("timestamp", "max"),
                                n=("timestamp", "size"), ne=("entity", "nunique"))
    hit = sum(((wins.st_ms <= w.ed_ms + look_fwd) & (wins.ed_ms >= w.st_ms - 600_000)).any()
              for _, w in train.iterrows())
    hit_root = 0
    for _, w in train.iterrows():
        m = wins[(wins.st_ms <= w.ed_ms + look_fwd) & (wins.ed_ms >= w.st_ms - 600_000)]
        if len(m) == 0:
            continue
        st, ed = m.st_ms.min(), m.ed_ms.max()
        if ((eff.timestamp >= st) & (eff.timestamp <= ed) & (eff.entity == w.service)).any():
            hit_root += 1
    dur = (wins.ed_ms - wins.st_ms) / 60000
    return dict(dthr=dthr, gap=gap_min, fwd=look_fwd, nw=len(wins), hit=hit,
                root=hit_root, dur_med=dur.median(), dur_max=dur.max(),
                alarms_med=wins.n.median())


rows = []
for dthr in [10, 12, 14]:
    for gap in [15, 20]:
        for fwd in [600_000]:
            rows.append(sweep(dthr, gap, fwd))
df = pd.DataFrame(rows)
df["recall"] = (df.hit / 112 * 100).round(0)
df["rootpct"] = (df.root / 112 * 100).round(0)
print(df.to_string(index=False))
