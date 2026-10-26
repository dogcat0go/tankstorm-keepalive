# tankstorm-keepalive  Copyright (C) 2026 Dimlitter
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# 本程序是自由软件：你可以依据自由软件基金会发布的 GNU Affero 通用公共许可证
# （第 3 版，或你选择的任何更新版本）之条款，再分发和/或修改它。
# 本程序希望能有用，但不提供任何担保；甚至不含适销性或特定用途适用性的默示担保。
# 详见随附的 LICENSE 文件，或 <https://www.gnu.org/licenses/>。
"""公会战 —— 参加（报名）。

这一项欠了很久：此前两份抓包里都找不到参战流程，任务表里长期写着
「参加尚未实现」。2026-08-30 的抓包解决了它，而且答案就在眼皮底下 ——
**`RceGuildOpt{type:70}` 就是"参加公会战"**。

为什么以前没认出来
------------------
type:70 的回包顶层只有 `type / ret / userGuild` 三个字段，看着就是"查看自己
的公会"，所以一直被当成浏览类请求放过了。真正的状态变化埋在两层嵌套加一个
通用字段名里：`userGuild.field17.field1`。

8/30 抓包的铁证（时刻是从 pcap 的包时间还原的，不是估的）：

| 请求 | 发出时刻 | 回包里的 userGuild.field17.field1 |
|---|---|---|
| `type:73`（公会列表）| 1788068602.4 | 1787556135 ← 5.93 天前的旧值 |
| `type:80`（列表翻页）| 1788068603.5 | 1787556135 ← 还是旧值 |
| **`type:70`** | **1788068607.7** | **1788068608** ← **同一秒，被刷新了** |

前后差 0.3 秒，而它前面 5 秒发的两条请求读到的都还是旧值。所以这个时间戳
就是**上次参加公会战的时刻**，由 type:70 写入。

⚠️ 那个 5.93 天只是"玩家有将近六天没参加过"，**不是活动周期**。
公会战是**一天一场**（用户确认）。所以限流按自然日算就对了，
别把这个间隔当成什么周期性依据。

限流依据
--------
两个条件都要满足才发：

  · `dayHasPK`（来自 type:73 的回包）为 true —— 今天有公会战活动
  · `userGuild.field17.field1` 不在今天之内 —— 今天还没参加过

判成败也落在同一个字段上：回包里的时间戳必须被刷新到"刚刚"，
而不是看 `ret`（type:70 的 ret 恒为 0，看它等于没看）。这是踩坑记录
第 15 条的做法：一个请求"成功"不等于这件事做成了。

⚠️ 与国战无关。这里是公会之间的战斗，走 RceGuildOpt；国战打魔多军团走的是
RceCountryOpt，两回事。
"""

import time

from . import sender
from .daily import _await_response, _drain_incoming, _nap, _read_path, raise_if_stopped
from .log import get_logger
from .proto_encode import encode_message

log = get_logger()

OPCODE = "0479"
RSE = "RseGuildOpt"

TYPE_SELF = 0           # 打开自己的公会，回包里才有今日捐献次数
TYPE_LIST = 73          # 公会列表，回包带 dayHasPK 和自己的参战时刻
TYPE_JOIN = 70          # 参加公会战

# 上次参加公会战的时刻。名字是 field17.field1 —— schema 里这两层都没解出真名，
# 含义靠 8/30 抓包的时刻对齐锁死（见模块开头的表）。
F_JOINED_AT = "userGuild.field17.field1"


def _fields(type_):
    """照抓包的字段集构造请求。

    真客户端 7 个字段全都显式写出来（其余 6 个都是 0），proto2 里"写了 0"
    和"没写"是两回事，所以一个都不能省 —— 这是踩坑记录第 6 条。
    """
    return {2: ("int32", type_), 4: ("int32", 0), 13: ("int32", 0),
            17: ("int32", 0), 18: ("int32", 0), 22: ("int32", 0),
            23: ("int32", 0)}


def _send(sock, rec, type_, fields=None):
    before = rec.seq_mark() if rec else 0
    body = encode_message(_fields(type_) if fields is None else fields,
                          omit_zero=False)
    sender.send_frame(sock, OPCODE, body, rec.rc4_c2s)
    return before


def _wait(sock, rec, since, type_, timeout=6.0):
    """等 RseGuildOpt，且必须是**本次 type** 的回包。

    公会这一族全走同一个 opcode（列表/战报/捐献/参战都是 0479），
    不核对 type 就会把上一步的回包当成这一步的结果。
    """
    return _await_response(sock, rec, RSE, since, timeout,
                           want=lambda d: d.get("type") == type_)


def _same_day(ts, now=None):
    """两个 unix 秒是不是同一个自然日（按本机时区）。

    公会战一天一场，所以"今天参加过没有"就是限流依据。按**本机自然日**算，
    和 logs/daily-state.json 的跨天逻辑保持一致 —— 整个每日任务体系都是这个
    模型，只给这一项换一套反而会前后矛盾。

    已知的小局限：服务端的"一天"要是不在本地零点翻篇，跨过它那一刻的那一次
    可能会被当成"今天已经参加过"而跳过。实际影响很小（每天本来也只跑一次），
    真遇到了再按服务端给的时刻去校准。
    """
    if not isinstance(ts, int) or isinstance(ts, bool) or ts <= 0:
        return False
    now = now if now is not None else time.time()
    return time.localtime(ts)[:3] == time.localtime(now)[:3]


