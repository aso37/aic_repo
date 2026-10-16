# -*- coding: utf-8 -*-
"""
M3 根因排序 · LLM 路线 B（D3）+ 截断喂基线

与 modules/m3_rank.py（A 的规则路线）对齐：
  - 同一个评测协议：仅评「可检测故障」——存在重叠洪水窗口、且根因实体出现在窗口证据内的 train 故障
  - 同样的输入数据：results/alarms_tagged.parquet / flood_windows.parquet / fault_windows.parquet
  - 评测到「实体」级：Top-K 命中的是根因实体（fault_windows 的 service 列）

相对开工方案 5.3 prompt 的两处演进（依据 D2 日志「给 D3 的输入」）：
  1. 把 tier（新发/半常驻/常驻）作为显式线索喂给 LLM，并给出层级语义
  2. 输出 JSON 增加 entity 字段，便于实体级评测（entity 仍必须来自输入集合，防幻觉）

健壮性补丁（fix/llm-robustness，2026-10-16）：
  1. 调用失败**自动重试 + 指数退避**（原先单次失败即放弃，一次网络抖动就白花 token）
  2. mode=llm 增加**证据条数上限**（按 tier 强度优先截断），避免 prompt 无界增长
  3. 可选**强制 JSON 输出**（response_format=json_object），网关不支持时自动降级重试
  4. **回退披露**：LLM 候选全部被过滤（疑似幻觉）时原会静默回退到规则桩预测，
     等于把规则路线的成绩算进 LLM 路线。现改为累计 `fallback_n` 并显式披露。
  可用环境变量：LLM_MAX_EVIDENCE / LLM_MAX_RETRY / LLM_RETRY_SLEEP / LLM_JSON_MODE

用法：
  python modules/m3_llm.py --dry-run                      # 不调 API，用桩预测验证整条流水线
  python modules/m3_llm.py                                # 真实调用（需要 OPENAI_API_KEY）
  python modules/m3_llm.py --mode llm --max-windows 8     # 只跑 llm 路线、最多 8 个窗口（调试）
"""
import argparse
import hashlib
import json
import os
import re
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).parents[1]
ALARMS = ROOT / "results" / "alarms_tagged.parquet"
FLOOD = ROOT / "results" / "flood_windows.parquet"
FAULTS = ROOT / "results" / "fault_windows.parquet"
CACHE_DIR = ROOT / "results" / "llm_cache"
OUT_EXP = ROOT / "results" / "llm_experiments.md"
OUT_COST = ROOT / "results" / "llm_cost.jsonl"

# tier 强度：novel 最强、core 最弱（与 m2_window.py / m3_rank.py 的分层语义一致）
TIER_RANK = {"core": 0, "semi": 1, "novel": 2}
TIER_LABEL = {"core": "常驻", "semi": "半常驻", "novel": "新发"}

# 截断喂基线：只喂最早出现的 N 条证据（D3 指南第5步，具体截断口径见文件末尾 TODO）
TRUNCATE_N = int(os.environ.get("TRUNCATE_N", "5"))

# ── 健壮性参数（fix/llm-robustness 引入）────────────────────────────────────
# 1) 证据条数上限：mode=llm 原先不设上限，窗口大时 prompt 无界增长、token 失控。
#    超出时按「tier 强度优先 → 首现更早」保留（与 D2 的判别逻辑一致）。
MAX_EVIDENCE = int(os.environ.get("LLM_MAX_EVIDENCE", "60"))
# 2) 失败重试 + 指数退避：原实现单次调用失败即放弃，一次网络抖动就白花 token。
MAX_RETRY = int(os.environ.get("LLM_MAX_RETRY", "3"))
RETRY_BASE_SLEEP = float(os.environ.get("LLM_RETRY_SLEEP", "2"))
# 3) 强制 JSON 输出；网关不支持 response_format 时自动关掉并重试。
JSON_MODE = os.environ.get("LLM_JSON_MODE", "1") not in ("0", "false", "False")

SYSTEM = "你是一名企业 IT 系统的资深运维专家（SRE），擅长告警根因分析。"

