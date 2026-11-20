#!/usr/bin/env python3
# tankstorm-keepalive  Copyright (C) 2026 Dimlitter
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# 本程序是自由软件：你可以依据自由软件基金会发布的 GNU Affero 通用公共许可证
# （第 3 版，或你选择的任何更新版本）之条款，再分发和/或修改它。
# 本程序希望能有用，但不提供任何担保；甚至不含适销性或特定用途适用性的默示担保。
# 详见随附的 LICENSE 文件，或 <https://www.gnu.org/licenses/>。
"""解开 CDN 上的 Item_版本.dat，写成网页用的道具表。

文件末尾若是 03a33cd9a31ee58c，先去掉再做 Base64，然后用 XXTEA 解密。
密钥是 redwar2021，按小端凑成 4 个 32 位整数，不足补 0。
解开是 UTF-8 制表符表。编号、名称、说明写成 web/src/items.json。
相对上一份表新出现的编号写到 web/src/items-new.json。

    python3 tools/refresh_items.py 2026111801
"""

import base64
import json
import struct
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
ITEMS = ROOT / "web" / "src" / "items.json"
NEW_ITEMS = ROOT / "web" / "src" / "items-new.json"
CDN = "https://redwar-cdn.sincetimes.com/100616028/res/20120522/config/Item_{}.dat"
MARKER = "03a33cd9a31ee58c"
KEY = b"redwar2021"
DELTA = 0x9E3779B9


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


def load_table(version: str):
    raw = requests.get(CDN.format(version), timeout=60).content.decode("ascii").strip()
    if raw.endswith(MARKER):
        raw = raw[: -len(MARKER)]
    text = xxtea_decrypt(base64.b64decode(raw)).split(b"\x00", 1)[0].decode("utf-8")
    rows = []
    for line in text.splitlines()[2:]:
        parts = line.split("\t")
        if not parts or not parts[0].isdigit():
            continue
        desc = parts[8] if len(parts) > 8 else ""
        rows.append([int(parts[0]), parts[1], desc])
    rows.sort(key=lambda row: row[0])
    return rows


def main():
    version = sys.argv[1] if len(sys.argv) > 1 else "2026111801"
    old = json.loads(ITEMS.read_text(encoding="utf-8")) if ITEMS.is_file() else []
    old_ids = {row[0] for row in old}
    rows = load_table(version)
    fresh = [row for row in rows if row[0] not in old_ids]
    ITEMS.write_text(json.dumps(rows, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    NEW_ITEMS.write_text(json.dumps(fresh, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"{version} 共 {len(rows)} 条，相对上一份新出 {len(fresh)} 条")


if __name__ == "__main__":
    main()
