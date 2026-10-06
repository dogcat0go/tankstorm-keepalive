#!/bin/bash
# Tankstorm 一键更新流程：git pull -> 构建页面 -> 重启服务
# 用法：bash /home/ubuntu/tankstorm-keepalive/update_tankstorm.sh

set -e

REPO_DIR="/home/ubuntu/tankstorm-keepalive"
SERVICES=("tankstorm.service" "tankstorm-web.service")

echo "[1/3] 在 $REPO_DIR 执行 git pull..."
# 处理 root 操作 ubuntu 属主仓库的 dubious ownership 问题
git config --global --add safe.directory "$REPO_DIR" 2>/dev/null || true
cd "$REPO_DIR"
git pull
echo "  git pull 完成"

echo "[2/3] 构建页面..."
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

echo "[3/3] 重新启动 Tankstorm 两个服务..."
for svc in "${SERVICES[@]}"; do
    systemctl restart "$svc"
    echo "  已启动 $svc ($(systemctl is-active "$svc"))"
done

echo "流程执行完毕。"
