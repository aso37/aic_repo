# -*- coding: utf-8 -*-
"""
L2 试点数据评测：把云服务器采回的真实告警流跑通 M1→M2→M3，并对已知真值打分。

前置：
  1) 云服务器执行 pilot/run_pilot.sh
  2) scp 回 data/pilot/alerts.jsonl 与 data/pilot/injections.json
  3) 本地执行 python loaders/loader_prometheus.py
运行：
  python scripts/run_pilot_eval.py

产出：results/pilot/pilot_report.md（可直接进答辩材料与测试报告）
"""
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "modules"))
import m1_templater  # noqa: E402
import m2_window  # noqa: E402
import m3_rank  # noqa: E402

PILOT = ROOT / "results" / "pilot"
PILOT.mkdir(parents=True, exist_ok=True)

# ---- M1 模板归并 ----
m1_templater.DATA = ROOT / "data" / "alarm_pilot.parquet"
m1_templater.OUT = PILOT / "alarms_with_template.parquet"
a1 = m1_templater.run()

# ---- M2 事件窗口 ----
m2_window.DATA = PILOT / "alarms_with_template.parquet"
m2_window.OUT_WINDOWS = PILOT / "flood_windows.parquet"
m2_window.OUT_TAGGED = PILOT / "alarms_tagged.parquet"
m2_window.run()

alarms = pd.read_parquet(PILOT / "alarms_tagged.parquet")
floods = pd.read_parquet(PILOT / "flood_windows.parquet")
faults = pd.read_parquet(ROOT / "results" / "fault_windows_pilot.parquet")

# ---- M3 排序评测（真值来自 injections.json）----
results = {}
for mode in ("freq", "rule"):
    r = m3_rank.evaluate(alarms, floods, faults, mode)
    results[mode] = r
    n = max(r["n_eval"], 1)
    print(f"[pilot] {mode:5s}: Top-1={r['top1']}/{r['n_eval']} ({r['top1']/n*100:.0f}%)  "
          f"Top-3={r['top3']}/{r['n_eval']} ({r['top3']/n*100:.0f}%)")

# ---- 根因排名细节（答辩案例素材）----
detail = ""
w = faults.iloc[0]
fl = floods[(floods.st_ms <= w.ed_ms + 600_000) & (floods.ed_ms >= w.st_ms - 600_000)]
if len(fl):
    st, ed = fl.st_ms.min(), fl.ed_ms.max()
    wdf = alarms[(alarms.timestamp >= st) & (alarms.timestamp <= ed)]
    scores = m3_rank.rank_entities(wdf, "rule").sort_values(ascending=False)
    print(f"\n[pilot] 故障窗口 {pd.to_datetime(st, unit='ms')} ~ {pd.to_datetime(ed, unit='ms')}"
          f"（真值根因 = {w.service}）")
    for i, (ent, sc) in enumerate(scores.items(), 1):
        sub = wdf[wdf.entity == ent]
        mark = "  ← 真值根因" if ent == w.service else ""
        print(f"   {i}. {ent:12s} score={sc:.3f}  告警{len(sub)}条"
              f"（novel {(sub.tier=='novel').sum()}）首现"
              f" {pd.to_datetime(sub.timestamp.min(), unit='ms'):%H:%M:%S}{mark}")
        detail += (f"| {i} | {ent} | {sc:.3f} | {len(sub)} | "
                   f"{(sub.tier=='novel').sum()} |{'✅' if ent == w.service else ''} |\n")

# ---- 关键指标 ----
n_alarms = len(alarms)
n_tpl = alarms.template_id.nunique()
dup_ratio = 1 - len(alarms.drop_duplicates(["entity", "alarm_id", "template_id"])) / n_alarms

report = f"""# L2 自建试点评测报告（真实监控栈）

> 数据来源：云服务器 Docker 监控栈（Prometheus + Alertmanager + 自建微服务），
> 故障由人工注入，真值已知。本文件由 `scripts/run_pilot_eval.py` 自动生成。

## 1. 试点环境

- 拓扑：`svc-api-1/2/3 → svc-db`，外加 node-exporter 采集宿主机
- 监控：Prometheus（5s 抓取）+ 10 条告警规则 + Alertmanager（repeat_interval=30s）
- 注入：`svc-db` 进程 CPU 饱和（4 线程 × 180s）
- 真值根因：**{w.service}**（{w.anomaly_type} 型）

## 2. 告警流规模

| 指标 | 数值 |
|---|---|
| 告警通知条数 | {n_alarms} |
| 涉及实体数 | {alarms.entity.nunique()} |
| 触发规则数 | {alarms.alarm_id.nunique()} |
| 归并后模板数 | {n_tpl} |
| **模板压缩比** | **{(1 - n_tpl / max(n_alarms,1)) * 100:.1f}%** |
| 重复报警占比（同实体同规则同模板的重复通知） | {dup_ratio * 100:.1f}% |
| 检测到的事件窗口数 | {len(floods)} |

## 3. 根因排序结果

| 方法 | Top-1 | Top-3 | 评测窗口数 |
|---|---|---|---|
| 频次基线 | {results['freq']['top1']}/{results['freq']['n_eval']} | {results['freq']['top3']}/{results['freq']['n_eval']} | {results['freq']['n_eval']} |
| 规则路线（M3） | {results['rule']['top1']}/{results['rule']['n_eval']} | {results['rule']['top3']}/{results['rule']['n_eval']} | {results['rule']['n_eval']} |

### 故障窗口内的根因候选排名（rule 路线）

| 排名 | 实体 | 得分 | 告警条数 | 其中新发 | 命中真值 |
|---|---|---|---|---|---|
{detail}
## 4. 结论要点

1. 系统在**真实监控栈产生的真实告警**上完成接入，新增一个 loader 即完成适配，下游模块零改动。
2. 真实告警含大量**重复通知**（Alertmanager 周期性重复 firing），模板归并将其压缩为少量模板。
3. 根因定位结果见上表；若为命中，可说明该场景下的证据结构（见"观察"段落）。
"""
(PILOT / "pilot_report.md").write_text(report, encoding="utf-8")
print(f"\n[pilot] 报告已写入 {PILOT / 'pilot_report.md'}")
