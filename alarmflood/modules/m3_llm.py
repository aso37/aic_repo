# -*- coding: utf-8 -*-
"""M3 路线 B：LLM 根因排序（D3）

做什么
--------------------------------------------------------------------
对每个"可评测故障窗口"，把窗口内去重的告警模板序列（首现时间 | 模板 | 次数 |
层级 | 相关实体）喂给 LLM，让它推断 1~5 个根因候选，并给出推理链。

与原方案的两处差异（都有理由，答辩要能讲）
--------------------------------------------------------------------
1. **要求 LLM 同时输出 `entity`（根因实体）**。
   原 prompt（开工方案 5.3）只让它输出"根因告警模板"，但评测的地面真值是
   **根因实体**（groundtruth 的 `service` 列）——两者之间缺一个映射。
   让 LLM 直接给出实体（且约束"只能是列表里出现过的实体"），
   既解决了评测口径，也正好是 M4 工单 `assignee_team` 需要的字段。

2. **把 tier（层级）作为显式线索喂进去**（D2 日志 §5 的建议）。
   novel=新发是根因的判别性信号，semi/core 是背景噪声。
   数值公式靠权重利用这个信息，LLM 也应该看到它。

评测口径
--------------------------------------------------------------------
窗口筛选**逐行复现 `m3_rank.evaluate()`**，保证分母与 freq / rule 完全一致（72），
否则对比表不可比。

缓存与成本
--------------------------------------------------------------------
- 每个窗口的响应缓存在 `results/llm_cache.json`，同窗口不重复调用（键含 prompt 版本与模型名）
- 原始响应存 `results/llm_raw/`，答辩可溯源
- 调用次数与 token 数汇总打印，并写进输出 JSON

用法
--------------------------------------------------------------------
    # 1) 先看 prompt 长什么样、估算 token（不花钱）
    .venv\\Scripts\\python.exe modules\\m3_llm.py --review

    # 2) 先跑 3 个窗口验证链路（D3"单模型跑通即用"）
    .venv\\Scripts\\python.exe modules\\m3_llm.py --limit 3

    # 3) 全量跑 + 出指标
    .venv\\Scripts\\python.exe modules\\m3_llm.py

需要 .env（见仓库根 .env.example）：LLM_API_KEY / LLM_BASE_URL / LLM_MODEL
"""
import argparse
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
CACHE_PATH = ROOT / "results" / "llm_cache.json"
RAW_DIR = ROOT / "results" / "llm_raw"
OUT_PATH = ROOT / "results" / "llm_results.json"

# 改了 prompt 就必须升版本号，否则会命中旧缓存
PROMPT_VERSION = "v2-tier-entity"

SYSTEM_BG = "某银行核心交易系统，含微服务集群、数据库（Mysql）、缓存（Redis）、Web 中间件（Tomcat/Apache）、监控（IG/MG）"

# 单个窗口喂给 LLM 的模板条数上限（控 token）；超出时按 novel→次数 优先保留
MAX_TEMPLATES = 60
# 每个模板最多附几个实体
MAX_ENTITIES_PER_TPL = 6

TIER_MEANING = (
    "novel=新发（本次故障期间首次出现，通常是根因的判别性信号）；"
    "semi=半常驻（间歇性出现）；"
    "core=全天候常驻（背景噪声，几乎每个窗口都有，判别价值低）"
)

# ---------------------------------------------------------------------------
# prompt
# ---------------------------------------------------------------------------

PROMPT_TMPL = """你是一名企业 IT 系统的资深运维专家（SRE）。下面是某故障时间窗内按时间排序的告警模板序列（已去重，附首现时间、出现次数、告警层级、相关实体）：

{template_lines}

系统背景：{system_bg}

层级说明：{tier_meaning}

任务：从这些告警中推断最可能的 1-5 个根因（root cause），并解释推理链。
要求：
1. 只能从上面列出的告警模板与实体中选择，禁止编造未出现的模板或实体；
2. `entity` 必须是上面"相关实体"里出现过的实体名，它是你要指认的根因主体；
3. 优先考虑：层级为 novel 的告警、出现时间最早、位于因果链上游（如设备本体告警先于联动告警）、出现次数多；
4. 若窗口内**所有**告警都是 core 层级（没有 novel），说明证据不足，请只输出 1-2 个候选并把 confidence 压低（<=0.3）；
5. 输出 JSON，不要输出任何其他文字：
{{"candidates": [{{"rank": 1, "alarm": "模板原文", "entity": "根因实体名", "reason": "推理链(<=80字)", "confidence": 0.0-1.0}}]}}
"""


