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
  python main.py --account 主号 --login
                               这个号单独登录，cookie 写到 accounts/主号/
  python main.py --multi       按 accounts.json 同时跑多个号，各执行各的参数
  python main.py --check       验证登录态，打印 uid/sid/level
  python main.py --keepalive   保活常驻
  python main.py --daily       跑一轮每日任务
  python main.py --list        列出每日任务及今日进度
  python main.py --reset       清空今日任务计数
  python main.py --country-war 10   单独跑国战：自动打摩多军团 10 次
  python main.py --city-players 2203              拉芝加哥玩家（从第 0 页）
  python main.py --city-players 2203 --city-page 232  从第 232 页继续
  python main.py --watch-cities                    常驻：按 config 城市监视每 5 分钟刷新指定城
  python main.py --atk 7826194927704102 --move 2302
                                       走到 2302 的相邻城，只打这个玩家
  python main.py --atk 7826194927704102           离线打人（默认普通攻击 1 次）
  python main.py --atk 7826194927704102 --sweep --atk-times 2
  python main.py --atk-city 2302 --sweep          现场翻页打城：先打再看士气，击退/打不过换人
  python main.py --list-cities        列出全部城市 ID 与中文名
  python main.py --capture            扫码后打开钩子版游戏窗口，实时抓包
  python main.py --pve                 从当前关打征战，最终关见 config「征战.最终关卡」
  python main.py --pve 150             从当前关打到第 150 关
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _take_account_arg(argv: list[str]) -> None:
    """在导入 tankstorm 之前把 --account 写进环境变量。

    cookie、日志、每日计数都是模块导入时就算好的路径。等 argparse 再改就晚了。
    """
    name = ""
    for i, a in enumerate(argv):
        if a == "--account" and i + 1 < len(argv):
            name = argv[i + 1].strip()
            break
        if a.startswith("--account="):
            name = a.split("=", 1)[1].strip()
            break
    if not name:
        return
    if name in (".", "..") or name.startswith("-") or any(c in name for c in '/\\:*?"<>|'):
        print(f"账号名称不合法: {name}", file=sys.stderr)
        raise SystemExit(2)
    os.environ["TANKSTORM_ACCOUNT"] = name


_take_account_arg(sys.argv[1:])

from tankstorm import engine, lockqq, notify, paths, qzone, socket_keepalive  # noqa: E402
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
LOCAL_CONFIG_FILE = os.path.join(paths.app_dir(), "config.local.json")  # 密钥，各账号共用
STATE_FILE = paths.user_path("state.json")


def load_json(path: str, required: bool = True) -> dict:
    if not os.path.exists(path):
        if required:
            log.error("缺少文件: %s", path)
            sys.exit(1)
        return {}
    with open(path, encoding="utf-8-sig") as f:
        return json.load(f)


def _deep_merge(base: dict, over: dict) -> dict:
    """把 over 深度合并进 base（用于 config.local.json 覆盖 config.json 里的密钥）。"""
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def load_config() -> dict:
    config = load_json(CONFIG_FILE)
    local = load_json(LOCAL_CONFIG_FILE, required=False)  # 本地密钥文件，不进仓库
    _deep_merge(config, local)
    name = paths.account_name()
    if name:
        log.info("账号 %s，数据目录 %s", name, paths.data_root())
        overlay = os.path.join(paths.data_root(), "config.json")
        extra = load_json(overlay, required=False)
        if not isinstance(extra, dict):
            extra = {}
        if extra:
            _deep_merge(config, extra)
            log.info("已合并账号配置 %s", overlay)
        # 根配置里的 QQ 号和昵称属于单账号。没在本账号配置里写明就清掉，
        # 避免给小号推登录时推到大号手机上，或把大号昵称当成自己。
        if "推送登录QQ号" not in (extra.get("登录") or {}):
            config.setdefault("登录", {})["推送登录QQ号"] = ""
        if "我的标识" not in (extra.get("录制") or {}):
            config.setdefault("录制", {})["我的标识"] = []
    return config


