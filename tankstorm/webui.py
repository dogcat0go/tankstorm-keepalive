# tankstorm-keepalive  Copyright (C) 2026 Dimlitter
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# 本程序是自由软件：你可以依据自由软件基金会发布的 GNU Affero 通用公共许可证
# （第 3 版，或你选择的任何更新版本）之条款，再分发和/或修改它。
# 本程序希望能有用，但不提供任何担保；甚至不含适销性或特定用途适用性的默示担保。
# 详见随附的 LICENSE 文件，或 <https://www.gnu.org/licenses/>。
"""订阅接口。库是本机的 city_players.db，前端是 web/dist 里的 Vue 页面。

进程只提供 HTTP。公网和 HTTPS 放在前面的 Caddy 或 Nginx，
反代到这个端口即可，证书不用装进这里。
"""

import json
import os
import re
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import citydb
from .log import get_logger
from .paths import app_dir

log = get_logger()

_DIST = os.path.join(app_dir(), "web", "dist")
_COOKIE = "ts_session"
_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json",
    ".woff2": "font/woff2",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


def _invite(config: dict) -> str:
    return str((config.get("订阅") or {}).get("注册口令") or "").strip()


def _dev_login(config: dict) -> bool:
    return bool((config.get("订阅") or {}).get("测试免注册"))


def _register_open(config: dict) -> bool:
    return bool((config.get("订阅") or {}).get("开放注册"))


def _json(handler, code, obj, cookie=None):
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(body)))
    if cookie is not None:
        handler.send_header("Set-Cookie", cookie)
    handler.end_headers()
    handler.wfile.write(body)


def _cookie_token(handler) -> str:
    raw = handler.headers.get("Cookie") or ""
    for part in raw.split(";"):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        if k.strip() == _COOKIE:
            return v.strip()
    return ""


def _set_cookie(handler, token: str) -> str:
    secure = (handler.headers.get("X-Forwarded-Proto") or "").lower() == "https"
    bits = [f"{_COOKIE}={token}", "HttpOnly", "Path=/", "SameSite=Lax"]
    if secure:
        bits.append("Secure")
    return "; ".join(bits)


def _clear_cookie() -> str:
    return f"{_COOKIE}=; HttpOnly; Path=/; Max-Age=0; SameSite=Lax"


def _read_json(handler):
    n = int(handler.headers.get("Content-Length") or 0)
    if n > 8192:
        raise ValueError("内容太长")
    raw = handler.rfile.read(n) if n else b""
    if not raw:
        return {}
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("需要 JSON 对象")
    return data


def _pair(data):
    try:
        city_id = int(str(data.get("city_id", "")).strip())
    except (TypeError, ValueError):
        raise ValueError("城市 ID 要是数字") from None
    uid = str(data.get("uid", "")).strip()
    if city_id <= 0:
        raise ValueError("城市 ID 要大于 0")
    if not uid.isdigit() or len(uid) > 32:
        raise ValueError("UID 要是数字")
    return city_id, uid


def _account(data):
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", ""))
    if not re.fullmatch(r"[A-Za-z0-9_\u4e00-\u9fff]{2,32}", username):
        raise ValueError("用户名用 2 到 32 位字母、数字或中文")
    if len(password) < 6 or len(password) > 72:
        raise ValueError("密码至少 6 位")
    return username, password


def _user_out(user: dict) -> dict:
    plan = citydb.clear_settings(int(user.get("id") or 0))
    return {
        "id": user["id"],
        "username": user["username"],
        "qq_target": user["qq_target"],
        "expires_at": user.get("expires_at") or "",
        "tier": user.get("tier") or "初级",
        "remote_attack": citydb.attack_tier(user.get("tier") or ""),
        "admin": bool(user.get("admin")),
        "auto_lock": bool(user.get("auto_lock")),
        "hold_min": int(user.get("hold_min") or 0),
        "hold_all": bool(user.get("hold_all")),
        "card_max": int(user.get("card_max") if user.get("card_max") is not None else 100),
        "retreat_mode": user.get("retreat_mode") or "hops",
        "retreat_hops": int(user.get("retreat_hops") or 3),
        "retreat_city": int(user.get("retreat_city") or 0),
        "retreat_fail": bool(user.get("retreat_fail")),
        "lock_cards": int(user.get("lock_cards") if user.get("lock_cards") is not None else 3),
        "clear_mode": plan["mode"],
        "clear_from": plan["page_from"],
        "clear_to": plan["page_to"],
        "clear_wait": plan["wait_min"],
        "clear_scan": plan["scan_sec"],
        "clear_priority": plan["priority"],
        "modo_cards": int(user.get("modo_cards") or 0),
        "region": int(user.get("region") or 0),
    }


