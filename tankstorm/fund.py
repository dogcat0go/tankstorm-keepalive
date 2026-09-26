# tankstorm-keepalive  Copyright (C) 2026 Dimlitter
# SPDX-License-Identifier: AGPL-3.0-or-later
"""成就建筑拨款。

2026-09-25 抓包（logs/packets/2026-09-25.jsonl，14:57–14:58）：

开卡（RceBagItemUse / 0440，optType=0）：
    槽位 1102  itemID=20818  count=4  → 每张石油 +10000000，油 157122 → 40157122
    槽位 1106  itemID=20817  count=4  → 每张金属 +10000000，金属 37137 → 40037137
    回包 RseBagItemUse.ret=0。槽位号会变，按背包里的 itemID 现查。

拨款（RceBuildingModify / 0414）：
    抓包里先有 type=93 点开建筑。2026-09-25 15:10 实发 type=93 被拒 error=28，
    所以不再点开，直接发 type=70 且 zhuziAll=1。帝国大厦 id=10138。
    客户端点「全部拨款」的线上字节是第 27 号 = 1（10 9a4f 18 46 d8 01 01）。
    仓库里有约 4000 万金属和石油时，这一条把两种都花掉，ZhuZiTimes=4。
    第 25 号（schema 里的 zhuziAll）实发只扣约 1000 万石油，金属不动。
    回包 error=0。一次「全部拨款」由服务端结算多次，
    所以次数参数是把这条请求重复发几次，不是往包里写个数。
"""

from . import sender
from .daily import _await_response, _nap, _pick_recent
from .log import get_logger
from .proto_encode import encode_message

log = get_logger()

OP_BUILD = "0414"
OP_USE = "0440"
RSE_BUILD = "RseBuildingModify"
RSE_USE = "RseBagItemUse"
RSE_BAG = "RseBagItemLst"
RSE_USER = "RseUserInfo"

TYPE_FUND = 70
# 全部拨款。schema 把这一号标成 addOil，和客户端对不上；以 14:58 的线上字节为准。
F_ZHUZI_ALL = 27

ITEM_METAL = 20817          # 1000 万金属卡
ITEM_OIL = 20818            # 1000 万石油卡
CARD_GAIN = 10_000_000

HARD_MAX_TIMES = 30
# 空仓库每种资源最多吃下 4 张 1000 万卡。一次只开 1 张，开满或被拒就停。
MAX_OPEN = 4


def _send(sock, rec, opcode, fields):
    before = rec.seq_mark() if rec else 0
    sender.send_frame(sock, opcode, encode_message(fields, omit_zero=False),
                      rec.rc4_c2s)
    return before


def _bag_items(data):
    items = (data or {}).get("bagItem") if isinstance(data, dict) else None
    if isinstance(items, dict):
        return [items]
    return items if isinstance(items, list) else []


def _find_card(bag, item_id):
    """正式背包里该物品的 (槽位, 数量)。没有就是 (None, 0)。"""
    for e in _bag_items(bag):
        if isinstance(e, dict) and e.get("field2") == item_id:
            return e.get("field1"), int(e.get("field4") or 0)
    return None, 0


def _wallet(data):
    if not isinstance(data, dict):
        return None, None
    return data.get("metal"), data.get("oil")


def _wan(n):
    if not isinstance(n, int) or isinstance(n, bool):
        return "未知"
    return f"{n / 10000:.2f}万"


def use_card(rec, sock, slot, item_id, count=1):
    """用一张资源卡。仓库有上限，不一次开一叠。"""
    if count != 1:
        return False, f"一次只开 1 张，收到 {count}"
    if not isinstance(slot, int) or isinstance(slot, bool):
        return False, "没有槽位，不用"
    before = _send(sock, rec, OP_USE, {
        3: ("int32", count), 4: ("int32", slot),
        5: ("int32", item_id), 7: ("int32", 0),
    })
    got = _await_response(
        sock, rec, RSE_USE, before, 6.0,
        want=lambda d: d.get("itemID") == item_id and d.get("id") == slot)
    if not isinstance(got, dict):
        return False, "用卡没有回包"
    if got.get("ret") not in (0, None):
        return False, f"用卡被拒 ret={got.get('ret')}"
    return True, got


