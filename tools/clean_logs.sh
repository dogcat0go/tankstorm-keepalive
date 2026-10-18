#!/bin/bash
# 清理 logs 里满 3 天的录制和运行日志。
# 不删当天任务进度（daily-state*.json）、已学消息类型（known-opcodes.json）、
# 正在写的 login.log，以及进程还打开着的文件。
#
# 立刻清一次：
#   ./tools/clean_logs.sh
#
# 每 3 天凌晨 4 点：
#   crontab -e
#   0 4 */3 * * /home/ubuntu/tankstorm-keepalive/tools/clean_logs.sh
#
# 保留天数：KEEP_DAYS=3 ./tools/clean_logs.sh

set -u
KEEP_DAYS="${KEEP_DAYS:-3}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOGS="${LOGS:-$ROOT/logs}"

if [ ! -d "$LOGS" ]; then
  echo "没有目录 $LOGS"
  exit 1
fi
if ! [[ "$KEEP_DAYS" =~ ^[0-9]+$ ]] || [ "$KEEP_DAYS" -lt 1 ]; then
  echo "KEEP_DAYS 要是大于 0 的整数"
  exit 1
fi

# find 的 +N 要满 N+1 个 24 小时。保留 3 天用 +2。
AGE="+$((KEEP_DAYS - 1))"

declare -A OPEN=()
while IFS= read -r link; do
  [ -n "$link" ] || continue
  OPEN["$link"]=1
done < <(find /proc -mindepth 3 -maxdepth 3 -path '/proc/[0-9]*/fd/*' -printf '%l\n' 2>/dev/null || true)

busy() {
  [ -n "${OPEN[$1]+x}" ]
}

freed=0
removed=0

drop_file() {
  local f="$1" base size
  base="$(basename "$f")"
  case "$base" in
    daily-state.json|daily-state-*.json|known-opcodes.json|login.log|cron.log)
      return
      ;;
  esac
  if busy "$f"; then
    echo "跳过正在写入: $f"
    return
  fi
  size=$(stat -c %s "$f" 2>/dev/null || echo 0)
  rm -f -- "$f"
  freed=$((freed + size))
  removed=$((removed + 1))
  echo "删除 $f"
}

while IFS= read -r -d '' f; do
  drop_file "$f"
done < <(find "$LOGS" -type f -mtime "$AGE" ! -path "$LOGS/streams/*" -print0)

if [ -d "$LOGS/streams" ]; then
  while IFS= read -r -d '' d; do
    skip=0
    while IFS= read -r -d '' f; do
      if busy "$f"; then
        skip=1
        break
      fi
    done < <(find "$d" -type f -print0)
    if [ "$skip" -eq 1 ]; then
      echo "跳过正在写入: $d"
      continue
    fi
    size=$(du -sb "$d" | awk '{print $1}')
    rm -rf -- "$d"
    freed=$((freed + size))
    removed=$((removed + 1))
    echo "删除 $d"
  done < <(find "$LOGS/streams" -mindepth 1 -maxdepth 1 -type d -mtime "$AGE" -print0)
fi

awk -v n="$freed" -v c="$removed" 'BEGIN {
  split("B K M G T", u, " ")
  i = 1
  x = n + 0
  while (x >= 1024 && i < 5) { x /= 1024; i++ }
  printf "共删除 %d 项，约 %.1f %s\n", c, x, u[i]
}'
du -h --max-depth=1 "$LOGS" | sort -h
