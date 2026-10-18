# -*- coding: utf-8 -*-
"""
AlarmFlood Copilot · 告警洪水治理值班台（M5 演示看板）
角色：C · 产品与交付
依赖：只读 results/ 下的 A/B 产物，不含业务计算逻辑
  - A：results/alarms_tagged.parquet、flood_windows.parquet、modules/m3_rank（规则路线）
  - B：results/llm_rankings.json（LLM 根因排序路线）
  - B：results/reports/window_*.json（M4 处置单；未落盘时用 LLM 结果生成预览版）
  - 数据集标注 root_entity_truth 默认隐藏，仅侧边栏开关打开时用于离线核对
运行：python -m streamlit run app.py
     （或双击 run_dashboard.bat；完整部署说明见仓库 README「快速开始」）
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

TIER_ZH = {"core": "常驻噪声", "semi": "半常驻", "novel": "新发信号"}
TIER_ORDER = {"core": 0, "semi": 1, "novel": 2}


def pretty_template(t: str) -> str:
    """模板展示名：把 Drain3 的 <NUM> 掩码占位符换成可读的 xx"""
    return t.replace("<NUM>", "xx")


# ---------- 数据加载（缓存：交互不重读文件） ----------
@st.cache_data(ttl=300)
def load_alarms():
    return pd.read_parquet(ROOT / "results" / "alarms_tagged.parquet")


@st.cache_data(ttl=300)
def load_floods():
    return pd.read_parquet(ROOT / "results" / "flood_windows.parquet")


@st.cache_data(ttl=300)
def load_llm():
    """B 的 LLM 根因排序；文件不存在返回 None（新旧两个存放位置兼容）"""
    for p in (ROOT / "results" / "llm_rankings.json",
              ROOT / "results" / "llm" / "llm_rankings.json"):
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    return None


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
            "排名": len(rows) + 1,
            "实体": entity,
            "评分": round(float(score), 2),
            "新发告警": int((sub.tier == "novel").sum()),
            "首现时刻": pd.to_datetime(sub.timestamp.min(), unit="ms").strftime("%H:%M:%S"),
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
    st.markdown(f"> {r['summary']}")

    st.markdown("**根因判定**")
    for c in r["root_causes"]:
        with st.container(border=True):
            cols = st.columns([1, 4, 2])
            cols[0].markdown(f"### #{c['rank']}")
            cols[1].markdown(f"**{c['entity']}**")
            cols[2].markdown(f"置信度 **{c['confidence']:.0%}**")
            st.caption(c["evidence"])

    st.markdown("**处置建议**")
    for a in r["actions"]:
        icon = {"高": "🔴", "中": "🟡", "低": "🟢"}.get(a["priority"], "⚪")
        st.markdown(f"{icon} **{a['order']}.** {a['action']}")

    st.caption(f"已归并告警：{r['suppressed']}")
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
        "suppressed": "衍生告警由模板归并 + 常驻搁置处理（详见窗口详情页签）",
        "disclaimer": "本处置单为界面预览版（依据 LLM 排序结果生成），正式版由处置单生成模块产出。",
        "ticket": {
            "title": f"[根因处置] {top['entity']} {entry['anomaly_type']} 异常 · 故障#{entry['fault_id']}",
            "assignee_team": "按责任团队映射表分派（config/teams.csv）",
            "priority": "P2",
            "related_changes": [],
            "root_cause_link": f"results/llm/llm_rankings.json#fault_{entry['fault_id']}",
        },
    }


def bordered_top1(title: str, entity: str, subline: str, color: str):
    """Top-1 结论卡：双路对比的视觉焦点"""
    with st.container(border=True):
        st.markdown(f"**{title}**")
        st.markdown(f"## :{color}[{entity}]")
        st.caption(subline)


# ---------- 页面 ----------
st.set_page_config(page_title="AlarmFlood Copilot", layout="wide")
st.title("🚨 AlarmFlood Copilot · 告警洪水治理值班台")
st.markdown("当故障发生、告警像洪水一样涌来时，这个值班台帮值班工程师在 **30 秒内** 回答三个问题："
            "**发生了什么 → 谁是根因 → 接下来怎么办**。")

with st.expander("📖 这个页面是什么？（首次访问请先读）", expanded=False):
    st.markdown(
        """
