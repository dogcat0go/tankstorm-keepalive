# tankstorm-keepalive  Copyright (C) 2026 Dimlitter
# SPDX-License-Identifier: AGPL-3.0-or-later
"""给新版 RedWar.swf 打抓包钩子。

不再整份替换 Transport。那个类每次更新都会把内部类型名混淆掉，
旧的 Transport.as 一编译就报「某类型不存在」。

现在只做两件事：
  1. 用 ffdec 加一个不依赖游戏内部类的 pkt.PktHook
  2. 在现有字节码里，发包 writeExternal 之后、收包 readExternal 之后，
     插上对 PktHook.reportOut / reportIn 的调用
"""

from __future__ import annotations

import os
import struct
import subprocess
import sys
import zlib

_TOOLS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")
if _TOOLS not in sys.path:
    sys.path.insert(0, _TOOLS)

from abcparse import ABC, extract_abc, _u30          # noqa: E402
from disasm import OPS, _s24                         # noqa: E402
from swfparse import iter_tags, uncompress           # noqa: E402


def inject(src: str, dest: str, java: str, ffdec: str, hook_dir: str) -> str:
    """把钩子打进 src，写到 dest。失败时返回错误说明，成功返回空串。"""
    as_path = os.path.join(hook_dir, "pkt", "PktHook.as")
    if not os.path.isfile(as_path):
        return f"没有 {as_path}"
    try:
        hook_abc = _compile_hook(java, ffdec, as_path, dest + ".hookabc")
    except Exception as exc:
        return f"编译 PktHook 失败：{exc}"
    merged = dest + ".merged.swf"
    try:
        _append_abc(src, merged, hook_abc)
        patch_swf(merged, dest)
    except Exception as exc:
        return f"插入调用失败：{exc}"
    finally:
        for p in (merged, dest + ".hookabc.swf", dest + ".hookabc"):
            if os.path.isfile(p) and os.path.abspath(p) != os.path.abspath(dest):
                try:
                    os.remove(p)
                except OSError:
                    pass
    return ""


def _compile_hook(java: str, ffdec: str, as_path: str, work: str,
                  qname: str = "pkt.PktHook", method: str = "reportOut") -> bytes:
    """先放一个空的 pkt.PktHook，再让 ffdec 用真正的源码替换它。

    ffdec 的 importScript 只会替换已经存在的类，加不进新类。
    所以单独做一份只含这个类的小 SWF，编完后把 ABC 整段接到游戏 SWF 后面。
    """
    stub = work + ".stub.swf"
    out = work + ".swf"
    with open(stub, "wb") as f:
        f.write(_empty_hook_swf(qname.rsplit(".", 1)[-1]))
    cmd = [java, "-jar", ffdec, "-replace", stub, out, qname, as_path]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=120)
    finally:
        if os.path.isfile(stub):
            os.remove(stub)
    if p.returncode != 0 or not os.path.isfile(out):
        err = (p.stderr or p.stdout or b"").decode("utf-8", "replace")
        raise RuntimeError(err[-500:] or "ffdec 没有写出文件")
    blocks = extract_abc(out)
    if not blocks:
        raise RuntimeError("编译结果里没有 ABC")
    abc = max(blocks, key=len)
    if not _has_static(ABC(abc), qname.replace(".", ":"), method):
        raise RuntimeError(f"编译结果里没有 {qname}.{method}")
    return abc


def _empty_hook_swf(class_name: str = "PktHook") -> bytes:
    abc = _empty_hook_abc_with_methods(class_name)
    payload = struct.pack("<I", 1) + f"pkt.{class_name}\x00".encode("ascii") + abc
    tags = b"".join([
        _tag(69, bytes.fromhex("19000000")),
        _tag(82, payload),
        _tag(1, b""),
        _tag(0, b""),
    ])
    body = b"\x00" + struct.pack("<HH", 30 * 256, 1) + tags
    return b"FWS" + bytes([11]) + struct.pack("<I", 8 + len(body)) + body