def _scan_view(config: dict) -> dict:
    saved = citydb.get_scan_plan()
    stored = saved is not None
    if saved is None:
        w = config.get("页范围监视") or {}
        try:
            gap = int(w.get("间隔秒") or 300)
        except (TypeError, ValueError):
            gap = 300
        ranges = []
        for item in w.get("范围") or []:
            if not isinstance(item, dict):
                continue
            try:
                city = int(item.get("城市") or 0)
                start_page = int(item.get("起始页") or 0)
                end_page = int(item.get("结束页"))
            except (TypeError, ValueError):
                continue
            if city > 0 and start_page >= 0 and end_page >= start_page:
                ranges.append({
                    "city_id": city, "start_page": start_page, "end_page": end_page,
                })
        saved = {
            "gap_sec": gap, "quiet_start": "", "quiet_end": "",
            "quiet": False, "ranges": ranges,
        }
    for row in saved["ranges"]:
        row["city_name"] = citydb.city_name(row["city_id"])
    return {
        "gap_sec": saved["gap_sec"],
        "quiet_start": saved["quiet_start"],
        "quiet_end": saved["quiet_end"],
        "quiet_now": bool(saved["quiet"]),
        "saved": stored,
        "ranges": saved["ranges"],
    }


def _after_attack_submit(config, user_id: int) -> dict:
    """订单已经记在挂着的连接上。暂停中的保活解开，由这条连接接着打。不另开登录。"""
    blocked = citydb.attack_qq_blocked(user_id)
    resumed = False
    if (citydb.attack_paused(user_id) and not blocked
            and citydb.attack_in_keepalive(user_id)):
        citydb.set_attack_paused(False, user_id)
        resumed = True
    return {"login": "busy", "resumed": resumed, "scan": ""}


