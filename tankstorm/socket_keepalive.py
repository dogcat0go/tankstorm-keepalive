# tankstorm-keepalive  Copyright (C) 2026 Dimlitter
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# 本程序是自由软件：你可以依据自由软件基金会发布的 GNU Affero 通用公共许可证
# （第 3 版，或你选择的任何更新版本）之条款，再分发和/或修改它。
# 本程序希望能有用，但不提供任何担保；甚至不含适销性或特定用途适用性的默示担保。
# 详见随附的 LICENSE 文件，或 <https://www.gnu.org/licenses/>。
"""保持在线守护进程：连上游戏 socket、登录、定时发心跳，掉线自动重连。

解决的问题：坦克风暴长时间无操作会弹"指挥官，您是否在线"并把你踢下线，导致基地
被打。真正维持在线的是 Flash 客户端与 tankstorm-proxy.sincetimes.com:8001 的 socket
连接 + 定时心跳。本守护进程用纯 Python socket 复刻这条连接。

工作流程：
  1. get_game_context() 拿最新 openid/openkey/uid/sid/secret/server/port（每次连接前刷新，
     因为 openkey 会过期）；
  2. 连 TCP 到 server:port，发登录握手（protocol.build_login）；
  3. 每 interval 秒发一次心跳（protocol.build_heartbeat）；
  4. 读服务器数据，若命中"是否在线"探测包则回应；
  5. 连接断开 → 退避重连（并刷新 openkey）。

协议细节（登录/心跳字节）来自 protocol.json，由 Wireshark 抓包分析生成；
没有该文件时本模块会给出清晰提示并退出，不会瞎跑。
"""

import re
import select
import signal
import socket
import sqlite3
import threading
import time
from contextlib import contextmanager

from . import daily, notify, protocol, sender
from .log import get_logger, task_log
from .qzone import get_game_context
from .recorder import Recorder

log = get_logger()

# 游戏连接活着时，网页票据可能一直没人碰。闲置大约一个多小时后腾讯会不认，
# 版本更新一重启就要扫码。这里每隔一刻钟再打开一次游戏页。
LOGIN_TOUCH_SEC = 15 * 60
# 页面停在登录中时，这条线程最多再等这么久。到点关掉，等人再点登录。
LOGIN_WAIT_SEC = 10 * 60
# 被超级强攻踢下线后，隔这么久再登录一次。登录上再把被打断的任务拉起来。
STORM_RETRY_SEC = 30
# 从被打断、游戏还没再登上起算。超过这段时间还没登上，就停止重复登录。
STORM_RETRY_LIMIT_SEC = 30 * 60
_storm_local = threading.local()
_login_limits = {}
_login_gen = {}
_login_limits_guard = threading.Lock()
_login_sessions = []
_login_sessions_guard = threading.Lock()
_login_sig_installed = False


def _limit_key() -> str:
    from . import citydb

    uin = str(citydb.attack_context_qq() or "").strip()
    if uin.isdigit():
        return uin
    uid = int(citydb.attack_context_user() or 0)
    return f"user:{uid}" if uid else ""


def _current_limit(key: str = "") -> float:
    key = str(key or "").strip() or _limit_key()
    if not key:
        return 0.0
    with _login_limits_guard:
        return float(_login_limits.get(key) or 0)


def _arm_login_limit() -> None:
    """从第一次进入登录等待起算 10 分钟。已经在计时就不动。"""
    key = _limit_key()
    if not key:
        return
    with _login_limits_guard:
        if not _login_limits.get(key):
            _login_limits[key] = time.monotonic() + LOGIN_WAIT_SEC


def _reset_login_limit(key: str) -> None:
    """再点一次登录，从现在重新给 10 分钟。"""
    key = str(key or "").strip()
    if not key:
        return
    with _login_limits_guard:
        _login_limits[key] = time.monotonic() + LOGIN_WAIT_SEC
        _login_gen[key] = _login_gen.get(key, 0) + 1


def _clear_login_limit() -> None:
    key = _limit_key()
    if not key:
        return
    with _login_limits_guard:
        _login_limits.pop(key, None)


def _drop_expired_limit() -> None:
    """上次线程没清掉的过期计时不能把这次登录立刻判死。"""
    key = _limit_key()
    if not key:
        return
    with _login_limits_guard:
        at = float(_login_limits.get(key) or 0)
        if at and time.monotonic() >= at:
            _login_limits.pop(key, None)


def _login_limit_hit(key: str = "") -> bool:
    at = _current_limit(key)
    return at > 0 and time.monotonic() >= at


def _login_until_wall(key: str = "") -> float:
    """给扫码用的墙钟截止。没设上限时返回 0。"""
    at = _current_limit(key)
    if at <= 0:
        return 0.0
    return time.time() + max(0.0, at - time.monotonic())


def _abandon_login(key: str, user_id: int = 0) -> bool:
    """这一轮登录到点才记过期。中途又点了登录就不写，返回 False。"""
    from . import citydb

    key = str(key or "").strip()
    with _login_limits_guard:
        at = float(_login_limits.get(key) or 0) if key else 0.0
        if not key or not at or time.monotonic() < at:
            return False
        gen = _login_gen.get(key, 0)
        _login_limits.pop(key, None)
    try:
        citydb.mark_login_expired(user_id)
    except Exception:
        log.info("登录超时标记没写上", exc_info=True)
        return False
    with _login_limits_guard:
        if key and _login_gen.get(key, 0) != gen:
            try:
                citydb.clear_login_expired(user_id)
            except Exception:
                log.info("登录超时标记没撤掉", exc_info=True)
            return False
    return True


_storm_guard = threading.Lock()
_storm_dropped_ids = set()


def _mark_storm_drop(ident=None) -> None:
    """记下这次是被超级强攻打断的。别的线程打断时，记到那条连接所在的线程上。"""
    ident = threading.get_ident() if ident is None else int(ident)
    if ident == threading.get_ident():
        _storm_local.dropped = True
        return
    with _storm_guard:
        _storm_dropped_ids.add(ident)


def _storm_interrupted() -> bool:
    if getattr(_storm_local, "dropped", False):
        return True
    with _storm_guard:
        return threading.get_ident() in _storm_dropped_ids


def _take_storm_drop() -> bool:
    dropped = _storm_interrupted()
    _storm_local.dropped = False
    with _storm_guard:
        _storm_dropped_ids.discard(threading.get_ident())
    return dropped


def _mark_game_online(on: bool) -> None:
    _storm_local.online = bool(on)


def _game_online() -> bool:
    return bool(getattr(_storm_local, "online", False))


def _storm_retry_decision(dropped, retrying, online, started, now):
    """返回 (继续重登, 起始时刻, 已超时)。

    从被超级打断、游戏还没再登上的那一刻起算。登上且这次没再被打断时，起始时刻归零。
    刚登上又被打下去，重新计 30 分钟。
    """
    dropped = bool(dropped)
    online = bool(online)
    started = float(started or 0)
    now = float(now)
    if online and not dropped:
        return False, 0.0, False
    if not (dropped or (retrying and not online)):
        return False, 0.0, False
    if (dropped and online) or started <= 0:
        started = now
    if now - started >= STORM_RETRY_LIMIT_SEC:
        return False, started, True
    return True, started, False


def _storm_retry_expired(started, now=None) -> bool:
    """已经在重登，并且从起始时刻起过了 30 分钟。"""
    started = float(started or 0)
    if started <= 0:
        return False
    if now is None:
        now = time.monotonic()
    return float(now) - started >= STORM_RETRY_LIMIT_SEC


def _stop_storm_relogin(uin: str = "") -> None:
    """30 分钟还没登上。自动拉起不再开线程，等人再点登录。"""
    from . import citydb

    ids = [int(i) for i in citydb.attack_context_users() if int(i or 0)]
    if not ids:
        uid = int(citydb.attack_context_user() or 0)
        if uid:
            ids = [uid]
    for uid in ids:
        citydb.mark_login_expired(uid, "storm")
    minutes = STORM_RETRY_LIMIT_SEC // 60
    if uin:
        log.info("被超级强攻打断后 %d 分钟还没登上，停止重复登录（攻打 QQ %s）",
                 minutes, uin)
    else:
        log.info("被超级强攻打断后 %d 分钟还没登上，停止重复登录", minutes)


def _give_up_login(uin: str = "") -> bool:
    key = str(uin or "").strip() or _limit_key()
    if not _abandon_login(key):
        return False
    if uin:
        log.info("登录超过 10 分钟还没完成，关掉攻打 QQ %s 的线程", uin)
    else:
        log.info("登录超过 10 分钟还没完成，关掉这次登录")
    return True


def install_login_refresh() -> None:
    """主线程接住 SIGTERM，退出前再刷新一次网页票据。systemctl stop 走的是这个信号。"""
    global _login_sig_installed
    if _login_sig_installed or threading.current_thread() is not threading.main_thread():
        return
    _login_sig_installed = True
    previous = signal.getsignal(signal.SIGTERM)

    def _on_term(signum, frame):
        refresh_login_sessions()
        if callable(previous) and previous not in (signal.SIG_DFL, signal.SIG_IGN):
            previous(signum, frame)
            return
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _on_term)


def bind_login_session(qq) -> None:
    """登记这个会话。网页票据由会话脉冲去续，进程退出时再刷一次。"""
    install_login_refresh()
    with _login_sessions_guard:
        if qq not in _login_sessions:
            _login_sessions.append(qq)


def unbind_login_session(qq) -> None:
    """这条会话不再用了，就从退出刷新名单里拿掉。"""
    stop = getattr(qq, "_touch_stop", None)
    if stop is not None:
        stop.set()
    with _login_sessions_guard:
        try:
            _login_sessions.remove(qq)
        except ValueError:
            pass


def refresh_login_sessions() -> None:
    """进程要退出时再写一次 cookie，让拉起来的新进程还能用这张票。"""
    with _login_sessions_guard:
        sessions = list(_login_sessions)
    for qq in sessions:
        try:
            qq.touch_login(block=False)
        except Exception:
            log.info("退出前刷新网页票据没做成", exc_info=True)


def _arm_super_storm(rec, sock, config, qq=None) -> None:
    """这条连接收到超级强攻就拒绝。拒绝发出去之后，记到对应登录账号的页面上。

    登录账号默认不拒绝。管理员填了大于 0 的每日上限，并且今天还没到这个数，才发拒绝包。
    同一个攻打 QQ 上有账号把上限填成 0 时，这条连接不再自动拒绝。
    次数按北京时间每天 0 点重新计算。没填上限也不拒绝。
    """
    if not rec or not getattr(rec, "auto_reject", False):
        return
    uin = str(getattr(qq, "uin", "") or "")
    from . import citydb
    bound_user = citydb.attack_context_user()

    def _uin_now() -> str:
        live = ""
        attack_uin = getattr(qq, "_attack_uin", None)
        if callable(attack_uin):
            try:
                live = str(attack_uin() or "").strip()
            except Exception:
                live = ""
        if not live.isdigit():
            live = str(getattr(qq, "uin", "") or uin or "").strip()
        return live

    def _abort_reject(user_id, day, session, warning=""):
        citydb.release_storm_reject(user_id, day)
        if warning:
            log.warning("%s", warning)
        _mark_storm_drop(session)
        log.info("拒绝包没发出去，断开当前连接，稍后重新登录")
        _force_close_socket(sock)

    def _on_super_storm(data):
        """能拒绝就在这条连接上拒绝，不重启。发不了返回 False，外层再断开重登。

        额度先在收包线程里占上，这样决定留不留连接时次数已经扣了。
        拒绝包本身仍丢到旁边的线程发，避免和收包锁死等。
        """
        snapshot = dict(data or {})
        who = str(snapshot.get("atkName") or "").strip()
        if not who:
            who = str(snapshot.get("atkUid") or "").strip()
        uin_now = _uin_now()
        user_id = citydb.storm_subject(bound_user, uin_now)
        day = citydb.beijing_day()
        why = citydb.reserve_storm_reject(user_id, day, uin_now)
        if why:
            log.info("%s（进攻方 %s）", why, who or "未知玩家")
            return False
        session = threading.get_ident()

        def _send():
            rc4 = rec.rc4_c2s
            if rc4 is None:
                _abort_reject(
                    user_id, day, session,
                    "自动拒绝超级强攻失败：RC4 C→S 实例不可用"
                    "（实时解密未启用或密钥自检失败）")
                return
            if not sender.send_reject_super_storm(sock, rc4, snapshot):
                _abort_reject(user_id, day, session)
                return
            try:
                citydb.note_storm_reject(who, user_id=user_id, qq=uin_now)
            except Exception:
                log.info("超级强攻拒绝没记上页面", exc_info=True)
            log.info("已拒绝超级强攻（进攻方 %s），这条连接不重启", who or "未知玩家")
            notify.send(config, "🛡️ 坦克风暴：已自动拒绝超级强攻",
                        f"进攻方：{snapshot.get('atkName', '?')}（{snapshot.get('atkUid', '?')}）\n"
                        f"防守方：{snapshot.get('deftName', '?')}（{snapshot.get('deftUid', '?')}）\n\n"
                        f"已自动发送 RceSuperStormOpt type=2 拒绝包。\n"
                        f"如果服务端要求验证码才接受拒绝，此包可能被忽略，"
                        f"请立刻打开游戏确认。")

        threading.Thread(target=_send, name="storm-reject", daemon=True).start()
        return True

    rec.on_super_storm = _on_super_storm
    log.info("超级强攻自动拒绝已就绪")


def _arm_storm_relogin(rec, sock) -> None:
    """这次没发出拒绝包时才断开。外层隔 30 秒再登录，并把任务拉起来。"""
    if not rec:
        return

    def _drop(_data):
        if _storm_interrupted():
            return
        _mark_storm_drop()
        log.info("被超级强攻，断开当前连接，稍后重新登录并继续任务")
        _force_close_socket(sock)

    rec.on_storm_drop = _drop


def _connect(host: str, port: int, timeout: float = 15) -> socket.socket:
    sock = socket.create_connection((host, port), timeout=timeout)
    sock.settimeout(timeout)
    return sock


def _http_warmup(qq, ctx: dict) -> None:
    """连 socket 前，复刻浏览器的 HTTP 调用（loadIdInfo.war），让服务器完成会话注册。
    失败不影响后续 socket 连接，仅记录调试日志。"""
    canvas = str(ctx.get("canvas_url") or "")
    host = canvas.split("/")[2] if canvas.startswith("http") else ""
    if ctx.get("coinserver"):
        base = ctx.get("coinserver")
    elif host:
        base = "https://" + host + "/"
    elif getattr(qq, "platform", None) == "大厅":
        base = "https://tankstorm-qqgame.sincetimes.com/"
    else:
        base = "https://tankstorm-qzone.sincetimes.com/"
    params = {"openid": ctx.get("openid", ""), "openkey": ctx.get("openkey", ""),
              "uid": ctx.get("uid", ""),
              "pf": ctx.get("pf") or ("qqgame" if getattr(qq, "platform", None) == "大厅" else "qzone")}
    if not params["openid"]:
        return
    try:
        r = qq.session.get(base.rstrip("/") + "/loadIdInfo.war", params=params,
                           headers={"Referer": ctx.get("canvas_url", "")}, timeout=15)
        log.debug("warmup loadIdInfo.war -> %s %r", r.status_code, r.text[:40])
    except Exception as exc:
        log.debug("warmup 失败(忽略): %s", exc)


def _remember_region(qq, ctx) -> None:
    """攻打号才记。区服在游戏链接的 region 里，扫描号不写到用户上。
    同一个 QQ 绑了几个登录账号，区服都写上。"""
    if not getattr(qq, "attack_account", False):
        return
    from . import citydb
    uin = str(getattr(qq, "uin", "") or "").strip()
    ids = citydb.users_bound_to_qq(uin) if uin.isdigit() else []
    user_id = citydb.attack_context_user() or citydb.login_for()
    if not user_id and not ids:
        user_id, _name = citydb.attack_qq_owner(uin)
    if user_id and user_id not in ids:
        ids = [user_id] + ids
    for uid in ids:
        citydb.note_game_region(uid, ctx.get("region"))


_region_guard = threading.Lock()
_region_inflight = set()
_region_tried = {}


def schedule_region_from_login(user_id: int) -> None:
    """区服还是 0 时，用这个登录账号已经保存的攻打票据打开游戏页认一次。

    保活连着的时候不会为了认区服重连。网页轮询只负责排队，认区服在旁边做。
    """
    from . import citydb

    user_id = int(user_id or 0)
    if user_id <= 0 or citydb.user_region(user_id):
        return
    now = time.monotonic()
    with _region_guard:
        if user_id in _region_inflight:
            return
        if now - _region_tried.get(user_id, 0) < 120:
            return
        _region_inflight.add(user_id)
        _region_tried[user_id] = now
    threading.Thread(
        target=_region_from_login, args=(user_id,),
        name=f"region-{user_id}", daemon=True).start()


def _region_from_login(user_id: int) -> None:
    import json
    import os

    from . import citydb
    from .qq_login import QQSession

    try:
        uin = citydb.attack_qq_of(user_id)
        if not uin.isdigit():
            log.info("登录账号 %s 还没绑定攻打 QQ，认不出区服", citydb.username_of(user_id))
            return
        path = attack_cookie_file(uin)
        if not os.path.isfile(path):
            log.info("登录账号 %s 的攻打 QQ %s 还没有票据，认不出区服",
                     citydb.username_of(user_id), uin)
            return
        try:
            with open(path, encoding="utf-8") as f:
                jar = json.load(f)
        except (OSError, ValueError):
            log.info("登录账号 %s 的攻打票据读不出来，认不出区服", citydb.username_of(user_id))
            return
        if not any(isinstance(c, dict) and c.get("name") == "skey" and c.get("value")
                   for c in jar):
            log.info("登录账号 %s 的攻打票据里没有登录态，认不出区服", citydb.username_of(user_id))
            return
        qq = QQSession(path)
        qq.attack_account = True
        qq.use_napcat = False
        ctx = get_game_context(qq)
        region = ctx.get("region")
        citydb.note_game_region(user_id, region)
        if not str(region or "").strip():
            log.info("登录账号 %s 的游戏页里没有区服", citydb.username_of(user_id))
    except Exception:
        log.info("从已登录的攻打号认区服没成", exc_info=True)
    finally:
        with _region_guard:
            _region_inflight.discard(user_id)