def daily_join(rec, sock, config):
    """每日任务用：今天有公会战、且还没参加过，就参加。

    返回 (是否成功, 说明)，签名符合 daily.Task 的 runner 约定。
    """
    conf = (config.get("公会战", {}) or {})
    if not conf.get("自动参加", True):
        return True, "成功：未开启自动参加公会战，什么都没做"

    # 前置：开公会列表。dayHasPK 和"上次参加时刻"都在这条回包里。
    since = _send(sock, rec, TYPE_LIST)
    panel = _wait(sock, rec, since, TYPE_LIST)
    if not isinstance(panel, dict):
        return False, "读不到公会面板（type:73 没回包），不参加"

    has_pk = panel.get("dayHasPK")
    if has_pk is not True:
        return True, f"成功：今天没有公会战活动（dayHasPK={has_pk}），不用参加"

    joined_at = _read_path(panel, F_JOINED_AT)
    if joined_at is None:
        # 铁律：读不到依据就不做。这个字段是唯一能说明"参加过没有"的东西。
        return False, (f"读不到上次参加时刻（{F_JOINED_AT}），"
                       f"判断不了今天参加过没有，不发（读不到就不做）")
    if _same_day(joined_at):
        return True, (f"成功：今天已经参加过了"
                      f"（{time.strftime('%m-%d %H:%M', time.localtime(joined_at))}）")

    log.info("[公会战] 今天有活动，上次参加是 %s，准备报名",
             time.strftime("%m-%d %H:%M", time.localtime(joined_at))
             if joined_at else "（没有记录）")

    _nap(0.6)
    sent_at = time.time()
    since = _send(sock, rec, TYPE_JOIN)
    r = _wait(sock, rec, since, TYPE_JOIN)
    if not isinstance(r, dict):
        return False, "参加请求没有回包，停手"
    ret = r.get("ret")
    if ret not in (0, None):
        return False, f"服务器拒绝 ret={ret}"

    # 成败以状态变化为准：时间戳必须被刷新到"刚刚"。type:70 的 ret 恒为 0，
    # 光看它等于没看（踩坑记录第 15 条）。留 5 秒余量给时钟差。
    now_at = _read_path(r, F_JOINED_AT)
    if not isinstance(now_at, int) or isinstance(now_at, bool):
        return False, f"回包里读不到 {F_JOINED_AT}，认不出成败"
    if now_at < sent_at - 5:
        return False, (f"参加时刻没有被刷新（还是 "
                       f"{time.strftime('%m-%d %H:%M', time.localtime(now_at))}），"
                       f"没报上名")
    log.info("[公会战] 已参加，服务端记的时刻 %s",
             time.strftime("%m-%d %H:%M:%S", time.localtime(now_at)))
    return True, (f"成功：已参加公会战（服务端记录时刻 "
                  f"{time.strftime('%m-%d %H:%M', time.localtime(now_at))}）")


# 2026-10-24 13:18 抓包。公会战役进攻，不是上面的 type:70 报名。
# 请求字段是 type / page / npcID / boxID / buffID / listCountryID，和报名那 7 个 0 不是同一套。
# type:200 打开，type:208 进战场，type:217 按页读 npcList.lvl。
# 对上等级后反复 type:203，npcID 用列表里的 baseid。回包是 type:251，紧跟着战报。
# 体力是 thew（字段 44）。当前积分是 nowPersonMsg.point（字段 40 里的字段 1）。
# 251 后面是战报。战报读完再发 type:208 回到战场，回到了才隔 6.5 秒打下一次。
# 战报没完就发下一次 203，服务器不回 251。体力下降才算打上。不发 type:204 领奖。
TYPE_CAMP_OPEN = 200
TYPE_CAMP_ENTER = 208
TYPE_CAMP_LIST = 217
TYPE_CAMP_ATK = 203
TYPE_CAMP_RET = 251
_CAMP_PAGES = 5


def _camp_fields(type_, page=0, npc="0"):
    return {2: ("int32", type_), 4: ("int32", page),
            25: ("string", str(npc)), 26: ("int32", 0),
            27: ("int32", 0), 28: ("int32", 0)}


def _camp_window(now=None):
    """周五 10:00 到周六 22:00，按本机时区。周六 22:00 整点已经出窗口。"""
    lt = time.localtime(now if now is not None else time.time())
    mins = lt.tm_hour * 60 + lt.tm_min
    if lt.tm_wday == 4 and mins >= 10 * 60:
        return True
    return lt.tm_wday == 5 and mins < 22 * 60


def _camp_npcs(data):
    """据点列表。读不全返回 None，这一页没有据点返回空列表。"""
    if not isinstance(data, dict) or data.get("ret") not in (0, None):
        return None
    raw = data.get("npcList")
    if raw is None:
        return []
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return None
    rows = []
    for row in raw:
        if not isinstance(row, dict):
            return None
        lvl = row.get("lvl")
        base = row.get("baseid")
        if isinstance(lvl, bool) or not isinstance(lvl, int):
            return None
        if isinstance(base, bool):
            return None
        if isinstance(base, int):
            base = str(base)
        if not isinstance(base, str) or not base.isdigit() or int(base) <= 0:
            return None
        rows.append((lvl, base))
    return rows


def _camp_int(data, key):
    v = data.get(key) if isinstance(data, dict) else None
    if isinstance(v, bool) or not isinstance(v, int):
        return None
    return v


def _camp_thew(data):
    return _camp_int(data, "thew")


def _camp_score(data):
    """当前积分。2026-10-24：nowPersonMsg.point，打开时 0，每打一次加一截。"""
    msg = data.get("nowPersonMsg") if isinstance(data, dict) else None
    return _camp_int(msg, "point")


def peek_campaign(rec, sock):
    """打开公会战役，记下体力和当前积分。不进攻。"""
    from . import citydb
    opened = _wait(sock, rec, _send(sock, rec, TYPE_CAMP_OPEN,
                                    _camp_fields(TYPE_CAMP_OPEN)),
                   TYPE_CAMP_OPEN)
    if not isinstance(opened, dict) or opened.get("ret") not in (0, None):
        log.info("[公会战役] 打开没回包，体力和积分没更新")
        return False
    thew = _camp_thew(opened)
    score = _camp_score(opened)
    _nap(0.6)
    entered = _wait(sock, rec, _send(sock, rec, TYPE_CAMP_ENTER,
                                     _camp_fields(TYPE_CAMP_ENTER)),
                    TYPE_CAMP_ENTER)
    if isinstance(entered, dict) and entered.get("ret") in (0, None):
        got = _camp_thew(entered)
        if got is not None:
            thew = got
        got = _camp_score(entered)
        if got is not None:
            score = got
    if thew is None and score is None:
        log.info("[公会战役] 打开了，体力和积分都没读到")
        return False
    citydb.note_guild_camp(thew, score)
    log.info("[公会战役] 读到体力 %s，积分 %s",
             thew if thew is not None else "读不到",
             score if score is not None else "读不到")
    return True


