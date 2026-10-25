# tankstorm-keepalive  Copyright (C) 2026 Dimlitter
# SPDX-License-Identifier: AGPL-3.0-or-later
"""清理背包。2026-10-25 15:43–15:50 抓包。

RceBagItemUse / 0440，optType=0。整叠一次发完，count 是背包里的数量。
不选的物品不要带 selectID。自选才带 selectID，选项按数量平均分，
余数给前面的选项。战略自选宝箱 110018 分成 10064、10065、10066、10067。
坦克研究宝箱 110071 分成 10087、10088。
"""

import time

from . import sender
from .daily import (
    _await_response, _beat, _nap, latest_card, note_bag_cards, raise_if_stopped,
)
from .log import get_logger
from .proto_encode import encode_message

log = get_logger()

# (物品号, 名称, 说明, 自选物品号)。自选为空就是整叠直接用。
CLEAN_ITEMS = (
    (30051, "战略宝箱", "整叠打开，随机得到 10064、10065、10066、10067", ()),
    (110018, "战略自选宝箱", "平均分成 10064、10065、10066、10067",
     (10064, 10065, 10066, 10067)),
    (30107, "研究宝箱", "整叠打开，随机得到 10087、10088", ()),
    (110071, "坦克研究宝箱", "平均分成 10088、10087", (10088, 10087)),
    (30130, "幸运军魂宝箱", "整叠打开，开出 50点军魂、100点军魂、200点军魂卡", ()),
    (20422, "25点军魂", "整叠使用，每张 25 点军魂", ()),
    (20423, "50点军魂", "整叠使用，每张 50 点军魂", ()),
    (20424, "100点军魂", "整叠使用，每张 100 点军魂", ()),
    (20582, "200点军魂卡", "整叠使用，每张 200 点军魂", ()),
    (30109, "初级炮手宝箱", "整叠打开", ()),
    (30117, "高级参谋配件宝箱", "整叠打开", ()),
    (20052, "荣誉卡", "整叠使用，每张荣誉 +1000", ()),
)
_ORDER = {item: i for i, (item, _name, _note, _opts) in enumerate(CLEAN_ITEMS)}
_BY_ID = {item: (name, note, opts) for item, name, note, opts in CLEAN_ITEMS}


def catalog():
    """给页面勾选。顺序就是使用顺序。"""
    return [
        {"id": item, "name": name, "note": note, "choice": bool(opts)}
        for item, name, note, opts in CLEAN_ITEMS
    ]


def default_ids():
    return [item for item, _name, _note, _opts in CLEAN_ITEMS]


def normalize_ids(raw):
    """收成目录顺序的物品号。不合法时返回 (None, 原因)。"""
    if isinstance(raw, str):
        raw = [part for part in raw.replace("，", ",").split(",") if part.strip()]
    if not isinstance(raw, (list, tuple)):
        return None, "要选择物品"
    chosen = []
    seen = set()
    for item in raw:
        try:
            item_id = int(str(item).strip())
        except (TypeError, ValueError):
            return None, "物品编号不对"
        if item_id not in _ORDER:
            return None, "没有这个物品"
        if item_id in seen:
            continue
        seen.add(item_id)
        chosen.append(item_id)
    if not chosen:
        return None, "至少选一种物品"
    chosen.sort(key=lambda item_id: _ORDER[item_id])
    return chosen, ""


def _even(count, options):
    base, extra = divmod(count, len(options))
    return [
        (opt, base + (1 if i < extra else 0))
        for i, opt in enumerate(options)
    ]


def use_item(rec, sock, item_id, count, select_id=0):
    """用掉指定张数。select_id 为 0 时不发 selectID。"""
    found = latest_card(rec, item_id)
    have = 0 if not found else found[0]
    slot = None if not found else found[1]
    if have < count or not slot:
        return False, f"只有 {have} 张"
    fields = {
        3: ("int32", count), 4: ("int32", slot),
        5: ("int32", item_id), 7: ("int32", 0),
    }
    if select_id:
        fields[1] = ("int32", select_id)
    mark = rec.seq_mark() if rec else 0
    sender.send_frame(
        sock, "0440", encode_message(fields, omit_zero=False), rec.rc4_c2s)
    got = _await_response(
        sock, rec, "RseBagItemUse", mark, 6.0,
        want=lambda d, item_id=item_id: (
            isinstance(d, dict) and d.get("itemID") == item_id))
    if not isinstance(got, dict):
        return False, "没有回包"
    if got.get("ret") not in (0, None):
        return False, f"被拒 ret={got.get('ret')}"
    deadline = time.time() + 0.8
    while time.time() < deadline:
        _beat()
        try:
            sock.settimeout(0.3)
            if not sock.recv(8192):
                break
        except TimeoutError:
            continue
        except OSError:
            break
    return True, ""


def clean_bag(rec, sock, item_ids, on_progress=None):
    """按勾选使用。背包里没有的跳过。自选按选项平均分。"""
    ids, why = normalize_ids(item_ids)
    if why:
        return False, why
    if latest_card(rec, ids[0]) is None:
        return False, "登录包里没有背包，没有使用"
    bits = []
    failed = False
    for item_id in ids:
        raise_if_stopped()
        name, _note, options = _BY_ID[item_id]
        found = latest_card(rec, item_id)
        have = 0 if not found else found[0]
        if have <= 0:
            log.info("[清理] 背包没有%s", name)
            bits.append(f"没有{name}")
            continue
        if options:
            parts = []
            stop = ""
            for opt, share in _even(have, options):
                if share <= 0:
                    continue
                raise_if_stopped()
                ok, reason = use_item(rec, sock, item_id, share, opt)
                if not ok:
                    stop = reason
                    break
                parts.append(f"{opt}×{share}")
                if on_progress:
                    on_progress(f"{name} {'、'.join(parts)}")
                _nap(0.35)
            if stop and not parts:
                failed = True
                bits.append(f"{name}没开成（{stop}）")
            elif stop:
                failed = True
                bits.append(f"{name}已分 {'、'.join(parts)}，下一步没开成（{stop}）")
            else:
                bits.append(f"{name}平均分了 {'、'.join(parts)}")
            continue
        ok, reason = use_item(rec, sock, item_id, have)
        if not ok:
            failed = True
            bits.append(f"{name}没开成（{reason}）")
            continue
        bits.append(f"{name}用了 {have} 张")
        if on_progress:
            on_progress(f"{name} {have}")
        _nap(0.35)
    note_bag_cards(rec)
    text = "，".join(bits)
    if failed:
        return False, text
    return True, "成功：" + text
