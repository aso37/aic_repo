# L2 自建试点监控栈（云服务器执行）

完整操作路径见项目内计划文档《D6-L2L3-无运维同学操作路径》（属团队内部文档，不随仓库分发）。

## 关于域名：不需要

这套栈**不依赖域名、不需要备案**：所有端口只监听 `127.0.0.1`，外部完全访问不到，
因此既不涉及 ICP 备案，也不用开安全组。想从本地电脑看 Prometheus 界面，用 SSH 隧道：

```bash
ssh -L 9090:127.0.0.1:9090 -L 9093:127.0.0.1:9093 root@43.142.131.159
# 然后本地浏览器打开 http://127.0.0.1:9090
```

## 上服务器第一件事：跑自检

```bash
bash preflight.sh      # 检查 docker / 资源 / 端口 / 外网 / 镜像拉取，5 分钟出结论
```

## 最快上手（3 条命令）

> 复制前先替换两处：`<本地仓库>` → 你自己机器上仓库根目录；`<服务器IP>` → 你的云服务器地址。

```bash
scp -r <本地仓库>/alarmflood/pilot root@<服务器IP>:/root/pilot                          # 本地执行，上传
cd /root/pilot && bash preflight.sh && chmod +x run_pilot.sh && ./run_pilot.sh           # 服务器执行，约 8 分钟
scp root@<服务器IP>:/root/pilot/data/{alerts.jsonl,injections.json} <本地仓库>/alarmflood/data/pilot/
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

- Prometheus UI：`ssh -L 9090:127.0.0.1:9090 root@<IP>` 后本地开 `http://127.0.0.1:9090`
  （/targets 看抓取状态、/alerts 看规则触发）；不想开隧道就在服务器上
  `curl -s localhost:9090/api/v1/alerts | head -c 800`
- Alertmanager：同样走隧道 `http://127.0.0.1:9093`
- 手动注入（服务器内执行）：`curl "http://127.0.0.1:18001/debug/stress/cpu?threads=4&seconds=180"`
- 实时看告警：`tail -f data/alerts.jsonl`

## 清理

```bash
docker compose down -v
```
