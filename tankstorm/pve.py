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

所以只能打「当前这一关」。请求里不能指定关卡号。

网页和命令行 --pve 同一套：留空从当前关打到打不过，只填一个数字就从当前关打到这一关。
每次重开之后都接着打，不会连着重开。
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
# 2026-09-27 00:40 抓包：type=4 买一次重开，扣 100 勋章。
# buyrefreshTimes 从 0 变成 1，并且重开后仍是 1，不是剩余次数。
TYPE_BUY = 4
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
        return False, "没读到重开次数或 VIP 加次，第4次不发"
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
    """第三次：花勋章买一次重开，再把关卡打回第 1 关。

    2026-09-27 00:40 抓包。buyrefreshTimes 是已经买过的次数，不是剩余次数。
    只在 buyrefreshTimes=0 且 refreshTimes=0 时发 type=4。
    type=4 会扣 100 勋章。次数没对上就停，不再发 type=2。
    条件不满足时返回成功，表示这次不用做。
    """
    cur, panel = query(rec, sock)
    if not isinstance(panel, dict):
        return False, "没读到征战面板，第三次不做"
    refresh = _num(panel, "field2")
    bought = _num(panel, "field3")
    if refresh is None or bought is None:
        return False, "没读到免费重开或付费重征次数，不发"
    if bought > 0:
        return True, f"付费重开已经用过（buyrefreshTimes={bought}），不用再做"
    if refresh > 0:
        return True, f"还有免费重开次数 {refresh}，第三次先不做"
    log.info("[征战] 第三次：当前第 %s 关，发 type=4", cur)
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


def end_stage(raw):
    """指定关卡。一个数字就是终点，一段或一串取最大的那个。空的返回 None。"""
    text = str(raw or "").strip()
    if not text:
        return None
    best = None
    for token in re.split(r"[,，\s]+", text):
        if not token:
            continue
        m = re.fullmatch(r"(\d+)\s*-\s*(\d+)", token)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if a > b:
                a, b = b, a
            n = b
        elif token.isdigit():
            n = int(token)
        else:
            raise ValueError(f"关卡写不认：{token}")
        if n <= 0:
            continue
        if n > HARD_MAX:
            raise ValueError(f"关卡超过 {HARD_MAX}")
        best = n if best is None else max(best, n)
    return best


def _restart_once(rec, sock):
    """发一次免费 type=2。回到第 1 关才算成功。"""
    before = _send(sock, rec, OP, {2: ("int32", TYPE_RESTART)})
    got = _await_response(
        sock, rec, RSE, before, 6.0,
        want=lambda d: d.get("type") == TYPE_RESTART)
    if not isinstance(got, dict) or got.get("result") != 0:
        result = None if not isinstance(got, dict) else got.get("result")
        return False, f"type=2 被拒 result={result}"
    stage = _stage(got)
    if stage != 1:
        return False, f"重开后关卡是 {stage}，不是第 1 关"
    return True, "已重开"


def _free_restarts(rec, sock, already, interval, panel=None, limit=None):
    """做今天还剩的免费重开。一天最多 2 次，并且不超过面板上的剩余次数。

    返回 (是否按规则做完, 说明, 今日已重开次数)。
    """
    try:
        already = int(already or 0)
    except (TypeError, ValueError):
        already = 0
    if already < 0:
        already = 0
    room = 2 - already
    if room <= 0:
        return True, "今日免费重开 2 次已用完", already
    if panel is None:
        _cur, panel = query(rec, sock)
    if not isinstance(panel, dict):
        return False, "没读到征战面板，不重开", already
    refresh = _num(panel, "field2")
    if refresh is None:
        return False, "没读到免费重开次数，不发", already
    if refresh <= 0:
        return True, "免费重开次数已用完", already
    times = min(room, int(refresh))
    if limit:
        times = min(times, int(limit))
    done = 0
    for i in range(times):
        ok, why = _restart_once(rec, sock)
        if not ok:
            return False, why, already + done
        done += 1
        log.info("[征战] 免费重开 %s/%s", done, times)
        if i + 1 < times and interval > 0:
            _nap(interval)
    return True, f"免费重开 {done} 次", already + done


def _blank_stages(raw):
    if raw is None or raw == []:
        return True
    return isinstance(raw, str) and not str(raw).strip()


def _single_end(raw):
    """单独一个正整数才是终点关。区间、逗号名单、空都不是。"""
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, list):
        if len(raw) != 1:
            return None
        return _single_end(raw[0])
    if isinstance(raw, int):
        return raw if raw > 0 else None
    text = str(raw).strip()
    if re.fullmatch(r"\d+", text):
        n = int(text)
        return n if n > 0 else None
    return None