def _one_session(qq, spec: dict, conf: dict, config: dict, rec=None,
                 with_daily: bool = False) -> str:
    """跑一次完整连接，直到断开。返回断开原因（字符串）。"""
    _mark_game_online(False)
    ctx = get_game_context(qq)
    _remember_region(qq, ctx)
    host = ctx.get("server") or spec.get("default_host", "tankstorm-proxy.sincetimes.com")
    port = int(ctx.get("port") or spec.get("default_port", 8001))
    if not ctx.get("openkey"):
        return "未取得 openkey（登录态可能失效）"

    if rec:
        # 告诉录制器"我是谁"，用于过滤广播里与我无关的关键词误报。
        # 守护进程要长跑，这里不能因为取标识失败就掀翻整个连接。
        try:
            rec.set_identity(ctx.get("uid"), ctx.get("openid"), getattr(qq, "uin", None))
        except Exception as exc:
            log.debug("设置录制标识失败(忽略): %s", exc)
        rec.login_pf = ctx.get("pf") or (
            "qqgame" if getattr(qq, "platform", None) == "大厅" else "qzone")

    if spec.get("http_warmup", True):
        _http_warmup(qq, ctx)

    interval = float(conf.get("心跳间隔秒") or protocol.heartbeat_interval(spec))
    log.info("连接游戏服务器 %s:%d …", host, port)
    try:
        sock = _connect(host, port)
    except OSError as exc:
        return f"连接失败: {exc}"
    if rec:
        rec.on_connect()   # 开始登录静默窗口：这段时间的新 opcode 只学不报
        # 必须在发出第一个字节之前包上：这样客户端上行的包也会被录制，
        # 而不是像以前那样只记服务器下行。返回值当普通 socket 用即可。
        sock = rec.wrap(sock, host=host, port=port,
                        uid=ctx.get("uid"), sid=ctx.get("sid"))
        # 密钥三要素 uid/sid/level/firstLogin 都在 FlashVars 里，connect 时就齐了。
        # 必须赶在第一条非豁免消息到达之前开，RC4 密钥流从那一条开始累积。
        rec.enable_crypto(ctx)

    sock = _LockedSock(sock)

    _arm_storm_relogin(rec, sock)
    _mark_game_online(False)
    _arm_super_storm(rec, sock, config, qq)

    heart = None
    restore_beat = None
    try:
        steps = protocol.build_login_sequence(spec, ctx)
        for i, (data, delay) in enumerate(steps, 1):
            sock.sendall(data)
            log.debug("登录步骤 %d/%d 已发送（%d 字节）", i, len(steps), len(data))
            if delay:
                time.sleep(delay)
        log.info("已完成登录握手（%d 步），uid=%s secret=%s",
                 len(steps), ctx.get("uid"), ctx.get("secret"))
        _mark_game_online(True)
        if rec:
            # sid 是加密载荷分析时的头号候选密钥材料，记进会话元信息
            rec.note("login_ok", sid=ctx.get("sid"), uid=ctx.get("uid"))

        # 每日任务默认**不在这里跑**。保活和每日任务是两件事：
        #   · 保活是常驻的、唯一的职责就是别掉线，必须尽可能不出错
        #   · 每日任务是一次性的批处理，跑完就该结束
        # 混在一起的坏处是每次重连都会重跑一轮任务，而且任务出问题会牵连保活。
        # 需要"连上顺便领一轮"时显式传 with_daily=True（或 --keepalive --daily）。
        # 心跳包提前构造好：任务执行期间也要发，不能等进了心跳循环才开始
        hb = protocol.build_heartbeat(spec, ctx)
        heart = _open_heart(sock, hb, interval)
        restore_beat = daily.bind_beat(heart)

        if with_daily:
            try:
                res, det = daily.run(rec, sock, config, beat=heart)
                log.info("任务执行期间共发心跳 %d 次", heart.count)
                _push_daily_summary(config, res, det)
            except Exception as exc:
                log.error("每日任务执行异常（不影响保活）: %s", exc)

        cities, watch_gap = _watch_city_ids(config)
        last_watch = 0.0
        shown = tuple(cities)
        page_mode = bool((config.get("页范围监视") or {}).get("启用"))
        page_jobs, page_gap, quiet, quiet_start, quiet_end = _scan_plan(config)
        shown_pages = tuple(page_jobs)
        last_pages = 0.0
        quiet_logged = False
        if cities:
            log.info("城市监视：每 %.0f 秒刷新 %d 座城 %s",
                     watch_gap, len(cities), ",".join(str(c) for c in cities))
        elif (config.get("城市监视") or {}).get("启用"):
            log.info("城市监视已启用。config 里的城市是空的，"
                     "在订阅页面加上城市和 UID 之后会开始刷新")
        if page_mode and page_jobs:
            brief = "、".join(f"{c} 第{a}–{b}页" for c, a, b in page_jobs)
            log.info("页范围监视：每 %.0f 秒刷新 %s", page_gap, brief)
        elif page_mode:
            log.info("页范围监视已启用，名单还是空的。在订阅页面的扫描安排里加上城市")
        if quiet:
            log.info("[扫描] 现在是停扫时段 %s–%s（北京时间）", quiet_start, quiet_end)

        if page_mode or (config.get("城市监视") or {}).get("启用"):
            if _await_role(sock, rec, spec, ctx, config):
                log.info("登录态数据接收完毕，开始翻城")
            else:
                got = "、".join(getattr(rec, "latest", {}) or {}) or "无"
                log.error("登录后没收到角色数据，读不到国家ID（已有回包：%s）。"
                          "这一轮先不翻。先停掉其它游戏窗口再试，多半是号被另一路占着",
                          got)
                last_pages = time.time()
                last_watch = time.time()

        sock.settimeout(1.0)
        while True:
            now = time.time()
            city_on = bool((config.get("城市监视") or {}).get("启用"))
            page_due = page_mode and now - last_pages >= page_gap
            city_due = city_on and now - last_watch >= watch_gap
            if page_due or city_due:
                page_jobs, page_gap, quiet, quiet_start, quiet_end = _scan_plan(config)
                if quiet:
                    if not quiet_logged:
                        log.info("[扫描] 停扫时段 %s–%s（北京时间），这一轮不翻页",
                                 quiet_start, quiet_end)
                        quiet_logged = True
                    if page_due:
                        last_pages = now
                    if city_due:
                        last_watch = now
                    page_due = False
                    city_due = False
                else:
                    quiet_logged = False
            if page_due:
                if tuple(page_jobs) != shown_pages:
                    shown_pages = tuple(page_jobs)
                    brief = "、".join(f"{c} 第{a}–{b}页" for c, a, b in page_jobs) or "(空)"
                    log.info("页范围监视：每 %.0f 秒刷新 %s", page_gap, brief)
                last_pages = now
                if page_jobs:
                    try:
                        _scan_page_ranges(rec, sock, config, page_jobs, heart)
                    except OSError as exc:
                        return (f"页范围监视时连接中断: {exc}"
                                f"（已发 {heart.count} 次心跳）")
                    except Exception as exc:
                        log.error("页范围监视异常（保活继续）: %s", exc)
                    sock.settimeout(1.0)
                continue
            if city_due:
                cities, watch_gap = _watch_city_ids(config)
                last_watch = now
                if tuple(cities) != shown:
                    shown = tuple(cities)
                    log.info("城市监视名单：%s",
                             "、".join(str(c) for c in cities) or "(空)")
                if cities:
                    try:
                        _watch_cities_round(rec, sock, config, cities, heart)
                    except OSError as exc:
                        return (f"城市监视时连接中断: {exc}"
                                f"（已发 {heart.count} 次心跳）")
                    except Exception as exc:
                        log.error("城市监视异常（保活继续）: %s", exc)
                    sock.settimeout(1.0)
                    continue
            try:
                data = sock.recv(8192)
            except socket.timeout:
                continue
            if not data:
                return f"服务器关闭连接（已发 {heart.count} 次心跳）"
            reply = protocol.maybe_online_reply(spec, data, ctx)
            if reply:
                sock.sendall(reply)
                log.info("收到在线探测，已回应")
    except OSError as exc:
        return f"连接中断: {exc}"
    finally:
        if restore_beat is not None:
            restore_beat()
        if heart is not None:
            heart.stop()
        try:
            sock.close()
        except OSError:
            pass


def _summary_ok(text: str) -> bool:
    """这一行算做成了。领过、今天已经做过，都不是失败。"""
    text = str(text or "")
    return (text.startswith("成功") or "今日已领" in text
            or "今日已执行" in text or "已做过" in text)


def _push_daily_summary(config: dict, results: dict, details: dict) -> None:
    """把每日任务成果推到 PushPlus，并在日志里打一份对照表。

    只有真正执行过的才推 —— 全是"未开启/冷却中"的轮次不值得打扰。
    命令行只开了一项时，汇总里只留这一项，不把没开的月卡和暂不执行算成失败。
    """
    switches = ((config.get("每日任务") or {}).get("任务") or {})
    enabled = [k for k, on in switches.items()
               if on is True and isinstance(k, str) and not k.startswith("_")]
    focus = enabled[0] if len(enabled) == 1 else None
    quiet = ("未开启", "冷却中", "未实测", "跳过", "暂不执行")
    acted = {}
    for k, v in results.items():
        if not v:
            continue
        if focus is not None:
            if k == focus:
                acted[k] = v
            continue
        if any(s in v for s in quiet):
            continue
        acted[k] = v
    if not acted:
        return

    okn = sum(1 for v in acted.values() if _summary_ok(v))
    bad = len(acted) - okn

    log.info("―― 每日任务成果 ―― 成功 %d，未成 %d", okn, bad)
    rows = []
    for k, v in acted.items():
        icon = "✅" if _summary_ok(v) else "❌"
        log.info("  %s %-12s %s", icon, k, v)
        extra = ""
        d = details.get(k)
        if isinstance(d, dict):
            kv = [f"{a}={d[a]}" for a in daily.REWARD_HINT if a in d]
            if kv:
                extra = f"<br><span style='color:#888'>{' '.join(kv)}</span>"
        rows.append(f"<tr><td>{icon}</td><td><b>{k}</b></td>"
                    f"<td>{v}{extra}</td></tr>")

    title = f"坦克风暴每日任务：成功 {okn}" + (f"，未成 {bad}" if bad else "")
    html = ("<p>本轮共执行 %d 项</p><table border='1' cellpadding='6' "
            "style='border-collapse:collapse;font-size:14px'>%s</table>"
            % (len(acted), "".join(rows)))
    notify.send(config, title, html, template="html")


def _force_close_socket(sock) -> None:
    """从别的线程也能掐断。心跳卡在发包锁上时，普通 close 会一起卡死。"""
    raw = sock
    for _ in range(8):
        nxt = getattr(raw, "_sock", None)
        if nxt is None or nxt is raw:
            break
        raw = nxt
    if raw is None:
        return
    try:
        raw.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        raw.close()
    except OSError:
        pass


class _LockedSock:
    """收发都加锁。心跳线程只 sendall；Windows 上和任务线程同时 recv 会让
    settimeout 失效，recv 一直卡住（冷却日志之后再也没下文）。"""

    def __init__(self, sock):
        self._sock = sock
        self._lock = threading.Lock()

    def sendall(self, data, *a, **kw):
        with self._lock:
            return self._sock.sendall(data, *a, **kw)

    def send(self, data, *a, **kw):
        with self._lock:
            return self._sock.send(data, *a, **kw)

    def recv(self, n, *a, **kw):
        with self._lock:
            return self._sock.recv(n, *a, **kw)

    def settimeout(self, t):
        with self._lock:
            return self._sock.settimeout(t)

    def write(self, data):
        return self.sendall(data)

    def close(self):
        _force_close_socket(self)

    def __getattr__(self, name):
        return getattr(self._sock, name)


class _Beater:
    """独立心跳线程。任务卡住（写库、等战报）时心跳照发。

    心跳是明文豁免、不碰 RC4。发包走 _LockedSock，避免和任务 sendall 字节交错。
    __call__ 保留给 _nap / _await_response，那边只排空下行，不再从任务线程发心跳。
    """

    def __init__(self, sock, hb: bytes, interval: float):
        self.sock = sock
        self.hb = hb
        self.interval = max(1.0, float(interval) or 10.0)
        self.last = time.time()
        self.count = 0
        self._link_note = 0.0
        self._stop = threading.Event()
        from . import citydb
        self._ctx_user = citydb.attack_context_user()
        self._ctx_qq = citydb.attack_context_qq()
        self._th = threading.Thread(target=self._loop, name="game-heartbeat",
                                    daemon=True)
        self._th.start()

    def _loop(self):
        if self._ctx_user or self._ctx_qq:
            from . import citydb
            citydb.set_attack_context(self._ctx_user, self._ctx_qq)
        while not self._stop.wait(self.interval):
            try:
                self.sock.sendall(self.hb)
                self.last = time.time()
                self.count += 1
                self._link_note = self.last
                try:
                    from . import citydb
                    citydb.note_attack_link(self.interval)
                except Exception:
                    log.debug("游戏心跳时间没写上", exc_info=True)
                if self.count % 10 == 1:
                    log.info("心跳運行中（第 %d 次，每 %.0fs）",
                             self.count, self.interval)
                else:
                    log.debug("心跳 #%d", self.count)
            except (TimeoutError, socket.timeout):
                continue
            except OSError:
                break

    def __call__(self) -> None:
        return

    def stop(self):
        self._stop.set()
        self._th.join(timeout=2)


_pulse_local = threading.local()


class _Heart:
    """这条连接的心跳句柄。停掉只卸下连接，会话脉冲还在。"""

    def __init__(self, pulse):
        self._pulse = pulse

    def __call__(self) -> None:
        return

    def stop(self) -> None:
        self._pulse.detach()

    @property
    def count(self):
        return self._pulse.count

    @property
    def interval(self):
        return self._pulse.interval


class _Pulse:
    """一条线程做三件事：游戏心跳、网页票据、每 10 秒写一次在线状态。"""

    def __init__(self, qq, user_id: int, uin: str, attack: bool):
        self.qq = qq
        self.user_id = int(user_id or 0)
        self.uin = str(uin or "")
        self.attack = bool(attack)
        self.interval = 10.0
        self.count = 0
        self.last = 0.0
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._sock = None
        self._hb = b""
        self._hb_at = 0.0
        now = time.monotonic()
        self._status_at = now
        self._touch_at = now
        self._th = threading.Thread(
            target=self._loop, name="session-pulse", daemon=True)

    def start(self) -> None:
        self._th.start()

    def alive(self) -> bool:
        return self._th.is_alive()

    def attach(self, sock, hb: bytes, interval: float):
        with self._lock:
            self._sock = sock
            self._hb = hb
            self.interval = max(1.0, float(interval) or 10.0)
            self.count = 0
            self.last = 0.0
            self._hb_at = time.monotonic()
        return _Heart(self)

    def detach(self) -> None:
        with self._lock:
            self._sock = None
            self._hb = b""

    def stop(self) -> None:
        self._stop.set()
        self._th.join(timeout=2)

    def _loop(self) -> None:
        from . import citydb
        if self.user_id or self.uin:
            citydb.set_attack_context(self.user_id, self.uin)
        while not self._stop.is_set():
            self._beat()
            if self.attack:
                self._status()
            self._touch()
            if self._stop.wait(1.0):
                return

    def _beat(self) -> None:
        with self._lock:
            sock = self._sock
            hb = self._hb
            interval = self.interval
            hb_at = self._hb_at
        if sock is None or not hb:
            return
        if time.monotonic() - hb_at < interval:
            return
        try:
            sock.sendall(hb)
        except (TimeoutError, socket.timeout):
            return
        except OSError:
            self.detach()
            return
        with self._lock:
            self._hb_at = time.monotonic()
            self.last = time.time()
            self.count += 1
            count = self.count
            interval = self.interval
        try:
            from . import citydb
            citydb.note_attack_link(interval)
        except Exception:
            log.debug("游戏心跳时间没写上", exc_info=True)
        if count % 10 == 1:
            log.info("心跳運行中（第 %d 次，每 %.0fs）", count, interval)
        else:
            log.debug("心跳 #%d", count)

    def _status(self) -> None:
        if time.monotonic() - self._status_at < 10:
            return
        self._status_at = time.monotonic()
        from . import citydb
        try:
            citydb.touch_attack_status()
        except Exception:
            log.debug("攻打进程心跳没写上", exc_info=True)
        if not self.user_id:
            return
        try:
            st = citydb.attack_status(self.user_id)
            if (st.get("online") and not st.get("keepalive")
                    and st.get("phase") in (
                        "hold", "paused", "running", "daily", "queue")):
                if _drop_live_sock(self.user_id):
                    log.info("挂机保活游戏心跳没了，掐掉旧连接")
        except Exception:
            log.debug("旧游戏连接没掐掉", exc_info=True)

    def _touch(self) -> None:
        if self.qq is None:
            return
        if time.monotonic() - self._touch_at < LOGIN_TOUCH_SEC:
            return
        self._touch_at = time.monotonic()
        try:
            self.qq.touch_login(block=False)
        except Exception:
            log.debug("网页票据保活没做成", exc_info=True)


def _open_heart(sock, hb: bytes, interval: float):
    pulse = getattr(_pulse_local, "pulse", None)
    if pulse is not None and pulse.alive():
        return pulse.attach(sock, hb, interval)
    return _Beater(sock, hb, interval)


def _start_pulse(qq, *, attack: bool):
    from . import citydb
    user_id = citydb.attack_context_user()
    uin = citydb.attack_context_qq()
    if attack:
        if user_id and not citydb.attack_qq_blocked(user_id):
            citydb.set_attack_paused(False, user_id)
        citydb.set_attack_status("login")
    pulse = _Pulse(qq, user_id, uin, attack)
    pulse.start()
    _pulse_local.pulse = pulse
    return pulse


def _stop_pulse(pulse) -> None:
    if getattr(_pulse_local, "pulse", None) is pulse:
        _pulse_local.pulse = None
    if pulse is None:
        return
    pulse.stop()
    if not pulse.attack:
        return
    from . import citydb
    try:
        citydb.clear_attack_hold()
        user_id = citydb.attack_context_user()
        if not citydb.attack_qq_blocked(user_id):
            citydb.set_attack_paused(False, user_id)
        citydb.set_attack_status("offline")
    except Exception:
        log.debug("攻打进程收尾状态没写上", exc_info=True)


def _watch_city_ids(config):
    w = config.get("城市监视") or {}
    if not w.get("启用"):
        return [], 300.0
    from . import citydb
    ids, seen = [], set()
    extra = []
    try:
        extra = citydb.watch_city_ids()
    except Exception as exc:
        log.info("读订阅城市失败：%s", exc)
    for x in list(w.get("城市") or []) + extra:
        try:
            v = int(x)
        except (TypeError, ValueError):
            continue
        if v > 0 and v not in seen:
            seen.add(v)
            ids.append(v)
    return ids, float(w.get("间隔秒") or 300)


def _scan_plan(config):
    """页面上保存过的安排优先。返回 (城市页范围, 间隔秒, 是否停扫, 开始, 结束)。"""
    from . import citydb

    saved = None
    try:
        saved = citydb.get_scan_plan()
    except Exception as exc:
        log.info("读扫描安排失败，沿用启动时的配置：%s", exc)
    if saved is None:
        jobs, gap = _page_range_jobs(config)
        return jobs, gap, False, "", ""
    return (saved["jobs"], float(saved["gap_sec"] or 300), bool(saved["quiet"]),
            saved["quiet_start"], saved["quiet_end"])


def _page_range_jobs(config):
    """页范围监视。返回 ([(城市, 起始页, 结束页), ...], 间隔秒)。没启用给空列表。"""
    w = config.get("页范围监视") or {}
    gap = float(w.get("间隔秒") or 300)
    if not w.get("启用"):
        return [], gap
    raw = w.get("范围") or []
    if not isinstance(raw, list):
        log.error("页范围监视.范围 要是列表")
        raw = []
    if not raw and w.get("城市"):
        raw = [{"城市": w.get("城市"), "起始页": w.get("起始页") or 0,
                "结束页": w.get("结束页")}]
    jobs, seen = [], set()
    for item in raw:
        if not isinstance(item, dict):
            log.error("页范围监视有一条不是对象，已跳过")
            continue
        try:
            city = int(item.get("城市") or 0)
            start = int(item.get("起始页") or 0)
            end = item.get("结束页")
            end = int(end)
        except (TypeError, ValueError):
            log.error("页范围监视配置不完整：%s", item)
            continue
        if city <= 0 or start < 0 or end < start:
            log.error("页范围监视页码无效：城市 %s，第 %s–%s 页", city, start, end)
            continue
        if city in seen:
            log.error("城市 %s 写了两段页范围，一座城只保留第一段", city)
            continue
        seen.add(city)
        jobs.append((city, start, end))
    return jobs, gap


