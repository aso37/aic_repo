# -*- coding: utf-8 -*-
"""
AlarmFlood Copilot · 告警洪水治理值班台（M5 演示看板）
角色：C · 产品与交付
依赖：只读 results/ 下的 A/B 产物，不含业务计算逻辑
  - A：results/alarms_tagged.parquet、flood_windows.parquet、modules/m3_rank（规则路线）
  - B：results/llm/llm_rankings.json（LLM 根因排序路线）
  - B：results/reports/window_*.json（M4 处置单；未落盘时用 LLM 结果生成预览版）
  - 数据集标注 root_entity_truth 默认隐藏，仅侧边栏开关打开时用于离线核对
运行：G:\\anaconda3\\envs\\aic\\python.exe -m streamlit run app.py
      （或双击 run_dashboard.bat）
"""
import sys
import json
from pathlib import Path

import pandas as pd
import streamlit as st
import matplotlib.pyplot as plt

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "modules"))
from m3_rank import rank_entities  # noqa: E402  A 的规则排序路线

plt.rcParams["font.sans-serif"] = ["SimHei", "Microsoft YaHei"]
plt.rcParams["axes.unicode_minus"] = False

TIER_ZH = {"core": "常驻", "semi": "半常驻", "novel": "新发"}


# ---------- 数据加载（缓存：交互不重读文件） ----------
@st.cache_data(ttl=300)
def load_alarms():
    return pd.read_parquet(ROOT / "results" / "alarms_tagged.parquet")


@st.cache_data(ttl=300)
def load_floods():
    return pd.read_parquet(ROOT / "results" / "flood_windows.parquet")


@st.cache_data(ttl=300)
def load_llm():
    """B 的 LLM 根因排序；文件不存在返回 None"""
    p = ROOT / "results" / "llm" / "llm_rankings.json"
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


@st.cache_data(ttl=300)
def rank_window(st_ms: int, ed_ms: int) -> pd.DataFrame:
    """规则路线：调用 A 的 m3_rank，返回 Top-5"""
    alarms = load_alarms()
    wdf = alarms[(alarms.timestamp >= st_ms) & (alarms.timestamp <= ed_ms)]
    if wdf.empty:
        return pd.DataFrame()
    scores = rank_entities(wdf, "rule").sort_values(ascending=False).head(5)
    rows = []
    for entity, score in scores.items():
        sub = wdf[wdf.entity == entity]
        rows.append({
            "rank": len(rows) + 1,
            "entity": entity,
            "score": round(float(score), 3),
            "novel": int((sub.tier == "novel").sum()),
            "total": len(sub),
            "first": pd.to_datetime(sub.timestamp.min(), unit="ms").strftime("%H:%M:%S"),
        })
    return pd.DataFrame(rows)


def match_llm(st_ms: int, ed_ms: int):
    """洪水窗口 → B 的 LLM 结果（时间区间重叠，取重叠量最大者）"""
    llm = load_llm()
    if llm is None:
        return None
    lo, hi = st_ms - 600_000, ed_ms + 600_000
    best = None
    for w in llm["windows"]:
        s, e = w["window"]["st_ms"], w["window"]["ed_ms"]
        if s <= hi and e >= lo:
            ov = min(hi, e) - max(lo, s)
            if best is None or ov > best[0]:
                best = (ov, w)
    return best[1] if best else None


def llm_top_entities(entry):
    """LLM candidates 按实体合并（同一实体取最高置信度）"""
    merged = {}
    for c in entry["candidates"]:
        e = c["entity"]
        if e not in merged or c["confidence"] > merged[e]["confidence"]:
            merged[e] = c
    return sorted(merged.values(), key=lambda c: (-c["confidence"], c["rank"]))[:5]


def load_report(window_id: int):
    """B 的 M4 处置单；未落盘返回 None"""
    p = ROOT / "results" / "reports" / f"window_{window_id}.json"
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def render_report(r: dict):
    st.markdown(f"**摘要**：{r['summary']}")
    st.subheader("根因判定")
    for c in r["root_causes"]:
        col1, col2 = st.columns([3, 1])
        col1.metric(f"#{c['rank']} 根因候选", c["entity"])
        col2.metric("置信度", f"{c['confidence']:.0%}")
        st.caption(c["evidence"])
    st.subheader("处置建议")
    for a in r["actions"]:
        icon = {"高": "🔴", "中": "🟡", "低": "🟢"}.get(a["priority"], "⚪")
        st.markdown(f"{icon} **{a['order']}.** {a['action']}（优先级：{a['priority']}）")
    st.info(f"已归并告警：{r['suppressed']}")
    st.warning(r["disclaimer"])


