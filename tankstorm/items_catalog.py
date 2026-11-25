# tankstorm-keepalive  Copyright (C) 2026 Dimlitter
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# 本程序是自由软件：你可以依据自由软件基金会发布的 GNU Affero 通用公共许可证
# （第 3 版，或你选择的任何更新版本）之条款，再分发和/或修改它。
# 本程序希望能有用，但不提供任何担保；甚至不含适销性或特定用途适用性的默示担保。
# 详见随附的 LICENSE 文件，或 <https://www.gnu.org/licenses/>。
"""从 CDN 解开道具表，写到用户数据目录。

Item_版本.dat 末尾若是 03a33cd9a31ee58c，先去掉再做 Base64，然后用 XXTEA。
密钥是 redwar2021，按小端凑成 4 个 32 位整数，不足补 0，delta 是 0x9E3779B9。
解开是 UTF-8 制表符表。编号、名称、说明在第 1、2、9 列。

游戏页 FlashVars 的 version 不一定就是这份表的文件名。先试它，以及同一天的
01、02、03 后缀。这些都不在或解不开，再按北京日期往回找最近三周里能解开的最新一份。

新出编号的基线是随包 web/src/items.json 去掉 web/src/items-new.json。
这两份文件不在刷新时改写。解开的结果放在程序目录的 items-live.json。
"""

import base64
import json
import os
import re
import struct
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import requests

from .paths import bundled_dir, user_path

CDN = "https://redwar-cdn.sincetimes.com/100616028/res/20120522/config/Item_{}.dat"
MARKER = "03a33cd9a31ee58c"
KEY = b"redwar2021"
DELTA = 0x9E3779B9
LIVE_NAME = "items-live.json"
STATE_NAME = "items-refresh.json"
LOCK_NAME = "items-refresh.lock"
LOOKBACK_DAYS = 21
DAY_SUFFIXES = ("03", "02", "01")
_BJ = timezone(timedelta(hours=8))
_MAX_BYTES = 8_000_000

_state_lock = threading.Lock()
_busy = False
_baseline_lock = threading.Lock()
_baseline = None


def _log():
    from .log import get_logger
    return get_logger()


def beijing_today() -> str:
    """北京时间的日期。最近三周的文件名按这个往回排。"""
    return datetime.now(_BJ).strftime("%Y-%m-%d")


def _u32(n):
    return n & 0xFFFFFFFF


def _mix(z, y, s, p, e, key):
    return _u32(((z >> 5 ^ y << 2) + (y >> 3 ^ z << 4)) ^ ((s ^ y) + (key[(p & 3) ^ e] ^ z)))


def xxtea_decrypt(data: bytes, key: bytes = KEY) -> bytes:
    n = len(data) // 4
    words = list(struct.unpack("<" + "I" * n, data[: n * 4]))
    key_words = list(struct.unpack("<IIII", key.ljust(16, b"\0")[:16]))
    rounds = 6 + 52 // n
    s = _u32(rounds * DELTA)
    y = words[0]
    while rounds:
        e = (s >> 2) & 3
        for p in range(n - 1, 0, -1):
            z = words[p - 1]
            words[p] = _u32(words[p] - _mix(z, y, s, p, e, key_words))
            y = words[p]
        z = words[n - 1]
        words[0] = _u32(words[0] - _mix(z, y, s, 0, e, key_words))
        y = words[0]
        s = _u32(s - DELTA)
        rounds -= 1
    return struct.pack("<" + "I" * n, *words)


def _clean_version(version: str) -> str:
    version = str(version or "").strip()
    if re.fullmatch(r"\d{8,12}", version):
        return version
    return ""


def _day_of(version: str) -> str:
    if re.fullmatch(r"20\d{6}", version) or re.fullmatch(r"20\d{8}", version):
        return version[:8]
    return ""


def ctx_candidates(version: str) -> list:
    """先试 FlashVars 里的版本，再试同一天的 03、02、01。新的排在前面。"""
    version = _clean_version(version)
    if not version:
        return []
    found = {version}
    day = _day_of(version)
    if day:
        for suffix in DAY_SUFFIXES:
            found.add(day + suffix)
    return sorted(found, reverse=True)


