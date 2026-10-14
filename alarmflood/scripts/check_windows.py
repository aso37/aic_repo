# -*- coding: utf-8 -*-
"""洪水/事件窗口 vs 故障窗口 对齐检查（查全率）"""
import pandas as pd
from pathlib import Path

ROOT = Path(__file__).parents[1]
fw = pd.read_parquet(ROOT / "results" / "fault_windows.parquet")
fl = pd.read_parquet(ROOT / "results" / "flood_windows.parquet")

train = fw[fw.data_type == "train"]
test = fw[fw.data_type == "test"]


def recall(faults):
    hit = 0
    per_type = {}
    for _, w in faults.iterrows():
        ok = ((fl.st_ms <= w.ed_ms + 600_000) &          # 抑制最多延迟10分钟
              (fl.ed_ms >= w.st_ms - 600_000)).any()     # 回看一个抑制期
        hit += ok
        t = w.anomaly_type.split(";")[0]
        per_type.setdefault(t, [0, 0])
        per_type[t][0] += ok
        per_type[t][1] += 1
    return hit, per_type


for name, faults in [("train", train), ("test", test)]:
    hit, per_type = recall(faults)
    print(f"[check] {name}: flood window recall = {hit}/{len(faults)} "
          f"({hit/len(faults)*100:.0f}%)")
    for t, (h, n) in sorted(per_type.items()):
        print(f"    {t:10s} {h}/{n} ({h/n*100:.0f}%)")
