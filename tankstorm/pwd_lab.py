# tankstorm-keepalive  Copyright (C) 2026 Dimlitter
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# 本程序是自由软件：你可以依据自由软件基金会发布的 GNU Affero 通用公共许可证
# （第 3 版，或你选择的任何更新版本）之条款，再分发和/或修改它。
# 本程序希望能有用，但不提供任何担保；甚至不含适销性或特定用途适用性的默示担保。
# 详见随附的 LICENSE 文件，或 <https://www.gnu.org/licenses/>。
"""密码登录测试。走 QQSession.password_login，票据单独放，不绑攻打号。"""

import glob
import os
import threading

from . import citydb
from .log import get_logger
from .paths import user_path
from .qq_login import QQSession, ticket_report_file

log = get_logger()

_guard = threading.Lock()
_boxes = {}


def _cookie_path(user_id: int) -> str:
    folder = user_path("accounts")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, f"pwd-lab-{int(user_id)}.json")


class _Box:
    def __init__(self, user_id: int):
        self.user_id = int(user_id)
        self.qq = QQSession(_cookie_path(user_id))
        self.phase = "idle"
        self.msg = ""
        self.game_ok = None
        self.low_login = False
        self.aid = ""
        self.sid = ""
        self.cap_cd = ""
        self.lock = threading.Lock()


def _box(user_id: int) -> _Box:
    user_id = int(user_id)
    with _guard:
        box = _boxes.get(user_id)
        if box is None:
            box = _Box(user_id)
            _boxes[user_id] = box
        return box


def _view(box: _Box) -> dict:
    report = box.qq.ticket_report()
    pending = getattr(box.qq, "_pwd_pending", None) or {}
    return {
        "phase": box.phase,
        "msg": box.msg,
        "game_ok": box.game_ok,
        "low_login": bool(box.low_login),
        "uin": report.get("uin") or pending.get("uin") or "",
        "long_term": bool(report.get("long_term")),
        "skey_left": report.get("skey_left"),
        "p_skey_left": report.get("p_skey_left"),
        "tickets": report.get("tickets") or [],
        "cookie": f"accounts/pwd-lab-{box.user_id}.json",
        "aid": box.aid or "",
        "sid": box.sid or "",
        "cap_cd": box.cap_cd or "",
    }


def snapshot(user_id: int) -> dict:
    box = _box(user_id)
    with box.lock:
        return _view(box)


def login(user_id: int, data: dict) -> dict:
    uin = str((data or {}).get("qq") or "").strip()
    password = str((data or {}).get("password") or "")
    low_login = bool((data or {}).get("low_login"))
    ticket = str((data or {}).get("ticket") or "").strip()
    randstr = str((data or {}).get("randstr") or "").strip()
    box = _box(user_id)
    with box.lock:
        result = box.qq.password_login(
            uin, password, low_login=low_login, ticket=ticket, randstr=randstr)
        box.low_login = low_login
        box.game_ok = None
        if result.get("ok"):
            box.phase = "ok"
            box.msg = result.get("msg") or "密码登录完成"
            box.game_ok = True
            box.aid = ""
            box.sid = ""
            box.cap_cd = ""
        elif result.get("captcha"):
            box.phase = "captcha"
            box.msg = result.get("msg") or "请完成滑动验证"
            box.aid = str(result.get("aid") or "")
            box.sid = str(result.get("sid") or "")
            box.cap_cd = str(result.get("cap_cd") or "")
        else:
            box.phase = "fail"
            box.msg = result.get("msg") or "密码登录失败"
            box.aid = ""
            box.sid = ""
            box.cap_cd = ""
        log.info("密码登录测试：%s %s", citydb.username_of(user_id), box.msg)
        return _view(box)


def check(user_id: int) -> dict:
    box = _box(user_id)
    with box.lock:
        box.game_ok = box.qq.is_valid()
        if box.game_ok:
            box.qq._save_cookies()
            box.msg = "游戏页认这张票"
        else:
            box.msg = "游戏页不认这张票"
        return _view(box)


def clear(user_id: int) -> dict:
    box = _box(user_id)
    with box.lock:
        path = _cookie_path(user_id)
        try:
            os.remove(path)
        except OSError:
            pass
        box.qq = QQSession(path)
        box.phase = "idle"
        box.msg = "已清掉测试票据"
        box.game_ok = None
        box.low_login = False
        box.aid = ""
        box.sid = ""
        box.cap_cd = ""
        return _view(box)


def account_tickets() -> list:
    """现有攻打号 cookie 的过期时间。只读，不访问游戏。"""
    items = []
    seen = set()
    for row in citydb.list_attack_fighters():
        rel = str(row.get("cookie") or "")
        path = user_path(rel) if rel else ""
        report = ticket_report_file(path)
        if path:
            seen.add(os.path.abspath(path))
        items.append({
            "username": row.get("username") or "",
            "qq": row.get("qq") or report.get("uin") or "",
            "cookie": rel,
            "phase": row.get("phase") or "",
            "online": bool(row.get("online")),
            "uin": report.get("uin") or "",
            "long_term": bool(report.get("long_term")),
            "skey_left": report.get("skey_left"),
            "p_skey_left": report.get("p_skey_left"),
            "tickets": report.get("tickets") or [],
            "missing": bool(report.get("missing")),
        })
    folder = user_path("accounts")
    if not os.path.isdir(folder):
        return items
    for path in sorted(glob.glob(os.path.join(folder, "qq-*.json"))):
        if os.path.abspath(path) in seen:
            continue
        report = ticket_report_file(path)
        items.append({
            "username": "",
            "qq": report.get("uin") or "",
            "cookie": os.path.join("accounts", os.path.basename(path)),
            "phase": "",
            "online": False,
            "uin": report.get("uin") or "",
            "long_term": bool(report.get("long_term")),
            "skey_left": report.get("skey_left"),
            "p_skey_left": report.get("p_skey_left"),
            "tickets": report.get("tickets") or [],
            "missing": bool(report.get("missing")),
        })
    return items
