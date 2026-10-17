# -*- coding: utf-8 -*-
"""功能测试执行器 —— 为《测试报告》产出真实结果（不是编的）。

用法：python tests_run.py
输出：results/test_run.md（机器生成的原始结果，供人工整理进测试报告）
"""
import hashlib
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import pandas as pd

ROOT = Path(__file__).parents[1] if "__file__" in globals() else Path(".")
ROOT = Path(r"D:\AI_Workspace\dsh_projects\AIC\aic_repo\alarmflood")
sys.path.insert(0, str(ROOT / "modules"))

RESULTS = []
ENV = {}


def rec(tc, name, expect, actual, ok, note=""):
    RESULTS.append({"tc": tc, "name": name, "expect": expect,
                    "actual": actual, "ok": bool(ok), "note": note})
    print(f"  [{'PASS' if ok else 'FAIL'}] {tc} {name}")


def wid_of(label: str) -> int:
    return int(str(label).split("·")[0].strip().lstrip("#"))


def find_option(at, wid):
    for o in at.sidebar.selectbox[0].options:
        if wid_of(o) == wid:
            return o
    return None


# ===========================================================================
print("=" * 78)
print("环境")
print("=" * 78)
import importlib.metadata as md  # noqa: E402
import platform  # noqa: E402

for pkg in ["pandas", "numpy", "pyarrow", "streamlit", "matplotlib", "drain3", "openai"]:
    try:
        ENV[pkg] = md.version(pkg)
    except Exception:
        ENV[pkg] = "?"
ENV["python"] = platform.python_version()
ENV["system"] = f"{platform.system()} {platform.release()}"
for k, v in ENV.items():
    print(f"  {k:12s} {v}")

floods = pd.read_parquet(ROOT / "results" / "flood_windows.parquet")
have = {int(p.stem.split("_")[1]) for p in (ROOT / "results").glob("reports/window_*.json")}
ENV["n_windows"] = len(floods)
ENV["n_reports"] = len(have)

# ===========================================================================
print()
print("=" * 78)
print("A. 页签一 · 洪水窗口详情")
print("=" * 78)
from streamlit.testing.v1 import AppTest  # noqa: E402

APP = str(ROOT / "app.py")

# --- TC-1.1 正常流：窗口 #137（告警量最大，5 条新发） ---
at = AppTest.from_file(APP, default_timeout=180)
at.run()
opt137 = find_option(at, 137)
at.sidebar.selectbox[0].select(opt137).run()
e = list(at.exception)
n_metric = len(at.metric)
titles = [t.value for t in at.title]
succ = [s.value for s in at.success]
# 语义断言（不绑定精确文案）：界面改文案不应误报。
# 旧文案「新发告警实体（判别性最强）：…」/ 新文案「本窗口发出**新发告警**的实体：…」
has_novel_msg = any("新发告警" in s for s in succ)
rec("TC-1.1", "页签一 正常流（窗口 #137）",
    "无异常；≥4 个窗口指标 metric；出现提及「新发告警」的提示",
    f"异常={len(e)}；metric={n_metric}；success={succ[:1]}",
    (not e) and n_metric >= 4 and has_novel_msg)

# --- TC-1.2 边界：窗口 #42（novel=0，证据弱） ---
warns = []
try:
    at2 = AppTest.from_file(APP, default_timeout=180)
    at2.run()
    o42 = find_option(at2, 42)
    at2.sidebar.selectbox[0].select(o42).run()
    e2 = list(at2.exception)
    warns = [w.value for w in at2.warning]
    weak_msg = any("无新发告警" in w for w in warns)
    ok2 = (not e2) and weak_msg
    actual2 = f"异常={len(e2)}；warning={warns[:1]}"
except Exception as ex:  # noqa: BLE001
    ok2, actual2 = False, f"执行失败: {type(ex).__name__}: {ex}"
rec("TC-1.2", "页签一 边界：无新发告警（窗口 #42）",
    "无异常；出现「本窗口无新发告警…证据弱」警告", actual2, ok2)

