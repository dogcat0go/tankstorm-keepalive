# tankstorm-keepalive  Copyright (C) 2026 Dimlitter
# SPDX-License-Identifier: AGPL-3.0-or-later
"""征战世界。

2026-09-26 抓包（logs/packets/2026-09-26.jsonl，00:17:46–00:20:43）：

开面板：
    RceStrategicArmyInfo type=1
    RcePVEFightOpt type=5，bAutoTreat=false
    回包 result=3，fightdata 第 1 号是当前关（customIndex）。

开战：
    RcePVEFightOpt type=7，bAutoTreat=true。请求里没有关卡号。
    先回 result=0（受理，关卡不变），再回 result=1（过关，关卡 +1）。
    result=2 且关卡不动，是没打过去，停手。

从面板上的当前关打起。最终关卡可选，不填就打到过不去为止。
"""

import re

from . import sender
from .daily import _await_response, _nap
from .log import get_logger
from .proto_encode import encode_message

log = get_logger()

OP = "045b"
OP_ARMY = "045c"
RSE = "RsePVEFightOpt"
TYPE_QUERY = 5
TYPE_FIGHT = 7
TYPE_RESTART = 2
TYPE_BUY = 4
TYPE_VIP = 101
HARD_MAX = 400


def _send(sock, rec, opcode, fields):
    before = rec.seq_mark() if rec else 0
    sender.send_frame(sock, opcode, encode_message(fields, omit_zero=False),
                      rec.rc4_c2s)
    return before


def parse_end(raw):
    """最终关卡。空、0 表示不限制。写了 "1-150" 时取较大的那个数。"""
    if raw is None or raw == "" or raw == [] or raw == 0:
        return None
    if isinstance(raw, bool):
        raise ValueError(f"最终关卡写不认：{raw}")
    if isinstance(raw, int):
        return raw if raw > 0 else None
    text = str(raw).strip()
    if not text or text == "0":
        return None
    m = re.fullmatch(r"(\d+)\s*-\s*(\d+)", text)
    if m:
        return max(int(m.group(1)), int(m.group(2)))
    if text.isdigit():
        n = int(text)
        return n if n > 0 else None
    raise ValueError(f"最终关卡写不认：{text}")


def _fd(data):
    fd = (data or {}).get("fightdata") if isinstance(data, dict) else None
    if isinstance(fd, list):
        fd = fd[0] if fd else None
    return fd if isinstance(fd, dict) else {}


def _num(data, key):
    v = _fd(data).get(key)
    if isinstance(v, bool) or not isinstance(v, int):
        return None
    return v


def _stage(data):
    return _num(data, "field1")


def query(rec, sock):
    """开征战面板，返回 (当前关, 回包)。当前关读不到时第一项是 None。"""
    _send(sock, rec, OP_ARMY, {1: ("int32", 1)})
    before = _send(sock, rec, OP, {
        1: ("bool", False), 2: ("int32", TYPE_QUERY),
    })
    got = _await_response(
        sock, rec, RSE, before, 6.0,
        want=lambda d: d.get("type") == TYPE_QUERY)
    return _stage(got), got


def _restart(rec, sock, cur, label):
    """type=2 把关卡打回第 1 关。调用前 refreshTimes 必须已经是至少 1。"""
    before = _send(sock, rec, OP, {2: ("int32", TYPE_RESTART)})
    got = _await_response(
        sock, rec, RSE, before, 6.0,
        want=lambda d: d.get("type") == TYPE_RESTART)
    if not isinstance(got, dict) or got.get("result") != 0:
        return False, "type=2 重开没有成功"
    stage = _stage(got)
    if stage != 1:
        return False, f"重开后关卡是 {stage}，不是第 1 关"
    return True, f"{label}已重开，从第 {cur} 关回到第 1 关"


