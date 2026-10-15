# -*- coding: utf-8 -*-
"""
M4 处置单与工单生成（D4 · B 角色）

输入：
  - results/flood_windows.parquet（洪水窗口）
  - results/alarms_tagged.parquet（窗口内告警证据）
  - config/teams.csv（实体 → 责任团队映射，工单分派用）
根因来源：
  - 默认 rank-source=rule：复用 A 的 modules/m3_rank.rank_entities（与看板页签一一致、可审计）
  - 可选 rank-source=llm：走 m3_llm 的 LLM 排序
输出：
  - results/reports/window_{id}.json，schema 对齐 C 的 app.py：
    summary / root_causes[{rank,entity,confidence,evidence,fault_type}] /
    actions[{order,action,priority}] / suppressed / ticket{...} / disclaimer

安全设计（答辩可讲）：
  1. LLM 只写 summary / root_causes / actions / suppressed；
     ticket 的 assignee_team、title、priority、related_changes 全部由代码确定性生成，不让 LLM 编。
  2. 校验器：root_causes 的 entity 必须来自窗口证据，违规 → 打回重生成（<=2 次），仍失败 → 降级为模板化处置单。
  3. related_changes 为"模拟"：数据集无变更记录，用"窗口起点前 2 小时内同实体的最早告警"代替，
     代码与输出中均如实标注（禁止当作真实变更单）。

用法：
  python modules/m4_report.py --dry-run --limit 3      # 不调 API，验证 schema 与流水线
  python modules/m4_report.py --min-alarms 10          # 真实生成（需 .env 里的 OPENAI_API_KEY）
  python modules/m4_report.py --window-ids 377,1483
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(Path(__file__).parent))
from m3_llm import (ALARMS, FLOOD, CACHE_DIR, build_evidence, window_summary,
                    evidence_to_text, extract_json, load_dotenv, make_client,
                    llm_predict)
from m3_rank import rank_entities

REPORTS = ROOT / "results" / "reports"
TEAMS_CSV = ROOT / "config" / "teams.csv"
FIXED_DISCLAIMER = "本处置单由 AI 生成，供值班人员参考，执行前请核对现场情况。"
SYSTEM = "你是一名企业 IT 系统资深 SRE，擅长告警根因分析与应急处置。"

M4_USER = """下面是某企业 IT 系统一个告警洪水窗口的证据与根因候选，请生成一份结构化处置单。

【窗口概况】__SUMMARY__

【证据列表】
__EVIDENCE__

【规则排序给出的根因候选】（确定性算法，可参考，也可据证据纠正）
__CANDIDATES__

要求：
1. 只能引用上面出现过的实体与告警，禁止编造未出现的实体、告警或变更单号；
2. root_causes 按置信度降序取 1-3 个；每个含 entity（必须来自[证据列表]）、confidence(0~1)、
   evidence（<=80字，说明判断依据）、fault_type（CPU/MEMORY/DISK/NETWORK/JVM 之一，无法判断则空串）；
