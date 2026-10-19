# -*- coding: utf-8 -*-
"""
AlarmFlood Copilot · 告警洪水治理值班台（M5 演示看板）
角色：C · 产品与交付

两种数据源模式（侧边栏第一个选择框）：
  1. 内置演示数据集 —— 只读 results/ 下 A/B 产物（AIOps + LLM + M4 处置单）
  2. 导入新告警流   —— 上传 csv/json/jsonl/parquet，现场跑 M1→M2→M3 轻量流水线
                     （modules/pipeline_adhoc.py），展示系统的即时处理能力。
                     不要求用户改文件格式：自动猜列名 → 摊开第 0 行样例值 →
                     用户用下拉框确认/修正「列映射」。

依赖：
  - A：results/alarms_tagged.parquet、flood_windows.parquet、modules/m3_rank（规则路线）
  - B：results/llm_rankings.json（LLM 根因排序）、results/reports/window_*.json（M4 处置单）
  - config/teams.csv（实体→责任团队映射，派单功能用）
  - 数据集标注 root_entity_truth 默认隐藏，仅侧边栏开关打开时用于离线核对
运行：python -m streamlit run app.py
     （或双击 run_dashboard.bat；完整部署说明见仓库 README「快速开始」）
"""
import sys
import json
import io
from pathlib import Path

import pandas as pd
import streamlit as st
import matplotlib.pyplot as plt

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "modules"))
from m3_rank import rank_entities  # noqa: E402  A 的规则排序路线
import pipeline_adhoc  # noqa: E402  导入模式轻量流水线

# ---------- 开发期热重载 ----------
# Streamlit 每次交互只重跑 app.py 本身，modules/ 下的改动不会自动生效——
# 改完代码界面还是旧的，典型症状就是 "module 'pipeline_adhoc' has no attribute ..."。
# 这里按源文件 mtime 自动重新 import（正式部署可整段删掉，不影响功能）。
import os
import importlib

for _name in ("pipeline_adhoc", "entity_extract", "m1_templater", "m2_window", "m3_rank"):
    _mod = sys.modules.get(_name)
    _file = getattr(_mod, "__file__", None) if _mod is not None else None
    if not _file or not os.path.exists(_file):
        continue
    _mt = os.path.getmtime(_file)
    if getattr(_mod, "_hot_mtime", 0) == 0:
        _mod._hot_mtime = _mt
    elif _mt > _mod._hot_mtime:
        importlib.reload(_mod)
        _mod._hot_mtime = os.path.getmtime(_file)

plt.rcParams["font.family"] = "sans-serif"
plt.rcParams["font.sans-serif"] = ["SimHei", "Microsoft YaHei"]
plt.rcParams["axes.unicode_minus"] = False


def pretty_template(t: str) -> str:
    """模板展示名：把 Drain3 的 <NUM> 掩码占位符换成可读的 xx"""
    return str(t).replace("<NUM>", "xx")


# ---------- 内置模式数据加载（缓存：交互不重读文件） ----------
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
def load_teams():
    """实体→责任团队映射（config/teams.csv）；文件缺失返回空表"""
    p = ROOT / "config" / "teams.csv"
    if not p.exists():
        return {}
    df = pd.read_csv(p)
    return dict(zip(df["entity"].astype(str), df["team"].astype(str)))


def load_report(window_id: int):
    """B 的 M4 处置单；未落盘返回 None"""
    p = ROOT / "results" / "reports" / f"window_{window_id}.json"
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


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


def rule_rank_df(wdf: pd.DataFrame) -> pd.DataFrame:
    """规则路线 Top-5（对任意窗口告警表通用）"""
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


# ---------- 处置单 ----------
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
                         "evidence": f"告警模板「{pretty_template(c['alarm'])}」· 推理：{c['reason']}"}
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
            "assignee_entity": top["entity"],
            "priority": "P2",
            "related_changes": [],
        },
    }


