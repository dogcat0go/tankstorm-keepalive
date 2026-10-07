# tankstorm-keepalive  Copyright (C) 2026 Dimlitter
# SPDX-License-Identifier: AGPL-3.0-or-later
"""授权时长。编译进服务器地址和会员账号时才启用，源码运行不限制。

对接后台 POST /api/clock：先用会员账号登录拿到会话，再提交本机单调计时的
毫秒数 mono_ms。响应 remaining_ms 是剩余毫秒，null 表示不限期。
拿到之后只用 time.monotonic 倒计时。
编译自带 GRACE_SEC（600）秒：这 10 分钟里必须拿到服务器的数字，否则到期。
这 10 分钟和单调计时记在 license_state.json，重启不会重新发给你。
曾经拿到过服务器时长之后，以后每次打开都必须再拿到，拿不到就到期。
"""

import json
import os
import threading
import time

import requests

from . import lockqq, paths
from .log import get_logger
from ._trial_bind import (
    GRACE_SEC, LICENSE_PASS_BLOB, LICENSE_PASS_SEED, LICENSE_URL,
    LICENSE_USER_BLOB, LICENSE_USER_SEED)

log = get_logger()

_lock = threading.Lock()
_started = False
_mono0 = 0.0
_grace_used0 = 0.0
_last_saved = 0.0
_prior = False
_settled = False
_grant_remain = None
_grant_at = None
_unlimited = False
_mono_base = 0
_last_mono_saved = 0
_user = ""
_password = ""


def enabled() -> bool:
    return (bool(str(LICENSE_URL or "").strip()) and int(GRACE_SEC or 0) > 0
            and bool(LICENSE_USER_BLOB) and bool(LICENSE_PASS_BLOB))


def start() -> None:
    global _started
    if not enabled() or _started:
        return
    _started = True
    global _user, _password
    try:
        _user = lockqq._open(bytes(LICENSE_USER_SEED), bytes(LICENSE_USER_BLOB))
        _password = lockqq._open(bytes(LICENSE_PASS_SEED), bytes(LICENSE_PASS_BLOB))
    except (UnicodeDecodeError, ValueError):
        _user = ""
        _password = ""
    _load()
    with _lock:
        _save_locked()
    threading.Thread(target=_loop, name="license", daemon=True).start()


def beat() -> None:
    """把已用掉的自带时长落盘。倒计时本身仍看 monotonic。"""
    if not enabled():
        return
    with _lock:
        mono_due = _mono_now_locked() - _last_mono_saved >= 10000
        grace_due = _grant_at is None and not _prior and (
            _grace_used_now() + 0.001 >= GRACE_SEC or _grace_used_now() - _last_saved >= 10)
        if not mono_due and not grace_due:
            return
        _save_locked()


def seconds_left():
    """剩余秒。None 表示这一次还在等服务器。未启用时也是 None。"""
    if not enabled():
        return None
    with _lock:
        if _unlimited:
            return float("inf")
        if _grant_at is not None:
            return max(0.0, _grant_remain - (time.monotonic() - _grant_at))
        # 自带时长已经用完，或以前拿到过服务器时长：这一次必须等服务器。
        if not _settled and (_prior or _grace_used_now() >= GRACE_SEC):
            return None
        if _settled and _grant_at is None:
            return 0.0
        return max(0.0, GRACE_SEC - _grace_used_now())


def blocked() -> bool:
    left = seconds_left()
    return enabled() and left is not None and left <= 0


def allow_run() -> bool:
    if not enabled():
        return True
    left = seconds_left()
    return left is not None and left > 0


def remain_text() -> str:
    left = seconds_left()
    if not enabled():
        return ""
    if left is None:
        return "正在核对剩余时长"
    if left == float("inf"):
        return "不限期"
    sec = max(0, int(left))
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    if h:
        return f"剩余 {h}小时{m:02d}分"
    return f"剩余 {m}分{s:02d}秒"


def _grace_used_now() -> float:
    return min(float(GRACE_SEC), _grace_used0 + (time.monotonic() - _mono0))


def _path() -> str:
    return paths.user_path("license_state.json")


def _mono_now_locked() -> int:
    return int(_mono_base + (time.monotonic() - _mono0) * 1000)