def preview_from_llm(entry):
    """M4 未落盘时，用 B 的 LLM 排序结果生成预览版处置单（界面层模板，非算法）"""
    cands = llm_top_entities(entry)
    top = cands[0]
    return {
        "summary": (f"本窗口判定为 {entry['anomaly_type']} 类故障，根因候选 {top['entity']}"
                    f"（LLM 置信度 {top['confidence']:.0%}）。窗口 "
                    f"{entry['window']['st_time']}~{entry['window']['ed_time']}，"
                    f"其余告警按模板归并后作为衍生告警处理。"),
        "root_causes": [{"rank": i + 1, "entity": c["entity"],
                         "confidence": c["confidence"],
                         "evidence": f"告警模板「{c['alarm']}」· 推理：{c['reason']}"}
                        for i, c in enumerate(cands)],
        "actions": [
            {"order": 1, "action": f"检查 {top['entity']} 的 {entry['anomaly_type']} 相关指标与进程状态", "priority": "高"},
            {"order": 2, "action": f"确认 {top['entity']} 是否为上游根因，排查下游受影响服务", "priority": "中"},
            {"order": 3, "action": "处置后持续观察 10 分钟，确认告警洪水是否消退", "priority": "低"},
        ],
        "suppressed": "衍生告警由 M1 模板归并 + M2 常驻搁置处理（详见窗口详情页签）",
        "disclaimer": "本处置单为界面预览版（依据 LLM 排序结果生成），正式版由 M4 模块产出。",
        "ticket": {
            "title": f"[根因处置] {top['entity']} {entry['anomaly_type']} 异常 · 故障#{entry['fault_id']}",
            "assignee_team": "按责任团队映射表分派（config/teams.csv）",
            "priority": "P2",
            "related_changes": [],
            "root_cause_link": f"results/llm/llm_rankings.json#fault_{entry['fault_id']}",
        },
    }


# ---------- 页面 ----------
st.set_page_config(page_title="AlarmFlood Copilot", layout="wide")
st.title("🚨 AlarmFlood Copilot · 告警洪水治理值班台")
st.caption("接入告警流 → 三级分诊 → 洪水窗口 → 双路根因排序（规则 / LLM）→ 处置单 → 派单（human-in-loop）")

alarms, floods = load_alarms(), load_floods()
llm_data = load_llm()