def preview_from_rule(wdf: pd.DataFrame, rule_df: pd.DataFrame, win_label: str):
    """导入模式：无 LLM，用规则路线 Top-1 生成模板化处置单（与 M4 降级逻辑同思路）"""
    top = rule_df.iloc[0]
    ent = top["实体"]
    sub = wdf[wdf.entity == ent]
    tpl = pretty_template(sub.template.iloc[0]) if len(sub) else "（无）"
    return {
        "summary": (f"窗口 {win_label} 内共 {len(wdf)} 条告警，"
                    f"规则路线判定根因候选 {ent}（评分 {top['评分']:.2f}，"
                    f"新发告警 {top['新发告警']} 条，首现 {top['首现时刻']}）。"),
        "root_causes": [{"rank": int(r["排名"]), "entity": r["实体"],
                         "confidence": min(max(r["评分"], 0.1), 0.95),
                         "evidence": f"新发告警 {r['新发告警']} 条 · 首现 {r['首现时刻']}"}
                        for _, r in rule_df.head(3).iterrows()],
        "actions": [
            {"order": 1, "action": f"检查 {ent} 的告警「{tpl}」对应的指标与进程状态", "priority": "高"},
            {"order": 2, "action": f"确认 {ent} 是否为上游根因，排查下游受影响服务", "priority": "中"},
            {"order": 3, "action": "处置后持续观察 10 分钟，确认告警是否消退", "priority": "低"},
        ],
        "suppressed": f"窗口内 {len(wdf)} 条告警已按三级分诊处理（详见窗口详情页签）",
        "disclaimer": "本处置单由规则模板生成（导入模式无大模型参与），仅供值班参考。",
        "ticket": {
            "title": f"[根因处置] {ent} 异常 · {win_label}",
            "assignee_entity": ent,
            "priority": "P2",
            "related_changes": [],
        },
    }


def bordered_top1(title: str, entity: str, subline: str, color: str):
    """Top-1 结论卡：双路对比的视觉焦点"""
    with st.container(border=True):
        st.markdown(f"**{title}**")
        st.markdown(f"## :{color}[{entity}]")
        st.caption(subline)


def render_dispatch(active: dict, win_label: str):
    """派单区：两步走（生成预览 → 值班人确认），讲清这个功能在替代什么人工动作"""
    st.markdown("**派单**")
    st.markdown(
        "找到根因后，值班员原本要**手工**做三件事：查“这个服务归谁管”、"
        "按格式写工单、发到对应值班群。派单把这三步合成一次点击——"
        "系统查责任团队映射表（`config/teams.csv`）自动填好工单，"
        "**值班人确认内容无误后才发出**（AI 只起草，不擅自执行）。")

    teams = load_teams()
    t = active["ticket"]
    entity = t.get("assignee_entity", "")
    team = teams.get(entity, "待指定（未在 teams.csv 中登记）")

    if st.button("📝 生成工单预览", key=f"gen_{win_label}"):
        st.session_state[f"ticket_{win_label}"] = True
    if not st.session_state.get(f"ticket_{win_label}"):
        return

    with st.container(border=True):
        st.markdown(f"**工单标题**：{t['title']}")
        c1, c2 = st.columns(2)
        c1.markdown(f"**责任团队**：{team}")
        if entity in teams:
            c1.caption(f"由根因实体 `{entity}` 查映射表得出")
        else:
            c1.caption("该实体未登记责任团队，请人工指定")
        c2.markdown(f"**优先级**：{t['priority']}")
        c2.caption("由最高严重度告警决定")
        if t.get("related_changes"):
            st.markdown(f"**关联变更**：{t['related_changes']}")

    st.download_button("⬇️ 下载工单（JSON）",
                       data=json.dumps({**t, "assignee_team": team},
                                       ensure_ascii=False, indent=2),
                       file_name=f"ticket_{win_label.replace(':', '-')}.json",
                       mime="application/json",
                       key=f"dl_{win_label}")
    if st.button("✅ 值班人确认，发送工单", type="primary", key=f"send_{win_label}"):
        st.success(f"已发送给 **{team}**，状态：待值班工程师接单处理")
        st.caption("演示环境未接真实 ITSM；正式部署时此处调用企业工单系统 webhook")


