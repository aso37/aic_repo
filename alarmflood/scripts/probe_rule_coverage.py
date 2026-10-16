# -*- coding: utf-8 -*-
"""诊断：告警规则的 KPI 名与阈值是否合理。

背景（为什么有这个脚本）
--------------------------------------------------------------------
`loaders/loader_aiops2021.py` 的 RULES 原先对全部 KPI 统一使用阈值 90，
但 10 条里只有 7 条触发过。本脚本用来查清：是 KPI 名字写错，还是阈值不对。

⚠️ **本脚本曾因只看单日数据而给出错误结论，务必注意**
--------------------------------------------------------------------
初版默认只扫 0304 一天，据此判定 `sdb_DSKPercentBusy` "阈值 90 不可达"（该日 max=36）。
但另外 4 天的 max 是 100.0 / 100.0 / 100.4 / 100.0 —— **原阈值其实可达，10 天触发 13 次**。
基于该误判把阈值改到 30 是一次错误的改动，已回退。

**因此本脚本默认扫描全部 10 天**，并打印逐日 max 供对照。
**单日结论不能用于判定阈值可达性。**

用法（在 alarmflood/ 目录下）
--------------------------------------------------------------------
    .venv\\Scripts\\python.exe scripts\\probe_rule_coverage.py           # 全部 10 天（默认）
    .venv\\Scripts\\python.exe scripts\\probe_rule_coverage.py 0304      # 只看某一天（仅快速查看）
"""
import importlib.util
import os
import re
import sys
from pathlib import Path

import pandas as pd

# Windows 控制台默认 GBK，打印 emoji（⚠️ 等）会抛 UnicodeEncodeError。
# 强制 UTF-8 输出，一次修好所有 print（实测在中文 PowerShell 下会崩）。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                    # noqa: BLE001
    pass

ROOT = Path(__file__).parents[1]


def _resolve_raw() -> Path:
    """定位原始数据集根目录。

    解析顺序与 `loaders/loader_aiops2021.py` 的 `_resolve_raw()` 保持一致，
    并**同时兼容两种环境变量命名**（队友用 AIOPS2021_RAW，诊断脚本原用 AIOPS_RAW）：
      1) 环境变量 AIOPS2021_RAW / AIOPS_RAW
      2) 仓库同级的 aic_data/raw/aiops2021/...（本机解压位置）
      3) 兜底 E:/aic_data/AIOps2021
    """
    for key in ("AIOPS2021_RAW", "AIOPS_RAW"):
        if os.environ.get(key):
            return Path(os.environ[key])
    local = (Path(__file__).parents[3] / "aic_data" / "raw" / "aiops2021" /
             "aiops2021-main" / "AIOps2021")
    if local.exists():
        return local
    return Path("E:/aic_data/AIOps2021")


RAWDIR = _resolve_raw() / "aiops2021-2"
ALL_DAYS = ["0304", "0305", "0306", "0307", "0309", "0310",
            "0312", "0323", "0324", "0325"]
USECOLS = ["timestamp", "cmdb_id", "kpi_name", "value"]


def load_days(days):
    frames = []
    for d in days:
        f = RAWDIR / d / "metric" / f"metric_{d}.csv"
        if not f.exists():
            print(f"  [warn] 缺文件，跳过：{f}")
            continue
        frames.append(pd.read_csv(f, usecols=USECOLS))
    if not frames:
        raise SystemExit(f"[probe] 没读到任何 metric 文件，检查数据集路径 = {RAWDIR.parent}")
    return frames


def tokens_of(kpi):
    return [t for t in re.split(r"[_\-]", kpi) if len(t) >= 4 and "OSLinux" not in t]


def find_candidates(kpi, kpi_list):
    cands = set()
    for t in tokens_of(kpi):
        for real in kpi_list:
            if t.lower() in real.lower():
                cands.add(real)
    return sorted(cands)


