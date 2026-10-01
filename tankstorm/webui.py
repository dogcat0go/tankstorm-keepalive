# tankstorm-keepalive  Copyright (C) 2026 Dimlitter
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# 本程序是自由软件：你可以依据自由软件基金会发布的 GNU Affero 通用公共许可证
# （第 3 版，或你选择的任何更新版本）之条款，再分发和/或修改它。
# 本程序希望能有用，但不提供任何担保；甚至不含适销性或特定用途适用性的默示担保。
# 详见随附的 LICENSE 文件，或 <https://www.gnu.org/licenses/>。
"""订阅页面：读本机 city_players.db，看某个 UID 在不在某座城里。

页面和库在同一台机器上。自己的电脑用 SSH 转到这个端口再打开，
不必把数据库文件拷出来，也不必对公网开放。
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import citydb
from .log import get_logger

log = get_logger()

_PAGE = """<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>城市订阅</title>
<style>
  body { margin: 0; font: 15px/1.5 sans-serif; color: #1a1a1a; background: #f6f6f4; }
  main { max-width: 880px; margin: 0 auto; padding: 24px 16px 48px; }
  h1 { font-size: 22px; margin: 0 0 8px; }
  p { margin: 0 0 16px; color: #444; }
  form { display: flex; flex-wrap: wrap; gap: 8px; align-items: end; margin-bottom: 8px; }
  label { display: flex; flex-direction: column; gap: 4px; font-size: 13px; color: #333; }
  input { font: inherit; padding: 8px 10px; border: 1px solid #bbb; border-radius: 6px; background: #fff; }
  input[name=city] { width: 8em; }
  input[name=uid] { width: 18em; }
  button { font: inherit; padding: 8px 14px; border: 0; border-radius: 6px; background: #1a1a1a; color: #fff; cursor: pointer; }
  button.ghost { background: transparent; color: #333; border: 1px solid #bbb; }
  .err { color: #9b1c1c; min-height: 1.5em; }
  table { width: 100%; border-collapse: collapse; background: #fff; }
  th, td { text-align: left; padding: 10px 8px; border-bottom: 1px solid #e6e6e6; vertical-align: top; }
  th { font-size: 13px; color: #555; font-weight: 600; }
  .on { color: #0b6b2f; font-weight: 700; }
  .off { color: #666; }
  code { font-size: 13px; }
  .muted { color: #777; font-size: 13px; }
</style>
<main>
  <h1>城市订阅</h1>
  <p>看某个用户 UID 现在在不在某座城里。数字来自这台服务器上的 <code id="db"></code>。
  另开一个进程跑 <code>python3 main.py --watch-cities</code>，订阅的城会加进刷新名单。
  人离开要等这座城被完整翻完页，记录才会消失。</p>
  <form id="f">
    <label>城市 ID<input name="city" inputmode="numeric" required placeholder="1201"></label>
    <label>用户 UID<input name="uid" inputmode="numeric" required placeholder="玩家 UID"></label>
    <button type="submit">订阅</button>
  </form>
  <div class="err" id="err"></div>
  <table>
    <thead><tr><th>城市</th><th>UID</th><th>昵称</th><th>状态</th><th>记录时间</th><th></th></tr></thead>
    <tbody id="rows"></tbody>
  </table>
  <p class="muted" id="empty">还没有订阅。</p>
</main>
<script>
const err = document.getElementById("err");
const rows = document.getElementById("rows");
const empty = document.getElementById("empty");
function esc(s) { return s == null ? "" : String(s); }
async function load() {
  const r = await fetch("/api/subs");
  const data = await r.json();
  document.getElementById("db").textContent = data.db || "";
  rows.replaceChildren();
  const items = data.items || [];
  empty.hidden = items.length > 0;
  for (const it of items) {
    const tr = document.createElement("tr");
    const city = document.createElement("td");
    city.textContent = (it.city_name ? it.city_name + " " : "") + it.city_id;
    const uid = document.createElement("td");
    uid.textContent = it.uid;
    const name = document.createElement("td");
    name.textContent = it.name || "—";
    const st = document.createElement("td");
    st.textContent = it.present ? "在城里" : (it.city_scanned_at ? "不在这座城" : "这座城还没扫过");
    st.className = it.present ? "on" : "off";
    const when = document.createElement("td");
    when.textContent = it.present ? (it.seen_at || "—") : (it.city_scanned_at || "—");
    const op = document.createElement("td");
    const b = document.createElement("button");
    b.type = "button";
    b.className = "ghost";
    b.textContent = "取消";
    b.onclick = async () => {
      await fetch("/api/subs/delete", {method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({city_id: it.city_id, uid: it.uid})});
      load();
    };
    op.appendChild(b);
    tr.append(city, uid, name, st, when, op);
    rows.appendChild(tr);
  }
}
document.getElementById("f").onsubmit = async (e) => {
  e.preventDefault();
  err.textContent = "";
  const fd = new FormData(e.target);
  const r = await fetch("/api/subs", {method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({city_id: fd.get("city"), uid: fd.get("uid")})});
  const data = await r.json().catch(() => ({}));
  if (!r.ok) { err.textContent = data.error || "没加上"; return; }
  e.target.reset();
  load();
};
load();
setInterval(load, 4000);
</script>
"""


def _json(handler, code, obj):
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _read_json(handler):
    n = int(handler.headers.get("Content-Length") or 0)
    if n > 4096:
        raise ValueError("内容太长")
    raw = handler.rfile.read(n) if n else b""
    if not raw:
        return {}
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("需要 JSON 对象")
    return data


def _pair(data):
    try:
        city_id = int(str(data.get("city_id", "")).strip())
    except (TypeError, ValueError):
        raise ValueError("城市 ID 要是数字") from None
    uid = str(data.get("uid", "")).strip()
    if city_id <= 0:
        raise ValueError("城市 ID 要大于 0")
    if not uid.isdigit() or len(uid) > 32:
        raise ValueError("UID 要是数字")
    return city_id, uid


def _handler():
    page = _PAGE.encode("utf-8")

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/":
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(page)))
                self.end_headers()
                self.wfile.write(page)
                return
            if path == "/api/subs":
                try:
                    items = citydb.list_watches()
                except Exception as exc:
                    _json(self, 500, {"error": str(exc)})
                    return
                _json(self, 200, {"db": citydb.DB_FILE, "items": items})
                return
            self.send_error(404)

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            try:
                data = _read_json(self)
                city_id, uid = _pair(data)
            except (ValueError, json.JSONDecodeError) as exc:
                _json(self, 400, {"error": str(exc) or "格式不对"})
                return
            try:
                if path == "/api/subs":
                    citydb.add_watch(city_id, uid)
                    _json(self, 200, {"ok": True})
                elif path == "/api/subs/delete":
                    citydb.remove_watch(city_id, uid)
                    _json(self, 200, {"ok": True})
                else:
                    self.send_error(404)
            except Exception as exc:
                _json(self, 500, {"error": str(exc)})

        def log_message(self, fmt, *args):
            return

    return H


def _announce(host, port):
    log.info("订阅页面 http://%s:%d/    库 %s", host, port, citydb.DB_FILE)
    if host in ("127.0.0.1", "localhost"):
        log.info("在自己的电脑上执行 ssh -L %d:127.0.0.1:%d 用户@这台服务器，"
                 "然后打开 http://127.0.0.1:%d/", port, port, port)
    else:
        log.info("页面监听在 %s，能访问这个地址的人都能看订阅、也能改订阅", host)

    def warm():
        try:
            citydb.ensure_catalog()
        except Exception as exc:
            log.info("城市目录暂不可用：%s", exc)

    threading.Thread(target=warm, name="city-catalog", daemon=True).start()


def _server(host, port):
    class _HTTP(ThreadingHTTPServer):
        allow_reuse_address = True

    httpd = _HTTP((host, int(port)), _handler())
    httpd.daemon_threads = True
    return httpd


def start(host="127.0.0.1", port=8765):
    """给保活进程挂一个后台页面。进程退出时页面一起停。"""
    httpd = _server(host, port)
    threading.Thread(target=httpd.serve_forever, name="city-web",
                     daemon=True).start()
    _announce(host, port)
    return httpd


def serve(host="127.0.0.1", port=8765) -> int:
    httpd = _server(host, port)
    _announce(host, port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("订阅页面已停止")
    finally:
        httpd.server_close()
    return 0
