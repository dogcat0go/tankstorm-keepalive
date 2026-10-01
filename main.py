# tankstorm-keepalive  Copyright (C) 2026 Dimlitter
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# 本程序是自由软件：你可以依据自由软件基金会发布的 GNU Affero 通用公共许可证
# （第 3 版，或你选择的任何更新版本）之条款，再分发和/或修改它。
# 本程序希望能有用，但不提供任何担保；甚至不含适销性或特定用途适用性的默示担保。
# 详见随附的 LICENSE 文件，或 <https://www.gnu.org/licenses/>。
"""坦克风暴（QQ空间 appid 100616028）—— 命令行入口。

这个脚本干**两件互相独立**的事，别把它们混在一起：

  保活   `--keepalive`   常驻，连着游戏 socket 定时发心跳，防止被踢下线。
                         唯一职责就是别掉线，跑几天几周不停。
  每日   `--daily`       跑一轮每日任务然后退出。一次性批处理。

分开的理由：保活断线会自动重连，如果每日任务挂在里面，每次重连都要重跑一轮；
而且任务出错会牵连保活这个更重要的进程。要"连上顺带领一轮"就显式写
`--keepalive --daily`。

常用：
  python main.py --login       扫码登录（cookie 存 cookies.json，之后自动续期）
  python main.py --qq 攻打 --login     给「登录.账号」里的另一个号扫码
  python main.py --qq 攻打 --move 2302 用这个号寻径并打这座城
  python main.py --check       验证登录态，打印 uid/sid/level
  python main.py --keepalive   保活常驻
  python main.py --daily       跑一轮每日任务
  python main.py --list        列出每日任务及今日进度
  python main.py --reset       清空今日任务计数
  python main.py --country-war 10   单独跑国战：自动打摩多军团 10 次
  python main.py --city-players 2203              拉芝加哥玩家（从第 0 页）
  python main.py --city-players 2203 --city-page 232  从第 232 页继续
  python main.py --city-players 2203 --city-page 10 --city-page-end 20
  python main.py --watch-pages 1201:10-20 1301:0-8
  python main.py --watch-cities                    常驻：按 config 城市监视每 5 分钟刷新指定城
  python web.py                                    订阅页面，不连游戏，默认 0.0.0.0:8765
  python main.py --atk 7826194927704102           离线打人（默认普通攻击 1 次）
  python main.py --atk 7826194927704102 --sweep --atk-times 2
  python main.py --atk-city 2302 --sweep          现场翻页打城：先打再看士气，击退/打不过换人
  python main.py --list-cities        列出全部城市 ID 与中文名
  python main.py --capture            扫码后打开钩子版游戏窗口，实时抓包
  python main.py --pve                 征战世界，关卡见 config「征战.关卡」
  python main.py --pve 1-10,15         只打这些关（当前关必须在名单里）
"""

import argparse
import fcntl
import json
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tankstorm import engine, notify, paths, qzone, socket_keepalive  # noqa: E402
from tankstorm.log import get_logger                 # noqa: E402
from tankstorm.qq_login import QQSession             # noqa: E402

log = get_logger()

# 打包成 exe 后，第一次运行把出厂配置复制到 exe 旁边，用户改那一份。
# 不复制的话用户看不到 config.json，也就无从配置。已存在则绝不覆盖。
for _f in ("config.json", "endpoints.json", "protocol.json"):
    if paths.ensure_user_copy(_f):
        log.info("已在程序目录生成 %s，可直接编辑", _f)

BASE_DIR = paths.app_dir()
# 配置和协议表：exe 旁边有就用用户那份，没有才用随包默认值
CONFIG_FILE = paths.data_file("config.json")
ENDPOINTS_FILE = paths.data_file("endpoints.json")
# 这两个一律写在程序目录：密钥文件和运行状态都是用户数据
LOCAL_CONFIG_FILE = paths.user_path("config.local.json")   # 放密钥，已 gitignore
STATE_FILE = paths.user_path("state.json")