def _empty_hook_abc_with_methods(class_name: str = "PktHook") -> bytes:
    strings = ["", "pkt", class_name, "Object", "reportOut", "reportIn", "*"]
    namespaces = [(0x16, 1), (0x16, 0)]      # 1=pkt, 2=public package
    # 0 占位。1 类，2 Object，3 reportOut，4 reportIn，5 ByteArray 不需要，参数用 *
    multinames = [(1, 2), (2, 3), (2, 4), (2, 5), (2, 6)]
    code = bytes([0xD0, 0x30, 0x47])          # getlocal0 pushscope returnvoid
    locals_n = 4
    out = bytearray()
    out += struct.pack("<HH", 16, 46)
    out += _u30b(0) + _u30b(0) + _u30b(0)
    out += _pool_strings(strings)
    out += _u30b(1 + len(namespaces))
    for kind, ni in namespaces:
        out += bytes([kind]) + _u30b(ni)
    out += _u30b(1)
    out += _u30b(1 + len(multinames))
    for ns, name in multinames:
        out += bytes([0x07]) + _u30b(ns) + _u30b(name)
    # methods: 0 iinit, 1 cinit, 2 script, 3 reportOut(2 params), 4 reportIn(1 param)
    out += _u30b(5)
    out += _method_info(0, 0)
    out += _method_info(0, 0)
    out += _method_info(0, 0)
    out += _method_info(2, 0)                 # reportOut(*, *)
    out += _method_info(1, 0)                 # reportIn(*)
    out += _u30b(0)
    out += _u30b(1)
    out += _u30b(1) + _u30b(2) + bytes([0]) + _u30b(0) + _u30b(0) + _u30b(0)
    out += _u30b(1)                           # cinit
    out += _u30b(2)                           # two static methods
    out += _trait_method(3, 3)
    out += _trait_method(4, 4)
    out += _u30b(1)                           # one script
    out += _u30b(2) + _u30b(1)                # init method 2, one trait
    out += _trait_class(1, 0)
    out += _u30b(5)
    for mi in range(5):
        out += _body(mi, code, locals_n)
    return bytes(out)


def _pool_strings(strings) -> bytes:
    out = bytearray(_u30b(len(strings)))
    for s in strings[1:]:
        data = s.encode("utf-8")
        out += _u30b(len(data)) + data
    return bytes(out)


def _method_info(param_count: int, ret: int) -> bytes:
    out = bytearray(_u30b(param_count) + _u30b(ret))
    for _ in range(param_count):
        out += _u30b(0)                       # type *
    out += _u30b(0) + bytes([0])              # name, flags
    return bytes(out)


def _trait_method(name_mn: int, method: int) -> bytes:
    return _u30b(name_mn) + bytes([0x01]) + _u30b(0) + _u30b(method)


def _trait_class(name_mn: int, classi: int) -> bytes:
    return _u30b(name_mn) + bytes([0x04]) + _u30b(1) + _u30b(classi)


def _body(mi: int, code: bytes, locals_n: int) -> bytes:
    return ( _u30b(mi) + _u30b(2) + _u30b(locals_n) + _u30b(1) + _u30b(2)
             + _u30b(len(code)) + code + _u30b(0) + _u30b(0))


def _tag(code: int, payload: bytes) -> bytes:
    if len(payload) < 0x3F:
        return struct.pack("<H", (code << 6) | len(payload)) + payload
    return struct.pack("<HI", (code << 6) | 0x3F, len(payload)) + payload


def _append_abc(src: str, dest: str, abc: bytes,
                abc_name: bytes = b"pkt.PktHook\x00") -> None:
    raw = open(src, "rb").read()
    body, _, ver, _ = uncompress(raw)
    # flags=0：解析到这个标签就执行脚本。必须放在 ShowFrame 之前，
    # 否则它落在这一帧外面，类还没登记，游戏初始化就停住。
    payload = struct.pack("<I", 0) + abc_name + abc
    tag = _tag(82, payload)
    from swfparse import _rect_len
    head = _rect_len(body) + 4
    out = bytearray(body[:head])
    p, n = head, len(body)
    inserted = False
    while p + 2 <= n:
        rh = struct.unpack_from("<H", body, p)[0]
        code, ln = rh >> 6, rh & 0x3F
        hdr = 2
        if ln == 0x3F:
            if p + 6 > n:
                break
            ln = struct.unpack_from("<I", body, p + 2)[0]
            hdr = 6
        if code == 1 and not inserted:
            out += tag
            inserted = True
        if p + hdr + ln > n:
            break
        out += body[p:p + hdr + ln]
        p += hdr + ln
        if code == 0:
            break
    if not inserted:
        raise ValueError("SWF 里没有 ShowFrame，PktHook 没处可放")
    with open(dest, "wb") as f:
        f.write(_pack_swf(bytes(out), ver))