def free_restart(rec, sock):
    """免费重开。refreshTimes 至少为 1 才发 type=2，发完次数必须减少。"""
    cur, panel = query(rec, sock)
    if not isinstance(panel, dict):
        return False, "没读到征战面板，不重开"
    refresh = _num(panel, "field2")
    if refresh is None:
        return False, "没读到重开次数，不发"
    if refresh < 1:
        return True, f"免费重开次数是 {refresh}，不用再重开"
    log.info("[征战] 免费重开：当前第 %s 关，剩余 %s 次，发 type=2", cur, refresh)
    ok, why = _restart(rec, sock, cur, "免费")
    if not ok:
        return ok, why
    _, panel2 = query(rec, sock)
    refresh2 = _num(panel2, "field2") if isinstance(panel2, dict) else None
    if refresh2 is None or refresh2 >= refresh:
        return False, f"type=2 之后重开次数仍是 {refresh2}，停手"
    return True, why


def buy_restart(rec, sock):
    """第 3 次：花勋章买一次重开，再把关卡打回第 1 关。

    2026-09-27 00:40 抓包。只在 buyrefreshTimes=0 且 refreshTimes=0 时发。
    type=4 会扣 100 勋章。次数没对上就停，不再发 type=2。
    条件不满足时返回成功，表示这次不用做。
    """
    cur, panel = query(rec, sock)
    if not isinstance(panel, dict):
        return False, "没读到征战面板，不重开"
    refresh = _num(panel, "field2")
    bought = _num(panel, "field3")
    if bought is None or refresh is None:
        return False, "没读到重开次数或付费次数，不发"
    if bought > 0:
        return True, f"付费重开已经用过（buyrefreshTimes={bought}），第3次不用再做"
    if refresh > 0:
        return True, f"还有重开次数 {refresh}，不用第3次"
    log.info("[征战] 第3次：当前第 %s 关，发 type=4", cur)
    before = _send(sock, rec, OP, {2: ("int32", TYPE_BUY)})
    got = _await_response(
        sock, rec, RSE, before, 6.0,
        want=lambda d: d.get("type") == TYPE_BUY)
    if not isinstance(got, dict) or got.get("result") != 0:
        return False, f"type=4 被拒 result={None if not isinstance(got, dict) else got.get('result')}"
    bought2 = _num(got, "field3")
    refresh2 = _num(got, "field2")
    if not bought2 or not refresh2:
        return False, f"type=4 之后次数不对（付费 {bought2}，重开 {refresh2}），停手"
    return _restart(rec, sock, cur, "第3次")


def vip_restart(rec, sock):
    """第 4 次：VIP 加次换成一次重开，再把关卡打回第 1 关。

    2026-09-26 00:36 抓包。只在 vipcardaddtimes>0 且 refreshTimes=0 时发。
    type=101 会扣 100 勋章。次数没对上就停，不再发 type=2。
    条件不满足时返回成功，表示这次不用做。
    """
    cur, panel = query(rec, sock)
    if not isinstance(panel, dict):
        return False, "没读到征战面板，不重开"
    refresh = _num(panel, "field2")
    vip = _num(panel, "field10")
    if vip is None or refresh is None:
        return False, "没读到重开次数或 VIP 加次，不发"
    if vip < 1:
        return True, f"VIP 加次是 {vip}，第4次不用再做"
    if refresh > 0:
        return True, f"还有重开次数 {refresh}，不用第4次"
    log.info("[征战] 第4次：当前第 %s 关，VIP 加次 %s，发 type=101", cur, vip)
    before = _send(sock, rec, OP, {2: ("int32", TYPE_VIP)})
    got = _await_response(
        sock, rec, RSE, before, 6.0,
        want=lambda d: d.get("type") == TYPE_VIP)
    if not isinstance(got, dict) or got.get("result") != 0:
        return False, f"type=101 被拒 result={None if not isinstance(got, dict) else got.get('result')}"
    vip2 = _num(got, "field10")
    refresh2 = _num(got, "field2")
    if vip2 is None or vip2 >= vip or not refresh2:
        return False, f"type=101 之后次数不对（VIP {vip2}，重开 {refresh2}），停手"
    return _restart(rec, sock, cur, "第4次")


