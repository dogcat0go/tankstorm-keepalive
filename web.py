# tankstorm-keepalive  Copyright (C) 2026 Dimlitter
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# 本程序是自由软件：你可以依据自由软件基金会发布的 GNU Affero 通用公共许可证
# （第 3 版，或你选择的任何更新版本）之条款，再分发和/或修改它。
# 本程序希望能有用，但不提供任何担保；甚至不含适销性或特定用途适用性的默示担保。
# 详见随附的 LICENSE 文件，或 <https://www.gnu.org/licenses/>。
"""订阅页面。不连游戏，和 main.py 分开跑。

  python3 web.py
  python3 web.py --host 0.0.0.0 --port 8765
  python3 web.py --add-user 用户名 --password 密码 --expires 2026-12-31 --tier 中级
  python3 web.py --set-expires 用户名 2026-12-31
  python3 web.py --set-tier 用户名 高级
  python3 web.py --set-admin 用户名 开
  python3 web.py --clear-attack 用户名

扫城仍用 main.py，例如 python3 main.py --watch-pages 1201:10-20。
注册默认关闭。有效期按北京时间的日期，这一天仍然有效。
两边读写同一份 city_players.db。
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import re
from datetime import datetime

from tankstorm.log import get_logger
from tankstorm.paths import data_file, ensure_user_copy, user_path
from tankstorm import citydb, webui

log = get_logger()

if ensure_user_copy("config.json"):
    log.info("已在程序目录生成 config.json，可直接编辑")


def _load_json(path: str, required: bool = True) -> dict:
    if not os.path.exists(path):
        if required:
            log.error("缺少文件: %s", path)
            sys.exit(1)
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _deep_merge(base: dict, over: dict) -> dict:
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def _date(text: str) -> str:
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        log.error("有效期要写成 2026-12-31 这种日期")
        return ""
    return text


def main() -> int:
    parser = argparse.ArgumentParser(
        description="坦克风暴订阅页面（不连游戏）")
    parser.add_argument("--host", default="0.0.0.0", metavar="地址",
                        help="监听地址，默认 0.0.0.0。证书由反向代理处理")
    parser.add_argument("--port", type=int, default=8765, metavar="端口",
                        help="端口，默认 8765")
    parser.add_argument("--add-user", metavar="用户名", help="后台添加账号，不启动网页")
    parser.add_argument("--password", metavar="密码", help="和 --add-user 一起用")
    parser.add_argument("--expires", metavar="日期", help="北京时间，这一天仍然有效")
    parser.add_argument("--tier", default="初级", metavar="档",
                        help="和 --add-user 一起用：初级、中级或高级，默认初级")
    parser.add_argument("--set-expires", nargs=2, metavar=("用户名", "日期"),
                        help="改已有账号的有效期，不启动网页")
    parser.add_argument("--set-tier", nargs=2, metavar=("用户名", "档"),
                        help="把账号标成初级、中级或高级，不启动网页")
    parser.add_argument("--set-admin", nargs=2, metavar=("用户名", "开关"),
                        help="开或关：该账号能否在页面上改扫描安排，不启动网页")
    parser.add_argument("--bind-attack", nargs=2, metavar=("用户名", "QQ号"),
                        help="把攻打 QQ 绑到这个登录账号。一个账号一个 QQ，同一个 QQ 可以绑多个账号，不启动网页")
    parser.add_argument("--clear-attack", metavar="用户名",
                        help="解开这个登录账号的攻打 QQ，未完成的攻打记为失败。别的账号还用这个 QQ 时不删票据，不启动网页")
    args = parser.parse_args()
    if (args.add_user or args.set_expires or args.set_tier or args.set_admin
            or args.bind_attack or args.clear_attack):
        if args.clear_attack:
            username = args.clear_attack.strip()
            user_id = citydb.user_id_by_name(username)
            if not user_id:
                log.error("没有这个账号：%s", username)
                return 1
            why = citydb.clear_attack_binding(user_id)
            if why:
                log.error("%s", why)
                return 1
            log.info("已解开 %s 的攻打 QQ", username)
            return 0
        if args.bind_attack:
            username, acct = args.bind_attack
            user_id = citydb.user_id_by_name(username)
            if not user_id:
                log.error("没有这个账号：%s", username)
                return 1
            why = citydb.bind_attack_account(user_id, acct.strip())
            if why:
                log.error("%s", why)
                return 1
            log.info("已把攻打 QQ %s 绑定到 %s", acct.strip(), username)
            return 0
        if args.set_admin:
            name, flag = args.set_admin
            if flag not in ("开", "关"):
                log.error("开关只能是开或关")
                return 1
            if not citydb.set_user_admin(name, flag == "开"):
                log.error("没有这个账号：%s", name)
                return 1
            log.info("已把 %s 的扫描管理%s", name, "打开" if flag == "开" else "关掉")
            return 0
        if args.set_tier:
            name, tier = args.set_tier
            if tier not in citydb.TIERS:
                log.error("订阅档只能是初级、中级、高级")
                return 1
            if not citydb.set_user_tier(name, tier):
                log.error("没有这个账号：%s", name)
                return 1
            log.info("已把 %s 标为%s", name, tier)
            return 0
        if args.set_expires:
            name, day = args.set_expires
            day = _date(day)
            if not day:
                return 1
            if not citydb.set_user_expiry(name, day):
                log.error("没有这个账号：%s", name)
                return 1
            log.info("已把 %s 的有效期改到 %s", name, day)
            return 0
        name = args.add_user.strip()
        password = args.password or ""
        day = _date(args.expires or "")
        if not re.fullmatch(r"[A-Za-z0-9_\u4e00-\u9fff]{2,32}", name):
            log.error("用户名用 2 到 32 位字母、数字或中文")
            return 1
        if len(password) < 6 or len(password) > 72:
            log.error("密码至少 6 位")
            return 1
        if not day:
            return 1
        tier = args.tier or "初级"
        if tier not in citydb.TIERS:
            log.error("订阅档只能是初级、中级、高级")
            return 1
        if citydb.create_user(name, password, day, tier) is None:
            log.error("这个用户名已经有了。改有效期：python3 web.py --set-expires %s %s",
                      name, day)
            return 1
        log.info("已添加账号 %s，%s，有效期至 %s", name, tier, day)
        return 0
    config = _deep_merge(
        _load_json(data_file("config.json")),
        _load_json(user_path("config.local.json"), required=False))
    try:
        return webui.serve(args.host, args.port, config)
    except OSError as exc:
        log.error("订阅页面没能监听 %s:%s：%s", args.host, args.port, exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
