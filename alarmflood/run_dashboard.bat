@echo off
REM AlarmFlood Copilot 看板启动脚本（跨机器可移植）
REM Python 优先级：环境变量 AIC_PYTHON > 项目 venv(.venv) > PATH 中的 python
cd /d "%~dp0"
set "PY="
if defined AIC_PYTHON set "PY=%AIC_PYTHON%"
if not defined PY if exist "%~dp0..\.venv\Scripts\python.exe" set "PY=%~dp0..\.venv\Scripts\python.exe"
if not defined PY set "PY=python"
echo [run_dashboard] using Python: %PY%
"%PY%" -m streamlit run app.py --server.port 8502
