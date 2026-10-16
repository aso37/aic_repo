# -*- coding: utf-8 -*-
"""M3 路线 C：rule × LLM 加权融合（D6）

做什么
--------------------------------------------------------------------
把两条已有路线合成一条，作为答辩的「最终方法」：

    fused(e) = α · rule_score(e) + (1 − α) · llm_conf(e)

- `rule_score`：`m3_rank.rank_entities(wdf, "rule")` 的输出。
  它是四路 min-max 加权和、权重合计 1.0，**天然落在 [0,1]**，不需要再归一化。
- `llm_conf`：`results/llm_rankings.json` 里该窗候选的 `confidence`（0.1~0.9）。
  **同一实体可能出现多次**（LLM 对同一实体给了不同模板），取其中**最大置信度**。
- 实体全集 = rule 的实体 ∪ LLM 的实体；LLM 未提及的实体记 0 分。

α 的选择（本模块最重要的方法论）
--------------------------------------------------------------------
α 是唯一超参数。**在同一批窗口上挑 α、又在同一批窗口上报成绩 = 过拟合**，
这是评审最容易质疑的一点。因此本模块给出两个数字：

1. **主结果：5 折交叉验证**（按故障类型分层）
   每折在训练折上挑 α，只在未参与挑选的验证折上评分，最后汇总。
2. **参考：全量挑 α 的成绩**（附明确的过拟合警示，不作为结论）

挑 α 的规则：先比 Top-1，再比 Top-3，**并列时取更大的 α**
（即更偏向可审计的 rule 路线——同样好时优先选可解释的那一侧）。

网格含两个端点：α=1.0 即纯 rule，α=0.0 即纯 LLM，作为对照。

缺口与去重
--------------------------------------------------------------------
- 若某窗 LLM 无候选 → 只用 rule，并**计数披露**（不静默混算）
- LLM 排名**按实体严格去重**（实测原始候选里同一实体重复出现，
  这会虚高 Top-3；本模块顺带输出该诊断）

用法
--------------------------------------------------------------------
    .venv\\Scripts\\python.exe modules\\m3_fusion.py
    .venv\\Scripts\\python.exe modules\\m3_fusion.py --folds 5 --seed 42

产出
--------------------------------------------------------------------
- `results/fusion_results.json`  供 `m3_rank.load_extra_rows()` 自动渲染进对比表
- `results/fusion_experiments.md` 完整报告（网格表 / 交叉验证 / 分类型 / 结论）
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "modules"))
import m3_rank  # noqa: E402

ALARMS = ROOT / "results" / "alarms_tagged.parquet"
FLOOD = ROOT / "results" / "flood_windows.parquet"
FAULTS = ROOT / "results" / "fault_windows.parquet"
LLM_RANK = ROOT / "results" / "llm_rankings.json"
OUT_JSON = ROOT / "results" / "fusion_results.json"
OUT_MD = ROOT / "results" / "fusion_experiments.md"

ALPHAS = [round(0.1 * i, 1) for i in range(11)]      # 0.0 ~ 1.0


# ---------------------------------------------------------------------------
# 数据装载
# ---------------------------------------------------------------------------

def load_llm():
    """读 LLM 排序结果。

    返回 {fault_id: {"scores": {entity: 最大置信度},
                     "raw": [原始实体序列（含重复）],
                     "order": [按实体去重后的实体序列]}}
    `raw` 用于去重诊断；`order` 用于融合的秩化 tiebreak。
    """
    if not LLM_RANK.exists():
        raise SystemExit(f"[fusion] 找不到 {LLM_RANK}，请先跑 modules/m3_llm.py")
    doc = json.loads(LLM_RANK.read_text(encoding="utf-8"))
    out = {}
    for w in doc["windows"]:
        scores, raw = {}, []
        for c in w.get("candidates", []):
            e = c.get("entity")
            if not e:
                continue
            raw.append(e)
            conf = c.get("confidence")
            conf = float(conf) if isinstance(conf, (int, float)) else 0.0
            scores[e] = max(scores.get(e, 0.0), conf)
        out[int(w["fault_id"])] = {"scores": scores, "raw": raw,
                                   "order": list(dict.fromkeys(raw))}
    return out, doc.get("model", "?"), doc.get("generated_at", "?")


def select_windows(alarms, floods, faults):
    """逐行复现 m3_rank / m3_llm 的「可检测故障」筛选，保证分母一致。"""
    wins = []
    for _, w in faults[faults.data_type == "train"].iterrows():
        fl = floods[(floods.st_ms <= w.ed_ms + 600_000) &
                    (floods.ed_ms >= w.st_ms - 600_000)]
        if len(fl) == 0:
            continue
        st, ed = fl.st_ms.min(), fl.ed_ms.max()
        wdf = alarms[(alarms.timestamp >= st) & (alarms.timestamp <= ed)]
        if wdf.empty or w.service not in wdf.entity.values:
            continue
        wins.append({
            "id": int(w.id),
            "root": w.service,
            "type": str(w.anomaly_type).split(";")[0].strip(),
            "wdf": wdf,
        })
    return wins


# ---------------------------------------------------------------------------
# 融合与评测
# ---------------------------------------------------------------------------

def _rank_tie(order):
    """把一条有序实体序列转成 [0,1] 的"秩分"（越靠前越高），用作并列时的确定性 tiebreak。"""
    n = len(order)
    if n == 0:
        return pd.Series(dtype=float)
    return pd.Series({e: 1.0 - i / max(n - 1, 1) for i, e in enumerate(order)})


def fuse(rule_s: pd.Series, llm_info: dict, alpha: float) -> pd.Series:
    """α·rule_score + (1−α)·llm_conf，返回按分数降序的 Series。

    ⚠️ 为什么还要 tiebreak：两个分量都有**大量并列值**
    （rule 在"窗内无新发告警"时 minmax 退化为全 1.0；LLM 置信度只有 10 个离散值）。
    若只按分数排序，并列顺序是任意的 —— 会导致 **α=0 不等于"纯 LLM 路线"**、
    α=1 不等于"纯 rule 路线"，两个对照端点都失真。

    因此再加一级**秩化 tiebreak**：`α·rule秩分 + (1−α)·llm秩分`，
    这样 α=1 精确复现 rule 的原序、α=0 精确复现 LLM 的原序（按实体去重）。
    """
    llm_scores = llm_info.get("scores", {})
    idx = sorted(set(rule_s.index) | set(llm_scores))

    r = rule_s.reindex(idx, fill_value=0.0).astype(float)
    l = pd.Series({e: float(llm_scores.get(e, 0.0)) for e in idx}, index=idx)
    fused = alpha * r + (1.0 - alpha) * l

    rule_tie = _rank_tie(list(rule_s.sort_values(ascending=False).index))
    llm_tie = _rank_tie(llm_info.get("order", []))
    tie = (alpha * rule_tie.reindex(idx, fill_value=0.0)
           + (1.0 - alpha) * llm_tie.reindex(idx, fill_value=0.0))

    df = pd.DataFrame({"fused": fused.values, "tie": tie.values, "name": idx})
    df = df.sort_values(["fused", "tie", "name"],
                        ascending=[False, False, True])
    return pd.Series(df["fused"].values, index=df["name"].values)


def _ranked(w, llm, alpha):
    info = llm.get(w["id"], {"scores": {}, "order": []})
    s = fuse(m3_rank.rank_entities(w["wdf"], "rule"), info, alpha)
    return s, bool(info.get("scores"))


def eval_at(windows, llm, alpha, keep_detail=False):
    """在给定窗口集合上按 α 评测，返回 (top1, top3, n_eval, per_type, no_llm, detail)。"""
    top1 = top3 = no_llm = 0
    per_type = {}
    detail = []
    for w in windows:
        s, has_llm = _ranked(w, llm, alpha)
        if not has_llm:
            no_llm += 1
        ranked = list(s.index)
        h1 = bool(ranked) and ranked[0] == w["root"]
        h3 = w["root"] in ranked[:3]
        top1 += h1
        top3 += h3
        d = per_type.setdefault(w["type"], [0, 0, 0])
        d[0] += h1
        d[1] += h3
        d[2] += 1
        if keep_detail:
            detail.append({
                "fault": w["id"], "root": w["root"], "type": w["type"],
                "top3": ranked[:3], "top1": bool(h1), "top3_hit": bool(h3),
                "has_llm": has_llm,
            })
    return top1, top3, len(windows), per_type, no_llm, detail


def pick_alpha(windows, llm, alphas):
    """在给定窗口集合上挑 α：先 Top-1、再 Top-3、并列取更大的 α。"""
    best_a, best_key = alphas[-1], None
    for a in alphas:
        t1, t3, n, _, _, _ = eval_at(windows, llm, a)
        key = (t1, t3)
        if best_key is None or key > best_key or (key == best_key and a > best_a):
            best_key, best_a = key, a
    return best_a, best_key


def make_folds(windows, n_fold, seed):
    """按故障类型分层的折划分（同一类型轮流分配到各折）。"""
    rng = np.random.default_rng(seed)
    by_type = {}
    for i, w in enumerate(windows):
        by_type.setdefault(w["type"], []).append(i)
    folds = [[] for _ in range(n_fold)]
    for _, idxs in sorted(by_type.items()):
        idxs = list(idxs)
        rng.shuffle(idxs)
        for j, i in enumerate(idxs):
            folds[j % n_fold].append(i)
    return folds


def cross_validate(windows, llm, alphas, n_fold, seed):
    """5 折交叉验证：每折在训练折挑 α，在验证折评分。"""
    folds = make_folds(windows, n_fold, seed)
    cv1 = cv3 = cvn = 0
    chosen, fold_rows = [], []
    for k in range(n_fold):
        test_idx = set(folds[k])
        train = [w for i, w in enumerate(windows) if i not in test_idx]
        test = [w for i, w in enumerate(windows) if i in test_idx]
        a, _ = pick_alpha(train, llm, alphas)
        t1, t3, n, _, _, _ = eval_at(test, llm, a)
        cv1 += t1
        cv3 += t3
        cvn += n
        chosen.append(a)
        fold_rows.append({"fold": k + 1, "n_train": len(train), "n_test": n,
                          "alpha": a, "top1": t1, "top3": t3})
    return cv1, cv3, cvn, chosen, fold_rows


# ---------------------------------------------------------------------------
# 诊断
# ---------------------------------------------------------------------------

def llm_protocols(windows, llm):
    """LLM 路线在两种「候选集口径」下的成绩 —— 两者不可直接比较。

    背景（本模块实测发现的口径不一致）：
      · `m3_rank.evaluate()` 对**窗内全部实体**排名 → rule 行是这个口径
      · `m3_llm.py` 的实现只对**LLM 自己点名的实体**排名（≤5 个）→ llm 行是这个口径

    当 LLM 点名的实体少于 3 个时，两种口径会给出不同结论。
    实测：窗口 #1872（NETWORK，根因 MG01）窗内只有 2 个实体，
    LLM 只点名了 1 个（Mysql02）→ 原生口径漏掉，全候选集口径命中。

    返回 {"native": (t1,t3,n), "full": (t1,t3,n)}
    """
    nat1 = nat3 = full1 = full3 = n = 0
    for w in windows:
        info = llm.get(w["id"])
        if not info or not info.get("order"):
            continue
        n += 1
        root = w["root"]
        order = info["order"]                       # 原生：只排 LLM 点名的
        nat1 += order[0] == root
        nat3 += root in order[:3]
        s, _ = _ranked(w, llm, 0.0)                 # 全候选集：与 rule 同口径
        full = list(s.index)
        full1 += bool(full) and full[0] == root
        full3 += root in full[:3]
    return {"native": (nat1, nat3, n), "full": (full1, full3, n)}


def llm_order_preserved(windows, llm):
    """自检：α=0 时，融合在「LLM 点名过的实体」内部的相对顺序必须与 LLM 原序一致。

    这是对 `fuse()` 的秩化 tiebreak 的独立校验（不依赖分数，只查顺序）。
    返回 (是否全部一致, 反例列表)
    """
    bad = []
    for w in windows:
        order = llm.get(w["id"], {}).get("order", [])
        if not order:
            continue
        s, _ = _ranked(w, llm, 0.0)
        named = set(order)
        got = [e for e in s.index if e in named]
        if got != order:
            bad.append({"fault": w["id"], "期望": order, "实际": got})
    return (len(bad) == 0), bad


def llm_dedup_diagnostic(windows, llm):
    """LLM 排名按实体去重前后的 Top-1/Top-3 对比。

    背景：实测 LLM 候选里同一实体会重复出现（同一实体给了不同模板），
    若直接用原始列表取前 3，会把重复项当成 3 个位置，虚高 Top-3。
    """
    raw1 = raw3 = ded1 = ded3 = n_dup_win = n = 0
    for w in windows:
        info = llm.get(w["id"])
        if not info:
            continue
        n += 1
        raw = info["raw"]
        ded = list(dict.fromkeys(raw))          # 保序去重
        if len(raw) != len(ded):
            n_dup_win += 1
        root = w["root"]
        raw1 += bool(raw) and raw[0] == root
        raw3 += root in raw[:3]
        ded1 += bool(ded) and ded[0] == root
        ded3 += root in ded[:3]
    return {"n": n, "n_dup_win": n_dup_win,
            "raw": (raw1, raw3), "dedup": (ded1, ded3)}


def complementarity(windows, llm, alpha):
    """融合相对单路线的增量：哪些窗口被救回 / 被弄坏。"""
    rows = []
    for w in windows:
        s_only_rule, _ = _ranked(w, llm, 1.0)
        s_only_llm, _ = _ranked(w, llm, 0.0)
        s_fuse, _ = _ranked(w, llm, alpha)
        rr, rl, rf = list(s_only_rule.index), list(s_only_llm.index), list(s_fuse.index)
        root = w["root"]
        rows.append({
            "fault": w["id"], "type": w["type"], "root": root,
            "rule_h1": bool(rr) and rr[0] == root, "llm_h1": bool(rl) and rl[0] == root,
            "fuse_h1": bool(rf) and rf[0] == root,
            "rule_h3": root in rr[:3], "llm_h3": root in rl[:3], "fuse_h3": root in rf[:3],
        })
    df = pd.DataFrame(rows)
    gain = df[(~df.rule_h1) & (~df.llm_h1) & df.fuse_h1]
    lose = df[(df.rule_h1 | df.llm_h1) & (~df.fuse_h1)]
    return df, gain, lose


def fmt(num, den):
    # 注意：gdf.iterrows() 会把整行转成同一 dtype，int 列会变成 float（35.0），
    # 所以这里显式取整。
    return f"{int(num)}/{int(den)} ({num / max(den, 1) * 100:.0f}%)"


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--folds", type=int, default=5, help="交叉验证折数（默认 5）")
    ap.add_argument("--seed", type=int, default=42, help="折划分随机种子")
    args = ap.parse_args()

    alarms = pd.read_parquet(ALARMS)
    floods = pd.read_parquet(FLOOD)
    faults = pd.read_parquet(FAULTS)
    llm, model, gen_at = load_llm()
    wins = select_windows(alarms, floods, faults)

    print(f"[fusion] 可评测窗口 {len(wins)} 个（应与 rule/llm 的 n_eval 一致）")
    print(f"[fusion] LLM 结果：model={model} generated_at={gen_at} "
          f"覆盖 {len(llm)} 个窗口")

    # ---- 1. 全量网格（参考，附过拟合警示）----
    grid = []
    for a in ALPHAS:
        t1, t3, n, _, no_llm, _ = eval_at(wins, llm, a)
        grid.append({"alpha": a, "top1": t1, "top3": t3, "n_eval": n, "no_llm": no_llm})
    gdf = pd.DataFrame(grid)
    best_full_a, best_full_key = pick_alpha(wins, llm, ALPHAS)
    print(f"\n[fusion] 全量网格最优 α={best_full_a}  "
          f"Top-1={fmt(best_full_key[0], len(wins))}  Top-3={fmt(best_full_key[1], len(wins))}"
          f"   （参考值，存在过拟合风险）")

    # ---- 端点自检（硬校验）----
    # α=1 必须精确复现纯 rule（独立于本模块的 m3_rank.evaluate 结果）
    # α=0 必须精确保持 LLM 的原序（在它点名过的实体内部）
    _g1 = next(g for g in grid if g["alpha"] == 1.0)
    _ref = m3_rank.evaluate(alarms, floods, faults, "rule")
    ok1 = (_g1["top1"], _g1["top3"]) == (_ref["top1"], _ref["top3"])
    ok0, bad0 = llm_order_preserved(wins, llm)
    print(f"[fusion] 端点自检 α=1（应=纯rule {(_ref['top1'], _ref['top3'])}）: "
          f"{(_g1['top1'], _g1['top3'])}  {'[OK]' if ok1 else '[!!] 不一致'}")
    print(f"[fusion] 端点自检 α=0（LLM 原序是否被保持）: "
          f"{'[OK]' if ok0 else '[!!] ' + str(bad0[:2])}")
    if not (ok0 and ok1):
        print("[fusion][warn] 端点未对齐 —— 融合的排序/tiebreak 逻辑有问题，结果不可信")

    # ---- LLM 两种候选集口径（口径不一致的实测证据）----
    prot = llm_protocols(wins, llm)
    print(f"[fusion] LLM 原生口径（只排自己点名的实体）  : "
          f"Top-1={fmt(prot['native'][0], prot['native'][2])}  "
          f"Top-3={fmt(prot['native'][1], prot['native'][2])}   ← 对比表里的 llm 行")
    print(f"[fusion] LLM 全候选集口径（与 rule 同口径）    : "
          f"Top-1={fmt(prot['full'][0], prot['full'][2])}  "
          f"Top-3={fmt(prot['full'][1], prot['full'][2])}")

    # ---- 2. 交叉验证（主结果）----
    cv1, cv3, cvn, chosen, fold_rows = cross_validate(
        wins, llm, ALPHAS, args.folds, args.seed)
    print(f"[fusion] {args.folds} 折交叉验证：Top-1={fmt(cv1, cvn)}  Top-3={fmt(cv3, cvn)}")
    print(f"[fusion] 各折选中的 α：{chosen}")

    # ---- 3. 去重诊断 ----
    ded = llm_dedup_diagnostic(wins, llm)

    # ---- 4. 用全量最优 α 出分类型明细与增量分析 ----
    t1, t3, n, per_type, no_llm, _ = eval_at(wins, llm, best_full_a)
    cdf, gain, lose = complementarity(wins, llm, best_full_a)
    r1, r3, _, _, _, _ = eval_at(wins, llm, 1.0)     # 纯 rule
    l1, l3, _, _, _, _ = eval_at(wins, llm, 0.0)     # 纯 LLM（已去重）

    # ---- 5. 写 fusion_results.json（供 m3_rank.load_extra_rows 渲染）----
    # 主数字用**无偏的交叉验证**结果，与 note 口径保持一致；
    # 全量调优的数字单独放在 full_tuned 里并标清"有偏"。
    OUT_JSON.write_text(json.dumps({
        "method": "fusion",
        "n_eval": cvn,
        "top1": cv1,
        "top3": cv3,
        "note": f"融合：α·rule+(1−α)·LLM；α 按 {args.folds} 折 CV 选取（无偏）",
        "full_tuned": {"alpha": best_full_a, "top1": t1, "top3": t3, "n_eval": n,
                       "caveat": "在同批窗口上选出，偏乐观，不作为结论"},
        "cv": {"top1": cv1, "top3": cv3, "n_eval": cvn, "chosen_alphas": chosen},
        "baseline_same_protocol": {"rule": {"top1": r1, "top3": r3},
                                   "llm_full_candidate": {"top1": l1, "top3": l3}},
        "llm_native_protocol": {"top1": prot["native"][0], "top3": prot["native"][1],
                                "n_eval": prot["native"][2],
                                "note": "只排 LLM 点名的实体；对比表 llm 行用的是这个口径"},
        "no_llm_windows": no_llm,
        "llm_dedup": ded,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[fusion] 已写入 {OUT_JSON}")

    # ---- 6. 写报告 ----
    write_report(wins, llm, model, gen_at, gdf, best_full_a, best_full_key,
                 (cv1, cv3, cvn), chosen, fold_rows, per_type, no_llm,
                 ded, cdf, gain, lose, (r1, r3, l1, l3), args, prot)
    print(f"[fusion] 已写入 {OUT_MD}")


def write_report(wins, llm, model, gen_at, gdf, best_a, best_key, cv, chosen,
                 fold_rows, per_type, no_llm, ded, cdf, gain, lose, base, args, prot):
    n = len(wins)
    cv1, cv3, cvn = cv
    r1, r3, l1, l3 = base
    L = []
    A = L.append

    A("# M3 路线 C：rule × LLM 加权融合（D6）")
    A("")
    A("> 本文件由 `modules/m3_fusion.py` 自动生成。")
    A(f"> LLM 结果来源：`results/llm_rankings.json`（model={model}，generated_at={gen_at}）")
    A(f"> 可评测窗口：**{n}** 个（逐行复现 m3_rank / m3_llm 的「可检测故障」筛选，分母一致）")
    A("")
    A("## 1. 方法")
    A("")
    A("对每个可评测窗口：")
    A("")
    A("```")
    A("fused(e) = α · rule_score(e) + (1 − α) · llm_conf(e)")
    A("```")
    A("")
    A("| 分量 | 来源 | 值域 | 说明 |")
    A("|---|---|---|---|")
    A("| `rule_score` | `m3_rank.rank_entities(wdf, \"rule\")` | **[0,1]** | 四路 min-max 加权和、权重合计 1.0，**天然落在 [0,1]，无需再归一化** |")
    A("| `llm_conf` | `llm_rankings.json` 的候选置信度 | [0.1, 0.9] | **同一实体出现多次时取最大置信度** |")
    A("")
    A(f"实体全集 = rule 的实体 ∪ LLM 的实体；LLM 未提及的实体记 0 分。"
      f"本批数据中 **{no_llm} 个窗口** LLM 无候选（若有则只用 rule，不计入 LLM 贡献）。")
    A("")
    A("## 2. α 的选择：为什么不能直接在评测集上挑")
    A("")
    A("α 是本方法唯一的超参数。**在同一批窗口上挑 α、又在同一批窗口上报成绩，等于过拟合**，")
    A("这是评审最容易质疑的一点。因此下面给出两个数字，**以交叉验证为准**。")
    A("")
    A(f"### 2.1 主结果：{args.folds} 折交叉验证（按故障类型分层）")
    A("")
    A("每折在训练折上挑 α，只在**未参与挑选**的验证折上评分：")
    A("")
    A("| 折 | 训练窗数 | 验证窗数 | 选中的 α | 该折 Top-1 | 该折 Top-3 |")
    A("|---|---|---|---|---|---|")
    for r in fold_rows:
        A(f"| {r['fold']} | {r['n_train']} | {r['n_test']} | {r['alpha']} | "
          f"{fmt(r['top1'], r['n_test'])} | {fmt(r['top3'], r['n_test'])} |")
    A(f"| **合计** | — | {cvn} | — | **{fmt(cv1, cvn)}** | **{fmt(cv3, cvn)}** |")
    A("")
    A(f"各折选中的 α：`{chosen}`")
    A("")
    A("### 2.2 参考：全量网格（⚠️ 存在过拟合风险，不作为结论）")
    A("")
    A("| α | Top-1 | Top-3 | 说明 |")
    A("|---|---|---|---|")
    for _, r in gdf.iterrows():
        tag = ""
        if r["alpha"] == 1.0:
            tag = "= 纯 rule（对照端点）"
        elif r["alpha"] == 0.0:
            tag = "= 纯 LLM（对照端点）"
        elif r["alpha"] == best_a:
            tag = "← 全量最优（**有偏**）"
        A(f"| {r['alpha']:.1f} | {fmt(r['top1'], n)} | {fmt(r['top3'], n)} | {tag} |")
    A("")
    A(f"全量最优 α = **{best_a}**，Top-1 = {fmt(best_key[0], n)}、Top-3 = {fmt(best_key[1], n)}。")
    A("**此数在同批窗口上选出，偏乐观，仅作参考。**")
    A("")
    A("## 3. 与单路线对比")
    A("")
    A("**统一口径**：下表主体三行都按「对**窗内全部实体**排名」计算 —— 这既是 `m3_rank` 的口径，")
    A("也是融合本身的口径。最后一行单列 LLM 的原生口径以便对照。")
    A("")
    A("| 方法 | Top-1 | Top-3 | 评测窗口数 | 候选集口径 |")
    A("|---|---|---|---|---|")
    A(f"| 纯 rule（α=1.0） | {fmt(r1, n)} | {fmt(r3, n)} | {n} | 窗内全部实体 |")
    A(f"| 纯 LLM（α=0.0） | {fmt(l1, n)} | {fmt(l3, n)} | {n} | 窗内全部实体 |")
    A(f"| **融合（{args.folds} 折 CV）** | **{fmt(cv1, cvn)}** | **{fmt(cv3, cvn)}** | {cvn} | 窗内全部实体 |")
    A(f"| 融合（全量 α={best_a}，有偏） | {fmt(best_key[0], n)} | {fmt(best_key[1], n)} | {n} | 窗内全部实体 |")
    A(f"| （参考）LLM 原生口径 | {fmt(prot['native'][0], prot['native'][2])} | "
      f"{fmt(prot['native'][1], prot['native'][2])} | {prot['native'][2]} | 只排 LLM 点名的实体 |")
    A("")
    A("> ⚠️ **最后一行为什么要单列**：`results/experiments.md` 里的 `llm` 行用的是")
    A("> **原生口径**（只对 LLM 自己点名的 ≤5 个实体排名），而 `rule` 行用的是「窗内全部实体」。")
    A("> **两行排的不是同一个候选集，严格说不可直接比较** —— 详见第 6.1 节。")
    A("")
    A("## 4. 分类型明细（融合，全量 α=" + f"{best_a}）")
    A("")
    A("| 故障类型 | Top-1 | Top-3 | 窗口数 |")
    A("|---|---|---|---|")
    for t, (a, b, c) in sorted(per_type.items(), key=lambda kv: -kv[1][2]):
        A(f"| {t} | {fmt(a, c)} | {fmt(b, c)} | {c} |")
    A("")
    A("## 5. 互补性分析")
    A("")
    A(f"⚠️ **前提**：最优 α = {best_a}，意味着融合**退化为纯 LLM**。因此下面的"
      "「救回 / 弄坏」实际上就是 rule 与 LLM 两条单路线的差异，**不是融合带来的增益**。")
    A("")
    A(f"全部窗口中被融合救回的（rule 与 LLM **单路都没命中 Top-1**，融合命中）：**{len(gain)} 个**")
    if len(gain):
        A("")
        A("| 故障 | 类型 | 真值根因 |")
        A("|---|---|---|")
        for _, r in gain.iterrows():
            A(f"| #{r['fault']} | {r['type']} | {r['root']} |")
    A("")
    A(f"被融合弄坏的（单路至少一个命中 Top-1，融合未命中）：**{len(lose)} 个**")
    if len(lose):
        A("")
        A("| 故障 | 类型 | 真值根因 | rule 命中 | LLM 命中 |")
        A("|---|---|---|---|---|")
        for _, r in lose.iterrows():
            A(f"| #{r['fault']} | {r['type']} | {r['root']} | "
              f"{'是' if r['rule_h1'] else '否'} | {'是' if r['llm_h1'] else '否'} |")
    A("")
    A("## 6. 附带发现：两处会让比较失真的口径问题")
    A("")
    A("### 6.1 候选集口径不一致（影响 rule 与 llm 的可比性）")
    A("")
    A("- `m3_rank.evaluate()` 对**窗内全部实体**排名 → `rule` 行是这个口径")
    A("- `m3_llm.py` 只对 **LLM 自己点名的实体**（≤5 个）排名 → `llm` 行是这个口径")
    A("")
    A("当 LLM 点名少于 3 个实体时，两种口径会给出不同结论。实测反例：")
    A("")
    A("| 窗口 | 类型 | 真值根因 | 窗内实体数 | LLM 点名数 | 原生 Top-3 | 全候选 Top-3 |")
    A("|---|---|---|---|---|---|---|")
    A("| #1872 | NETWORK | MG01 | 2 | 1 | `['Mysql02']` → 漏 | `['Mysql02','MG01']` → 命中 |")
    A("")
    A(f"因此 LLM 的成绩是：**原生口径** {fmt(prot['native'][0], prot['native'][2])} / "
      f"{fmt(prot['native'][1], prot['native'][2])}；"
      f"**全候选集口径** {fmt(prot['full'][0], prot['full'][2])} / "
      f"{fmt(prot['full'][1], prot['full'][2])}。")
    A("")
    A("**建议**：对比表里三行统一用「窗内全部实体」口径（本文件第 3 节已如此），")
    A("或在表里加一列注明口径。")
    A("")
    A("### 6.2 LLM 排名里的重复实体（会**低估** Top-3，非 bug）")
    A("")
    A("实测 LLM 候选里**同一实体会重复出现**（同一实体被标了不同模板），例如：")
    A("")
    A("```")
    A("rank1: apache01")
    A("rank2: apache01   ← 同一实体重复")
    A("rank3: MG01")
    A("```")
    A("")
    A("重复项会**白占位置**：若直接用原始列表取前 3，去重后本可进前 3 的实体被挤掉，")
    A("因此原始口径会**低估** Top-3。")
    A("")
    A("| 统计口径 | Top-1 | Top-3 |")
    A("|---|---|---|")
    A(f"| 原始列表（含重复，前 3 可能有重复项） | {fmt(ded['raw'][0], ded['n'])} | {fmt(ded['raw'][1], ded['n'])} |")
    A(f"| **按实体去重** | {fmt(ded['dedup'][0], ded['n'])} | {fmt(ded['dedup'][1], ded['n'])} |")
    A("")
    A(f"有重复实体的窗口：**{ded['n_dup_win']}/{ded['n']}**。")
    A(f"→ **`m3_llm.py` 的实现已按实体去重**（`candidates_to_entities()` 用 `e not in order` 过滤），"
      f"得到 {fmt(ded['dedup'][1], ded['n'])}，与 `experiments.md` 里 llm 行的数字**一致** —— "
      f"**这一项没有 bug**。本模块同样去重，以保持一致。")
    A("")
    A("## 7. 结论")
    A("")
    d1 = cv1 - max(r1, l1)
    A(f"- 融合的**无偏**估计（{args.folds} 折 CV）：Top-1 **{fmt(cv1, cvn)}**、Top-3 **{fmt(cv3, cvn)}**")
    A(f"- 单路线：rule {fmt(r1, n)} / {fmt(r3, n)}，LLM {fmt(l1, n)} / {fmt(l3, n)}")
    if d1 > 0:
        A(f"- 融合相对最强单路线（Top-1）**提升 {d1} 个窗口**，说明两条路线的证据互补。")
    elif d1 == 0:
        A("- 融合与最强单路线（Top-1）持平——两条路线证据重叠度高，融合未带来额外信息。")
    else:
        A(f"- ⚠️ 融合略低于最强单路线（{d1} 个窗口）。这是**诚实的负结果**："
          "说明在当前数据上两条路线的分歧多为「一错一对」，简单加权无法自动择善。")
    A(f"- 互补性：融合救回 {len(gain)} 个单路都漏的窗口，同时弄坏 {len(lose)} 个单路能命中的窗口。")
    A("")
    A("## 8. 局限（答辩如实说明）")
    A("")
    A("1. **LLM 置信度未校准**：这里把 `confidence` 当作可直接与 rule 分数相加的量。")
    A("   实测其为 0.1~0.9 的 10 个离散值，是模型自报值，与真实正确率未必对应；")
    A("   若做置信度校准（如 isotonic / 分箱），融合还有提升空间。")
    A("2. **评测集偏小**：72 个窗口，交叉验证每折验证集约 14 个窗口，指标波动较大。")
    A("3. **LLM 结果只有一份**：单一模型（" + f"{model}" + "）、单次运行，未做多次采样取平均。")
    A("4. **α 是全局单一值**：未按故障类型分别调 α（分类型调参会进一步放大过拟合风险）。")
    A("")
    A("## 9. 复现与接入对比表")
    A("")
    A("```bash")
    A(f"python modules/m3_fusion.py --folds {args.folds} --seed {args.seed}")
    A("```")
    A("")
    A("`m3_rank.load_extra_rows()` 会自动读取 `results/fusion_results.json` 并渲染成")
    A("对比表的 fusion 行 —— 但需要 `experiments.md` 里先放好")
    A("`<!-- AUTO-TABLE:BEGIN -->` / `<!-- AUTO-TABLE:END -->` 标记。")
    A("")
    A("⚠️ **不要直接把现有表格整段包进标记**：`load_extra_rows()` 只认")
    A("`results/*_results.json`，而 llm 两行目前来自 `llm_experiments.md`，")
    A("没有对应的 `llm_results.json` —— 包标记后重跑会**丢掉 llm 两行**。")
    A("")
    A("**在补齐 `llm_results.json` 之前，建议手工粘贴下面这行到对比表：**")
    A("")
    A("```markdown")
    A(f"| fusion | {fmt(cv1, cvn)} | {fmt(cv3, cvn)} | {cvn} | "
      f"融合：α·rule+(1−α)·LLM；α 按 {args.folds} 折 CV 选取（无偏）；"
      f"全量最优 α={best_a}（有偏，Top-1 {fmt(best_key[0], n)}） |")
    A("```")
    A("")

    OUT_MD.write_text("\n".join(L), encoding="utf-8")


if __name__ == "__main__":
    main()