def daily_campaign(rec, sock, config):
    """窗口内对上等级后按体力连打。窗口外不发包。一轮只算一次任务。"""
    from . import citydb
    if not citydb.base_tier(citydb.user_tier(citydb.attack_context_user() or 0)):
        return False, "活动助手需要高级pro订阅"
    if not _camp_window():
        return False, "不在周五 10:00 到周六 22:00（本机时间），跳过"
    level = (config.get("公会战役") or {}).get("敌方等级")
    if isinstance(level, bool) or not isinstance(level, int) or level <= 0:
        return False, "没配敌方等级（公会战役.敌方等级），不打"

    opened = _wait(sock, rec, _send(sock, rec, TYPE_CAMP_OPEN,
                                    _camp_fields(TYPE_CAMP_OPEN)),
                   TYPE_CAMP_OPEN)
    if not isinstance(opened, dict) or opened.get("ret") not in (0, None):
        return False, "打不开公会战役（type:200 没回包），不打"
    _nap(0.6)
    entered = _wait(sock, rec, _send(sock, rec, TYPE_CAMP_ENTER,
                                     _camp_fields(TYPE_CAMP_ENTER)),
                    TYPE_CAMP_ENTER)
    if not isinstance(entered, dict) or entered.get("ret") not in (0, None):
        return False, "进不了战场（type:208 没回包），不打"

    found = None
    thew = None
    score = _camp_score(opened)
    got = _camp_score(entered)
    if got is not None:
        score = got
    seen = set()
    for page in range(_CAMP_PAGES):
        raise_if_stopped()
        _nap(0.6)
        data = _wait(sock, rec, _send(
            sock, rec, TYPE_CAMP_LIST, _camp_fields(TYPE_CAMP_LIST, page)),
            TYPE_CAMP_LIST)
        if not isinstance(data, dict) or data.get("page") != page:
            return False, f"读不到第 {page} 页据点，不打"
        if data.get("ret") not in (0, None):
            return False, f"据点列表被拒绝 ret={data.get('ret')}，不打"
        rows = _camp_npcs(data)
        if rows is None:
            return False, f"第 {page} 页据点等级读不全，不打"
        got = _camp_thew(data)
        if got is None:
            return False, "据点列表里读不到体力，不打"
        thew = got
        got = _camp_score(data)
        if got is not None:
            score = got
        ids = {npc for _, npc in rows}
        if ids and ids <= seen:
            break
        seen |= ids
        for lvl, npc in rows:
            if lvl == level:
                found = npc
                break
        if found or not rows:
            break
    if not found:
        return False, f"据点列表里没有 {level} 级，不打"

    from .country_war import _CD_GEARS, _sleep_cd
    log.info("[公会战役] 打 %s 级据点 %s，当前体力 %s，积分 %s",
             level, found, thew, score if score is not None else "读不到")
    citydb.note_guild_camp(thew, score)
    start = thew
    hits = 0
    cost = None
    why = ""
    last_act = 0.0
    gap = float(_CD_GEARS[0])
    while True:
        raise_if_stopped()
        if not _camp_window():
            why = "已经离开周五 10:00 到周六 22:00，停手"
            break
        if thew <= 0 or (cost is not None and thew < cost):
            why = f"体力不够（剩余 {thew}），停手"
            break
        _sleep_cd(last_act, 0, gap, "公会战役")
        r = _wait(sock, rec, _send(
            sock, rec, TYPE_CAMP_ATK, _camp_fields(TYPE_CAMP_ATK, 0, found)),
            TYPE_CAMP_RET)
        if not isinstance(r, dict):
            why = "进攻没有回包，停手"
            break
        ret = r.get("ret")
        if ret not in (0, None):
            why = f"服务器拒绝 ret={ret}，停手"
            break
        now = _camp_thew(r)
        if now is None:
            why = "回包里读不到体力，停手"
            break
        got = _camp_score(r)
        if got is not None:
            score = got
        if now >= thew:
            why = f"体力没有下降（{thew}→{now}），停手"
            break
        cost = thew - now
        hits += 1
        log.info("[公会战役] 第 %s 次进攻 %s 级，体力 %s→%s，积分 %s",
                 hits, level, thew, now,
                 score if score is not None else "读不到")
        thew = now
        citydb.note_guild_camp(thew, score)
        # 251 一到就返回，战报还在后面。先读完再回战场，否则下一次进攻没有 251。
        quiet_until = time.time() + 10.0
        not_before = time.time() + 3.0
        idle_since = None
        while time.time() < quiet_until:
            raise_if_stopped()
            mark = rec.seq_mark() if rec else 0
            _drain_incoming()
            now_ts = time.time()
            if rec and rec.seq_mark() != mark:
                idle_since = None
            elif idle_since is None:
                idle_since = now_ts
            if now_ts >= not_before and idle_since is not None and now_ts - idle_since >= 1.0:
                break
            time.sleep(0.2)
        back = None
        for _try in range(2):
            back = _wait(sock, rec, _send(
                sock, rec, TYPE_CAMP_ENTER, _camp_fields(TYPE_CAMP_ENTER)),
                TYPE_CAMP_ENTER, timeout=8.0)
            if isinstance(back, dict) and back.get("ret") in (0, None):
                break
        if not isinstance(back, dict) or back.get("ret") not in (0, None):
            why = "打完没有回到战场，停手"
            break
        got = _camp_thew(back)
        if got is not None:
            thew = got
        got = _camp_score(back)
        if got is not None:
            score = got
        citydb.note_guild_camp(thew, score)
        last_act = time.time()
    point = (f"，当前积分 {score}（nowPersonMsg.point）" if score is not None
             else "，积分读不到")
    if hits <= 0:
        return False, why + point
    return True, (f"成功：{level} 级据点（{found}）打了 {hits} 次，"
                  f"体力 {start}→{thew}{point}。{why}")