def _load() -> None:
    global _mono0, _grace_used0, _prior, _last_saved, _mono_base, _last_mono_saved
    _mono0 = time.monotonic()
    _grace_used0 = 0.0
    _prior = False
    _mono_base = 0
    try:
        with open(_path(), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        data = {}
    if isinstance(data, dict):
        try:
            _grace_used0 = max(0.0, float(data.get("grace_used") or 0))
        except (TypeError, ValueError):
            _grace_used0 = 0.0
        _prior = bool(data.get("had_server"))
        try:
            wall = float(data.get("wall") or 0)
        except (TypeError, ValueError):
            wall = 0.0
        if wall and time.time() + 30 < wall:
            _grace_used0 = float(GRACE_SEC)
        try:
            _mono_base = max(0, int(data.get("mono_ms") or 0))
        except (TypeError, ValueError):
            _mono_base = 0
    if _grace_used0 > GRACE_SEC:
        _grace_used0 = float(GRACE_SEC)
    _last_saved = _grace_used0
    _last_mono_saved = _mono_base


def _save_locked() -> None:
    global _last_saved, _last_mono_saved
    used = float(GRACE_SEC) if _prior else _grace_used_now()
    mono = _mono_now_locked()
    path = _path()
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"grace_used": used, "had_server": _prior,
                       "wall": time.time(), "mono_ms": mono}, f)
        os.replace(tmp, path)
    except OSError as exc:
        log.info("授权计时没写下来：%s", exc)
        return
    _last_saved = used
    _last_mono_saved = mono


def _api(path: str) -> str:
    return str(LICENSE_URL).rstrip("/") + path


def _accept(seconds, unlimited: bool, force: bool = False) -> bool:
    """记下一份服务器时长。返回 False 表示这次应答晚于编译自带的 10 分钟，已丢掉。"""
    global _prior, _settled, _grant_remain, _grant_at, _unlimited
    with _lock:
        late = ((not force) and (not _prior) and _grace_used0 < GRACE_SEC
                and _grace_used_now() >= GRACE_SEC)
        if not late:
            _unlimited = unlimited
            _grant_remain = None if unlimited else max(0.0, float(seconds))
            _grant_at = time.monotonic()
            _prior = True
        _settled = True
        _save_locked()
    return not late


def _once() -> bool:
    if not _user or not _password:
        log.error("授权账号没有编译进这个包")
        _accept(0, False, force=True)
        return True
    session = requests.Session()
    session.trust_env = False
    session.proxies = {}
    try:
        login = session.post(
            _api("/api/login"),
            json={"username": _user, "password": _password},
            timeout=8)
    except requests.RequestException as exc:
        log.info("授权服务器没回应：%s", exc)
        return False
    if login.status_code == 403:
        log.error("授权账号已过期")
        _accept(0, False, force=True)
        return True
    if login.status_code == 401:
        log.error("授权账号或密码不对")
        _accept(0, False, force=True)
        return True
    if login.status_code != 200:
        log.info("授权登录没成：HTTP %s", login.status_code)
        return False
    try:
        token = session.cookies.get("ts_session") or ""
        headers = {"Authorization": "Bearer " + token} if token else {}
        with _lock:
            mono = _mono_now_locked()
        clock = session.post(
            _api("/api/clock"), json={"mono_ms": mono}, headers=headers, timeout=8)
        data = clock.json()
    except (requests.RequestException, ValueError) as exc:
        log.info("授权服务器没回应：%s", exc)
        return False
    if clock.status_code != 200 or not isinstance(data, dict) or "remaining_ms" not in data:
        log.info("授时没成：HTTP %s", clock.status_code)
        return False
    remaining = data.get("remaining_ms")
    if remaining is None:
        ok = _accept(0, True)
        if ok:
            log.info("服务器剩余时长不限期")
        else:
            log.error("剩余时长到得太晚，已超过编译时的 10 分钟")
        return True
    try:
        ms = int(remaining)
    except (TypeError, ValueError):
        return False
    if ms < 0:
        return False
    ok = _accept(ms / 1000.0, False)
    if ok:
        log.info("服务器剩余时长 %s 毫秒", ms)
    else:
        log.error("剩余时长到得太晚，已超过编译时的 10 分钟")
    return True


def _loop() -> None:
    global _settled
    fails = 0
    while True:
        if _once():
            return
        fails += 1
        with _lock:
            prior = _prior
            started_spent = _grace_used0 >= GRACE_SEC
            over = _grace_used_now() >= GRACE_SEC
        if prior or started_spent:
            if fails < 6:
                time.sleep(5)
                continue
            with _lock:
                _settled = True
                _save_locked()
            log.error("没有从服务器拿到剩余时长")
            return
        if over:
            with _lock:
                _settled = True
                _save_locked()
            log.error("10 分钟内没有从服务器拿到剩余时长")
            return
        time.sleep(5)
