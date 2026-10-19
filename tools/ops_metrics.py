# tankstorm-keepalive  Copyright (C) 2026 Dimlitter
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# 本程序是自由软件：你可以依据自由软件基金会发布的 GNU Affero 通用公共许可证
# （第 3 版，或你选择的任何更新版本）之条款，再分发和/或修改它。
# 本程序希望能有用，但不提供任何担保；甚至不含适销性或特定用途适用性的默示担保。
# 详见随附的 LICENSE 文件，或 <https://www.gnu.org/licenses/>。
"""记下内存、日志体积和数据库体积，方便长期看涨势。

在项目根目录执行。每跑一次追加一行到 logs/ops-metrics.jsonl，并在终端打一行。
监控文件本身不计入日志体积。

    python3 tools/ops_metrics.py
    python3 tools/ops_metrics.py --interval 300
    python3 tools/ops_metrics.py --report

服务器上可以每 5 分钟记一次：

    */5 * * * * cd /home/ubuntu/tankstorm-keepalive && python3 tools/ops_metrics.py
"""

import argparse
import json
import os
import time
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(ROOT, "logs")
DB_PATH = os.path.join(ROOT, "city_players.db")
HISTORY = os.path.join(LOG_DIR, "ops-metrics.jsonl")
BEIJING = timezone(timedelta(hours=8))
KEEP_DAYS = 90


def _now():
    return datetime.now(BEIJING).replace(microsecond=0)


def _fmt(n):
    n = int(n or 0)
    sign = "-" if n < 0 else ""
    value = float(abs(n))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            if unit == "B":
                return f"{sign}{int(value)} B"
            text = f"{value:.1f}" if value < 10 else f"{value:.0f}"
            return f"{sign}{text} {unit}"
        value /= 1024
    return f"{sign}{value:.0f} TB"


def _meminfo():
    out = {}
    with open("/proc/meminfo", encoding="utf-8") as f:
        for line in f:
            key, rest = line.split(":", 1)
            out[key] = int(rest.strip().split()[0]) * 1024
    return out


def _rss(pid):
    try:
        with open(f"/proc/{pid}/status", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        return 0
    return 0


def _cmdline(pid):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            raw = f.read().replace(b"\0", b" ").decode("utf-8", "replace").strip()
    except OSError:
        return ""
    return " ".join(raw.split())


def _under_root(path):
    if not path or not path.startswith("/"):
        return False
    root = os.path.realpath(ROOT)
    try:
        path = os.path.realpath(path)
    except OSError:
        return False
    return path == root or path.startswith(root + os.sep)


def _cmd_points_here(cmd):
    return any(_under_root(token) for token in cmd.split())


def _tank_pids():
    found = []
    try:
        names = os.listdir("/proc")
    except OSError:
        return found
    for name in names:
        if not name.isdigit():
            continue
        pid = int(name)
        cmd = _cmdline(pid)
        if not cmd or "ops_metrics.py" in cmd:
            continue
        if "tankstorm" in cmd or _cmd_points_here(cmd):
            found.append((pid, cmd))
            continue
        # systemd 的启动命令是 python main.py / web.py，路径不一定出现在命令行里。
        try:
            cwd = os.readlink(f"/proc/{pid}/cwd")
        except OSError:
            continue
        if not _under_root(cwd):
            continue
        base = os.path.basename(cmd.split()[0])
        if base.startswith("python"):
            found.append((pid, cmd))
    return found


def _cpu_times(pids):
    with open("/proc/stat", encoding="utf-8") as f:
        parts = f.readline().split()
    # guest 已经含在 user/nice 里，只加到 steal。
    nums = [int(x) for x in parts[1:9]]
    total = sum(nums)
    idle = nums[3] if len(nums) > 3 else 0
    proc = {}
    for pid in pids:
        try:
            with open(f"/proc/{pid}/stat", encoding="utf-8") as f:
                text = f.read()
        except OSError:
            continue
        end = text.rfind(")")
        if end < 0:
            continue
        fields = text[end + 2:].split()
        if len(fields) < 13:
            continue
        proc[pid] = int(fields[11]) + int(fields[12])
    return total, idle, proc


def _cpu_sample(pids, wait=0.2):
    t0, idle0, p0 = _cpu_times(pids)
    time.sleep(wait)
    t1, idle1, p1 = _cpu_times(pids)
    span = max(1, t1 - t0)
    used = {}
    for pid in pids:
        if pid in p0 and pid in p1:
            used[pid] = (p1[pid] - p0[pid]) / span * 100
    machine = (span - max(0, idle1 - idle0)) / span * 100
    return machine, used


def _bucket(rel):
    name = os.path.basename(rel)
    head = rel.split("/", 1)[0]
    if head in ("streams", "task", "enc", "packets"):
        return head
    if name == "ops-metrics.jsonl":
        return "metrics"
    if name.startswith("frames-") and name.endswith(".jsonl"):
        return "frames"
    if name == "login.log" or name.startswith("login.log."):
        return "login"
    if name.endswith(".log"):
        return "run"
    return "other"


def _logs():
    parts = {}
    files = 0
    if not os.path.isdir(LOG_DIR):
        return 0, 0, parts
    for dirpath, _dirs, names in os.walk(LOG_DIR):
        for name in names:
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, LOG_DIR)
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            files += 1
            key = _bucket(rel)
            parts[key] = parts.get(key, 0) + size
    total = sum(size for key, size in parts.items() if key != "metrics")
    return total, files, parts


def _db():
    files = {}
    for suffix in ("", "-wal", "-shm"):
        path = DB_PATH + suffix
        try:
            files[suffix or "db"] = os.path.getsize(path)
        except OSError:
            files[suffix or "db"] = 0
    return files


