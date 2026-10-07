# tankstorm-keepalive  Copyright (C) 2026 Dimlitter
# SPDX-License-Identifier: AGPL-3.0-or-later
"""试用版把 QQ、公会 ID 加密后写进包里。对不上就不往下跑。

密文在 tankstorm/_trial_bind.py，由打包规格在构建时写入。
开发运行和未设置对应环境变量的正式包都是空元组，不限制。
公会要等进了游戏才能核对，见 guild.refuse。
"""

import hashlib
import os

from .log import get_logger
from ._trial_bind import BLOB, GUILD_BLOB, GUILD_SEED, SEED

log = get_logger()


def _open(seed: bytes, blob: bytes) -> str:
    """和 tools/trial_bind.py 里的加密互为逆运算。"""
    if not seed or not blob:
        return ""
    cut = len(blob) // 2
    mixed = blob[len(blob) - cut:] + blob[:len(blob) - cut]
    stream = b""
    block = seed
    while len(stream) < len(mixed):
        block = hashlib.sha256(block).digest()
        stream += block
    raw = bytes(mixed[i] ^ stream[i] ^ ((i * 31) & 0xFF) for i in range(len(mixed)))
    return raw.decode("utf-8")


def bind_guild() -> str:
    """没锁定返回空串。密文解不开返回 \\0，调用方应停手。"""
    if not GUILD_BLOB:
        return ""
    try:
        text = _open(bytes(GUILD_SEED), bytes(GUILD_BLOB)).strip()
    except (UnicodeDecodeError, ValueError):
        return "\0"
    if not text.isdigit() or int(text) <= 0:
        return "\0"
    return str(int(text))


def bind_uin() -> str:
    if not BLOB:
        return ""
    try:
        text = _open(bytes(SEED), bytes(BLOB)).strip().lstrip("o0")
    except (UnicodeDecodeError, ValueError):
        return "\0"
    if not text.isdigit():
        return "\0"
    return text


def allow(uin) -> bool:
    """没锁定，或还没登录，或就是这个 QQ，返回 True。"""
    want = bind_uin()
    if not want:
        return True
    got = str(uin or "").strip().lstrip("o0")
    if not got:
        return True
    return got == want


def refuse(qq) -> bool:
    """锁定了且当前登录不是这个 QQ 时，清掉这份登录态并返回 True。"""
    if allow(getattr(qq, "uin", "")):
        return False
    want = bind_uin()
    got = str(getattr(qq, "uin", "") or "").strip().lstrip("o0")
    log.error("此版本只允许 QQ %s 使用，当前登录的是 %s，已停止", want, got)
    path = getattr(qq, "cookie_file", "") or ""
    if path and os.path.isfile(path):
        try:
            os.remove(path)
            log.error("已清除 %s，下次请用 QQ %s 登录", path, want)
        except OSError as exc:
            log.error("请手动删除 %s 后再用 QQ %s 登录（%s）", path, want, exc)
    return True