def patch_swf(src: str, dest: str) -> None:
    raw = open(src, "rb").read()
    body, sig, ver, _ = uncompress(raw)
    blocks = _abc_blocks(body)
    main_i = max(range(len(blocks)), key=lambda i: len(blocks[i][2]))
    abc = ABC(blocks[main_i][2])
    cls_mn, out_mn, in_mn, pay_mn, result_mn, mark_mn, abc, blocks = _hook_names(
        abc, blocks, main_i)
    raw = abc.raw
    for s in ("Send", "Recv", "RedWar"):
        if _str_index(abc, s) is None:
            raw = _add_string(raw, abc, s)
            abc = ABC(raw)
    blocks = list(blocks)
    blocks[main_i] = (blocks[main_i][0], blocks[main_i][1], abc.raw)
    send_mi, send_at, send_blob = _send_site(abc, cls_mn, out_mn)
    recv_mi, recv_at, recv_blob = _recv_site(abc, cls_mn, in_mn)
    marks = []
    for mi, label in ((send_mi, "Send"), (recv_mi, "Recv"),
                      (_redwar_init(abc), "RedWar")):
        at = _scope_end(abc.bodies[mi])
        blob = _mark_blob(cls_mn, mark_mn, _str_index(abc, label))
        marks.append((mi, at, blob))
        if mi == send_mi and send_at >= at:
            send_at += len(blob)
        if mi == recv_mi and recv_at >= at:
            recv_at += len(blob)
    code = dict(abc.bodies)
    exc = {mi: list(abc.body_pack[mi]["exc"]) for mi in abc.body_order}
    stack = {}
    for mi, at, blob in marks + [(send_mi, send_at, send_blob),
                                 (recv_mi, recv_at, recv_blob)]:
        code[mi] = _insert(code[mi], at, blob)
        exc[mi] = _shift_exc(exc[mi], at, len(blob))
        stack[mi] = max(stack.get(mi, abc.body_pack[mi]["max_stack"]),
                        abc.body_pack[mi]["max_stack"] + 6)
    _force_fund_buy(abc, code, exc)
    _open_fund_buy(abc, code)
    _arm_fund_click(abc, code, exc)
    _log_payorder(abc, code, exc, stack, cls_mn, pay_mn, result_mn)
    new_abc = _rebuild(abc, code, exc, stack)
    _check(new_abc, send_mi, recv_mi)
    new_body = _swap_abc(body, blocks[main_i], new_abc)
    out = _pack_swf(new_body, ver)
    with open(dest, "wb") as f:
        f.write(out)


def _abc_blocks(body: bytes):
    out = []
    for code, payload, off in iter_tags(body):
        if code != 82:
            continue
        z = payload.index(b"\x00", 4)
        out.append((off, payload[: z + 1], payload[z + 1:]))
    if not out:
        raise ValueError("SWF 里没有 ABC")
    return out


def _hook_names(abc: ABC, blocks, main_i):
    found = _find_hook(abc)
    if found:
        return (*found, abc, blocks)
    for i, (_, _, data) in enumerate(blocks):
        if i == main_i:
            continue
        other = ABC(data)
        found = _find_hook(other)
        if not found:
            continue
        cls_mn, out_mn, in_mn, pay_mn, result_mn, mark_mn = found
        raw, idxs = _copy_names(abc, other, (cls_mn, out_mn, in_mn, pay_mn, result_mn, mark_mn))
        abc = ABC(raw)
        if abc.qname(idxs[0]) != "pkt:PktHook":
            raise ValueError(f"主程序里的类名是 {abc.qname(idxs[0])}")
        blocks = list(blocks)
        head = blocks[main_i][1]
        blocks[main_i] = (blocks[main_i][0], head, raw)
        return (*idxs, abc, blocks)
    raise ValueError("SWF 里没有 pkt.PktHook，ffdec 没有把上报类加进去")


def _has_static(abc: ABC, qn: str, method: str) -> bool:
    for idx, inst in enumerate(abc.instances):
        if abc.qname(inst["name"]) != qn:
            continue
        for name, kind, _mi in abc.classes[idx]["traits"]:
            if kind == "method" and name == method:
                return True
    return False


def _find_hook(abc: ABC):
    cls_mn = out_mn = in_mn = pay_mn = result_mn = mark_mn = None
    for idx, inst in enumerate(abc.instances):
        if abc.qname(inst["name"]) != "pkt:PktHook":
            continue
        cls_mn = inst["name"]
        traits = abc.classes[idx]["traits"]
        mns = abc.trait_mn.get(id(traits), [])
        for (name, kind, _), mn in zip(traits, mns):
            if kind != "method":
                continue
            if name == "reportOut":
                out_mn = mn
            elif name == "reportIn":
                in_mn = mn
            elif name == "reportPay":
                pay_mn = mn
            elif name == "reportPayResult":
                result_mn = mn
            elif name == "mark":
                mark_mn = mn
    if cls_mn is None:
        return None
    if None in (out_mn, in_mn, pay_mn, result_mn, mark_mn):
        raise ValueError("PktHook 缺少 reportPay、reportPayResult 或 mark")
    return cls_mn, out_mn, in_mn, pay_mn, result_mn, mark_mn


def _mark_blob(cls_mn: int, meth_mn: int, si: int) -> bytes:
    return (bytes([0x60]) + _u30b(cls_mn) + bytes([0x2C]) + _u30b(si)
            + bytes([0x4F]) + _u30b(meth_mn) + _u30b(1))


