# loaders/loader_bank.py
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