# --- TC-1.3 边界：窗口 #0（仅 1 条告警、1 实体） ---
try:
    at3 = AppTest.from_file(APP, default_timeout=180)
    at3.run()
    o0 = find_option(at3, 0)
    at3.sidebar.selectbox[0].select(o0).run()
    e3 = list(at3.exception)
    rec("TC-1.3", "页签一 边界：极小输入（窗口 #0，1 条告警）",
        "无异常，界面可渲染",
        f"异常={len(e3)}；metric={len(at3.metric)}", not e3)
except Exception as ex:  # noqa: BLE001
    rec("TC-1.3", "页签一 边界：极小输入（窗口 #0）", "无异常",
        f"执行失败: {type(ex).__name__}", False)

# ===========================================================================
print()
print("=" * 78)
print("B. 页签二 · 处置单与派单")
print("=" * 78)

# --- TC-2.1 正常流：窗口 #137 有处置单，且可派单 ---
try:
    at4 = AppTest.from_file(APP, default_timeout=180)
    at4.run()
    at4.sidebar.selectbox[0].select(find_option(at4, 137)).run()
    e4 = list(at4.exception)
    n_btn = len(at4.button)
    btn_label = at4.button[0].label if n_btn else "(无按钮)"
    n_dl = len(at4.get("download_button"))
    # 点击派单，并核对**工单创建**的具体反馈（不是随便一个 success）
    click_err, click_msgs, click_md = [], [], []
    if n_btn:
        at4.button[0].click().run()
        click_err = list(at4.exception)
        click_msgs = [s.value for s in at4.success]
        click_md = [m.value for m in at4.markdown]
    ticket_created = any("工单已创建" in m for m in click_msgs)
    fields = [k for k in ("责任团队", "优先级", "关联变更")
              if any(k in m for m in click_md)]
    ok4 = (not e4) and (not click_err) and n_btn > 0 and ticket_created and len(fields) >= 2
    rec("TC-2.1", "页签二 正常流 + 派单（窗口 #137）",
        "无异常；存在「派单」按钮与下载按钮；点击后出现「工单已创建」，"
        "且工单卡片含责任团队/优先级/关联变更",
        f"异常={len(e4)}/点击后异常={len(click_err)}；按钮={n_btn}（{btn_label[:16]}…）；"
        f"下载按钮={n_dl}；工单已创建={ticket_created}；"
        f"工单字段命中={fields}", ok4)
except Exception as ex:  # noqa: BLE001
    rec("TC-2.1", "页签二 正常流 + 派单（窗口 #137）", "无异常",
        f"执行失败: {type(ex).__name__}", False)

# --- TC-2.2 边界：窗口 #234 无处置单 → 优雅降级 ---
try:
    at5 = AppTest.from_file(APP, default_timeout=180)
    at5.run()
    o234 = find_option(at5, 234)
    at5.sidebar.selectbox[0].select(o234).run()
    e5 = list(at5.exception)
    w5 = [w.value for w in at5.warning]
    # 语义断言：只需要「提到处置单缺失」+「不崩」，不绑定精确文案。
    # 旧文案「本窗口暂无处置单（M4 结果未落盘）…」/
    # 新文案「本窗口既无处置单也无大模型排序结果…」
    degraded = any(("处置单" in w) and ("无" in w) for w in w5)
    rec("TC-2.2", "页签二 边界：无处置单（窗口 #234）→ 优雅降级",
        "无异常；出现说明「无处置单」的降级提示，不崩溃",
        f"异常={len(e5)}；warning={[x[:46] for x in w5[:1]]}",
        (not e5) and degraded)
except Exception as ex:  # noqa: BLE001
    rec("TC-2.2", "页签二 边界：无处置单（窗口 #234）", "无异常+降级提示",
        f"执行失败: {type(ex).__name__}", False)

# ===========================================================================
print()
print("=" * 78)
print("C. 主链路模块（M1–M4 + 融合）")
print("=" * 78)
import m3_rank  # noqa: E402
import m3_fusion  # noqa: E402

