#!/usr/bin/env python3
# tankstorm-keepalive  Copyright (C) 2026 Dimlitter
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# 本程序是自由软件：你可以依据自由软件基金会发布的 GNU Affero 通用公共许可证
# （第 3 版，或你选择的任何更新版本）之条款，再分发和/或修改它。
# 本程序希望能有用，但不提供任何担保；甚至不含适销性或特定用途适用性的默示担保。
# 详见随附的 LICENSE 文件，或 <https://www.gnu.org/licenses/>。
"""从 RedWar SWF 还原完整协议：opcode 表 + 带字段号和类型的 .proto。

    python3 extract_proto.py <swf> <输出目录>

三步
----
1. opcode 表：注册函数把「base + offset 的立即数 + 消息类」配在一起。旧版这个
   函数叫 _-055；secureSWF 每次更新都会改名，还会在两个立即数中间插 if/jump。
   对不上时按调用形状重找：离调用最近的一次 add，把它的两个操作数相加。
2. 真实消息名：类名被改成 _-4mF 这种，但 protobuf-as3 的报错模板
   'Bad data format: <消息名>.<字段名> cannot be set twice.' 是字符串字面量，
   混淆器动不了。
3. 字段号和类型：writeToBuffer 里 writeTag 的 tag 是立即数。写类型的函数
   旧版有一份写死的对照；新版改名之后，改看方法体调用了 writeUTFBytes、
   writeByte、writeDouble 里的哪一个。

交互流程（谁先 Send 谁）不在这张表里，见 tools/build_wiremap.py。
"""
import json
import os
import re
import sys

from disasm import load, disasm, _s24, _u30

BAD = re.compile(r"Bad data format: ([A-Za-z_]\w*)\.(\w+) cannot be set twice\.")
NUMPUSH = ("pushbyte", "pushshort", "pushint", "pushuint")


def _switch_cases(abc, code, lines):
    """取 readExternal 里 lookupswitch 的**非 default** 分支偏移，按 case 序排列。

    AVM2 的 lookupswitch 是 default 偏移 + case_count + (case_count+1) 个目标。
    编译器会把 switch 的取值归一化到 0 起始，所以 case 序号和字段号之间差多少
    取决于该消息最小的字段号 —— 不能假定差 1（RceAdmiralVisit 的字段从 2 开始）。
    字段号有空洞时，那些 case 直接指向 default。
    因此这里只把"真正的分支"按序取出，交给调用方和升序字段号逐个配对。
    """
    for addr, txt, _ in lines:
        if not txt.startswith("lookupswitch"):
            continue
        p = addr + 1
        dflt, p = _s24(code, p)
        cnt, p = _u30(code, p)
        out = []
        for _ in range(cnt + 1):
            t, p = _s24(code, p)
            out.append(addr + t)
        return [t for t in out if t != addr + dflt], sorted(set(out))
    return [], []


def num_of(t):
    p = t.split()
    if p[0] in NUMPUSH and len(p) > 1:
        try:
            return int(p[1])
        except ValueError:
            return None
    return None


CALL = re.compile(r"^([A-Za-z_][\w-]*|_-[\w-]+)\(")
_CALL_ARG = re.compile(r"^([A-Za-z_][\w-]*|_-[\w-]+)\((\d+) 参\)$")
_SKIP = {
    "add", "subtract", "multiply", "divide", "modulo", "lshift", "rshift", "urshift",
    "bitand", "bitor", "bitxor", "add_i", "subtract_i", "multiply_i",
    "increment", "decrement", "increment_i", "decrement_i",
    "inclocal", "declocal", "inclocal_i", "declocal_i",
    "negate", "negate_i", "not", "bitnot", "dup", "swap", "pop",
    "convert_i", "convert_u", "convert_d", "convert_b", "convert_s", "convert_o",
    "coerce", "coerce_a", "coerce_s",
    "getlocal", "getlocal0", "getlocal1", "getlocal2", "getlocal3",
    "setlocal", "setlocal0", "setlocal1", "setlocal2", "setlocal3",
    "pushnull", "pushundefined", "pushtrue", "pushfalse",
    "getscopeobject", "getglobalscope", "getslot", "setslot",
    "findpropstrict", "findproperty", "getproperty",
    "debug", "debugline", "debugfile", "label", "nop", "bkpt", "bkptline",
}
_HEAVY = {"writeDouble", "writeFloat", "writeUTFBytes", "writeUTF", "writeBytes",
          "writeInt", "writeShort", "writeUnsignedInt"}
