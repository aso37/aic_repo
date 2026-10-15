@echo off
REM AlarmFlood Copilot 看板启动脚本（conda aic 环境）
cd /d G:\aic_repo\alarmflood
G:\anaconda3\envs\aic\python.exe -m streamlit run app.py --server.port 8502
