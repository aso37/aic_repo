# -*- coding: utf-8 -*-
"""
M3 根因排序：频次基线 + 规则路线 A（D2）

证据设计（与 M2 的三级告警分诊联动）：
- novel（新发，强证据）权重 2.0；semi（半常驻）1.0；core（全天候常驻）0.5
- rule 模式打分（四路 min-max 归一化后加权，权重合计 1.0）：
      0.50 × 新发(novel)告警数            ← 主证据：根因的判别性信号
      0.15 × 新发告警首现时间（越早越高）
      0.20 × 分层加权频次
      0.15 × 活跃度（该实体在窗口内"有告警的分钟数"）
  注：第四路原名"共现"，但实现上并非模板共现 Jaccard 度（名实不符，已更正命名）。
      若 D6 要上真正的 Jaccard 共现，替换 rank_entities 中的 active_minutes 即可。
- freq 模式（最笨基线）：按告警条数排序（全部告警，不分层）

experiments.md 写入约定（重要）：
- 本脚本**只替换** experiments.md 中 <!-- AUTO-TABLE:BEGIN --> 与
  <!-- AUTO-TABLE:END --> 之间的内容；标记之外的文字（如"## D2 观察与分析"）
  一律不动，避免重跑脚本抹掉人工分析。
- 若文件里找不到标记，则在文件末尾追加一个区块（同样不删任何原文）。
"""
import json

import pandas as pd
from pathlib import Path

ROOT = Path(__file__).parents[1]
ALARMS = ROOT / "results" / "alarms_tagged.parquet"
FLOOD = ROOT / "results" / "flood_windows.parquet"
FAULTS = ROOT / "results" / "fault_windows.parquet"
EXP = ROOT / "results" / "experiments.md"

TIER_W = {"novel": 2.0, "semi": 1.0, "core": 0.5}

# experiments.md 中自动生成区块的标记
BEGIN = "<!-- AUTO-TABLE:BEGIN -->"
END = "<!-- AUTO-TABLE:END -->"

# 表格"备注"列文案，必须与 rank_entities 的实际公式一致
NOTE = {
    "freq": "最笨基线：按告警条数",
    "rule": "0.5新发数+0.15新发早+0.2分层加权频次+0.15活跃度",
}

# 其它方法落盘的结果（m3_llm.py → llm_results.json；D6 融合 → fusion_results.json）
# 由本脚本统一渲染进表格，保证 experiments.md 只有一个写入口，避免互相覆盖
EXTRA_ORDER = {"llm": 2, "fusion": 3}


def load_extra_rows():
    """扫描 results/*_results.json，取出 {method,n_eval,top1,top3,note} 形式的行"""
    rows = []
    for p in sorted((ROOT / "results").glob("*_results.json")):
        try:
            # utf-8-sig：容忍 BOM（用记事本/PowerShell 编辑过的 JSON 会带 BOM，
            # 用 utf-8 读会 json 解析失败，而且这里必须抛出来而不是静默跳过）
            d = json.loads(p.read_text(encoding="utf-8-sig"))
        except Exception as e:
            print(f"[m3][warn] 跳过 {p.name}：无法解析（{type(e).__name__}: {e}）")
            continue
        if not isinstance(d, dict) or "n_eval" not in d or "top1" not in d:
            print(f"[m3][warn] 跳过 {p.name}：缺少 n_eval/top1 字段")
            continue
        rows.append({
            "mode": d.get("method", p.stem),
            "n_eval": int(d["n_eval"]),
            "top1": int(d["top1"]),
            "top3": int(d.get("top3", 0)),
            "note": d.get("note", f"来自 {p.name}"),
        })
    rows.sort(key=lambda r: EXTRA_ORDER.get(r["mode"], 9))
    return rows


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
    # 活跃度 = 该实体在窗口内"有告警的分钟数"。
    # 注意：这不是模板共现 Jaccard 度（旧文档曾如此表述，属名实不符，已更正）。
    by_minute = wdf.groupby(["minute", "entity"]).size().unstack(fill_value=0)
    active_minutes = (by_minute > 0).sum()
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
        return (0.50 * minmax(nf)              # 新发告警数（主证据）
                + 0.15 * minmax(ne)            # 新发告警越早越可疑
                + 0.20 * minmax(wfreq)         # 分层加权频次（辅助）
                + 0.15 * minmax(active_minutes))
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


def render_table(results) -> str:
    """生成带标记的表格区块（只含协议说明与表格，不含标题/分析）"""
    block = [BEGIN,
             "> 评测协议：仅评「可检测故障」——存在重叠洪水窗口且根因实体在窗口证据中的 "
             "train 故障（文献标准做法，答辩如实说明）",
             "",
             "| 方法 | Top-1 | Top-3 | 评测窗口数 | 备注 |",
             "|---|---|---|---|---|"]
    rows = [{"mode": r["mode"], "n_eval": r["n_eval"], "top1": r["top1"],
             "top3": r["top3"], "note": NOTE.get(r["mode"], "")} for r in results]
    rows += load_extra_rows()          # llm / fusion 等由各自脚本落盘，这里统一渲染
    for r in rows:
        n = max(r["n_eval"], 1)
        block.append(f"| {r['mode']} | {r['top1']}/{r['n_eval']} "
                     f"({r['top1'] / n * 100:.0f}%) | "
                     f"{r['top3']}/{r['n_eval']} "
                     f"({r['top3'] / n * 100:.0f}%) | "
                     f"{r['n_eval']} | {r['note']} |")
    block.append(END)
    return "\n".join(block)


def write_table(results, path: Path) -> None:
    """只替换标记区块，标记之外的文字一律保留"""
    block = render_table(results)
    if path.exists():
        old = path.read_text(encoding="utf-8")
    else:
        old = "# 对比实验表（每天填，空表不过夜）\n"

    if BEGIN in old and END in old:
        pre, rest = old.split(BEGIN, 1)
        _, post = rest.split(END, 1)
        path.write_text(pre + block + post, encoding="utf-8")
        action = "已更新标记区块（人工分析保留）"
    else:
        path.write_text(old.rstrip("\n") + "\n\n" + block + "\n", encoding="utf-8")
        action = "未找到标记，已追加区块（原文未改动）"

    print(f"[m3] experiments.md {action} -> {path}")


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
    write_table(results, EXP)


if __name__ == "__main__":
    main()
