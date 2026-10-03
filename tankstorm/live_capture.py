# tankstorm-keepalive  Copyright (C) 2026 Dimlitter
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# 本程序是自由软件：你可以依据自由软件基金会发布的 GNU Affero 通用公共许可证
# （第 3 版，或你选择的任何更新版本）之条款，再分发和/或修改它。
# 本程序希望能有用，但不提供任何担保；甚至不含适销性或特定用途适用性的默示担保。
# 详见随附的 LICENSE 文件，或 <https://www.gnu.org/licenses/>。
"""用钩子版 SWF 实时抓客户端明文包。

官方 QQ 游戏大厅加载原版 SWF，Transport 加解密之后不会把字段交出来。
hjdz 能抓，是因为它自己的 WebEngine 窗口经 CDN 代理换上打过钩子的 RedWar.swf，
ExternalInterface 把明文打到页面。大厅登不进去之后，这条链路改到本仓库：

  扫码登录（QZone，已经能用）→ 拿到 canvas_url
  本机 HTTPS 代理冒充 redwar-cdn.sincetimes.com，投递钩子版 SWF
  flash_host（Qt WebEngine + pepflashplayer）打开 canvas_url
  钩子经 __pkt 上报，这里落盘并打印攻击相关字段

必须先停掉 --keepalive。游戏窗口是 flash_host，不是大厅。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .log import get_logger
from .paths import app_dir, bundled_dir, user_path
from .qzone import get_game_context
from .socket_keepalive import relogin_with_push

log = get_logger()

CDN_HOST = "redwar-cdn.sincetimes.com"
PROXY_PORT = 8443
PKT_PORT = 18766
SWF_RE = re.compile(r"RedWar_\d+\.swf", re.I)
SWF_URL_RE = re.compile(
    r"https?://[^\"'\s]+RedWar_\d+\.swf", re.I)

_HJDZ = os.path.normpath(os.path.join(app_dir(), "..", "hjdz-automation"))


def run(qq, config: dict, tank_range: int = 0) -> int:
    if not qq.is_valid() and not relogin_with_push(qq, config):
        return 1
    ctx = get_game_context(qq)
    canvas = ctx.get("canvas_url")
    if not canvas:
        log.error("没有游戏外框地址，扫码登录可能失败")
        return 1

    pkt_dir = user_path("pktcap")
    os.makedirs(pkt_dir, exist_ok=True)
    certs = _ensure_certs(pkt_dir)
    if not certs:
        return 1
    crt, key, spki = certs

    # FlashVars.version 不是 SWF 文件名（实测 2026092203 的主体是 RedWar_2026092201.swf）。
    # 能预下载就预下载；没有也不拦，等游戏向 CDN 要哪一份再打钩子。
    _prefetch_swf(qq, ctx, canvas, pkt_dir)

    ignore = _load_ignore()
    log_path = os.path.join(user_path("logs"), "packets",
                            datetime.now().strftime("%Y-%m-%d") + ".jsonl")
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    sink = {"fh": open(log_path, "a", encoding="utf-8"), "n": 0}

    httpd = _pkt_server(lambda rec: _on_pkt(rec, ignore, sink), canvas)
    proxy = _CdnProxy(crt, key, pkt_dir, tank_range)
    proxy.start()
    time.sleep(0.3)
    if not proxy.ok:
        httpd.shutdown()
        sink["fh"].close()
        return 1

    host_exe = _find_flash_host()
    flash_dll = _find_flash_dll(os.path.dirname(host_exe) if host_exe else "")
    if not host_exe:
        log.error("没有 flash_host.exe。先编：")
        log.error("  powershell -File tools/flash_host/build.ps1")
        proxy.stop()
        httpd.shutdown()
        sink["fh"].close()
        return 1
    if not flash_dll:
        log.error("没有 pepflashplayer.dll。把它放到 flash_host.exe 旁边，"
                  "或放在 hjdz-automation 目录里")
        proxy.stop()
        httpd.shutdown()
        sink["fh"].close()
        return 1

    cfg_path = os.path.join(pkt_dir, "flash_host.json")
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump({
            "url": f"http://127.0.0.1:{PKT_PORT}/play",
            "cdn_host": CDN_HOST,
            "proxy_port": PROXY_PORT,
            "spki": spki,
            "flash": flash_dll.replace("\\", "/"),
            "pkt_url": f"http://127.0.0.1:{PKT_PORT}/pkt",
        }, f, ensure_ascii=False, indent=1)

    log.info("保活是另一个窗口里的 py main.py --keepalive，在那个窗口 Ctrl+C 停")
    log.info("关掉游戏窗口才会结束。日志 %s", log_path)
    proc = subprocess.Popen(
        [host_exe, "--cfg", cfg_path],
        cwd=os.path.dirname(host_exe),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE)
    def _host_err():
        for raw in proc.stderr:
            line = raw.decode("utf-8", "replace").rstrip()
            if line:
                log.info("窗口 %s", line)
    threading.Thread(target=_host_err, daemon=True).start()
    try:
        while proc.poll() is None:
            time.sleep(0.4)
    except KeyboardInterrupt:
        log.info("停止抓包")
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    finally:
        proxy.stop()
        threading.Thread(target=httpd.shutdown, daemon=True).start()
        time.sleep(0.2)
        sink["fh"].close()
    log.info("游戏窗口已关 exit=%s，本轮共记下 %d 条", proc.returncode, sink["n"])
    return 0


def _on_pkt(rec: dict, ignore: set, sink: dict):
    cls = _short_cls(rec.get("cls") or "")
    if cls in ignore:
        return
    rec = dict(rec)
    rec["cls"] = cls
    rec["ts"] = time.strftime("%H:%M:%S")
    sink["fh"].write(json.dumps(rec, ensure_ascii=False) + "\n")
    sink["fh"].flush()
    sink["n"] += 1
    dump = rec.get("dump") or ""
    direction = rec.get("dir") or ""
    if cls in ("RceCountryOpt", "RseCountryOpt", "RseCountryUserLst") or (
            direction == "out" and "atkUserID" in dump):
        log.info("[抓包] %s %s  %s", direction, cls, dump[:400])
    elif sink["n"] <= 8 or sink["n"] % 50 == 0:
        log.info("[抓包] 已收 %d 条  最近 %s %s", sink["n"], direction, cls)


def _short_cls(cls: str) -> str:
    i = cls.rfind("::")
    s = cls[i + 2:] if i >= 0 else cls
    return s.strip("§")


def _load_ignore() -> set:
    names = set()
    for p in (os.path.join(bundled_dir(), "tools", "hook", "ignore.txt"),
              user_path(os.path.join("pktcap", "ignore.txt"))):
        if not os.path.isfile(p):
            continue
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.split("#", 1)[0].strip()
                if line:
                    names.add(line)
    return names


def _pkt_server(on_pkt, play_src=""):
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.split("?", 1)[0] != "/play" or not play_src:
                self.send_error(404)
                return
            # QZone frameCheck.js：顶层打开外框会 alert「运行环境出错」并跳走。
            body = (
                "<!doctype html><meta charset=utf-8><title>坦克风暴</title>"
                "<style>html,body,iframe{margin:0;height:100%;width:100%;"
                "border:0;overflow:hidden}</style>"
                f'<iframe src="{play_src.replace("&", "&amp;")}" allow="plugins *;fullscreen *"></iframe>'
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if self.path != "/pkt":
                self.send_error(404)
                return
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n)
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            try:
                on_pkt(json.loads(raw.decode("utf-8")))
            except Exception as exc:
                log.debug("pkt 解析失败：%s", exc)

        def log_message(self, *args):
            return

    httpd = ThreadingHTTPServer(("127.0.0.1", PKT_PORT), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def _swf_candidates(qq, ctx, canvas):
    """FlashVars.version 常是配置表号，SWF 文件名另算。按可能性列出候选 URL。"""
    html = ""
    try:
        html = qq.session.get(canvas, timeout=20,
                              headers={"Referer": "https://game.qzone.qq.com/"}).text
    except Exception as exc:
        log.warning("拉外框页失败：%s", exc)
    seen, out = set(), []

    def add(url, name=None):
        found = SWF_RE.search(url)
        name = name or (found.group(0) if found else "")
        if not name or url in seen:
            return
        seen.add(url)
        out.append((name, url))

    m = SWF_URL_RE.search(html or "")
    if m:
        add(m.group(0))
    base = (ctx.get("storageURL") or
            f"https://{CDN_HOST}/100616028/res/20120522/").rstrip("/")
    ver = str(ctx.get("version") or "").strip()
    names = []
    if re.fullmatch(r"\d{10}", ver):
        names.append(f"RedWar_{ver[:8]}01.swf")
        names.append(f"RedWar_{ver}.swf")
    elif ver.isdigit():
        names.append(f"RedWar_{ver}.swf")
    for name in names:
        add(f"{base}/flash/{name}", name)
    return out


def _prefetch_swf(qq, ctx, canvas, pkt_dir) -> str:
    as_path, ffdec = _hook_as(), _find_ffdec()
    if not as_path or not ffdec:
        log.error("打钩子需要 tools/hook/Transport.as 和 ffdec.jar"
                  "（环境变量 FFDEC_JAR，或 D:\\tools\\ffdec\\ffdec.jar）")
        return ""
    for name, url in _swf_candidates(qq, ctx, canvas):
        dest = os.path.join(pkt_dir, name)
        if os.path.isfile(dest) and os.path.getsize(dest) > 1_000_000:
            log.info("已有钩子版 %s", dest)
            return name
        if _hook_swf(pkt_dir, name, url):
            return name
    log.info("预下载没碰到现成的 SWF，等游戏窗口自己来要")
    return ""


def _hook_swf(pkt_dir, name, url) -> bool:
    dest = os.path.join(pkt_dir, name)
    if os.path.isfile(dest) and os.path.getsize(dest) > 1_000_000:
        return True
    as_path, ffdec = _hook_as(), _find_ffdec()
    if not as_path or not ffdec:
        return False
    src = os.path.join(pkt_dir, "orig_" + name)
    log.info("下载原版 %s", url)
    try:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=120) as r:
            data = r.read()
        if len(data) < 1_000_000:
            log.warning("不像主体 SWF（%d 字节），跳过 %s", len(data), name)
            return False
        with open(src, "wb") as f:
            f.write(data)
    except Exception as exc:
        log.info("没有 %s（%s）", name, exc)
        return False
    tmp = dest + ".tmp.swf"
    java = _find_java()
    cmd = [java, "-jar", ffdec, "-replace", src, tmp,
           "com.sincetimes.redwar.game.comnunicate.Transport", as_path]
    log.info("打钩子 %s", name)
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=180)
    except Exception as exc:
        log.error("ffdec 失败：%s", exc)
        return False
    if p.returncode != 0 or not os.path.isfile(tmp) or os.path.getsize(tmp) < 1_000_000:
        err = (p.stderr or p.stdout or b"").decode("utf-8", "replace")[:400]
        log.error("打钩子失败：%s", err)
        return False
    os.replace(tmp, dest)
    log.info("已写入 %s（%d 字节）", dest, os.path.getsize(dest))
    return True


def _hook_as() -> str:
    for p in (os.path.join(bundled_dir(), "tools", "hook", "Transport.as"),
              os.path.join(_HJDZ, "swf_dump", "hook", "Transport.as")):
        if os.path.isfile(p):
            return p
    return ""


def _find_ffdec() -> str:
    env = os.environ.get("FFDEC_JAR") or ""
    cands = [env, r"D:\tools\ffdec\ffdec.jar", r"D:\ffdec\ffdec.jar",
             r"C:\ffdec\ffdec.jar",
             os.path.join(bundled_dir(), "tools", "ffdec.jar")]
    for p in cands:
        if p and os.path.isfile(p):
            return p
    return ""


def _find_java() -> str:
    home = os.environ.get("JAVA_HOME") or ""
    if home:
        exe = os.path.join(home, "bin", "java.exe")
        if os.path.isfile(exe):
            return exe
    return "java"


def _find_flash_host() -> str:
    root = bundled_dir()
    cands = [
        os.path.join(root, "tools", "flash_host", "build", "release",
                     "release", "flash_host.exe"),
        os.path.join(root, "tools", "flash_host", "build", "Release",
                     "release", "flash_host.exe"),
        os.path.join(root, "tools", "flash_host", "build", "release",
                     "flash_host.exe"),
        os.path.join(app_dir(), "flash_host.exe"),
    ]
    for p in cands:
        if os.path.isfile(p):
            return p
    build = os.path.join(root, "tools", "flash_host", "build")
    if os.path.isdir(build):
        for dirpath, _, files in os.walk(build):
            if "flash_host.exe" in files:
                return os.path.join(dirpath, "flash_host.exe")
    return ""


def _find_flash_dll(host_dir: str) -> str:
    cands = [
        os.path.join(host_dir, "pepflashplayer.dll"),
        os.path.join(_HJDZ, "pepflashplayer.dll"),
        os.path.join(_HJDZ, "build", "Release", "pepflashplayer.dll"),
        os.path.join(_HJDZ, "dist", "hjdz-20260924", "pepflashplayer.dll"),
    ]
    for p in cands:
        if p and os.path.isfile(p):
            return os.path.abspath(p)
    return ""


def _cert_pair_ok(crt, key) -> bool:
    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(crt, key)
        return True
    except ssl.SSLError:
        return False


def _ensure_certs(pkt_dir: str):
    crt = os.path.join(pkt_dir, "pktproxy.crt")
    key = os.path.join(pkt_dir, "pktproxy.key")
    spki_p = os.path.join(pkt_dir, "pktproxy.spki")
    names = ("pktproxy.crt", "pktproxy.key", "pktproxy.spki")

    def ready():
        return (os.path.isfile(crt) and os.path.isfile(key)
                and os.path.isfile(spki_p) and _cert_pair_ok(crt, key))

    # hjdz 的 pktcap/ 证书和私钥曾经对不上，配对文件在 tools/pktcert/。
    if not ready():
        for src_dir in (os.path.join(_HJDZ, "tools", "pktcert"),
                        os.path.join(_HJDZ, "pktcap")):
            srcs = [os.path.join(src_dir, n) for n in names]
            if not all(os.path.isfile(p) for p in srcs):
                continue
            if not _cert_pair_ok(srcs[0], srcs[1]):
                continue
            for n in names:
                shutil.copy2(os.path.join(src_dir, n), os.path.join(pkt_dir, n))
            break
    if ready():
        spki = open(spki_p, encoding="ascii").read().strip()
        log.info("CDN 代理证书就绪 spki=%s…", spki[:16])
        return crt, key, spki
    openssl = _find_openssl()
    if not openssl:
        log.error("没有代理证书。把 hjdz 的 pktcap/pktproxy.{crt,key,spki} "
                  "拷到 pktcap/，或安装 openssl 后重跑")
        return None
    cfg = os.path.join(pkt_dir, "openssl.cnf")
    with open(cfg, "w", encoding="ascii") as f:
        f.write(
            "[req]\ndistinguished_name=dn\nx509_extensions=ext\nprompt=no\n"
            f"[dn]\nCN={CDN_HOST}\n[ext]\nsubjectAltName=DNS:{CDN_HOST}\n"
            "basicConstraints=critical,CA:FALSE\n"
            "keyUsage=critical,digitalSignature,keyEncipherment\n"
            "extendedKeyUsage=serverAuth\n")
    r = subprocess.run(
        [openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-sha256",
         "-days", "3650", "-keyout", key, "-out", crt, "-config", cfg],
        capture_output=True)
    if r.returncode != 0 or not os.path.isfile(crt):
        log.error("openssl 发证书失败：%s", r.stderr[:300])
        return None
    spki = _spki(openssl, crt)
    if not spki:
        return None
    with open(spki_p, "w", encoding="ascii") as f:
        f.write(spki)
    return crt, key, spki


def _spki(openssl, crt) -> str:
    p1 = subprocess.run([openssl, "x509", "-in", crt, "-pubkey", "-noout"],
                        capture_output=True)
    p2 = subprocess.run([openssl, "pkey", "-pubin", "-outform", "der"],
                        input=p1.stdout, capture_output=True)
    p3 = subprocess.run([openssl, "dgst", "-sha256", "-binary"],
                        input=p2.stdout, capture_output=True)
    p4 = subprocess.run([openssl, "enc", "-base64"],
                        input=p3.stdout, capture_output=True)
    return (p4.stdout or b"").decode("ascii").strip()


def _find_openssl() -> str:
    for p in (os.environ.get("OPENSSL") or "",
              r"D:\strawberry-perl-5.42.0.1-64bit-portable\c\bin\openssl.exe",
              # Git for Windows 自带一份，克隆了仓库的机器基本都有
              r"C:\Program Files\Git\usr\bin\openssl.exe",
              os.path.join(os.environ.get("LOCALAPPDATA", ""),
                           r"Programs\Git\usr\bin\openssl.exe"),
              shutil.which("openssl") or ""):
        if p and os.path.isfile(p):
            return p
    return ""


def _patch_tank_range(swf: bytes, rng: int) -> bytes:
    """把坦克表里的射程 375 改成 rng。

    armyData 表以 DefineBinaryData 内嵌在主体 SWF（RedWar 把它列在 securityList，
    CDN 上的同名外部配置会被内嵌版覆盖，所以只能改 SWF）。Unit._attackRange 就是从
    这张表的 attackRange 列按等级取的；armyPacker 上报给服务端的 range 也是它。
    表是 GBK、\\r\\n 分行、\\t 分列，第 0 行列类型、第 1 行表头「中文(英文键)」。
    """
    from tools.swfparse import replace_binary_data

    def fix(tsv: bytes) -> bytes:
        rows = tsv.decode("gbk").split("\r\n")
        keys = [(re.search(r"\((\w+)\)", h) or [h, h])[1] for h in rows[1].split("\t")]
        gi, ri = keys.index("group"), keys.index("attackRange")
        for i, row in enumerate(rows):
            c = row.split("\t")
            if i >= 2 and len(c) > ri and c[gi] == "2":
                c[ri] = re.sub(r"(?<!\d)375(?!\d)", str(rng), c[ri])
                rows[i] = "\t".join(c)
        return "\r\n".join(rows).encode("gbk")

    return replace_binary_data(
        swf, "com.sincetimes.redwar.game.RedWar_armyData_zh_CN", fix)


class _CdnProxy:
    def __init__(self, crt, key, pkt_dir, tank_range=0):
        self.pkt_dir = pkt_dir
        self.tank_range = tank_range
        self.ok = False
        self._stop = threading.Event()
        self._hook_lock = threading.Lock()
        self._srv = None
        self._tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        # Qt 5.15 的 Chromium 偏旧，Python 3.14 默认 TLS1.3/新密钥组会对不上，握手一直等到超时。
        self._tls.minimum_version = ssl.TLSVersion.TLSv1_2
        self._tls.maximum_version = ssl.TLSVersion.TLSv1_2
        self._tls.set_ciphers(
            "ECDHE-RSA-AES128-GCM-SHA256:ECDHE-RSA-AES256-GCM-SHA384:"
            "ECDHE-RSA-AES128-SHA256:AES128-GCM-SHA256:AES128-SHA")
        self._tls.load_cert_chain(crt, key)
        try:
            self._tls.set_alpn_protocols(["http/1.1"])
        except ssl.SSLError:
            pass

    def start(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind(("127.0.0.1", PROXY_PORT))
            srv.listen(32)
            srv.settimeout(0.5)
        except OSError as exc:
            log.error("听不了 127.0.0.1:%d：%s", PROXY_PORT, exc)
            srv.close()
            return
        self._srv = srv
        self.ok = True
        threading.Thread(target=self._accept, daemon=True).start()
        log.info("CDN 代理 127.0.0.1:%d → %s（RedWar_*.swf 换成钩子版）",
                 PROXY_PORT, CDN_HOST)

    def stop(self):
        self._stop.set()
        if self._srv:
            try:
                self._srv.close()
            except OSError:
                pass

    def _accept(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._client, args=(conn,),
                             daemon=True).start()

    def _http_loop(self, sock):
        b = _Buf(sock)
        while not self._stop.is_set():
            try:
                head = b.read_headers()
            except (TimeoutError, socket.timeout):
                return
            if not head:
                return
            self._http1(b, head)

    def _client(self, conn: socket.socket):
        peeked = b""
        try:
            conn.settimeout(30)
            peeked = conn.recv(3, socket.MSG_PEEK)
            if not peeked:
                return
            if peeked[:1] == b"\x16":
                self._http_loop(self._tls.wrap_socket(conn, server_side=True))
                return
            # 逐字节读完 CONNECT，避免把随后的 ClientHello 吃进 Python 缓冲。
            # wrap_socket 走的是 fd，那边已经读走的字节找不回来。
            head = b""
            while b"\r\n\r\n" not in head:
                ch = conn.recv(1)
                if not ch:
                    break
                head += ch
                if len(head) > 65536:
                    break
            if not head:
                log.warning("代理没读到请求头 peek=%s", peeked.hex())
                return
            if head.startswith(b"CONNECT"):
                conn.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                self._http_loop(self._tls.wrap_socket(conn, server_side=True))
            else:
                b = _Buf(conn)
                self._http1(b, head)
                while not self._stop.is_set():
                    more = b.read_headers()
                    if not more:
                        return
                    self._http1(b, more)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError,
                TimeoutError, socket.timeout, ssl.SSLError):
            return
        except OSError as exc:
            if getattr(exc, "winerror", None) in (10053, 10054, 10038):
                return
            log.warning("代理连接结束：%s peek=%s", exc, peeked.hex())
        except Exception as exc:
            log.warning("代理连接结束：%s peek=%s", exc, peeked.hex())
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _http1(self, b, head: bytes):
        line = head.split(b"\r\n", 1)[0].decode("latin-1", "replace")
        parts = line.split(" ")
        if len(parts) < 2:
            return
        path = parts[1]
        asked = path.rsplit("/", 1)[-1].split("?", 1)[0]
        n = _content_length(head)
        if n:
            b.read_exact(n)
        if SWF_RE.fullmatch(asked):
            dest = os.path.join(self.pkt_dir, asked)
            with self._hook_lock:
                if not (os.path.isfile(dest) and os.path.getsize(dest) > 1_000_000):
                    url_path = path.split("?", 1)[0]
                    if not url_path.startswith("/"):
                        url_path = "/" + url_path.split("/", 3)[-1]
                    _hook_swf(self.pkt_dir, asked, f"https://{CDN_HOST}{url_path}")
            if os.path.isfile(dest) and os.path.getsize(dest) > 1_000_000:
                data = open(dest, "rb").read()
                if self.tank_range > 0:
                    try:
                        data = _patch_tank_range(data, self.tank_range)
                        log.info("坦克射程 375 -> %d", self.tank_range)
                    except Exception as exc:
                        log.warning("改射程失败，按原样投递：%s", exc)
                _write_http(b, 200, {
                    b"Content-Type": b"application/x-shockwave-flash",
                    b"Cache-Control": b"no-store",
                    b"Access-Control-Allow-Origin": b"*",
                }, data)
                log.info("已投递钩子版 %s（%d 字节）", asked, len(data))
                return
            log.warning("钩子版没有就绪，本次透传原版 %s", asked)
        if path.startswith("http://") or path.startswith("https://"):
            parsed = urlparse(path)
            path = parsed.path + (("?" + parsed.query) if parsed.query else "")
        if not path.startswith("/"):
            path = "/" + path
        if asked.lower().endswith(".swf"):
            log.info("透传 %s", asked)
        self._forward(b, path)

    def _forward(self, b, path: str):
        url = f"https://{CDN_HOST}{path}"
        try:
            req = Request(url, headers={"Accept-Encoding": "identity",
                                        "User-Agent": "Mozilla/5.0"})
            with urlopen(req, timeout=60) as r:
                data = r.read()
                ctype = r.headers.get("Content-Type", "application/octet-stream")
                status = getattr(r, "status", 200)
        except Exception as exc:
            log.warning("转发失败 %s：%s", path[:80], exc)
            _write_http(b, 502, {b"Content-Type": b"text/plain"},
                        b"upstream error")
            return
        _write_http(b, int(status), {
            b"Content-Type": ctype.encode("latin-1", "replace"),
            b"Cache-Control": b"no-store",
        }, data)


class _Buf:
    def __init__(self, sock):
        self.sock = sock
        self.buf = b""

    def sendall(self, data):
        self.sock.sendall(data)

    def read_headers(self) -> bytes:
        while b"\r\n\r\n" not in self.buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                break
            self.buf += chunk
            if len(self.buf) > 64 * 1024:
                break
        i = self.buf.find(b"\r\n\r\n")
        if i < 0:
            return b""
        head, self.buf = self.buf[: i + 4], self.buf[i + 4:]
        return head

    def read_exact(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(max(4096, n - len(self.buf)))
            if not chunk:
                break
            self.buf += chunk
        take, self.buf = self.buf[:n], self.buf[n:]
        return take


def _content_length(head: bytes) -> int:
    for line in head.split(b"\n"):
        if line.lower().startswith(b"content-length:"):
            try:
                return int(line.split(b":", 1)[1].strip())
            except ValueError:
                return 0
    return 0


def _write_http(sock, status: int, headers: dict, body: bytes):
    reason = b"OK" if status == 200 else b"ERR"
    rows = [f"HTTP/1.1 {status} {reason.decode()}".encode("latin-1"),
            b"Connection: keep-alive",
            f"Content-Length: {len(body)}".encode("ascii")]
    for k, v in headers.items():
        rows.append(k + b": " + v)
    sock.sendall(b"\r\n".join(rows) + b"\r\n\r\n" + body)
