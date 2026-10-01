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

import socket
import threading
import time

from . import daily, notify, protocol, sender
from .log import get_logger
from .qzone import get_game_context
from .recorder import Recorder

log = get_logger()


def _connect(host: str, port: int, timeout: float = 15) -> socket.socket:
    sock = socket.create_connection((host, port), timeout=timeout)
    sock.settimeout(timeout)
    return sock


def _http_warmup(qq, ctx: dict) -> None:
    """连 socket 前，复刻浏览器的 HTTP 调用（loadIdInfo.war），让服务器完成会话注册。
    失败不影响后续 socket 连接，仅记录调试日志。"""
    base = ctx.get("coinserver") or "https://tankstorm-qzone.sincetimes.com/"
    params = {"openid": ctx.get("openid", ""), "openkey": ctx.get("openkey", ""),
              "uid": ctx.get("uid", ""), "pf": ctx.get("pf", "qzone")}
    if not params["openid"]:
        return
    try:
        r = qq.session.get(base.rstrip("/") + "/loadIdInfo.war", params=params,
                           headers={"Referer": ctx.get("canvas_url", "")}, timeout=15)
        log.debug("warmup loadIdInfo.war -> %s %r", r.status_code, r.text[:40])
    except Exception as exc:
        log.debug("warmup 失败(忽略): %s", exc)


def _one_session(qq, spec: dict, conf: dict, config: dict, rec=None,
                 with_daily: bool = False) -> str:
    """跑一次完整连接，直到断开。返回断开原因（字符串）。"""
    ctx = get_game_context(qq)
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

    if rec and rec.auto_reject:
        # 超级强攻自动拒绝：在当前会话的 sock 和 RC4 上下文里构造回调
        _sock_ref = sock
        def _on_super_storm(data):
            rc4 = rec.rc4_c2s
            if rc4 is None:
                log.warning("自动拒绝超级强攻失败：RC4 C→S 实例不可用"
                            "（实时解密未启用或密钥自检失败）")
                return
            ok = sender.send_reject_super_storm(_sock_ref, rc4, data)
            if ok:
                notify.send(config, "🛡️ 坦克风暴：已自动拒绝超级强攻",
                            f"进攻方：{data.get('atkName', '?')}（{data.get('atkUid', '?')}）\n"
                            f"防守方：{data.get('deftName', '?')}（{data.get('deftUid', '?')}）\n\n"
                            f"已自动发送 RceSuperStormOpt type=2 拒绝包。\n"
                            f"如果服务端要求验证码才接受拒绝，此包可能被忽略，"
                            f"请立刻打开游戏确认。")
        rec.on_super_storm = _on_super_storm
        log.info("超级强攻自动拒绝已就绪")

    heart = None
    prev_beat = daily._BEAT
    try:
        steps = protocol.build_login_sequence(spec, ctx)
        for i, (data, delay) in enumerate(steps, 1):
            sock.sendall(data)
            log.debug("登录步骤 %d/%d 已发送（%d 字节）", i, len(steps), len(data))
            if delay:
                time.sleep(delay)
        log.info("已完成登录握手（%d 步），uid=%s secret=%s",
                 len(steps), ctx.get("uid"), ctx.get("secret"))
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
        heart = _Beater(sock, hb, interval)
        daily._BEAT = heart

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
        if cities:
            log.info("城市监视：每 %.0f 秒刷新 %d 座城 %s",
                     watch_gap, len(cities), ",".join(str(c) for c in cities))
        elif (config.get("城市监视") or {}).get("启用"):
            log.info("城市监视已启用。config 里的城市是空的，"
                     "在订阅页面加上城市和 UID 之后会开始刷新")

        sock.settimeout(1.0)
        while True:
            now = time.time()
            if ((config.get("城市监视") or {}).get("启用")
                    and now - last_watch >= watch_gap):
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
        daily._BEAT = prev_beat
        if heart is not None:
            heart.stop()
        try:
            sock.close()
        except OSError:
            pass


def _push_daily_summary(config: dict, results: dict, details: dict) -> None:
    """把每日任务成果推到 PushPlus，并在日志里打一份对照表。

    只有真正执行过的才推 —— 全是"未开启/冷却中"的轮次不值得打扰。
    """
    # "跳过"不是失败 —— 今日额度已用完、冷却中、未开启，这些都不该算进成败统计，
    # 否则一份"全是跳过"的日志会显示成"未成 14"，看着像全崩了。
    acted = {k: v for k, v in results.items()
             if v and not any(s in v for s in
                              ("未开启", "冷却中", "未实测", "跳过"))}
    if not acted:
        return

    okn = sum(1 for v in acted.values() if v.startswith("成功"))
    bad = len(acted) - okn

    log.info("―― 每日任务成果 ―― 成功 %d，未成 %d", okn, bad)
    rows = []
    for k, v in acted.items():
        icon = "✅" if v.startswith("成功") else "❌"
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
        with self._lock:
            return self._sock.close()

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
        self._stop = threading.Event()
        self._th = threading.Thread(target=self._loop, name="game-heartbeat",
                                    daemon=True)
        self._th.start()

    def _loop(self):
        while not self._stop.wait(self.interval):
            try:
                self.sock.sendall(self.hb)
                self.last = time.time()
                self.count += 1
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