USER_TEMPLATE = """某故障时间窗内的告警证据如下（按首现时间排序，已按“实体+模板”去重）。

【窗口概况】__SUMMARY__

【证据列表】
__EVIDENCE__

层级含义：新发(novel)=故障注入产生的强根因信号；半常驻(semi)=周期性背景告警，弱信号；常驻(core)=全天候背景噪声，几乎总是与本次故障无关。
运维先验：真实运维中，根因通常是“新发层级、且为该实体独有或最早出现”的告警；常驻告警（如全体实体的内存/磁盘周期性告警）几乎不可能是根因。
系统背景：某银行核心交易系统，含微服务集群、数据库、中间件、网络设备。

任务：从上述证据中推断最可能的 1-5 个根因实体，并解释推理链。
要求：
1. 只能从上面列出的实体与告警中选择，禁止编造未出现的实体或告警；
2. 排序依据按重要性依次为：① 该实体是否有新发(novel)告警；② 首现时间是否最早；③ 是否位于因果链上游；④ 出现次数只作次要参考，不要因为常驻告警次数高就选它；
3. 若窗口内没有任何新发告警，说明证据不足，请给出较低 confidence（不超过 0.4），不要强猜；
4. 严格只输出一个 JSON 对象，不要输出任何多余文字：
{"candidates": [{"rank": 1, "entity": "实体名", "alarm": "模板原文", "reason": "推理链(<=80字)", "confidence": 0.0}]}
"""


def build_evidence(wdf: pd.DataFrame, mode: str) -> list:
    """窗口切片 → 去重证据列表（按首现时间排序，每组取最强 tier）。"""
    g = wdf.groupby(["entity", "template"]).agg(
        first_seen_ms=("timestamp", "min"),
        count=("timestamp", "size"),
        tier=("tier", lambda s: max(s, key=TIER_RANK.get)),
    ).reset_index()
    g = g.sort_values("first_seen_ms").reset_index(drop=True)
    items = g.to_dict("records")
    if mode == "truncated":
        # 截断喂基线：只保留最早 N 条
        items = items[:TRUNCATE_N]
    elif len(items) > MAX_EVIDENCE:
        # mode=llm：按 tier 强度优先、其次首现更早，取前 MAX_EVIDENCE 条，
        # 再按时间排回，保持因果链的阅读顺序
        keep = sorted(items, key=lambda it: (-TIER_RANK.get(it["tier"], -1),
                                             it["first_seen_ms"]))[:MAX_EVIDENCE]
        items = sorted(keep, key=lambda it: it["first_seen_ms"])
    return items


def evidence_to_text(items: list) -> str:
    lines = []
    for i, it in enumerate(items, 1):
        hhmmss = pd.to_datetime(it["first_seen_ms"], unit="ms").strftime("%H:%M:%S")
        lines.append(
            f"{i}. {hhmmss} | {it['entity']} | {it['template']} | "
            f"次数={it['count']} | 层级={TIER_LABEL[it['tier']]}"
        )
    return "\n".join(lines)


def window_summary(items: list) -> str:
    """把“谁有新发告警”这一判别性信号直接写进概况，帮 LLM 抓住重点。"""
    k = sum(1 for it in items if it["tier"] == "novel")
    m = sum(1 for it in items if it["tier"] == "semi")
    c = sum(1 for it in items if it["tier"] == "core")
    novel_ents = sorted({it["entity"] for it in items if it["tier"] == "novel"})
    ents = "、".join(novel_ents) if novel_ents else "无"
    return (f"共 {len(items)} 条证据：新发 {k} / 半常驻 {m} / 常驻 {c}；"
            f"有新发告警的实体：{ents}")


def build_user_message(items: list) -> str:
    return (USER_TEMPLATE
            .replace("__SUMMARY__", window_summary(items))
            .replace("__EVIDENCE__", evidence_to_text(items)))


