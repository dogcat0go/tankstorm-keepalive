# tankstorm-keepalive  Copyright (C) 2026 Dimlitter
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# 本程序是自由软件：你可以依据自由软件基金会发布的 GNU Affero 通用公共许可证
# （第 3 版，或你选择的任何更新版本）之条款，再分发和/或修改它。
# 本程序希望能有用，但不提供任何担保；甚至不含适销性或特定用途适用性的默示担保。
# 详见随附的 LICENSE 文件，或 <https://www.gnu.org/licenses/>。
import atexit
import logging
import logging.handlers
import os
import queue
import re
import sys
import threading
from datetime import date, datetime, timedelta, timezone

from .paths import user_path                      # noqa: E402

# 日志和当天任务计数是**可写数据**，打包后要落在 exe 旁边而不是临时解压目录，
# 否则每次运行完就被删掉，当天次数永远从零开始。见 paths.py。
LOG_DIR = user_path("logs")

_log_listener = None
_login_logger = None
_console_handler = None


def console_lock():
    """控制台打印和二维码上色共用这把锁，避免两行叠在一起。"""
    handler = _console_handler
    if handler is None or handler.lock is None:
        return threading.Lock()
    return handler.lock


def _force_utf8_console() -> None:
    """把 stdout/stderr 钉成 UTF-8。

    输出**重定向到文件或管道**时，Python 不走控制台的 Unicode 通道，
    而是回落到系统 locale 编码 —— 中文 Windows 上是 GBK，编不了日志里的
    ✅ / ❌，于是抛 UnicodeEncodeError 把整个进程带崩。
    README 里 cron 那行 `>> logs/cron.log` 正是这种用法，打包成 exe 后
    用户更容易这么跑，所以这不是显示问题，是会真的挂掉。

    errors="replace" 是兜底：再冷门的字符最多显示成问号，不该让程序退出。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass          # 流被换成了不支持 reconfigure 的对象，忽略即可


def _stop_log_listener() -> None:
    """先把控制台队列打完再退出。否则后台线程和收尾抢 stdout，进程直接崩。"""
    global _log_listener
    lis = _log_listener
    if lis is None:
        return
    _log_listener = None
    try:
        lis.stop()
    except Exception:
        pass


_force_utf8_console()
atexit.register(_stop_log_listener)


def get_login_logger() -> logging.Logger:
    """登录过程单独一份日志，按天切分。每行前缀是登录账号名称和攻打 QQ。"""
    global _login_logger
    if _login_logger is not None:
        return _login_logger
    logger = logging.getLogger("tankstorm.login")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    os.makedirs(LOG_DIR, exist_ok=True)
    path = os.path.join(LOG_DIR, "login.log")
    fh = logging.handlers.TimedRotatingFileHandler(
        path, when="midnight", backupCount=14, encoding="utf-8")
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S"))
    logger.addHandler(fh)
    _login_logger = logger
    return logger


_task_loggers = {}
_task_log_lock = threading.Lock()
_mirror_local = threading.local()
_WAR_HEADS = ("[国战]", "[移动]", "[打人]", "[路线]", "[索敌]", "[魔多]",
              "―― 国战", "―― 摩多", "―― 移动")
PINNED_TASKS = ("拒绝超级强攻", "国战助手")


def bind_account_log(qq: str) -> str:
    """这条线程接下来的强攻拒绝和国战日志记到这个攻打 QQ。返回之前的号。"""
    prev = str(getattr(_mirror_local, "qq", "") or "")
    _mirror_local.qq = "".join(ch for ch in str(qq or "") if ch.isdigit())
    return prev


def _account_log_qq() -> str:
    qq = str(getattr(_mirror_local, "qq", "") or "")
    if qq:
        return qq
    try:
        from .citydb import attack_context_qq
    except Exception:
        return ""
    return "".join(ch for ch in str(attack_context_qq() or "") if ch.isdigit())


def _account_log_task(msg: str) -> str:
    """运行日志里这两类单独抄一份，管理页按攻打 QQ 能打开。"""
    text = str(msg or "").strip()
    if not text:
        return ""
    if "超级强攻" in text:
        return "拒绝超级强攻"
    if text.startswith(_WAR_HEADS):
        return "国战助手"
    return ""


class _AccountTaskMirror(logging.Filter):
    def filter(self, record):
        if record.levelno < logging.INFO or getattr(_mirror_local, "busy", False):
            return True
        try:
            title = _account_log_task(record.getMessage())
            qq = _account_log_qq() if title else ""
        except Exception:
            return True
        if not title or not qq:
            return True
        _mirror_local.busy = True
        try:
            task_log(qq, title, record.getMessage())
        except Exception:
            return True
        finally:
            _mirror_local.busy = False
        return True


def task_log(qq: str, task: str, msg: str) -> None:
    """按攻打 QQ 和任务名各写一份日志。文件在 logs/task/QQ/任务名.log。"""
    digits = "".join(ch for ch in str(qq or "") if ch.isdigit())
    who = digits or "未知QQ"
    title = " ".join(str(task or "未知任务").split())[:40] or "未知任务"
    safe = "".join(ch for ch in title if ch not in '\\/:*?"<>|') or "任务"
    key = (who, safe)
    with _task_log_lock:
        logger = _task_loggers.get(key)
        if logger is None:
            logger = logging.getLogger(f"tankstorm.task.{who}.{safe}")
            logger.setLevel(logging.INFO)
            logger.propagate = False
            folder = os.path.join(LOG_DIR, "task", who)
            os.makedirs(folder, exist_ok=True)
            fh = logging.handlers.TimedRotatingFileHandler(
                os.path.join(folder, safe + ".log"),
                when="midnight", backupCount=14, encoding="utf-8")
            fh.setFormatter(logging.Formatter(
                "%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S"))
            logger.addHandler(fh)
            _task_loggers[key] = logger
    logger.info("%s %s %s", who, title, " ".join(str(msg or "").split()))


def get_logger(name: str = "tankstorm") -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S")

    os.makedirs(LOG_DIR, exist_ok=True)
    logfile = os.path.join(LOG_DIR, f"{date.today().isoformat()}.log")
    fh = logging.FileHandler(logfile, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(fh)
    logger.addFilter(_AccountTaskMirror())

    # 控制台写到独立线程：点选窗口卡住的是打印，不能把打人线程一起堵住。
    global _console_handler
    console = logging.StreamHandler(sys.stdout)
    _console_handler = console
    console.setLevel(logging.INFO)
    console.setFormatter(fmt)
    q = queue.Queue(-1)
    qh = logging.handlers.QueueHandler(q)
    qh.setLevel(logging.INFO)
    logger.addHandler(qh)
    global _log_listener
    _log_listener = logging.handlers.QueueListener(q, console)
    _log_listener.start()
    return logger


_DAY_LOG_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.log$")
_ORDER_ID_RE = re.compile(r"订单\s+(\d{1,12})\b")
_TASK_ROTATED_RE = re.compile(r"^(.+)\.log\.(\d{4}-\d{2}-\d{2})$")
_LOG_LINE_MAX = 500
_DAY_LOG_BYTES = 8_000_000
_TASK_LOG_BYTES = 1_500_000


def _log_start(days: int) -> date:
    """近几天的起点。日志文件名用机器本地日期，页面按北京时间理解，两边都算上。"""
    days = max(1, min(int(days or 1), 7))
    today_local = date.today()
    today_bj = datetime.now(timezone(timedelta(hours=8))).date()
    return min(today_local, today_bj) - timedelta(days=days - 1)


def _inside_logs(path: str) -> str:
    """只读 logs 目录里面的普通文件。指到外面的路径丢掉。"""
    root = os.path.realpath(LOG_DIR)
    full = os.path.realpath(path)
    if full != root and not full.startswith(root + os.sep):
        return ""
    if not os.path.isfile(full):
        return ""
    return full


def _tail_lines(path: str, max_bytes: int):
    """从文件末尾最多读这么多字节，按行往外给。开头被截断的半行丢掉。"""
    try:
        size = os.path.getsize(path)
    except OSError:
        return
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            if size > max_bytes:
                handle.seek(max(0, size - max_bytes))
                handle.readline()
            for line in handle:
                text = line.rstrip("\n\r")
                if text.strip():
                    yield text
    except OSError:
        return


def _clip_line(text: str) -> str:
    if len(text) <= _LOG_LINE_MAX:
        return text
    return text[:_LOG_LINE_MAX] + "…"


def _names_mentioned(line: str, names: set) -> bool:
    """一行里「登录账号 甲、乙 后面的话」有没有这些名字。"""
    key = "登录账号 "
    start = 0
    while True:
        at = line.find(key, start)
        if at < 0:
            return False
        chunk = line[at + len(key):].split(" ", 1)[0]
        for part in chunk.split("、"):
            if part in names:
                return True
        start = at + len(key)


def _qq_mentioned(line: str, qq: str) -> bool:
    key = "攻打 QQ " + qq
    start = 0
    size = len(key)
    while True:
        at = line.find(key, start)
        if at < 0:
            return False
        after = line[at + size:at + size + 1]
        if not after.isdigit():
            return True
        start = at + size


def _order_mentioned(line: str, order_ids: set) -> bool:
    for match in _ORDER_ID_RE.finditer(line):
        if int(match.group(1)) in order_ids:
            return True
    return False


def _dated_log_files(days: int) -> list:
    """近几天的 logs/YYYY-MM-DD.log，按日期从旧到新。"""
    start = _log_start(days)
    folder = LOG_DIR
    if not os.path.isdir(folder):
        return []
    found = []
    for name in os.listdir(folder):
        match = _DAY_LOG_RE.fullmatch(name)
        if not match:
            continue
        try:
            day = date.fromisoformat(match.group(1))
        except ValueError:
            continue
        if day < start:
            continue
        path = _inside_logs(os.path.join(folder, name))
        if path:
            found.append((day, path))
    found.sort()
    return [path for _day, path in found]


def qq_log_lines(usernames, order_ids, qq: str, days: int, limit: int = 800) -> tuple:
    """运行日志里和这个攻打 QQ 对得上的行。返回 (行, 是否被截断)。

    对得上是指：写了这些登录账号、写了「攻打 QQ 号」、或写了这些订单号。
    """
    names = {str(name).strip() for name in usernames if str(name or "").strip()}
    ids = {int(item) for item in order_ids}
    qq = "".join(ch for ch in str(qq or "") if ch.isdigit())
    kept = []
    cut = False
    limit = max(1, min(int(limit or 800), 800))
    for path in _dated_log_files(days):
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        if size > _DAY_LOG_BYTES:
            cut = True
        for line in _tail_lines(path, _DAY_LOG_BYTES):
            hit = _order_mentioned(line, ids) or _names_mentioned(line, names)
            if not hit and qq:
                hit = _qq_mentioned(line, qq)
            if not hit:
                continue
            clipped = _clip_line(line)
            if clipped != line:
                cut = True
            kept.append(clipped)
    if len(kept) > limit:
        kept = kept[-limit:]
        cut = True
    return kept, cut


def qq_task_logs(qq: str, days: int, per_file: int = 160, max_tasks: int = 12,
                 pin=PINNED_TASKS) -> list:
    """logs/task/攻打QQ/ 里近几天的任务日志。每个任务一份，只留末尾。

    pin 里的任务始终带回，不占 max_tasks 的名额。
    """
    qq = "".join(ch for ch in str(qq or "") if ch.isdigit())
    if not qq:
        return []
    folder = os.path.join(LOG_DIR, "task", qq)
    root = os.path.realpath(LOG_DIR)
    real_folder = os.path.realpath(folder)
    if not os.path.isdir(real_folder):
        return []
    if real_folder != root and not real_folder.startswith(root + os.sep):
        return []
    start = _log_start(days)
    groups = {}
    try:
        names = os.listdir(real_folder)
    except OSError:
        return []
    for name in names:
        if not name or name.startswith(".") or "/" in name or "\\" in name:
            continue
        path = _inside_logs(os.path.join(real_folder, name))
        if not path:
            continue
        if name.endswith(".log") and ".log." not in name:
            task = name[:-4] or name
            key = "9999-99-99"
        else:
            match = _TASK_ROTATED_RE.fullmatch(name)
            if not match:
                continue
            try:
                when = date.fromisoformat(match.group(2))
            except ValueError:
                continue
            if when < start:
                continue
            task = match.group(1) or name
            key = match.group(2)
        groups.setdefault(task, []).append((key, path))
    ordered = sorted(groups)
    pinned = [task for task in pin if task in groups]
    rest = [task for task in ordered if task not in pinned]
    extra = len(rest) > max_tasks
    per_file = max(1, min(int(per_file or 160), 200))
    out = []
    for task in pinned + rest[:max_tasks]:
        lines = []
        cut = extra and task not in pinned
        for _key, path in sorted(groups[task]):
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            if size > _TASK_LOG_BYTES:
                cut = True
            for line in _tail_lines(path, _TASK_LOG_BYTES):
                clipped = _clip_line(line)
                if clipped != line:
                    cut = True
                lines.append(clipped)
        if len(lines) > per_file:
            lines = lines[-per_file:]
            cut = True
        out.append({"name": task, "lines": lines, "cut": cut})
    return out