# ---------- 窗口分析渲染（内置/导入两种模式共用） ----------
def render_window_analysis(alarms: pd.DataFrame, w, llm_entry, show_truth: bool,
                           mode: str):
    wdf = alarms[(alarms.timestamp >= w["st_ms"]) & (alarms.timestamp <= w["ed_ms"])]
    win_label = f"#{w['window_id']} {pd.to_datetime(w['st_ms'], unit='ms'):%m-%d %H:%M}"

    # ---------- ① 发生了什么 ----------
    st.header("① 发生了什么", anchor=False)
    st.caption(f"窗口 {win_label} 起 · 持续 {w['minutes']} 分钟")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("窗口内告警", f"{len(wdf)} 条", f"全量 {w['n_all_alarms']} 条")
    c2.metric("涉及实体", f"{wdf.entity.nunique()} 个", "服务器/服务实例")
    c3.metric("告警模板", f"{wdf.template.nunique()} 个", "归并后")
    c4.metric("平均速率", f"{len(wdf) / max(w['minutes'], 1):.1f} 条/分钟")

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
        g = wdf.groupby(["template", "tier"], sort=False).agg(
            first=("timestamp", "min"), n=("timestamp", "size"))
        g = g.reset_index().sort_values("first")
        labels = [pretty_template(t) for t in g.template]
        labels = [t if len(t) <= 24 else t[:22] + "…" for t in labels]
        fig, ax = plt.subplots(figsize=(10, max(2.5, 0.45 * len(g))))
        colors = g.tier.map({"novel": "#D85A30", "semi": "#EF9F27", "core": "#9E9E9E"})
        ax.barh(range(len(g)), g.n, left=(g["first"] - w["st_ms"]) / 60_000,
                height=0.6, color=colors)
        ax.set_xlabel("窗口内时间偏移（分钟）")
        ax.set_yticks(range(len(g)))
        ax.set_yticklabels(labels, fontsize=9)
        ax.invert_yaxis()
        from matplotlib.patches import Patch
        ax.legend(handles=[Patch(color="#D85A30", label="新发信号"),
                           Patch(color="#EF9F27", label="半常驻"),
                           Patch(color="#9E9E9E", label="常驻噪声")],
                  loc="lower right", fontsize=9)
        fig.tight_layout()  # 防止长中文模板名被裁掉
        st.pyplot(fig)
        st.caption("颜色越暖越值得关注：红色=新发（根因证据），灰色=常驻（已搁置）")

    # ---------- ③ 谁是根因 ----------
    st.header("③ 谁是根因", anchor=False)
    rule_df = rule_rank_df(wdf)
    llm_cands = llm_top_entities(llm_entry) if llm_entry else []
    rule_top1 = rule_df["实体"].iloc[0] if not rule_df.empty else None
    llm_top1 = llm_cands[0]["entity"] if llm_cands else None

    if mode == "builtin":
        st.markdown("同一批告警证据，交给**两套独立方法**各自排序——确定性规则保证可审计，"
                    "大模型负责推理；两路结论一致即可高置信派单。")
        if rule_top1 and llm_top1:
            if rule_top1 == llm_top1:
                st.success(f"✅ **两路结论一致：{rule_top1}** —— 可高置信派单")
            else:
                st.warning(f"⚠️ **两路分歧**：规则路线 `{rule_top1}` / 大模型路线 `{llm_top1}`"
                           f" —— 已触发人工复核提示")
    else:
        st.markdown("导入模式下由**规则路线**即时排序（确定性、可审计）；"
                    "大模型推理与正式处置单需离线管线生成。")

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
            if mode == "builtin":
                llm_data = load_llm()
                n_cov = len(llm_data["windows"]) if llm_data else 0
                st.info(f"本窗口暂无大模型排序结果（当前已覆盖 {n_cov} 个故障窗口）")
            else:
                st.info("导入模式不调用大模型——接入离线管线后可获得本列结果")
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

    return wdf, rule_df, win_label


def render_report_tab(wdf, rule_df, win_label, w, llm_entry, mode):
    st.header("④ 怎么办", anchor=False)
    st.markdown("根因确认后，系统生成**结构化处置单**——值班工程师据此操作，"
                "确认无误后派单到责任团队（人在回路，AI 只建议不执行）。")

    report, preview = None, None
    if mode == "builtin":
        report = load_report(int(w["window_id"]))
        if report is not None:
            st.caption("来源：处置单生成模块正式产物（results/reports/）")
        elif llm_entry is not None:
            preview = preview_from_llm(llm_entry)
            st.caption("来源：处置单模块未落盘，界面依据大模型排序结果生成预览版")
    else:
        if rule_df.empty:
            st.warning("窗口内无告警证据，无法生成处置单")
            return
        preview = preview_from_rule(wdf, rule_df, win_label)
        st.caption("来源：导入模式 · 规则模板即时生成（无大模型参与）")

    active = report or preview
    if active is None:
        st.warning("本窗口既无处置单也无大模型排序结果。"
                   "演示建议选 **#377**（已有示例处置单）或侧边栏靠前的窗口。")
        return

    render_report(active)
    st.divider()
    st.download_button("⬇️ 导出处置单（JSON）",
                       data=json.dumps(active, ensure_ascii=False, indent=2),
                       file_name=f"report_{win_label.replace(':', '-')}.json",
                       mime="application/json",
                       key=f"rep_{win_label}")
    render_dispatch(active, win_label)


