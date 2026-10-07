# tankstorm-keepalive  Copyright (C) 2026 Dimlitter
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# 本程序是自由软件：你可以依据自由软件基金会发布的 GNU Affero 通用公共许可证
# （第 3 版，或你选择的任何更新版本）之条款，再分发和/或修改它。
# 本程序希望能有用，但不提供任何担保；甚至不含适销性或特定用途适用性的默示担保。
# 详见随附的 LICENSE 文件，或 <https://www.gnu.org/licenses/>。
"""QQ 扫码登录（ptlogin2 协议），获取空间游戏所需 cookie（uin/skey/p_skey）。

流程：
  1. 请求 ptqrshow 拿二维码图片，同时得到 cookie qrsig；
  2. 手机 QQ 扫码；脚本轮询 ptqrlogin，携带 hash33(qrsig) 计算的 ptqrtoken；
  3. 扫码确认后返回 check_sig 跳转地址，GET 一次即种下全套登录 cookie；
  4. cookie 序列化到 cookies.json，下次运行直接复用；失效后需要重新扫码。

保活默认走扫码。窗口里也可以账号密码登录（pt_tea=2）。
滑块开在独立浏览器窗口里，并在同一个窗口提交密码，避免和日常浏览器串成两台设备。
划过之后这套窗口的设备票据会留下，下次密码登录就不必再扫码。

推送登录（页面上点头像 → 手机收到确认）—— 2026-08-13 抓浏览器逆出来的
--------------------------------------------------------------------
它不是"另一种取二维码"，而是**挂在已有二维码会话上的一个动作**。顺序必须是：

    xlogin                      建立 pt_login_sig
    ptqrshow（普通）            拿到 PNG 和 qrsig
    pt_fetch_dev_uin            换取 dev_mid_sig（设备签名）
    ptqrshow?qr_push=1&type=1   把这个会话推给指定 QQ（不带 e/l/s/d/v 图片参数）
    ptqrlogin 轮询              和扫码共用一条轮询，带 has_onekey=1

三个错误码的含义（试出来的）：

    ec=313 提交参数错误  —— 没有 dev_mid_sig，服务端认不出这台设备
    ec=315 页面过期      —— 有 dev_mid_sig 但已失效，得重新 pt_fetch_dev_uin
    ec=0                —— 推送已发出，手机上点确认即可

设备记录挂在哪：不是 pt_guid_sig（2026-08-13 实测）
--------------------------------------------------
对着 pt_fetch_dev_uin 试出来的：

    什么设备 cookie 都不带                          -> errcode 22027，不补发
    只带 pt_guid_sig（哪怕浏览器里正在用的那个）    -> errcode 22027，不补发
    带 dev_mid_sig（+pt_guid_sig+pt_recent_uins）   -> errcode 22028，但 data:[] 且**不补发**
    浏览器自己发的同一个接口                        -> errcode 22028，data:[<uin>]，**补发**

也就是说 pt_guid_sig 不是设备身份，dev_mid_sig 才是；而这个接口对我们
只认不发。浏览器比我们多带 pt-ev-token / dlock / it_c / eas_sid 等设备安全
cookie，差别很可能在那里，但**尚未证实**。

第一个 dev_mid_sig 从哪来仍然未知 —— 两份登录页抓包都只拍到"把早就存在的那份
续下去"，没拍到它被创建。所以现在只能从浏览器搬一次（`--import-device`）。
注意搬来的签名有时效：拿一小时前抓包里的那份去推送，服务端回 ec=315。

pt_guid_sig 与它配对，由 xlogin 或登录成功时的 ptqrlogin 下发。注意
**xlogin 会无条件重新签发一个**，所以做 pt_fetch_dev_uin 之前要把原来那个存住。

另外两条路确实走不通：

- **静默续期**（pt_login）：该端点现在返回一张腾讯网首页 HTML，已下线。
- **网页「快捷登录」里的本地通道**：反复 CONNECT
  `localhost.ptlogin2.qq.com:430X`，是在跟本机 QQ 桌面客户端要票据，
  且有证书绑定。但实测**推送并不依赖它** —— 把那几个本地令牌全删掉，
  推送照常工作。
"""

import base64
import hashlib
import json
import os
import random
import re
import secrets
import socket
import struct
import subprocess
import time
import urllib.parse

import requests

from . import GAME_URL
from . import lockqq
from . import paths
from .log import get_logger

log = get_logger()

PTLOGIN_APPID = "549000912"  # QQ空间的 ptlogin aid（游戏页 302 时携带的就是它）
DAID = "5"

# 设备/长效凭据：推送登录靠它们识别"推给哪台设备"，重新登录时不能清掉。
#
# 2026-08-13 抓包定位：真正让推送成立的是 **dev_mid_sig**（设备中间签名）。
# 早先这份名单里没有它，于是每次登录前的清理都会把它删掉，
# ptqrshow?qr_push=1 就只能回 ec=313「提交参数错误」。
# 一并保住同批的几个设备态 cookie，它们都是浏览器里跟着设备走的。
DEVICE_COOKIES = {"ptcz", "RK", "superkey", "supertoken", "superuin",
                  "pt2gguin", "pt_recent_uins", "ETK",
                  "dev_mid_sig", "pt_guid_sig", "uikey", "pt-ev-token",
                  "dlock", "it_c", "eas_sid", "pt_local_token",
                  "_qpsvr_localtk"}

# 从浏览器搬设备记录时**只搬这几个**：都是跟着设备走的，不含任何登录凭据。
# 特意**不搬** skey/p_skey/uin（登录态，会和本脚本自己的登录态打架），
# 也不搬 ptcz/RK/superkey/supertoken（长效登录凭据，同理）。
#
# 前三个是设备签名本体，后四个是浏览器请求 pt_fetch_dev_uin 时比我们多带的
# 设备安全类 cookie —— 只带前三个时服务端回 data:[] 且不补发签名，
# 浏览器带全了才回 data:[<uin>] 并补发，所以一并搬过来试。
DEVICE_BOOTSTRAP = ("dev_mid_sig", "pt_guid_sig", "pt_recent_uins",
                    "pt-ev-token", "dlock", "it_c", "eas_sid")
PTLOGIN_DOMAIN = ".ptlogin2.qq.com"

# cookie 和二维码是用户数据，必须落在 exe 旁边而不是打包的临时解压目录 ——
# 落错地方的后果是每次启动都要重新扫码，而且不报错。见 paths.py。
BASE_DIR = paths.app_dir()
COOKIE_FILE = paths.user_path("cookies.json")
QRCODE_FILE = paths.user_path("qrcode.png")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/109.0.0.0 Safari/537.36")


def hash33(s: str) -> int:
    """腾讯 ptqrtoken 算法。"""
    e = 0
    for c in s:
        e += (e << 5) + ord(c)
    return 2147483647 & e


def calc_g_tk(p_skey: str) -> int:
    """腾讯 g_tk / bkn 算法，很多接口用它做 CSRF 校验。"""
    h = 5381
    for c in p_skey:
        h += (h << 5) + ord(c)
    return h & 0x7FFFFFFF


# ptlogin2 密码加密（c_login_2.js getEncryption / TEA / RSA），与扫码无关。
_PT_RSA_N = int(
    "e9a815ab9d6e86abbf33a4ac64e9196d5be44a09bd0ed6ae052914e1a865ac8331fed863de8ea697"
    "e9a7f63329e5e23cda09c72570f46775b7e39ea9670086f847d3c9c51963b131409b1e04265d97"
    "47419c635404ca651bbcbc87f99b8008f7f5824653e3658be4ba73e4480156b390bb73bc1f8b33"
    "578e7a4e12440e9396f2552c1aff1c92e797ebacdc37c109ab7bce2367a19c56a033ee04534723"
    "cc2558cb27368f5b9d32c04d12dbd86bbd68b1d99b7c349a8453ea75d1b2e94491ab30acf6c46a"
    "36a75b721b312bedf4e7aad21e54e9bcbcf8144c79b6e3c05eb4a1547750d224c0085d80e6da39"
    "07c3d945051c13c7c1dcefd6520ee8379c4f5231ed",
    16,
)
_PT_RSA_E = 0x10001
_PT_MASK32 = 0xFFFFFFFF