def _scope_end(code: bytes) -> int:
    ins = _parse(code)
    for i, cur in enumerate(ins):
        if cur["name"] == "pushscope" and i + 1 < len(ins):
            return ins[i + 1]["addr"]
    raise ValueError("方法开头没有 pushscope")


def _redwar_init(abc: ABC) -> int:
    for inst in abc.instances:
        if abc.qname(inst["name"]) == "com.sincetimes.redwar.game:RedWar":
            return inst["iinit"]
    raise ValueError("找不到 RedWar 构造函数")


def _copy_names(abc: ABC, src: ABC, mns):
    """PktHook 若在另一块 ABC 里，把同名的 QName 补进主程序的常量池。

    类本身还在那一块 ABC 里。这里只补名字，调用时按名字去找。
    """
    raw = abc.raw
    idxs = []
    for mn in mns:
        kind, pay = src.multinames[mn]
        if kind not in (0x07, 0x0D):
            raise ValueError(f"PktHook 的名字不是 QName（kind {kind:#x}）")
        ns_kind, ns_ni = src.namespaces[pay[0]]
        ns_name = src.strings[ns_ni]
        name = src.strings[pay[1]]
        raw, idx = _ensure_qname(raw, ns_kind, ns_name, name)
        idxs.append(idx)
    return raw, idxs


def _ensure_qname(raw: bytes, ns_kind: int, ns_name: str, name: str):
    abc = ABC(raw)
    ns_i = _ns_index(abc, ns_kind, ns_name)
    name_i = _str_index(abc, name)
    if ns_i is None or name_i is None:
        raw = _append_pool(raw, abc, ns_kind, ns_name, name)
        abc = ABC(raw)
        ns_i = _ns_index(abc, ns_kind, ns_name)
        name_i = _str_index(abc, name)
    for i, (kind, pay) in enumerate(abc.multinames):
        if kind == 0x07 and pay == (ns_i, name_i):
            return raw, i
    entry = bytes([0x07]) + _u30b(ns_i) + _u30b(name_i)
    idx = len(abc.multinames)
    raw = _bump_count(raw, abc.pool_at["mn"], idx, entry, abc.pool_at["mn_end"])
    return raw, idx


def _ns_index(abc: ABC, kind: int, name: str):
    for i, (k, ni) in enumerate(abc.namespaces):
        if i and k == kind and abc.strings[ni] == name:
            return i
    return None


def _str_index(abc: ABC, s: str):
    if s == "":
        return 0
    for i, cur in enumerate(abc.strings):
        if i and cur == s:
            return i
    return None


def _append_pool(raw: bytes, abc: ABC, ns_kind: int, ns_name: str, name: str) -> bytes:
    if _str_index(abc, ns_name) is None:
        raw = _add_string(raw, abc, ns_name)
        abc = ABC(raw)
    if _str_index(abc, name) is None:
        raw = _add_string(raw, abc, name)
        abc = ABC(raw)
    if _ns_index(abc, ns_kind, ns_name) is None:
        ni = _str_index(abc, ns_name)
        entry = bytes([ns_kind]) + _u30b(ni)
        raw = _bump_count(raw, abc.pool_at["ns"], len(abc.namespaces), entry, abc.pool_at["nss"])
    return raw


def _add_string(raw: bytes, abc: ABC, s: str) -> bytes:
    data = s.encode("utf-8")
    return _bump_count(raw, abc.pool_at["str"], len(abc.strings),
                       _u30b(len(data)) + data, abc.pool_at["ns"])


def _bump_count(raw: bytes, count_at: int, count: int, entry: bytes, insert_at: int) -> bytes:
    old, new = _u30b(count), _u30b(count + 1)
    if len(old) != len(new):
        raise ValueError("常量池计数的编码长度变了，不敢改")
    # count_at 处的 u30 宽度必须和 old 一致
    width = _u30_width(raw, count_at)
    if width != len(old):
        raise ValueError("常量池计数宽度对不上")
    return raw[:count_at] + new + raw[count_at + width:insert_at] + entry + raw[insert_at:]


def _u30_width(b, p) -> int:
    start = p
    _, p = _u30(b, p)
    return p - start


def _u30b(v: int) -> bytes:
    out = bytearray()
    while True:
        c = v & 0x7F
        v >>= 7
        if v:
            out.append(c | 0x80)
        else:
            out.append(c)
            return bytes(out)


def _send_site(abc: ABC, cls_mn: int, meth_mn: int):
    mi = _method(abc, "com.sincetimes.redwar.game.comnunicate:Transport", "Send")
    ins = _parse(abc.bodies[mi])
    for i, cur in enumerate(ins):
        if cur["name"] != "callpropvoid" or abc.name_of(cur["mn"]) != "writeExternal":
            continue
        prev = ins[i - 2:i]
        if [p["name"] for p in prev] != ["getlocal1", "getlocal"]:
            continue
        if prev[1]["ops"] != ["4"]:
            continue
        blob = (bytes([0x60]) + _u30b(cls_mn)
                + bytes([0xD1, 0x62]) + _u30b(4)
                + bytes([0x4F]) + _u30b(meth_mn) + _u30b(2))
        return mi, cur["end"], blob
    raise ValueError("Send 里找不到 writeExternal 之后的插入点")


