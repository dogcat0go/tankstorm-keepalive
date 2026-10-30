# tankstorm-keepalive  Copyright (C) 2026 Dimlitter
# SPDX-License-Identifier: AGPL-3.0-or-later
"""打指定城市，或先移动到该城再打。窗口程序，不带钩子版 SWF。"""

import json
import logging
import os
import queue
import sys
import threading
import time
from urllib.parse import urlencode
import tkinter as tk
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from tkinter import ttk

import requests
import urllib3.util.connection as _u3conn

import main as cli
from tankstorm import citydb, socket_keepalive
from tankstorm.log import get_logger
from tankstorm.qq_login import QQSession

log = get_logger()

# 打包前改这三行。打进 exe 之后，改旁边的 config.json、config.local.json 都不会换服务器、账号，也不会多放行 QQ。
LOCK_SERVER = "https://tankstorm.site"
LOCK_IP = "101.33.228.207"
LOCK_ACCOUNT = "wuliao"
LOCK_QQS = (1785866691,807020971,470422983,972843421)

# 登录不走对方电脑的 DNS，TCP 直连上面这个地址。证书仍按域名校验。
_login_host = LOCK_SERVER.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0]
_login_ip = str(LOCK_IP or "").strip()
_real_create_connection = _u3conn.create_connection


def _direct_create_connection(address, *args, **kwargs):
    host, port = address
    if _login_ip and host == _login_host:
        address = (_login_ip, port)
    return _real_create_connection(address, *args, **kwargs)


_u3conn.create_connection = _direct_create_connection


def _locked():
    """编译进程序的登录账号，以及这个账号能用的 QQ。"""
    username = str(LOCK_ACCOUNT or "").strip()
    qqs = []
    for item in LOCK_QQS:
        uin = str(item).strip()
        if uin.isdigit() and 5 <= len(uin) <= 12 and uin not in qqs:
            qqs.append(uin)
    return username, qqs


def _server():
    base = str(LOCK_SERVER or "").strip().rstrip("/")
    if base.startswith("http://") or base.startswith("https://"):
        return base
    return ""


def _json_body(resp):
    try:
        data = resp.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _fetch_clock(session):
    """向服务器取这个登录态的最新有效期。返回 (错误说明, 正文)。"""
    base = _server()
    if not base or session is None:
        return "请先登录账号", None
    try:
        resp = session.post(
            base + "/api/clock",
            json={"mono_ms": int(time.monotonic() * 1000)},
            timeout=20)
        body = _json_body(resp)
    except Exception as exc:
        return f"有效期没取到：{exc}", None
    if resp.status_code == 401:
        return "请先登录账号", None
    if resp.status_code != 200 or not body.get("ok"):
        return str(body.get("error") or "有效期没取到"), None
    return "", body


def _login_remote(username, password):
    """向编译时写定的服务器登录，并取回最新有效期。"""
    base = _server()
    if not base:
        return "这个版本编译时没有写入服务器", None, None
    session = requests.Session()
    session.trust_env = False
    try:
        resp = session.post(
            base + "/api/login",
            json={"username": username, "password": password},
            timeout=20)
        body = _json_body(resp)
    except Exception as exc:
        return f"连不上服务器：{exc}", None, None
    if resp.status_code == 403:
        return str(body.get("error") or "账号已过期"), None, None
    if resp.status_code != 200 or not body.get("ok"):
        return str(body.get("error") or "用户名或密码不对"), None, None
    why, clock = _fetch_clock(session)
    if why:
        return why, None, None
    if clock.get("expired"):
        return "账号已过期", None, None
    return "", session, clock