def load_json(path: str, required: bool = True) -> dict:
    if not os.path.exists(path):
        if required:
            log.error("缺少文件: %s", path)
            sys.exit(1)
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _deep_merge(base: dict, over: dict) -> dict:
    """把 over 深度合并进 base（用于 config.local.json 覆盖 config.json 里的密钥）。"""
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def _cli_page_ranges(tokens, page, page_end):
    """命令行上的页范围。一座城一段，返回 [(城市, 起始页, 结束页)]。"""
    bare = [t for t in tokens if ":" not in t]
    if bare and len(tokens) > 1:
        raise ValueError("多座城请每座都写成 城市:起始-结束，例如 1201:10-20 1301:0-8")
    spans = []
    if len(tokens) == 1 and bare:
        try:
            city = int(tokens[0])
        except ValueError:
            raise ValueError(f"无法识别 {tokens[0]}，写成 城市:起始-结束，例如 1201:10-20")
        if page_end is None:
            raise ValueError("单座城请带 --city-page-end，或写成 1201:10-20")
        spans.append((city, page, page_end))
    else:
        for token in tokens:
            city_s, sep, span = token.partition(":")
            start_s, dash, end_s = span.partition("-")
            if not sep or not dash or not city_s or start_s == "" or end_s == "":
                raise ValueError(f"无法识别 {token}，写成 城市:起始-结束，例如 1201:10-20")
            try:
                spans.append((int(city_s), int(start_s), int(end_s)))
            except ValueError:
                raise ValueError(f"无法识别 {token}，写成 城市:起始-结束，例如 1201:10-20")
    seen = set()
    for city, start, end in spans:
        if city <= 0:
            raise ValueError("城市 ID 要大于 0")
        if start < 0 or end < 0:
            raise ValueError("页码从 0 起，不能是负数")
        if end < start:
            raise ValueError(f"城市 {city} 的结束页 {end} 小于起始页 {start}")
        if city in seen:
            raise ValueError(f"城市 {city} 写了两段页范围，一座城只对应一段")
        seen.add(city)
    return spans


def load_config() -> dict:
    config = load_json(CONFIG_FILE)
    local = load_json(LOCAL_CONFIG_FILE, required=False)  # 本地密钥文件，不进仓库
    return _deep_merge(config, local)


_QQ_LOCK = None


def _lock_cookie(path: str, fatal: bool = True) -> bool:
    """同一份 cookie 只能有一个进程。扫描号和攻打号因此不会互相覆盖票据。"""
    global _QQ_LOCK
    fh = open(path + ".lock", "a+")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        if fatal:
            log.error("这个号已有进程在用 %s", path)
            sys.exit(1)
        log.info("这个号已有进程在用 %s", path)
        return False
    _QQ_LOCK = fh
    return True


def release_qq_lock() -> None:
    global _QQ_LOCK
    if _QQ_LOCK is not None:
        _QQ_LOCK.close()
        _QQ_LOCK = None


def _cookie_path(name: str, cookie: str) -> str:
    if not cookie or os.path.isabs(cookie) or ".." in cookie.split("/"):
        log.error("账号「%s」的 cookie 要写成程序目录下的相对路径", name)
        sys.exit(1)
    path = os.path.normpath(paths.user_path(cookie))
    root = os.path.normpath(paths.app_dir())
    if path != root and not path.startswith(root + os.sep):
        log.error("账号「%s」的 cookie 要写在程序目录里", name)
        sys.exit(1)
    return path


def _uin_in_cookie_file(path: str) -> str:
    if not os.path.exists(path):
        return ""
    try:
        with open(path, encoding="utf-8") as f:
            jar = json.load(f)
    except (OSError, ValueError):
        return ""
    picked = ""
    for c in jar:
        if not isinstance(c, dict) or c.get("name") != "uin":
            continue
        picked = str(c.get("value") or "").lstrip("o0")
        if "qq.com" in str(c.get("domain") or ""):
            return picked
    return picked