def extract_json(text: str) -> dict:
    """从 LLM 输出里稳健地抠出 JSON（容忍代码围栏与前后杂文）。"""
    text = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if m:
        text = m.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start:end + 1]
    return json.loads(text)


def stub_predict(items: list) -> list:
    """干跑桩：不调 LLM，按「新发数 > 首现更早 > 次数」给实体排序，仅用于验证流水线。"""
    if not items:
        return []
    df = pd.DataFrame(items)
    g = df.groupby("entity").agg(
        novel_n=("tier", lambda s: int((s == "novel").sum())),
        first=("first_seen_ms", "min"),
        cnt=("count", "sum"),
    ).sort_values(["novel_n", "first", "cnt"], ascending=[False, True, False])
    return list(g.index)


def candidates_to_entities(data: dict, items: list):
    """把 LLM 候选映射为实体顺序；过滤幻觉实体。

    返回 (order, used_fallback)。**所有候选都是幻觉时会降级到规则桩预测**，
    此时 used_fallback=True —— 调用方必须累计并在结果中披露这一数量。

    ⚠️ 为什么必须披露：回退用的是 stub_predict（即规则路线），若静默计入，
    等于**把规则路线的成绩算进 LLM 路线**，会虚高 LLM 表现且无从察觉。
    """
    valid = {it["entity"] for it in items}
    order = []
    for c in data.get("candidates", []):
        e = c.get("entity")
        if e in valid and e not in order:
            order.append(e)
    if order:
        return order, False
    return stub_predict(items), True


def load_dotenv(path: Path):
    """极简 .env 读取（避免额外依赖 python-dotenv）：KEY=VALUE，已存在的环境变量不覆盖。"""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        os.environ.setdefault(key.strip(), val.strip())


def make_client():
    from openai import OpenAI  # 惰性导入：--dry-run 不需要 openai

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise SystemExit("缺少 OPENAI_API_KEY（参考仓库根目录 .env.example 设置环境变量）")
    kwargs = {"api_key": api_key}
    if os.environ.get("OPENAI_BASE_URL"):
        kwargs["base_url"] = os.environ["OPENAI_BASE_URL"]
    return OpenAI(**kwargs), os.environ.get("LLM_MODEL", "gpt-4o-mini")


def write_examples(examples: list, path: Path):
    """把推理链样例落盘（答辩素材，D3 验收物）。"""
    if not examples:
        return
    lines = ["# LLM 根因推理链样例（D3）", ""]
    for ex in examples:
        lines.append(f"## 故障 #{ex['fault_id']} · 根因 {ex['root']}（{ex['anomaly_type']}）")
        for c in ex["parsed"].get("candidates", [])[:3]:
            lines.append(f"- rank {c.get('rank')} · **{c.get('entity')}** · confidence {c.get('confidence')}")
            lines.append(f"  - 告警：{c.get('alarm')}")
            lines.append(f"  - 推理链：{c.get('reason')}")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def _call_once(client, model: str, messages: list, use_json_mode: bool):
    kwargs = dict(model=model, messages=messages, temperature=0)
    if use_json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    return client.chat.completions.create(**kwargs)