_SCALAR = {"int", "uint", "String", "Number", "Boolean", "ByteArray", "Object",
           "Array", "*", "null"}
# 这些名字只在 RedWar_2026101403 前后的 SWF 里成立。新版本对不上就改走形状识别。
_LEGACY_TYPES = {
    "_-1lP": "double", "_-6pT": "float", "_-30c": "bool", "_-yF": "string",
    "_-6YX": "bytes", "_-jt": "enum", "_-1CD": "message", "_-AL": "group",
    "write_TYPE_INT64": "int64", "write_TYPE_UINT64": "uint64",
    "write_TYPE_INT32": "int32", "write_TYPE_FIXED64": "fixed64",
    "write_TYPE_FIXED32": "fixed32", "write_TYPE_UINT32": "uint32",
    "write_TYPE_SFIXED32": "sfixed32", "write_TYPE_SFIXED64": "sfixed64",
    "write_TYPE_SINT32": "sint32", "write_TYPE_SINT64": "sint64",
}


def _call(c):
    m = _CALL_ARG.match(c or "")
    if not m:
        return None, None
    return m.group(1), int(m.group(2))


def _imm(t, c):
    """pushint / pushuint 的操作数是常量池下标，值在注释里。"""
    p = t.split()
    if not p or p[0] not in NUMPUSH:
        return None
    if p[0] in ("pushint", "pushuint"):
        try:
            return int((c or "").strip())
        except ValueError:
            return None
    return num_of(t)


def _short_index(qnames):
    out, bad = {}, set()
    for q in qnames:
        s = q.split(":")[-1]
        if s in out and out[s] != q:
            bad.add(s)
        else:
            out[s] = q
    for s in bad:
        out.pop(s, None)
    return out


def _q_of(t, c, proto_q, short_to_q):
    op = t.split()[0] if t else ""
    if op == "getlex" and c in proto_q:
        return c
    if op in ("getlex", "constructprop", "findpropstrict", "findproperty"):
        name = (c or "").split("(")[0].split(":")[-1]
        q = short_to_q.get(name)
        if q in proto_q:
            return q
    return None


def _opcode_from(nums):
    if not nums:
        return None
    total = sum(nums)
    if 0x0100 <= total <= 0x0FFF:
        return total
    rest = [n for n in nums if n > 16 or n < 0]
    total = sum(rest)
    if rest and 0x0100 <= total <= 0x0FFF:
        return total
    if len(nums) >= 2:
        pair = nums[0] + nums[1]
        if 0x0100 <= pair <= 0x0FFF:
            return pair
    for n in nums:
        if 0x0100 <= n <= 0x0FFF:
            return n
    return None


def _reg_window(lines, i, proto_q, short_to_q):
    # 先认 add 的两个操作数。假分支会把 base 和 offset 拆开，只拿最近的那个数
    # 会把 512+257 认成 257。
    nums, klass, seen = [], None, False
    j, steps = i - 1, 0
    while j >= 0 and steps < 80 and len(nums) < 2:
        steps += 1
        _, t, c = lines[j]
        op = t.split()[0]
        if _call(c)[0]:
            break
        q = _q_of(t, c, proto_q, short_to_q)
        if q and klass is None:
            klass = q
        elif klass and not seen and op in ("add", "add_i"):
            seen = True
        elif seen:
            v = _imm(t, c)
            if v is not None:
                nums.append(v)
        j -= 1
    if klass and seen and len(nums) >= 2:
        opv = _opcode_from(nums[:2])
        if opv is not None:
            return opv, klass

    nums, klass = [], None
    j, steps = i - 1, 0
    while j >= 0 and steps < 24:
        steps += 1
        _, t, c = lines[j]
        op = t.split()[0]
        q = _q_of(t, c, proto_q, short_to_q)
        if q:
            klass = q
            j -= 1
            continue
        v = _imm(t, c)
        if v is not None:
            nums.append(v)
            j -= 1
            continue
        if op in _SKIP:
            j -= 1
            continue
        break
    if not klass:
        return None
    opv = _opcode_from(nums)
    if opv is None:
        return None
    return opv, klass