3. 若窗口内没有任何新发(novel)告警，说明证据不足，confidence 不得超过 0.4 并在 evidence 中说明；
4. actions 必须具体可执行、分优先级（高/中/低）、不超过 5 条；
5. 严格只输出一个 JSON 对象，不要任何多余文字：
{"summary":"...","root_causes":[{"rank":1,"entity":"...","confidence":0.0,"evidence":"...","fault_type":""}],"actions":[{"order":1,"action":"...","priority":"高"}],"suppressed":"..."}
"""


def load_teams() -> dict:
    if not TEAMS_CSV.exists():
        return {}
    df = pd.read_csv(TEAMS_CSV)
    return dict(zip(df["entity"], df["team"]))


def pick_candidates(wdf: pd.DataFrame, rank_source: str, client=None, model=None) -> list:
    """根因候选（规则或 LLM），统一成 [{entity,score,novel,total,first}]。"""
    if rank_source == "llm":
        ranked, _, _ = llm_predict(build_evidence(wdf, "llm"), client, model, "llm")
        scores = pd.Series({e: 1.0 / (i + 1) for i, e in enumerate(ranked)})
    else:
        scores = rank_entities(wdf, "rule").sort_values(ascending=False)
    cands = []
    for e, s in scores.head(5).items():
        sub = wdf[wdf.entity == e]
        cands.append({
            "entity": e,
            "score": float(s),
            "novel": int((sub.tier == "novel").sum()),
            "total": int(len(sub)),
            "first": pd.to_datetime(int(sub.timestamp.min()), unit="ms").strftime("%H:%M:%S"),
        })
    return cands


def candidates_to_text(cands: list) -> str:
    return "\n".join(
        f"{i}. {c['entity']}  score={c['score']:.2f}  新发={c['novel']}  "
        f"总告警={c['total']}  首现={c['first']}" for i, c in enumerate(cands, 1)
    )


def call_llm(items: list, cands: list, client, model, violations=None):
    """一次 LLM 调用产出处置单主体（带本地缓存）。"""
    user = (M4_USER
            .replace("__SUMMARY__", window_summary(items))
            .replace("__EVIDENCE__", evidence_to_text(items))
            .replace("__CANDIDATES__", candidates_to_text(cands)))
    if violations:
        user += (f"\n\n注意：上一次输出引用了不存在的实体 {violations}，"
                 f"本次必须只使用[证据列表]中出现过的实体。")
    key = hashlib.sha1(("m4\n" + user).encode("utf-8")).hexdigest()
    cp = CACHE_DIR / f"m4_{key}.json"
    if cp.exists():
        return json.loads(cp.read_text(encoding="utf-8"))
    resp = client.chat.completions.create(
        model=model, temperature=0,
        messages=[{"role": "system", "content": SYSTEM},
                  {"role": "user", "content": user}])
    content = resp.choices[0].message.content
    payload = {"raw": content, "parsed": extract_json(content),
               "usage": {"prompt_tokens": getattr(resp.usage, "prompt_tokens", None),
                         "completion_tokens": getattr(resp.usage, "completion_tokens", None)}}
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def validate_entities(obj: dict, valid: set) -> list:
    """幻觉校验：root_causes 的 entity 必须来自窗口证据。"""
    bad = []
    for c in obj.get("root_causes", []):
        if c.get("entity") not in valid:
            bad.append(c.get("entity"))
    return bad


def normalize_core(core: dict) -> dict:
    """归一化 LLM 输出：confidence→float(0~1)、priority 限定 高/中/低、rank 重排、字段转 str。"""
    out = {"summary": str(core.get("summary", "")), "root_causes": [], "actions": [],
           "suppressed": str(core.get("suppressed", ""))}
    for i, c in enumerate(core.get("root_causes", [])[:3]):
        try:
            conf = float(c.get("confidence"))
        except (TypeError, ValueError):
            conf = 0.0
        out["root_causes"].append({
            "rank": i + 1, "entity": str(c.get("entity", "")),
            "confidence": max(0.0, min(1.0, conf)),
            "evidence": str(c.get("evidence", "")),
            "fault_type": str(c.get("fault_type") or ""),
        })
    for i, a in enumerate(core.get("actions", [])[:5]):
        pri = a.get("priority")
        out["actions"].append({
            "order": i + 1, "action": str(a.get("action", "")),
            "priority": pri if pri in ("高", "中", "低") else "中"})
    return out


def degrade_core(w, wdf: pd.DataFrame, cands: list) -> dict:
    """降级：LLM 不可用或校验不通过时，用规则候选填模板化处置单。"""
    name = cands[0]["entity"] if cands else "未知实体"
    rc = [{"rank": i + 1, "entity": c["entity"],
           "confidence": 0.3 if i == 0 else 0.15,
           "evidence": f"规则排序给出（降级模式，未经 LLM 复核）：新发 {c['novel']} 条 / "
                       f"总告警 {c['total']} 条 / 首现 {c['first']}",
           "fault_type": ""} for i, c in enumerate(cands[:3])]
    return {
        "summary": f"本窗口共 {int(w.n_all_alarms)} 条告警，涉及 {int(wdf.entity.nunique())} 个实体、"
                   f"{int(wdf.template.nunique())} 个模板；规则排序首选根因为 {name}"
                   f"（降级处置单，未经 LLM 复核）。",
        "root_causes": rc,
        "actions": [{"order": 1, "action": f"人工复核 {name} 在窗口内的告警与近期变更，确认根因",
                     "priority": "高"}],
        "suppressed": "其余告警判定为背景常驻/半常驻与连锁衍生告警，已归并，不纳入本处置单。",
    }


def simulate_related_change(w, alarms: pd.DataFrame, entity: str) -> str:
    """数据集无变更记录 → 用窗口起点前 2 小时内同实体的最早告警模拟（如实标注）。"""
    start = int(w.st_ms)
    sub = alarms[(alarms.entity == entity) & (alarms.timestamp < start) &
                 (alarms.timestamp >= start - 2 * 3_600_000)]
    if sub.empty:
        return ""
    t = pd.to_datetime(int(sub.timestamp.min()), unit="ms").strftime("%m-%d %H:%M")
    return f"模拟关联（数据集无变更记录，非真实变更单）：{entity} 窗口前 2 小时内最早告警 {t}"


def make_ticket(w, wdf: pd.DataFrame, core: dict, teams: dict, alarms: pd.DataFrame) -> dict:
    top = core["root_causes"][0] if core.get("root_causes") else {"entity": "未知", "confidence": 0}
    e = top.get("entity", "未知")
    ft = (top.get("fault_type") or "").strip()
    novel = int(((wdf.entity == e) & (wdf.tier == "novel")).sum())
    conf = float(top.get("confidence") or 0)
    pri = "P1" if (novel >= 1 and conf >= 0.7) else ("P2" if novel >= 1 else "P3")
    return {
        "title": f"[根因处置] {e}{(' ' + ft) if ft else ''} 引发告警洪水窗口 #{int(w.window_id)}",
        "assignee_team": teams.get(e, "待分派"),
        "priority": pri,
        "related_changes": simulate_related_change(w, alarms, e),
        "root_cause_link": f"results/reports/window_{int(w.window_id)}.json",
    }


def generate_report(w, alarms, teams, client, model, rank_source, dry_run):
    wdf = alarms[(alarms.timestamp >= int(w.st_ms)) & (alarms.timestamp <= int(w.ed_ms))]
    items = build_evidence(wdf, "llm")
    valid = {it["entity"] for it in items}
    cands = pick_candidates(wdf, rank_source, client, model)

    retries, core = 0, None
    if not dry_run:
        violations = None
        for attempt in range(3):
            parsed = call_llm(items, cands, client, model, violations)["parsed"]
            bad = validate_entities(parsed, valid)
            if not bad and parsed.get("root_causes"):
                core, retries = parsed, attempt
                break
            violations = bad
    downgraded = core is None
    if not downgraded:
        core = normalize_core(core)
        if not core["root_causes"]:
            downgraded = True
    if downgraded:
        core = degrade_core(w, wdf, cands)
        retries = 2

    report = {
        "window_id": int(w.window_id),
        "window": {"st_time": pd.to_datetime(int(w.st_ms), unit="ms").strftime("%Y-%m-%d %H:%M:%S"),
                   "ed_time": pd.to_datetime(int(w.ed_ms), unit="ms").strftime("%Y-%m-%d %H:%M:%S"),
                   "n_all_alarms": int(w.n_all_alarms), "minutes": int(w.minutes)},
        "generated_at": pd.Timestamp.now().isoformat(timespec="seconds"),
        "summary": core["summary"],
        "root_causes": core["root_causes"],
        "actions": core["actions"],
        "suppressed": core["suppressed"],
        "ticket": make_ticket(w, wdf, core, teams, alarms),
        "disclaimer": FIXED_DISCLAIMER,
    }
    return report, retries, downgraded


def spotcheck() -> pd.DataFrame:
    """程序化抽检：对已落盘处置单逐份核查（D4 验收物）。"""
    alarms = pd.read_parquet(ALARMS)
    floods = pd.read_parquet(FLOOD)
    teams = load_teams()
    rows = []
    for p in sorted(REPORTS.glob("window_*.json")):
        r = json.loads(p.read_text(encoding="utf-8"))
        wid = r.get("window_id")
        w = floods[floods.window_id == wid]
        valid = set()
        if len(w):
            w0 = w.iloc[0]
            valid = set(alarms[(alarms.timestamp >= int(w0.st_ms)) &
                               (alarms.timestamp <= int(w0.ed_ms))].entity)
        schema_ok = all(k in r for k in ["summary", "root_causes", "actions",
                                         "suppressed", "ticket", "disclaimer"])
        ent_ok = bool(r.get("root_causes")) and all(
            c.get("entity") in valid for c in r["root_causes"])
        team = r.get("ticket", {}).get("assignee_team", "")
        team_ok = team in set(teams.values())
        n_act = len(r.get("actions", []))
        rows.append({
            "window_id": wid, "schema": schema_ok, "entity_in_evidence": ent_ok,
            "team_mapped": team_ok, "disclaimer": bool(r.get("disclaimer")),
            "actions_1_5": 1 <= n_act <= 5, "priority": r.get("ticket", {}).get("priority"),
            "root": r["root_causes"][0]["entity"] if r.get("root_causes") else "-",
        })
    df = pd.DataFrame(rows)
    md = ["# M4 处置单抽检记录（D4）", "",
          f"- 处置单总数：{len(df)}",
          f"- schema 完整：{int(df.schema.sum())}/{len(df)}",
          f"- 根因实体均在窗口证据内（无幻觉）：{int(df.entity_in_evidence.sum())}/{len(df)}",
          f"- 责任团队全部查表命中：{int(df.team_mapped.sum())}/{len(df)}",
          f"- 免责声明齐备：{int(df.disclaimer.sum())}/{len(df)}",
          f"- 处置建议 1~5 条：{int(df.actions_1_5.sum())}/{len(df)}",
          "",
          "## 抽样明细（前 12 份）", "",
          "| window | 根因 | 优先级 | schema | 实体无幻觉 | 团队 | 免责 |",
          "|---|---|---|---|---|---|---|"]
    for _, x in df.head(12).iterrows():
        md.append(f"| #{x.window_id} | {x.root} | {x.priority} | {x.schema} | "
                  f"{x.entity_in_evidence} | {x.team_mapped} | {x.disclaimer} |")
    (REPORTS / "_spotcheck.md").write_text("\n".join(md), encoding="utf-8")
    return df


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="不调 LLM，生成模板化降级处置单以验证 schema")
    ap.add_argument("--min-alarms", type=int, default=10, help="只处理 n_all_alarms>=N 的窗口")
    ap.add_argument("--limit", type=int, default=None, help="最多处理窗口数")
    ap.add_argument("--window-ids", type=str, default=None, help="逗号分隔的窗口 id（指定则忽略 --min-alarms）")
    ap.add_argument("--rank-source", choices=["rule", "llm"], default="rule")
    ap.add_argument("--spotcheck", action="store_true", help="只对已落盘处置单做程序化抽检")
    args = ap.parse_args()

    load_dotenv(ROOT.parent / ".env")

    if args.spotcheck:
        df = spotcheck()
        print(f"[m4] spotcheck: {len(df)} reports, "
              f"schema={int(df.schema.sum())}, entity_safe={int(df.entity_in_evidence.sum())}, "
              f"team_mapped={int(df.team_mapped.sum())}, actions_ok={int(df.actions_1_5.sum())}")
        print(f"[m4] written -> {REPORTS / '_spotcheck.md'}")
        return

    client = model = None
    if not args.dry_run:
        client, model = make_client()

    alarms = pd.read_parquet(ALARMS)
    floods = pd.read_parquet(FLOOD)
    teams = load_teams()

    if args.window_ids:
        ids = [int(x) for x in args.window_ids.split(",")]
        sel = floods[floods.window_id.isin(ids)]
    else:
        sel = floods[floods.n_all_alarms >= args.min_alarms]
    sel = sel.sort_values("n_all_alarms", ascending=False)
    if args.limit:
        sel = sel.head(args.limit)

    REPORTS.mkdir(parents=True, exist_ok=True)
    n_down = 0
    for _, w in sel.iterrows():
        rep, retries, downgraded = generate_report(w, alarms, teams, client, model,
                                                   args.rank_source, args.dry_run)
        (REPORTS / f"window_{int(w.window_id)}.json").write_text(
            json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
        n_down += downgraded
        status = "DOWNGRADED" if downgraded else ("PASS" if retries == 0 else f"PASS(retry={retries})")
        top = rep["root_causes"][0]["entity"] if rep["root_causes"] else "-"
        print(f"[m4] window #{int(w.window_id)}: {status}  root={top}  "
              f"team={rep['ticket']['assignee_team']}  {rep['ticket']['priority']}")
    print(f"[m4] wrote {len(sel)} reports -> {REPORTS}  (downgraded={n_down})")


if __name__ == "__main__":
    main()
