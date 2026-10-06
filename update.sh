#!/bin/bash
# 服务器更新。拉当前分支，重新构建页面，再把订阅页面拉起来。
# web/dist 不进仓库，页面只在这一步生成。服务器需要已安装 Node。
set -euo pipefail
cd "$(dirname "$0")"

PYTHON="${CONDA_PYTHON:-$HOME/miniconda3/envs/tank/bin/python}"
if [[ ! -x "$PYTHON" ]]; then
  PYTHON="$(command -v python3)"
fi

git pull --ff-only

"$PYTHON" -m pip install -r requirements.txt

npm ci --prefix web
npm run build --prefix web

mkdir -p logs
cmd=""
mapfile -t pids < <(pgrep -f '[P]ython.*web\.py' || true)
if ((${#pids[@]})); then
  cmd=$(ps -ww -o args= -p "${pids[0]}" | sed 's/^[[:space:]]*//')
  kill "${pids[@]}" || true
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    alive=0
    for pid in "${pids[@]}"; do
      if kill -0 "$pid" 2>/dev/null; then
        alive=1
      fi
    done
    if [[ "$alive" -eq 0 ]]; then
      break
    fi
    sleep 0.3
  done
  if [[ "$alive" -ne 0 ]]; then
    echo "旧的 web.py 没退出，页面没有重新拉起" >&2
    exit 1
  fi
fi
if [[ -z "$cmd" ]]; then
  cmd="$PYTHON web.py"
fi
# 命令来自原来的进程参数，按空格拆开再拉起。
# shellcheck disable=SC2086
nohup $cmd >> logs/web.log 2>&1 &
echo "订阅页面已重新拉起：$cmd"
