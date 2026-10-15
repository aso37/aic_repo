# -*- coding: utf-8 -*-
"""
test_llm.py — LLM API 接入最小验证（D2 交付物）

作用：确认 API key / base_url / 模型名三者都对得上、能跑通一次对话。
密钥从仓库根目录 .env 读取（见 .env.example），严禁硬编码进代码。

用法：
  python scripts/test_llm.py            # 用 .env 里的 LLM_MODEL 测一次
  python scripts/test_llm.py --probe    # 额外探测一批候选模型名，报告哪些可用
"""
import argparse
import os
import time
from pathlib import Path

ROOT = Path(__file__).parents[2]  # 仓库根（aic_repo）

CANDIDATES = ["glm-4-plus", "glm-4-flash", "glm-4-air", "glm-4-long",
              "glm-4-flashx", "glm-4.5", "glm-4.6", "glm-z1-flash"]


def load_dotenv(path: Path):
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def make_client():
    from openai import OpenAI
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise SystemExit("缺少 OPENAI_API_KEY（在仓库根目录 .env 里配置）")
    kwargs = {"api_key": key}
    if os.environ.get("OPENAI_BASE_URL"):
        kwargs["base_url"] = os.environ["OPENAI_BASE_URL"]
    return OpenAI(**kwargs)


def ping(client, model: str):
    t0 = time.time()
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": "只回复两个字：收到"}],
        temperature=0,
    )
    dt = time.time() - t0
    u = resp.usage
    return resp.choices[0].message.content.strip(), dt, u


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true", help="探测一批候选模型名")
    args = ap.parse_args()

    load_dotenv(ROOT / ".env")
    print(f"[test_llm] base_url = {os.environ.get('OPENAI_BASE_URL', '(默认)')}")
    client = make_client()

    if args.probe:
        print("[test_llm] 探测候选模型：")
        for m in CANDIDATES:
            try:
                text, dt, u = ping(client, m)
                print(f"  [OK]   {m:16s} {dt:4.1f}s  reply={text!r}")
            except Exception as e:
                print(f"  [FAIL] {m:16s} {type(e).__name__}: {str(e)[:80]}")
        return

    model = os.environ.get("LLM_MODEL", "glm-4-plus")
    try:
        text, dt, u = ping(client, model)
        print(f"[test_llm] OK  model={model}  {dt:.1f}s  reply={text!r}")
        print(f"[test_llm] usage: prompt={u.prompt_tokens} completion={u.completion_tokens}")
    except Exception as e:
        print(f"[test_llm] FAIL model={model}: {type(e).__name__}: {e}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
