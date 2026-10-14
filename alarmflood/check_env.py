"""环境自检：三个人跑完输出应完全一致。

用法（在 alarmflood/ 目录下）：
    .venv\\Scripts\\python.exe check_env.py

全部使用 ASCII 标记（OK / MISS / PASS / FAIL），避免 Windows GBK 控制台
无法编码 emoji 导致脚本崩溃。若中文显示为乱码，先执行 chcp 65001。
"""
import sys
import importlib

REQUIRED = [
    "pandas",
    "pyarrow",
    "drain3",
    "sklearn",
    "streamlit",
    "matplotlib",
    "dotenv",
    "openai",
]

LABEL = {
    "sklearn": "scikit-learn",
    "dotenv": "python-dotenv",
}


def main() -> int:
    print("=" * 58)
    print("AlarmFlood Copilot - 环境自检")
    print("=" * 58)
    print(f"Python      : {sys.version.split()[0]}")
    print(f"解释器路径  : {sys.executable}")
    venv_ok = sys.prefix != sys.base_prefix
    print(f"虚拟环境    : {'是' if venv_ok else '否 (!! 请使用 alarmflood/.venv 里的解释器)'}")
    print("-" * 58)

    missing = []
    for mod in REQUIRED:
        name = LABEL.get(mod, mod)
        try:
            m = importlib.import_module(mod)
            ver = getattr(m, "__version__", "-")
            print(f"  OK    {name:<16} {ver}")
        except Exception as exc:  # noqa: BLE001
            missing.append(name)
            print(f"  MISS  {name:<16} {type(exc).__name__}: {exc}")

    print("-" * 58)
    if missing:
        print(f"[FAIL] 缺失 {len(missing)} 项: {', '.join(missing)}")
        print("       修复: .venv\\Scripts\\python.exe -m pip install -r requirements.txt")
        return 1

    print(f"[PASS] 全部 {len(REQUIRED)} 项依赖就绪")
    return 0


if __name__ == "__main__":
    sys.exit(main())
