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

保活默认走扫码。管理员测试页另有账号密码登录（pt_tea=2），用来对照票据寿命。

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
import shutil
import struct
import sys
import tempfile
import threading
import time
import zlib

import requests

from . import (PLATFORM_COOKIE, PLAT_HALL, PLAT_QZONE,
               config_platform, normalize_platform, platform_spec)
from . import paths
from .log import console_lock, get_logger
from .qzone import fetch_hall_launch, hall_logged_in

log = get_logger()

# 取票和测试接口共用这一路计数。默认同时只允许 1 次，避免小机器上叠请求。
_napcat_guard = threading.Lock()
_napcat_inflight = 0


def napcat_limit(config: dict | None) -> int:
    raw = ((config or {}).get("登录") or {}).get("NapCat同时")
    try:
        n = int(raw if raw not in (None, "") else 1)
    except (TypeError, ValueError):
        n = 1
    return min(4, max(1, n))


def napcat_inflight() -> int:
    with _napcat_guard:
        return _napcat_inflight


def napcat_begin(limit: int, wait_sec: float) -> bool:
    """占一个取票名额。名额满了就在已有线程上等，等到点仍满则返回 False。不开新线程。"""
    global _napcat_inflight
    deadline = time.monotonic() + max(0.0, float(wait_sec or 0))
    while True:
        with _napcat_guard:
            if _napcat_inflight < max(1, int(limit)):
                _napcat_inflight += 1
                return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.2)


def napcat_end() -> None:
    global _napcat_inflight
    with _napcat_guard:
        if _napcat_inflight:
            _napcat_inflight -= 1


def napcat_host(api: str) -> str:
    text = str(api or "").strip()
    if "://" in text:
        text = text.split("://", 1)[1]
    return text.split("/", 1)[0].split("@")[-1]


def _account_napcat_box(config: dict | None, uin: str) -> dict:
    """这个攻打号配置里的 NapCat。没有就空字典，不回落到扫描号。"""
    accounts = ((config or {}).get("登录") or {}).get("账号") or {}
    if not isinstance(accounts, dict):
        return {}
    uin = str(uin or "").strip()
    found = None
    if uin:
        for key in (f"qq-{uin}", uin):
            item = accounts.get(key)
            if isinstance(item, dict):
                found = item
                break
    if found is None and uin:
        suffix = f"qq-{uin}.json"
        for item in accounts.values():
            if isinstance(item, dict) and str(item.get("cookie") or "").endswith(suffix):
                found = item
                break
    box = (found or {}).get("NapCat") if isinstance(found, dict) else {}
    return box if isinstance(box, dict) else {}


def attack_napcat_endpoint(config: dict | None, uin: str) -> tuple:
    """攻打号自己的 OneBot 地址。没有就返回空，不回落到扫描号或通知机器人。"""
    box = _account_napcat_box(config, uin)
    api = str(box.get("地址") or "").strip().rstrip("/")
    token = str(box.get("Token") or "").strip()
    return api, token


def attack_napcat_webui(config: dict | None, uin: str) -> tuple:
    """NapCat 自己的登录页。界面 Token 是 WebUI 口令，和 OneBot 的 Token 不是同一个。"""
    box = _account_napcat_box(config, uin)
    web = str(box.get("界面") or "").strip().rstrip("/")
    token = str(box.get("界面Token") or "").strip()
    return web, token


def pull_napcat_cookies(api: str, token: str, domain: str):
    """只 POST /get_cookies。不带网页票据，不发消息。成功返回 (True, cookie 字典)。"""
    api = str(api or "").strip().rstrip("/")
    if not api:
        return False, "没有 NapCat 地址"
    headers = {}
    if token:
        headers["Authorization"] = "Bearer " + token
    try:
        r = requests.post(
            api + "/get_cookies",
            json={"domain": domain},
            headers=headers,
            timeout=10,
        )
        data = r.json()
    except Exception as exc:
        return False, f"向 NapCat 要票据失败：{exc}"
    if not isinstance(data, dict):
        return False, "NapCat 返回的不是票据"
    if data.get("status") != "ok" and data.get("retcode") != 0:
        why = data.get("message") or data.get("wording") or data.get("status") or "没有交出票据"
        return False, f"NapCat 没有交出票据：{why}"
    raw = ((data.get("data") or {}).get("cookies") or "")
    pairs = {}
    for part in str(raw).split(";"):
        key, sep, val = part.strip().partition("=")
        if sep and key and key != "domain_id":
            pairs[key] = val
    if not pairs.get("skey") or not pairs.get("p_skey"):
        return False, "NapCat 票据里没有 skey 或 p_skey"
    return True, pairs


_webui_cred = {}
_webui_guard = threading.Lock()


def _webui_password_hash(token: str) -> str:
    return hashlib.sha256((str(token) + ".napcat").encode()).hexdigest()


def _webui_credential(base: str, token: str, force: bool = False):
    """换 NapCat 界面凭证。凭证留在本进程里，不返回给网页，也不另开线程。"""
    now = time.monotonic()
    with _webui_guard:
        hit = _webui_cred.get(base)
        if not force and hit and hit[0] == token and hit[2] > now:
            return True, hit[1]
    try:
        resp = requests.post(
            base + "/api/auth/login",
            json={"hash": _webui_password_hash(token)},
            timeout=8,
        )
        data = resp.json()
    except Exception as exc:
        return False, f"连不上 NapCat 界面：{exc}"
    if not isinstance(data, dict) or data.get("code") not in (0, "0"):
        why = str((data or {}).get("message") or "") if isinstance(data, dict) else ""
        if why == "token is invalid":
            return False, "界面 Token 不对"
        return False, f"NapCat 界面没有放行：{why or '登录失败'}"
    cred = str((data.get("data") or {}).get("Credential") or "")
    if not cred:
        return False, "NapCat 界面没有返回凭证"
    with _webui_guard:
        _webui_cred[base] = (token, cred, now + 50 * 60)
    return True, cred


def _webui_post(base: str, path: str, token: str, retry: bool = True):
    ok, cred = _webui_credential(base, token)
    if not ok:
        return False, cred
    try:
        resp = requests.post(
            base + path,
            json={},
            headers={"Authorization": "Bearer " + cred},
            timeout=8,
        )
        data = resp.json()
    except Exception as exc:
        return False, f"连不上 NapCat 界面：{exc}"
    if not isinstance(data, dict):
        return False, "NapCat 界面返回的不是登录信息"
    message = str(data.get("message") or "")
    if (data.get("code") not in (0, "0") and retry
            and message.lower() in ("unauthorized", "authorization failed")):
        with _webui_guard:
            _webui_cred.pop(base, None)
        return _webui_post(base, path, token, retry=False)
    return True, data


def _webui_qrcode(data: dict) -> str:
    if not isinstance(data, dict) or data.get("code") not in (0, "0"):
        return ""
    body = data.get("data") or {}
    if not isinstance(body, dict):
        return ""
    return str(body.get("qrcode") or body.get("qrcodeurl") or "")