alarms = pd.read_parquet(ROOT / "results" / "alarms_tagged.parquet")
faults = pd.read_parquet(ROOT / "results" / "fault_windows.parquet")

# TC-3.1 主链路产物完整性
need = ["alarms_with_template.parquet", "alarms_tagged.parquet", "flood_windows.parquet"]
miss = [f for f in need if not (ROOT / "results" / f).exists()]
rec("TC-3.1", "主链路产物齐全（M1/M2 输出）",
    f"{len(need)} 个 parquet 均存在", f"缺失={miss}", not miss)

# TC-3.2 三级分诊
tiers = alarms.tier.value_counts().to_dict()
rec("TC-3.2", "M2 三级分诊标注完整",
    "tier 只含 core/semi/novel，无空值",
    f"分布={tiers}", set(tiers) <= {"core", "semi", "novel"} and alarms.tier.notna().all())

# TC-3.3 排序分母与协议一致
r_rule = m3_rank.evaluate(alarms, floods, faults, "rule")
r_freq = m3_rank.evaluate(alarms, floods, faults, "freq")
rec("TC-3.3", "M3 排序：freq/rule 分母一致（同一评测协议）",
    "n_eval 相同",
    f"freq n={r_freq['n_eval']}, rule n={r_rule['n_eval']}；"
    f"rule Top-1={r_rule['top1']}/{r_rule['n_eval']} Top-3={r_rule['top3']}/{r_rule['n_eval']}",
    r_freq["n_eval"] == r_rule["n_eval"] and r_rule["n_eval"] > 0)

# TC-3.4 融合端点自检
try:
    llm, _model, _ = m3_fusion.load_llm()
    wins = m3_fusion.select_windows(alarms, floods, faults)
    g1 = m3_fusion.eval_at(wins, llm, 1.0)
    ok1 = (g1[0], g1[1]) == (r_rule["top1"], r_rule["top3"])
    okp, bad = m3_fusion.llm_order_preserved(wins, llm)
    rec("TC-3.4", "M3 融合：端点自检（α=1 复现 rule；α=0 保持 LLM 原序）",
        "两项均为真",
        f"α=1 得({g1[0]},{g1[1]}) 期望({r_rule['top1']},{r_rule['top3']})；"
        f"α=0 原序保持={okp}（反例 {len(bad)} 个）", ok1 and okp)
except Exception as ex:  # noqa: BLE001
    rec("TC-3.4", "M3 融合：端点自检", "两项均为真",
        f"执行失败: {type(ex).__name__}: {ex}", False)

# TC-3.5 M4 处置单 schema 契约
import json  # noqa: E402
rep_files = sorted((ROOT / "results" / "reports").glob("window_*.json"))
KEYS = ["window", "root_causes", "ticket", "disclaimer"]
bad_schema = []
for p in rep_files[:200]:
    d = json.loads(p.read_text(encoding="utf-8-sig"))
    if not all(k in d for k in KEYS):
        bad_schema.append(p.name)
rec("TC-3.5", "M4 处置单 schema 契约（window/root_causes/ticket/disclaimer）",
    "抽查的处置单均含 4 个必需字段",
    f"抽查 {min(len(rep_files),200)} 份，缺字段 {len(bad_schema)} 份",
    not bad_schema and len(rep_files) > 0)

# TC-3.6 M4 反幻觉：独立复核处置单的根因实体是否真的来自该窗口的告警证据
# 说明：幻觉校验在生成期是「门槛」（validate_entities + 重试 + 降级），
#       并不在每份处置单里落一个"校验结果"字段。因此这里做**独立复核**：
#       拿处置单里的根因实体，去和原始告警流比对，看有没有编造出来的实体。
hallu, checked, no_evi = [], 0, 0
for p in rep_files[:200]:
    d = json.loads(p.read_text(encoding="utf-8-sig"))
    wid = int(d["window_id"])
    row = floods[floods.window_id == wid]
    if row.empty:
        continue
    w = row.iloc[0]
    sub = alarms[(alarms.timestamp >= w.st_ms) & (alarms.timestamp <= w.ed_ms)]
    valid = set(sub.entity)
    checked += 1
    for rc in d.get("root_causes", []):
        if not rc.get("evidence"):
            no_evi += 1
        if rc.get("entity") not in valid:
            hallu.append((wid, rc.get("entity")))
