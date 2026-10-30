# tankstorm-keepalive  Copyright (C) 2026 Dimlitter
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# 本程序是自由软件：你可以依据自由软件基金会发布的 GNU Affero 通用公共许可证
# （第 3 版，或你选择的任何更新版本）之条款，再分发和/或修改它。
# 本程序希望能有用，但不提供任何担保；甚至不含适销性或特定用途适用性的默示担保。
# 详见随附的 LICENSE 文件，或 <https://www.gnu.org/licenses/>。
"""登录后，还原游戏客户端启动时拿到的全部参数（socket 登录鉴权要用）。

坦克风暴的真实加载链路（已由抓包确认）：

  QQ 空间：
  game.qzone.qq.com/100616028   ← 带 QQ 登录 cookie 请求，Qzone 当场签发 openid/openkey
        │  HTML 里： <iframe data-src="https://tankstorm-qzone.sincetimes.com/?openid=..&openkey=..&pf=qzone&pfkey=..">
        ▼
  tankstorm-qzone.sincetimes.com/?openid=..&openkey=..   ← 游戏外框页

  QQ 游戏大厅：
  ptlogin aid=7000201 daid=207，s_url 为大厅 app_frame
        │
  app.lobby.minigame.qq.com/cgi-bin/qqgame_launchapp?appid=10407&pf=qqgame
        │  JSON：openid/openkey/jump_url
        ▼
  jump_url（通常是 tankstorm-qqgame.sincetimes.com/?openid=..&pf=qqgame）

        │  HTML 里： <param name="FlashVars" value="openid=..&openkey=..&uid=..&secret=..&server=tankstorm-proxy.sincetimes.com&region=18&port=8001&sid=..&pfkey=..&version=..&level=..">
        ▼
  RedWar.swf 用这些参数连 socket：tankstorm-proxy.sincetimes.com:8001

因此本模块两步走：抓 iframe / jump_url → 抓 FlashVars，产出 socket 登录所需的完整 ctx。
openid/openkey 每次都是新鲜签发的，所以保活守护进程可反复调用本函数拿最新票据。
"""

import json
import os
import re
from urllib.parse import parse_qsl, urljoin, urlparse

from . import APPID, HALL_APPID, PLAT_HALL, platform_spec
from .log import get_logger

log = get_logger()

from . import paths                               # noqa: E402

BASE_DIR = paths.app_dir()
DEBUG_DIR = paths.user_path("debug")              # 抓下来的页面，属可写数据

CANVAS_HOST = "tankstorm-qzone.sincetimes.com"

# 外层页里的游戏 iframe 地址
_IFRAME_RE = re.compile(
    r'(?:data-src|src)=["\'](https?://tankstorm[^"\']*sincetimes\.com/\?[^"\']+)["\']',
    re.I)
_CANVAS_URL_JS_RE = re.compile(
    r'canvas_url["\']?\s*[:=]\s*["\'](https?://tankstorm[^"\']*sincetimes\.com[^"\']*)',
    re.I)
# iframe 页里传给 SWF 的 FlashVars
_FLASHVARS_RE = re.compile(
    r'(?:FlashVars["\']?\s*(?:value=)?["\']|flashvars\s*=\s*["\'])([^"\']+)', re.I)

# socket 登录关心的字段（缺失不报错，按需使用）
WANTED = ("openid", "openkey", "uid", "secret", "sid", "region", "pf", "pfkey",
          "server", "port", "port1", "version", "level", "coinserver",
          "storageURL", "firstLogin", "lang")

_HALL_LAUNCH = (
    "https://app.lobby.minigame.qq.com/cgi-bin/qqgame_launchapp",
    "https://lobby.minigame.qq.com/cgi-bin/qqgame_launchapp",
)


def _dump(name: str, text: str) -> None:
    os.makedirs(DEBUG_DIR, exist_ok=True)
    with open(os.path.join(DEBUG_DIR, name), "w", encoding="utf-8", errors="replace") as f:
        f.write(text)


