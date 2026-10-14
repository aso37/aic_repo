# -*- coding: utf-8 -*-
"""
loader_aiops2021.py — AIOps 2021(银行场景)数据 → 统一告警格式

数据事实（2026-10-14 实测确认）：
- 该数据集没有现成告警文件，只有 metric / logs / trace 三类原料
- 真实运维中告警由监控系统对"指标越限"产生，因此本 loader 用阈值规则从 metric 生成告警流
- 根因标注在 aiops21_groundtruth.csv（service 列 = 根因实体）
  实测有效窗口 159 个：train 112 / test 47

输出：
- data/alarm.parquet          统一格式（已做重复抑制）：timestamp(ms)/alarm_id/content/entity/severity
- data/alarms_raw.parquet     同一格式但**抑制前**的原始流，供 D3「截断喂」基线实验用
- results/fault_windows.parquet 故障窗口：id/service/anomaly_type/st/ed/data_type

环境变量：
- AIOPS_RAW  原始数据根目录，默认 E:/aic_data/AIOps2021
  （原来硬编码在代码里，换机器就崩；D7 换机验证需要它能被覆盖）
"""
import os
from pathlib import Path

import pandas as pd

RAW = Path(os.environ.get("AIOPS_RAW", "E:/aic_data/AIOps2021"))
OUT_ALARMS = Path(__file__).parents[1] / "data" / "alarm.parquet"
OUT_ALARMS_RAW = Path(__file__).parents[1] / "data" / "alarms_raw.parquet"
OUT_WINDOWS = Path(__file__).parents[1] / "results" / "fault_windows.parquet"

DAYS = ["0304", "0305", "0306", "0307", "0309", "0310", "0312", "0323", "0324", "0325"]

# 告警规则：(kpi_name, 阈值, 告警内容模板, 严重度)
#
# 【2026-10-14 规则校准，结案记录】
# 原方案对全部 10 个 KPI 统一使用阈值 90，实测只有 7 条触发。
# 用真实数据（10 天 metric）逐 KPI 校准后查明：**KPI 名称全部正确（10/10 存在），
# 问题出在阈值**——各 KPI 的正常范围差异极大，统一 90 不可行：
#
#   KPI                                 常态      实测 max   原阈值90  处置
#   MEMUsedMemPerc                      84%       98.0      可触发    保留
#   NoCacheMemPerc                      45.7%     92.9      可触发    保留
#   CPUCpuUtil                          25.7%     97.3      可触发    保留
#   CPUUserTime                         25.1%     96.2      阈55 可触发 保留
#   sdc_DSKPercentBusy                  1.8%      100.0     可触发    保留
#   sda_DSKPercentBusy                  0%        100.4     可触发    保留
#   FSInodeUsedPercent                  16.4%     29.93     不可达 →  阈值改 20
#   sdb_DSKPercentBusy                  0%        36.0      不可达 →  阈值改 30
#   ens160_NETBandwidthUtil             0(0~1比例) 0.032     不可达 →  换 TCP-FIN-WAIT
#   SWPTotSwapUsedPercent               恒为 0     0.0       不可达 →  删除（系统未启用 Swap）
#
# 其中最重要的收获：换成 TCP-FIN-WAIT 后，NETWORK 型故障窗口的覆盖率从
# 20/45 提升到 32/45（71%），而 CPU/DISK/MEMORY/JVM 全部 0% —— 判别性极强。
# 复核脚本：scripts/probe_rule_coverage.py
RULES = [
    ("OSLinux-CPU_CPU_CPUCpuUtil", 90.0, "CPU使用率{v}%超过阈值90%", 2),
    ("OSLinux-CPU_CPU_CPUUserTime", 55.0, "用户态CPU时间{v}%异常升高", 3),
    ("OSLinux-OSLinux_MEMORY_MEMORY_MEMUsedMemPerc", 90.0, "内存使用率{v}%超过阈值90%", 2),
    ("OSLinux-OSLinux_MEMORY_MEMORY_NoCacheMemPerc", 90.0, "无缓存内存使用率{v}%超过阈值90%", 3),
    # [删] FSInodeUsedPercent：原阈值 90 不可达（实测 max=29.93 / 常态 16.4）。
    #      试过把阈值改到 20 让它生效，结果：触发 29,391 条 → 抑制后 6,276 条，
    #      全部落入 core 常驻层，成为新的背景噪声源，把 CPU 型的 Top-1 从 96% 拉到 79%；
    #      而 DISK 型毫无改善（2/11 → 2/17）。**净损失，故整条删除。**
    #      （这是个有记录的被否决方案，答辩可讲"我们试过并量化了副作用"）
    ("OSLinux-OSLinux_LOCALDISK_LOCALDISK-sda_DSKPercentBusy", 90.0, "磁盘sda繁忙率{v}%超过阈值90%", 2),
    # [改] 原阈值 90：实测该指标全程 max=36.0（常态 0），90 永远不可能达到。阈值改为 30。
    ("OSLinux-OSLinux_LOCALDISK_LOCALDISK-sdb_DSKPercentBusy", 30.0, "磁盘sdb繁忙率{v}%异常", 2),
    ("OSLinux-OSLinux_LOCALDISK_LOCALDISK-sdc_DSKPercentBusy", 90.0, "磁盘sdc繁忙率{v}%超过阈值90%", 2),
    # [换] 原为 ens160 NETBandwidthUtil（阈值 90）：实测该指标是 0~1 的比例，
    #      全程 max=0.032（即 3.2%），阈值 90 永远不可能达到；且该网络负载极低，无量纲可用性。
    #      换成 TCP-FIN-WAIT（TCP 半关闭连接数）：实测非零样本占比仅 1.18%，
    #      而在 NETWORK 型故障窗内 32/45（71%）出现非零，对其他故障类型 0% 命中 —— 判别性极强。
    ("OSLinux-OSLinux_NETWORK_NETWORK_TCP-FIN-WAIT", 0.0, "TCP FIN-WAIT 连接数{v}异常升高", 2),
    # [删] 原 SWPTotSwapUsedPercent（阈值 90）：实测该指标**全程恒为 0**
    #      （SWPTotSwapSize 也是 0，即该系统未启用 Swap），任何阈值都不可能触发。
]
# 阈值依据：对 10 天 metric 的实测分布校准（见 results/data_choices.md 与
# scripts/probe_rule_coverage.py）。原方案对全部 KPI 统一使用阈值 90，
# 但各 KPI 正常范围差异极大（内存常态 84%、inode 常态 16%、sdb 磁盘常态 0%），
# 统一阈值导致 3 条规则从未触发。
RULE_KPI = {r[0] for r in RULES}