def _webui_already_in(data: dict) -> bool:
    return "logined" in str((data or {}).get("message") or "").lower()


def napcat_login_qr(base: str, token: str) -> dict:
    """向 NapCat 界面要它自己的登录二维码。已经登录就不再要码。不收发消息。"""
    base = str(base or "").strip().rstrip("/")
    token = str(token or "").strip()
    out = {
        "ok": False, "logged_in": False, "offline": False, "qrcode": "",
        "host": napcat_host(base), "msg": "",
    }
    if not base:
        out["msg"] = "没有 NapCat 界面地址。写在登录.账号的 NapCat.界面"
        return out
    if not token:
        out["msg"] = "没有 NapCat 界面 Token"
        return out
    ok, data = _webui_post(base, "/api/QQLogin/CheckLoginStatus", token)
    if not ok:
        out["msg"] = data
        return out
    if data.get("code") not in (0, "0"):
        out["msg"] = str(data.get("message") or "看不了 NapCat 登录状态")
        return out
    info = data.get("data") if isinstance(data.get("data"), dict) else {}
    if info.get("isLogin"):
        out["ok"] = True
        out["logged_in"] = True
        out["msg"] = "NapCat 已经登录，不用再扫码"
        return out
    out["offline"] = bool(info.get("isOffline"))
    qr = str(info.get("qrcodeurl") or "")
    err = str(info.get("loginError") or "")
    if not qr:
        ok, got = _webui_post(base, "/api/QQLogin/GetQQLoginQrcode", token)
        if ok and _webui_already_in(got):
            out["ok"] = True
            out["logged_in"] = True
            out["msg"] = "NapCat 已经登录，不用再扫码"
            return out
        qr = _webui_qrcode(got) if ok else ""
    if not qr:
        _webui_post(base, "/api/QQLogin/RefreshQRcode", token)
        ok, got = _webui_post(base, "/api/QQLogin/GetQQLoginQrcode", token)
        if ok and _webui_already_in(got):
            out["ok"] = True
            out["logged_in"] = True
            out["msg"] = "NapCat 已经登录，不用再扫码"
            return out
        qr = _webui_qrcode(got) if ok else ""
    if not qr:
        out["msg"] = err or "NapCat 还没有登录二维码"
        return out
    out["ok"] = True
    out["qrcode"] = qr
    out["msg"] = err or "用这个攻打号的手机 QQ 扫 NapCat 的登录码"
    return out


def probe_attack_napcat(config: dict | None, uin: str, api: str = "",
                        token: str = "", platform: str = "",
                        apply: bool = False, worker: bool = False) -> dict:
    """在当前线程向攻打号的 NapCat 要一次票，并用游戏页验。不收发消息。

    apply 为真且游戏页认、攻打线程没在跑时，才写入 accounts/qq-<uin>.json。
    """
    uin = str(uin or "").strip()
    if not uin.isdigit() or not 5 <= len(uin) <= 12:
        return {"ok": False, "busy": False, "msg": "QQ 号要是 5 到 12 位数字"}
    cfg_api, cfg_token = attack_napcat_endpoint(config, uin)
    api = str(api or cfg_api).strip().rstrip("/")
    token = cfg_token if token == "" else str(token).strip()
    limit = napcat_limit(config)
    base = {
        "ok": False, "busy": False, "applied": False, "worker": bool(worker),
        "game_ok": None, "host": napcat_host(api), "limit": limit,
        "inflight": napcat_inflight(), "uin": "", "bound": uin,
    }
    if not api:
        base["msg"] = "这个攻打号没有 NapCat 地址。写在登录.账号的 NapCat.地址，或这次请求里带上地址"
        return base
    if not napcat_begin(limit, 0):
        base["busy"] = True
        base["inflight"] = napcat_inflight()
        base["msg"] = "已有取票在进行，这次没有另开"
        return base
    tmp = ""
    try:
        plat = normalize_platform(platform) if str(platform or "").strip() else config_platform(config)
        ok, got = pull_napcat_cookies(api, token, platform_spec(plat)["napcat_domain"])
        if not ok:
            base["msg"] = got
            base["inflight"] = napcat_inflight()
            return base
        tmp = tempfile.mkdtemp(prefix="ts-napcat-")
        qq = QQSession(os.path.join(tmp, "cookies.json"))
        qq.attack_account = True
        qq.bound_uin = uin
        qq.set_platform(plat)
        qq._apply_napcat_pairs(got)
        report = qq.ticket_report()
        base.update({
            "uin": report.get("uin") or "",
            "long_term": bool(report.get("long_term")),
            "skey_left": report.get("skey_left"),
            "p_skey_left": report.get("p_skey_left"),
            "tickets": report.get("tickets") or [],
        })
        if qq.uin != uin:
            base["msg"] = f"NapCat 登录的是 {qq.uin or '空'}，不是这个攻打号"
            return base
        game_ok = qq.is_valid()
        base["game_ok"] = bool(game_ok)
        if not game_ok:
            base["msg"] = "游戏页不认这张票，没有写入"
            return base
        if not apply:
            base["ok"] = True
            base["msg"] = "游戏页认这张票，没有写入"
            return base
        if worker:
            base["ok"] = True
            base["msg"] = "游戏页认这张票。攻打线程在跑，没有改它正在用的 cookie"
            return base
        dest = paths.user_path(f"accounts/qq-{uin}.json")
        qq.cookie_file = dest
        qq._save_cookies()
        base["ok"] = True
        base["applied"] = True
        base["msg"] = "游戏页认这张票，已写入攻打号 cookie"
        return base
    finally:
        napcat_end()
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


def list_attack_napcat(config: dict | None, rows: list) -> dict:
    """列出攻打号有没有自己的 NapCat。不访问网络，不返回 Token 和 cookie 值。"""
    items = []
    seen = set()
    for row in rows or []:
        uin = str((row or {}).get("qq") or "").strip()
        if not uin or uin in seen:
            continue
        seen.add(uin)
        api, token = attack_napcat_endpoint(config, uin)
        web, web_token = attack_napcat_webui(config, uin)
        path = paths.user_path(f"accounts/qq-{uin}.json") if uin.isdigit() else ""
        report = ticket_report_file(path) if path else {}
        items.append({
            "username": (row or {}).get("username") or "",
            "qq": uin,
            "phase": (row or {}).get("phase") or "",
            "online": bool((row or {}).get("online")),
            "host": napcat_host(api),
            "configured": bool(api),
            "token_set": bool(token),
            "web_host": napcat_host(web),
            "web_set": bool(web),
            "web_token_set": bool(web_token),
            "uin": report.get("uin") or "",
            "long_term": bool(report.get("long_term")),
            "skey_left": report.get("skey_left"),
            "p_skey_left": report.get("p_skey_left"),
            "tickets": report.get("tickets") or [],
            "missing": bool(report.get("missing", True)),
        })
    return {
        "limit": napcat_limit(config),
        "inflight": napcat_inflight(),
        "items": items,
    }


