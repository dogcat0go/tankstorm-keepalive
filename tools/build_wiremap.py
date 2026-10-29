#!/usr/bin/env python3
# tankstorm-keepalive  Copyright (C) 2026 Dimlitter
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# 本程序是自由软件：你可以依据自由软件基金会发布的 GNU Affero 通用公共许可证
# （第 3 版，或你选择的任何更新版本）之条款，再分发和/或修改它。
# 本程序希望能有用，但不提供任何担保；甚至不含适销性或特定用途适用性的默示担保。
# 详见随附的 LICENSE 文件，或 <https://www.gnu.org/licenses/>。
"""游戏更新后，从新版 SWF 生成 wiremap，并对照交互流程。

    python tools/build_wiremap.py <新的.swf> [--canon-swf <基准.swf>] [--version 版本号]

编号对照只靠混淆器改不掉的东西：报错模板里的消息名和字段名。
原版 SWF 的地址是固定的，只改文件名里的 10 位版本：
https://redwar-cdn.sincetimes.com/100616028/res/20120522/flash/RedWar_<版本>.swf
交互流程是客户端按什么顺序 Send。字节码把 if/else 挤成一条直线；
装了 ffdec（环境变量 FFDEC_JAR）时，改用反编译出来的脚本看分支。
没有基准 SWF 时，嵌套消息的旧字段号用 docs/redwar.proto。

生成的 out/wiremap.json 放到程序目录（和 main.py 同级）。
tankstorm/wiremap.py 会把它叠到随包的那份上，不用改代码、也不用等发版。
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from extract_proto import _anchor, parse_swf          # noqa: E402

_FIELD_N = re.compile(r"^field\d+$")
_FUNC = re.compile(r"\bfunction\s+([A-Za-z0-9_\-]+)\s*\(")
_VAR = re.compile(
    r"\b([A-Za-z_][A-Za-z0-9_]*)\s*(?::\s*([A-Za-z0-9_\-]+))?\s*=\s*new\s+([A-Za-z0-9_\-]+)")
_SEND = re.compile(r"\.Send\s*\(\s*(?:new\s+([A-Za-z0-9_\-]+)|([A-Za-z_][A-Za-z0-9_]*))")
_STR = re.compile(r'"((?:\\.|[^"\\])*)"')
_VER = re.compile(r"(\d{10})")
_PROTO_FIELD = re.compile(
    r"^\s*(?:optional|required|repeated)\s+(\w+)\s+(\w+)\s*=\s*(\d+)\s*;")


def usable(name):
    return bool(name) and not str(name).startswith("_-") and not _FIELD_N.match(str(name))


def find_ffdec(explicit=""):
    cands = [
        explicit or "",
        os.environ.get("FFDEC_JAR") or "",
        "/usr/share/java/ffdec/ffdec.jar",
        "/usr/share/java/ffdec.jar",
        "/usr/share/ffdec/ffdec.jar",
        "/opt/ffdec/ffdec.jar",
        os.path.expanduser("~/ffdec/ffdec.jar"),
        os.path.expanduser("~/tools/ffdec/ffdec.jar"),
        r"D:\tools\ffdec\ffdec.jar",
        r"D:\ffdec\ffdec.jar",
        r"C:\ffdec\ffdec.jar",
    ]
    for p in cands:
        if p and os.path.isfile(p):
            return p
    return ""


def version_of(path, explicit):
    if explicit:
        return explicit
    m = _VER.search(os.path.basename(path))
    return m.group(1) if m else ""


def _rows_named(rows):
    return [r for r in rows or [] if usable(r.get("name"))]


def index_decl(parsed):
    best = {}
    for q, rows in parsed["decl"].items():
        name = parsed["real"].get(q) or ""
        if not usable(name):
            continue
        prev = best.get(name)
        if prev is None or len(_rows_named(rows)) > len(_rows_named(prev)):
            best[name] = rows
    return best


def _sig(names):
    sig = tuple(sorted(n for n in names if usable(n)))
    return sig or None


def wire_by_name(parsed):
    out = {}
    for op, q in parsed["reg"].items():
        name = parsed["real"].get(q) or ""
        if not usable(name):
            continue
        rows = parsed["decl"].get(q) or []
        prev = out.get(name)
        if prev is None or len(_rows_named(rows)) > len(_rows_named(prev["rows"])):
            out[name] = {"op": f"{op:04x}", "rows": rows}
    return out


def wire_by_sig(parsed):
    """没有真名的消息，按字段名签名对。签名不唯一的放弃。"""
    found, bad = {}, set()
    for op, q in parsed["reg"].items():
        if usable(parsed["real"].get(q) or ""):
            continue
        sig = _sig(r.get("name") for r in (parsed["decl"].get(q) or []))
        if not sig:
            continue
        if sig in found:
            bad.add(sig)
        else:
            found[sig] = {"op": f"{op:04x}", "rows": parsed["decl"].get(q) or []}
    for sig in bad:
        found.pop(sig, None)
    return found


def canon_rows(schema_entry, decl_rows):
    by = {r["name"]: r for r in _rows_named(decl_rows)}
    out = []
    for tag, meta in (schema_entry.get("fields") or {}).items():
        fname, ftype = meta[0], meta[1]
        extra = by.get(fname) or {}
        out.append({
            "tag": int(tag), "type": ftype, "name": fname,
            "nested_name": extra.get("nested_name"),
        })
    out.sort(key=lambda r: r["tag"])
    return out


def schema_match(parsed, schema):
    """基准 SWF 的字段号必须和 schema.json 一致，才用它补嵌套类型。"""
    wire = wire_by_name(parsed)
    same = total = 0
    for entry in schema.values():
        name = entry.get("name") or ""
        hit = wire.get(name)
        if not hit:
            continue
        tags = {r["name"]: r["tag"] for r in _rows_named(hit["rows"])}
        for tag, meta in (entry.get("fields") or {}).items():
            if not usable(meta[0]):
                continue
            total += 1
            if tags.get(meta[0]) == int(tag):
                same += 1
    return same, total


def proto_decl(path):
    """docs/redwar.proto 里的字段号就是本项目编号。类型写成 message 时没有嵌套类名。"""
    out, cur = {}, None
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.startswith("message "):
                cur = line.split()[1]
                out.setdefault(cur, [])
                continue
            if cur and line.startswith("}"):
                cur = None
                continue
            m = _PROTO_FIELD.match(line)
            if not m or not cur:
                continue
            ty, name, tag = m.group(1), m.group(2), int(m.group(3))
            out[cur].append({
                "tag": tag, "type": ty, "name": name, "nested_name": None,
            })
    return out


def build_entry(schema, new_parsed, canon_parsed=None, fallback_decl=None):
    wire = wire_by_name(new_parsed)
    by_sig = wire_by_sig(new_parsed)
    canon_sigs, canon_bad = {}, set()
    for cop, entry in schema.items():
        if usable(entry.get("name") or ""):
            continue
        sig = _sig(meta[0] for meta in (entry.get("fields") or {}).values())
        if not sig:
            continue
        if sig in canon_sigs:
            canon_bad.add(sig)
        else:
            canon_sigs[sig] = cop
    for sig in canon_bad:
        canon_sigs.pop(sig, None)
    canon_decl = index_decl(canon_parsed) if canon_parsed else {}
    for name, rows in (fallback_decl or {}).items():
        canon_decl.setdefault(name, rows)
    new_decl = index_decl(new_parsed)
    items = []
    missed = []
    for cop in sorted(schema, key=lambda s: int(s, 16)):
        entry = schema[cop]
        name = entry.get("name") or ""
        if usable(name):
            hit = wire.get(name)
            label = name
        else:
            sig = _sig(meta[0] for meta in (entry.get("fields") or {}).values())
            hit = by_sig.get(sig) if sig and canon_sigs.get(sig) == cop else None
            label = name or cop
        if not hit:
            if usable(name):
                missed.append(name)
            continue
        items.append((label, cop, hit["op"],
                      canon_rows(entry, canon_decl.get(name) if usable(name) else None),
                      hit["rows"]))

    name_at = {name: i for i, (name, *_rest) in enumerate(items)}
    extras = []

    def add_nested(name):
        if not usable(name) or name in name_at:
            return
        c_rows, w_rows = canon_decl.get(name), new_decl.get(name)
        if not c_rows or not w_rows:
            return
        name_at[name] = len(items) + len(extras)
        extras.append((name, c_rows, w_rows))
        for row in list(c_rows) + list(w_rows):
            add_nested(row.get("nested_name"))

    for _name, _cop, _wop, c_rows, w_rows in items:
        for row in list(c_rows) + list(w_rows):
            add_nested(row.get("nested_name"))

    def align(c_rows, w_rows):
        w_by = {}
        for row in w_rows:
            if usable(row.get("name")):
                w_by.setdefault(row["name"], row)
        out, unresolved = [], 0
        for row in c_rows:
            if not usable(row.get("name")):
                continue
            w = w_by.get(row["name"])
            if not w:
                continue
            nested = None
            # 线上写函数的类型会被同名方法污染，标量也会被看成 message。
            # 嵌套与否以本项目 schema / proto 为准。
            if row.get("type") == "message":
                nn = row.get("nested_name") or w.get("nested_name")
                if nn and nn in name_at:
                    nested = name_at[nn]
                else:
                    nested = -1
                    unresolved += 1
            out.append([row["tag"], int(w["tag"]), nested])
        return out, unresolved

    classes, ops, msgs, unresolved = [], {}, {}, 0
    for i, (_name, cop, wop, c_rows, w_rows) in enumerate(items):
        rows, n = align(c_rows, w_rows)
        unresolved += n
        classes.append(rows)
        ops[cop] = wop
        msgs[cop] = i
    for _name, c_rows, w_rows in extras:
        rows, n = align(c_rows, w_rows)
        unresolved += n
        classes.append(rows)
    return {"ops": ops, "msgs": msgs, "classes": classes}, unresolved, missed


def numbers_changed(entry):
    for cop, wop in entry["ops"].items():
        if cop != wop:
            return True
    for rows in entry["classes"]:
        for ctag, wtag, _nested in rows:
            if ctag != wtag:
                return True
    return False


def _seq(flow):
    return tuple(s for s in flow.get("sends") or [] if s and s != "?")


def summarize(flows, limit=25):
    cnt = {}
    for flow in flows:
        seq = _seq(flow)
        if len(seq) >= 2:
            cnt[seq] = cnt.get(seq, 0) + 1
    return sorted(cnt.items(), key=lambda kv: -kv[1])[:limit]


def diff_flows(old, new):
    used, changes = set(), []
    for a in old:
        aa = [x for x in a.get("anchors") or [] if _anchor(x)]
        if len(aa) < 2:
            continue
        best, score = None, 0.0
        for j, b in enumerate(new):
            if j in used:
                continue
            bb = [x for x in b.get("anchors") or [] if _anchor(x)]
            inter = set(aa) & set(bb)
            if len(inter) < 2:
                continue
            sc = len(inter) / len(set(aa) | set(bb))
            if sc > score:
                best, score = j, sc
        if best is None or score < 0.5:
            continue
        used.add(best)
        if _seq(a) != _seq(new[best]):
            changes.append((a, new[best]))
    return changes


def _fmt_steps(flow):
    steps = flow.get("steps") or []
    if not steps:
        return " → ".join(_seq(flow)) or "（没有认出来的消息）"
    lines = []
    for step in steps:
        pad = "  " * max(0, int(step.get("d") or 1) - 1)
        if step.get("send"):
            lines.append(f"{pad}Send {step['send']}")
        elif step.get("if"):
            lines.append(f"{pad}if")
        elif step.get("else"):
            lines.append(f"{pad}else")
    return "\n".join(lines)


def _unquote(s):
    if "\\" not in s:
        return s
    try:
        return bytes(s, "utf-8").decode("unicode_escape")
    except Exception:
        return s


def flows_from_as(root, short_to_msg):
    out = []
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            if fn.endswith(".as"):
                with open(os.path.join(dirpath, fn), encoding="utf-8", errors="replace") as f:
                    out.extend(_as_methods(f.read(), short_to_msg))
    return out


def _as_methods(text, short_to_msg):
    depth, cur, var, out, base = 0, None, {}, [], None

    def close():
        nonlocal cur, var, base
        if cur and any(s != "?" for s in cur["sends"]):
            cur["anchors"] = cur["anchors"][:12]
            out.append(cur)
        cur, var, base = None, {}, None

    for line in text.splitlines():
        opened = _FUNC.search(line) if "function " in line else None
        if opened:
            close()
            cur = {"class": "", "method": opened.group(1), "sends": [],
                   "anchors": [], "source": "ffdec", "steps": []}
            var = {}
            base = depth
        if cur is not None:
            for raw in _STR.findall(line):
                s = _unquote(raw)
                if _anchor(s) and len(cur["anchors"]) < 12:
                    cur["anchors"].append(s[:80])
            vm = _VAR.search(line)
            if vm:
                var[vm.group(1)] = vm.group(3)
            sm = _SEND.search(line)
            if sm:
                raw_cls = sm.group(1) or var.get(sm.group(2) or "", "")
                msg = short_to_msg.get(raw_cls) or (
                    raw_cls if raw_cls in short_to_msg.values() else "")
                cur["sends"].append(msg or "?")
                cur["steps"].append({"d": max(depth, 1), "send": msg or "?"})
            elif re.search(r"\bif\s*\(", line):
                cur["steps"].append({"d": max(depth, 1), "if": True})
            elif re.search(r"\belse\b", line):
                cur["steps"].append({"d": max(depth, 1), "else": True})
        depth += line.count("{") - line.count("}")
        if depth < 0:
            depth = 0
        if cur is not None and base is not None and "}" in line and depth <= base:
            close()
    close()
    return out


def short_messages(parsed):
    out, bad = {}, set()
    for q, name in parsed["real"].items():
        if not usable(name):
            continue
        s = q.split(":")[-1]
        if s in out and out[s] != name:
            bad.add(s)
        else:
            out[s] = name
    for s in bad:
        out.pop(s, None)
    return out


def enrich_flows(parsed, swf, script_dir, ffdec):
    """有 ffdec 就反编译发过协议的类，用脚本里的 if/else 换掉直线扫描。"""
    java = shutil.which("java")
    if not ffdec or not java:
        return parsed["flows"], "bytecode", ""
    classes = sorted({f.get("class") or "" for f in parsed["flows"] if f.get("class")})
    names = []
    for q in classes:
        pkg, _, name = q.partition(":")
        names.append(f"{pkg}.{name}" if pkg else name)
    if not names:
        return parsed["flows"], "bytecode", ""
    os.makedirs(script_dir, exist_ok=True)
    err = []
    for i in range(0, len(names), 40):
        cmd = [java, "-jar", ffdec, "-selectclass", ",".join(names[i:i + 40]),
               "-export", "script", script_dir, swf]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        except subprocess.TimeoutExpired:
            err.append("ffdec 超时")
            break
        if proc.returncode != 0:
            err.append((proc.stderr or proc.stdout or "ffdec 失败")[-400:])
    got = flows_from_as(script_dir, short_messages(parsed))
    if got:
        return got, "ffdec", "\n".join(err)
    return parsed["flows"], "bytecode", "\n".join(err)


def _load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _write_json(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        f.write("\n")


def main():
    ap = argparse.ArgumentParser(description="从新版 SWF 生成协议编号对照和交互流程差异")
    ap.add_argument("swf")
    ap.add_argument("--canon-swf", default="", help="本项目编号那一版的 SWF，用来对照嵌套字段和交互流程")
    ap.add_argument("--version", default="")
    ap.add_argument("--out", default="out")
    ap.add_argument("--schema", default="")
    ap.add_argument("--flows-prev", default="", help="上一版 flows.json，没有旧 SWF 时用它对照发送顺序")
    ap.add_argument("--ffdec", default="")
    ap.add_argument("--merge", default="", help="把这一版写进已有的 wiremap.json")
    args = ap.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)
    schema_path = args.schema or os.path.join(root, "tankstorm", "schema.json")
    wire_path = os.path.join(root, "tankstorm", "wiremap.json")
    schema = _load_json(schema_path)
    pack = _load_json(wire_path)
    version = version_of(args.swf, args.version)
    if not version:
        sys.exit("认不出版本号。文件名里要有 10 位日期，或传 --version")
    canon_ver = str(pack.get("_canon") or "")
    ffdec = find_ffdec(args.ffdec)

    print(f"解析 {args.swf}")
    new_parsed = parse_swf(args.swf)
    flows, source, err = enrich_flows(
        new_parsed, args.swf, os.path.join(args.out, "scripts"), ffdec)
    print(f"opcode {len(new_parsed['reg'])} 条，具名类 {len(new_parsed['real'])} 个，"
          f"发送顺序 {len(flows)} 段（{source}）")
    if source == "bytecode":
        if ffdec and err:
            print(f"ffdec 没导出脚本：{err[-300:]}")
            print("发送顺序退回字节码直线扫描，if/else 会挤在一条线上。")
        elif not ffdec:
            print("没找到 ffdec。在服务器上装好后执行：")
            print("  export FFDEC_JAR=/实际路径/ffdec.jar")
            print("再跑一次，交互流程才会按反编译脚本里的分支来认。")
        elif not shutil.which("java"):
            print("找到了 ffdec，但没有 java，反编译跑不起来。")

    canon_parsed = None
    if args.canon_swf:
        print(f"解析基准 {args.canon_swf}")
        canon_parsed = parse_swf(args.canon_swf)
        same, total = schema_match(canon_parsed, schema)
        if not total or same / total < 0.9:
            print(f"基准 SWF 和 schema.json 对上 {same}/{total} 个字段，"
                  f"不像 {_canon_label(canon_ver)}。嵌套消息不会跟着翻。")
            canon_parsed = None
        else:
            print(f"基准 SWF 和 schema.json 对上 {same}/{total} 个字段")

    fallback = None
    if not canon_parsed:
        proto = os.path.join(root, "docs", "redwar.proto")
        if os.path.isfile(proto):
            decl = proto_decl(proto)
            same = total = 0
            for entry in schema.values():
                rows = {r["name"]: r["tag"] for r in decl.get(entry.get("name") or "", [])
                        if usable(r.get("name"))}
                for tag, meta in (entry.get("fields") or {}).items():
                    if not usable(meta[0]):
                        continue
                    total += 1
                    if rows.get(meta[0]) == int(tag):
                        same += 1
            if total and same / total >= 0.9:
                fallback = decl
                print(f"用 redwar.proto 补嵌套字段，和 schema.json 对上 {same}/{total} 个字段")
            else:
                print(f"redwar.proto 和 schema.json 对上 {same}/{total}，不用它补嵌套字段")

    entry, unresolved, missed = build_entry(schema, new_parsed, canon_parsed, fallback)
    print(f"对上 {len(entry['ops'])} 个消息，没在新版里找到 {len(missed)} 个，"
          f"嵌套字段认不出 {unresolved} 个")
    if missed[:8]:
        print("  新版没有：", "、".join(missed[:8]))

    overlay_path = os.path.join(args.out, "wiremap.json")
    if version == canon_ver:
        print(f"{version} 就是本项目的编号，不用翻译。")
    elif not numbers_changed(entry):
        print("opcode 和字段号都没变，不用加映射。")
    else:
        data = {}
        if os.path.exists(overlay_path):
            try:
                data = _load_json(overlay_path)
            except (OSError, ValueError):
                data = {}
        data[version] = entry
        _write_json(overlay_path, data)
        print(f"已写 {overlay_path}")
        print("放到程序目录（和 main.py 同级）后，下次连接会按这一版翻译。")
        if unresolved:
            print("标成 -1 的嵌套字段发出去会被拒绝，避免用旧字段号。")
            print(f"把 {canon_ver} 的 SWF 用 --canon-swf 传进来，才能把嵌套字段也对上。")
        if args.merge:
            merged = _load_json(args.merge)
            merged[version] = entry
            _write_json(args.merge, merged)
            print(f"已写入 {args.merge}")

    flow_path = os.path.join(args.out, f"flows-{version}.json")
    _write_json(flow_path, {"version": version, "source": source, "flows": flows})
    print(f"已写 {flow_path}")

    prev_flows = []
    prev_label = ""
    if canon_parsed:
        prev_flows, prev_source, _perr = enrich_flows(
            canon_parsed, args.canon_swf,
            os.path.join(args.out, "scripts-canon"), ffdec)
        prev_label = f"基准 SWF（{prev_source}）"
        if prev_source != source:
            print("两边的流程来源不一样，对照只看消息顺序，不看分支嵌套。")
    elif args.flows_prev:
        prev = _load_json(args.flows_prev)
        prev_flows = prev.get("flows") or []
        prev_label = f"上一份 {args.flows_prev}（{prev.get('source') or '未知'}）"

    diff_path = os.path.join(args.out, f"flow-diff-{version}.txt")
    lines = [f"版本 {version} 的发送顺序来自 {source}", ""]
    if prev_flows:
        changes = diff_flows(prev_flows, flows)
        lines.append(f"和{prev_label}相比，{len(changes)} 个方法的发送顺序变了。")
        lines.append("编号对照不会改这个顺序。下面这些要改对应的任务代码。")
        lines.append("")
        for old, now in changes[:40]:
            lines.append("——")
            anchors = "、".join((old.get("anchors") or [])[:4])
            lines.append(f"锚点 {anchors}")
            lines.append("旧:")
            lines.append(_fmt_steps(old))
            lines.append("新:")
            lines.append(_fmt_steps(now))
            lines.append("")
        if not changes:
            lines.append("对得上的方法里，发送顺序没有变化。")
    else:
        lines.append("没有上一版可以对照。下面是新版里出现不止一次的连发。")
        lines.append("下次更新把这份 flows 用 --flows-prev 传回来。")
        lines.append("")
        for seq, n in summarize(flows):
            lines.append(f"{n:4d} 次  {' → '.join(seq)}")
    text = "\n".join(lines) + "\n"
    with open(diff_path, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"已写 {diff_path}")
    print(text[:1200])


def _canon_label(ver):
    return ver or "schema.json 那一版"


if __name__ == "__main__":
    main()