# GuildContribute_2016092801.dat。一键用每种的第二行。
# 次数在 userGuild.field9，下标就是捐献类型，同类型两行共用。
# 类型 0 是勋章，日上限还是 -1，不捐。
# (contributeID, 类型下标, 日上限, 花费种类, 数量)
_DONATE_ROWS = (
    (2, 1, 3, "both", 2500000),   # 金属和石油，两边都要够
    (4, 2, 6, "feats", 5000),     # 功勋
    (6, 3, 6, "item", 1),         # 军令
)
_ORDER_ITEM = 10044               # 2026-10-04 抓包：contributeID=6 每包扣背包 10044 一件
_DONATE_NAME = {2: "金属石油", 4: "功勋", 6: "军令"}


def _donate_counts(data, missing_ok=False):
    """userGuild.field9 是今日各类型已捐次数。查询包里没写，就是今天还没捐。"""
    if not isinstance(data, dict):
        return None
    guild = data.get("userGuild")
    if not isinstance(guild, dict):
        return None
    if "field9" not in guild or guild.get("field9") is None:
        return [] if missing_ok else None
    raw = guild.get("field9")
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return [raw]
    if isinstance(raw, list) and all(
            isinstance(x, int) and not isinstance(x, bool) for x in raw):
        return raw
    return None


def _res(rec):
    got = rec.latest.get("RseUserInfo") if rec else None
    d = got[1] if got and isinstance(got[1], dict) else {}

    def num(key):
        v = d.get(key)
        return v if isinstance(v, int) and not isinstance(v, bool) else None

    return num("metal"), num("oil"), num("feats")


def _send_donate(sock, rec, name, contribute_id):
    fields = _fields(16)
    fields[6] = ("string", name)
    fields[12] = ("int32", contribute_id)
    before = rec.seq_mark() if rec else 0
    sender.send_frame(sock, OPCODE, encode_message(fields, omit_zero=False),
                      rec.rc4_c2s)
    return before


def _lack(kind, cost, metal, oil, feats, items):
    """够就返回空串。读不到和数量不够分开，读不到不能发。"""
    if kind == "both":
        if metal is None or oil is None:
            return "读不到金属或石油"
        if metal < cost or oil < cost:
            return "金属或石油不够"
    elif kind == "feats":
        if feats is None:
            return "读不到功勋"
        if feats < cost:
            return "功勋不够"
    elif items is None:
        return "读不到军令"
    elif items < cost:
        return "军令不够"
    return ""


def _spend(kind, cost, metal, oil, feats, items):
    if kind == "both":
        return metal - cost, oil - cost, feats, items
    if kind == "feats":
        return metal, oil, feats - cost, items
    return metal, oil, feats, items - cost