def _scan_one_city(rec, sock, config, city_id, beat, country_id=0, start_page=0):
    """拉一座城：玩家入库，并记下当前归属国。"""
    from . import citydb, country_war

    cname = citydb.city_name(city_id) or str(city_id)
    ts = citydb.now_ts()
    n = [0]

    def on_page(batch, page):
        n[0] += citydb.upsert_players(city_id, batch, ts, page=page)

    out = country_war.list_city_players(
        rec, sock, config, city_id, country=country_id, beat=beat,
        on_page=on_page, start_page=start_page)
    players = out.get("玩家") or []
    last = out.get("last_page")
    total = out.get("userCnt")
    owner = out.get("owner")
    if owner is not None or total is not None:
        citydb.record_occupy(city_id, owner, total, ts)
    full = (start_page == 0 and not out.get("原因")
            and total is not None and len(players) >= total)
    changes = citydb.sync_watch(
        city_id, [str(p.get("uid") or "") for p in players], full)
    if full:
        gone = citydb.drop_stale(city_id, ts)
        if gone:
            log.info("已清掉本城过期记录 %d 条", gone)
    token = ((config.get("通知") or {}).get("pushplus_token") or "").strip()
    for ch in changes:
        who = ch["name"] or ch["uid"]
        where = f"{city_id} {cname}".strip()
        verb = "出现在" if ch["present"] else "已离开"
        log.info("[订阅] %s %s %s", who, verb, where)
        if token:
            notify.send(config, f"坦克风暴：{who} {verb} {where}",
                        f"城市 {where}\nUID {ch['uid']}\n"
                        f"{'在城里' if ch['present'] else '不在城里'}")
    oname = citydb.country_name(owner) if owner else ""
    log.info("―― 城市 %s %s ―― 归属国家 %s%s，面板人数 %s，本轮写入 %d 人，最后一页 %s",
             out.get("city"), cname, owner,
             f" {oname}" if oname else "", total, n[0], last)
    return out, n[0]


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
                          country_id: int = 0, start_page: int = 0) -> int:
    """连一次游戏、查询指定城市的玩家列表、写入 sqlite、断开退出。"""
    from . import citydb

    def _work(rec, sock, spec, ctx, beater):
        try:
            citydb.ensure_catalog()
        except Exception as exc:
            log.warning("城市目录更新失败（仍会写玩家）：%s", exc)
        out, n = _scan_one_city(rec, sock, config, city_id, beater,
                               country_id=country_id, start_page=start_page)
        last = out.get("last_page")
        log.info("   查询期间共发心跳 %d 次；库文件 %s", beater.count, citydb.DB_FILE)
        if out.get("原因"):
            nxt = (last + 1) if isinstance(last, int) else start_page
            log.info("   %s", out["原因"])
            log.info("   续拉：py main.py --city-players %s --city-page %s",
                     city_id, nxt)
        return 0 if n or not out.get("原因") else 1

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


