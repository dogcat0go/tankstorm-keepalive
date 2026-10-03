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

所以只能打「当前这一关」。配置里列出要打的关，当前关在名单里才发 type=7。
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
TYPE_VIP = 101
# 付费重征会扣勋章。购买那一条的 type 还没抓到，留空就不发。
TYPE_BUY = None
HARD_MAX = 400


def _send(sock, rec, opcode, fields):
    before = rec.seq_mark() if rec else 0
    sender.send_frame(sock, opcode, encode_message(fields, omit_zero=False),
                      rec.rc4_c2s)
    return before


def parse_stages(raw):
    """关卡配置 → 有序且去重的关卡号。

    接受列表（可混进 "1-10"）或字符串 "1-10,15,20"。空的返回 []。
    """
    if raw is None or raw == "" or raw == []:
        return []
    parts = raw if isinstance(raw, list) else [raw]
    out = []
    for part in parts:
        text = str(part).strip()
        if not text:
            continue
        for token in re.split(r"[,，\s]+", text):
            if not token:
                continue
            m = re.fullmatch(r"(\d+)\s*-\s*(\d+)", token)
            if m:
                a, b = int(m.group(1)), int(m.group(2))
                if a > b:
                    a, b = b, a
                out.extend(range(a, b + 1))
                continue
            if token.isdigit():
                out.append(int(token))
                continue
            raise ValueError(f"关卡写不认：{token}")
    seen = set()
    stages = []
    for n in out:
        if n <= 0 or n in seen:
            continue
        seen.add(n)
        stages.append(n)
    return stages


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
    before = _send(sock, rec, OP, {2: ("int32", TYPE_RESTART)})
    got2 = _await_response(
        sock, rec, RSE, before, 6.0,
        want=lambda d: d.get("type") == TYPE_RESTART)
    if not isinstance(got2, dict) or got2.get("result") != 0:
        return False, "type=2 重开没有成功"
    stage = _stage(got2)
    if stage != 1:
        return False, f"重开后关卡是 {stage}，不是第 1 关"
    return True, f"第4次已重开，从第 {cur} 关回到第 1 关"


def paid_restart(rec, sock):
    """第三次：付费重征。

    面板里 buyrefreshTimes（fightdata 字段 3）是剩余付费次数。
    免费重开次数还在时先不做。购买请求没有抓包，次数还在也不发。
    条件不满足时返回成功，表示这次不用做。
    """
    cur, panel = query(rec, sock)
    if not isinstance(panel, dict):
        return False, "没读到征战面板，第三次不做"
    refresh = _num(panel, "field2")
    bought = _num(panel, "field3")
    if refresh is None or bought is None:
        return False, "没读到免费重开或付费重征次数，不发"
    if bought < 1:
        return True, f"付费重征次数是 {bought}，第三次不用再做"
    if refresh > 0:
        return True, f"还有免费重开次数 {refresh}，第三次先不做"
    if TYPE_BUY is None:
        log.info("[征战] 第三次：当前第 %s 关，付费重征还剩 %s，购买请求没抓到，不发",
                 cur, bought)
        return False, "付费重征的购买请求还没抓到包，这一轮不发"
    log.info("[征战] 第三次：当前第 %s 关，付费重征 %s，发 type=%s",
             cur, bought, TYPE_BUY)
    before = _send(sock, rec, OP, {2: ("int32", TYPE_BUY)})
    got = _await_response(
        sock, rec, RSE, before, 6.0,
        want=lambda d: d.get("type") == TYPE_BUY)
    if not isinstance(got, dict) or got.get("result") != 0:
        return False, f"购买付费重征被拒 result={None if not isinstance(got, dict) else got.get('result')}"
    bought2 = _num(got, "field3")
    refresh2 = _num(got, "field2")
    if bought2 is None or bought2 >= bought or not refresh2:
        return False, f"购买之后次数不对（付费 {bought2}，重开 {refresh2}），停手"
    before = _send(sock, rec, OP, {2: ("int32", TYPE_RESTART)})
    got2 = _await_response(
        sock, rec, RSE, before, 6.0,
        want=lambda d: d.get("type") == TYPE_RESTART)
    if not isinstance(got2, dict) or got2.get("result") != 0:
        return False, "type=2 重开没有成功"
    stage = _stage(got2)
    if stage != 1:
        return False, f"重开后关卡是 {stage}，不是第 1 关"
    return True, f"第三次已重开，从第 {cur} 关回到第 1 关"


def fight_once(rec, sock):
    """打当前关。返回结算回包（result 为 1 或 2），没有就 None。"""
    before = _send(sock, rec, OP, {
        1: ("bool", True), 2: ("int32", TYPE_FIGHT),
    })
    return _await_response(
        sock, rec, RSE, before, 8.0,
        want=lambda d: d.get("type") == TYPE_FIGHT and d.get("result") in (1, 2))


def fight(rec, sock, stages, interval=1.0):
    """按名单打。当前关不在名单里就停，不跳关。

    返回 (是否把名单里、且从当前关能连续打到的都打完, 说明)。
    """
    stages = parse_stages(stages)
    if not stages:
        return False, "没有配置关卡"
    if len(stages) > HARD_MAX:
        return False, f"关卡超过 {HARD_MAX} 个，不打"
    want = set(stages)
    cur, _panel = query(rec, sock)
    if cur is None:
        return False, "没读到当前关，不打"
    log.info("[征战] 当前第 %s 关，名单 %s", cur, _brief(stages))
    if cur not in want:
        return False, f"当前第 {cur} 关不在名单里，请求不能跳关，停手"
    start = cur
    interval = float(interval if interval is not None else 1)
    done = []
    for _ in range(len(stages)):
        if cur not in want:
            break
        got = fight_once(rec, sock)
        if not isinstance(got, dict):
            return False, f"第 {cur} 关没有结算回包，已过 {_brief(done)}"
        nxt = _stage(got)
        result = got.get("result")
        if result != 1 or nxt is None or nxt == cur:
            return False, f"第 {cur} 关没过去 result={result}，已过 {_brief(done)}"
        done.append(cur)
        log.info("[征战] 第 %s 关过了，下一关 %s", cur, nxt)
        cur = nxt
        if cur not in want:
            break
        if interval > 0:
            _nap(interval)
    missed = [n for n in stages if n not in set(done) and n >= start]
    if missed:
        return False, (f"已过 {_brief(done)}，当前第 {cur} 关。"
                       f"名单里的 {_brief(missed)} 没打到，中间关不在名单里，不能跳")
    return True, f"打完 {_brief(done)}，当前第 {cur} 关"


def _brief(nums):
    if not nums:
        return "无"
    if len(nums) <= 8:
        return "、".join(str(n) for n in nums)
    return f"{nums[0]}–{nums[-1]} 共 {len(nums)} 关"