def open_qq(config: dict, name: str, fatal_lock: bool = True):
    """不带名字是扫描号：根目录 cookies.json，可以向 NapCat 要票据。
    带名字是攻打号：独立 cookie，不碰 NapCat，也不能和扫描号是同一个 QQ。"""
    scan_path = os.path.normpath(paths.user_path("cookies.json"))
    if not name:
        if not _lock_cookie(scan_path, fatal=fatal_lock):
            return None
        return QQSession(scan_path)
    accounts = (config.get("登录") or {}).get("账号") or {}
    spec = accounts.get(name)
    if not isinstance(spec, dict):
        names = [k for k, v in accounts.items() if isinstance(v, dict)]
        log.error("登录.账号 里没有「%s」%s", name,
                  ("。已有：" + "、".join(names)) if names else "")
        sys.exit(1)
    if spec.get("用NapCat"):
        log.error("攻打号「%s」不能使用 NapCat。NapCat 只给扫描号续票", name)
        sys.exit(1)
    by_name = {}
    for key, item in accounts.items():
        if not isinstance(item, dict):
            continue
        path = _cookie_path(key, str(item.get("cookie") or "").strip())
        if path == scan_path:
            log.error("攻打号「%s」不能使用扫描号的 cookies.json", key)
            sys.exit(1)
        other = next((n for n, p in by_name.items() if p == path), "")
        if other:
            log.error("攻打号「%s」和「%s」用了同一份 cookie", key, other)
            sys.exit(1)
        by_name[key] = path
    path = by_name[name]
    who = str(spec.get("扫码QQ") or "").strip()
    if who and (not who.isdigit() or not 5 <= len(who) <= 12):
        log.error("攻打号「%s」的扫码QQ 要写成 5 到 12 位数字", name)
        sys.exit(1)
    blocked = set()
    scan_uin = _uin_in_cookie_file(scan_path)
    if scan_uin:
        blocked.add(scan_uin)
    for key, item_path in by_name.items():
        if key == name:
            continue
        uin = _uin_in_cookie_file(item_path)
        if uin:
            blocked.add(uin)
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    qr = (path[:-5] if path.endswith(".json") else path) + ".qrcode.png"
    if not _lock_cookie(path, fatal=fatal_lock):
        return None
    qq = QQSession(path, qrcode_file=qr)
    qq.use_napcat = False
    qq.attack_account = True
    qq.account_name = name
    qq.notify_qq = who
    qq.blocked_uins = blocked
    if qq.shares_blocked_uin():
        sys.exit(1)
    config.setdefault("登录", {})["推送登录QQ号"] = ""
    log.info("使用攻打号「%s」，cookie=%s", name, path)
    return qq


