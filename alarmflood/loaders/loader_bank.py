# loaders/loader_bank.py
# 【模板，当前未使用】为 D6"真实脱敏告警"接入预留的骨架：
# AIOps2021 数据集没有现成告警文件（只有 metric/logs/trace），
# 因此主数据走 loader_aiops2021.py（从 metric 生成告警）。
# D6 拿到运维同学的真实告警 CSV 时，把本文件的 RAW 指向真实文件、
# 改好列名即可直接使用——这就是"新数据源 = 一个新 loader"的活例子。
import pandas as pd
from pathlib import Path

RAW = Path(__file__).parents[1] / "data" / "raw" / "bank" / "alarm.csv"  # 【改】真实文件名
OUT = Path(__file__).parents[1] / "data" / "alarm.parquet"

def load():
    df = pd.read_csv(RAW)
    # 【改】把右侧换成你的真实列名
    out = pd.DataFrame({
        "timestamp": pd.to_datetime(df["time"]).astype("int64") // 10**6,
        "alarm_id": df["alarm_id"],
        "content": df["content"].astype(str),
        "entity": df["entity"].astype(str),
        "severity": df["severity"],
    })
    out = out.sort_values("timestamp").reset_index(drop=True)
    out.to_parquet(OUT, index=False)
    print(f"[loader] loaded {len(out):,} alarms from bank")
    return out

if __name__ == "__main__":
    load()