def _js_bytes(text: str) -> bytes:
    return bytes(ord(ch) & 0xFF for ch in text)


def _md5_hex(data: bytes) -> str:
    return hashlib.md5(data).hexdigest().upper()


def _tea_block(block: bytes, key: bytes) -> bytes:
    y = int.from_bytes(block[0:4], "big")
    z = int.from_bytes(block[4:8], "big")
    k0 = int.from_bytes(key[0:4], "big")
    k1 = int.from_bytes(key[4:8], "big")
    k2 = int.from_bytes(key[8:12], "big")
    k3 = int.from_bytes(key[12:16], "big")
    s = 0
    m = _PT_MASK32
    for _ in range(16):
        s = (s + 0x9E3779B9) & m
        y = (y + (((z << 4) + k0) & m ^ (z + s) & m ^ ((z >> 5) + k1) & m)) & m
        z = (z + (((y << 4) + k2) & m ^ (y + s) & m ^ ((y >> 5) + k3) & m)) & m
    return y.to_bytes(4, "big") + z.to_bytes(4, "big")


def _qq_tea_encrypt(plain: bytes, key: bytes) -> bytes:
    n = len(plain)
    pad = (n + 10) % 8
    if pad:
        pad = 8 - pad
    out = bytearray(n + pad + 10)
    buf = bytearray(8)
    prev = bytearray(8)
    fill = 0
    written = 0
    prev_off = 0
    first = True

    def flush():
        nonlocal fill, written, prev_off, first
        for i in range(8):
            buf[i] ^= prev[i] if first else out[prev_off + i]
        enc = _tea_block(bytes(buf), key)
        for i in range(8):
            out[written + i] = enc[i] ^ prev[i]
            prev[i] = buf[i]
        prev_off = written
        written += 8
        fill = 0
        first = False

    buf[0] = (secrets.randbits(32) & 248) | pad
    for i in range(1, pad + 1):
        buf[i] = secrets.randbits(32) & 255
    fill = pad + 1
    extra = 1
    src = 0
    left = n
    while extra <= 2:
        if fill < 8:
            buf[fill] = secrets.randbits(32) & 255
            fill += 1
            extra += 1
        if fill == 8:
            flush()
    while left:
        if fill < 8:
            buf[fill] = plain[src]
            fill += 1
            src += 1
            left -= 1
        if fill == 8:
            flush()
    extra = 1
    while extra <= 7:
        if fill < 8:
            buf[fill] = 0
            fill += 1
            extra += 1
        if fill == 8:
            flush()
    return bytes(out)


def _rsa_encrypt(plain: bytes) -> bytes:
    k = (_PT_RSA_N.bit_length() + 7) // 8
    if k < len(plain) + 11:
        raise ValueError("RSA 明文太长")
    ps = bytearray()
    while len(ps) < k - len(plain) - 3:
        b = secrets.randbelow(256)
        if b:
            ps.append(b)
    em = bytes([0, 2]) + bytes(ps) + bytes([0]) + plain
    c = pow(int.from_bytes(em, "big"), _PT_RSA_E, _PT_RSA_N)
    return c.to_bytes(k, "big")


def encrypt_pt_password(password: str, salt: bytes, vcode: str) -> str:
    """pt_tea=2 的 p 参数。算法照 c_login_2.js getEncryption。"""
    pwd_md5 = _md5_hex(_js_bytes(password))
    tea_key = bytes.fromhex(_md5_hex(bytes.fromhex(pwd_md5) + salt))
    vcode_hex = vcode.upper().encode("utf-8").hex()
    vlen = format(len(vcode_hex) // 2, "x").zfill(4)
    plain = bytes.fromhex(pwd_md5 + salt.hex() + vlen + vcode_hex)
    cipher = _qq_tea_encrypt(plain, tea_key)
    blob = len(cipher).to_bytes(2, "big") + cipher
    raw = _rsa_encrypt(blob)
    return base64.b64encode(raw).decode("ascii").translate(
        str.maketrans({"/": "-", "+": "*", "=": "_"}))


def _js_string_bytes(raw: str) -> bytes:
    try:
        return raw.encode("latin1").decode("unicode_escape").encode("latin1")
    except UnicodeError:
        return _js_bytes(raw)


def _browser_exe() -> str:
    """独立窗口优先用 Edge，免得附着到正在开着的 Chrome 上。"""
    pf = os.environ.get("PROGRAMFILES", r"C:\Program Files")
    pf86 = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
    local = os.environ.get("LOCALAPPDATA", "")
    names = (r"Microsoft\Edge\Application\msedge.exe",
             r"Google\Chrome\Application\chrome.exe")
    for base in (pf86, pf, local):
        if not base:
            continue
        for name in names:
            path = os.path.join(base, name)
            if os.path.isfile(path):
                return path
    return ""


def _kill_profile(profile: str) -> None:
    if not profile or not os.path.isdir(profile):
        return
    env = os.environ.copy()
    env["PWD_BROWSER_PROFILE"] = os.path.normcase(profile)
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "$m=$env:PWD_BROWSER_PROFILE; "
             "Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | "
             "Where-Object { $_.CommandLine -and $_.CommandLine.ToLower().Contains($m.ToLower()) } | "
             "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"],
            env=env, timeout=30, creationflags=flags,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        pass


class _Ws:
    """只够跟本机浏览器调试端口说话。"""

    def __init__(self, url: str):
        u = urllib.parse.urlparse(url)
        self.sock = socket.create_connection((u.hostname, u.port), 10)
        key = base64.b64encode(os.urandom(16)).decode()
        path = u.path or "/"
        if u.query:
            path += "?" + u.query
        req = (f"GET {path} HTTP/1.1\r\nHost: {u.hostname}:{u.port}\r\n"
               "Upgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        self.sock.sendall(req.encode())
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise OSError("调试端口握手失败")
            data += chunk
        head, rest = data.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise OSError("调试端口拒绝连接")
        self._buf = rest

    def send_text(self, text: str) -> None:
        payload = text.encode()
        n = len(payload)
        mask = os.urandom(4)
        if n < 126:
            header = bytes([0x81, 0x80 | n])
        elif n < 65536:
            header = bytes([0x81, 0xFE]) + struct.pack("!H", n)
        else:
            header = bytes([0x81, 0xFF]) + struct.pack("!Q", n)
        masked = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
        self.sock.sendall(header + mask + masked)

    def recv_text(self, timeout: float) -> str:
        self.sock.settimeout(max(0.2, timeout))
        while True:
            opcode, data = self._frame()
            if opcode == 0x1:
                return data.decode("utf-8", "replace")
            if opcode == 0x8:
                raise OSError("调试连接已关闭")
            if opcode == 0x9:
                self._pong(data)

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass

    def _exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise OSError("调试连接中断")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _frame(self):
        parts = []
        opcode = None
        while True:
            h = self._exact(2)
            op, fin, ln = h[0] & 0x0F, h[0] & 0x80, h[1] & 0x7F
            if ln == 126:
                ln = struct.unpack("!H", self._exact(2))[0]
            elif ln == 127:
                ln = struct.unpack("!Q", self._exact(8))[0]
            if h[1] & 0x80:
                mask = self._exact(4)
                raw = bytes(b ^ mask[i & 3] for i, b in enumerate(self._exact(ln)))
            else:
                raw = self._exact(ln)
            if op in (0x8, 0x9) and fin:
                return op, raw
            if opcode is None:
                opcode = op
            parts.append(raw)
            if fin:
                return opcode or 0x1, b"".join(parts)

    def _pong(self, data: bytes) -> None:
        n = len(data)
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i & 3] for i, b in enumerate(data))
        header = bytes([0x8A, 0x80 | n]) if n < 126 else bytes([0x8A, 0xFE]) + struct.pack("!H", n)
        self.sock.sendall(header + mask + masked)


