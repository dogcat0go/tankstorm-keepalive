# tankstorm-keepalive  Copyright (C) 2026 Dimlitter
# SPDX-License-Identifier: AGPL-3.0-or-later
"""军事演习战报。

RseWarGameOpt.fightReoprt 是 CountryFightData。第 3 号是 PVEFightRecord，
里面第 1 号最多 5 路（一次进攻会拆成多条回包，每条一路）。
每一路第 1 号是我方坦克，第 2 号是对方。
一辆坦克是 PVEArmyRecord：编号、型号、剩余血、满血，第 7 号字段是带时间的血量。

对外只有三件事：
  analyze(report)  从战报得到双方的阶段结果，读不出返回 None
  line(battle)     一行中文摘要
  clean(raw)       页面和库存只留数字，对不上返回 None
"""


def _int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _nodes(value):
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        return [value]
    return []


def _is_army(node):
    if not isinstance(node, dict):
        return False
    final = node.get("nFinalHp", node.get("field4"))
    mx = node.get("nMaxHp", node.get("field6"))
    typ = node.get("nType", node.get("field2"))
    ident = node.get("nID", node.get("field1"))
    if not all(_int(v) for v in (final, mx, typ, ident)):
        return False
    if ident <= 0 or not (0 <= typ <= 200) or mx < 100 or final < 0 or final > mx:
        return False
    return isinstance(node.get("field7"), (dict, list))


def _tank(node):
    samples = []
    for item in _nodes(node.get("field7")):
        if _is_army(item):
            continue
        tick = item.get("nTick", item.get("field1"))
        hp = item.get("nHp", item.get("field2"))
        if _int(tick) and _int(hp) and tick >= 0 and hp >= 0:
            samples.append((tick, hp))
    samples.sort()
    return {
        "final": node.get("nFinalHp", node.get("field4")),
        "max": node.get("nMaxHp", node.get("field6")),
        "samples": samples,
    }


def _tanks(value):
    return [_tank(item) for item in _nodes(value) if _is_army(item)]


def _ways(report):
    found = []

    def walk(node, depth):
        if depth > 6 or len(found) >= 5:
            return
        if isinstance(node, dict):
            ours = _tanks(node.get("fightRecordSelf", node.get("field1")))
            theirs = _tanks(node.get("fightRecordEnemy", node.get("field2")))
            if ours and theirs:
                found.append((ours, theirs))
                return
            for value in node.values():
                walk(value, depth + 1)
        elif isinstance(node, list):
            for value in node:
                walk(value, depth + 1)

    for root in (report if isinstance(report, list) else [report]):
        walk(root, 0)
    return found


def _hp_at(tank, tick):
    hp = tank["max"]
    seen = False
    for at, value in tank["samples"]:
        if at <= tick:
            hp = value
            seen = True
        else:
            break
    if not seen and not tank["samples"]:
        hp = tank["final"]
    return hp if hp > 0 else 0


def _side_at(tanks, tick, use_final=False):
    left_n = left_hp = max_hp = 0
    for tank in tanks:
        max_hp += tank["max"]
        hp = tank["final"] if use_final else _hp_at(tank, tick)
        if hp > tank["max"]:
            hp = tank["max"]
        if hp > 0:
            left_n += 1
            left_hp += hp
    return {"tanks": left_n, "total": len(tanks), "hp": left_hp, "maxHp": max_hp}


def _seconds(span):
    """时间戳跨度。跨度大时按毫秒，短时按逻辑 30 帧/秒。"""
    span = max(0, int(span))
    if span >= 8000:
        return int(round(span / 1000.0))
    return int(round(span / 30.0))


def _sec_text(seconds):
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}秒"
    minute, second = divmod(seconds, 60)
    return f"{minute}分{second}秒" if second else f"{minute}分"


def _wan(n):
    n = int(n)
    if n < 10000:
        return str(n)
    text = f"{n / 10000:.1f}"
    if text.endswith(".0"):
        text = text[:-2]
    return text + "万"


def _pct(hp, max_hp):
    if max_hp <= 0:
        return "0%"
    return f"{int(round(hp * 100 / max_hp))}%"