rec("TC-3.6", "M4 反幻觉：处置单根因实体均来自窗口告警证据（独立复核）",
    "复核的处置单中根因实体 100% 出现在该窗口告警实体集内，且每条根因都带 evidence",
    f"复核 {checked} 份；编造实体 {len(hallu)} 个{hallu[:3]}；无 evidence 的根因 {no_evi} 条",
    checked > 0 and not hallu and no_evi == 0)

# TC-3.7 抽检记录存在
spot = ROOT / "results" / "reports" / "_spotcheck.md"
spot_txt = spot.read_text(encoding="utf-8") if spot.exists() else ""
rec("TC-3.7", "M4 抽检记录存在并写明反幻觉结论",
    "_spotcheck.md 存在且含「无幻觉」统计",
    f"存在={spot.exists()}；含「无幻觉」={'无幻觉' in spot_txt}", spot.exists() and "无幻觉" in spot_txt)

# ===========================================================================
print()
print("=" * 78)
print("D. 幂等与可重复性")
print("=" * 78)

# TC-4.1 AUTO-TABLE 幂等：重跑 m3_rank 后 experiments.md 的哈希应不变
exp = ROOT / "results" / "experiments.md"
before = hashlib.sha1(exp.read_bytes()).hexdigest()
import subprocess  # noqa: E402
py = sys.executable
out = subprocess.run([py, str(ROOT / "modules" / "m3_rank.py")],
                     capture_output=True, text=True, cwd=str(ROOT), timeout=600)
after = hashlib.sha1(exp.read_bytes()).hexdigest()
rec("TC-4.1", "对比表 AUTO-TABLE 幂等（重跑 m3_rank 不改变文件）",
    "重跑前后 experiments.md 哈希一致（人工分析不被覆盖）",
    f"before={before[:12]} after={after[:12]}；"
    f"哈希{'一致' if before == after else '不一致'}；退出码={out.returncode}",
    before == after)
if before != after:
    print("  [说明] 哈希变化，可用 git checkout 恢复")

# TC-4.2 回放自检（复用 B 的脚本，只读取其报告）
rc = ROOT / "results" / "replay_check.md"
done = rc.exists()
txt = rc.read_text(encoding="utf-8") if done else ""
rec("TC-4.2", "回放模式自检报告存在且总体 PASS",
    "results/replay_check.md 存在且含「总体：PASS」",
    f"存在={done}；含总体PASS={'总体：PASS' in txt}", done and "总体：PASS" in txt)

# ===========================================================================
print()
print("=" * 78)
print("汇总")
print("=" * 78)
npass = sum(1 for r in RESULTS if r["ok"])
total = len(RESULTS)
print(f"  用例 {total} 条，通过 {npass}，失败 {total - npass}")

lines = ["# 功能测试执行结果（机器生成）", "",
         "> 由 `python tests_run.py` 生成，供人工整理进《测试报告》。", "",
         "## 环境", "", "| 项 | 值 |", "|---|---|"]
for k, v in ENV.items():
    lines.append(f"| {k} | {v} |")
lines += ["", "## 用例结果", "",
          "| 编号 | 用例 | 预期 | 实际 | 结果 |", "|---|---|---|---|---|"]
for r in RESULTS:
    lines.append(f"| {r['tc']} | {r['name']} | {r['expect']} | "
                 f"{r['actual']} | {'PASS' if r['ok'] else 'FAIL'} |")
lines += ["", f"**合计：{npass}/{total} 通过**", ""]
(ROOT / "results" / "test_run.md").write_text("\n".join(lines), encoding="utf-8")
print(f"\n  已写入 {ROOT / 'results' / 'test_run.md'}")
