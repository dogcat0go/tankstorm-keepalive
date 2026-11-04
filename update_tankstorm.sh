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
# 旧提交把 city_players.db-wal / -shm 放进了仓库。线上库一直在改这两个文件，
# git pull 要删掉它们时会拒绝覆盖。不能 git checkout 回去，那是会截断主库的旧快照。
# 先停服务，把 wal 收进主库，再把文件挪开，拉取才能继续。
stopped_early=0
if git ls-files --error-unmatch city_players.db-wal city_players.db-shm >/dev/null 2>&1 \
   && ! git diff --quiet HEAD -- city_players.db-wal city_players.db-shm; then
    echo "  仓库里还记着数据库 wal，先停服务并挪开正在用的文件"
    for svc in "${SERVICES[@]}"; do
        systemctl stop "$svc" || true
        echo "  已停止 $svc ($(systemctl is-active "$svc" || true))"
    done
    stopped_early=1
    stamp=$(date +%Y%m%d%H%M%S)
    owner=$(stat -c '%U' "$REPO_DIR")
    if [[ "$(id -u)" -eq 0 && "$owner" != root ]]; then
        run=(sudo -u "$owner")
    else
        run=()
    fi
    checkpoint=$("${run[@]}" python3 - "$REPO_DIR" "$stamp" << 'PY'
import os
import sqlite3
import sys
root, stamp = sys.argv[1], sys.argv[2]
db = os.path.join(root, "city_players.db")
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
for suf in ("-wal", "-shm"):
    src = db + suf
    if os.path.isfile(src):
        os.replace(src, f"{src}.live-{stamp}")
print("OK" if ok else "BUSY")
PY
)
    if [[ "$checkpoint" != "OK" ]]; then
        echo "  wal 没能完全收进主库，拉取后会把挪开的文件放回去"
    fi
fi
# 仓库里曾经的 city_players.db-wal 会把主库截成 391 页。拉代码前先挪走。
python3 - << 'PY'
import os
import sys
sys.path.insert(0, os.getcwd())
try:
    from tankstorm.citydb import sideline_poison_wal
except Exception:
    raise SystemExit(0)
sideline_poison_wal()
PY
if ! git pull; then
    if [[ "$stopped_early" -eq 1 && "${checkpoint:-OK}" != "OK" ]]; then
        "${run[@]}" python3 - "$REPO_DIR" "$stamp" << 'PY'
import os
import sys
root, stamp = sys.argv[1], sys.argv[2]
db = os.path.join(root, "city_players.db")
for suf in ("-wal", "-shm"):
    dest = f"{db}{suf}.live-{stamp}"
    if os.path.isfile(dest):
        os.replace(dest, db + suf)
PY
    fi
    if [[ "$stopped_early" -eq 1 ]]; then
        echo "  拉取失败，先把服务拉起来"
        for svc in "${SERVICES[@]}"; do
            systemctl start "$svc" || true
        done
    fi
    exit 1
fi
if [[ "$stopped_early" -eq 1 && "${checkpoint:-OK}" == "OK" ]]; then
    "${run[@]}" python3 - "$REPO_DIR" "$stamp" << 'PY'
import os
import sys
root, stamp = sys.argv[1], sys.argv[2]
db = os.path.join(root, "city_players.db")
for suf in ("-wal", "-shm"):
    dest = f"{db}{suf}.live-{stamp}"
    if os.path.isfile(dest):
        os.remove(dest)
PY
elif [[ "$stopped_early" -eq 1 ]]; then
    "${run[@]}" python3 - "$REPO_DIR" "$stamp" << 'PY'
import os
import sys
root, stamp = sys.argv[1], sys.argv[2]
db = os.path.join(root, "city_players.db")
for suf in ("-wal", "-shm"):
    dest = f"{db}{suf}.live-{stamp}"
    if os.path.isfile(dest) and not os.path.exists(db + suf):
        os.replace(dest, db + suf)
PY
fi
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