# 重复报警抑制窗口（秒）：同实体同规则在该窗口内只保留首条
SUPPRESS_WINDOW_S = 600


def load_fault_windows() -> pd.DataFrame:
    """故障标注 → 统一窗口表

    注意：st_time/ed_time 是"数据时钟"的字符串，机器本地时区与其不一致，
    所以窗口的 epoch 一律以数据自带的 time 列（ms）为锚点推算，禁止用 .timestamp()
    """
    gt = pd.read_csv(RAW / "aiops21_groundtruth.csv", index_col=0)
    st_dt = pd.to_datetime(gt["st_time"])
    ed_dt = pd.to_datetime(gt["ed_time"])
    st_ms = gt["time"].astype("int64")                                # 数据时钟锚点
    ed_ms = st_ms + ((ed_dt - st_dt).dt.total_seconds() * 1000).astype("int64")
    win = pd.DataFrame({
        "id": gt["id"],
        "service": gt["service"],           # 根因实体（评测的 ground truth）
        "anomaly_type": gt["anomaly_type"], # CPU/DISK/MEMORY/NETWORK/JVM;MEMORY
        "st": st_dt,
        "ed": ed_dt,
        "st_ms": st_ms,
        "ed_ms": ed_ms,
        "data_type": gt["data_type"],       # train / test
    }).dropna(subset=["st", "ed"])
    win.to_parquet(OUT_WINDOWS, index=False)
    print(f"[loader] {len(win)} fault windows "
          f"(train={len(win[win.data_type=='train'])}, test={len(win[win.data_type=='test'])})")
    return win


def report_rule_fires(alarms: pd.DataFrame) -> None:
    """打印每条规则的触发次数 —— 未触发的规则会在这里直接暴露。

    这是 2026-10-14 审计后新增的检查：原代码只打印"n rules fired"，
    掩盖了"定义了 10 条、只跑了 7 条"这个事实。
    """
    fired = alarms["alarm_id"].value_counts()
    print("[loader] 逐规则触发次数（0 = 该规则从未触发，需检查 KPI 名/量纲）：")
    for kpi, thr, _tpl, _sev in RULES:
        n = int(fired.get(kpi, 0))
        flag = "   <-- 未触发！见 scripts/probe_rule_coverage.py" if n == 0 else ""
        print(f"    {n:>8,}  thr={thr:<5}  {kpi}{flag}")