def main():
    days = sys.argv[1:] or ALL_DAYS
    single_day = len(days) == 1

    print(f"数据目录 : {RAWDIR}")
    print(f"扫描日期 : {days}  （共 {len(days)} 天）")
    if single_day:
        print("⚠️  单日模式：**不能**据此判定阈值是否可达，仅用于快速查看")

    # ---- 从 loader 读取 RULES，避免两处维护同一份清单 ----
    spec = importlib.util.spec_from_file_location(
        "loader_aiops2021", ROOT / "loaders" / "loader_aiops2021.py")
    loader = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loader)
    RULES = loader.RULES

    frames = load_days(days)
    m = pd.concat(frames, ignore_index=True)
    kpi_list = sorted(m.kpi_name.unique())
    print(f"样本 {len(m):,} 行 | KPI {len(kpi_list)} 种 | 实体 {m.cmdb_id.nunique()} 个")

    # ---- 1) 规则里的 KPI 是否存在 ----
    print("\n" + "=" * 96)
    print("1) 规则里的 KPI 是否存在于数据中")
    print("=" * 96)
    missing = []
    for kpi, thr, _tpl, _sev in RULES:
        ok = kpi in kpi_list
        if not ok:
            missing.append((kpi, thr))
        print(f"  [{'存在' if ok else '缺失'}]  thr={thr:<6}  {kpi}")
    print(f"\n  小计：{len(RULES) - len(missing)}/{len(RULES)} 存在，{len(missing)} 条缺失")

    # ---- 2) 缺失规则的候选 KPI ----
    if missing:
        print("\n" + "=" * 96)
        print("2) 缺失规则的候选真实 KPI（判断是否只是名字写错）")
        print("=" * 96)
        for kpi, thr in missing:
            cands = find_candidates(kpi, kpi_list)
            print(f"\n  规则 KPI : {kpi}   (阈值 {thr})")
            if not cands:
                print("    -> 无近名候选（该指标可能不存在于本数据集）")
            for c in cands[:10]:
                v = m.loc[m.kpi_name == c, "value"]
                print(f"    -> 候选: {c}   max={v.max():.4f}  "
                      f"超阈次数={int((v > thr).sum()):,}")

    # ---- 3) 每条规则的阈值 vs 实际分布（核心表）----
    print("\n" + "=" * 96)
    print("3) 每条规则：阈值 vs 实际分布   ← 判定阈值是否合理的核心表")
    print("=" * 96)
    print(f"  {'阈值':>7} {'超阈次数':>10} {'median':>11} {'max':>11}  {'结论':<12} KPI")
    for kpi, thr, _tpl, _sev in RULES:
        v = m.loc[m.kpi_name == kpi, "value"]
        if v.empty:
            print(f"  {thr:>7} {'—':>10} {'—':>11} {'—':>11}  {'无数据':<12} {kpi}")
            continue
        n_over = int((v > thr).sum())
        verdict = "可触发" if n_over > 0 else "⚠️ 从未触发"
        print(f"  {thr:>7} {n_over:>10,} {v.median():>11.3f} {v.max():>11.3f}  "
              f"{verdict:<12} {kpi.split('_', 1)[-1]}")

    # ---- 4) 逐日 max 对照（防止单日误判）----
    print("\n" + "=" * 96)
    print("4) 逐日 max 对照 —— 任何「不可达」结论都必须经得起这张表")
    print("=" * 96)
    print(f"  {'KPI':<34} " + " ".join(f"{d:>8}" for d in days))
    for kpi, thr, _tpl, _sev in RULES:
        vals = []
        for fr in frames:
            v = fr.loc[fr.kpi_name == kpi, "value"]
            vals.append(f"{v.max():>8.2f}" if len(v) else f"{'—':>8}")
        print(f"  {kpi.split('_', 1)[-1]:<34} " + " ".join(vals))

    # ---- 5) 判定规则 ----
    print("\n" + "=" * 96)
    print("5) 判定规则（重要）")
    print("=" * 96)
    print("  · KPI 缺失     → 改名字，或换成数据里真实存在的指标")
    print("  · 从未触发     → **先别急着调低阈值**。先问：这个指标该不该做告警？")
    print("      判据是「改完之后对目标故障类型的判别力是否提升，且噪声代价是否可接受」，")
    print("      而不是「够不够得到」。调低阈值可能只是把正常状态变成误报。")
    print("  · 唯一的正面判据 → 换指标后，在目标故障窗内高覆盖、在其他故障类型 0% 命中")
    print("      （参见 scripts/probe_net_signals.py 的 TCP-FIN-WAIT 分析）")
    print("  · 改完 RULES 后必须重跑全链路：")
    print("      python loaders/loader_aiops2021.py")
    print("      python modules/m1_templater.py")
    print("      python modules/m2_window.py")
    print("      python modules/m3_rank.py")


if __name__ == "__main__":
    main()