def llm_predict(items: list, client, model: str, mode: str):
    """调用 LLM（带本地缓存 + 重试）。

    返回 (实体顺序, 原始 payload, 是否命中缓存, 是否用了回退预测)。
    """
    user_msg = build_user_message(items)
    key = hashlib.sha1((mode + "\n" + user_msg).encode("utf-8")).hexdigest()
    cache_path = CACHE_DIR / f"{key}.json"
    if cache_path.exists():
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        order, fb = candidates_to_entities(payload.get("parsed") or {}, items)
        return order, payload, True, fb

    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": user_msg},
    ]

    # 重试 + 指数退避（原实现单次失败即放弃，一次网络抖动就白花 token）
    resp, err, use_json = None, None, JSON_MODE
    for attempt in range(1, MAX_RETRY + 2):
        try:
            resp = _call_once(client, model, messages, use_json)
            break
        except Exception as e:                       # noqa: BLE001
            err = e
            # 网关不认识 response_format → 关掉它继续重试，而不是直接失败
            if use_json and "response_format" in str(e):
                use_json = False
                continue
            if attempt <= MAX_RETRY:
                time.sleep(min(RETRY_BASE_SLEEP * 2 ** (attempt - 1), 16))

    if resp is None:
        content = f"[ERROR] 调用失败（已重试 {MAX_RETRY} 次）：{type(err).__name__}: {err}"
        usage = {"prompt_tokens": None, "completion_tokens": None}
        parsed = {"candidates": []}
    else:
        content = resp.choices[0].message.content or ""
        usage = {
            "prompt_tokens": getattr(resp.usage, "prompt_tokens", None),
            "completion_tokens": getattr(resp.usage, "completion_tokens", None),
        }
        try:
            parsed = extract_json(content)
        except Exception as e:                       # noqa: BLE001
            # 解析失败不再中断整轮评测，而是记为空候选（会被计入回退并披露）
            parsed = {"candidates": [],
                      "_parse_error": f"{type(e).__name__}: {e}"}

    payload = {"mode": mode, "evidence": items, "raw": content,
               "parsed": parsed, "usage": usage, "json_mode": use_json}
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                          encoding="utf-8")
    order, fb = candidates_to_entities(parsed, items)
    return order, payload, False, fb


def evaluate(mode: str, dry_run: bool, max_windows: int | None,
             client=None, model: str | None = None):
    """按 m3_rank.py 的协议评测一条路线，返回指标与样例。"""
    alarms = pd.read_parquet(ALARMS)
    floods = pd.read_parquet(FLOOD)
    faults = pd.read_parquet(FAULTS)
    train = faults[faults.data_type == "train"]

    top1 = top3 = n_eval = 0
    fallback_n = 0          # 因候选全被过滤（疑似幻觉）而回退到规则桩预测的窗口数
    misses, examples, usage_total = [], [], {"prompt_tokens": 0, "completion_tokens": 0}
    per_type = {}
    for _, w in train.iterrows():
        if max_windows is not None and n_eval >= max_windows:
            break
        fl = floods[(floods.st_ms <= w.ed_ms + 600_000) &
                    (floods.ed_ms >= w.st_ms - 600_000)]
        if len(fl) == 0:
            continue
        st, ed = fl.st_ms.min(), fl.ed_ms.max()
        wdf = alarms[(alarms.timestamp >= st) & (alarms.timestamp <= ed)]
        if wdf.empty or w.service not in wdf.entity.values:
            continue  # 无窗口证据 → 不计入评测（可检测故障协议）

        items = build_evidence(wdf, mode)
        if dry_run:
            ranked, payload, cached, used_fallback = stub_predict(items), None, False, False
        else:
            ranked, payload, cached, used_fallback = llm_predict(items, client, model, mode)
            if used_fallback:
                fallback_n += 1
            if not cached and payload:
                usage_total["prompt_tokens"] += payload["usage"]["prompt_tokens"] or 0
                usage_total["completion_tokens"] += payload["usage"]["completion_tokens"] or 0

        n_eval += 1
        hit = w.service in ranked
        h1, h3 = w.service == ranked[0], w.service in ranked[:3]
        top1 += h1
        top3 += h3
        t = str(w.anomaly_type).split(";")[0].strip()
        d = per_type.setdefault(t, [0, 0, 0])
        d[0] += h1
        d[1] += h3
        d[2] += 1
        if hit and len(examples) < 3 and payload is not None:
            examples.append({"fault_id": int(w.id), "root": w.service,
                             "anomaly_type": w.anomaly_type, "parsed": payload["parsed"]})
        if not hit and len(misses) < 5:
            misses.append((int(w.id), w.service, w.anomaly_type, ranked[:3]))

    return {"mode": mode, "n_eval": n_eval, "top1": top1, "top3": top3,
            "miss": misses, "examples": examples, "usage": usage_total,
            "per_type": per_type, "fallback_n": fallback_n}


def fmt_rate(num: int, den: int) -> str:
    return f"{num}/{den} ({num / max(den, 1) * 100:.0f}%)"