def _stuck(why):
    """没打过这一关。没读到回包不算，那种要停，不能接着重开。"""
    text = str(why or "")
    return "没过去" in text and "没有结算" not in text


def _fight_until(rec, sock, end, interval):
    """end 为空就从当前关打到打不过。否则打到终点关（含）。"""
    cur, _panel = query(rec, sock)
    if cur is None:
        return False, "没读到当前关，不打"
    if end is None:
        log.info("[征战] 当前第 %s 关，打到打不过", cur)
    else:
        if cur > end:
            return False, f"当前第 {cur} 关，已经过了终点第 {end} 关"
        span = end - cur + 1
        if span > HARD_MAX:
            return False, f"从第 {cur} 关打到第 {end} 关，超过 {HARD_MAX} 关，不打"
        log.info("[征战] 当前第 %s 关，打到终点第 %s 关", cur, end)
    interval = float(interval if interval is not None else 1)
    done = []
    for _ in range(HARD_MAX):
        if end is not None and cur > end:
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
        if end is not None and cur > end:
            break
        if interval > 0:
            _nap(interval)
    return True, f"打完 {_brief(done)}，当前第 {cur} 关"


def campaign(rec, sock, stages, already=0, interval=1.0):
    """每日任务和「打这些关」同一套。

    填了终点：还没到就只打到终点，这一轮不重开。到了或超过终点，免费重开一次就打一轮，
    最多两次，两次之间会打，不会连着重开。
    没填终点：从当前关打到打不过，然后同样按次免费重开再打。
    打到打不过算这一轮打完。没读到回包才算失败。
    返回 (是否成功, 说明, 今日免费重开次数, 这一轮是否还在往终点打)。
    最后一项为真时，调用方不要接着做第三次、第4次。
    """
    interval = float(interval if interval is not None else 1)
    try:
        already = int(already or 0)
    except (TypeError, ValueError):
        already = 0
    if already < 0:
        already = 0
    text = "" if _blank_stages(stages) else str(stages).strip()
    try:
        end = end_stage(text) if text else None
    except ValueError as exc:
        return False, f"失败：{exc}", already, True
    cur, panel = query(rec, sock)
    if cur is None:
        return False, "失败：没读到当前关，不打也不重开", already, True
    log.info("[征战] 当前第 %s 关，终点 %s", cur, end if end is not None else "打到打不过")
    total = already
    notes = []

    def _once(panel_now, allow_stuck):
        nonlocal total
        ok, why, nxt = _free_restarts(
            rec, sock, total, interval, panel=panel_now, limit=1)
        if not ok:
            total = nxt
            return False, why
        if nxt <= total:
            return None, why
        total = nxt
        ok2, why2 = fight(rec, sock, text, interval)
        why = f"{why}。然后{why2}"
        if ok2 or (allow_stuck and _stuck(why2)):
            return True, why
        return False, why

    if end is not None and cur < end:
        ok, why = fight(rec, sock, text, interval)
        if not ok:
            if not str(why).startswith("失败"):
                why = f"失败：{why}"
            return False, why, total, True
        return True, why, total, True
    if end is not None:
        where = "已到" if cur == end else "已过"
        notes.append(f"当前第 {cur} 关，{where}指定的第 {end} 关")
    else:
        ok, why = fight(rec, sock, text, interval)
        notes.append(why)
        if not ok and not _stuck(why):
            if not str(why).startswith("失败"):
                why = f"失败：{why}"
            return False, why, total, True
    used = panel if end is not None else None
    for _ in (1, 2):
        did, why = _once(used, end is None)
        used = None
        notes.append(why)
        if did is None:
            break
        if not did:
            return False, f"失败：{'。'.join(notes)}", total, True
    return True, "。".join(notes), total, False


def fight_once(rec, sock):
    """打当前关。返回结算回包（result 为 1 或 2），没有就 None。"""
    before = _send(sock, rec, OP, {
        1: ("bool", True), 2: ("int32", TYPE_FIGHT),
    })
    return _await_response(
        sock, rec, RSE, before, 8.0,
        want=lambda d: d.get("type") == TYPE_FIGHT and d.get("result") in (1, 2))


def fight(rec, sock, stages, interval=1.0):
    """打征战。和命令行 --pve 同一套。

    留空：从当前关打到打不过。
    一个正整数：终点关。从当前关打到这一关（含）。已经过了终点就不打。
    区间或逗号名单：当前关必须在名单里，不能跳关。

    返回 (是否打完, 说明)。
    """
    if _blank_stages(stages):
        return _fight_until(rec, sock, None, interval)
    end = _single_end(stages)
    if end is not None:
        if end > HARD_MAX:
            return False, f"终点关超过 {HARD_MAX}，不打"
        return _fight_until(rec, sock, end, interval)
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
