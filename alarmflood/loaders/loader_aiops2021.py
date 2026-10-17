# -*- coding: utf-8 -*-
"""
loader_aiops2021.py — AIOps 2021(银行场景)数据 → 统一告警格式

数据事实（2026-10-14 实测确认）：
- 该数据集没有现成告警文件，只有 metric / logs / trace 三类原料
- 真实运维中告警由监控系统对"指标越限"产生，因此本 loader 用阈值规则从 metric 生成告警流
- 根因标注在 aiops21_groundtruth.csv（service 列 = 根因实体，165 个故障，109 train / 44 test）

输出：
- data/alarm.parquet        统一格式：timestamp(ms)/alarm_id/content/entity/severity
- results/fault_windows.parquet 故障窗口：id/service/anomaly_type/st/ed/data_type
"""
import os
import pandas as pd
from pathlib import Path

# 数据根目录解析（三种机器都能跑，互不影响）：
#   1) 环境变量 AIOPS2021_RAW 显式指定
#   2) 仓库同级的 aic_data/...（本机解压后的位置）
#   3) 回退到 E:/aic_data/AIOps2021（队友机器上的既有路径）
def _resolve_raw() -> Path:
    if os.environ.get("AIOPS2021_RAW"):
        return Path(os.environ["AIOPS2021_RAW"])
    local = (Path(__file__).parents[3] / "aic_data" / "raw" / "aiops2021" /
             "aiops2021-main" / "AIOps2021")
    if local.exists():
        return local
    return Path("E:/aic_data/AIOps2021")


RAW = _resolve_raw()
OUT_ALARMS = Path(__file__).parents[1] / "data" / "alarm.parquet"
OUT_WINDOWS = Path(__file__).parents[1] / "results" / "fault_windows.parquet"

DAYS = ["0304", "0305", "0306", "0307", "0309", "0310", "0312", "0323", "0324", "0325"]

# 告警规则：(kpi_name, 阈值, 告警内容模板, 严重度)
# 阈值参考 zabbix 常见默认值 + 对数据分布的实测校准（见 results/data_choice.md）
RULES = [
    ("OSLinux-CPU_CPU_CPUCpuUtil", 90.0, "CPU使用率{v}%超过阈值90%", 2),
    ("OSLinux-CPU_CPU_CPUUserTime", 55.0, "用户态CPU时间{v}%异常升高", 3),
    ("OSLinux-OSLinux_MEMORY_MEMORY_MEMUsedMemPerc", 90.0, "内存使用率{v}%超过阈值90%", 2),
    ("OSLinux-OSLinux_MEMORY_MEMORY_NoCacheMemPerc", 90.0, "无缓存内存使用率{v}%超过阈值90%", 3),
    ("OSLinux-OSLinux_LOCALDISK_LOCALDISK-sda_DSKPercentBusy", 90.0, "磁盘sda繁忙率{v}%超过阈值90%", 2),
    ("OSLinux-OSLinux_LOCALDISK_LOCALDISK-sdb_DSKPercentBusy", 90.0, "磁盘sdb繁忙率{v}%超过阈值90%", 2),
    ("OSLinux-OSLinux_LOCALDISK_LOCALDISK-sdc_DSKPercentBusy", 90.0, "磁盘sdc繁忙率{v}%超过阈值90%", 2),
    # ↓ 以下 3 条在本数据集上为「空转规则」：KPI 存在但取值从未越限（实测见 D3 核查）
    #   NETBandwidthUtil 全期 max=0.03%  /  SWPTotSwapUsedPercent 全期 max=0.00%
    #   FSInodeUsedPercent 亦未触发。保留原因：规则集取自 Zabbix 社区默认值，未针对本
    #   数据集调参（这正是「规则先验定死」的证据）；换真实环境即会生效。
    ("OSLinux-OSLinux_FILESYSTEM_-_FSInodeUsedPercent", 90.0, "文件系统Inode使用率{v}%超过阈值90%", 2),
    ("OSLinux-OSLinux_NETWORK_ens160_NETBandwidthUtil", 90.0, "网卡ens160带宽利用率{v}%超过阈值90%", 2),
    ("OSLinux-OSLinux_SWAP_SWAP_SWPTotSwapUsedPercent", 90.0, "Swap使用率{v}%超过阈值90%", 3),
]
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


def generate_alarms() -> pd.DataFrame:
    """逐日读取 metric，套用阈值规则 → 统一告警表"""
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
    alarms = pd.concat(frames, ignore_index=True).sort_values("timestamp")
    alarms = alarms.reset_index(drop=True)

    # 重复报警抑制（对应工业报警治理的 chattering/duplicate suppression）：
    # 同 (entity, alarm_id) 在 SUPPRESS_WINDOW_S 内只保留首条
    raw_n = len(alarms)
    alarms = suppress_duplicates(alarms)
    print(f"[loader] duplicate suppression: {raw_n:,} -> {len(alarms):,} "
          f"(rate {(1 - len(alarms)/raw_n)*100:.1f}%, window={SUPPRESS_WINDOW_S}s)")

    alarms.to_parquet(OUT_ALARMS, index=False)
    print(f"[loader] generated {len(alarms):,} alarms from {len(DAYS)} days "
          f"({alarms['alarm_id'].nunique()} rules fired, "
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