def _scan_one_city(rec, sock, config, city_id, beat, country_id=0, start_page=0,
                   end_page=None):
    """拉一座城：玩家入库，并记下当前归属国。"""
    from . import citydb, country_war

    cname = citydb.city_name(city_id) or str(city_id)
    ts = citydb.now_ts()
    n = [0]

    def on_page(batch, page):
        n[0] += citydb.upsert_players(city_id, batch, ts, page=page)

    out = country_war.list_city_players(
        rec, sock, config, city_id, country=country_id, beat=beat,
        on_page=on_page, start_page=start_page, end_page=end_page)
    players = out.get("玩家") or []
    last = out.get("last_page")
    total = out.get("userCnt")
    owner = out.get("owner")
    if owner is not None or total is not None:
        citydb.record_occupy(city_id, owner, total, ts)
    full = (start_page == 0 and not out.get("原因")
            and total is not None and len(players) >= total)
    changes = citydb.sync_watch(
        city_id, [str(p.get("uid") or "") for p in players], not out.get("原因"))
    if full:
        gone = citydb.drop_stale(city_id, ts)
        if gone:
            log.info("已清掉本城过期记录 %d 条", gone)
    token = ((config.get("通知") or {}).get("pushplus_token") or "").strip()
    for ch in changes:
        who = ch["name"] or ch["uid"]
        where = f"{cname} {city_id}".strip()
        page = f"第 {ch['page']} 页" if ch.get("page") else "页数未知"
        if ch.get("push", True):
            text = f"{who}\n城市 {where}\n{page}\nUID {ch['uid']}"
            log.info("[订阅] %s 出现在 %s %s", who, where, page)
            notify.push_watch(config, ch, text)
            if token:
                notify.send(config, f"坦克风暴：{who} 出现在 {where} {page}", text)
        if not ch.get("arm"):
            continue
        try:
            queued = citydb.enqueue_online_attack(ch["user_id"], city_id, ch["uid"])
            if queued or citydb.online_attack_busy(ch["user_id"], city_id, ch["uid"]):
                citydb.mark_lock_sent(ch["user_id"], city_id, ch["uid"])
        except Exception:
            log.info("[订阅] %s 上线攻打没排上", who, exc_info=True)
            queued = False
        if queued:
            log.info("[订阅] %s 在 %s，已交给攻打号排队", who, where)
    oname = citydb.country_name(owner) if owner else ""
    log.info("―― 城市 %s %s ―― 归属国家 %s%s，面板人数 %s，本轮写入 %d 人，最后一页 %s",
             out.get("city"), cname, owner,
             f" {oname}" if oname else "", total, n[0], last)
    return out, n[0]


def _scan_page_ranges(rec, sock, config, jobs, beat):
    from . import citydb

    try:
        citydb.ensure_catalog()
    except Exception as exc:
        log.warning("城市目录更新失败（仍会拉玩家）：%s", exc)
    log.info("[页范围] 开始刷新 %d 座城", len(jobs))
    done = 0
    for city, start, end in jobs:
        try:
            out, _n = _scan_one_city(rec, sock, config, city, beat,
                                     start_page=start, end_page=end)
        except OSError:
            raise
        except Exception as exc:
            log.error("页范围监视 城市 %s 异常：%s", city, exc)
            continue
        reason = out.get("原因") or ""
        if reason:
            log.info("[页范围] 城市 %s：%s", city, reason)
        if any(s in reason for s in ("没有回包", "读不到国战面板", "连接断开",
                                     "读不到自己的国家ID")):
            log.info("[页范围] 本轮剩下的下次再拉")
            break
        done += 1
    log.info("[页范围] 本轮完成 %d/%d 座城", done, len(jobs))


def _watch_cities_round(rec, sock, config, cities, beat):
    from . import citydb

    try:
        citydb.ensure_catalog()
    except Exception as exc:
        log.warning("城市目录更新失败（仍会拉玩家）：%s", exc)
    log.info("[监视] 开始刷新 %d 座城", len(cities))
    done = 0
    for cid in cities:
        out, _n = _scan_one_city(rec, sock, config, cid, beat)
        reason = out.get("原因") or ""
        if any(s in reason for s in ("没有回包", "读不到国战面板", "连接断开")):
            log.info("[监视] %s，本轮剩下的下次再拉", reason)
            break
        done += 1
    log.info("[监视] 本轮完成 %d/%d 座城", done, len(cities))


def run_country_war_once(qq, config: dict, rounds: int) -> int:
    """连一次游戏、自动打 N 次摩多军团、断开退出。

    和 run_daily_once 走同一套连接/登录/心跳流程，只是把跑的东西换成国战。
    打几百次动辄十几分钟，心跳必须全程续着。
    """
    from . import country_war, shop

    def _work(rec, sock, spec, ctx, beater):
        # 支援兵是国战的消耗品，开打之前先按配置补货（默认关闭，开了才买）。
        # 放在这里而不是打完之后：库存不够的话这一轮就打不动了。
        if (config.get("功勋商城", {}) or {}).get("自动补支援兵"):
            ok, why = shop.daily_restock(rec, sock, config)
            log.info("[国战] 开打前补支援兵：%s %s", "✅" if ok else "❌", why)
        out = country_war.run(rec, sock, config, rounds=rounds, beat=beater)
        log.info("―― 国战成果 ―― 扫荡 %d 次，攻击 %d 次，召唤 %d 次，战功 +%s",
                 out["扫荡"], out["攻击"], out["召唤"], out["战功"])
        log.info("   剩余行动力 %s，今日攻击次数 %s；结束原因：%s",
                 out.get("剩余行动力"), out.get("今日攻击次数"),
                 out["停止原因"])
        log.info("   任务执行期间共发心跳 %d 次", beater.count)
        notify.send(config, "坦克风暴：国战自动战斗完成",
                    f"扫荡 {out['扫荡']} 次，攻击 {out['攻击']} 次，"
                    f"战功 +{out['战功']}，"
                    f"剩余行动力 {out.get('剩余行动力')}。<br>"
                    f"结束原因：{out['停止原因']}")
        return 0 if (out["扫荡"] + out["攻击"]) else 1

    return _connect_and(qq, config, _work)


def run_city_players_once(qq, config: dict, city_id: int,
                          country_id: int = 0, start_page: int = 0,
                          end_page=None) -> int:
    """连一次游戏、查询指定城市的玩家列表、写入 sqlite、断开退出。"""
    from . import citydb

    def _work(rec, sock, spec, ctx, beater):
        try:
            citydb.ensure_catalog()
        except Exception as exc:
            log.warning("城市目录更新失败（仍会写玩家）：%s", exc)
        out, n = _scan_one_city(rec, sock, config, city_id, beater,
                               country_id=country_id, start_page=start_page,
                               end_page=end_page)
        last = out.get("last_page")
        log.info("   查询期间共发心跳 %d 次；库文件 %s", beater.count, citydb.DB_FILE)
        if out.get("原因"):
            nxt = (last + 1) if isinstance(last, int) else start_page
            log.info("   %s", out["原因"])
            log.info("   续拉：py main.py --city-players %s --city-page %s",
                     city_id, nxt)
        return 0 if n or not out.get("原因") else 1

    return _connect_and(qq, config, _work)


def run_base_atk_once(qq, config: dict, uid) -> int:
    """连一次。照 2026-10-17 雷电24：拉地方列表、进入基地、进攻、5 秒后撤回。"""
    uid = str(uid or "").strip()
    if not uid.isdigit():
        log.error("基地 UID 要是一串数字")
        return 1

    def _work(rec, sock, spec, ctx, beater):
        from .proto_encode import encode_message

        def send(op, fields):
            mark = rec.seq_mark() if rec else 0
            sender.send_frame(sock, op, encode_message(fields, omit_zero=False),
                              rec.rc4_c2s)
            return mark

        def load(kind, base):
            # 客户端把没填的 h/hn 写成字符串 undefined，enterType 显式写 0。
            return {2: ("string", base), 3: ("string", kind),
                    4: ("string", "undefined"), 5: ("string", "undefined"),
                    6: ("int32", 0)}

        def has_uid(obj):
            if isinstance(obj, str):
                return obj == uid
            if isinstance(obj, dict):
                return any(has_uid(v) for v in obj.values())
            if isinstance(obj, list):
                return any(has_uid(v) for v in obj)
            return False

        prev = daily.bind_sock(sock)
        viewed = False
        try:
            # 2026-10-17 00:49 雷电24：先 RceGet type=1 拉地方列表，
            # 再 RceLoad view，等到对方基地数据才 RceAttack。
            log.info("[基地] 拉地方列表")
            since = send("041a", {1: ("string", "0"), 2: ("int32", 1)})
            listed = daily._await_response(
                sock, rec, "RseGet", since, 3.0,
                want=lambda d: isinstance(d, dict) and d.get("type") == 1)
            if not isinstance(listed, dict):
                log.info("[基地] 地方列表没有回包")
            else:
                daily._nap(1.0)
            rows = [d for seq, d in (getattr(rec, "recent", {}) or {}).get("RseGet", [])
                    if seq > since]
            found = any(has_uid(d) for d in rows)
            log.info("[基地] 列表 %d 条，%s", len(rows),
                     "有这个人" if found else "没有这个人")
            if not found:
                # 列表是随一批 25 人。指定 UID 再要一次：baseid 从 "0" 换成这个人。
                log.info("[基地] 按 UID 再要一次 %s", uid)
                since = send("041a", {1: ("string", uid), 2: ("int32", 1)})
                daily._await_response(
                    sock, rec, "RseGet", since, 3.0,
                    want=lambda d: isinstance(d, dict) and has_uid(d))
                daily._nap(0.4)
                found = any(
                    has_uid(d) for seq, d in (getattr(rec, "recent", {}) or {}).get("RseGet", [])
                    if seq > since)
                log.info("[基地] 指定查询%s", "命中" if found else "没有回这个人")
            log.info("[基地] 进入 %s", uid)
            since = send("041d", load("view", uid))
            viewed = True
            loaded = daily._await_response(
                sock, rec, "RseLoad", since, 5.0,
                want=lambda d: isinstance(d, dict) and (
                    str(d.get("baseid") or "") == uid
                    or str(d.get("userid") or "") == uid))
            if isinstance(loaded, dict):
                log.info("[基地] 已拿到 %s", loaded.get("basename") or uid)
            else:
                log.info("[基地] 没有这个基地的数据，直接进攻")
            log.info("[基地] 进攻 %s", uid)
            t0 = time.time()
            since = send("0411", {1: ("int32", 1), 2: ("string", uid),
                                  17: ("int32", 0), 31: ("int32", 3)})
            reply = daily._await_response(
                sock, rec, "RseAttack", since, 4.0,
                want=lambda d: isinstance(d, dict) and d.get("type") == 1)
            allowed = isinstance(reply, dict) and reply.get("canAttack") is True
            if reply is None:
                log.info("[基地] 4 秒内没有进攻回包")
            else:
                log.info("[基地] canAttack=%s errorid=%s",
                         reply.get("canAttack"), reply.get("errorid"))
            left = 5.0 - (time.time() - t0)
            if left > 0:
                daily._nap(left)
            log.info("[基地] 撤回")
            since = send("0411", {1: ("int32", 3)})
            back = daily._await_response(
                sock, rec, "RseAttack", since, 3.0,
                want=lambda d: isinstance(d, dict) and d.get("type") == 3)
            log.info("[基地] 撤回回包 %s",
                     "type=3" if isinstance(back, dict) else "没有")
            return 0 if allowed else 1
        finally:
            if viewed:
                try:
                    send("041d", load("build", "0"))
                    log.info("[基地] 已回自己基地")
                except OSError as exc:
                    log.warning("[基地] 回家失败: %s", exc)
            daily.bind_sock(prev)

    return _connect_and(qq, config, _work)


def run_attack_once(qq, config: dict, uid, times=1, sweep=False,
                    city_id=0, country=0) -> int:
    """连一次游戏、打指定玩家若干次、断开退出。不迁城。"""
    from . import country_war

    def _work(rec, sock, spec, ctx, beater):
        out = country_war.attack_player(
            rec, sock, config, uid, times=times, sweep=sweep,
            city_id=city_id, country=country, beat=beater)
        who = out.get("名字") or out.get("目标")
        log.info("―― 打人 %s ―― %s %d/%d 次，用卡 %s，战功 +%s，剩余行动力 %s",
                 who, out.get("动作"), out.get("成功") or 0, times,
                 out.get("用卡"), out.get("战功"), out.get("剩余行动力"))
        if out.get("停止原因"):
            log.info("   结束原因：%s", out["停止原因"])
        log.info("   任务执行期间共发心跳 %d 次", beater.count)
        return 0 if out.get("成功") else 1

    return _connect_and(qq, config, _work)


def run_route_once(qq, config: dict, city_id, country=0) -> int:
    """连一次游戏，读自己当前城市，规划到目标城的路线，然后断开。不迁城。"""
    from . import citydb, country_war

    def _work(rec, sock, spec, ctx, beater):
        my = int(country or (config.get("国战") or {}).get("自己国家ID") or 0) \
            or daily.read_my_country(rec)
        if not my:
            log.error("读不到自己的国家ID，停手")
            return 1
        _, loc, _, panel = country_war._panel(sock, rec, my)
        if panel is None or not loc:
            log.error("读不到当前所在城市，停手")
            return 1
        log.info("[路线] 当前在 %s %s，目标 %s %s",
                 loc, citydb.city_name(loc) or loc,
                 city_id, citydb.city_name(city_id) or city_id)
        plan = country_war.live_plan(sock, rec, loc, city_id, my)
        for line in citydb.format_route(plan):
            log.info("[路线] %s", line)
        return 0 if plan.get("路径") else 1

    return _connect_and(qq, config, _work)


def run_move_once(qq, config: dict, city_id, sweep=False, country=0) -> int:
    """连一次游戏，沿路线走到目标城，然后断开。"""
    from . import country_war

    def _work(rec, sock, spec, ctx, beater):
        if country:
            config.setdefault("国战", {})["自己国家ID"] = int(country)
        out = country_war.walk_to(
            rec, sock, config, city_id, sweep=sweep, beat=beater)
        log.info("―― 移动到 %s ―― 走了 %d 步，停在 %s，打中 %s 次",
                 city_id, out.get("移动") or 0, out.get("走到"),
                 out.get("攻击") if out.get("攻击") is not None else "未打")
        if out.get("停止原因"):
            log.info("   结束原因：%s", out["停止原因"])
        return 0 if out.get("攻击") is not None else 1

    return _connect_and(qq, config, _work)


def _cards_used_up(reason: str) -> bool:
    """这一单的恢复卡额度用完了，或者背包里已经没有。算打完，不算没打成。"""
    text = str(reason or "")
    if "国战恢复卡已用完" in text:
        return True
    if "恢复卡" in text and "达到上限" in text:
        return True
    matched = re.search(r"本次已用\s*(\d+)\s*/\s*(\d+)\s*张恢复卡", text)
    if not matched:
        return False
    used, limit = int(matched.group(1)), int(matched.group(2))
    return used >= limit


def _person_blocking(reason: str) -> bool:
    """停下来是因为路上有打不过的人。目标城里清不完不算，那不是通路的问题。"""
    text = str(reason or "")
    if "这座城清不完" in text:
        return False
    if "挡路" in text or "打不过的人" in text:
        return True
    if "都打不过" in text and "路径" in text:
        return True
    return "这条路就不通" in text


def _defeated(reason: str) -> bool:
    """自己被别人打败。一般是人回到了首都。"""
    text = str(reason or "")
    return "被别人打败" in text or "回到首都" in text or "出不了首都" in text


def _here_of(out) -> int:
    """这一单人在哪座城。被打回首都时，走到还是战场，以记下的位置为准。"""
    from . import citydb

    noted = citydb.attack_here_id()
    if noted > 0:
        return noted
    try:
        here = int((out or {}).get("走到") or 0)
    except (TypeError, ValueError):
        here = 0
    return here if here > 0 else 0


def _ready_for_same_city(here, city_id) -> bool:
    """人还在目标城或相邻城，同城的索敌可以接着打。"""
    from . import citydb

    try:
        here = int(here or 0)
        city_id = int(city_id or 0)
    except (TypeError, ValueError):
        return False
    return bool(here and city_id and citydb.can_reach(here, city_id))


def _lock_no_retreat(why: str, status: str) -> bool:
    """被打回首都或手动停，不要再执行后退。"""
    if status == "ended":
        return True
    text = str(why or "")
    return _defeated(text) or text == "已手动关停"


def _lock_no_chain(why: str, status: str) -> bool:
    """这种停手不能再连着打同城的索敌。"""
    if _lock_no_retreat(why, status):
        return True
    text = str(why or "")
    if text in ("已暂停", "已定时关闭"):
        return True
    if "连接中断" in text or "攻打中断" in text:
        return True
    if "行动力" in text or "体力不够" in text or _cards_used_up(text) or "遣返" in text:
        return True
    return False


def _auto_retreat(rec, sock, fight_config, beater, plan) -> str:
    """打完后退走一次。返回说明；没走就是空字符串。连接断了直接抛。"""
    from . import country_war

    retreat_mode = str((plan or {}).get("mode") or "off")
    if retreat_mode not in ("hops", "city"):
        return ""
    label = str((plan or {}).get("name") or "").strip()
    if not label:
        label = "马奇诺" if retreat_mode == "hops" else "目标城"
    prefix = f"朝{label}后退" if retreat_mode == "hops" else f"退到{label}"
    try:
        back = country_war.retreat_toward(
            rec, sock, fight_config, beat=beater, name=label,
            city_id=int((plan or {}).get("city_id") or 0),
            hops=int((plan or {}).get("hops") or 3), mode=retreat_mode)
    except OSError:
        raise
    except Exception as exc:
        log.info("%s失败", prefix, exc_info=True)
        detail = _interrupt_reason(exc)
        if detail.startswith("攻打中断："):
            detail = detail[len("攻打中断："):]
        return f"{prefix}时：{detail}"
    return str((back or {}).get("说明") or "").strip()


def _merge_reason(why: str, note: str) -> str:
    why = str(why or "").strip()
    note = str(note or "").strip()
    if note and not why:
        return note
    if note and why and note not in why:
        return f"{why}。{note}"
    return why


def _empty_city(reason: str) -> bool:
    """清城扫完这几页，一个人都没有。"""
    return str(reason or "") in ("这几页没有可打的人", "这一页没有可打的人")


def _clear_hard_stop(reason: str) -> bool:
    """这种停手不能过一会儿再清。"""
    text = str(reason or "")
    return any(k in text for k in (
        "行动力", "体力不够", "已暂停", "已定时关闭", "已手动关停", "被别人打败", "回到首都", "出不了首都",
        "遣返", "不相邻", "恢复卡", "位置变了", "连接中断", "攻打中断"))


def _clear_stuck(reason: str, out) -> bool:
    """城里还留着打不过的人。空城不算。"""
    text = str(reason or "")
    if "清不完" in text or "打不过" in text or "没打过" in text or "人数没减少" in text:
        return True
    try:
        return int((out or {}).get("没打过") or 0) > 0
    except (TypeError, ValueError):
        return False


def _clear_cycle(reason: str, out) -> bool:
    """空城，或还剩打不过的人，可以按空城再打的分钟再来一轮。"""
    if _clear_hard_stop(reason):
        return False
    return _empty_city(reason) or _clear_stuck(reason, out)


def _clear_finished(status: str, reason: str) -> bool:
    """这几页打完了。空城再打开着时，过一会儿再来看有没有新人。"""
    if str(status or "") != "done":
        return False
    if _clear_hard_stop(reason):
        return False
    return str(reason or "") in ("", "这座城打完了")