def build_template_lines(wdf: pd.DataFrame):
    """把窗口内的告警聚合成模板行（含层级与实体），并做 token 预算截断"""
    g = wdf.groupby("template")
    rows = []
    for tpl, sub in g:
        tiers = sub["tier"].value_counts()
        tier = tiers.index[0]
        ents = (sub.groupby("entity").size()
                .sort_values(ascending=False).head(MAX_ENTITIES_PER_TPL))
        rows.append({
            "template": tpl,
            "first_ms": int(sub["timestamp"].min()),
            "count": len(sub),
            "tier": tier,
            "entities": list(ents.index),
        })

    # 截断优先级：novel 优先，其次按次数
    prio = {"novel": 0, "semi": 1, "core": 2}
    rows.sort(key=lambda r: (prio.get(r["tier"], 9), -r["count"], r["first_ms"]))
    truncated = max(0, len(rows) - MAX_TEMPLATES)
    rows = rows[:MAX_TEMPLATES]
    # 展示时按首现时间排序，更符合"因果链"的阅读顺序
    rows.sort(key=lambda r: r["first_ms"])

    lines = []
    for r in rows:
        ts = pd.to_datetime(r["first_ms"], unit="ms").strftime("%H:%M:%S")
        lines.append(f"{ts} | {r['template']} | 次数={r['count']} | "
                     f"层级={r['tier']} | 实体={','.join(r['entities'])}")
    return lines, len(rows), truncated


def build_prompt(wdf: pd.DataFrame):
    lines, n_tpl, truncated = build_template_lines(wdf)
    prompt = PROMPT_TMPL.format(
        template_lines="\n".join(lines),
        system_bg=SYSTEM_BG,
        tier_meaning=TIER_MEANING,
    )
    meta = {"n_rows": len(wdf), "n_templates": n_tpl, "truncated": truncated,
            "est_chars": len(prompt)}
    return prompt, meta


# ---------------------------------------------------------------------------
# LLM 调用
# ---------------------------------------------------------------------------

