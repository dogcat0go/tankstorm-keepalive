#!/bin/bash
# Tankstorm 一键更新流程：git pull -> 构建页面 -> 暂停服务 -> 重启服务
# 用法：bash /home/ubuntu/tankstorm-keepalive/update_tankstorm.sh

set -e

REPO_DIR="/home/ubuntu/tankstorm-keepalive"
SERVICES=("tankstorm.service" "tankstorm-web.service")

# 先拉代码、构建页面，最后再重启。游戏连接只在重启那几秒断开。
# 进程活着时会定期打开游戏页，退出前再写一次 cookie，新进程才能不扫码接上。
echo "[1/4] 在 $REPO_DIR 执行 git pull..."
# 处理 root 操作 ubuntu 属主仓库的 dubious ownership 问题
git config --global --add safe.directory "$REPO_DIR" 2>/dev/null || true
cd "$REPO_DIR"
git pull
echo "  git pull 完成"

echo "[2/4] 构建页面..."
# web/dist 不进仓库。用 root 跑时改以 ubuntu 构建，避免 node_modules 变成 root。
if [[ "$(id -u)" -eq 0 ]]; then
    sudo -u ubuntu bash -lc "cd \"$REPO_DIR/web\" && npm ci && npm run build"
else
    if ! command -v npm >/dev/null 2>&1 && [[ -s "$HOME/.nvm/nvm.sh" ]]; then
        # shellcheck disable=SC1090
        . "$HOME/.nvm/nvm.sh"
    fi
    npm ci --prefix web
    npm run build --prefix web
fi
echo "  页面构建完成"

echo "[3/4] 停止 Tankstorm 两个服务..."
for svc in "${SERVICES[@]}"; do
    systemctl stop "$svc"
    echo "  已停止 $svc ($(systemctl is-active "$svc"))"
done

echo "[4/4] 重新启动 Tankstorm 两个服务..."
for svc in "${SERVICES[@]}"; do
    systemctl start "$svc"
    echo "  已启动 $svc ($(systemctl is-active "$svc"))"
done

echo "流程执行完毕。"
