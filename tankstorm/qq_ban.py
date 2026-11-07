# tankstorm-keepalive  Copyright (C) 2026 Dimlitter
# SPDX-License-Identifier: AGPL-3.0-or-later
"""攻打 QQ 黑名单。名单写在配置「登录.攻打黑名单」。

扫描号不看这份名单。对上的攻打号不再登录、不再保活，页面上交给它的操作也拒绝。
"""

from .log import get_logger

log = get_logger()

# 这些接口会让绑定的攻打号去做事。看页面、改密码、取消订单和定时关闭仍然可以。
ATTACK_API = {
    "/api/attacks",
    "/api/daily",
    "/api/daily/schedule",
    "/api/daily/modo-schedule",
    "/api/daily/switch",
    "/api/daily/guild-accept",
    "/api/daily/guild-level",
    "/api/daily/cancel",
    "/api/base",
    "/api/base/resource-cards",
    "/api/base/clean-items",
    "/api/modo",
    "/api/attack-login",
    "/api/attack-here",
    "/api/camp-live",
    "/api/base-equip",
    "/api/attack-move",
    "/api/attack-pause",
    "/api/attack-hold",
    "/api/lock-cards",
    "/api/clear-plan",
    "/api/retreat",
    "/api/auto-lock",
    "/api/watch-lock",
}

STOP_TEXT = "这个攻打 QQ 在黑名单里，已退出"


def banned_attack_qqs(config) -> set:
    """配置里的攻打黑名单。只认 5 到 12 位数字，别的丢掉。"""
    box = (config or {}).get("登录") if isinstance(config, dict) else None
    raw = box.get("攻打黑名单") if isinstance(box, dict) else None
    if isinstance(raw, str):
        raw = raw.replace("，", ",").replace("、", ",").replace(";", ",").split(",")
    elif isinstance(raw, int) and not isinstance(raw, bool):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return set()
    out = set()
    for item in raw:
        uin = str(item or "").strip()
        if uin.isdigit() and 5 <= len(uin) <= 12:
            out.add(uin)
    return out


def attack_qq_banned(config, uin) -> bool:
    uin = str(uin or "").strip()
    return bool(uin) and uin in banned_attack_qqs(config)


def refuse_banned_attack(config, uin) -> bool:
    """在黑名单里返回 True，并写一行日志。空号码返回 False。"""
    uin = str(uin or "").strip()
    if not attack_qq_banned(config, uin):
        return False
    log.error("攻打 QQ %s 在黑名单里，直接退出", uin)
    return True


def attack_api_stop(config, uin, path: str) -> str:
    """这个页面操作会用到攻打号，而且号在黑名单里。否则返回空字符串。"""
    if path not in ATTACK_API:
        return ""
    if not attack_qq_banned(config, uin):
        return ""
    return STOP_TEXT