def _recv_site(abc: ABC, cls_mn: int, meth_mn: int):
    mi = None
    for idx, inst in enumerate(abc.instances):
        if abc.qname(inst["name"]).endswith(":Transport"):
            for name, kind, m in abc.classes[idx]["traits"]:
                if kind == "method" and m is not None and _has(abc, m, "decode"):
                    mi = m
    if mi is None:
        raise ValueError("找不到收包方法")
    ins = _parse(abc.bodies[mi])
    for i, cur in enumerate(ins):
        if cur["name"] != "callpropvoid" or abc.name_of(cur["mn"]) != "readExternal":
            continue
        # 语义上紧挨调用的是「getslot 消息 / getscopeobject 1 / getslot 字节数组」。
        # 2026102601 起混淆把消息对象的 getscopeobject 和 getslot 拆开了。
        # 2026103001 又在 getscopeobject 和字节数组的 getslot 之间插了无条件 jump，
        # 跳过几条垃圾指令。只认这种短跳，顺着它找回这三条。
        picked = []
        idx = i
        for _ in range(3):
            addr = ins[idx]["addr"]
            prev_i = idx - 1
            for j in range(idx - 1, -1, -1):
                prev = ins[j]
                if addr - prev["addr"] > 80:
                    break
                off = prev["ops"][0] if prev["ops"] else None
                if (prev["name"] == "jump" and isinstance(off, int)
                        and 0 < off <= 64 and prev["end"] + off == addr):
                    prev_i = j - 1
                    break
            if prev_i < 0:
                break
            picked.append(ins[prev_i])
            idx = prev_i
        if len(picked) != 3:
            continue
        arg, base, msg = picked
        if (msg["name"] != "getslot" or base["text"] != "getscopeobject 1"
                or arg["name"] != "getslot"):
            continue
        blob = (bytes([0x60]) + _u30b(cls_mn)
                + bytes([0x65, 0x01, 0x6C]) + _u30b(int(msg["ops"][0]))
                + bytes([0x4F]) + _u30b(meth_mn) + _u30b(1))
        return mi, cur["end"], blob
    raise ValueError("收包方法里找不到 readExternal 之后的插入点")


def _s24_to(off: int) -> bytes:
    if off < -(1 << 23) or off >= (1 << 23):
        raise ValueError(f"跳转超出 24 位：{off}")
    if off < 0:
        off += 1 << 24
    return bytes((off & 0xFF, (off >> 8) & 0xFF, (off >> 16) & 0xFF))


def _buy_join(abc: ABC, ins):
    """growthFund_buyPrice 之后，_-5OY 调用前的 getlocal0。两条分支都汇合在这里。"""
    seen = False
    for i, cur in enumerate(ins):
        if cur["name"] == "pushstring":
            si = int(cur["ops"][0])
            if 0 <= si < len(abc.strings) and abc.strings[si] == "growthFund_buyPrice":
                seen = True
        if not seen or cur["name"] != "callpropvoid" or abc.name_of(cur["mn"]) != "_-5OY":
            continue
        if int(cur["ops"][1]) != 3 or i < 2:
            continue
        if ins[i - 1]["name"] != "getproperty" or ins[i - 2]["name"] != "getlocal0":
            raise ValueError("成长基金购买按钮的调用形状变了")
        return ins[i - 2]
    return None


def _force_fund_buy(abc: ABC, code: dict, exc: dict) -> None:
    """成长基金 buyBtn 交给 _-5OY 的开关改成 true。

    条件不成立时 iffalse 直接跳到汇合点，插在汇合点前面会被跳过去。
    所以 pop/pushtrue 放在汇合点上，再把这些跳转指过来。
    """
    for idx, inst in enumerate(abc.instances):
        if not abc.qname(inst["name"]).endswith(":GrowthFundPanel"):
            continue
        for name, kind, m in inst["traits"]:
            if kind != "method" or m is None or m not in code:
                continue
            ins = _parse(code[m])
            if _buy_join(abc, ins) is None:
                continue
            join = _buy_join(abc, ins)
            pos = ins.index(join)
            if not (pos >= 2 and ins[pos - 1]["name"] == "pushtrue"
                    and ins[pos - 2]["op"] == 0x29):
                code[m] = _insert(code[m], join["addr"], bytes([0x29, 0x26]))
                exc[m] = _shift_exc(exc[m], join["addr"], 2)
                ins = _parse(code[m])
                join = _buy_join(abc, ins)
                pos = ins.index(join)
            pop_at = ins[pos - 2]["addr"]
            raw = bytearray(code[m])
            changed = False
            for cur in ins:
                if cur["sig"] != ("s24",):
                    continue
                dest = cur["end"] + cur["ops"][0]
                if dest != join["addr"]:
                    continue
                raw[cur["addr"] + 1:cur["addr"] + 4] = _s24_to(pop_at - cur["end"])
                changed = True
            if changed:
                code[m] = bytes(raw)
            return
    raise ValueError("找不到成长基金 980Q 购买按钮")


