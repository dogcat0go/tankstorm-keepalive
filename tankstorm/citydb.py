# tankstorm-keepalive  Copyright (C) 2026 Dimlitter
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# 本程序是自由软件：你可以依据自由软件基金会发布的 GNU Affero 通用公共许可证
# （第 3 版，或你选择的任何更新版本）之条款，再分发和/或修改它。
# 本程序希望能有用，但不提供任何担保；甚至不含适销性或特定用途适用性的默示担保。
# 详见随附的 LICENSE 文件，或 <https://www.gnu.org/licenses/>。
"""城市玩家 SQLite：目录表来自游戏 CDN 的 CityData / CountryName，玩家表由
`--city-players` 翻页写入。库文件在程序目录 `city_players.db`。
"""

import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from .log import get_logger
from .paths import user_path

log = get_logger()

DB_FILE = user_path("city_players.db")

# 文件名带版本日期，游戏改版后从 config_*.xml 的 countryCity / countryName 更新。
_CDN = "https://redwar-cdn.sincetimes.com/100616028/res/20120522/config/"
CITY_DAT = _CDN + "CityData_2019091902.dat"
COUNTRY_DAT = _CDN + "CountryName_2018122001.dat"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS country (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS city (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    country_id  INTEGER,
    city_level  INTEGER,
    city_type   INTEGER
);
CREATE TABLE IF NOT EXISTS player (
    uid               TEXT NOT NULL,
    city_id           INTEGER NOT NULL,
    name              TEXT,
    lvl               INTEGER,
    morale            INTEGER,
    combat_power      INTEGER,
    country_id        INTEGER,
    vip_type          INTEGER,
    vip_level         INTEGER,
    officer_position  INTEGER,
    league_point      INTEGER,
    league_title      INTEGER,
    pic               TEXT,
    fetched_at        TEXT NOT NULL,
    page              INTEGER,
    PRIMARY KEY (uid, city_id)
);
CREATE INDEX IF NOT EXISTS player_city ON player(city_id);
CREATE TABLE IF NOT EXISTS atk_fail (
    uid      TEXT PRIMARY KEY,
    name     TEXT,
    city_id  INTEGER,
    ret      INTEGER,
    reason   TEXT,
    at       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS city_occupy (
    city_id         INTEGER PRIMARY KEY,
    occupy_country  INTEGER,
    user_cnt        INTEGER,
    fetched_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS app_user (
    id              INTEGER PRIMARY KEY,
    username        TEXT UNIQUE NOT NULL,
    password_hash   TEXT NOT NULL,
    feishu_webhook  TEXT,
    qq_api          TEXT,
    qq_token        TEXT,
    qq_target       TEXT,
    expires_at      TEXT,
    tier            TEXT NOT NULL DEFAULT '初级',
    admin           INTEGER NOT NULL DEFAULT 0,
    auto_lock       INTEGER NOT NULL DEFAULT 0,
    hold_min        INTEGER NOT NULL DEFAULT 0,
    card_max        INTEGER NOT NULL DEFAULT 100,
    created_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scan_plan (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    gap_sec      INTEGER NOT NULL,
    quiet_start  TEXT NOT NULL DEFAULT '',
    quiet_end    TEXT NOT NULL DEFAULT '',
    ranges_json  TEXT NOT NULL DEFAULT '[]',
    updated_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS atk_order (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER NOT NULL,
    city_id     INTEGER NOT NULL,
    uid         TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL,
    reason      TEXT,
    auto        INTEGER NOT NULL DEFAULT 0,
    beats       INTEGER,
    card_max    INTEGER,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS atk_signal (
    name  TEXT PRIMARY KEY,
    value TEXT,
    at    TEXT
);
CREATE TABLE IF NOT EXISTS app_session (
    token      TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS watch_sub (
    user_id      INTEGER NOT NULL,
    city_id      INTEGER NOT NULL,
    uid          TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    last_present INTEGER,
    PRIMARY KEY (user_id, city_id, uid)
);
"""


_schema_ready = False

# Windows 上 sqlite3.connect 的 timeout 拦不住文件锁，会在任务线程里卡死。
# 写库丢到短线程里，超过这个时间就放弃，让打人继续。
DB_OP_TIMEOUT = 1.0


def _run_timeout(fn, timeout=DB_OP_TIMEOUT, default=None):
    box = [default]

    def run():
        try:
            box[0] = fn()
        except Exception as exc:
            box[0] = exc

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        log.info("sqlite 超过 %.0f 秒未完成，跳过本次写库", timeout)
        return default
    if isinstance(box[0], Exception):
        log.info("sqlite 失败，跳过：%s", box[0])
        return default
    return box[0]


def connect(readonly=False, timeout=15):
    """打开库。只读不改 journal，避免 DB Browser 开着时 PRAGMA 抢锁失败。

    建表/改列只做一次。每次 connect 都 PRAGMA journal_mode 的话，
    DB Browser 占着写锁会连等三次，国战心跳就被拖死。
    """
    global _schema_ready
    if not _schema_ready:
        setup = sqlite3.connect(DB_FILE, timeout=timeout)
        try:
            setup.execute("PRAGMA journal_mode=WAL")
            setup.executescript(_SCHEMA)
            cols = {r[1] for r in setup.execute("PRAGMA table_info(player)")}
            if "page" not in cols:
                setup.execute("ALTER TABLE player ADD COLUMN page INTEGER")
                setup.commit()
            ccols = {r[1] for r in setup.execute("PRAGMA table_info(city)")}
            if "near_city" not in ccols:
                setup.execute("ALTER TABLE city ADD COLUMN near_city TEXT")
                setup.commit()
            wcols = {r[1] for r in setup.execute("PRAGMA table_info(watch_sub)")}
            if wcols and "user_id" not in wcols:
                setup.execute("DROP TABLE watch_sub")
                setup.executescript(_SCHEMA)
                setup.commit()
            ucols = {r[1] for r in setup.execute("PRAGMA table_info(app_user)")}
            if ucols and "expires_at" not in ucols:
                setup.execute("ALTER TABLE app_user ADD COLUMN expires_at TEXT")
                setup.commit()
            if ucols and "tier" not in ucols:
                setup.execute(
                    "ALTER TABLE app_user ADD COLUMN tier TEXT NOT NULL DEFAULT '初级'")
                setup.commit()
            if ucols and "admin" not in ucols:
                setup.execute(
                    "ALTER TABLE app_user ADD COLUMN admin INTEGER NOT NULL DEFAULT 0")
                setup.commit()
            if ucols and "auto_lock" not in ucols:
                setup.execute(
                    "ALTER TABLE app_user ADD COLUMN auto_lock INTEGER NOT NULL DEFAULT 0")
                setup.commit()
            if ucols and "hold_min" not in ucols:
                setup.execute(
                    "ALTER TABLE app_user ADD COLUMN hold_min INTEGER NOT NULL DEFAULT 0")
                setup.commit()
            if ucols and "card_max" not in ucols:
                setup.execute(
                    "ALTER TABLE app_user ADD COLUMN card_max INTEGER NOT NULL DEFAULT 100")
                setup.commit()
            ocols = {r[1] for r in setup.execute("PRAGMA table_info(atk_order)")}
            if ocols and "uid" not in ocols:
                setup.execute(
                    "ALTER TABLE atk_order ADD COLUMN uid TEXT NOT NULL DEFAULT ''")
                setup.commit()
            if ocols and "auto" not in ocols:
                setup.execute(
                    "ALTER TABLE atk_order ADD COLUMN auto INTEGER NOT NULL DEFAULT 0")
                setup.commit()
            if ocols and "beats" not in ocols:
                setup.execute("ALTER TABLE atk_order ADD COLUMN beats INTEGER")
                setup.commit()
            if ocols and "card_max" not in ocols:
                setup.execute("ALTER TABLE atk_order ADD COLUMN card_max INTEGER")
                setup.commit()
            _schema_ready = True
        finally:
            setup.close()
    if readonly:
        uri = Path(DB_FILE).resolve().as_uri() + "?mode=ro"
        return sqlite3.connect(uri, uri=True, timeout=timeout)
    return sqlite3.connect(DB_FILE, timeout=timeout)


def _gbk_tsv(url: str) -> list:
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    lines = r.content.decode("gbk").splitlines()
    rows = []
    for line in lines[2:]:          # 第 0 行类型，第 1 行中文列名
        if not line.strip():
            continue
        rows.append(line.split("\t"))
    return rows


def _parse_near(s):
    out = []
    for x in str(s or "").replace('"', "").split(","):
        x = x.strip()
        if x.isdigit():
            out.append(int(x))
    return out


def ensure_catalog(force: bool = False) -> int:
    """把官方城市表灌进 sqlite。已有数据且不 force 就跳过下载。返回城市数。"""
    conn = connect()
    try:
        n = conn.execute("SELECT COUNT(*) FROM city").fetchone()[0]
        near_n = conn.execute(
            "SELECT COUNT(*) FROM city WHERE IFNULL(near_city,'')!=''"
        ).fetchone()[0]
        if n and near_n and not force:
            return n
        cities = _gbk_tsv(CITY_DAT)
        countries = _gbk_tsv(COUNTRY_DAT)
        conn.executemany(
            "INSERT OR REPLACE INTO country(id, name) VALUES (?,?)",
            [(int(c[0]), c[1]) for c in countries if c[0].isdigit()])
        rows = []
        for c in cities:
            if not c[0].isdigit():
                continue
            near = ",".join(str(x) for x in _parse_near(c[10] if len(c) > 10 else ""))
            rows.append((int(c[0]), c[1],
                         int(c[2]) if len(c) > 2 and c[2].isdigit() else None,
                         int(c[3]) if len(c) > 3 and c[3].isdigit() else None,
                         int(c[6]) if len(c) > 6 and c[6].isdigit() else None,
                         near or None))
        conn.executemany(
            "INSERT OR REPLACE INTO city(id, name, country_id, city_level, city_type, near_city) "
            "VALUES (?,?,?,?,?,?)", rows)
        conn.commit()
        log.info("已写入城市目录 %d 座、阵营 %d 个 → %s",
                 len(rows), len(countries), DB_FILE)
        return len(rows)
    finally:
        conn.close()


def city_name(city_id: int) -> str:
    conn = connect()
    try:
        row = conn.execute("SELECT name FROM city WHERE id=?", (int(city_id),)).fetchone()
        return row[0] if row else ""
    finally:
        conn.close()


def country_name(country_id) -> str:
    if not country_id:
        return ""
    conn = connect()
    try:
        row = conn.execute("SELECT name FROM country WHERE id=?",
                           (int(country_id),)).fetchone()
        return row[0] if row else ""
    finally:
        conn.close()


def record_occupy(city_id, country_id, user_cnt=None, fetched_at=None):
    """记下这座城当前被哪个国家占着。不改目录表里的原属国。"""
    try:
        conn = connect(timeout=2)
    except sqlite3.OperationalError as exc:
        log.info("写归属国时 sqlite 忙，跳过：%s", exc)
        return
    try:
        conn.execute(
            "INSERT INTO city_occupy(city_id, occupy_country, user_cnt, fetched_at) "
            "VALUES (?,?,?,?) "
            "ON CONFLICT(city_id) DO UPDATE SET "
            "occupy_country=excluded.occupy_country, "
            "user_cnt=excluded.user_cnt, fetched_at=excluded.fetched_at",
            (int(city_id), country_id, user_cnt, fetched_at or now_ts()))
        conn.commit()
    except sqlite3.OperationalError as exc:
        log.info("写归属国时 sqlite 忙，跳过：%s", exc)
    finally:
        conn.close()


def neighbors(city_id: int) -> set:
    """官方 CityData.nearCity。同一座城不算邻居，用 can_reach 判断能不能打。"""
    ensure_catalog()
    conn = connect()
    try:
        row = conn.execute("SELECT near_city FROM city WHERE id=?",
                           (int(city_id),)).fetchone()
        return set(_parse_near(row[0] if row else ""))
    finally:
        conn.close()


def fort_locked(city_id) -> bool:
    """编号第 2 位是 1 或 2 的城，不是自己国家时不能占领。"""
    s = str(int(city_id or 0))
    return len(s) > 1 and s[1] in "12"


def can_reach(here, there) -> bool:
    """自己所在城能否打到目标城：同城，或官方连接城市（双向）。"""
    here, there = int(here or 0), int(there or 0)
    if not here or not there:
        return False
    if here == there:
        return True
    return there in neighbors(here) or here in neighbors(there)


def city_map() -> dict:
    """id → {name, owner, near}。归属优先用占领记录，没有则用原属国。边补成双向。"""
    ensure_catalog()
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT c.id, c.name, IFNULL(c.country_id,0), "
            "o.occupy_country, o.user_cnt, c.near_city "
            "FROM city c LEFT JOIN city_occupy o ON o.city_id=c.id"
        ).fetchall()
    finally:
        conn.close()
    g = {}
    for cid, name, home, occ, users, near in rows:
        cid = int(cid)
        g[cid] = {
            "name": name or "",
            "home": int(home or 0),
            "owner": int(occ) if isinstance(occ, int) else int(home or 0),
            "users": int(users) if isinstance(users, int) else 0,
            "near": set(_parse_near(near)),
        }
    for cid, info in list(g.items()):
        info["near"] = {n for n in info["near"] if n in g and n != cid}
    for cid, info in g.items():
        for n in info["near"]:
            g[n]["near"].add(cid)
    return g


def plan_route(here, target, my_country, avoid=None, avoid_why=None) -> dict:
    """规划从当前城打到目标城的走法。只算路线，不发移动包。

    归属国与自己相同的城可以直接经过。别国的城要先占领，才能落脚或当走廊。
    编号第 2 位是 1 或 2、又不是自己国家的城不能占领，也不能借道。
    原属国是 21（黑暗联盟）的城例外，可以占领。
    目标城本身不必走进去，站在相邻城就能打。先走最短：少占领，再少走几步。
    avoid 里的城是已经打不过的，这条路不再经过。
    """
    import heapq

    here, target = int(here or 0), int(target or 0)
    my = int(my_country or 0)
    g = city_map()
    out = {"here": here, "target": target, "my": my, "可打": False,
           "落点": 0, "路径": [], "须占领": [], "原因": ""}

    def label(cid):
        info = g.get(cid) or {}
        return f"{cid} {info.get('name') or ''}".strip()

    if here not in g or target not in g:
        out["原因"] = "起点或目标不在城市目录里"
        return out
    if not my:
        out["原因"] = "没有自己的国家ID"
        return out

    def mine(cid):
        return g[cid]["owner"] == my

    skip = {int(c) for c in (avoid or ())}
    why = {}
    for key, name in (avoid_why or {}).items():
        try:
            cid = int(key)
        except (TypeError, ValueError):
            continue
        text = str(name or "").strip()
        if cid > 0 and text:
            why[cid] = text

    def is_fort(cid):
        home = g[cid].get("home") or 0
        return fort_locked(cid) and not mine(cid) and home != 21

    def blocked(cid):
        return cid in skip or is_fort(cid)

    def search(forbid):
        pq = [(0, 0, here)]          # (须占领数, 步数, 城市)
        best = {here: (0, 0)}
        prev = {here: None}
        found = None
        while pq:
            occ, hops, u = heapq.heappop(pq)
            if (occ, hops) != best.get(u):
                continue
            if u == target or target in g[u]["near"]:
                found = u
                break
            for v in g[u]["near"]:
                if v == target or forbid(v):
                    continue
                add = 0 if mine(v) else 1
                nxt = (occ + add, hops + 1)
                if nxt < best.get(v, (10 ** 9, 10 ** 9)):
                    best[v] = nxt
                    prev[v] = u
                    heapq.heappush(pq, (*nxt, v))
        if found is None:
            return None
        path = []
        cur = found
        while cur is not None:
            path.append(cur)
            cur = prev[cur]
        path.reverse()
        return path

    path = search(blocked)
    if path is None:
        opened = search(lambda cid: False)
        parts = []
        forts = []
        for cid in opened or []:
            if cid == here:
                continue
            if cid in skip:
                who = why.get(cid) or ""
                if not who:
                    who = "、".join(failed_names_in(cid))
                if who:
                    parts.append(f"{label(cid)} 有 {who} 挡路")
                else:
                    parts.append(f"{label(cid)} 有打不过的人挡路")
            elif is_fort(cid):
                forts.append(label(cid))
        if forts:
            shown = "、".join(forts[:6])
            if len(forts) > 6:
                shown += f" 等 {len(forts)} 座城"
            parts.append(
                f"{shown} 不是自己国家，编号第2位是1或2，不能占领，也不能借道")
        if parts:
            out["原因"] = f"从 {label(here)} 到 {label(target)} 没有通路。" + "；".join(parts)
        else:
            out["原因"] = f"从 {label(here)} 到 {label(target)} 没有通路。城市之间没有相连的路"
        return out
    found = path[-1]
    must = [c for c in path if c != here and not mine(c)]
    out["落点"] = found
    out["路径"] = path
    out["须占领"] = must
    out["可打"] = not must
    via = " → ".join(label(c) for c in path)
    if must:
        block = " → ".join(label(c) for c in must)
        out["原因"] = (f"还不能直接打。先按顺序占领 {block}，"
                      f"再沿 {via} 走到 {label(found)} 打 {label(target)}")
    elif found == here:
        out["原因"] = f"人已经在 {label(here)}，可以直接打 {label(target)}"
    else:
        out["原因"] = f"沿同国城市 {via} 走到 {label(found)}，就能打 {label(target)}"
    return out


def format_route(plan) -> list:
    """把 plan_route 的结果画成从上到下的路线，每行一条，方便打进日志。"""
    lines = []
    if plan.get("原因"):
        lines.append(plan["原因"])
    path = plan.get("路径") or []
    if not path:
        return lines
    g = city_map()
    conn = connect(readonly=True)
    try:
        countries = {int(i): n for i, n in conn.execute(
            "SELECT id, name FROM country")}
    finally:
        conn.close()
    here, target = plan.get("here"), plan.get("target")
    must = set(plan.get("须占领") or [])

    def one(cid, tag):
        info = g.get(cid) or {}
        owner = countries.get(info.get("owner") or 0) or "未知"
        return f"{cid} {info.get('name') or ''}（{owner}）  [{tag}]"

    for i, cid in enumerate(path):
        if i:
            hop = "须占领后通过" if cid in must else "同国，直接通过"
            lines.append(f"   |  {hop}")
            lines.append("   v")
        tags = []
        if cid == here:
            tags.append("起点")
        if cid == plan.get("落点"):
            tags.append("落点")
        if cid in must:
            tags.append("须占领")
        if cid == target:
            tags.append("目标")
        lines.append(one(cid, "·".join(tags) or "经过"))
    if target not in path:
        lines.append("   |  相邻，从落点打")
        lines.append("   v")
        lines.append(one(target, "目标"))
    return lines


def list_cities() -> list:
    """[(id, name, country_id, country_name), ...] 按 id 排。"""
    ensure_catalog()
    conn = connect()
    try:
        return conn.execute(
            "SELECT c.id, c.name, c.country_id, IFNULL(n.name,'') "
            "FROM city c LEFT JOIN country n ON n.id=c.country_id "
            "ORDER BY c.id").fetchall()
    finally:
        conn.close()


def upsert_players(city_id: int, players: list, fetched_at: str, page=None):
    """写入一页玩家。同一座城从第 0 页拉全量且成功结束后，再调 drop_stale。"""
    if not players:
        return 0
    city_id = int(city_id)
    rows = []
    for p in players:
        uid = str(p.get("uid") or "").strip()
        if not uid:
            continue
        pg = p.get("page") if p.get("page") is not None else page
        rows.append((
            uid, city_id, p.get("name"), p.get("lvl"), p.get("morale"),
            p.get("combatPowerValue"), p.get("countryID"), p.get("vipType"),
            p.get("vipLevel"), p.get("officerPosition"), p.get("leaguePoint"),
            p.get("leagueTitle"), p.get("pic"), fetched_at, pg,
        ))
    if not rows:
        return 0
    conn = connect()
    try:
        conn.executemany(
            "INSERT INTO player(uid, city_id, name, lvl, morale, combat_power, "
            "country_id, vip_type, vip_level, officer_position, league_point, "
            "league_title, pic, fetched_at, page) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(uid, city_id) DO UPDATE SET "
            "name=excluded.name, lvl=excluded.lvl, morale=excluded.morale, "
            "combat_power=excluded.combat_power, country_id=excluded.country_id, "
            "vip_type=excluded.vip_type, vip_level=excluded.vip_level, "
            "officer_position=excluded.officer_position, "
            "league_point=excluded.league_point, league_title=excluded.league_title, "
            "pic=excluded.pic, fetched_at=excluded.fetched_at, page=excluded.page",
            rows)
        conn.commit()
        return len(rows)
    finally:
        conn.close()


def drop_stale(city_id: int, fetched_at: str) -> int:
    """删掉这座城里本次没再出现的人（完整拉完才调用）。"""
    conn = connect()
    try:
        cur = conn.execute(
            "DELETE FROM player WHERE city_id=? AND fetched_at<?",
            (int(city_id), fetched_at))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def now_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def beijing_day() -> str:
    return datetime.now(timezone.utc).astimezone(
        timezone(timedelta(hours=8))).strftime("%Y-%m-%d")


TIERS = ("初级", "中级", "高级")


def attack_tier(tier: str) -> bool:
    """中级和高级可以使用远程扫码攻打。"""
    return (tier or "初级") in ("中级", "高级")


def account_expired(expires_at: str) -> bool:
    """有效期是北京时间的日期，这一天仍然有效。空表示不限期。"""
    day = (expires_at or "").strip()[:10]
    if not day:
        return False
    try:
        datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        return True
    return day < beijing_day()


def beijing_ts(ts: str) -> str:
    """库存 UTC（末尾 Z）换成北京时间，给页面显示。"""
    if not ts:
        return ""
    try:
        dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return ts
    return dt.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")


def _password_hash(password: str, salt: str = "") -> str:
    import hashlib
    import secrets
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 120000)
    return f"{salt}${dk.hex()}"


def create_user(username: str, password: str, expires_at: str = "", tier: str = "初级"):
    """创建账号。用户名已存在返回 None。expires_at 为北京时间日期，空表示不限期。"""
    conn = connect()
    try:
        cur = conn.execute(
            "INSERT INTO app_user(username, password_hash, expires_at, tier, created_at) "
            "VALUES (?,?,?,?,?)",
            (username, _password_hash(password), (expires_at or "").strip(),
             tier or "初级", now_ts()))
        conn.commit()
        return int(cur.lastrowid)
    except sqlite3.IntegrityError:
        return None
    finally:
        conn.close()


def set_user_admin(username: str, on: bool) -> bool:
    """打开或关掉扫描安排权限。没有这个用户返回 False。"""
    conn = connect()
    try:
        cur = conn.execute(
            "UPDATE app_user SET admin=? WHERE username=?",
            (1 if on else 0, username))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def set_user_tier(username: str, tier: str) -> bool:
    """把账号标成初级、中级或高级。没有这个用户返回 False。"""
    conn = connect()
    try:
        cur = conn.execute(
            "UPDATE app_user SET tier=? WHERE username=?", (tier, username))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def set_user_expiry(username: str, expires_at: str) -> bool:
    """改这个账号的有效期。没有这个用户返回 False。"""
    conn = connect()
    try:
        cur = conn.execute(
            "UPDATE app_user SET expires_at=? WHERE username=?",
            ((expires_at or "").strip(), username))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def login_user(username: str, password: str):
    """密码正确返回 session token。密码不对返回 None，账号过期返回 False。"""
    import secrets
    conn = connect()
    try:
        row = conn.execute(
            "SELECT id, password_hash, IFNULL(expires_at,'') FROM app_user WHERE username=?",
            (username,)).fetchone()
        if not row:
            return None
        salt, digest = row[1].split("$", 1)
        if not secrets.compare_digest(_password_hash(password, salt).split("$", 1)[1], digest):
            return None
        if account_expired(row[2]):
            return False
        token = secrets.token_urlsafe(32)
        conn.execute(
            "INSERT INTO app_session(token, user_id, created_at) VALUES (?,?,?)",
            (token, row[0], now_ts()))
        conn.commit()
        return token
    finally:
        conn.close()


def user_by_token(token: str):
    if not token:
        return None
    conn = connect(readonly=True)
    try:
        row = conn.execute(
            "SELECT u.id, u.username, IFNULL(u.qq_target,''), IFNULL(u.expires_at,''), "
            "IFNULL(u.tier,'初级'), IFNULL(u.admin,0), IFNULL(u.auto_lock,0), "
            "IFNULL(u.hold_min,0), IFNULL(u.card_max,100) "
            "FROM app_session s JOIN app_user u ON u.id=s.user_id WHERE s.token=?",
            (token,)).fetchone()
        if not row or account_expired(row[3]):
            return None
        return {"id": row[0], "username": row[1], "qq_target": row[2],
                "expires_at": row[3], "tier": row[4], "admin": bool(row[5]),
                "auto_lock": bool(row[6]), "hold_min": int(row[7] or 0),
                "card_max": int(row[8] if row[8] is not None else 100)}
    finally:
        conn.close()


def open_session(username: str) -> str:
    """给账号发一张登录态。没有这个账号就建一个，测试入口用，不校验密码。"""
    import secrets
    conn = connect()
    try:
        row = conn.execute(
            "SELECT id FROM app_user WHERE username=?", (username,)).fetchone()
        if row:
            user_id = row[0]
        else:
            cur = conn.execute(
                "INSERT INTO app_user(username, password_hash, created_at) VALUES (?,?,?)",
                (username, _password_hash(secrets.token_urlsafe(18)), now_ts()))
            user_id = int(cur.lastrowid)
        token = secrets.token_urlsafe(32)
        conn.execute(
            "INSERT INTO app_session(token, user_id, created_at) VALUES (?,?,?)",
            (token, user_id, now_ts()))
        conn.commit()
        return token
    finally:
        conn.close()


def logout_token(token: str) -> None:
    if not token:
        return
    conn = connect()
    try:
        conn.execute("DELETE FROM app_session WHERE token=?", (token,))
        conn.commit()
    finally:
        conn.close()


def save_push(user_id: int, qq_target: str) -> None:
    """只存这个账号的接收 QQ。机器人地址和 Token 在服务器配置里。"""
    conn = connect()
    try:
        conn.execute(
            "UPDATE app_user SET qq_target=?, feishu_webhook='', qq_api='', qq_token='' "
            "WHERE id=?",
            (qq_target, int(user_id)))
        conn.commit()
    finally:
        conn.close()


def add_attack_order(user_id: int, city_id: int, uid: str, cards=None) -> str:
    """提交一条远程扫码攻打。已有未完成的单时返回原因，成功返回空字符串。"""
    if cards is None:
        cards = 100
    conn = connect()
    try:
        row = conn.execute(
            "SELECT 1 FROM atk_order WHERE user_id=? AND status IN ('pending','running')",
            (int(user_id),)).fetchone()
        if row:
            return "已经有一条还没打完"
        now = now_ts()
        conn.execute(
            "INSERT INTO atk_order(user_id, city_id, uid, status, reason, card_max, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (int(user_id), int(city_id), str(uid).strip(), "pending", "",
             int(cards), now, now))
        conn.commit()
        return ""
    finally:
        conn.close()


def set_attack_cards(user_id: int, cards) -> str:
    """这一单最多用几张恢复卡。成功返回空字符串。"""
    try:
        n = int(str(cards).strip())
    except (TypeError, ValueError, AttributeError):
        return "恢复卡数量要是数字"
    if n < 0 or n > 999:
        return "恢复卡数量要是 0 到 999"
    conn = connect()
    try:
        conn.execute(
            "UPDATE app_user SET card_max=? WHERE id=?",
            (n, int(user_id)))
        conn.commit()
    finally:
        conn.close()
    return ""


def set_attack_hold(user_id: int, minutes) -> str:
    """挂机保活分钟。0 表示打完就断开。成功返回空字符串。"""
    try:
        n = int(str(minutes).strip())
    except (TypeError, ValueError, AttributeError):
        return "挂机保活要是分钟数"
    if n < 0 or n > 1440:
        return "挂机保活要是 0 到 1440 分钟"
    conn = connect()
    try:
        conn.execute(
            "UPDATE app_user SET hold_min=? WHERE id=?",
            (n, int(user_id)))
        conn.commit()
    finally:
        conn.close()
    return ""


def note_attack_hold(until_epoch: float) -> None:
    """记下这次挂机保活到什么时候。重连时接着用，不重新计时。"""
    text = datetime.fromtimestamp(float(until_epoch), timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO atk_signal(name, value, at) VALUES ('hold', ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET value=excluded.value, at=excluded.at",
            (text, now_ts()))
        conn.commit()
    finally:
        conn.close()


def clear_attack_hold() -> None:
    conn = connect()
    try:
        conn.execute("DELETE FROM atk_signal WHERE name='hold'")
        conn.commit()
    finally:
        conn.close()


def attack_hold_left():
    """挂机还剩多少秒。没有这次挂机返回 None，过期返回 0 或负数。"""
    conn = connect(readonly=True)
    try:
        row = conn.execute(
            "SELECT value FROM atk_signal WHERE name='hold'").fetchone()
    finally:
        conn.close()
    raw = str(row[0] or "").strip() if row else ""
    if not raw:
        return None
    try:
        dt = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return int((dt - datetime.now(timezone.utc)).total_seconds())


def attack_hold_minutes() -> int:
    """攻打进程挂机多久。取还有效的中级、高级里最长的那一档。"""
    conn = connect(readonly=True)
    try:
        rows = conn.execute(
            "SELECT IFNULL(hold_min,0), IFNULL(tier,'初级'), IFNULL(expires_at,'') "
            "FROM app_user").fetchall()
    finally:
        conn.close()
    best = 0
    for minutes, tier, expires_at in rows:
        if not attack_tier(tier) or account_expired(expires_at):
            continue
        if int(minutes) > best:
            best = int(minutes)
    return best


def set_auto_lock(user_id: int, on: bool) -> None:
    conn = connect()
    try:
        conn.execute(
            "UPDATE app_user SET auto_lock=? WHERE id=?",
            (1 if on else 0, int(user_id)))
        conn.commit()
    finally:
        conn.close()


def enqueue_online_attack(user_id: int, city_id: int, uid: str) -> bool:
    """订阅的人刚上线，排一条自动攻打。同一人还没打完就不再排。"""
    uid = str(uid or "").strip()
    if not uid:
        return False
    conn = connect()
    try:
        row = conn.execute(
            "SELECT 1 FROM atk_order WHERE user_id=? AND city_id=? AND uid=? "
            "AND status IN ('pending','running')",
            (int(user_id), int(city_id), uid)).fetchone()
        if row:
            return False
        saved = conn.execute(
            "SELECT IFNULL(card_max,100) FROM app_user WHERE id=?",
            (int(user_id),)).fetchone()
        cards = int(saved[0]) if saved else 100
        now = now_ts()
        conn.execute(
            "INSERT INTO atk_order(user_id, city_id, uid, status, reason, auto, card_max, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (int(user_id), int(city_id), uid, "pending", "", 1, cards, now, now))
        conn.commit()
        return True
    finally:
        conn.close()


def list_attack_orders(user_id: int, limit: int = 20) -> list:
    conn = connect(readonly=True)
    try:
        rows = conn.execute(
            "SELECT id, city_id, IFNULL(uid,''), status, IFNULL(reason,''), created_at, beats "
            "FROM atk_order WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (int(user_id), int(limit))).fetchall()
    finally:
        conn.close()
    return [{"id": r[0], "city_id": r[1], "uid": r[2], "status": r[3],
             "reason": r[4], "created_at": beijing_ts(r[5]), "beats": r[6],
             "city_name": city_name(r[1])}
            for r in rows]


def set_page_qr(on: bool) -> None:
    """没配攻打号时，登录二维码显示在网页上。扫完或进程停了就关掉。"""
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO atk_signal(name, value, at) VALUES ('pageqr', ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET value=excluded.value, at=excluded.at",
            ("1" if on else "0", now_ts()))
        conn.commit()
    finally:
        conn.close()


def _proc_payload(raw) -> dict:
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _kept_here(data: dict) -> int:
    here = data.get("here")
    if isinstance(here, bool) or not isinstance(here, int) or here <= 0:
        return 0
    return here


def set_attack_status(phase: str) -> None:
    """攻打进程把自己的阶段写进库。网页只读，不靠推送。所在城市留着。"""
    conn = connect()
    try:
        row = conn.execute(
            "SELECT value FROM atk_signal WHERE name='proc'").fetchone()
        prev = _proc_payload(row[0] if row else "")
        data = {"phase": phase}
        here = _kept_here(prev)
        if here:
            data["here"] = here
        if phase != "offline":
            link = prev.get("link")
            gap = prev.get("gap")
            if isinstance(link, int) and not isinstance(link, bool) and link > 0:
                data["link"] = link
            if isinstance(gap, int) and not isinstance(gap, bool) and gap > 0:
                data["gap"] = gap
        conn.execute(
            "INSERT INTO atk_signal(name, value, at) VALUES ('proc', ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET value=excluded.value, at=excluded.at",
            (json.dumps(data, ensure_ascii=False), now_ts()))
        conn.commit()
    finally:
        conn.close()


def note_attack_here(city_id) -> None:
    """记下攻打号当前所在城市。库写失败不影响正在打的那一单。进程已停则不改。"""
    try:
        cid = int(city_id or 0)
    except (TypeError, ValueError):
        return
    if isinstance(city_id, bool) or cid <= 0:
        return
    try:
        conn = connect()
    except sqlite3.Error:
        return
    try:
        row = conn.execute(
            "SELECT value FROM atk_signal WHERE name='proc'").fetchone()
        if not row:
            return
        data = _proc_payload(row[0])
        if not data or data.get("phase") == "offline":
            return
        data["here"] = cid
        conn.execute(
            "UPDATE atk_signal SET value=?, at=? WHERE name='proc'",
            (json.dumps(data, ensure_ascii=False), now_ts()))
        conn.commit()
    except sqlite3.Error:
        return
    finally:
        conn.close()


def note_attack_link(interval: float) -> None:
    """游戏心跳刚发出去。挂机时用这个判断连接是不是真的还在。"""
    try:
        conn = connect()
    except sqlite3.Error:
        return
    try:
        row = conn.execute(
            "SELECT value FROM atk_signal WHERE name='proc'").fetchone()
        if not row:
            return
        data = _proc_payload(row[0])
        if not data or data.get("phase") == "offline":
            return
        data["link"] = int(time.time())
        try:
            gap = int(float(interval))
        except (TypeError, ValueError):
            gap = 0
        if gap > 0:
            data["gap"] = gap
        conn.execute(
            "UPDATE atk_signal SET value=? WHERE name='proc'",
            (json.dumps(data, ensure_ascii=False),))
        conn.commit()
    except sqlite3.Error:
        return
    finally:
        conn.close()


def _hold_link_ok(data: dict) -> bool:
    """游戏心跳还新鲜，说明这条连接真的保活着。"""
    link = data.get("link")
    if isinstance(link, bool) or not isinstance(link, int) or link <= 0:
        return False
    gap = data.get("gap")
    if isinstance(gap, bool) or not isinstance(gap, int) or gap <= 0:
        gap = 30
    age = time.time() - link
    return age <= max(40, gap * 2 + 10)


def touch_attack_status() -> None:
    """进程还活着就刷新时间。停掉之后不再把「没在跑」刷成在线。"""
    conn = connect()
    try:
        row = conn.execute(
            "SELECT value FROM atk_signal WHERE name='proc'").fetchone()
        if not row:
            return
        try:
            phase = json.loads(row[0] or "{}").get("phase")
        except json.JSONDecodeError:
            return
        if phase == "offline":
            return
        conn.execute(
            "UPDATE atk_signal SET at=? WHERE name='proc'", (now_ts(),))
        conn.commit()
    finally:
        conn.close()


def attack_status(user_id: int) -> dict:
    """给页面看的攻打进程。超过 25 秒没心跳就当没在跑。别人的城市和 UID 不带出来。"""
    conn = connect(readonly=True)
    try:
        row = conn.execute(
            "SELECT value, at FROM atk_signal WHERE name='proc'").fetchone()
        page_qr = conn.execute(
            "SELECT value FROM atk_signal WHERE name='pageqr'").fetchone()
        own = conn.execute(
            "SELECT city_id, IFNULL(uid,'') FROM atk_order "
            "WHERE user_id=? AND status='running' ORDER BY id DESC LIMIT 1",
            (int(user_id),)).fetchone()
        latest = conn.execute(
            "SELECT status, IFNULL(reason,'') FROM atk_order "
            "WHERE user_id=? ORDER BY id DESC LIMIT 1",
            (int(user_id),)).fetchone()
        paused_row = conn.execute(
            "SELECT value FROM atk_signal WHERE name='pause'").fetchone()
        hold_row = conn.execute(
            "SELECT value FROM atk_signal WHERE name='hold'").fetchone()
    finally:
        conn.close()
    paused = bool(paused_row and paused_row[0] == "1")
    hold_left = None
    raw_hold = str(hold_row[0] or "").strip() if hold_row else ""
    if raw_hold:
        try:
            dt = datetime.strptime(raw_hold, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc)
            hold_left = int((dt - datetime.now(timezone.utc)).total_seconds())
        except ValueError:
            hold_left = None
    phase = "offline"
    seen = ""
    online = False
    here_id = 0
    if row:
        parsed = _proc_payload(row[0])
        phase = parsed.get("phase") or "offline"
        here_id = _kept_here(parsed)
        seen = row[1] or ""
        if seen and phase != "offline":
            try:
                dt = datetime.strptime(seen, "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=timezone.utc)
                online = (datetime.now(timezone.utc) - dt).total_seconds() <= 25
            except ValueError:
                online = False
    show_qr = bool(page_qr and page_qr[0] == "1" and online and phase == "login")
    if not online:
        # 进程已经停了。暂停只对还在跑的进程有意义，留下的标记会让下次打开页面一直显示已暂停。
        if paused:
            set_attack_paused(False)
        return {"online": False, "phase": "offline",
                "detail": "没在跑",
                "seen_at": beijing_ts(seen), "qr": False, "here": "",
                "paused": False, "hold_left": None}
    if paused:
        detail = "已暂停"
    elif phase == "login":
        detail = "正在等扫码"
    elif phase == "hold":
        head = "挂机保活成功" if _hold_link_ok(parsed) else "挂机保活没连上"
        if hold_left is not None and hold_left > 0:
            detail = f"{head}，还剩 {(hold_left + 59) // 60} 分钟"
        else:
            detail = head
    elif phase == "running" and own and str(own[1] or "").strip():
        detail = f"正在打城市 {own[0]} 的 {own[1]}"
    elif phase == "running" and own:
        detail = f"正在清城市 {own[0]}"
    elif phase == "running":
        detail = "正在执行订单"
    else:
        detail = "空闲，等订单"
        if latest and latest[0] == "failed" and latest[1]:
            detail = f"空闲。上一单没打成：{latest[1]}"
    here = ""
    if here_id:
        name = city_name(here_id)
        here = f"{here_id} {name}".strip() if name else str(here_id)
    show_hold = (not paused and phase == "hold"
                 and hold_left is not None and hold_left > 0)
    return {"online": True, "phase": phase, "detail": detail,
            "seen_at": beijing_ts(seen), "qr": show_qr, "here": here,
            "paused": paused,
            "hold_left": int(hold_left) if show_hold else None}


def _clock(text) -> str:
    raw = str(text or "").strip()
    if not raw:
        return ""
    parts = raw.split(":")
    if len(parts) not in (2, 3) or any(not p.isdigit() for p in parts[:2]):
        raise ValueError("时间写成 01:00 这样")
    hour, minute = int(parts[0]), int(parts[1])
    if hour > 23 or minute > 59:
        raise ValueError("时间写成 01:00 这样")
    return f"{hour:02d}:{minute:02d}"


def scan_quiet(start: str, end: str, now=None) -> bool:
    """北京时间落在停扫时段里。开始等于结束、或有一边空着，就不停。跨过零点也算。"""
    if not start or not end or start == end:
        return False
    a = int(start[:2]) * 60 + int(start[3:5])
    b = int(end[:2]) * 60 + int(end[3:5])
    now = now or datetime.now(timezone(timedelta(hours=8)))
    cur = now.hour * 60 + now.minute
    if a < b:
        return a <= cur < b
    return cur >= a or cur < b


def get_scan_plan():
    """管理员在页面上保存过的扫描安排。还没保存过返回 None，调用方继续用 config。"""
    conn = connect(readonly=True)
    try:
        row = conn.execute(
            "SELECT gap_sec, IFNULL(quiet_start,''), IFNULL(quiet_end,''), "
            "IFNULL(ranges_json,'[]') FROM scan_plan WHERE id=1").fetchone()
    finally:
        conn.close()
    if not row:
        return None
    try:
        raw = json.loads(row[3] or "[]")
    except json.JSONDecodeError:
        raw = []
    ranges, jobs = [], []
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            try:
                city = int(item.get("city_id") or 0)
                start_page = int(item.get("start_page") or 0)
                end_page = int(item.get("end_page"))
            except (TypeError, ValueError):
                continue
            if city <= 0 or start_page < 0 or end_page < start_page:
                continue
            ranges.append({"city_id": city, "start_page": start_page, "end_page": end_page})
            jobs.append((city, start_page, end_page))
    start, end = row[1] or "", row[2] or ""
    return {
        "gap_sec": int(row[0] or 300),
        "quiet_start": start,
        "quiet_end": end,
        "quiet": scan_quiet(start, end),
        "ranges": ranges,
        "jobs": jobs,
    }


def save_scan_plan(gap_sec, quiet_start, quiet_end, ranges) -> str:
    """保存扫描安排。返回空字符串表示成功，否则是给页面看的原因。"""
    try:
        gap = int(str(gap_sec).strip())
    except (TypeError, ValueError, AttributeError):
        return "间隔要是数字"
    if gap < 30 or gap > 86400:
        return "间隔要在 30 秒到 24 小时之间"
    try:
        start = _clock(quiet_start)
        end = _clock(quiet_end)
    except ValueError as exc:
        return str(exc)
    if bool(start) != bool(end):
        return "停扫时段要开始和结束都填，或者都留空"
    if start and start == end:
        return "停扫的开始和结束不能是同一分钟"
    if not isinstance(ranges, list):
        return "城市页范围要是列表"
    if len(ranges) > 100:
        return "一座进程最多 100 座城"
    clean, seen = [], set()
    for item in ranges:
        if not isinstance(item, dict):
            return "城市页范围有一条不是对象"
        try:
            city = int(str(item.get("city_id", "")).strip())
            start_page = int(str(item.get("start_page", "")).strip())
            end_page = int(str(item.get("end_page", "")).strip())
        except (TypeError, ValueError, AttributeError):
            return "城市 ID 和页码要是数字"
        if city <= 0:
            return "城市 ID 要大于 0"
        if start_page < 0 or end_page < start_page or end_page > 100000:
            return f"城市 {city} 的页码无效"
        if city in seen:
            return f"城市 {city} 写了两段，一座城只保留一段"
        seen.add(city)
        clean.append({"city_id": city, "start_page": start_page, "end_page": end_page})
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO scan_plan(id, gap_sec, quiet_start, quiet_end, ranges_json, updated_at) "
            "VALUES (1,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET gap_sec=excluded.gap_sec, "
            "quiet_start=excluded.quiet_start, quiet_end=excluded.quiet_end, "
            "ranges_json=excluded.ranges_json, updated_at=excluded.updated_at",
            (gap, start, end, json.dumps(clean, ensure_ascii=False), now_ts()))
        conn.commit()
    finally:
        conn.close()
    return ""


def ask_attack_login() -> None:
    """网页发起登录，但攻打进程正占着这个号。让那个进程去推二维码。"""
    conn = connect()
    try:
        now = now_ts()
        conn.execute(
            "INSERT INTO atk_signal(name, value, at) VALUES ('login','1',?) "
            "ON CONFLICT(name) DO UPDATE SET value='1', at=excluded.at",
            (now,))
        conn.commit()
    finally:
        conn.close()


def take_attack_login() -> bool:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT value FROM atk_signal WHERE name='login'").fetchone()
        if not row or row[0] != "1":
            return False
        conn.execute(
            "UPDATE atk_signal SET value='0', at=? WHERE name='login'",
            (now_ts(),))
        conn.commit()
        return True
    finally:
        conn.close()


def attack_paused() -> bool:
    conn = connect(readonly=True)
    try:
        row = conn.execute(
            "SELECT value FROM atk_signal WHERE name='pause'").fetchone()
        return bool(row and row[0] == "1")
    finally:
        conn.close()


def set_attack_paused(on: bool) -> None:
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO atk_signal(name, value, at) VALUES ('pause', ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET value=excluded.value, at=excluded.at",
            ("1" if on else "0", now_ts()))
        conn.commit()
    finally:
        conn.close()


def attack_order_open() -> bool:
    """还有没打完的订单。网页据此在攻打进程没心跳时把它拉起来。"""
    conn = connect(readonly=True)
    try:
        row = conn.execute(
            "SELECT 1 FROM atk_order WHERE status IN ('pending','running') LIMIT 1"
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def requeue_running_orders() -> None:
    """拿到攻打号之后调用。标着正在打的是上一轮进程留下的，改回排队。"""
    conn = connect()
    try:
        conn.execute(
            "UPDATE atk_order SET status='pending', updated_at=? WHERE status='running'",
            (now_ts(),))
        conn.commit()
    finally:
        conn.close()


def claim_attack_order():
    """领最新的一条排队订单。后提交的优先。档位不够的记为失败并接着看下一条。"""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        now = now_ts()
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=20)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        conn.execute(
            "UPDATE atk_order SET status='pending', updated_at=? "
            "WHERE status='running' AND updated_at<?",
            (now, cutoff))
        while True:
            row = conn.execute(
                "SELECT o.id, o.city_id, IFNULL(o.uid,''), IFNULL(u.tier,'初级'), "
                "IFNULL(u.expires_at,''), IFNULL(o.auto,0), o.card_max "
                "FROM atk_order o JOIN app_user u ON u.id=o.user_id "
                "WHERE o.status='pending' ORDER BY o.id DESC LIMIT 1").fetchone()
            if not row:
                conn.commit()
                return None
            if not attack_tier(row[3]) or account_expired(row[4]):
                conn.execute(
                    "UPDATE atk_order SET status='failed', reason=?, updated_at=? WHERE id=?",
                    ("订阅档不够或账号已过期", now, row[0]))
                continue
            cur = conn.execute(
                "UPDATE atk_order SET status='running', updated_at=? "
                "WHERE id=? AND status='pending'",
                (now, row[0]))
            conn.commit()
            if cur.rowcount != 1:
                return None
            return {"id": row[0], "city_id": row[1], "uid": row[2], "auto": bool(row[5]),
                    "cards": None if row[6] is None else int(row[6])}
    finally:
        conn.close()


def note_attack_beats(order_id: int, n: int) -> None:
    """正在打的订单记下已经击退几个人。写库失败不影响继续打。"""
    try:
        conn = connect()
    except sqlite3.Error:
        return
    try:
        conn.execute(
            "UPDATE atk_order SET beats=? WHERE id=? AND status='running'",
            (int(n), int(order_id)))
        conn.commit()
    except sqlite3.Error:
        return
    finally:
        conn.close()


def defer_attack_order(order_id: int) -> None:
    """暂停时把正在打的单放回排队。继续后会再领。"""
    conn = connect()
    try:
        conn.execute(
            "UPDATE atk_order SET status='pending', reason='', beats=NULL, updated_at=? "
            "WHERE id=? AND status='running'",
            (now_ts(), int(order_id)))
        conn.commit()
    finally:
        conn.close()


def finish_attack_order(order_id: int, status: str, reason: str = "", beats=None) -> None:
    conn = connect()
    try:
        if beats is None:
            conn.execute(
                "UPDATE atk_order SET status=?, reason=?, updated_at=? "
                "WHERE id=? AND status='running'",
                (status, reason or "", now_ts(), int(order_id)))
        else:
            conn.execute(
                "UPDATE atk_order SET status=?, reason=?, beats=?, updated_at=? "
                "WHERE id=? AND status='running'",
                (status, reason or "", int(beats), now_ts(), int(order_id)))
        conn.commit()
    finally:
        conn.close()


def add_watch(user_id: int, city_id: int, uid: str) -> None:
    """记下这个账号要盯的「这座城里有没有这个人」。已有的不改上次状态。"""
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO watch_sub(user_id, city_id, uid, created_at) VALUES (?,?,?,?) "
            "ON CONFLICT(user_id, city_id, uid) DO NOTHING",
            (int(user_id), int(city_id), str(uid).strip(), now_ts()))
        conn.commit()
    finally:
        conn.close()


def remove_watch(user_id: int, city_id: int, uid: str) -> None:
    conn = connect()
    try:
        conn.execute(
            "DELETE FROM watch_sub WHERE user_id=? AND city_id=? AND uid=?",
            (int(user_id), int(city_id), str(uid).strip()))
        conn.commit()
    finally:
        conn.close()


def watch_city_ids() -> list:
    """网页里订阅过的城市。读失败时给空列表，不拖住游戏连接。"""
    try:
        conn = connect(readonly=True, timeout=1)
    except sqlite3.OperationalError:
        return []
    try:
        return [int(r[0]) for r in conn.execute(
            "SELECT DISTINCT city_id FROM watch_sub ORDER BY city_id")]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def list_watches(user_id: int) -> list:
    """这个账号的订阅现在在不在。在不在看上一轮扫描记下来的状态。"""
    conn = connect(readonly=True)
    try:
        rows = conn.execute(
            "SELECT s.city_id, s.uid, s.created_at, s.last_present, IFNULL(c.name, ''), "
            "p.name, p.lvl, p.fetched_at, p.page, "
            "(SELECT MAX(fetched_at) FROM player WHERE city_id=s.city_id), "
            "o.fetched_at "
            "FROM watch_sub s "
            "LEFT JOIN city c ON c.id=s.city_id "
            "LEFT JOIN player p ON p.city_id=s.city_id AND p.uid=s.uid "
            "LEFT JOIN city_occupy o ON o.city_id=s.city_id "
            "WHERE s.user_id=? "
            "ORDER BY s.created_at DESC, s.rowid DESC",
            (int(user_id),)).fetchall()
    finally:
        conn.close()
    out = []
    for city_id, uid, created, last, cname, pname, lvl, seen, page, city_seen, occ_seen in rows:
        scanned = [t for t in (city_seen, occ_seen) if t]
        checked = last is not None
        out.append({
            "city_id": int(city_id),
            "city_name": cname,
            "uid": uid,
            "name": pname or "",
            "lvl": lvl,
            "present": checked and int(last) == 1,
            "checked": checked,
            "seen_at": beijing_ts(seen or ""),
            "city_scanned_at": beijing_ts(max(scanned) if scanned else ""),
            "page": None if page is None else int(page) + 1,
            "created_at": created,
        })
    return out


def sync_watch(city_id: int, seen_uids, finished: bool) -> list:
    """用这一轮拉到的人更新订阅状态。只返回要推送的「变成在线」。

    这一轮正常扫完后，没见到的订阅 UID 记成不在线。扫描中断时不改这些 UID。
    第一次出现，以及从不在线变成在线，各推一条。离开只改状态，不推。
    """
    city_id = int(city_id)
    seen = {str(u).strip() for u in seen_uids if str(u).strip()}
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT s.user_id, s.uid, s.last_present, IFNULL(u.qq_target,''), "
            "IFNULL(u.expires_at,''), IFNULL(u.tier,'初级'), IFNULL(u.auto_lock,0) "
            "FROM watch_sub s JOIN app_user u ON u.id=s.user_id "
            "WHERE s.city_id=?",
            (city_id,)).fetchall()
        if not rows:
            return []
        cname = conn.execute("SELECT name FROM city WHERE id=?",
                             (city_id,)).fetchone()
        cname = cname[0] if cname else ""
        changes = []
        for user_id, uid, last, qq_target, expires_at, tier, auto_lock in rows:
            if uid in seen:
                now = 1
            elif finished:
                now = 0
            else:
                continue
            name, page = "", None
            nrow = conn.execute(
                "SELECT name, page FROM player WHERE city_id=? AND uid=?",
                (city_id, uid)).fetchone()
            if nrow:
                name = nrow[0] or ""
                if nrow[1] is not None:
                    page = int(nrow[1]) + 1
            if last is None or int(last) != now:
                conn.execute(
                    "UPDATE watch_sub SET last_present=? "
                    "WHERE user_id=? AND city_id=? AND uid=?",
                    (now, user_id, city_id, uid))
            if (now == 1 and (last is None or int(last) != 1)
                    and not account_expired(expires_at)):
                changes.append({
                    "user_id": user_id, "city_id": city_id, "city_name": cname,
                    "uid": uid, "name": name, "page": page, "present": now == 1,
                    "qq_target": qq_target, "tier": tier or "初级",
                    "auto_lock": bool(auto_lock),
                })
        conn.commit()
        return changes
    finally:
        conn.close()


def list_city_targets(city_id: int, skip_failed=True, exclude_uid=""):
    """这座城里可打的人。skip_failed 时排除 atk_fail 里记过失败的。"""
    city_id = int(city_id)
    skip = set()
    if skip_failed:
        skip |= failed_uids()
    me = str(exclude_uid or "").strip()
    if me:
        skip.add(me)
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT p.uid, p.name, p.city_id, "
            "IFNULL(o.occupy_country, IFNULL(c.country_id, p.country_id)) "
            "FROM player p LEFT JOIN city c ON c.id=p.city_id "
            "LEFT JOIN city_occupy o ON o.city_id=p.city_id "
            "WHERE p.city_id=? ORDER BY p.fetched_at DESC",
            (city_id,)).fetchall()
    finally:
        conn.close()
    out = []
    seen = set()
    for uid, name, cid, owner in rows:
        uid = str(uid or "").strip()
        if not uid or uid in seen or uid in skip:
            continue
        seen.add(uid)
        out.append({"uid": uid, "name": name or "", "city_id": cid,
                    "city_country": int(owner or 0)})
    return out


def failed_uids() -> set:
    return set(failed_names())


def failed_names_in(city_id) -> list:
    """这座城里战败表记下的名字。含还没落盘的队列。没有名字时用 uid。"""
    try:
        city_id = int(city_id or 0)
    except (TypeError, ValueError):
        return []
    if city_id <= 0:
        return []

    def _read():
        conn = connect(timeout=DB_OP_TIMEOUT)
        try:
            rows = conn.execute(
                "SELECT uid, IFNULL(name,'') FROM atk_fail "
                "WHERE city_id=? AND IFNULL(ret,0) NOT IN (21)",
                (city_id,)).fetchall()
            return [(str(u).strip(), str(n or "").strip()) for u, n in rows if u]
        finally:
            conn.close()

    rows = _run_timeout(_read, default=[])
    names = {}
    if isinstance(rows, list):
        for uid, name in rows:
            if uid:
                names[uid] = name
    for op in _atk_q:
        if op[0] == "wipe":
            names.clear()
        elif op[0] == "clear":
            names.pop(str(op[1]).strip(), None)
        elif op[0] == "record":
            uid = str(op[1]).strip()
            if not uid:
                continue
            if op[4] in (21,):
                names.pop(uid, None)
                continue
            if int(op[3] or 0) == city_id:
                names[uid] = str(op[2] or "").strip()
            else:
                names.pop(uid, None)
    out = []
    for uid, name in names.items():
        text = name or uid
        if text and text not in out:
            out.append(text)
    return out


def failed_names() -> dict:
    """战败表里的 uid → 当时记下的名字。ret=21 不算打不过。含还没落盘的队列。"""
    def _read():
        conn = connect(timeout=DB_OP_TIMEOUT)
        try:
            rows = conn.execute(
                "SELECT uid, IFNULL(name,'') FROM atk_fail "
                "WHERE IFNULL(ret,0) NOT IN (21)").fetchall()
            return {str(u).strip(): str(n or "").strip() for u, n in rows if u}
        finally:
            conn.close()
    got = _run_timeout(_read, default={})
    names = dict(got) if isinstance(got, dict) else {}
    for op in _atk_q:
        if op[0] == "wipe":
            names.clear()
        elif op[0] == "clear":
            names.pop(str(op[1]).strip(), None)
        elif op[0] == "record":
            names[str(op[1]).strip()] = str(op[2] or "").strip()
    return names


def same_failed(uid, live_name, names=None) -> bool:
    """这个现场的人是不是战败表里的那一个。

    uid 对上但名字对不上，多半是编号截断撞了别人，不能当成挡路。
    """
    uid = str(uid or "").strip()
    if not uid:
        return False
    if names is None:
        names = failed_names()
    if uid not in names:
        return False
    recorded = str(names.get(uid) or "").strip()
    live = str(live_name or "").strip()
    if live and recorded != live:
        return False
    return True


def in_atk_fail(uid) -> bool:
    """这个人现在算不算失败库里的。先看本轮还没落盘的队列，再查库。"""
    uid = str(uid or "").strip()
    if not uid:
        return False
    pending = None
    for op in _atk_q:
        if op[0] == "wipe":
            pending = False
        elif op[0] == "clear" and op[1] == uid:
            pending = False
        elif op[0] == "record" and op[1] == uid:
            pending = True
    if pending is True:
        return True
    if pending is False:
        return False

    def _read():
        conn = connect(readonly=True, timeout=DB_OP_TIMEOUT)
        try:
            row = conn.execute(
                "SELECT 1 FROM atk_fail WHERE uid=? AND IFNULL(ret,0) NOT IN (21)",
                (uid,)).fetchone()
            return bool(row)
        finally:
            conn.close()

    return bool(_run_timeout(_read, default=False))


# 打人热路径不能同步写库：L 盘/DB Browser 一锁，sqlite3.connect 能卡住几分钟，
# 心跳发不出去，服务端直接掐连接。先记队列，断开前再 flush。
_atk_q = []


def record_atk_fail(uid, city_id=0, ret=None, reason="", name=""):
    uid = str(uid or "").strip()
    if uid:
        _atk_q.append(("record", uid, name or "", int(city_id or 0), ret, reason or ""))


def clear_atk_fail(uid=None):
    if uid is None:
        _atk_q.append(("wipe",))
        return
    uid = str(uid or "").strip()
    if uid:
        _atk_q.append(("clear", uid))


def flush_atk_fail():
    """战斗结束再写盘。超过 DB_OP_TIMEOUT 就放弃，不堵下一轮。"""
    global _atk_q
    if not _atk_q:
        return
    batch, _atk_q = _atk_q, []

    def _write():
        conn = connect(timeout=DB_OP_TIMEOUT)
        try:
            for op in batch:
                if op[0] == "wipe":
                    conn.execute("DELETE FROM atk_fail")
                elif op[0] == "clear":
                    conn.execute("DELETE FROM atk_fail WHERE uid=?", (op[1],))
                else:
                    _, uid, name, city_id, ret, reason = op
                    conn.execute(
                        "INSERT INTO atk_fail(uid, name, city_id, ret, reason, at) "
                        "VALUES (?,?,?,?,?,?) "
                        "ON CONFLICT(uid) DO UPDATE SET "
                        "name=excluded.name, city_id=excluded.city_id, "
                        "ret=excluded.ret, reason=excluded.reason, at=excluded.at",
                        (uid, name, city_id, ret, reason, now_ts()))
            conn.commit()
        finally:
            conn.close()

    _run_timeout(_write)


def find_player(uid: str):
    """按 baseid/uid 取最近一条驻地记录。没有返回 None。"""
    uid = str(uid or "").strip()
    if not uid:
        return None

    def _read():
        conn = connect(readonly=True, timeout=DB_OP_TIMEOUT)
        try:
            row = conn.execute(
                "SELECT p.uid, p.city_id, p.name, p.lvl, p.country_id, p.morale, "
                "IFNULL(o.occupy_country, c.country_id), c.name, IFNULL(p.page,0) "
                "FROM player p LEFT JOIN city c ON c.id=p.city_id "
                "LEFT JOIN city_occupy o ON o.city_id=p.city_id "
                "WHERE p.uid=? ORDER BY p.fetched_at DESC LIMIT 1",
                (uid,)).fetchone()
            if not row:
                return None
            return {"uid": row[0], "city_id": row[1], "name": row[2], "lvl": row[3],
                    "country_id": row[4], "morale": row[5],
                    "city_country": row[6], "city_name": row[7], "page": row[8]}
        finally:
            conn.close()

    got = _run_timeout(_read)
    return got if isinstance(got, dict) else None
