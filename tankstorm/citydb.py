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

import sqlite3
import threading
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
    created_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS atk_order (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER NOT NULL,
    city_id     INTEGER NOT NULL,
    status      TEXT NOT NULL,
    reason      TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
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


def plan_route(here, target, my_country, avoid=None) -> dict:
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

    def blocked(cid):
        home = g[cid].get("home") or 0
        return cid in skip or (fort_locked(cid) and not mine(cid) and home != 21)

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
            if v == target or blocked(v):
                continue
            add = 0 if mine(v) else 1
            nxt = (occ + add, hops + 1)
            if nxt < best.get(v, (10 ** 9, 10 ** 9)):
                best[v] = nxt
                prev[v] = u
                heapq.heappush(pq, (*nxt, v))
    if found is None:
        out["原因"] = (f"从 {label(here)} 到 {label(target)} 没有通路。"
                      "编号第2位是1或2、又不是自己国家的城不能占领，也不能借道")
        return out
    path = []
    cur = found
    while cur is not None:
        path.append(cur)
        cur = prev[cur]
    path.reverse()
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
            "IFNULL(u.tier,'初级') "
            "FROM app_session s JOIN app_user u ON u.id=s.user_id WHERE s.token=?",
            (token,)).fetchone()
        if not row or account_expired(row[3]):
            return None
        return {"id": row[0], "username": row[1], "qq_target": row[2],
                "expires_at": row[3], "tier": row[4]}
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


def add_attack_order(user_id: int, city_id: int) -> str:
    """提交一条远程扫码攻打。已有未完成的单时返回原因，成功返回空字符串。"""
    conn = connect()
    try:
        row = conn.execute(
            "SELECT 1 FROM atk_order WHERE user_id=? AND status IN ('pending','running')",
            (int(user_id),)).fetchone()
        if row:
            return "已经有一条还没打完"
        now = now_ts()
        conn.execute(
            "INSERT INTO atk_order(user_id, city_id, status, reason, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?)",
            (int(user_id), int(city_id), "pending", "", now, now))
        conn.commit()
        return ""
    finally:
        conn.close()


def list_attack_orders(user_id: int, limit: int = 20) -> list:
    conn = connect(readonly=True)
    try:
        rows = conn.execute(
            "SELECT id, city_id, status, IFNULL(reason,''), created_at "
            "FROM atk_order WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (int(user_id), int(limit))).fetchall()
    finally:
        conn.close()
    return [{"id": r[0], "city_id": r[1], "status": r[2], "reason": r[3],
             "created_at": beijing_ts(r[4])} for r in rows]


def claim_attack_order():
    """领最旧的一条排队订单。档位不够或已过期的记为失败。没有则返回 None。"""
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
        row = conn.execute(
            "SELECT o.id, o.city_id, IFNULL(u.tier,'初级'), IFNULL(u.expires_at,'') "
            "FROM atk_order o JOIN app_user u ON u.id=o.user_id "
            "WHERE o.status='pending' ORDER BY o.id LIMIT 1").fetchone()
        if not row:
            conn.commit()
            return None
        if not attack_tier(row[2]) or account_expired(row[3]):
            conn.execute(
                "UPDATE atk_order SET status='failed', reason=?, updated_at=? WHERE id=?",
                ("订阅档不够或账号已过期", now, row[0]))
            conn.commit()
            return None
        cur = conn.execute(
            "UPDATE atk_order SET status='running', updated_at=? "
            "WHERE id=? AND status='pending'",
            (now, row[0]))
        conn.commit()
        if cur.rowcount != 1:
            return None
        return {"id": row[0], "city_id": row[1]}
    finally:
        conn.close()


def finish_attack_order(order_id: int, status: str, reason: str = "") -> None:
    conn = connect()
    try:
        conn.execute(
            "UPDATE atk_order SET status=?, reason=?, updated_at=? "
            "WHERE id=? AND status='running'",
            (status, reason or "", now_ts(), int(order_id)))
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
            "IFNULL(u.expires_at,'') "
            "FROM watch_sub s JOIN app_user u ON u.id=s.user_id "
            "WHERE s.city_id=?",
            (city_id,)).fetchall()
        if not rows:
            return []
        cname = conn.execute("SELECT name FROM city WHERE id=?",
                             (city_id,)).fetchone()
        cname = cname[0] if cname else ""
        changes = []
        for user_id, uid, last, qq_target, expires_at in rows:
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
                    "qq_target": qq_target,
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
    def _read():
        conn = connect(timeout=DB_OP_TIMEOUT)
        try:
            return {r[0] for r in conn.execute(
                "SELECT uid FROM atk_fail WHERE IFNULL(ret,0) NOT IN (21)")
                if r[0]}
        finally:
            conn.close()
    got = _run_timeout(_read, default=set())
    return got if isinstance(got, set) else set()


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
                "IFNULL(o.occupy_country, c.country_id), c.name "
                "FROM player p LEFT JOIN city c ON c.id=p.city_id "
                "LEFT JOIN city_occupy o ON o.city_id=p.city_id "
                "WHERE p.uid=? ORDER BY p.fetched_at DESC LIMIT 1",
                (uid,)).fetchone()
            if not row:
                return None
            return {"uid": row[0], "city_id": row[1], "name": row[2], "lvl": row[3],
                    "country_id": row[4], "morale": row[5],
                    "city_country": row[6], "city_name": row[7]}
        finally:
            conn.close()

    got = _run_timeout(_read)
    return got if isinstance(got, dict) else None