def _order_result(job, out) -> tuple:
    """这一单打完怎么收。返回 (动作, 状态, 原因)。

    动作是 defer、finish、park。自动锁敌没打完就结束，等下一次触发。
    清整座城时被打回首都，记为已结束。
    """
    uid = str((job or {}).get("uid") or "").strip()
    reason = str((out or {}).get("停止原因") or "")
    auto = bool((job or {}).get("auto"))
    attacked = bool((out or {}).get("攻击")) if uid else (out or {}).get("攻击") is not None
    if reason in ("已暂停", "已定时关闭"):
        return "defer", "", reason
    if reason == "已手动关停":
        return "finish", "ended", reason
    if auto:
        if (out or {}).get("遣返") or _defeated(reason):
            why = reason or "被别人打败，已回到首都"
            if bool((out or {}).get("击退")) or attacked:
                return "finish", "done", why
            return "finish", "failed", why
        if bool((out or {}).get("击退")) or (not reason and attacked):
            return "finish", "done", reason
        return "finish", "failed", reason or "没打完，等下一次索敌"
    if not uid and _defeated(reason):
        return "finish", "ended", reason or "被别人打败，已回到首都"
    if not uid and reason == "这座城打完了":
        return "finish", "done", ""
    if reason and not _cards_used_up(reason) and _person_blocking(reason):
        return "park", "", reason
    if reason and _cards_used_up(reason):
        return "finish", "done", reason
    if reason:
        return "finish", "failed", reason
    if attacked:
        return "finish", "done", ""
    return "finish", "failed", "未打成"


def _interrupt_reason(exc) -> str:
    """中断时把具体错误写进说明。只写「攻打中断」看不出是什么错。"""
    name = type(exc).__name__
    detail = " ".join(str(exc).split()).strip()
    if detail and name not in detail:
        text = f"{name}：{detail}"
    else:
        text = detail or name
    if len(text) > 160:
        text = text[:160].rstrip() + "…"
    return "攻打中断：" + text


def _fight_modo(rec, sock, config, beater, job) -> None:
    """刷摩多军团。走进离自己最近的一座城，召唤支援兵再打。恢复卡按订单上的数量。"""
    from . import citydb, country_war

    citydb.set_attack_status("running")
    citydb.set_fighting_order(job["id"])
    start_beats = int(job.get("beats") or 0)

    def _beat_note(n, name=""):
        citydb.note_attack_beats(job["id"], n)

    tally = {"n": start_beats, "note": _beat_note}
    citydb.note_attack_beats(job["id"], start_beats)
    try:
        try:
            country_war.guard_low_morale(sock, rec, config)
            out = country_war.farm_modo_order(
                rec, sock, config, int(job.get("cards") or 0),
                beat=beater, tally=tally)
        except OSError:
            citydb.finish_attack_order(
                job["id"], "failed", "连接中断", beats=int(tally.get("n") or 0))
            raise
        except Exception as exc:
            why = _interrupt_reason(exc)
            log.info("订单 %s %s", job["id"], why, exc_info=True)
            citydb.finish_attack_order(
                job["id"], "failed", why, beats=int(tally.get("n") or 0))
            return
        why = str(out.get("停止原因") or "")
        note = str(out.get("说明") or "").strip()
        hits = int(out.get("攻击") or 0)
        beats = int(tally.get("n") or 0)
        log.info("―― 摩多军团 订单 %s ―― 打 %d 次，召唤 %d 次，用卡 %d 张，补支援兵 %d 个",
                 job["id"], hits, int(out.get("召唤") or 0), int(out.get("用卡") or 0),
                 int(out.get("补兵") or 0))
        if why:
            log.info("订单 %s 结束原因：%s", job["id"], why)
        if why in ("已暂停", "已定时关闭"):
            citydb.defer_attack_order(job["id"])
            log.info("订单 %s %s，放回排队", job["id"], why)
            return
        if why == "已手动关停" or why.endswith("已手动关停"):
            citydb.finish_attack_order(job["id"], "ended", "已手动关停", beats=beats)
            return
        if _defeated(why):
            citydb.finish_attack_order(
                job["id"], "ended", why or "被别人打败，已回到首都", beats=beats)
            return
        show = note or why
        if why and _person_blocking(why) and (
                citydb.attack_hold_minutes() > 0 or citydb.attack_hold_always()):
            citydb.park_attack_order(job["id"], show or why, beats=beats)
            log.info("订单 %s 有人挡路，挂机期间继续看路径", job["id"])
            return
        if why and _person_blocking(why):
            citydb.finish_attack_order(job["id"], "failed", show or why, beats=beats)
            return
        if hits > 0:
            citydb.finish_attack_order(job["id"], "done", show, beats=beats)
            return
        citydb.finish_attack_order(
            job["id"], "failed", show or why or "未打成", beats=beats)
    finally:
        if _storm_interrupted() and not citydb.attack_paused() and not citydb.shutdown_due():
            citydb.reopen_interrupted_order(job["id"])
        citydb.set_fighting_order(0)


_LOCK_LEFT = "已不在这座城，等下次出现再打"


def _skip_stale_lock(rec, sock, config, beater, job, beats=0) -> bool:
    """排队的自动锁敌开打前再确认人还在订单这座城。已经离开就结束这一单。
    面板没打开时不算离开，照常走路。返回 True 表示这一单已经收掉。"""
    from . import citydb, country_war

    uid = str(job.get("uid") or "").strip()
    if not job.get("auto") or not uid:
        return False
    try:
        city_id = int(job.get("city_id") or 0)
    except (TypeError, ValueError):
        city_id = 0
    if city_id <= 0:
        return False
    info = citydb.lock_target_info(city_id, uid)
    if not info.get("gone"):
        try:
            peek = country_war.lock_target_here(
                rec, sock, config, city_id, uid,
                page=int(info.get("page") or 0), beat=beater)
        except OSError:
            raise
        except Exception:
            log.info("订单 %s 索敌前没看清 UID %s 还在不在城 %s，照常走",
                     job["id"], uid, city_id, exc_info=True)
            return False
        if peek.get("here") is None:
            log.info("订单 %s 索敌前没看清 UID %s 还在不在城 %s：%s，照常走",
                     job["id"], uid, city_id, peek.get("原因") or "面板没打开")
            return False
        if peek.get("here"):
            p = peek.get("player") or {}
            if p.get("morale") is not None:
                citydb.note_lock_morale(p.get("morale"))
            try:
                citydb.upsert_players(
                    city_id, [p], citydb.now_ts(), page=int(peek.get("page") or 0))
            except Exception:
                pass
            return False
    log.info("订单 %s 索敌前 UID %s 已不在城 %s，跳过", job["id"], uid, city_id)
    citydb.mark_watch_repelled(city_id, uid)
    citydb.finish_attack_order(job["id"], "failed", _LOCK_LEFT, beats=int(beats or 0))
    return True


@contextmanager
def _order_account(user_id: int):
    """打这一单时用下单的登录账号，打完回到这条线程原来的账号。"""
    from . import citydb
    prev = citydb.attack_context_user()
    qq = citydb.attack_context_qq()
    owner = int(user_id or 0)
    switched = bool(owner and owner != prev)
    if switched:
        citydb.set_attack_context(owner, qq or citydb.attack_qq_of(owner))
    try:
        yield
    finally:
        if switched and prev:
            citydb.set_attack_context(prev, qq)


_sched_local = threading.local()
# 外层空等还剩这么多秒以内，就不再新开一单国战，免得起步后立刻被打断。
_PUMP_MIN = 8.0


def _war_depth() -> int:
    return int(getattr(_sched_local, "war", 0) or 0)


def _enter_war() -> None:
    _sched_local.war = _war_depth() + 1


def _leave_war() -> bool:
    """退出一层国战。最外层退出时返回 True。"""
    left = _war_depth() - 1
    if left < 0:
        left = 0
    _sched_local.war = left
    return left == 0


def _fight_claimed(rec, sock, config, beater, job, from_chain=False) -> dict:
    from . import citydb

    prev = citydb.current_lane()
    _enter_war()
    citydb.set_lane("war")
    try:
        try:
            with daily.lane_hold():
                return _fight_locked(rec, sock, config, beater, job, from_chain)
        except daily.Preempt:
            if citydb.yield_attack_order(int(job["id"])):
                log.info("订单 %s 先让出连接，外层到点的动作做完再打", job["id"])
            raise
        except daily.Stopped:
            if citydb.yield_attack_order(int(job["id"])):
                log.info("订单 %s 先放回排队，当前这项已停下", job["id"])
            raise
    finally:
        outermost = _leave_war()
        try:
            if outermost:
                citydb.set_lane("war")
                citydb.set_attack_status("hold")
        finally:
            citydb.set_lane(prev)


def _fight_locked(rec, sock, config, beater, job, from_chain=False) -> dict:
    from . import citydb, country_war

    if str(job.get("kind") or "") == "modo":
        _fight_modo(rec, sock, config, beater, job)
        return {}
    uid = str(job.get("uid") or "").strip()
    citydb.set_attack_status("running")
    citydb.set_fighting_order(job["id"])
    start_beats = int(job.get("beats") or 0)

    def _beat_note(n, name=""):
        who = "" if uid else str(name or "").strip()
        citydb.note_attack_beats(job["id"], n, who)
        if str(name or "").strip():
            citydb.remember_order_name(job["id"], name)

    def _cut():
        return {
            "id": job["id"], "status": "", "why": "", "here": 0,
            "done": False, "failed": False, "stop": True, "parked": False,
        }

    tally = {"n": start_beats, "note": _beat_note}
    citydb.note_attack_beats(job["id"], start_beats)
    fight_config = config
    if job.get("cards") is not None:
        fight_config = dict(config)
        war = dict(config.get("国战") or {})
        war["单次最多用几张恢复卡"] = int(job["cards"])
        fight_config["国战"] = war
        log.info("订单 %s 最多用 %d 张恢复卡", job["id"], int(job["cards"]))
    guarding = bool(job.get("auto"))
    if guarding and not from_chain:
        citydb.begin_lock_cards()
    try:
        skipped = False
        out = {}
        try:
            if guarding and uid and _skip_stale_lock(
                    rec, sock, config, beater, job, tally.get("n") or 0):
                skipped = True
            else:
                country_war.guard_low_morale(sock, rec, fight_config)
                if uid:
                    info = citydb.find_player(uid) or {}
                    citydb.note_lock_morale(info.get("morale"))
                    if info.get("name"):
                        citydb.remember_order_name(job["id"], info.get("name"))
                plan = citydb.clear_fight_plan() if not uid else None
                out = country_war.walk_to(
                    rec, sock, fight_config, job["city_id"], beat=beater, uid=uid,
                    hold_if_blocked=bool(job.get("auto")), tally=tally,
                    clear_plan=plan)
        except OSError:
            citydb.finish_attack_order(
                job["id"], "failed", "连接中断", beats=int(tally.get("n") or 0))
            raise
        except Exception as exc:
            why = _interrupt_reason(exc)
            log.info("订单 %s %s", job["id"], why, exc_info=True)
            citydb.finish_attack_order(
                job["id"], "failed", why, beats=int(tally.get("n") or 0))
            return _cut()
        beats = int(tally.get("n") or 0)
        cycle_min = 0
        cycle_stuck = False
        cycle_cleared = False
        if skipped:
            status, why = "failed", _LOCK_LEFT
            here = citydb.attack_here_id()
            if job.get("auto") and not from_chain:
                log.info("订单 %s 是自动锁敌，人已不在，先看同城还有没有索敌", job["id"])
            result = {
                "id": job["id"], "status": status, "why": why, "here": here,
                "done": False, "failed": True,
                "stop": _lock_no_chain(why, status), "parked": False,
            }
            if from_chain:
                return result
        else:
            if uid:
                log.info("―― 打 UID %s 城 %s 订单 %s ―― 走了 %d 步，停在 %s，打中 %s 次",
                         uid, job["city_id"], job["id"], out.get("移动") or 0, out.get("走到"),
                         out.get("攻击") if out.get("攻击") is not None else "未打")
            else:
                log.info("―― 清城 %s 订单 %s ―― 走了 %d 步，停在 %s，打中 %s 次",
                         job["city_id"], job["id"], out.get("移动") or 0, out.get("走到"),
                         out.get("攻击") if out.get("攻击") is not None else "未打")
            action, status, why = _order_result(job, out)
            if why:
                log.info("订单 %s 结束原因：%s", job["id"], why)
            if (not uid and not job.get("auto") and action == "finish"
                    and (_clear_cycle(why, out) or _clear_finished(status, why))):
                cycle_min = citydb.clear_wait_minutes()
                cycle_stuck = _clear_stuck(why, out)
                cycle_cleared = _clear_finished(status, why) and not cycle_stuck
            if action == "defer":
                citydb.defer_attack_order(job["id"])
                log.info("订单 %s %s，放回排队", job["id"], why or "已暂停")
                return {"id": job["id"], "status": "", "why": why or "已暂停",
                        "here": _here_of(out), "done": False, "failed": False,
                        "stop": True, "parked": True}
            if action == "park" and (
                    citydb.attack_hold_minutes() > 0 or citydb.attack_hold_always()):
                citydb.park_attack_order(job["id"], why, beats=beats)
                log.info("订单 %s 有人挡路，挂机期间继续看路径", job["id"])
                return {"id": job["id"], "status": status, "why": why,
                        "here": _here_of(out), "done": False, "failed": True,
                        "stop": True, "parked": True}
            if action == "park":
                status = "failed"
            if job.get("auto") and status == "failed":
                log.info("订单 %s 是自动锁敌，没打完，跳过，等下一次触发", job["id"])
            elif status == "ended" and why != "已手动关停":
                log.info("订单 %s 清城时被打回首都，订单结束", job["id"])
            elif status == "ended":
                log.info("订单 %s 已手动关停", job["id"])
            here = _here_of(out)
            keep = status == "done" and not why and not uid
            result = {
                "id": job["id"], "status": status, "why": why, "here": here,
                "done": status == "done", "failed": status == "failed",
                "stop": _lock_no_chain(why, status), "parked": False,
            }
            if from_chain:
                citydb.finish_attack_order(
                    job["id"], status, why, beats=beats, keep_reason=keep)
                return result

        any_done = bool(result.get("done"))
        any_failed = bool(result.get("failed"))
        stop = bool(result.get("stop"))
        parked = bool(result.get("parked"))
        last_id = job["id"]
        finished = skipped
        chained = False
        city_id = int(job.get("city_id") or 0)
        if (job.get("auto") and not from_chain and not stop and not parked
                and _ready_for_same_city(here, city_id)):
            while _ready_for_same_city(here, city_id):
                nxt = citydb.claim_attack_order(city_id=city_id, auto_only=True)
                if not nxt:
                    break
                if not finished:
                    keep = status == "done" and not why and not uid
                    citydb.finish_attack_order(
                        job["id"], status, why, beats=beats, keep_reason=keep)
                    finished = True
                chained = True
                log.info("同城索敌订单 %s 生效，连着打，打完再后退", nxt["id"])
                with _order_account(int(nxt.get("user_id") or 0)):
                    nxt_res = _fight_claimed(
                        rec, sock, config, beater, nxt, from_chain=True)
                last_id = int(nxt_res.get("id") or last_id)
                here = int(nxt_res.get("here") or here)
                if nxt_res.get("done"):
                    any_done = True
                if nxt_res.get("failed"):
                    any_failed = True
                if nxt_res.get("parked"):
                    parked = True
                    stop = True
                    break
                if nxt_res.get("stop"):
                    stop = True
                    result["why"] = nxt_res.get("why") or result.get("why")
                    result["status"] = nxt_res.get("status") or result.get("status")
                    break
        if skipped and not chained:
            return result
        if job.get("auto"):
            plan = citydb.retreat_settings()
            clear_back = False
        elif not uid:
            plan = citydb.retreat_settings(clear=True)
            clear_back = plan.get("mode") in ("hops", "city")
        else:
            plan = {}
            clear_back = False
        country = _my_country(rec, fight_config)
        try:
            here = int(here or result.get("here") or 0)
        except (TypeError, ValueError):
            here = int(result.get("here") or 0)
        if country:
            try:
                _power, loc, _times, panel = country_war._panel(sock, rec, country)
            except Exception:
                loc, panel = 0, None
            if (panel is not None and isinstance(loc, int)
                    and not isinstance(loc, bool) and loc > 0):
                citydb.note_attack_here(loc)
                here = loc
        at_home = bool(here and country and country_war._is_capital(here, country))
        want = ((job.get("auto") or clear_back) and not parked and not _lock_no_retreat(
                    str(result.get("why") or why or ""),
                    str(result.get("status") or status or ""))
                and (any_done or (any_failed and plan.get("on_fail"))))
        if want and chained and any_failed:
            log.info("同城索敌还有没打完的订单，不后退")
            want = False
        if want and at_home:
            name = citydb.city_name(here) or ""
            where = f"{here} {name}".strip() if name else str(here)
            log.info("人已在所在国首都 %s，不再后退", where)
            note = f"人已经在所在国首都 {where}，不再后退"
            why = _merge_reason(why, note)
            if finished:
                citydb.append_order_reason(last_id, note)
            want = False
        back_note = ""
        if want:
            prefix = ""
            retreat_mode = str(plan.get("mode") or "off")
            if retreat_mode in ("hops", "city"):
                label = str(plan.get("name") or "").strip()
                if not label:
                    label = "马奇诺" if retreat_mode == "hops" else "目标城"
                prefix = f"朝{label}后退" if retreat_mode == "hops" else f"退到{label}"
            try:
                note = _auto_retreat(rec, sock, fight_config, beater, plan)
            except OSError:
                note = f"{prefix}时连接中断" if prefix else "后退时连接中断"
                log.info("订单 %s %s", last_id, note)
                if finished:
                    citydb.append_order_reason(last_id, note)
                else:
                    citydb.finish_attack_order(
                        job["id"], status, _merge_reason(why, note), beats=beats)
                raise
            if note:
                log.info("订单 %s %s", last_id, note)
                back_note = note
                why = _merge_reason(why, note)
                if finished:
                    citydb.append_order_reason(last_id, note)
        if cycle_min > 0 and citydb.schedule_empty_order(
                job["id"], cycle_min, beats=beats, stuck=cycle_stuck,
                cleared=cycle_cleared, note=back_note):
            if cycle_stuck:
                log.info("订单 %s 还有打不过的人，%d 分钟后再打", job["id"], cycle_min)
            elif cycle_cleared:
                log.info("订单 %s 这几页打完了，%d 分钟后再打", job["id"], cycle_min)
            else:
                log.info("订单 %s 这座城是空的，%d 分钟后再打", job["id"], cycle_min)
            return _cut()
        if not finished:
            keep = status == "done" and not why and not uid
            citydb.finish_attack_order(
                job["id"], status, why, beats=beats, keep_reason=keep)
        return result
    finally:
        if guarding and not from_chain:
            citydb.end_lock_cards()
        if _storm_interrupted() and not citydb.attack_paused() and not citydb.shutdown_due():
            citydb.reopen_interrupted_order(job["id"])
        citydb.set_fighting_order(0)


def _socket_raw(sock):
    raw = sock
    while hasattr(raw, "_sock") and getattr(raw, "_sock", None) is not raw:
        raw = raw._sock
    return raw


def _reply_online(sock, spec, ctx, data: bytes) -> bool:
    """这块字节开头是在线探测就回应。连接还在返回 True。"""
    reply = protocol.maybe_online_reply(spec, data, ctx)
    if not reply:
        return True
    try:
        sock.sendall(reply)
    except OSError:
        return False
    log.info("收到在线探测，已回应")
    return True


def _wait_socket(sock, spec, ctx) -> bool:
    """空闲时最多等一秒，并把已经到达的包读完。连接还在返回 True。

    用 select 等可读，不占收发锁。没有字节才等到那一秒；有字节就立刻读，
    内核里排队的块一次读完，然后再回到领单。
    """
    raw = _socket_raw(sock)
    try:
        readable, _, _ = select.select([raw], [], [], 1.0)
    except (OSError, TypeError, ValueError):
        readable = None
    if readable is not None and not readable:
        return True
    for _ in range(48):
        try:
            sock.settimeout(0.2)
            data = sock.recv(8192)
        except socket.timeout:
            return True
        except OSError:
            return False
        if not data:
            return False
        if not _reply_online(sock, spec, ctx, data):
            return False
        try:
            more, _, _ = select.select([raw], [], [], 0)
        except (OSError, TypeError, ValueError):
            return True
        if not more:
            return True
    return True