def _side_text(side):
    return (f"剩 {side['tanks']}/{side['total']} 辆、"
            f"血 {_wan(side['hp'])}/{_wan(side['maxHp'])}"
            f"（{_pct(side['hp'], side['maxHp'])}）")


def analyze(report):
    """第一阶段的剩余坦克、总血和耗时。时间轴上出现明显间断且当时两边都还活着，就算进入决战。"""
    ours, theirs = [], []
    for left, right in _ways(report):
        ours.extend(left)
        theirs.extend(right)
    if not ours or not theirs:
        return None
    ticks = sorted({tick for tank in ours + theirs for tick, _hp in tank["samples"]})
    if not ticks:
        end = 0
        return {
            "climax": None,
            "phase1": {
                "seconds": 0,
                "self": _side_at(ours, end, True),
                "enemy": _side_at(theirs, end, True),
            },
        }
    start, end = ticks[0], ticks[-1]
    split = None
    if len(ticks) >= 4:
        gaps = [(ticks[i] - ticks[i - 1], i) for i in range(1, len(ticks))]
        widths = sorted(width for width, _index in gaps)
        median = widths[len(widths) // 2]
        # 均匀采样的间隔不会被当成决战。间断要明显长于平常间隔，并且至少 8 秒。
        limit = max(8000 if (end - start) >= 8000 else 240, median * 4)
        width, index = max(gaps)
        if width > limit:
            before = ticks[index - 1]
            alive = (_side_at(ours, before)["tanks"] and _side_at(theirs, before)["tanks"])
            if alive and index > 1 and ticks[index] < end:
                split = (before, ticks[index])
    if split is None:
        return {
            "climax": None,
            "phase1": {
                "seconds": _seconds(end - start),
                "self": _side_at(ours, end, True),
                "enemy": _side_at(theirs, end, True),
            },
        }
    phase_end, climax_at = split
    return {
        "phase1": {
            "seconds": _seconds(phase_end - start),
            "self": _side_at(ours, phase_end),
            "enemy": _side_at(theirs, phase_end),
        },
        "climax": {
            "seconds": _seconds(end - climax_at),
            "self": _side_at(ours, end, True),
            "enemy": _side_at(theirs, end, True),
        },
    }


def line(battle):
    """一行中文摘要。"""
    phase = battle["phase1"]
    text = f"第一阶段 {_sec_text(phase['seconds'])}，我方{_side_text(phase['self'])}，对方{_side_text(phase['enemy'])}"
    climax = battle.get("climax")
    if not climax:
        return text + "。未进入决战"
    text += (f"。决战 {_sec_text(climax['seconds'])}，"
             f"我方最终血 {_wan(climax['self']['hp'])}"
             f"（{_pct(climax['self']['hp'], climax['self']['maxHp'])}，"
             f"剩 {climax['self']['tanks']} 辆），"
             f"对方最终血 {_wan(climax['enemy']['hp'])}"
             f"（{_pct(climax['enemy']['hp'], climax['enemy']['maxHp'])}，"
             f"剩 {climax['enemy']['tanks']} 辆）")
    return text


def _clean_side(raw):
    if not isinstance(raw, dict):
        return None
    try:
        side = {
            "tanks": int(raw["tanks"]),
            "total": int(raw["total"]),
            "hp": int(raw["hp"]),
            "maxHp": int(raw["maxHp"]),
        }
    except (KeyError, TypeError, ValueError):
        return None
    if min(side.values()) < 0 or side["tanks"] > side["total"] or side["hp"] > side["maxHp"]:
        return None
    return side


def _clean_phase(raw):
    if not isinstance(raw, dict):
        return None
    try:
        seconds = int(raw["seconds"])
    except (KeyError, TypeError, ValueError):
        return None
    left, right = _clean_side(raw.get("self")), _clean_side(raw.get("enemy"))
    if seconds < 0 or left is None or right is None:
        return None
    return {"seconds": seconds, "self": left, "enemy": right}


def clean(raw):
    """页面和库存只留数字。对不上返回 None。"""
    if not isinstance(raw, dict):
        return None
    phase = _clean_phase(raw.get("phase1"))
    if phase is None:
        return None
    climax = raw.get("climax")
    if climax is not None:
        climax = _clean_phase(climax)
        if climax is None:
            return None
    return {"phase1": phase, "climax": climax}
