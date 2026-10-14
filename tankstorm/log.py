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
import sys
import threading
from datetime import date

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


class DailyFileHandler(logging.FileHandler):
    """主日志按本地日期写 logs/YYYY-MM-DD.log。过了 24 点，下一条日志换到新文件。"""

    def __init__(self):
        day = date.today().isoformat()
        os.makedirs(LOG_DIR, exist_ok=True)
        self._day = day
        super().__init__(os.path.join(LOG_DIR, f"{day}.log"), encoding="utf-8")

    def emit(self, record):
        try:
            day = date.fromtimestamp(record.created).isoformat()
        except (TypeError, ValueError, OverflowError, OSError):
            day = date.today().isoformat()
        if day != self._day:
            try:
                self._switch(day)
            except Exception:
                self.handleError(record)
        super().emit(record)

    def _switch(self, day: str) -> None:
        stream = self.stream
        if stream:
            try:
                self.flush()
            except Exception:
                pass
            self.stream = None
            try:
                stream.close()
            except OSError:
                pass
        self._day = day
        os.makedirs(LOG_DIR, exist_ok=True)
        self.baseFilename = os.path.abspath(os.path.join(LOG_DIR, f"{day}.log"))
        self.stream = self._open()


def get_logger(name: str = "tankstorm") -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S")

    fh = DailyFileHandler()
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(fh)

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