def _legacy_opcodes(lines):
    """旧版注册循环。符号还在时走这里，结果和原先逐条一致。"""
    reg, nums, klass = {}, [], None
    for _, t, c in lines:
        if c == "_-055(2 参)":
            if klass and nums:
                op = sum(nums)
                if 0x0100 <= op <= 0x0FFF:
                    reg.setdefault(op, klass)
            nums, klass = [], None
            continue
        v = num_of(t)
        if v is not None:
            nums.append(v)
        elif t.startswith("getlex") and "protocol:" in c:
            klass = c
        elif "_-5sY" in c:
            nums, klass = [], None
    return reg


def _is_transport_send(lines, i):
    name, argc = _call(lines[i][2])
    if name != "Send" or argc != 1:
        return False
    for _, t, c in lines[max(0, i - 8):i]:
        if t.split()[0] not in ("getlex", "findpropstrict", "findproperty", "getproperty"):
            continue
        tail = (c or "").split("(")[0].split(":")[-1]
        if tail == "Transport":
            return True
    return False


def _anchor(s):
    if not s or s.startswith("Bad data format:"):
        return False
    if len(s) < 4 or s.startswith("_-"):
        return False
    return True


def _flows_in(lines, abc, proto_q, short_to_q, real):
    sends, pending, anchors = [], None, []
    for i, (_, t, c) in enumerate(lines):
        if t.startswith("pushstring"):
            try:
                s = abc.strings[int(t.split()[1])]
            except (ValueError, IndexError):
                s = ""
            if _anchor(s) and len(anchors) < 12:
                anchors.append(s[:80])
        q = _q_of(t, c, proto_q, short_to_q)
        if q:
            pending = q
        if pending and _is_transport_send(lines, i):
            name = real.get(pending) or ""
            sends.append(name if name and not name.startswith("_-") else "?")
            pending = None
    if not sends:
        return None
    return {"sends": sends, "anchors": anchors, "source": "bytecode"}


def _body_calls(abc, by_q, fname):
    seen = set()
    for _, (inst, cls) in by_q.items():
        for group in (inst["traits"], cls["traits"]):
            for n, _k, mi in group:
                if n != fname or mi not in abc.bodies:
                    continue
                for _, _t, c in disasm(abc, abc.bodies[mi]):
                    name, _argc = _call(c)
                    if name:
                        seen.add(name)
    return seen


def _discover_tag(write_lines, abc, by_q):
    counts = {}
    for lines in write_lines:
        last = None
        for _, t, c in lines:
            v = num_of(t)
            if v is not None:
                last = v
                continue
            name, argc = _call(c)
            if name and argc == 3 and last is not None:
                counts[name] = counts.get(name, 0) + 1
            if name:
                last = None
    ranked = sorted(counts, key=counts.get, reverse=True)[:8]
    for name in ranked:
        seen = _body_calls(abc, by_q, name)
        if ("writeByte" in seen and not (seen & _HEAVY)
                and "writeExternal" not in seen and "writeToBuffer" not in seen):
            return name
    return ranked[0] if ranked else None


def _type_fn(abc, by_q, cache):
    def type_of(name, tag_name):
        if not name or name == tag_name:
            return None
        key = (name, tag_name)
        if key in cache:
            return cache[key]
        if name in _LEGACY_TYPES:
            cache[key] = _LEGACY_TYPES[name]
            return cache[key]
        if name.startswith("write_TYPE_"):
            cache[key] = name[len("write_TYPE_"):].lower()
            return cache[key]
        seen = _body_calls(abc, by_q, name)
        if "writeExternal" in seen or "writeToBuffer" in seen:
            ty = "message"
        elif "writeDouble" in seen:
            ty = "double"
        elif "writeFloat" in seen:
            ty = "float"
        elif "writeUTFBytes" in seen or "writeUTF" in seen:
            ty = "string"
        elif "writeBytes" in seen:
            ty = "bytes"
        elif "writeByte" in seen or "writeBoolean" in seen:
            ty = "bool"
        elif seen & {"writeUnsignedInt", "writeInt", "writeShort"}:
            ty = "int32"
        else:
            ty = None
        cache[key] = ty
        return ty
    return type_of


