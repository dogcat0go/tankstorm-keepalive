# tankstorm-keepalive  Copyright (C) 2026 Dimlitter
# SPDX-License-Identifier: AGPL-3.0-or-later
"""精准打击。RcePreciseStrikeOpt / 04d9。type=0 打开，type=1 打一次。不发 nRewardID。

2026-11-06 21:34:56–21:35:27 抓包（logs/packets/2026-11-06.jsonl）。
标记从「打算开始捕捉活动：精准打击」到「活动：精准打击 结束」。
打开的正文是 08 00，打一次是 08 01，都只有 type。
打开时剩余 76。连打 13 次，每次剩余减 1，停在 63。
骰子 nRandNum 是 1 到 6。圈数不是每下都加。
第 7 下回包 nRewardID=3，背包物品 110009 从 97 加到 99。
请求仍然不带 nRewardID。回包里没有活动起止时间。

2026-11-19 19:30:35–19:31:55 又抓了一次（logs/packets/2026-11-19.jsonl）。
标记从「准备开始-精准打击」到「结束-精准打击」。
打开仍是 08 00，打一下仍是 08 01。线上类名换成了 _-3It / _-sK，字段号没变。
打开时剩余 176、圈数 0。连打 26 次，剩余 176→150，圈数 0→16。
最后一下奖励标记亮起，背包物品 10070 加了 1。
奖励不是每下都有。没出奖励也继续打，直到剩余次数变成 0。

2026111802 的回包里，剩余次数 nCurEnergy 在线上第 5 号，
nPartNum 在第 3 号。2026102901 的旧对照把这两号对反了，
打开时第 3 号是 0，页面就把剩余次数显示成 0，并且一次都不打。
"""

from . import citydb, sender
from .daily import _await_response, _nap, _one, raise_if_stopped
from .log import get_logger
from .proto_encode import encode_message

log = get_logger()


def _send(rec, sock, typ):
    mark = rec.seq_mark() if rec else 0
    sender.send_frame(
        sock, "04d9",
        encode_message({1: ("int32", typ)}, omit_zero=False),
        rec.rc4_c2s)
    return _await_response(
        sock, rec, "RsePreciseStrikeOpt", mark, 6.0,
        want=lambda d, typ=typ: isinstance(d, dict) and d.get("type") == typ)


def peek_strike(rec, sock):
    """打开精准打击，把剩余次数记给页面。不打击。"""
    got = _send(rec, sock, 0)
    if not isinstance(got, dict):
        return None
    left = _one(got.get("nCurEnergy"))
    if left is not None:
        citydb.note_strike(left)
    return got


def _landed(before_left, before_circle, data):
    """次数下降，或圈数增加，才算这一下打中。"""
    now = _one(data.get("nCurEnergy")) if isinstance(data, dict) else None
    circle = _one(data.get("nCurCircle")) if isinstance(data, dict) else None
    if now is not None and before_left is not None and now < before_left:
        return True, now, circle
    if (circle is not None and before_circle is not None and circle > before_circle):
        return True, now if now is not None else before_left, circle
    return False, now if now is not None else before_left, circle


def strike_once(rec, sock):
    """打开后只打一下，用来对这次抓包。不把剩余打完。"""
    panel = peek_strike(rec, sock)
    if not isinstance(panel, dict):
        return False, "失败：没打开精准打击"
    left = _one(panel.get("nCurEnergy"))
    if left is None:
        return False, "失败：读不到剩余次数"
    if left <= 0:
        return True, "成功：没有剩余次数"
    got = _send(rec, sock, 1)
    if not isinstance(got, dict):
        return False, "打击没有回包"
    ret = _one(got.get("nRet"))
    if ret not in (0, None):
        return False, f"被拒 nRet={ret}，剩余 {left}"
    now = _one(got.get("nCurEnergy"))
    if now is not None:
        citydb.note_strike(now)
    roll = _one(got.get("nRandNum"))
    circle = _one(got.get("nCurCircle"))
    reward = _one(got.get("nRewardID"))
    text = f"成功：打了 1 次，剩余 {left}→{now}，骰子 {roll}，圈数 {circle}"
    if reward:
        text += f"，奖励 {reward}"
    return True, text


def run_strike(rec, sock, on_progress=None):
    """有剩余次数就一直打，打到服务器上的次数变成 0。"""
    panel = peek_strike(rec, sock)
    if not isinstance(panel, dict):
        return False, "失败：没打开精准打击"
    left = _one(panel.get("nCurEnergy"))
    if left is None:
        return False, "失败：读不到剩余次数"
    circle = _one(panel.get("nCurCircle"))
    if left <= 0:
        return True, "成功：没有剩余次数"
    done = 0
    rolls = []
    misses = 0
    # 奖励可有可无。次数还在就继续打。176 这种次数要能打完，上限只防死循环。
    while left > 0 and done < 999 and misses < 5:
        raise_if_stopped()
        got = _send(rec, sock, 1)
        if not isinstance(got, dict):
            head = f"已打 {done} 次。" if done else ""
            return False, head + "打击没有回包"
        ret = _one(got.get("nRet"))
        if ret not in (0, None):
            head = f"已打 {done} 次。" if done else ""
            return False, head + f"被拒 nRet={ret}，剩余 {left}"
        hit, now, now_circle = _landed(left, circle, got)
        if not hit:
            fresh = _send(rec, sock, 0)
            hit, now, now_circle = _landed(left, circle, fresh)
            if isinstance(fresh, dict) and _one(fresh.get("nCurEnergy")) is not None:
                now = _one(fresh.get("nCurEnergy"))
        if not hit:
            misses += 1
            if now is not None:
                left = now
                citydb.note_strike(left)
            _nap(1.0)
            continue
        misses = 0
        done += 1
        if now is not None:
            left = now
        if now_circle is not None:
            circle = now_circle
        rolled = _one(got.get("nRandNum"))
        if rolled:
            rolls.append(rolled)
        citydb.note_strike(left)
        if on_progress:
            on_progress(f"已打 {done} 次，剩余 {left}")
        log.info("[精准打击] 第 %d 次，剩余 %d，圈数 %s，抽出 %s",
                 done, left, circle, rolled)
        if left <= 0:
            break
        _nap(0.35)
    tail = f"，抽出 {'、'.join(str(n) for n in rolls)}" if rolls else ""
    if circle is not None:
        tail += f"，圈数 {circle}"
    if left > 0:
        head = f"已打 {done} 次。" if done else ""
        if misses >= 5:
            return False, head + f"连续 {misses} 次次数没有下降，剩余 {left}" + tail
        return False, head + f"剩余 {left} 次还没打完" + tail
    return True, f"成功：精准打击 {done} 次，剩余 {left}{tail}"