class _Cdp:
    def __init__(self, ws: _Ws):
        self.ws = ws
        self.n = 0

    def call(self, method: str, params=None, timeout: float = 20):
        self.n += 1
        i = self.n
        self.ws.send_text(json.dumps({"id": i, "method": method, "params": params or {}}))
        end = time.time() + timeout
        while time.time() < end:
            msg = json.loads(self.ws.recv_text(end - time.time()))
            if msg.get("id") != i:
                continue
            if msg.get("error"):
                raise OSError(msg["error"].get("message") or method)
            return msg.get("result") or {}
        raise TimeoutError(method)


def _xlogin_url(low_login: bool) -> str:
    q = urllib.parse.urlencode({
        "daid": DAID, "appid": PTLOGIN_APPID, "hide_title_bar": "1",
        "low_login": "1" if low_login else "0", "qlogin_auto_login": "1",
        "no_verifyimg": "1", "link_target": "blank", "style": "22",
        "target": "self", "s_url": GAME_URL,
    })
    return "https://xui.ptlogin2.qq.com/cgi-bin/xlogin?" + q


def _check_url(uin: str, login_sig: str) -> str:
    q = urllib.parse.urlencode({
        "regmaster": "", "pt_tea": "2", "pt_vcode": "1", "uin": uin,
        "appid": PTLOGIN_APPID, "js_ver": "26071711", "js_type": "1",
        "login_sig": login_sig, "u1": GAME_URL, "r": str(random.random()),
        "pt_uistyle": "22", "daid": DAID, "pt_3rd_aid": "0",
    })
    return "https://ssl.ptlogin2.qq.com/check?" + q


_SLIDER_HTML = """<!DOCTYPE html>
<meta charset="utf-8">
<title>滑动验证</title>
<p id="m">请在这个窗口里完成滑动。不要换到平时的浏览器，否则设备对不上。</p>
<script src="https://ssl.captcha.qq.com/TCaptcha.js"></script>
<script>
function done(res) {
  var ticket = res && res.ticket;
  var randstr = res && (res.randstr || res.randStr);
  if (res && res.ret === 0 && ticket && randstr) {
    fetch("/ticket", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({ticket: ticket, randstr: randstr})})
      .then(function(){ document.getElementById("m").textContent = "验证完成，可以回到程序。"; });
    return;
  }
  if (res && res.ret === 2) return;
  document.getElementById("m").textContent =
    (res && (res.errorMessage || res.errMessage)) || "滑动验证没过";
}
var cap = new TencentCaptcha(%(aid)s, done, {
  sid: %(sid)s, uin: %(uin)s, type: "popup", enableAged: true});
cap.show();
</script>
"""


_WINDOW_CLOSED = "登录窗口已关闭，请重新登录"


class _LoginWindow:
    """单独的 Edge/Chrome 配置。滑块和密码提交都在这个窗口里，不碰日常浏览器。"""

    def __init__(self):
        self.proc = None
        self.ws = None
        self.cdp = None
        self.httpd = None
        self.dbg_port = 0
        self._page_miss = 0
        self.box = {}
        self.page = {"aid": PTLOGIN_APPID, "sid": "", "uin": ""}
        self.profile = paths.user_path("pwd_browser")

    def gone(self) -> bool:
        """用户关掉了登录窗口。

        不能看启动进程是否退出：Edge 的启动进程经常马上交出去，窗口还在。
        调试端口上没有页面，并且连续几次都这样，才算关掉。
        """
        if not self.dbg_port:
            return False
        try:
            rows = requests.get(
                f"http://127.0.0.1:{self.dbg_port}/json/list", timeout=1).json()
        except requests.ConnectionError:
            self._page_miss += 1
            return self._page_miss >= 3
        except requests.RequestException:
            return False
        if any(r.get("type") == "page" for r in rows):
            self._page_miss = 0
            return False
        self._page_miss += 1
        return self._page_miss >= 3

    def _raise_if_closed(self) -> None:
        if self.gone():
            raise OSError(_WINDOW_CLOSED)

    def start(self) -> str:
        import http.server
        import threading

        exe = _browser_exe()
        if not exe:
            return "没有找到 Edge 或 Chrome，打不开独立登录窗口"
        os.makedirs(self.profile, exist_ok=True)
        _kill_profile(self.profile)
        box, page = self.box, self.page

        class _H(http.server.BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                return

            def do_GET(self):
                path = self.path.split("?", 1)[0]
                if path == "/slide":
                    body = _SLIDER_HTML % {
                        "aid": json.dumps(str(page["aid"])),
                        "sid": json.dumps(str(page["sid"])),
                        "uin": json.dumps(str(page["uin"])),
                    }
                else:
                    body = ("<!DOCTYPE html><meta charset='utf-8'><title>密码登录</title>"
                            "<p>这是密码登录专用窗口，和日常浏览器分开。</p>"
                            "<p>需要滑动时会在这里出现，不要换到平时那个窗口。</p>")
                raw = body.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                try:
                    data = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    data = {}
                box["ticket"] = str(data.get("ticket") or "")
                box["randstr"] = str(data.get("randstr") or "")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), _H)
        http_port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        dbg = socket.socket()
        dbg.bind(("127.0.0.1", 0))
        dbg_port = dbg.getsockname()[1]
        dbg.close()
        self.dbg_port = dbg_port
        self.proc = subprocess.Popen(
            [exe, f"--user-data-dir={self.profile}",
             f"--remote-debugging-port={dbg_port}",
             "--remote-allow-origins=*",
             "--no-first-run", "--no-default-browser-check",
             "--disable-sync", "--disable-extensions",
             "--disable-popup-blocking", "--hide-crash-restore-bubble",
             "--window-size=440,680",
             f"--app=http://127.0.0.1:{http_port}/"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        page_url = ""
        deadline = time.time() + 15
        while time.time() < deadline and not page_url:
            try:
                rows = requests.get(f"http://127.0.0.1:{dbg_port}/json/list", timeout=1).json()
            except (requests.RequestException, ValueError):
                time.sleep(0.25)
                continue
            for row in rows:
                if row.get("type") == "page" and row.get("webSocketDebuggerUrl"):
                    page_url = row["webSocketDebuggerUrl"]
                    break
            if not page_url:
                time.sleep(0.25)
        if not page_url:
            self.close()
            return "独立登录窗口没有连上"
        self.ws = _Ws(page_url)
        self.cdp = _Cdp(self.ws)
        self.cdp.call("Page.enable")
        self.cdp.call("Network.enable")
        self.cdp.call("Network.setExtraHTTPHeaders", {"headers": {
            "Referer": "https://xui.ptlogin2.qq.com/",
        }})
        try:
            self.cdp.call("Page.bringToFront")
        except (OSError, TimeoutError):
            pass
        return ""

    def close(self) -> None:
        if self.cdp:
            try:
                self.cdp.call("Browser.close", timeout=3)
            except (OSError, TimeoutError):
                pass
            self.cdp = None
        if self.ws:
            self.ws.close()
            self.ws = None
        if self.httpd:
            try:
                self.httpd.shutdown()
                self.httpd.server_close()
            except OSError:
                pass
            self.httpd = None
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.proc.pid)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=flags)
        self.proc = None
        _kill_profile(self.profile)

    def _eval(self, expr: str, timeout: float = 20, await_promise: bool = False):
        result = self.cdp.call("Runtime.evaluate", {
            "expression": expr, "returnByValue": True, "awaitPromise": await_promise,
        }, timeout=timeout)
        if result.get("exceptionDetails"):
            raise OSError("页面脚本失败")
        return (result.get("result") or {}).get("value")

    def page_text(self) -> str:
        return self._eval(
            "(document.documentElement&&(document.documentElement.innerText"
            "+'\\n'+document.documentElement.outerHTML))||''") or ""

    def goto(self, url: str, needle: str, timeout: float = 20) -> str:
        self._raise_if_closed()
        self.cdp.call("Page.navigate", {"url": url}, timeout=15)
        end = time.time() + timeout
        text = ""
        while time.time() < end:
            self._raise_if_closed()
            time.sleep(0.25)
            try:
                text = self._eval(
                    "(document.documentElement&&(document.documentElement.innerText"
                    "+'\\n'+document.documentElement.outerHTML))||''", timeout=2) or ""
            except (OSError, TimeoutError):
                self._raise_if_closed()
                continue
            if needle in text:
                return text
        self._raise_if_closed()
        return text

    def open_until(self, url: str, href_part: str, timeout: float = 20) -> bool:
        self._raise_if_closed()
        self.cdp.call("Page.navigate", {"url": url}, timeout=15)
        end = time.time() + timeout
        while time.time() < end:
            self._raise_if_closed()
            time.sleep(0.25)
            try:
                href = self._eval("location.href", timeout=2) or ""
                state = self._eval("document.readyState", timeout=2) or ""
            except (OSError, TimeoutError):
                self._raise_if_closed()
                continue
            if href_part in href and state == "complete":
                return True
        self._raise_if_closed()
        return False

    def follow(self, url: str, timeout: float = 20) -> None:
        try:
            before_href = self._eval("location.href") or ""
        except (OSError, TimeoutError):
            before_href = ""
        before_skey = self.cookie("skey")
        self._raise_if_closed()
        self.cdp.call("Page.navigate", {"url": url}, timeout=15)
        end = time.time() + timeout
        while time.time() < end:
            self._raise_if_closed()
            time.sleep(0.3)
            try:
                href = self._eval("location.href", timeout=2) or ""
                state = self._eval("document.readyState", timeout=2) or ""
                skey = self.cookie("skey")
            except (OSError, TimeoutError):
                self._raise_if_closed()
                continue
            if not href or href == before_href or state != "complete":
                continue
            if "check_sig" in href:
                continue
            if skey and (skey != before_skey or "ptlogin2" not in href):
                time.sleep(0.4)
                return

    def cookie(self, name: str) -> str:
        vals = [c.get("value") or "" for c in self.dump_cookies()
                if c.get("name") == name and c.get("value")]
        return vals[-1] if vals else ""

    def dump_cookies(self):
        return self.cdp.call("Network.getAllCookies").get("cookies") or []

    def set_cookie(self, name: str, value: str, domain: str, path: str = "/",
                   expires=None) -> None:
        params = {"name": name, "value": value, "domain": domain, "path": path or "/",
                  "secure": str(domain).endswith("qq.com")}
        if expires:
            params["expires"] = expires
        self.cdp.call("Network.setCookie", params)

    def inject(self, session) -> None:
        for c in session.session.cookies:
            if c.name not in DEVICE_COOKIES or not c.domain:
                continue
            try:
                self.set_cookie(c.name, c.value, c.domain, c.path or "/", c.expires)
            except (OSError, TimeoutError):
                log.debug("独立窗口没种上 cookie %s", c.name)

    def show_slider(self, aid: str, sid: str, uin: str, timeout: int = 180):
        self.page.update(aid=str(aid), sid=str(sid), uin=str(uin))
        self.box.clear()
        try:
            self.cdp.call("Page.bringToFront")
        except (OSError, TimeoutError):
            pass
        doc = ("<!DOCTYPE html><meta charset='utf-8'><p>请在这个窗口里完成滑动，"
               "不要换到平时的浏览器。</p>"
               "<script src='https://ssl.captcha.qq.com/TCaptcha.js'></script>")
        try:
            self._eval("document.open();document.write(%s);document.close();'ok'"
                       % json.dumps(doc))
            end = time.time() + 8
            ready = False
            while time.time() < end:
                self._raise_if_closed()
                ready = bool(self._eval("!!window.TencentCaptcha", timeout=2))
                if ready:
                    break
                time.sleep(0.25)
            if ready:
                expr = (
                    "window.__cap='';"
                    "var cap=new TencentCaptcha(%s,function(res){window.__cap=JSON.stringify(res||{});},"
                    "{sid:%s,uin:%s,type:'popup',enableAged:true});cap.show();'ok'"
                ) % (json.dumps(str(aid)), json.dumps(str(sid)), json.dumps(str(uin)))
                self._eval(expr)
                raw = ""
                end = time.time() + timeout
                while time.time() < end and not raw:
                    self._raise_if_closed()
                    raw = self._eval("window.__cap", timeout=2) or ""
                    if not raw:
                        time.sleep(0.25)
                try:
                    res = json.loads(raw) if raw else {}
                except (TypeError, json.JSONDecodeError):
                    res = {}
                ticket = str(res.get("ticket") or "")
                randstr = str(res.get("randstr") or res.get("randStr") or "")
                if res.get("ret") == 0 and ticket and randstr:
                    return ticket, randstr
                if res.get("ret") == 2:
                    return None
        except (OSError, TimeoutError) as exc:
            if self.gone() or _WINDOW_CLOSED in str(exc):
                raise OSError(_WINDOW_CLOSED) from exc
            log.debug("登录页里打开滑块失败，改走独立页: %s", exc)
        self._raise_if_closed()
        self.box.clear()
        port = self.httpd.server_address[1]
        self.cdp.call("Page.navigate", {"url": f"http://127.0.0.1:{port}/slide"}, timeout=15)
        end = time.time() + timeout
        while time.time() < end and not self.box.get("ticket"):
            self._raise_if_closed()
            time.sleep(0.2)
        if self.box.get("ticket") and self.box.get("randstr"):
            return self.box["ticket"], self.box["randstr"]
        return None