def accept_napcat_cookies(config: dict | None, api: str, token: str = "",
                          platform: str = "") -> dict:
    """还没绑定攻打 QQ 时，用 NapCat 交出的号登录。游戏页认了才写入 cookie。

    在调用方当前线程上做，不收发消息。
    """
    api = str(api or "").strip().rstrip("/")
    token = str(token or "").strip()
    limit = napcat_limit(config)
    if not api:
        return {"ok": False, "msg": "没有 NapCat 地址", "uin": ""}
    if not napcat_begin(limit, 0):
        return {"ok": False, "busy": True, "msg": "已有取票在进行", "uin": ""}
    tmp = ""
    try:
        plat = normalize_platform(platform) if str(platform or "").strip() else config_platform(config)
        ok, got = pull_napcat_cookies(api, token, platform_spec(plat)["napcat_domain"])
        if not ok:
            return {"ok": False, "msg": got, "uin": ""}
        tmp = tempfile.mkdtemp(prefix="ts-napcat-")
        qq = QQSession(os.path.join(tmp, "cookies.json"))
        qq.set_platform(plat)
        qq._apply_napcat_pairs(got)
        uin = str(qq.uin or "").strip()
        if not uin.isdigit():
            return {"ok": False, "msg": "NapCat 票据里没有 QQ 号", "uin": ""}
        if not qq.is_valid():
            return {"ok": False, "msg": "游戏页不认这张票", "uin": uin}
        scan_uin = str(ticket_report_file(paths.user_path("cookies.json")).get("uin") or "")
        if scan_uin and scan_uin == uin:
            return {"ok": False, "msg": "这个号是扫描号，不能当攻打号", "uin": uin}
        dest = paths.user_path(f"accounts/qq-{uin}.json")
        qq.cookie_file = dest
        qq._save_cookies()
        return {"ok": True, "msg": "已用 NapCat 登录", "uin": uin}
    finally:
        napcat_end()
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


def try_lock(fh) -> bool:
    """非阻塞独占这份已打开的文件。句柄要一直留着，关掉锁就没了。

    fcntl 只有 Linux / macOS 有。Windows 用 msvcrt 锁文件开头 1 个字节。
    """
    if os.name == "nt":
        import msvcrt
        fh.seek(0, os.SEEK_END)
        if fh.tell() == 0:
            fh.write("0")
            fh.flush()
        fh.seek(0)
        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    import fcntl
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


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


def _qq_tea_encrypt(plain: bytes, key: bytes, rand32=None) -> bytes:
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
    if rand32 is None:
        def rand32():
            return secrets.randbits(32)

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

    buf[0] = (rand32() & 248) | pad
    for i in range(1, pad + 1):
        buf[i] = rand32() & 255
    fill = pad + 1
    extra = 1
    src = 0
    left = n
    while extra <= 2:
        if fill < 8:
            buf[fill] = rand32() & 255
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