def recent_versions(days: int = LOOKBACK_DAYS):
    """从北京今天往回列版本号。同一天先 03，再 02，再 01。"""
    today = datetime.now(_BJ).date()
    for offset in range(max(0, int(days))):
        day = (today - timedelta(days=offset)).strftime("%Y%m%d")
        for suffix in DAY_SUFFIXES:
            yield day + suffix


def decrypt_dat(blob: bytes) -> list:
    """解开一份 Item_版本.dat。解不开就抛异常。"""
    raw = blob.decode("ascii").strip()
    if raw.endswith(MARKER):
        raw = raw[: -len(MARKER)]
    data = base64.b64decode(raw)
    if len(data) < 8 or len(data) % 4:
        raise ValueError("密文长度不对")
    plain = xxtea_decrypt(data).split(b"\x00", 1)[0]
    text = plain.decode("utf-8")
    lines = text.splitlines()
    if len(lines) < 3:
        raise ValueError("表太短")
    rows = []
    for line in lines[2:]:
        parts = line.split("\t")
        if not parts or not parts[0].isdigit():
            continue
        desc = parts[8] if len(parts) > 8 else ""
        rows.append([int(parts[0]), parts[1], desc])
    if len(rows) < 100:
        raise ValueError("解开后行数太少")
    rows.sort(key=lambda row: row[0])
    return rows


def _download(version: str):
    url = CDN.format(version)
    try:
        resp = requests.get(url, timeout=(10, 60))
    except requests.RequestException:
        return None
    if resp.status_code != 200 or not resp.content or len(resp.content) > _MAX_BYTES:
        return None
    return resp.content


def _load_version(version: str):
    blob = _download(version)
    if not blob:
        return None
    try:
        return decrypt_dat(blob)
    except (ValueError, UnicodeError, struct.error):
        return None


def _bundled_json(name: str) -> str:
    return os.path.join(bundled_dir(), "web", "src", name)


def _read_list(path: str) -> list:
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, list):
        raise ValueError(f"{os.path.basename(path)} 不是道具表")
    return data


def _id_set(rows: list) -> set:
    found = set()
    for row in rows:
        if isinstance(row, list) and row and str(row[0]).isdigit():
            found.add(int(row[0]))
    return found


def baseline_ids() -> frozenset:
    """随包全表去掉随包新出列表。刷新之后这两份文件仍是基线。"""
    global _baseline
    with _baseline_lock:
        if _baseline is None:
            old = _id_set(_read_list(_bundled_json("items.json")))
            fresh = _id_set(_read_list(_bundled_json("items-new.json")))
            if not old:
                raise ValueError("随包道具表是空的")
            _baseline = frozenset(old - fresh)
        return _baseline


def _new_rows(rows: list) -> list:
    base = baseline_ids()
    return [row for row in rows if row[0] not in base]