def _mn_named(abc: ABC, name: str) -> int:
    hits = [i for i in range(1, len(abc.multinames)) if abc.name_of(i) == name]
    if not hits:
        raise ValueError(f"没有名字 {name}")
    for i in hits:
        if abc.ns_of(i) in ("", "*"):
            return i
    return hits[0]


def _arm_fund_click(abc: ABC, code: dict, exc: dict) -> None:
    """_-5OY 开头强制 local2=true，并直接打开按钮的 mouseEnabled。

    调用点把 true 放进参数之后，假分支仍可能在进这个函数前就把点击丢掉。
    这里不看参数，登记函数自己打开鼠标。
    """
    me = _mn_named(abc, "mouseEnabled")
    kids = _mn_named(abc, "mouseChildren")
    blob = bytes([0x26, 0xD6, 0xD1, 0x26, 0x61]) + _u30b(me)
    # 文字盖在按钮上时，点文字不会落到按钮。关掉子对象命中，整块都由按钮接收。
    kids_blob = bytes([0xD1, 0x27, 0x61]) + _u30b(kids)
    for idx, inst in enumerate(abc.instances):
        if not abc.qname(inst["name"]).endswith(":GrowthFundPanel"):
            continue
        for name, kind, mi in inst["traits"]:
            if name != "_-5OY" or kind != "method" or mi not in code:
                continue
            ins = _parse(code[mi])
            scope = next(cur for cur in ins if cur["name"] == "pushscope")
            nxt = ins[ins.index(scope) + 1]
            if not (nxt["name"] == "pushtrue" and ins[ins.index(scope) + 2]["op"] == 0xD6):
                at = nxt["addr"]
                code[mi] = _insert(code[mi], at, blob)
                exc[mi] = _shift_exc(exc.get(mi, []), at, len(blob))
                ins = _parse(code[mi])
            if not any(cur["name"] == "setproperty" and cur.get("mn")
                       and abc.name_of(cur["mn"]) == "mouseChildren" for cur in ins):
                me_set = next(cur for cur in ins
                              if cur["name"] == "setproperty" and cur.get("mn")
                              and abc.name_of(cur["mn"]) == "mouseEnabled")
                at = ins[ins.index(me_set) + 1]["addr"]
                code[mi] = _insert(code[mi], at, kids_blob)
                exc[mi] = _shift_exc(exc.get(mi, []), at, len(kids_blob))
                ins = _parse(code[mi])
            raw = bytearray(code[mi])
            ret = next(cur["addr"] for cur in ins if cur["name"] == "returnvoid")
            changed = False
            for cur in ins:
                if cur["name"] != "iffalse" or cur["sig"] != ("s24",):
                    continue
                if cur["end"] + cur["ops"][0] != ret:
                    continue
                raw[cur["addr"] + 1:cur["addr"] + 4] = _s24_to(0)
                changed = True
            if changed:
                code[mi] = bytes(raw)
            return
    raise ValueError("找不到成长基金点击登记")


def _pay_blob(cls_mn: int, meth_mn: int) -> bytes:
    """dup 之后把栈顶字符串交给 PktHook，原来的那份留给后面的 URLRequest / 比较。"""
    return (bytes([0x2A, 0x5D]) + _u30b(cls_mn) + bytes([0x2B, 0x4F])
            + _u30b(meth_mn) + _u30b(1))


def _log_payorder(abc: ABC, code: dict, exc: dict, stack: dict,
                  cls_mn: int, pay_mn: int, result_mn: int) -> None:
    """BUY 拼好 payorder 地址、以及回包 data 时，经现有 __pkt 写进抓包日志。"""
    url_at = body_at = None
    url_mi = body_mi = None
    for mi, body in code.items():
        ins = _parse(body)
        strings = []
        for cur in ins:
            if cur["name"] == "pushstring":
                si = int(cur["ops"][0])
                if 0 <= si < len(abc.strings):
                    strings.append(abc.strings[si])
        if "payorder.war?" in strings:
            for cur in ins:
                if cur["name"] == "constructprop" and abc.name_of(cur["mn"]) == "URLRequest":
                    url_mi, url_at = mi, cur["addr"]
        if "QPointResult:" in strings:
            for i, cur in enumerate(ins):
                if cur["name"] == "getproperty" and abc.name_of(cur["mn"]) == "data":
                    body_mi, body_at = mi, ins[i + 1]["addr"]
                    break
    if url_mi is None or body_mi is None:
        raise ValueError("找不到 payorder 的请求或回包")
    for mi, at, meth in ((url_mi, url_at, pay_mn), (body_mi, body_at, result_mn)):
        blob = _pay_blob(cls_mn, meth)
        code[mi] = _insert(code[mi], at, blob)
        exc[mi] = _shift_exc(exc.get(mi, []), at, len(blob))
        stack[mi] = max(stack.get(mi, abc.body_pack[mi]["max_stack"]), 
                        abc.body_pack[mi]["max_stack"]) + 4


