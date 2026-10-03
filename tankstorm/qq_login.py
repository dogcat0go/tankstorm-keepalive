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

不涉及账号密码 —— 只用扫码，服务器上把 qrcode.png 取下来扫或直接看终端字符画。

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

import json
import os
import random
import re
import shutil
import struct
import time
import zlib

import requests

from . import GAME_URL
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


def describe_tickets(cookies) -> list:
    """把 cookie 过期时间列出来，不含值。密码登录常出现 expires 为空的会话 cookie。"""
    now = time.time()
    rows = []
    for c in cookies:
        if isinstance(c, dict):
            name = str(c.get("name") or "")
            domain = str(c.get("domain") or "")
            path = str(c.get("path") or "/")
            exp = c.get("expires")
        else:
            name = str(getattr(c, "name", "") or "")
            domain = str(getattr(c, "domain", "") or "")
            path = str(getattr(c, "path", "/") or "/")
            exp = getattr(c, "expires", None)
        if not name:
            continue
        left = None
        if exp not in (None, "", 0):
            try:
                exp = int(float(exp))
                left = round(exp - now)
            except (TypeError, ValueError):
                exp = None
        else:
            exp = None
        rows.append({
            "name": name,
            "domain": domain,
            "path": path or "/",
            "expires": exp,
            "left": left,
            "session": exp is None,
        })
    order = ("skey", "p_skey", "superkey", "supertoken", "superuin",
             "RK", "ptcz", "uin", "p_uin", "pt4_token")
    rank = {name: i for i, name in enumerate(order)}
    rows.sort(key=lambda row: (rank.get(row["name"], 100), row["name"], row["domain"]))
    return rows


def cookie_ticket_report(cookies, uin: str = "") -> dict:
    rows = describe_tickets(cookies)

    def left_of(*names):
        vals = [row["left"] for row in rows
                if row["name"] in names and row["left"] is not None]
        return min(vals) if vals else None

    if not uin:
        for item in cookies:
            if isinstance(item, dict):
                if item.get("name") == "uin" and item.get("value"):
                    uin = str(item["value"]).lstrip("o0")
                    break
            elif getattr(item, "name", "") == "uin" and getattr(item, "value", ""):
                uin = str(item.value).lstrip("o0")
                break
    names = {row["name"] for row in rows}
    return {
        "uin": uin,
        "long_term": bool(names & {"superkey", "RK", "ptcz"}),
        "skey_left": left_of("skey"),
        "p_skey_left": left_of("p_skey"),
        "tickets": rows,
        "missing": False,
    }


