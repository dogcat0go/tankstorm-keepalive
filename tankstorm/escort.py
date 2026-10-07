# tankstorm-keepalive  Copyright (C) 2026 Dimlitter
# SPDX-License-Identifier: AGPL-3.0-or-later
"""物资护送。

2026-10-04 抓包（logs/packets/2026-10-04.jsonl，02:12:37–02:13:53）：

开面板 RceMaterialProtect type=0，刷新别人的车 type=3，查看某一辆 type=7，
掠夺 type=4。查看和掠夺都带列表位置（从 1 起）和 uid。
品质 4 是橙色。列表里没有物资，物资在查看回包的第 10 号（车上全部）
和第 11 号（已经被拿走）。02:36 同一辆车掠夺过后，第 9 号次数变成 2，
第 8 号状态从 0 变成 1。次数是 0 时可以抢 2 次，已经是 1 时再抢 1 次。
掠夺成功时回包 result=0，并带上这次拿到的物品。
"""

from . import sender
from .daily import _await_response, _nap
from .log import get_logger
from .proto_encode import encode_message
from .schema import _varint

log = get_logger()

OP = "04fb"
RSE = "RseMaterialProtect"
ORANGE = 4
CORE = 10238
HITS = 2
TYPE_OPEN = 0
TYPE_REFRESH = 3
TYPE_PLUNDER = 4
TYPE_VIEW = 7
NAMES = {
    10244: "超级能量石",
    21565: "老兵勋章",
    10308: "高温合金",
    10281: "英魂",
    10238: "火炮核心",
}


def _send(sock, rec, fields):
    before = rec.seq_mark() if rec else 0
    sender.send_frame(sock, OP, encode_message(fields, omit_zero=False),
                      rec.rc4_c2s)
    return before


def _fields(body):
    """拆一层 protobuf。长度字段保留原始字节，变长整数保留整数。"""
    out, i, n = [], 0, len(body)
    while i < n:
        key, i = _varint(body, i)
        if key is None:
            return None
        wt, fn = key & 7, key >> 3
        if fn == 0 or wt in (3, 4, 6, 7):
            return None
        if wt == 0:
            v, i = _varint(body, i)
            if v is None:
                return None
        elif wt == 1:
            if i + 8 > n:
                return None
            v, i = body[i:i + 8], i + 8
        elif wt == 5:
            if i + 4 > n:
                return None
            v, i = body[i:i + 4], i + 4
        else:
            ln, i = _varint(body, i)
            if ln is None or ln > n - i:
                return None
            v, i = body[i:i + ln], i + ln
        out.append((fn, v))
    return out


def _text(raw):
    if not isinstance(raw, (bytes, bytearray)):
        return None
    try:
        s = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return s if s.isprintable() else None


def _cars(body):
    """别人的车。OtheCarInfo：编号是字符串，结束时间是整数，品质是第 4 号。"""
    rows = _fields(body) if body else None
    if not rows:
        return []
    groups = {}
    for fn, v in rows:
        groups.setdefault(fn, []).append(v)
    found = []
    for vals in groups.values():
        cars = []
        for raw in vals:
            item = _fields(raw) if isinstance(raw, (bytes, bytearray)) else None
            if not item:
                cars = []
                break
            uid = end = quality = None
            plunder = 0
            for fn, v in item:
                if fn == 1:
                    uid = _text(v)
                elif fn == 2 and isinstance(v, int):
                    end = v
                elif fn == 4 and isinstance(v, int):
                    quality = v
                elif fn == 5 and isinstance(v, int):
                    plunder = v
            if not (uid and uid.isdigit() and end is not None and quality is not None):
                cars = []
                break
            cars.append({"uid": uid, "quality": quality, "plunder": plunder})
        if cars:
            found.append(cars)
    if not found:
        return []
    cars = max(found, key=len)
    for i, car in enumerate(cars):
        car["index"] = i + 1
    return cars


def _items(rows, fn):
    out = []
    for num, raw in rows:
        if num != fn or not isinstance(raw, (bytes, bytearray)):
            continue
        item = _fields(raw)
        if not item:
            continue
        iid = cnt = None
        for k, v in item:
            if k == 1 and isinstance(v, int):
                iid = v
            elif k == 2 and isinstance(v, int):
                cnt = v
        if iid is not None:
            out.append((iid, 0 if cnt is None else cnt))
    return out


def _cargo(rows):
    """第 10 号是车上全部物资，第 11 号是已经被拿走的。

    02:36:04 的查看回包里，第 11 号是第 10 号的一部分，对得上掠夺后少掉的那些。
    """
    return _items(rows, 10), _items(rows, 11)


def _detail(body):
    rows = _fields(body) if body else None
    if not rows:
        return None
    blobs = [v for fn, v in rows if fn == 13 and isinstance(v, (bytes, bytearray))]
    if not blobs:
        return None
    item = _fields(blobs[-1])
    if not item:
        return None
    name = ""
    quality = power = state = plunder = None
    for fn, v in item:
        if fn == 2:
            name = (_text(v) or "").strip()
        elif fn == 4 and isinstance(v, int):
            power = v
        elif fn == 7 and isinstance(v, int):
            quality = v
        elif fn == 8 and isinstance(v, int):
            state = v
        elif fn == 9 and isinstance(v, int):
            plunder = v
    cargo, taken = _cargo(item)
    return {"name": name, "quality": quality, "power": power,
            "state": state, "plunder": plunder,
            "cargo": cargo, "taken": taken}


