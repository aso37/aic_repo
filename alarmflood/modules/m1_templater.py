# -*- coding: utf-8 -*-
"""
m1_templater.py — M1 告警模板归并（Drain3）
输入：data/alarm.parquet
输出：results/alarms_with_template.parquet（新增 template_id / template 两列）

注意：drain3 必须配置数字掩码（MaskingInstruction），否则每条消息都会
成为独立模板（2026-10-14 实测踩坑）。用编程式配置，不依赖 drain3.ini。
"""
import pandas as pd
from drain3 import TemplateMiner
from drain3.masking import MaskingInstruction
from drain3.template_miner_config import TemplateMinerConfig
from pathlib import Path

DATA = Path(__file__).parents[1] / "data" / "alarm.parquet"
OUT = Path(__file__).parents[1] / "results" / "alarms_with_template.parquet"


def make_miner() -> TemplateMiner:
    cfg = TemplateMinerConfig()
    cfg.masking_instructions.append(MaskingInstruction(r"\d+(\.\d+)?", "NUM"))
    return TemplateMiner(None, cfg)


def run() -> pd.DataFrame:
    df = pd.read_parquet(DATA)
    miner = make_miner()

    tids, texts = [], {}
    for content in df["content"]:
        r = miner.add_log_message(content)
        tid = r["cluster_id"]
        tids.append(tid)
        if tid not in texts:
            texts[tid] = r["template_mined"]  # 数字已替换为 <NUM>

    df["template_id"] = tids
    df["template"] = df["template_id"].map(texts)
    OUT.parent.mkdir(exist_ok=True)
    df.to_parquet(OUT, index=False)

    n_tpl = df["template_id"].nunique()
    print(f"[m1] extracted {n_tpl} templates from {len(df):,} alarms")
    print("[m1] all templates:")
    for tpl, cnt in df["template"].value_counts().items():
        print(f"    '{tpl}'  count={cnt:,} ({cnt/len(df)*100:.1f}%)")
    print(f"[m1] compression_ratio = {n_tpl}/{len(df):,} = {n_tpl/len(df)*100:.2f}%")
    return df


if __name__ == "__main__":
    run()