def _run_multi() -> int:
    """按 accounts.json 给每个账号起一个进程，同时跑、各干各的命令。"""
    path = os.path.join(paths.app_dir(), "accounts.json")
    if not os.path.exists(path):
        log.error("缺少 %s。复制 accounts.example.json 为 accounts.json 后填写", path)
        return 1
    rows = load_json(path).get("账号")
    if not isinstance(rows, list) or not rows:
        log.error("%s 的「账号」必须是非空列表", path)
        return 1
    seen: set[str] = set()
    specs: list[tuple[str, list[str]]] = []
    for row in rows:
        name = str(row.get("名称") or "").strip() if isinstance(row, dict) else ""
        argv = row.get("参数") if isinstance(row, dict) else None
        bad_name = (not name or name in seen or name in (".", "..")
                    or name.startswith("-")
                    or any(c in name for c in '/\\:*?"<>|'))
        bad_argv = (not isinstance(argv, list) or not argv
                    or not all(isinstance(x, str) and x.strip() for x in argv))
        if bad_name or bad_argv:
            log.error("账号条目不合法（要有不重复的名称，以及非空的参数列表）")
            return 1
        if any(a == "--multi" or a == "--account" or a.startswith("--account=")
               for a in argv):
            log.error("账号 %s 的参数里不能再写 --multi 或 --account", name)
            return 1
        seen.add(name)
        specs.append((name, argv))

    flags: dict = {}
    if os.name == "nt":
        flags["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        flags["start_new_session"] = True
    script = os.path.abspath(__file__)
    procs = []
    for name, argv in specs:
        env = os.environ.copy()
        env["TANKSTORM_ACCOUNT"] = name
        cmd = ([sys.executable, *argv] if paths.is_frozen()
               else [sys.executable, script, *argv])
        log.info("启动 %s：%s", name, " ".join(argv))
        procs.append((name, subprocess.Popen(
            cmd, env=env, cwd=paths.app_dir(), **flags)))

    code = 0
    try:
        pending = {name: p for name, p in procs}
        while pending:
            done = [n for n, p in pending.items() if p.poll() is not None]
            for n in done:
                rc = pending.pop(n).returncode
                log.info("账号 %s 已退出，代码 %s", n, rc)
                if rc:
                    code = 1
            if pending:
                time.sleep(0.4)
    except KeyboardInterrupt:
        log.info("正在停止各账号")
        for _, p in procs:
            if p.poll() is None:
                p.terminate()
        for _, p in procs:
            try:
                p.wait(timeout=8)
            except subprocess.TimeoutExpired:
                p.kill()
        return 130
    return code


def main() -> int:
    parser = argparse.ArgumentParser(
        description="坦克风暴：保活守护 + 每日任务 + 国战（三件独立的事）",
        epilog="本程序是自由软件，依 GNU AGPL-3.0 或更新版本授权，不提供任何担保。"
               "源码：https://github.com/Dimlitter/tankstorm-keepalive",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    g1 = parser.add_argument_group("登录")
    g1.add_argument("--account", metavar="名称",
                    help="使用 accounts/<名称>/ 下的登录态、日志和每日计数。"
                         "各账号先单独登录：--account 主号 --login。"
                         "任务开关与别人不同时，写 accounts/<名称>/config.json")
    g1.add_argument("--multi", action="store_true",
                    help="按程序目录的 accounts.json 同时启动多个账号，"
                         "各自执行其中的「参数」。Ctrl+C 会一起停")
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
                    help="一般不用填。自己的国家从登录数据读，"
                         "目标城归属国打开面板时读")
    g5.add_argument("--city-page", type=int, metavar="页码", default=0,
                    help="配合 --city-players：从第几页继续（0 起算。"
                         "上次停在第 231 页就传 232）")
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
                    help="常驻刷新 config「城市监视.城市」的归属国和玩家；"
                         "间隔见「间隔秒」，默认 5 分钟。走保活同一条连接")
    g5.add_argument("--list-cities", action="store_true",
                    help="列出全部城市 ID 与中文名（读官方配置表，不用登录）")
    g5.add_argument("--mordor", action="store_true",
                    help="走到本国首都卫星城旁的摩多军团城，召唤志愿兵再打。"
                         "恢复卡张数用「单次最多用几张恢复卡」")
    g5.add_argument("--move", type=int, metavar="城市ID", default=None,
                    help="沿路线走到能打到这座城的相邻城。单独用时扫荡这座城。"
                         "和 --atk 一起用时只打这个玩家，不进目标城")
    g5.add_argument("--route", type=int, metavar="城市ID", default=None,
                    help="规划怎么打到这座城。当前城市、自己的国家、沿途归属都从面板读。"
                         "同国城市直接通过，异国城市须先占领。"
                         "只打印路线，不迁城、不发攻击")

    g6 = parser.add_argument_group("成就建筑拨款")
    g6.add_argument("--fund", type=int, metavar="建筑ID", default=None,
                    help="每次全部拨款前，各开 4 张 1000 万金属卡和石油卡。"
                         "次数见 --fund-times")
    g6.add_argument("--fund-times", type=int, metavar="次数", default=1,
                    help="全部拨款的次数，默认 1。每一次都会先开 4 张金属卡和 4 张石油卡")

    g7 = parser.add_argument_group("征战世界")
    g7.add_argument("--pve", nargs="?", const="", default=None, metavar="最终关卡",
                    help="从当前关打征战世界后退出。不带参数用 config「征战.最终关卡」。"
                         "写 150 表示打到第 150 关。0 或不填表示打到过不去")

    g8 = parser.add_argument_group("物资护送")
    g8.add_argument("--escort", nargs="?", const=0, type=int, default=None,
                    metavar="刷新次数",
                    help="一直刷新，直到橙色车里还有火炮核心，并且这辆车还能被掠夺。"
                         "还能抢 2 次就抢 2 次，只剩 1 次就抢 1 次。"
                         "掠夺失败就继续刷下一辆。写次数则最多刷新这么多次")

    g4 = parser.add_argument_group("其它")
    g4.add_argument("--capture", action="store_true",
                    help="扫码登录后打开钩子版 Flash 窗口，实时抓游戏明文包。"
                         "不要用 QQ 游戏大厅。请先停掉 --keepalive")
    g4.add_argument("--task", help="（旧的 HTTP 接口任务，见 endpoints.json）")
    g4.add_argument("--real", action="store_true",
                    help="（已废弃，保留兼容：现在 --daily 一律真实发送）")
    args = parser.parse_args()

    if args.multi:
        if paths.account_name():
            log.error("--multi 不要和 --account 一起用")
            return 2
        return _run_multi()

    # 什么都不给就打印用法。以前默认会去跑 endpoints.json 里那套早已废弃的
    # HTTP 任务，全部失败还把退出码带成 1，看着像登录坏了。
    acted = any((args.login, args.check, args.keepalive, args.daily,
                 args.list, args.reset, args.task, args.import_device,
                 args.country_war, args.city_players is not None,
                 args.atk, args.atk_city is not None, args.watch_cities,
                 args.list_cities, args.route is not None, args.move is not None,
                 args.mordor,
                 args.capture, args.fund is not None,
                 args.pve is not None, args.escort is not None))
    if args.account and not acted:
        log.error("--account 只选定账号，还要带上命令，例如 --login、--daily、--keepalive")
        return 2
    if not acted:
        parser.print_help()
        return 0

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
        who = paths.account_name()
        head = f"{who} 的每日任务" if who else "每日任务"
        print(f"\n{head}（{'已启用' if conf.get('启用') else '未启用'}，"
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

    qq = QQSession()

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
    if args.watch_cities:
        w = config.setdefault("城市监视", {})
        w["启用"] = True
        ids = w.get("城市") or []
        if not ids:
            log.error("config「城市监视.城市」是空的，先填城市 ID（可用 --list-cities 查）")
            return 1
        config.setdefault("保持活跃", {})["启用"] = True
    if args.keepalive or args.watch_cities:
        return socket_keepalive.run(qq, config, with_daily=args.daily)

    if args.capture:
        from tankstorm import live_capture
        return live_capture.run(qq, config)

    # 国战自动战斗：连一次、打 N 次、退出
    if args.country_war:
        return socket_keepalive.run_country_war_once(qq, config,
                                                     args.country_war)
    if args.mordor:
        return socket_keepalive.run_own_legion_once(qq, config)

    # 城市玩家：连一次、开指定城市面板、把列表打出来、退出
    if args.route is not None:
        return socket_keepalive.run_route_once(
            qq, config, args.route, args.city_country)

    if args.atk and args.move is not None:
        return socket_keepalive.run_approach_once(
            qq, config, args.move, args.atk, times=args.atk_times,
            sweep=args.sweep, country=args.city_country)

    if args.move is not None:
        return socket_keepalive.run_move_once(
            qq, config, args.move, sweep=args.sweep, country=args.city_country)

    if args.city_players is not None:
        return socket_keepalive.run_city_players_once(
            qq, config, args.city_players, args.city_country, args.city_page)

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

    if args.escort is not None:
        return socket_keepalive.run_escort_once(qq, config, args.escort)

    # 每日任务：连一次、跑一轮、退出
    if args.daily:
        config.setdefault("每日任务", {})["启用"] = True
        return socket_keepalive.run_daily_once(qq, config)

    # 需要人工介入时把二维码推到 PushPlus。配了 QQ 号则走「推送登录」，
    # 手机QQ点确认即可，不用扫码（存图后同机扫码会被腾讯拒）。
    push_uin = (config.get("登录", {}) or {}).get("推送登录QQ号") or qq.uin or None

    def on_qr(path, pushed=False):
        if pushed:
            notify.send_qrcode(config, "坦克风暴：请在手机QQ点「确认登录」", path,
                               note=f"已向 QQ {push_uin} 推送登录确认，"
                                    f"<b>打开手机QQ点确认即可，不用扫码</b>。")
        else:
            notify.send_qrcode(config, "坦克风暴：请扫码登录", path,
                               note="请用<b>另一台设备</b>打开本条消息再扫码。")

    if lockqq.bind_uin():
        push_uin = lockqq.bind_uin()
    if args.login:
        if not qq.qr_login(on_qr=on_qr, push_uin=push_uin):
            return 1
    elif not qq.ensure_login(on_qr=on_qr, push_uin=push_uin):
        return 1
    if lockqq.refuse(qq):
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