def _print_qr_ascii(png_path: str) -> None:
    """尽力把二维码渲染成终端字符画（依赖 Pillow，失败则静默跳过）。"""
    try:
        from PIL import Image
    except ImportError:
        return
    try:
        img = Image.open(png_path).convert("L")
        w, h = img.size
        px = img.load()
        binary = [[1 if px[x, y] < 128 else 0 for x in range(w)] for y in range(h)]
        ys = [y for y in range(h) if any(binary[y])]
        xs = [x for x in range(w) if any(row[x] for row in binary)]
        if not ys or not xs:
            return
        top, bottom, left, right = ys[0], ys[-1], xs[0], xs[-1]
        # 用左上角定位图形的第一段黑色横向长度 / 7 估算模块大小
        run = 0
        for x in range(left, right + 1):
            if binary[top][x]:
                run += 1
            else:
                break
        module = max(1, run // 7)
        n = (right - left + 1 + module // 2) // module
        lines = []
        # 终端多为深色背景，反色输出（黑模块→空格）扫码成功率更高
        for r in range(n):
            y = top + r * module + module // 2
            if y > bottom:
                break
            row = ""
            for c in range(n):
                x = left + c * module + module // 2
                dark = binary[y][x] if x <= right else 0
                row += "  " if dark else "██"
            lines.append(row)
        print("\n".join(lines))
        print("(若上方二维码扫不出来，请直接打开 qrcode.png 扫码)")
    except Exception as exc:  # 渲染失败不影响主流程
        log.debug("二维码字符画渲染失败: %s", exc)


class QQSession:
    """带 cookie 持久化的 QQ 登录会话。"""

    def __init__(self, cookie_file: str = COOKIE_FILE):
        self.cookie_file = cookie_file
        self.session = requests.Session()
        self.session.headers["User-Agent"] = UA
        self._load_cookies()

    # ---------- cookie 持久化 ----------

    def _load_cookies(self) -> None:
        if not os.path.exists(self.cookie_file):
            return
        try:
            with open(self.cookie_file, encoding="utf-8") as f:
                jar = json.load(f)
            for c in jar:
                self.session.cookies.set(c["name"], c["value"],
                                         domain=c["domain"], path=c["path"],
                                         expires=c.get("expires"))
            log.debug("已从 %s 载入 %d 条 cookie", self.cookie_file, len(jar))
        except Exception as exc:
            log.warning("cookie 文件读取失败，将重新登录: %s", exc)

    def _save_cookies(self) -> None:
        # 必须保存 expires：没有它就无法判断票据何时到期，只能等请求失败才发现，
        # 也就没法在到期前静默续期。长效凭据(superkey/RK/ptcz)的有效期也靠它看。
        jar = [{"name": c.name, "value": c.value, "domain": c.domain,
                "path": c.path, "expires": c.expires}
               for c in self.session.cookies]
        os.makedirs(os.path.dirname(os.path.abspath(self.cookie_file)), exist_ok=True)
        with open(self.cookie_file, "w", encoding="utf-8") as f:
            json.dump(jar, f, ensure_ascii=False, indent=1)
        log.info("cookie 已保存到 %s", self.cookie_file)

    def ticket_status(self) -> dict:
        """返回各票据的剩余寿命，用于判断是否该续期。

        {名字: 剩余秒数或 None(会话cookie/无过期)}
        """
        now = time.time()
        out = {}
        for c in self.session.cookies:
            out[c.name] = None if not c.expires else round(c.expires - now)
        return out

    def expires_within(self, seconds: float) -> bool:
        """核心票据(skey/p_skey)是否将在 seconds 内过期。取不到过期时间时返回 False。"""
        st = self.ticket_status()
        vals = [st.get(n) for n in ("skey", "p_skey") if st.get(n) is not None]
        return bool(vals) and min(vals) <= seconds

    # ---------- 登录态 ----------

    @property
    def uin(self) -> str:
        raw = self.session.cookies.get("uin", domain=".qq.com") or \
            self.session.cookies.get("uin") or ""
        return raw.lstrip("o0")

    def _get_p_skey(self) -> str:
        for c in self.session.cookies:
            if c.name == "p_skey" and "qzone" in (c.domain or ""):
                return c.value
        return self.session.cookies.get("p_skey") or ""

    @property
    def g_tk(self) -> int:
        key = self._get_p_skey() or self.session.cookies.get("skey") or ""
        return calc_g_tk(key)

    def has_long_term_ticket(self) -> bool:
        """是否持有长效登录凭据（腾讯"快速登录/下次自动登录"用的那套）。

        skey/p_skey 约 24 小时就过期，但 superkey/RK/ptcz 是长效的 ——
        浏览器能几周不重新扫码就是靠它们换发新 skey。
        """
        names = {c.name for c in self.session.cookies}
        return bool(names & {"superkey", "RK", "ptcz"})

    def silent_renew(self) -> bool:
        """尝试用长效凭据静默换发新 skey，成功则不必重新扫码。

        走的是 ptlogin2 的快速登录(qlogin)通道：带着 superkey/RK/ptcz 请求
        xlogin 建立 login_sig，再走 pt_login 拿新票据。失败时保持原样返回 False，
        由调用方回退到扫码 —— 绝不能因为续期失败就把现有 cookie 弄坏。
        """
        if not self.has_long_term_ticket():
            log.info("没有长效凭据(superkey/RK/ptcz)，无法静默续期")
            return False

        before = {c.name: c.value for c in self.session.cookies}
        try:
            # 1) xlogin 建立 login_sig —— 快速登录的入口，会带出 pt_login_sig
            r = self.session.get(
                "https://xui.ptlogin2.qq.com/cgi-bin/xlogin",
                params={"proxy_url": "https://qzs.qq.com/qzone/v6/portal/proxy.html",
                        "daid": DAID, "hide_title_bar": "1", "low_login": "0",
                        "qlogin_auto_login": "1", "no_verifyimg": "1",
                        "link_target": "blank", "appid": PTLOGIN_APPID,
                        "style": "22", "target": "self", "s_url": GAME_URL},
                timeout=15)
            login_sig = self._cookie("pt_login_sig") or ""
            if not login_sig:
                log.info("静默续期：未取得 pt_login_sig，放弃")
                return False

            # 2) 快速登录：服务端凭 superkey/RK/ptcz 识别设备，直接下发新票据
            r = self.session.get(
                "https://ptlogin2.qq.com/pt_login",
                params={"u": self.uin, "aid": PTLOGIN_APPID, "daid": DAID,
                        "login_sig": login_sig, "u1": GAME_URL,
                        "ptlang": "2052", "pt_uistyle": "40",
                        "action": f"0-0-{int(time.time() * 1000)}"},
                headers={"Referer": "https://xui.ptlogin2.qq.com/"},
                timeout=15)
            m = re.search(r"ptuiCB\('(\d+)','\d+','([^']*)','\d+','([^']*)'", r.text)
            if m and m.group(1) == "0" and m.group(2):
                self.session.get(m.group(2), allow_redirects=True, timeout=20)
            elif m:
                log.info("静默续期被拒(code=%s): %s", m.group(1), m.group(3)[:60])
                return False
            else:
                # 2026-08-13 实测：这个端点已经不返回 ptuiCB 了，直接给一张
                # 腾讯网首页 HTML。也就是说 pt_login 这条快速登录通道没了。
                # 以前这里没有 else 分支，匹配不上就悄悄掉到最后一句
                # "静默续期未生效"，看不出到底是被拒还是接口变了。
                head = (r.text or "")[:120].replace("\n", " ")
                is_html = "<html" in r.text[:300].lower()
                log.info("静默续期：pt_login 没有返回 ptuiCB（%s）—— "
                         "腾讯这个接口已变更，不是凭据问题。返回开头：%s",
                         "是一张 HTML 页面" if is_html else "格式不认识", head)
                return False
        except requests.RequestException as exc:
            log.info("静默续期请求异常: %s", exc)
            return False

        if self.is_valid():
            self._save_cookies()
            left = self.ticket_status().get("skey")
            log.info("✅ 静默续期成功，无需扫码%s",
                     f"（新 skey 剩余 {left // 3600} 小时）" if left else "")
            return True

        # 没成功就还原，别把原来还能用的 cookie 搞坏
        for name, val in before.items():
            try:
                self.session.cookies.set(name, val)
            except Exception:
                pass
        log.info("静默续期未生效，需要重新扫码")
        return False

    def is_valid(self) -> bool:
        """访问游戏页：已登录返回 200 页面，未登录会 302 去 ptlogin。"""
        if not self.session.cookies.get("skey"):
            return False
        try:
            r = self.session.get(GAME_URL, allow_redirects=False, timeout=15)
        except requests.RequestException as exc:
            log.warning("登录态校验请求失败: %s", exc)
            return False
        if r.status_code == 200 and "ptlogin" not in r.text[:2000]:
            return True
        loc = r.headers.get("Location", "")
        log.info("登录态已失效 (status=%s, location=%s...)", r.status_code, loc[:80])
        return False

    # ---------- 扫码 / 推送登录 ----------

    def _clear_session_cookies(self, keep=()) -> None:
        """清掉会话票据，但保留 keep 里的设备/长效凭据。

        推送登录依赖 ptcz/RK/superkey 识别设备，全清就推不出去。
        """
        keep = set(keep)
        doomed = [(c.name, c.domain, c.path)
                  for c in self.session.cookies if c.name not in keep]
        for name, domain, path in doomed:
            try:
                self.session.cookies.clear(domain, path, name)
            except KeyError:
                pass

    def _cookie(self, name):
        """安全地取 cookie 值。

        同名 cookie 可能同时存在于 .qq.com 和 ptlogin2.qq.com 两个域上，
        requests 的 cookies.get() 遇到重名会直接抛 CookieConflictError。
        这里取最后一个（域更具体的通常是服务端刚下发的那个）。
        """
        vals = [c.value for c in self.session.cookies if c.name == name]
        return vals[-1] if vals else None

    def _set_cookie(self, name, value, domain=PTLOGIN_DOMAIN):
        """把某个 cookie 设成指定值，先清掉所有同名的。

        不先清就可能留下两个同名 cookie（不同域），之后 _cookie() 取到哪个
        全看顺序。
        """
        for c in [c for c in self.session.cookies if c.name == name]:
            try:
                self.session.cookies.clear(c.domain, c.path, c.name)
            except KeyError:
                pass
        self.session.cookies.set(name, value, domain=domain, path="/")

    def import_device_cookies(self, text: str) -> list:
        """从浏览器搬一份设备记录过来 —— 推送登录的一次性引导。

        `pt_fetch_dev_uin` 只能给已有的 dev_mid_sig 续期，签发不出第一个，
        所以这一份得从一个登录过 QQ 网页的浏览器里取。取法：在
        ptlogin2.qq.com 的页面上打开开发者工具，把 Cookie 复制出来。

        三种写法都认：
          - 开发者工具里复制的 Cookie 头：``name=value; name=value``
          - ``{"name": "value", ...}`` 的 JSON
          - ``[{"name": ..., "value": ...}, ...]`` 的 JSON（各类导出插件的格式）

        只取 DEVICE_BOOTSTRAP 里那几个，其余一律忽略。返回搬进来的名字列表。
        """
        text = (text or "").strip()
        pairs = {}
        if text.startswith(("{", "[")):
            data = json.loads(text)
            if isinstance(data, dict):
                pairs = {k: v for k, v in data.items() if isinstance(v, str)}
            else:
                pairs = {c["name"]: c["value"] for c in data
                         if isinstance(c, dict) and "name" in c}
        else:
            text = re.sub(r"^\s*Cookie\s*:\s*", "", text, flags=re.I)
            for kv in text.split(";"):
                k, sep, v = kv.strip().partition("=")
                if sep and k:
                    pairs[k.strip()] = v.strip()

        got = []
        for name in DEVICE_BOOTSTRAP:
            if pairs.get(name):
                self._set_cookie(name, pairs[name])
                got.append(name)
        if got:
            self._save_cookies()
        return got

    def device_status(self) -> str:
        """一句话说明设备记录当前处于什么状态，给 --check 和报错文案用。"""
        if self._cookie("dev_mid_sig"):
            return ("有设备签名 dev_mid_sig，可以试推送。"
                    "（签名有时效，过期时推送回 ec=315，得重搬一份新的）")
        if self._cookie("pt_guid_sig"):
            return ("没有设备记录：只有 pt_guid_sig，而 pt_fetch_dev_uin 只能给"
                    "已有的 dev_mid_sig 续期，签发不出第一个")
        return "没有任何设备凭据"

    def _push_login(self, push_uin):
        """把**已建立的二维码会话**推送到手机 QQ。返回 (是否成功, 说明)。

        关键在顺序（2026-08-13 抓浏览器抓出来的）：推送不是"另一种取二维码"，
        而是挂在已有 qrsig 会话上的一个动作 —— 页面上先加载二维码，
        点头像才发这一条。所以必须**先走一次普通 ptqrshow 拿到 qrsig**，
        再发这条；直接上来就发，服务端回 ec=313「提交参数错误」。

        另外它不带 e/l/s/d/v 那几个图片参数（本来就不是要图），但要带 u1。
        """
        if not self._cookie("qrsig"):
            return False, "还没有二维码会话（qrsig），推送无从挂载"

        # 给设备签名续期。注意这个接口**只能续期，不能签发第一个**：
        # 实测只带 pt_guid_sig（哪怕是浏览器里正在用的那个）一律 errcode 22027；
        # 带上 dev_mid_sig 才回 22028 并下发新的。所以没有 dev_mid_sig 时
        # 这一步是白跑，直接跳过，把话说清楚让用户去引导一次。
        guid_sig = self._cookie("pt_guid_sig")
        if guid_sig and self._cookie("dev_mid_sig"):
            try:
                # pt_guid_token = hash33(pt_guid_sig)，和 ptqrtoken = hash33(qrsig)
                # 是同一个套路（拿抓包里的一对逐位验证过）。
                r = self.session.get(
                    "https://ssl.ptlogin2.qq.com/pt_fetch_dev_uin",
                    params={"r": str(random.random()),
                            "pt_guid_token": str(hash33(guid_sig))},
                    headers={"Referer": "https://xui.ptlogin2.qq.com/"},
                    timeout=15)
                log.debug("pt_fetch_dev_uin: %s", (r.text or "")[:120])
            except requests.RequestException as exc:
                log.debug("pt_fetch_dev_uin 失败(忽略): %s", exc)
        if not self._cookie("dev_mid_sig"):
            return False, ("没有设备记录 dev_mid_sig，服务端认不出这台设备。"
                           "pt_fetch_dev_uin 只能给已有的续期、签发不出第一个，"
                           "所以要先从一个登录过 QQ 网页的浏览器搬一次："
                           "main.py --import-device <文件>（详见 README）")
        try:
            r = self.session.get(
                "https://ssl.ptlogin2.qq.com/ptqrshow",
                params={"qr_push": "1", "qr_push_uin": str(push_uin),
                        "type": "1", "appid": PTLOGIN_APPID,
                        "t": str(random.random()), "ptlang": "2052",
                        "u1": GAME_URL, "daid": DAID, "pt_3rd_aid": "0"},
                headers={"Referer": "https://xui.ptlogin2.qq.com/"},
                timeout=15)
        except requests.RequestException as exc:
            return False, f"请求异常 {exc}"
        text = (r.text or "")[:300]
        m = re.search(r'"ec"\s*:\s*(\d+)', text)
        ec = m.group(1) if m else ""
        if ec == "0":
            return True, ""
        em = re.search(r'"em"\s*:\s*"([^"]*)"', text)
        why = f"ec={ec} {em.group(1) if em else ''}".strip() or text.strip()
        if ec == "313":
            why += ("（多半是缺 dev_mid_sig 设备绑定；"
                    "见 README「关于免扫码」）")
        return False, why

    def _ptqrshow(self, push_uin=None):
        """请求二维码。返回 (response, 失败原因)；成功时原因为空串。

        腾讯拒绝时不返回 PNG，而是回一段 JSONP 或空内容；早先代码把它当成图片
        写进 qrcode.png，只报一句"未获取到 qrsig"，看不出真正原因。
        """
        params = {
            "appid": PTLOGIN_APPID, "e": "2", "l": "M", "s": "3", "d": "72",
            "v": "4", "t": str(random.random()), "daid": DAID, "pt_3rd_aid": "0",
            "ptlang": "2052", "u1": GAME_URL,
        }
        try:
            r = self.session.get("https://xui.ptlogin2.qq.com/ssl/ptqrshow",
                                 params=params,
                                 headers={"Referer": "https://xui.ptlogin2.qq.com/"},
                                 timeout=15)
        except requests.RequestException as exc:
            return None, f"请求异常 {exc}"

        body = r.content or b""
        if body[:8] == b"\x89PNG\r\n\x1a\n":
            if not self._cookie("qrsig"):
                return None, "返回了图但没有 qrsig，无法轮询"
            return r, ""

        # 失败：把 ec 码解出来，好判断是"参数变了"还是别的
        text = body[:300].decode("utf-8", "replace").strip()
        m = re.search(r'"ec"\s*:\s*(\d+)', text)
        ec = m.group(1) if m else ""
        if r.status_code == 403:
            return None, "网关直接 403（参数组合不被接受）"
        if ec:
            m2 = re.search(r'"em"\s*:\s*"([^"]*)"', text)
            return None, f"ec={ec} {m2.group(1) if m2 else ''}".strip()
        return None, (f"HTTP {r.status_code}，{len(body)} 字节，"
                      f"content-type={r.headers.get('Content-Type', '?')}")

    def password_login(self, uin: str, password: str, low_login: bool = True,
                       ticket: str = "", randstr: str = "", on_status=None) -> dict:
        """ptlogin2 账号密码登录。要滑块时在独立窗口里划，并在同一窗口提交密码。

        low_login 为真时带 low_login_enable=1、low_login_hour=720。
        划过并登录成功后，这套窗口的设备票据会留下，下次密码登录就不必再扫码。
        """
        uin = str(uin or "").strip()
        if not uin.isdigit() or not 5 <= len(uin) <= 12:
            return {"ok": False, "captcha": False, "code": "", "msg": "QQ 号要是 5 到 12 位数字"}
        if not password:
            return {"ok": False, "captcha": False, "code": "", "msg": "密码是空的"}

        log.info("密码登录 uin=%s，打开独立窗口", uin)
        return self._finish_in_window(uin, password, low_login, on_status)

    def _take_cookies(self, items) -> None:
        for c in items or []:
            name = c.get("name") or ""
            value = c.get("value")
            domain = c.get("domain") or ""
            path = c.get("path") or "/"
            if not name or value is None or not domain:
                continue
            try:
                self.session.cookies.clear(domain, path, name)
            except KeyError:
                pass
            exp = c.get("expires")
            kw = {}
            if isinstance(exp, (int, float)) and exp > 0:
                kw["expires"] = exp
            self.session.cookies.set(name, value, domain=domain, path=path, **kw)

    def _finish_in_window(self, uin: str, password: str, low_login: bool, on_status) -> dict:
        """滑块和密码提交共用一个独立窗口，避免票据记在日常浏览器上。"""
        win = _LoginWindow()
        try:
            if on_status:
                on_status("正在打开独立登录窗口…")
            err = win.start()
            if err:
                return {"ok": False, "captcha": False, "code": "", "msg": err}
            win.inject(self)
            guid_before = win.cookie("pt_guid_sig") or self._cookie("pt_guid_sig") or ""
            if not win.open_until(_xlogin_url(low_login), "ptlogin2.qq.com", 20):
                msg = _WINDOW_CLOSED if win.gone() else "独立窗口没打开登录页"
                return {"ok": False, "captcha": False, "code": "", "msg": msg}
            if guid_before and win.cookie("pt_guid_sig") != guid_before:
                win.set_cookie("pt_guid_sig", guid_before, PTLOGIN_DOMAIN)
            login_sig = win.cookie("pt_login_sig") or ""
            text = win.goto(_check_url(uin, login_sig), "ptui_checkVC", 20)
            quoted = re.findall(r"'((?:\\.|[^'\\])*)'", text or "")
            if len(quoted) < 7:
                head = (text or "")[:120].replace("\n", " ")
                return {"ok": False, "captcha": False, "code": "",
                        "msg": f"独立窗口里的 check 无法解析：{head}"}
            check_ret, vcode, salt_js, verifysession, randsalt, ptdrvs, sid = quoted[:7]
            if check_ret == "2":
                return {"ok": False, "captcha": False, "code": "2", "msg": "QQ 号不对"}
            if check_ret not in ("0", "1", "3"):
                return {"ok": False, "captcha": False, "code": check_ret,
                        "msg": f"check 被拒（code={check_ret}）"}
            salt = _js_string_bytes(salt_js)
            if len(salt) < 8:
                salt = salt.ljust(8, b"\x00")
            ticket, randstr = "", ""
            # check 返回 1 是要滑块，不是手机确认。推送认不出没签过 dev_mid_sig 的窗口。
            if check_ret == "1":
                if on_status:
                    on_status("请在独立窗口里完成滑动")
                log.info("密码登录要滑块 uin=%s", uin)
                got = win.show_slider(PTLOGIN_APPID, sid or "", uin)
                if not got:
                    msg = _WINDOW_CLOSED if win.gone() else "滑动没有完成"
                    return {"ok": False, "captcha": False, "code": "1", "msg": msg}
                ticket, randstr = got
            pending = {
                "uin": uin,
                "salt_hex": salt.hex(),
                "vcode": vcode or "!AAA",
                "verifysession": verifysession or "",
                "randsalt": randsalt or "2",
                "ptdrvs": ptdrvs or "",
                "sid": sid or "",
                "login_sig": login_sig,
                "at": time.time(),
            }
            return self._pt_password_submit(
                password, pending, low_login=low_login, ticket=ticket, randstr=randstr,
                win=win, on_status=on_status)
        except (OSError, TimeoutError) as exc:
            log.info("独立窗口登录中断: %s", exc)
            msg = _WINDOW_CLOSED if _WINDOW_CLOSED in str(exc) or win.gone() else f"独立窗口登录中断: {exc}"
            return {"ok": False, "captcha": False, "code": "", "msg": msg}
        finally:
            win.close()

    def _pt_password_submit(self, password: str, pending: dict, low_login: bool = True,
                            ticket: str = "", randstr: str = "", win=None, on_status=None) -> dict:
        uin = pending.get("uin") or ""
        salt = bytes.fromhex(pending.get("salt_hex") or "")
        if len(salt) < 8:
            salt = salt.ljust(8, b"\x00")
        if ticket and randstr:
            vcode = randstr
            session = ticket
            pt_vcode = "1"
        else:
            vcode = pending.get("vcode") or "!AAA"
            session = pending.get("verifysession") or ""
            pt_vcode = "0"
        try:
            enc = encrypt_pt_password(password, salt, vcode)
        except (ValueError, TypeError) as exc:
            return {"ok": False, "captcha": False, "code": "",
                    "msg": f"密码加密失败: {exc}"}
        params = {
            "u": uin, "verifycode": vcode, "pt_vcode_v1": pt_vcode,
            "pt_verifysession_v1": session, "p": enc,
            "pt_randsalt": pending.get("randsalt") or "2",
            "u1": GAME_URL, "ptredirect": "0", "h": "1", "t": "1", "g": "1",
            "from_ui": "1", "ptlang": "2052",
            "action": f"2-0-{int(time.time() * 1000)}",
            "js_ver": "26071711", "js_type": "1",
            "login_sig": pending.get("login_sig") or "",
            "pt_uistyle": "22",
            "aid": PTLOGIN_APPID, "daid": DAID,
        }
        if pending.get("ptdrvs"):
            params["ptdrvs"] = pending["ptdrvs"]
        if pending.get("sid"):
            params["sid"] = pending["sid"]
        if ticket:
            params["ticket"] = ticket
            params["rand_str"] = randstr
        if low_login:
            params["low_login_enable"] = "1"
            params["low_login_hour"] = "720"
        try:
            if win:
                text = win.goto(
                    "https://ssl.ptlogin2.qq.com/login?" + urllib.parse.urlencode(params),
                    "ptuiCB", 20)
            else:
                r = self.session.get(
                    "https://ssl.ptlogin2.qq.com/login",
                    params=params,
                    headers={"Referer": "https://xui.ptlogin2.qq.com/"},
                    timeout=20)
                text = r.text or ""
        except (requests.RequestException, OSError, TimeoutError) as exc:
            return {"ok": False, "captcha": False, "code": "", "msg": f"login 失败: {exc}"}

        m = re.search(r"ptuiCB\('(\d+)','\d+','([^']*)','\d+','([^']*)'", text or "")
        if not m:
            head = (text or "")[:120].replace("\n", " ")
            return {"ok": False, "captcha": False, "code": "",
                    "msg": f"login 响应无法解析：{head}"}
        code, url, msg = m.group(1), m.group(2), m.group(3)
        if code == "10009":
            if win:
                try:
                    self._take_cookies(
                        [c for c in win.dump_cookies() if c.get("name") in DEVICE_COOKIES])
                except (OSError, TimeoutError):
                    pass
                win.close()
            log.info("密码登录要手机验证 uin=%s %s，改推送到手机", uin, (msg or "")[:40])
            return self._confirm_on_phone(uin, on_status)
        if code != "0":
            self._pwd_pending = None
            log.info("密码登录被拒 uin=%s code=%s %s", uin, code, (msg or "")[:60])
            if code == "10009":
                text = f"登录保护要求验证密保手机 {msg}".strip()
            else:
                text = msg or f"登录失败 code={code}"
            if any(k in text for k in ("安全风险", "常用设备", "更换网络")):
                text += "。密码是从这台机器提交的，机房 IP 常被腾讯判成异常环境。扫码登录不受这条限制。"
            return {"ok": False, "captcha": False, "code": code, "msg": text}
        self._pwd_pending = None
        try:
            if win:
                win.follow(url, 20)
                self._take_cookies(win.dump_cookies())
            else:
                self.session.get(url, allow_redirects=True, timeout=20)
        except (requests.RequestException, OSError, TimeoutError) as exc:
            return {"ok": False, "captcha": False, "code": "0",
                    "msg": f"check_sig 失败: {exc}"}
        self._save_cookies()
        if self.is_valid():
            log.info("密码登录完成，uin=%s", self.uin)
            return {"ok": True, "captcha": False, "code": "0", "msg": msg or "登录成功"}
        log.error("密码登录 check_sig 后游戏页仍不认 uin=%s", uin)
        return {"ok": False, "captcha": False, "code": "0",
                "msg": "check_sig 后游戏页仍不认这张票"}

    def _confirm_on_phone(self, uin: str, on_status=None) -> dict:
        """验证改走推送：手机 QQ 收到确认，点一下即可，不用在电脑上滑块或填密保。"""
        if on_status:
            on_status(f"请在手机 QQ 上确认登录 {uin}")
        log.info("验证改推送到手机 QQ %s", uin)
        ok = self.qr_login(push_uin=uin, push_only=True)
        if ok:
            return {"ok": True, "captcha": False, "code": "0", "msg": "手机已确认"}
        note = getattr(self, "_login_note", "") or "手机没有确认登录"
        return {"ok": False, "captcha": False, "code": "", "msg": note}

    def qr_login(self, timeout_sec: int = 180, on_qr=None, push_uin=None,
                 push_only: bool = False) -> bool:
        """扫码登录。on_qr(qrcode_path) 在二维码生成后回调（用于推送到手机等）。

        push_uin 非空时启用**推送登录**：不用扫码，腾讯直接往该 QQ 号的手机客户端
        推一条登录确认，用户点"确认登录"即可。这解决了"把二维码图片存到本地、
        用同一台手机的相册扫码"被拒（提示"限制本地扫码登录"）的问题 ——
        腾讯的防钓鱼策略要求二维码显示在**另一块屏幕**上，而推送登录没有这个限制。

        参数取自真实客户端抓包：ptqrshow?qr_push=1&qr_push_uin=<uin>&type=1
        """
        s = self.session
        # uin 要在清 cookie 之前取，否则就拿不到了
        if push_uin is None:
            push_uin = self.uin or None
        # 试用包写死了 QQ，推送确认只发给这个号，不跟配置或旧 cookie 走。
        if lockqq.bind_uin():
            push_uin = lockqq.bind_uin()

        # 清会话票据但**保住设备凭据** —— 推送靠 dev_mid_sig 之类识别"推给哪台设备"，
        # 全清了就只能回 ec=313。
        self._clear_session_cookies(keep=DEVICE_COOKIES)
        # 浏览器进登录页第一件事就是 xlogin，它建立 pt_login_sig 上下文。
        # 但它同时会**无条件重新签发 pt_guid_sig**（实测：本来就有一个也照换），
        # 而 pt_guid_sig 是和 dev_mid_sig 配对的，被换掉就对不上了。
        # 所以先存后还。
        guid_before = self._cookie("pt_guid_sig")
        try:
            s.get("https://xui.ptlogin2.qq.com/cgi-bin/xlogin",
                  params={"daid": DAID, "appid": PTLOGIN_APPID,
                          "hide_title_bar": "1", "low_login": "0",
                          "qlogin_auto_login": "1", "no_verifyimg": "1",
                          "link_target": "blank", "style": "22",
                          "target": "self", "s_url": GAME_URL},
                  timeout=15)
        except requests.RequestException as exc:
            log.debug("xlogin 预热失败(忽略): %s", exc)
        if guid_before and self._cookie("pt_guid_sig") != guid_before:
            self._set_cookie("pt_guid_sig", guid_before)
            log.debug("xlogin 换掉了 pt_guid_sig，已还原成与设备记录配对的那个")

        # 先拿二维码：不管走不走推送都要这一步 —— 推送是挂在这个会话上的
        r, why = self._ptqrshow()
        if r is None:
            log.error("二维码请求失败，无法登录：%s", why)
            return False

        pushed = False
        if push_uin:
            pushed, why = self._push_login(push_uin)
            if not pushed:
                self._login_note = why
                if push_only:
                    log.info("推送登录没成（%s）", why)
                    return False
                log.info("推送登录没成（%s），本次用扫码", why)

        os.makedirs(os.path.dirname(os.path.abspath(QRCODE_FILE)), exist_ok=True)
        with open(QRCODE_FILE, "wb") as f:
            f.write(r.content)
        qrsig = self._cookie("qrsig")

        if pushed:
            log.info("已向 QQ %s 推送登录确认 —— 打开手机QQ点「确认登录」即可，"
                     "不需要扫码（扫码图仍保存在 %s 作为备用）", push_uin, QRCODE_FILE)
        else:
            log.info("请用手机 QQ 扫码登录（二维码已保存: %s）", QRCODE_FILE)
        # 只在"本机交互式使用且没有别的送达方式"时才弹图片查看器。
        # 有 on_qr（PushPlus 推送）时再弹窗没意义；推送登录更是压根不需要看图。
        if os.name == "nt" and on_qr is None and not pushed:
            try:
                os.startfile(QRCODE_FILE)
            except OSError:
                pass
        if not pushed:
            _print_qr_ascii(QRCODE_FILE)
        if on_qr:
            try:
                # 带上 pushed，让调用方的文案跟实际走的路径一致
                # （推送失败回退到扫码时，不能还提示"点确认登录"）
                on_qr(QRCODE_FILE, pushed)
            except Exception as exc:
                log.warning("二维码推送回调失败: %s", exc)

        ptqrtoken = hash33(qrsig)
        # 轮询参数照浏览器来：带上 xlogin 拿到的 login_sig，以及 has_onekey=1
        # （"一键/推送登录"标记，推送确认要靠它认）。早先 login_sig 传空串、
        # 也没有 has_onekey，扫码能过但推送这条路认不出来。
        login_sig = self._cookie("pt_login_sig") or ""
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            r = s.get("https://xui.ptlogin2.qq.com/ssl/ptqrlogin", params={
                "u1": GAME_URL, "ptqrtoken": ptqrtoken, "ptredirect": "0",
                "h": "1", "t": "1", "g": "1", "from_ui": "1", "ptlang": "2052",
                "action": f"0-0-{int(time.time() * 1000)}",
                "js_ver": "26071711", "js_type": "1", "login_sig": login_sig,
                "pt_uistyle": "40", "aid": PTLOGIN_APPID, "daid": DAID,
                "has_onekey": "1",
            }, headers={"Referer": "https://xui.ptlogin2.qq.com/"},
                timeout=15)
            m = re.search(r"ptuiCB\('(\d+)','\d+','([^']*)','\d+','([^']*)'", r.text)
            if not m:
                log.warning("轮询响应无法解析: %s", r.text[:200])
                time.sleep(3)
                continue
            code, url, msg = m.group(1), m.group(2), m.group(3)
            if code == "0":
                log.info("扫码确认成功: %s", msg)
                s.get(url, allow_redirects=True, timeout=20)  # check_sig，种登录 cookie
                self._save_cookies()
                if self.is_valid():
                    log.info("登录完成，uin=%s", self.uin)
                    self._save_cookies()  # 校验过程可能刷新 cookie，再存一次
                    return True
                log.error("check_sig 后登录态仍无效")
                return False
            if code == "65":
                self._login_note = "手机确认已失效，请重新登录"
                log.error("二维码已失效，请重新运行")
                return False
            if code == "67":
                log.info("已扫码，请在手机上确认…")
            time.sleep(3)
        self._login_note = "手机没有确认登录"
        log.error("扫码超时（%d 秒）", timeout_sec)
        return False

    def ensure_login(self, on_qr=None, push_uin=None) -> bool:
        """保证登录可用。顺序：现成 cookie → 长效凭据静默续期 → 推送/扫码登录。

        需要人工介入的那步是最后手段 —— 守护进程要无人值守地跑。
        push_uin 见 qr_login()：给了就用推送登录，免去扫码。
        """
        if self.is_valid():
            left = self.ticket_status().get("skey")
            log.info("cookie 有效，uin=%s%s", self.uin,
                     f"（skey 剩余约 %.1f 小时）" % (left / 3600) if left else "")
            # 快到期就提前续，别等失效了才补救
            if self.expires_within(6 * 3600) and self.has_long_term_ticket():
                log.info("skey 即将到期，提前静默续期…")
                self.silent_renew()
            return True
        if self.silent_renew():
            return True
        return self.qr_login(on_qr=on_qr, push_uin=push_uin)
