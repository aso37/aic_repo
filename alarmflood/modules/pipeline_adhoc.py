# -*- coding: utf-8 -*-
"""
pipeline_adhoc.py — 导入模式的轻量处理流水线（供 app.py 界面上传告警流时使用）

复用 M1/M2 的核心逻辑，但输入是内存中的 DataFrame 而非磁盘 parquet：
  guess_mapping     按常见列名猜一份「列角色映射」，界面据此预填下拉框
  normalize_alarms  任意列名 → 统一五列（timestamp/entity/content/alarm_id/level）
  mine_templates    M1 同款 Drain3 归并（数字掩码）
  tag_tiers         M2 同款三级分诊（core/semi/novel）
  detect_windows    M2 同款窗口聚类（gap 5min / maxdur 20min）
  run_pipeline      以上四步串成一条

设计取舍：不要求用户把文件改成我们的固定格式。
  自动猜一次 → 界面把「第 0 行样例值」摊给用户看 → 用户用下拉框改（列映射）。
  猜不出来或时间列解析失败时给可读报错，绝不静默产出负数时间戳
  （旧实现用 .astype("int64") 转 NaT，会变成 -9223372036854775808，
   在时间轴上表现为 1677 年 / 负数，已在本版彻底堵住）。

只用于演示界面的即时处理，不替代正式离线管线（无持久化、无校验）。
"""
import pandas as pd

from m1_templater import make_miner
from m2_window import CORE_MIN, SEMI_MIN, GAP_MS, MAX_DUR_MS
from entity_extract import (UNKNOWN, extract, refine_by_template_diff, summarize)  # noqa: F401

# 实体列的特殊取值：该文件没有实体列，实体要从告警内容文本里抽
ENTITY_FROM_CONTENT = "⟳ 从告警内容中抽取"
ENTITY_METHOD_COL = "entity_method"

# 常见列名候选（全部小写匹配）
COL_CANDIDATES = {
    "timestamp": ["timestamp", "time", "ts", "stime", "start_time", "alert_time", "startsat",
                  "occur_time", "alarm_time", "datetime", "date", "event_time", "create_time",
                  "时间", "发生时间", "告警时间", "首次发生时间", "时间戳", "上报时间", "采集时间"],
    "entity": ["entity", "cmdb_id", "host", "service", "instance", "resource", "node", "target",
               "ip", "hostname", "ci", "app", "pod", "server",
               "实体", "主机", "对象", "设备", "网元", "业务", "服务", "实例", "告警对象", "资源"],
    "content": ["content", "message", "msg", "alertname", "alarm_content", "description",
                "alert", "summary", "text", "detail", "alarm_text", "title",
                "告警内容", "内容", "告警描述", "描述", "告警文本", "告警标题", "详情", "正文",
                "告警", "告警信息"],
    "alarm_id": ["alarm_id", "rule", "alert_id", "rule_id", "rule_name", "alertname", "metric",
                 "规则", "规则ID", "告警ID", "告警规则", "指标", "监控项"],
    "level": ["level", "severity", "priority", "urgency", "级别", "严重度", "告警级别", "等级"],
}

# 五个角色：必填三项 + 可选两项（界面下拉框的标签）
ROLE_LABEL = {
    "timestamp": "时间（必填）",
    "entity": "实体/对象（必填）",
    "content": "告警内容（必填）",
    "alarm_id": "告警规则ID（可选·用于三级分诊计数）",
    "level": "级别/严重度（可选）",
}
REQUIRED_ROLES = ["timestamp", "entity", "content"]

# 合法时间的绝对区间：1971-01-01 ~ 2100-01-01（毫秒）
MIN_TS_MS = 31_536_000_000
MAX_TS_MS = 4_102_444_800_000


# 兜底模糊匹配：列名里包含这些关键词就算命中（解决"告警发生时间"这类组合列名）
FUZZY_HINT = {
    "timestamp": ["time", "date", "ts", "时间", "日期", "时刻"],
    "entity": ["host", "ip", "node", "entity", "instance", "service", "pod",
               "主机", "实体", "设备", "网元", "对象", "服务", "业务", "实例", "资源"],
    "content": ["msg", "message", "content", "desc", "detail", "summary", "text",
                "内容", "描述", "详情", "告警", "标题", "正文"],
    "alarm_id": ["rule", "id", "规则", "指标", "监控项"],
    "level": ["level", "severity", "priority", "urgency", "级别", "严重度", "等级"],
}


