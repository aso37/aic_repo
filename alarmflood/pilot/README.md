# L2 自建试点监控栈（云服务器执行）

完整操作路径见：`G:\repo\ai+\working_logs\d6\D6-L2L3-无运维同学操作路径.md`

## 最快上手（3 条命令）

```bash
scp -r <本地>/pilot root@<服务器IP>:/root/pilot   # 本地执行，上传
cd /root/pilot && chmod +x run_pilot.sh && ./run_pilot.sh   # 服务器执行，约 8 分钟
scp root@<服务器IP>:/root/pilot/data/{alerts.jsonl,injections.json} <本地>/alarmflood/data/pilot/
```

## 目录

| 文件 | 作用 |
|---|---|
| `docker-compose.yml` | 8 容器：Prometheus / Alertmanager / node-exporter / sink / svc-db / svc-api-1~3 |
| `prometheus/prometheus.yml` | 5s 抓取 4 个服务 + 主机 |
| `prometheus/alert.rules.yml` | 10 条告警规则（5 服务级 + 5 主机级），阈值先验定死 |
| `alertmanager/alertmanager.yml` | 路由到 webhook，repeat_interval=30s（重复报警来源） |
| `sink/sink.py` | webhook 接收端 → `/data/alerts.jsonl` |
| `app/app.py` | 试点微服务（db/api 角色）+ 故障注入端点 |
| `run_pilot.sh` | 一键时间线：基线 → 注入 → 恢复 → 写真值 |

## 手动验证入口

- Prometheus UI：`http://<IP>:9090`（/targets 看抓取状态、/alerts 看规则触发）
- Alertmanager：`http://<IP>:9093`
- 手动注入：`curl "http://127.0.0.1:18001/debug/stress/cpu?threads=4&seconds=180"`
- 实时看告警：`tail -f data/alerts.jsonl`

## 清理

```bash
docker compose down -v
```