# ---------- 导入模式：读取上传文件 ----------
def read_uploaded(up) -> pd.DataFrame:
    name = up.name.lower()
    if name.endswith(".csv"):
        return pd.read_csv(up)
    if name.endswith(".parquet"):
        return pd.read_parquet(up)
    if name.endswith((".json", ".jsonl")):
        text = up.read().decode("utf-8")
        try:
            return pd.read_json(io.StringIO(text), lines=True)  # JSONL 优先
        except ValueError:
            return pd.read_json(io.StringIO(text))              # 普通 JSON 数组
    raise ValueError("仅支持 csv / json / jsonl / parquet")


SAMPLE_CSV = (
    "timestamp,entity,content,level\n"
    "2026-10-19 10:00:01,svc-a,CPU使用率95.2%超过阈值90%,2\n"
    "2026-10-19 10:00:31,svc-a,CPU使用率97.8%超过阈值90%,2\n"
    "2026-10-19 10:01:05,svc-b,调用svc-a超时,3\n"
    "2026-10-19 10:01:35,svc-c,调用svc-a超时,3\n"
    "2026-10-19 10:02:10,svc-b,调用svc-a超时,3\n"
    "2026-10-19 10:02:40,svc-c,调用svc-a超时,3\n"
    "2026-10-19 10:03:12,svc-a,磁盘sda繁忙率92.4%超过阈值90%,2\n"
    "2026-10-19 10:03:45,svc-b,调用svc-a超时,3\n"
)


# ---------- 导入模式：列映射（不要求用户改文件格式） ----------
def render_mapping_ui(raw: pd.DataFrame, name: str):
    """摊开「第 0 行样例值」让用户点选列角色——比逼用户把文件改成固定格式通用得多"""
    st.header("① 这一步：告诉系统哪一列是什么", anchor=False)
    st.markdown(
        "**不需要**把你的告警文件改成本项目的固定格式。系统按常见列名先猜一次，"
        "你只要对照下面这张表的**第 0 行样例值**核对，猜错了就在下拉框里改一下。\n\n"
        "必填三项：**时间**、**实体**（哪台机器/哪个服务）、**内容**（告警文本）；"
        "另两项可选，给了会更准（规则ID 用于三级分诊计数，级别用于严重度）。")

    cols = list(raw.columns)
    disp = [str(c) for c in cols]
    sample = pd.DataFrame({
        "列名": disp,
        "第 0 行样例值": [str(raw.iloc[0][c])[:38] if len(raw) else "" for c in cols],
        "非空行数": [int(raw[c].notna().sum()) for c in cols],
        "示例值2": [str(raw.iloc[1][c])[:38] if len(raw) > 1 else "" for c in cols],
    })
    try:
        st.dataframe(sample, width="stretch", hide_index=True)
    except TypeError:
        st.dataframe(sample, hide_index=True, use_container_width=True)

    guess = pipeline_adhoc.guess_mapping(raw)
    opts = ["（不使用）"] + disp
    ent_opts = ["（不使用）", pipeline_adhoc.ENTITY_FROM_CONTENT] + disp
    mapping = {}
    c1, c2 = st.columns(2)
    boxes = [c1, c2, c1, c2, c1]
    for i, role in enumerate(["timestamp", "entity", "content", "alarm_id", "level"]):
        o = ent_opts if role == "entity" else opts
        cur = guess.get(role)
        if cur is not None and str(cur) in o:
            idx = o.index(str(cur))
        else:
            # 没有实体列时，默认选中"从内容抽取"而不是留空——这是最常见的真实情况
            idx = 1 if role == "entity" else 0
        sel = boxes[i].selectbox(pipeline_adhoc.ROLE_LABEL[role], o, index=idx,
                                 key=f"map_{role}")
        mapping[role] = None if sel == "（不使用）" else (
            sel if sel == pipeline_adhoc.ENTITY_FROM_CONTENT else cols[disp.index(sel)])

    regex = None
    if mapping["entity"] == pipeline_adhoc.ENTITY_FROM_CONTENT:
        st.info("**没有实体列**时系统会从告警内容文本里抽实体：先按 IP / 键值对 / 域名 / "
                "带编号名（Redis02）/ 连字符名（svc-a）正则抽；抽不出的再用**模板差分**补——"
                "同一族模板里唯一变化的那个词就是实体（svc-a / svc-b、节点A / 节点B）。"
                "抽取结果处理完会摊给你核对。")
        regex = st.text_input(
            "自定义正则（可选）· 用第 1 个捕获组作为实体",
            value="", key="map_regex",
            placeholder=r"例如：^\[?([\w\-\.]+)\]? 或 (?:主机|服务器)\s*(\S+)",
            help="自动抽取不对时用它兜底；留空即用上面的自动策略")

    st.caption("映射只存在本次会话内存里，不会改动你的原始文件；"
               "时间支持 2026-10-19 10:00:01 / 2026/10/19 10:00 / ISO8601 / 秒级或毫秒级时间戳，"
               "解析不出来的行会直接报错而不是画出负数时间轴。")

    if st.button("⚙️ 开始处理（归并 → 抽实体 → 三级分诊 → 窗口检测 → 根因排序）",
                 type="primary", key="run_map"):
        try:
            with st.spinner("正在跑处理流水线…"):
                a2, w2, info2 = pipeline_adhoc.run_pipeline(raw, mapping, regex)
        except Exception as e:  # 列映射/时间解析/正则编译失败，给值班员可读提示
            st.error(f"处理失败：{e}")
            return
        st.session_state["custom"] = {"alarms": a2, "floods": w2, "name": name,
                                      "ent": info2.get("entity_stats")}
        if w2.empty:
            st.warning("未检出事件窗口——这份告警流里没有足够的非常驻告警聚集，"
                       "这本身也是一个结论：当前没有发生告警洪水。")
        else:
            st.success(f"处理完成：{len(a2)} 条告警 → {a2.template.nunique()} 个模板，"
                       f"检出 {len(w2)} 个事件窗口。左侧「② 窗口选择」里挑一个继续。")
        st.rerun()
    return


