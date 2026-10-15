# -*- coding: utf-8 -*-
"""
AlarmFlood Copilot · 告警洪水治理值班台（M5 演示看板）
角色：C · 产品与交付
依赖：只读 results/ 下的 A/B 产物（parquet + json），不含业务计算逻辑；
     根因排序复用 A 的 modules/m3_rank.rank_entities（import 复用，不重写）
运行：C:/Users/zzw/AppData/Local/Programs/Python/Python310/python.exe -m streamlit run app.py
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
TIER_COLOR = {"core": "#B4B2A9", "semi": "#EF9F27", "novel": "#D85A30"}


# ---------- 数据加载（缓存：交互不重读 parquet） ----------
@st.cache_data(ttl=300)
def load_data():
    alarms = pd.read_parquet(ROOT / "results" / "alarms_tagged.parquet")
    floods = pd.read_parquet(ROOT / "results" / "flood_windows.parquet")
    return alarms, floods


@st.cache_data(ttl=300)
def rank_window(st_ms: int, ed_ms: int) -> pd.DataFrame:
    """调用 A 的规则排序，返回 Top-5 根因候选表"""
    alarms, _ = load_data()
    wdf = alarms[(alarms.timestamp >= st_ms) & (alarms.timestamp <= ed_ms)]
    if wdf.empty:
        return pd.DataFrame()
    scores = rank_entities(wdf, "rule").sort_values(ascending=False).head(5)
    rows = []
    for entity, score in scores.items():
        sub = wdf[wdf.entity == entity]
        novel = int((sub.tier == "novel").sum())
        rows.append({
            "rank": len(rows) + 1,
            "entity": entity,
            "score": round(float(score), 3),
            "novel": novel,
            "total": len(sub),
            "first": pd.to_datetime(sub.timestamp.min(), unit="ms").strftime("%H:%M:%S"),
        })
    return pd.DataFrame(rows)


def load_report(window_id: int):
    """读 B 的 M4 处置单；未落盘则返回 None（界面降级提示）"""
    p = ROOT / "results" / "reports" / f"window_{window_id}.json"
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def render_md(r: dict):
    """处置单 JSON → markdown（B 的 D4 render_report 落地前的界面内渲染）"""
    st.markdown(f"**摘要**：{r['summary']}")
    st.subheader("根因判定")
    for c in r["root_causes"]:
        col1, col2 = st.columns([3, 1])
        col1.metric(f"#{c['rank']} 根因候选", c["entity"])
        col2.metric("置信度", f"{c['confidence']:.0%}")
        st.caption(c["evidence"])
    st.subheader("处置建议")
    for a in r["actions"]:
        pri_color = {"高": "🔴", "中": "🟡", "低": "🟢"}[a["priority"]]
        st.markdown(f"{pri_color} **{a['order']}.** {a['action']}（优先级：{a['priority']}）")
    st.info(f"已归并告警：{r['suppressed']}")
    st.warning(r["disclaimer"])


# ---------- 页面 ----------
st.set_page_config(page_title="AlarmFlood Copilot", layout="wide")
st.title("🚨 AlarmFlood Copilot · 告警洪水治理值班台")
st.caption("接入告警流 → 三级分诊 → 洪水窗口 → 根因排序 → 处置单 → 派单（human-in-loop）")

alarms, floods = load_data()

# 顶部时间轴：按小时告警量（一眼看到"洪水"在哪）
per_hour = alarms.groupby(alarms.timestamp // 3_600_000).size()
per_hour.index = pd.to_datetime(per_hour.index * 3_600_000, unit="ms").strftime("%m-%d %H:%M")
try:
    st.bar_chart(per_hour, height=140,
                 x_label="时间（小时）", y_label="告警条数（抑制后）")
except TypeError:  # 兼容 streamlit < 1.36（bar_chart 无 x_label/y_label）
    st.bar_chart(per_hour, height=140)

# 侧边栏：选窗口（按告警量倒序，重的排前面）
floods_sorted = floods.sort_values("n_alarms", ascending=False).reset_index(drop=True)
w = st.sidebar.selectbox(
    "选择洪水窗口（按告警量排序）",
    floods_sorted.itertuples(),
    format_func=lambda r: (f"#{r.window_id} · {pd.to_datetime(r.st_ms, unit='ms'):%m-%d %H:%M}"
                           f" · {r.n_alarms}条/{r.minutes}分钟 · {r.n_entities}实体"))
st.sidebar.caption(f"常驻搁置后触发 · 全库 {len(floods)} 个窗口（原始告警 101,478 条，"
                   f"抑制+搁置后 14,806 条）")

wdf = alarms[(alarms.timestamp >= w.st_ms) & (alarms.timestamp <= w.ed_ms)]

tab1, tab2 = st.tabs(["📋 洪水窗口详情", "📄 处置单与派单"])

# ===== 页签一：窗口详情 =====
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
        st.warning("本窗口无新发告警——根因排序证据弱，处置单建议低置信度人工复核")

    st.subheader("根因候选（规则路线 Top-5）")
    cand = rank_window(w.st_ms, w.ed_ms)
    if not cand.empty:
        for _, c in cand.iterrows():
            col1, col2, col3, col4, col5 = st.columns([2, 3, 1.5, 1.5, 1.5])
            col1.metric(f"#{c['rank']}", c["entity"])
            col2.progress(min(c["score"], 1.0), text=f"score {c['score']:.2f}")
            col3.metric("新发告警", f"{c['novel']} 条")
            col4.metric("总告警", f"{c['total']} 条")
            col5.metric("首现", c["first"])
    st.caption("排序依据：新发告警数（主证据 0.5）+ 新发越早越可疑（0.15）+ 分层加权频次（0.2）+ 分钟共现（0.15）")

# ===== 页签二：处置单与派单 =====
with tab2:
    report = load_report(w.window_id)
    if report is None:
        st.warning("本窗口暂无处置单（M4 结果未落盘）。演示请选择窗口 **#377**——"
                   "示例处置单已就位，D4 完成后所有窗口自动可用。")
    else:
        render_md(report)

        st.download_button("⬇️ 导出处置单（JSON）",
                           data=json.dumps(report, ensure_ascii=False, indent=2),
                           file_name=f"report_window_{w.window_id}.json",
                           mime="application/json")
        st.divider()
        if st.button("📨 派单（创建工单并分派责任团队）", type="primary"):
            t = report["ticket"]
            st.success(f"工单已创建：{t['title']}")
            st.markdown(f"- **责任团队**：{t['assignee_team']}  \n"
                        f"- **优先级**：{t['priority']}  \n"
                        f"- **关联变更**：{t['related_changes'] or '无'}")
            st.info("状态：已派单（等待值班人确认执行）· 责任边界：Agent 建议与分诊，"
                    "工单由值班人员确认后执行 · ITSM webhook 接口预留")

st.sidebar.divider()
st.sidebar.caption("数据：AIOps 挑战赛公开数据（银行系统）· 界面只读 results/ 产物")