def _captcha_html(aid, sid, uin):
    opts = json.dumps({
        "sid": str(sid or ""),
        "uin": str(uin or ""),
        "type": "popup",
        "enableAged": True,
    }, ensure_ascii=False)
    aid_js = json.dumps(str(aid or "549000912"))
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>滑动验证</title></head>
<body>
<p id="tip">腾讯滑动验证</p>
<button id="go" type="button">打开滑动验证</button>
<script src="https://ssl.captcha.qq.com/TCaptcha.js"></script>
<script>
function go() {{
  var tip = document.getElementById("tip");
  if (!window.TencentCaptcha) {{
    tip.textContent = "验证码脚本没加载出来";
    return;
  }}
  var cap = new TencentCaptcha({aid_js}, function(res) {{
    fetch("/done", {{
      method: "POST",
      headers: {{"Content-Type": "application/json"}},
      body: JSON.stringify(res || {{}})
    }});
    tip.textContent = (res && res.ret === 0) ? "验证已提交，可以关掉这个窗口" : "滑动验证没过";
  }}, {opts});
  cap.show();
}}
document.getElementById("go").onclick = go;
window.addEventListener("load", function() {{ setTimeout(go, 300); }});
</script>
</body></html>"""


def _captcha_form(url):
    """在程序自己的窗口里打开滑块，不调用电脑上的浏览器。返回关闭函数。"""
    import clr
    from System import Action
    from System.Threading import ApartmentState, Thread, ThreadStart
    from webview.util import interop_dll_path

    clr.AddReference("System.Windows.Forms")
    clr.AddReference(interop_dll_path("Microsoft.Web.WebView2.Core.dll"))
    clr.AddReference(interop_dll_path("Microsoft.Web.WebView2.WinForms.dll"))
    import System.Windows.Forms as WinForms
    from Microsoft.Web.WebView2.WinForms import WebView2

    holder = {}

    def launch():
        form = WinForms.Form()
        form.Text = "滑动验证"
        form.Width = 440
        form.Height = 680
        form.StartPosition = WinForms.FormStartPosition.CenterScreen
        form.TopMost = True
        view = WebView2()
        view.Dock = WinForms.DockStyle.Fill
        form.Controls.Add(view)

        def on_ready(sender, args):
            if view.CoreWebView2 is not None:
                view.CoreWebView2.Navigate(url)

        def on_load(sender, args):
            view.CoreWebView2InitializationCompleted += on_ready
            view.EnsureCoreWebView2Async(None)

        form.Load += on_load
        form._keep = (on_ready, on_load, view)
        holder["form"] = form
        WinForms.Application.Run(form)

    thread = Thread(ThreadStart(launch))
    thread.SetApartmentState(ApartmentState.STA)
    thread.IsBackground = True
    thread.Start()

    def close():
        form = holder.get("form")
        if form is None:
            return
        try:
            form.BeginInvoke(Action(form.Close))
        except Exception:
            pass

    return close


def _qq_secret_path(uin: str) -> str:
    path = socket_keepalive.attack_cookie_file(uin)
    return path[:-5] + ".secret" if path.endswith(".json") else path + ".secret"


def _save_qq_password(uin: str, password: str) -> None:
    """用 Windows 当前用户的密钥把 QQ 密码存在本机。换一台电脑读不出来。"""
    import ctypes
    from ctypes import wintypes
    password = str(password or "")
    if not password:
        return

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]

    raw = password.encode("utf-8")
    buf = ctypes.create_string_buffer(raw)
    blob = DATA_BLOB(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte)))
    out = DATA_BLOB()
    if not ctypes.windll.crypt32.CryptProtectData(
            ctypes.byref(blob), None, None, None, None, 0, ctypes.byref(out)):
        log.warning("QQ 密码没能记下")
        return
    try:
        data = ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)
    path = _qq_secret_path(uin)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def _load_qq_password(uin: str) -> str:
    import ctypes
    from ctypes import wintypes
    path = _qq_secret_path(uin)
    if not os.path.isfile(path):
        return ""
    try:
        data = open(path, "rb").read()
    except OSError:
        return ""

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]

    buf = ctypes.create_string_buffer(data)
    blob = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte)))
    out = DATA_BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(blob), None, None, None, None, 0, ctypes.byref(out)):
        return ""
    try:
        return ctypes.string_at(out.pbData, out.cbData).decode("utf-8")
    except Exception:
        return ""
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def _hall_login_url() -> str:
    """QQ 游戏大厅的登录页。密码在这个页面里手输。"""
    from tankstorm import PLAT_HALL, platform_spec
    spec = platform_spec(PLAT_HALL)
    return "https://xui.ptlogin2.qq.com/cgi-bin/xlogin?" + urlencode({
        "proxy_url": spec["proxy_url"],
        "daid": spec["daid"],
        "hide_title_bar": "1",
        "low_login": "0",
        "qlogin_auto_login": "0",
        "no_verifyimg": "1",
        "link_target": "blank",
        "appid": spec["ptlogin_appid"],
        "style": "22",
        "target": "self",
        "s_url": "https://minigame.qq.com/",
    })


def _hall_login(expect, stop, password: str = ""):
    """打开游戏大厅登录窗，等这个 QQ 手输密码登上来。返回 cookie 列表，取消则是 None。"""
    import clr
    from System import Action
    from System.Threading import ApartmentState, Thread, ThreadStart
    from System.Threading.Tasks import Task
    from webview.util import interop_dll_path

    clr.AddReference("System.Windows.Forms")
    clr.AddReference(interop_dll_path("Microsoft.Web.WebView2.Core.dll"))
    clr.AddReference(interop_dll_path("Microsoft.Web.WebView2.WinForms.dll"))
    import System.Windows.Forms as WinForms
    from Microsoft.Web.WebView2.WinForms import WebView2

    url = _hall_login_url()
    remembered = {"text": str(password or "")}
    box = {}
    done = threading.Event()
    holder = {}

    def launch():
        form = WinForms.Form()
        form.Text = "QQ游戏大厅登录"
        form.Width = 480
        form.Height = 720
        form.StartPosition = WinForms.FormStartPosition.CenterScreen
        form.TopMost = True
        view = WebView2()
        view.Dock = WinForms.DockStyle.Fill
        form.Controls.Add(view)

        def finish(jar, uin):
            box["jar"] = jar
            box["uin"] = uin
            form.Close()

        finished = {"on": False}

        def consider(cookies):
            if finished["on"]:
                return
            jar = []
            uin = ""
            has_skey = False
            for cookie in cookies:
                if not isinstance(cookie, dict):
                    continue
                name = str(cookie.get("name") or "")
                value = str(cookie.get("value") or "")
                if not name:
                    continue
                if name == "uin":
                    uin = value.lstrip("o0")
                if name in ("skey", "p_skey") and value:
                    has_skey = True
                exp = cookie.get("expires")
                try:
                    exp = None if exp is None or float(exp) < 0 else float(exp)
                except (TypeError, ValueError):
                    exp = None
                jar.append({
                    "name": name,
                    "value": value,
                    "domain": str(cookie.get("domain") or ""),
                    "path": str(cookie.get("path") or "/"),
                    "expires": exp,
                })
            log.info("登录票：页面 QQ=%s，期望 %s，skey=%s，共 %d 条",
                     uin or "无", expect, "有" if has_skey else "无", len(jar))
            if not uin or not has_skey:
                return
            log.info("登录已确认，关闭登录窗。页面 QQ=%s，期望 %s", uin, expect)
            finished["on"] = True
            saved_jar, saved_uin = jar, uin

            def go():
                finish(saved_jar, saved_uin)

            form.BeginInvoke(Action(go))

        def on_ready(sender, args):
            core = view.CoreWebView2
            if core is None:
                return

            def on_popup(sender, args):
                uri = str(getattr(args, "Uri", "") or "")
                try:
                    args.Handled = True
                except Exception:
                    pass
                if uri:
                    core.Navigate(uri)

            core.NewWindowRequested += on_popup

            reading = {"task": None, "n": 0}

            def take_result(task):
                if task.IsFaulted:
                    log.info("读取登录票据失败：%s", task.Exception)
                    return
                data = json.loads(str(task.Result))
                cookies = data.get("cookies") or []
                names = sorted({str(c.get("name") or "") for c in cookies if isinstance(c, dict)})
                log.info("读到票据 %d 条：%s", len(cookies), "、".join(names) or "空")
                consider(cookies)

            filled = {"on": False}

            def on_script(task):
                try:
                    if task.IsFaulted:
                        return
                    text = json.loads(str(task.Result))
                except Exception:
                    return
                if isinstance(text, str) and text:
                    remembered["text"] = text
                    _save_qq_password(expect, text)

            def tick(sender, args):
                reading["n"] += 1
                try:
                    log.info("第 %d 次检查登录，页面在 %s", reading["n"], core.Source)
                except Exception as exc:
                    log.info("第 %d 次检查登录，页面地址读不到：%s", reading["n"], exc)
                task = reading["task"]
                if task is not None and task.IsCompleted:
                    reading["task"] = None
                    try:
                        take_result(task)
                    except Exception as exc:
                        log.info("登录票据解析失败：%s", exc)
                    task = None
                if task is None:
                    try:
                        reading["task"] = core.CallDevToolsProtocolMethodAsync(
                            "Network.getAllCookies", "{}")
                    except Exception as exc:
                        log.info("没能向页面要登录票据：%s", exc)
                if remembered["text"] and not filled["on"]:
                    script = (
                        "(function(){var p=document.querySelector('input[type=password]');"
                        "if(!p)return false;var set=Object.getOwnPropertyDescriptor("
                        "window.HTMLInputElement.prototype,'value').set;"
                        "set.call(p,%s);p.dispatchEvent(new Event('input',{bubbles:true}));"
                        "var b=document.querySelector('#login_button,.login_button,button');"
                        "if(b)b.click();return true;})()"
                    ) % json.dumps(remembered["text"])
                    try:
                        core.ExecuteScriptAsync(script)
                        filled["on"] = True
                    except Exception:
                        log.exception("没能填入记下的 QQ 密码")
                try:
                    core.ExecuteScriptAsync(
                        "(function(){var p=document.querySelector('input[type=password]');"
                        "return p&&p.value?p.value:'';})()"
                    ).ContinueWith(Action[Task[str]](on_script))
                except Exception:
                    pass

            timer = WinForms.Timer()
            timer.Interval = 1000
            timer.Tick += tick
            timer.Start()
            log.info("登录窗已打开，开始检查登录票")
            core.Navigate(url)
            form._keep = (on_ready, on_popup, timer, view)

        def on_load(sender, args):
            view.CoreWebView2InitializationCompleted += on_ready
            view.EnsureCoreWebView2Async(None)

        form.Load += on_load
        holder["form"] = form
        WinForms.Application.Run(form)
        done.set()

    thread = Thread(ThreadStart(launch))
    thread.SetApartmentState(ApartmentState.STA)
    thread.IsBackground = True
    thread.Start()
    while not done.wait(0.2):
        if stop and stop():
            form = holder.get("form")
            if form is not None:
                try:
                    form.BeginInvoke(Action(form.Close))
                except Exception:
                    pass
            return None
    if not box.get("jar"):
        return "", []
    return box.get("uin") or "", box["jar"]


def _captcha_ticket(aid, sid, uin, stop):
    """打开滑动验证，等用户划完。返回 (ticket, randstr)，取消时返回 None。"""
    page = _captcha_html(aid, sid, uin).encode("utf-8")
    box = {}
    done = threading.Event()

    class H(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            return

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if 0 < n <= 65536 else b""
            try:
                box["res"] = json.loads(raw.decode("utf-8"))
            except Exception:
                box["res"] = {}
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            done.set()

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        close = _captcha_form(f"http://127.0.0.1:{port}/")
    except Exception:
        log.exception("滑动验证窗口没打开")
        httpd.shutdown()
        httpd.server_close()
        return "", ""
    try:
        while not done.wait(0.2):
            if stop and stop():
                return None
        res = box.get("res") or {}
        ticket = str(res.get("ticket") or "")
        randstr = str(res.get("randstr") or res.get("randStr") or "")
        if res.get("ret") == 0 and ticket and randstr:
            return ticket, randstr
        return "", ""
    finally:
        close()
        httpd.shutdown()
        httpd.server_close()


def _saved_uin(uin):
    """这份票据里的 QQ。对不上或没有 skey 就当没登录。"""
    path = socket_keepalive.attack_cookie_file(uin)
    if not os.path.isfile(path):
        return ""
    qq = QQSession(path)
    got = str(qq.uin or "").strip()
    try:
        has_skey = bool(qq.session.cookies.get("skey"))
    except Exception:
        has_skey = False
    return got if has_skey and got == uin else ""


# 9/25 点选过的成就建筑。只有帝国大厦在抓包里对上了名字。
BUILDINGS = [
    (10138, "帝国大厦"),
    (10129, ""),
    (10126, ""),
    (10132, ""),
    (10135, ""),
    (10136, ""),
    (10133, ""),
    (10137, ""),
]


class _UiLog(logging.Handler):
    def __init__(self, q):
        super().__init__(logging.INFO)
        self.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))
        self.q = q

    def emit(self, record):
        self.q.put(("log", self.format(record)))


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("坦克风暴 · 打城")
        self.geometry("720x760")
        self.minsize(640, 620)
        self._q = queue.Queue()
        self._busy = False
        self._stop = threading.Event()
        self._cities = []
        self._photo = None
        self._http = None
        self._expires_at = ""
        self._remain_until = None
        self._config = cli.load_config()
        self._account_name, self._allowed = _locked()
        self._prio_rows = []
        self._scan_rows = []
        log.addHandler(_UiLog(self._q))

        gate = ttk.Frame(self, padding=(12, 12, 12, 0))
        gate.pack(fill="x")
        ttk.Label(gate, text="账号").grid(row=0, column=0, sticky="w")
        ttk.Label(gate, text=self._account_name or "未配置").grid(
            row=0, column=1, sticky="w", padx=(8, 0))
        ttk.Label(gate, text="密码").grid(row=0, column=2, sticky="w", padx=(8, 0))
        self.password = tk.StringVar()
        ttk.Entry(gate, textvariable=self.password, show="*", width=18).grid(
            row=0, column=3, sticky="w", padx=(8, 0))
        self.acct_btn = ttk.Button(gate, text="登录账号", command=self._login_account)
        self.acct_btn.grid(row=0, column=4, sticky="w", padx=(8, 0))
        self.acct_note = tk.StringVar(value="请输入密码，再点登录账号")
        ttk.Label(gate, textvariable=self.acct_note).grid(
            row=1, column=1, columnspan=4, sticky="w", padx=(8, 0), pady=(6, 0))
        ttk.Label(gate, text="QQ").grid(row=2, column=0, sticky="w", pady=(8, 0))
        self.qq_pick = tk.StringVar()
        self.qq_box = ttk.Combobox(
            gate, textvariable=self.qq_pick, state="readonly", width=16,
            values=self._allowed)
        self.qq_box.grid(row=2, column=1, sticky="w", padx=(8, 0), pady=(8, 0))
        self.qq_box.bind("<<ComboboxSelected>>", self._on_qq_pick)
        ttk.Label(gate, text="QQ密码").grid(row=2, column=2, sticky="w", padx=(8, 0), pady=(8, 0))
        self.qq_password = tk.StringVar()
        ttk.Entry(gate, textvariable=self.qq_password, show="*", width=18).grid(
            row=2, column=3, sticky="w", padx=(8, 0), pady=(8, 0))
        self.pwd_btn = ttk.Button(gate, text="密码登录", command=self._pwd_login)
        self.pwd_btn.grid(row=2, column=4, sticky="w", padx=(8, 0), pady=(8, 0))
        if self._allowed:
            self.qq_pick.set(self._allowed[0])

        top = ttk.Frame(self, padding=12)
        top.pack(fill="x")
        self.pick_label = tk.StringVar(value="城市")
        ttk.Label(top, textvariable=self.pick_label).grid(row=0, column=0, sticky="w")
        self.city = tk.StringVar()
        ent = ttk.Entry(top, textvariable=self.city)
        ent.grid(row=0, column=1, sticky="ew", padx=(8, 0))
        ent.bind("<KeyRelease>", self._filter)
        top.columnconfigure(1, weight=1)

        self.hits = tk.Listbox(top, height=6)
        self.hits.grid(row=1, column=1, sticky="ew", pady=(6, 0))
        self.hits.bind("<<ListboxSelect>>", self._pick)

        self.mode = tk.StringVar(value="atk")
        modes = ttk.Frame(top)
        modes.grid(row=2, column=1, sticky="w", pady=(10, 0))
        ttk.Radiobutton(modes, text="攻击这座城（人已在同城或相邻）",
                        variable=self.mode, value="atk",
                        command=self._on_mode).pack(anchor="w")
        ttk.Radiobutton(modes, text="先移动到这座城，路上的敌城和目标城都打",
                        variable=self.mode, value="move",
                        command=self._on_mode).pack(anchor="w")
        fund_row = ttk.Frame(modes)
        fund_row.pack(anchor="w")
        ttk.Radiobutton(fund_row, text="自动拨款",
                        variable=self.mode, value="fund",
                        command=self._on_mode).pack(side="left")
        ttk.Label(fund_row, text="次数").pack(side="left", padx=(8, 4))
        self.fund_times = tk.IntVar(value=1)
        ttk.Spinbox(fund_row, from_=1, to=30, width=4,
                    textvariable=self.fund_times).pack(side="left")
        self.sweep = tk.BooleanVar(value=True)
        ttk.Checkbutton(top, text="扫荡（只对「攻击这座城」生效，移动过去时固定扫荡）",
                        variable=self.sweep).grid(row=3, column=1, sticky="w", pady=(6, 0))
        cfg = ttk.Frame(top)
        cfg.grid(row=4, column=1, sticky="w", pady=(6, 0))
        ttk.Button(cfg, text="优先UID", command=self._edit_priority).pack(side="left")
        self.prio_note = tk.StringVar(value="还没配置")
        ttk.Label(cfg, textvariable=self.prio_note).pack(side="left", padx=(8, 16))
        ttk.Button(cfg, text="扫描城市", command=self._edit_scan).pack(side="left")
        self.scan_note = tk.StringVar(value="还没配置")
        ttk.Label(cfg, textvariable=self.scan_note).pack(side="left", padx=(8, 0))
        ttk.Label(cfg, text="成功间隔").pack(side="left", padx=(16, 4))
        self.hit_gap = tk.IntVar(value=6)
        ttk.Spinbox(cfg, from_=1, to=120, width=4, textvariable=self.hit_gap).pack(side="left")
        ttk.Label(cfg, text="秒").pack(side="left", padx=(4, 0))
        ttk.Label(cfg, text="恢复卡").pack(side="left", padx=(16, 4))
        self.card_max = tk.IntVar(value=1)
        ttk.Spinbox(cfg, from_=0, to=999, width=4, textvariable=self.card_max,
                    command=self._remember_cards).pack(side="left")
        ttk.Label(cfg, text="张").pack(side="left", padx=(4, 0))

        # 勾选项以后可以继续加每日任务。现在先放征战世界。
        self.do_war = tk.BooleanVar(value=False)
        war = ttk.Frame(top)
        war.grid(row=5, column=1, sticky="w", pady=(6, 0))
        ttk.Checkbutton(war, text="征战世界", variable=self.do_war).pack(side="left")
        ttk.Label(war, text="关卡").pack(side="left", padx=(8, 4))
        self.pve_stages = tk.StringVar()
        ttk.Entry(war, textvariable=self.pve_stages, width=24).pack(side="left")
        ttk.Label(war, text="最终关卡，留空用 config").pack(side="left", padx=(8, 0))

        bar = ttk.Frame(top)
        bar.grid(row=6, column=1, sticky="w", pady=(10, 0))
        self.login_btn = ttk.Button(bar, text="扫码登录", command=self._login)
        self.login_btn.pack(side="left")
        self.go_btn = ttk.Button(bar, text="开始", command=self._go)
        self.go_btn.pack(side="left", padx=(8, 0))
        self.stop_btn = ttk.Button(bar, text="停止", command=self._stop_wait)
        self.stop_btn.pack(side="left", padx=(8, 0))
        self.status = tk.StringVar(value="正在读城市目录…")
        ttk.Label(bar, textvariable=self.status).pack(side="left", padx=(12, 0))

        self.qr = ttk.Label(top)
        self.qr.grid(row=7, column=1, sticky="w", pady=(8, 0))

        self.logbox = tk.Text(self, height=16, wrap="word", state="disabled")
        self.logbox.pack(fill="both", expand=True, padx=12, pady=(0, 12))

        self.after(200, self._drain)
        threading.Thread(target=self._load_cities, daemon=True).start()
        self._read_plan(self._picked_qq())
        self._set_busy(False)
        self._refresh_login()

    def _load_cities(self):
        try:
            rows = citydb.list_cities()
        except Exception as exc:
            self._q.put(("log", f"城市目录没拉下来：{exc}"))
            self._ui(lambda: self.status.set("城市目录失败，仍可手填城市 ID"))
            return
        self._cities = [(int(i), name or "", n or "") for i, name, _, n in rows]
        self._ui(self._show_cities)

    def _on_mode(self):
        self.pick_label.set("建筑" if self.mode.get() == "fund" else "城市")
        self.city.set("")
        self._filter()

    def _catalog(self):
        if self.mode.get() == "fund":
            return [(i, name, "") for i, name in BUILDINGS]
        return self._cities

    def _show_cities(self):
        if self.mode.get() != "fund":
            self._filter()
        note = f"城市 {len(self._cities)} 座"
        cur = self.status.get()
        if note not in cur:
            self.status.set(f"{cur} · {note}" if cur else note)

    def _filter(self, _evt=None):
        key = self.city.get().strip()
        self.hits.delete(0, "end")
        shown = 0
        for cid, name, nation in self._catalog():
            line = f"{cid}  {name}  {nation}"
            if key and key not in line:
                continue
            self.hits.insert("end", line)
            shown += 1
            if shown >= 40:
                break

    def _pick(self, _evt=None):
        sel = self.hits.curselection()
        if not sel:
            return
        self.city.set(self.hits.get(sel[0]).split()[0])

    def _city_id(self):
        text = self.city.get().strip().split()
        if not text or not text[0].isdigit():
            return 0
        return int(text[0])

    def _on_qq_pick(self, _evt=None):
        self._switch_plan()
        self._refresh_login()

    def _plan_path(self, uin, kind):
        path = socket_keepalive.attack_cookie_file(uin)
        base = path[:-5] if path.endswith(".json") else path
        return base + "." + kind + ".json"

    def _write_plan(self, uin):
        if not uin:
            return
        path = self._plan_path(uin, "plan")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            try:
                cards = int(self.card_max.get())
            except (TypeError, ValueError, tk.TclError, AttributeError):
                cards = 1
            json.dump({"prio": self._prio_rows, "scan": self._scan_rows,
                       "cards": cards}, f, ensure_ascii=False)

    def _read_plan(self, uin):
        self._prio_rows = []
        self._scan_rows = []
        self._plan_uin = uin or ""
        path = self._plan_path(uin, "plan") if uin else ""
        if path and os.path.isfile(path):
            try:
                data = json.load(open(path, encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
            for row in data.get("prio") or []:
                if isinstance(row, (list, tuple)) and len(row) >= 2:
                    uid = str(row[0]).strip()
                    if uid.isdigit():
                        self._prio_rows.append((uid, int(row[1])))
            for row in data.get("scan") or []:
                if isinstance(row, (list, tuple)) and len(row) >= 3:
                    self._scan_rows.append((int(row[0]), int(row[1]), int(row[2])))
            if data.get("cards") is not None and hasattr(self, "card_max"):
                try:
                    self.card_max.set(max(0, int(data.get("cards"))))
                except (TypeError, ValueError, tk.TclError):
                    pass
        self._refresh_cfg_notes()

    def _remember_cards(self):
        self._write_plan(self._picked_qq())

    def _switch_plan(self):
        if getattr(self, "_plan_uin", ""):
            self._write_plan(self._plan_uin)
        self._read_plan(self._picked_qq())

    def _load_hits(self, uin):
        path = self._plan_path(uin, "hits") if uin else ""
        if not path or not os.path.isfile(path):
            return []
        try:
            data = json.load(open(path, encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return data if isinstance(data, list) else []

    def _save_hits(self, uin, hits):
        if not uin:
            return
        path = self._plan_path(uin, "hits")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(hits or [], f, ensure_ascii=False)

    def _priority(self):
        return {uid: rank for uid, rank in self._prio_rows}

    def _scan_spans(self):
        return [(city, start - 1, end - 1) for city, start, end in self._scan_rows]

    def _refresh_cfg_notes(self):
        n = len(self._prio_rows)
        self.prio_note.set(f"{n} 人" if n else "还没配置")
        n = len(self._scan_rows)
        self.scan_note.set(f"{n} 座" if n else "还没配置")

    def _edit_priority(self):
        win = tk.Toplevel(self)
        win.title("优先攻打 UID")
        win.transient(self)
        box = tk.Listbox(win, width=36, height=10)
        box.grid(row=0, column=0, columnspan=4, padx=12, pady=(12, 6), sticky="ew")
        for uid, rank in self._prio_rows:
            box.insert("end", f"{rank}  {uid}")
        tk.Label(win, text="UID").grid(row=1, column=0, padx=(12, 4), pady=6, sticky="e")
        uid_var = tk.StringVar()
        tk.Entry(win, textvariable=uid_var, width=18).grid(row=1, column=1, sticky="w")
        tk.Label(win, text="优先级").grid(row=1, column=2, padx=(8, 4))
        rank_var = tk.IntVar(value=1)
        tk.Spinbox(win, from_=1, to=99, width=4, textvariable=rank_var).grid(row=1, column=3, padx=(0, 12))
        note = tk.StringVar()
        tk.Label(win, textvariable=note).grid(row=2, column=0, columnspan=4, padx=12, sticky="w")

        def add():
            uid = uid_var.get().strip()
            if not uid.isdigit() or len(uid) > 32:
                note.set("UID 去掉空格后要是数字")
                return
            if any(old == uid for old, _ in self._prio_rows):
                note.set("这个 UID 已经在名单里")
                return
            if len(self._prio_rows) >= 50:
                note.set("最多 50 个")
                return
            try:
                rank = int(rank_var.get())
            except (TypeError, ValueError, tk.TclError):
                rank = 1
            if not 1 <= rank <= 99:
                note.set("优先级要是 1 到 99")
                return
            self._prio_rows.append((uid, rank))
            self._prio_rows.sort(key=lambda row: (row[1], row[0]))
            box.delete(0, "end")
            for item_uid, item_rank in self._prio_rows:
                box.insert("end", f"{item_rank}  {item_uid}")
            uid_var.set("")
            note.set("")
            self._write_plan(self._picked_qq())
            self._refresh_cfg_notes()

        def drop():
            sel = box.curselection()
            if not sel:
                return
            del self._prio_rows[sel[0]]
            box.delete(sel[0])
            self._write_plan(self._picked_qq())
            self._refresh_cfg_notes()

        tk.Button(win, text="添加", command=add).grid(row=3, column=1, pady=(0, 12), sticky="w")
        tk.Button(win, text="删除选中", command=drop).grid(row=3, column=2, pady=(0, 12), sticky="w")

    def _edit_scan(self):
        win = tk.Toplevel(self)
        win.title("扫描城市")
        win.transient(self)
        box = tk.Listbox(win, width=42, height=8)
        box.grid(row=0, column=0, columnspan=6, padx=12, pady=(12, 6), sticky="ew")
        for city_id, start, end in self._scan_rows:
            box.insert("end", f"{city_id}  第 {start} 页到第 {end} 页")
        tk.Label(win, text="城市").grid(row=1, column=0, padx=(12, 4), pady=6, sticky="e")
        options = [f"{cid}  {name}  {nation}".strip() for cid, name, nation in self._cities]
        city_var = tk.StringVar()
        current = str(self._city_id() or "")
        for line in options:
            if line.split()[:1] == [current]:
                city_var.set(line)
                break
        ttk.Combobox(
            win, textvariable=city_var, width=22, values=options,
            state="readonly" if options else "normal").grid(row=1, column=1, sticky="w")
        tk.Label(win, text="起始页").grid(row=1, column=2, padx=(8, 4))
        start_var = tk.IntVar(value=1)
        tk.Spinbox(win, from_=1, to=999, width=4, textvariable=start_var).grid(row=1, column=3)
        tk.Label(win, text="终止页").grid(row=1, column=4, padx=(8, 4))
        end_var = tk.IntVar(value=5)
        tk.Spinbox(win, from_=1, to=999, width=4, textvariable=end_var).grid(row=1, column=5, padx=(0, 12))
        note = tk.StringVar()
        tk.Label(win, textvariable=note).grid(row=2, column=0, columnspan=6, padx=12, sticky="w")

        def add():
            text = city_var.get().strip().split()
            if not text or not text[0].isdigit():
                note.set("先选择城市")
                return
            city_id = int(text[0])
            if city_id <= 0:
                note.set("城市 ID 要大于 0")
                return
            if any(old == city_id for old, _, _ in self._scan_rows):
                note.set("这座城已经在名单里")
                return
            if len(self._scan_rows) >= 6:
                note.set("最多 6 座城")
                return
            try:
                start = int(start_var.get())
                end = int(end_var.get())
            except (TypeError, ValueError, tk.TclError):
                note.set("页码要是数字")
                return
            if start < 1 or end < 1:
                note.set("页码从 1 起")
                return
            if end < start:
                note.set("终止页不能小于起始页")
                return
            if end - start > 4:
                note.set("一座城最多连续 5 页")
                return
            self._scan_rows.append((city_id, start, end))
            box.insert("end", f"{city_id}  第 {start} 页到第 {end} 页")
            note.set("")
            self._write_plan(self._picked_qq())
            self._refresh_cfg_notes()

        def drop():
            sel = box.curselection()
            if not sel:
                return
            del self._scan_rows[sel[0]]
            box.delete(sel[0])
            self._write_plan(self._picked_qq())
            self._refresh_cfg_notes()

        tk.Button(win, text="添加", command=add).grid(row=3, column=1, pady=(0, 12), sticky="w")
        tk.Button(win, text="删除选中", command=drop).grid(row=3, column=3, pady=(0, 12), sticky="w")

    def _account_ok(self):
        if self._http is None or not self._account_name:
            return None
        if self._remain_until is not None and time.monotonic() >= self._remain_until:
            self._http = None
            return None
        return {"username": self._account_name, "expires_at": self._expires_at}

    def _apply_clock(self, body):
        self._expires_at = str((body or {}).get("expires_at") or "")
        if (body or {}).get("unlimited") or (body or {}).get("remaining_ms") is None:
            self._remain_until = None
            return
        left = int((body or {}).get("remaining_ms") or 0)
        self._remain_until = time.monotonic() + max(0, left) / 1000.0

    def _clock_failed(self, why):
        if why in ("请先登录账号", "账号已过期"):
            self._http = None
            self._remain_until = 0
        self._set_busy(False)
        self._refresh_login()
        self.status.set(why)

    def _picked_qq(self):
        uin = self.qq_pick.get().strip()
        return uin if uin in self._allowed else ""

    def _renew_expiry(self):
        """再向服务器要一次最新有效期。过期或失败时返回说明。"""
        why, clock = _fetch_clock(self._http)
        if why:
            return why
        if not clock or clock.get("expired"):
            return "账号已过期"
        self._apply_clock(clock)
        return ""

    def _refresh_login(self):
        if not _server():
            self.acct_note.set("这个版本编译时没有写入服务器")
            self.status.set("这个版本编译时没有写入服务器")
            return
        if not self._account_name:
            self.acct_note.set("这个版本编译时没有写入登录账号")
            self.status.set("这个版本编译时没有写入登录账号")
            return
        if not self._allowed:
            self.acct_note.set("这个版本编译时没有写入指定 QQ")
            self.status.set("这个版本编译时没有写入指定 QQ")
            return
        user = self._account_ok()
        uin = self._picked_qq()
        if not user:
            self.acct_note.set(f"请输入 {self._account_name} 的密码，再点登录账号")
            self.status.set(f"请登录账号 {self._account_name}")
            return
        exp = self._expires_at
        tail = f"有效期至 {exp}" if exp else "不限期"
        self.acct_note.set(f"已登录 {user['username']} · {tail}")
        qq_note = f"QQ {uin} 已登录" if uin and _saved_uin(uin) == uin else f"QQ {uin or '未选'} 未登录"
        self.status.set(qq_note)

    def _set_busy(self, busy):
        self._busy = busy
        if not busy:
            self._stop.clear()
        if busy:
            for btn in (self.go_btn, self.login_btn, self.pwd_btn, self.acct_btn):
                btn.configure(state="disabled")
            self.qq_box.configure(state="disabled")
            self.stop_btn.configure(state="normal")
            return
        self.stop_btn.configure(state="disabled")
        self.acct_btn.configure(state="normal" if self._account_name and _server() else "disabled")
        self.qq_box.configure(state="readonly" if self._allowed else "disabled")
        state = "normal" if self._account_ok() else "disabled"
        self.go_btn.configure(state=state)
        self.login_btn.configure(state=state)
        self.pwd_btn.configure(state=state)

    def _stop_wait(self):
        self._stop.set()
        self.status.set("正在停止…")

    def _login_account(self):
        if self._busy:
            return
        if not _server():
            self.status.set("这个版本编译时没有写入服务器")
            return
        if not self._account_name:
            self.status.set("这个版本编译时没有写入登录账号")
            return
        if not self._allowed:
            self.status.set("这个版本编译时没有写入指定 QQ")
            return
        password = self.password.get()
        self._set_busy(True)
        self.status.set("正在向服务器登录…")

        def work():
            why, session, clock = _login_remote(self._account_name, password)
            self._ui(lambda w=why, s=session, c=clock: self._login_account_done(w, s, c))

        threading.Thread(target=work, daemon=True).start()

    def _login_account_done(self, why, session, clock):
        if why or session is None:
            self._http = None
            self._remain_until = 0 if why == "账号已过期" else None
            self._set_busy(False)
            self._refresh_login()
            self.status.set(why or "登录没有完成")
            return
        self._http = session
        self._apply_clock(clock)
        self.password.set("")
        self._set_busy(False)
        self._refresh_login()

    def _login(self):
        if self._busy:
            return
        expect = self._picked_qq()
        if not self._account_ok():
            self.status.set("请先登录账号")
            return
        if not expect:
            self.status.set("先从指定 QQ 里选一个")
            return
        self._stop.clear()
        self._set_busy(True)
        self.status.set(f"等待 QQ {expect} 扫码…")

        def work():
            why = self._renew_expiry()
            if why:
                self._ui(lambda w=why: self._clock_failed(w))
                return
            path = socket_keepalive.attack_cookie_file(expect)
            qr = (path[:-5] if path.endswith(".json") else path) + ".qrcode.png"
            os.makedirs(os.path.dirname(path), exist_ok=True)
            qq = QQSession(path, qrcode_file=qr)
            qq.apply_config_platform(self._config)
            qq.use_napcat = False
            qq.attack_account = True

            def on_qr(qr_path, pushed=False):
                self._ui(lambda p=qr_path, z=pushed: self._show_qr(p, z))

            ok = qq.qr_login(on_qr=on_qr, push_uin=expect, stop=self._stop.is_set)
            scanned = str(qq.uin or "").strip()
            if ok and scanned != expect:
                log.error("扫到的 QQ 是 %s，配置允许的是 %s", scanned, expect)
                try:
                    os.remove(path)
                except OSError:
                    pass
                ok = False
            self._ui(lambda o=ok, u=scanned, want=expect: self._login_done(o, u, want))

        threading.Thread(target=work, daemon=True).start()

    def _show_qr(self, path, pushed):
        try:
            self._photo = tk.PhotoImage(file=path)
            self.qr.configure(image=self._photo)
        except tk.TclError as exc:
            log.info("二维码打不开：%s", exc)
        self.status.set("请在手机 QQ 点确认" if pushed else "用另一台设备扫窗口里的码")

    def _login_done(self, ok, uin, expect):
        stopped = self._stop.is_set()
        self._stop.clear()
        self._set_busy(False)
        self.qr.configure(image="")
        if stopped and not ok:
            self.status.set("已停止")
            return
        if ok and uin == expect:
            self.status.set(f"已登录 QQ {uin}，填写城市后点开始")
            return
        if uin and uin != expect:
            self.status.set(f"扫到的是 {uin}，只能用配置里的 {expect}")
            return
        self.status.set(f"QQ {expect} 登录没有完成")

    def _pwd_login(self):
        if self._busy:
            return
        expect = self._picked_qq()
        if not self._account_ok():
            self.status.set("请先登录账号")
            return
        if not expect:
            self.status.set("先从指定 QQ 里选一个")
            return
        typed = self.qq_password.get()
        if typed:
            _save_qq_password(expect, typed)
        password = typed or _load_qq_password(expect)
        self._stop.clear()
        self._set_busy(True)
        self.status.set("已填入记下的 QQ 密码" if password and not typed
                        else "请在 QQ游戏大厅登录窗口里输入密码")

        def work():
            why = self._renew_expiry()
            if why:
                self._ui(lambda w=why: self._clock_failed(w))
                return
            try:
                got = _hall_login(expect, self._stop.is_set, password)
            except Exception as exc:
                log.exception("游戏大厅登录窗没打开")
                self._ui(lambda e=exc: self._pwd_done(False, "", f"登录窗没打开：{e}", expect))
                return
            if got is None or self._stop.is_set():
                self._ui(lambda want=expect: self._login_done(False, "", want))
                return
            scanned, jar = got
            if not jar:
                self._ui(lambda: self._pwd_done(False, scanned, "登录没有完成", expect))
                return
            if scanned != expect:
                self._ui(lambda u=scanned, want=expect: self._pwd_done(
                    False, u, f"QQ不正确。登录的是 {u}，当前选的是 {want}", want))
                return
            path = socket_keepalive.attack_cookie_file(expect)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            qq = QQSession(path)
            qq.use_napcat = False
            qq.attack_account = True
            qq.session.cookies.clear()
            for item in jar:
                qq.session.cookies.set(
                    item["name"], item["value"],
                    domain=item["domain"], path=item["path"], expires=item["expires"])
            from tankstorm import PLAT_HALL
            qq.set_platform(PLAT_HALL)
            qq._save_cookies()
            ok = str(qq.uin or "").strip() == expect
            if not ok:
                try:
                    os.remove(path)
                except OSError:
                    pass
            if ok:
                def keep():
                    try:
                        socket_keepalive.run(qq, self._config)
                    except Exception:
                        log.exception("保活结束")
                threading.Thread(target=keep, daemon=True).start()
            self._ui(lambda o=ok, u=scanned, want=expect: self._pwd_done(
                o, u, "" if o else "登录的 QQ 和指定的不一致", want))

        threading.Thread(target=work, daemon=True).start()

    def _pwd_done(self, ok, uin, msg, expect):
        self._set_busy(False)
        if ok and uin == expect:
            self.qq_password.set("")
            self.status.set(f"已登录 QQ {uin}，登录窗已收起，保活已接上")
            return
        self.status.set(msg or "密码登录没有完成")

    def _go(self):
        if self._busy:
            return
        uin = self._picked_qq()
        if not self._account_ok():
            self.status.set("请先登录账号")
            self._set_busy(False)
            return
        if not uin or _saved_uin(uin) != uin:
            self.status.set("先用指定的 QQ 扫码登录")
            return
        target = self._city_id()
        mode = self.mode.get()
        do_city = mode in ("atk", "move")
        do_fund = mode == "fund"
        do_war = bool(self.do_war.get())
        if do_fund and not target:
            self.status.set("先填写建筑 ID，或从列表里点一座")
            return
        priority = self._priority() if do_city else {}
        scan_spans = self._scan_spans() if priority else []
        if do_city and not target and not scan_spans and not do_war:
            self.status.set("先填写城市 ID，或从列表里点一座")
            return
        if priority and not scan_spans:
            self.status.set("先配置扫描城市，最多 6 座")
            return
        if do_city and not target:
            do_city = False
        if not do_city and not do_fund and not do_war:
            self.status.set("先填写城市，或勾选征战世界")
            return
        sweep = bool(self.sweep.get())
        try:
            times = int(self.fund_times.get())
        except (TypeError, ValueError, tk.TclError):
            times = 1
        try:
            hit_gap = int(self.hit_gap.get())
        except (TypeError, ValueError, tk.TclError):
            hit_gap = 6
        hit_gap = min(120, max(1, hit_gap))
        self._set_busy(True)
        self.status.set("执行中…")

        def work():
            why = self._renew_expiry()
            if why:
                self._ui(lambda w=why: self._clock_failed(w))
                return
            codes = []
            try:
                path = socket_keepalive.attack_cookie_file(uin)
                qq = QQSession(path)
                if str(qq.uin or "").strip() != uin:
                    self._ui(lambda: self._done("这个登录态不是指定的 QQ"))
                    return
                qq.apply_config_platform(self._config)
                qq.use_napcat = False
                qq.attack_account = True
                citydb.set_attack_context(0, uin)
                config = self._config
                try:
                    cards = max(0, int(self.card_max.get()))
                except (TypeError, ValueError, tk.TclError):
                    cards = 1
                war = config.setdefault("国战", {})
                war["自动使用国战恢复卡"] = cards > 0
                war["单次最多用几张恢复卡"] = cards
                if do_fund:
                    codes.append(socket_keepalive.run_fund_once(
                        qq, config, target, times))
                elif priority and scan_spans:
                    brief = "、".join(
                        f"{city} 第{start + 1}-{end + 1}页" for city, start, end in scan_spans)
                    log.info("清空 %s，同时扫描 %s。上次匹配 %d 人先打",
                             target or "不指定城", brief, len(self._load_hits(uin)))
                    result = socket_keepalive.run_priority_watch(
                        qq, config, scan_spans, priority,
                        sweep or mode == "move", self._stop.is_set, hit_gap,
                        target if do_city else 0, mode == "move", self._load_hits(uin))
                    if isinstance(result, tuple):
                        codes.append(result[0])
                        self._save_hits(uin, result[1])
                    else:
                        codes.append(result)
                elif do_city:
                    if mode == "move":
                        codes.append(socket_keepalive.run_move_once(
                            qq, config, target, sweep=True))
                    else:
                        codes.append(socket_keepalive.run_farm_city_once(
                            qq, config, target, sweep=sweep))
                if do_war:
                    codes.append(socket_keepalive.run_pve_once(
                        qq, config, self.pve_stages.get().strip()))
                tail = "完成" if codes and not any(codes) else f"结束，代码 {codes}"
            except Exception as exc:
                log.exception("执行失败")
                tail = f"失败：{exc}"
            self._ui(lambda t=tail: self._done(t))

        threading.Thread(target=work, daemon=True).start()

    def _done(self, text):
        self._set_busy(False)
        self.status.set(text)

    def _ui(self, fn):
        self._q.put(("ui", fn))

    def _drain(self):
        while True:
            try:
                kind, payload = self._q.get_nowait()
            except queue.Empty:
                break
            if kind == "ui":
                payload()
                continue
            self.logbox.configure(state="normal")
            self.logbox.insert("end", payload + "\n")
            self.logbox.see("end")
            self.logbox.configure(state="disabled")
        self.after(200, self._drain)


def main():
    for name in ("config.json", "endpoints.json", "protocol.json"):
        cli.paths.ensure_user_copy(name)
    App().mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
