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

    2026-10-24 12:53–12:54 再对过，还是 type=70、第 27 号=1。
    埃菲尔铁塔 id=10133（类型 48），万磁陀螺 id=10144（类型 57）。
    两边都高于 3000 万时，各扣 3000 万金属和 3000 万石油，zhuziValue 升高。
"""

from . import sender
from .daily import (
    _await_response, _nap, _pick_recent, latest_card, note_bag_cards,
    raise_if_stopped,
)
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
# 2026-10-22 23:59 从资源卡包开出后用掉的卡。面额来自回包 medal/oil。
# 按面额从小到大，同面额先金属后石油。1000 万也开，排在最后。
# 旧的 30 万 20012、20019 不是这一批。
RESOURCE_CARDS = (
    (20008, 10, "10万金属"),
    (20015, 10, "10万石油"),
    (20051, 50, "50万金属"),
    (20050, 50, "50万石油"),
    (20030, 200, "200万金属"),
    (20025, 200, "200万石油"),
    (20116, 500, "500万金属"),
    (20117, 500, "500万石油"),
    (20817, 1000, "1000万金属"),
    (20818, 1000, "1000万石油"),
)
_CARD_ORDER = {item: i for i, (item, _wan, _name) in enumerate(RESOURCE_CARDS)}
BASE_CARDS = tuple((item, name) for item, _wan, name in RESOURCE_CARDS)
# 2026-10-25 15:49–15:50。整叠打开，每张同时得到金属卡和石油卡各 1 张。
# 20377 → 20051、20050。20835 → 20116、20117。然后再按面额开这些卡。
RESOURCE_BOXES = (
    (20377, "50万资源箱"),
    (20835, "500万资源箱"),
)
_BOX_ORDER = {item: i for i, (item, _name) in enumerate(RESOURCE_BOXES)}
_BOX_NAMES = {item: name for item, name in RESOURCE_BOXES}

HARD_MAX_TIMES = 999
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


def _fund_value(got):
    """回包里的拨款值。嵌套建筑没有 schema 名，常见是 field31。"""
    building = got.get("building") if isinstance(got, dict) else None
    if not isinstance(building, dict):
        return None
    for key in ("zhuziValue", "field31", "field35"):
        val = building.get(key)
        if isinstance(val, int) and not isinstance(val, bool):
            return val
        if isinstance(val, dict):
            inner = val.get("field1")
            if isinstance(inner, int) and not isinstance(inner, bool):
                return inner
    return None


def default_resource_ids():
    """没配过时先开资源箱，再按面额从小到大开资源卡。"""
    return ([item for item, _name in RESOURCE_BOXES]
            + [item for item, _wan, _name in RESOURCE_CARDS])


def resource_catalog():
    """给页面勾选。资源箱在前，后面按面额从小到大。"""
    boxes = [
        {"id": item, "wan": 0, "name": name, "box": True}
        for item, name in RESOURCE_BOXES
    ]
    cards = [
        {"id": item, "wan": wan, "name": name, "box": False}
        for item, wan, name in RESOURCE_CARDS
    ]
    return boxes + cards


def normalize_resource_ids(raw):
    """收成从小到大的物品号。不合法时返回 (None, 原因)。"""
    if isinstance(raw, str):
        raw = [part for part in raw.replace("，", ",").split(",") if part.strip()]
    if not isinstance(raw, (list, tuple)):
        return None, "要选择资源卡"
    chosen = []
    seen = set()
    for item in raw:
        try:
            item_id = int(str(item).strip())
        except (TypeError, ValueError):
            return None, "资源卡编号不对"
        if item_id not in _CARD_ORDER and item_id not in _BOX_ORDER:
            return None, "没有这张资源卡"
        if item_id in seen:
            continue
        seen.add(item_id)
        chosen.append(item_id)
    if not chosen:
        return None, "至少选一种资源卡"
    chosen.sort(key=lambda item_id: (
        (0, _BOX_ORDER[item_id]) if item_id in _BOX_ORDER else (1, _CARD_ORDER[item_id])))
    return chosen, ""


def resource_card_rows(ids):
    """物品号换成开卡用的 (物品号, 名字)，顺序跟传入的一致。"""
    names = {item: name for item, _wan, name in RESOURCE_CARDS}
    return [(item_id, names[item_id]) for item_id in ids]


def open_resource_cards(rec, sock, bag=None, on_progress=None, label="开卡",
                        cards=None):
    """按顺序开资源卡。一次一张。某种没有或没开成就停这种，换下一种。

    cards 不传时仍是拨款前的 1000 万金属卡和石油卡，每种最多 4 张。
    传了就按这个顺序，背包里有多少开多少。
    bag 不传就用最近的背包。返回 (背包, [(资源, 开了几张, 原因)])。
    没有背包时不发请求。
    """
    if bag is None:
        got_bag = rec.latest.get(RSE_BAG) if rec else None
        bag = got_bag[1] if got_bag and isinstance(got_bag[1], dict) else None
    default = cards is None
    names = ((ITEM_METAL, "金属"), (ITEM_OIL, "石油")) if default else cards
    if not isinstance(bag, dict):
        return None, [(name, 0, "没有背包") for _item, name in names]
    if default:
        log.info("[%s] 先各开 %d 张金属卡和石油卡", label, MAX_OPEN)
    else:
        log.info("[%s] 按顺序开资源卡", label)
    rows = []
    for item_id, name in names:
        raise_if_stopped()
        slot, count = _find_card(bag, item_id)
        if not count:
            if default:
                log.info("[%s] 背包没有 1000 万%s卡", label, name)
            else:
                log.info("[%s] 背包没有%s卡", label, name)
            rows.append((name, 0, "背包没有"))
            continue
        opened = 0
        why = ""
        cap = MAX_OPEN if default else count
        while opened < cap and slot is not None and count > 0:
            ok, reason = use_card(rec, sock, slot, item_id, 1)
            if not ok:
                tail = "，接着拨款" if "拨款" in label else ""
                log.info("[%s] %s卡第 %d 张没开成（%s）%s",
                         label, name, opened + 1, reason, tail)
                why = str(reason)
                break
            opened += 1
            metal, oil = _wallet(_pick_recent(rec, RSE_USER, 0))
            if default:
                log.info("[%s] 开了 1 张 1000 万%s卡（%d/%d），基地金属 %s 石油 %s",
                         label, name, opened, cap, _wan(metal), _wan(oil))
            else:
                log.info("[%s] 开了 1 张%s卡（%d/%d），基地金属 %s 石油 %s",
                         label, name, opened, cap, _wan(metal), _wan(oil))
            if on_progress:
                on_progress(f"{name} {opened}/{cap}")
            _nap(0.4)
            bag = _pick_recent(rec, RSE_BAG, 0) or bag
            slot, count = _find_card(bag, item_id)
        rows.append((name, opened, why))
    return bag, rows


def open_base_cards(rec, sock, on_progress=None, card_ids=None):
    """基地战开资源卡。先整叠打开勾选的资源箱，再一次一张按面额开卡。

    card_ids 空着就全开，含资源箱和 1000 万。
    """
    from . import bag_clean, citydb
    if "cards" in citydb.LOCKED_DAILY_KINDS:
        return False, citydb.RECOVERING_TEXT
    if card_ids is None:
        ids = default_resource_ids()
    else:
        ids, why = normalize_resource_ids(card_ids)
        if why:
            return False, why
    box_bits = []
    for item_id in ids:
        if item_id not in _BOX_ORDER:
            continue
        raise_if_stopped()
        name = _BOX_NAMES[item_id]
        found = latest_card(rec, item_id)
        have = 0 if not found else found[0]
        if have <= 0:
            if found is None:
                return False, "登录包里没有背包，没有开卡"
            log.info("[开卡] 背包没有%s", name)
            box_bits.append(f"没有{name}")
            continue
        ok, reason = bag_clean.use_item(rec, sock, item_id, have)
        if not ok:
            box_bits.append(f"{name}没开成（{reason}）")
            continue
        box_bits.append(f"{name}开了 {have} 张")
        if on_progress:
            on_progress(f"{name} {have}")
        _nap(0.35)
    cards = resource_card_rows([item for item in ids if item in _CARD_ORDER])
    if not cards:
        note_bag_cards(rec)
        return True, "成功：" + "，".join(box_bits)
    _bag, rows = open_resource_cards(
        rec, sock, on_progress=on_progress, label="开卡", cards=cards)
    if any(why == "没有背包" for _name, _opened, why in rows):
        return False, "登录包里没有背包，没有开卡"
    bits = []
    for name, opened, why in rows:
        if why == "背包没有":
            bits.append(f"没有{name}卡")
        elif why:
            bits.append(f"{name}开了 {opened} 张，下一张没开成（{why}）")
        else:
            bits.append(f"{name}开了 {opened} 张")
    note_bag_cards(rec)
    metal, oil = _wallet(_pick_recent(rec, RSE_USER, 0))
    text = "，".join(box_bits + bits)
    if isinstance(metal, int) or isinstance(oil, int):
        from . import citydb
        citydb.note_attack_wallet(
            metal if isinstance(metal, int) else None,
            oil if isinstance(oil, int) else None)
        text += f"。基地金属 {_wan(metal)} 石油 {_wan(oil)}"
    return True, text


def fund(rec, sock, building_id, times, on_progress=None):
    """每次全部拨款之前，各开 4 张 1000 万金属卡和石油卡。

    返回 (是否按次数做完, 说明)。拨款升级后建设值会重算，下降不算失败。
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
        raise_if_stopped()
        if isinstance(bag, dict):
            bag, _rows = open_resource_cards(
                rec, sock, bag, label=f"拨款 第 {i}/{times} 次")
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
        now = _fund_value(got)
        metal, oil = _wallet(_pick_recent(rec, RSE_USER, 0))
        log.info("[拨款] 第 %d/%d 次 建筑 %s error=0 拨款值 %s，"
                 "剩余金属 %s 石油 %s",
                 i, times, building_id, now, _wan(metal), _wan(oil))
        last_times = now
        done += 1
        if on_progress:
            text = f"第 {i}/{times} 次"
            if now is not None:
                text += f"，拨款值 {now}"
            on_progress(text)
        if short:
            return True, (f"建筑 {building_id} 拨款 {done} 次，拨款值={last_times}。"
                          f"第 {i} 次资源没补满，已拨款并停止")
        if i < times:
            _nap(0.4)
    return True, f"建筑 {building_id} 拨款 {done} 次，拨款值={last_times}"