**背景**：企业 IT 系统一次故障会在几分钟内触发成百上千条告警（告警洪水），
值班工程师平均需要 40 分钟以上人工梳理才能定位根因。

**本页面的作用**：把"告警流 → 根因 → 处置"这条人工链路自动化——

| 步骤 | 页面位置 | 你看到的 | 背后的技术 |
|---|---|---|---|
| ① 发生了什么 | 顶部时间轴 + 左侧选窗口 | 告警洪水的时段与规模 | 告警模板归并、重复抑制、常驻搁置 |
| ② 告警从哪来 | 页签一上半 | 每条告警是"常驻噪声"还是"新发信号" | 三级告警分诊 |
| ③ 谁是根因 | 页签一下半 | 两套方法各自给出根因排序并互相印证 | 规则排序（确定性）× 大模型推理 |
| ④ 怎么办 | 页签二 | 结构化处置单 + 一键派单 | 大模型生成 + 幻觉校验 |

**演示动线**：左侧选一个窗口 → 页签一看根因对比 → 页签二看处置单 → 点"派单"。
建议演示窗口：**#377**（双路一致命中真实根因 IG02）。
        """
    )

alarms, floods = load_alarms(), load_floods()
llm_data = load_llm()

# 顶部时间轴
per_hour = alarms.groupby(alarms.timestamp // 3_600_000).size()
per_hour.index = pd.to_datetime(per_hour.index * 3_600_000, unit="ms").strftime("%m-%d %H:%M")
st.markdown("##### 全量告警时间轴（抑制后，按小时）")
try:
    st.bar_chart(per_hour, height=260,
                 x_label="时间（小时）", y_label="告警条数")
except TypeError:  # 兼容 streamlit < 1.36（bar_chart 无 x_label/y_label）
    st.bar_chart(per_hour, height=260)

# 侧边栏
st.sidebar.header("窗口选择")
floods_sorted = floods.sort_values("n_alarms", ascending=False).reset_index(drop=True)
# 默认窗口：有 LLM 排序结果 + 告警量最大（保证一进界面就能看到双路对比）
default_idx = 0
for i, r in enumerate(floods_sorted.itertuples()):
    if match_llm(r.st_ms, r.ed_ms) is not None:
        default_idx = i
        break
w = st.sidebar.selectbox(
    "选择告警洪水窗口（按告警量排序）",
    floods_sorted.itertuples(),
    index=default_idx,
    format_func=lambda r: (f"#{r.window_id} · {pd.to_datetime(r.st_ms, unit='ms'):%m-%d %H:%M}"
                           f" · {r.n_alarms}条/{r.minutes}分钟"))
show_truth = st.sidebar.toggle("显示数据集标注（仅供核对）", value=False)
st.sidebar.divider()
st.sidebar.markdown("**数据概况**")
st.sidebar.caption(f"原始告警 101,478 条 → 抑制+搁置后 **14,806 条**（-85.4%）")
st.sidebar.caption(f"共检测出 {len(floods)} 个洪水窗口")
if llm_data:
    st.sidebar.caption(f"大模型路线：{llm_data['model']} · 覆盖 {len(llm_data['windows'])} 个故障窗口")
st.sidebar.divider()
st.sidebar.caption("数据：AIOps 挑战赛公开数据（银行系统）· 界面只读分析产物")

wdf = alarms[(alarms.timestamp >= w.st_ms) & (alarms.timestamp <= w.ed_ms)]
llm_entry = match_llm(w.st_ms, w.ed_ms)
win_time = f"{pd.to_datetime(w.st_ms, unit='ms'):%m-%d %H:%M} ~ {pd.to_datetime(w.ed_ms, unit='ms'):%H:%M}"

tab1, tab2 = st.tabs(["📋 ①~③ 发生了什么 / 谁是根因", "📄 ④ 怎么办：处置单与派单"])

# ===== 页签一 =====
with tab1:
    # ---------- ① 发生了什么 ----------
    st.header("① 发生了什么", anchor=False)
    st.caption(f"窗口 #{w.window_id} · {win_time} · 持续 {w.minutes} 分钟")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("窗口内告警", f"{len(wdf)} 条", f"全量 {w.n_all_alarms} 条")
    c2.metric("涉及实体", f"{wdf.entity.nunique()} 个", "服务器/服务实例")
    c3.metric("告警模板", f"{wdf.template.nunique()} 个", "归并后")
    c4.metric("平均速率", f"{len(wdf) / max(w.minutes, 1):.1f} 条/分钟")

    # ---------- ② 告警从哪来 ----------
    st.header("② 告警从哪来", anchor=False)
    st.markdown("把窗口内告警按**三级分诊**拆开：长期反复响的是**常驻噪声**（被搁置、不作为根因证据），"
                "故障前后才出现的是**新发信号**（根因判别的主要依据）。")

    tier_cnt = (wdf.groupby("tier").size()
                .reindex(["core", "semi", "novel"]).fillna(0).astype(int))
    t1, t2, t3 = st.columns(3)
    t1.metric("🔇 常驻噪声", f"{tier_cnt.get('core', 0)} 条", "已搁置")
    t2.metric("🔉 半常驻", f"{tier_cnt.get('semi', 0)} 条", "辅助证据")
    t3.metric("🎯 新发信号", f"{tier_cnt.get('novel', 0)} 条", "主要证据")

    novel_entities = wdf[wdf.tier == "novel"].entity.unique().tolist()
    if novel_entities:
        st.success(f"🎯 本窗口发出**新发告警**的实体：{', '.join(f'`{e}`' for e in novel_entities)}"
                   f" —— 根因大概率在其中")
    else:
        st.warning("本窗口无新发告警——根因排序证据弱，建议人工复核")

    with st.expander("📊 查看告警瀑布图（模板 × 时间）", expanded=True):
        g = wdf.assign(tier_ord=wdf.tier.map(TIER_ORDER))
        g = g.groupby(["template", "tier"], sort=False).agg(
            first=("timestamp", "min"), n=("timestamp", "size"))
        g = g.reset_index().sort_values("first")
        fig, ax = plt.subplots(figsize=(10, max(2.5, 0.45 * len(g))))
        colors = g.tier.map({"novel": "#D85A30", "semi": "#EF9F27", "core": "#9E9E9E"})
        ax.barh(g.template.map(pretty_template), g.n, left=(g["first"] - w.st_ms) / 60_000,
                height=0.6, color=colors)
        ax.set_xlabel("窗口内时间偏移（分钟）")
        ax.set_yticks(range(len(g)))
        ax.set_yticklabels([pretty_template(t) if len(t) <= 30 else pretty_template(t)[:28] + "…"
                            for t in g.template], fontsize=9)
        ax.invert_yaxis()
        from matplotlib.patches import Patch
        ax.legend(handles=[Patch(color="#D85A30", label="新发信号"),
                           Patch(color="#EF9F27", label="半常驻"),
                           Patch(color="#9E9E9E", label="常驻噪声")],
                  loc="lower right", fontsize=9)
        st.pyplot(fig)
        st.caption("颜色越暖越值得关注：红色=新发（根因证据），灰色=常驻（已搁置）")

    # ---------- ③ 谁是根因 ----------
    st.header("③ 谁是根因", anchor=False)
    st.markdown("同一批告警证据，交给**两套独立方法**各自排序——确定性规则保证可审计，"
                "大模型负责推理；两路结论一致即可高置信派单。")

    rule_df = rank_window(w.st_ms, w.ed_ms)
    llm_cands = llm_top_entities(llm_entry) if llm_entry else []
    rule_top1 = rule_df["实体"].iloc[0] if not rule_df.empty else None
    llm_top1 = llm_cands[0]["entity"] if llm_cands else None

    if rule_top1 and llm_top1:
        if rule_top1 == llm_top1:
            st.success(f"✅ **两路结论一致：{rule_top1}** —— 可高置信派单")
        else:
            st.warning(f"⚠️ **两路分歧**：规则路线 `{rule_top1}` / 大模型路线 `{llm_top1}`"
                       f" —— 已触发人工复核提示")

    col_a, col_b = st.columns(2)
    with col_a:
        st.markdown("**路线 A · 规则排序**（确定性，可审计）")
        if rule_df.empty:
            st.info("窗口内无告警证据")
        else:
            top = rule_df.iloc[0]
            bordered_top1("规则路线 Top-1", top["实体"],
                          f"评分 {top['评分']:.2f} · 新发告警 {top['新发告警']} 条 · 首现 {top['首现时刻']}",
                          "red")
            try:
                st.dataframe(rule_df, width="stretch", hide_index=True, height=210)
            except TypeError:
                st.dataframe(rule_df, hide_index=True, height=210, use_container_width=True)

    with col_b:
        st.markdown("**路线 B · 大模型根因推理**（会推理，需校验）")
        if not llm_cands:
            n_cov = len(llm_data["windows"]) if llm_data else 0
            st.info(f"本窗口暂无大模型排序结果（当前已覆盖 {n_cov} 个故障窗口）")
        else:
            top = llm_cands[0]
            bordered_top1("大模型路线 Top-1", top["entity"],
                          f"置信度 {top['confidence']:.0%} · 告警「{pretty_template(top['alarm'])}」",
                          "blue")
            llm_df = pd.DataFrame([{
                "排名": i, "实体": c["entity"], "置信度": f"{c['confidence']:.0%}",
                "依据告警": pretty_template(c["alarm"]),
            } for i, c in enumerate(llm_cands, 1)])
            try:
                st.dataframe(llm_df, width="stretch", hide_index=True, height=150)
            except TypeError:
                st.dataframe(llm_df, hide_index=True, height=150, use_container_width=True)
            with st.expander("💬 查看大模型推理过程"):
                for i, c in enumerate(llm_cands, 1):
                    st.markdown(f"**#{i} {c['entity']}**（{c['confidence']:.0%}）：{c['reason']}")

    st.caption("规则依据：新发告警数（主证据 0.5）+ 新发越早越可疑（0.15）"
               "+ 分层加权频次（0.2）+ 分钟共现（0.15）")

    if show_truth and llm_entry:
        st.divider()
        truth = llm_entry.get("root_entity_truth")
        hit_r = "✅ 命中" if truth and truth == rule_top1 else "❌ 未命中"
        hit_l = "✅ 命中" if truth and truth == llm_top1 else "❌ 未命中"
        st.markdown(f"**数据集标注核对**（故障 #{llm_entry['fault_id']}，类型 {llm_entry['anomaly_type']}）："
                    f"真实根因 `{truth}` · 规则路线 {hit_r} · 大模型路线 {hit_l}")
        st.caption("注：标注仅用于离线核对与评测，不作为模型输入")

# ===== 页签二 =====
with tab2:
    st.header("④ 怎么办", anchor=False)
    st.markdown("根因确认后，系统生成**结构化处置单**——值班工程师据此操作，"
                "确认无误后一键派单到责任团队（人在回路，AI 只建议不执行）。")

    report = load_report(w.window_id)
    preview = None
    if report is not None:
        st.caption("来源：处置单生成模块正式产物（results/reports/）")
    elif llm_entry is not None:
        preview = preview_from_llm(llm_entry)
        st.caption("来源：处置单模块未落盘，界面依据大模型排序结果生成预览版")
    else:
        st.warning("本窗口既无处置单也无大模型排序结果。"
                   "演示建议选 **#377**（已有示例处置单）或侧边栏靠前的窗口。")

    active = report or preview
    if active is not None:
        render_report(active)

        st.divider()
        dl_col, _ = st.columns([1, 3])
        dl_col.download_button("⬇️ 导出处置单（JSON）",
                               data=json.dumps(active, ensure_ascii=False, indent=2),
                               file_name=f"report_window_{w.window_id}.json",
                               mime="application/json")

        st.markdown("**派单**")
        st.caption("点击后创建工单并分派到责任团队；工单需值班人员确认后才执行")
        if st.button("📨 派单", type="primary"):
            t = active["ticket"]
            st.success(f"工单已创建：{t['title']}")
            st.markdown(f"- **责任团队**：{t['assignee_team']}  \n"
                        f"- **优先级**：{t['priority']}  \n"
                        f"- **关联变更**：{t['related_changes'] or '无'}")
            st.info("状态：已派单（等待值班人确认执行）· 责任边界：AI 建议与分诊，"
                    "工单由值班人员确认后执行 · ITSM webhook 接口预留")