def guess_mapping(df: pd.DataFrame) -> dict:
    """按常见列名猜列角色。

    两轮：① 全名精确匹配（中英文候选表）→ ② 关键词包含匹配（未被认领的列）。
    猜不到就返回 None，由用户在界面下拉框里指定——不猜硬凑。
    """
    lower = {str(c).lower(): c for c in df.columns}

    def pick(key):
        for cand in COL_CANDIDATES[key]:
            if cand in lower:
                return lower[cand]
        return None

    mapping = {k: pick(k) for k in ROLE_LABEL}
    taken = {v for v in mapping.values() if v is not None}
    for key in ["timestamp", "entity", "content", "alarm_id", "level"]:
        if mapping[key] is not None:
            continue
        for col in df.columns:
            if col in taken:
                continue
            lc = str(col).lower()
            if any(h in lc for h in FUZZY_HINT[key]):
                mapping[key] = col
                taken.add(col)
                break
    return mapping


def to_epoch_ms(series: pd.Series) -> pd.Series:
    """任意时间列 → 毫秒时间戳。解析不出的一律抛错，绝不产生负数。"""
    s = series
    if pd.api.types.is_numeric_dtype(s):
        v = pd.to_numeric(s, errors="coerce")
        if v.isna().any():
            bad = s[v.isna()].head(3).tolist()
            raise ValueError(f"时间列含无法解析的值 {bad} —— 请填毫秒/秒时间戳或标准时间字符串")
        v = v.astype("float64")
        mx = float(v.abs().max())
        if mx > 1e17:
            v = v / 1e6        # 纳秒
        elif mx > 1e14:
            v = v / 1e3        # 微秒
        elif mx < 1e11:
            v = v * 1e3        # 秒
        out = v.round().astype("int64")
    else:
        txt = s.astype(str).str.strip()
        try:
            dt = pd.to_datetime(txt, errors="coerce", format="mixed")
        except (ValueError, TypeError):  # 老版本 pandas 无 format="mixed"
            dt = pd.to_datetime(txt, errors="coerce")
        if dt.isna().any():
            bad = txt[dt.isna()].head(3).tolist()
            raise ValueError(f"时间列无法解析（示例：{bad}）—— 支持 "
                             f"2026-10-19 10:00:01 / 2026/10/19 10:00 / ISO8601 等格式")
        out = (dt.astype("int64") // 10**6).astype("int64")

    bad = out[(out < MIN_TS_MS) | (out > MAX_TS_MS)]
    if len(bad):
        sample = s.loc[bad.index[:3]].tolist()
        raise ValueError(f"时间列存在越界值 {sample} —— 需在 1971~2100 年之间；"
                         f"若是相对秒数/自增序号，请改成绝对时间")
    return out


def normalize_alarms(df: pd.DataFrame, mapping: dict | None = None) -> pd.DataFrame:
    """任意告警表 → 统一五列。

    mapping 形如 {"timestamp":"ts","entity":"host","content":"msg",...}，
    由界面的「列映射」下拉框给出；不传则调用 guess_mapping 自动推断。
    缺可选列给默认值，解析失败抛 ValueError（界面直接展示给值班员）。
    """
    mapping = mapping or guess_mapping(df)
    from_content = mapping.get("entity") == ENTITY_FROM_CONTENT   # 实体藏在内容里
    missing = [ROLE_LABEL[k] for k in REQUIRED_ROLES if not mapping.get(k)]
    if missing:
        raise ValueError(f"缺少必要列：{'、'.join(missing)}。请在上方「列映射」里指定对应的列"
                         f"（当前文件的列：{list(df.columns)}）")
    for k, col in mapping.items():
        if k == "entity" and from_content:
            continue
        if col and col not in df.columns:
            raise ValueError(f"映射的列「{col}」不在文件中（当前列：{list(df.columns)}）")

    out = pd.DataFrame()
    out["timestamp"] = to_epoch_ms(df[mapping["timestamp"]])
    # 无实体列时先留占位，等模板归并后由 apply_entity_extraction 回填
    out["entity"] = UNKNOWN if from_content else df[mapping["entity"]].astype(str).str.strip()
    out["content"] = df[mapping["content"]].astype(str).str.strip()

    c_rule = mapping.get("alarm_id")
    out["alarm_id"] = df[c_rule].astype(str).str.strip() if c_rule else ""
    c_lv = mapping.get("level")
    out["level"] = (pd.to_numeric(df[c_lv], errors="coerce").fillna(3).astype(int)
                    if c_lv else 3)

    empty = (out["content"] == "") | (out["content"] == "nan")
    if empty.any():
        raise ValueError(f"有 {int(empty.sum())} 行的内容列为空，请先清洗"
                         f"（示例行号 {empty[empty].index[:3].tolist()}）")
    return out.sort_values("timestamp").reset_index(drop=True)


def apply_entity_extraction(df: pd.DataFrame, custom_regex: str | None = None):
    """无实体列时：正则抽取 → 模板差分补全 → 写回 entity 列。

    必须在 mine_templates 之后调用（模板差分需要模板号）。
    返回 (df, stats)，stats 供界面如实展示覆盖率。
    """
    df = df.copy()
    ent, meth = extract(df["content"], custom_regex)
    ent, meth = refine_by_template_diff(df, ent, meth)
    df["entity"] = ent.values
    df[ENTITY_METHOD_COL] = meth.values
    return df, summarize(ent, meth)


def mine_templates(df: pd.DataFrame) -> pd.DataFrame:
    """M1 同款 Drain3 归并，新增 template_id / template 两列"""
    miner = make_miner()
    tids, texts = [], {}
    for content in df["content"]:
        r = miner.add_log_message(content)
        tids.append(r["cluster_id"])
        texts.setdefault(r["cluster_id"], r["template_mined"])
    df = df.copy()
    df["template_id"] = tids
    df["template"] = df["template_id"].map(texts)
    # 没有规则列时，用模板号当规则键（三级分诊需要"同实体同规则"计数）
    df.loc[df["alarm_id"] == "", "alarm_id"] = "T" + df["template_id"].astype(str)
    return df


def tag_tiers(df: pd.DataFrame) -> pd.DataFrame:
    """M2 同款三级分诊：core >=900 / semi 100-899 / novel <100（按实体×规则计数）"""
    cnt = df.groupby(["entity", "alarm_id"])["timestamp"].size().to_dict()
    df = df.copy()
    df["tier"] = [
        "core" if cnt[(e, r)] >= CORE_MIN else ("semi" if cnt[(e, r)] >= SEMI_MIN else "novel")
        for e, r in zip(df["entity"], df["alarm_id"])
    ]
    return df


def detect_windows(df: pd.DataFrame) -> pd.DataFrame:
    """M2 同款窗口聚类：semi+novel 触发，间隔<=5min、窗长<=20min"""
    eff = df[df["tier"].isin(["semi", "novel"])].sort_values("timestamp").reset_index(drop=True)
    if eff.empty:
        return pd.DataFrame(columns=["window_id", "st_ms", "ed_ms", "n_alarms", "minutes",
                                     "n_entities", "n_all_alarms"])
    ts = eff["timestamp"].values
    wid, start_idx = [0], 0
    for i in range(1, len(ts)):
        if ts[i] - ts[i-1] <= GAP_MS and ts[i] - ts[start_idx] <= MAX_DUR_MS:
            wid.append(wid[-1])
        else:
            wid.append(wid[-1] + 1)
            start_idx = i
    eff = eff.assign(w=wid)
    wins = eff.groupby("w").agg(st_ms=("timestamp", "min"), ed_ms=("timestamp", "max"),
                                n_alarms=("timestamp", "size"), n_entities=("entity", "nunique"))
    wins["minutes"] = ((wins.ed_ms - wins.st_ms) / 60_000).round().astype(int) + 1
    wins["n_all_alarms"] = wins.apply(
        lambda r: int(((df.timestamp >= r.st_ms) & (df.timestamp <= r.ed_ms)).sum()), axis=1)
    wins = wins.reset_index(drop=True)
    wins.insert(0, "window_id", wins.index)
    return wins


def run_pipeline(df_raw: pd.DataFrame, mapping: dict | None = None,
                 entity_regex: str | None = None):
    """上传的原始表 → (带标注告警, 窗口表, 附加信息)。任一步失败抛 ValueError 由界面提示。

    mapping["entity"] == ENTITY_FROM_CONTENT 时走「从内容抽实体」路线，
    抽取统计放进 info["entity_stats"] 供界面展示核对。
    """
    a = normalize_alarms(df_raw, mapping)
    if len(a) < 5:
        raise ValueError(f"告警条数过少（{len(a)} 条），不足以演示处理链路")
    a = mine_templates(a)
    info = {}
    if (mapping or {}).get("entity") == ENTITY_FROM_CONTENT:
        a, info["entity_stats"] = apply_entity_extraction(a, entity_regex)
    a = tag_tiers(a)
    wins = detect_windows(a)
    return a, wins, info
