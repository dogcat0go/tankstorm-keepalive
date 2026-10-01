# tankstorm-keepalive  Copyright (C) 2026 Dimlitter
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# 本程序是自由软件：你可以依据自由软件基金会发布的 GNU Affero 通用公共许可证
# （第 3 版，或你选择的任何更新版本）之条款，再分发和/或修改它。
# 本程序希望能有用，但不提供任何担保；甚至不含适销性或特定用途适用性的默示担保。
# 详见随附的 LICENSE 文件，或 <https://www.gnu.org/licenses/>。
"""消息推送：PushPlus（https://www.pushplus.plus）。

用途聚焦一件事：保活守护进程跑在服务器上时，如果 QQ 登录态过期、需要重新扫码，
就把二维码图片推送到你的微信，你扫一下即可恢复。平时不打扰。

token 配置在 config.local.json 的 通知.pushplus_token（该文件已 gitignore，不进仓库）。
"""

import base64

import requests

from .log import get_logger

log = get_logger()

PUSHPLUS_URL = "https://www.pushplus.plus/send"


def _token(config: dict) -> str:
    return (config.get("通知", {}) or {}).get("pushplus_token", "").strip()


def send(config: dict, title: str, content: str, template: str = "txt") -> bool:
    """推送一条文本/HTML 消息。content 为 HTML 时 template 传 'html'。"""
    token = _token(config)
    if not token:
        log.warning("未配置 pushplus_token（config.local.json 通知.pushplus_token），跳过推送")
        return False
    try:
        r = requests.post(PUSHPLUS_URL, json={
            "token": token, "title": title, "content": content, "template": template,
        }, timeout=15)
        data = r.json()
        if data.get("code") == 200:
            log.info("PushPlus 推送成功: %s", title)
            return True
        log.warning("PushPlus 推送返回异常: %s", data)
    except Exception as exc:
        log.warning("PushPlus 推送失败: %s", exc)
    return False


def send_qrcode(config: dict, title: str, qrcode_path: str, note: str = "") -> bool:
    """把二维码 PNG 以内嵌图片(HTML)推送。打开 PushPlus 消息即可看到二维码。"""
    try:
        with open(qrcode_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
    except OSError as exc:
        log.warning("读取二维码失败: %s", exc)
        return False
    html = (
        f'<p>{note or "坦克风暴登录态已过期，请用手机 QQ 扫码重新登录："}</p>'
        f'<p><img src="data:image/png;base64,{b64}" '
        f'style="width:220px;height:220px;border:1px solid #ddd"/></p>'
        f'<p style="color:#888;font-size:12px">'
        f'手机上可长按/保存图片，用「手机QQ→扫一扫→相册」选它扫码；'
        f'二维码有效期约 2 分钟，过期后脚本会自动重发。</p>'
    )
    return send(config, title, html, template="html")


def _host(url: str) -> str:
    from urllib.parse import urlparse
    try:
        p = urlparse(url)
        return p.netloc or url[:40]
    except Exception:
        return "?"


def send_feishu(webhook: str, text: str) -> bool:
    """飞书自定义机器人。webhook 是群里添加机器人后给的地址。"""
    webhook = (webhook or "").strip()
    if not webhook:
        return False
    try:
        r = requests.post(webhook, json={
            "msg_type": "text", "content": {"text": text},
        }, timeout=10)
        data = r.json()
        if data.get("code") in (0, None) and data.get("StatusCode", 0) in (0, None):
            if data.get("code", 0) == 0:
                log.info("飞书推送成功")
                return True
        log.warning("飞书推送返回异常 %s: %s", _host(webhook), data)
    except Exception as exc:
        log.warning("飞书推送失败 %s: %s", _host(webhook), exc)
    return False


def send_qq(api: str, token: str, target: str, text: str, message=None) -> bool:
    """OneBot HTTP（NapCat / Lagrange 这类 QQ 机器人）。

    api 是机器人的 HTTP 根，例如 http://127.0.0.1:3000。
    target 是私聊 QQ 号；群消息写成 g:群号。
    message 给定时原样作为 OneBot 的 message 字段，用来带图片。
    """
    api = (api or "").strip().rstrip("/")
    target = (target or "").strip()
    if not api or not target:
        return False
    group = target.startswith("g:")
    tid = target[2:] if group else target
    path = "/send_group_msg" if group else "/send_private_msg"
    key = "group_id" if group else "user_id"
    try:
        ident = int(tid)
    except ValueError:
        ident = tid
    headers = {}
    if (token or "").strip():
        headers["Authorization"] = "Bearer " + token.strip()
    try:
        r = requests.post(
            api + path,
            json={key: ident, "message": text if message is None else message},
            headers=headers, timeout=10)
        data = r.json()
        if data.get("status") == "ok" or data.get("retcode") == 0:
            log.info("QQ 机器人推送成功")
            return True
        log.warning("QQ 机器人返回异常 %s: %s", _host(api), data)
    except Exception as exc:
        log.warning("QQ 机器人推送失败 %s: %s", _host(api), exc)
    return False


def send_admin_login_qr(config: dict, qrcode_path: str, target: str = "",
                        text: str = "") -> bool:
    """用 NapCat 把登录二维码私聊出去。地址用「登录.内部QQ」，没填则用「通知」。
    target 是接收人；没给就发给「内部QQ.管理员」。"""
    inner = ((config.get("登录") or {}).get("内部QQ") or {})
    api = str(inner.get("地址") or "").strip()
    token = str(inner.get("Token") or "").strip()
    admin = str(target or inner.get("管理员") or "").strip()
    if not api:
        note = config.get("通知") or {}
        api = str(note.get("qq_api") or "").strip()
        token = str(note.get("qq_token") or "").strip()
    if not api or not admin:
        return False
    text = text or ("坦克风暴登录态失效。用另一台设备的游戏 QQ 扫这张码，"
                    "不要把图存进同一台手机相册再扫。")
    message = text
    try:
        with open(qrcode_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        message = [
            {"type": "text", "data": {"text": text}},
            {"type": "image", "data": {"file": "base64://" + b64}},
        ]
    except OSError as exc:
        log.warning("读取登录二维码失败: %s", exc)
    ok = send_qq(api, token, admin, text, message=message)
    if ok:
        log.info("已把登录二维码发给 %s", admin)
    return ok


def push_watch(config: dict, row: dict, text: str) -> None:
    """按服务器「通知」里的机器人，私聊给这个账号填的 QQ。没填的跳过。"""
    note = config.get("通知") or {}
    send_feishu(note.get("feishu_webhook") or "", text)
    send_qq(note.get("qq_api") or "", note.get("qq_token") or "",
            row.get("qq_target") or "", text)