def _disk_free():
    st = os.statvfs(ROOT)
    return st.f_bavail * st.f_frsize


def sample():
    info = _meminfo()
    pids = _tank_pids()
    cpu_pct, per_cpu = _cpu_sample([pid for pid, _cmd in pids])
    procs = []
    rss = 0
    for pid, cmd in pids:
        got = _rss(pid)
        rss += got
        procs.append({
            "pid": pid,
            "rss": got,
            "cpu": round(per_cpu.get(pid, 0.0), 1),
            "cmd": cmd[:180],
        })
    procs.sort(key=lambda row: row["rss"], reverse=True)
    logs, log_files, parts = _logs()
    db = _db()
    return {
        "at": _now().isoformat(),
        "mem_total": info.get("MemTotal", 0),
        "mem_available": info.get("MemAvailable", 0),
        "mem_cached": info.get("Cached", 0),
        "swap_used": max(0, info.get("SwapTotal", 0) - info.get("SwapFree", 0)),
        "load1": round(os.getloadavg()[0], 2),
        "cpu_pct": round(cpu_pct, 1),
        "proc_rss": rss,
        "proc_cpu_pct": round(sum(per_cpu.values()), 1),
        "procs": procs,
        "db": db.get("db", 0),
        "db_wal": db.get("-wal", 0),
        "db_shm": db.get("-shm", 0),
        "logs": logs,
        "log_files": log_files,
        "logs_parts": parts,
        "disk_free": _disk_free(),
    }


def _line(row):
    used = row["mem_total"] - row["mem_available"]
    parts = row.get("logs_parts") or {}
    bits = []
    for key in ("frames", "streams", "run", "task", "enc", "packets", "login", "other"):
        if parts.get(key):
            bits.append(f"{key} {_fmt(parts[key])}")
    detail = "，".join(bits[:4])
    db = row["db"] + row["db_wal"] + row["db_shm"]
    text = (
        f"{row['at']}  内存 {_fmt(used)}/{_fmt(row['mem_total'])}"
        f"（可用 {_fmt(row['mem_available'])}，缓存 {_fmt(row['mem_cached'])}）"
        f"  进程 {_fmt(row['proc_rss'])}  CPU {row['cpu_pct']:.0f}%（进程 {row['proc_cpu_pct']:.0f}%）"
        f"  日志 {_fmt(row['logs'])}"
    )
    if detail:
        text += f"（{detail}）"
    text += f"  库 {_fmt(db)}"
    if row["db_wal"]:
        text += f"（wal {_fmt(row['db_wal'])}）"
    text += f"  磁盘剩 {_fmt(row['disk_free'])}"
    return text


def _append(row):
    os.makedirs(LOG_DIR, exist_ok=True)
    trim = os.path.isfile(HISTORY) and os.path.getsize(HISTORY) > 8 * 1024 * 1024
    old = []
    if trim:
        keep_after = _now() - timedelta(days=KEEP_DAYS)
        with open(HISTORY, encoding="utf-8") as f:
            for line in f:
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                try:
                    at = datetime.fromisoformat(item.get("at") or "")
                except ValueError:
                    continue
                if at.tzinfo is None:
                    at = at.replace(tzinfo=BEIJING)
                if at >= keep_after:
                    old.append(item)
    with open(HISTORY, "w" if trim else "a", encoding="utf-8") as f:
        for item in old:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _load():
    rows = []
    if not os.path.isfile(HISTORY):
        return rows
    with open(HISTORY, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _per_day(delta, hours):
    if hours <= 0:
        return 0
    return delta / hours * 24


def report():
    rows = _load()
    if not rows:
        print("还没有记录。先跑一次 python3 tools/ops_metrics.py")
        return
    first, last = rows[0], rows[-1]
    try:
        start = datetime.fromisoformat(first["at"])
        end = datetime.fromisoformat(last["at"])
    except ValueError:
        start = end = _now()
    hours = max(0.0, (end - start).total_seconds() / 3600)
    print(f"记录 {len(rows)} 条，从 {first['at']} 到 {last['at']}，跨 {hours:.1f} 小时")
    print(_line(last))
    if len(rows) == 1:
        return
    pairs = (
        ("进程内存", "proc_rss"),
        ("日志", "logs"),
        ("数据库", "db"),
        ("数据库 wal", "db_wal"),
        ("可用内存", "mem_available"),
    )
    for label, key in pairs:
        delta = int(last.get(key) or 0) - int(first.get(key) or 0)
        print(f"  {label} {_fmt(delta)}，大约每天 {_fmt(_per_day(delta, hours))}")
    grown = []
    first_parts = first.get("logs_parts") or {}
    last_parts = last.get("logs_parts") or {}
    for key in sorted(set(first_parts) | set(last_parts)):
        if key == "metrics":
            continue
        delta = int(last_parts.get(key) or 0) - int(first_parts.get(key) or 0)
        if delta:
            grown.append((delta, key))
    grown.sort(reverse=True)
    if grown:
        text = "，".join(f"{key} {_fmt(delta)}" for delta, key in grown[:6])
        print(f"  日志里变化大的：{text}")


def main():
    parser = argparse.ArgumentParser(description="记录内存、日志体积和数据库体积")
    parser.add_argument("--interval", type=int, default=0, help="每隔多少秒记一次，0 表示只记一次")
    parser.add_argument("--report", action="store_true", help="只看已经记下的变化")
    args = parser.parse_args()
    if args.report:
        report()
        return
    while True:
        row = sample()
        _append(row)
        print(_line(row))
        if args.interval <= 0:
            return
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