# 2026-10-24 22:09 抓包。领奖和参加的编号和早先记的相反。
# ntype=3 开面板，4 刷新候选，6 承接看板上的候选（小编号）。
# ntype=7 参加已经发布的团队任务（大编号）。今日参与次数加 1，公会积分不变。
# ntype=9 领取整队已完成的奖励（大编号）。公会积分加 10，参与次数不变。
# 领过的自己那一份 field6（bGetReward）为 true。接着会推一条 ntype=102，不用发。
# nFinishNum 就是游戏里「今日参与任务次数」。参加一次加一，领奖不加。
# 身上 field22 的任务已经算在这个数里，再加一遍会把 3 个算成 5 个。
# 公会活跃（type=350/351）是另一项，不写这个数。
# 星级不在协议里。4 星和 5 星的 nTaskType 来自 BaseGuildTask 的 LEVEL。
# 一天最多 5 个。付费刷新不发。
_BUILD_OP = "04f3"
_BUILD_RSE = "RseGuildBuildOpt"
_TASK_OPEN = 3
_TASK_REFRESH = 4
_TASK_PUBLISH = 6
_TASK_JOIN = 7
_TASK_CLAIM = 9
# 4 星、5 星里默认不参加：摧毁基地 4/5、战争令牌 24/25、消耗勋章 29/30、探索 39/40、公会捐献 49/50。
_STAR_IDS = frozenset({
    9, 10, 14, 15, 19, 20, 34, 35,
    44, 45, 54, 55, 59, 60, 64, 65, 69, 70,
})
_TASK_CAP = 5
_TASK_REFRESH_MAX = 8
# 名字、星级、说明。BaseGuildTask_2023071801.dat，列 VALUE / LEVEL / INSTRUCTIONS。
# 协议里只有 nTaskType，客户端自己查这张表。
_TASK_BOOK = {
    1: ("摧毁基地", 1, "完全摧毁10个基地"),
    2: ("摧毁基地", 2, "完全摧毁20个基地"),
    3: ("摧毁基地", 3, "完全摧毁30个基地"),
    4: ("摧毁基地", 4, "完全摧毁40个基地"),
    5: ("摧毁基地", 5, "完全摧毁60个基地"),
    6: ("国战进攻", 1, "国战进攻30次"),
    7: ("国战进攻", 2, "国战进攻90次"),
    8: ("国战进攻", 3, "国战进攻150次"),
    9: ("国战进攻", 4, "国战进攻300次"),
    10: ("国战进攻", 5, "国战进攻600次"),
    11: ("高级招募", 1, "高级招募英雄、将领、参谋、火炮20次"),
    12: ("高级招募", 2, "高级招募英雄、将领、参谋、火炮40次"),
    13: ("高级招募", 3, "高级招募英雄、将领、参谋、火炮60次"),
    14: ("高级招募", 4, "高级招募英雄、将领、参谋、火炮80次"),
    15: ("高级招募", 5, "高级招募英雄、将领、参谋、火炮100次"),
    16: ("征战世界", 1, "征战世界3次"),
    17: ("征战世界", 2, "征战世界6次"),
    18: ("征战世界", 3, "征战世界9次"),
    19: ("征战世界", 4, "征战世界12次"),
    20: ("征战世界", 5, "征战世界15次"),
    21: ("战争令牌", 1, "获得战争令牌经验1200"),
    22: ("战争令牌", 2, "获得战争令牌经验2400"),
    23: ("战争令牌", 3, "获得战争令牌经验3600"),
    24: ("战争令牌", 4, "获得战争令牌经验4800"),
    25: ("战争令牌", 5, "获得战争令牌经验6500"),
    26: ("消耗勋章", 1, "累计消耗500勋章"),
    27: ("消耗勋章", 2, "累计消耗1000勋章"),
    28: ("消耗勋章", 3, "累计消耗1500勋章"),
    29: ("消耗勋章", 4, "累计消耗3000勋章"),
    30: ("消耗勋章", 5, "累计消耗5000勋章"),
    31: ("争霸战", 1, "争霸战10次"),
    32: ("争霸战", 2, "争霸战20次"),
    33: ("争霸战", 3, "争霸战30次"),
    34: ("争霸战", 4, "争霸战40次"),
    35: ("争霸战", 5, "争霸战60次"),
    36: ("探索", 1, "实验室或自动化累计探索100次"),
    37: ("探索", 2, "实验室或自动化累计探索200次"),
    38: ("探索", 3, "实验室或自动化累计探索300次"),
    39: ("探索", 4, "实验室或自动化累计探索800次"),
    40: ("探索", 5, "实验室或自动化累计探索1500次"),
    41: ("剿灭军阀", 1, "合成剿灭军阀1次"),
    42: ("剿灭军阀", 2, "合成剿灭军阀2次"),
    43: ("剿灭军阀", 3, "合成剿灭军阀3次"),
    44: ("剿灭军阀", 4, "合成剿灭军阀4次"),
    45: ("剿灭军阀", 5, "合成剿灭军阀10次"),
    46: ("公会捐献", 1, "公会捐献15次"),
    47: ("公会捐献", 2, "公会捐献30次"),
    48: ("公会捐献", 3, "公会捐献45次"),
    49: ("公会捐献", 4, "公会捐献60次"),
    50: ("公会捐献", 5, "公会捐献100次"),
    51: ("每日活跃度", 1, "领取每日100活跃度奖励1次"),
    52: ("每日活跃度", 2, "领取每日100活跃度奖励2次"),
    53: ("每日活跃度", 3, "领取每日100活跃度奖励3次"),
    54: ("每日活跃度", 4, "领取每日100活跃度奖励4次"),
    55: ("每日活跃度", 5, "领取每日100活跃度奖励5次"),
    56: ("每日签到", 1, "每日签到1次"),
    57: ("每日签到", 2, "每日签到2次"),
    58: ("每日签到", 3, "每日签到3次"),
    59: ("每日签到", 4, "每日签到4次"),
    60: ("每日签到", 5, "每日签到5次"),
    61: ("战略技能训练", 1, "战略技能训练10次"),
    62: ("战略技能训练", 2, "战略技能训练20次"),
    63: ("战略技能训练", 3, "战略技能训练30次"),
    64: ("战略技能训练", 4, "战略技能训练40次"),
    65: ("战略技能训练", 5, "战略技能训练55次"),
    66: ("国家战功", 1, "国家累计获得战功50000"),
    67: ("国家战功", 2, "国家累计获得战功100000"),
    68: ("国家战功", 3, "国家累计获得战功150000"),
    69: ("国家战功", 4, "国家累计获得战功200000"),
    70: ("国家战功", 5, "国家累计获得战功300000"),
}
_ACT_OPEN = 350
_ACT_CLAIM = 351


def _build_send(sock, rec, ntype, task_id=0):
    fields = {1: ("int32", ntype)}
    if task_id:
        fields[3] = ("int32", task_id)
    before = rec.seq_mark() if rec else 0
    sender.send_frame(sock, _BUILD_OP, encode_message(fields), rec.rc4_c2s)
    return _await_response(
        sock, rec, _BUILD_RSE, before, 6.0,
        want=lambda d: isinstance(d, dict) and d.get("ntype") == ntype)


def _as_int(row, *keys):
    if not isinstance(row, dict):
        return None
    for key in keys:
        val = row.get(key)
        if isinstance(val, bool) or not isinstance(val, int):
            continue
        return val
    return None


def _task_rows(data, key):
    """field23 候选，field21 已发布，field22 自己的。每项 nID、nTaskType、进度、需求。"""
    raw = data.get(key) if isinstance(data, dict) else None
    rows = raw if isinstance(raw, list) else ([raw] if isinstance(raw, dict) else [])
    out = []
    for row in rows:
        nid = _as_int(row, "nID", "field1")
        kind = _as_int(row, "nTaskType", "field2")
        proc = _as_int(row, "nProcess", "field3") or 0
        need = _as_int(row, "nTotalNeed", "field4") or 0
        flag = row.get("bGetReward", row.get("field6"))
        got = flag is True or flag == 1
        if nid and kind and nid > 0:
            out.append((nid, kind, proc, need, got))
    return out


def _finish_num(panel):
    n = _as_int(panel, "nFinishNum")
    return 0 if n is None else n


def _as_count(val):
    if isinstance(val, bool) or not isinstance(val, int) or val < 0:
        return 0
    return val