def _paint_qr_win(cells, quiet: int) -> bool:
    """直接给控制台格子上色。Windows 默认不解释 ANSI，转义码会原样印出来。"""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        import msvcrt
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = wintypes.HANDLE(msvcrt.get_osfhandle(sys.stdout.fileno()))
        kernel.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.GetConsoleMode.restype = wintypes.BOOL
        if not kernel.GetConsoleMode(handle, ctypes.byref(wintypes.DWORD())):
            return False

        class COORD(ctypes.Structure):
            _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]

        class SMALL_RECT(ctypes.Structure):
            _fields_ = [("Left", ctypes.c_short), ("Top", ctypes.c_short),
                        ("Right", ctypes.c_short), ("Bottom", ctypes.c_short)]

        class CONSOLE_SCREEN_BUFFER_INFO(ctypes.Structure):
            _fields_ = [
                ("dwSize", COORD),
                ("dwCursorPosition", COORD),
                ("wAttributes", ctypes.c_ushort),
                ("srWindow", SMALL_RECT),
                ("dwMaximumWindowSize", COORD),
            ]

        info = CONSOLE_SCREEN_BUFFER_INFO()
        kernel.GetConsoleScreenBufferInfo.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(CONSOLE_SCREEN_BUFFER_INFO)]
        kernel.GetConsoleScreenBufferInfo.restype = wintypes.BOOL
        if not kernel.GetConsoleScreenBufferInfo(handle, ctypes.byref(info)):
            return False
        kernel.SetConsoleTextAttribute.argtypes = [wintypes.HANDLE, wintypes.WORD]
        kernel.SetConsoleTextAttribute.restype = wintypes.BOOL
        kernel.WriteConsoleW.argtypes = [
            wintypes.HANDLE, wintypes.LPCWSTR, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        kernel.WriteConsoleW.restype = wintypes.BOOL
        written = wintypes.DWORD()
        width = len(cells[0]) + 2 * quiet
        blank = [0] * width
        rows = ([blank] * quiet
                + [[0] * quiet + row + [0] * quiet for row in cells]
                + [blank] * quiet)
        sys.stdout.flush()
        try:
            for bits in rows:
                i = 0
                while i < width:
                    j = i + 1
                    while j < width and bits[j] == bits[i]:
                        j += 1
                    kernel.SetConsoleTextAttribute(handle, 0x00 if bits[i] else 0xF0)
                    n = (j - i) * 2
                    kernel.WriteConsoleW(handle, " " * n, n, ctypes.byref(written), None)
                    i = j
                kernel.SetConsoleTextAttribute(handle, info.wAttributes)
                kernel.WriteConsoleW(handle, "\r\n", 2, ctypes.byref(written), None)
        finally:
            kernel.SetConsoleTextAttribute(handle, info.wAttributes)
        return True
    except OSError:
        return False


def _print_qr_ascii(png_path: str) -> None:
    """把二维码打进终端。失败时说明原因，不再静默跳过。

    旧实现依赖 Pillow，没装就直接 return。服务器上常见只装了 requests，
    于是只剩「二维码已保存」一句，终端里没有码。这里用标准库解 PNG。
    Windows 控制台不解释 ANSI，转义码会原样印成 [107m。那里改成直接上色。
    别的终端仍用背景色，两格都是半角空格，避免 █ 在中文环境变成全角。
    四周补白边。
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
    # 一格两个半角空格，黑白只靠颜色，宽度才一致。
    visible = (len(cells[0]) + 2 * quiet) * 2
    if visible > cols:
        log.warning("终端只有 %d 列，二维码大约 %d 列，换行之后扫不中。"
                    "请把窗口拉宽，或打开 %s", cols, visible, png_path)
    with console_lock():
        painted = _paint_qr_win(cells, quiet)
    if painted:
        log.info("（若扫不出来，请把终端拉宽后重试，或打开 %s）", png_path)
        return
    if sys.platform == "win32":
        log.info("当前输出不是控制台，请打开 %s 扫码", png_path)
        return
    dark_cell, light_cell = "\033[40m  \033[0m", "\033[107m  \033[0m"
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
        self._login_busy = False
        self._platform = None
        # 不用可重入锁。SIGTERM 处理函数和正在进行的请求在同一条线程上，
        # 重入会把同一次会话的两个请求叠在一起。
        self._http_lock = threading.Lock()
        self._load_cookies()
        found = self._detect_platform()
        if found:
            self.set_platform(found)

    # ---------- cookie 持久化 ----------

    def _load_cookies(self) -> None:
        if not os.path.exists(self.cookie_file):
            self.note_login("没有 cookie 文件 %s", os.path.basename(self.cookie_file))
            return
        try:
            with open(self.cookie_file, encoding="utf-8") as f:
                jar = json.load(f)
            for c in jar:
                self.session.cookies.set(c["name"], c["value"],
                                         domain=c["domain"], path=c["path"],
                                         expires=c.get("expires"))
            log.debug("已从 %s 载入 %d 条 cookie", self.cookie_file, len(jar))
            self.note_login("载入 %d 条 cookie。%s", len(jar), self.ticket_brief())
        except Exception as exc:
            log.warning("cookie 文件读取失败，将重新登录: %s", exc)
            self.note_login("cookie 文件读取失败，将重新登录：%s", exc)

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
        self.note_login("cookie 已写入 %s。%s",
                        os.path.basename(self.cookie_file), self.ticket_brief())

    def _attack_uin(self) -> str:
        """攻打 QQ。cookie 里还没有时，从 qq-<号>.json 的文件名取。"""
        raw = str(self._cookie("uin") or "").lstrip("o0")
        if raw.isdigit():
            return raw
        base = os.path.basename(self.cookie_file or "")
        if base.startswith("qq-") and base.endswith(".json"):
            tail = base[3:-5]
            if tail.isdigit():
                return tail
        return ""

    def _account_names(self, uin: str) -> list:
        """这个攻打 QQ 绑着的登录账号名称。还没绑上时用当前线程的账号。"""
        names = []
        try:
            from . import citydb
            ids = citydb.users_bound_to_qq(uin) if uin else []
            if not ids:
                base = os.path.basename(self.cookie_file or "")
                if base.startswith("page-login-") and base.endswith(".json"):
                    tail = base[len("page-login-"):-5]
                    if tail.isdigit():
                        ids = [int(tail)]
            if not ids:
                ids = citydb.attack_context_users()
            for uid in ids:
                name = citydb.username_of(uid)
                if name and name not in names:
                    names.append(name)
        except Exception:
            return names
        return names

    def _qq_label(self) -> str:
        """登录日志前缀：登录账号名称，加上攻打 QQ。扫描号单独标出来。"""
        base = os.path.basename(self.cookie_file or "")
        uin = self._attack_uin()
        if base == "cookies.json":
            return f"扫描号 QQ {uin}" if uin else "扫描号"
        names = self._account_names(uin)
        who = "、".join(names)
        if who and uin:
            return f"{who} QQ {uin}"
        if who:
            return who
        if uin:
            return f"QQ {uin}"
        return "未知"

    def ticket_brief(self) -> str:
        """票据还剩多久。只写寿命，不写 cookie 值。"""
        st = self.ticket_status()

        def one(name):
            if name not in st:
                return f"{name}没有"
            left = st.get(name)
            if left is None:
                return f"{name}无过期时间"
            if left <= 0:
                return f"{name}已过期{abs(left) / 3600:.1f}小时"
            return f"{name}剩余{left / 3600:.1f}小时"

        long = "有长效凭据" if self.has_long_term_ticket() else "没有长效凭据"
        return "，".join((one("skey"), one("p_skey"), long))

    def note_login(self, msg, *args) -> None:
        """写到 logs/login.log。每行带上登录账号名称和攻打 QQ。"""
        try:
            from .log import get_login_logger
            text = msg % args if args else str(msg)
            get_login_logger().info("%s %s", self._qq_label(), text)
        except Exception:
            log.debug("登录日志没写上", exc_info=True)

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

    def set_platform(self, name) -> str:
        """这次扫码/续期走空间还是大厅。写进 cookie，下次打开同一份票据还能认。

        值只用 hall/qzone，域名用 ts.local：中文进不了 Cookie 头，也不能把自定义
        标记发给 qq.com。
        """
        self._platform = normalize_platform(name)
        marker = "hall" if self._platform == PLAT_HALL else "qzone"
        self._set_cookie(PLATFORM_COOKIE, marker, domain="ts.local")
        return self._platform

    def apply_config_platform(self, config: dict | None) -> None:
        """cookie 里还看不出平台时，用配置里的「登录.平台」。"""
        if self._detect_platform():
            return
        self.set_platform(config_platform(config))

    def _detect_platform(self) -> str:
        marked = self._cookie(PLATFORM_COOKIE)
        if marked:
            return normalize_platform(marked)
        for c in self.session.cookies:
            if c.name != "p_skey":
                continue
            domain = c.domain or ""
            if "qzone" in domain:
                return PLAT_QZONE
            if "minigame" in domain or "qqgame" in domain:
                return PLAT_HALL
        return ""

    @property
    def platform(self) -> str:
        if self._platform:
            return self._platform
        return self._detect_platform() or PLAT_QZONE

    def _pt(self) -> dict:
        return platform_spec(self.platform)

    @property
    def game_url(self) -> str:
        return self._pt()["game_url"]

    @property
    def pt_appid(self) -> str:
        return self._pt()["ptlogin_appid"]

    @property
    def daid(self) -> str:
        return self._pt()["daid"]

    def _xlogin_params(self, low_login: str = "0") -> dict:
        spec = self._pt()
        return {
            "proxy_url": spec["proxy_url"],
            "daid": spec["daid"],
            "hide_title_bar": "1",
            "low_login": low_login,
            "qlogin_auto_login": "1",
            "no_verifyimg": "1",
            "link_target": "blank",
            "appid": spec["ptlogin_appid"],
            "style": "22",
            "target": "self",
            "s_url": spec["game_url"],
        }

    def _get_p_skey(self) -> str:
        needles = self._pt()["p_skey_needles"]
        for c in self.session.cookies:
            if c.name != "p_skey":
                continue
            domain = c.domain or ""
            if any(n in domain for n in needles):
                return c.value
        for c in self.session.cookies:
            if c.name == "p_skey" and "qzone" in (c.domain or ""):
                return c.value
        return self.session.cookies.get("p_skey") or ""

    @property
    def g_tk(self) -> int:
        key = self._get_p_skey() or self.session.cookies.get("skey") or ""
        return calc_g_tk(key)

    def adopt_napcat(self, config: dict) -> bool:
        """向已登录的 NapCat 要网页票据。只 POST /get_cookies，不收发消息。

        扫描号用「登录.内部QQ」，没填则用「通知」里的地址。
        攻打号只用自己账号上的 NapCat.地址，不借用扫描号那一台。
        """
        if not self.use_napcat:
            return False
        if self.attack_account:
            api = str(getattr(self, "napcat_api", "") or "").strip().rstrip("/")
            token = str(getattr(self, "napcat_token", "") or "").strip()
            if not api:
                api, token = attack_napcat_endpoint(
                    config, getattr(self, "bound_uin", "") or self.uin)
            if not api:
                return False
            wait = 12
        else:
            inner = ((config.get("登录") or {}).get("内部QQ") or {})
            api = str(inner.get("地址") or "").strip().rstrip("/")
            token = str(inner.get("Token") or "").strip()
            if not api:
                note = config.get("通知") or {}
                api = str(note.get("qq_api") or "").strip().rstrip("/")
                token = str(note.get("qq_token") or "").strip()
            if not api:
                return False
            wait = 0
        if not napcat_begin(napcat_limit(config), wait):
            self.note_login("NapCat 取票已有 %d 路在进行，这次跳过", napcat_inflight())
            log.info("NapCat 取票已有 %d 路在进行，这次跳过", napcat_inflight())
            return False
        try:
            return self._adopt_napcat_once(api, token)
        finally:
            napcat_end()

    def _snapshot_cookies(self) -> list:
        return [(c.name, c.value, c.domain, c.path, c.expires)
                for c in self.session.cookies]

    def _restore_cookies(self, snap: list) -> None:
        self._clear_session_cookies()
        for name, value, domain, path, expires in snap:
            self.session.cookies.set(
                name, value, domain=domain, path=path or "/", expires=expires)

    def _apply_napcat_pairs(self, pairs: dict) -> None:
        named = {"p_skey", "p_uin", "pt4_token"}
        p_domain = self._pt()["p_skey_domain"]
        for key, val in pairs.items():
            domain = p_domain if key in named else ".qq.com"
            self._set_cookie(key, val, domain=domain)

    def _adopt_napcat_once(self, api: str, token: str) -> bool:
        self.note_login("向 NapCat 要票据 %s", napcat_host(api))
        ok, got = pull_napcat_cookies(api, token, self._pt()["napcat_domain"])
        if not ok:
            log.info("%s", got)
            self.note_login("%s", got)
            return False
        snap = self._snapshot_cookies()
        self._apply_napcat_pairs(got)
        want = str(getattr(self, "bound_uin", "") or "").strip()
        if self.attack_account and want and self.uin != want:
            self._restore_cookies(snap)
            log.info("NapCat 登录的是 %s，不是绑定的攻打号 %s", self.uin or "空", want)
            self.note_login("NapCat 登录的是 %s，不是绑定的 %s", self.uin or "空", want)
            return False
        if self.shares_blocked_uin():
            self._restore_cookies(snap)
            return False
        if not self.is_valid():
            self._restore_cookies(snap)
            log.info("NapCat 票据打开游戏页未通过")
            self.note_login("NapCat 票据游戏页不认，已还原。%s", self.ticket_brief())
            return False
        self._save_cookies()
        log.info("已用 NapCat 当前登录的票据续上，uin=%s", self.uin)
        self.note_login("已用 NapCat 票据续上。%s", self.ticket_brief())
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
            self.note_login("没有长效凭据(superkey/RK/ptcz)，无法静默续期。%s",
                            self.ticket_brief())
            return False
        self.note_login("开始静默续期。%s", self.ticket_brief())
        self._login_busy = True
        try:
            return self._silent_renew_once()
        finally:
            self._login_busy = False

    def _silent_renew_once(self) -> bool:
        before = {c.name: c.value for c in self.session.cookies}
        try:
            # 1) xlogin 建立 login_sig —— 快速登录的入口，会带出 pt_login_sig
            r = self.session.get(
                "https://xui.ptlogin2.qq.com/cgi-bin/xlogin",
                params=self._xlogin_params(),
                timeout=15)
            login_sig = self._cookie("pt_login_sig") or ""
            if not login_sig:
                log.info("静默续期：未取得 pt_login_sig，放弃")
                self.note_login("静默续期未取得 pt_login_sig，放弃")
                return False

            # 2) 快速登录：服务端凭 superkey/RK/ptcz 识别设备，直接下发新票据
            r = self.session.get(
                "https://ptlogin2.qq.com/pt_login",
                params={"u": self.uin, "aid": self.pt_appid, "daid": self.daid,
                        "login_sig": login_sig, "u1": self.game_url,
                        "ptlang": "2052", "pt_uistyle": "40",
                        "action": f"0-0-{int(time.time() * 1000)}"},
                headers={"Referer": "https://xui.ptlogin2.qq.com/"},
                timeout=15)
            m = re.search(r"ptuiCB\('(\d+)','\d+','([^']*)','\d+','([^']*)'", r.text)
            if m and m.group(1) == "0" and m.group(2):
                self.session.get(m.group(2), allow_redirects=True, timeout=20)
            elif m:
                log.info("静默续期被拒(code=%s): %s", m.group(1), m.group(3)[:60])
                self.note_login("静默续期被拒 code=%s %s", m.group(1), m.group(3)[:60])
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
                self.note_login("静默续期：pt_login 没有返回票据（%s），接着看游戏页认不认现有的票",
                                "是一张 HTML 页面" if is_html else "格式不认识")
        except requests.RequestException as exc:
            log.info("静默续期请求异常: %s", exc)
            self.note_login("静默续期请求异常：%s", exc)
            return False

        if self.is_valid():
            self._save_cookies()
            left = self.ticket_status().get("skey")
            log.info("✅ 静默续期成功，无需扫码%s",
                     f"（新 skey 剩余 {left // 3600} 小时）" if left else "")
            self.note_login("静默续期成功。%s", self.ticket_brief())
            return True

        # 没成功就还原，别把原来还能用的 cookie 搞坏
        for name, val in before.items():
            try:
                self.session.cookies.set(name, val)
            except Exception:
                pass
        log.info("静默续期未生效，需要重新扫码")
        self.note_login("静默续期未生效，需要重新扫码。%s", self.ticket_brief())
        return False

    def touch_login(self, *, block: bool = True) -> bool:
        """再访问一次游戏页，把网页票据的空闲计时往后推，并写回 cookie。

        游戏 socket 活着时不会再打开这个页面。skey 没有过期时间，闲置一段时间后
        腾讯仍会不认。版本更新把进程停掉再拉起，一检查就是 302，只能扫码。
        block=False 用于进程退出：锁被占用就跳过，避免和正在进行的请求死等。
        """
        if getattr(self, "_qr_poll", None) or getattr(self, "_login_busy", False):
            return False
        if not self._http_lock.acquire(blocking=block):
            return False
        try:
            if getattr(self, "_qr_poll", None) or getattr(self, "_login_busy", False):
                return False
            if not self._cookie("skey"):
                self.note_login("网页票据保活跳过：没有 skey。%s", self.ticket_brief())
                return False
            ok, why = self._ticket_alive()
            if ok:
                self._save_cookies()
                self.note_login("网页票据已保活。%s", self.ticket_brief())
                return True
            self.note_login("网页票据保活时游戏页已不认 %s。%s", why, self.ticket_brief())
            return False
        finally:
            self._http_lock.release()

    def is_valid(self) -> bool:
        """访问游戏入口：空间看游戏页是否 200，大厅看 launchapp 是否签发票据。"""
        with self._http_lock:
            try:
                has_skey = bool(self.session.cookies.get("skey"))
            except Exception as exc:
                self.note_login("读取 skey 失败，当作未登录：%s", exc)
                return False
            if not has_skey:
                self.note_login("没有 skey，登录态无效。%s", self.ticket_brief())
                return False
            ok, why = self._ticket_alive()
            if ok:
                return True
            log.info("登录态已失效 (%s)", why)
            self.note_login("游戏页不认这张票 %s。%s", why, self.ticket_brief())
            return False

    def _ticket_alive(self) -> tuple:
        """当前 cookie 能不能打开游戏入口。返回 (是否有效, 失败说明)。"""
        if self.platform == PLAT_HALL:
            try:
                data = fetch_hall_launch(self.session, referer=self.game_url)
            except Exception as exc:
                return False, f"大厅 launchapp 失败：{exc}"
            if hall_logged_in(data):
                return True, ""
            why = (data.get("resultstr") or data.get("SeedErr")
                   or data.get("_error") or data.get("msg") or data.get("result"))
            return False, f"大厅未登录 {why}".strip()
        try:
            r = self.session.get(self.game_url, allow_redirects=False, timeout=15)
        except requests.RequestException as exc:
            return False, f"请求失败：{exc}"
        if r.status_code == 200 and "ptlogin" not in (r.text or "")[:2000]:
            return True, ""
        loc = r.headers.get("Location", "")
        return False, f"status={r.status_code} 跳去登录={'ptlogin' in loc.lower()}"

    # ---------- 扫码 / 推送登录 ----------

    def forget_login(self) -> None:
        """这次登录不能留。会话和已经写进文件的票据都丢掉，避免换成另一个 QQ。"""
        self.note_login("这次登录的 QQ 不能留，丢掉会话并删除 cookie 文件 %s",
                        os.path.basename(self.cookie_file or ""))
        self._clear_session_cookies()
        self._qr_poll = None
        try:
            os.remove(self.cookie_file)
        except OSError:
            pass

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
                        "type": "1", "appid": self.pt_appid,
                        "t": str(random.random()), "ptlang": "2052",
                        "u1": self.game_url, "daid": self.daid, "pt_3rd_aid": "0"},
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
            "appid": self.pt_appid, "e": "2", "l": "M", "s": "3", "d": "72",
            "v": "4", "t": str(random.random()), "daid": self.daid, "pt_3rd_aid": "0",
            "ptlang": "2052", "u1": self.game_url,
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

    def start_qr(self, push_uin=None, on_qr=None, announce: bool = True) -> dict:
        """取出二维码，供网页轮询或 qr_login 接着等。不自己循环。

        返回 {ok, pushed, why}。push_uin 为真才试推送；测试页传 False，避免
        上次扫上的 uin 被当成推送目标。announce 为假时先不弹图、不回调，
        等调用方确认这张码还有效再交给页面。
        """
        s = self.session
        self._qr_poll = None
        plat = self.platform
        # 清会话票据但**保住设备凭据** —— 推送靠 dev_mid_sig 之类识别"推给哪台设备"，
        # 全清了就只能回 ec=313。磁盘上的文件要等登录成功才会重写。
        before = self.ticket_brief()
        self.note_login("开始取二维码（%s），清掉内存里的会话票据。清掉前：%s", plat, before)
        self._clear_session_cookies(keep=DEVICE_COOKIES)
        self.set_platform(plat)
        # 浏览器进登录页第一件事就是 xlogin，它建立 pt_login_sig 上下文。
        # 但它同时会**无条件重新签发 pt_guid_sig**（实测：本来就有一个也照换），
        # 而 pt_guid_sig 是和 dev_mid_sig 配对的，被换掉就对不上了。
        # 所以先存后还。
        guid_before = self._cookie("pt_guid_sig")
        try:
            s.get("https://xui.ptlogin2.qq.com/cgi-bin/xlogin",
                  params=self._xlogin_params(),
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
            self.note_login("二维码请求失败：%s", why)
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
        if qrsig:
            # 响应里的 qrsig 有时是 host-only，或和旧的叠在一起。轮询带错这一张，
            # 手机上刚扫就会提示过期。收成一条，钉在 .ptlogin2.qq.com 上。
            self._set_cookie("qrsig", qrsig)

        if pushed:
            log.info("已向 QQ %s 推送登录确认 —— 打开手机QQ点「确认登录」即可，"
                     "不需要扫码（扫码图仍保存在 %s 作为备用）", push_uin, self.qrcode_file)
            self.note_login("已向 QQ %s 推送登录确认，等待手机点确认", push_uin)
        else:
            log.info("请用手机 QQ 扫码登录（二维码已保存: %s）", self.qrcode_file)
            self.note_login("二维码已生成，等待扫码")
        # 只在"本机交互式使用且没有别的送达方式"时才弹图片查看器。
        # 有 on_qr（PushPlus 推送）时再弹窗没意义；推送登录更是压根不需要看图。
        if announce and os.name == "nt" and on_qr is None and not pushed:
            try:
                os.startfile(self.qrcode_file)
            except OSError:
                pass
        if not pushed:
            _print_qr_ascii(self.qrcode_file)
        if announce and on_qr:
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
        """轮询一次 ptqrlogin。返回 {code, msg, done, ok, expired, scanned}。

        done 表示这次扫码结束（成功、失效或 check_sig 失败），ok 表示登录完成。
        失效只看文案和 10009/10006。65 有时是「已扫码待确认」，不能一律换图。
        """
        state = getattr(self, "_qr_poll", None)
        if not state:
            return {"code": "", "msg": "还没取二维码", "done": True, "ok": False,
                    "expired": False, "scanned": False}
        try:
            r = self.session.get("https://xui.ptlogin2.qq.com/ssl/ptqrlogin", params={
                "u1": self.game_url, "ptqrtoken": state["ptqrtoken"], "ptredirect": "0",
                "h": "1", "t": "1", "g": "1", "from_ui": "1", "ptlang": "2052",
                "action": f"0-0-{int(time.time() * 1000)}",
                "js_ver": "26071711", "js_type": "1",
                "login_sig": state["login_sig"],
                "pt_uistyle": "40", "aid": self.pt_appid, "daid": self.daid,
                "has_onekey": "1",
            }, headers={"Referer": "https://xui.ptlogin2.qq.com/"},
                timeout=15)
        except requests.RequestException as exc:
            return {"code": "", "msg": f"请求异常 {exc}", "done": False, "ok": False,
                    "expired": False, "scanned": False}
        m = re.search(r"ptuiCB\('(\d+)','\d+','([^']*)','\d+','([^']*)'", r.text)
        if not m:
            log.warning("轮询响应无法解析: %s", r.text[:200])
            return {"code": "", "msg": "轮询响应无法解析", "done": False, "ok": False,
                    "expired": False, "scanned": False}
        code, url, msg = m.group(1), m.group(2), m.group(3)
        if code == "0":
            log.info("扫码确认成功: %s", msg)
            self.note_login("手机已确认登录")
            try:
                self.session.get(url, allow_redirects=True, timeout=20)
            except requests.RequestException as exc:
                self._qr_poll = None
                self.note_login("扫码确认后 check_sig 失败：%s", exc)
                return {"code": "0", "msg": f"check_sig 失败: {exc}", "done": True, "ok": False,
                        "expired": False, "scanned": False}
            self._save_cookies()
            self._qr_poll = None
            if self.is_valid():
                log.info("登录完成，uin=%s", self.uin)
                self._save_cookies()
                self.note_login("扫码登录完成。%s", self.ticket_brief())
                return {"code": "0", "msg": msg, "done": True, "ok": True,
                        "expired": False, "scanned": False}
            log.error("check_sig 后登录态仍无效")
            self.note_login("扫码确认后游戏页仍不认。%s", self.ticket_brief())
            return {"code": "0", "msg": "check_sig 后登录态仍无效", "done": True, "ok": False,
                    "expired": False, "scanned": False}
        text = msg or ""
        scanned = any(k in text for k in ("已扫描", "认证中", "请在手机")) or (
            code == "67" and "未失效" not in text and "等待" not in text)
        if any(k in text for k in ("未失效", "已扫描", "认证中", "请在手机")):
            expired = False
        elif "失效" in text or "过期" in text or code in ("10009", "10006"):
            expired = True
        else:
            expired = False
        if expired:
            log.info("二维码已失效 code=%s %s", code, text)
            self.note_login("二维码已失效 code=%s %s", code, text[:40])
            self._qr_poll = None
            return {"code": code, "msg": text or "二维码已失效", "done": True, "ok": False,
                    "expired": True, "scanned": False}
        if scanned and not state.get("scanned"):
            state["scanned"] = True
            log.info("已扫码，请在手机上确认…")
            self.note_login("已扫码，等待手机确认")
        return {"code": code, "msg": text, "done": False, "ok": False,
                "expired": False, "scanned": scanned}

    def password_login(self, uin: str, password: str, low_login: bool = False,
                       ticket: str = "", randstr: str = "") -> dict:
        """ptlogin2 账号密码登录。第一次要滑块时把 sid 交给页面，用户划完再带着 ticket 继续。

        low_login 为真时带 low_login_enable=1、low_login_hour=720（下次自动登录）。
        默认 false，与现有扫码 xlogin 的 low_login=0 同一套入口，方便对照票据寿命。
        划过并登录成功后设备票据会留下，下次 check 可能不再要滑块。
        """
        uin = str(uin or "").strip()
        ticket = str(ticket or "").strip()
        randstr = str(randstr or "").strip()
        if not uin.isdigit() or not 5 <= len(uin) <= 12:
            return {"ok": False, "captcha": False, "code": "", "msg": "QQ 号要是 5 到 12 位数字"}
        if not password:
            return {"ok": False, "captcha": False, "code": "", "msg": "密码是空的"}

        pending = getattr(self, "_pwd_pending", None) or {}
        fresh = pending.get("uin") == uin and time.time() - float(pending.get("at") or 0) < 600
        if ticket and randstr and fresh:
            return self._pt_password_submit(
                password, pending, low_login=low_login, ticket=ticket, randstr=randstr)

        self._pwd_pending = None
        plat = self.platform
        self._clear_session_cookies(keep=DEVICE_COOKIES)
        self.set_platform(plat)
        guid_before = self._cookie("pt_guid_sig")
        low = "1" if low_login else "0"
        try:
            self.session.get(
                "https://xui.ptlogin2.qq.com/cgi-bin/xlogin",
                params=self._xlogin_params(low),
                timeout=15)
        except requests.RequestException as exc:
            return {"ok": False, "captcha": False, "code": "", "msg": f"xlogin 失败: {exc}"}
        if guid_before and self._cookie("pt_guid_sig") != guid_before:
            self._set_cookie("pt_guid_sig", guid_before)

        login_sig = self._cookie("pt_login_sig") or ""
        try:
            r = self.session.get(
                "https://ssl.ptlogin2.qq.com/check",
                params={"regmaster": "", "pt_tea": "2", "pt_vcode": "1",
                        "uin": uin, "appid": self.pt_appid,
                        "js_ver": "26071711", "js_type": "1",
                        "login_sig": login_sig, "u1": self.game_url,
                        "r": str(random.random()), "pt_uistyle": "22",
                        "daid": self.daid, "pt_3rd_aid": "0"},
                headers={"Referer": "https://xui.ptlogin2.qq.com/"},
                timeout=15)
        except requests.RequestException as exc:
            return {"ok": False, "captcha": False, "code": "", "msg": f"check 失败: {exc}"}

        quoted = re.findall(r"'((?:\\.|[^'\\])*)'", r.text or "")
        if len(quoted) < 7:
            head = (r.text or "")[:120].replace("\n", " ")
            return {"ok": False, "captcha": False, "code": "",
                    "msg": f"check 响应无法解析：{head}"}
        check_ret, vcode, salt_js, verifysession, randsalt, ptdrvs, sid = quoted[:7]
        if check_ret == "2":
            return {"ok": False, "captcha": False, "code": "2", "msg": "QQ 号不对"}
        if check_ret not in ("0", "1", "3"):
            return {"ok": False, "captcha": False, "code": check_ret,
                    "msg": f"check 被拒（code={check_ret}）"}
        salt = _js_string_bytes(salt_js)
        if len(salt) < 8:
            salt = salt.ljust(8, b"\x00")
        pending = {
            "uin": uin,
            "salt_hex": salt.hex(),
            "vcode": vcode or "!AAA",
            "verifysession": verifysession or self._cookie("verifysession") or "",
            "randsalt": randsalt or "2",
            "ptdrvs": ptdrvs or "",
            "sid": sid or "",
            "login_sig": login_sig,
            "cap_cd": vcode if check_ret == "1" else "",
            "at": time.time(),
        }
        if check_ret == "1":
            self._pwd_pending = pending
            self._save_cookies()
            log.info("密码登录要滑块 uin=%s，等页面划完", uin)
            return {
                "ok": False, "captcha": True, "code": "1",
                "msg": "请完成滑动验证。划过一次之后，下次密码登录可能就不用再验证。",
                "aid": self.pt_appid, "sid": pending["sid"],
                "cap_cd": pending["cap_cd"],
            }
        return self._pt_password_submit(
            password, pending, low_login=low_login, ticket="", randstr="")

    def _pt_password_submit(self, password: str, pending: dict, low_login: bool = False,
                            ticket: str = "", randstr: str = "") -> dict:
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
        except Exception as exc:
            return {"ok": False, "captcha": False, "code": "",
                    "msg": f"密码加密失败: {exc}"}
        params = {
            "u": uin, "verifycode": vcode, "pt_vcode_v1": pt_vcode,
            "pt_verifysession_v1": session, "p": enc,
            "pt_randsalt": pending.get("randsalt") or "2",
            "u1": self.game_url, "ptredirect": "0", "h": "1", "t": "1", "g": "1",
            "from_ui": "1", "ptlang": "2052",
            "action": f"2-0-{int(time.time() * 1000)}",
            "js_ver": "26071711", "js_type": "1",
            "login_sig": pending.get("login_sig") or "",
            "pt_uistyle": "22",
            "aid": self.pt_appid, "daid": self.daid,
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
            r = self.session.get(
                "https://ssl.ptlogin2.qq.com/login",
                params=params,
                headers={"Referer": "https://xui.ptlogin2.qq.com/"},
                timeout=20)
        except requests.RequestException as exc:
            return {"ok": False, "captcha": False, "code": "", "msg": f"login 失败: {exc}"}

        m = re.search(r"ptuiCB\('(\d+)','\d+','([^']*)','\d+','([^']*)'", r.text or "")
        if not m:
            head = (r.text or "")[:120].replace("\n", " ")
            return {"ok": False, "captcha": False, "code": "",
                    "msg": f"login 响应无法解析：{head}"}
        code, url, msg = m.group(1), m.group(2), m.group(3)
        if code != "0":
            self._pwd_pending = None
            log.info("密码登录被拒 uin=%s code=%s %s", uin, code, (msg or "")[:60])
            return {"ok": False, "captcha": False, "code": code,
                    "msg": msg or f"登录失败 code={code}"}
        self._pwd_pending = None
        try:
            self.session.get(url, allow_redirects=True, timeout=20)
        except requests.RequestException as exc:
            return {"ok": False, "captcha": False, "code": "0",
                    "msg": f"check_sig 失败: {exc}"}
        self._save_cookies()
        if self.is_valid():
            log.info("密码登录完成，uin=%s low_login=%s", self.uin, low_login)
            self.note_login("密码登录完成 low_login=%s。%s", low_login, self.ticket_brief())
            self._save_cookies()
            return {"ok": True, "captcha": False, "code": "0", "msg": msg or "登录成功"}
        log.error("密码登录 check_sig 后游戏页仍不认 uin=%s", uin)
        return {"ok": False, "captcha": False, "code": "0",
                "msg": "check_sig 后游戏页仍不认这张票"}

    def qr_login(self, timeout_sec: int = 180, on_qr=None, push_uin=None,
                 until: float = 0) -> bool:
        """扫码登录。on_qr(qrcode_path) 在二维码生成后回调（用于推送到手机等）。

        push_uin 非空时启用**推送登录**：不用扫码，腾讯直接往该 QQ 号的手机客户端
        推一条登录确认，用户点"确认登录"即可。这解决了"把二维码图片存到本地、
        用同一台手机的相册扫码"被拒（提示"限制本地扫码登录"）的问题 ——
        腾讯的防钓鱼策略要求二维码显示在**另一块屏幕**上，而推送登录没有这个限制。

        参数取自真实客户端抓包：ptqrshow?qr_push=1&qr_push_uin=<uin>&type=1
        二维码过期或这张等太久，马上换一张，页面才能跟上。
        先轮询一次，确认还没失效再交给页面。已扫上的会话一直留到确认或真正失效。
        """
        # uin 要在清 cookie 之前取，否则就拿不到了
        if push_uin is None:
            push_uin = self.uin or None
        while True:
            if until and time.time() >= until:
                return False
            started = self.start_qr(push_uin=push_uin, on_qr=on_qr, announce=False)
            if not started.get("ok"):
                return False
            result = self.poll_qr()
            if result.get("expired"):
                log.info("二维码过期，马上换一张")
                time.sleep(1)
                continue
            if result.get("done"):
                return bool(result.get("ok"))
            if on_qr:
                try:
                    on_qr(self.qrcode_file, bool(started.get("pushed")))
                except Exception as exc:
                    log.warning("二维码推送回调失败: %s", exc)
            elif os.name == "nt" and not started.get("pushed"):
                try:
                    os.startfile(self.qrcode_file)
                except OSError:
                    pass
            deadline = time.time() + timeout_sec
            if until:
                deadline = min(deadline, until)
            if result.get("scanned"):
                deadline = max(deadline, time.time() + 120)
                if until:
                    deadline = min(deadline, until)
            timed_out = True
            while time.time() < deadline:
                time.sleep(3)
                result = self.poll_qr()
                if result.get("scanned"):
                    deadline = max(deadline, time.time() + 120)
                    if until:
                        deadline = min(deadline, until)
                if not result.get("done"):
                    continue
                if result.get("ok"):
                    return True
                if result.get("expired"):
                    log.info("二维码过期，马上换一张")
                    timed_out = False
                    break
                return False
            if timed_out:
                log.info("这张二维码等了 %d 秒还没扫上，马上换一张", timeout_sec)
                self.note_login("这张二维码等了 %d 秒还没扫上，马上换一张", timeout_sec)

    def ensure_login(self, on_qr=None, push_uin=None) -> bool:
        """保证登录可用。顺序：现成 cookie → 长效凭据静默续期 → 推送/扫码登录。

        需要人工介入的那步是最后手段 —— 守护进程要无人值守地跑。
        push_uin 见 qr_login()：给了就用推送登录，免去扫码。
        """
        if self.is_valid():
            left = self.ticket_status().get("skey")
            log.info("cookie 有效，uin=%s%s", self.uin,
                     f"（skey 剩余约 %.1f 小时）" % (left / 3600) if left else "")
            self.note_login("现成 cookie 仍有效。%s", self.ticket_brief())
            # 快到期就提前续，别等失效了才补救。攻打线程不走这里。
            if self.expires_within(6 * 3600) and self.has_long_term_ticket():
                log.info("skey 即将到期，提前静默续期…")
                self.note_login("skey 或 p_skey 将在 6 小时内到期，提前静默续期")
                self.silent_renew()
            return True
        self.note_login("现成 cookie 无效，先试静默续期。%s", self.ticket_brief())
        if self.silent_renew():
            return True
        self.note_login("静默续期没接上，改为扫码")
        return self.qr_login(on_qr=on_qr, push_uin=push_uin)
