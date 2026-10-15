# -*- coding: utf-8 -*-
"""
check_replay.py — 回放模式可用性验收（D5 · B 角色）

在线演示采用「回放模式」：界面只读预计算产物（parquet + 处置单 JSON），
运行时零 LLM 调用，评审点开任意窗口都不依赖 API 可用性/限流（对应开工方案要求 4）。

本脚本用 streamlit 官方 AppTest 实跑看板，逐项验收：
  1. 静态检查：app.py 不 import 任何 LLM 相关模块（运行时零 API 依赖）
  2. 默认加载无异常
  3. 点击「派单」按钮无异常（human-in-loop 演示路径）
  4. 选到无处置单的窗口能优雅降级、不崩溃
并统计处置单覆盖情况，报告写入 results/replay_check.md

用法：python scripts/check_replay.py
"""
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).parents[1]
REPORTS = ROOT / "results" / "reports"
OUT = ROOT / "results" / "replay_check.md"
BANNED = ["openai", "m3_llm", "OPENAI_API_KEY"]


def static_check() -> list:
    """命中即说明界面运行时引入了 LLM 依赖。"""
    src = (ROOT / "app.py").read_text(encoding="utf-8")
    return [m for m in BANNED if m in src]


def wid_of(label: str) -> int:
    """把侧边栏选项文本 '#137 · 03-15 12:00 · 9条/19分钟 · 12实体' 解析出窗口号。"""
    return int(label.split("·")[0].strip().lstrip("#"))


def main():
    from streamlit.testing.v1 import AppTest

    banned = static_check()
    floods = pd.read_parquet(ROOT / "results" / "flood_windows.parquet")
    have = {int(p.stem.split("_")[1]) for p in REPORTS.glob("window_*.json")}
    total = len(floods)
    top = floods.sort_values("n_alarms", ascending=False)

    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=180)
    at.run()
    err_load = list(at.exception)

    err_click = []
    if at.button:
        at.button[0].click().run()
        err_click = list(at.exception)

    err_none = []
    opts = at.sidebar.selectbox[0].options
    no_rep = [o for o in opts if wid_of(o) not in have]
    if no_rep:
        at2 = AppTest.from_file(str(ROOT / "app.py"), default_timeout=180)
        at2.run()
        at2.sidebar.selectbox[0].select(no_rep[0]).run()
        err_none = list(at2.exception)

    ok = not banned and not err_load and not err_click and not err_none
    lines = [
        "# 回放模式可用性验收（D5 · B 角色）", "",
        "> 由 `python scripts/check_replay.py` 生成。回放模式 = 界面只读预计算产物、运行时零 LLM 调用。",
        "",
        "| 检查项 | 结果 |", "|---|---|",
        f"| app.py 无 LLM 依赖（禁词 {BANNED}） | {'PASS' if not banned else 'FAIL: ' + str(banned)} |",
        f"| 默认加载无异常 | {'PASS' if not err_load else 'FAIL: ' + str(err_load)[:120]} |",
        f"| 点击「派单」无异常 | {'PASS' if not err_click else 'FAIL: ' + str(err_click)[:120]} |",
        f"| 无处置单窗口优雅降级 | {'PASS' if not err_none else 'FAIL: ' + str(err_none)[:120]} |",
        "", "## 处置单覆盖", "",
        f"- 洪水窗口总数：{total}",
        f"- 已有处置单：{len(have)}（{len(have) / total * 100:.0f}%）",
        f"- 侧边栏前 15 个窗口均已有处置单：{all(w in have for w in top.head(15).window_id)}",
        "", f"**总体：{'PASS' if ok else 'FAIL'}**",
    ]
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    print(f"\n[check_replay] {'PASS' if ok else 'FAIL'} -> {OUT}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