def collect_rankings(mode: str, client, model, dry_run: bool) -> list:
    """逐窗口收集 LLM 根因排序（优先读缓存；缺缓存才调用 API），供导出 JSON。"""
    alarms = pd.read_parquet(ALARMS)
    floods = pd.read_parquet(FLOOD)
    faults = pd.read_parquet(FAULTS)
    train = faults[faults.data_type == "train"]

    out = []
    for _, w in train.iterrows():
        fl = floods[(floods.st_ms <= w.ed_ms + 600_000) &
                    (floods.ed_ms >= w.st_ms - 600_000)]
        if len(fl) == 0:
            continue
        st, ed = fl.st_ms.min(), fl.ed_ms.max()
        wdf = alarms[(alarms.timestamp >= st) & (alarms.timestamp <= ed)]
        if wdf.empty or w.service not in wdf.entity.values:
            continue

        items = build_evidence(wdf, mode)
        if dry_run:
            ranked = stub_predict(items)
            candidates = [{"rank": i + 1, "entity": e, "alarm": None,
                           "reason": "(dry-run stub)", "confidence": None}
                          for i, e in enumerate(ranked[:5])]
        else:
            key = hashlib.sha1((mode + "\n" + build_user_message(items)).encode("utf-8")).hexdigest()
            cache_path = CACHE_DIR / f"{key}.json"
            if not cache_path.exists():
                llm_predict(items, client, model, mode)
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            candidates = payload["parsed"].get("candidates", [])

        out.append({
            "fault_id": int(w.id),
            "anomaly_type": str(w.anomaly_type).replace("\n", " ").strip(),
            "window": {
                "st_ms": int(st), "ed_ms": int(ed),
                "st_time": pd.to_datetime(int(st), unit="ms").strftime("%Y-%m-%d %H:%M:%S"),
                "ed_time": pd.to_datetime(int(ed), unit="ms").strftime("%Y-%m-%d %H:%M:%S"),
            },
            "root_entity_truth": w.service,          # 数据集标注，仅供核对
            "candidates": candidates,
        })
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="不调 LLM，用桩预测验证流水线")
    ap.add_argument("--mode", choices=["llm", "truncated", "all"], default="llm",
                    help="all = 依次跑 llm 与 truncated 两条路线")
    ap.add_argument("--max-windows", type=int, default=None, help="最多评测窗口数（调试用）")
    ap.add_argument("--export-json", nargs="?", default=None,
                    const=str(ROOT / "results" / "llm_rankings.json"),
                    help="导出根因排序 JSON（默认 results/llm_rankings.json）")
    args = ap.parse_args()

    load_dotenv(ROOT.parent / ".env")
    client = model = None
    if not args.dry_run:
        client, model = make_client()

    if args.export_json is not None:
        exp_mode = "llm" if args.mode == "all" else args.mode
        windows = collect_rankings(exp_mode, client, model, args.dry_run)
        doc = {
            "generated_at": pd.Timestamp.now().isoformat(timespec="seconds"),
            "model": model or "stub",
            "mode": exp_mode,
            "note": ("根因排序由 LLM 基于 results/alarms_tagged.parquet 的窗口切片推理得出；"
                     "root_entity_truth 为数据集标注，仅供核对，不属于模型输出。"),
            "n_windows": len(windows),
            "windows": windows,
        }
        path = Path(args.export_json)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[m3-llm] exported {len(windows)} windows -> {path}")
        return

    modes = ["llm", "truncated"] if args.mode == "all" else [args.mode]
    results = [evaluate(m, args.dry_run, args.max_windows, client, model) for m in modes]

    for r in results:
        print(f"[m3-llm] {r['mode']:9s}: "
              f"Top-1={fmt_rate(r['top1'], r['n_eval'])}  "
              f"Top-3={fmt_rate(r['top3'], r['n_eval'])}  (n_eval={r['n_eval']})")
        if not args.dry_run:
            print(f"[m3-llm] token usage: prompt={r['usage']['prompt_tokens']:,} "
                  f"completion={r['usage']['completion_tokens']:,}")
            if r["fallback_n"]:
                print(f"[m3-llm] ⚠️ {r['fallback_n']}/{r['n_eval']} 个窗口的 LLM 候选"
                      f"全部被过滤（疑似幻觉），已回退到规则桩预测 —— "
                      f"这部分成绩不属于 LLM 路线，报告时必须披露")
        if r["miss"]:
            print(f"[m3-llm] {r['mode']} miss samples (fault_id, root, type, top3):")
            for m in r["miss"]:
                print("   ", m)
        for t, (a, b, n) in sorted(r["per_type"].items()):
            print(f"[m3-llm]   {t:9s} Top-1={fmt_rate(a, n)}  Top-3={fmt_rate(b, n)}")

    # 推理链样例：打印 + 落盘
    for r in results:
        for ex in r["examples"]:
            print(f"\n[m3-llm] 样例({r['mode']}) 故障#{ex['fault_id']} "
                  f"根因={ex['root']} ({ex['anomaly_type']}):")
            for c in ex["parsed"].get("candidates", [])[:3]:
                print(f"    rank={c.get('rank')} {c.get('entity')} conf={c.get('confidence')} "
                      f"reason={c.get('reason')}")
    all_examples = [ex for r in results for ex in r["examples"]]
    if all_examples:
        write_examples(all_examples, ROOT / "results" / "llm_examples.md")
        print(f"[m3-llm] written -> {ROOT / 'results' / 'llm_examples.md'}")

    # 落盘：对比表行（不覆盖 A 的 experiments.md，由人工合并到主表）
    #
    # ⚠️ 干跑 / 部分窗口运行时**不写盘**：否则桩预测或部分结果会覆盖已提交的正式
    #    实验结果（实测 `--dry-run --max-windows 5` 会把 llm_experiments.md 从
    #    26 行冲成 11 行）。正式结果只能由完整跑产生。
    if args.dry_run or args.max_windows is not None:
        print("[m3-llm] --dry-run / --max-windows 模式下不写 llm_experiments.md"
              "（避免桩结果或部分结果覆盖正式实验结果）")
        return

    OUT_EXP.parent.mkdir(parents=True, exist_ok=True)
    note = {"llm": "LLM 路线（tier 线索）",
            "truncated": f"截断喂基线（只喂最早 {TRUNCATE_N} 条）"}
    rows = [f"| {r['mode']} | {fmt_rate(r['top1'], r['n_eval'])} | "
            f"{fmt_rate(r['top3'], r['n_eval'])} | {r['n_eval']} | {note[r['mode']]} |"
            for r in results]
    blocks = ["# LLM 路线实验结果（D3）", "",
              "| 方法 | Top-1 | Top-3 | 评测窗口数 | 备注 |",
              "|---|---|---|---|---|"] + rows
    for r in results:
        blocks += ["", f"### {r['mode']} · 分类型", "",
                   "| 故障类型 | Top-1 | Top-3 | 窗口数 |", "|---|---|---|---|"]
        for t, (a, b, n) in sorted(r["per_type"].items()):
            blocks.append(f"| {t} | {fmt_rate(a, n)} | {fmt_rate(b, n)} | {n} |")
        if r["usage"]["prompt_tokens"]:
            blocks += ["", f"> {r['mode']} 本轮新增调用 token：prompt={r['usage']['prompt_tokens']:,}，"
                           f"completion={r['usage']['completion_tokens']:,}"]
        if r["fallback_n"]:
            blocks += ["", f"> ⚠️ **{r['mode']} 有 {r['fallback_n']}/{r['n_eval']} 个窗口因 "
                           f"LLM 候选全部被过滤（疑似幻觉）而回退到规则桩预测**——"
                           f"这部分成绩不属于 LLM 路线，对比时应扣除或单独说明。"]
    OUT_EXP.write_text("\n".join(blocks), encoding="utf-8")
    print(f"[m3-llm] written -> {OUT_EXP}")


if __name__ == "__main__":
    main()