def fund_once(rec, sock, building_id):
    """全部拨款：type=70，第 27 号 = 1。花掉当前金属和石油。"""
    before = _send(sock, rec, OP_BUILD, {
        2: ("int32", building_id), 3: ("int32", TYPE_FUND),
        F_ZHUZI_ALL: ("int32", 1),
    })
    got = _await_response(
        sock, rec, RSE_BUILD, before, 6.0,
        want=lambda d: d.get("type") == TYPE_FUND and d.get("id") == building_id)
    return got


def fund(rec, sock, building_id, times):
    """每次全部拨款之前，各开 4 张 1000 万金属卡和石油卡。

    返回 (是否按次数做完, 说明)。
    """
    building_id = int(building_id)
    times = int(times)
    if not (1 <= times <= HARD_MAX_TIMES):
        return False, f"次数 {times} 超出 1..{HARD_MAX_TIMES}"

    # type=93 点开建筑实发会被拒（error=28），不发。背包用登录时已经推下来的。
    got_bag = rec.latest.get(RSE_BAG) if rec else None
    bag = got_bag[1] if got_bag and isinstance(got_bag[1], dict) else None
    if not isinstance(bag, dict):
        log.info("[拨款] 登录包里没有背包，跳过开卡，直接拨款")
    done = 0
    last_times = None
    for i in range(1, times + 1):
        if isinstance(bag, dict):
            log.info("[拨款] 第 %d/%d 次：先各开 %d 张金属卡和石油卡",
                     i, times, MAX_OPEN)
            for item_id, name in ((ITEM_METAL, "金属"), (ITEM_OIL, "石油")):
                slot, count = _find_card(bag, item_id)
                if not count:
                    log.info("[拨款] 背包没有 1000 万%s卡", name)
                opened = 0
                while opened < MAX_OPEN and slot is not None and count > 0:
                    ok, why = use_card(rec, sock, slot, item_id, 1)
                    if not ok:
                        log.info("[拨款] %s卡第 %d 张没开成（%s），接着拨款",
                                 name, opened + 1, why)
                        break
                    opened += 1
                    metal, oil = _wallet(_pick_recent(rec, RSE_USER, 0))
                    log.info("[拨款] 第 %d 次 开了 1 张 1000 万%s卡（%d/%d），"
                             "基地金属 %s 石油 %s",
                             i, name, opened, MAX_OPEN, _wan(metal), _wan(oil))
                    _nap(0.4)
                    bag = _pick_recent(rec, RSE_BAG, 0) or bag
                    slot, count = _find_card(bag, item_id)
        metal, oil = _wallet(_pick_recent(rec, RSE_USER, 0))
        log.info("[拨款] 第 %d 次拨款前 基地金属 %s 石油 %s",
                 i, _wan(metal), _wan(oil))
        short = not (isinstance(metal, int) and metal >= 30_000_000
                     and isinstance(oil, int) and oil >= 30_000_000)
        if short:
            log.info("[拨款] 第 %d 次资源没补满，仍拨款一次，然后停止", i)
        got = fund_once(rec, sock, building_id)
        if not isinstance(got, dict):
            return False, f"第 {i} 次拨款没有回包，已完成 {done} 次"
        if got.get("error") not in (0, None):
            return False, (f"第 {i} 次拨款被拒 error={got.get('error')} "
                           f"resources={got.get('resources')}，已完成 {done} 次")
        now = (got.get("building") or {}).get("field35")
        metal, oil = _wallet(_pick_recent(rec, RSE_USER, 0))
        log.info("[拨款] 第 %d/%d 次 建筑 %s error=0 拨款值 %s → %s，"
                 "剩余金属 %s 石油 %s",
                 i, times, building_id, last_times, now, _wan(metal), _wan(oil))
        if isinstance(now, int) and isinstance(last_times, int) and now <= last_times:
            return False, (f"第 {i} 次拨款后拨款值没有增加"
                           f"（{last_times} → {now}），停在 {done} 次")
        last_times = now
        done += 1
        if short:
            return True, (f"建筑 {building_id} 拨款 {done} 次，拨款值={last_times}。"
                          f"第 {i} 次资源没补满，已拨款并停止")
        if i < times:
            _nap(0.4)
    return True, f"建筑 {building_id} 拨款 {done} 次，拨款值={last_times}"