def _connect_and(qq, config: dict, work) -> int:
    """连一次游戏、登录、把活交给 work，然后断开退出。

    `--daily`、`--country-war`、`--city-players`、`--atk` / `--atk-city` 共用这一套：登录、建 RC4、
    实时解密、等服务端把登录后的状态推完，一步都不能少。
    work(rec, sock, spec, ctx, beater) 返回进程退出码。心跳在独立线程里发。
    """
    try:
        spec = protocol.load_spec()
    except protocol.ProtocolNotConfigured as exc:
        log.error("%s", exc)
        return 2

    if not qq.is_valid() and not relogin_with_push(qq, config):
        return 1

    ctx = get_game_context(qq)
    host = ctx.get("server") or spec.get("default_host", "tankstorm-proxy.sincetimes.com")
    port = int(ctx.get("port") or spec.get("default_port", 8001))
    if not ctx.get("openkey"):
        log.error("未取得 openkey，登录态可能失效")
        return 1

    rec = Recorder(config, on_alert=None)
    if spec.get("http_warmup", True):
        _http_warmup(qq, ctx)
    try:
        sock = _connect(host, port)
    except OSError as exc:
        log.error("连接失败: %s", exc)
        return 1

    heart = None
    try:
        rec.on_connect()
        sock = rec.wrap(sock, host=host, port=port,
                        uid=ctx.get("uid"), sid=ctx.get("sid"))
        rec.enable_crypto(ctx)
        sock = _LockedSock(sock)
        for data, delay in protocol.build_login_sequence(spec, ctx):
            sock.sendall(data)
            if delay:
                time.sleep(delay)
        log.info("已登录，uid=%s sid=%s", ctx.get("uid"), ctx.get("sid"))

        interval = float(config.get("保持活跃", {}).get("心跳间隔秒")
                         or protocol.heartbeat_interval(spec))
        heart = _Beater(sock, protocol.build_heartbeat(spec, ctx), interval)

        # 等到角色数据（国家ID）到了再干活。只收到 RseAuthState 就开打，
        # 后面一律是「读不到自己的国家ID」。保活连上前会走 loadIdInfo.war，
        # 这里以前漏了，服务端有时只回认证包、不推 RseLoad。
        sock.settimeout(1.0)
        deadline = time.time() + 12
        while time.time() < deadline:
            if daily.read_my_country(rec):
                break
            try:
                chunk = sock.recv(8192)
            except socket.timeout:
                continue
            if not chunk:
                break
            reply = protocol.maybe_online_reply(spec, chunk, ctx)
            if reply:
                sock.sendall(reply)
                log.info("收到在线探测，已回应")
        if not daily.read_my_country(rec):
            got = "、".join(rec.latest.keys()) or "无"
            log.error("登录后没收到角色数据，读不到国家ID（已有回包：%s）。"
                      "先停掉 --keepalive 和游戏窗口再试，多半是号被另一路占着",
                      got)
            return 1
        log.info("登录态数据接收完毕，开始执行")

        return work(rec, sock, spec, ctx, heart)
    except OSError as exc:
        log.error("连接中断: %s", exc)
        return 1
    finally:
        if heart is not None:
            heart.stop()
        try:
            sock.close()
        except OSError:
            pass
        rec.close()
        from . import citydb
        citydb.flush_atk_fail()


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
    """连一次、按名单打征战世界、退出。stages 为空则用 config「征战.关卡」。"""
    from . import pve

    def _work(rec, sock, spec, ctx, beater):
        cfg = config.get("征战") or {}
        if cfg.get("第4次"):
            ok, why = pve.vip_restart(rec, sock)
            log.info("[征战] %s", why)
            if not ok:
                return 1
        raw = stages if stages else cfg.get("关卡")
        if not raw:
            if not cfg.get("第4次"):
                log.error("[征战] 没有配置关卡")
                return 1
            log.info("任务执行期间共发心跳 %d 次", beater.count)
            return 0
        try:
            ok, why = pve.fight(rec, sock, raw, cfg.get("间隔秒", 1))
        except ValueError as exc:
            log.error("[征战] %s", exc)
            return 1
        log.info("[征战] %s", why)
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


def relogin_with_push(qq, config: dict) -> bool:
    """需要重新扫码时：生成二维码并通过 PushPlus 推送给用户，等待扫码。
    二维码过期/超时则自动重发新码，一直重试直到扫码成功（守护进程不能自己退场）。"""
    # 先试静默续期：skey 只活约 24 小时，但 superkey/RK/ptcz 是长效的，
    # 能换发新 skey 而不必惊动你。成功就不用你动手了。
    if qq.silent_renew():
        log.info("已用长效凭据静默续期，无需人工介入")
        return True

    # 推送登录：直接往手机QQ推确认，免去扫码。
    # 这解决了"二维码图存本地、同一台手机相册扫码"被腾讯拒（限制本地扫码登录）的问题。
    push_uin = (config.get("登录", {}) or {}).get("推送登录QQ号") or qq.uin or None

    def on_qr(path, pushed=False):
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
    while True:
        attempt += 1
        # 注意：这里只说"正在尝试"，别在请求发出前就宣称已推送 —— 之前那样写，
        # 推送其实失败了日志却显示"已推送"，很误导。
        log.info("登录态失效，正在%s（第 %d 次尝试）",
                 f"向 QQ {push_uin} 发起推送登录" if push_uin else "生成二维码", attempt)
        if qq.qr_login(on_qr=on_qr, push_uin=push_uin):
            notify.send(config, "坦克风暴：已重新登录", "登录成功，保活已恢复在线。")
            return True
        log.warning("本轮登录未完成（超时/过期），15 秒后重试", )
        time.sleep(15)


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
    if not qq.is_valid():
        relogin_with_push(qq, config)

    log.info("保持活跃启动（Ctrl+C 停止）")
    try:
        while True:
            if run_hours and (time.time() - started) >= run_hours * 3600:
                log.info("达到设定运行时长，退出")
                break
            if not qq.is_valid():
                relogin_with_push(qq, config)

            reason = _one_session(qq, spec, conf, config, rec,
                                  with_daily=with_daily)
            log.warning("本次连接结束：%s", reason)
            # 断开后退避重连
            wait = min(backoff, max_backoff)
            log.info("%.0f 秒后重连…", wait)
            time.sleep(wait)
            backoff = min(backoff * 2, max_backoff)
            # 若刚成功跑过一段，重置退避
    except KeyboardInterrupt:
        log.info("手动停止保持活跃")
    finally:
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