_PATH_GAP = 10.0


def _my_country(rec, config) -> int:
    from . import country_war

    conf = config.get("国战") or {}
    try:
        country = int(conf.get("自己国家ID") or 0)
    except (TypeError, ValueError):
        country = 0
    if country <= 0:
        country = country_war._daily.read_my_country(rec) or 0
    return int(country or 0)


def _blocked_path_open(rec, sock, config, job):
    """看原来的订单现在有没有通路。通了返回 True。还不通时带上原因。"""
    from . import citydb, country_war

    my = _my_country(rec, config)
    if not my:
        return False, "读不到自己的国家ID"
    with daily.lane_hold():
        _power, loc, _times, panel = country_war._panel(sock, rec, my)
        if panel is None or isinstance(loc, bool) or not isinstance(loc, int) or loc <= 0:
            return False, "读不到当前所在城市"
        citydb.note_attack_here(loc)
        plan = country_war.live_plan(
            sock, rec, loc, int(job["city_id"]), my,
            stop_on_block=bool(job.get("auto")))
    if plan.get("暂停") or not plan.get("路径"):
        return False, plan.get("暂停") or plan.get("原因") or "没有通路"
    return True, ""


def _watch_blocked_path(rec, sock, config, beater, path_at: float) -> float:
    """挂机时盯着被挡住的订单。路径一通就立刻接着打。"""
    from . import citydb

    job = citydb.next_blocked_order()
    if not job:
        return path_at
    now = time.time()
    if now - path_at < _PATH_GAP:
        return path_at
    if citydb.attack_paused():
        return now
    try:
        open_path, why = _blocked_path_open(rec, sock, config, job)
    except Exception:
        log.info("订单 %s 看路径失败", job["id"], exc_info=True)
        return time.time()
    if citydb.attack_paused():
        return time.time()
    if not open_path:
        citydb.note_blocked_reason(job["id"], why)
        log.info("订单 %s 路径还不通：%s", job["id"], why)
        return time.time()
    if citydb.attack_order_open():
        log.info("订单 %s 路径通了，先打新到的订单", job["id"])
        return 0.0
    taken = citydb.take_blocked_order(job["id"])
    if not taken:
        return time.time()
    log.info("订单 %s 路径通了，立刻接着打", taken["id"])
    with _order_account(int(taken.get("user_id") or 0)):
        _fight_claimed(rec, sock, config, beater, taken)
    citydb.set_attack_status("hold")
    return time.time()


def _refresh_asked_here(rec, sock, config) -> bool:
    """页面刚进入国战助手。读一次当前城市、士气和体力。没有请求返回 False。"""
    from . import citydb, country_war

    if not citydb.take_attack_here():
        return False
    country = _my_country(rec, config)
    if not country:
        log.info("页面要看所在城市，还没有自己的国家")
        return True
    try:
        with daily.lane_hold():
            power, loc, _times, panel = country_war._panel(sock, rec, country)
    except OSError:
        raise
    if panel is None:
        log.info("页面要看所在城市，没读到面板")
        return True
    try:
        loc = int(loc or 0)
    except (TypeError, ValueError):
        loc = 0
    name = citydb.city_name(loc) or ""
    where = f"{loc} {name}".strip() if loc else "未知"
    morale = country_war._read_path(panel, country_war.F_MORALE)
    log.info("页面刷新所在城市 %s，士气 %s，体力 %s", where, morale, power)
    return True


def _refresh_equip_bag(rec, sock) -> None:
    """页面刚进入基地战助手。读一次装备栏。没有请求就什么都不做。

    线程正忙或超过 10 秒还没读到，这次作废。再试隔 10 秒，一共最多 3 次。
    取信号和发包都在收发锁里，避免和别的路同时 recv。
    """
    from . import citydb, daily

    if not citydb.equip_bag_ready():
        return
    try:
        with daily.lane_hold():
            if not citydb.take_equip_bag():
                return
            daily.refresh_virtual_items(rec, sock)
    except OSError:
        citydb.finish_equip_bag()
        raise
    if citydb.finish_equip_bag():
        log.info("页面读了一次装备栏")
        return
    log.info("页面要看补给箱，没读到装备栏")


def _refresh_camp_live(rec, sock) -> None:
    """页面刚进入活动助手。读一次公会战役体力和积分，不进攻。"""
    from . import citydb, daily, guild

    try:
        with daily.lane_hold():
            if not citydb.take_camp_live():
                return
            guild.peek_campaign(rec, sock)
    except OSError:
        raise


def _refresh_strike_live(rec, sock) -> None:
    """页面刚进入活动助手。读一次精准打击剩余次数，不打击。"""
    from . import citydb, daily, strike

    try:
        with daily.lane_hold():
            if not citydb.take_strike_live():
                return
            strike.peek_strike(rec, sock)
    except OSError:
        raise


def _run_requested_move(rec, sock, config, beater) -> bool:
    """页面点了移动到指定城。走进那座城，不打它。没有请求返回 False。"""
    from . import citydb, country_war

    city = citydb.take_attack_move()
    if not city:
        return False
    label = f"{city} {citydb.city_name(city) or ''}".strip()
    citydb.note_attack_move_text(f"正在移动到 {label}")
    try:
        with daily.lane_hold():
            out = country_war.walk_to(
                rec, sock, config, city, beat=beater,
                march_only=True, enter_target=True)
    except OSError:
        if _storm_interrupted():
            citydb.restore_attack_move(city)
            citydb.note_attack_move_text(f"被超级强攻打断，重新登录后再移动到 {label}")
        else:
            citydb.note_attack_move_text(f"移动到 {label} 时连接中断")
        raise
    except Exception as exc:
        log.info("移动到 %s 失败", label, exc_info=True)
        detail = _interrupt_reason(exc)
        if detail.startswith("攻打中断："):
            detail = detail[len("攻打中断："):]
        citydb.note_attack_move_text(f"移动到 {label} 时：{detail}")
        return True
    why = str((out or {}).get("停止原因") or "").strip()
    landed = int((out or {}).get("走到") or 0)
    if _storm_interrupted() and landed != city:
        citydb.restore_attack_move(city)
        citydb.note_attack_move_text(f"被超级强攻打断，重新登录后再移动到 {label}")
        return True
    if why:
        citydb.note_attack_move_text(f"移动到 {label} 时停下：{why}")
    elif landed == city:
        steps = int((out or {}).get("步数") or 0)
        if steps <= 0:
            citydb.note_attack_move_text(f"人已经在 {label}")
        else:
            citydb.note_attack_move_text(f"已移动到 {label}")
    else:
        landed_name = citydb.city_name(landed) or str(landed or "")
        citydb.note_attack_move_text(
            f"朝 {label} 走了，停在 {landed} {landed_name}".strip())
    if landed > 0:
        citydb.note_attack_here(landed)
    log.info("移动到 %s 结束，停在 %s", label, landed or "未知")
    return True


# 点「推送登录二维码」之后，先挂上这么久，连接断了也在这段时间里重连。
PUSH_KEEPALIVE_MINUTES = 180
# 全天候开着时，剩余不到这么多分钟就再续一段，避免到点断开。
ALWAYS_HOLD_REFRESH_MIN = 10
ALWAYS_HOLD_WINDOW_MIN = 30


def _arm_push_keepalive() -> None:
    """推送登录之后开始保活。已经比这更长的，不把时间改短。"""
    from . import citydb

    left = citydb.attack_hold_left() or 0
    need = PUSH_KEEPALIVE_MINUTES * 60
    if left >= need:
        return
    citydb.note_attack_hold(time.time() + need)
    log.info("推送登录后挂机保活 %d 分钟", PUSH_KEEPALIVE_MINUTES)


def _begin_attack_hold(fresh=False) -> bool:
    """队列空了就按配置开始或继续挂机。该结束时返回 False。

    推送登录已经记下的剩余时间要接着挂，不因为表单里的分钟数是 0 就清掉。
    """
    from . import citydb

    minutes = citydb.attack_hold_minutes()
    always = citydb.attack_hold_always()
    left = citydb.attack_hold_left()
    if always and (left is None or left <= ALWAYS_HOLD_REFRESH_MIN * 60):
        citydb.note_attack_hold(time.time() + ALWAYS_HOLD_WINDOW_MIN * 60)
        if left is None or left <= 0:
            log.info("全天候挂机，接着保活")
            fresh = True
        left = ALWAYS_HOLD_WINDOW_MIN * 60
    if left is not None and left > 0:
        if fresh:
            citydb.set_attack_status(
                "hold" if citydb.attack_link_alive() else "login")
        return True
    if minutes <= 0:
        citydb.fail_blocked_orders()
        if left is not None:
            citydb.clear_attack_hold()
            log.info("挂机保活已关掉，攻打连接断开")
        else:
            log.info("没有下一条订单，攻打连接断开")
        return False
    if left is None:
        citydb.note_attack_hold(time.time() + minutes * 60)
        log.info("没有下一条订单，挂机保活 %d 分钟", minutes)
        fresh = True
    else:
        citydb.fail_blocked_orders()
        citydb.clear_attack_hold()
        log.info("挂机保活结束，攻打连接断开")
        return False
    if fresh:
        citydb.set_attack_status(
            "hold" if citydb.attack_link_alive() else "login")
    return True


def _campaign_end(params, user_id) -> str:
    """这一轮用的征战终点。任务里带了关卡就用那一份，否则用账号上记下的。"""
    from . import citydb

    if isinstance(params, dict) and "stages" in params:
        return str(params.get("stages") or "").strip()
    if user_id:
        return citydb.campaign_stages(user_id)
    return ""


def _run_one_daily(rec, sock, config, beater, job) -> None:
    """在这条攻打连接上做一项日常。做完写回队列，不另开连接。"""
    import copy

    from . import citydb, daily, fund, pve

    kind = str(job.get("kind") or "")
    label = citydb.DAILY_KIND_LABEL.get(kind, kind)
    params = job.get("params") or {}

    def _show(text):
        citydb.touch_daily_job(job["id"], text)
        citydb.set_attack_status("daily", task=label, note=text)
        task_log(citydb.attack_context_qq(), label, text)

    def _show_stage(stage):
        n = daily.current_campaign_round()
        if n:
            _show(f"征战世界：第{n}次 当前第 {int(stage)} 关")
        else:
            _show(f"征战世界：当前第 {int(stage)} 关")

    prev_sock = daily.bind_sock(sock)
    prev_rec = daily.bind_rec(rec)
    daily.set_campaign_progress(_show_stage)
    try:
        citydb.set_running_daily(job["id"])
        if citydb.daily_job_stopped():
            raise daily.Stopped()
        citydb.set_attack_status("daily", task=label)
        task_log(citydb.attack_context_qq(), label, "开始")
        log.info("开始做日常：%s", label)
        with daily.lane_hold():
            if rec and getattr(rec, "auto_reject", False):
                class _QQ:
                    uin = citydb.attack_context_qq()
                _arm_super_storm(rec, sock, config, _QQ())
            if kind in citydb.LOCKED_DAILY_KINDS:
                _show(citydb.RECOVERING_TEXT)
                citydb.finish_daily_job(job["id"], "failed", citydb.RECOVERING_TEXT)
                return
            if kind == "daily":
                cfg = copy.deepcopy(config)
                block = cfg.setdefault("每日任务", {})
                block["启用"] = True
                only = params.get("only") if isinstance(params, dict) else None
                if isinstance(only, str):
                    only = [only]
                if isinstance(only, list):
                    picked = [str(item).strip() for item in only if str(item or "").strip()]
                    if picked:
                        block["只做"] = picked
                user_id = citydb.attack_context_user()
                if user_id:
                    block["任务"] = citydb.daily_switches(
                        user_id, (config.get("每日任务") or {}).get("任务") or {})
                    level = citydb.guild_camp_level(user_id)
                    if level is not None:
                        camp = cfg.get("公会战役")
                        if not isinstance(camp, dict):
                            camp = {}
                            cfg["公会战役"] = camp
                        camp["敌方等级"] = level
                cfg.setdefault("征战", {})["终点"] = _campaign_end(params, user_id)
                path = daily.state_path_for_qq(citydb.attack_context_qq())
                with daily.using_state(path):
                    results, _details = daily.run(
                        rec, sock, cfg, beat=beater, on_fail=_show)
                text = daily.page_brief(results) or "这一轮没有要做的"
                citydb.set_attack_status("daily", task=label, note=text)
                citydb.finish_daily_job(job["id"], "done", text)
            elif kind == "chest":
                from . import country_war
                path = daily.state_path_for_qq(citydb.attack_context_qq())
                with daily.using_state(path):
                    ok, why = country_war.daily_chest(rec, sock, config)
                _show(why)
                citydb.finish_daily_job(job["id"], "done" if ok else "failed", why)
            elif kind == "pve":
                user_id = citydb.attack_context_user()
                raw = _campaign_end(params, user_id)
                raw_gap = (config.get("征战") or {}).get("间隔秒", 1)
                if raw_gap is None or raw_gap == "":
                    raw_gap = 1
                interval = float(raw_gap)
                path = daily.state_path_for_qq(citydb.attack_context_qq())
                with daily.using_state(path):
                    ok, why = daily.campaign_round(rec, sock, raw, interval)
                    switches = citydb.daily_switches(
                        user_id, (config.get("每日任务") or {}).get("任务") or {}
                    ) if user_id else {}
                    if daily.campaign_pushing():
                        if switches.get("征战第三次") or switches.get("征战第4次"):
                            why = f"{why}。第三次、第4次这一轮先不做"
                    else:
                        extra = {}
                        daily._run_campaign_extras(
                            rec, sock, switches, daily._load_state(), extra,
                            stages=raw, interval=interval)
                        tail = daily.page_brief(extra)
                        if tail:
                            why = f"{why}。{tail}" if why else tail
                why = daily.page_result(why)
                if not ok:
                    citydb.set_attack_status("daily", task=label, note=why)
                citydb.finish_daily_job(job["id"], "done" if ok else "failed", why)
            elif kind == "fund":
                ok, why = fund.fund(
                    rec, sock, params.get("building_id"), params.get("times") or 1,
                    on_progress=_show)
                _show(why)
                citydb.finish_daily_job(job["id"], "done" if ok else "failed", why)
            elif kind == "guild_camp":
                from . import guild
                if not citydb.base_tier(citydb.user_tier(job.get("user_id") or 0)):
                    citydb.finish_daily_job(job["id"], "failed", "活动助手需要高级pro订阅")
                    return
                cfg = copy.deepcopy(config)
                user_id = citydb.attack_context_user()
                level = citydb.guild_camp_level(user_id) if user_id else None
                if level is not None:
                    camp = cfg.get("公会战役")
                    if not isinstance(camp, dict):
                        camp = {}
                        cfg["公会战役"] = camp
                    camp["敌方等级"] = level
                ok, why = guild.daily_campaign(rec, sock, cfg)
                _show(why)
                citydb.finish_daily_job(job["id"], "done" if ok else "failed", why)
            elif kind == "strike":
                from . import strike
                if not citydb.base_tier(citydb.user_tier(job.get("user_id") or 0)):
                    citydb.finish_daily_job(job["id"], "failed", "活动助手需要高级pro订阅")
                    return
                ok, why = strike.run_strike(rec, sock, on_progress=_show)
                _show(why)
                citydb.finish_daily_job(job["id"], "done" if ok else "failed", why)
            elif kind == "guild_claim":
                from . import guild
                ok, why = guild.claim_guild_rewards(rec, sock)
                _show(why)
                citydb.finish_daily_job(job["id"], "done" if ok else "failed", why)
            elif kind == "blast":
                if not citydb.base_tier(citydb.user_tier(job.get("user_id") or 0)):
                    citydb.finish_daily_job(job["id"], "failed", "基地战需要高级pro订阅")
                    return
                ok, why = daily.daily_blast(
                    rec, sock, config, count=params.get("count") or 1,
                    on_progress=_show, done=params.get("done") or 0)
                _show(why)
                citydb.finish_daily_job(job["id"], "done" if ok else "failed", why)
            elif kind == "factory":
                if not citydb.base_tier(citydb.user_tier(job.get("user_id") or 0)):
                    citydb.finish_daily_job(job["id"], "failed", "基地战需要高级pro订阅")
                    return
                ok, why = daily.daily_factory(
                    rec, sock, config, count=params.get("count") or 1,
                    army=params.get("army") or 0, on_progress=_show,
                    done=params.get("done") or 0)
                _show(why)
                citydb.finish_daily_job(job["id"], "done" if ok else "failed", why)
            elif kind == "cards":
                if not citydb.base_tier(citydb.user_tier(job.get("user_id") or 0)):
                    citydb.finish_daily_job(job["id"], "failed", "基地战需要高级pro订阅")
                    return
                ok, why = fund.open_base_cards(
                    rec, sock, on_progress=_show, card_ids=params.get("ids"))
                _show(why)
                citydb.finish_daily_job(job["id"], "done" if ok else "failed", why)
            elif kind == "clean":
                if not citydb.base_tier(citydb.user_tier(job.get("user_id") or 0)):
                    citydb.finish_daily_job(job["id"], "failed", "基地战需要高级pro订阅")
                    return
                from . import bag_clean
                ok, why = bag_clean.clean_bag(
                    rec, sock, params.get("ids") or [], on_progress=_show)
                _show(why)
                citydb.finish_daily_job(job["id"], "done" if ok else "failed", why)
            elif kind in ("haoyin", "admiral", "admiral10"):
                if not citydb.base_tier(citydb.user_tier(job.get("user_id") or 0)):
                    citydb.finish_daily_job(job["id"], "failed", "基地战需要高级pro订阅")
                    return
                ok, why = daily.draw_probe(
                    rec, sock, kind, count=params.get("count") or 1,
                    on_progress=_show, by_card=True)
                daily.note_bag_cards(rec)
                _show(why)
                citydb.finish_daily_job(job["id"], "done" if ok else "failed", why)
            elif kind in ("box_mid", "box_high"):
                if not citydb.base_tier(citydb.user_tier(job.get("user_id") or 0)):
                    citydb.finish_daily_job(job["id"], "failed", "基地战需要高级pro订阅")
                    return
                ok, why = daily.open_supply_box(
                    rec, sock, kind, count=params.get("count") or 10,
                    on_progress=_show)
                daily.note_bag_cards(rec)
                _show(why)
                citydb.finish_daily_job(job["id"], "done" if ok else "failed", why)
            elif kind in ("pick_general", "pick_staff", "pick_lab", "pick_auto"):
                if not citydb.base_tier(citydb.user_tier(job.get("user_id") or 0)):
                    citydb.finish_daily_job(job["id"], "failed", "基地战需要高级pro订阅")
                    return
                ok, why = daily.pick_pack(
                    rec, sock, kind.split("_", 1)[1], count=params.get("count") or 1,
                    on_progress=_show)
                daily.note_bag_cards(rec)
                _show(why)
                citydb.finish_daily_job(job["id"], "done" if ok else "failed", why)
            elif kind == "auto50":
                if not citydb.base_tier(citydb.user_tier(job.get("user_id") or 0)):
                    citydb.finish_daily_job(job["id"], "failed", "基地战需要高级pro订阅")
                    return
                ok, why = daily.auto50_probe(
                    rec, sock, times=params.get("count") or 1, on_progress=_show)
                daily.note_bag_cards(rec)
                _show(why)
                citydb.finish_daily_job(job["id"], "done" if ok else "failed", why)
            elif kind == "lab50":
                if not citydb.base_tier(citydb.user_tier(job.get("user_id") or 0)):
                    citydb.finish_daily_job(job["id"], "failed", "基地战需要高级pro订阅")
                    return
                ok, why = daily.lab50_probe(
                    rec, sock, times=params.get("count") or 1, on_progress=_show)
                daily.note_bag_cards(rec)
                _show(why)
                citydb.finish_daily_job(job["id"], "done" if ok else "failed", why)
            else:
                citydb.finish_daily_job(job["id"], "failed", "没有这项日常")
                task_log(citydb.attack_context_qq(), label, "没有这项日常")
    except daily.Later as wait:
        params = dict(job.get("params") or {})
        params["done"] = int(wait.done or 0)
        _show(wait.text)
        citydb.defer_daily_job(job["id"], wait.delay, wait.text, params)
        log.info("%s 排到 %d 秒后再做", label, int(wait.delay))
    except daily.Stopped:
        if citydb.shutdown_due():
            citydb.shelve_shutdown_work()
            task_log(citydb.attack_context_qq(), label, "定时关闭，停下")
            log.info("定时关闭，日常停下：%s", label)
        else:
            task_log(citydb.attack_context_qq(), label, "已手动关停")
            log.info("日常已手动关停：%s", label)
    except OSError:
        task_log(citydb.attack_context_qq(), label, "连接中断")
        citydb.set_attack_status("daily", task=label, note="连接中断")
        if kind == "daily":
            citydb.touch_daily_job(job["id"], "连接中断，重连后再做")
        else:
            citydb.finish_daily_job(job["id"], "failed", "连接中断")
        raise
    except Exception as exc:
        log.info("日常没做成", exc_info=True)
        task_log(citydb.attack_context_qq(), label, f"没做成：{exc}")
        citydb.set_attack_status("daily", task=label, note=str(exc))
        citydb.finish_daily_job(job["id"], "failed", str(exc))
    else:
        task_log(citydb.attack_context_qq(), label, "这一项结束")
        log.info("日常做完：%s", label)
    finally:
        if (_storm_interrupted() and not citydb.daily_job_stopped()
                and not citydb.shutdown_due()):
            citydb.reopen_interrupted_daily(job["id"])
        citydb.set_running_daily(0)
        daily.set_campaign_progress(None)
        daily.bind_rec(prev_rec)
        daily.bind_sock(prev_sock)