def generate_alarms(save_raw: bool = True) -> pd.DataFrame:
    """逐日读取 metric，套用阈值规则 → 统一告警表

    save_raw=True 时额外输出 **抑制前** 的原始流 data/alarms_raw.parquet，
    供 D3「截断喂」基线实验使用（A 的当日交付物）。
    """
    print(f"[loader] AIOPS_RAW = {RAW}")
    frames = []
    for day in DAYS:
        f = RAW / "aiops2021-2" / day / "metric" / f"metric_{day}.csv"
        df = pd.read_csv(f, usecols=["timestamp", "cmdb_id", "kpi_name", "value"])
        df = df[df["kpi_name"].isin(RULE_KPI)]
        for kpi, thr, tpl, sev in RULES:
            hit = df[(df["kpi_name"] == kpi) & (df["value"] > thr)]
            if len(hit) == 0:
                continue
            frames.append(pd.DataFrame({
                "timestamp": hit["timestamp"].astype("int64") * 1000,  # s → ms
                "alarm_id": kpi,
                "content": [tpl.format(v=round(v, 1)) for v in hit["value"]],
                "entity": hit["cmdb_id"].astype(str),
                "severity": sev,
            }))
    if not frames:
        raise SystemExit(
            "[loader] 没有任何规则触发 —— 检查 AIOPS_RAW 路径与 KPI 命名"
            "（跑 scripts/probe_rule_coverage.py）")

    raw = pd.concat(frames, ignore_index=True).sort_values("timestamp")
    raw = raw.reset_index(drop=True)

    report_rule_fires(raw)

    # 抑制前的原始流：D3「截断喂」基线实验的输入
    if save_raw:
        raw.to_parquet(OUT_ALARMS_RAW, index=False)
        print(f"[loader] raw（抑制前）-> {OUT_ALARMS_RAW.name}  {len(raw):,} 条"
              f"   ← D3「截断喂」基线用")

    # 重复报警抑制（对应工业报警治理的 chattering/duplicate suppression）：
    # 同 (entity, alarm_id) 在 SUPPRESS_WINDOW_S 内只保留首条
    raw_n = len(raw)
    alarms = suppress_duplicates(raw)
    print(f"[loader] duplicate suppression: {raw_n:,} -> {len(alarms):,} "
          f"(rate {(1 - len(alarms)/raw_n)*100:.1f}%, window={SUPPRESS_WINDOW_S}s)")

    alarms.to_parquet(OUT_ALARMS, index=False)
    print(f"[loader] generated {len(alarms):,} alarms from {len(DAYS)} days "
          f"({alarms['alarm_id'].nunique()}/{len(RULES)} rules fired, "
          f"{alarms['entity'].nunique()} entities)")
    return alarms


def suppress_duplicates(alarms: pd.DataFrame) -> pd.DataFrame:
    """按 (entity, alarm_id) 分组，组内时间差超过抑制窗口才保留"""
    keep = []
    for (_, _), g in alarms.groupby(["entity", "alarm_id"], sort=False):
        last = -10**18
        for i, ts in enumerate(g["timestamp"]):
            if ts - last >= SUPPRESS_WINDOW_S * 1000:
                keep.append(g.index[i])
                last = ts
    return alarms.loc[sorted(keep)].reset_index(drop=True)


def sanity_check(alarms: pd.DataFrame, win: pd.DataFrame):
    """验证规则有效性：train 窗口内根因实体是否有告警。

    语义：抑制后的告警代表"通知"，其状态在抑制窗口内持续生效，
    因此窗口起点往前回看 SUPPRESS_WINDOW_S 也算覆盖。
    """
    a = alarms
    back = SUPPRESS_WINDOW_S * 1000
    hit = 0
    train = win[win["data_type"] == "train"]
    for _, w in train.iterrows():
        m = a[(a["timestamp"] >= w["st_ms"] - back) & (a["timestamp"] <= w["ed_ms"])
              & (a["entity"] == w["service"])]
        if len(m) > 0:
            hit += 1
    print(f"[loader] sanity: root-entity alarms present in {hit}/{len(train)} "
          f"train fault windows ({hit/len(train)*100:.0f}%)")


if __name__ == "__main__":
    win = load_fault_windows()
    alarms = generate_alarms()
    sanity_check(alarms, win)
