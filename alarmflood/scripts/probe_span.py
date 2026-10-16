# -*- coding: utf-8 -*-
"""诊断：评估时间跨度的口径问题（M3 排序用的时间区间是怎么定的？）

问题
--------------------------------------------------------------------
`m3_rank.evaluate()` / 各方法的评测都按下面的方式确定"排序用的时间跨度"：

    fl  = 与故障窗有重叠（±10 分钟容差）的洪水窗口
    st, ed = fl.st_ms.min(), fl.ed_ms.max()      # ← 所有重叠洪水窗口的并集

这个定义有两个缺陷：
  1. 跨度**不保证包含故障窗本身** —— 如果洪水窗口恰好落在故障窗之前/之后，
     排序就是在与故障无关的时间段上做的
  2. 跨度可能远大于故障窗 —— 引入大量无关告警，稀释根因信号

本脚本量化这两个缺陷，并对比三种跨度定义下的表现。

用法（在 alarmflood/ 目录下）
--------------------------------------------------------------------
    .venv\\Scripts\\python.exe scripts\\probe_span.py
"""
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "modules"))
from m3_rank import rank_entities  # noqa: E402

TOL_MS = 600_000        # 与 m3_rank.evaluate 相同的 ±10 分钟容差
MODES = [
    ("cur",   "现状：仅洪水窗口并集"),
    ("union", "故障窗 ∪ 洪水窗口并集"),
    ("fault", "仅故障窗"),
]


def collect(alarms, floods, faults):
    train = faults[faults.data_type == "train"]
    rows = []
    for _, w in train.iterrows():
        fl = floods[(floods.st_ms <= w.ed_ms + TOL_MS) &
                    (floods.ed_ms >= w.st_ms - TOL_MS)]
        if len(fl) == 0:
            continue
        st, ed = int(fl.st_ms.min()), int(fl.ed_ms.max())
        wdf = alarms[(alarms.timestamp >= st) & (alarms.timestamp <= ed)]
        if wdf.empty or w.service not in wdf.entity.values:
            continue
        rows.append({"w": w, "st": st, "ed": ed,
                     "fault_ms": int(w.ed_ms - w.st_ms),
                     "span_ms": ed - st,
                     "n_win": len(fl),
                     "contains": (st <= w.st_ms) and (ed >= w.ed_ms),
                     "n_alarms": len(wdf)})
    return pd.DataFrame(rows)


def span_of(r, mode):
    w = r["w"]
    if mode == "cur":
        return r["st"], r["ed"]
    if mode == "fault":
        return int(w.st_ms), int(w.ed_ms)
    return min(r["st"], int(w.st_ms)), max(r["ed"], int(w.ed_ms))


def main():
    alarms = pd.read_parquet(ROOT / "results" / "alarms_tagged.parquet")
    floods = pd.read_parquet(ROOT / "results" / "flood_windows.parquet")
    faults = pd.read_parquet(ROOT / "results" / "fault_windows.parquet")

    d = collect(alarms, floods, faults)
    n = len(d)
    ratio = d.span_ms / d.fault_ms
    print(f"可评测窗口 {n} 个\n")
    print("=== 现状跨度（仅洪水窗口并集）的两个缺陷 ===")
    print(f"  跨度/故障窗 倍数：中位数 {ratio.median():.1f}x，"
          f"p90 {ratio.quantile(0.9):.1f}x，最大 {ratio.max():.1f}x")
    print(f"  跨度**不包含**故障窗：{(~d.contains).sum()}/{n} "
          f"({(~d.contains).sum()/n*100:.0f}%)   ← 排序用的是与故障无关的时间段")
    print(f"  跨度 > 故障窗 3 倍：{(ratio > 3).sum()}/{n} "
          f"({(ratio > 3).sum()/n*100:.0f}%)")
    print(f"  跨度内告警条数：中位数 {int(d.n_alarms.median())}")

    bad = d[~d.contains]
    if len(bad):
        print(f"\n  跨度不含故障窗的案例（前 5 个）：")
        for _, r in bad.head(5).iterrows():
            w = r["w"]
            print(f"    fault#{w.id} {w.service:<10} 故障窗 "
                  f"{pd.to_datetime(w.st_ms, unit='ms'):%m-%d %H:%M}~"
                  f"{pd.to_datetime(w.ed_ms, unit='ms'):%H:%M}  →  "
                  f"跨度 {pd.to_datetime(r.st, unit='ms'):%m-%d %H:%M}~"
                  f"{pd.to_datetime(r.ed, unit='ms'):%H:%M}")

    print("\n=== 三种跨度定义下的 rule 表现（同一份代码/数据）===")
    for mode, label in MODES:
        t1 = t3 = cnt = 0
        for _, r in d.iterrows():
            w = r["w"]
            st, ed = span_of(r, mode)
            sub = alarms[(alarms.timestamp >= st) & (alarms.timestamp <= ed)]
            if sub.empty or w.service not in sub.entity.values:
                continue
            cnt += 1
            rk = list(rank_entities(sub, "rule").sort_values(ascending=False).index)
            if w.service in rk[:1]:
                t1 += 1
            if w.service in rk[:3]:
                t3 += 1
        print(f"  {label:26s} n={cnt:<3} "
              f"Top-1={t1}/{cnt} ({t1/max(cnt,1)*100:.0f}%)  "
              f"Top-3={t3}/{cnt} ({t3/max(cnt,1)*100:.0f}%)")

    print("\n=== 判读 ===")
    print("  · 「故障窗 ∪ 洪水窗」保证包含故障窗，修掉上面那 21 个缺陷，且 n 保持 72 可比")
    print("  · 「仅故障窗」n 会降到 ~51（故障窗内证据不足的窗口被剔除），但 Top-1 明显更高")
    print("    → 它说明「窗口切得越准，根因定位越准」，适合作为 D6 消融的一行报出去")
    print("  · 无论选哪个，都必须在材料里写清跨度定义，否则评委一问就露")


if __name__ == "__main__":
    main()