def _shutdown_stop() -> bool:
    """定时关闭到了，这条线程上的账号都要下线。还在做的任务先放回排队。"""
    from . import citydb

    ids = citydb.attack_context_users()
    if not ids or not any(citydb.shutdown_due(uid) for uid in ids):
        return False
    citydb.shelve_shutdown_work(ids)
    if not citydb.shutdown_covers_context():
        return False
    citydb.clear_attack_hold()
    citydb.set_attack_status("offline")
    log.info("定时关闭到点，账号下线，不再执行任务")
    return True


def _attack_orders(rec, sock, spec, ctx, beater, config) -> int:
    """同一条游戏连接上处理国战、日常、兵营、工厂和开卡。

    兵营和战车工厂按剩余时间排下次加速，到点再领。心跳在会话脉冲里发。
    """
    from . import citydb

    stop = threading.Event()
    prev_stop = daily.bind_stop(stop)
    prev_sock = daily.bind_sock(sock)
    prev_rec = daily.bind_rec(rec)
    citydb.set_lane("war")
    stated = False
    path_box = [0.0]
    hold_closed = False
    side_kinds = (
        "blast", "factory",
        "daily", "pve", "fund", "guild_camp", "guild_claim", "strike", "chest", "cards", "clean",
        "haoyin", "admiral", "admiral10", "box_mid", "box_high",
        "pick_general", "pick_staff", "pick_lab", "pick_auto", "lab50", "auto50",
    )

    def _lane_for(kind):
        if kind in ("daily", "pve", "fund", "guild_camp", "guild_claim", "strike", "chest"):
            return "daily"
        if kind == "blast":
            return "blast"
        if kind == "factory":
            return "factory"
        return "cards"

    def _jobs_open():
        return (citydb.daily_job_open() or citydb.attack_order_open()
                or citydb.attack_move_pending() > 0
                or citydb.attack_here_pending())

    def _pump():
        """冷却空档。到点的空城改回排队；剩下的时间还够，就先打一单国战。"""
        if citydb.attack_paused():
            return False
        try:
            citydb.release_due_waits()
        except sqlite3.Error:
            log.info("到点的空城订单没改回排队", exc_info=True)
            return False
        if _war_depth() or getattr(_sched_local, "pumping", False):
            return False
        left = daily.soonest_wait()
        if left is not None and left < _PUMP_MIN:
            return False
        _sched_local.pumping = True
        try:
            try:
                job = citydb.claim_attack_order()
            except sqlite3.Error:
                log.info("冷却空档领国战没领成", exc_info=True)
                return False
            if job:
                log.info("冷却空档领到订单 %s，城市 %s UID %s",
                         job["id"], job["city_id"], job["uid"])
                with _order_account(int(job.get("user_id") or 0)):
                    _fight_claimed(rec, sock, config, beater, job)
                return True
            try:
                if citydb.attack_order_open():
                    return False
            except sqlite3.Error:
                log.info("冷却空档看国战队列没看成", exc_info=True)
                return False
            path_box[0] = _watch_blocked_path(
                rec, sock, config, beater, path_box[0])
            return False
        finally:
            _sched_local.pumping = False

    prev_pump = daily.bind_idle_pump(_pump)
    prev_recv = daily.bind_on_recv(
        lambda data: _reply_online(sock, spec, ctx, data))
    try:
        while not stop.is_set():
            try:
                if _shutdown_stop():
                    return 0
                daily.note_blast_count(rec)
                daily.note_population(rec)
                daily.note_base_wallet(rec)
                daily.note_bag_cards(rec)
                if citydb.attack_paused():
                    asked = citydb.take_attack_login()
                    if asked:
                        _arm_push_keepalive()
                        if not citydb.attack_link_alive():
                            citydb.set_attack_paused(False)
                            log.info("点了推送登录，游戏没挂上，准备重连")
                            return 0
                    citydb.set_attack_status("paused")
                    _refresh_asked_here(rec, sock, config)
                    _refresh_equip_bag(rec, sock)
                    _refresh_camp_live(rec, sock)
                    _refresh_strike_live(rec, sock)
                    if not _wait_socket(sock, spec, ctx):
                        raise OSError("服务器关闭连接")
                    continue
                asked = citydb.take_attack_login()
                if asked:
                    _arm_push_keepalive()
                if (hold_closed and not citydb.attack_hold_on()
                        and not _jobs_open() and not citydb.attack_paused()):
                    return 0
                if _run_requested_move(rec, sock, config, beater):
                    stated = False
                    continue
                if _refresh_asked_here(rec, sock, config):
                    continue
                _refresh_equip_bag(rec, sock)
                _refresh_camp_live(rec, sock)
                _refresh_strike_live(rec, sock)
                for uid in citydb.attack_context_users():
                    citydb.skip_unfinished_auto(uid)
                citydb.release_due_waits()
                citydb.set_lane("war")
                job = citydb.claim_attack_order()
                if job:
                    stated = False
                    hold_closed = False
                    citydb.clear_attack_hold()
                    log.info("领到订单 %s，城市 %s UID %s",
                             job["id"], job["city_id"], job["uid"])
                    with _order_account(int(job.get("user_id") or 0)):
                        _fight_claimed(rec, sock, config, beater, job)
                    continue
                if citydb.attack_order_open():
                    hold_closed = False
                    citydb.set_attack_status("queue")
                    if not _wait_socket(sock, spec, ctx):
                        raise OSError("服务器关闭连接")
                    continue
                side = citydb.claim_daily_job(side_kinds)
                if side:
                    stated = False
                    hold_closed = False
                    citydb.set_lane(_lane_for(side.get("kind")))
                    with _order_account(int(side.get("user_id") or 0)):
                        _run_one_daily(rec, sock, config, beater, side)
                    citydb.set_attack_status("hold")
                    citydb.set_lane("war")
                    continue
                if not citydb.attack_hold_on() and _jobs_open():
                    if not _wait_socket(sock, spec, ctx):
                        raise OSError("服务器关闭连接")
                    continue
                if citydb.attack_hold_on():
                    hold_closed = False
                if hold_closed:
                    if not _wait_socket(sock, spec, ctx):
                        raise OSError("服务器关闭连接")
                    continue
                if not _begin_attack_hold(fresh=not stated):
                    hold_closed = True
                    if not _wait_socket(sock, spec, ctx):
                        raise OSError("服务器关闭连接")
                    continue
                if not _jobs_open() and not citydb.attack_link_alive():
                    log.info("挂机保活游戏心跳没了，准备重连")
                    return 0
                if not stated:
                    path_box[0] = 0.0
                stated = True
                # 空挂只心跳、等订单。打开国战助手时另读一次所在城市。扫描号自己翻城。
                path_box[0] = _watch_blocked_path(
                    rec, sock, config, beater, path_box[0])
                if not _wait_socket(sock, spec, ctx):
                    log.info("挂机时游戏连接断了，准备重连")
                    return 0
            except sqlite3.Error as exc:
                log.info("读库失败，游戏先不断开：%s", exc)
                if not _wait_socket(sock, spec, ctx):
                    log.info("攻打连接断了，准备重连")
                    return 0
            except OSError:
                log.info("攻打连接断了，准备重连")
                return 0
    finally:
        stop.set()
        daily.bind_on_recv(prev_recv)
        daily.bind_idle_pump(prev_pump)
        daily.bind_rec(prev_rec)
        daily.bind_sock(prev_sock)
        daily.bind_stop(prev_stop)


def _connect_attack_orders(qq, config) -> int:
    def _work(rec, sock, spec, ctx, beater):
        return _attack_orders(rec, sock, spec, ctx, beater, config)

    return _connect_and(qq, config, _work)


def _bind_named_account(config: dict, name: str) -> int:
    """配置里写了「用户」就用那个登录账号。名字是 attack-数字 时用这条映射。
    都没有、而且只有一个登录账号绑了攻打 QQ 时，才用那一个。"""
    from . import citydb

    spec = ((config.get("登录") or {}).get("账号") or {}).get(name) or {}
    if isinstance(spec, dict):
        who = str(spec.get("用户") or "").strip()
        if who:
            user_id = citydb.user_id_by_name(who)
            if not user_id:
                log.error("攻打号「%s」写的用户「%s」不存在", name, who)
                return 0
            return user_id
    if name.startswith("qq-") and name[3:].isdigit():
        owner_id, _owner = citydb.attack_qq_owner(name[3:])
        return owner_id
    if name.startswith("attack-") and name[7:].isdigit():
        return int(name[7:])
    mapped = citydb.mapped_attack_user_ids()
    if len(mapped) == 1:
        return mapped[0]
    return 0


def run_remote_orders(qq, config: dict) -> int:
    """领取页面上中级、高级提交的城市和 UID。没登录就先把二维码发给扫码 QQ。
    这个攻打 QQ 绑了几个登录账号，就领这几个的订单。"""
    from . import citydb

    name = getattr(qq, "account_name", "") or ""
    user_id = _bind_named_account(config, name)
    told = False
    storm_retry = False
    storm_started = 0.0
    while not user_id:
        if not told:
            log.error("攻打号「%s」还没绑定登录账号，不领订单。扫码后会记下 QQ，或执行：python3 web.py --bind-attack 用户名 QQ号",
                      name or "攻打号")
            told = True
        time.sleep(5)
        user_id = _bind_named_account(config, name)
    citydb.set_attack_context(user_id, citydb.attack_qq_of(user_id))
    who = "、".join(citydb.username_of(i) for i in citydb.attack_context_users()) or citydb.username_of(user_id)
    log.info("开始领取远程扫码攻打，攻打号「%s」打登录账号 %s 的订单", name, who)
    pulse = _start_pulse(qq, attack=True)
    login_checked = [0.0]

    def _skey_alive() -> bool:
        now = time.time()
        for c in qq.session.cookies:
            if getattr(c, "name", "") != "skey" or not c.value:
                continue
            exp = getattr(c, "expires", None)
            if not exp:
                return True
            try:
                if float(exp) > now:
                    return True
            except (TypeError, ValueError):
                return True
        return False

    def _idle_needs_login() -> bool:
        """空闲时没有游戏连接。票据没了就是掉线，要重新扫码。票据还在时，一分钟问一次游戏。"""
        if not _skey_alive():
            return True
        now = time.time()
        if now - login_checked[0] < 60:
            return False
        login_checked[0] = now
        return not qq.is_valid()

    try:
        if citydb.manual_orders_running():
            citydb.mark_attack_resume()
            log.info("登录账号 %s 有订单还在打，重新登录接着做", who)
        citydb.resume_stranded_orders()
        citydb.resume_daily_jobs()
        while True:
            current = citydb.attack_context_user()
            if current:
                user_id = current
            if _shutdown_stop():
                time.sleep(5)
                continue
            if citydb.attack_paused():
                citydb.set_attack_status("paused")
                blocked = any(
                    citydb.attack_qq_blocked(uid) for uid in citydb.attack_context_users())
                if blocked and citydb.take_attack_login():
                    citydb.set_attack_status("login")
                    relogin_with_push(qq, config, force_qr=True)
                    continue
                time.sleep(5)
                continue
            citydb.resume_stranded_orders()
            citydb.resume_daily_jobs()
            citydb.release_due_waits()
            asked = citydb.take_attack_login()
            resumed = citydb.attack_resume_marked()
            left = citydb.attack_hold_left()
            moving = citydb.attack_move_pending() > 0
            daily_wait = citydb.daily_job_open()
            locating = citydb.attack_here_pending()
            # 新订单本身不登录。服务器停前正在打的那一单要接着做。
            # 打开国战助手要读一次所在城市，没挂着也先连上读完再断开。
            need = (asked or resumed or moving or daily_wait or locating
                    or citydb.attack_hold_on())
            if need and not qq.is_valid():
                citydb.set_attack_status("login")
                relogin_with_push(qq, config)
                if not citydb.attack_hold_on():
                    citydb.set_attack_status("idle")
                left = citydb.attack_hold_left()
                moving = citydb.attack_move_pending() > 0
                daily_wait = citydb.daily_job_open()
                locating = citydb.attack_here_pending()
                resumed = citydb.attack_resume_marked()
                need = (asked or resumed or moving or daily_wait or locating
                        or citydb.attack_hold_on())
            if not need:
                citydb.fail_blocked_orders()
                if left is not None:
                    citydb.clear_attack_hold()
                if _idle_needs_login():
                    citydb.set_attack_status("login")
                    owner = citydb.attack_context_user()
                    citydb.set_page_qr(True, owner)
                    try:
                        relogin_with_push(qq, config)
                    finally:
                        citydb.set_page_qr(False, owner)
                    login_checked[0] = time.time()
                    continue
                citydb.set_attack_status("idle")
                time.sleep(5)
                continue
            if storm_retry and _storm_retry_expired(storm_started):
                _stop_storm_relogin(str(getattr(qq, "uin", "") or ""))
                return 0
            _connect_attack_orders(qq, config)
            dropped = _take_storm_drop()
            keep, storm_started, timed_out = _storm_retry_decision(
                dropped, storm_retry, _game_online(), storm_started, time.monotonic())
            if timed_out:
                _stop_storm_relogin(str(getattr(qq, "uin", "") or ""))
                return 0
            if keep:
                storm_retry = True
                citydb.set_attack_status("login")
                log.info("被超级强攻打断，%d 秒后重新登录并继续任务",
                         STORM_RETRY_SEC)
                time.sleep(STORM_RETRY_SEC)
                continue
            storm_retry = False
            if resumed:
                citydb.clear_attack_resume()
            if not citydb.attack_hold_on():
                citydb.fail_blocked_orders()
                if citydb.daily_job_open() or citydb.attack_move_pending() > 0:
                    time.sleep(2)
                else:
                    citydb.set_attack_status("idle")
            elif citydb.attack_order_open():
                time.sleep(1)
            else:
                time.sleep(5)
    except KeyboardInterrupt:
        log.info("停止领取远程扫码攻打")
        return 0
    finally:
        _stop_pulse(pulse)


_PAGE_ACCOUNT = "网页"
_PAGE_COOKIE = "accounts/web.json"


def _qr_file(cookie: str) -> str:
    from . import paths
    base = paths.user_path(cookie)
    return (base[:-5] if base.endswith(".json") else base) + ".qrcode.png"


def page_attack_qr_path(config: dict = None, user_id: int = 0) -> str:
    """网页上的登录图。还没绑定的用自己的网页二维码。已经绑定的只看这个攻打 QQ 的图。"""
    from . import citydb
    user_id = int(user_id or 0)
    mine = citydb.page_login_path(user_id)
    if mine:
        return mine
    shared = citydb.login_qr_path(user_id)
    if shared:
        return shared
    uin = citydb.attack_qq_of(user_id) if user_id else ""
    cookie = f"accounts/qq-{uin}.json" if uin else _PAGE_COOKIE
    if user_id:
        username = citydb.username_of(user_id)
        accounts = ((config or {}).get("登录") or {}).get("账号") or {}
        if isinstance(accounts, dict):
            for spec in accounts.values():
                if not isinstance(spec, dict):
                    continue
                if str(spec.get("用户") or "").strip() != username:
                    continue
                chosen = str(spec.get("cookie") or "").strip()
                if chosen:
                    cookie = chosen
                    break
    return _qr_file(cookie)


def _page_qr_account(config: dict, name: str) -> bool:
    login = config.get("登录") or {}
    if str(login.get("网页攻打号") or "").strip():
        return False
    spec = (login.get("账号") or {}).get(name) or {}
    if not isinstance(spec, dict):
        return False
    cookie = str(spec.get("cookie") or "").strip()
    own = (cookie == _PAGE_COOKIE or cookie.startswith("accounts/attack-")
           or cookie.startswith("accounts/qq-"))
    return own and not str(spec.get("扫码QQ") or "").strip()


def attack_account_for_user(config: dict, user_id: int) -> tuple:
    """这个登录账号对应哪一份攻打票据。返回 (名字, 错误)。
    配置里写了「用户」就用那一份，否则用这个登录账号自己的映射。"""
    from . import citydb

    user_id = int(user_id or 0)
    login = config.setdefault("登录", {})
    accounts = login.get("账号")
    if not isinstance(accounts, dict):
        accounts = {}
        login["账号"] = accounts
    names = [k for k, v in accounts.items() if isinstance(v, dict)]
    username = citydb.username_of(user_id)
    for name in names:
        spec = accounts.get(name) or {}
        who = str(spec.get("用户") or "").strip()
        if who and who == username:
            return name, ""
    uin = citydb.attack_qq_of(user_id)
    if not uin:
        log.info("登录账号 %s 还没有攻打 QQ", username)
        return "", "unbound"
    # 攻打号以 QQ 号为索引。cookie 和配置都挂在这个号上。
    slot = f"qq-{uin}"
    accounts.setdefault(slot, {"cookie": f"accounts/{slot}.json"})
    return slot, ""


def web_attack_account(config: dict) -> str:
    """网页触发时用哪个攻打号。填了「登录.网页攻打号」就用它，否则用账号里的第一个。
    一个都没写时，用 accounts/web.json，二维码显示在网页上。"""
    login = config.setdefault("登录", {})
    accounts = login.get("账号")
    if not isinstance(accounts, dict):
        accounts = {}
        login["账号"] = accounts
    names = [k for k, v in accounts.items() if isinstance(v, dict)]
    named = str(login.get("网页攻打号") or "").strip()
    if named:
        return named if named in names else ""
    if names:
        return names[0]
    accounts[_PAGE_ACCOUNT] = {"cookie": _PAGE_COOKIE}
    return _PAGE_ACCOUNT


_page_login_guard = threading.Lock()
_page_login_users = set()
_wanted_platform = {}
_wanted_platform_guard = threading.Lock()