def _fmt(items):
    if not items:
        return "无"
    return "，".join(f"{NAMES.get(iid, '物品' + str(iid))}×{cnt}" for iid, cnt in items)


def _count(items, iid):
    return sum(cnt for i, cnt in items if i == iid)


def _pairs(nums):
    if not isinstance(nums, list) or len(nums) < 2 or len(nums) % 2:
        return []
    if any(isinstance(x, bool) or not isinstance(x, int) for x in nums):
        return []
    return [(nums[i], nums[i + 1]) for i in range(0, len(nums), 2)]


def _loot(data, raw=None):
    """掠夺回包第 12 号：物品编号、数量交替排列。"""
    v = data.get("field12") if isinstance(data, dict) else None
    if isinstance(v, int):
        v = [v]
    pairs = _pairs(v)
    if pairs or not raw:
        return pairs
    nums = []
    for fn, val in _fields(raw) or []:
        if fn != 12:
            continue
        if isinstance(val, int):
            nums.append(val)
        elif isinstance(val, (bytes, bytearray)):
            i, n = 0, len(val)
            while i < n:
                num, i = _varint(val, i)
                if num is None:
                    return []
                nums.append(num)
    return _pairs(nums)


def _ask(rec, sock, typ, index=None, uid=None):
    fields = {1: ("int32", typ)}
    if index is not None:
        fields[2] = ("int32", index)
    if uid is not None:
        fields[3] = ("string", uid)
    before = _send(sock, rec, fields)
    data = _await_response(
        sock, rec, RSE, before, 8.0,
        want=lambda d: isinstance(d, dict) and d.get("type") == typ)
    raw = None
    if data is not None:
        raw = getattr(rec, "body_of", {}).get(id(data))
    return data, raw


def _plunder_ok(data, raw):
    return isinstance(data, dict) and data.get("result") == 0 and bool(_loot(data, raw))


def escort(rec, sock, refreshes=0, interval=1.0):
    """一直刷新，直到橙色车里还有火炮核心并且还能抢，然后按剩余次数掠夺。"""
    data, raw = _ask(rec, sock, TYPE_OPEN)
    if not data:
        return False, "没有面板回包"
    log.info("[护送] 护送次数 %s，已掠夺 %s",
             data.get("ProtectTimes"), data.get("PlunderTimes"))
    used = 0
    limit = max(0, int(refreshes or 0))
    failed = set()
    while True:
        cars = _cars(raw)
        if not cars:
            return False, "回包里没有别人的车"
        log.info("[护送] 别人的车 %s",
                 " ".join(f"{c['index']}:品质{c['quality']}" for c in cars))
        oranges = [c for c in cars
                   if c["quality"] >= ORANGE and c["uid"] not in failed
                   and c["plunder"] < HITS]
        target = None
        for car in oranges:
            _nap(interval)
            view, view_raw = _ask(rec, sock, TYPE_VIEW, car["index"], car["uid"])
            if not view:
                log.info("[护送] 第 %d 辆没有查看回包", car["index"])
                continue
            detail = _detail(view_raw)
            if not detail:
                log.info("[护送] 第 %d 辆没有物资", car["index"])
                continue
            who = detail["name"] or car["uid"]
            left = _count(detail["cargo"], CORE) - _count(detail["taken"], CORE)
            done = detail["plunder"] if detail["plunder"] is not None else car["plunder"]
            hits = HITS - done
            log.info("[护送] 第 %d 辆 %s 品质%s 状态 %s 已抢 %s 次",
                     car["index"], who,
                     detail["quality"] if detail["quality"] is not None else car["quality"],
                     detail["state"] if detail["state"] is not None else "未知",
                     done)
            log.info("[护送] 车上物资 %s", _fmt(detail["cargo"]))
            log.info("[护送] 已被拿走 %s", _fmt(detail["taken"]))
            if detail["state"] not in (None, 0) or hits <= 0:
                log.info("[护送] 第 %d 辆不能再掠夺", car["index"])
                continue
            if left <= 0:
                log.info("[护送] 第 %d 辆没有剩下的火炮核心", car["index"])
                continue
            target = (car, who, hits)
            break
        if target:
            car, who, hits = target
            got = []
            for n in range(1, hits + 1):
                _nap(interval)
                ans, ans_raw = _ask(rec, sock, TYPE_PLUNDER, car["index"], car["uid"])
                loot = _loot(ans, ans_raw)
                if not _plunder_ok(ans, ans_raw):
                    failed.add(car["uid"])
                    log.info("[护送] %s 第 %d 次掠夺失败，继续刷新", who, n)
                    break
                got.extend(loot)
                log.info("[护送] %s 第 %d/%d 次掠夺 %s", who, n, hits, _fmt(loot))
            else:
                return True, f"{who} 掠夺 {hits} 次：{_fmt(got)}"
        if limit and used >= limit:
            return False, f"刷新 {used} 次，没有抢到带火炮核心的橙色车"
        used += 1
        log.info("[护送] 继续刷新 %d", used)
        _nap(interval)
        data, raw = _ask(rec, sock, TYPE_REFRESH)
        if not data:
            return False, "刷新没有回包"