def _team_done(panel, nid):
    """整队进度满了，游戏里才是「领取」。自己那一份满了仍是「强制完成」。

    field21 是团队任务，nProcess 是大家加起来的进度，nTotalNeed 是整队需求。
    """
    for tid, _kind, proc, need, *_rest in _task_rows(panel, "field21"):
        if tid == nid and need > 0 and proc >= need:
            return True
    return False


def guild_reward_ready(panel):
    """身上有整队都做完、还没领的任务。"""
    if not isinstance(panel, dict):
        return False
    return any(_team_done(panel, row[0]) and not row[4]
               for row in _task_rows(panel, "field22"))


def guild_hour_plan(finish_num, reward_ready, cap=_TASK_CAP):
    """这一小时走哪一支。

    finish_num 是面板上的 nFinishNum，游戏里叫今日参与任务次数。
    参加一次就加一，身上还在的任务已经算在里面，不再相加。领奖不加。
    活跃任务是另一项，不进这个数。不到 5 个就去领任务。
    已经满了，有做完的才领奖，没有就不发。
    """
    finish = _as_count(finish_num)
    limit = _as_count(cap) or _TASK_CAP
    if finish < limit:
        return "accept"
    if reward_ready:
        return "reward"
    return "idle"


def _task_text(kind):
    """名字、星级、说明。表里没有的不编一个名字。"""
    row = _TASK_BOOK.get(kind)
    if row:
        return row
    if isinstance(kind, int) and not isinstance(kind, bool) and kind > 0:
        return ("类型%d" % kind, 0, "")
    return ("任务", 0, "")


def _progress_text(proc, need):
    if isinstance(need, bool) or not isinstance(need, int) or need <= 0:
        return ""
    if isinstance(proc, bool) or not isinstance(proc, int) or proc < 0:
        proc = 0
    return "%d/%d" % (proc, need)


def guild_panel_board(panel, just_claimed=()):
    """面板上能看到的公会任务。给页面放在最近执行上面。

    身上的记进行中、等队友或可领奖。这一轮刚领走、面板上已经没有的，记已领奖。
    可领奖是整队都完成。自己做完、队友还没齐是等队友。
    """
    finish = _finish_num(panel) if isinstance(panel, dict) else 0
    mine = _task_rows(panel, "field22") if isinstance(panel, dict) else []
    tasks = []
    left = {}
    for nid, kind, proc, need, got in mine:
        name, star, detail = _task_text(kind)
        mine_full = (isinstance(need, int) and not isinstance(need, bool) and need > 0
                     and isinstance(proc, int) and not isinstance(proc, bool) and proc >= need)
        taken = got is True
        if taken:
            state = "已领奖"
        elif _team_done(panel, nid):
            state = "可领奖"
        elif mine_full:
            state = "等队友"
        else:
            state = "进行中"
        tasks.append({
            "name": name,
            "star": star,
            "state": state,
            "progress": _progress_text(proc, need),
            "detail": detail,
        })
        left[kind] = left.get(kind, 0) + 1
    for kind in just_claimed or ():
        if isinstance(kind, bool) or not isinstance(kind, int):
            continue
        if left.get(kind, 0) > 0:
            left[kind] -= 1
            continue
        name, star, detail = _task_text(kind)
        tasks.append({
            "name": name,
            "star": star,
            "state": "已领奖",
            "progress": "",
            "detail": detail,
        })
    from . import citydb
    return {
        "finish": finish,
        "cap": _TASK_CAP,
        "reward": guild_reward_ready(panel),
        "read_at": citydb.beijing_now().strftime("%Y-%m-%d %H:%M:%S"),
        "tasks": tasks,
    }


def _remember_guild_panel(panel, just_claimed=()):
    """把这次读到的面板记在登录账号上。记失败不影响领任务。"""
    if not isinstance(panel, dict):
        return
    try:
        from . import citydb
        user_id = citydb.attack_context_user()
        if not user_id:
            return
        citydb.save_guild_task_board(
            user_id, guild_panel_board(panel, just_claimed))
    except Exception:
        log.info("公会任务面板没记下", exc_info=True)


_STAR5 = frozenset({
    10, 15, 20, 35, 45, 55, 60, 65, 70,
})


def _star(kind):
    if kind in _STAR5:
        return 5
    if kind in _STAR_IDS:
        return 4
    return 0


def _best(rows, seen, mine):
    """5 星先于 4 星。已经接过、做满的不要。"""
    cands = [
        row for row in rows
        if row[0] not in seen and row[0] not in mine and _star(row[1]) >= 4
        and row[3] > 0 and row[2] < row[3]
    ]
    cands.sort(key=lambda row: _star(row[1]), reverse=True)
    return cands[0] if cands else None


def _claim_tasks(sock, rec, panel):
    """整队做完、自己还没领过的才领。ntype=9，编号用大 id。一条被拒就跳过。"""
    claimed = []
    seen = set()
    while True:
        raise_if_stopped()
        mine = [row for row in _task_rows(panel, "field22")
                if row[0] not in seen and not row[4] and _team_done(panel, row[0])]
        if not mine:
            return panel, claimed
        nid, kind, _proc, _need, _got = mine[0]
        seen.add(nid)
        got = _build_send(sock, rec, _TASK_CLAIM, nid)
        if not isinstance(got, dict):
            return None, claimed
        ret = got.get("nRet")
        if ret not in (0, None):
            log.info("[公会任务] 领奖被拒 nID=%s nRet=%s，跳过", nid, ret)
            continue
        claimed.append(kind)
        panel = got
        _remember_guild_panel(panel, claimed)
        _nap(0.35)


def _published(panel, prev):
    if isinstance(panel, dict) and "field21" in panel:
        return _task_rows(panel, "field21")
    return prev