def remember_login_platform(user_id: int = 0, uin: str = "", platform: str = "") -> None:
    """网页点登录时选的空间/大厅，留给随后的扫码会话。"""
    from . import normalize_platform
    plat = str(platform or "").strip()
    if not plat:
        return
    plat = normalize_platform(plat)
    with _wanted_platform_guard:
        if int(user_id or 0):
            _wanted_platform[f"user-{int(user_id)}"] = plat
        if str(uin or "").strip():
            _wanted_platform[str(uin).strip()] = plat


def consume_login_platform(user_id: int = 0, uin: str = "", qq=None) -> str:
    """取出这次要走的平台，写到 QQSession 上。"""
    plat = ""
    with _wanted_platform_guard:
        if str(uin or "").strip():
            plat = _wanted_platform.pop(str(uin).strip(), "") or plat
        if int(user_id or 0):
            plat = _wanted_platform.pop(f"user-{int(user_id)}", "") or plat
    if plat and qq is not None:
        qq.set_platform(plat)
    return plat


def attack_cookie_file(uin: str) -> str:
    """攻打票据按 QQ 号放。"""
    from . import paths
    return paths.user_path(f"accounts/qq-{str(uin).strip()}.json")


def _store_attack_cookie(user_id: int, src: str, uin: str = "") -> None:
    """扫上之后，把这次登录票据留在这个攻打 QQ 自己的文件里。"""
    import os

    from . import citydb, paths
    uin = str(uin or citydb.attack_qq_of(user_id) or "").strip()
    if not uin.isdigit():
        return
    dest = attack_cookie_file(uin)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if os.path.abspath(src) == os.path.abspath(dest):
        return
    os.replace(src, dest)
    old = paths.user_path(f"accounts/attack-{int(user_id)}.json")
    if os.path.abspath(old) != os.path.abspath(dest) and os.path.isfile(old):
        try:
            os.remove(old)
        except OSError:
            pass


def _adopt_old_cookie(user_id: int, uin: str) -> None:
    """以前按登录账号放的票据，挪到 QQ 号这份上。"""
    import os

    from . import paths
    dest = attack_cookie_file(uin)
    if os.path.isfile(dest):
        return
    old = paths.user_path(f"accounts/attack-{int(user_id)}.json")
    if not os.path.isfile(old):
        return
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    os.replace(old, dest)


def _page_login_cookie(user_id: int) -> str:
    import os

    from . import paths
    folder = paths.user_path("accounts")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, f"page-login-{int(user_id)}.json")


def start_unbound_page_qr(config: dict, user_id: int, platform: str = "") -> str:
    """还没绑定攻打 QQ 时，只在网页上出二维码。不读、不拉、不改正在跑的攻打进程。
    扫上并记到这个登录账号之后，立刻拉起这个 QQ 的挂机保活。"""
    import os

    from . import citydb
    from .qq_login import QQSession, try_lock

    user_id = int(user_id)
    with _page_login_guard:
        if user_id in _page_login_users:
            return "qr"
        _page_login_users.add(user_id)
    cookie = _page_login_cookie(user_id)
    qr = (cookie[:-5] if cookie.endswith(".json") else cookie) + ".qrcode.png"
    try:
        os.remove(cookie)
    except OSError:
        pass
    try:
        lockf = open(cookie + ".lock", "a+")
    except OSError:
        lockf = None
    if lockf is None or not try_lock(lockf):
        if lockf is not None:
            lockf.close()
        with _page_login_guard:
            _page_login_users.discard(user_id)
        return "qr"
    # 新图写好之前，页面上不留上一张。
    citydb.clear_page_login(user_id)
    try:
        os.remove(qr)
    except OSError:
        pass
    qq = QQSession(cookie, qrcode_file=qr)
    from . import config_platform
    plat = str(platform or "").strip() or config_platform(config)
    qq.set_platform(plat)
    remember_login_platform(user_id, "", plat)
    who = citydb.username_of(user_id)

    def on_page_qr(path, pushed=False):
        citydb.set_page_login(user_id, path)
        log.info("登录账号 %s 还没有攻打 QQ，二维码在网页上：%s", who, path)

    def _run():
        nonlocal cookie
        bound = ""
        limit_key = f"user:{user_id}"
        at = _current_limit(limit_key)
        if not at or time.monotonic() >= at:
            _reset_login_limit(limit_key)
        try:
            while not citydb.attack_qq_of(user_id):
                if _login_limit_hit(limit_key):
                    if _abandon_login(limit_key, user_id):
                        log.info("登录账号 %s 超过 10 分钟还没扫上，关掉这次登录", who)
                        break
                until = _login_until_wall(limit_key)
                left = 180 if not until else max(1, int(until - time.time()))
                if qq.qr_login(on_qr=on_page_qr, timeout_sec=min(180, left), until=until):
                    uin = str(getattr(qq, "uin", "") or "").strip()
                    if uin.isdigit() and citydb.confirm_attack_qq(user_id, uin):
                        _store_attack_cookie(user_id, cookie, uin)
                        remember_login_platform(user_id, uin, plat)
                        log.info("登录账号 %s 扫码绑定攻打 QQ %s（%s）", who, uin, plat)
                        cookie = ""
                        bound = uin
                        citydb.clear_login_expired(user_id)
                        citydb.note_attack_logging_in(user_id)
                    break
                if _login_limit_hit(limit_key) and _abandon_login(limit_key, user_id):
                    log.info("登录账号 %s 超过 10 分钟还没扫上，关掉这次登录", who)
                    break
                until = _login_until_wall(limit_key)
                pause = 15.0 if not until else min(15.0, max(0.0, until - time.time()))
                if pause > 0:
                    time.sleep(pause)
        finally:
            citydb.clear_page_login(user_id)
            if cookie:
                try:
                    os.remove(cookie)
                except OSError:
                    pass
            try:
                lockf.close()
            except OSError:
                pass
            with _page_login_guard:
                _page_login_users.discard(user_id)
        if bound:
            kick_attack_login(config, user_id, claim=True)

    threading.Thread(target=_run, name=f"page-login-{user_id}", daemon=True).start()
    return "qr"


_pool_guard = threading.Lock()
_pool = {}
_live_guard = threading.Lock()
_live_socks = {}


def _live_key(user_id=0) -> int:
    from . import citydb
    return int(user_id or citydb.attack_context_user() or 0)


def _register_live_sock(sock) -> None:
    key = _live_key()
    if not key:
        return
    with _live_guard:
        _live_socks[key] = sock


def _drop_live_sock(user_id=0) -> bool:
    """掐掉这条攻打号当前的游戏连接。心跳卡死时点推送登录也要能重连。"""
    key = _live_key(user_id)
    if not key:
        return False
    with _live_guard:
        sock = _live_socks.pop(key, None)
    if sock is None:
        return False
    _force_close_socket(sock)
    return True


def attack_worker_alive(uin: str) -> bool:
    with _pool_guard:
        th = _pool.get(str(uin or ""))
        return bool(th and th.is_alive())


def _attack_worker(config: dict, user_id: int, uin: str, name: str, on_page: bool) -> None:
    """一个攻打 QQ 一条线程。这个 QQ 绑了几个登录账号就打这几个的订单，状态写在这个 QQ 上。"""
    import main as cli

    from . import citydb
    citydb.set_attack_context(user_id, uin)
    limit_key = str(uin or "").strip() or f"user:{user_id}"
    seen_gen = None
    _adopt_old_cookie(user_id, uin)
    qq = cli.open_qq(config, name, fatal_lock=False)
    if qq is None:
        log.info("攻打 QQ %s 已有进程在用这份票据", uin)
        with _pool_guard:
            if _pool.get(uin) is threading.current_thread():
                _pool.pop(uin, None)
        return
    qq.page_qr = on_page
    consume_login_platform(user_id, uin, qq)
    who = "、".join(
        citydb.username_of(i) for i in citydb.attack_context_users()
    ) or citydb.username_of(user_id)
    log.info("攻打 QQ %s 的线程已启动，打登录账号 %s 的订单", uin, who)

    def _owner() -> int:
        return citydb.attack_context_user() or user_id

    pulse = None
    try:
        _drop_expired_limit()
        with _login_limits_guard:
            seen_gen = _login_gen.get(limit_key, 0)
        bind_login_session(qq)
        pulse = _start_pulse(qq, attack=True)
        if citydb.manual_orders_running():
            citydb.mark_attack_resume()
            log.info("登录账号 %s 有订单还在打，重新登录接着做", who)
        citydb.resume_stranded_orders()
        citydb.resume_daily_jobs()
        valid = qq.is_valid()
        if valid:
            note_attack_qq(qq)
            qq.note_login("攻打线程启动，票据仍有效。%s。%s",
                          _login_context(), qq.ticket_brief())
        scope_blocked = any(
            citydb.attack_qq_blocked(uid) for uid in citydb.attack_context_users())
        just_scanned = False
        storm_retry = False
        storm_started = 0.0
        if scope_blocked or not valid:
            citydb.set_attack_status("login")
            _arm_login_limit()
            if on_page:
                citydb.set_page_qr(True, user_id)
            ok = relogin_with_push(qq, config, force_qr=scope_blocked)
            if on_page:
                citydb.set_page_qr(False, user_id)
            if ok:
                just_scanned = True
            elif _give_up_login(uin):
                return
        while True:
            try:
                if _shutdown_stop():
                    break
                if citydb.attack_paused():
                    citydb.set_attack_status("paused")
                    if citydb.take_attack_login():
                        if any(citydb.attack_qq_blocked(uid)
                               for uid in citydb.attack_context_users()):
                            citydb.set_attack_status("login")
                            _arm_login_limit()
                            if on_page:
                                citydb.set_page_qr(True, user_id)
                            ok = relogin_with_push(qq, config, force_qr=True)
                            if on_page:
                                citydb.set_page_qr(False, user_id)
                            if not ok and _give_up_login(uin):
                                break
                            continue
                        _arm_push_keepalive()
                        if not citydb.attack_in_keepalive(_owner()):
                            citydb.set_attack_paused(False, _owner())
                            citydb.set_attack_status("login")
                            _arm_login_limit()
                            if _login_limit_hit() and _give_up_login(uin):
                                break
                            _connect_attack_orders(qq, config)
                            if (not citydb.attack_in_keepalive(_owner())
                                    and _login_limit_hit() and _give_up_login(uin)):
                                break
                            continue
                    time.sleep(5)
                    continue
                citydb.resume_stranded_orders()
                citydb.resume_daily_jobs()
                citydb.release_due_waits()
                # 新订单本身不登录。先推送登录挂上游戏，这条连接再领单。
                # 服务器停前正在打的那一单要接着做，不另加挂机时间。
                # 点了推送登录就先记 180 分钟。一直停在登录中超过 10 分钟就关掉线程。
                asked = citydb.take_attack_login()
                resumed = citydb.attack_resume_marked()
                if asked:
                    _arm_push_keepalive()
                    if citydb.attack_in_keepalive(_owner()):
                        citydb.set_attack_status("hold")
                    else:
                        citydb.set_attack_status("login")
                holding = citydb.attack_hold_on()
                if not (asked or resumed or holding or citydb.daily_job_open()
                        or citydb.attack_here_pending()):
                    break
                if storm_retry and _storm_retry_expired(storm_started):
                    _stop_storm_relogin(uin)
                    break
                if just_scanned:
                    just_scanned = False
                elif storm_retry or citydb.attack_in_keepalive(_owner()):
                    # 已经挂上游戏的，按原来的挂机时间走，不套这 10 分钟。
                    # 被超级打断后的重登也不套这 10 分钟，30 分钟还没登上再停。
                    _clear_login_limit()
                else:
                    _arm_login_limit()
                    if _login_limit_hit() and _give_up_login(uin):
                        break
                _connect_attack_orders(qq, config)
                dropped = _take_storm_drop()
                keep, storm_started, timed_out = _storm_retry_decision(
                    dropped, storm_retry, _game_online(), storm_started, time.monotonic())
                if timed_out:
                    _stop_storm_relogin(uin)
                    break
                if keep:
                    storm_retry = True
                    citydb.set_attack_status("login")
                    log.info("被超级强攻打断，%d 秒后重新登录并继续任务",
                             STORM_RETRY_SEC)
                    time.sleep(STORM_RETRY_SEC)
                    continue
                storm_retry = False
                if (not citydb.attack_in_keepalive(_owner())
                        and _login_limit_hit() and _give_up_login(uin)):
                    break
                if resumed:
                    citydb.clear_attack_resume()
                if citydb.attack_hold_on():
                    time.sleep(1 if citydb.attack_order_open() else 5)
                elif citydb.daily_job_open():
                    time.sleep(2)
            except sqlite3.Error as exc:
                log.info("读库失败，攻打线程先留着：%s", exc)
                time.sleep(1)
                continue
    finally:
        if on_page:
            citydb.set_page_qr(False, user_id)
        _stop_pulse(pulse)
        citydb.set_attack_context(0, "")
        cli.release_qq_lock(getattr(qq, "cookie_file", "") or "")
        unbind_login_session(qq)
        with _pool_guard:
            if _pool.get(uin) is threading.current_thread():
                _pool.pop(uin, None)
        if seen_gen is not None:
            with _login_limits_guard:
                again = _login_gen.get(limit_key, 0) != seen_gen
            if again:
                try:
                    kick_attack_login(config, user_id, claim=True)
                except Exception:
                    log.info("重新点登录后没能再拉起", exc_info=True)


def kick_attack_login(config: dict, user_id: int = 0, claim: bool = False,
                      platform: str = "") -> str:
    """网页或主进程拉起：每个攻打 QQ 各有一条线程。
    还没绑定、并且是本人点了推送登录时，只在网页上出二维码，不碰任何攻打线程。"""
    from . import citydb
    user_id = int(user_id or 0)
    if not user_id:
        return "no_account"
    if citydb.shutdown_due(user_id) and not claim:
        log.info("登录账号 %s 已定时关闭，不再自动上线", citydb.username_of(user_id))
        return "off"
    if claim and citydb.shutdown_due(user_id):
        citydb.set_shutdown_at(user_id, "")
        log.info("登录账号 %s 点了登录，定时关闭取消", citydb.username_of(user_id))
    if not claim and citydb.login_expired(user_id):
        return "timeout"
    if claim:
        citydb.clear_login_expired(user_id)
        bound = str(citydb.attack_qq_of(user_id) or "").strip()
        _reset_login_limit(bound or f"user:{user_id}")
        remember_login_platform(user_id, bound, platform)
    if claim and not citydb.attack_qq_of(user_id):
        log.info("登录账号 %s 还没有攻打 QQ，二维码直接显示在网页上",
                 citydb.username_of(user_id))
        return start_unbound_page_qr(config, user_id, platform=platform)
    name, why = attack_account_for_user(config, user_id)
    if why:
        if why == "taken":
            log.info("登录账号 %s 不能使用别人的攻打号", citydb.username_of(user_id))
        elif why == "unbound":
            log.info("登录账号 %s 还没绑定攻打号", citydb.username_of(user_id))
        else:
            log.error("登录.账号 里没有可以给 %s 用的攻打号", citydb.username_of(user_id))
        return why
    uin = citydb.attack_qq_of(user_id)
    on_page = _page_qr_account(config, name)
    with _pool_guard:
        running = _pool.get(uin)
        if running and running.is_alive():
            if (citydb.attack_paused(user_id) and not citydb.attack_qq_blocked(user_id)
                    and citydb.attack_in_keepalive(user_id)):
                log.info("攻打 QQ %s 已在保活里暂停，先不拉起", uin)
                return "paused"
            if claim:
                citydb.ask_attack_login(user_id)
            if not citydb.attack_in_keepalive(user_id):
                citydb.note_attack_logging_in(user_id)
                if on_page:
                    citydb.set_page_qr(True, user_id)
                if _drop_live_sock(user_id):
                    log.info("攻打 QQ %s 游戏没挂上，掐掉旧连接准备重连", uin)
            log.info("攻打 QQ %s 的线程已在跑", uin)
            return "busy"
        if claim:
            citydb.ask_attack_login(user_id)
        citydb.note_attack_logging_in(user_id)
        th = threading.Thread(
            target=_attack_worker, args=(config, user_id, uin, name, on_page),
            name=f"attack-qq-{uin}", daemon=True)
        _pool[uin] = th
        th.start()
    log.info("已为攻打 QQ %s 拉起线程，登录账号 %s", uin, citydb.username_of(user_id))
    return "qr" if on_page else "started"


def run_farm_city_once(qq, config: dict, city_id, times=1, sweep=False,
                       country=0) -> int:
    """连一次游戏、打指定城市里库中的人、断开退出。不迁城。"""
    from . import country_war

    def _work(rec, sock, spec, ctx, beater):
        out = country_war.farm_city(
            rec, sock, config, city_id, sweep=sweep, times=times,
            country=country, beat=beater)
        log.info("―― 打城 %s ―― 命中 %d 次，失败 %d 人，跳过 %d，用卡 %d",
                 out.get("城市"), out.get("成功") or 0, out.get("失败") or 0,
                 out.get("跳过") or 0, out.get("用卡") or 0)
        if out.get("停止原因"):
            log.info("   结束原因：%s", out["停止原因"])
        log.info("   任务执行期间共发心跳 %d 次", beater.count)
        return 0 if out.get("成功") else 1

    return _connect_and(qq, config, _work)


def _await_role(sock, rec, spec, ctx, config=None, timeout=12) -> bool:
    """读登录推送，直到出现自己的国家ID。手填了「国战.自己国家ID」则直接返回。"""
    filled = int(((config or {}).get("国战") or {}).get("自己国家ID") or 0)
    if filled or daily.read_my_country(rec):
        return True
    try:
        from .proto_encode import encode_message
        flag = encode_message({2: ("int32", 100), 4: ("int32", 9)},
                              omit_zero=False)
        sender.send_frame(sock, "0481", flag, getattr(rec, "rc4_c2s", None))
        sender.send_frame(sock, "0462", b"", getattr(rec, "rc4_c2s", None))
        log.info("已请求国家面板")
    except Exception as exc:
        log.warning("请求国家面板失败：%s", exc)
    sock.settimeout(1.0)
    deadline = time.time() + timeout
    while time.time() < deadline:
        cid = daily.read_my_country(rec)
        if cid:
            log.info("国家ID=%s", cid)
            return True
        try:
            chunk = sock.recv(8192)
        except socket.timeout:
            continue
        if not chunk:
            return False
        reply = protocol.maybe_online_reply(spec, chunk, ctx)
        if reply:
            sock.sendall(reply)
            log.info("收到在线探测，已回应")
    cid = daily.read_my_country(rec)
    if cid:
        log.info("国家ID=%s", cid)
    return bool(cid)


