# tankstorm-keepalive  Copyright (C) 2026 Dimlitter
# SPDX-License-Identifier: AGPL-3.0-or-later
"""军事演习战报。

RseWarGameOpt.fightReoprt 是 CountryFightData。第 3 号是 PVEFightRecord，
里面第 1 号最多 6 路（一次进攻会拆成多条回包，每条一路）。
2026-11-20 21:20:51 有决战的那次是 wayIndex 0 到 5。没有决战的那次只到 4。
每一路第 1 号是我方坦克，第 2 号是对方。
一辆坦克是 PVEArmyRecord：编号、型号、剩余血、满血，第 7 号字段是带时间的血量。
同一路第 8 号是我方火炮，第 4 项是进阶品阶，第 7 项是固定发射间隔。
第 9 号是对方火炮，第 4 项是进阶品阶，第 5 项是开火时刻，相邻差恒定才算这一路的间隔。
第 4 号是我方将领，第 5 号是对方将领。第 11 号是我方参谋，第 12 号是对方参谋。
将领和参谋只放在深度解析里，不写进一行摘要。

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


def _gap(value, timed):
    """我方读固定间隔。对方用开火时刻的相邻差，差不恒定就不猜一个数。"""
    node = next(iter(_nodes(value)), None)
    if node is None:
        return None
    if not timed:
        tick = node.get("nAtackTick", node.get("field7"))
        return tick if _int(tick) and tick >= 4 else None
    times = node.get("field5")
    if not isinstance(times, list) or len(times) < 2:
        return None
    diffs = []
    prev = None
    for tick in times:
        if not _int(tick):
            return None
        if prev is not None:
            gap = tick - prev
            if gap < 4:
                return None
            diffs.append(gap)
        prev = tick
    if len(set(diffs)) != 1:
        return None
    return diffs[0]


_GENERAL = (
    ("id", "nGeneralID", "field1"),
    ("lv", "nGeneralLvl", "field2"),
    ("train", "nGeneralTrainLvl", "field3"),
    ("atk", "nGeneralAtk", "field4"),
    ("def", "nGeneralDef", "field5"),
    ("hp", "nGeneralHP", "field6"),
    ("crt", "nGeneralCrt", "field7"),
    ("score", "nGeneralScore", "field8"),
    ("eqpAtk", "nGeneralEqpAtk", "field9"),
    ("eqpHp", "nGeneralEqpHp", "field10"),
    ("buffAtk", "nGeneralBuffAtk", "field11"),
    ("buffHp", "nGeneralBuffHp", "field12"),
    ("cut", "nRducAdmXiangxin", "field14"),
    ("add", "nAddAdmXiangxin", "field15"),
)

_ADVISER = (
    ("id", "nAdviserID", "field1"),
    ("hit", "nChanceHitNum", "field2"),
    ("avoid", "nAvoidNum", "field3"),
    ("crt", "nCritcal", "field4"),
    ("star", "nAdviserStar", "field5"),
)


def _person(value, spec):
    """将领或参谋。只留非负整数，没有就返回 None。"""
    node = next(iter(_nodes(value)), None)
    if not isinstance(node, dict):
        return None
    out = {}
    for key, named, numbered in spec:
        raw = node[named] if named in node else node.get(numbered)
        if _int(raw) and raw >= 0:
            out[key] = raw
    return out or None


def _pair(node, self_name, self_field, enemy_name, enemy_field, spec):
    mine = _person(node[self_name] if self_name in node else node.get(self_field), spec)
    theirs = _person(node[enemy_name] if enemy_name in node else node.get(enemy_field), spec)
    out = {}
    if mine:
        out["self"] = mine
    if theirs:
        out["enemy"] = theirs
    return out or None


def _grade(value):
    """火炮进阶品阶，读这一门火炮的第 4 项。只留 0 到 30 的整数。"""
    node = next(iter(_nodes(value)), None)
    if node is None:
        return None
    grade = node.get("nGrade")
    if grade is None:
        grade = node.get("field4")
    if _int(grade) and 0 <= grade <= 30:
        return grade
    return None


def _ways(report):
    found = []

    def walk(node, depth):
        if depth > 6 or len(found) >= 6:
            return
        if isinstance(node, dict):
            ours = _tanks(node.get("fightRecordSelf", node.get("field1")))
            theirs = _tanks(node.get("fightRecordEnemy", node.get("field2")))
            if ours and theirs:
                found.append((
                    ours, theirs,
                    _gap(node.get("field8"), False),
                    _gap(node.get("field9"), True),
                    _grade(node.get("field8")),
                    _grade(node.get("field9")),
                    _pair(node, "fightGeneralSelf", "field4",
                          "fightGeneralEnemy", "field5", _GENERAL),
                    _pair(node, "rAdviserSelf", "field11",
                          "rAdviserEnemy", "field12", _ADVISER),
                ))
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


def _span(tanks):
    ticks = [tick for tank in tanks for tick, _hp in tank["samples"]]
    if not ticks:
        return 0
    return max(ticks) - min(ticks)


def _seconds(span, millis=None):
    """时间戳跨度。跨度大时按毫秒，短时按逻辑 30 帧/秒。

    millis 沿用这一阶段的总跨度，短的一路不再单独换算。
    """
    span = max(0, int(span))
    if millis is None:
        millis = span >= 8000
    if millis:
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


def _collect(ways):
    ours, theirs = [], []
    for left, right, *_rest in ways:
        ours.extend(left)
        theirs.extend(right)
    return ours, theirs


def _mark_phase_lanes(ways, lanes, count, clock, end=None):
    """第一阶段每一路的战斗时间，用这一路自己的血量时间跨度。"""
    millis = max(0, int(clock)) >= 8000
    for index in range(min(count, len(ways), len(lanes))):
        ticks = [tick for tank in ways[index][0] + ways[index][1]
                 for tick, _hp in tank["samples"]
                 if end is None or tick <= end]
        if ticks:
            lanes[index]["seconds"] = _seconds(max(ticks) - min(ticks), millis)


def analyze(report):
    """第一阶段的剩余坦克、总血和耗时。有第 6 路就是决战，决战时间用这一路自己的跨度。

    不到 6 路时，仍看时间轴上有没有明显间断。
    lanes 跟路的顺序一致。第一阶段每一路另有自己的战斗时间。
    我方火炮是固定间隔，对方是开火时刻的恒定相邻差。双方火炮进阶品阶读第 4 项。
    每一路的将领和参谋放在 general、adviser 里，给深度解析用。
    """
    ways = _ways(report)
    lanes = []
    for _left, _right, me, them, me_grade, them_grade, general, adviser in ways:
        lane = {}
        if me is not None:
            lane["self"] = me
        if them is not None:
            lane["enemy"] = them
        if me_grade is not None:
            lane["selfGrade"] = me_grade
        if them_grade is not None:
            lane["enemyGrade"] = them_grade
        if general:
            lane["general"] = general
        if adviser:
            lane["adviser"] = adviser
        lanes.append(lane)
    if len(ways) >= 6:
        ours, theirs = _collect(ways[:5])
        climax_ours, climax_theirs = ways[5][0], ways[5][1]
        if not ours or not theirs or not climax_ours or not climax_theirs:
            return None
        span = _span(ours + theirs)
        _mark_phase_lanes(ways, lanes, 5, span)
        return {
            "phase1": {
                "seconds": _seconds(span),
                "self": _side_at(ours, 0, True),
                "enemy": _side_at(theirs, 0, True),
            },
            "climax": {
                "seconds": _seconds(_span(climax_ours + climax_theirs)),
                "self": _side_at(climax_ours, 0, True),
                "enemy": _side_at(climax_theirs, 0, True),
            },
            "lanes": lanes,
        }
    ours, theirs = _collect(ways)
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
            "lanes": lanes,
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
        _mark_phase_lanes(ways, lanes, len(ways), end - start)
        return {
            "climax": None,
            "phase1": {
                "seconds": _seconds(end - start),
                "self": _side_at(ours, end, True),
                "enemy": _side_at(theirs, end, True),
            },
            "lanes": lanes,
        }
    phase_end, climax_at = split
    _mark_phase_lanes(ways, lanes, len(ways), phase_end - start, phase_end)
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
        "lanes": lanes,
    }


def _lane_line(lanes):
    bits = []
    for i, lane in enumerate(lanes or (), 1):
        if not isinstance(lane, dict):
            continue
        parts = []
        seconds = lane.get("seconds")
        if _int(seconds) and seconds >= 0:
            parts.append(f"战斗 {_sec_text(seconds)}")
        me, them = lane.get("self"), lane.get("enemy")
        me_grade, them_grade = lane.get("selfGrade"), lane.get("enemyGrade")
        if _int(me_grade) and 0 <= me_grade <= 30:
            parts.append(f"我方火炮 {me_grade} 阶")
        if _int(them_grade) and 0 <= them_grade <= 30:
            parts.append(f"敌方火炮 {them_grade} 阶")
        if _int(me) and me >= 4:
            parts.append(f"我方火炮发射间隔 {me}")
        if _int(them) and them >= 4:
            parts.append(f"敌方火炮发射间隔 {them}")
        if parts:
            bits.append(f"第 {i} 路 " + "，".join(parts))
    return "。".join(bits)


def line(battle):
    """一行中文摘要。"""
    phase = battle["phase1"]
    text = f"第一阶段 {_sec_text(phase['seconds'])}，我方{_side_text(phase['self'])}，对方{_side_text(phase['enemy'])}"
    climax = battle.get("climax")
    if not climax:
        text += "。未进入决战"
    else:
        text += (f"。决战 {_sec_text(climax['seconds'])}，"
                 f"我方最终血 {_wan(climax['self']['hp'])}"
                 f"（{_pct(climax['self']['hp'], climax['self']['maxHp'])}，"
                 f"剩 {climax['self']['tanks']} 辆），"
                 f"对方最终血 {_wan(climax['enemy']['hp'])}"
                 f"（{_pct(climax['enemy']['hp'], climax['enemy']['maxHp'])}，"
                 f"剩 {climax['enemy']['tanks']} 辆）")
    extra = _lane_line(battle.get("lanes"))
    if extra:
        text += "。" + extra
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


def _clean_gap(value):
    if not _int(value) or value < 4:
        return None
    return value


def _clean_grade(value):
    if not _int(value) or value < 0 or value > 30:
        return None
    return value


def _clean_person(raw, keys):
    if not isinstance(raw, dict):
        return None
    out = {}
    for key in keys:
        value = raw.get(key)
        if _int(value) and value >= 0:
            out[key] = value
    return out or None


def _clean_pair(raw, keys):
    if not isinstance(raw, dict):
        return None
    out = {}
    for side in ("self", "enemy"):
        person = _clean_person(raw.get(side), keys)
        if person:
            out[side] = person
    return out or None


def _clean_lanes(raw):
    if not isinstance(raw, list):
        return None
    lanes = []
    kept = False
    for item in raw:
        lane = {}
        if isinstance(item, dict):
            for key in ("self", "enemy"):
                gap = _clean_gap(item.get(key))
                if gap is not None:
                    lane[key] = gap
                    kept = True
            for key in ("selfGrade", "enemyGrade"):
                grade = _clean_grade(item.get(key))
                if grade is not None:
                    lane[key] = grade
                    kept = True
            seconds = item.get("seconds")
            if _int(seconds) and seconds >= 0:
                lane["seconds"] = seconds
                kept = True
            general = _clean_pair(item.get("general"), tuple(item[0] for item in _GENERAL))
            adviser = _clean_pair(item.get("adviser"), tuple(item[0] for item in _ADVISER))
            if general:
                lane["general"] = general
                kept = True
            if adviser:
                lane["adviser"] = adviser
                kept = True
        lanes.append(lane)
    return lanes if kept else None


def clean(raw):
    """页面和库存只留数字。阶段对不上返回 None，火炮、将领、参谋对不上只丢掉这一项。"""
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
    out = {"phase1": phase, "climax": climax}
    lanes = _clean_lanes(raw.get("lanes"))
    if lanes:
        out["lanes"] = lanes
    return out