def _guild_tasks(sock, rec):
    panel = _build_send(sock, rec, _TASK_OPEN)
    if not isinstance(panel, dict):
        return False, "失败：没收到公会任务面板"
    _remember_guild_panel(panel)
    finish = _finish_num(panel)
    plan = guild_hour_plan(finish, guild_reward_ready(panel))
    # 今日参与已经到 5，不再参加、不再刷新。能领奖才发 ntype=9。
    if plan != "accept":
        if plan == "idle":
            noted = _note_guild_joined(panel, finish)
            return True, "成功：今日参与 %d/%d，没有可领的奖" % (noted, _TASK_CAP)
        panel, claimed = _claim_tasks(sock, rec, panel)
        noted = _note_guild_joined(panel, finish)
        if panel is None:
            return False, "失败：领取已完成任务没有成功（今日参与 %d/%d）" % (noted, _TASK_CAP)
        _remember_guild_panel(panel, claimed)
        if not claimed:
            return True, "成功：今日参与 %d/%d，没有可领的奖" % (noted, _TASK_CAP)
        return True, "成功：今日参与 %d/%d，领奖 %d 个" % (
            noted, _TASK_CAP, len(claimed))
    # 参与次数就是名额。身上的任务已经算进 nFinishNum，不再扣一遍。
    # 接上的任务如果这一轮整队做完，顺手领掉。领奖不增加参与次数。
    room = _TASK_CAP - finish
    if room < 0:
        room = 0
    joined = []
    taken = []
    refreshes = 0
    seen = set()
    pub = _task_rows(panel, "field21")
    got_n = 0
    while got_n < room:
        raise_if_stopped()
        mine = {row[0] for row in _task_rows(panel, "field22")}
        pub = _published(panel, pub)
        live = _best(pub, seen, mine)
        if live is not None:
            nid, kind, _proc, _need, _got = live
            seen.add(nid)
            got = _build_send(sock, rec, _TASK_JOIN, nid)
            if not isinstance(got, dict):
                return False, "失败：参加已有团队任务没有回包"
            ret = got.get("nRet")
            if ret not in (0, None):
                return False, "失败：参加类型 %d 被拒 nRet=%s" % (kind, ret)
            joined.append(kind)
            panel = got
            _remember_guild_panel(panel)
            got_n += 1
            _nap(0.35)
            continue
        pick = _best(_task_rows(panel, "field23"), seen, mine)
        if pick is not None:
            nid, kind, _proc, _need, _got = pick
            seen.add(nid)
            got = _build_send(sock, rec, _TASK_PUBLISH, nid)
            if not isinstance(got, dict):
                return False, "失败：承接类型 %d 没有回包" % kind
            ret = got.get("nRet")
            if ret not in (0, None):
                return False, "失败：承接类型 %d 被拒 nRet=%s" % (kind, ret)
            taken.append(kind)
            panel = got
            _remember_guild_panel(panel)
            got_n += 1
            _nap(0.35)
            continue
        if refreshes >= _TASK_REFRESH_MAX:
            break
        credit = _as_int(panel, "nCreditFreshNum") or 0
        if credit > 0:
            break
        nxt = _build_send(sock, rec, _TASK_REFRESH)
        refreshes += 1
        if not isinstance(nxt, dict):
            return False, "失败：刷新没有回包"
        panel = nxt
        _remember_guild_panel(panel)
        _nap(0.35)
    panel, claimed = _claim_tasks(sock, rec, panel)
    noted = _note_guild_joined(panel, finish + len(joined) + len(taken))
    if panel is None:
        return False, "失败：领取已完成任务没有成功（今日参与 %d/%d）" % (noted, _TASK_CAP)
    _remember_guild_panel(panel, claimed)
    bits = []
    if joined:
        bits.append("参加已有 %d 个" % len(joined))
    if taken:
        bits.append("承接 %d 个" % len(taken))
    if claimed:
        bits.append("领奖 %d 个" % len(claimed))
    head = "今日参与 %d/%d" % (noted, _TASK_CAP)
    if not bits:
        return True, "成功：%s，没有可参加或可承接的 4 星、5 星，刷新 %d 次" % (head, refreshes)
    return True, "成功：%s，" % head + "，".join(bits) + "，刷新 %d 次" % refreshes


def _note_guild_joined(panel, fallback):
    """今日次数记面板上的参与次数，不把身上的任务再加一遍，也不记活跃任务。

    回包里有 nFinishNum 就用它。没有就用这一轮算出来的次数，避免把上次的数清掉。
    """
    from .daily import _load_state, _save_state
    if isinstance(panel, dict) and "nFinishNum" in panel:
        total = _finish_num(panel)
    elif isinstance(fallback, int) and not isinstance(fallback, bool) and fallback >= 0:
        total = fallback
    else:
        total = 0
    if total > _TASK_CAP:
        total = _TASK_CAP
    st = _load_state()
    prev = st.get("done", {}).get("公会任务", 0)
    if not isinstance(prev, int) or isinstance(prev, bool) or prev < 0:
        prev = 0
    if total != prev:
        st.setdefault("done", {})["公会任务"] = total
        _save_state(st)
    return total


def _touch_guild_hour():
    """这一轮已经看过面板。一小时内不再排，也不在日常轮次里再做一遍。"""
    from .daily import _load_state, _save_state
    st = _load_state()
    st.setdefault("last", {})["公会任务"] = time.time()
    _save_state(st)


def _flag_list(val):
    if isinstance(val, bool) or val is None:
        return []
    if isinstance(val, int):
        return [val]
    if isinstance(val, list):
        return [x for x in val if isinstance(x, int) and not isinstance(x, bool)]
    return []


