# -*- coding: utf-8 -*-
"""
M3 根因排序：频次基线 + 规则路线 A（D2）

证据设计（与 M2 的三级告警分诊联动）：
- novel（新发，强证据）权重 2.0；semi（半常驻）1.0；core（全天候常驻）0.5
- score(entity) = 0.4×最早得分 + 0.3×加权频次得分 + 0.3×共现得分
- freq 模式（最笨基线）：按告警条数排序（全部告警，不分层）
"""
import pandas as pd
from pathlib import Path

ROOT = Path(__file__).parents[1]
ALARMS = ROOT / "results" / "alarms_tagged.parquet"
FLOOD = ROOT / "results" / "flood_windows.parquet"
FAULTS = ROOT / "results" / "fault_windows.parquet"
EXP = ROOT / "results" / "experiments.md"

TIER_W = {"novel": 2.0, "semi": 1.0, "core": 0.5}


def minmax(s: pd.Series) -> pd.Series:
    if s.max() == s.min():
        return pd.Series(1.0, index=s.index)
    return (s - s.min()) / (s.max() - s.min())


def rank_entities(wdf: pd.DataFrame, mode: str) -> pd.Series:
    wdf = wdf.copy()
    wdf["minute"] = wdf["timestamp"] // 60_000
    wdf["w"] = wdf["tier"].map(TIER_W)
    g = wdf.groupby("entity")
    freq = g.size()
    wfreq = wdf.groupby("entity")["w"].sum()          # 分层加权频次
    cooc = wdf.groupby(["minute", "entity"]).size().unstack(fill_value=0)
    cooc_score = (cooc > 0).sum()                      # 有告警的分钟数
    if mode == "freq":
        return minmax(freq)
    if mode == "rule":
        # 新发（novel）告警为主证据：根因的判别性信号
        novel = wdf[wdf["tier"] == "novel"]
        if novel.empty:
            nf = pd.Series(0.0, index=freq.index)
            ne = pd.Series(0.0, index=freq.index)
        else:
            ng = novel.groupby("entity")
            nf = ng.size().reindex(freq.index, fill_value=0)
            ne = (-ng["timestamp"].min()).reindex(freq.index, fill_value=0)
        first = g["timestamp"].min()
        return (0.5 * minmax(nf)          # 新发告警数（主证据）
                + 0.15 * minmax(ne)       # 新发告警越早越可疑
                + 0.2 * minmax(wfreq)     # 分层加权频次（辅助）
                + 0.15 * minmax(cooc_score))
    raise ValueError(mode)


def evaluate(alarms, floods, faults, mode):
    train = faults[faults.data_type == "train"]
    top1 = top3 = n_eval = 0
    miss_cases = []
    for _, w in train.iterrows():
        fl = floods[(floods.st_ms <= w.ed_ms + 600_000) &
                    (floods.ed_ms >= w.st_ms - 600_000)]
        if len(fl) == 0:
            continue
        st, ed = fl.st_ms.min(), fl.ed_ms.max()
        wdf = alarms[(alarms.timestamp >= st) & (alarms.timestamp <= ed)]
        if wdf.empty or w.service not in wdf.entity.values:
            n_eval += 0  # 无窗口证据，不计入评测（可检测故障协议）
            continue
        scores = rank_entities(wdf, mode).sort_values(ascending=False)
        ranked = list(scores.index)
        n_eval += 1
        if w.service in ranked[:1]:
            top1 += 1
        if w.service in ranked[:3]:
            top3 += 1
        elif w.service not in ranked[:3]:
            if len(miss_cases) < 5:
                miss_cases.append((w.id, w.service, w.anomaly_type,
                                   ranked[:3], int((wdf.entity == w.service).sum())))
    return {"mode": mode, "n_eval": n_eval, "top1": top1, "top3": top3,
            "miss": miss_cases}


def main():
    alarms = pd.read_parquet(ALARMS)
    floods = pd.read_parquet(FLOOD)
    faults = pd.read_parquet(FAULTS)
    results = []
    for mode in ["freq", "rule"]:
        r = evaluate(alarms, floods, faults, mode)
        results.append(r)
        print(f"[m3] {r['mode']:5s}: Top-1={r['top1']}/{r['n_eval']} "
              f"({r['top1']/max(r['n_eval'],1)*100:.0f}%)  "
              f"Top-3={r['top3']}/{r['n_eval']} "
              f"({r['top3']/max(r['n_eval'],1)*100:.0f}%)")
    # 失误样例（供人工分析）
    for r in results:
        if r["miss"]:
            print(f"[m3] {r['mode']} miss samples (id, root, type, top3, root_alarm_cnt):")
            for m in r["miss"]:
                print("   ", m)
    # 写入对比实验表
    lines = ["# 对比实验表（每天填，空表不过夜）", "",
             "> 评测协议：仅评「可检测故障」——存在重叠洪水窗口且根因实体在窗口证据中的 "
             "train 故障（文献标准做法，答辩如实说明）",
             "",
             "| 方法 | Top-1 | Top-3 | 评测窗口数 | 备注 |",
             "|---|---|---|---|---|"]
    for r in results:
        note = {"freq": "最笨基线：按告警条数", "rule": "0.4最早+0.3加权频次+0.3共现"}[r["mode"]]
        lines.append(f"| {r['mode']} | {r['top1']}/{r['n_eval']} "
                     f"({r['top1']/max(r['n_eval'],1)*100:.0f}%) | "
                     f"{r['top3']}/{r['n_eval']} "
                     f"({r['top3']/max(r['n_eval'],1)*100:.0f}%) | "
                     f"{r['n_eval']} | {note} |")
    EXP.write_text("\n".join(lines), encoding="utf-8")
    print(f"[m3] experiments table written -> {EXP}")


if __name__ == "__main__":
    main()
