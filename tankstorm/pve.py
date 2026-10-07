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

网页和命令行 --pve 同一套：留空只打当前关，只填一个数字就从当前关打到这一关。
每日任务里，当前关到了或超过终点时，免费重开一次就从第 1 关打到终点。
打到了还有免费次数，再重开一次再打。这一轮没打过终点就停，不再重开。
第三次、第4次也是重开之后从第 1 关打到终点，打完才算这一次做成。
"""

import re

from . import sender
from .daily import _await_response, _nap, raise_if_stopped
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


def extra_campaign(rec, sock, stages, interval=1.0, kind=3, already_done=False):
    """第三次或第4次：必要时重开，再从当前关打到终点。

    kind 为 3 或 4。返回 (是否成功, 说明, 这一次是否已经打完, 是否还在往终点打)。
    还没打到终点时不要接着做下一次。只重开、没打完不算做成。
    """
    from . import daily

    daily.note_campaign_round(3 if kind == 3 else 4)
    interval = float(interval if interval is not None else 1)
    text = "" if _blank_stages(stages) else str(stages).strip()
    try:
        end = end_stage(text) if text else None
    except ValueError as exc:
        return False, str(exc), False, True

    cur, panel = query(rec, sock)
    if cur is None or not isinstance(panel, dict):
        return False, "没读到当前关，不打", False, True
    daily.note_campaign_stage(cur)

    refresh = _num(panel, "field2")
    bought = _num(panel, "field3")
    vip = _num(panel, "field10")
    if refresh is None:
        return False, "没读到免费重开次数，不发", False, True
    if refresh > 0:
        which = "第三次" if kind == 3 else "第4次"
        return True, f"还有免费重开次数 {refresh}，{which}先不做", False, False

    below = end is not None and cur < end
    if below:
        if kind == 3:
            fourth_started = vip is not None and vip < 1
            if already_done and fourth_started:
                return True, "今日已做过，跳过", True, False
            if not (bought and bought > 0):
                return True, "还没到终点，第三次先不做", False, True
        elif vip and vip > 0:
            return True, "第三次还没打完，这一轮先不做第4次", False, True
        log.info("[征战] 第%s次：当前第 %s 关，接着打到终点第 %s 关",
                 kind, cur, end)
        ok, why = fight(rec, sock, text, interval)
        if not ok:
            if not str(why).startswith("失败"):
                why = f"失败：{why}"
            return False, why, False, True
        return True, why, True, False

    if already_done:
        return True, "今日已做过，跳过", True, False

    restart = paid_restart if kind == 3 else vip_restart
    ok, why = restart(rec, sock)
    if not ok:
        return False, why, False, False
    if "已重开" not in str(why):
        finished = "不用再做" in str(why) or "已经用过" in str(why)
        return True, why, finished, False

    daily.note_campaign_stage(1)
    if interval > 0:
        _nap(interval)
    ok2, why2 = fight(rec, sock, text, interval)
    text_out = f"{why}。{why2}"
    if not ok2:
        if not str(why2).startswith("失败"):
            why2 = f"失败：{why2}"
        return False, f"{why}。{why2}", False, True
    return True, text_out, True, False


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


def _free_round(already):
    """免费重开后正在打的是第几次。没有重开过算第 1 次，最多第 2 次。"""
    try:
        n = int(already or 0)
    except (TypeError, ValueError):
        n = 0
    if n < 1:
        return 1
    if n > 2:
        return 2
    return n


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
    from . import daily
    daily.note_campaign_stage(1)
    return True, "已重开"


def _restart_if_room(rec, sock, already, panel=None):
    """还有今日名额、面板上也还有次数时，免费重开一次。

    一天最多 2 次。返回 (是否按规则处理完, 说明, 今日已重开次数, 这次有没有重开)。
    次数用完时第一项为真，第四项为假。
    """
    try:
        already = int(already or 0)
    except (TypeError, ValueError):
        already = 0
    if already < 0:
        already = 0
    if already >= 2:
        return True, "今日免费重开 2 次已用完", already, False
    if panel is None:
        _cur, panel = query(rec, sock)
    if not isinstance(panel, dict):
        return False, "没读到征战面板，不重开", already, False
    refresh = _num(panel, "field2")
    if refresh is None:
        return False, "没读到免费重开次数，不发", already, False
    if refresh <= 0:
        return True, "免费重开次数已用完", already, False
    from . import daily
    daily.note_campaign_round(_free_round(already + 1))
    ok, why = _restart_once(rec, sock)
    if not ok:
        return False, why, already, False
    done = already + 1
    log.info("[征战] 免费重开 %s/2", done)
    return True, f"免费重开 {done}/2", done, True


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


def _fight_until(rec, sock, end, interval):
    """end 为空只打当前关。否则从当前关打到终点关（含）。和命令行 --pve 同一套。"""
    cur, _panel = query(rec, sock)
    if cur is None:
        return False, "没读到当前关，不打"
    if end is None:
        end = cur
        log.info("[征战] 留空，只打当前第 %s 关", cur)
    else:
        if cur > end:
            return False, f"当前第 {cur} 关，已经过了终点第 {end} 关"
        span = end - cur + 1
        if span > HARD_MAX:
            return False, f"从第 {cur} 关打到第 {end} 关，超过 {HARD_MAX} 关，不打"
        log.info("[征战] 当前第 %s 关，打到终点第 %s 关", cur, end)
    from . import daily
    daily.note_campaign_stage(cur)
    interval = float(interval if interval is not None else 1)
    done = []
    for _ in range(HARD_MAX):
        raise_if_stopped()
        if cur > end:
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
        daily.note_campaign_stage(cur)
        if cur > end:
            break
        if interval > 0:
            _nap(interval)
    return True, f"打完 {_brief(done)}，当前第 {cur} 关"


def _restart_and_refight(rec, sock, end, cur, panel, total, interval):
    """当前关已经到了终点。每重开一次，立刻从第 1 关打到终点。

    这一轮没打过终点就停，不再花下一次免费重开。
    """
    where = "已到" if cur == end else "已过"
    opened = cur
    rounds = []
    fought = False
    while cur >= end:
        raise_if_stopped()
        if fought and interval > 0:
            _nap(interval)
        ok, why, total, restarted = _restart_if_room(
            rec, sock, total, panel=panel)
        panel = None
        if not ok:
            return False, (
                f"失败：当前第 {opened} 关，{where}指定的第 {end} 关，{why}"
            ), total, True
        if not restarted:
            if not fought:
                return True, (
                    f"当前第 {opened} 关，{where}指定的第 {end} 关，{why}"
                ), total, False
            break
        rounds.append(why)
        if interval > 0:
            _nap(interval)
        ok2, why2 = fight(rec, sock, str(end), interval)
        fought = True
        rounds.append(why2)
        if not ok2:
            return False, (
                f"失败：当前第 {opened} 关，{where}指定的第 {end} 关。"
                + "。".join(rounds)
            ), total, True
        cur, panel = query(rec, sock)
        if cur is None:
            return False, (
                f"失败：当前第 {opened} 关，{where}指定的第 {end} 关。"
                + "。".join(rounds) + "。打完后没读到当前关"
            ), total, True
        if cur < end:
            return False, (
                f"失败：当前第 {opened} 关，{where}指定的第 {end} 关。"
                + "。".join(rounds)
                + f"。停在第 {cur} 关，还没到终点，不再重开"
            ), total, True
    text = (
        f"当前第 {opened} 关，{where}指定的第 {end} 关。"
        + "。".join(rounds)
    )
    if cur is not None:
        text += f"。现在第 {cur} 关"
    return True, text, total, False


def campaign(rec, sock, stages, already=0, interval=1.0):
    """每日任务里的征战。开打走 fight()，和命令行 --pve 同一套。

    还没到终点就直接打，不重开。到了或超过终点，免费重开一次，再从第 1 关
    打到终点。打到了还有免费次数，再重开一次再打。这一轮没打过终点就停。
    没填终点就只打当前关。
    返回 (是否成功, 说明, 今日免费重开次数, 这一轮是否还在往终点打)。
    最后一项为真时，调用方不要接着做第三次、第4次。打到终点之后
    这一项为假，第三次重开后要从第 1 关打到终点，打完才做第4次。
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
    log.info("[征战] 当前第 %s 关，终点 %s", cur, end if end is not None else "未填")
    from . import daily
    total = already
    if end is not None and cur >= end:
        daily.note_campaign_round(_free_round(already + 1))
        daily.note_campaign_stage(cur)
        return _restart_and_refight(
            rec, sock, end, cur, panel, total, interval)
    daily.note_campaign_round(_free_round(already))
    daily.note_campaign_stage(cur)
    ok, why = fight(rec, sock, text, interval)
    if not ok:
        if not str(why).startswith("失败"):
            why = f"失败：{why}"
        return False, why, total, True
    return True, why, total, False


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

    留空：读面板，只打当前这一关。
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
    from . import daily
    daily.note_campaign_stage(cur)
    if cur not in want:
        return False, f"当前第 {cur} 关不在名单里，请求不能跳关，停手"
    start = cur
    interval = float(interval if interval is not None else 1)
    done = []
    for _ in range(len(stages)):
        raise_if_stopped()
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
        daily.note_campaign_stage(cur)
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