def parse_iframe_url(html: str) -> str | None:
    """从外层游戏页 HTML 里取出 tankstorm iframe 的完整 URL。"""
    m = _IFRAME_RE.search(html)
    if m:
        return m.group(1).replace("&amp;", "&")
    return None


def parse_flashvars(html: str) -> dict:
    """从 iframe 页 HTML 里解析 FlashVars（key=value&...）为 dict。"""
    m = _FLASHVARS_RE.search(html)
    if not m:
        return {}
    fv = m.group(1).replace("&amp;", "&")
    return dict(parse_qsl(fv, keep_blank_values=True))


def extract_context(outer_html: str, iframe_html: str) -> dict:
    """纯解析（不发请求），供离线测试。合并 iframe URL 参数 + FlashVars。"""
    ctx = {}
    iframe_url = parse_iframe_url(outer_html)
    if iframe_url:
        ctx["canvas_url"] = iframe_url
        ctx.update(dict(parse_qsl(urlparse(iframe_url).query, keep_blank_values=True)))
    ctx.update(parse_flashvars(iframe_html))   # FlashVars 更全，优先
    return ctx


def _parse_jsonish(text: str) -> dict:
    raw = (text or "").strip()
    if not raw:
        return {}
    if raw.startswith("{") or raw.startswith("["):
        try:
            data = json.loads(raw)
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def hall_logged_in(data: dict) -> bool:
    """大厅 launchapp 是否表示当前 cookie 仍被认。"""
    if not data or data.get("_error"):
        return False
    if data.get("result") in (1000005, "1000005"):
        return False
    if str(data.get("SeedErr") or "") == "No Login":
        return False
    if data.get("result") == 0 and data.get("openid"):
        return True
    return bool(data.get("jump_url") and data.get("openid"))


def fetch_hall_launch(session, *, referer: str = "", appid: str = "") -> dict:
    """向大厅 launchapp 要 openid/openkey/jump_url。未登录时返回 No Login。"""
    spec = platform_spec(PLAT_HALL)
    referer = referer or spec["game_url"]
    last = {}
    wanted = [appid] if appid else [HALL_APPID, APPID]
    seen = []
    for aid in wanted:
        if aid in seen:
            continue
        seen.append(aid)
        params = {"type": "json", "appid": aid, "pf": "qqgame"}
        headers = {
            "Referer": referer,
            "Origin": "https://minigame.qq.com",
        }
        for base in _HALL_LAUNCH:
            try:
                r = session.get(base, params=params, headers=headers, timeout=20)
                data = _parse_jsonish(r.text)
            except Exception as exc:
                last = {"_error": str(exc)}
                continue
            if not data:
                if not last:
                    last = {"_error": f"HTTP {r.status_code}"}
                continue
            last = data
            if hall_logged_in(data) or data.get("jump_url") or data.get("openid"):
                return data
    return last


def get_game_context(qq) -> dict:
    """在线版：真正发请求，返回 socket 登录所需的完整 ctx。qq 为已登录 QQSession。"""
    lock = getattr(qq, "_http_lock", None)
    if lock is None:
        return _fetch_game_context(qq)
    with lock:
        return _fetch_game_context(qq)


def _fetch_game_context(qq) -> dict:
    if getattr(qq, "platform", None) == PLAT_HALL:
        return _fetch_hall_context(qq)
    return _fetch_qzone_context(qq)


def _base_ctx(qq) -> dict:
    s = qq.session
    return {"uin": qq.uin, "g_tk": str(qq.g_tk), "skey": s.cookies.get("skey") or ""}