def _extract_json(text: str):
    """容错解析：剥 markdown 围栏 → 直接 loads → 抓第一个 {...}"""
    if not text:
        return None
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    try:
        return json.loads(t)
    except Exception:
        pass
    m = re.search(r"\{.*\}", t, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            return None
    return None


def call_llm(prompt: str, client, model: str, temperature: float,
             json_mode: bool, max_retry: int):
    """返回 (parsed_or_None, usage_dict, raw_text, attempts)"""
    last_raw = ""
    for attempt in range(1, max_retry + 2):
        try:
            kwargs = dict(model=model,
                          messages=[{"role": "user", "content": prompt}],
                          temperature=temperature)
            if json_mode:
                kwargs["response_format"] = {"type": "json_object"}
            resp = client.chat.completions.create(**kwargs)
            raw = resp.choices[0].message.content or ""
            last_raw = raw
            usage = {}
            if getattr(resp, "usage", None):
                usage = {"prompt_tokens": resp.usage.prompt_tokens,
                         "completion_tokens": resp.usage.completion_tokens,
                         "total_tokens": resp.usage.total_tokens}
            parsed = _extract_json(raw)
            if parsed and isinstance(parsed.get("candidates"), list):
                return parsed, usage, raw, attempt
        except Exception as e:  # noqa: BLE001
            last_raw = f"[ERROR] {type(e).__name__}: {e}"
            # 退避后重试
            time.sleep(min(2 ** attempt, 8))
    return None, {}, last_raw, max_retry + 1


# ---------------------------------------------------------------------------
# 评测（窗口筛选逐行复现 m3_rank.evaluate）
# ---------------------------------------------------------------------------

def select_eval_windows(alarms, floods, faults):
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
        out.append({"fault": w, "st": int(st), "ed": int(ed), "wdf": wdf})
    return out


def load_cache():
    if CACHE_PATH.exists():
        try:
            # utf-8-sig：容忍 BOM
            return json.loads(CACHE_PATH.read_text(encoding="utf-8-sig"))
        except Exception as e:
            print(f"[m3-llm][warn] 缓存无法解析，将重新调用 API（{type(e).__name__}: {e}）")
            return {}
    return {}


def save_cache(cache):
    CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=1),
                          encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--review", action="store_true",
                    help="只打印第一个窗口的 prompt 与 token 估算，不调用 API")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 个窗口（0=全量）")
    ap.add_argument("--no-cache", action="store_true", help="忽略缓存强制重调")
    args = ap.parse_args()

    alarms = pd.read_parquet(ALARMS)
    floods = pd.read_parquet(FLOOD)
    faults = pd.read_parquet(FAULTS)
    wins = select_eval_windows(alarms, floods, faults)
    print(f"[m3-llm] 可评测窗口数 = {len(wins)}（应与 freq/rule 的 n_eval 一致）")

    if args.review:
        w = wins[0]
        prompt, meta = build_prompt(w["wdf"])
        print("=" * 74)
        print(f"窗口示例: fault_id={w['fault']['id']} "
              f"root={w['fault']['service']} type={w['fault']['anomaly_type']}")
        print(f"告警 {meta['n_rows']} 条 → 模板 {meta['n_templates']} 个"
              f"（截断 {meta['truncated']} 个）")
        print(f"prompt 字符数 {meta['est_chars']}（粗估 ~{meta['est_chars']//2} tokens）")
        print("=" * 74)
        print(prompt)
        return

    # ---- 环境变量 ----
    try:
        from dotenv import load_dotenv
        for p in (ROOT.parent / ".env", ROOT / ".env"):
            if p.exists():
                load_dotenv(p)
                print(f"[m3-llm] 已加载 {p}")
                break
    except ImportError:
        pass

    api_key = os.environ.get("LLM_API_KEY", "").strip()
    base_url = os.environ.get("LLM_BASE_URL", "").strip() or None
    model = os.environ.get("LLM_MODEL", "").strip()
    if not api_key or not model:
        raise SystemExit(
            "[m3-llm] 缺少 LLM_API_KEY / LLM_MODEL。\n"
            "  复制仓库根的 .env.example 为 .env 并填写（.env 已被 .gitignore 忽略）")

    temperature = float(os.environ.get("LLM_TEMPERATURE", "0.0"))
    max_retry = int(os.environ.get("LLM_MAX_RETRY", "2"))
    timeout = float(os.environ.get("LLM_TIMEOUT", "60"))
    json_mode = os.environ.get("LLM_JSON_MODE", "1") not in ("0", "false", "False")

    from openai import OpenAI
    client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout)
    print(f"[m3-llm] model={model} base_url={base_url} json_mode={json_mode} "
          f"temperature={temperature} prompt_version={PROMPT_VERSION}")

    cache = load_cache()
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    todo = wins[:args.limit] if args.limit else wins
    results = []
    n_api = n_cache = n_fail = 0
    tok_in = tok_out = 0

    for i, w in enumerate(todo, 1):
        f = w["fault"]
        key = f"{PROMPT_VERSION}|{model}|{w['st']}|{w['ed']}|{f['service']}"
        if not args.no_cache and key in cache:
            entry = cache[key]
            n_cache += 1
        else:
            prompt, meta = build_prompt(w["wdf"])
            parsed, usage, raw, attempts = call_llm(
                prompt, client, model, temperature, json_mode, max_retry)
            n_api += 1
            tok_in += usage.get("prompt_tokens", 0)
            tok_out += usage.get("completion_tokens", 0)
            (RAW_DIR / f"win_{i:03d}_{f['id']}.json").write_text(
                json.dumps({"prompt": prompt, "raw_response": raw,
                            "attempts": attempts, "usage": usage},
                           ensure_ascii=False, indent=1), encoding="utf-8")
            entry = {"parsed": parsed, "usage": usage,
                     "attempts": attempts, "meta": meta}
            cache[key] = entry
            save_cache(cache)          # 每个窗口落盘一次，中断不丢
            if parsed is None:
                n_fail += 1

        cands = (entry.get("parsed") or {}).get("candidates", []) or []
        ents = [c.get("entity") for c in cands if isinstance(c, dict)]
        top1 = 1 if (ents and ents[0] == f["service"]) else 0
        top3 = 1 if f["service"] in ents[:3] else 0
        results.append({
            "fault_id": int(f["id"]), "root": f["service"],
            "anomaly_type": f["anomaly_type"],
            "n_rows": entry.get("meta", {}).get("n_rows"),
            "candidates": cands, "entities": ents,
            "top1": top1, "top3": top3,
            "failed": entry.get("parsed") is None,
        })
        print(f"[{i:>3}/{len(todo)}] fault#{f['id']:<5} root={f['service']:<10} "
              f"{f['anomaly_type']:<10} top1={'Y' if top1 else 'n'} "
              f"top3={'Y' if top3 else 'n'} "
              f"{'FAILED' if entry.get('parsed') is None else ''}")

    n = len(results)
    t1 = sum(r["top1"] for r in results)
    t3 = sum(r["top3"] for r in results)
    print("\n" + "=" * 74)
    print(f"[m3-llm] Top-1={t1}/{n} ({t1/max(n,1)*100:.0f}%)  "
          f"Top-3={t3}/{n} ({t3/max(n,1)*100:.0f}%)")
    print(f"[m3-llm] API 调用 {n_api} 次 / 命中缓存 {n_cache} 次 / 解析失败 {n_fail} 次")
    print(f"[m3-llm] tokens: in={tok_in:,} out={tok_out:,} total={tok_in+tok_out:,}")

    if args.limit:
        print("[m3-llm] --limit 模式下不写 llm_results.json（避免用部分结果污染对比表）")
        return

    OUT_PATH.write_text(json.dumps({
        "method": "llm",
        "note": f"LLM {model} @ {PROMPT_VERSION}；prompt 含 tier 线索，要求输出根因实体",
        "model": model, "prompt_version": PROMPT_VERSION,
        "n_eval": n, "top1": t1, "top3": t3,
        "api_calls": n_api, "cache_hits": n_cache, "parse_failures": n_fail,
        "tokens": {"in": tok_in, "out": tok_out},
        "per_window": results,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[m3-llm] 结果已写入 {OUT_PATH}")
    print("[m3-llm] 下一步：重跑 modules/m3_rank.py，对比表的 llm 行会自动出现")


if __name__ == "__main__":
    main()