# ---------- 实体抽取结果核对（无实体列时） ----------
def render_entity_review(ent_stats: dict, alarms: pd.DataFrame):
    """把"从内容里抽实体"的结果摊开给值班人核对——绝不 silently 编造"""
    cov = ent_stats["coverage"]
    with st.expander(f"🔍 实体抽取结果核对（覆盖率 {cov:.0%}）", expanded=cov < 0.9):
        c1, c2, c3 = st.columns(3)
        c1.metric("抽取覆盖率", f"{cov:.0%}", f"{ent_stats['n'] - ent_stats['unknown']}"
                                             f"/{ent_stats['n']} 条")
        c2.metric("抽出实体数", f"{ent_stats['n_entities']} 个")
        c3.metric("未识别", f"{ent_stats['unknown']} 条")

        st.markdown("**各方法命中条数**")
        st.dataframe(pd.DataFrame({"抽取方法": list(ent_stats["methods"].keys()),
                                   "条数": list(ent_stats["methods"].values())}),
                     hide_index=True)
        if ent_stats["top"]:
            st.markdown("**抽出的实体 Top10**：" +
                        "、".join(f"`{k}`({v})" for k, v in ent_stats["top"].items()))

        cols = [c for c in ["content", "entity", "entity_method"] if c in alarms.columns]
        sample = alarms[cols].drop_duplicates().head(10).rename(
            columns={"content": "告警内容", "entity": "抽出实体", "entity_method": "抽取方法"})
        st.markdown("**样例（内容 → 实体 → 方法）**")
        st.dataframe(sample, hide_index=True)

        if ent_stats["unknown"]:
            st.warning(f"有 {ent_stats['unknown']} 条没能抽出实体，统一记作 `(未识别)`；"
                       f"它们不参与根因归属——一个笼统的「未识别」不算证据。"
                       f"可在下面填自定义正则后点「重新抽取」。")

        rgx = st.text_input("自定义正则（第 1 个捕获组为实体）", value="",
                            key="reextract_regex",
                            placeholder=r"(?:主机|服务器|host)\s*[=:：]?\s*([\w\-\.]+)")
        if st.button("🔁 用该正则重新抽取", key="reextract"):
            try:
                st.session_state["custom"]["alarms"], st.session_state["custom"]["ent"] = \
                    pipeline_adhoc.apply_entity_extraction(alarms, rgx or None)
                a3 = st.session_state["custom"]["alarms"]
                a3 = pipeline_adhoc.tag_tiers(a3)
                st.session_state["custom"]["alarms"] = a3
                st.session_state["custom"]["floods"] = pipeline_adhoc.detect_windows(a3)
            except Exception as e:
                st.error(f"重新抽取失败：{e}")
            else:
                st.rerun()
    return