def _open_fund_buy(abc: ABC, code: dict) -> None:
    """_-6L3 在 passid 为 0 时直接 return，点下去没有任何界面。

    把这个 ifeq 的跳转改成 0，比较完仍继续调用 BUY._-s5。
    """
    for idx, inst in enumerate(abc.instances):
        if not abc.qname(inst["name"]).endswith(":GrowthFundPanel"):
            continue
        for name, kind, mi in inst["traits"]:
            if name != "_-6L3" or kind != "method" or mi not in code:
                continue
            ins = _parse(code[mi])
            by = {cur["addr"]: cur for cur in ins}
            for i, cur in enumerate(ins):
                if cur["name"] != "ifeq" or i < 2:
                    continue
                if ins[i - 1]["text"] != "pushbyte 0" or ins[i - 2]["name"] != "getproperty":
                    continue
                if abc.name_of(ins[i - 2]["mn"]) != "_-6QS":
                    continue
                off = cur["ops"][0]
                if off == 0:
                    return
                target = cur["end"] + off
                if target not in by or by[target]["name"] != "returnvoid":
                    raise ValueError("passid 判断没有跳到 return")
                raw = bytearray(code[mi])
                raw[cur["addr"] + 1:cur["addr"] + 4] = b"\x00\x00\x00"
                code[mi] = bytes(raw)
                return
    raise ValueError("找不到成长基金购买点击")


def enable_fund_buy(path: str) -> None:
    """只改磁盘上已经打过抓包钩子的主 SWF，让 980Q 按钮能进购买。"""
    raw = open(path, "rb").read()
    body, _sig, ver, _ = uncompress(raw)
    blocks = _abc_blocks(body)
    main_i = max(range(len(blocks)), key=lambda i: len(blocks[i][2]))
    abc = ABC(blocks[main_i][2])
    code = dict(abc.bodies)
    exc = {mi: list(abc.body_pack[mi]["exc"]) for mi in abc.body_order}
    _force_fund_buy(abc, code, exc)
    _open_fund_buy(abc, code)
    _arm_fund_click(abc, code, exc)
    new_body = _swap_abc(body, blocks[main_i], _rebuild(abc, code, exc, {}))
    tmp = path + ".buy.tmp"
    with open(tmp, "wb") as f:
        f.write(_pack_swf(new_body, ver))
    os.replace(tmp, path)


def _method(abc: ABC, qn: str, name: str) -> int:
    for idx, inst in enumerate(abc.instances):
        if abc.qname(inst["name"]) != qn:
            continue
        for n, kind, mi in abc.classes[idx]["traits"]:
            if n == name and kind == "method" and mi is not None:
                return mi
    raise ValueError(f"没有 {qn}.{name}")


def _has(abc: ABC, mi: int, tail: str) -> bool:
    code = abc.bodies.get(mi)
    if not code:
        return False
    return any(ins["mn"] is not None and abc.name_of(ins["mn"]) == tail
               for ins in _parse(code))


def _parse(code: bytes):
    out = []
    p, n = 0, len(code)
    while p < n:
        addr = p
        op = code[p]
        ent = OPS.get(op)
        if ent is None:
            raise ValueError(f"未知字节码 0x{op:02x} @ {addr}")
        p += 1
        name, sig = ent
        ops, notes, branches = [], [], []
        if sig == ("switch",):
            base, p = _s24(code, p)
            cnt, p2 = _u30(code, p)
            p = p2
            branches.append(("switch", addr, base))
            cases = []
            for _ in range(cnt + 1):
                t, p = _s24(code, p)
                cases.append(t)
                branches.append(("switch", addr, t))
            ops.append((cnt, cases))
        else:
            for kind in sig:
                if kind == "s24":
                    v, p = _s24(code, p)
                    ops.append(v)
                    notes.append(f"-> {p + v}")
                    branches.append(("jump", p, v))
                elif kind == "s8":
                    v = code[p] - 256 if code[p] > 127 else code[p]
                    p += 1
                    ops.append(str(v))
                elif kind == "u8":
                    ops.append(str(code[p]))
                    p += 1
                else:
                    v, p = _u30(code, p)
                    ops.append(str(v))
                    if kind == "mn" and v < 1:
                        notes.append("*")
        text = (name + " " + " ".join(str(x) if not isinstance(x, tuple) else "" for x in ops)).strip()
        note = ""
        if sig and sig[0] == "mn":
            note = name
        out.append({"addr": addr, "end": p, "name": name, "op": op, "sig": sig,
                    "ops": ops, "raw": code[addr:p], "text": text,
                    "note": notes[0] if notes else note, "br": branches,
                    "mn": int(ops[0]) if sig and sig[0] == "mn" else None})
    return out


