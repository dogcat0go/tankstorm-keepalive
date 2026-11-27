# tankstorm-keepalive  Copyright (C) 2026 Dimlitter
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# 本程序是自由软件：你可以依据自由软件基金会发布的 GNU Affero 通用公共许可证
# （第 3 版，或你选择的任何更新版本）之条款，再分发和/或修改它。
# 本程序希望能有用，但不提供任何担保；甚至不含适销性或特定用途适用性的默示担保。
# 详见随附的 LICENSE 文件，或 <https://www.gnu.org/licenses/>。
"""管理页的指挥测试：同区同国组队，在地图上点城。

城市表没有坐标。位置按已经存下的 CityData 连接来排：
马奇诺在中心，六座内圈城围着它，再往外是一圈中立城，六个国家贴在外圈。
整张图左右对调，巴黎在左边。
编队名称默认是国战编队。填写对方账号发出邀请，对方接受后才入队。
"""

import math

from . import citydb

_OPS = {
    "move": "移动",
    "morale": "补士气",
    "open": "打开城市",
    "attack": "攻打",
}
_ATTACK_STEPS = 8
_TEAM_NAME = "国战编队"


def layout_cities(graph: dict):
    """把城市图画成坐标。返回 (城市列表, 宽, 高)。

    目录里有马奇诺和 9301–9318 这一圈时，按这条已经存下的连接来排。
    缺了这一圈就按原属国分成几块。
    """
    if not graph:
        return [], 320, 180
    if _war_cycle(graph):
        return _layout_war_map(graph)
    return _layout_blocks(graph)


def _war_cycle(graph) -> list:
    """沿连接把 9301–9318 走成一圈。走不成返回空。"""
    ring = {i for i in range(9301, 9319) if i in graph}
    if len(ring) < 18 or 9101 not in graph:
        return []
    mates = {}
    for cid in ring:
        near = [int(n) for n in (graph[cid].get("near") or ()) if int(n) in ring]
        if len(near) != 2:
            return []
        mates[cid] = near
    start = 9301 if 9301 in mates else min(mates)
    forward = start + 1 if start + 1 in mates[start] else min(mates[start])
    order = [start]
    prev, cur = start, forward
    while cur != start and len(order) <= 18:
        order.append(cur)
        a, b = mates[cur]
        prev, cur = cur, a if b == prev else b
    if len(order) != 18 or cur != start:
        return []
    return order


# 内圈六座城各对着外圈连续的三座。对应关系是 CityData 的连接，不是另外画的。
_FORT_OUTERS = {
    9201: (9302, 9303, 9304),
    9202: (9317, 9318, 9301),
    9203: (9314, 9315, 9316),
    9204: (9311, 9312, 9313),
    9205: (9308, 9309, 9310),
    9206: (9305, 9306, 9307),
}
_CX = _CY = 700.0
_MAP = 1400


def _at(deg, radius):
    """角度从正上方起、顺时针增大，再左右对调，巴黎落在左边。"""
    rad = math.radians(deg)
    return _CX - radius * math.sin(rad), _CY - radius * math.cos(rad)


def _layout_deg(x, y):
    """屏幕坐标还原成排版角度。_at 做过左右对调，这里把对调算回去。"""
    return math.degrees(math.atan2(_CX - x, _CY - y)) % 360


def _mean_deg(degs):
    if not degs:
        return 0.0
    s = sum(math.sin(math.radians(d)) for d in degs)
    c = sum(math.cos(math.radians(d)) for d in degs)
    if s == 0 and c == 0:
        return float(degs[0])
    return math.degrees(math.atan2(s, c)) % 360


