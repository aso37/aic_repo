# -*- coding: utf-8 -*-
"""
M2 洪水/事件窗口检测 —— 最终版（D2，三级告警分诊）

设计演进记录（相对手把手指南的 mean+3σ 原设计）：
1. 原设计失效：全局 mean+3σ 检出 210 个 1 分钟碎窗——88.7% 的告警是
   常驻告警（standing alarm），抑制期满集体重触发形成周期性背景（EEMUA 191 术语）。
2. 实验对比（scripts/probe_d2_*.py）：
   - 仅按天数判常驻（>=10天）：召回 70%，但半常驻（apache01 内存 5 天响 485 次
     ≈97 次/天）以"新发"身份霸榜，排序 Top-1 仅 26-28%
   - 仅按总次数判常驻（>=100次）：排序 Top-1 89%/Top-3 100%，但召回跌至 40%
3. 最终设计——三级告警分诊（alarm rationalization）：
   - core  (count>=900)  ：全天候常驻（11 个内存对）→ 不触发窗口，排序弱证据
   - semi  (100<=n<900)  ：半常驻（apache 内存、Mysql02 sdc 磁盘）→ 触发窗口（保灵敏度），排序弱证据
   - novel (count<100)   ：真事件信号（CPU/磁盘/JVM 告警）→ 触发窗口，排序强证据
   检测灵敏度与排序精度解耦：宁可多醒（semi 触发），由排序的分层权重去伪。
"""
import pandas as pd
from pathlib import Path

ROOT = Path(__file__).parents[1]
DATA = ROOT / "results" / "alarms_with_template.parquet"
OUT_WINDOWS = ROOT / "results" / "flood_windows.parquet"
OUT_TAGGED = ROOT / "results" / "alarms_tagged.parquet"

CORE_MIN = 900    # >=900 → core 常驻（全天候）
SEMI_MIN = 100    # 100~899 → semi 半常驻；<100 → novel 新发
GAP_MS = 5 * 60_000       # 告警间隔 <= 5 分钟归入同窗（防止批量注入的相邻故障混窗）
MAX_DUR_MS = 20 * 60_000  # 窗口最长 20 分钟（同上；实测比 15/60 配置 Top-1 +8pt）


def run():
    a = pd.read_parquet(DATA)
    a["day"] = a["timestamp"] // 86_400_000

    # 1) 三级分诊
    g = a.groupby(["entity", "alarm_id"])["timestamp"].size()
    cnt = {(e, r): n for (e, r), n in g.items()}

    def tier(e, r):
        n = cnt[(e, r)]
        return "core" if n >= CORE_MIN else ("semi" if n >= SEMI_MIN else "novel")

    a["tier"] = [tier(e, r) for e, r in zip(a["entity"], a["alarm_id"])]
    a.drop(columns=["day"]).to_parquet(OUT_TAGGED, index=False)

    print(f"[m2] alarm tiers: "
          f"core={ (a.tier=='core').sum():,}  "
          f"semi={(a.tier=='semi').sum():,}  "
          f"novel={(a.tier=='novel').sum():,}  "
          f"(pairs: {sum(1 for v in cnt.values() if v>=CORE_MIN)} core / "
          f"{sum(1 for v in cnt.values() if SEMI_MIN<=v<CORE_MIN)} semi / "
          f"{sum(1 for v in cnt.values() if v<SEMI_MIN)} novel)")

    # 2) 事件窗口：semi + novel 触发
    eff = a[a["tier"].isin(["semi", "novel"])].sort_values("timestamp").reset_index(drop=True)
    ts = eff["timestamp"].values
    wid, start_idx = [0], 0
    for i in range(1, len(ts)):
        if ts[i] - ts[i-1] <= GAP_MS and ts[i] - ts[start_idx] <= MAX_DUR_MS:
            wid.append(wid[-1])
        else:
            wid.append(wid[-1] + 1)
            start_idx = i
    eff["w"] = wid

    rows = []
    for w_id, grp in eff.groupby("w"):
        st_ms, ed_ms = int(grp["timestamp"].min()), int(grp["timestamp"].max())
        in_win = a[(a["timestamp"] >= st_ms) & (a["timestamp"] <= ed_ms)]
        rows.append({"window_id": w_id,
                     "st_ms": st_ms, "ed_ms": ed_ms,
                     "minutes": (ed_ms - st_ms) // 60_000 + 1,
                     "n_alarms": len(grp),
                     "n_all_alarms": len(in_win),
                     "n_entities": in_win["entity"].nunique(),
                     "n_templates": in_win["template_id"].nunique()})
    out = pd.DataFrame(rows)
    out.to_parquet(OUT_WINDOWS, index=False)

    dur = out["minutes"]
    print(f"[m2] detected {len(out)} incident windows "
          f"(median {dur.median():.0f} min, max {dur.max()} min)")
    print(f"[m2] window list (first 8):")
    for _, r in out.head(8).iterrows():
        st = pd.to_datetime(r.st_ms, unit="ms")
        print(f"  #{r.window_id} {st} ({r.minutes} min) "
              f"trig={r.n_alarms} all={r.n_all_alarms} ent={r.n_entities}")
    return out


if __name__ == "__main__":
    run()