def _read_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _atomic_json(path: str, obj) -> None:
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".items-", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(obj, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@contextmanager
def _hold_lock(path: str):
    """独占锁。Linux 用 flock，Windows 锁文件开头 1 个字节。句柄关掉锁就没了。"""
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    handle = open(path, "a+")
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write("0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX)
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_UN)
        except OSError:
            pass
        handle.close()


def _resolve(version: str | None, ctx_version: str | None):
    if version is not None:
        cleaned = _clean_version(version)
        if not cleaned:
            raise ValueError("版本号要用 8 到 12 位数字")
        rows = _load_version(cleaned)
        if not rows:
            raise RuntimeError(f"{cleaned} 的道具表不在，或解不开")
        return cleaned, rows
    tried = set()
    for cand in ctx_candidates(ctx_version or ""):
        tried.add(cand)
        rows = _load_version(cand)
        if rows:
            return cand, rows
    for cand in recent_versions():
        if cand in tried:
            continue
        tried.add(cand)
        rows = _load_version(cand)
        if rows:
            return cand, rows
    raise RuntimeError("最近三周没有能解开的道具表")


def refresh(version: str | None = None, ctx_version: str | None = None):
    """解开并写入用户目录。失败时不改已经写好的表。

    传了 version 就只用这一版。没传就按 ctx_version 和最近三周自己找。
    返回 (版本号, 全表, 新出列表)。
    """
    found, rows = _resolve(version, ctx_version)
    fresh = _new_rows(rows)
    _atomic_json(user_path(LIVE_NAME), {
        "version": found,
        "items": rows,
        "new": fresh,
    })
    return found, rows, fresh


def live_view():
    """给页面的已解开结果。还没有，或文件读不出来，就返回 None。"""
    data = _read_json(user_path(LIVE_NAME))
    items = data.get("items")
    fresh = data.get("new")
    if not isinstance(items, list) or not items or not isinstance(fresh, list):
        return None
    head = items[0]
    if not isinstance(head, list) or len(head) < 2:
        return None
    return {
        "live": True,
        "version": str(data.get("version") or ""),
        "items": items,
        "new": fresh,
    }


def _version_newer(seen: str, saved: str) -> bool:
    """登录里看到的版本是不是比已保存的新。

    两边都要先收成 8 到 12 位数字。从左往右比，大的就是新的：
    日期在前，所以后一天更大；同一天后缀 03 比 02、01 大。
    有一边认不出来，就不算新。
    """
    seen = _clean_version(seen)
    saved = _clean_version(saved)
    if not seen or not saved:
        return False
    return seen > saved


def _saved_version() -> str:
    """已经写好的道具表版本。没有表就返回空字符串。"""
    data = _read_json(user_path(LIVE_NAME))
    items = data.get("items")
    version = _clean_version(str(data.get("version") or ""))
    if version and isinstance(items, list) and items:
        return version
    state = _read_json(user_path(STATE_NAME))
    if state.get("ok") is True:
        return _clean_version(str(state.get("version") or ""))
    return ""


def _open_newer(seen: str, saved: str):
    """打开比已保存更新、并且能解开的那一份。没有就返回 ("", None)。

    登录版本和最近三周的文件名合在一起，新的排在前面。
    不比已保存的新就停，不再下载旧表。解不开的跳过，不写文件。
    """
    ordered = []
    have = set()
    for cand in list(ctx_candidates(seen)) + list(recent_versions()):
        if cand in have:
            continue
        have.add(cand)
        ordered.append(cand)
    ordered.sort(reverse=True)
    for cand in ordered:
        if saved and not _version_newer(cand, saved):
            break
        rows = _load_version(cand)
        if rows:
            return cand, rows
    return "", None


def _mark_refresh_ok(version: str) -> None:
    today = beijing_today()
    with _hold_lock(user_path(LOCK_NAME)):
        state = _read_json(user_path(STATE_NAME))
        state["day"] = str(state.get("day") or today)
        state["version"] = version
        state["ok"] = True
        _atomic_json(user_path(STATE_NAME), state)


def schedule_refresh(qq, ctx) -> None:
    """管理员绑定的游戏号登上时，后台拉道具表。立刻返回。

    绑着这个攻打 QQ 的登录账号里没有管理员，就不拉。
    只打开比已保存版本更新、并且能解开的那一份。没有就不改现有的表，
    也不把没拉开的版本记成已经完成，下次登上还会再找。
    """
    global _busy
    uin = str(getattr(qq, "uin", "") or "").strip()
    version = ""
    if isinstance(ctx, dict):
        if not uin:
            uin = str(ctx.get("uin") or "").strip()
        version = str(ctx.get("version") or "").strip()
    with _state_lock:
        if _busy:
            return
    try:
        from . import citydb
        if not uin or not citydb.attack_qq_has_admin(uin):
            return
        with _state_lock:
            if _busy:
                return
            _busy = True
    except Exception:
        _log().info("道具表没开始更新", exc_info=True)
        return

    def work():
        global _busy
        try:
            saved = _saved_version()
            found, rows = _open_newer(version, saved)
            if not rows:
                if saved:
                    _log().info("没有比 %s 更新的道具表", saved)
                else:
                    _log().info("没有能解开的道具表")
                return
            fresh = _new_rows(rows)
            _atomic_json(user_path(LIVE_NAME), {
                "version": found,
                "items": rows,
                "new": fresh,
            })
            try:
                _mark_refresh_ok(found)
            except Exception:
                _log().info("道具表已经写好，日期标记没写上")
            _log().info("道具表已更新到 %s，共 %d 条，新出 %d 条",
                        found, len(rows), len(fresh))
        except Exception as exc:
            _log().info("道具表没更新成，页面仍用原来的表：%s", exc)
        finally:
            with _state_lock:
                _busy = False

    thread = threading.Thread(target=work, name="items-refresh", daemon=True)
    try:
        thread.start()
    except Exception:
        with _state_lock:
            _busy = False
        _log().info("道具表刷新线程没启动上", exc_info=True)
