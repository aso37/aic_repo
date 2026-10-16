# -*- coding: utf-8 -*-
"""诊断：把"漏报窗口的指标信号"从单样本观察升级为全样本统计。

为什么需要这个脚本
--------------------------------------------------------------------
`probe_missed2.py` 对每个故障类型只分析 `missed[0]` —— **一个样本**。
而 D1/D2 材料里的结论是全覆盖的：

    "NETWORK 型漏报：OS 层网络指标无指纹"（47 个 train 故障 / 20 个可评测窗口）

这是从 1 个窗口（Tomcat02）推广到全部 NETWORK 故障。**结论可能成立，但证据强度不够**
—— 评委问"你怎么知道 47 个都是这样"，只有一个案例是答不上来的。

本脚本遍历**所有**漏报窗口，给出统计：
  · 每个类型：漏报窗口数 / 其中有可判别信号的窗口数
  · 哪些 KPI 最常出现信号（含 NET/TCP 类）
  · 明确指出"多少个窗口确实是全指标无信号"

用法（在 alarmflood/ 目录下，需要 E 盘原始数据）
--------------------------------------------------------------------
    .venv\\Scripts\\python.exe scripts\\probe_missed_all.py
    .venv\\Scripts\\python.exe scripts\\probe_missed_all.py NETWORK   # 只看某一类

时间基准：全程使用 st_ms / ed_ms（**禁止 .timestamp()**，会偏 8 小时）。
"""
import sys
from collections import Counter
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).parents[1]
RAW_ROOT = Path("E:/aic_data/AIOps2021/aiops2021-2")

RATIO_HI = 2.0      # 窗口均值 / 基线均值 ≥ 此值算"显著升高"
RATIO_LO = 0.5      # ≤ 此值算"显著降低"
BASE_MS = 7200_000  # 基线：故障前 2 小时

# 逐日缓存，避免同一天 86MB 的 metric 被重复读取
_day_cache: dict = {}


def load_day(day: str) -> pd.DataFrame:
    if day not in _day_cache:
        f = RAW_ROOT / day / "metric" / f"metric_{day}.csv"
        if f.exists():
            _day_cache[day] = pd.read_csv(
                f, usecols=["timestamp", "cmdb_id", "kpi_name", "value"])
        else:
            _day_cache[day] = pd.DataFrame(
                columns=["timestamp", "cmdb_id", "kpi_name", "value"])
    return _day_cache[day]


def signals_for(w, m: pd.DataFrame):
    """返回该窗口的显著指标列表 [(kpi, win_mean, base_mean, ratio), ...]"""
    sub = m[m.cmdb_id == w.service]
    if sub.empty:
        return None
    ts = sub["timestamp"] * 1000                      # 秒 → 毫秒（与 st_ms 同基准）
    winm = sub[(ts >= w.st_ms) & (ts <= w.ed_ms)]
    base = sub[(ts >= w.st_ms - BASE_MS) & (ts < w.st_ms)]
    if winm.empty or base.empty:
        return None
    a = winm.groupby("kpi_name")["value"].mean()
    b = base.groupby("kpi_name")["value"].mean()
    cmp = pd.DataFrame({"win": a, "base": b}).dropna()
    if cmp.empty:
        return None
    cmp["ratio"] = cmp["win"] / cmp["base"].replace(0, float("nan"))
    sig = cmp[(cmp.ratio >= RATIO_HI) | (cmp.ratio <= RATIO_LO)]
    return [(k, float(r.win), float(r.base), float(r.ratio))
            for k, r in sig.iterrows()]


def main():
    types = [sys.argv[1]] if len(sys.argv) > 1 else \
        ["NETWORK", "DISK", "MEMORY", "CPU", "JVM;MEMORY", "JVM;CPU"]

    win = pd.read_parquet(ROOT / "results" / "fault_windows.parquet")
    alarms = pd.read_parquet(ROOT / "data" / "alarm.parquet")

    train = win[win.data_type == "train"]
    # 漏报 = 故障窗内根因实体没有任何告警（与 probe_missed2 同口径）
    missed_all = []
    for _, w in train.iterrows():
        hit = alarms[(alarms.timestamp >= w.st_ms) & (alarms.timestamp <= w.ed_ms)
                     & (alarms.entity == w.service)]
        if len(hit) == 0:
            missed_all.append(w)
    print(f"train 漏报窗口合计: {len(missed_all)}/{len(train)}")
    print(f"判据: 窗口均值/前2h基线均值 ≥{RATIO_HI} 或 ≤{RATIO_LO}\n")

    summary = []
    for t in types:
        grp = [w for w in missed_all if w.anomaly_type == t]
        if not grp:
            continue
        n_no_data = n_no_signal = n_with_signal = 0
        kpi_counter = Counter()
        net_examples = []
        for w in grp:
            m = load_day(pd.to_datetime(w.st_ms, unit="ms").strftime("%m%d"))
            if m.empty:
                n_no_data += 1
                continue
            sig = signals_for(w, m)
            if sig is None:
                n_no_data += 1
                continue
            if not sig:
                n_no_signal += 1
                continue
            n_with_signal += 1
            for k, *_ in sig:
                kpi_counter[k] += 1
            netk = [s for s in sig if "NET" in s[0].upper() or "TCP" in s[0].upper()]
            if netk and len(net_examples) < 3:
                net_examples.append((w.service,
                                     pd.to_datetime(w.st_ms, unit="ms"),
                                     netk[:3]))
        summary.append({
            "类型": t, "漏报窗口": len(grp),
            "有信号": n_with_signal, "全指标无信号": n_no_signal,
            "数据缺失": n_no_data,
        })
        print("=" * 74)
        print(f"{t} —— 漏报 {len(grp)} 个窗口：有信号 {n_with_signal} / "
              f"全指标无信号 {n_no_signal} / 数据缺失 {n_no_data}")
        if kpi_counter:
            print("  最常出现信号的 KPI（出现窗口数）:")
            for k, c in kpi_counter.most_common(8):
                print(f"    {c:>3} 个窗口   {k}")
        if net_examples:
            print("  含 NET/TCP 类信号的窗口示例:")
            for svc, ts, ks in net_examples:
                print(f"    {svc} {ts}")
                for k, wv, bv, rt in ks:
                    print(f"        {k}  win={wv:.3f} base={bv:.3f} ratio={rt:.2f}")

    print("\n" + "=" * 74)
    print("汇总")
    print("=" * 74)
    print(pd.DataFrame(summary).to_string(index=False))
    print("\n判读：")
    print("  · 「全指标无信号」占多数 → D1「该类型在本数据集上无可用信号」的结论**有统计支撑**")
    print("  · 若 NET 类 KPI 在多窗口反复出现信号 → 说明存在可用信号，只是没做成告警规则")
    print("  · 「数据缺失」>0 → 该类型部分故障落在没有 metric 的日期，应纳入口径披露")


if __name__ == "__main__":
    main()