# ---------- 批量导出：一次把一批窗口的处置结论交出去 ----------
def build_ticket_table(alarms: pd.DataFrame, floods: pd.DataFrame, mode: str,
                       only_official: bool, min_alarms: int = 3) -> pd.DataFrame:
    """逐窗口产出一行工单：根因实体 → 责任团队 → 优先级 → 标题（人在回路前的一手清单）

    置信度如实标注：证据不足（告警太少）的窗口不给具体百分比，只写"证据不足"，
    避免把单条告警的窗口包装成 95% 置信度——那是不诚实的演示。
    """
    teams = load_teams()
    rows, prog = [], st.progress(0, text="正在生成工单清单…")
    ws = floods.sort_values("st_ms").reset_index(drop=True)
    n_skip = 0
    for i, r in enumerate(ws.itertuples()):
        if i % 20 == 0:
            prog.progress(i / max(len(ws), 1), text=f"正在生成工单清单… {i}/{len(ws)}")
        wid = int(r.window_id)
        n_alarm = int(getattr(r, "n_alarms", 0))
        if n_alarm < min_alarms:      # 太小的窗口证据不足，默认不进清单
            n_skip += 1
            continue
        wdf = alarms[(alarms.timestamp >= r.st_ms) & (alarms.timestamp <= r.ed_ms)]
        n_novel = int((wdf.tier == "novel").sum()) if not wdf.empty else 0
        rep = load_report(wid) if mode == "builtin" else None
        if only_official and rep is None:
            continue
        if rep is not None:
            rc = rep["root_causes"][0] if rep.get("root_causes") else {}
            t = rep.get("ticket", {})
            ent = rc.get("entity") or t.get("assignee_entity", "")
            team = t.get("assignee_team") or teams.get(ent, "待指定")
            src = "正式处置单（大模型+校验）"
            conf = f"{rc.get('confidence', 0):.0%}"
            act = rep["actions"][0]["action"] if rep.get("actions") else ""
            title = t.get("title", "")
        else:
            if wdf.empty:
                continue
            sc = rank_entities(wdf, "rule").sort_values(ascending=False)
            ent = str(sc.index[0])
            team = teams.get(ent, "待指定")
            src = "规则排序（未生成正式处置单）"
            conf = f"{min(max(float(sc.iloc[0]), 0.1), 0.95):.0%}" if n_novel else "—（无新发告警）"
            act = f"检查 {ent} 的指标与进程状态"
            title = f"[根因处置] {ent} 异常 · 窗口#{wid}"
        rows.append({
            "窗口ID": wid,
            "开始时间": pd.to_datetime(r.st_ms, unit="ms").strftime("%Y-%m-%d %H:%M"),
            "持续分钟": int(r.minutes),
            "告警条数": n_alarm,
            "新发告警": n_novel,
            "证据强度": "强" if (n_alarm >= 10 and n_novel) else ("中" if n_alarm >= 5 else "弱"),
            "根因实体": ent,
            "置信度": conf,
            "责任团队": team,
            "优先级": rep.get("ticket", {}).get("priority", "P2") if rep else "P2",
            "工单标题": title,
            "首要处置动作": act,
            "结论来源": src,
        })
    prog.empty()
    if n_skip:
        st.session_state["batch_skip"] = n_skip
    return pd.DataFrame(rows)


def render_batch_export(alarms: pd.DataFrame, floods: pd.DataFrame, mode: str):
    st.divider()
    st.header("批量导出工单（全部窗口）", anchor=False)
    st.markdown("单个窗口派单之外，值班交接与故障复盘常常要**一次交出一批结论**："
                "这里把全部检出窗口的根因、责任团队、优先级一次性打成一张表——"
                "CSV 可直接贴进工单系统/表格，JSON 供下游系统对接。"
                "同样遵循人在回路：导出的是**待确认清单**，不是自动派发。")

    scope = st.radio("导出范围",
                     ["全部检出窗口", "仅已有正式处置单的窗口"],
                     horizontal=True, key="batch_scope",
                     help="正式处置单由离线管线（含大模型+幻觉校验）生成；"
                          "其余窗口用确定性规则路线给根因候选")
    min_alarms = st.slider("最少告警条数（低于此值证据不足，不进清单）",
                           1, 20, 3, key="batch_min",
                           help="一条告警就下根因结论是没有说服力的，默认过滤掉")
    only_official = scope.startswith("仅")
    gen = st.button("🔨 生成工单清单", type="primary", key="batch_gen")
    if gen:
        st.session_state.pop("batch_skip", None)
        st.session_state["batch"] = build_ticket_table(alarms, floods, mode,
                                                       only_official, min_alarms)

    df = st.session_state.get("batch")
    if df is None:
        st.caption(f"当前共 {len(floods)} 个检出窗口，点击上方按钮生成清单。")
        return
    if df.empty:
        st.warning("该范围内没有可导出的窗口（导入模式下没有正式处置单，请选「全部检出窗口」，"
                   "或把「最少告警条数」调低）")
        return

    n_skip = st.session_state.get("batch_skip", 0)
    st.metric("工单条数", f"{len(df)} 条",
              f"覆盖责任团队 {df['责任团队'].nunique()} 个" +
              (f" · 已过滤 {n_skip} 个证据不足的小窗口" if n_skip else ""))
    weak = int((df["证据强度"] == "弱").sum())
    if weak:
        st.caption(f"其中 {weak} 条证据强度为「弱」，建议值班人优先复核后再派发。")
    try:
        st.dataframe(df, width="stretch", hide_index=True, height=320)
    except TypeError:
        st.dataframe(df, hide_index=True, height=320, use_container_width=True)

    csv_bytes = df.to_csv(index=False).encode("utf-8-sig")   # BOM：Excel 打开中文不乱码
    payload = json.dumps({"generated_at": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"),
                          "source": mode, "status": "待值班人确认",
                          "tickets": df.to_dict("records")},
                         ensure_ascii=False, indent=2)
    d1, d2 = st.columns(2)
    d1.download_button("⬇️ 导出全部工单（CSV）", data=csv_bytes,
                       file_name="tickets_all.csv", mime="text/csv", key="dl_csv")
    d2.download_button("⬇️ 导出全部工单（JSON）", data=payload,
                       file_name="tickets_all.json", mime="application/json", key="dl_json")
    st.caption("导出即「待确认」状态：值班人核对后再逐条发送，AI 不擅自派发。")


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
| ④ 怎么办 | 页签二 | 结构化处置单 + 派单 | 大模型生成 + 幻觉校验 |
| ⑤ 批量导出 | 页签二底部 | 一批窗口的根因/责任团队/优先级打成一张表 | 规则排序 + 责任团队映射 |

