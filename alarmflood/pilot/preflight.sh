#!/usr/bin/env bash
# 上服务器第一件事：跑它，5 分钟内判断这条路通不通（不依赖域名/备案）
# 用法： bash preflight.sh
echo "===== 1. Docker 环境 ====="
docker --version 2>&1 | head -1
docker compose version 2>&1 | head -1
docker info >/dev/null 2>&1 && echo "docker daemon: OK" || echo "docker daemon: 未运行 → systemctl start docker"

echo "===== 2. 资源 ====="
echo "CPU 核数: $(nproc)"
free -g | head -2
df -h / | tail -1

echo "===== 3. 端口占用（都要空闲） ====="
for p in 9090 9093 9099 9100 18001 18011 18012 18013; do
  if ss -lntp 2>/dev/null | grep -q ":$p "; then echo "  $p 已被占用（改用其它端口或停掉占用进程）"; else echo "  $p 空闲"; fi
done

echo "===== 4. 外网连通性（国内服务器重点关注） ====="
probe() { curl -s -o /dev/null -w "  $1 -> HTTP %{http_code}  %{time_total}s\n" --max-time 8 "$2" 2>&1 || echo "  $1 -> 不可达"; }
probe "pypi.org        " https://pypi.org/simple/
probe "清华 pypi 镜像  " https://pypi.tuna.tsinghua.edu.cn/simple/
probe "docker hub      " https://registry-1.docker.io/v2/
probe "daocloud 镜像   " https://docker.m.daocloud.io/v2/

echo "===== 5. 镜像拉取实测（最关键） ====="
if timeout 150 docker pull hello-world >/dev/null 2>&1; then
  echo "  docker pull: OK（可以正常拉镜像）"
else
  echo "  docker pull: 失败 → 配镜像加速："
  echo "    sudo mkdir -p /etc/docker"
  echo "    echo '{\"registry-mirrors\":[\"https://docker.m.daocloud.io\",\"https://hub-mirror.c.163.com\"]}' | sudo tee /etc/docker/daemon.json"
  echo "    sudo systemctl restart docker"
fi

echo "===== 6. 结论 ====="
echo "  以上全部 OK → 直接 ./run_pilot.sh"
echo "  仅 docker pull 失败 → 配镜像加速后重试"
echo "  外网全不通 → 告诉我，改用「离线镜像包」方案（本地打 tar 传上去 docker load）"