def _connect_and(qq, config: dict, work) -> int:
    """连一次游戏、登录、把活交给 work，然后断开退出。

    `--daily`、`--country-war`、`--city-players`、`--atk` / `--atk-city` 共用这一套：登录、建 RC4、
    实时解密、等服务端把登录后的状态推完，一步都不能少。
    work(rec, sock, spec, ctx, beater) 返回进程退出码。心跳在独立线程里发。
    """
    _mark_game_online(False)
    try:
        spec = protocol.load_spec()
    except protocol.ProtocolNotConfigured as exc:
        log.error("%s", exc)
        return 2

    from . import citydb
    if citydb.attack_context_user() or citydb.attack_context_qq():
        if not citydb.attack_in_keepalive():
            citydb.set_attack_status("login")
    consume_login_platform(
        citydb.attack_context_user(),
        getattr(qq, "uin", "") or citydb.attack_context_qq(),
        qq)

    if not qq.is_valid() and not relogin_with_push(qq, config):
        qq.note_login("游戏连接前登录没完成。%s。%s",
                      _login_context(), qq.ticket_brief())
        return 1
    if not note_attack_qq(qq):
        qq.note_login("登录后的 QQ 和绑定对不上，不连接游戏。%s", qq.ticket_brief())
        return 1
    if qq.shares_blocked_uin():
        return 1

    ctx = get_game_context(qq)
    _remember_region(qq, ctx)
    host = ctx.get("server") or spec.get("default_host", "tankstorm-proxy.sincetimes.com")
    port = int(ctx.get("port") or spec.get("default_port", 8001))
    if not ctx.get("openkey"):
        log.error("未取得 openkey，登录态可能失效")
        qq.note_login("未取得 openkey，游戏页没签发新票据。%s", qq.ticket_brief())
        return 1

    rec = Recorder(config, on_alert=None)
    rec.login_pf = ctx.get("pf") or (
        "qqgame" if getattr(qq, "platform", None) == "大厅" else "qzone")
    if spec.get("http_warmup", True):
        _http_warmup(qq, ctx)
    try:
        sock = _connect(host, port)
    except OSError as exc:
        log.error("连接失败: %s", exc)
        qq.note_login("游戏服连接失败：%s。%s", exc, qq.ticket_brief())
        return 1

    heart = None
    try:
        rec.on_connect()
        sock = rec.wrap(sock, host=host, port=port,
                        uid=ctx.get("uid"), sid=ctx.get("sid"))
        rec.enable_crypto(ctx)
        sock = _LockedSock(sock)
        _register_live_sock(sock)
        _arm_storm_relogin(rec, sock)
        _mark_game_online(False)
        _arm_super_storm(rec, sock, config, qq)
        for data, delay in protocol.build_login_sequence(spec, ctx):
            sock.sendall(data)
            if delay:
                time.sleep(delay)
        log.info("已登录，uid=%s sid=%s", ctx.get("uid"), ctx.get("sid"))
        qq.note_login("游戏连接开始。%s。%s", _login_context(), qq.ticket_brief())

        interval = float(config.get("保持活跃", {}).get("心跳间隔秒")
                         or protocol.heartbeat_interval(spec))
        heart = _open_heart(sock, protocol.build_heartbeat(spec, ctx), interval)

        # 只收到 RseAuthState 就开打，后面一律是「读不到自己的国家ID」。
        # 保活连上前会走 loadIdInfo.war，这里以前漏了，服务端有时只回认证包、不推 RseLoad。
        if not _await_role(sock, rec, spec, ctx, config):
            got = "、".join(rec.latest.keys()) or "无"
            log.error("登录后没收到角色数据，读不到国家ID（已有回包：%s）。"
                      "先停掉 --keepalive 和游戏窗口再试，多半是号被另一路占着",
                      got)
            return 1
        log.info("登录态数据接收完毕，开始执行")
        _mark_game_online(True)
        citydb.note_attack_link(interval)
        if citydb.attack_context_user() or citydb.attack_context_qq():
            _clear_login_limit()
            citydb.clear_login_expired()

        return work(rec, sock, spec, ctx, heart)
    except OSError as exc:
        log.error("连接中断: %s", exc)
        return 1
    finally:
        _drop_live_sock()
        if heart is not None:
            heart.stop()
        try:
            sock.close()
        except OSError:
            pass
        rec.close()
        from . import citydb
        citydb.clear_attack_link()
        citydb.flush_atk_fail()
        try:
            qq.note_login("游戏连接断开。%s。%s", _login_context(), qq.ticket_brief())
        except Exception:
            log.debug("登录日志没写上连接断开", exc_info=True)


def run_fund_once(qq, config: dict, building_id: int, times: int) -> int:
    """连一次、开资源卡、给指定建筑拨款、退出。"""
    from . import fund

    def _work(rec, sock, spec, ctx, beater):
        ok, why = fund.fund(rec, sock, building_id, times)
        log.info("[拨款] %s", why)
        log.info("任务执行期间共发心跳 %d 次", beater.count)
        return 0 if ok else 1

    return _connect_and(qq, config, _work)


def run_pve_once(qq, config: dict, stages=None) -> int:
    """连一次、打征战世界、退出。

    stages 留空只打当前关。一个数字是终点关，从当前关打到这一关。
    到了或超过终点关时，按免费重开次数重置并从第 1 关打到终点。
    """
    def _work(rec, sock, spec, ctx, beater):
        cfg = config.get("征战") or {}
        raw_gap = cfg.get("间隔秒", 1)
        interval = float(1 if raw_gap is None or raw_gap == "" else raw_gap)
        raw = stages if stages else None
        path = daily.state_path_for_qq(getattr(qq, "uin", "") or "")
        try:
            with daily.using_state(path):
                ok, why = daily.campaign_round(rec, sock, raw, interval)
        except ValueError as exc:
            log.error("[征战] %s", exc)
            return 1
        log.info("[征战] %s", why)
        log.info("任务执行期间共发心跳 %d 次", beater.count)
        return 0 if ok else 1

    return _connect_and(qq, config, _work)


def run_pick_once(qq, config: dict, kind: str, count: int) -> int:
    """连一次，用自选包换成指定卡，看回包后退出。"""
    def _work(rec, sock, spec, ctx, beater):
        ok, why = daily.pick_pack(rec, sock, kind, count)
        log.info("[自选包] %s", why)
        log.info("任务执行期间共发心跳 %d 次", beater.count)
        return 0 if ok else 1

    return _connect_and(qq, config, _work)


def run_lab50_once(qq, config: dict, times: int) -> int:
    """连一次，做实验室探索 50 连，看回包后退出。"""
    def _work(rec, sock, spec, ctx, beater):
        ok, why = daily.lab50_probe(rec, sock, times)
        log.info("[实验室探索] %s", why)
        log.info("任务执行期间共发心跳 %d 次", beater.count)
        return 0 if ok else 1

    return _connect_and(qq, config, _work)


def run_draw_once(qq, config: dict, mode: str) -> int:
    """连一次，按抓包发指定的那一种抽取，看回包后退出。"""
    def _work(rec, sock, spec, ctx, beater):
        ok, why = daily.draw_probe(rec, sock, mode)
        log.info("[抽取] %s", why)
        log.info("任务执行期间共发心跳 %d 次", beater.count)
        return 0 if ok else 1

    return _connect_and(qq, config, _work)


def run_daily_once(qq, config: dict) -> int:
    """连一次游戏、跑一轮每日任务、断开退出。供 `main.py --daily` 用。

    与 --keepalive 的区别：不常驻，任务跑完就走；但任务执行期间照样发心跳。
    """
    def _work(rec, sock, spec, ctx, beater):
        results, details = daily.run(rec, sock, config, beat=beater)
        log.info("任务执行期间共发心跳 %d 次", beater.count)
        _push_daily_summary(config, results, details)
        failed = sum(1 for v in results.values()
                     if "失败" in v or "拦截" in v)
        return 1 if failed else 0

    return _connect_and(qq, config, _work)


def note_attack_qq(qq) -> bool:
    """攻打号登录之后核对一次。第一次扫上的 QQ 绑到当前登录账号。
    已经绑过的不能换成另一个号。对不上就清掉这次票据并暂停，返回 False。"""
    if not getattr(qq, "attack_account", False):
        return True
    from . import citydb

    # 线程启动时的攻打 QQ 为准。打到别的登录账号的订单时，上下文会临时换人，不能拿那个人来改绑。
    thread_qq = str(citydb.attack_context_qq() or "").strip()
    user_id = citydb.attack_context_user() or citydb.login_for()
    uin = str(getattr(qq, "uin", "") or "").strip()
    if thread_qq.isdigit() and uin.isdigit() and thread_qq != uin:
        owner, _name = citydb.attack_qq_owner(thread_qq)
        target = user_id if user_id and citydb.attack_qq_of(user_id) == thread_qq else owner
        log.error("这条攻打线程绑定的是 %s，这次登录的是 %s，不切换", thread_qq, uin)
        if target:
            citydb.confirm_attack_qq(target, uin)
        qq._attack_qq_seen = (target or user_id, uin)
        qq._attack_qq_bad = True
        if hasattr(qq, "forget_login"):
            qq.forget_login()
        return False
    if not user_id or not uin.isdigit():
        return True
    seen = getattr(qq, "_attack_qq_seen", None)
    if seen == (user_id, uin):
        if getattr(qq, "_attack_qq_bad", False):
            citydb.set_attack_paused(True, user_id)
            return False
        return True
    ok = citydb.confirm_attack_qq(user_id, uin)
    qq._attack_qq_seen = (user_id, uin)
    qq._attack_qq_bad = not ok
    if ok:
        citydb.clear_login_for()
        citydb.set_attack_context(user_id, uin)
    elif hasattr(qq, "forget_login"):
        qq.forget_login()
    return ok


def _login_context() -> str:
    """重新登录时带上挂机配置，用来区分是计时到了，还是票据没了。"""
    from . import citydb

    ids = citydb.attack_context_users()
    if not ids:
        return "挂机：没有登录账号上下文"
    bits = []
    for uid in ids:
        name = citydb.username_of(uid) or str(uid)
        flag = "开" if citydb.attack_hold_always(uid) else "关"
        bits.append(f"{name}全天候{flag}")
    left = citydb.attack_hold_left()
    minutes = citydb.attack_hold_minutes()
    if left is None:
        left_text = "这次没有挂机计时"
    else:
        left_text = f"这次还剩{left}秒"
    return (f"登录账号{'、'.join(bits)}，配置挂机最长{minutes}分钟，{left_text}")


def relogin_with_push(qq, config: dict, force_qr: bool = False) -> bool:
    """需要重新扫码时：生成二维码并通过 PushPlus 推送给用户，等待扫码。
    二维码过期/超时则自动重发新码，一直重试直到扫码成功（守护进程不能自己退场）。
    攻打线程设了 10 分钟上限时，到点返回 False。
    force_qr 为真时不再用旧票据续上，必须重新扫。攻打 QQ 对不上时用这个。"""
    # 先向本机 NapCat 要当前票据。没有再试长效凭据静默续期。
    from . import citydb as _citydb
    consume_login_platform(_citydb.attack_context_user(), getattr(qq, "uin", ""), qq)
    qq.note_login("进入重新登录（%s）。强制扫码=%s。%s。%s",
                  qq.platform, "是" if force_qr else "否", _login_context(),
                  qq.ticket_brief())
    own_napcat = bool(getattr(qq, "attack_account", False) and getattr(qq, "use_napcat", False))
    if (own_napcat or not force_qr) and qq.adopt_napcat(config):
        if note_attack_qq(qq):
            qq.note_login("NapCat 已接上，不再扫码。%s", qq.ticket_brief())
            if own_napcat:
                log.info("已用这个攻打号的 NapCat 续上，不发消息")
            return True
        log.error("NapCat 续上的 QQ 不是这个账号绑定的，改走扫码")
        qq.note_login("NapCat 续上的 QQ 不是绑定的号，改为扫码")
    if not force_qr and qq.silent_renew():
        qq.note_login("静默续期已接上。%s", qq.ticket_brief())
        if note_attack_qq(qq):
            log.info("已用长效凭据静默续期，无需人工介入")
            return True
        log.error("续上的 QQ 不是这个账号绑定的，改走扫码")
        qq.note_login("续上的 QQ 不是绑定的号，改为扫码")
    elif force_qr:
        qq.note_login("这一次不续旧票据，直接扫码。%s", qq.ticket_brief())
    else:
        qq.note_login("续期没有接上，改为扫码。%s", qq.ticket_brief())

    # 推送登录：直接往手机QQ推确认，免去扫码。
    # 这解决了"二维码图存本地、同一台手机相册扫码"被腾讯拒（限制本地扫码登录）的问题。
    if getattr(qq, "attack_account", False):
        push_uin = None
    else:
        push_uin = (config.get("登录", {}) or {}).get("推送登录QQ号") or qq.uin or None

    def on_qr(path, pushed=False):
        from . import citydb
        if getattr(qq, "attack_account", False):
            citydb.note_login_qr(path)
        if own_napcat:
            log.info("攻打号使用自己的 NapCat 取票，不发登录消息")
            return
        if getattr(qq, "page_qr", False):
            log.info("登录二维码留在网页上：%s", path)
            return
        elif getattr(qq, "attack_account", False):
            name = getattr(qq, "account_name", "") or "攻打号"
            notify.send_admin_login_qr(
                config, path, target=getattr(qq, "notify_qq", ""),
                text=(f"攻打号「{name}」需要扫码。用这个号的手机 QQ，"
                      f"在另一台设备上扫这张图。不要把图存进同一台手机相册再扫。"))
        elif attempt == 1:
            # 扫描号同一次失效只私聊管理员一次，后面换码不再发
            notify.send_admin_login_qr(config, path)
        if pushed:
            notify.send_qrcode(
                config, "坦克风暴：请在手机QQ点「确认登录」", path,
                note=f"已向 QQ {push_uin} 推送登录确认，<b>打开手机QQ点确认即可，"
                     f"不用扫码</b>。<br>若没收到推送，可用<b>另一台设备</b>打开本条消息，"
                     f"再用手机QQ扫下面的码（同一台手机存图后扫会被拒）。")
        else:
            notify.send_qrcode(
                config, "坦克风暴：需要扫码登录", path,
                note="请用<b>另一台设备</b>打开本条消息，再用手机QQ扫码。"
                     "<br>把图存到手机再用同一台手机相册扫，腾讯会提示"
                     "「限制本地扫码登录」。")

    attempt = 0
    owner = 0
    from . import citydb
    if getattr(qq, "page_qr", False):
        owner = int(citydb.attack_context_user() or 0)
        if owner:
            citydb.set_page_qr(True, owner)
    qq._login_busy = True
    try:
        while True:
            until = _login_until_wall()
            if _login_limit_hit():
                log.info("登录超过 10 分钟还没完成，停止等待扫码")
                qq.note_login("登录超过 10 分钟还没完成，停止等待扫码")
                return False
            attempt += 1
            # 注意：这里只说"正在尝试"，别在请求发出前就宣称已推送 —— 之前那样写，
            # 推送其实失败了日志却显示"已推送"，很误导。
            log.info("登录态失效，正在%s（第 %d 次尝试）",
                     f"向 QQ {push_uin} 发起推送登录" if push_uin else "生成二维码", attempt)
            qq.note_login("扫码第 %d 次。%s", attempt, _login_context())
            timeout = 180 if not until else max(1, int(until - time.time()))
            if qq.qr_login(on_qr=on_qr, push_uin=push_uin,
                           timeout_sec=min(180, timeout), until=until):
                qq.note_login("重新登录成功。%s。%s", _login_context(), qq.ticket_brief())
                if not note_attack_qq(qq):
                    log.error("这次登录的 QQ 不是绑定的那个，请改用原来的号再扫")
                    qq.note_login("扫上的 QQ 不是绑定的号，继续等下一次扫码")
                    continue
                if not getattr(qq, "attack_account", False):
                    notify.send(config, "坦克风暴：已重新登录", "登录成功，保活已恢复在线。")
                return True
            if _login_limit_hit():
                log.info("登录超过 10 分钟还没完成，停止等待扫码")
                qq.note_login("登录超过 10 分钟还没完成，停止等待扫码")
                return False
            qq.note_login("这一轮扫码未完成，15 秒后重试。%s", qq.ticket_brief())
            log.warning("本轮登录未完成（超时/过期），15 秒后重试", )
            until = _login_until_wall()
            pause = 15.0 if not until else min(15.0, until - time.time())
            if pause <= 0:
                if _login_limit_hit():
                    log.info("登录超过 10 分钟还没完成，停止等待扫码")
                    qq.note_login("登录超过 10 分钟还没完成，停止等待扫码")
                    return False
                pause = 15.0
            time.sleep(pause)
    finally:
        qq._login_busy = False
        if owner:
            citydb.set_page_qr(False, owner)


def run(qq, config: dict, with_daily: bool = False) -> int:
    """保活守护进程。只负责别掉线。

    with_daily=True 时，每次连上后顺带跑一轮每日任务（`--keepalive --daily`）。
    默认不跑 —— 保活和每日任务是两件独立的事，见 _one_session 里的说明。
    """
    conf = config.get("保持活跃", {})
    if not conf.get("启用", False):
        log.info("保持活跃未启用（config.json 保持活跃.启用=false）")
        return 0

    try:
        spec = protocol.load_spec()
    except protocol.ProtocolNotConfigured as exc:
        log.error("%s", exc)
        return 2

    run_hours = float(conf.get("持续小时", 0))       # 0 = 一直跑
    min_backoff = float(conf.get("重连最小秒", 5))
    max_backoff = float(conf.get("重连最大秒", 120))
    started = time.time()
    backoff = min_backoff
    storm_retry = False
    storm_started = 0.0

    # 录制服务器消息；发现异常事件（如超级强攻的验证码通知）立刻推送到手机
    def on_alert(op, seq, body, text, reason):
        notify.send(config, "⚠️ 坦克风暴：检测到异常事件，请立刻查看游戏",
                    f"原因：{'；'.join(reason)}\n"
                    f"消息类型：{op}  长度：{len(body)}\n"
                    f"内容片段：{text[:200] or '(无可读文本)'}\n\n"
                    f"若是「超级强攻」验证码，你只有约 5 分钟处理时间，"
                    f"请马上打开游戏输入验证码。")
    # on_super_storm 回调在 _one_session 里根据当前 sock 动态设置
    rec = Recorder(config, on_alert=on_alert)

    # 启动时若未登录（如服务器首次部署），也走"推送二维码"流程
    install_login_refresh()
    bind_login_session(qq)
    pulse = _start_pulse(qq, attack=False)
    if not qq.is_valid():
        relogin_with_push(qq, config)

    log.info("保持活跃启动（Ctrl+C 停止）")
    try:
        while True:
            if run_hours and (time.time() - started) >= run_hours * 3600:
                log.info("达到设定运行时长，退出")
                break
            if storm_retry and _storm_retry_expired(storm_started):
                _stop_storm_relogin(str(getattr(qq, "uin", "") or ""))
                break
            if not qq.is_valid():
                relogin_with_push(qq, config)

            reason = _one_session(qq, spec, conf, config, rec,
                                  with_daily=with_daily)
            log.warning("本次连接结束：%s", reason)
            dropped = _take_storm_drop()
            keep, storm_started, timed_out = _storm_retry_decision(
                dropped, storm_retry, _game_online(), storm_started, time.monotonic())
            if timed_out:
                _stop_storm_relogin(str(getattr(qq, "uin", "") or ""))
                break
            if keep:
                storm_retry = True
                wait = STORM_RETRY_SEC
                log.info("被超级强攻断开，%d 秒后重新登录", wait)
            else:
                storm_retry = False
                wait = min(backoff, max_backoff)
                backoff = min(backoff * 2, max_backoff)
                if _game_online():
                    backoff = min_backoff
            log.info("%.0f 秒后重连…", wait)
            time.sleep(wait)
            # 若刚成功跑过一段，重置退避
    except KeyboardInterrupt:
        log.info("手动停止保持活跃")
    finally:
        _stop_pulse(pulse)
        unbind_login_session(qq)
        rec.close()
        if rec.counts:
            top = sorted(rec.counts.items(), key=lambda kv: -kv[1])[:8]
            log.info("本次录制消息统计（前 8 类）：%s",
                     "，".join(f"{o}×{c}" for o, c in top))
        if rec.counts_out:
            top = sorted(rec.counts_out.items(), key=lambda kv: -kv[1])[:5]
            log.info("上行消息统计：%s", "，".join(f"{o}×{c}" for o, c in top))
        if rec.enc_ops:
            log.info("本次加密消息：%s",
                     "，".join(f"{o}×{c}" for o, c in
                               sorted(rec.enc_ops.items(), key=lambda kv: -kv[1])[:8]))
            if rec.stream.session_dir:
                log.info("解密：python tools/redwar_rc4.py %s/s2c.bin --uid %s --write",
                         rec.stream.session_dir, ctx.get("uid") or "<uid>")
    return 0
