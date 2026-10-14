# -*- coding: utf-8 -*-
"""诊断 0（最高优先级）：告警规则覆盖度 —— 为什么 10 条规则只有 7 条触发过？

背景
--------------------------------------------------------------------
`loaders/loader_aiops2021.py` 的 RULES 定义了 10 条阈值规则，但生成的
`data/alarm.parquet` 里只出现了 **7 种** alarm_id。从未触发的三条：

    OSLinux-OSLinux_NETWORK_ens160_NETBandwidthUtil     （网络带宽利用率）
    OSLinux-OSLinux_FILESYSTEM_-_FSInodeUsedPercent      （inode 使用率）
    OSLinux-OSLinux_SWAP_SWAP_SWPTotSwapUsedPercent      （Swap 使用率）

而 NETWORK 型故障占 train 故障的 42%（47/112），是最大的一类 —— 所以
"网络规则不触发"直接导致 NETWORK Top-1 = 0/20，拖垮整体指标。

本脚本一次查清：KPI 名是否写错 / 阈值量纲是否对不上，并**直接给出改法**。

用法（在 alarmflood/ 目录下）
--------------------------------------------------------------------
    .venv\\Scripts\\python.exe scripts\\probe_rule_coverage.py
"""
import importlib.util
import os
import re
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).parents[1]
# 原始数据根目录：优先读环境变量 AIOPS_RAW（与 loaders/loader_aiops2021.py 一致）
RAW_ROOT = Path(os.environ.get("AIOPS_RAW", "E:/aic_data/AIOps2021")) / "aiops2021-2"
DAY = os.environ.get("AIOPS_DAY", "0304")

# ---- 从 loader 里读取 RULES，避免两处维护同一份清单 ----
spec = importlib.util.spec_from_file_location(
    "loader_aiops2021", ROOT / "loaders" / "loader_aiops2021.py")
loader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(loader)
RULES = loader.RULES
THRESHOLDS = {r[0]: r[1] for r in RULES}


def tokens_of(kpi: str) -> list:
    """从规则 KPI 名里提取可搜索的词，丢掉 OSLinux 这类公共前缀"""
    return [t for t in re.split(r"[_\-]", kpi)
            if len(t) >= 4 and "OSLinux" not in t]


def diagnose(kpi: str, thr: float, kpi_list, m: pd.DataFrame):
    """给出结论：(类型, 建议文本, 候选KPI, 候选实测max)"""
    toks = tokens_of(kpi)
    cands = set()
    for t in toks:
        for real in kpi_list:
            if t.lower() in real.lower():
                cands.add(real)

    if not cands:
        return ("数据中无此指标",
                "没有任何近名 KPI → 该指标可能不存在于本数据集，"
                "需更换指标，或人工在下面的全量清单里找",
                [], None)

    stats = {c: m.loc[m.kpi_name == c, "value"] for c in cands}
    stats = {c: v for c, v in stats.items() if len(v) > 0}

    over = [c for c, v in stats.items() if v.max() > thr]
    if over:
        best = max(over, key=lambda c: stats[c].max())
        return ("疑似 KPI 名写错",
                f"改成 `{best}`（实测 max={stats[best].max():.3f} > 阈值 {thr}）后应能触发",
                sorted(cands), stats[best].max())

    if stats:
        best = max(stats, key=lambda c: stats[c].max())
        return ("疑似量纲不一致",
                f"`{best}` 的实测 max 只有 {stats[best].max():.3f}，"
                f"阈值 {thr} 永远不成立 → 需换算量纲（如 ×100）或下调阈值",
                sorted(cands), stats[best].max())

    return ("候选 KPI 全为空列", "候选 KPI 在该实体/该天没有数据点", sorted(cands), None)


def main():
    f = RAW_ROOT / DAY / "metric" / f"metric_{DAY}.csv"
    m = pd.read_csv(f, usecols=["timestamp", "cmdb_id", "kpi_name", "value"])
    kpi_list = sorted(m.kpi_name.unique())

    print(f"metric 文件 : {f}")
    print(f"行数 {len(m):,} | KPI {len(kpi_list)} 种 | 实体 {m.cmdb_id.nunique()} 个")

    # ---- 1) 规则里的 KPI 是否存在 ----
    print("\n" + "=" * 74)
    print("1) 10 条规则的 KPI 是否存在于数据中")
    print("=" * 74)
    missing = []
    for kpi, thr, _tpl, _sev in RULES:
        ok = kpi in kpi_list
        if not ok:
            missing.append((kpi, thr))
        print(f"  [{'存在' if ok else '缺失'}]  thr={thr:<5}  {kpi}")
    print(f"\n  小计：{len(RULES) - len(missing)}/{len(RULES)} 存在，{len(missing)} 条缺失")

    # ---- 2) 缺失规则的自动诊断 ----
    if missing:
        print("\n" + "=" * 74)
        print("2) 缺失规则自动诊断（结论 + 建议改法）")
        print("=" * 74)
        for kpi, thr in missing:
            kind, advice, cands, _mx = diagnose(kpi, thr, kpi_list, m)
            print(f"\n  ── 规则 KPI: {kpi}   (阈值 {thr})")
            print(f"     结论：{kind}")
            print(f"     建议：{advice}")
            if cands:
                print(f"     候选 KPI（{len(cands)}）：")
                for c in cands[:10]:
                    cnt = int((m.kpi_name == c).sum())
                    print(f"        · {c}   (数据点 {cnt:,})")

    # ---- 3) 取值分布：判断量纲 ----
    print("\n" + "=" * 74)
    print("3) 关键 KPI 的取值分布（min / median / max）—— 判断量纲")
    print("=" * 74)
    groups = {
        "CPU":     r"CPU",
        "MEMORY":  r"MEMUsed|NoCacheMem",
        "DISK":    r"DSKPercentBusy",
        "NETWORK": r"NET|Bandwidth|ens",
        "INODE":   r"Inode|INODE",
        "SWAP":    r"Swap|SWAP",
    }
    for label, pat in groups.items():
        sub = m[m.kpi_name.str.contains(pat, case=False, na=False, regex=True)]
        if sub.empty:
            print(f"\n  [{label}] 无匹配 KPI")
            continue
        g = sub.groupby("kpi_name")["value"].agg(["min", "median", "max"]).round(3)
        print(f"\n  [{label}]")
        print(g.to_string())

    # ---- 4) 行动指引 ----
    print("\n" + "=" * 74)
    print("4) 下一步怎么做")
    print("=" * 74)
    print("  · 结论为「疑似 KPI 名写错」→ 改 loader RULES 里的 KPI 名")
    print("  · 结论为「疑似量纲不一致」→ 换算量纲，或把阈值改成合理值")
    print("  · 改完 RULES 后必须**重跑全链路**：")
    print("      python loaders/loader_aiops2021.py")
    print("      python modules/m1_templater.py")
    print("      python modules/m2_window.py")
    print("      python modules/m3_rank.py")
    print("    对比表前两行会变，experiments.md 的标记区块会自动更新（人工分析不受影响）")


if __name__ == "__main__":
    main()