def _fill_from_canvas(qq, ctx: dict, canvas_url: str, referer: str) -> dict:
    canvas_url = urljoin(referer, canvas_url)
    ctx["canvas_url"] = canvas_url
    ctx.update(dict(parse_qsl(urlparse(canvas_url).query, keep_blank_values=True)))
    log.info("游戏外框: %s", canvas_url.split("?")[0])
    try:
        r2 = qq.session.get(canvas_url, headers={"Referer": referer}, timeout=20)
        _dump("2_iframe.html", r2.text)
        fv = parse_flashvars(r2.text)
        ctx.update(fv)
        if not str(ctx.get("region") or "").strip():
            found = re.search(r"[?&]region=(\d+)", r2.text)
            if found:
                ctx["region"] = found.group(1)
        if not ctx.get("openid"):
            nested = parse_iframe_url(r2.text)
            if nested:
                ctx.update(dict(parse_qsl(urlparse(nested).query, keep_blank_values=True)))
        if fv or ctx.get("region"):
            log.info("已解析 FlashVars：server=%s port=%s uid=%s sid=%s region=%s",
                     ctx.get("server"), ctx.get("port"), ctx.get("uid"),
                     ctx.get("sid"), ctx.get("region"))
        else:
            log.warning("未在 iframe 页找到 FlashVars（HTML 已存 debug/2_iframe.html）")
    except Exception as exc:
        log.warning("请求 iframe 页失败: %s", exc)
    return ctx


def _ready_log(ctx: dict) -> dict:
    have = [k for k in ("openid", "openkey", "uid", "secret", "sid", "server", "port")
            if ctx.get(k)]
    log.info("socket 登录参数就绪: %s", ", ".join(have) if have else "（不足，见 debug/）")
    return ctx


def _fetch_qzone_context(qq) -> dict:
    s = qq.session
    ctx = _base_ctx(qq)
    game_url = getattr(qq, "game_url", None) or platform_spec("空间")["game_url"]

    log.info("打开游戏页，获取 openid/openkey…")
    r = s.get(game_url, timeout=20)
    _dump("1_outer.html", r.text)
    iframe_url = parse_iframe_url(r.text)
    if not iframe_url:
        m = _CANVAS_URL_JS_RE.search(r.text)
        log.warning("未从外层页解析到游戏 iframe 地址（HTML 已存 debug/1_outer.html）")
        if not m:
            return ctx
        iframe_url = m.group(1)
    _fill_from_canvas(qq, ctx, iframe_url, game_url)
    return _ready_log(ctx)


def _fetch_hall_context(qq) -> dict:
    ctx = _base_ctx(qq)
    spec = platform_spec(PLAT_HALL)
    log.info("打开 QQ 游戏大厅，获取 openid/openkey…")
    iframe_url = ""
    try:
        r = qq.session.get(spec["game_url"], timeout=20)
        _dump("1_outer.html", r.text)
        iframe_url = parse_iframe_url(r.text) or ""
        if not iframe_url:
            m = _CANVAS_URL_JS_RE.search(r.text)
            if m:
                iframe_url = m.group(1)
    except Exception as exc:
        log.warning("打开大厅游戏页失败: %s", exc)
    data = fetch_hall_launch(qq.session, referer=spec["game_url"])
    _dump("1_launchapp.json", json.dumps(data, ensure_ascii=False, indent=2))
    if not hall_logged_in(data) and not data.get("jump_url") and not iframe_url:
        why = data.get("resultstr") or data.get("SeedErr") or data.get("_error") or data.get("msg")
        log.warning("大厅 launchapp 未给出票据：%s", why or data)
        return ctx
    for key in ("openid", "openkey", "pf", "pfkey"):
        if data.get(key):
            ctx[key] = str(data.get(key))
    if not ctx.get("pf"):
        ctx["pf"] = "qqgame"
    jump = str(data.get("jump_url") or "").strip() or iframe_url
    if not jump:
        log.warning("大厅 launchapp 没有 jump_url（JSON 已存 debug/1_launchapp.json）")
        return _ready_log(ctx)
    _fill_from_canvas(qq, ctx, jump, spec["game_url"])
    return _ready_log(ctx)