def _insert(code: bytes, at: int, blob: bytes) -> bytes:
    ins = _parse(code)
    if not any(i["addr"] == at for i in ins) and at != len(code):
        raise ValueError(f"插入点 {at} 不在指令边界上")
    n = len(blob)

    def mp(a):
        return a + n if a >= at else a

    out = bytearray()
    for cur in ins:
        if cur["addr"] == at:
            out += blob
        out += _encode(cur, mp)
    if at == len(code):
        out += blob
    return bytes(out)


def _encode(cur, mp) -> bytes:
    if not cur["br"]:
        return cur["raw"]
    raw = bytearray([cur["op"]])
    if cur["sig"] == ("switch",):
        cnt, cases = cur["ops"][0]
        base = cur["br"][0][2]
        raw += _s24b(mp(cur["addr"] + base) - mp(cur["addr"]))
        raw += _u30b(cnt)
        for off in cases:
            raw += _s24b(mp(cur["addr"] + off) - mp(cur["addr"]))
        return bytes(raw)
    # 普通指令：只改 s24，其余原样抄。s24 一定是唯一操作数。
    v = cur["ops"][0]
    end = cur["end"]
    target = end + v
    new_end = mp(cur["addr"]) + 4
    raw += _s24b(mp(target) - new_end)
    return bytes(raw)


def _s24b(v: int) -> bytes:
    if v < -(1 << 23) or v >= (1 << 23):
        raise ValueError(f"跳转超出 24 位：{v}")
    if v < 0:
        v += 1 << 24
    return bytes((v & 0xFF, (v >> 8) & 0xFF, (v >> 16) & 0xFF))


def _shift_exc(exc, at, n):
    out = []
    for fr, to, tg, et, vn in exc:
        if fr >= at:
            fr += n
        if to >= at:
            to += n
        if tg >= at:
            tg += n
        out.append((fr, to, tg, et, vn))
    return out


def _rebuild(abc: ABC, code: dict, exc: dict, stack: dict) -> bytes:
    out = bytearray(abc.raw[:abc.bodies_at])
    out += _u30b(len(abc.body_order))
    for mi in abc.body_order:
        info = abc.body_pack[mi]
        body = code[mi]
        out += _u30b(mi)
        out += _u30b(stack.get(mi, info["max_stack"]))
        out += _u30b(info["local_count"])
        out += _u30b(info["init_scope"])
        out += _u30b(info["max_scope"])
        out += _u30b(len(body))
        out += body
        items = exc[mi]
        out += _u30b(len(items))
        for row in items:
            for x in row:
                out += _u30b(x)
        out += info["traits"]
    return bytes(out)


def _check(raw: bytes, send_mi: int, recv_mi: int) -> None:
    abc = ABC(raw)
    for mi in (send_mi, recv_mi):
        ins = _parse(abc.bodies[mi])
        ends = {i["addr"] for i in ins}
        for cur in ins:
            for kind, origin, off in cur["br"]:
                target = (cur["end"] + off) if kind == "jump" else (cur["addr"] + off)
                if target not in ends:
                    raise ValueError(f"方法 {mi} 跳到了 {target}，不在指令上")
        if not any(i["name"] == "getlex" for i in ins):
            raise ValueError(f"方法 {mi} 没有插入 getlex")


def _swap_abc(body: bytes, block, new_abc: bytes) -> bytes:
    off, head, _old = block
    # off 是原来 payload 的起点。常量池补过名字之后，_old 可能已经变长，
    # 所以长度只信文件里的标签头。
    header_at = off - 6
    rh = struct.unpack_from("<H", body, header_at)[0]
    if (rh & 0x3F) != 0x3F or (rh >> 6) != 82:
        raise ValueError("DoABC 标签头对不上")
    old_len = struct.unpack_from("<I", body, header_at + 2)[0]
    if body[off:off + len(head)] != head:
        raise ValueError("DoABC 名字对不上")
    payload = head + new_abc
    return (body[:header_at + 2]
            + struct.pack("<I", len(payload))
            + payload
            + body[off + old_len:])


def _pack_swf(body: bytes, ver: int) -> bytes:
    comp = zlib.compress(body, 9)
    return b"CWS" + bytes([ver]) + struct.pack("<I", 8 + len(body)) + comp
