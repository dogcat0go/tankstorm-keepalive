#!/usr/bin/env python3
# tankstorm-keepalive  Copyright (C) 2026 Dimlitter
# SPDX-License-Identifier: AGPL-3.0-or-later
"""从新版 RedWar SWF 生成 wiremap，并更新 RC4 豁免名单。

    python tools/build_wiremap.py --latest
    python tools/build_wiremap.py path/RedWar_YYYYMMDDNN.swf
    python tools/build_wiremap.py path/RedWar_YYYYMMDDNN.swf --check

对照按消息名对齐（RceInit、RceCountryOpen 这类名字混淆器改不了）。
字段表沿用上一版：字段号集合没变、或只是多了字段，就继续用；
对不上的消息不进表，发出去会拒发，避免旧编号被服务器当成别的请求。

加密白名单不靠猜 opcode。Transport 的类初始化里，newarray 前面
连续取出的 C2S_* / S2C_* 属性就是白名单，属性值是 pushshort。

登录包和心跳在 protocol.json 里用本项目编号（041c / 041d / 040e），
发送时由 wiremap 翻译。这个脚本不改 protocol.json。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from disasm import disasm, load  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WIREMAP_PATH = os.path.join(ROOT, "tankstorm", "wiremap.json")
SCHEMA_PATH = os.path.join(ROOT, "tankstorm", "schema.json")
CRYPTO_PATH = os.path.join(ROOT, "tankstorm", "crypto.py")
CDN = "https://redwar-cdn.sincetimes.com/100616028/res/20120522/flash/"

# 这些对不上就不要写出对照表。没有它们，登录和读国家都会发错。
CRITICAL = {
    "RceInit", "RceLoad", "RceHeartbeat", "RceDebug",
    "RceCountryOpen", "RceActiveUIFlag",
    "RseCountryOpen", "RseFightSimpInfo", "RseLoad",
}
BAD = re.compile(r"Bad data format: ([A-Za-z_]\w*)\.(\w+) cannot be set twice\.")
NUMPUSH = ("pushbyte", "pushshort", "pushint", "pushuint")


def _num(text: str):
    parts = text.split()
    if parts and parts[0] in NUMPUSH and len(parts) > 1:
        try:
            return int(parts[1])
        except ValueError:
            return None
    return None


def _name_of(comment: str) -> str:
    comment = comment or ""
    if ":" in comment:
        comment = comment.split(":")[-1]
    return comment.strip()


def _real_names(abc) -> dict:
    """混淆类名 -> 消息名。报错模板是字符串，混淆器动不了。"""
    real = {}
    for inst in abc.instances:
        q = abc.qname(inst["name"])
        if "protocol:" not in q:
            continue
        names = set()
        short = q.split(":")[-1]
        mids = [inst["iinit"]] + [mi for n, k, mi in inst["traits"] if mi]
        for mi in mids:
            code = abc.bodies.get(mi)
            if not code:
                continue
            for _a, text, _c in disasm(abc, code):
                if not text.startswith("pushstring"):
                    continue
                matched = BAD.match(abc.strings[int(text.split()[1])])
                if matched:
                    names.add(matched.group(1))
        if len(names) == 1:
            real[q] = names.pop()
        elif not short.startswith("_-"):
            real[q] = short
    return real


def _protocol_methods(abc):
    for idx, inst in enumerate(abc.instances):
        q = abc.qname(inst["name"])
        if not q.startswith("com.sincetimes.redwar.game.comnunicate.protocol:"):
            continue
        cls = abc.classes[idx]
        for name, _k, mi in list(inst["traits"]) + list(cls["traits"]):
            if mi and mi in abc.bodies:
                yield q, name, mi


def _registry(abc, real):
    """返回 (name -> opcode, qname -> opcode, writeToBuffer 方法号)。"""
    biggest = None
    writers = {}
    for q, name, mi in _protocol_methods(abc):
        size = len(abc.bodies[mi])
        if biggest is None or size > biggest[0]:
            biggest = (size, q, name, mi)
        if name == "writeToBuffer":
            writers[q] = mi
    if not biggest or biggest[0] < 8000:
        raise SystemExit("没找到协议注册方法（protocol 包里没有足够大的方法）")
    lines = disasm(abc, abc.bodies[biggest[3]])
    calls = Counter(c for _a, _t, c in lines if c and c.endswith("(2 参)"))
    if not calls or calls.most_common(1)[0][1] < 400:
        raise SystemExit(f"注册方法 {biggest[1]}::{biggest[2]} 里没有成批的 2 参调用")
    call = calls.most_common(1)[0][0]
    name_op = {}
    q_op = {}
    pending = []
    for _a, text, comment in lines:
        if comment == call:
            add_at = next((i for i in range(len(pending) - 1, -1, -1)
                           if pending[i][0] == "add"), None)
            klass = next((c for t, c in reversed(pending) if t.startswith("getlex")), None)
            nums = []
            if add_at is not None:
                for text_i, _c in reversed(pending[:add_at]):
                    value = _num(text_i)
                    if value is not None:
                        nums.append(value)
                        if len(nums) == 2:
                            break
            if klass and len(nums) == 2:
                opcode = sum(nums)
                q_op[klass] = opcode
                message = real.get(klass, "")
                if message:
                    name_op[message] = opcode
            pending = []
            continue
        if text.startswith(NUMPUSH) or text == "add" or text.startswith("getlex"):
            pending.append((text, comment))
    if len(q_op) < 400:
        raise SystemExit(f"只解析出 {len(q_op)} 条 opcode，注册表可能又换了形态")
    return name_op, q_op, writers, call


def _write_tag_call(abc, writers) -> str:
    counts = Counter()
    for mi in writers.values():
        for _a, _t, comment in disasm(abc, abc.bodies[mi]):
            if comment and comment.endswith("(3 参)"):
                counts[comment] += 1
    if not counts:
        raise SystemExit("writeToBuffer 里没有 3 参调用，认不出 writeTag")
    return counts.most_common(1)[0][0]


def _tags(abc, mi, write_tag: str) -> tuple:
    if not mi:
        return tuple()
    found = []
    last = None
    for _a, text, comment in disasm(abc, abc.bodies[mi]):
        value = _num(text)
        if value is not None:
            last = value
            continue
        if comment == write_tag and last is not None:
            found.append(last)
            last = None
    return tuple(sorted(found))


def _exempt(abc) -> dict:
    """Transport 类初始化里 newarray 收走的 C2S_/S2C_ 属性，就是不加密的 opcode。"""
    transport = None
    for idx, inst in enumerate(abc.instances):
        if abc.qname(inst["name"]).endswith(":Transport"):
            transport = abc.classes[idx]["cinit"]
            break
    if transport not in abc.bodies:
        raise SystemExit("找不到 Transport 的类初始化，读不出加密白名单")
    groups = []
    buf = []
    for _a, text, comment in disasm(abc, abc.bodies[transport]):
        name = _name_of(comment)
        if text.startswith("getproperty") and (name.startswith("C2S_") or name.startswith("S2C_")):
            buf.append(name)
            continue
        if text.startswith("newarray"):
            count = int(text.split()[1])
            if buf:
                groups.append(buf[-count:])
            buf = []
    wanted = [name for group in groups for name in group]
    if len(wanted) < 9:
        raise SystemExit(f"白名单属性只有 {wanted}，数量不对")

    values = {}
    inits = []
    for idx, inst in enumerate(abc.instances):
        q = abc.qname(inst["name"])
        if "comnunicate" not in q:
            continue
        cls = abc.classes[idx]
        inits.extend([inst["iinit"], cls["cinit"]])
    for mi in inits:
        code = abc.bodies.get(mi)
        if not code:
            continue
        last = None
        for _a, text, comment in disasm(abc, code):
            value = _num(text)
            if value is not None:
                last = value
                continue
            prop = _name_of(comment)
            if text.startswith("initproperty") and prop in wanted and last is not None:
                values[prop] = last
    missing = [name for name in wanted if name not in values]
    if missing:
        raise SystemExit(f"这些白名单属性没有立即数：{missing}")
    out = {"c2s": [], "s2c": []}
    for name in wanted:
        side = "c2s" if name.startswith("C2S_") else "s2c"
        out[side].append(f"{values[name]:04x}")
    return out


def _latest_swf(dest_dir: str) -> str:
    import datetime
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    today = datetime.date.today()
    found = []
    for back in range(0, 21):
        day = today - datetime.timedelta(days=back)
        for seq in ("01", "02", "03"):
            version = f"{day:%Y%m%d}{seq}"
            url = CDN + f"RedWar_{version}.swf"
            try:
                with opener.open(urllib.request.Request(url, method="HEAD"), timeout=12) as resp:
                    if resp.status == 200:
                        found.append((version, url))
            except Exception:
                continue
    if not found:
        raise SystemExit("CDN 上近 21 天没有 RedWar_*.swf")
    version, url = max(found)
    path = os.path.join(dest_dir, f"RedWar_{version}.swf")
    print(f"下载 {url}")
    with opener.open(url, timeout=60) as resp, open(path, "wb") as fh:
        fh.write(resp.read())
    return path


def _base_entry(wm: dict, version: str):
    others = sorted(k for k in wm if k.isdigit() and k != version)
    if others:
        return others[-1], wm[others[-1]]
    if version in wm:
        return version, wm[version]
    raise SystemExit("wiremap.json 里没有上一版可以沿用字段表")


def build(swf_path: str) -> dict:
    matched = re.search(r"RedWar_(\d+)\.swf", os.path.basename(swf_path), re.I)
    if not matched:
        raise SystemExit("文件名必须是 RedWar_<版本>.swf，版本号用来当 wiremap 的键")
    version = matched.group(1)
    print(f"解析 {swf_path} → {version}")
    abc = load(swf_path)
    real = _real_names(abc)
    name_op, _q_op, writers, _call = _registry(abc, real)
    write_tag = _write_tag_call(abc, writers)
    exempt = _exempt(abc)
    schema = json.load(open(SCHEMA_PATH, encoding="utf-8"))
    wm = json.load(open(WIREMAP_PATH, encoding="utf-8"))
    base_ver, base = _base_entry(wm, version)
    op_name = {op: (info.get("name") or "") for op, info in schema.items()}

    ops, msgs = {}, {}
    dropped = []
    extra_fields = []
    for canon, _old in base["ops"].items():
        message = op_name.get(canon) or ""
        if not message or message.startswith("_-") or message not in name_op:
            continue
        new_op = f"{name_op[message]:04x}"
        cls = base["msgs"].get(canon)
        old_tags = tuple(sorted(row[1] for row in base["classes"][cls])) if cls is not None else tuple()
        qname = next((q for q, n in real.items() if n == message), "")
        new_tags = _tags(abc, writers.get(qname), write_tag)
        if new_tags != old_tags and not set(old_tags) <= set(new_tags):
            dropped.append((message, canon, old_tags, new_tags))
            continue
        if new_tags != old_tags:
            extra_fields.append((message, sorted(set(new_tags) - set(old_tags))))
        ops[canon] = new_op
        if cls is not None:
            msgs[canon] = cls

    wires = list(ops.values())
    if len(wires) != len(set(wires)):
        raise SystemExit("不同消息译到了同一个线上 opcode，对照表不能用")
    have = {op_name.get(op, "") for op in ops}
    missing = sorted(name for name in CRITICAL if name not in have)
    print(f"沿用 {base_ver} 的字段表，写入 {len(ops)} 条 opcode，"
          f"上一版 {len(base['ops'])} 条")
    print("发送白名单", " ".join(exempt["c2s"]))
    print("接收白名单", " ".join(exempt["s2c"]))
    if extra_fields:
        print(f"多出来的字段（解码时会跳过）{len(extra_fields)} 个，例如 {extra_fields[:6]}")
    if dropped:
        print(f"字段号对不上、已从对照表去掉 {len(dropped)} 个：")
        for row in dropped[:20]:
            print(" ", row[0], row[1], "旧", row[2], "新", row[3])
    if missing:
        raise SystemExit(f"关键消息没有进表：{missing}。不要发登录包。")
    for canon, message in (("040e", "RceHeartbeat"), ("041c", "RceInit"), ("041d", "RceLoad"),
                           ("0455", "RceDebug")):
        if ops.get(canon) not in exempt["c2s"]:
            raise SystemExit(f"{message} 的线上编号 {ops.get(canon)} 不在发送白名单里")
    return {
        "version": version,
        "entry": {"ops": ops, "msgs": msgs, "classes": base["classes"], "exempt": exempt},
        "exempt": exempt,
    }


def _write_crypto(exempt: dict) -> None:
    text = open(CRYPTO_PATH, encoding="utf-8").read()
    c2s = ", ".join(f'"{op}"' for op in exempt["c2s"])
    s2c = ", ".join(f'"{op}"' for op in exempt["s2c"])
    block = (
        "EXEMPT = {\n"
        f'    "s2c": {{{s2c}}},\n'
        f'    "c2s": {{{c2s}}},\n'
        "}"
    )
    new, n = re.subn(r"EXEMPT = \{\n(?:.*\n)*?\}", block, text, count=1)
    if n != 1:
        raise SystemExit("tankstorm/crypto.py 里的 EXEMPT 没替换成功")
    open(CRYPTO_PATH, "w", encoding="utf-8").write(new)


def _write_wiremap(version: str, entry: dict) -> None:
    wm = json.load(open(WIREMAP_PATH, encoding="utf-8"))
    if "2026102102" in wm and "exempt" not in wm["2026102102"]:
        wm["2026102102"]["exempt"] = {
            "c2s": ["043e", "044a", "0451", "04ee"],
            "s2c": ["0237", "032a", "0200", "0246", "0298"],
        }
    wm[version] = entry
    text = json.dumps(wm, ensure_ascii=False, separators=(",", ":"))
    open(WIREMAP_PATH, "w", encoding="utf-8").write(text + "\n")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="从 RedWar SWF 生成协议对照")
    parser.add_argument("swf", nargs="?", help="RedWar_<版本>.swf")
    parser.add_argument("--latest", action="store_true", help="从 CDN 取近 21 天里最新的 SWF")
    parser.add_argument("--check", action="store_true", help="只打印结果，不写文件")
    args = parser.parse_args(argv)
    if args.latest:
        swf = _latest_swf(os.path.dirname(os.path.abspath(__file__)))
    elif args.swf:
        swf = args.swf
    else:
        parser.error("要给 SWF 路径，或加 --latest")
    built = build(swf)
    if args.check:
        print("check：未写文件")
        return 0
    _write_wiremap(built["version"], built["entry"])
    _write_crypto(built["exempt"])
    print(f"已写入 wiremap[{built['version']}] 和 tankstorm/crypto.py 的 EXEMPT")
    print("protocol.json 不用改。登录和心跳仍是本项目编号，发送时再翻译。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
