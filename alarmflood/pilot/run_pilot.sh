#!/usr/bin/env bash
# L2 自建试点 · 一键跑完全流程（在云服务器上执行）
#
# 时间线（默认约 8 分钟）：
#   0~120s   基线：正常流量，无告警（或极少）
#   120~300s 注入：svc-db CPU 饱和 → 根因告警 + api 衍生告警 + 重复通知
#   300~360s 恢复：告警 resolved
#   360s     收尾：落盘 injections.json（真值）
set -e
cd "$(dirname "$0")"
mkdir -p data

echo "===== [0/5] 构建并启动监控栈 ====="
docker compose up -d --build
sleep 15
docker compose ps --format "table {{.Name}}\t{{.Status}}"

echo "===== [1/5] 基线采集 120s（请勿注入） ====="
: > data/alerts.jsonl
sleep 120

echo "===== [2/5] 注入故障：svc-db CPU 饱和 180s ====="
ST_MS=$(( $(date +%s) * 1000 ))
curl -s "http://127.0.0.1:18001/debug/stress/cpu?threads=4&seconds=180"
echo
sleep 195

ED_MS=$(( $(date +%s) * 1000 + 60000 ))
echo "===== [3/5] 恢复观察 60s ====="
sleep 60

cat > data/injections.json <<EOF
{
  "root_entity": "svc-db",
  "anomaly_type": "CPU",
  "st_ms": ${ST_MS},
  "ed_ms": ${ED_MS},
  "note": "svc-db 进程 CPU 饱和注入（4 线程 × 180s），真值根因 = svc-db"
}
EOF

echo "===== [4/5] 结果摘要 ====="
echo "告警文件: $(pwd)/data/alerts.jsonl  行数: $(wc -l < data/alerts.jsonl)"
echo "真值文件: $(pwd)/data/injections.json"
cat data/injections.json
echo
echo "最近 5 条告警:"
tail -5 data/alerts.jsonl

echo "===== [5/5] 完成 ====="
echo "把两个文件拷回本地（在本地执行，注意改成你的服务器地址）："
echo "  scp root@<服务器IP>:<本目录>/data/alerts.jsonl   G:/aic_repo/alarmflood/data/pilot/"
echo "  scp root@<服务器IP>:<本目录>/data/injections.json G:/aic_repo/alarmflood/data/pilot/"
echo "清理环境： docker compose down -v"
