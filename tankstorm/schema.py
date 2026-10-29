# tankstorm-keepalive  Copyright (C) 2026 Dimlitter
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# 本程序是自由软件：你可以依据自由软件基金会发布的 GNU Affero 通用公共许可证
# （第 3 版，或你选择的任何更新版本）之条款，再分发和/或修改它。
# 本程序希望能有用，但不提供任何担保；甚至不含适销性或特定用途适用性的默示担保。
# 详见随附的 LICENSE 文件，或 <https://www.gnu.org/licenses/>。
"""协议 schema：opcode 与消息名、字段号与字段名的对照。

schema.json 由 tools/extract_proto.py 从 RedWar SWF 还原：
  · opcode 表来自「立即数 + 消息类」的注册调用。旧版函数名是 _-055，
    更新后改名就按调用形状重找
  · 消息真名来自 protobuf-as3 的报错模板 'Bad data format: <消息>.<字段> ...'
    ——字符串字面量，secureSWF 动不了
  · 字段号来自 writeToBuffer 里 writeTag(output, wiretype, <tag>) 的立即数
  · 字段类型看写函数的方法体（writeUTFBytes 是 string，writeByte 是 bool）

游戏更新后不要整份替换这份表。本项目代码认的是这里的编号，
新版编号放到程序目录的 wiremap.json，见 tools/build_wiremap.py。
"""

import json
import os

_HERE = os.path.dirname(os.path.abspath(__file__))

# schema.json 跟着包走，是只读数据。打包成单文件后它被解压到 _MEIPASS/tankstorm/，
# 此时 __file__ 也指向那里，所以第一条就能命中；退路是 exe 旁边放一份，
# 方便游戏更新后直接替换而不必等新版本。
_CANDIDATES = [os.path.join(_HERE, "schema.json")]
try:
    from .paths import bundled_dir, user_path
    _CANDIDATES += [os.path.join(bundled_dir(), "tankstorm", "schema.json"),
                    user_path("schema.json")]
except ImportError:                                # 极端情况下别让导入失败拖垮整个包
    pass

SCHEMA = {}
for _PATH in _CANDIDATES:
    try:
        with open(_PATH, encoding="utf-8") as _f:
            SCHEMA = json.load(_f)
        break
    except (OSError, ValueError):
        continue


def name_of(op: str) -> str:
    """opcode（4位小写十六进制）-> 消息名，认不出就原样返回。"""
    e = SCHEMA.get(op)
    return e["name"] if e else op


def describe(op: str) -> str:
    e = SCHEMA.get(op)
    return f"{op} {e['name']}" if e else op


# ---------------------------------------------------------------- protobuf

def _varint(b, i):
    shift = val = 0
    n = len(b)
    while i < n:
        c = b[i]
        i += 1
        val |= (c & 0x7F) << shift
        if not c & 0x80:
            return val, i
        shift += 7
        if shift > 63:
            return None, i
    return None, i


_ZIGZAG = {"sint32", "sint64"}

# 嵌套消息没有自己的 opcode。CountryBackCaptial 挂在国战回包的 ememyBack 上，
# 自己被打回首都时带进攻方名字。
_NESTED = {
    "ememyBack": {
        "1": ("atkID", "string"),
        "2": ("atkName", "string"),
        "3": ("atkCountry", "int32"),
        "4": ("defID", "string"),
        "5": ("defName", "string"),
        "6": ("defCountry", "int32"),
    },
    # RseGuildOpt.npcList，CountryUser。2026-10-24 公会战役据点：baseid 是进攻用的 npcID，lvl 是等级。
    "npcList": {
        "3": ("baseid", "string"),
        "6": ("lvl", "int32"),
    },
    # RseGuildOpt.nowPersonMsg，GVEPersonalMsg。2026-10-24：point 是当前积分，rank 是名次。
    # 打开时 point=0，两次进攻后 1122、2244。lastPersonMsg / HighestPersonMsg 不是这一场的当前积分。
    "nowPersonMsg": {
        "1": ("point", "int32"),
        "2": ("rank", "int32"),
    },
    # RseGuildBuildOpt 的团队任务。field6 是自己领过没有，写出时是 0/1。
    "lstHelper": {
        "1": ("lUid", "string"),
        "2": ("strName", "string"),
        "3": ("nProcess", "int32"),
        "4": ("nTotalNeed", "int32"),
    },
    "field21": {
        "1": ("nID", "int32"),
        "2": ("nTaskType", "int32"),
        "3": ("nProcess", "int32"),
        "4": ("nTotalNeed", "int32"),
        "5": ("lholder", "string"),
        "6": ("bGetReward", "bool"),
        "7": ("lstHelper", "message"),
    },
}
_NESTED["field22"] = _NESTED["field21"]
_NESTED["field23"] = _NESTED["field21"]


def decode(body: bytes, op: str = None, depth: int = 0, fields=None):
    """解析 protobuf，按 schema 给字段命名。返回 dict，解析失败返回 None。

    同一字段出现多次（repeated）会收成 list。
    """
    if fields is None:
        fields = (SCHEMA.get(op or "", {}) or {}).get("fields", {})
    out, i, n = {}, 0, len(body)
    while i < n:
        key, i = _varint(body, i)
        if key is None:
            return None
        wt, fn = key & 7, key >> 3
        if fn == 0 or wt in (3, 4, 6, 7):
            return None
        meta = fields.get(str(fn))
        fname, ftype = (meta[0], meta[1]) if meta else (f"field{fn}", None)
        if wt == 0:
            v, i = _varint(body, i)
            if v is None:
                return None
            if ftype in _ZIGZAG:
                v = (v >> 1) ^ -(v & 1)
            elif ftype == "bool":
                v = bool(v)
        elif wt == 1:
            if i + 8 > n:
                return None
            v, i = int.from_bytes(body[i:i + 8], "little"), i + 8
        elif wt == 5:
            if i + 4 > n:
                return None
            v, i = int.from_bytes(body[i:i + 4], "little"), i + 4
        else:
            ln, i = _varint(body, i)
            if ln is None or ln > n - i:
                return None
            raw, i = body[i:i + ln], i + ln
            if ftype == "string":
                v = raw.decode("utf-8", "replace")
            else:
                v = None
                if ftype in ("message", "group", None) and depth < 6:
                    child = _NESTED.get(fname) if ftype == "message" else None
                    v = decode(raw, None, depth + 1, child)
                if v is None:              # 不是嵌套消息就试当文本，再不行才给 hex
                    try:
                        t = raw.decode("utf-8")
                        v = t if t.isprintable() else raw.hex()
                    except UnicodeDecodeError:
                        v = raw.hex()
        if fname in out:
            if not isinstance(out[fname], list):
                out[fname] = [out[fname]]
            out[fname].append(v)
        else:
            out[fname] = v
    return out