def _handler(config: dict):
    invite = _invite(config)
    dev_login = _dev_login(config)
    register_open = _register_open(config)

    class H(BaseHTTPRequestHandler):
        def _user(self):
            return citydb.user_by_token(_cookie_token(self))

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/api/meta":
                _json(self, 200, {"dev_login": dev_login, "invite": bool(invite),
                                  "register": register_open})
                return
            if path == "/api/me":
                user = self._user()
                if not user:
                    _json(self, 401, {"error": "请先登录"})
                    return
                _json(self, 200, {"user": _user_out(user), "invite": bool(invite)})
                return
            if path == "/api/cities":
                user = self._user()
                if not user:
                    _json(self, 401, {"error": "请先登录"})
                    return
                try:
                    rows = citydb.list_cities()
                except Exception as exc:
                    _json(self, 500, {"error": str(exc)})
                    return
                _json(self, 200, {"items": [{"id": r[0], "name": r[1]} for r in rows]})
                return
            if path == "/api/subs":
                user = self._user()
                if not user:
                    _json(self, 401, {"error": "请先登录"})
                    return
                try:
                    items = citydb.list_watches(user["id"])
                    counts = citydb.list_watch_counts(user["id"])
                except Exception as exc:
                    _json(self, 500, {"error": str(exc)})
                    return
                _json(self, 200, {"items": items, "counts": counts,
                                  "region": int(user.get("region") or 0)})
                return
            if path == "/api/scan-plan":
                user = self._user()
                if not user:
                    _json(self, 401, {"error": "请先登录"})
                    return
                if not user.get("admin"):
                    _json(self, 403, {"error": "只有管理员能看扫描安排"})
                    return
                _json(self, 200, _scan_view(config))
                return
            if path == "/api/attacks":
                user = self._user()
                if not user:
                    _json(self, 401, {"error": "请先登录"})
                    return
                _json(self, 200, {
                    "items": citydb.list_attack_orders(user["id"]),
                    "process": citydb.attack_status(user["id"]),
                    "storms": citydb.list_storm_rejects(user["id"]),
                })
                return
            if path == "/api/daily":
                user = self._user()
                if not user:
                    _json(self, 401, {"error": "请先登录"})
                    return
                from . import daily
                uin = citydb.attack_qq_of(user["id"])
                switches = citydb.daily_switches(
                    user["id"], (config.get("每日任务") or {}).get("任务") or {})
                _json(self, 200, {
                    "qq": uin,
                    "tasks": daily.task_board(uin, switches),
                    "jobs": citydb.list_daily_jobs(user["id"]),
                    "process": citydb.attack_status(user["id"]),
                    "stages": citydb.campaign_stages(user["id"]),
                    "schedule": citydb.daily_schedule(user["id"]),
                })
                return
            if path == "/api/admin/fighters":
                user = self._user()
                if not user:
                    _json(self, 401, {"error": "请先登录"})
                    return
                if not user.get("admin"):
                    _json(self, 403, {"error": "只有管理员能看全部攻打 QQ"})
                    return
                _json(self, 200, {"fighters": citydb.list_attack_fighters()})
                return
            if path == "/api/attack-qr":
                user = self._user()
                if not user:
                    _json(self, 401, {"error": "请先登录"})
                    return
                from .socket_keepalive import page_attack_qr_path
                fp = page_attack_qr_path(config, user["id"])
                if not os.path.isfile(fp):
                    self.send_error(404)
                    return
                with open(fp, "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if path == "/api/pwd-lab" or path.startswith("/api/pwd-lab/"):
                self._pwd_lab_get(path)
                return
            self._file(path)

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            try:
                data = _read_json(self)
            except (ValueError, json.JSONDecodeError) as exc:
                _json(self, 400, {"error": str(exc) or "格式不对"})
                return
            if path == "/api/register":
                self._register(data)
                return
            if path == "/api/login":
                self._login(data)
                return
            if path == "/api/dev-login":
                if not dev_login:
                    _json(self, 404, {"error": "测试入口已关闭"})
                    return
                token = citydb.open_session("test")
                log.info("测试免注册：已以 test 登录")
                _json(self, 200, {"ok": True, "username": "test"},
                      cookie=_set_cookie(self, token))
                return
            if path == "/api/logout":
                citydb.logout_token(_cookie_token(self))
                _json(self, 200, {"ok": True}, cookie=_clear_cookie())
                return
            user = self._user()
            if not user:
                _json(self, 401, {"error": "请先登录"})
                return
            if path.startswith("/api/pwd-lab"):
                self._pwd_lab_post(path, data)
                return
            try:
                if path == "/api/subs":
                    region = int(user.get("region") or 0)
                    if region and region != 1:
                        raise ValueError("目前只有 1 区支持订阅敌方")
                    city_id, uid = _pair(data)
                    citydb.add_watch(user["id"], city_id, uid)
                    _json(self, 200, {"ok": True})
                elif path == "/api/subs/delete":
                    city_id, uid = _pair(data)
                    citydb.remove_watch(user["id"], city_id, uid)
                    _json(self, 200, {"ok": True})
                elif path == "/api/attacks":
                    if not citydb.attack_tier(user.get("tier") or ""):
                        _json(self, 403, {"error": "远程扫码攻打需要中级或高级订阅"})
                        return
                    try:
                        city_id = int(str(data.get("city_id", "")).strip())
                    except (TypeError, ValueError):
                        raise ValueError("城市 ID 要是数字") from None
                    uid = str(data.get("uid", "")).strip()
                    if city_id <= 0:
                        raise ValueError("城市 ID 要大于 0")
                    if uid and (not uid.isdigit() or len(uid) > 32):
                        raise ValueError("UID 要是数字")
                    minutes = data.get("minutes", None)
                    if minutes is not None and str(minutes).strip() != "":
                        why = citydb.set_attack_hold(user["id"], minutes)
                        if why:
                            raise ValueError(why)
                    cards = data.get("cards", None)
                    card_max = int(user.get("card_max") if user.get("card_max") is not None else 100)
                    if cards is not None and str(cards).strip() != "":
                        why = citydb.set_attack_cards(user["id"], cards)
                        if why:
                            raise ValueError(why)
                        card_max = int(str(cards).strip())
                    why = citydb.add_attack_order(user["id"], city_id, uid, cards=card_max)
                    if why:
                        raise ValueError(why)
                    followed = _after_attack_submit(config, user["id"])
                    hold_min = int(user.get("hold_min") or 0)
                    if minutes is not None and str(minutes).strip() != "":
                        hold_min = int(str(minutes).strip())
                    _json(self, 200, {"ok": True, "login": followed["login"],
                                      "resumed": followed["resumed"], "scan": followed["scan"],
                                      "hold_min": hold_min, "card_max": card_max})
                elif path == "/api/daily/schedule":
                    why = citydb.set_daily_schedule(user["id"], data.get("at"))
                    if why:
                        raise ValueError(why)
                    _json(self, 200, {
                        "ok": True,
                        "schedule": citydb.daily_schedule(user["id"]),
                    })
                elif path == "/api/daily/switch":
                    from . import daily
                    on = data.get("on")
                    if isinstance(on, str):
                        on = on.strip().lower() in ("1", "true", "on", "开")
                    else:
                        on = bool(on)
                    defaults = (config.get("每日任务") or {}).get("任务") or {}
                    why = citydb.set_daily_switch(
                        user["id"], data.get("key"), on,
                        daily.switch_keys(), defaults)
                    if why:
                        raise ValueError(why)
                    uin = citydb.attack_qq_of(user["id"])
                    switches = citydb.daily_switches(user["id"], defaults)
                    _json(self, 200, {
                        "ok": True,
                        "tasks": daily.task_board(uin, switches),
                    })
                elif path == "/api/daily/cancel":
                    try:
                        job_id = int(str(data.get("id", "")).strip())
                    except (TypeError, ValueError):
                        raise ValueError("任务编号不对") from None
                    why = citydb.cancel_daily_job(user["id"], job_id)
                    if why:
                        raise ValueError(why)
                    _json(self, 200, {"ok": True})
                elif path == "/api/daily":
                    if citydb.account_expired(user.get("expires_at") or ""):
                        raise ValueError("账号已过期")
                    why = citydb.enqueue_daily_job(user["id"], data.get("kind"), data)
                    if why:
                        raise ValueError(why)
                    from .socket_keepalive import kick_attack_login
                    login = kick_attack_login(config, user["id"])
                    _json(self, 200, {"ok": True, "login": login})
                elif path == "/api/modo":
                    if citydb.account_expired(user.get("expires_at") or ""):
                        raise ValueError("账号已过期")
                    why = citydb.add_modo_order(user["id"], data.get("cards"))
                    if why:
                        raise ValueError(why)
                    saved = citydb.user_by_token(_cookie_token(self)) or user
                    _json(self, 200, {
                        "ok": True, "login": "busy",
                        "modo_cards": int(saved.get("modo_cards") or 0),
                    })
                elif path == "/api/attacks/cancel":
                    try:
                        order_id = int(str(data.get("id", "")).strip())
                    except (TypeError, ValueError):
                        raise ValueError("订单编号不对") from None
                    why = citydb.cancel_attack_order(user["id"], order_id)
                    if why:
                        raise ValueError(why)
                    _json(self, 200, {"ok": True})
                elif path == "/api/attack-login":
                    from .socket_keepalive import kick_attack_login
                    _json(self, 200, {"ok": True, "login": kick_attack_login(
                        config, user["id"], claim=True)})
                elif path == "/api/attack-move":
                    if not citydb.attack_tier(user.get("tier") or ""):
                        _json(self, 403, {"error": "移动到指定城市需要中级或高级订阅"})
                        return
                    why = citydb.request_attack_move(user["id"], data.get("city_id"))
                    if why:
                        raise ValueError(why)
                    status = citydb.attack_status(user["id"])
                    _json(self, 200, {"ok": True, "move_note": status.get("move_note") or ""})
                elif path == "/api/attack-pause":
                    why = citydb.pause_attack_for(user["id"], bool(data.get("on")))
                    if why:
                        _json(self, 403, {"error": why})
                        return
                    _json(self, 200, {"ok": True, "paused": bool(data.get("on"))})
                elif path == "/api/attack-hold":
                    if not citydb.attack_tier(user.get("tier") or ""):
                        _json(self, 403, {"error": "挂机保活需要中级或高级订阅"})
                        return
                    minutes = data.get("minutes", None)
                    has_minutes = minutes is not None and str(minutes).strip() != ""
                    has_all = "all" in data
                    if not has_minutes and not has_all:
                        raise ValueError("挂机保活要是分钟数")
                    out = {"ok": True}
                    if has_minutes:
                        why = citydb.set_attack_hold(user["id"], minutes)
                        if why:
                            raise ValueError(why)
                        out["hold_min"] = int(str(minutes).strip())
                    if has_all:
                        raw = data.get("all")
                        if isinstance(raw, str):
                            on = raw.strip().lower() in ("1", "true", "on", "开")
                        else:
                            on = bool(raw)
                        why = citydb.set_hold_all(user["id"], on)
                        if why:
                            raise ValueError(why)
                        out["hold_all"] = on
                        if on:
                            from .socket_keepalive import kick_attack_login
                            out["login"] = kick_attack_login(config, user["id"])
                    _json(self, 200, out)
                elif path == "/api/lock-cards":
                    if not citydb.attack_tier(user.get("tier") or ""):
                        _json(self, 403, {"error": "锁敌恢复卡限制需要中级或高级订阅"})
                        return
                    why = citydb.set_lock_cards(user["id"], data.get("cards"))
                    if why:
                        raise ValueError(why)
                    saved = citydb.user_by_token(_cookie_token(self)) or user
                    _json(self, 200, {"ok": True, "user": _user_out(saved)})
                elif path == "/api/clear-plan":
                    if not citydb.attack_tier(user.get("tier") or ""):
                        _json(self, 403, {"error": "清城高级配置需要中级或高级订阅"})
                        return
                    why = citydb.set_clear_plan(
                        user["id"], data.get("mode"), data.get("page_from"),
                        data.get("page_to"), data.get("priority"), data.get("wait_min"),
                        data.get("scan_sec"))
                    if why:
                        raise ValueError(why)
                    saved = citydb.user_by_token(_cookie_token(self)) or user
                    _json(self, 200, {"ok": True, "user": _user_out(saved)})
                elif path == "/api/retreat":
                    if not citydb.attack_tier(user.get("tier") or ""):
                        _json(self, 403, {"error": "打完后退需要中级或高级订阅"})
                        return
                    why = citydb.set_retreat(
                        user["id"], data.get("mode"), data.get("hops"),
                        data.get("city_id"), data.get("on_fail"))
                    if why:
                        raise ValueError(why)
                    saved = citydb.user_by_token(_cookie_token(self)) or user
                    _json(self, 200, {"ok": True, "user": _user_out(saved)})
                elif path == "/api/auto-lock":
                    if not citydb.attack_tier(user.get("tier") or ""):
                        _json(self, 403, {"error": "自动锁敌需要中级或高级订阅"})
                        return
                    on = bool(data.get("on"))
                    citydb.set_auto_lock(user["id"], on)
                    queued = citydb.queue_present_locks(user["id"]) if on else 0
                    login = ""
                    if queued:
                        from .socket_keepalive import kick_attack_login
                        login = kick_attack_login(config, user["id"])
                    _json(self, 200, {"ok": True, "auto_lock": on, "queued": queued,
                                      "login": login})
                elif path == "/api/watch-lock":
                    if not citydb.attack_tier(user.get("tier") or ""):
                        _json(self, 403, {"error": "自动索敌需要中级或高级订阅"})
                        return
                    city_id, uid = _pair(data)
                    why = citydb.queue_watch_lock(user["id"], city_id, uid)
                    if why:
                        raise ValueError(why)
                    from .socket_keepalive import kick_attack_login
                    login = kick_attack_login(config, user["id"])
                    _json(self, 200, {"ok": True, "login": login})
                elif path == "/api/scan-plan":
                    if not user.get("admin"):
                        _json(self, 403, {"error": "只有管理员能改扫描安排"})
                        return
                    why = citydb.save_scan_plan(
                        data.get("gap_sec"), data.get("quiet_start"),
                        data.get("quiet_end"), data.get("ranges"))
                    if why:
                        raise ValueError(why)
                    _json(self, 200, _scan_view(config))
                elif path == "/api/password":
                    why = citydb.change_password(
                        user["id"], data.get("current"), data.get("password"),
                        keep_token=_cookie_token(self))
                    if why:
                        raise ValueError(why)
                    _json(self, 200, {"ok": True})
                elif path == "/api/push":
                    qq_target = str(data.get("qq_target", "")).strip()
                    if qq_target and (not qq_target.isdigit() or not 5 <= len(qq_target) <= 12):
                        raise ValueError("QQ 号要是 5 到 12 位数字")
                    citydb.save_push(user["id"], qq_target)
                    _json(self, 200, {"ok": True})
                else:
                    self.send_error(404)
            except ValueError as exc:
                _json(self, 400, {"error": str(exc)})
            except Exception as exc:
                _json(self, 500, {"error": str(exc)})

        def _register(self, data):
            if not register_open:
                _json(self, 403, {"error": "注册已关闭"})
                return
            try:
                username, password = _account(data)
            except ValueError as exc:
                _json(self, 400, {"error": str(exc)})
                return
            if invite and not secrets.compare_digest(str(data.get("invite", "")), invite):
                _json(self, 403, {"error": "注册口令不对"})
                return
            uid = citydb.create_user(username, password)
            if uid is None:
                _json(self, 409, {"error": "这个用户名已经有了"})
                return
            token = citydb.login_user(username, password)
            _json(self, 200, {"ok": True}, cookie=_set_cookie(self, token))

        def _login(self, data):
            try:
                username, password = _account(data)
            except ValueError as exc:
                _json(self, 400, {"error": str(exc)})
                return
            token = citydb.login_user(username, password)
            if token is False:
                _json(self, 403, {"error": "账号已过期"})
                return
            if not token:
                _json(self, 401, {"error": "用户名或密码不对"})
                return
            _json(self, 200, {"ok": True}, cookie=_set_cookie(self, token))

        def _pwd_lab_user(self):
            user = self._user()
            if not user:
                _json(self, 401, {"error": "请先登录"})
                return None
            if not user.get("admin"):
                _json(self, 403, {"error": "只有管理员能用密码登录测试页"})
                return None
            return user

        def _pwd_lab_get(self, path):
            from . import pwd_lab
            user = self._pwd_lab_user()
            if not user:
                return
            try:
                if path == "/api/pwd-lab":
                    _json(self, 200, pwd_lab.snapshot(user["id"]))
                    return
                if path == "/api/pwd-lab/accounts":
                    _json(self, 200, {"items": pwd_lab.account_tickets()})
                    return
            except Exception as exc:
                _json(self, 500, {"error": str(exc)})
                return
            self.send_error(404)

        def _pwd_lab_post(self, path, data):
            from . import pwd_lab
            user = self._pwd_lab_user()
            if not user:
                return
            try:
                if path == "/api/pwd-lab/login":
                    _json(self, 200, pwd_lab.login(user["id"], data))
                    return
                if path == "/api/pwd-lab/check":
                    _json(self, 200, pwd_lab.check(user["id"]))
                    return
                if path == "/api/pwd-lab/clear":
                    _json(self, 200, pwd_lab.clear(user["id"]))
                    return
            except Exception as exc:
                _json(self, 500, {"error": str(exc)})
                return
            self.send_error(404)

        def _file(self, path):
            if path == "/":
                path = "/index.html"
            elif path in ("/admin", "/admin/"):
                path = "/admin.html"
            elif path in ("/pwd-lab", "/pwd-lab/", "/qr-lab", "/qr-lab/"):
                path = "/pwd-lab.html"
            rel = os.path.normpath(path.lstrip("/"))
            if rel.startswith(".."):
                self.send_error(404)
                return
            full = os.path.join(_DIST, rel)
            if not os.path.isfile(full):
                if path.startswith("/admin") or path.startswith("/pwd-lab") or path.startswith("/qr-lab"):
                    self.send_error(404)
                    return
                full = os.path.join(_DIST, "index.html")
                if not os.path.isfile(full):
                    body = ("前端还没构建。在仓库根目录执行 bash update_tankstorm.sh"
                            ).encode("utf-8")
                    self.send_response(503)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
            ext = os.path.splitext(full)[1].lower()
            with open(full, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", _TYPES.get(ext, "application/octet-stream"))
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            return

    return H


def _announce(host, port, config):
    log.info("订阅接口 http://%s:%d/    库 %s", host, port, citydb.DB_FILE)
    log.info("本进程只提供 HTTP。公网 HTTPS 用 Caddy 或 Nginx 反代到 %s:%d", host, port)
    if _register_open(config) and host not in ("127.0.0.1", "localhost") and not _invite(config):
        log.warning("注册口令是空的，公网上任何人都能注册。填 config「订阅.注册口令」")
    if not _register_open(config):
        log.info("注册已关闭。添加账号：python3 web.py --add-user 用户名 --password 密码 --expires 2026-12-31 --tier 中级")
        log.info("改订阅档：python3 web.py --set-tier 用户名 初级|中级|高级。中级和高级可提交远程扫码攻打")
        log.info("扫描安排：python3 web.py --set-admin 用户名 开。该账号登录后打开 /admin")
    log.info("密码登录测试页：/pwd-lab（只要管理员。独立票据，不绑攻打号）")
    if _dev_login(config):
        log.warning("测试免注册已打开：POST /api/dev-login 会直接以 test 登录。正式对外前关掉「订阅.测试免注册」")

    def warm():
        try:
            citydb.ensure_catalog()
        except Exception as exc:
            log.info("城市目录暂不可用：%s", exc)

    threading.Thread(target=warm, name="city-catalog", daemon=True).start()


def _server(host, port, config):
    class _HTTP(ThreadingHTTPServer):
        allow_reuse_address = True

    httpd = _HTTP((host, int(port)), _handler(config or {}))
    httpd.daemon_threads = True
    return httpd


def start(host="0.0.0.0", port=8765, config=None):
    """给保活进程挂一个后台接口。进程退出时一起停。"""
    httpd = _server(host, port, config)
    threading.Thread(target=httpd.serve_forever, name="city-web",
                     daemon=True).start()
    _announce(host, port, config or {})
    return httpd


def _wake_attack_orders(config) -> None:
    """主进程：每个已绑定的攻打 QQ 各看一条线程。有日常、还在挂机、开了全天候，或有一单正在打、线程不在时拉起。"""
    from .socket_keepalive import attack_worker_alive, kick_attack_login

    gap = threading.Event()
    noted = set()
    while not gap.wait(5):
        try:
            citydb.enqueue_due_daily_jobs()
            users = citydb.users_needing_attack()
        except Exception:
            log.info("自动拉起攻打没成", exc_info=True)
            continue
        for user_id in users:
            try:
                uin = citydb.attack_qq_of(user_id)
                if not uin:
                    if user_id not in noted:
                        log.info("登录账号 %s 还有订单，攻打 QQ 还没绑到这个人，不自动扫码",
                                 citydb.username_of(user_id))
                        noted.add(user_id)
                    continue
                # 进程刚停时，库里的心跳可能还新鲜。线程不在就要拉起，不能当成还在跑。
                citydb.attack_status(user_id)
                if attack_worker_alive(uin):
                    noted.discard(user_id)
                    continue
                if citydb.unbound_login_waiting():
                    continue
                login = kick_attack_login(config, user_id, claim=False)
            except SystemExit:
                log.error("攻打号配置有误，网页不再自动拉起")
                return
            except Exception:
                log.info("自动拉起攻打没成", exc_info=True)
                continue
            if login in ("taken", "no_account", "unbound"):
                if user_id not in noted:
                    if login == "taken":
                        log.error("登录账号 %s 的订单不能用别人的攻打号",
                                  citydb.username_of(user_id))
                    elif login == "unbound":
                        log.error("登录账号 %s 还没绑定攻打号",
                                  citydb.username_of(user_id))
                    else:
                        log.error("登录账号 %s 有攻打订单，但没有可绑定的攻打号",
                                  citydb.username_of(user_id))
                noted.add(user_id)
                continue
            noted.discard(user_id)
            if login in ("started", "qr"):
                log.info("攻打进程不在，已按订单拉起")


def serve(host="0.0.0.0", port=8765, config=None) -> int:
    httpd = _server(host, port, config)
    _announce(host, port, config or {})
    threading.Thread(
        target=_wake_attack_orders, args=(config or {},),
        name="attack-wake", daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("订阅接口已停止")
    finally:
        httpd.server_close()
    return 0
