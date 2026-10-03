# tankstorm-keepalive  Copyright (C) 2026 Dimlitter
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# 本程序是自由软件：你可以依据自由软件基金会发布的 GNU Affero 通用公共许可证
# （第 3 版，或你选择的任何更新版本）之条款，再分发和/或修改它。
# 本程序希望能有用，但不提供任何担保；甚至不含适销性或特定用途适用性的默示担保。
# 详见随附的 LICENSE 文件，或 <https://www.gnu.org/licenses/>。
"""扫码登录测试。走现有 QQSession.start_qr / poll_qr，票据单独放，不绑攻打号。"""

import glob
import os
import threading
import time

from . import citydb
from .log import get_logger
from .paths import user_path
from .qq_login import QQSession, ticket_report_file

log = get_logger()

_QR_WAIT = 180
_guard = threading.Lock()
_boxes = {}


def _files(user_id: int):
    folder = user_path("accounts")
    os.makedirs(folder, exist_ok=True)
    cookie = os.path.join(folder, f"qr-lab-{int(user_id)}.json")
    return cookie, cookie[:-5] + ".qrcode.png"


def qr_path(user_id: int) -> str:
    return _files(user_id)[1]


class _Box:
    def __init__(self, user_id: int):
        cookie, qr = _files(user_id)
        self.user_id = int(user_id)
        self.qq = QQSession(cookie, qrcode_file=qr)
        self.phase = "idle"
        self.msg = ""
        self.game_ok = None
        self.started = 0.0
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
    waiting = box.phase in ("qr", "scanned")
    left = 0
    if box.started and waiting:
        left = max(0, int(_QR_WAIT - (time.time() - box.started)))
    return {
        "phase": box.phase,
        "msg": box.msg,
        "game_ok": box.game_ok,
        "qr": waiting and os.path.isfile(box.qq.qrcode_file),
        "wait_left": left,
        "uin": report.get("uin") or "",
        "long_term": bool(report.get("long_term")),
        "skey_left": report.get("skey_left"),
        "p_skey_left": report.get("p_skey_left"),
        "tickets": report.get("tickets") or [],
        "cookie": f"accounts/qr-lab-{box.user_id}.json",
    }


def _apply_poll(box: _Box, result: dict) -> None:
    code = result.get("code") or ""
    if result.get("ok"):
        box.phase = "ok"
        box.msg = "扫码登录完成"
        box.game_ok = True
        return
    if result.get("done"):
        if code == "65":
            box.phase = "expired"
            box.msg = "二维码已失效"
        else:
            box.phase = "fail"
            box.msg = result.get("msg") or "扫码失败"
        return
    if code == "67":
        box.phase = "scanned"
        box.msg = "已扫码，请在手机上确认"
        return
    box.phase = "qr"
    if result.get("msg") and result.get("code"):
        box.msg = result["msg"]


def snapshot(user_id: int) -> dict:
    box = _box(user_id)
    with box.lock:
        if box.phase in ("qr", "scanned"):
            if box.started and time.time() - box.started > _QR_WAIT:
                box.phase = "expired"
                box.msg = "扫码超时（180 秒）"
            else:
                _apply_poll(box, box.qq.poll_qr())
        return _view(box)


def start(user_id: int) -> dict:
    box = _box(user_id)
    with box.lock:
        started = box.qq.start_qr(push_uin=False)
        box.game_ok = None
        box.started = time.time()
        if not started.get("ok"):
            box.phase = "fail"
            box.msg = started.get("why") or "二维码请求失败"
        else:
            box.phase = "qr"
            box.msg = "请用另一台设备上的手机 QQ 扫码。不要把图存进同一台手机相册再扫。"
        log.info("扫码测试：%s %s", citydb.username_of(user_id), box.msg)
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
        cookie, qr = _files(user_id)
        for path in (cookie, qr):
            try:
                os.remove(path)
            except OSError:
                pass
        box.qq = QQSession(cookie, qrcode_file=qr)
        box.phase = "idle"
        box.msg = "已清掉测试票据"
        box.game_ok = None
        box.started = 0.0
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