# 顶部时间轴
per_hour = alarms.groupby(alarms.timestamp // 3_600_000).size()
per_hour.index = pd.to_datetime(per_hour.index * 3_600_000, unit="ms").strftime("%m-%d %H:%M")
try:
    st.bar_chart(per_hour, height=140,
                 x_label="时间（小时）", y_label="告警条数（抑制后）")
except TypeError:  # 兼容 streamlit < 1.36（bar_chart 无 x_label/y_label）
    st.bar_chart(per_hour, height=140)

# 侧边栏
floods_sorted = floods.sort_values("n_alarms", ascending=False).reset_index(drop=True)
# 默认窗口：有 LLM 排序结果 + 告警量最大（保证一进界面就能看到双路对比）
default_idx = 0
for i, r in enumerate(floods_sorted.itertuples()):
    if match_llm(r.st_ms, r.ed_ms) is not None:
        default_idx = i
        break
w = st.sidebar.selectbox(
    "选择洪水窗口（按告警量排序）",
    floods_sorted.itertuples(),
    index=default_idx,
    format_func=lambda r: (f"#{r.window_id} · {pd.to_datetime(r.st_ms, unit='ms'):%m-%d %H:%M}"
                           f" · {r.n_alarms}条/{r.minutes}分钟 · {r.n_entities}实体"))
show_truth = st.sidebar.toggle("显示数据集标注（仅供核对）", value=False)
st.sidebar.caption(f"全库 {len(floods)} 个窗口（原始告警 101,478 条 → 抑制+搁置后 14,806 条）")
if llm_data:
    st.sidebar.caption(f"LLM 路线：{llm_data['model']} · 已覆盖 {len(llm_data['windows'])} 个故障窗口")

wdf = alarms[(alarms.timestamp >= w.st_ms) & (alarms.timestamp <= w.ed_ms)]
llm_entry = match_llm(w.st_ms, w.ed_ms)

tab1, tab2 = st.tabs(["📋 洪水窗口详情", "📄 处置单与派单"])

# ===== 页签一 =====
with tab1:
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("窗口内告警", f"{len(wdf)} 条", f"全量 {w.n_all_alarms} 条")
    c2.metric("涉及实体", f"{wdf.entity.nunique()} 个")
    c3.metric("告警模板", f"{wdf.template.nunique()} 个")
    c4.metric("窗口时长", f"{w.minutes} 分钟")

    st.subheader("告警瀑布图（模板 × 时间）")
    g = wdf.groupby("template").agg(first=("timestamp", "min"),
                                    n=("timestamp", "size")).sort_values("first")
    fig, ax = plt.subplots(figsize=(10, max(3, 0.5 * len(g))))
    ax.barh(g.index, g["n"], left=(g["first"] - w.st_ms) / 60_000, height=0.6, color="#D85A30")
    ax.set_xlabel("窗口内时间偏移（分钟）")
    ax.set_ylabel("告警模板")
    ax.set_yticks(range(len(g.index)))
    ax.set_yticklabels([t if len(t) <= 28 else t[:26] + "…" for t in g.index], fontsize=9)
    st.pyplot(fig)

    st.subheader("三级分诊明细")
    tier_cnt = wdf.groupby(["tier", "entity"]).size().reset_index(name="n")
    tier_cnt["tier_zh"] = tier_cnt.tier.map(TIER_ZH)
    novel_entities = wdf[wdf.tier == "novel"].entity.unique().tolist()
    tier_view = tier_cnt.sort_values(["tier", "n"], ascending=[True, False])
    try:
        st.dataframe(tier_view, width="stretch", height=220)
    except TypeError:  # 兼容旧版 streamlit（width 需 int → 改用 use_container_width）
        st.dataframe(tier_view, height=220, use_container_width=True)

    if novel_entities:
        st.success(f"🎯 新发告警实体（判别性最强）：{', '.join(novel_entities)}")
    else:
        st.warning("本窗口无新发告警——根因排序证据弱，建议人工复核")

    # ---- 双路根因排序对比 ----
    st.subheader("根因候选：双路对比")
    rule_df = rank_window(w.st_ms, w.ed_ms)
    llm_cands = llm_top_entities(llm_entry) if llm_entry else []
    rule_top1 = rule_df.entity.iloc[0] if not rule_df.empty else None
    llm_top1 = llm_cands[0]["entity"] if llm_cands else None

    if rule_top1 and llm_top1:
        if rule_top1 == llm_top1:
            st.success(f"✅ 两路一致：规则路线与 LLM 路线均指向 **{rule_top1}**，可高置信派单")
        else:
            st.warning(f"⚠️ 两路分歧：规则路线 **{rule_top1}** / LLM 路线 **{llm_top1}**"
                       f"——触发人工复核提示（融合排序在 D6 完成）")

    col_a, col_b = st.columns(2)
    with col_a:
        st.markdown("**路线 A · 规则排序（确定性，可审计）**")
        if rule_df.empty:
            st.info("窗口内无告警证据")
        else:
            for _, c in rule_df.iterrows():
                x1, x2, x3, x4 = st.columns([2, 2, 1.5, 1.5])
                x1.metric(f"#{c['rank']}", c["entity"])
                x2.progress(min(c["score"], 1.0), text=f"score {c['score']:.2f}")
                x3.metric("新发", f"{c['novel']} 条")
                x4.metric("首现", c["first"])
    with col_b:
        st.markdown("**路线 B · LLM 根因推理（会推理，需校验）**")
        if not llm_cands:
            st.info("本窗口暂无 LLM 排序结果（B 已覆盖 72 个故障窗口）")
        else:
            for i, c in enumerate(llm_cands, 1):
                y1, y2 = st.columns([2, 3])
                y1.metric(f"#{i}", c["entity"])
                y2.progress(min(c["confidence"], 1.0), text=f"confidence {c['confidence']:.0%}")
                st.caption(f"告警：{c['alarm']} · 推理：{c['reason']}")

    st.caption("规则依据：新发告警数（主证据 0.5）+ 新发越早越可疑（0.15）+ 分层加权频次（0.2）+ 分钟共现（0.15）")

    if show_truth and llm_entry:
        st.divider()
        truth = llm_entry.get("root_entity_truth")
        hit_r = "✅" if truth and truth == rule_top1 else "❌"
        hit_l = "✅" if truth and truth == llm_top1 else "❌"
        st.markdown(f"**数据集标注核对**（故障 #{llm_entry['fault_id']}，类型 {llm_entry['anomaly_type']}）："
                    f"真实根因 `{truth}` · 规则路线 {hit_r} · LLM 路线 {hit_l}")
        st.caption("注：标注仅用于离线核对与评测，不作为模型输入")

# ===== 页签二 =====
with tab2:
    report = load_report(w.window_id)
    if report is not None:
        st.caption("来源：M4 正式产物（results/reports/）")
        render_report(report)
    elif llm_entry is not None:
        st.caption("来源：M4 未落盘，界面依据 LLM 排序结果生成预览版")
        render_report(preview_from_llm(llm_entry))
    else:
        st.warning("本窗口既无 M4 处置单也无 LLM 排序结果。演示建议选 **#377**（已有 M4 示例处置单）"
                   "或侧边栏靠前的窗口（LLM 已覆盖）。")
        st.stop()

    st.download_button("⬇️ 导出处置单（JSON）",
                       data=json.dumps(report or preview_from_llm(llm_entry),
                                       ensure_ascii=False, indent=2),
                       file_name=f"report_window_{w.window_id}.json",
                       mime="application/json")
    st.divider()
    if st.button("📨 派单（创建工单并分派责任团队）", type="primary"):
        t = (report or preview_from_llm(llm_entry))["ticket"]
        st.success(f"工单已创建：{t['title']}")
        st.markdown(f"- **责任团队**：{t['assignee_team']}  \n"
                    f"- **优先级**：{t['priority']}  \n"
                    f"- **关联变更**：{t['related_changes'] or '无'}")
        st.info("状态：已派单（等待值班人确认执行）· 责任边界：Agent 建议与分诊，"
                "工单由值班人员确认后执行 · ITSM webhook 接口预留")

st.sidebar.divider()
st.sidebar.caption("数据：AIOps 挑战赛公开数据（银行系统）· 界面只读 results/ 产物")