def _slot_types(inst, cls):
    out = {}
    out.update(inst.get("slots") or {})
    out.update(cls.get("slots") or {})
    return out


def _legacy_fields(lines, slot_types):
    flds, slots = [], {}
    last_num, cur_tag, want_slot, nested = None, None, False, None
    for _, t, c in lines:
        v = num_of(t)
        if v is not None:
            last_num = v
            continue
        if want_slot and t.startswith("getproperty"):
            slots[cur_tag] = c
            prop = (c or "").split(":")[-1]
            nq = slot_types.get(prop) or slot_types.get(c)
            if nq and nq.split(":")[-1] not in _SCALAR:
                nested = nq
            want_slot = False
            continue
        m = CALL.match(c or "")
        if not m:
            continue
        fn = m.group(1)
        if fn == "_-9g":
            cur_tag, last_num, nested = last_num, None, None
            want_slot = True
        elif cur_tag is not None and fn in _LEGACY_TYPES:
            flds.append((cur_tag, _LEGACY_TYPES[fn], nested))
            cur_tag, want_slot, nested = None, False, None
    return flds, slots


def _shaped_fields(lines, tag_name, type_of, slot_types):
    flds, slots = [], {}
    last_num, cur_tag, want_slot, nested = None, None, False, None
    for _, t, c in lines:
        v = num_of(t)
        if v is not None:
            last_num = v
            continue
        if want_slot and t.startswith("getproperty"):
            slots[cur_tag] = c
            prop = (c or "").split(":")[-1]
            nq = slot_types.get(prop) or slot_types.get(c)
            if nq and nq.split(":")[-1] not in _SCALAR:
                nested = nq
            want_slot = False
            continue
        name, argc = _call(c)
        if not name:
            continue
        if name == tag_name and argc == 3:
            cur_tag, last_num, nested = last_num, None, None
            want_slot = True
        elif cur_tag is not None:
            ty = type_of(name, tag_name)
            if ty:
                flds.append((cur_tag, ty, nested))
                cur_tag, want_slot, nested = None, False, None
    return flds, slots


