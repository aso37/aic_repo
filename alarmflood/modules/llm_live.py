# -*- coding: utf-8 -*-
"""
llm_live.py — 值班台「现场启用路线 B」：界面填 API Key + 模型名，当场调用大模型做根因推理

与离线路线 B（modules/m3_llm.py）的关系（**这份关系是设计约束，不要打破**）：
  1. **同一份 prompt**：直接复用 m3_llm.USER_TEMPLATE / m4_report.M4_USER，
     因此实时跑出来的结果与 results/llm_rankings.json 里的离线产物**可直接对照**，
     不是另一套"演示专用"的效果。
  2. **密钥只活在内存**：client 由界面传入的 (api_key, base_url, model) 现场构造，
     不读环境变量、不写 .env、不进日志、不随任何产物落盘。
  3. **实时结果单独缓存**：落在 results/llm_cache_live/，缓存键含模型名——
     换模型必然重算，不会拿 A 模型的答案冒充 B 模型。

支持的接口：任何 OpenAI 兼容的 /chat/completions（OpenAI、DeepSeek、通义、GLM、
Moonshot、本地 Ollama 等），靠 base_url 切换。

用法（界面层调用，见 app.py）：
    from llm_live import make_client, rank_window, draft_report
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import pandas as pd

import m3_llm                      # 复用同一份 prompt 与证据构造 —— 保证与离线同口径
import m4_report                   # 复用 M4 的处置单 prompt / 归一化 / 校验 / 降级

ROOT = Path(__file__).parents[1]
LIVE_CACHE = ROOT / "results" / "llm_cache_live"

# 常见厂商预设（base_url, 默认模型）。选「自定义」可手填任意兼容接口。
PRESETS = {
    "自定义": None,
    "OpenAI": ("https://api.openai.com/v1", "gpt-4o-mini"),
    "DeepSeek": ("https://api.deepseek.com/v1", "deepseek-chat"),
    "通义千问（OpenAI 兼容）": ("https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus"),
    "智谱 GLM": ("https://open.bigmodel.cn/api/paas/v4", "glm-4-flash"),
    "Moonshot / Kimi": ("https://api.moonshot.cn/v1", "moonshot-v1-8k"),
    "本地 Ollama（无需联网）": ("http://localhost:11434/v1", "qwen2.5:7b"),
}

# 单次调用超时（秒）。现场演示时网络抖动不应把整个页面卡死。
TIMEOUT = 120
MAX_RETRY = 2


# ---------- 连接 ----------
def check_ready(api_key: str, model: str, base_url: str = "") -> tuple[bool, str]:
    """调用前的自检：缺什么就直说，避免把 401 甩给用户猜。"""
    if not model or not model.strip():
        return False, "请填写模型名（如 gpt-4o-mini / deepseek-chat）"
    if not api_key or not api_key.strip():
        # Ollama 等本地服务不需要 key，用占位符即可；但不能为空（SDK 会报 401）
        if "localhost" in (base_url or "") or "127.0.0.1" in (base_url or ""):
            return True, ""
        return False, "请填写 API Key"
    return True, ""


def make_client(api_key: str, base_url: str = "", model: str = ""):
    """构造 OpenAI 兼容客户端。返回 (client, error)，失败时 client=None。"""
    try:
        from openai import OpenAI
    except ImportError as e:
        return None, f"缺少依赖 openai（pip install openai）：{e}"

    kwargs = {"api_key": (api_key or "").strip() or "ollama", "timeout": TIMEOUT}
    if base_url and base_url.strip():
        kwargs["base_url"] = base_url.strip()
    try:
        return OpenAI(**kwargs), ""
    except Exception as e:                                   # noqa: BLE001
        return None, f"客户端构造失败：{type(e).__name__}: {e}"


def test_connection(client, model: str) -> dict:
    """连通性自检：一个极短的请求，验证 key / base_url / model 三者是否真的能跑通。"""
    t0 = time.time()
    try:
        resp = client.chat.completions.create(
            model=model, temperature=0, max_tokens=8,
            messages=[{"role": "user", "content": "只回复两个字：正常"}])
        content = (resp.choices[0].message.content or "").strip()
        return {"ok": True, "reply": content[:40],
                "elapsed": round(time.time() - t0, 2),
                "usage": {"prompt_tokens": getattr(resp.usage, "prompt_tokens", None),
                          "completion_tokens": getattr(resp.usage, "completion_tokens", None)}}
    except Exception as e:                                   # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}",
                "elapsed": round(time.time() - t0, 2)}


# ---------- 调用（重试 + JSON 模式降级） ----------
def chat_json(client, model: str, system: str, user: str) -> dict:
    """一次 JSON 对话：带指数退避重试；网关不认 response_format 时自动关掉重试。

    返回 {ok, parsed, raw, usage, json_mode, error, elapsed}
    """
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": user}]
    use_json, err, t0 = True, None, time.time()
    for attempt in range(MAX_RETRY + 1):
        kwargs = dict(model=model, messages=messages, temperature=0, timeout=TIMEOUT)
        if use_json:
            kwargs["response_format"] = {"type": "json_object"}
        try:
            resp = client.chat.completions.create(**kwargs)
            raw = resp.choices[0].message.content or ""
            try:
                parsed = m3_llm.extract_json(raw)
            except Exception as pe:                          # noqa: BLE001
                return {"ok": False, "raw": raw, "parsed": {"candidates": []},
                        "usage": _usage(resp), "json_mode": use_json,
                        "error": f"返回内容不是合法 JSON（{type(pe).__name__}）",
                        "elapsed": round(time.time() - t0, 2)}
            return {"ok": True, "parsed": parsed, "raw": raw,
                    "usage": _usage(resp), "json_mode": use_json,
                    "error": "", "elapsed": round(time.time() - t0, 2)}
        except Exception as e:                               # noqa: BLE001
            err = e
            msg = str(e)
            # 网关不认识 response_format → 关掉它继续试，而不是直接判死
            if use_json and ("response_format" in msg or "json_object" in msg
                             or "unsupported" in msg.lower()):
                use_json = False
                continue
            if attempt < MAX_RETRY:
                time.sleep(min(2 * 2 ** attempt, 8))
    return {"ok": False, "parsed": {"candidates": []}, "raw": "",
            "usage": {"prompt_tokens": None, "completion_tokens": None},
            "json_mode": use_json, "error": f"{type(err).__name__}: {err}",
            "elapsed": round(time.time() - t0, 2)}


def _usage(resp) -> dict:
    u = getattr(resp, "usage", None)
    return {"prompt_tokens": getattr(u, "prompt_tokens", None),
            "completion_tokens": getattr(u, "completion_tokens", None)}


def _cache_path(prefix: str, text: str) -> Path:
    key = hashlib.sha1((prefix + "\n" + text).encode("utf-8")).hexdigest()
    return LIVE_CACHE / f"{key}.json"


def _read_cache(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:                                        # noqa: BLE001
        return None


# ---------- 路线 B：根因排序 ----------
def rank_window(wdf: pd.DataFrame, client, model: str, use_cache: bool = True) -> dict:
    """对某个窗口的告警证据跑一次 LLM 根因推理（与离线路线 B 同 prompt）。

    返回 {ok, candidates, raw, usage, cached, fallback, n_items, prompt_chars,
          json_mode, error, elapsed, model}
    """
    items = m3_llm.build_evidence(wdf, "llm")
    if not items:
        return {"ok": False, "error": "窗口内没有可用的告警证据",
                "candidates": [], "n_items": 0, "model": model}
    user = m3_llm.build_user_message(items)
    cp = _cache_path(f"rank|{model}", user)

    if use_cache and cp.exists():
        hit = _read_cache(cp)
        if hit:
            return {**hit, "cached": True}

    res = chat_json(client, model, m3_llm.SYSTEM, user)
    valid = {it["entity"] for it in items}

    if not res["ok"]:
        out = {"ok": False, "error": res["error"], "candidates": [],
               "n_items": len(items), "prompt_chars": len(user),
               "usage": res["usage"], "raw": res["raw"],
               "json_mode": res["json_mode"], "elapsed": res["elapsed"],
               "fallback": False, "model": model}
        return out

    cands, fb = _clean_candidates(res["parsed"], valid, items)
    out = {"ok": True, "candidates": cands, "raw": res["raw"],
           "usage": res["usage"], "json_mode": res["json_mode"],
           "elapsed": res["elapsed"], "error": "", "fallback": fb,
           "n_items": len(items), "prompt_chars": len(user), "model": model}
    LIVE_CACHE.mkdir(parents=True, exist_ok=True)
    cp.write_text(json.dumps({k: v for k, v in out.items() if k != "cached"},
                             ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def _clean_candidates(parsed: dict, valid: set, items: list) -> tuple[list, bool]:
    """过滤幻觉实体，并按 rank/confidence 排序。全部是幻觉 → 如实标记回退。"""
    rows, seen = [], set()
    for c in parsed.get("candidates", []) or []:
        e = str(c.get("entity", "")).strip()
        if e not in valid or e in seen:
            continue
        seen.add(e)
        try:
            conf = float(c.get("confidence"))
        except (TypeError, ValueError):
            conf = 0.0
        rows.append({"entity": e, "confidence": max(0.0, min(1.0, conf)),
                     "alarm": str(c.get("alarm") or ""),
                     "reason": str(c.get("reason") or "")})
    rows.sort(key=lambda r: -r["confidence"])
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    if rows:
        return rows[:5], False
    # 候选全部是幻觉 → 用规则桩兜底，但必须打上 fallback 标记（不冒充 LLM 成绩）
    order = m3_llm.stub_predict(items)
    return [{"rank": i, "entity": e, "confidence": 0.0, "alarm": "",
             "reason": "LLM 候选全部为窗口外的实体（疑似幻觉），已回退到规则排序"}
            for i, e in enumerate(order[:5], 1)], True


# ---------- 路线 B：处置单草稿 ----------
def draft_report(wdf: pd.DataFrame, cands: list, client, model: str,
                 win: dict, teams: dict, alarms: pd.DataFrame | None = None,
                 use_cache: bool = True) -> dict:
    """用同一份 M4 prompt 现场生成处置单草稿（含幻觉校验与降级）。

    win 需含 window_id / st_ms / ed_ms / n_all_alarms / minutes。
    返回 {ok, report, downgraded, retries, usage, error, elapsed, cached}
    """
    import types
    w = types.SimpleNamespace(**win)
    items = m3_llm.build_evidence(wdf, "llm")
    valid = {it["entity"] for it in items}
    user = (m4_report.M4_USER
            .replace("__SUMMARY__", m3_llm.window_summary(items))
            .replace("__EVIDENCE__", m3_llm.evidence_to_text(items))
            .replace("__CANDIDATES__", m4_report.candidates_to_text(cands)))
    cp = _cache_path(f"m4|{model}", user)

    cached_hit = None
    if use_cache and cp.exists():
        cached_hit = _read_cache(cp)
    if cached_hit:
        parsed = cached_hit["parsed"]
        usage, elapsed = cached_hit.get("usage", {}), cached_hit.get("elapsed", 0)
    else:
        res = chat_json(client, model, m4_report.SYSTEM, user)
        if not res["ok"]:
            return {"ok": False, "error": res["error"], "report": None,
                    "usage": res["usage"], "elapsed": res["elapsed"]}
        parsed, usage, elapsed = res["parsed"], res["usage"], res["elapsed"]
        LIVE_CACHE.mkdir(parents=True, exist_ok=True)
        cp.write_text(json.dumps({"parsed": parsed, "usage": usage, "elapsed": elapsed},
                                 ensure_ascii=False, indent=2), encoding="utf-8")

    # 幻觉校验：root_causes 的实体必须来自窗口证据；不通过就用规则候选降级
    bad = m4_report.validate_entities(parsed, valid)
    downgraded = bool(bad) or not parsed.get("root_causes")
    if downgraded:
        core = m4_report.degrade_core(w, wdf, cands)
    else:
        core = m4_report.normalize_core(parsed)

    report = {
        "window_id": int(win["window_id"]),
        "window": {
            "st_time": pd.to_datetime(int(win["st_ms"]), unit="ms").strftime("%Y-%m-%d %H:%M:%S"),
            "ed_time": pd.to_datetime(int(win["ed_ms"]), unit="ms").strftime("%Y-%m-%d %H:%M:%S"),
            "n_all_alarms": int(win["n_all_alarms"]), "minutes": int(win["minutes"]),
        },
        "generated_at": pd.Timestamp.now().isoformat(timespec="seconds"),
        "summary": core["summary"],
        "root_causes": core["root_causes"],
        "actions": core["actions"],
        "suppressed": core["suppressed"],
        "ticket": m4_report.make_ticket(w, wdf, core, teams,
                                        alarms if alarms is not None else wdf),
        "disclaimer": m4_report.FIXED_DISCLAIMER,
    }
    return {"ok": True, "report": report, "downgraded": downgraded,
            "violations": bad, "usage": usage, "elapsed": elapsed,
            "cached": cached_hit is not None}