**两种玩法**：左侧数据源选「内置演示数据集」看完整双路对比；
选「导入新告警流」上传你自己的告警文件，**不用改格式**——
在页面里点一下哪一列是时间/实体/内容，现场跑一遍处理流水线。
        """
    )

# ================= 侧边栏：① 数据源 → ② 窗口 =================
st.sidebar.header("① 数据源")
mode_label = st.sidebar.selectbox(
    "选择数据来源",
    ["内置演示数据集（AIOps 银行系统）", "导入新告警流（上传文件）"])
mode = "builtin" if mode_label.startswith("内置") else "upload"
raw = None  # 上传模式的原始表（未跑流水线前用）

if mode == "builtin":
    alarms, floods = load_alarms(), load_floods()
    st.sidebar.caption(f"原始告警 101,478 条 → 抑制+搁置后 **14,806 条**（-85.4%）")
    llm_data = load_llm()
    if llm_data:
        st.sidebar.caption(f"大模型路线：{llm_data['model']} · 覆盖 {len(llm_data['windows'])} 个故障窗口")
else:
    st.sidebar.caption("支持 csv / json / jsonl / parquet。**不用改文件格式**："
                       "上传后在页面里点一下哪一列是时间/实体/内容即可。")
    st.sidebar.download_button("📄 下载示例文件（照葫芦画瓢用）",
                               data=SAMPLE_CSV, file_name="alarm_sample.csv",
                               mime="text/csv")
    up = st.sidebar.file_uploader("上传告警文件", type=["csv", "json", "jsonl", "parquet"])
    alarms, floods = None, None
    if up is not None:
        try:
            raw = read_uploaded(up)
            # 换文件了就把上一次的处理结果和列映射作废
            if st.session_state.get("up_name") != up.name:
                st.session_state.pop("custom", None)
                st.session_state["up_name"] = up.name
            st.sidebar.caption(f"已读取 {len(raw)} 行 · {len(raw.columns)} 列")
        except Exception as e:
            st.sidebar.error(f"文件读取失败：{e}")
            raw = None
            st.session_state.pop("custom", None)
    else:
        raw = None

    if "custom" in st.session_state:
        alarms = st.session_state["custom"]["alarms"]
        floods = st.session_state["custom"]["floods"]
        _ent = st.session_state["custom"].get("ent")
        st.sidebar.success(
            f"已处理 {st.session_state['custom']['name']}："
            f"{len(alarms)} 条告警 → {alarms.template.nunique()} 个模板，"
            f"检出 {len(floods)} 个事件窗口"
            + (f"；实体由内容抽取，覆盖率 {_ent['coverage']:.0%}" if _ent else ""))
    if st.sidebar.button("🔄 重新上传/重新映射", key="reset_up"):
        st.session_state.pop("custom", None)
        st.session_state.pop("up_name", None)
        st.rerun()

# ---- 窗口选择（两种模式共用） ----
if alarms is not None and not floods.empty:
    st.sidebar.header("② 窗口选择")
    floods_sorted = floods.sort_values("n_alarms", ascending=False).reset_index(drop=True)
    default_idx = 0
    if mode == "builtin":  # 默认窗口：有 LLM 结果 + 告警量最大
        for i, r in enumerate(floods_sorted.itertuples()):
            if match_llm(r.st_ms, r.ed_ms) is not None:
                default_idx = i
                break
    w_row = st.sidebar.selectbox(
        "选择告警洪水窗口（按告警量排序）",
        floods_sorted.itertuples(),
        index=default_idx,
        format_func=lambda r: (f"#{r.window_id} · {pd.to_datetime(r.st_ms, unit='ms'):%m-%d %H:%M}"
                               f" · {r.n_alarms}条/{r.minutes}分钟"))
    w = {c: getattr(w_row, c) for c in floods_sorted.columns}
elif mode == "upload" and alarms is None:
    if raw is not None:
        render_mapping_ui(raw, up.name)
    else:
        st.info("👈 在左侧上传告警文件（csv / json / jsonl / parquet），"
                "然后在页面里点选哪一列是时间/实体/内容，即可对你的告警流"
                "现场执行归并、三级分诊与根因排序。")
    st.stop()
else:
    st.warning("未检出事件窗口：告警流中没有足够的非常驻告警聚集。"
               "这本身也是一个结论——当前没有发生告警洪水。")
    st.stop()

show_truth = (st.sidebar.toggle("显示数据集标注（仅供核对）", value=False)
              if mode == "builtin" else False)
st.sidebar.divider()
st.sidebar.caption("数据：AIOps 挑战赛公开数据（银行系统）· 界面只读分析产物"
                   if mode == "builtin" else "导入模式：数据仅在本机内存处理，不落盘不上传")

# ---- 顶部时间轴 ----
# 防御：时间戳必须为正的 epoch 毫秒。解析失败曾产生 NaT→int64 的 -9.2e18，
# 画出来就是 1677 年 / 负数轴，这里统一挡掉并明确告知丢弃了多少条。
ts_all = pd.to_numeric(alarms["timestamp"], errors="coerce")
n_bad = int((ts_all.isna() | (ts_all <= 0)).sum())
ts_ok = ts_all[ts_all.notna() & (ts_all > 0)].astype("int64")

unit_name = "小时"
if ts_ok.empty:
    st.error("时间轴无法绘制：所有告警的时间戳都非法（≤0 或解析失败）。"
             "请检查上传文件的时间列，或在上方「列映射」里重新指定时间列。")
    per_hour = pd.Series(dtype=int)
else:
    if n_bad:
        st.warning(f"⚠️ 已忽略 {n_bad} 条时间戳非法（≤0 / 解析失败）的告警，它们不进入时间轴与窗口分析。")
    # 跨度过大（>90 天）时按天聚合，避免横轴挤成一片
    if (ts_ok.max() - ts_ok.min()) / 86_400_000 > 90:
        unit_ms, fmt, unit_name = 86_400_000, "%m-%d", "天"
    else:
        unit_ms, fmt, unit_name = 3_600_000, "%m-%d %H:%M", "小时"
    # rename_axis(None)：否则索引名沿用 "timestamp"，bar_chart 内部 reset_index 会撞列名报错
    per_hour = ts_ok.groupby(ts_ok // unit_ms).size().rename_axis(None)
    per_hour.index = pd.to_datetime(per_hour.index * unit_ms, unit="ms").strftime(fmt)

ent_stats = st.session_state.get("custom", {}).get("ent") if mode == "upload" else None
if ent_stats:
    render_entity_review(ent_stats, alarms)

st.markdown(f"##### 全量告警时间轴（按{unit_name}）" + ("" if mode == "builtin" else " · 导入数据"))
if per_hour.empty:
    st.info("无有效时间戳可绘制")
else:
    try:
        st.bar_chart(per_hour, height=260, x_label=f"时间（{unit_name}）", y_label="告警条数")
    except TypeError:  # 兼容 streamlit < 1.36（bar_chart 无 x_label/y_label）
        st.bar_chart(per_hour, height=260)

llm_entry = match_llm(w["st_ms"], w["ed_ms"]) if mode == "builtin" else None

tab1, tab2 = st.tabs(["📋 ①~③ 发生了什么 / 谁是根因",
                      "📄 ④ 怎么办：处置单 · 派单 · 批量导出"])
with tab1:
    wdf, rule_df, win_label = render_window_analysis(alarms, w, llm_entry, show_truth, mode)
with tab2:
    render_report_tab(wdf, rule_df, win_label, w, llm_entry, mode)
    render_batch_export(alarms, floods, mode)