def _guild_activity(sock, rec):
    """公会活跃。2026-10-23 23:15：type=350 开面板，type=351 按 0 起的档位领。

    atvQstMsg.field4 是可领标记，field3 是已领标记。0 也要写出来。
    """
    fields = _fields(_ACT_OPEN)
    before = rec.seq_mark() if rec else 0
    sender.send_frame(sock, OPCODE, encode_message(fields, omit_zero=False), rec.rc4_c2s)
    panel = _wait(sock, rec, before, _ACT_OPEN)
    if not isinstance(panel, dict):
        return False, "失败：没收到公会活跃"
    msg = panel.get("atvQstMsg")
    if not isinstance(msg, dict):
        return False, "失败：公会活跃面板没有奖励"
    ready = _flag_list(msg.get("field4"))
    got_flags = _flag_list(msg.get("field3"))
    claimed = []
    for i, flag in enumerate(ready):
        raise_if_stopped()
        if flag != 1:
            continue
        if i < len(got_flags) and got_flags[i] == 1:
            continue
        body = _fields(_ACT_CLAIM)
        body[30] = ("int32", i)
        before = rec.seq_mark() if rec else 0
        sender.send_frame(
            sock, OPCODE, encode_message(body, omit_zero=False), rec.rc4_c2s)
        got = _wait(sock, rec, before, _ACT_CLAIM)
        if not isinstance(got, dict):
            return False, "失败：领取公会活跃第 %d 档没有回包" % (i + 1)
        ret = got.get("ret")
        if ret not in (0, None):
            return False, "失败：领取公会活跃第 %d 档被拒 ret=%s" % (i + 1, ret)
        claimed.append(i + 1)
        _nap(0.35)
    if not claimed:
        return True, "公会活跃没有可领的"
    return True, "公会活跃领了第 %s 档" % "、".join(str(n) for n in claimed)


def claim_guild_rewards(rec, sock):
    """只领整队已完成、自己还没领过的奖。不参加、不承接。"""
    panel = _build_send(sock, rec, _TASK_OPEN)
    if not isinstance(panel, dict):
        return False, "失败：没收到公会任务面板"
    _remember_guild_panel(panel)
    finish = _finish_num(panel)
    panel, claimed = _claim_tasks(sock, rec, panel)
    noted = _note_guild_joined(panel, finish)
    if panel is None:
        return False, "失败：领取已完成任务没有成功（今日参与 %d/%d）" % (noted, _TASK_CAP)
    _remember_guild_panel(panel, claimed)
    if not claimed:
        return True, "成功：没有可领的奖（今日参与 %d/%d）" % (noted, _TASK_CAP)
    return True, "成功：领奖 %d 个（今日参与 %d/%d）" % (len(claimed), noted, _TASK_CAP)


def daily_guild_task(rec, sock, config):
    """参与没到 5 个就领 4 星、5 星任务。到了 5 个，能领奖才领。公会活跃另领，不计入这 5 个。"""
    ok, why = _guild_tasks(sock, rec)
    if ok:
        _touch_guild_hour()
    act_ok, act_why = _guild_activity(sock, rec)
    text = why + "。" + act_why
    if not ok and not act_ok:
        return False, text
    if not ok or not act_ok:
        return False, text
    return True, text


def _donate_total(counts):
    """三种一键的已捐次数相加，每种不超过当天上限，合计最多 15。"""
    total = 0
    for _, idx, limit, _, _ in _DONATE_ROWS:
        used = counts[idx] if idx < len(counts) else 0
        if isinstance(used, bool) or not isinstance(used, int) or used < 0:
            continue
        total += limit if used > limit else used
    return total


def _note_donate(counts):
    """把今天已经捐成的次数写进每日进度。没捐满也记，下次接着捐。"""
    total = _donate_total(counts)
    from .daily import _load_state, _save_state
    st = _load_state()
    st.setdefault("done", {})["公会捐献"] = total
    _save_state(st)
    return total


def daily_donate(rec, sock, config):
    """把金属石油、功勋、军令三种一键捐到当天上限。不捐勋章。"""
    got = rec.latest.get("RseInit") if rec else None
    name = got[1].get("username") if got and isinstance(got[1], dict) else None
    if not isinstance(name, str) or not name:
        return False, "读不到角色名，不捐"

    since = _send(sock, rec, TYPE_SELF)
    panel = _wait(sock, rec, since, TYPE_SELF)
    counts = _donate_counts(panel, missing_ok=True)
    if counts is None:
        return False, "读不到今日捐献次数，不捐（读不到就不做）"
    total = _note_donate(counts)

    metal, oil, feats = _res(rec)
    from .shop import _bag_count
    _, items = _bag_count(rec, _ORDER_ITEM)

    done = []
    short = []
    for cid, idx, limit, kind, cost in _DONATE_ROWS:
        label = _DONATE_NAME[cid]
        used = counts[idx] if idx < len(counts) else 0
        n = 0
        while used < limit:
            why = _lack(kind, cost, metal, oil, feats, items)
            if why:
                if why.startswith("读不到"):
                    return False, f"{label}{why}，不捐" + _donated(done)
                short.append(label + why)
                break
            _nap(0.4)
            sent = _send_donate(sock, rec, name, cid)
            r = _wait(sock, rec, sent, 16)
            if not isinstance(r, dict):
                return False, f"{label}没有回包" + _donated(done)
            ret = r.get("ret")
            if isinstance(ret, int) and not isinstance(ret, bool) and ret != 0:
                return False, f"{label}服务器返回 ret={ret}" + _donated(done)
            counts = _donate_counts(r)
            if counts is None:
                return False, f"{label}回包读不到捐献次数" + _donated(done)
            now = counts[idx] if idx < len(counts) else 0
            if now != used + 1:
                return False, f"{label}次数没有增加（{used}→{now}）" + _donated(done)
            used = now
            n += 1
            total = _note_donate(counts)
            metal, oil, feats, items = _spend(kind, cost, metal, oil, feats, items)
        if n:
            done.append(f"{label}{n}次")

    full = all(
        (counts[idx] if idx < len(counts) else 0) >= limit
        for _, idx, limit, _, _ in _DONATE_ROWS
    )
    text = "、".join(done)
    mark = f"（今日{total}/15）"
    if full:
        return True, "成功：" + (text or "今日金属石油、功勋、军令都已捐满") + mark
    head = (text + "；") if text else ""
    return False, head + "；".join(short) + "，跳过" + mark


def _donated(done):
    if not done:
        return ""
    return "（已捐" + "、".join(done) + "）"