def fight_once(rec, sock):
    """打当前关。返回结算回包（result 为 1 或 2），没有就 None。"""
    before = _send(sock, rec, OP, {
        1: ("bool", True), 2: ("int32", TYPE_FIGHT),
    })
    return _await_response(
        sock, rec, RSE, before, 8.0,
        want=lambda d: d.get("type") == TYPE_FIGHT and d.get("result") in (1, 2))


def fight(rec, sock, end=None, interval=1.0):
    """从当前关打到最终关。end 为空则打到过不去。

    返回 ("ok"|"stuck"|"error", 说明)。同一关连败 2 次是 stuck。
    """
    end = parse_end(end)
    cur, _panel = query(rec, sock)
    if cur is None:
        return "error", "没读到当前关，不打"
    if end is not None and cur > end:
        return "ok", f"当前第 {cur} 关已过最终关卡 {end}，不打"
    log.info("[征战] 当前第 %s 关，%s", cur,
             f"打到第 {end} 关" if end else "打到过不去")
    interval = float(interval if interval is not None else 1)
    first = cur
    passed = 0
    for _ in range(HARD_MAX):
        if end is not None and cur > end:
            break
        stuck = True
        for attempt in (1, 2):
            got = fight_once(rec, sock)
            if not isinstance(got, dict):
                return "error", f"第 {cur} 关没有结算回包，已过 {passed} 关"
            nxt = _stage(got)
            result = got.get("result")
            if result == 1 and nxt not in (None, cur):
                passed += 1
                log.info("[征战] 第 %s 关过了，下一关 %s", cur, nxt)
                cur = nxt
                stuck = False
                break
            log.info("[征战] 第 %s 关没过去 result=%s（第 %s 次）", cur, result, attempt)
            if attempt == 1 and interval > 0:
                _nap(interval)
        if stuck:
            return "stuck", f"第 {cur} 关连败 2 次，已从第 {first} 关过了 {passed} 关"
        if end is not None and cur > end:
            break
        if interval > 0:
            _nap(interval)
    else:
        return "error", f"已连打 {HARD_MAX} 关，停在第 {cur} 关"
    return "ok", f"从第 {first} 关打到第 {cur - 1} 关，当前第 {cur} 关"


def _again(rec, sock, end, interval, restart, notes):
    """重开成功就再打到最终关。条件不满足（说明里有「不用」）则跳过。"""
    ok, why = restart(rec, sock)
    notes.append(why)
    if not ok or "不用" in why:
        return ok
    code, why = fight(rec, sock, end, interval)
    notes.append(why)
    return code == "ok"


def campaign(rec, sock, end=None, interval=1.0, third=False, fourth=False):
    """先打到最终关。已经通过后：两次免费重开，再按开关做第 3、第 4 次，每次都再打到最终关。

    同一关连败 2 次时不走这四轮，只在开了第 4 次时重开再打一轮。
    """
    code, why = fight(rec, sock, end, interval)
    if code == "error":
        return False, why
    if code == "stuck":
        if not fourth:
            return False, why
        log.info("[征战] %s，发第4次重开", why)
        notes = [why]
        ok = _again(rec, sock, end, interval, vip_restart, notes)
        if "不用" in notes[-1]:
            ok = False
        return ok, "；".join(notes)
    notes = [why]
    for _ in (1, 2):
        if not _again(rec, sock, end, interval, free_restart, notes):
            return False, "；".join(notes)
        if "不用" in notes[-1]:
            break
    if third and not _again(rec, sock, end, interval, buy_restart, notes):
        return False, "；".join(notes)
    if fourth and not _again(rec, sock, end, interval, vip_restart, notes):
        return False, "；".join(notes)
    return True, "；".join(notes)


def daily_fight(rec, sock, config):
    """每日任务「征战世界」：按 config「征战」从当前关打。"""
    cfg = config.get("征战") or {}
    return campaign(rec, sock, cfg.get("最终关卡"), cfg.get("间隔秒", 1),
                    third=bool(cfg.get("第3次")), fourth=bool(cfg.get("第4次")))
