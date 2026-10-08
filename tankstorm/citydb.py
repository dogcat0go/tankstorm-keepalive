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
    acct     TEXT NOT NULL DEFAULT '',
    uid      TEXT NOT NULL,
    name     TEXT,
    city_id  INTEGER,
    ret      INTEGER,
    reason   TEXT,
    at       TEXT NOT NULL,
    PRIMARY KEY (acct, uid)
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
    hold_all        INTEGER NOT NULL DEFAULT 0,
    card_max        INTEGER NOT NULL DEFAULT 100,
    retreat_mode    TEXT NOT NULL DEFAULT 'hops',
    retreat_hops    INTEGER NOT NULL DEFAULT 3,
    retreat_city    INTEGER NOT NULL DEFAULT 0,
    retreat_fail    INTEGER NOT NULL DEFAULT 0,
    lock_cards      INTEGER NOT NULL DEFAULT 3,
    clear_mode      TEXT NOT NULL DEFAULT 'head',
    clear_from      INTEGER NOT NULL DEFAULT 1,
    clear_to        INTEGER NOT NULL DEFAULT 5,
    clear_wait      INTEGER NOT NULL DEFAULT 0,
    clear_scan      INTEGER NOT NULL DEFAULT 0,
    modo_cards      INTEGER NOT NULL DEFAULT 0,
    daily_switch    TEXT,
    daily_at        TEXT NOT NULL DEFAULT '',
    daily_last      TEXT NOT NULL DEFAULT '',
    off_at          TEXT NOT NULL DEFAULT '',
    region          INTEGER NOT NULL DEFAULT 0,
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
    run_at      TEXT,
    kind        TEXT NOT NULL DEFAULT '',
    name        TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS daily_job (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER NOT NULL,
    kind        TEXT NOT NULL,
    params      TEXT NOT NULL DEFAULT '{}',
    status      TEXT NOT NULL,
    detail      TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS atk_signal (
    name  TEXT PRIMARY KEY,
    value TEXT,
    at    TEXT
);
CREATE TABLE IF NOT EXISTS lock_card_use (
    id       INTEGER PRIMARY KEY,
    user_id  INTEGER NOT NULL,
    at       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS storm_reject (
    id       INTEGER PRIMARY KEY,
    user_id  INTEGER NOT NULL,
    name     TEXT NOT NULL,
    at       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS clear_prio (
    user_id  INTEGER NOT NULL,
    uid      TEXT NOT NULL,
    rank     INTEGER NOT NULL,
    seq      INTEGER NOT NULL,
    PRIMARY KEY (user_id, uid)
);
CREATE TABLE IF NOT EXISTS app_session (
    token      TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS app_clock (
    user_id    INTEGER PRIMARY KEY,
    delta_ms   INTEGER NOT NULL,
    mono_ms    INTEGER NOT NULL,
    seen_ms    INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS watch_sub (
    user_id      INTEGER NOT NULL,
    city_id      INTEGER NOT NULL,
    uid          TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    last_present INTEGER,
    lock_sent    INTEGER NOT NULL DEFAULT 0,
    name         TEXT NOT NULL DEFAULT '',
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
                wcols = {r[1] for r in setup.execute("PRAGMA table_info(watch_sub)")}
            if wcols and "lock_sent" not in wcols:
                setup.execute(
                    "ALTER TABLE watch_sub ADD COLUMN lock_sent INTEGER NOT NULL DEFAULT 0")
                setup.commit()
            if wcols and "name" not in wcols:
                setup.execute(
                    "ALTER TABLE watch_sub ADD COLUMN name TEXT NOT NULL DEFAULT ''")
                setup.execute(
                    "UPDATE watch_sub SET name=("
                    " SELECT p.name FROM player p"
                    " WHERE p.uid=watch_sub.uid AND TRIM(IFNULL(p.name,''))!=''"
                    " ORDER BY p.fetched_at DESC LIMIT 1"
                    ") WHERE TRIM(IFNULL(name,''))='' AND EXISTS ("
                    " SELECT 1 FROM player p"
                    " WHERE p.uid=watch_sub.uid AND TRIM(IFNULL(p.name,''))!=''"
                    ")")
                setup.execute(
                    "UPDATE watch_sub SET name=("
                    " SELECT f.name FROM atk_fail f"
                    " WHERE f.uid=watch_sub.uid AND TRIM(IFNULL(f.name,''))!=''"
                    " ORDER BY f.at DESC LIMIT 1"
                    ") WHERE TRIM(IFNULL(name,''))='' AND EXISTS ("
                    " SELECT 1 FROM atk_fail f"
                    " WHERE f.uid=watch_sub.uid AND TRIM(IFNULL(f.name,''))!=''"
                    ")")
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
            if ucols and "hold_all" not in ucols:
                setup.execute(
                    "ALTER TABLE app_user ADD COLUMN hold_all INTEGER NOT NULL DEFAULT 0")
                setup.commit()
            if ucols and "card_max" not in ucols:
                setup.execute(
                    "ALTER TABLE app_user ADD COLUMN card_max INTEGER NOT NULL DEFAULT 100")
                setup.commit()
            if ucols and "retreat_mode" not in ucols:
                setup.execute(
                    "ALTER TABLE app_user ADD COLUMN retreat_mode TEXT NOT NULL DEFAULT 'hops'")
                setup.commit()
            if ucols and "retreat_hops" not in ucols:
                setup.execute(
                    "ALTER TABLE app_user ADD COLUMN retreat_hops INTEGER NOT NULL DEFAULT 3")
                setup.commit()
            if ucols and "retreat_city" not in ucols:
                setup.execute(
                    "ALTER TABLE app_user ADD COLUMN retreat_city INTEGER NOT NULL DEFAULT 0")
                setup.commit()
            if ucols and "retreat_fail" not in ucols:
                setup.execute(
                    "ALTER TABLE app_user ADD COLUMN retreat_fail INTEGER NOT NULL DEFAULT 0")
                setup.commit()
            if ucols and "lock_cards" not in ucols:
                setup.execute(
                    "ALTER TABLE app_user ADD COLUMN lock_cards INTEGER NOT NULL DEFAULT 3")
                setup.commit()
            if ucols and "clear_mode" not in ucols:
                setup.execute(
                    "ALTER TABLE app_user ADD COLUMN clear_mode TEXT NOT NULL DEFAULT 'head'")
                setup.commit()
            if ucols and "clear_from" not in ucols:
                setup.execute(
                    "ALTER TABLE app_user ADD COLUMN clear_from INTEGER NOT NULL DEFAULT 1")
                setup.commit()
            if ucols and "clear_to" not in ucols:
                setup.execute(
                    "ALTER TABLE app_user ADD COLUMN clear_to INTEGER NOT NULL DEFAULT 5")
                setup.commit()
            if ucols and "clear_wait" not in ucols:
                setup.execute(
                    "ALTER TABLE app_user ADD COLUMN clear_wait INTEGER NOT NULL DEFAULT 0")
                setup.commit()
            if ucols and "clear_scan" not in ucols:
                setup.execute(
                    "ALTER TABLE app_user ADD COLUMN clear_scan INTEGER NOT NULL DEFAULT 0")
                setup.commit()
            if ucols and "modo_cards" not in ucols:
                setup.execute(
                    "ALTER TABLE app_user ADD COLUMN modo_cards INTEGER NOT NULL DEFAULT 0")
                setup.commit()
            if ucols and "attack_acct" not in ucols:
                setup.execute("ALTER TABLE app_user ADD COLUMN attack_acct TEXT")
                setup.commit()
            if ucols and "attack_qq" not in ucols:
                setup.execute("ALTER TABLE app_user ADD COLUMN attack_qq TEXT")
                setup.commit()
            if ucols and "attack_qq_block" not in ucols:
                setup.execute(
                    "ALTER TABLE app_user ADD COLUMN attack_qq_block INTEGER NOT NULL DEFAULT 0")
                setup.commit()
            if ucols and "daily_switch" not in ucols:
                setup.execute("ALTER TABLE app_user ADD COLUMN daily_switch TEXT")
                setup.commit()
            if ucols and "daily_at" not in ucols:
                setup.execute(
                    "ALTER TABLE app_user ADD COLUMN daily_at TEXT NOT NULL DEFAULT ''")
                setup.commit()
            if ucols and "daily_last" not in ucols:
                setup.execute(
                    "ALTER TABLE app_user ADD COLUMN daily_last TEXT NOT NULL DEFAULT ''")
                setup.commit()
            if ucols and "off_at" not in ucols:
                setup.execute(
                    "ALTER TABLE app_user ADD COLUMN off_at TEXT NOT NULL DEFAULT ''")
                setup.commit()
            if ucols and "region" not in ucols:
                setup.execute(
                    "ALTER TABLE app_user ADD COLUMN region INTEGER NOT NULL DEFAULT 0")
                setup.commit()
            _fix_region_face(setup)
            _forget_page_names(setup)
            _ensure_attack_qq_map(setup)
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
            if ocols and "run_at" not in ocols:
                setup.execute("ALTER TABLE atk_order ADD COLUMN run_at TEXT")
                setup.commit()
            if ocols and "kind" not in ocols:
                setup.execute(
                    "ALTER TABLE atk_order ADD COLUMN kind TEXT NOT NULL DEFAULT ''")
                setup.commit()
            if ocols and "name" not in ocols:
                setup.execute(
                    "ALTER TABLE atk_order ADD COLUMN name TEXT NOT NULL DEFAULT ''")
                setup.execute(
                    "UPDATE atk_order SET name=("
                    " SELECT p.name FROM player p"
                    " WHERE p.uid=atk_order.uid AND p.city_id=atk_order.city_id"
                    " AND TRIM(IFNULL(p.name,''))!='' LIMIT 1"
                    ") WHERE TRIM(IFNULL(name,''))='' AND TRIM(IFNULL(uid,''))!=''"
                    " AND EXISTS ("
                    " SELECT 1 FROM player p WHERE p.uid=atk_order.uid"
                    " AND p.city_id=atk_order.city_id AND TRIM(IFNULL(p.name,''))!=''"
                    ")")
                setup.execute(
                    "UPDATE atk_order SET name=("
                    " SELECT p.name FROM player p"
                    " WHERE p.uid=atk_order.uid AND TRIM(IFNULL(p.name,''))!=''"
                    " ORDER BY p.fetched_at DESC LIMIT 1"
                    ") WHERE TRIM(IFNULL(name,''))='' AND TRIM(IFNULL(uid,''))!=''"
                    " AND EXISTS ("
                    " SELECT 1 FROM player p WHERE p.uid=atk_order.uid"
                    " AND TRIM(IFNULL(p.name,''))!=''"
                    ")")
                setup.execute(
                    "UPDATE atk_order SET name=("
                    " SELECT f.name FROM atk_fail f"
                    " WHERE f.uid=atk_order.uid AND TRIM(IFNULL(f.name,''))!=''"
                    " ORDER BY f.at DESC LIMIT 1"
                    ") WHERE TRIM(IFNULL(name,''))='' AND TRIM(IFNULL(uid,''))!=''"
                    " AND EXISTS ("
                    " SELECT 1 FROM atk_fail f WHERE f.uid=atk_order.uid"
                    " AND TRIM(IFNULL(f.name,''))!=''"
                    ")")
                setup.commit()
            fcols = {r[1] for r in setup.execute("PRAGMA table_info(atk_fail)")}
            if fcols and "acct" not in fcols:
                setup.execute(
                    "CREATE TABLE atk_fail_new ("
                    "acct TEXT NOT NULL DEFAULT '', uid TEXT NOT NULL, name TEXT, "
                    "city_id INTEGER, ret INTEGER, reason TEXT, at TEXT NOT NULL, "
                    "PRIMARY KEY (acct, uid))")
                setup.execute(
                    "INSERT INTO atk_fail_new(acct, uid, name, city_id, ret, reason, at) "
                    "SELECT '', uid, name, city_id, ret, reason, at FROM atk_fail")
                setup.execute("DROP TABLE atk_fail")
                setup.execute("ALTER TABLE atk_fail_new RENAME TO atk_fail")
                setup.commit()
            _assign_legacy_fails(setup)
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


def city_id_named(name: str) -> int:
    """按城名找目录里的 id。同名取最小的那个。没有这座城返回 0。"""
    text = str(name or "").strip()
    if not text:
        return 0
    ensure_catalog()
    conn = connect()
    try:
        row = conn.execute(
            "SELECT id FROM city WHERE name=? ORDER BY id", (text,)).fetchone()
        return int(row[0]) if row else 0
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
    """编号第 2 位是 1 或 2 的城，不是自己国家时不能占领。

    编号以 9 开头的是马奇诺及周边，可以攻打和占领，不受第 2 位限制。
    """
    s = str(int(city_id or 0))
    if s.startswith("9"):
        return False
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
    编号以 9 开头的马奇诺及周边城例外，可以攻打和占领。
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
    """删掉这座城里本次没再出现的人（完整拉完才调用）。名字先留在订阅上。"""
    conn = connect()
    try:
        conn.execute(
            "UPDATE watch_sub SET name=("
            " SELECT p.name FROM player p"
            " WHERE p.uid=watch_sub.uid AND p.city_id=? AND p.fetched_at<?"
            " AND TRIM(IFNULL(p.name,''))!='' LIMIT 1"
            ") WHERE TRIM(IFNULL(name,''))='' AND uid IN ("
            " SELECT uid FROM player WHERE city_id=? AND fetched_at<?"
            " AND TRIM(IFNULL(name,''))!=''"
            ")",
            (int(city_id), fetched_at, int(city_id), fetched_at))
        cur = conn.execute(
            "DELETE FROM player WHERE city_id=? AND fetched_at<?",
            (int(city_id), fetched_at))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def now_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def beijing_now():
    return datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8)))


def beijing_day() -> str:
    return beijing_now().strftime("%Y-%m-%d")


def beijing_clock() -> str:
    return beijing_now().strftime("%H:%M")


TIERS = ("初级", "中级", "高级")


def attack_tier(tier: str) -> bool:
    """中级和高级可以使用远程扫码攻打。"""
    return (tier or "初级") in ("中级", "高级")


def high_tier(tier: str) -> bool:
    """只有高级可以使用自动索敌、自动锁敌和清城高级配置。"""
    return (tier or "初级") == "高级"


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


def expiry_deadline_ms(expires_at: str):
    """有效期截止的 Unix 毫秒。空表示不限期，返回 None。写错的日期返回 0。

    库存的是北京时间日期，这一天仍然有效，到次日 0 点才算过。
    """
    day = (expires_at or "").strip()[:10]
    if not day:
        return None
    try:
        ended = datetime.strptime(day, "%Y-%m-%d") + timedelta(days=1)
    except ValueError:
        return 0
    return int(ended.replace(tzinfo=timezone(timedelta(hours=8))).timestamp() * 1000)


def sync_clock(user_id: int, mono_ms) -> dict:
    """用单调计时和库里的时间差，算出这个账号的会员还剩多少毫秒。

    mono_ms 是本地软件单调递增计时器的读数，单位毫秒。
    第一次把时间差记成当时的服务器时刻减去 mono_ms。
    之后的真实时刻是 mono_ms 加上这份时间差。
    计时比上次小，或停在原地，就改用已经对齐过的时刻和服务器时刻中更晚的一个，
    再按这个时刻重写时间差。
    有效期空着表示不限期，remaining_ms 为 None。
    """
    if isinstance(mono_ms, bool):
        raise ValueError("单调计时要是非负整数毫秒")
    if isinstance(mono_ms, float):
        if not mono_ms.is_integer():
            raise ValueError("单调计时要是非负整数毫秒")
        mono_ms = int(mono_ms)
    elif isinstance(mono_ms, str):
        text = mono_ms.strip()
        if not text.isdigit():
            raise ValueError("单调计时要是非负整数毫秒")
        mono_ms = int(text)
    if not isinstance(mono_ms, int) or mono_ms < 0 or mono_ms > 10**15:
        raise ValueError("单调计时要是非负整数毫秒")
    user_id = int(user_id)
    server_ms = int(time.time() * 1000)
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        user = conn.execute(
            "SELECT IFNULL(expires_at,''), IFNULL(tier,'初级') FROM app_user WHERE id=?",
            (user_id,)).fetchone()
        if not user:
            raise ValueError("账号不存在")
        expires_at, tier = user[0] or "", user[1] or "初级"
        row = conn.execute(
            "SELECT delta_ms, mono_ms, seen_ms FROM app_clock WHERE user_id=?",
            (user_id,)).fetchone()
        if row is None:
            seen = server_ms
            delta = server_ms - mono_ms
        elif mono_ms < int(row[1]):
            seen = max(int(row[2]), server_ms)
            delta = seen - mono_ms
        else:
            seen = max(mono_ms + int(row[0]), server_ms, int(row[2]))
            delta = seen - mono_ms
        conn.execute(
            "INSERT OR REPLACE INTO app_clock(user_id, delta_ms, mono_ms, seen_ms, updated_at) "
            "VALUES (?,?,?,?,?)",
            (user_id, int(delta), int(mono_ms), int(seen), now_ts()))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    deadline = expiry_deadline_ms(expires_at)
    if deadline is None:
        remaining = None
        expired = False
    else:
        remaining = deadline - seen
        if remaining < 0:
            remaining = 0
        expired = remaining == 0
    return {
        "ok": True,
        "tier": tier,
        "expires_at": expires_at,
        "unlimited": deadline is None,
        "expired": expired,
        "remaining_ms": remaining,
        "delta_ms": int(delta),
    }


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


def change_password(user_id: int, current, new, keep_token: str = "") -> str:
    """改登录密码。成功返回空字符串。别的登录态作废，这一次留下。"""
    import secrets
    current = "" if current is None else str(current)
    new = "" if new is None else str(new)
    if len(new) < 6 or len(new) > 72:
        return "密码至少 6 位"
    user_id = int(user_id)
    conn = connect()
    try:
        row = conn.execute(
            "SELECT password_hash FROM app_user WHERE id=?",
            (user_id,)).fetchone()
        if not row or "$" not in str(row[0] or ""):
            return "当前密码不对"
        salt, digest = str(row[0]).split("$", 1)
        if not secrets.compare_digest(_password_hash(current, salt).split("$", 1)[1], digest):
            return "当前密码不对"
        if secrets.compare_digest(_password_hash(new, salt).split("$", 1)[1], digest):
            return "新密码要和当前密码不一样"
        conn.execute(
            "UPDATE app_user SET password_hash=? WHERE id=?",
            (_password_hash(new), user_id))
        keep = str(keep_token or "")
        if keep:
            conn.execute(
                "DELETE FROM app_session WHERE user_id=? AND token!=?",
                (user_id, keep))
        else:
            conn.execute("DELETE FROM app_session WHERE user_id=?", (user_id,))
        conn.commit()
    finally:
        conn.close()
    return ""


def user_by_token(token: str, allow_expired: bool = False):
    if not token:
        return None
    conn = connect(readonly=True)
    try:
        row = conn.execute(
            "SELECT u.id, u.username, IFNULL(u.qq_target,''), IFNULL(u.expires_at,''), "
            "IFNULL(u.tier,'初级'), IFNULL(u.admin,0), IFNULL(u.auto_lock,0), "
            "IFNULL(u.hold_min,0), IFNULL(u.hold_all,0), IFNULL(u.card_max,100), "
            "IFNULL(u.retreat_mode,'hops'), IFNULL(u.retreat_hops,3), IFNULL(u.retreat_city,0), "
            "IFNULL(u.retreat_fail,0), "
            "IFNULL(u.lock_cards,3), IFNULL(u.modo_cards,0), IFNULL(u.region,0), "
            "IFNULL(u.off_at,'') "
            "FROM app_session s JOIN app_user u ON u.id=s.user_id WHERE s.token=?",
            (token,)).fetchone()
        if not row or (account_expired(row[3]) and not allow_expired):
            return None
        return {"id": row[0], "username": row[1], "qq_target": row[2],
                "expires_at": row[3], "tier": row[4], "admin": bool(row[5]),
                "auto_lock": bool(row[6]), "hold_min": int(row[7] or 0),
                "hold_all": bool(row[8]),
                "card_max": int(row[9] if row[9] is not None else 100),
                "retreat_mode": row[10] or "hops",
                "retreat_hops": int(row[11] or 3),
                "retreat_city": int(row[12] or 0),
                "retreat_fail": bool(row[13]),
                "lock_cards": int(row[14] if row[14] is not None else 3),
                "modo_cards": int(row[15] if row[15] is not None else 0),
                "region": int(row[16] or 0),
                "off_at": str(row[17] or "").strip()}
    finally:
        conn.close()


def user_region(user_id: int) -> int:
    conn = connect(readonly=True)
    try:
        row = conn.execute(
            "SELECT IFNULL(region,0) FROM app_user WHERE id=?",
            (int(user_id),)).fetchone()
        return int(row[0] or 0) if row else 0
    finally:
        conn.close()


def _fix_region_face(conn) -> None:
    """以前把链接里的 region 原样当成区号。玩家说的区号要加 1，只改一次。"""
    conn.execute("BEGIN IMMEDIATE")
    row = conn.execute(
        "SELECT 1 FROM atk_signal WHERE name='region_face'").fetchone()
    if row:
        conn.commit()
        return
    conn.execute("UPDATE app_user SET region = region + 1 WHERE region > 0")
    conn.execute(
        "INSERT INTO atk_signal(name, value, at) VALUES ('region_face', '1', ?)",
        (now_ts(),))
    conn.commit()


def note_game_region(user_id: int, region) -> None:
    """攻打号打开游戏页后，把区号记到这个登录账号。

    链接里的 region 从 0 起算，玩家说的区号是它加 1。1 区就是链接里的 0。
    认不出就不改。游戏连接仍用链接里的原值，不读这里。
    """
    user_id = int(user_id or 0)
    text = str(region if region is not None else "").strip()
    if user_id <= 0 or not text:
        return
    try:
        raw = int(text)
    except (TypeError, ValueError):
        return
    if raw < 0:
        return
    face = raw + 1
    conn = connect()
    try:
        cur = conn.execute(
            "UPDATE app_user SET region=? WHERE id=? AND IFNULL(region,0)!=?",
            (face, user_id, face))
        conn.commit()
        changed = cur.rowcount
    finally:
        conn.close()
    if changed:
        log.info("登录账号 %s 的区服是 %s", username_of(user_id), face)


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


def _known_player_name(conn, uid, city_id=0) -> str:
    """这个 UID 现在能查到的名字。先看这座城，再看别的城，最后看失败库。"""
    uid = str(uid or "").strip()
    if not uid:
        return ""
    row = None
    try:
        city_id = int(city_id or 0)
    except (TypeError, ValueError):
        city_id = 0
    if city_id > 0:
        row = conn.execute(
            "SELECT name FROM player WHERE uid=? AND city_id=? "
            "AND TRIM(IFNULL(name,''))!=''",
            (uid, city_id)).fetchone()
    if not row:
        row = conn.execute(
            "SELECT name FROM player WHERE uid=? AND TRIM(IFNULL(name,''))!='' "
            "ORDER BY fetched_at DESC LIMIT 1",
            (uid,)).fetchone()
    if not row:
        row = conn.execute(
            "SELECT name FROM atk_fail WHERE uid=? AND TRIM(IFNULL(name,''))!='' "
            "ORDER BY at DESC LIMIT 1",
            (uid,)).fetchone()
    return str(row[0] or "").strip() if row else ""


def _open_attack_count(conn, user_id: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM atk_order WHERE user_id=? "
        "AND status IN ('pending','running','blocked','wait')",
        (int(user_id),)).fetchone()
    return int(row[0] or 0) if row else 0


def add_attack_order(user_id: int, city_id: int, uid: str, cards=None) -> str:
    """提交一条远程扫码攻打。还没打完的最多两条。成功返回空字符串。

    线程已经退出时，等通路和中断的手动单改回排队。同一城同一人已经在排队就不再加。
    """
    if cards is None:
        cards = 100
    user_id = int(user_id)
    uid = str(uid).strip()
    city_id = int(city_id)
    if shutdown_due(user_id):
        return "已定时关闭，不再执行任务"
    if not attack_in_keepalive(user_id):
        return "不在保活，下了订单游戏也登不进去"
    online = proc_online(user_id)
    if not online:
        skip_unfinished_auto(user_id)
    conn = connect()
    try:
        now = now_ts()
        conn.execute("BEGIN IMMEDIATE")
        if not online:
            cur = conn.execute(
                "UPDATE atk_order SET status='pending', updated_at=? "
                "WHERE user_id=? AND IFNULL(auto,0)=0 AND status IN ('blocked','running')",
                (now, user_id))
            if cur.rowcount:
                log.info("登录账号 %s 的攻打线程不在，%d 条等通路或中断的订单改回排队",
                         username_of(user_id), cur.rowcount)
        dup = conn.execute(
            "SELECT 1 FROM atk_order WHERE user_id=? AND city_id=? AND IFNULL(uid,'')=? "
            "AND status IN ('pending','running','blocked','wait')",
            (user_id, city_id, uid)).fetchone()
        if dup:
            conn.commit()
            return ""
        if _open_attack_count(conn, user_id) >= 2:
            conn.commit()
            return "最多同时两条攻打订单"
        conn.execute(
            "INSERT INTO atk_order(user_id, city_id, uid, status, reason, card_max, name, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (user_id, city_id, uid, "pending", "", int(cards),
             _known_player_name(conn, uid, city_id), now, now))
        conn.commit()
        return ""
    finally:
        conn.close()


def modo_stands(country_id: int) -> list:
    """这个国家首都旁边挂着魔多军团的城。返回 [(落点, 驻地, 落点名)]，按驻地 id 排。"""
    country_id = int(country_id or 0)
    if not country_id:
        return []
    ensure_catalog()
    conn = connect(readonly=True)
    try:
        rows = conn.execute(
            "SELECT id, near_city FROM city WHERE name LIKE '%魔多军团%'"
        ).fetchall()
        stands = []
        for npc, near in rows:
            nids = _parse_near(near)
            if len(nids) != 1:
                continue
            home = conn.execute(
                "SELECT IFNULL(country_id,0), IFNULL(name,'') FROM city WHERE id=?",
                (nids[0],)).fetchone()
            if not home or int(home[0] or 0) != country_id:
                continue
            stands.append((int(nids[0]), int(npc), str(home[1] or "")))
    finally:
        conn.close()
    stands.sort(key=lambda item: item[1])
    return stands


def modo_first_cards(budget) -> int:
    """两座城共用这些恢复卡。第一座最多用一半，余数留给第二座。0 表示不用卡。"""
    try:
        n = int(budget or 0)
    except (TypeError, ValueError):
        return 0
    if n <= 0:
        return 0
    return (n + 1) // 2


def add_modo_order(user_id: int, cards) -> str:
    """提交一条刷摩多军团。按攻打号的国家打首都旁边两座。成功返回空字符串。"""
    try:
        n = int(str(cards).strip())
    except (TypeError, ValueError, AttributeError):
        return "恢复卡数量要是数字"
    if n < 0 or n > 999:
        return "恢复卡数量要是 0 到 999"
    user_id = int(user_id)
    if shutdown_due(user_id):
        return "已定时关闭，不再执行任务"
    if not attack_in_keepalive(user_id):
        return "不在保活，下了订单游戏也登不进去"
    online = proc_online(user_id)
    if not online:
        skip_unfinished_auto(user_id)
    conn = connect()
    try:
        now = now_ts()
        conn.execute("BEGIN IMMEDIATE")
        who = conn.execute(
            "SELECT IFNULL(expires_at,'') FROM app_user WHERE id=?",
            (user_id,)).fetchone()
        if not who:
            conn.commit()
            return "没有这个登录账号"
        if account_expired(who[0]):
            conn.commit()
            return "账号已过期"
        dup = conn.execute(
            "SELECT 1 FROM atk_order WHERE user_id=? AND IFNULL(kind,'')='modo' "
            "AND status IN ('pending','running','blocked','wait')",
            (user_id,)).fetchone()
        if dup:
            conn.execute(
                "UPDATE atk_order SET card_max=?, updated_at=? "
                "WHERE user_id=? AND IFNULL(kind,'')='modo' AND status='pending'",
                (n, now, user_id))
            conn.execute(
                "UPDATE app_user SET modo_cards=? WHERE id=?",
                (n, user_id))
            conn.commit()
            return ""
        if _open_attack_count(conn, user_id) >= 2:
            conn.commit()
            return "最多同时两条攻打订单"
        conn.execute(
            "INSERT INTO atk_order(user_id, city_id, uid, status, reason, card_max, kind, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (user_id, 0, "", "pending", "", n, "modo", now, now))
        conn.execute(
            "UPDATE app_user SET modo_cards=? WHERE id=?",
            (n, user_id))
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


def set_hold_all(user_id: int, on) -> str:
    """全天候挂机。关掉并且挂机时间是 0 时，这次保活也停。"""
    user_id = int(user_id)
    flag = 1 if on else 0
    conn = connect()
    try:
        row = conn.execute(
            "SELECT IFNULL(hold_min,0) FROM app_user WHERE id=?",
            (user_id,)).fetchone()
        if not row:
            return "没有这个账号"
        conn.execute(
            "UPDATE app_user SET hold_all=? WHERE id=?",
            (flag, user_id))
        conn.commit()
        minutes = int(row[0] or 0)
    finally:
        conn.close()
    if not flag and minutes <= 0:
        _delete_signal(_mark_name("hold", user_id))
    return ""


def _shutdown_text(raw) -> str:
    """北京时间，精确到分钟。空字符串表示不关。"""
    text = str(raw or "").strip().replace("T", " ")
    if not text:
        return ""
    text = text[:16]
    try:
        datetime.strptime(text, "%Y-%m-%d %H:%M")
    except ValueError:
        raise ValueError("定时关闭写成 2026-10-08 18:00 这样，留空则不关") from None
    return text


def shutdown_at(user_id: int) -> str:
    """这个登录账号的定时关闭。空字符串表示没配，到点也不会关。"""
    user_id = int(user_id or 0)
    if not user_id:
        return ""
    conn = connect(readonly=True)
    try:
        row = conn.execute(
            "SELECT IFNULL(off_at,'') FROM app_user WHERE id=?",
            (user_id,)).fetchone()
    finally:
        conn.close()
    return str(row[0] or "").strip() if row else ""


def set_shutdown_at(user_id: int, raw) -> str:
    """记下这个登录账号什么时候下线。空的表示取消。成功返回空字符串。"""
    try:
        at = _shutdown_text(raw)
    except ValueError as exc:
        return str(exc)
    conn = connect()
    try:
        cur = conn.execute(
            "UPDATE app_user SET off_at=? WHERE id=?",
            (at, int(user_id)))
        conn.commit()
        if cur.rowcount != 1:
            return "没有这个账号"
    finally:
        conn.close()
    return ""


def shutdown_due(user_id=None) -> bool:
    """这个账号配了定时关闭，而且北京时间已经到了。没配就是假。"""
    if user_id is None:
        user_id = attack_context_user()
    at = shutdown_at(user_id)
    if not at:
        return False
    return at <= beijing_now().strftime("%Y-%m-%d %H:%M")


def executable_users(ids=None) -> list:
    """还要执行任务的登录账号。定时关闭到点的不算。"""
    if ids is None:
        ids = attack_context_users()
    return [int(i) for i in ids if not shutdown_due(int(i))]


def shutdown_covers_context() -> bool:
    """这条攻打线程上的登录账号全都到了定时关闭。没人绑着不算。"""
    ids = attack_context_users()
    return bool(ids) and not executable_users(ids)


def shelve_shutdown_work(user_ids=None) -> None:
    """到点的账号正在做的订单和日常放回排队，先不再做。"""
    if user_ids is None:
        ids = [uid for uid in attack_context_users() if shutdown_due(uid)]
    else:
        ids = [int(i) for i in user_ids if shutdown_due(int(i))]
    if not ids:
        return
    slot = ",".join("?" * len(ids))
    now = now_ts()
    conn = connect()
    try:
        running = conn.execute(
            "SELECT DISTINCT user_id FROM atk_order "
            "WHERE user_id IN (" + slot + ") AND status='running' AND IFNULL(auto,0)=0",
            tuple(ids)).fetchall()
        conn.execute(
            "UPDATE atk_order SET status='pending', reason='', updated_at=? "
            "WHERE user_id IN (" + slot + ") AND status='running'",
            (now, *ids))
        conn.execute(
            "UPDATE daily_job SET status='pending', updated_at=? "
            "WHERE user_id IN (" + slot + ") AND status='running'",
            (now, *ids))
        conn.commit()
    finally:
        conn.close()
    for (uid,) in running:
        mark_attack_resume(int(uid))


def shelve_due_accounts() -> None:
    """主进程巡视：已经到点的账号，把还在做的任务放回排队。"""
    conn = connect(readonly=True)
    try:
        rows = conn.execute(
            "SELECT id, IFNULL(off_at,'') FROM app_user "
            "WHERE IFNULL(off_at,'')!=''").fetchall()
    finally:
        conn.close()
    now = beijing_now().strftime("%Y-%m-%d %H:%M")
    due = [int(user_id) for user_id, at in rows if str(at or "") <= now]
    if due:
        shelve_shutdown_work(due)


def _hold_owner_and_text(raw: str):
    """挂机值是「登录账号|时间」。旧数据没有账号，只还给当时的攻打进程。"""
    text = str(raw or "").strip()
    if not text:
        return 0, ""
    if "|" in text:
        owner, when = text.split("|", 1)
        try:
            return int(owner), when.strip()
        except ValueError:
            return 0, ""
    return 0, text


def note_attack_hold(until_epoch: float) -> None:
    """记下这个攻打 QQ 的挂机保活到什么时候。重连时接着用，不重新计时。"""
    user_id = attack_context_user()
    if not user_id:
        return
    text = datetime.fromtimestamp(float(until_epoch), timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    _upsert_signal(_mark_name("hold", user_id), text)


def clear_attack_hold() -> None:
    user_id = attack_context_user()
    if not user_id:
        return
    _delete_signal(_mark_name("hold", user_id))


def attack_hold_left(user_id=None):
    """这个登录账号的攻打 QQ 挂机还剩多少秒。没有这次挂机返回 None。"""
    if user_id is None:
        uid = attack_context_user()
    else:
        uid = int(user_id or 0)
    if not uid or shutdown_due(uid):
        return None
    raw = _signal_value(_mark_name("hold", uid))
    if not raw:
        owner, raw = _hold_owner_and_text(_signal_value("hold"))
        if not raw:
            return None
        if owner and owner != uid:
            return None
        if not owner:
            return None
    try:
        dt = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return int((dt - datetime.now(timezone.utc)).total_seconds())


def attack_hold_minutes() -> int:
    """这条攻打线程挂机多久。同一个 QQ 上几个账号都设了，取最长的。"""
    ids = executable_users()
    if not ids:
        return 0
    slot = ",".join("?" * len(ids))
    conn = connect(readonly=True)
    try:
        rows = conn.execute(
            "SELECT IFNULL(hold_min,0), IFNULL(tier,'初级'), IFNULL(expires_at,'') "
            f"FROM app_user WHERE id IN ({slot})",
            tuple(ids)).fetchall()
    finally:
        conn.close()
    best = 0
    for hold_min, tier, expires in rows:
        if attack_tier(tier) and not account_expired(expires):
            best = max(best, int(hold_min or 0))
    return best


def attack_hold_always(user_id=None) -> bool:
    """这个账号开了全天候挂机。初级和过期账号不算。
    没指定账号时，同一个攻打 QQ 上有一个开了就算。"""
    if user_id is None:
        return any(attack_hold_always(uid) for uid in attack_context_users())
    user_id = int(user_id or 0)
    if not user_id or shutdown_due(user_id):
        return False
    conn = connect(readonly=True)
    try:
        row = conn.execute(
            "SELECT IFNULL(hold_all,0), IFNULL(tier,'初级'), IFNULL(expires_at,'') "
            "FROM app_user WHERE id=?",
            (user_id,)).fetchone()
    finally:
        conn.close()
    if not row or not int(row[0] or 0):
        return False
    return bool(attack_tier(row[1]) and not account_expired(row[2]))


def attack_hold_on(user_id=None) -> bool:
    """还要挂着：这次保活没到点，或者开了全天候。定时关闭到点的账号不再挂。"""
    if user_id is None and shutdown_covers_context():
        return False
    if user_id is not None and shutdown_due(user_id):
        return False
    if (attack_hold_left(user_id) or 0) > 0:
        return True
    return attack_hold_always(user_id)


def username_of(user_id: int) -> str:
    conn = connect(readonly=True)
    try:
        row = conn.execute(
            "SELECT username FROM app_user WHERE id=?",
            (int(user_id),)).fetchone()
        return str(row[0] or "") if row else ""
    finally:
        conn.close()


def user_id_by_name(username: str) -> int:
    conn = connect(readonly=True)
    try:
        row = conn.execute(
            "SELECT id FROM app_user WHERE username=?",
            (str(username or "").strip(),)).fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


def _forget_page_names(conn) -> None:
    """绑定只留 attack_qq。网页占位清掉；攻打号列里如果本来就是 QQ 号，搬到 attack_qq。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(app_user)")}
    if "attack_qq" not in cols:
        return
    if "attack_acct" in cols:
        conn.execute(
            "UPDATE app_user SET attack_qq=attack_acct "
            "WHERE IFNULL(attack_qq,'')='' AND attack_acct GLOB '[0-9]*' "
            "AND length(attack_acct) BETWEEN 5 AND 12")
        conn.execute("UPDATE app_user SET attack_acct=''")
    if "attack_qq_block" in cols:
        conn.execute(
            "UPDATE app_user SET attack_qq_block=0 "
            "WHERE IFNULL(attack_qq,'')='' AND IFNULL(attack_qq_block,0)!=0")
    row = conn.execute(
        "SELECT attack_qq FROM app_user WHERE IFNULL(attack_qq,'')!='' "
        "ORDER BY id LIMIT 1").fetchone()
    fail_cols = {r[1] for r in conn.execute("PRAGMA table_info(atk_fail)")}
    if "acct" not in fail_cols:
        return
    if row and str(row[0] or "").strip():
        conn.execute(
            "UPDATE atk_fail SET acct=? WHERE acct='' OR acct='网页' OR "
            "(acct LIKE '网页-%' AND substr(acct, 4) GLOB '[0-9]*')",
            (str(row[0]).strip(),))
    else:
        conn.execute(
            "UPDATE atk_fail SET acct='' WHERE acct='网页' OR "
            "(acct LIKE '网页-%' AND substr(acct, 4) GLOB '[0-9]*')")


def _ensure_attack_qq_map(conn) -> None:
    """一个登录账号只留一个攻打 QQ。同一个 QQ 可以绑在多个登录账号上。

    以前建过唯一索引的库，启动时拆掉，否则第二个账号写不进去。
    """
    conn.execute("DROP INDEX IF EXISTS app_user_attack_qq")


def bound_attack_qq() -> tuple:
    """最早记下的一条登录账号和攻打 QQ 的映射。没有则是 (0, '', '')。"""
    conn = connect(readonly=True)
    try:
        row = conn.execute(
            "SELECT id, username, attack_qq FROM app_user "
            "WHERE IFNULL(attack_qq,'')!='' ORDER BY id LIMIT 1").fetchone()
        if not row:
            return 0, "", ""
        return int(row[0]), str(row[1] or ""), str(row[2] or "").strip()
    finally:
        conn.close()


def users_bound_to_qq(uin: str) -> list:
    """这个攻打 QQ 绑着的登录账号，按 id 排。"""
    uin = str(uin or "").strip()
    if not uin:
        return []
    conn = connect(readonly=True)
    try:
        rows = conn.execute(
            "SELECT id FROM app_user WHERE attack_qq=? ORDER BY id",
            (uin,)).fetchall()
    finally:
        conn.close()
    return [int(row[0]) for row in rows]


def attack_qq_owner(uin: str) -> tuple:
    """这个攻打 QQ 最早绑上的登录账号。没有则是 (0, '')。线程启动用它。"""
    uin = str(uin or "").strip()
    if not uin:
        return 0, ""
    conn = connect(readonly=True)
    try:
        row = conn.execute(
            "SELECT id, username FROM app_user WHERE attack_qq=? ORDER BY id LIMIT 1",
            (uin,)).fetchone()
        if not row:
            return 0, ""
        return int(row[0]), str(row[1] or "")
    finally:
        conn.close()


def mapped_attack_user_ids() -> list:
    """已经有攻打 QQ 映射的登录账号，按 id 排。"""
    conn = connect(readonly=True)
    try:
        rows = conn.execute(
            "SELECT id FROM app_user WHERE IFNULL(attack_qq,'')!='' ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    return [int(row[0]) for row in rows]


QQ_MISMATCH = "扫码的 QQ 和绑定的不一致，已暂停"


def attack_qq_of(user_id: int) -> str:
    conn = connect(readonly=True)
    try:
        row = conn.execute(
            "SELECT IFNULL(attack_qq,'') FROM app_user WHERE id=?",
            (int(user_id),)).fetchone()
        return str(row[0] or "").strip() if row else ""
    finally:
        conn.close()


def attack_qq_blocked(user_id: int) -> bool:
    """这个登录账号已经绑了攻打 QQ，但上次核对没对上。还没绑过的不算。"""
    user_id = int(user_id or 0)
    if not user_id:
        return False
    conn = connect(readonly=True)
    try:
        row = conn.execute(
            "SELECT IFNULL(attack_qq,''), IFNULL(attack_qq_block,0) FROM app_user WHERE id=?",
            (user_id,)).fetchone()
        return bool(row and str(row[0] or "").strip() and int(row[1] or 0))
    finally:
        conn.close()


def _move_user_mark(conn, user_id: int, uin: str, kind: str) -> None:
    """绑上 QQ 之前，状态记在登录账号上。页面之后按 QQ 号读，这一份要跟着过去。"""
    src = f"{kind}:user:{int(user_id)}"
    dst = f"{kind}:{uin}"
    old = conn.execute(
        "SELECT value, at FROM atk_signal WHERE name=?", (src,)).fetchone()
    if not old:
        return
    value = old[0]
    if kind == "proc":
        data = _proc_payload(value)
        if data:
            data["user"] = int(user_id)
            data["acct"] = uin
            value = json.dumps(data, ensure_ascii=False)
    new = conn.execute(
        "SELECT at FROM atk_signal WHERE name=?", (dst,)).fetchone()
    if not new:
        conn.execute(
            "INSERT INTO atk_signal(name, value, at) VALUES (?,?,?)",
            (dst, value, old[1]))
    elif str(old[1] or "") >= str(new[0] or ""):
        conn.execute(
            "UPDATE atk_signal SET value=?, at=? WHERE name=?",
            (value, old[1], dst))
    conn.execute("DELETE FROM atk_signal WHERE name=?", (src,))


def confirm_attack_qq(user_id: int, uin: str) -> bool:
    """第一次扫码登录的 QQ 绑到这个登录账号。之后只核对这一次。
    对上返回 True。对不上就暂停这个账号的攻打，返回 False。"""
    user_id = int(user_id or 0)
    uin = str(uin or "").strip()
    if not user_id or not uin.isdigit():
        return True
    conn = connect()
    try:
        row = conn.execute(
            "SELECT IFNULL(attack_qq,''), IFNULL(attack_qq_block,0), username "
            "FROM app_user WHERE id=?",
            (user_id,)).fetchone()
        if not row:
            return True
        bound = str(row[0] or "").strip()
        blocked = int(row[1] or 0)
        who = str(row[2] or "")
        if not bound:
            cur = conn.execute(
                "UPDATE app_user SET attack_qq=?, attack_acct='', attack_qq_block=0 "
                "WHERE id=? AND IFNULL(TRIM(attack_qq),'')=''",
                (uin, user_id))
            if cur.rowcount == 1:
                for kind in ("proc", "hold", "login", "pause", "move"):
                    _move_user_mark(conn, user_id, uin, kind)
                conn.commit()
                log.info("登录账号 %s 第一次扫码，绑定攻打 QQ %s", who, uin)
                return True
            again = conn.execute(
                "SELECT IFNULL(attack_qq,'') FROM app_user WHERE id=?",
                (user_id,)).fetchone()
            bound = str(again[0] or "").strip() if again else ""
            if not bound:
                conn.rollback()
                return False
        if bound == uin:
            if blocked:
                conn.execute(
                    "UPDATE app_user SET attack_qq_block=0 WHERE id=?",
                    (user_id,))
                conn.commit()
                log.info("登录账号 %s 的攻打 QQ %s 已对上，暂停解开", who, uin)
                set_attack_paused(False, user_id)
            else:
                log.info("登录账号 %s 的攻打 QQ %s 核对通过", who, uin)
            return True
        conn.execute(
            "UPDATE app_user SET attack_qq_block=1 WHERE id=?",
            (user_id,))
        conn.commit()
    finally:
        conn.close()
    log.error("登录账号 %s 绑定的攻打 QQ 是 %s，这次登录的是 %s，已暂停",
              who, bound, uin)
    set_attack_paused(True, user_id)
    return False


def bind_attack_account(user_id: int, account: str) -> str:
    """把攻打 QQ 绑到这个登录账号。一个账号只能有一个 QQ，同一个 QQ 可以绑多个账号。成功返回空字符串。"""
    account = str(account or "").strip()
    user_id = int(user_id)
    if not account.isdigit() or not 5 <= len(account) <= 12:
        return "攻打号就是 QQ 号，要写成 5 到 12 位数字"
    conn = connect()
    try:
        mine = conn.execute(
            "SELECT IFNULL(attack_qq,'') FROM app_user WHERE id=?",
            (user_id,)).fetchone()
        if not mine:
            return "没有这个账号"
        current = str(mine[0] or "").strip()
        if current and current != account:
            return f"这个登录账号已经绑定了攻打 QQ「{current}」"
        conn.execute(
            "UPDATE app_user SET attack_qq=?, attack_acct='', attack_qq_block=0 WHERE id=?",
            (account, user_id))
        conn.commit()
    finally:
        conn.close()
    return ""


_ATTACK_MARKS = ("proc", "hold", "login", "pause", "move", "pageqr", "qrpath", "resume")


def clear_attack_binding(user_id: int) -> str:
    """解开这个登录账号的攻打 QQ。成功返回空字符串。

    未完成的攻打订单和日常记为失败，避免下一个号接着打。
    别的账号还绑着同一个 QQ 时，票据和那份攻打状态留着。
    """
    user_id = int(user_id)
    conn = connect()
    try:
        row = conn.execute(
            "SELECT IFNULL(attack_qq,'') FROM app_user WHERE id=?",
            (user_id,)).fetchone()
        if not row:
            return "没有这个账号"
        uin = str(row[0] or "").strip()
        others = 0
        if uin:
            others = int(conn.execute(
                "SELECT COUNT(*) FROM app_user WHERE attack_qq=? AND id!=?",
                (uin, user_id)).fetchone()[0] or 0)
        now = now_ts()
        conn.execute(
            "UPDATE app_user SET attack_qq='', attack_acct='', attack_qq_block=0 WHERE id=?",
            (user_id,))
        names = [f"{kind}:user:{user_id}" for kind in _ATTACK_MARKS]
        names.append(f"pagelogin-{user_id}")
        if uin and not others:
            names.extend(f"{kind}:{uin}" for kind in _ATTACK_MARKS)
        slot = ",".join("?" * len(names))
        conn.execute(
            f"DELETE FROM atk_signal WHERE name IN ({slot})", tuple(names))
        conn.execute(
            "UPDATE atk_signal SET value='0' WHERE name='login_for' AND value=?",
            (str(user_id),))
        conn.execute(
            "UPDATE atk_order SET status='failed', reason=?, updated_at=? "
            "WHERE user_id=? AND status IN ('pending','running','blocked','wait')",
            ("已解除攻打 QQ 绑定", now, user_id))
        conn.execute(
            "UPDATE daily_job SET status='failed', detail=?, updated_at=? "
            "WHERE user_id=? AND status IN ('pending','running')",
            ("已解除攻打 QQ 绑定", now, user_id))
        conn.commit()
    finally:
        conn.close()
    _drop_attack_files(user_id, uin if uin and not others else "")
    log.info("登录账号 %s 的攻打 QQ 已解开%s",
             username_of(user_id), f"，{uin}" if uin else "")
    return ""


def _drop_attack_files(user_id: int, uin: str) -> None:
    """删这个账号自己的登录图。uin 有值才删这份攻打票据。"""
    paths = [
        user_path(f"accounts/attack-{int(user_id)}.json"),
        user_path(f"accounts/page-login-{int(user_id)}.json"),
    ]
    if uin:
        paths.append(user_path(f"accounts/qq-{uin}.json"))
    for path in paths:
        for extra in (path, (path[:-5] + ".qrcode.png" if path.endswith(".json") else path),
                      path + ".lock"):
            try:
                Path(extra).unlink()
            except OSError:
                pass


def set_retreat(user_id: int, mode, hops, city_id, on_fail=None) -> str:
    """打完后退。hops 是朝一座城退几座，city 是退进指定城。两种不能同时用。"""
    mode = str(mode or "").strip()
    if mode not in ("off", "hops", "city"):
        return "后退策略要选不后退、后退几座城或退到指定城市"
    try:
        n = int(str(hops).strip())
    except (TypeError, ValueError, AttributeError):
        return "后退座数要是数字"
    if n < 1 or n > 20:
        return "后退座数要是 1 到 20"
    try:
        cid = int(str(city_id if city_id is not None else "0").strip() or "0")
    except (TypeError, ValueError, AttributeError):
        return "城市不对"
    if cid < 0:
        return "城市不对"
    if mode == "city" and cid <= 0:
        return "要选退到哪座城"
    if cid > 0 and not city_name(cid):
        return "这座城不在目录里"
    if isinstance(on_fail, str):
        fail = 1 if on_fail.strip().lower() in ("1", "true", "on", "开") else 0
    else:
        fail = 1 if on_fail else 0
    conn = connect()
    try:
        conn.execute(
            "UPDATE app_user SET retreat_mode=?, retreat_hops=?, retreat_city=?, "
            "retreat_fail=? WHERE id=?",
            (mode, n, cid, fail, int(user_id)))
        conn.commit()
    finally:
        conn.close()
    return ""


def retreat_settings(user_id: int = 0) -> dict:
    """这个登录账号的打完后退。没填朝向时，后退几座城默认朝马奇诺。"""
    if not user_id:
        user_id = attack_context_user()
    mode, hops, city_id, on_fail = "hops", 3, 0, False
    if user_id:
        conn = connect(readonly=True)
        try:
            row = conn.execute(
                "SELECT IFNULL(retreat_mode,'hops'), IFNULL(retreat_hops,3), "
                "IFNULL(retreat_city,0), IFNULL(retreat_fail,0) FROM app_user WHERE id=?",
                (int(user_id),)).fetchone()
        finally:
            conn.close()
        if row:
            mode = str(row[0] or "hops")
            hops = int(row[1] or 3)
            city_id = int(row[2] or 0)
            on_fail = bool(row[3])
    if mode not in ("off", "hops", "city"):
        mode = "hops"
    if hops < 1:
        hops = 3
    name = ""
    if mode == "hops" and city_id <= 0:
        name = "马奇诺"
        city_id = city_id_named(name)
    elif city_id > 0:
        name = city_name(city_id) or str(city_id)
    return {"mode": mode, "hops": hops, "city_id": city_id, "name": name,
            "on_fail": on_fail}


def set_lock_cards(user_id: int, cards) -> str:
    """自动锁敌每小时最多开几张恢复卡。成功返回空字符串。"""
    try:
        n = int(str(cards).strip())
    except (TypeError, ValueError, AttributeError):
        return "锁敌恢复卡数量要是数字"
    if n < 0 or n > 99:
        return "锁敌恢复卡数量要是 0 到 99"
    conn = connect()
    try:
        conn.execute(
            "UPDATE app_user SET lock_cards=? WHERE id=?",
            (n, int(user_id)))
        conn.commit()
    finally:
        conn.close()
    return ""


def lock_card_limit(user_id: int = 0) -> int:
    """这个账号自动锁敌每小时的恢复卡上限。没填过是 3。"""
    if not user_id:
        user_id = attack_context_user()
    n = 3
    if user_id:
        conn = connect(readonly=True)
        try:
            row = conn.execute(
                "SELECT IFNULL(lock_cards,3) FROM app_user WHERE id=?",
                (int(user_id),)).fetchone()
        finally:
            conn.close()
        if row and row[0] is not None:
            n = int(row[0])
    if n < 0:
        return 3
    return n


def _lock_card_cutoff() -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")


def lock_cards_used(user_id: int = 0) -> int:
    """最近 1 小时里，自动锁敌已经开掉几张恢复卡。"""
    user_id = int(user_id or attack_context_user() or 0)
    if not user_id:
        return 0
    conn = connect(readonly=True)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM lock_card_use WHERE user_id=? AND at>=?",
            (user_id, _lock_card_cutoff())).fetchone()
        return int(row[0] or 0) if row else 0
    finally:
        conn.close()


def lock_card_block(user_id: int = 0) -> str:
    """这一小时锁敌还能不能再开一张。能开返回空字符串。"""
    user_id = int(user_id or attack_context_user() or 0)
    limit = lock_card_limit(user_id)
    used = lock_cards_used(user_id)
    if used >= limit:
        return f"1小时内锁敌最多开 {limit} 张恢复卡，这一小时已经用满"
    return ""


def note_lock_card(user_id: int = 0) -> None:
    """记下自动锁敌刚开掉的一张恢复卡。写库失败不影响已经发出的那张。"""
    user_id = int(user_id or attack_context_user() or 0)
    if not user_id:
        return
    try:
        conn = connect()
    except sqlite3.Error:
        return
    try:
        conn.execute(
            "INSERT INTO lock_card_use(user_id, at) VALUES (?,?)",
            (user_id, now_ts()))
        conn.execute(
            "DELETE FROM lock_card_use WHERE user_id=? AND at<?",
            (user_id, _lock_card_cutoff()))
        conn.commit()
    except sqlite3.Error:
        return
    finally:
        conn.close()


def begin_lock_cards() -> None:
    """这一单是自动锁敌。接下来开的恢复卡算进每小时上限。"""
    _attack_local.lock_cards = True


def end_lock_cards() -> None:
    """这一单结束。之后开的恢复卡不再算进锁敌上限。"""
    _attack_local.lock_cards = False


def lock_cards_active() -> bool:
    return bool(getattr(_attack_local, "lock_cards", False))


def note_storm_reject(name: str, user_id: int = 0, qq: str = "") -> None:
    """记下一次已经发出去的超级强攻拒绝。没绑到登录账号的不进页面。"""
    name = " ".join(str(name or "").split()).strip() or "未知玩家"
    if len(name) > 32:
        name = name[:32]
    user_id = int(user_id or attack_context_user() or 0)
    qq = str(qq or attack_context_qq() or "").strip()
    if not user_id and qq:
        user_id, _ = attack_qq_owner(qq)
    if not user_id:
        log.info("超级强攻已拒绝（%s），这个号没绑登录账号，页面上看不到", name)
        return
    try:
        conn = connect()
    except sqlite3.Error:
        return
    try:
        conn.execute(
            "INSERT INTO storm_reject(user_id, name, at) VALUES (?,?,?)",
            (user_id, name, now_ts()))
        conn.execute(
            "DELETE FROM storm_reject WHERE user_id=? AND at<?",
            (user_id, _lock_card_cutoff()))
        conn.commit()
    except sqlite3.Error:
        return
    finally:
        conn.close()


def list_storm_rejects(user_id: int) -> list:
    """这个登录账号最近 1 小时拒绝的超级强攻。新的在前。"""
    user_id = int(user_id or 0)
    if not user_id:
        return []
    conn = connect(readonly=True)
    try:
        rows = conn.execute(
            "SELECT name, at FROM storm_reject WHERE user_id=? AND at>=? "
            "ORDER BY at DESC, id DESC LIMIT 20",
            (user_id, _lock_card_cutoff())).fetchall()
    finally:
        conn.close()
    return [{"name": str(name or ""), "at": beijing_ts(at)} for name, at in rows]


def set_clear_plan(user_id: int, mode, page_from, page_to, priority,
                   wait_min=0, scan_sec=0) -> str:
    """清城高级配置。前 5 页，或一个页码范围。优先 UID 最多 50 个。

    wait_min 是空城后再打的分钟。0 表示空了就结束。
    scan_sec 是两次扫页至少隔开的秒数。0 表示每次出手冷却都扫。成功返回空字符串。
    """
    mode = str(mode or "head").strip()
    if mode not in ("head", "range"):
        return "清城扫页要选前5页或指定范围"
    try:
        start = int(str(page_from if page_from is not None else "1").strip() or "1")
        end = int(str(page_to if page_to is not None else "5").strip() or "5")
    except (TypeError, ValueError, AttributeError):
        return "页码要是数字"
    if mode == "range" and (start < 1 or end < start or end > 200):
        return "页码范围要是 1 到 200，结束不能小于开始"
    if start < 1 or end < start or end > 200:
        start, end = 1, 5
    items = priority if isinstance(priority, list) else []
    if len(items) > 50:
        return "优先 UID 最多 50 个"
    rows = []
    seen = set()
    for seq, item in enumerate(items):
        if not isinstance(item, dict):
            return "优先 UID 格式不对"
        uid = str(item.get("uid") or "").strip()
        if not uid.isdigit() or len(uid) > 32:
            return "优先 UID 要是数字"
        if uid in seen:
            return "同一个 UID 只留一条"
        seen.add(uid)
        try:
            rank = int(str(item.get("rank") if item.get("rank") is not None else "1").strip() or "1")
        except (TypeError, ValueError, AttributeError):
            return "优先级要是数字"
        if rank < 1 or rank > 99:
            return "优先级要是 1 到 99"
        rows.append((uid, rank, seq))
    if wait_min is None or str(wait_min).strip() == "":
        wait = 0
    else:
        try:
            wait = int(str(wait_min).strip())
        except (TypeError, ValueError, AttributeError):
            return "空城周期要是分钟数"
    if wait < 0 or wait > 1440:
        return "空城周期要是 0 到 1440 分钟"
    if scan_sec is None or str(scan_sec).strip() == "":
        scan = 0
    else:
        try:
            scan = int(str(scan_sec).strip())
        except (TypeError, ValueError, AttributeError):
            return "扫页冷却要是秒数"
    if scan < 0 or scan > 300:
        return "扫页冷却要是 0 到 300 秒"
    conn = connect()
    try:
        conn.execute(
            "UPDATE app_user SET clear_mode=?, clear_from=?, clear_to=?, "
            "clear_wait=?, clear_scan=? WHERE id=?",
            (mode, start, end, wait, scan, int(user_id)))
        conn.execute("DELETE FROM clear_prio WHERE user_id=?", (int(user_id),))
        conn.executemany(
            "INSERT INTO clear_prio(user_id, uid, rank, seq) VALUES (?,?,?,?)",
            [(int(user_id), uid, rank, seq) for uid, rank, seq in rows])
        conn.commit()
    finally:
        conn.close()
    return ""


def clear_settings(user_id: int = 0) -> dict:
    """这个登录账号的清城扫页和优先 UID。名单按优先级、再按添加顺序。"""
    mode, start, end, wait, scan = "head", 1, 5, 0, 0
    rows = []
    user_id = int(user_id or 0)
    if user_id:
        conn = connect(readonly=True)
        try:
            saved = conn.execute(
                "SELECT IFNULL(clear_mode,'head'), IFNULL(clear_from,1), IFNULL(clear_to,5), "
                "IFNULL(clear_wait,0), IFNULL(clear_scan,0) FROM app_user WHERE id=?",
                (user_id,)).fetchone()
            if saved:
                mode = str(saved[0] or "head")
                start = int(saved[1] or 1)
                end = int(saved[2] or 5)
                wait = int(saved[3] or 0)
                scan = int(saved[4] or 0)
            rows = conn.execute(
                "SELECT uid, rank FROM clear_prio WHERE user_id=? ORDER BY rank, seq, uid",
                (user_id,)).fetchall()
        finally:
            conn.close()
    if mode not in ("head", "range"):
        mode = "head"
    if start < 1:
        start = 1
    if end < start:
        end = start
    if wait < 0 or wait > 1440:
        wait = 0
    if scan < 0 or scan > 300:
        scan = 0
    return {
        "mode": mode,
        "page_from": start,
        "page_to": end,
        "wait_min": wait,
        "scan_sec": scan,
        "priority": [{"uid": str(uid), "rank": int(rank)} for uid, rank in rows],
    }


def _stored_tier(user_id: int) -> str:
    conn = connect(readonly=True)
    try:
        row = conn.execute(
            "SELECT IFNULL(tier,'初级') FROM app_user WHERE id=?",
            (int(user_id or 0),)).fetchone()
    finally:
        conn.close()
    return row[0] if row else "初级"


def clear_wait_minutes(user_id: int = 0) -> int:
    """空城后再打要等几分钟。0 表示空了就结束。中级不使用这项。"""
    if not user_id:
        user_id = attack_context_user()
    if not high_tier(_stored_tier(user_id)):
        return 0
    return int(clear_settings(user_id).get("wait_min") or 0)


def schedule_empty_order(order_id: int, minutes: int, beats=None, stuck=False) -> bool:
    """同一条清城订单过这么多分钟再排队。已经不在打的返回 False。

    stuck 为真表示城里还留着打不过的人。否则是这几页没人。
    """
    minutes = int(minutes or 0)
    if minutes <= 0:
        return False
    due = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    run_at = due.strftime("%Y-%m-%dT%H:%M:%SZ")
    if stuck:
        reason = f"还有打不过的人，{minutes} 分钟后再打 · {beijing_ts(run_at)}"
    else:
        reason = f"这座城是空的，{minutes} 分钟后再打 · {beijing_ts(run_at)}"
    conn = connect()
    try:
        if beats is None:
            cur = conn.execute(
                "UPDATE atk_order SET status='wait', reason=?, run_at=?, updated_at=? "
                "WHERE id=? AND status='running'",
                (reason, run_at, now_ts(), int(order_id)))
        else:
            cur = conn.execute(
                "UPDATE atk_order SET status='wait', reason=?, run_at=?, beats=?, updated_at=? "
                "WHERE id=? AND status='running'",
                (reason, run_at, int(beats), now_ts(), int(order_id)))
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def attack_wait_pending() -> bool:
    """这条线程还有没到点的清城再打。同一个攻打 QQ 上的账号都算。"""
    ids = attack_context_users()
    if not ids:
        return False
    slot = ",".join("?" * len(ids))
    conn = connect(readonly=True)
    try:
        row = conn.execute(
            f"SELECT 1 FROM atk_order WHERE user_id IN ({slot}) AND status='wait' LIMIT 1",
            tuple(ids)).fetchone()
    finally:
        conn.close()
    return bool(row)


def release_due_waits() -> int:
    """到点的空城订单改回排队。还没到的不动。同一个攻打 QQ 上的账号都算。"""
    ids = attack_context_users()
    if not ids:
        return 0
    now = now_ts()
    slot = ",".join("?" * len(ids))
    conn = connect()
    try:
        cur = conn.execute(
            "UPDATE atk_order SET status='pending', reason='', updated_at=? "
            f"WHERE user_id IN ({slot}) AND status='wait' "
            "AND IFNULL(run_at,'')!='' AND run_at<=?",
            (now, *ids, now))
        conn.commit()
        n = int(cur.rowcount or 0)
        if n:
            who = "、".join(username_of(i) for i in ids)
            log.info("登录账号 %s 有 %d 条空城订单到点，改回排队", who, n)
        return n
    finally:
        conn.close()


def clear_fight_plan(user_id: int = 0) -> dict:
    """留给空 UID 的清城。pages 是从 0 开始的页，含首尾。priority 是 UID 到优先级。

    中级仍按前 5 页清城，不带优先名单，也不拉长扫页间隔。
    """
    if not user_id:
        user_id = attack_context_user()
    if not high_tier(_stored_tier(user_id)):
        return {"pages": (0, 4), "priority": {}, "scan_sec": 0}
    saved = clear_settings(user_id)
    if saved["mode"] == "range":
        pages = (saved["page_from"] - 1, saved["page_to"] - 1)
    else:
        pages = (0, 4)
    priority = {row["uid"]: int(row["rank"]) for row in saved["priority"]}
    return {"pages": pages, "priority": priority,
            "scan_sec": int(saved.get("scan_sec") or 0)}


def set_auto_lock(user_id: int, on: bool) -> None:
    conn = connect()
    try:
        conn.execute(
            "UPDATE app_user SET auto_lock=? WHERE id=?",
            (1 if on else 0, int(user_id)))
        conn.commit()
    finally:
        conn.close()


def online_attack_busy(user_id: int, city_id: int, uid: str) -> bool:
    """这个人已经有一条还没打完的自动或手动订单。"""
    uid = str(uid or "").strip()
    if not uid:
        return False
    conn = connect(readonly=True)
    try:
        row = conn.execute(
            "SELECT 1 FROM atk_order WHERE user_id=? AND city_id=? AND uid=? "
            "AND status IN ('pending','running','blocked')",
            (int(user_id), int(city_id), uid)).fetchone()
        return row is not None
    finally:
        conn.close()


def _lock_gone_on(conn, user_id, city_id, uid) -> bool:
    """库里已经能确定这个人不在订单这座城。扫描还没改、看不出时返回 False。"""
    uid = str(uid or "").strip()
    if not uid:
        return False
    try:
        city_id = int(city_id)
    except (TypeError, ValueError):
        return False
    if city_id <= 0:
        return False
    if user_id:
        sub = conn.execute(
            "SELECT last_present FROM watch_sub "
            "WHERE user_id=? AND city_id=? AND uid=?",
            (int(user_id), city_id, uid)).fetchone()
        if sub is not None and sub[0] is not None and int(sub[0]) == 0:
            return True
    row = conn.execute(
        "SELECT city_id FROM player WHERE uid=? ORDER BY fetched_at DESC LIMIT 1",
        (uid,)).fetchone()
    return row is not None and int(row[0]) != city_id


def lock_target_info(city_id, uid) -> dict:
    """自动锁敌开打前看库。gone 为真就可以跳过；否则带着 page 去现场翻页。"""
    uid = str(uid or "").strip()
    out = {"gone": False, "page": 0, "morale": None}
    try:
        city_id = int(city_id or 0)
    except (TypeError, ValueError):
        return out
    if not uid or city_id <= 0:
        return out
    user_id = attack_context_user()
    try:
        conn = connect(readonly=True)
    except sqlite3.Error:
        return out
    try:
        if _lock_gone_on(conn, user_id, city_id, uid):
            out["gone"] = True
            return out
        row = conn.execute(
            "SELECT IFNULL(page,0), morale FROM player WHERE uid=? AND city_id=?",
            (uid, city_id)).fetchone()
        if row:
            out["page"] = int(row[0] or 0)
            out["morale"] = row[1]
        return out
    except sqlite3.Error:
        return out
    finally:
        conn.close()


def enqueue_online_attack(user_id: int, city_id: int, uid: str) -> bool:
    """给这个订阅排一条自动攻打。同一人还没打完就不再排。还没打完的最多两条。没挂在游戏上就不排。"""
    uid = str(uid or "").strip()
    if (not uid or shutdown_due(user_id) or not attack_in_keepalive(user_id)
            or online_attack_busy(user_id, city_id, uid)):
        return False
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if _open_attack_count(conn, user_id) >= 2:
            conn.commit()
            return False
        if online_attack_busy(user_id, city_id, uid):
            conn.commit()
            return False
        saved = conn.execute(
            "SELECT IFNULL(card_max,100) FROM app_user WHERE id=?",
            (int(user_id),)).fetchone()
        cards = int(saved[0]) if saved else 100
        now = now_ts()
        conn.execute(
            "INSERT INTO atk_order(user_id, city_id, uid, status, reason, auto, card_max, name, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (int(user_id), int(city_id), uid, "pending", "", 1, cards,
             _known_player_name(conn, uid, city_id), now, now))
        conn.commit()
        return True
    finally:
        conn.close()


def mark_lock_sent(user_id: int, city_id: int, uid: str) -> None:
    """这一次人在城里，攻打已经排过，扫描不要重复排。"""
    conn = connect()
    try:
        conn.execute(
            "UPDATE watch_sub SET lock_sent=1 WHERE user_id=? AND city_id=? AND uid=?",
            (int(user_id), int(city_id), str(uid or "").strip()))
        conn.commit()
    finally:
        conn.close()


def queue_present_locks(user_id: int) -> int:
    """自动锁敌刚打开。已经在城里、这轮还没排过的订阅，各排一条。"""
    conn = connect()
    try:
        user = conn.execute(
            "SELECT IFNULL(tier,'初级'), IFNULL(expires_at,'') FROM app_user WHERE id=?",
            (int(user_id),)).fetchone()
        if not user or not high_tier(user[0]) or account_expired(user[1]):
            return 0
        rows = conn.execute(
            "SELECT city_id, uid FROM watch_sub "
            "WHERE user_id=? AND last_present=1 AND IFNULL(lock_sent,0)=0",
            (int(user_id),)).fetchall()
    finally:
        conn.close()
    n = 0
    for city_id, uid in rows:
        queued = enqueue_online_attack(user_id, city_id, uid)
        if queued or online_attack_busy(user_id, city_id, uid):
            mark_lock_sent(user_id, city_id, uid)
        if queued:
            n += 1
    return n


def queue_watch_lock(user_id: int, city_id: int, uid: str) -> str:
    """手动给这条订阅排一条自动索敌。人要正在这座城里。成功返回空字符串。

    和扫描自动排的是同一种订单。这一单还没打完时不再排第二条。
    """
    user_id = int(user_id)
    try:
        city_id = int(city_id)
    except (TypeError, ValueError):
        return "城市不对"
    uid = str(uid or "").strip()
    if city_id <= 0 or not uid:
        return "没有这条订阅"
    conn = connect(readonly=True)
    try:
        user = conn.execute(
            "SELECT IFNULL(tier,'初级'), IFNULL(expires_at,'') FROM app_user WHERE id=?",
            (user_id,)).fetchone()
        sub = conn.execute(
            "SELECT last_present FROM watch_sub WHERE user_id=? AND city_id=? AND uid=?",
            (user_id, city_id, uid)).fetchone()
    finally:
        conn.close()
    if not user or not high_tier(user[0]):
        return "自动索敌需要高级订阅"
    if account_expired(user[1]):
        return "账号已过期"
    if not sub:
        return "没有这条订阅"
    if sub[0] is None or int(sub[0]) != 1:
        return "这个人不在这座城"
    if not attack_in_keepalive(user_id):
        return "不在保活，下了订单游戏也登不进去"
    if online_attack_busy(user_id, city_id, uid):
        return "这个人已经有一条还没打完的订单"
    if not enqueue_online_attack(user_id, city_id, uid):
        if online_attack_busy(user_id, city_id, uid):
            return "这个人已经有一条还没打完的订单"
        return "最多同时两条攻打订单"
    mark_lock_sent(user_id, city_id, uid)
    log.info("登录账号 %s 手动索敌，城市 %s UID %s", username_of(user_id), city_id, uid)
    return ""


def _order_row(r) -> dict:
    kind = str(r[7] or "") if len(r) > 7 else ""
    run_at = str(r[8] or "") if len(r) > 8 else ""
    if str(r[3] or "") != "wait":
        run_at = ""
    name = str(r[9] or "").strip() if len(r) > 9 else ""
    uid = str(r[2] or "")
    if not name and uid:
        conn = connect(readonly=True)
        try:
            name = _known_player_name(conn, uid, r[1])
        finally:
            conn.close()
        if name:
            remember_order_name(r[0], name)
    return {"id": r[0], "city_id": r[1], "uid": uid, "name": name, "status": r[3],
            "reason": r[4], "created_at": beijing_ts(r[5]), "beats": r[6],
            "city_name": "" if kind == "modo" else city_name(r[1]),
            "kind": kind, "run_at": run_at}


def list_attack_orders(user_id: int, limit: int = 3) -> list:
    """页面上的订单。还没打完的最多带上两条，总共最多三条，新的在前。"""
    limit = max(1, min(int(limit or 3), 3))
    user_id = int(user_id)
    cols = ("id, city_id, IFNULL(uid,''), status, IFNULL(reason,''), created_at, beats, "
            "IFNULL(kind,''), IFNULL(run_at,''), IFNULL(name,'')")
    conn = connect(readonly=True)
    try:
        valid = conn.execute(
            f"SELECT {cols} FROM atk_order WHERE user_id=? "
            "AND status IN ('pending','running','blocked','wait') ORDER BY id DESC LIMIT 2",
            (user_id,)).fetchall()
        room = limit - len(valid)
        done = []
        if room > 0:
            skip = [int(r[0]) for r in valid]
            extra = ""
            args = [user_id]
            if skip:
                extra = " AND id NOT IN (" + ",".join("?" for _ in skip) + ")"
                args.extend(skip)
            args.append(room)
            done = conn.execute(
                f"SELECT {cols} FROM atk_order WHERE user_id=? "
                "AND status NOT IN ('pending','running','blocked','wait')"
                f"{extra} ORDER BY id DESC LIMIT ?",
                args).fetchall()
    finally:
        conn.close()
    rows = list(valid) + list(done)
    rows.sort(key=lambda r: int(r[0]), reverse=True)
    return [_order_row(r) for r in rows]


def set_page_qr(on: bool, user_id: int = 0) -> None:
    """这个攻打 QQ 的登录二维码显示在它自己的网页上。扫完或进程停了就关掉。"""
    _upsert_signal(_mark_name("pageqr", user_id), "1" if on else "0")
    if not on:
        _upsert_signal(_mark_name("qrpath", user_id), "")


def note_login_qr(path: str, user_id: int = 0) -> None:
    """这次扫码的图写在哪。只给这个攻打 QQ 对应的登录账号看。"""
    _upsert_signal(
        _mark_name("qrpath", user_id), str(path or ""),
        at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"))


def login_qr_path(user_id: int = 0) -> str:
    return _signal_value(_mark_name("qrpath", user_id)).strip()


def login_qr_at(user_id: int = 0) -> str:
    """这张网页二维码是什么时候写下的。页面用它判断该不该换图。"""
    user_id = int(user_id or 0)
    if not user_id:
        return ""
    if page_login_path(user_id):
        return _signal_at(_page_login_key(user_id))
    if login_qr_path(user_id):
        return _signal_at(_mark_name("qrpath", user_id))
    return ""


def _page_login_key(user_id: int) -> str:
    return f"pagelogin-{int(user_id)}"


def set_page_login(user_id: int, path: str) -> None:
    """这个登录账号还没绑定攻打 QQ，网页上单独显示一张二维码。不改攻打进程。"""
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO atk_signal(name, value, at) VALUES (?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET value=excluded.value, at=excluded.at",
            (_page_login_key(user_id), str(path or ""),
             datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")))
        conn.commit()
    finally:
        conn.close()


def page_login_path(user_id: int) -> str:
    if not user_id:
        return ""
    conn = connect(readonly=True)
    try:
        row = conn.execute(
            "SELECT value FROM atk_signal WHERE name=?",
            (_page_login_key(user_id),)).fetchone()
    finally:
        conn.close()
    return str(row[0] or "").strip() if row else ""


def clear_page_login(user_id: int) -> None:
    set_page_login(user_id, "")


def _with_page_qr(status: dict, user_id: int) -> dict:
    """网页二维码只属于还没绑定的这个登录账号，不跟攻打进程走。"""
    if not page_login_path(int(user_id)):
        return status
    status = dict(status)
    status["qr"] = True
    return status


def unbound_login_waiting() -> bool:
    """有个攻打进程正在等扫码，而且它的登录账号还没有攻打 QQ。自动拉起不要把这张码抢走。"""
    if not proc_online() or proc_phase() != "login":
        return False
    owner = proc_user()
    if not owner:
        return True
    return not attack_qq_of(owner)


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


def _kept_int(data: dict, key: str):
    """士气和体力可以是 0。缺了或类型不对就当还没读到。"""
    val = data.get(key)
    if isinstance(val, bool) or not isinstance(val, int):
        return None
    return val


_attack_local = threading.local()


def _assign_legacy_fails(conn) -> None:
    """还没归号的失败记录。只有一条映射时才归给那个攻打 QQ，避免记错人。"""
    rows = conn.execute(
        "SELECT attack_qq FROM app_user WHERE IFNULL(attack_qq,'')!=''"
    ).fetchall()
    if len(rows) != 1 or not str(rows[0][0] or "").strip():
        return
    conn.execute(
        "UPDATE atk_fail SET acct=? WHERE acct='' OR acct='网页' OR "
        "(acct LIKE '网页-%' AND substr(acct, 4) GLOB '[0-9]*')",
        (str(rows[0][0]).strip(),))


def set_fighting_order(order_id: int) -> None:
    """这条线程正在打的订单。关停后 fighting_order_stopped 变成真。"""
    _attack_local.order = int(order_id or 0)


def fighting_order_stopped() -> bool:
    """网页已经关停当前这一单。没有正在打的订单时不算停。"""
    order_id = int(getattr(_attack_local, "order", 0) or 0)
    if not order_id:
        return False
    conn = connect(readonly=True)
    try:
        row = conn.execute(
            "SELECT status FROM atk_order WHERE id=?", (order_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return True
    return str(row[0] or "") != "running"


def set_attack_context(user_id: int, account: str) -> None:
    """这条线程打这个攻打 QQ。同一个 QQ 上的登录账号都会被领到。别的线程有自己的一份。"""
    _attack_local.user = int(user_id or 0)
    _attack_local.qq = str(account or "").strip()
    if _attack_local.qq:
        conn = connect()
        try:
            _assign_legacy_fails(conn)
            conn.commit()
        finally:
            conn.close()


def attack_context_user() -> int:
    return int(getattr(_attack_local, "user", 0) or 0)


def attack_context_qq() -> str:
    return str(getattr(_attack_local, "qq", "") or "").strip()


def attack_context_users() -> list:
    """这条攻打线程要照看的登录账号。同一个攻打 QQ 绑了几个，就都算上。"""
    uin = attack_context_qq()
    if uin:
        ids = users_bound_to_qq(uin)
        if ids:
            return ids
    uid = attack_context_user()
    return [uid] if uid else []


def _identity(user_id: int = 0) -> tuple:
    """返回 (登录账号, 攻打 QQ)。查别人时不用这条线程自己的 QQ。"""
    ctx_user = attack_context_user()
    user_id = int(user_id or ctx_user or 0)
    uin = ""
    if not user_id or user_id == ctx_user:
        uin = attack_context_qq()
    if not uin.isdigit() and user_id:
        uin = attack_qq_of(user_id)
    return user_id, str(uin or "").strip()


def _mark_name(kind: str, user_id: int = 0) -> str:
    """暂停、挂机、登录、进程状态都以攻打 QQ 为名。还没扫上时才落到登录账号。"""
    uid, uin = _identity(user_id)
    if uin.isdigit():
        return f"{kind}:{uin}"
    if uid:
        return f"{kind}:user:{uid}"
    return kind


def _signal_value(name: str) -> str:
    conn = connect(readonly=True)
    try:
        row = conn.execute(
            "SELECT value FROM atk_signal WHERE name=?", (name,)).fetchone()
    finally:
        conn.close()
    return str(row[0] or "") if row else ""


def _signal_at(name: str) -> str:
    conn = connect(readonly=True)
    try:
        row = conn.execute(
            "SELECT at FROM atk_signal WHERE name=?", (name,)).fetchone()
    finally:
        conn.close()
    return str(row[0] or "") if row else ""


def _upsert_signal(name: str, value: str, touch: bool = True, at: str = "") -> None:
    conn = connect()
    try:
        stamp = at or now_ts()
        if touch:
            conn.execute(
                "INSERT INTO atk_signal(name, value, at) VALUES (?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET value=excluded.value, at=excluded.at",
                (name, value, stamp))
        else:
            conn.execute(
                "INSERT INTO atk_signal(name, value, at) VALUES (?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET value=excluded.value",
                (name, value, stamp))
        conn.commit()
    finally:
        conn.close()


def _delete_signal(name: str) -> None:
    conn = connect()
    try:
        conn.execute("DELETE FROM atk_signal WHERE name=?", (name,))
        conn.commit()
    finally:
        conn.close()


def _read_named_proc(name: str) -> tuple:
    """返回 (内容, 心跳时间, 是否在线, 这一行在不在)。"""
    conn = connect(readonly=True)
    try:
        row = conn.execute(
            "SELECT value, at FROM atk_signal WHERE name=?", (name,)).fetchone()
    finally:
        conn.close()
    if not row:
        return {}, "", False, False
    parsed = _proc_payload(row[0])
    phase = parsed.get("phase") or "offline"
    seen = row[1] or ""
    online = False
    if seen and phase != "offline":
        try:
            dt = datetime.strptime(seen, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc)
            online = (datetime.now(timezone.utc) - dt).total_seconds() <= 25
        except ValueError:
            online = False
    return parsed, seen, online, True


def _read_user_proc(user_id: int) -> tuple:
    """这个登录账号自己的攻打 QQ 的进程。不读别人的。"""
    user_id = int(user_id or 0)
    name = _mark_name("proc", user_id) if user_id else "proc"
    parsed, seen, online, found = _read_named_proc(name)
    if user_id:
        alt_name = f"proc:user:{user_id}"
        if alt_name != name:
            alt, alt_seen, alt_online, alt_found = _read_named_proc(alt_name)
            if alt_found and alt_online and not online:
                return alt, alt_seen, True
    if found:
        return parsed, seen, online
    if name == "proc" or not user_id:
        return {}, "", False
    parsed, seen, online, found = _read_named_proc("proc")
    if not found:
        return {}, "", False
    try:
        owner = int(parsed.get("user") or 0)
    except (TypeError, ValueError):
        owner = 0
    acct = str(parsed.get("acct") or "").strip()
    uin = attack_qq_of(user_id)
    if owner == user_id or (uin and acct == uin):
        return parsed, seen, online
    return {}, "", False


def _read_proc() -> tuple:
    """返回这条线程对应的 (内容, 心跳时间, 是否在线)。"""
    uid = attack_context_user()
    if uid:
        return _read_user_proc(uid)
    parsed, seen, online, _found = _read_named_proc("proc")
    return parsed, seen, online


def proc_online(user_id: int = 0) -> bool:
    uid = int(user_id or attack_context_user() or 0)
    if not uid:
        return False
    return _read_user_proc(uid)[2]


def proc_user() -> int:
    """这条线程的攻打进程属于哪个登录账号。没在跑就是 0。"""
    parsed, _seen, online = _read_proc()
    if not online:
        return 0
    try:
        return int(parsed.get("user") or attack_context_user() or 0)
    except (TypeError, ValueError):
        return 0


def proc_acct() -> str:
    parsed, _seen, online = _read_proc()
    if not online:
        return ""
    return str(parsed.get("acct") or attack_context_qq() or "")


def proc_phase() -> str:
    parsed, _seen, online = _read_proc()
    if not online:
        return "offline"
    return str(parsed.get("phase") or "offline")


def login_for() -> int:
    """这次扫码要绑到哪个登录账号。没有另外指定就是 0。"""
    conn = connect(readonly=True)
    try:
        row = conn.execute(
            "SELECT value FROM atk_signal WHERE name='login_for'").fetchone()
    finally:
        conn.close()
    if not row:
        return 0
    try:
        return int(row[0] or 0)
    except (TypeError, ValueError):
        return 0


def set_login_for(user_id: int) -> None:
    """这次攻打 QQ 记到哪个登录账号。只由这个人自己的推送登录来写，不按订单去改。"""
    user_id = int(user_id)
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO atk_signal(name, value, at) VALUES ('login_for', ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET value=excluded.value, at=excluded.at",
            (str(user_id), now_ts()))
        conn.commit()
    finally:
        conn.close()


def hand_login_to(user_id: int) -> None:
    """还没绑定攻打 QQ，把这次扫码交给当前点推送的登录账号。正在等扫码时，二维码也转到他的页面上。"""
    user_id = int(user_id)
    set_login_for(user_id)
    set_attack_context(user_id, attack_qq_of(user_id))
    phase = proc_phase()
    if phase in ("", "offline"):
        phase = "login"
    set_attack_status(phase)
    if phase == "login":
        set_page_qr(True)


def clear_login_for() -> None:
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO atk_signal(name, value, at) VALUES ('login_for', '0', ?) "
            "ON CONFLICT(name) DO UPDATE SET value='0', at=excluded.at",
            (now_ts(),))
        conn.commit()
    finally:
        conn.close()


def set_attack_status(phase: str, task: str = "", note: str = "") -> None:
    """这条攻打线程把自己的阶段写进库，键是攻打 QQ。网页只读。所在城市留着。"""
    name = _mark_name("proc")
    prev = _proc_payload(_signal_value(name))
    data = {"phase": phase}
    if phase == "daily":
        label = " ".join(str(task or "").split())[:40]
        if label:
            data["task"] = label
        text = " ".join(str(note or "").split())[:180]
        if text:
            data["note"] = text
    here = _kept_here(prev)
    if here:
        data["here"] = here
    for key in ("morale", "power"):
        val = _kept_int(prev, key)
        if val is not None:
            data[key] = val
    user_id = attack_context_user()
    qq = attack_context_qq()
    if user_id:
        data["user"] = user_id
    if qq:
        data["acct"] = qq
    if phase != "offline":
        # 登录中是扫码或重连窗口，不能把上一轮心跳留下，否则页面会当成已经保活。
        if phase != "login":
            link = prev.get("link")
            gap = prev.get("gap")
            if isinstance(link, int) and not isinstance(link, bool) and link > 0:
                data["link"] = link
            if isinstance(gap, int) and not isinstance(gap, bool) and gap > 0:
                data["gap"] = gap
        note = str(prev.get("move_note") or "").strip()
        if note:
            data["move_note"] = note
    _upsert_signal(name, json.dumps(data, ensure_ascii=False))


def attack_here_id() -> int:
    """这个攻打 QQ 上次记下的所在城市。没读到是 0。"""
    parsed, _seen, _online = _read_proc()
    return _kept_here(parsed)


def note_attack_here(city_id, power=None, morale=None) -> None:
    """记下这个攻打 QQ 当前所在城市，以及面板上的体力和士气。

    页面上的体力就是国战行动力。没读到的那一项留着上次的数。
    库写失败不影响正在打的那一单。进程已停则不改。
    """
    try:
        cid = int(city_id or 0)
    except (TypeError, ValueError):
        cid = 0
    got_city = not isinstance(city_id, bool) and cid > 0
    got_power = isinstance(power, int) and not isinstance(power, bool)
    got_morale = isinstance(morale, int) and not isinstance(morale, bool)
    if not got_city and not got_power and not got_morale:
        return
    if not attack_context_user() and not attack_context_qq():
        return
    name = _mark_name("proc")
    try:
        data = _proc_payload(_signal_value(name))
        if not data or data.get("phase") == "offline":
            return
        if got_city:
            data["here"] = cid
        if got_power:
            data["power"] = int(power)
        if got_morale:
            data["morale"] = int(morale)
        _upsert_signal(name, json.dumps(data, ensure_ascii=False))
    except sqlite3.Error:
        return


def note_attack_move_text(text: str, user_id: int = 0) -> None:
    """页面上的「目前在」旁边显示这次移动的结果。进程已停则不改。"""
    user_id = int(user_id or attack_context_user() or 0)
    if not user_id and not attack_context_qq():
        return
    name = _mark_name("proc", user_id) if user_id else _mark_name("proc")
    try:
        data = _proc_payload(_signal_value(name))
        if not data or data.get("phase") == "offline":
            return
        data["move_note"] = " ".join(str(text or "").split())[:160]
        _upsert_signal(name, json.dumps(data, ensure_ascii=False))
    except sqlite3.Error:
        return


def request_attack_move(user_id: int, city_id) -> str:
    """让这条攻打线程移动到指定城。只走路，不打那座城。成功返回空字符串。"""
    user_id = int(user_id)
    try:
        cid = int(str(city_id).strip())
    except (TypeError, ValueError, AttributeError):
        return "城市不对"
    if cid <= 0:
        return "要选移动到哪座城"
    label = city_name(cid)
    if not label:
        return "这座城不在目录里"
    if shutdown_due(user_id):
        return "已定时关闭，不再执行任务"
    if not attack_qq_of(user_id):
        return "还没绑定攻打 QQ"
    status = attack_status(user_id)
    if not status.get("online"):
        return "攻打没在跑，不能移动"
    if status.get("paused"):
        return "攻打暂停着，先点继续再移动"
    where = f"{cid} {label}"
    if status.get("phase") == "running":
        note = f"打完这一单就移动到 {where}"
    else:
        note = f"开始移动到 {where}"
    _upsert_signal(_mark_name("move", user_id), str(cid))
    note_attack_move_text(note, user_id)
    return ""


def attack_move_pending(user_id: int = 0) -> int:
    """这条攻打线程还没开始的移动目标。没有返回 0。"""
    user_id = int(user_id or attack_context_user() or 0)
    if not user_id or (shutdown_due(user_id) and shutdown_covers_context()):
        return 0
    raw = _signal_value(_mark_name("move", user_id)).strip()
    try:
        cid = int(raw or 0)
    except ValueError:
        return 0
    return cid if cid > 0 else 0


def take_attack_move() -> int:
    """领走这次移动。领过就不再走第二次。"""
    user_id = attack_context_user()
    cid = attack_move_pending(user_id)
    if not cid:
        return 0
    _delete_signal(_mark_name("move", user_id))
    return cid


def note_attack_link(interval: float) -> None:
    """这个攻打 QQ 的游戏心跳刚发出去。挂机时用这个判断连接是不是真的还在。

    扫码或重连还停在登录中时，心跳成功就切到挂机。日常、攻打那些阶段原样留着。
    """
    if not attack_context_user() and not attack_context_qq():
        return
    name = _mark_name("proc")
    try:
        raw = _signal_value(name)
        data = _proc_payload(raw) if raw else {}
        if data.get("phase") == "offline":
            return
        if not data:
            uid = attack_context_user()
            acct = attack_context_qq() or (attack_qq_of(uid) if uid else "")
            if uid:
                data["user"] = uid
            if acct:
                data["acct"] = acct
        data["link"] = int(time.time())
        try:
            gap = int(float(interval))
        except (TypeError, ValueError):
            gap = 0
        if gap > 0:
            data["gap"] = gap
        phase = str(data.get("phase") or "")
        if phase in ("login", "idle", ""):
            data["phase"] = "hold"
        _upsert_signal(name, json.dumps(data, ensure_ascii=False), touch=False)
    except sqlite3.Error:
        return


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


def clear_attack_link() -> None:
    """游戏连接已经断开。留下的心跳时间不能再当成还在保活。"""
    if not attack_context_user() and not attack_context_qq():
        return
    name = _mark_name("proc")
    try:
        raw = _signal_value(name)
        if not raw:
            return
        data = _proc_payload(raw)
        if not data or "link" not in data:
            return
        data.pop("link", None)
        _upsert_signal(name, json.dumps(data, ensure_ascii=False), touch=False)
    except sqlite3.Error:
        return


def attack_link_alive(user_id: int = 0) -> bool:
    """游戏心跳还新鲜。不看阶段，登录中刚连上了也算。"""
    user_id = int(user_id or attack_context_user() or 0)
    if not user_id:
        return False
    parsed, _seen, online = _read_user_proc(user_id)
    if not online:
        return False
    return _hold_link_ok(parsed)


def attack_in_keepalive(user_id: int = 0) -> bool:
    """游戏连接还在，才能继续和接订单。进程心跳还在、游戏没连上，不算。

    登录中只是扫码或重连窗口，即使上一轮心跳还在也不接订单。
    """
    user_id = int(user_id or attack_context_user() or 0)
    if not user_id:
        return False
    parsed, _seen, online = _read_user_proc(user_id)
    if not online:
        return False
    phase = str(parsed.get("phase") or "")
    if phase not in ("hold", "paused", "running", "daily", "queue"):
        return False
    return _hold_link_ok(parsed)


def touch_attack_status() -> None:
    """这条攻打线程还活着就刷新时间。停掉之后不再把「没在跑」刷成在线。"""
    if not attack_context_user() and not attack_context_qq():
        return
    name = _mark_name("proc")
    conn = connect()
    try:
        row = conn.execute(
            "SELECT value FROM atk_signal WHERE name=?", (name,)).fetchone()
        if not row:
            return
        try:
            phase = json.loads(row[0] or "{}").get("phase")
        except json.JSONDecodeError:
            return
        if phase == "offline":
            return
        conn.execute(
            "UPDATE atk_signal SET at=? WHERE name=?", (now_ts(), name))
        conn.commit()
    finally:
        conn.close()


def attack_cookie_logged_in(user_id: int) -> bool:
    """这个攻打 QQ 的本地票据里还有没过期的 skey。不访问游戏。"""
    uin = attack_qq_of(int(user_id or 0))
    if not str(uin or "").isdigit():
        return False
    path = user_path(f"accounts/qq-{uin}.json")
    try:
        with open(path, encoding="utf-8") as f:
            jar = json.load(f)
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(jar, list):
        return False
    now = time.time()
    for item in jar:
        if not isinstance(item, dict) or item.get("name") != "skey" or not item.get("value"):
            continue
        exp = item.get("expires")
        if exp in (None, "", 0):
            return True
        try:
            if float(exp) > now:
                return True
        except (TypeError, ValueError):
            return True
    return False


def _queued_attack_text(row) -> str:
    """进程还写着空闲时，队列里已经有单。页面按这张单说，不再说空闲。"""
    if not row:
        return ""
    city_id = int(row[0] or 0)
    uid = str(row[1] or "").strip()
    status = str(row[2] or "")
    kind = str(row[3] or "")
    reason = " ".join(str(row[4] or "").split())
    if kind == "modo":
        where = "摩多军团"
    else:
        name = city_name(city_id)
        where = f"{city_id} {name}".strip() if name else str(city_id)
    if status == "running":
        if uid:
            return f"正在打城市 {city_id} 的 {uid}"
        if kind == "modo":
            return "正在打摩多军团"
        return f"正在清城市 {city_id}"
    if status == "blocked":
        head = f"有订单在等通路：{where}"
        return f"{head}，{reason}" if reason else head
    if status == "wait":
        return f"有订单等再打：{where}"
    head = f"有订单在排队：{where}"
    if uid:
        return f"{head} 的 {uid}"
    return head


def attack_status(user_id: int) -> dict:
    """给页面看这个登录账号自己的攻打 QQ。超过 25 秒没心跳就当没在跑。"""
    user_id = int(user_id)
    uin = attack_qq_of(user_id)
    parsed, seen, online = _read_user_proc(user_id)
    phase = parsed.get("phase") or "offline"
    here_id = _kept_here(parsed)
    conn = connect(readonly=True)
    try:
        own = conn.execute(
            "SELECT city_id, IFNULL(uid,'') FROM atk_order "
            "WHERE user_id=? AND status='running' ORDER BY id DESC LIMIT 1",
            (user_id,)).fetchone()
        waiting = conn.execute(
            "SELECT 1 FROM atk_order WHERE user_id=? AND status='blocked' LIMIT 1",
            (user_id,)).fetchone()
        latest = conn.execute(
            "SELECT status, IFNULL(reason,'') FROM atk_order "
            "WHERE user_id=? ORDER BY id DESC LIMIT 1",
            (user_id,)).fetchone()
        queued = conn.execute(
            "SELECT city_id, IFNULL(uid,''), status, IFNULL(kind,''), IFNULL(reason,'') "
            "FROM atk_order WHERE user_id=? "
            "AND status IN ('pending','running','blocked','wait') "
            "ORDER BY id DESC LIMIT 1",
            (user_id,)).fetchone()
    finally:
        conn.close()
    blocked = attack_qq_blocked(user_id)
    paused = blocked or attack_paused(user_id)
    hold_left = attack_hold_left(user_id)
    always = attack_hold_always(user_id)

    def hold_note(text):
        if always:
            return f"{text}，全天候"
        if hold_left is not None and hold_left > 0:
            return f"{text}，还剩 {(hold_left + 59) // 60} 分钟"
        return text

    page_on = _signal_value(_mark_name("pageqr", user_id)) == "1"
    show_qr = bool(
        page_login_path(user_id)
        or (online and phase == "login"
            and (page_on or login_qr_path(user_id))))

    keeping = (
        phase in ("hold", "paused", "running", "daily", "queue")
        and _hold_link_ok(parsed))

    def pack(row: dict) -> dict:
        row = dict(row)
        row["qq"] = uin
        row.setdefault("need_login", False)
        row.setdefault("keepalive", False)
        row = _with_page_qr(row, user_id)
        row["qr_at"] = login_qr_at(user_id) if row.get("qr") else ""
        return row

    mismatch = pack({"online": False, "phase": "offline", "detail": QQ_MISMATCH,
                     "seen_at": beijing_ts(seen), "qr": False, "here": "",
                     "paused": True, "hold_left": None})
    if shutdown_due(user_id):
        return pack({"online": False, "phase": "offline", "detail": "已定时关闭",
                     "seen_at": "", "qr": False, "here": "",
                     "paused": False, "hold_left": None, "shutdown": True,
                     "keepalive": False, "need_login": False})
    if not online:
        # 进程已经停了。暂停只对还在跑的进程有意义。QQ 对不上的暂停留着。
        if not blocked and attack_paused(user_id):
            set_attack_paused(False, user_id)
        if blocked:
            return mismatch
        return pack({"online": False, "phase": "offline", "detail": "没在跑",
                     "seen_at": beijing_ts(seen), "qr": False, "here": "",
                     "paused": False, "hold_left": None})
    if paused and phase != "login":
        if blocked:
            detail = QQ_MISMATCH
        elif _hold_link_ok(parsed):
            detail = "已暂停"
        else:
            detail = "已暂停，不在保活，先连上再继续"
    elif phase == "login":
        detail = hold_note("登录中")
    elif phase == "daily":
        label = str(parsed.get("task") or "").strip()
        detail = f"正在做{label}" if label else "正在做日常任务"
        note = " ".join(str(parsed.get("note") or "").split())
        if note:
            detail = f"{detail}，{note}"
    elif phase == "hold":
        if _hold_link_ok(parsed):
            detail = hold_note("保活中")
            if waiting:
                detail = f"{detail}，正在看路径"
        else:
            detail = hold_note("登录中")
    elif phase == "running" and own and str(own[1] or "").strip():
        detail = f"正在打城市 {own[0]} 的 {own[1]}"
    elif phase == "running" and own:
        detail = f"正在清城市 {own[0]}"
    elif phase == "running":
        detail = "正在执行订单"
    else:
        if not _hold_link_ok(parsed):
            detail = hold_note("登录中") if ((hold_left or 0) > 0 or always) else "不在保活，先连上再接订单"
        else:
            detail = _queued_attack_text(queued)
            if not detail:
                detail = "空闲，等订单"
                if latest and latest[0] == "failed" and latest[1]:
                    detail = f"空闲。上一单没打成：{latest[1]}"
        if uin and not attack_cookie_logged_in(user_id):
            detail = "登录已失效，请重新扫码"
    here = ""
    if here_id:
        name = city_name(here_id)
        here = f"{here_id} {name}".strip() if name else str(here_id)
    show_hold = (not paused and phase == "hold" and not always
                 and hold_left is not None and hold_left > 0)
    return pack({"online": True, "phase": phase, "detail": detail,
                 "seen_at": beijing_ts(seen), "qr": show_qr, "here": here,
                 "morale": _kept_int(parsed, "morale"),
                 "power": _kept_int(parsed, "power"),
                 "paused": paused, "keepalive": keeping,
                 "hold_left": int(hold_left) if show_hold else None,
                 "move_note": str(parsed.get("move_note") or ""),
                 "need_login": bool(
                     uin and phase not in ("login", "running", "hold", "daily")
                     and not paused and not attack_cookie_logged_in(user_id))})


def list_attack_fighters() -> list:
    """每个已绑定的攻打 QQ 现在在干什么。主进程用这个看全部线程。"""
    rows = []
    for user_id in mapped_attack_user_ids():
        status = attack_status(user_id)
        rows.append({
            "user_id": user_id,
            "username": username_of(user_id),
            "qq": status.get("qq") or attack_qq_of(user_id),
            "cookie": f"accounts/qq-{status.get('qq') or attack_qq_of(user_id)}.json",
            "online": status.get("online"),
            "phase": status.get("phase"),
            "detail": status.get("detail"),
            "here": status.get("here") or "",
            "paused": status.get("paused"),
        })
    return rows


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


def ask_attack_login(user_id: int = 0) -> None:
    """让这个攻打 QQ 自己的线程去推二维码。不碰别的 QQ。"""
    _upsert_signal(_mark_name("login", user_id), "1")


def note_attack_logging_in(user_id: int = 0) -> None:
    """点了推送登录或刚拉起线程，游戏还没连上。页面先显示登录中。

    已经挂上游戏的不改，免得把保活中盖成登录中。
    """
    user_id = int(user_id or attack_context_user() or 0)
    if not user_id:
        return
    if attack_in_keepalive(user_id):
        return
    name = _mark_name("proc", user_id)
    prev = _proc_payload(_signal_value(name))
    phase = str(prev.get("phase") or "")
    if phase in ("running", "daily") and _hold_link_ok(prev):
        return
    data = dict(prev) if prev else {}
    data["phase"] = "login"
    data.pop("link", None)
    data["user"] = user_id
    uin = attack_qq_of(user_id)
    if uin:
        data["acct"] = uin
    _upsert_signal(name, json.dumps(data, ensure_ascii=False))


def take_attack_login() -> bool:
    name = _mark_name("login")
    if _signal_value(name) != "1":
        return False
    _upsert_signal(name, "0")
    return True


def attack_paused(user_id: int = 0) -> bool:
    """这个登录账号自己的攻打 QQ 是不是被暂停了。"""
    user_id = int(user_id or attack_context_user() or 0)
    if not user_id:
        return False
    value = _signal_value(_mark_name("pause", user_id))
    if value in ("1", "true"):
        return True
    legacy = _signal_value("pause")
    return bool(user_id) and legacy == str(user_id)


def set_attack_paused(on: bool, user_id: int = 0) -> bool:
    """暂停或继续这个登录账号的攻打 QQ。别的 QQ 的标记不动。"""
    user_id = int(user_id or attack_context_user() or 0)
    if not user_id:
        return False
    name = _mark_name("pause", user_id)
    if on:
        _upsert_signal(name, "1")
        return True
    if _signal_value(name) in ("", "0") and _signal_value("pause") != str(user_id):
        return True
    _upsert_signal(name, "0")
    if _signal_value("pause") == str(user_id):
        _upsert_signal("pause", "0")
    return True


def pause_attack_for(user_id: int, on: bool) -> str:
    """网页上的暂停和继续。只动这个登录账号自己的攻打 QQ。"""
    user_id = int(user_id)
    if not on and attack_qq_blocked(user_id):
        return QQ_MISMATCH
    if not attack_qq_of(user_id):
        return "这个登录账号还没有攻打 QQ"
    if not attack_in_keepalive(user_id):
        if on:
            return "不在保活，先连上再暂停"
        return "不在保活，不能继续接订单"
    if not set_attack_paused(on, user_id):
        return "这个登录账号还没有攻打 QQ"
    return ""


def users_with_open_orders() -> list:
    conn = connect(readonly=True)
    try:
        rows = conn.execute(
            "SELECT DISTINCT user_id FROM atk_order "
            "WHERE status IN ('pending','running','blocked','wait')").fetchall()
    finally:
        conn.close()
    return [int(r[0]) for r in rows]


DAILY_KIND_LABEL = {
    "daily": "日常任务",
    "pve": "征战世界",
    "fund": "成就拨款",
}
_DAILY_STATUS = {
    "pending": "排队",
    "running": "正在做",
    "done": "做完",
    "failed": "没做成",
    "ended": "已关停",
}


def users_with_daily_jobs() -> list:
    conn = connect(readonly=True)
    try:
        rows = conn.execute(
            "SELECT DISTINCT user_id FROM daily_job "
            "WHERE status IN ('pending','running')").fetchall()
    finally:
        conn.close()
    return [int(r[0]) for r in rows]


def users_with_running_orders() -> list:
    """手动订单还标着正在打。服务器停在这一下时，起来要登录接着打。"""
    conn = connect(readonly=True)
    try:
        rows = conn.execute(
            "SELECT DISTINCT user_id FROM atk_order "
            "WHERE status='running' AND IFNULL(auto,0)=0").fetchall()
    finally:
        conn.close()
    return [int(r[0]) for r in rows]


def manual_orders_running(user_id: int = 0) -> bool:
    """这个登录账号有没有还在打的手动订单。没指定时看这条线程的攻打 QQ。"""
    user_id = int(user_id or 0)
    ids = executable_users([user_id] if user_id else None)
    if not ids:
        return False
    slot = ",".join("?" * len(ids))
    conn = connect(readonly=True)
    try:
        row = conn.execute(
            "SELECT 1 FROM atk_order WHERE user_id IN (" + slot + ") AND status='running' "
            "AND IFNULL(auto,0)=0 LIMIT 1",
            tuple(ids)).fetchone()
    finally:
        conn.close()
    return row is not None


def mark_attack_resume(user_id: int = 0) -> None:
    """记下这一单要在进程重新拉起后续打。改回排队之后标记还在，失败了也能再登。"""
    user_id = int(user_id or attack_context_user() or 0)
    if not user_id:
        return
    _upsert_signal(_mark_name("resume", user_id), "1")


def attack_resume_marked(user_id: int = 0) -> bool:
    user_id = int(user_id or attack_context_user() or 0)
    if not user_id:
        return False
    return _signal_value(_mark_name("resume", user_id)) == "1"


def clear_attack_resume(user_id: int = 0) -> None:
    user_id = int(user_id or attack_context_user() or 0)
    if not user_id:
        return
    _delete_signal(_mark_name("resume", user_id))


def users_marked_resume() -> list:
    conn = connect(readonly=True)
    try:
        rows = conn.execute(
            "SELECT name FROM atk_signal WHERE value='1' AND name LIKE 'resume:%'"
        ).fetchall()
    finally:
        conn.close()
    ids = []
    for (name,) in rows:
        tail = str(name or "").split(":", 1)[1]
        if tail.startswith("user:"):
            try:
                ids.append(int(tail[5:]))
            except ValueError:
                continue
            continue
        ids.extend(users_bound_to_qq(tail))
    return ids


def users_needing_attack() -> list:
    """主进程要照看的登录账号：还有日常，挂机还没结束，开了全天候，或有一单正在打。

    只是排队、还没开打的订单不拉起。没挂在游戏上就去登录，游戏那边进不去。
    服务器停前正在打的那一单还标着执行中，起来要重新登录接着打。
    定时关闭已经到点的账号不拉起。
    """
    ids = set(users_with_daily_jobs())
    ids.update(users_with_running_orders())
    ids.update(users_marked_resume())
    for user_id in mapped_attack_user_ids():
        left = attack_hold_left(user_id)
        if left is not None and left > 0:
            ids.add(user_id)
    conn = connect(readonly=True)
    try:
        rows = conn.execute(
            "SELECT id, IFNULL(tier,'初级'), IFNULL(expires_at,'') "
            "FROM app_user WHERE IFNULL(hold_all,0)!=0"
        ).fetchall()
    finally:
        conn.close()
    for user_id, tier, expires in rows:
        if attack_tier(tier) and not account_expired(expires):
            ids.add(int(user_id))
    return sorted(uid for uid in ids if not shutdown_due(uid))


def daily_job_open() -> bool:
    """这个攻打 QQ 上还有没做完的日常。定时关闭到点的账号不算。"""
    ids = executable_users()
    if not ids:
        return False
    slot = ",".join("?" * len(ids))
    conn = connect(readonly=True)
    try:
        row = conn.execute(
            "SELECT 1 FROM daily_job WHERE user_id IN (" + slot + ") "
            "AND status IN ('pending','running') LIMIT 1",
            tuple(ids)).fetchone()
    finally:
        conn.close()
    return bool(row)


def _saved_daily_switches(conn, user_id: int):
    """这个账号自己存过的开关。没存过返回 None。"""
    row = conn.execute(
        "SELECT daily_switch FROM app_user WHERE id=?",
        (int(user_id),)).fetchone()
    if not row or not str(row[0] or "").strip():
        return None
    try:
        data = json.loads(row[0])
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    return data


def daily_switches(user_id: int, defaults: dict) -> dict:
    """这个登录账号的每日任务开关。没改过的项用服务器配置。"""
    defaults = defaults if isinstance(defaults, dict) else {}
    base = {}
    for key, val in defaults.items():
        name = str(key or "")
        if not name or name.startswith("_"):
            continue
        base[name] = bool(val)
    conn = connect(readonly=True)
    try:
        saved = _saved_daily_switches(conn, user_id)
    finally:
        conn.close()
    if not saved:
        from . import daily
        for key in daily.HELD_TASKS:
            if key in base:
                base[key] = False
        return base
    for key, val in saved.items():
        name = str(key or "")
        if not name or name.startswith("_"):
            continue
        base[name] = bool(val)
    from . import daily
    for key in daily.HELD_TASKS:
        if key in base:
            base[key] = False
    return base


def set_daily_switch(user_id: int, key: str, on, known, defaults: dict) -> str:
    """记下一项开关。第一次保存时把当前看到的开关整份记下。"""
    key = str(key or "").strip()
    allowed = []
    for item in known or []:
        name = str(item or "").strip()
        if name and not name.startswith("_") and name not in allowed:
            allowed.append(name)
    if key not in allowed:
        return "没有这项任务"
    from . import daily
    if key in daily.HELD_TASKS and bool(on):
        return f"{key}暂不执行，反复被拒会打断连接"
    current = daily_switches(user_id, defaults)
    stored = {}
    for name in allowed:
        if name in current:
            stored[name] = bool(current[name])
        else:
            stored[name] = bool((defaults or {}).get(name))
    stored[key] = bool(on)
    conn = connect()
    try:
        saved = _saved_daily_switches(conn, user_id) or {}
        for name, val in saved.items():
            if str(name).startswith("_") and str(name) not in stored:
                stored[str(name)] = val
        cur = conn.execute(
            "UPDATE app_user SET daily_switch=? WHERE id=?",
            (json.dumps(stored, ensure_ascii=False), int(user_id)))
        conn.commit()
        if cur.rowcount != 1:
            return "账号不存在"
        return ""
    finally:
        conn.close()


_CAMPAIGN_END = "_征战终点"


def campaign_stages(user_id: int) -> str:
    """这个登录账号记下的征战终点。空字符串表示没填。"""
    conn = connect(readonly=True)
    try:
        saved = _saved_daily_switches(conn, user_id) or {}
    finally:
        conn.close()
    return str(saved.get(_CAMPAIGN_END) or "").strip()


def daily_schedule(user_id: int) -> str:
    """这个登录账号记下的定时。空字符串表示不定时。"""
    conn = connect(readonly=True)
    try:
        row = conn.execute(
            "SELECT IFNULL(daily_at,'') FROM app_user WHERE id=?",
            (int(user_id),)).fetchone()
    finally:
        conn.close()
    return str(row[0] or "").strip() if row else ""


def set_daily_schedule(user_id: int, raw) -> str:
    """记下每天跑日常的北京时间。空的表示关掉。成功返回空字符串。

    今天这一档已经排过，又改到还没到的时刻，就清掉标记，让新时刻再排一次。
    改到已经过去的时刻则把新时刻记成做过，避免马上再跑一轮。
    """
    try:
        at = _clock(raw)
    except ValueError as exc:
        return str(exc)
    conn = connect()
    try:
        row = conn.execute(
            "SELECT IFNULL(daily_last,'') FROM app_user WHERE id=?",
            (int(user_id),)).fetchone()
        if not row:
            return "账号不存在"
        old_last = str(row[0] or "")
        done_day, sep, done_at = old_last.partition("|")
        ran_today = bool(old_last) and done_day == beijing_day()
        clock = beijing_clock()
        last = old_last
        if at and ran_today and at >= clock and (not sep or done_at != at):
            last = ""
        elif at and ran_today and sep and done_at != at and at < clock:
            last = f"{done_day}|{at}"
        conn.execute(
            "UPDATE app_user SET daily_at=?, daily_last=? WHERE id=?",
            (at, last, int(user_id)))
        conn.commit()
        return ""
    finally:
        conn.close()


def enqueue_due_daily_jobs() -> list:
    """到点且这一档今天还没排过的账号，排一轮日常。返回刚排上的 user_id。"""
    clock = beijing_clock()
    day = beijing_day()
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT id, username, daily_at, IFNULL(daily_last,''), IFNULL(expires_at,''), "
            "IFNULL(attack_qq,''), daily_switch "
            "FROM app_user WHERE IFNULL(daily_at,'') != ''").fetchall()
        queued = []
        for user_id, name, at, last, expires, qq, switch_raw in rows:
            done_day, sep, done_at = str(last or "").partition("|")
            if ((done_day == day and (not sep or done_at == at))
                    or clock < at or account_expired(expires)
                    or shutdown_due(user_id)):
                continue
            if not str(qq or "").strip():
                continue
            waiting = conn.execute(
                "SELECT COUNT(*) FROM daily_job "
                "WHERE user_id=? AND status IN ('pending','running')",
                (user_id,)).fetchone()[0]
            if int(waiting or 0) >= 3:
                continue
            daily_wait = conn.execute(
                "SELECT created_at, params FROM daily_job "
                "WHERE user_id=? AND kind='daily' AND status IN ('pending','running') "
                "ORDER BY id DESC LIMIT 1",
                (user_id,)).fetchone()
            if daily_wait:
                created = beijing_ts(daily_wait[0] or "")[:10]
                if created == day:
                    job_at = ""
                    try:
                        queued_params = json.loads(daily_wait[1] or "{}")
                    except ValueError:
                        queued_params = None
                    if isinstance(queued_params, dict):
                        job_at = str(queued_params.get("at") or "")
                    # 手动排的、或就是这一档，记上。改点后旧档还在做，别把新时刻写成已做。
                    if not job_at or job_at == at:
                        conn.execute(
                            "UPDATE app_user SET daily_last=? WHERE id=?",
                            (f"{day}|{at}", user_id))
                continue
            stages = ""
            if switch_raw:
                try:
                    saved = json.loads(switch_raw)
                except ValueError:
                    saved = None
                if isinstance(saved, dict):
                    stages = str(saved.get(_CAMPAIGN_END) or "").strip()
            clean = {"at": at}
            if stages:
                clean["stages"] = stages
            now = now_ts()
            conn.execute(
                "INSERT INTO daily_job(user_id, kind, params, status, detail, created_at, updated_at) "
                "VALUES (?,'daily',?,'pending','',?,?)",
                (user_id, json.dumps(clean, ensure_ascii=False), now, now))
            conn.execute(
                "UPDATE app_user SET daily_last=? WHERE id=?",
                (f"{day}|{at}", user_id))
            queued.append(int(user_id))
            log.info("定时日常：账号 %s 到点 %s，已排队", name, at)
        conn.commit()
        return queued
    finally:
        conn.close()


def set_campaign_stages(user_id: int, raw) -> str:
    """记下征战终点。空的表示清除。成功返回空字符串。"""
    text = str(raw or "").strip()
    if len(text) > 80:
        return "关卡名单太长"
    if text:
        from . import pve
        try:
            end = pve.end_stage(text)
        except ValueError as exc:
            return str(exc)
        if not end:
            return "关卡名单是空的"
    conn = connect()
    try:
        saved = _saved_daily_switches(conn, user_id) or {}
        if text:
            saved[_CAMPAIGN_END] = text
        else:
            saved.pop(_CAMPAIGN_END, None)
        cur = conn.execute(
            "UPDATE app_user SET daily_switch=? WHERE id=?",
            (json.dumps(saved, ensure_ascii=False), int(user_id)))
        conn.commit()
        if cur.rowcount != 1:
            return "账号不存在"
        return ""
    finally:
        conn.close()


def enqueue_daily_job(user_id: int, kind: str, params=None) -> str:
    """排一项日常。成功返回空字符串。只排给这个登录账号绑定的攻打号。"""
    user_id = int(user_id)
    kind = str(kind or "").strip()
    if kind not in DAILY_KIND_LABEL:
        return "没有这项日常"
    if shutdown_due(user_id):
        return "已定时关闭，不再执行任务"
    if not attack_qq_of(user_id):
        return "还没绑定攻打 QQ"
    params = params if isinstance(params, dict) else {}
    clean = {}
    if kind in ("daily", "pve") and "stages" in params:
        why = set_campaign_stages(user_id, params.get("stages"))
        if why:
            return why
        clean["stages"] = str(params.get("stages") or "").strip()
    elif kind == "fund":
        try:
            building = int(str(params.get("building_id") or "").strip())
            times = int(str(params.get("times") or "1").strip())
        except (TypeError, ValueError):
            return "建筑和次数要是数字"
        if building <= 0:
            return "要填建筑 ID"
        if not (1 <= times <= 999):
            return "拨款次数要在 1 到 999"
        clean["building_id"] = building
        clean["times"] = times
    conn = connect()
    try:
        waiting = conn.execute(
            "SELECT COUNT(*) FROM daily_job WHERE user_id=? AND status IN ('pending','running')",
            (user_id,)).fetchone()[0]
        if int(waiting or 0) >= 3:
            return "还有日常没做完，先等这几项"
        now = now_ts()
        conn.execute(
            "INSERT INTO daily_job(user_id, kind, params, status, detail, created_at, updated_at) "
            "VALUES (?,?,?,'pending','',?,?)",
            (user_id, kind, json.dumps(clean, ensure_ascii=False), now, now))
        conn.commit()
        return ""
    finally:
        conn.close()


def claim_daily_job():
    """领这个攻打 QQ 上最早的一条日常。没有就返回 None。到点关闭的账号不领。"""
    ids = executable_users()
    if not ids:
        return None
    slot = ",".join("?" * len(ids))
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT id, kind, params, user_id FROM daily_job "
            "WHERE user_id IN (" + slot + ") AND status='pending' ORDER BY id LIMIT 1",
            tuple(ids)).fetchone()
        if not row:
            conn.rollback()
            return None
        conn.execute(
            "UPDATE daily_job SET status='running', updated_at=? WHERE id=?",
            (now_ts(), row[0]))
        conn.commit()
    finally:
        conn.close()
    try:
        params = json.loads(row[2] or "{}")
    except ValueError:
        params = {}
    if not isinstance(params, dict):
        params = {}
    return {"id": int(row[0]), "kind": str(row[1] or ""), "params": params,
            "user_id": int(row[3])}


def _job_detail(detail: str) -> str:
    """说明里的换行留着，同一行里的多余空白收掉。"""
    text = str(detail or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [" ".join(line.split()) for line in text.split("\n")]
    return "\n".join(line for line in lines if line)[:4000]


def touch_daily_job(job_id: int, detail: str) -> None:
    """日常还在做。把当前结果写进说明，状态仍是正在做。"""
    text = _job_detail(detail)
    if not text:
        return
    conn = connect()
    try:
        conn.execute(
            "UPDATE daily_job SET detail=?, updated_at=? WHERE id=? AND status='running'",
            (text, now_ts(), int(job_id)))
        conn.commit()
    finally:
        conn.close()


def finish_daily_job(job_id: int, status: str, detail: str) -> None:
    status = "done" if status == "done" else "failed"
    text = _job_detail(detail)
    conn = connect()
    try:
        conn.execute(
            "UPDATE daily_job SET status=?, detail=?, updated_at=? "
            "WHERE id=? AND status='running'",
            (status, text, now_ts(), int(job_id)))
        conn.commit()
    finally:
        conn.close()


def set_running_daily(job_id: int) -> None:
    """这条线程正在做的日常。关停后 daily_job_stopped 变成真。"""
    _attack_local.daily_job = int(job_id or 0)


def daily_job_stopped() -> bool:
    """网页已经关停当前这项。没有正在做的日常时不算停。"""
    job_id = int(getattr(_attack_local, "daily_job", 0) or 0)
    if not job_id:
        return False
    conn = connect(readonly=True)
    try:
        row = conn.execute(
            "SELECT status FROM daily_job WHERE id=?", (job_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return True
    return str(row[0] or "") != "running"


def cancel_daily_job(user_id: int, job_id: int) -> str:
    """手动关停这一项。正在做的会在下一轮动作里停手。已经结束的不动。"""
    user_id = int(user_id)
    job_id = int(job_id)
    conn = connect()
    try:
        row = conn.execute(
            "SELECT status FROM daily_job WHERE id=? AND user_id=?",
            (job_id, user_id)).fetchone()
        if not row:
            return "没有这项日常"
        if str(row[0] or "") not in ("pending", "running"):
            return "这项日常已经结束"
        conn.execute(
            "UPDATE daily_job SET status='ended', detail=?, updated_at=? "
            "WHERE id=? AND user_id=? AND status IN ('pending','running')",
            ("已手动关停", now_ts(), job_id, user_id))
        conn.commit()
        log.info("登录账号 %s 关停日常 %s", username_of(user_id), job_id)
        return ""
    finally:
        conn.close()


def resume_daily_jobs() -> None:
    """线程重新拉起。每日任务可以再做，次数闸门会挡住已经领过的。拨款和征战不自动重做。"""
    ids = attack_context_users()
    if not ids:
        return
    now = now_ts()
    slot = ",".join("?" * len(ids))
    conn = connect()
    try:
        conn.execute(
            "UPDATE daily_job SET status='pending', updated_at=? "
            "WHERE user_id IN (" + slot + ") AND kind='daily' AND status='running'",
            (now, *ids))
        conn.execute(
            "UPDATE daily_job SET status='failed', detail=?, updated_at=? "
            "WHERE user_id IN (" + slot + ") AND kind!='daily' AND status='running'",
            ("进程中断，请再点一次", now, *ids))
        conn.commit()
    finally:
        conn.close()


def list_daily_jobs(user_id: int, limit: int = 2) -> list:
    conn = connect(readonly=True)
    try:
        rows = conn.execute(
            "SELECT id, kind, status, detail, created_at FROM daily_job "
            "WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (int(user_id), int(limit))).fetchall()
    finally:
        conn.close()
    from . import daily
    out = []
    for row in rows:
        out.append({
            "id": int(row[0]),
            "kind": row[1],
            "label": DAILY_KIND_LABEL.get(row[1], row[1]),
            "status": _DAILY_STATUS.get(row[2], row[2]),
            "detail": daily.display_job_detail(row[2], row[3] or ""),
            "created_at": beijing_ts(row[4]) if row[4] else "",
        })
    return out


def attack_order_open() -> bool:
    """还有没打完的订单。攻打进程看这个 QQ 上的登录账号。网页没绑定时看全部，用来决定要不要拉起。"""
    raw = attack_context_users()
    ids = executable_users(raw)
    conn = connect(readonly=True)
    try:
        if raw and not ids:
            return False
        if ids:
            slot = ",".join("?" * len(ids))
            row = conn.execute(
                "SELECT 1 FROM atk_order WHERE user_id IN (" + slot + ") "
                "AND status IN ('pending','running') LIMIT 1",
                tuple(ids)).fetchone()
        else:
            row = conn.execute(
                "SELECT 1 FROM atk_order WHERE status IN ('pending','running') LIMIT 1"
            ).fetchone()
        return row is not None
    finally:
        conn.close()


def requeue_running_orders() -> None:
    """拿到攻打号之后调用。标着正在打的是上一轮进程留下的，改回排队。同一个 QQ 上的单都改。"""
    ids = attack_context_users()
    if not ids:
        return
    slot = ",".join("?" * len(ids))
    conn = connect()
    try:
        conn.execute(
            "UPDATE atk_order SET status='pending', updated_at=? "
            "WHERE status='running' AND user_id IN (" + slot + ") AND IFNULL(auto,0)=0",
            (now_ts(), *ids))
        conn.commit()
    finally:
        conn.close()


def requeue_blocked_orders() -> None:
    """等通路的订单改回排队。只在挂机已经结束、线程要重新领单时用。"""
    ids = attack_context_users()
    if not ids:
        return
    slot = ",".join("?" * len(ids))
    conn = connect()
    try:
        cur = conn.execute(
            "UPDATE atk_order SET status='pending', updated_at=? "
            "WHERE status='blocked' AND user_id IN (" + slot + ") AND IFNULL(auto,0)=0",
            (now_ts(), *ids))
        conn.commit()
        if cur.rowcount:
            who = "、".join(username_of(i) for i in ids)
            log.info("登录账号 %s 的攻打线程要继续，%d 条等通路的订单改回排队",
                     who, cur.rowcount)
    finally:
        conn.close()


def skip_unfinished_auto(user_id: int) -> int:
    """自动锁敌没打完的单直接结束。正在排队、还没领的留给下一次触发。"""
    user_id = int(user_id or 0)
    if not user_id:
        return 0
    conn = connect()
    try:
        cur = conn.execute(
            "UPDATE atk_order SET status='failed', "
            "reason=CASE WHEN TRIM(IFNULL(reason,''))!='' THEN reason "
            "ELSE '没打完，等下一次索敌' END, updated_at=? "
            "WHERE user_id=? AND IFNULL(auto,0)=1 AND status IN ('blocked','running')",
            (now_ts(), user_id))
        conn.commit()
        if cur.rowcount:
            log.info("登录账号 %s 有 %d 条自动锁敌没打完，已跳过，等下一次触发",
                     username_of(user_id), cur.rowcount)
        return int(cur.rowcount or 0)
    finally:
        conn.close()


def resume_stranded_orders() -> None:
    """线程拉起来接着干。自动锁敌没打完的跳过；手动单改回排队。"""
    for user_id in attack_context_users():
        skip_unfinished_auto(user_id)
    requeue_running_orders()
    if not attack_hold_on():
        requeue_blocked_orders()


def claim_attack_order(city_id=None, auto_only=False):
    """领这个攻打 QQ 上最新的一条排队订单。后提交的优先。没绑上的账号不领。

    city_id 有值时只领这座城的。auto_only 只领自动索敌，不领摩多。
    """
    ids = executable_users()
    if not ids:
        return None
    try:
        want_city = int(city_id or 0)
    except (TypeError, ValueError):
        want_city = 0
    slot = ",".join("?" * len(ids))
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        now = now_ts()
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=20)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        conn.execute(
            "UPDATE atk_order SET status='failed', "
            "reason=CASE WHEN TRIM(IFNULL(reason,''))!='' THEN reason "
            "ELSE '没打完，等下一次索敌' END, updated_at=? "
            "WHERE status='running' AND user_id IN (" + slot + ") "
            "AND updated_at<? AND IFNULL(auto,0)=1",
            (now, *ids, cutoff))
        conn.execute(
            "UPDATE atk_order SET status='pending', updated_at=? "
            "WHERE status='running' AND user_id IN (" + slot + ") "
            "AND updated_at<? AND IFNULL(auto,0)=0",
            (now, *ids, cutoff))
        extra = ""
        args = list(ids)
        if want_city > 0:
            extra += "AND o.city_id=? "
            args.append(want_city)
        if auto_only:
            extra += "AND IFNULL(o.auto,0)=1 AND IFNULL(o.kind,'')!=? "
            args.append("modo")
        while True:
            row = conn.execute(
                "SELECT o.id, o.city_id, IFNULL(o.uid,''), IFNULL(u.tier,'初级'), "
                "IFNULL(u.expires_at,''), IFNULL(o.auto,0), o.card_max, IFNULL(o.kind,''), "
                "o.user_id "
                "FROM atk_order o JOIN app_user u ON u.id=o.user_id "
                "WHERE o.status='pending' AND o.user_id IN (" + slot + ") "
                + extra +
                "ORDER BY o.id DESC LIMIT 1",
                tuple(args)).fetchone()
            if not row:
                conn.commit()
                return None
            owner = int(row[8])
            if account_expired(row[4]) or (str(row[7] or "") != "modo" and not attack_tier(row[3])):
                conn.execute(
                    "UPDATE atk_order SET status='failed', reason=?, updated_at=? WHERE id=?",
                    ("订阅档不够或账号已过期", now, row[0]))
                continue
            uid = str(row[2] or "").strip()
            if bool(row[5]) and uid and str(row[7] or "") != "modo":
                if _lock_gone_on(conn, owner, int(row[1]), uid):
                    conn.execute(
                        "UPDATE atk_order SET status='failed', reason=?, updated_at=? "
                        "WHERE id=? AND status='pending'",
                        ("已不在这座城，等下次出现再打", now, row[0]))
                    conn.execute(
                        "UPDATE watch_sub SET last_present=0, lock_sent=0 "
                        "WHERE city_id=? AND uid=? "
                        "AND (last_present IS NULL OR last_present!=0 "
                        "OR IFNULL(lock_sent,0)!=0)",
                        (int(row[1]), uid))
                    log.info("登录账号 %s 排队的自动锁敌 UID %s 已不在城 %s，跳过",
                             username_of(owner), uid, row[1])
                    continue
            cur = conn.execute(
                "UPDATE atk_order SET status='running', updated_at=? "
                "WHERE id=? AND status='pending'",
                (now, row[0]))
            conn.commit()
            if cur.rowcount != 1:
                return None
            return {"id": row[0], "city_id": row[1], "uid": row[2], "auto": bool(row[5]),
                    "cards": None if row[6] is None else int(row[6]),
                    "kind": str(row[7] or ""), "user_id": owner}
    finally:
        conn.close()


def note_lock_morale(morale, confirmed=False) -> None:
    """索敌正在打时，把敌方当前士气写进说明。清城订单不改。写库失败不影响继续打。

    没读到数字就不改。已经记下大于 0 的士气后，城市名单或库里的 0
    不算拿到了敌方士气，留着上次的数。战报里明确读到的 0 才覆盖。
    """
    if isinstance(morale, bool) or not isinstance(morale, int):
        return
    order_id = int(getattr(_attack_local, "order", 0) or 0)
    if not order_id:
        return
    morale = int(morale)
    try:
        conn = connect()
    except sqlite3.Error:
        return
    try:
        if morale == 0 and not confirmed:
            row = conn.execute(
                "SELECT reason FROM atk_order WHERE id=? AND status='running' "
                "AND TRIM(IFNULL(uid,''))!=''",
                (order_id,)).fetchone()
            prev = str(row[0] or "") if row else ""
            head = "敌方士气 "
            if prev.startswith(head):
                tail = prev[len(head):].strip()
                if tail.isdigit() and int(tail) > 0:
                    return
        conn.execute(
            "UPDATE atk_order SET reason=? WHERE id=? AND status='running' "
            "AND TRIM(IFNULL(uid,''))!=''",
            (f"敌方士气 {morale}", order_id))
        conn.commit()
    except sqlite3.Error:
        return
    finally:
        conn.close()


def remember_order_name(order_id: int, name: str) -> None:
    """把这一单要打的人的名字留下。人离开城市后，订单仍显示名称。"""
    name = str(name or "").strip()
    if name.startswith("击退"):
        name = name[2:].strip()
    try:
        order_id = int(order_id or 0)
    except (TypeError, ValueError):
        return
    if order_id <= 0 or not name:
        return
    try:
        conn = connect()
    except sqlite3.Error:
        return
    try:
        conn.execute(
            "UPDATE atk_order SET name=? WHERE id=? AND IFNULL(name,'')!=?",
            (name, order_id, name))
        conn.commit()
    except sqlite3.Error:
        return
    finally:
        conn.close()


def note_attack_beats(order_id: int, n: int, name: str = "") -> None:
    """正在打的订单记下已经击退几个人。清城时说明写成「击退 玩家名」。写库失败不影响继续打。"""
    try:
        conn = connect()
    except sqlite3.Error:
        return
    try:
        raw = str(name or "").strip()
        if raw.startswith("击退"):
            raw = raw[2:].strip()
        who = f"击退 {raw}" if raw else ""
        if who:
            conn.execute(
                "UPDATE atk_order SET beats=?, reason=? WHERE id=? AND status='running'",
                (int(n), who, int(order_id)))
        else:
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


def park_attack_order(order_id: int, reason: str = "", beats=None) -> None:
    """有人挡路。订单先停在这里，挂机期间路径通了再接着打。"""
    conn = connect()
    try:
        conn.execute(
            "UPDATE atk_order SET status='blocked', reason=?, beats=?, updated_at=? "
            "WHERE id=? AND status='running'",
            (reason or "", None if beats is None else int(beats),
             now_ts(), int(order_id)))
        conn.commit()
    finally:
        conn.close()


def note_blocked_reason(order_id: int, reason: str) -> None:
    """路径还是不通。把最新的原因写回等通路的订单。"""
    text = str(reason or "").strip()
    if not text:
        return
    conn = connect()
    try:
        conn.execute(
            "UPDATE atk_order SET reason=?, updated_at=? WHERE id=? AND status='blocked'",
            (text, now_ts(), int(order_id)))
        conn.commit()
    finally:
        conn.close()


def next_blocked_order():
    """挂机时要盯的那一单。后提交的优先。不改状态。同一个攻打 QQ 上的都看。"""
    ids = executable_users()
    if not ids:
        return None
    slot = ",".join("?" * len(ids))
    conn = connect(readonly=True)
    try:
        row = conn.execute(
            "SELECT id, city_id, IFNULL(uid,''), IFNULL(auto,0), card_max, beats, "
            "IFNULL(kind,''), user_id "
            "FROM atk_order WHERE user_id IN (" + slot + ") AND status='blocked' "
            "AND IFNULL(auto,0)=0 ORDER BY id DESC LIMIT 1",
            tuple(ids),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return {"id": row[0], "city_id": row[1], "uid": row[2], "auto": bool(row[3]),
            "cards": None if row[4] is None else int(row[4]),
            "beats": 0 if row[5] is None else int(row[5]),
            "kind": str(row[6] or ""), "user_id": int(row[7])}


def take_blocked_order(order_id: int):
    """路径通了，把等通路的订单拿回来接着打。"""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT id, city_id, IFNULL(uid,''), IFNULL(auto,0), card_max, beats, "
            "IFNULL(kind,''), user_id "
            "FROM atk_order WHERE id=? AND status='blocked'",
            (int(order_id),)).fetchone()
        if not row or shutdown_due(int(row[7])):
            conn.commit()
            return None
        cur = conn.execute(
            "UPDATE atk_order SET status='running', updated_at=? "
            "WHERE id=? AND status='blocked'",
            (now_ts(), int(order_id)))
        conn.commit()
        if cur.rowcount != 1:
            return None
        return {"id": row[0], "city_id": row[1], "uid": row[2], "auto": bool(row[3]),
                "cards": None if row[4] is None else int(row[4]),
                "beats": 0 if row[5] is None else int(row[5]),
                "kind": str(row[6] or ""), "user_id": int(row[7])}
    finally:
        conn.close()


def fail_blocked_orders() -> None:
    """挂机结束了。这个攻打 QQ 上还在等通路的订单记为没打成，原因留着。"""
    ids = attack_context_users()
    if not ids:
        return
    slot = ",".join("?" * len(ids))
    conn = connect()
    try:
        conn.execute(
            "UPDATE atk_order SET status='failed', updated_at=? "
            "WHERE user_id IN (" + slot + ") AND status='blocked'",
            (now_ts(), *ids))
        conn.commit()
    finally:
        conn.close()


def append_order_reason(order_id: int, extra: str) -> None:
    """把后退说明接到已经结束的订单上。连着打完再退时，说明写在最后一单。"""
    extra = " ".join(str(extra or "").split()).strip()
    if not extra:
        return
    try:
        order_id = int(order_id or 0)
    except (TypeError, ValueError):
        return
    if order_id <= 0:
        return
    conn = connect()
    try:
        row = conn.execute(
            "SELECT IFNULL(reason,'') FROM atk_order WHERE id=?",
            (order_id,)).fetchone()
        if not row:
            return
        prev = str(row[0] or "").strip()
        if extra in prev:
            return
        text = extra if not prev else f"{prev}。{extra}"
        conn.execute(
            "UPDATE atk_order SET reason=?, updated_at=? WHERE id=?",
            (text, now_ts(), order_id))
        conn.commit()
    finally:
        conn.close()


def finish_attack_order(order_id: int, status: str, reason: str = "", beats=None,
                         keep_reason: bool = False) -> None:
    conn = connect()
    try:
        now = now_ts()
        if keep_reason:
            if beats is None:
                conn.execute(
                    "UPDATE atk_order SET status=?, updated_at=? "
                    "WHERE id=? AND status='running'",
                    (status, now, int(order_id)))
            else:
                conn.execute(
                    "UPDATE atk_order SET status=?, beats=?, updated_at=? "
                    "WHERE id=? AND status='running'",
                    (status, int(beats), now, int(order_id)))
        elif beats is None:
            conn.execute(
                "UPDATE atk_order SET status=?, reason=?, updated_at=? "
                "WHERE id=? AND status='running'",
                (status, reason or "", now, int(order_id)))
        else:
            conn.execute(
                "UPDATE atk_order SET status=?, reason=?, beats=?, updated_at=? "
                "WHERE id=? AND status='running'",
                (status, reason or "", int(beats), now, int(order_id)))
        conn.commit()
    finally:
        conn.close()


def cancel_attack_order(user_id: int, order_id: int) -> str:
    """手动关停这一条。正在打的会在下一轮动作里停手。已经结束的不动。"""
    user_id = int(user_id)
    order_id = int(order_id)
    conn = connect()
    try:
        row = conn.execute(
            "SELECT status FROM atk_order WHERE id=? AND user_id=?",
            (order_id, user_id)).fetchone()
        if not row:
            return "没有这条订单"
        if str(row[0] or "") not in ("pending", "running", "blocked", "wait"):
            return "这条订单已经结束"
        conn.execute(
            "UPDATE atk_order SET status='ended', reason=?, updated_at=? "
            "WHERE id=? AND user_id=? AND status IN ('pending','running','blocked','wait')",
            ("已手动关停", now_ts(), order_id, user_id))
        conn.commit()
        log.info("登录账号 %s 关停订单 %s", username_of(user_id), order_id)
        return ""
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
            "COALESCE(NULLIF(TRIM(IFNULL(p.name,'')), ''), "
            "NULLIF(TRIM(IFNULL(s.name,'')), ''), "
            "(SELECT s2.name FROM watch_sub s2 WHERE s2.uid=s.uid "
            "AND TRIM(IFNULL(s2.name,''))!='' LIMIT 1), "
            "(SELECT p2.name FROM player p2 WHERE p2.uid=s.uid "
            "AND TRIM(IFNULL(p2.name,''))!='' "
            "ORDER BY p2.fetched_at DESC LIMIT 1), "
            "(SELECT f.name FROM atk_fail f WHERE f.uid=s.uid "
            "AND TRIM(IFNULL(f.name,''))!='' "
            "ORDER BY f.at DESC LIMIT 1), ''), "
            "p.lvl, p.fetched_at, p.page, "
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


def list_watch_counts(user_id: int) -> list:
    """这个账号订阅过的城，每座城最近一次扫描的玩家数量。"""
    conn = connect(readonly=True)
    try:
        rows = conn.execute(
            "SELECT DISTINCT s.city_id, IFNULL(c.name,''), o.user_cnt, o.fetched_at "
            "FROM watch_sub s "
            "LEFT JOIN city c ON c.id=s.city_id "
            "LEFT JOIN city_occupy o ON o.city_id=s.city_id "
            "WHERE s.user_id=? "
            "ORDER BY CASE WHEN IFNULL(o.fetched_at,'')='' THEN 1 ELSE 0 END, "
            "o.fetched_at DESC, IFNULL(c.name,''), s.city_id",
            (int(user_id),)).fetchall()
    finally:
        conn.close()
    out = []
    for city_id, name, cnt, at in rows:
        out.append({
            "city_id": int(city_id),
            "city_name": name or "",
            "user_cnt": None if cnt is None else int(cnt),
            "scanned_at": beijing_ts(at or ""),
        })
    return out


def sync_watch(city_id: int, seen_uids, finished: bool) -> list:
    """用这一轮拉到的人更新订阅状态。

    这一轮正常扫完后，没见到的订阅 UID 记成不在线。扫描中断时不改这些 UID。
    push 为真的是刚变成在线，要推送。arm 为真的是自动锁敌该排一条攻打。
    离开只改状态，不推。人离开后，下一次再见到可以再排。
    """
    city_id = int(city_id)
    seen = {str(u).strip() for u in seen_uids if str(u).strip()}
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT s.user_id, s.uid, s.last_present, IFNULL(u.qq_target,''), "
            "IFNULL(u.expires_at,''), IFNULL(u.tier,'初级'), IFNULL(u.auto_lock,0), "
            "IFNULL(s.lock_sent,0) "
            "FROM watch_sub s JOIN app_user u ON u.id=s.user_id "
            "WHERE s.city_id=?",
            (city_id,)).fetchall()
        if not rows:
            return []
        cname = conn.execute("SELECT name FROM city WHERE id=?",
                             (city_id,)).fetchone()
        cname = cname[0] if cname else ""
        changes = []
        for user_id, uid, last, qq_target, expires_at, tier, auto_lock, lock_sent in rows:
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
                name = str(nrow[0] or "").strip()
                if nrow[1] is not None:
                    page = int(nrow[1]) + 1
            if name:
                conn.execute(
                    "UPDATE watch_sub SET name=? WHERE uid=? AND IFNULL(name,'')!=?",
                    (name, uid, name))
            just = last is None or int(last) != 1
            sent = int(lock_sent or 0)
            if last is None or int(last) != now or (now == 0 and sent):
                conn.execute(
                    "UPDATE watch_sub SET last_present=?, lock_sent=? "
                    "WHERE user_id=? AND city_id=? AND uid=?",
                    (now, 0 if now == 0 else sent, user_id, city_id, uid))
            if account_expired(expires_at):
                continue
            armed = bool(auto_lock) and high_tier(tier or "") and not shutdown_due(user_id)
            if now == 1 and just:
                changes.append({
                    "user_id": user_id, "city_id": city_id, "city_name": cname,
                    "uid": uid, "name": name, "page": page, "present": True,
                    "qq_target": qq_target, "tier": tier or "初级",
                    "auto_lock": bool(auto_lock), "push": True, "arm": armed,
                })
            elif now == 1 and armed and not sent:
                changes.append({
                    "user_id": user_id, "city_id": city_id, "city_name": cname,
                    "uid": uid, "name": name, "page": page, "present": True,
                    "qq_target": qq_target, "tier": tier or "初级",
                    "auto_lock": True, "push": False, "arm": True,
                })
        conn.commit()
        return changes
    finally:
        conn.close()


def mark_watch_repelled(city_id, uid) -> None:
    """攻打把这个人从这座城击退了。订阅立刻改成不在，下次再出现可以再排。

    别的城不动。写库失败不影响正在打的那一单。
    """
    if isinstance(city_id, bool):
        return
    try:
        city_id = int(city_id or 0)
    except (TypeError, ValueError):
        return
    uid = str(uid or "").strip()
    if city_id <= 0 or not uid:
        return
    try:
        conn = connect()
        try:
            cur = conn.execute(
                "UPDATE watch_sub SET last_present=0, lock_sent=0 "
                "WHERE city_id=? AND uid=? "
                "AND (last_present IS NULL OR last_present!=0 OR IFNULL(lock_sent,0)!=0)",
                (city_id, uid))
            conn.commit()
            n = cur.rowcount
        finally:
            conn.close()
    except sqlite3.Error:
        return
    if n:
        log.info("击退 uid=%s，城市 %s 的监控改成不在这座城，%d 条", uid, city_id, n)


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


def _fail_acct() -> str:
    """当前这条攻打线程的 QQ。失败库按这个号分开。"""
    return attack_context_qq()


def _fail_op_mine(op) -> bool:
    return len(op) >= 2 and op[1] == _fail_acct()


def failed_uids() -> set:
    return set(failed_names())


def failed_names_in(city_id) -> list:
    """这座城里、当前攻打号战败表记下的名字。含还没落盘的队列。没有名字时用 uid。"""
    try:
        city_id = int(city_id or 0)
    except (TypeError, ValueError):
        return []
    if city_id <= 0:
        return []
    acct = _fail_acct()

    def _read():
        conn = connect(timeout=DB_OP_TIMEOUT)
        try:
            rows = conn.execute(
                "SELECT uid, IFNULL(name,'') FROM atk_fail "
                "WHERE acct=? AND city_id=? AND IFNULL(ret,0) NOT IN (21)",
                (acct, city_id)).fetchall()
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
        if not _fail_op_mine(op):
            continue
        if op[0] == "wipe":
            names.clear()
        elif op[0] == "clear":
            names.pop(str(op[2]).strip(), None)
        elif op[0] == "record":
            uid = str(op[2]).strip()
            if not uid:
                continue
            if op[5] in (21,):
                names.pop(uid, None)
                continue
            if int(op[4] or 0) == city_id:
                names[uid] = str(op[3] or "").strip()
            else:
                names.pop(uid, None)
    out = []
    for uid, name in names.items():
        text = name or uid
        if text and text not in out:
            out.append(text)
    return out


def failed_names() -> dict:
    """当前攻打号战败表里的 uid → 当时记下的名字。ret=21 不算打不过。含还没落盘的队列。"""
    acct = _fail_acct()

    def _read():
        conn = connect(timeout=DB_OP_TIMEOUT)
        try:
            rows = conn.execute(
                "SELECT uid, IFNULL(name,'') FROM atk_fail "
                "WHERE acct=? AND IFNULL(ret,0) NOT IN (21)",
                (acct,)).fetchall()
            return {str(u).strip(): str(n or "").strip() for u, n in rows if u}
        finally:
            conn.close()
    got = _run_timeout(_read, default={})
    names = dict(got) if isinstance(got, dict) else {}
    for op in _atk_q:
        if not _fail_op_mine(op):
            continue
        if op[0] == "wipe":
            names.clear()
        elif op[0] == "clear":
            names.pop(str(op[2]).strip(), None)
        elif op[0] == "record":
            names[str(op[2]).strip()] = str(op[3] or "").strip()
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
    """这个人现在算不算当前攻打号失败库里的。先看本轮还没落盘的队列，再查库。"""
    uid = str(uid or "").strip()
    if not uid:
        return False
    acct = _fail_acct()
    pending = None
    for op in _atk_q:
        if not _fail_op_mine(op):
            continue
        if op[0] == "wipe":
            pending = False
        elif op[0] == "clear" and op[2] == uid:
            pending = False
        elif op[0] == "record" and op[2] == uid:
            pending = True
    if pending is True:
        return True
    if pending is False:
        return False

    def _read():
        conn = connect(readonly=True, timeout=DB_OP_TIMEOUT)
        try:
            row = conn.execute(
                "SELECT 1 FROM atk_fail WHERE acct=? AND uid=? AND IFNULL(ret,0) NOT IN (21)",
                (acct, uid)).fetchone()
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
        _atk_q.append(("record", _fail_acct(), uid, name or "", int(city_id or 0),
                       ret, reason or ""))


def clear_atk_fail(uid=None):
    acct = _fail_acct()
    if uid is None:
        _atk_q.append(("wipe", acct))
        return
    uid = str(uid or "").strip()
    if uid:
        _atk_q.append(("clear", acct, uid))


def flush_atk_fail():
    """战斗结束再写盘。超过 DB_OP_TIMEOUT 就放弃，不堵下一轮。只动记下时那个攻打号的记录。"""
    global _atk_q
    if not _atk_q:
        return
    batch, _atk_q = _atk_q, []

    def _write():
        conn = connect(timeout=DB_OP_TIMEOUT)
        try:
            for op in batch:
                if op[0] == "wipe":
                    conn.execute("DELETE FROM atk_fail WHERE acct=?", (op[1],))
                elif op[0] == "clear":
                    conn.execute(
                        "DELETE FROM atk_fail WHERE acct=? AND uid=?",
                        (op[1], op[2]))
                else:
                    _, acct, uid, name, city_id, ret, reason = op
                    conn.execute(
                        "INSERT INTO atk_fail(acct, uid, name, city_id, ret, reason, at) "
                        "VALUES (?,?,?,?,?,?,?) "
                        "ON CONFLICT(acct, uid) DO UPDATE SET "
                        "name=excluded.name, city_id=excluded.city_id, "
                        "ret=excluded.ret, reason=excluded.reason, at=excluded.at",
                        (acct, uid, name, city_id, ret, reason, now_ts()))
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