def main() -> int:
    parser = argparse.ArgumentParser(
        description="坦克风暴：保活守护 + 每日任务 + 国战（三件独立的事）",
        epilog="本程序是自由软件，依 GNU AGPL-3.0 或更新版本授权，不提供任何担保。"
               "源码：https://github.com/Dimlitter/tankstorm-keepalive",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    g1 = parser.add_argument_group("登录")
    g1.add_argument("--qq", metavar="名字", default="",
                    help="攻打号，对应「登录.账号」。扫描不要加这个参数")
    g1.add_argument("--login", action="store_true", help="强制重新扫码登录")
    g1.add_argument("--check", action="store_true", help="验证登录态并打印上下文")
    g1.add_argument("--import-device", metavar="文件",
                    help="从浏览器搬一次设备记录以启用推送登录（一次性，"
                         "文件里放浏览器的 Cookie；给 - 表示从标准输入读）")

    g2 = parser.add_argument_group("保活（常驻）")
    g2.add_argument("--keepalive", action="store_true",
                    help="连游戏 socket 定时心跳，防掉线；断线自动重连")

    g3 = parser.add_argument_group("每日任务（一次性）")
    g3.add_argument("--daily", action="store_true",
                    help="跑一轮每日任务后退出；与 --keepalive 同时给则由保活带着跑")
    g3.add_argument("--list", action="store_true", help="列出每日任务及今日进度")
    g3.add_argument("--reset", action="store_true", help="清空今日任务计数")

    g5 = parser.add_argument_group("国战")
    g5.add_argument("--country-war", type=int, metavar="次数", default=0,
                    help="自动扫荡摩多军团 N 次（行动力够就扫荡，不够改普通攻击，"
                         "低于 5 点停手）")
    g5.add_argument("--city-players", type=int, metavar="城市ID", default=None,
                    help="查询指定城市内的全部玩家，写入 city_players.db")
    g5.add_argument("--city-country", type=int, metavar="国家ID", default=0,
                    help="配合 --city-players / --atk：城市所属国家；"
                         "拉玩家时不填则用自己的国家，打人时不填则查库")
    g5.add_argument("--city-page", type=int, metavar="页码", default=0,
                    help="从第几页开始（0 起算，含这一页。"
                         "上次停在第 231 页就传 232。"
                         "订阅页面上的页数要减 1）")
    g5.add_argument("--city-page-end", type=int, metavar="页码", default=None,
                    help="翻到第几页为止（0 起算，含这一页）。"
                         "配合 --city-players 只拉一轮；"
                         "配合 --watch-pages 每 5 分钟重复这段")
    g5.add_argument("--watch-pages", nargs="*", default=None,
                    metavar="城市:起始-结束",
                    help="常驻：每 5 分钟按各自的页范围翻这些城。"
                         "可写多座，一座城一段：1201:10-20 1301:0-8。"
                         "页码从 0 起，含结束页。只写一座城 ID 时仍用 "
                         "--city-page 和 --city-page-end。"
                         "不带参数则用 config「页范围监视.范围」。"
                         "不删范围外的人。间隔见「间隔秒」")
    g5.add_argument("--atk", metavar="BASEID", default=None,
                    help="离线打指定玩家（type:14 普通攻击；加 --sweep 改扫荡。"
                         "不迁城，目标须在邻城）")
    g5.add_argument("--atk-times", type=int, metavar="次数", default=1,
                    help="配合 --atk：打几次，默认 1。打城时无效（一人打到击退或打不过）")
    g5.add_argument("--sweep", action="store_true",
                    help="配合 --atk / --atk-city：用 type:19 扫荡（15 点行动力），"
                         "默认 type:14 普通攻击（5 点）")
    g5.add_argument("--atk-city", type=int, metavar="城市ID", default=None,
                    help="不带 --atk：现场翻页打这座城。每人先打、再看士气，"
                         "归零或不在城内才换下一个；第一次就被拒记失败。"
                         "行动力低于 15 自动开恢复卡。带 --atk：指定目标所在城市")
    g5.add_argument("--watch-cities", action="store_true",
                    help="常驻刷新 config「城市监视.城市」和网页订阅的城；"
                         "间隔见「间隔秒」，默认 5 分钟。走保活同一条连接")
    g5.add_argument("--web", action="store_true",
                    help="已分开。订阅页面请另开 python3 web.py，不再挂在这个进程里")
    g5.add_argument("--list-cities", action="store_true",
                    help="列出全部城市 ID 与中文名（读官方配置表，不用登录）")
    g5.add_argument("--move", type=int, metavar="城市ID", default=None,
                    help="沿路线走到能打到这座城的相邻城，然后扫荡这座城"
                         "（type:19，每次 15 点行动力）。敌城有人就先扫荡")
    g5.add_argument("--orders", action="store_true",
                    help="领取页面上中级、高级提交的远程扫码攻打。必须 --qq。"
                         "和扫描进程可以同时开")
    g5.add_argument("--route", type=int, metavar="城市ID", default=None,
                    help="规划怎么打到这座城。当前城市和自己的国家从国战面板读。"
                         "同国城市直接通过，异国城市须先占领。"
                         "只打印路线，不迁城、不发攻击")

    g6 = parser.add_argument_group("成就建筑拨款")
    g6.add_argument("--fund", type=int, metavar="建筑ID", default=None,
                    help="每次全部拨款前，各开 4 张 1000 万金属卡和石油卡。"
                         "次数见 --fund-times")
    g6.add_argument("--fund-times", type=int, metavar="次数", default=1,
                    help="全部拨款的次数，默认 1。每一次都会先开 4 张金属卡和 4 张石油卡")

    g7 = parser.add_argument_group("征战世界")
    g7.add_argument("--pve", nargs="?", const="", default=None, metavar="关卡",
                    help="打征战世界后退出。不带参数用 config「征战.关卡」。"
                         "也可写 1-10 或 3,5,8。当前关不在名单里会停，不会跳关")

    g4 = parser.add_argument_group("其它")
    g4.add_argument("--capture", action="store_true",
                    help="扫码登录后打开钩子版 Flash 窗口，实时抓游戏明文包。"
                         "不要用 QQ 游戏大厅。请先停掉 --keepalive")
    g4.add_argument("--task", help="（旧的 HTTP 接口任务，见 endpoints.json）")
    g4.add_argument("--real", action="store_true",
                    help="（已废弃，保留兼容：现在 --daily 一律真实发送）")
    args = parser.parse_args()

    # 什么都不给就打印用法。以前默认会去跑 endpoints.json 里那套早已废弃的
    # HTTP 任务，全部失败还把退出码带成 1，看着像登录坏了。
    if not any((args.login, args.check, args.keepalive, args.daily,
                args.list, args.reset, args.task, args.import_device,
                args.country_war, args.city_players is not None,
                args.atk, args.atk_city is not None, args.watch_cities,
                args.watch_pages is not None,
                args.web, args.list_cities, args.route is not None,
                args.move is not None, args.orders,
                args.capture, args.fund is not None,
                args.pve is not None)):
        parser.print_help()
        return 0

    if args.web and not any((args.login, args.check, args.keepalive, args.daily,
                              args.list, args.reset, args.task, args.import_device,
                              args.country_war, args.city_players is not None,
                              args.atk, args.atk_city is not None, args.watch_cities,
                              args.watch_pages is not None, args.list_cities,
                              args.route is not None, args.move is not None,
                              args.orders,
                              args.capture, args.fund is not None,
                              args.pve is not None)):
        log.error("订阅页面已和游戏分开。另开一个进程：python3 web.py")
        return 1

    config = load_config()
    endpoints = load_json(ENDPOINTS_FILE)

    if args.list_cities:
        from tankstorm import citydb
        try:
            rows = citydb.list_cities()
        except Exception as exc:
            log.error("拉城市目录失败：%s", exc)
            return 1
        print(f"\n{'ID':<8} {'城市':<16} {'阵营ID':<8} 阵营")
        print("-" * 52)
        for cid, name, n_id, n_name in rows:
            print(f"{cid:<8} {name:<16} {str(n_id or ''):<8} {n_name}")
        print("-" * 52)
        print(f"共 {len(rows)} 座  库文件 {citydb.DB_FILE}\n")
        return 0

    if args.reset:
        from tankstorm import daily as _daily
        if os.path.exists(_daily.STATE_FILE):
            os.remove(_daily.STATE_FILE)
            print(f"已清空今日任务计数：{_daily.STATE_FILE}")
        else:
            print("今日计数本来就是空的")
        if not args.daily:
            return 0

    if args.list:
        from tankstorm import daily as _daily
        sw = (config.get("每日任务", {}) or {}).get("任务", {})
        conf = config.get("每日任务", {}) or {}
        st = _daily._load_state()
        print(f"\n每日任务（{'已启用' if conf.get('启用') else '未启用'}，"
              f"实发模式）")
        print(f"{'执行顺序':<4} {'任务':<12} {'opcode':<8} {'消息':<22} "
              f"{'上限':<5} {'今日':<5} {'参数':<6} 开关")
        print("-" * 92)
        for i, t in enumerate(_daily.ordered_tasks(), 1):
            done = st.get("done", {}).get(t.key, 0)
            on = "✅开" if sw.get(t.key) else "  关"
            cd = f"/{t.cooldown_sec // 60}分冷却" if t.cooldown_sec else ""
            mark = "实测" if t.confidence == "实测" else "待确认"
            print(f"{i:>4}   {t.key:<12} {t.opcode:<8} {t.msg:<22} "
                  f"{str(t.max_per_day) + cd:<5} {done:<5} {mark:<6} {on}")
        print("-" * 92)
        print("『实测』= 参数来自真实抓包，可放心开；『待确认』= 默认跳过，需先抓包核对")
        print("周任务/每日任务由代码强制排最后（前面的操作会推进它们的进度）\n")
        return 0

    # 同日去重只对旧的 HTTP 任务有意义。每日任务自己按 logs/daily-state.json
    # 记每项的次数，比"今天整体跑没跑过"精确得多，不该再被这个开关拦住。
    if config.get("同日去重", False) and args.task:
        state = load_json(STATE_FILE, required=False)
        if state.get("last_run") == date.today().isoformat():
            log.info("今天已经跑过（state.json），退出。删掉 state.json 可强制重跑")
            return 0

    attacking = (args.move is not None or bool(args.atk)
                 or args.atk_city is not None or args.orders)
    scanning = bool(args.keepalive or args.watch_cities
                    or args.watch_pages is not None)
    if attacking and not args.qq:
        log.error("攻打必须用 --qq 指定攻打号，不能用扫描号的 cookies.json")
        return 1
    if args.qq and scanning:
        log.error("「%s」是攻打号，不能用来扫描。扫描进程不要加 --qq", args.qq)
        return 1

    qq = open_qq(config, args.qq)

    # 一次性引导：把浏览器的设备记录搬进来，之后推送登录才有 dev_mid_sig 可用。
    # pt_fetch_dev_uin 只能给已有的续期，签发不出第一个，所以只能这么来。
    if args.import_device:
        if args.import_device == "-":
            text = sys.stdin.read()
        else:
            with open(args.import_device, encoding="utf-8") as f:
                text = f.read()
        try:
            got = qq.import_device_cookies(text)
        except (ValueError, KeyError, TypeError) as exc:
            log.error("设备记录解析失败：%s", exc)
            return 1
        if not got:
            log.error("没找到可用的设备记录。至少要有 dev_mid_sig —— "
                      "在浏览器里打开 ptlogin2.qq.com 的页面，从开发者工具里"
                      "把 Cookie 复制出来")
            return 1
        log.info("已导入 %s", "、".join(got))
        log.info("设备状态：%s", qq.device_status())
        return 0

    # 保活：常驻。--keepalive --daily 时才顺带跑一轮任务
    if args.city_page < 0 or (args.city_page_end is not None and args.city_page_end < 0):
        log.error("页码从 0 起，不能是负数")
        return 1
    if args.city_page_end is not None and args.city_page_end < args.city_page:
        log.error("结束页 %s 小于起始页 %s", args.city_page_end, args.city_page)
        return 1
    if args.watch_pages is not None:
        span = config.setdefault("页范围监视", {})
        if args.watch_pages:
            try:
                specs = _cli_page_ranges(args.watch_pages, args.city_page,
                                         args.city_page_end)
            except ValueError as exc:
                log.error("%s", exc)
                return 1
            span["范围"] = [{"城市": c, "起始页": a, "结束页": b}
                          for c, a, b in specs]
        span["启用"] = True
        from tankstorm.socket_keepalive import _page_range_jobs
        if not _page_range_jobs(config)[0]:
            log.error("页范围监视没有可用的城市。写成 1201:10-20 1301:0-8，"
                      "或填 config「页范围监视.范围」")
            return 1
        config.setdefault("保持活跃", {})["启用"] = True
    if args.watch_cities:
        w = config.setdefault("城市监视", {})
        w["启用"] = True
        from tankstorm import citydb
        ids = w.get("城市") or []
        subs = citydb.watch_city_ids()
        if not ids and not subs:
            log.error("config「城市监视.城市」是空的，网页里也还没有订阅。"
                      "先 python3 web.py 加上，或在 config 里填城市 ID")
            return 1
        config.setdefault("保持活跃", {})["启用"] = True
    if args.web:
        log.error("订阅页面已和游戏分开，这个进程不再监听网页。另开：python3 web.py")
    if args.keepalive or args.watch_cities or args.watch_pages is not None:
        return socket_keepalive.run(qq, config, with_daily=args.daily)

    if args.capture:
        from tankstorm import live_capture
        return live_capture.run(qq, config)

    # 国战自动战斗：连一次、打 N 次、退出
    if args.country_war:
        return socket_keepalive.run_country_war_once(qq, config,
                                                     args.country_war)

    # 城市玩家：连一次、开指定城市面板、把列表打出来、退出
    if args.route is not None:
        return socket_keepalive.run_route_once(
            qq, config, args.route, args.city_country)

    if args.orders:
        return socket_keepalive.run_remote_orders(qq, config)

    if args.move is not None:
        return socket_keepalive.run_move_once(
            qq, config, args.move, sweep=args.sweep, country=args.city_country)

    if args.city_players is not None:
        return socket_keepalive.run_city_players_once(
            qq, config, args.city_players, args.city_country, args.city_page,
            end_page=args.city_page_end)

    # 离线打人：连一次、开目标城面板、type:14/19，不迁城
    if args.atk:
        return socket_keepalive.run_attack_once(
            qq, config, args.atk, times=args.atk_times, sweep=args.sweep,
            city_id=args.atk_city or 0, country=args.city_country)

    if args.atk_city is not None:
        return socket_keepalive.run_farm_city_once(
            qq, config, args.atk_city, times=args.atk_times, sweep=args.sweep,
            country=args.city_country)

    if args.fund is not None:
        return socket_keepalive.run_fund_once(
            qq, config, args.fund, args.fund_times)

    if args.pve is not None:
        return socket_keepalive.run_pve_once(qq, config, args.pve)

    # 每日任务：连一次、跑一轮、退出
    if args.daily:
        config.setdefault("每日任务", {})["启用"] = True
        return socket_keepalive.run_daily_once(qq, config)

    # 需要人工介入时把二维码推到 PushPlus。配了 QQ 号则走「推送登录」，
    # 手机QQ点确认即可，不用扫码（存图后同机扫码会被腾讯拒）。
    push_uin = (config.get("登录", {}) or {}).get("推送登录QQ号") or qq.uin or None

    def on_qr(path, pushed=False):
        notify.send_admin_login_qr(config, path)
        if pushed:
            notify.send_qrcode(config, "坦克风暴：请在手机QQ点「确认登录」", path,
                               note=f"已向 QQ {push_uin} 推送登录确认，"
                                    f"<b>打开手机QQ点确认即可，不用扫码</b>。")
        else:
            notify.send_qrcode(config, "坦克风暴：请扫码登录", path,
                               note="请用<b>另一台设备</b>打开本条消息再扫码。")

    if not args.login and not qq.is_valid():
        qq.adopt_napcat(config)
    if args.login and qq.attack_account:
        if not socket_keepalive.relogin_with_push(qq, config):
            return 1
    elif args.login:
        if not qq.qr_login(on_qr=on_qr, push_uin=push_uin):
            return 1
    elif not qq.ensure_login(on_qr=on_qr, push_uin=push_uin):
        return 1
    if qq.shares_blocked_uin():
        return 1

    ctx = qzone.get_game_context(qq)
    if args.check:
        printable = {k: (v[:12] + "…" if isinstance(v, str) and len(v) > 16 else v)
                     for k, v in ctx.items() if k not in ("skey",)}
        log.info("提取到的上下文: %s", json.dumps(printable, ensure_ascii=False))
        log.info("设备状态：%s", qq.device_status())
        return 0

    if args.login:      # --login 到这里就算完了，别再去跑那套废弃的 HTTP 任务
        log.info("登录完成，uin=%s", qq.uin)
        return 0

    # 旧的 HTTP 接口任务（endpoints.json），只有显式 --task 才会走到
    results = engine.run_tasks(qq.session, endpoints, config, ctx, only=args.task)
    summary = engine.summarize(results)
    log.info("执行完毕:\n%s", summary)

    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"last_run": date.today().isoformat()}, f)

    failed = any(r.status == "失败" for r in results)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