def parse_swf(swf):
    """还原一份 SWF。返回 opcode、真名、字段（含声明顺序和嵌套类）以及 Send 顺序。

    旧符号还在时，opcode 和字段类型走原来的对照，避免把已核对过的版本重新猜一遍。
    """
    abc = load(swf)
    by_q, meth_of = {}, {}
    for idx, inst in enumerate(abc.instances):
        q = abc.qname(inst["name"])
        cls = abc.classes[idx]
        by_q[q] = (inst, cls)
        mids = [inst["iinit"], cls["cinit"]]
        mids += [mi for _, k, mi in inst["traits"] if mi is not None]
        mids += [mi for _, k, mi in cls["traits"] if mi is not None]
        for mi in mids:
            meth_of.setdefault(mi, q)

    real = {}
    for q, (inst, cls) in by_q.items():
        mids = [inst["iinit"]] + [mi for _, k, mi in inst["traits"] if mi is not None]
        names = set()
        for mi in mids:
            code = abc.bodies.get(mi)
            if not code:
                continue
            for _a, t, _c in disasm(abc, code):
                if t.startswith("pushstring"):
                    s = abc.strings[int(t.split()[1])]
                    m = BAD.match(s)
                    if m:
                        names.add(m.group(1))
        if len(names) == 1:
            real[q] = names.pop()
        elif names:
            real[q] = sorted(names)[0]
        else:
            # 没有字段的消息（心跳）不会生成报错字符串。类名没被改掉时，类名就是消息名。
            short = q.split(":")[-1]
            if "protocol" in q.split(":")[0] and short and not short.startswith("_-"):
                real[q] = short

    proto_q = {q for q in by_q if q in real or "protocol" in q.split(":")[0]}
    short_to_q = _short_index(proto_q)

    reg, flows, saw_legacy = {}, [], False
    for mi, code in abc.bodies.items():
        lines = disasm(abc, code)
        if any(c == "_-055(2 参)" for _, _, c in lines):
            saw_legacy = True
            for op, klass in _legacy_opcodes(lines).items():
                reg.setdefault(op, klass)
        flow = _flows_in(lines, abc, proto_q, short_to_q, real)
        if flow:
            flow["class"] = meth_of.get(mi, "")
            flows.append(flow)
    legacy_opcode = saw_legacy and len(reg) >= 200
    if not legacy_opcode:
        found, counts, cached = {}, {}, []
        for _mi, code in abc.bodies.items():
            lines = disasm(abc, code)
            cached.append(lines)
            for i, (_, _t, c) in enumerate(lines):
                name, argc = _call(c)
                if argc == 2 and name and _reg_window(lines, i, proto_q, short_to_q):
                    counts[name] = counts.get(name, 0) + 1
        registrar, n = max(counts.items(), key=lambda kv: kv[1]) if counts else (None, 0)
        if registrar and n >= 200:
            for lines in cached:
                for i, (_, _t, c) in enumerate(lines):
                    name, argc = _call(c)
                    if name != registrar or argc != 2:
                        continue
                    hit = _reg_window(lines, i, proto_q, short_to_q)
                    if hit:
                        found.setdefault(hit[0], hit[1])
        if len(found) >= len(reg):
            reg = found

    write_cached, saw_tag = [], False
    for q, (inst, cls) in by_q.items():
        we = next((mi for n, k, mi in inst["traits"]
                   if n in ("writeToBuffer", "writeExternal") and mi is not None), None)
        if we is None or we not in abc.bodies:
            continue
        lines = disasm(abc, abc.bodies[we])
        write_cached.append((q, inst, cls, lines))
        if any(_call(c)[0] == "_-9g" for _, _, c in lines):
            saw_tag = True
    type_of = _type_fn(abc, by_q, {})
    tag_name = None if saw_tag else _discover_tag([ln for _, _, _, ln in write_cached], abc, by_q)

    schema, slots, decl = {}, {}, {}
    for q, inst, cls, lines in write_cached:
        st = _slot_types(inst, cls)
        if saw_tag:
            flds, slot = _legacy_fields(lines, st)
        elif tag_name:
            flds, slot = _shaped_fields(lines, tag_name, type_of, st)
        else:
            continue
        if not flds:
            continue
        decl[q] = []
        for tag, ty, nq in flds:
            nn = real.get(nq) if nq else None
            if nn and nn.startswith("_-"):
                nn = None
            decl[q].append({"tag": tag, "type": ty, "nested": nq, "nested_name": nn})
        schema[q] = sorted({(tag, ty) for tag, ty, _nq in flds})
        slots[q] = slot

    fieldnames, skipped = {}, 0
    for q, (inst, _cls) in by_q.items():
        if q not in schema:
            continue
        tags = sorted(t for t, _ in schema[q])
        names, nested_at = {}, {}
        rx = next((mi for n, k, mi in inst["traits"]
                   if n == "readExternal" and mi is not None), None)
        if rx and rx in abc.bodies:
            code = abc.bodies[rx]
            lines = disasm(abc, code)
            cases, bounds = _switch_cases(abc, code, lines)
            if len(cases) == len(tags):
                for tag, start in zip(tags, cases):
                    end = next((b for b in bounds if b > start), 1 << 30)
                    for a, t, c in lines:
                        if not start <= a < end:
                            continue
                        if t.startswith("pushstring") and tag not in names:
                            m = BAD.match(abc.strings[int(t.split()[1])])
                            if m:
                                names[tag] = m.group(2)
                        if tag not in nested_at and t.split()[0] == "constructprop":
                            nq = _q_of(t, c, proto_q, short_to_q)
                            if nq and nq != q:
                                nested_at[tag] = nq
            elif cases:
                skipped += 1
        getters = {}
        for n, k, mi in inst["traits"]:
            if k != "getter" or mi is None or mi >= len(abc.methods):
                continue
            ret = abc.methods[mi].get("ret")
            if ret:
                getters[n] = abc.qname(ret)
        for tag in tags:
            if tag in names:
                continue
            s = slots.get(q, {}).get(tag, "")
            if s and not s.startswith("_-"):
                names[tag] = s[1:] if s.startswith("_") else s
        fieldnames[q] = names
        for row in decl.get(q, []):
            row["name"] = names.get(row["tag"]) or f"field{row['tag']}"
            nq = nested_at.get(row["tag"]) or getters.get(row["name"])
            if not nq:
                continue
            short = nq.split(":")[-1]
            if short in _SCALAR:
                continue
            nn = real.get(nq) or (None if short.startswith("_-") else short)
            if nn and not str(nn).startswith("_-"):
                row["nested"] = nq
                row["nested_name"] = nn

    return {"reg": reg, "real": real, "schema": schema, "decl": decl,
            "fieldnames": fieldnames,             "flows": flows, "skipped": skipped,
            "legacy_opcode": legacy_opcode,
            "legacy_fields": saw_tag}