def _layout_war_map(graph):
    order = _war_cycle(graph)
    ang = {cid: i * (360 / len(order)) for i, cid in enumerate(order)}
    pos = {9101: (_CX, _CY)}
    for cid, deg in ang.items():
        pos[cid] = _at(deg, 310)
    fort_ang = {}
    for fort, outers in _FORT_OUTERS.items():
        degs = [ang[c] for c in outers if c in ang]
        if fort in graph and degs:
            fort_ang[fort] = _mean_deg(degs)
            pos[fort] = _at(fort_ang[fort], 118)
    for cid in range(9319, 9331):
        if cid not in graph:
            continue
        degs = [ang[int(n)] for n in (graph[cid].get("near") or ()) if int(n) in ang]
        if not degs:
            degs = [fort_ang[int(n)] for n in (graph[cid].get("near") or ()) if int(n) in fort_ang]
        if degs:
            pos[cid] = _at(_mean_deg(degs), 210)
    for cid in range(7301, 7305):
        if cid not in graph:
            continue
        degs = []
        for n in graph[cid].get("near") or ():
            n = int(n)
            if n in ang:
                degs.append(ang[n])
            elif n in fort_ang:
                degs.append(fort_ang[n])
            elif n in pos and n != cid:
                degs.append(_layout_deg(*pos[n]))
        if degs:
            pos[cid] = _at(_mean_deg(degs), 168)
    for country in range(1, 7):
        smalls = []
        for seq in (301, 302, 303):
            cid = country * 1000 + seq
            if cid not in graph:
                continue
            outers = [int(n) for n in (graph[cid].get("near") or ()) if int(n) in ang]
            if not outers:
                continue
            deg = ang[outers[0]]
            pos[cid] = _at(deg, 400)
            big = cid - 100
            if big in graph:
                pos[big] = _at(deg, 485)
            smalls.append(deg)
        cap = country * 1000 + 101
        if cap in graph and smalls:
            pos[cap] = _at(_mean_deg(smalls), 575)
        for seq, side in ((201, 12), (203, -12)):
            big = country * 1000 + seq
            modo = country * 10000 + seq * 10
            if modo in graph and big in pos:
                pos[modo] = _at(_layout_deg(*pos[big]) + side, 530)
    pending = [cid for cid in graph if cid not in pos]
    for _ in range(4):
        still = []
        for cid in pending:
            nbrs = [pos[int(n)] for n in (graph[cid].get("near") or ()) if int(n) in pos]
            if not nbrs:
                still.append(cid)
                continue
            pos[cid] = (sum(p[0] for p in nbrs) / len(nbrs), sum(p[1] for p in nbrs) / len(nbrs))
        pending = still
        if not pending:
            break
    for i, cid in enumerate(sorted(pending)):
        pos[cid] = (40 + (i % 8) * 28, 24 + (i // 8) * 22)
    placed = [_city_point(graph, cid, xy[0], xy[1]) for cid, xy in sorted(pos.items()) if cid in graph]
    return placed, _MAP, _MAP


def _city_point(graph, cid, x, y):
    info = graph[cid]
    home = int(info.get("home") or 0)
    dx, dy = x - _CX, y - _CY
    dist = math.hypot(dx, dy) or 1
    lx, ly = (x, y + 16) if dist < 2 else (x + dx / dist * 14, y + dy / dist * 14)
    return {
        "id": int(cid),
        "name": info.get("name") or "",
        "x": round(x, 1),
        "y": round(y, 1),
        "lx": round(lx, 1),
        "ly": round(ly, 1),
        "home": home,
        "owner": int(info.get("owner") or 0),
        "users": int(info.get("users") or 0),
        "capital": int(cid) == home * 1000 + 101 and home in range(1, 7),
        "near": sorted(int(n) for n in (info.get("near") or ())),
    }


def _layout_blocks(graph):
    homes = {}
    for cid, info in graph.items():
        homes.setdefault(int(info.get("home") or 0), []).append(int(cid))
    blocks = []
    for home, ids in sorted(homes.items()):
        depth = _home_depth(graph, home, ids)
        groups = {}
        for cid in ids:
            groups.setdefault(depth[cid], []).append(cid)
        span = 20 + max(groups) * 26
        blocks.append((home, groups, span))
    x, y = 28, 28
    row_h = 0
    max_x, max_y = 320, 180
    placed = []
    for home, groups, span in blocks:
        if x > 28 and x + span * 2 > 1040:
            x = 28
            y += row_h + 32
            row_h = 0
        cx, cy = x + span, y + span
        for ring, members in groups.items():
            members.sort()
            radius = 0 if ring == 0 else 18 + ring * 24
            for i, cid in enumerate(members):
                if radius == 0:
                    px, py = cx, cy
                else:
                    ang = (i / len(members)) * math.tau + ring * 0.35
                    px = cx + radius * math.cos(ang)
                    py = cy + radius * math.sin(ang)
                info = graph[cid]
                near = info.get("near") or ()
                placed.append({
                    "id": cid,
                    "name": info.get("name") or "",
                    "x": round(px, 1),
                    "y": round(py, 1),
                    "home": int(info.get("home") or 0),
                    "owner": int(info.get("owner") or 0),
                    "users": int(info.get("users") or 0),
                    "capital": cid == home * 1000 + 101,
                    "near": sorted(int(n) for n in near),
                })
        x += span * 2 + 40
        row_h = max(row_h, span * 2)
        max_x = max(max_x, x)
        max_y = max(max_y, y + span * 2 + 20)
    return placed, int(max_x + 8), int(max_y + 8)


def _home_depth(graph, home, ids) -> dict:
    own = set(ids)
    cap = home * 1000 + 101
    root = cap if cap in own else min(own)
    depth = {root: 0}
    queue = [root]
    while queue:
        cur = queue.pop(0)
        for nxt in graph[cur].get("near") or ():
            if nxt in own and nxt not in depth:
                depth[nxt] = depth[cur] + 1
                queue.append(nxt)
    extra = (max(depth.values()) if depth else 0) + 1
    for cid in ids:
        depth.setdefault(cid, extra)
    return depth


def snapshot() -> dict:
    graph = citydb.city_map()
    cities, width, height = layout_cities(graph)
    accounts = _accounts()
    return {
        "cities": cities,
        "width": width,
        "height": height,
        "accounts": accounts,
        "teams": _teams(),
        "countries": _countries(),
    }


def _clean_name(raw) -> tuple:
    text = " ".join(str(raw or "").split())
    if not text:
        return _TEAM_NAME, ""
    if len(text) > 16:
        return "", "编队名称最多 16 个字"
    return text, ""


def create_team(organizer_id, country_id, name=_TEAM_NAME) -> str:
    """编一支队伍。名称空着就是国战编队。组织者自己也在队里。成功返回空字符串。"""
    try:
        organizer_id = int(organizer_id)
        country_id = int(country_id)
    except (TypeError, ValueError):
        return "账号或国家不对"
    title, why = _clean_name(name)
    if why:
        return why
    who = _account_row(organizer_id)
    if not who:
        return "没有这个账号"
    if not who["qq"]:
        return "这个号还没绑定攻打 QQ"
    if who["region"] <= 0:
        return "这个号还没有区号，进过游戏才会记上"
    if country_id <= 0 or not citydb.country_name(country_id):
        return "要选一个国家"
    if who["country"] and who["country"] != country_id:
        return f"指挥者的国家是{citydb.country_name(who['country']) or who['country']}"
    conn = citydb.connect()
    try:
        other = conn.execute(
            "SELECT team_id FROM war_member WHERE user_id=? AND IFNULL(status,'wait')!='out'",
            (organizer_id,)).fetchone()
        if other:
            return "这个号已经在一支编队里"
        cur = conn.execute(
            "INSERT INTO war_team(region, country_id, organizer_id, name, created_at) "
            "VALUES (?,?,?,?,?)",
            (who["region"], country_id, organizer_id, title, citydb.now_ts()))
        team_id = int(cur.lastrowid)
        conn.execute(
            "INSERT INTO war_member(team_id, user_id, accept, status) VALUES (?,?,1,'in')",
            (team_id, organizer_id))
        conn.commit()
    finally:
        conn.close()
    return ""


def rename_team(team_id, name) -> str:
    """改编队名称。空的改回国战编队。成功返回空字符串。"""
    title, why = _clean_name(name)
    if why:
        return why
    try:
        team_id = int(team_id)
    except (TypeError, ValueError):
        return "没有这支编队"
    conn = citydb.connect()
    try:
        row = conn.execute(
            "SELECT id FROM war_team WHERE id=?", (team_id,)).fetchone()
        if not row:
            return "没有这支编队"
        conn.execute("UPDATE war_team SET name=? WHERE id=?", (title, team_id))
        conn.commit()
    finally:
        conn.close()
    return ""


def drop_team(team_id) -> str:
    try:
        team_id = int(team_id)
    except (TypeError, ValueError):
        return "没有这支编队"
    conn = citydb.connect()
    try:
        row = conn.execute(
            "SELECT id FROM war_team WHERE id=?", (team_id,)).fetchone()
        if not row:
            return "没有这支编队"
        conn.execute("DELETE FROM war_member WHERE team_id=?", (team_id,))
        conn.execute("DELETE FROM war_team WHERE id=?", (team_id,))
        conn.commit()
    finally:
        conn.close()
    return ""


def invite(team_id, username) -> str:
    """按登录账号邀请。成功返回空字符串。"""
    username = str(username or "").strip()
    if not username:
        return "要填对方的账号"
    try:
        team_id = int(team_id)
    except (TypeError, ValueError):
        return "没有这支编队"
    conn = citydb.connect(readonly=True)
    try:
        row = conn.execute(
            "SELECT id FROM app_user WHERE username=?", (username,)).fetchone()
    finally:
        conn.close()
    if not row:
        return "没有这个账号"
    return _invite_id(team_id, int(row[0]))


def kick(team_id, user_id) -> str:
    """把这个号请出编队。成功返回空字符串。"""
    try:
        team_id = int(team_id)
        user_id = int(user_id)
    except (TypeError, ValueError):
        return "编队或账号不对"
    conn = citydb.connect()
    try:
        team = conn.execute(
            "SELECT organizer_id, IFNULL(name,'') FROM war_team WHERE id=?",
            (team_id,)).fetchone()
        if not team:
            return "没有这支编队"
        if int(team[0]) == user_id:
            return "指挥者不能踢出自己。要散伙就解散这支编队"
        row = conn.execute(
            "SELECT IFNULL(status,'wait') FROM war_member WHERE team_id=? AND user_id=?",
            (team_id, user_id)).fetchone()
        if not row:
            return "这个号不在这支编队里"
        if row[0] == "out":
            return "已经退出了"
        conn.execute(
            "UPDATE war_member SET accept=0, status='out' WHERE team_id=? AND user_id=?",
            (team_id, user_id))
        conn.commit()
        title = team[1] or _TEAM_NAME
    finally:
        conn.close()
    citydb.clear_desk(user_id)
    citydb.note_desk_text(f"已被请出编队「{title}」", user_id)
    return ""


def _invite_id(team_id, user_id) -> str:
    who = _account_row(user_id)
    if not who:
        return "没有这个账号"
    if not who["qq"]:
        return "这个号还没绑定攻打 QQ"
    conn = citydb.connect()
    try:
        team = conn.execute(
            "SELECT region, country_id, organizer_id, IFNULL(name,'') FROM war_team WHERE id=?",
            (int(team_id),)).fetchone()
        if not team:
            return "没有这支编队"
        if int(team[2]) == int(user_id):
            return "指挥者不用邀请自己"
        if who["region"] != int(team[0]):
            return "不在同一个区"
        if who["country"] and who["country"] != int(team[1]):
            return f"这个号的国家是{citydb.country_name(who['country']) or who['country']}"
        other = conn.execute(
            "SELECT team_id FROM war_member WHERE user_id=? AND team_id!=? "
            "AND IFNULL(status,'wait')!='out'",
            (int(user_id), int(team_id))).fetchone()
        if other:
            return "这个号已经在另一支编队里"
        here = conn.execute(
            "SELECT IFNULL(status,'wait') FROM war_member WHERE team_id=? AND user_id=?",
            (int(team_id), int(user_id))).fetchone()
        if here and here[0] == "in":
            return "已经在这支编队里"
        if here and here[0] == "wait":
            return "已经邀请过了"
        if here:
            conn.execute(
                "UPDATE war_member SET accept=0, status='wait' WHERE team_id=? AND user_id=?",
                (int(team_id), int(user_id)))
        else:
            conn.execute(
                "INSERT INTO war_member(team_id, user_id, accept, status) VALUES (?,?,0,'wait')",
                (int(team_id), int(user_id)))
        conn.commit()
        title = team[3] or _TEAM_NAME
    finally:
        conn.close()
    citydb.note_desk_text(f"收到编队「{title}」的邀请", int(user_id))
    return ""


def set_member(team_id, user_id, on) -> str:
    if on:
        return _invite_id(team_id, user_id)
    return kick(team_id, user_id)


def order(team_id, op, city_id) -> tuple:
    """让接受了指挥的队员做同一件事。组织者自己总是做。返回 (错误, 每人一行的说明)。"""
    op = str(op or "")
    if op not in _OPS:
        return "没有这个操作", []
    try:
        team_id = int(team_id)
        city_id = int(city_id or 0)
    except (TypeError, ValueError):
        return "编队或城市不对", []
    team = _team_row(team_id)
    if not team:
        return "没有这支编队", []
    if op != "morale" and city_id <= 0:
        return "要选一座城", []
    if op == "attack":
        owner = int((citydb.city_map().get(city_id) or {}).get("owner") or 0)
        if owner == int(team["country_id"]):
            return "这是己方城市", []
    members = _member_rows(team_id)
    if not members:
        return "队里还没有人", []
    lines = []
    seen = {}
    for who in members:
        name = who["username"] or str(who["id"])
        if who["state"] == "out":
            lines.append(f"{name}：已退出，跳过")
            continue
        if who["id"] != int(team["organizer_id"]) and who["state"] != "in":
            lines.append(f"{name}：等待加入，跳过")
            continue
        if who["country"] and who["country"] != int(team["country_id"]):
            got = citydb.country_name(who["country"]) or who["country"]
            lines.append(f"{name}：国家是{got}，和编队不是一个国家，跳过")
            continue
        qq = who["qq"]
        if qq and qq in seen:
            other, why = seen[qq]
            if why:
                lines.append(f"{name}：和{other}共用攻打 QQ，{why}")
            else:
                lines.append(f"{name}：和{other}共用攻打 QQ，跟着这一条一起做")
            continue
        why = citydb.request_desk(who["id"], op, city_id)
        if qq:
            seen[qq] = (name, why)
        if why:
            lines.append(f"{name}：{why}")
        else:
            lines.append(f"{name}：已排队，{_OPS[op]}")
    return "", lines


def _run_move(rec, sock, config, beater, city, country_war):
    label = _label(city)
    citydb.note_desk_text(f"正在移动到 {label}")
    with _lane():
        out = country_war.walk_to(
            rec, sock, config, city, beat=beater,
            march_only=True, enter_target=True)
    _note_walk(label, out, "已移动到", "移动到")
    _note_here(out)


def _run_morale(rec, sock, config, country_war):
    my = _country(rec, config, country_war)
    if not my:
        citydb.note_desk_text("读不到自己的国家，不能补士气")
        return
    with _lane():
        power, loc, _times, panel = country_war._panel(sock, rec, my)
        morale = country_war._read_path(panel, country_war.F_MORALE) if isinstance(panel, dict) else None
        _remember_here(loc, power, morale)
        if not isinstance(loc, int) or isinstance(loc, bool) or loc <= 0:
            citydb.note_desk_text("读不到当前所在城市，不能补士气")
            return
        spot = _label(loc)
        if not country_war._supply_city(my, loc):
            citydb.note_desk_text(f"人在 {spot}，不在首都和相邻城，不能用体力补士气")
            return
        if isinstance(morale, int) and not isinstance(morale, bool) and morale >= country_war.MORALE_CAP:
            citydb.note_desk_text(f"人在 {spot}，士气已经是满的")
            return
        if not isinstance(power, int) or isinstance(power, bool) or power <= country_war.SUPPLY_AP:
            shown = power if isinstance(power, int) and not isinstance(power, bool) else "未知"
            citydb.note_desk_text(f"人在 {spot}，体力 {shown}，要大于 {country_war.SUPPLY_AP} 才能补士气")
            return
        filled = country_war._fill_morale(sock, rec, power, morale)
        if filled is None:
            citydb.note_desk_text(f"人在 {spot}，补充士气没成功")
            return
        power2, loc2, _times2, panel2 = country_war._panel(sock, rec, my)
        morale2 = country_war._read_path(panel2, country_war.F_MORALE) if isinstance(panel2, dict) else filled
        _remember_here(loc2 or loc, power2, morale2 if isinstance(morale2, int) else filled)
        citydb.note_desk_text(f"在 {spot} 用体力补了士气，现在 {filled}")


def _run_attack(rec, sock, config, beater, city, country_war):
    my = _country(rec, config, country_war)
    if not my:
        citydb.note_desk_text("读不到自己的国家，不能攻打")
        return
    owner = int((citydb.city_map().get(city) or {}).get("owner") or 0)
    label = _label(city)
    if owner == my:
        citydb.note_desk_text(f"{label} 是己方城市")
        return
    citydb.note_desk_text(f"正在攻打 {label}")
    with _lane():
        out = country_war.walk_to(
            rec, sock, config, city, beat=beater,
            enter_target=True, max_steps=_ATTACK_STEPS)
    why = str((out or {}).get("停止原因") or "").strip()
    landed = int((out or {}).get("走到") or 0)
    if why:
        citydb.note_desk_text(f"攻打 {label} 时停下：{why}")
    elif landed == city:
        citydb.note_desk_text(f"已攻到 {label}")
    else:
        citydb.note_desk_text(
            f"朝 {label} 走了，停在 {_label(landed)}。这一次最多走进 {_ATTACK_STEPS} 座城")
    _note_here(out)


def _open_city(rec, sock, config, city, country_war):
    label = _label(city)
    citydb.note_desk_text(f"正在打开 {label}")
    my = _country(rec, config, country_war)
    with _lane():
        out = country_war.list_city_players(
            rec, sock, config, city, country=my or 0,
            start_page=0, end_page=4)
    why = str((out or {}).get("原因") or "").strip()
    players = []
    self_uid = str(getattr(rec, "uid", "") or "")
    for row in (out or {}).get("玩家") or []:
        if not isinstance(row, dict):
            continue
        uid = str(row.get("uid") or "")
        if self_uid and uid == self_uid:
            continue
        vip = row.get("vipLevel")
        office = row.get("officerPosition")
        players.append({
            "uid": uid,
            "name": row.get("name") or "",
            "lvl": row.get("lvl") or 0,
            "morale": row.get("morale") or 0,
            "vip": bool(isinstance(vip, int) and not isinstance(vip, bool) and vip > 0),
            "officer": office if isinstance(office, int) and not isinstance(office, bool) else 0,
            "power": row.get("combatPowerValue") or 0,
        })
        if len(players) >= 80:
            break
    owner = (out or {}).get("owner")
    owner_id = int(owner) if isinstance(owner, int) and not isinstance(owner, bool) else 0
    count = (out or {}).get("userCnt")
    if owner_id:
        citydb.record_occupy(city, owner_id, count if isinstance(count, int) else None)
    note = why or f"前 5 页看到 {len(players)} 人"
    citydb.save_desk_look({
        "city": city,
        "name": citydb.city_name(city) or "",
        "owner": owner_id,
        "owner_name": citydb.country_name(owner_id) if owner_id else "",
        "count": count if isinstance(count, int) and not isinstance(count, bool) else None,
        "players": players,
        "note": note,
    })
    citydb.note_desk_text(f"打开 {label}：{note}")


def run_command(rec, sock, config, beater, op: str, city: int) -> None:
    """攻打线程已经领走的一条指挥。连接断了照旧把 OSError 抛出去。"""
    from . import country_war

    op = str(op or "")
    city = int(city or 0)
    if op == "move":
        _run_move(rec, sock, config, beater, city, country_war)
    elif op == "morale":
        _run_morale(rec, sock, config, country_war)
    elif op == "open":
        _open_city(rec, sock, config, city, country_war)
    elif op == "attack":
        _run_attack(rec, sock, config, beater, city, country_war)


def _lane():
    from . import daily
    return daily.lane_hold()


def _country(rec, config, country_war) -> int:
    conf = config.get("国战") or {}
    try:
        country = int(conf.get("自己国家ID") or 0)
    except (TypeError, ValueError):
        country = 0
    if country <= 0:
        country = country_war._daily.read_my_country(rec) or 0
    return int(country or 0)


def _label(city) -> str:
    try:
        cid = int(city or 0)
    except (TypeError, ValueError):
        return ""
    name = citydb.city_name(cid) if cid else ""
    return f"{cid} {name}".strip()


def _note_walk(label, out, done, verb):
    why = str((out or {}).get("停止原因") or "").strip()
    landed = int((out or {}).get("走到") or 0)
    target = int((out or {}).get("目标") or 0)
    if why:
        citydb.note_desk_text(f"{verb} {label} 时停下：{why}")
    elif landed == target:
        steps = int((out or {}).get("步数") or 0)
        if steps <= 0:
            citydb.note_desk_text(f"人已经在 {label}")
        else:
            citydb.note_desk_text(f"{done} {label}")
    else:
        citydb.note_desk_text(f"朝 {label} 走了，停在 {_label(landed)}")


def _note_here(out):
    landed = int((out or {}).get("走到") or 0)
    if landed > 0:
        citydb.note_attack_here(landed)


def _remember_here(loc, power, morale):
    if isinstance(loc, int) and not isinstance(loc, bool) and loc > 0:
        citydb.note_attack_here(loc, power=power, morale=morale)


def _accounts() -> list:
    conn = citydb.connect(readonly=True)
    try:
        rows = conn.execute(
            "SELECT id, username, IFNULL(region,0), IFNULL(game_country,0), "
            "IFNULL(attack_qq,'') FROM app_user "
            "WHERE IFNULL(attack_qq,'')!='' ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    out = []
    for uid, username, region, country, qq in rows:
        status = citydb.attack_status(int(uid))
        here = str(status.get("here") or "")
        here_id = int(here.split(" ", 1)[0]) if here.split(" ", 1)[0].isdigit() else 0
        out.append({
            "id": int(uid),
            "username": username or "",
            "region": int(region or 0),
            "country": int(country or 0),
            "country_name": citydb.country_name(int(country or 0)) if int(country or 0) else "",
            "qq": str(qq or ""),
            "online": bool(status.get("online")),
            "paused": bool(status.get("paused")),
            "detail": status.get("detail") or "",
            "here": here,
            "here_id": here_id,
            "morale": status.get("morale"),
            "power": status.get("power"),
            "desk_note": status.get("desk_note") or "",
            "look": citydb.load_desk_look(int(uid)),
        })
    return out


def _account_row(user_id: int):
    conn = citydb.connect(readonly=True)
    try:
        row = conn.execute(
            "SELECT id, username, IFNULL(region,0), IFNULL(game_country,0), "
            "IFNULL(attack_qq,'') FROM app_user WHERE id=?",
            (int(user_id),)).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return {
        "id": int(row[0]),
        "username": row[1] or "",
        "region": int(row[2] or 0),
        "country": int(row[3] or 0),
        "qq": str(row[4] or ""),
    }


def _team_row(team_id: int):
    conn = citydb.connect(readonly=True)
    try:
        row = conn.execute(
            "SELECT id, region, country_id, organizer_id, IFNULL(name,'') FROM war_team WHERE id=?",
            (int(team_id),)).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return {
        "id": int(row[0]),
        "region": int(row[1] or 0),
        "country_id": int(row[2] or 0),
        "organizer_id": int(row[3] or 0),
        "name": row[4] or _TEAM_NAME,
    }


def _member_rows(team_id: int) -> list:
    conn = citydb.connect(readonly=True)
    try:
        rows = conn.execute(
            "SELECT u.id, u.username, IFNULL(u.region,0), IFNULL(u.game_country,0), "
            "IFNULL(u.attack_qq,''), IFNULL(m.status,'wait') "
            "FROM war_member m JOIN app_user u ON u.id=m.user_id "
            "WHERE m.team_id=? ORDER BY u.id",
            (int(team_id),)).fetchall()
    finally:
        conn.close()
    return [{
        "id": int(row[0]),
        "username": row[1] or "",
        "region": int(row[2] or 0),
        "country": int(row[3] or 0),
        "qq": str(row[4] or ""),
        "state": row[5] if row[5] in ("wait", "in", "out") else "wait",
    } for row in rows]


def _seat(state, online, paused) -> str:
    if state == "out":
        return "已退出"
    if state != "in":
        return "等待加入"
    if not online:
        return "离线"
    if paused:
        return "暂停"
    return "已加入"


def _roster(team_id, organizer_id) -> list:
    out = []
    for who in _member_rows(team_id):
        status = citydb.attack_status(who["id"])
        online = bool(status.get("online"))
        paused = bool(status.get("paused"))
        out.append({
            "id": who["id"],
            "username": who["username"],
            "state": who["state"],
            "status": _seat(who["state"], online, paused),
            "organizer": who["id"] == int(organizer_id),
        })
    return out


def _teams() -> list:
    conn = citydb.connect(readonly=True)
    try:
        teams = conn.execute(
            "SELECT id, region, country_id, organizer_id, IFNULL(name,'') "
            "FROM war_team ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    out = []
    for team_id, region, country_id, organizer_id, name in teams:
        roster = _roster(team_id, organizer_id)
        out.append({
            "id": int(team_id),
            "name": name or _TEAM_NAME,
            "region": int(region or 0),
            "country_id": int(country_id or 0),
            "country_name": citydb.country_name(int(country_id or 0)),
            "organizer_id": int(organizer_id or 0),
            "members": [item["id"] for item in roster if item["state"] == "in"],
            "roster": roster,
        })
    return out


def _active_row(user_id):
    conn = citydb.connect(readonly=True)
    try:
        return conn.execute(
            "SELECT t.id, t.region, t.country_id, t.organizer_id, IFNULL(t.name,''), "
            "IFNULL(m.status,'wait'), IFNULL(o.username,'') "
            "FROM war_member m JOIN war_team t ON t.id=m.team_id "
            "LEFT JOIN app_user o ON o.id=t.organizer_id "
            "WHERE m.user_id=? AND IFNULL(m.status,'wait')!='out' "
            "ORDER BY t.id DESC LIMIT 1",
            (int(user_id),)).fetchone()
    finally:
        conn.close()


def command_of(user_id) -> dict:
    """这个号还在的编队或邀请。已经退出则空。"""
    try:
        user_id = int(user_id)
    except (TypeError, ValueError):
        return {}
    row = _active_row(user_id)
    if not row:
        return {}
    country_id = int(row[2] or 0)
    organizer_id = int(row[3] or 0)
    state = row[5] if row[5] in ("wait", "in") else "wait"
    return {
        "team_id": int(row[0]),
        "name": row[4] or _TEAM_NAME,
        "region": int(row[1] or 0),
        "country_id": country_id,
        "country_name": citydb.country_name(country_id),
        "organizer_id": organizer_id,
        "organizer": row[6] or "指挥者",
        "self": organizer_id == user_id,
        "state": state,
        "accept": state == "in",
        "roster": _roster(row[0], organizer_id),
    }


def _led_by(user_id):
    """这个号指挥的编队。不是指挥者返回 0。"""
    row = _active_row(user_id)
    if not row or int(row[3]) != int(user_id) or row[5] != "in":
        return 0
    return int(row[0])


def invite_own(user_id, username) -> str:
    team_id = _led_by(user_id)
    if not team_id:
        return "你不是编队的指挥者"
    return invite(team_id, username)


def kick_own(user_id, member_id) -> str:
    team_id = _led_by(user_id)
    if not team_id:
        return "你不是编队的指挥者"
    return kick(team_id, member_id)


def rename_own(user_id, name) -> str:
    team_id = _led_by(user_id)
    if not team_id:
        return "你不是编队的指挥者"
    return rename_team(team_id, name)


def set_accept(user_id, on) -> str:
    """接受邀请就入队。拒绝或退出记成已退出。成功返回空字符串。"""
    try:
        user_id = int(user_id)
    except (TypeError, ValueError):
        return "没有这个账号"
    on = str(on).lower() in ("1", "true", "on", "yes") if not isinstance(on, bool) else on
    conn = citydb.connect()
    try:
        row = conn.execute(
            "SELECT t.organizer_id, IFNULL(m.status,'wait'), IFNULL(t.name,'') "
            "FROM war_member m JOIN war_team t ON t.id=m.team_id "
            "WHERE m.user_id=? AND IFNULL(m.status,'wait')!='out' "
            "ORDER BY t.id DESC LIMIT 1",
            (user_id,)).fetchone()
        if not row:
            return "没有这份邀请"
        if int(row[0]) == user_id:
            return "指挥者不用接受自己的邀请"
        title = row[2] or _TEAM_NAME
        was_in = row[1] == "in"
        if on:
            conn.execute(
                "UPDATE war_member SET accept=1, status='in' WHERE user_id=? "
                "AND IFNULL(status,'wait')!='out'",
                (user_id,))
        else:
            conn.execute(
                "UPDATE war_member SET accept=0, status='out' WHERE user_id=? "
                "AND IFNULL(status,'wait')!='out'",
                (user_id,))
        conn.commit()
    finally:
        conn.close()
    if on:
        citydb.note_desk_text(f"已加入编队「{title}」", user_id)
    else:
        citydb.clear_desk(user_id)
        citydb.note_desk_text("已退出编队" if was_in else "已拒绝邀请", user_id)
    return ""


def _countries() -> list:
    conn = citydb.connect(readonly=True)
    try:
        rows = conn.execute("SELECT id, name FROM country ORDER BY id").fetchall()
    finally:
        conn.close()
    return [{"id": int(row[0]), "name": row[1] or ""} for row in rows]
