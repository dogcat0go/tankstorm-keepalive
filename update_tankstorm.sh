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
# 仓库里曾经的 city_players.db-wal 提交页数只有 381、382 或 391。
# 拉代码盖回去之后，打开就会把主库截短。用 root 跑会把当天日志建成 root 的。
py_as_owner() {
    if [[ "$(id -u)" -eq 0 ]]; then
        sudo -u ubuntu bash -lc "cd \"$REPO_DIR\" && python3 -"
    else
        python3 -
    fi
}
sideline_wal() {
    py_as_owner << 'PY'
import os
import sys
sys.path.insert(0, os.getcwd())
try:
    from tankstorm.citydb import sideline_poison_wal
except Exception:
    raise SystemExit(0)
sideline_poison_wal()
PY
}
# 这两个文件还被 git 跟踪时，pull 会把旧快照写回正在用的库。先停写。
stopped_early=0
if [[ -n "$(git ls-files -- city_players.db-wal city_players.db-shm)" ]]; then
    echo "  仓库里还记着数据库 wal，先停服务再挪开"
    for svc in "${SERVICES[@]}"; do
        systemctl stop "$svc" || true
        echo "  已停止 $svc ($(systemctl is-active "$svc" || true))"
    done
    stopped_early=1
fi
sideline_wal
if [[ "$stopped_early" -eq 1 ]] && ! git diff --quiet HEAD -- city_players.db-wal city_players.db-shm; then
    checkpoint=$(py_as_owner << 'PY'
import os
import sqlite3
import sys
db = os.path.join(os.getcwd(), "city_players.db")
ok = True
if os.path.isfile(db):
    try:
        conn = sqlite3.connect(db, timeout=30)
        try:
            row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            ok = bool(row) and row[0] == 0
        finally:
            conn.close()
    except sqlite3.Error as exc:
        print(f"checkpoint failed: {exc}", file=sys.stderr)
        ok = False
print("OK" if ok else "BUSY")
PY
)
    if [[ "$checkpoint" == "OK" ]]; then
        stamp=$(date +%Y%m%d%H%M%S)
        for suf in -wal -shm; do
            src="city_players.db$suf"
            if [[ -f "$src" ]]; then
                mv "$src" "$src.live-$stamp"
            fi
        done
    else
        echo "  wal 没能收进主库，不拉取，先把服务拉起来"
        for svc in "${SERVICES[@]}"; do
            systemctl start "$svc" || true
        done
        exit 1
    fi
fi
if ! git pull; then
    if [[ "$stopped_early" -eq 1 ]]; then
        echo "  拉取失败，先把服务拉起来"
        for svc in "${SERVICES[@]}"; do
            systemctl start "$svc" || true
        done
    fi
    exit 1
fi
# pull 可能把旧 wal 又拿回来。服务还没拉起来时再挪一次。
sideline_wal
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
# 今天的日志如果是 root 建的，服务用户打不开，进程在导入时就退出。
if [[ "$(id -u)" -eq 0 && -d "$REPO_DIR/logs" ]]; then
    chown -R --reference="$REPO_DIR" "$REPO_DIR/logs" || true
fi
for svc in "${SERVICES[@]}"; do
    systemctl start "$svc"
    echo "  已启动 $svc ($(systemctl is-active "$svc"))"
done

echo "流程执行完毕。"