def main():
    swf, outdir = sys.argv[1], sys.argv[2]
    os.makedirs(outdir, exist_ok=True)
    parsed = parse_swf(swf)
    reg, real = parsed["reg"], parsed["real"]
    schema, fieldnames, skipped = parsed["schema"], parsed["fieldnames"], parsed["skipped"]
    how = "旧符号" if parsed["legacy_opcode"] else "按调用形状"
    fields_how = "旧符号" if parsed["legacy_fields"] else "按方法体"
    print(f"1) opcode 表: {len(reg)} 条（{how}）")
    print(f"2) 还原真实消息名: {len(real)} 个类")
    ok = sum(1 for q in schema if len(fieldnames.get(q, {})) == len(schema[q]))
    print(f"3) 提到字段号的类 {len(schema)} 个，字段名完全还原 {ok} 个"
          f"（{skipped} 个分支数对不上，已放弃猜名；字段类型{fields_how}）")
    print(f"   会发协议的方法 {len(parsed['flows'])} 个")

    # ---------- 输出 ----------
    opmap = {}
    for op, q in sorted(reg.items()):
        opmap[f"{op:04x}"] = {"class": q, "name": real.get(q, "")}
    json.dump(opmap, open(os.path.join(outdir, "opcodes.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)

    lines = ['syntax = "proto2";', "", "// 由 extract_proto.py 从 RedWar SWF 还原",
             "// 字段号/类型来自 writeExternal 的立即数，",
             "// 字段名来自 readExternal 中该字段 case 分支里的报错模板", ""]
    named = 0
    for q, flds in sorted(schema.items(), key=lambda kv: real.get(kv[0], kv[0])):
        name = real.get(q)
        if not name:
            continue
        named += 1
        fnames = fieldnames.get(q, {})
        op = next((f"{o:04x}" for o, k in reg.items() if k == q), None)
        lines.append(f"// {q}" + (f"   opcode 0x{op}" if op else ""))
        lines.append(f"message {name} {{")
        for tag, ty in sorted(set(flds)):
            fn = fnames.get(tag) or f"field{tag}"
            lines.append(f"  optional {ty.replace('TYPE_', '').lower():<10} {fn} = {tag};")
        lines.append("}")
        lines.append("")
    open(os.path.join(outdir, "redwar.proto"), "w", encoding="utf-8").write(
        "\n".join(lines))
    # 给工具用的紧凑 schema：opcode -> {name, fields:{tag:[名字, 类型]}}
    compact = {}
    for op, q in sorted(reg.items()):
        flds = schema.get(q, [])
        names = fieldnames.get(q, {})
        fmap = {}
        for tag, ty in flds:
            fmap[str(tag)] = [names.get(tag) or f"field{tag}", ty]
        compact[f"{op:04x}"] = {"name": real.get(q) or q.split(":")[-1],
                                "cls": q.split(":")[-1], "fields": fmap}
    json.dump(compact, open(os.path.join(outdir, "schema.json"), "w",
                            encoding="utf-8"), ensure_ascii=False, indent=0)
    print(f"写出 {outdir}/opcodes.json、{outdir}/redwar.proto（{named} 个具名消息）"
          f"和 {outdir}/schema.json")

    # 抽样自检
    print("\n抽样（opcode -> 消息名）:")
    for op in sorted(reg)[:5] + [o for o in (0x0268, 0x027c, 0x0215, 0x0283) if o in reg]:
        print(f"  {op:04x} {reg[op]:<58} {real.get(reg[op], '?')}")


if __name__ == "__main__":
    main()
