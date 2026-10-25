# tankstorm-keepalive  Copyright (C) 2026 Dimlitter
# SPDX-License-Identifier: AGPL-3.0-or-later
"""精准打击。2026-10-25 17:21–17:22 抓包。

RcePreciseStrikeOpt / 04d9。type=0 打开，type=1 打一次。不发 nRewardID。
打开时 nCurEnergy=5，打一次变成 4。nCurCircle 62→63，nRandNum 变成 5。
回包里没有活动开始或结束时间。
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


def run_strike(rec, sock, on_progress=None):
    """先读剩余次数，再打到次数下降为 0 或不再下降。"""
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
    before = left
    while done < left and done < 99:
        raise_if_stopped()
        got = _send(rec, sock, 1)
        if not isinstance(got, dict):
            head = f"已打 {done} 次。" if done else ""
            return False, head + "打击没有回包"
        now = _one(got.get("nCurEnergy"))
        ret = _one(got.get("nRet"))
        if now is None or now >= before:
            if ret not in (0, None):
                head = f"已打 {done} 次。" if done else ""
                return False, head + f"被拒 nRet={ret}"
            break
        done += 1
        before = now
        rolled = _one(got.get("nRandNum"))
        if rolled:
            rolls.append(rolled)
        now_circle = _one(got.get("nCurCircle"))
        if now_circle is not None:
            circle = now_circle
        citydb.note_strike(now)
        if on_progress:
            on_progress(f"{done}/{left}，剩余 {now}")
        log.info("[精准打击] 第 %d 次，剩余 %d，抽出 %s", done, now, rolled)
        if now <= 0:
            break
        _nap(0.35)
    tail = f"，抽出 {'、'.join(str(n) for n in rolls)}" if rolls else ""
    if circle is not None:
        tail += f"，圈数 {circle}"
    return True, f"成功：精准打击 {done} 次，剩余 {before}{tail}"