def ticket_report_file(path: str) -> dict:
    """读已保存的 cookie 文件，只返回过期时间。文件不在或坏了时 missing=True。"""
    empty = {"uin": "", "long_term": False, "skey_left": None,
             "p_skey_left": None, "tickets": [], "missing": True}
    if not path or not os.path.isfile(path):
        return empty
    try:
        with open(path, encoding="utf-8") as f:
            jar = json.load(f)
    except (OSError, json.JSONDecodeError, UnicodeError):
        return empty
    if not isinstance(jar, list):
        return empty
    report = cookie_ticket_report(jar)
    report["missing"] = False
    return report


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def _png_dark(data: bytes) -> list[list[int]]:
    """非隔行 PNG 解成 0/1 矩阵（1 = 深色）。只用标准库，服务器不必装 Pillow。"""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("不是 PNG")
    pos = 8
    width = height = depth = color = 0
    palette = None
    trns = b""
    idat = []
    while pos + 8 <= len(data):
        ln = struct.unpack(">I", data[pos:pos + 4])[0]
        if pos + 12 + ln > len(data):
            raise ValueError("PNG 截断")
        kind = data[pos + 4:pos + 8]
        chunk = data[pos + 8:pos + 8 + ln]
        pos += 12 + ln
        if kind == b"IHDR":
            width, height, depth, color, comp, filt, inter = struct.unpack(
                ">IIBBBBB", chunk)
            if comp or filt or inter or depth not in (1, 2, 4, 8):
                raise ValueError(f"不支持的 PNG（位深 {depth}）")
        elif kind == b"PLTE":
            palette = [tuple(chunk[i:i + 3]) for i in range(0, len(chunk), 3)]
        elif kind == b"tRNS":
            trns = chunk
        elif kind == b"IDAT":
            idat.append(chunk)
        elif kind == b"IEND":
            break
    try:
        channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color]
    except KeyError:
        raise ValueError(f"不支持的颜色类型 {color}") from None
    if color == 3 and not palette:
        raise ValueError("调色板 PNG 缺少 PLTE")
    bpp = max(1, channels * depth // 8)
    stride = (width * channels * depth + 7) // 8
    raw = zlib.decompress(b"".join(idat))
    rows: list[list[int]] = []
    i, prev = 0, bytearray(stride)
    mask = (1 << depth) - 1
    half = mask / 2
    for _ in range(height):
        ft = raw[i]
        row = bytearray(raw[i + 1:i + 1 + stride])
        i += 1 + stride
        out = bytearray(stride)
        for x, v in enumerate(row):
            a = out[x - bpp] if x >= bpp else 0
            b, c = prev[x], prev[x - bpp] if x >= bpp else 0
            if ft == 1:
                v += a
            elif ft == 2:
                v += b
            elif ft == 3:
                v += (a + b) // 2
            elif ft == 4:
                v += _paeth(a, b, c)
            elif ft != 0:
                raise ValueError(f"未知滤波器 {ft}")
            out[x] = v & 255
        prev = out
        bits, bit = [], 0
        for _px in range(width):
            px = []
            for _ch in range(channels):
                px.append((out[bit // 8] >> (8 - depth - bit % 8)) & mask)
                bit += depth
            if color == 3:
                idx = px[0]
                if idx < len(trns) and trns[idx] == 0:
                    bits.append(0)
                    continue
                r, g, b = palette[idx]
                bits.append(1 if r * 30 + g * 59 + b * 11 < 12800 else 0)
            elif color in (4, 6) and px[-1] <= half:
                bits.append(0)
            else:
                vals = px[:-1] if color in (4, 6) else px
                bits.append(1 if sum(vals) / len(vals) < half else 0)
        rows.append(bits)
    return rows


def _print_qr_ascii(png_path: str) -> None:
    """把二维码打进日志。失败时说明原因，不再静默跳过。

    旧实现依赖 Pillow，没装就直接 return。服务器上常见只装了 requests，
    于是只剩「二维码已保存」一句，终端里没有码。这里用标准库解 PNG。
    黑白用 ANSI 背景色写死，不跟终端主题走；白模块仍带 ██，
    纯文本日志里也能看出形状。四周补白边，方便手机对着屏幕扫。
    """
    try:
        with open(png_path, "rb") as f:
            bitmap = _png_dark(f.read())
    except Exception as exc:
        log.warning("终端二维码渲染失败: %s。请打开 %s 扫码", exc, png_path)
        return
    height = len(bitmap)
    width = len(bitmap[0]) if bitmap else 0
    ys = [y for y in range(height) if any(bitmap[y])]
    xs = [x for x in range(width) if any(bitmap[y][x] for y in range(height))]
    if not ys or not xs:
        log.warning("二维码是空白图，请打开 %s 扫码", png_path)
        return
    top, bottom, left, right = ys[0], ys[-1], xs[0], xs[-1]
    run = 0
    for x in range(left, right + 1):
        if bitmap[top][x]:
            run += 1
        else:
            break
    module = max(1, run // 7)
    n = (right - left + 1 + module // 2) // module
    cells = []
    for r in range(n):
        y = top + r * module + module // 2
        if y > bottom:
            break
        row_cells = []
        for c in range(n):
            x = left + c * module + module // 2
            row_cells.append(1 if x <= right and bitmap[y][x] else 0)
        cells.append(row_cells)
    if not cells:
        log.warning("二维码无法排成字符画，请打开 %s 扫码", png_path)
        return
    cols = shutil.get_terminal_size((120, 24)).columns
    quiet = 4
    while quiet and (len(cells[0]) + 2 * quiet) * 2 > cols - 1:
        quiet -= 1
    # 背景色固定黑/白。██ 留给不解释转义序列的查看器，形状还在。
    visible = (len(cells[0]) + 2 * quiet) * 2
    if visible > cols:
        log.warning("终端只有 %d 列，二维码大约 %d 列，换行之后扫不中。"
                    "请把窗口拉宽，或打开 %s", cols, visible, png_path)
    dark_cell, light_cell = "\033[40m  \033[0m", "\033[97;107m██\033[0m"
    pad = light_cell * quiet
    blank = pad + light_cell * len(cells[0]) + pad
    body = "\n".join(
        [blank] * quiet
        + [pad + "".join(dark_cell if d else light_cell for d in row) + pad
           for row in cells]
        + [blank] * quiet
    )
    # 消息以换行开头，后面每一行都没有时间戳前缀，扫码时不会被日志头切开。
    log.info("\n%s\n（若扫不出来，请把终端拉宽后重试，或打开 %s）", body, png_path)


class QQSession:
    """带 cookie 持久化的 QQ 登录会话。"""

    def __init__(self, cookie_file: str = COOKIE_FILE, qrcode_file: str = None):
        self.cookie_file = cookie_file
        self.qrcode_file = qrcode_file or QRCODE_FILE
        self.use_napcat = True
        self.attack_account = False
        self.blocked_uins = set()
        self.session = requests.Session()
        self.session.headers["User-Agent"] = UA
        self._qr_poll = None
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
        folder = os.path.dirname(self.cookie_file)
        if folder:
            os.makedirs(folder, exist_ok=True)
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

    def ticket_report(self) -> dict:
        """给测试页看的票据摘要。不含 cookie 值。"""
        return cookie_ticket_report(self.session.cookies, self.uin)

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

    def adopt_napcat(self, config: dict) -> bool:
        """向本机已登录的 NapCat 要空间票据。游戏页认这张票就不用扫码。"""
        if not self.use_napcat:
            return False
        if self.attack_account:
            log.error("攻打号不向 NapCat 要票据")
            return False
        inner = ((config.get("登录") or {}).get("内部QQ") or {})
        api = str(inner.get("地址") or "").strip().rstrip("/")
        token = str(inner.get("Token") or "").strip()
        if not api:
            note = config.get("通知") or {}
            api = str(note.get("qq_api") or "").strip().rstrip("/")
            token = str(note.get("qq_token") or "").strip()
        if not api:
            return False
        headers = {}
        if token:
            headers["Authorization"] = "Bearer " + token
        try:
            r = self.session.post(
                api + "/get_cookies",
                json={"domain": "game.qzone.qq.com"},
                headers=headers, timeout=10)
            data = r.json()
        except Exception as exc:
            log.info("向 NapCat 要票据失败: %s", exc)
            return False
        if data.get("status") != "ok" and data.get("retcode") != 0:
            log.info("NapCat 没有交出票据: %s", data.get("message") or data.get("wording") or data.get("status"))
            return False
        raw = ((data.get("data") or {}).get("cookies") or "")
        pairs = {}
        for part in raw.split(";"):
            key, sep, val = part.strip().partition("=")
            if sep and key and key != "domain_id":
                pairs[key] = val
        if not pairs.get("skey") or not pairs.get("p_skey"):
            log.info("NapCat 票据里没有 skey 或 p_skey")
            return False
        qzone_names = {"p_skey", "p_uin", "pt4_token"}
        for key, val in pairs.items():
            domain = ".qzone.qq.com" if key in qzone_names else ".qq.com"
            self._set_cookie(key, val, domain=domain)
        self._save_cookies()
        if not self.is_valid():
            log.info("NapCat 票据打开游戏页未通过")
            return False
        log.info("已用 NapCat 当前登录的票据续上，uin=%s", self.uin)
        return True

    def shares_blocked_uin(self) -> bool:
        """攻打号的 uin 不得与扫描号或其他攻打号相同。还没登录时 uin 为空，先放过。"""
        if not self.attack_account:
            return False
        if self.uin and self.uin in self.blocked_uins:
            log.error("攻打号 uin=%s 与扫描号或其他攻打号相同，已停", self.uin)
            return True
        return False

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

    def start_qr(self, push_uin=None, on_qr=None) -> dict:
        """取出二维码，供网页轮询或 qr_login 接着等。不自己循环。

        返回 {ok, pushed, why}。push_uin 为真才试推送；测试页传 False，避免
        上次扫上的 uin 被当成推送目标。
        """
        s = self.session
        self._qr_poll = None
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
            return {"ok": False, "pushed": False, "why": why}

        pushed = False
        if push_uin:
            pushed, why = self._push_login(push_uin)
            if not pushed:
                log.info("推送登录没成（%s），本次用扫码", why)

        folder = os.path.dirname(self.qrcode_file)
        if folder:
            os.makedirs(folder, exist_ok=True)
        with open(self.qrcode_file, "wb") as f:
            f.write(r.content)
        qrsig = self._cookie("qrsig")

        if pushed:
            log.info("已向 QQ %s 推送登录确认 —— 打开手机QQ点「确认登录」即可，"
                     "不需要扫码（扫码图仍保存在 %s 作为备用）", push_uin, self.qrcode_file)
        else:
            log.info("请用手机 QQ 扫码登录（二维码已保存: %s）", self.qrcode_file)
        # 只在"本机交互式使用且没有别的送达方式"时才弹图片查看器。
        # 有 on_qr（PushPlus 推送）时再弹窗没意义；推送登录更是压根不需要看图。
        if os.name == "nt" and on_qr is None and not pushed:
            try:
                os.startfile(self.qrcode_file)
            except OSError:
                pass
        if not pushed:
            _print_qr_ascii(self.qrcode_file)
        if on_qr:
            try:
                # 带上 pushed，让调用方的文案跟实际走的路径一致
                # （推送失败回退到扫码时，不能还提示"点确认登录"）
                on_qr(self.qrcode_file, pushed)
            except Exception as exc:
                log.warning("二维码推送回调失败: %s", exc)

        # 轮询参数照浏览器来：带上 xlogin 拿到的 login_sig，以及 has_onekey=1
        # （"一键/推送登录"标记，推送确认要靠它认）。早先 login_sig 传空串、
        # 也没有 has_onekey，扫码能过但推送这条路认不出来。
        self._qr_poll = {
            "ptqrtoken": hash33(qrsig),
            "login_sig": self._cookie("pt_login_sig") or "",
        }
        return {"ok": True, "pushed": pushed, "why": ""}

    def poll_qr(self) -> dict:
        """轮询一次 ptqrlogin。返回 {code, msg, done, ok}。

        done 表示这次扫码结束（成功、失效或 check_sig 失败），ok 表示登录完成。
        """
        state = getattr(self, "_qr_poll", None)
        if not state:
            return {"code": "", "msg": "还没取二维码", "done": True, "ok": False}
        try:
            r = self.session.get("https://xui.ptlogin2.qq.com/ssl/ptqrlogin", params={
                "u1": GAME_URL, "ptqrtoken": state["ptqrtoken"], "ptredirect": "0",
                "h": "1", "t": "1", "g": "1", "from_ui": "1", "ptlang": "2052",
                "action": f"0-0-{int(time.time() * 1000)}",
                "js_ver": "26071711", "js_type": "1",
                "login_sig": state["login_sig"],
                "pt_uistyle": "40", "aid": PTLOGIN_APPID, "daid": DAID,
                "has_onekey": "1",
            }, headers={"Referer": "https://xui.ptlogin2.qq.com/"},
                timeout=15)
        except requests.RequestException as exc:
            return {"code": "", "msg": f"请求异常 {exc}", "done": False, "ok": False}
        m = re.search(r"ptuiCB\('(\d+)','\d+','([^']*)','\d+','([^']*)'", r.text)
        if not m:
            log.warning("轮询响应无法解析: %s", r.text[:200])
            return {"code": "", "msg": "轮询响应无法解析", "done": False, "ok": False}
        code, url, msg = m.group(1), m.group(2), m.group(3)
        if code == "0":
            log.info("扫码确认成功: %s", msg)
            try:
                self.session.get(url, allow_redirects=True, timeout=20)
            except requests.RequestException as exc:
                self._qr_poll = None
                return {"code": "0", "msg": f"check_sig 失败: {exc}", "done": True, "ok": False}
            self._save_cookies()
            self._qr_poll = None
            if self.is_valid():
                log.info("登录完成，uin=%s", self.uin)
                self._save_cookies()
                return {"code": "0", "msg": msg, "done": True, "ok": True}
            log.error("check_sig 后登录态仍无效")
            return {"code": "0", "msg": "check_sig 后登录态仍无效", "done": True, "ok": False}
        if code == "65":
            log.error("二维码已失效，请重新运行")
            self._qr_poll = None
            return {"code": "65", "msg": "二维码已失效", "done": True, "ok": False}
        if code == "67":
            log.info("已扫码，请在手机上确认…")
            return {"code": "67", "msg": "已扫码，请在手机上确认", "done": False, "ok": False}
        return {"code": code, "msg": msg, "done": False, "ok": False}

    def qr_login(self, timeout_sec: int = 180, on_qr=None, push_uin=None) -> bool:
        """扫码登录。on_qr(qrcode_path) 在二维码生成后回调（用于推送到手机等）。

        push_uin 非空时启用**推送登录**：不用扫码，腾讯直接往该 QQ 号的手机客户端
        推一条登录确认，用户点"确认登录"即可。这解决了"把二维码图片存到本地、
        用同一台手机的相册扫码"被拒（提示"限制本地扫码登录"）的问题 ——
        腾讯的防钓鱼策略要求二维码显示在**另一块屏幕**上，而推送登录没有这个限制。

        参数取自真实客户端抓包：ptqrshow?qr_push=1&qr_push_uin=<uin>&type=1
        """
        # uin 要在清 cookie 之前取，否则就拿不到了
        if push_uin is None:
            push_uin = self.uin or None
        started = self.start_qr(push_uin=push_uin, on_qr=on_qr)
        if not started.get("ok"):
            return False
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            result = self.poll_qr()
            if result.get("done"):
                return bool(result.get("ok"))
            time.sleep(3)
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
