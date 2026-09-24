# tankstorm-keepalive  Copyright (C) 2026 Dimlitter
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# 本程序是自由软件：你可以依据自由软件基金会发布的 GNU Affero 通用公共许可证
# （第 3 版，或你选择的任何更新版本）之条款，再分发和/或修改它。
# 本程序希望能有用，但不提供任何担保；甚至不含适销性或特定用途适用性的默示担保。
# 详见随附的 LICENSE 文件，或 <https://www.gnu.org/licenses/>。
"""国战自动扫荡摩多军团。

机制来自 2026-08-29 抓包（8.29捕捉国战和争霸战.pcapng，14 分半完整会话，
上下行 RC4 全流校验通过）。全部动作走同一条 RceCountryOpt(0463)，靠 type 区分：

    type:2   刷新自己国家的面板   → countryData 里有行动力、当前城市、今日攻击次数
    type:3   打开某城市的面板     → cityData 里有"这城还有没有支援兵"
    type:45  召唤摩多军团支援兵   → 随后服务端推 RseCountryUserLst，给出攻击目标
    type:19  扫荡                 行动力 -15，dayatktimes +3；打玩家时带 atkUserID
    type:14  普通攻击             行动力 -5， dayatktimes +1；打玩家时带 atkUserID
    type:4   移动到相邻城市       行动力 -5（离线打人暂不发）

字段含义是靠**数量关系**锁死的，不是猜的：抓包里行动力
136→121→106→91→76→61→46→31→16 每次正好 -15，与玩家所述"一次扫荡消耗 15 点"
一致；恢复卡用掉后回到 121，正是其所述上限；普通攻击那次 121→116 恰为 -5。
支援兵士气 1000→700→400→100 每次 -300，四次打光，也与所述一致。

⚠️ 攻击目标 ID（atkUserID）**在任何响应里都不以字符串形式出现**。
它在 RseCountryUserLst.user.field3.field6，而 schema 把该字段类型标成了整数，
于是解码器把 "100030000" 这个字符串的后 8 字节当成 int64 读了出来。
还原方式见 _target_id()。这属于踩坑记录第三条那一类：值是对的，类型错了。
"""

import time

from . import daily as _daily
from . import sender
from .daily import _await_response, _beat, _nap, _read_path
from .log import get_logger
from .proto_encode import encode_message

log = get_logger()

OPCODE = "0463"
RSE = "RseCountryOpt"
USER_LST = "RseCountryUserLst"
BTL = "RseCountryBtlResult"

# 抓包实测的开销，用来在**发之前**就判断够不够，而不是发出去等服务端拒绝。
# "拿服务器的拒绝当探针"在这个项目里花掉过 60 勋章。
COST_SWEEP = 15      # type:19 扫荡
COST_ATTACK = 5      # type:14 普通攻击
WAIT_ATK = 3.0       # 扫荡/攻击回包超时，超时打下一次
WAIT_BTL = 2.0       # 战报超时，超时打下一次

# countryData 里这几个字段的含义由抓包的数量关系确定
F_POWER = "countryData.field13"     # 行动力
F_MORALE = "countryData.field14"    # 士气（被别人打会掉，掉光遣返主城）
F_CITY = "countryData.field6"       # 当前所在城市
F_MERIT = "countryData.field17"     # 累计战功


BAG_USE_OPCODE = "0440"       # RceBagItemUse
BAG_LST = "RseBagItemLst"
CARD_ITEM_ID = 40004          # 国战恢复卡，抓包实测：用掉后行动力与士气全满


def _find_bag_item(rec, item_id):
    """在背包里找某件物品，返回 (槽位ID, 数量)；没有返回 (None, 0)。

    抓包实测背包条目形如
        {"field1": 1035, "field2": 40004, "field3": 1696834514, "field4": 63}
    field1 是**槽位 ID**（正是 RceBagItemUse 请求里的 id），field2 是物品 ID，
    field4 是数量。槽位 ID 跟着这一格走，不能写死。
    """
    got = rec.latest.get(BAG_LST) if rec else None
    if not got or not isinstance(got[1], dict):
        return None, 0
    items = (got[1].get("bagItem") or {})
    if isinstance(items, dict):
        items = items.get("field1") or items.get("field2") or []
    if not isinstance(items, list):
        return None, 0
    for e in items:
        if isinstance(e, dict) and e.get("field2") == item_id:
            return e.get("field1"), int(e.get("field4") or 0)
    return None, 0


def _use_recovery_card(sock, rec, item_id):
    """用一张国战恢复卡。返回 (是否发出, 说明)。

    ⚠️ 这是全项目**唯一**主动发送 count 非零的地方。count 命中危险字段正则，
    平时一律钉死为 0；这里 count=1 表示"用一张卡"，消耗的是玩家自己背包里的
    道具、不花勋章，而且必须在 config 里显式打开开关才会走到。
    仍然守着铁律：先从背包读到数量 > 0 才发，读不到就不发。
    """
    slot, count = _find_bag_item(rec, item_id)
    if slot is None:
        return False, f"背包里没读到物品 {item_id}（读不到就不用）"
    if count <= 0:
        return False, f"国战恢复卡已用完（背包剩 {count} 张）"
    # 字段照抓包逐个对齐：真客户端**不发 selectID(1)**，只发 3/4/5/7。
    # proto2 里"写了 0"和"没写"是两回事，多发一个 selectID=0 就不是同一个包了。
    body = encode_message({3: ("int32", 1), 4: ("int32", slot),
                           5: ("int32", item_id), 7: ("int32", 0)},
                          omit_zero=False)
    sender.send_frame(sock, BAG_USE_OPCODE, body, rec.rc4_c2s)
    return True, f"已用掉 1 张国战恢复卡（用前背包有 {count} 张）"


def _fields(type_, country=0, city=0, atk=None, check=False, page=0):
    """照抓包的字段集构造请求。

    真客户端把 0 值字段也显式写出来，所以一律 omit_zero=False 发全套；
    只有攻击/扫荡才带 atkUserID，别的 type 抓包里根本没有这个字段。
    """
    f = {1: ("int32", 0),          # costCredit：花勋章的字段，永远 0
         2: ("int32", 0),          # count：同上
         3: ("int32", country),
         4: ("int32", type_),
         6: ("int32", city),
         7: ("int32", 0),
         9: ("bool", check),       # bCheckFinishSet：抓包里只有 type:2 是 true
         13: ("int32", page),      # pageInx：城市玩家列表翻页，开面板默认 0
         14: ("int32", 0),
         15: ("int32", 0),
         16: ("int32", 0)}
    if atk:
        f[5] = ("string", str(atk))
    return f


def _send(sock, rec, type_, **kw):
    """发一条 RceCountryOpt，返回发送前的消息序号（用来挑本次的回包）。"""
    before = rec.seq_mark() if rec else 0
    body = encode_message(_fields(type_, **kw), omit_zero=False)
    sender.send_frame(sock, OPCODE, body, rec.rc4_c2s)
    return before


def _wait(sock, rec, since, type_, timeout=6.0):
    """等 RseCountryOpt，且必须是**本次 type** 的回包。

    前置和动作是同一个 opcode、回包也同名，不核对 type 就会把上一步的回包
    当成这一步的结果 —— 这个坑项目里已经栽过两次。
    """
    return _await_response(sock, rec, RSE, since, timeout,
                           want=lambda d: d.get("type") == type_)


def _def_from_btl(data, uid=""):
    """从 RseCountryBtlResult 取出防守方。field8 剩余士气，field14 本击掉了多少。"""
    if not isinstance(data, dict):
        return None
    fr = data.get("fightReoprt")
    if not isinstance(fr, dict):
        return None
    defu = fr.get("field2")
    if not isinstance(defu, dict):
        return None
    if uid:
        duid = str(defu.get("field1") or "")
        if duid and duid != str(uid):
            return None
    return defu


def _target_id(rec, since=0):
    """从 RseCountryUserLst 还原攻击目标的 atkUserID 和它的剩余士气。

    since 是"只认这个消息序号之后到达的那份"。**不能省** —— rec.latest 里存的
    可能是上一轮留下的旧列表。2026-08-29 实盘就栽在这里：召唤请求刚发出去，
    读到的还是召唤前那份（支援兵只剩 100 士气、下一击就死），拿它去打，
    服务端回 ret=21。这和"把前置的回包当成动作结果"是同一类错误，
    只不过这次是把上一次的推送当成了这一次的。

    schema 把 user.field3.field6 标成了整数，实际是字符串。抓包实测：
        3472328296278011952 → little-endian 8 字节 → "00030000"
    而客户端真正发出去的是 "100030000"，首字符 '1' 落在字段边界之外，
    所以还原时补回前缀。三次召唤分别得到 100030000 / 100020000 / 100010000，
    与请求逐字对上。

    读不到返回 (None, 士气或None)。
    """
    got = rec.latest.get(USER_LST) if rec else None
    if not got or not isinstance(got[1], dict):
        return None, None
    if got[0] <= since:          # 是旧的那份，当作没读到
        return None, None
    user = got[1].get("user")
    if isinstance(user, list):          # 城里有真人玩家时会是个列表
        user = next((u for u in user if isinstance(u, dict)), None)
    if not isinstance(user, dict):
        return None, None
    morale = user.get("field7")
    raw = (user.get("field3") or {}).get("field6")
    if not isinstance(raw, int) or isinstance(raw, bool):
        return None, morale
    try:
        tail = raw.to_bytes(8, "little").decode("latin-1")
    except (OverflowError, UnicodeDecodeError):
        return None, morale
    if not tail.isdigit():
        return None, morale
    return "1" + tail, morale


def _find_npc_city(sock, rec, npc_country, configured, current_city):
    """确定摩多驻地的城市 ID。返回 (城市ID, 说明)；找不到返回 (None, 原因)。

    配置里填了就用配置的，只做一次校验。没填就从当前城市推一个候选：
    抓包里玩家在曼彻斯特(3201)，相邻的摩多驻地是 32010，正好是 ×10。
    这只有一个数据点，所以**只当候选、不当结论** —— 发一次 type:3 去试，
    再看回包里 cityData.field2 是不是摩多的国家 ID，验过了才用。

    试探是安全的：type:3 只是开面板，不花行动力，也不会改变任何状态。
    """
    cands = []
    if configured:
        cands.append(int(configured))
    elif current_city:
        cands.append(int(current_city) * 10)

    for city in cands:
        since = _send(sock, rec, 3, country=npc_country, city=city)
        cd = _wait(sock, rec, since, 3)
        if not isinstance(cd, dict):
            continue
        owner = _read_path(cd, "cityData.field2")
        got = _read_path(cd, "cityData.field3")
        if owner == npc_country and got == city:
            return city, f"摩多驻地城市 {city}（已校验 owner={owner}）"
    if configured:
        return None, (f"配置里的摩多驻地城市ID={configured} 校验不通过，"
                      f"可能站错位置或该城不属于国家 {npc_country}")
    return None, ("没配摩多驻地城市ID，按当前城市推算的候选也没验过。"
                  "请先在游戏里走到能打摩多军团的城市，再把国战面板那条 "
                  "type:3 请求里的 cityID 填进 config 的「摩多驻地城市ID」")


def _panel(sock, rec, country):
    """刷新自己国家的面板。返回 (行动力, 当前城市, 今日攻击次数, 原始响应)。

    回包偶尔会丢，连着再读两次。对端已经把连接掐了就立刻停，别空等。
    """
    for n in range(1, 3):
        t0 = time.time()
        try:
            since = _send(sock, rec, 2, country=country, check=True)
            data = _wait(sock, rec, since, 2, timeout=WAIT_ATK)
        except OSError:
            log.info("[国战] 连接已断开，读不了面板")
            return None, None, None, None
        if isinstance(data, dict):
            return (_read_path(data, F_POWER), _read_path(data, F_CITY),
                    data.get("dayatktimes"), data)
        if time.time() - t0 < 0.4:
            log.info("[国战] 连接已断开，不再重试读面板")
            return None, None, None, None
        if n < 2:
            log.info("[国战] 读不到面板，1 秒后再读（%d/2）", n)
            _nap(1.0)
    return None, None, None, None


def daily_attack(rec, sock, config):
    """每日任务用：对摩多军团做 10 次**普通攻击**。

    每日任务只要求攻击次数，不要求战功，所以固定用 type:14（每次 5 点行动力），
    不用扫荡 —— 扫荡一次 15 点，拿来刷次数太贵。
    返回 (是否成功, 说明)，签名符合 daily.Task 的 runner 约定。
    """
    conf = (config.get("国战", {}) or {})
    want = int(conf.get("每日攻击次数", 10))
    out = run(rec, sock, config, rounds=want, attack_only=True)
    done = out["攻击"] + out["扫荡"]
    ok = done >= want
    # 汇总那边是按 v.startswith("成功") 判 ✅/❌ 的（socket_keepalive._push_daily_summary），
    # 所以成功时开头必须是"成功"两个字，否则 10/10 打满了也会显示成失败。
    why = (f"{'成功：' if ok else ''}攻击 {done}/{want} 次；"
           f"战功 +{out['战功']}；剩余行动力 {out.get('剩余行动力')}")
    if out["停止原因"] and not ok:
        why += f"；{out['停止原因']}"
    return ok, why


def run(rec, sock, config: dict, rounds: int = 0, beat=None,
        attack_only: bool = False) -> dict:
    """自动扫荡摩多军团。rounds 是最多打多少次，返回成果字典。

    有意**不做自动移动**：抓包里玩家全程待在同一座城，"当前城市"那个字段
    自始至终没变过，所以它到底是不是当前位置**并没有得到验证**。而在边路被
    别国玩家打掉士气会被遣返主城，位置随时可能变。在拿到"位置确实变了"的
    抓包之前，宁可读不到就停，也不猜着发移动指令白烧行动力。
    """
    conf = (config.get("国战", {}) or {})
    # 自己是哪个国家：优先从服务端读（登录时就推来了），配置里填了非 0 才覆盖。
    # 以前写死 3，那只是这个号是英国，换个号就错 —— 而且争霸战的名次是本国
    # 内部排名，这个值错了整张名单都不对。
    country = int(conf.get("自己国家ID") or 0) or _daily.read_my_country(rec)
    npc_country = int(conf.get("摩多国家ID", 21))
    npc_city = int(conf.get("摩多驻地城市ID", 32010))
    cooldown = float(conf.get("扫荡间隔秒", 15))
    rounds = int(rounds or conf.get("默认次数", 0))
    use_card = bool(conf.get("自动使用国战恢复卡", False))
    card_limit = int(conf.get("单次最多用几张恢复卡", 1))
    card_item = int(conf.get("国战恢复卡物品ID", CARD_ITEM_ID))

    out = {"扫荡": 0, "攻击": 0, "召唤": 0, "战功": 0, "用卡": 0, "停止原因": ""}
    if rounds <= 0:
        out["停止原因"] = "次数为 0，什么都没做"
        return out
    if not country:
        # 读不到就停手。猜一个国家 ID 发出去，轻则请求无效，重则打错国家。
        out["停止原因"] = ("读不到自己的国家ID（RseFightSimpInfo.countryid 和 "
                        "RseLoad.countryData.field5 都没有），停手；"
                        "可在 config 的「国战.自己国家ID」里手填")
        return out
    log.info("[国战] 自己国家ID=%s（%s）", country,
             "配置指定" if conf.get("自己国家ID") else "从服务端读到")

    # 打几百次要很久，全程必须续心跳。
    # beat 为 None 时**不能动** _daily._BEAT —— 作为每日任务被调用时，
    # daily.run() 已经装好了心跳器，这里再赋一次 None 就把它废了。
    # 2026-08-29 实盘：国战跑了两分半，"共发心跳 0 次"，就是这么来的。
    prev = _daily._BEAT
    if beat is not None:
        _daily._BEAT = beat
    try:
        return _loop(rec, sock, rounds, country, npc_country, npc_city,
                     cooldown, out, attack_only, use_card, card_limit,
                     card_item)
    finally:
        _daily._BEAT = prev


def _loop(rec, sock, rounds, country, npc_country, npc_city, cooldown, out,
          attack_only=False, use_card=False, card_limit=1,
          card_item=CARD_ITEM_ID):
    merit0 = None
    last_act = 0.0
    located = False
    force_summon = False     # 上一击被拒时置位，下一轮强制换个新目标
    fails = 0                # 连续被拒次数
    target = None            # 当前攻击目标，跨轮保留
    npc_morale = None        # 它剩多少士气
    cards_used = 0           # 本次用掉几张恢复卡

    for i in range(1, rounds + 1):
        power, city, atk_times, panel = _panel(sock, rec, country)
        if power is None:
            out["停止原因"] = "读不到国战面板（行动力未知），停手"
            break
        if merit0 is None:
            merit0 = _read_path(panel, F_MERIT) or 0

        # 第一轮先把摩多驻地的城市 ID 定下来（配置没填就按当前城市推算并校验）
        if not located:
            npc_city, why = _find_npc_city(sock, rec, npc_country,
                                           npc_city, city)
            if npc_city is None:
                out["停止原因"] = why
                break
            log.info("[国战] %s；当前所在城市 %s", why, city)
            located = True

        if power < COST_ATTACK:
            if not use_card or cards_used >= card_limit:
                why = (f"行动力只剩 {power}，连普通攻击都不够（要 {COST_ATTACK}）")
                if use_card and cards_used >= card_limit:
                    why += f"；本次已用掉 {cards_used} 张恢复卡，达到上限"
                elif not use_card:
                    why += "；未开启自动使用国战恢复卡"
                out["停止原因"] = why
                break
            sent, msg = _use_recovery_card(sock, rec, card_item)
            log.info("[国战] 行动力不足，%s", msg)
            if not sent:
                out["停止原因"] = f"行动力只剩 {power}，且{msg}"
                break
            cards_used += 1
            out["用卡"] = cards_used
            _nap(2.0)                    # 等服务端把新的行动力推回来
            power, city, atk_times, panel = _panel(sock, rec, country)
            if power is None or power < COST_ATTACK:
                out["停止原因"] = f"用了恢复卡但行动力仍不足（{power}），停手"
                break
            log.info("[国战] 恢复卡生效，行动力回到 %d", power)

        # 开摩多驻地的城市面板，看还有没有支援兵
        since = _send(sock, rec, 3, country=npc_country, city=npc_city)
        cd = _wait(sock, rec, since, 3)
        if not isinstance(cd, dict):
            out["停止原因"] = "打开摩多驻地面板没有回包，停手"
            break
        has_npc = _read_path(cd, "cityData.field5")

        # 服务端不是每次开面板都推 RseCountryUserLst，所以**目标要跨轮保留**：
        # 只在真的没有目标、或上一击被拒时才重新召唤。
        # 支援兵是功勋买来的消耗品，每轮召唤一次等于打 10 下烧 10 张，
        # 而实际三张就够 —— 2026-08-29 实盘发现的浪费。
        fresh, fresh_morale = _target_id(rec, since)
        if fresh is not None:                 # 有新推送就更新缓存
            target, npc_morale = fresh, fresh_morale
        if (not has_npc) or target is None or not npc_morale or force_summon:
            mark = rec.seq_mark() if rec else 0
            since = _send(sock, rec, 45, country=npc_country, city=npc_city)
            if not isinstance(_wait(sock, rec, since, 45), dict):
                out["停止原因"] = "召唤支援兵没有回包，停手"
                break
            out["召唤"] += 1
            # 等**召唤之后**才推来的那份列表，最多等 6 秒
            target, npc_morale = None, None
            deadline = time.time() + 6
            while time.time() < deadline:
                target, npc_morale = _target_id(rec, mark)
                if target is not None:
                    break
                _beat()
                try:
                    sock.settimeout(0.5)
                    sock.recv(8192)
                except Exception:
                    pass
            if target is None:
                out["停止原因"] = "召唤后 6 秒内没等到新的目标列表，停手"
                break
            force_summon = False
            log.info("[国战] 已召唤支援兵，目标 %s（士气 %s）", target, npc_morale)

        # 够 15 点就扫荡，不够就退而求其次用普通攻击。
        # attack_only 是每日任务模式：只要次数不要战功，扫荡太贵。
        if power >= COST_SWEEP and not attack_only:
            act, name, cost = 19, "扫荡", COST_SWEEP
        else:
            act, name, cost = 14, "攻击", COST_ATTACK

        # 冷却对**扫荡和普通攻击是共用的**，不是扫荡专属。
        # 2026-08-29 实盘发现：给扫荡等了 15 秒，一次都没被拒；而普通攻击没等，
        # 成功一次之后隔 1~3 秒再发就固定 ret=21。原先只给扫荡加等待是错的。
        wait = cooldown - (time.time() - last_act)
        if wait > 0:
            log.info("[国战] %s冷却，等 %.0f 秒", name, wait)
            _nap(wait)

        since = _send(sock, rec, act, country=npc_country, city=npc_city,
                      atk=target)
        last_act = time.time()          # 冷却从**发出时刻**起算，玩家实测如此
        r = _wait(sock, rec, since, act)
        if not isinstance(r, dict):
            out["停止原因"] = f"{name}没有回包，停手"
            break
        ret = r.get("ret")
        if ret not in (0, None):
            # 目标可能刚被打死（士气归零），换一个再试。给一次机会，
            # 连着两次被拒就停 —— 不拿服务端的拒绝当探针反复撞。
            fails += 1
            if fails >= 2:
                out["停止原因"] = f"{name}连续两次被服务端拒绝 ret={ret}，停手"
                break
            log.info("[国战] %s被拒 ret=%s，换个支援兵重试一次", name, ret)
            force_summon = True
            _nap(1.0)
            continue
        fails = 0

        out["扫荡" if act == 19 else "攻击"] += 1
        log.info("[国战] 第 %d/%d 轮：%s 成功，行动力 %d→约 %d，今日攻击次数 %s",
                 i, rounds, name, power, power - cost, atk_times)
        _nap(1.0)
        # 打完服务端会推新的列表，顺手读一下这个目标还剩多少士气。
        # 归零就清掉，下一轮自然会召唤新的 —— 比等着被 ret 拒绝再补召唤省一次请求。
        t2, m2 = _target_id(rec, since)
        if t2 is not None:
            target, npc_morale = t2, m2
        if npc_morale is not None and npc_morale <= 0:
            log.info("[国战] 目标 %s 士气已归零，下一轮换新的", target)
            target, npc_morale = None, None

    # 收尾再读一次面板，算出这轮总共挣了多少战功
    power, city, atk_times, panel = _panel(sock, rec, country)
    if panel is not None and merit0 is not None:
        out["战功"] = (_read_path(panel, F_MERIT) or 0) - merit0
    out["剩余行动力"] = power
    out["今日攻击次数"] = atk_times
    if not out["停止原因"]:
        out["停止原因"] = "已打满设定次数"
    return out


# CountryUser（RseCountryUserLst.user）字段。嵌套消息没有 opcode，schema
# 解出来全是 fieldN；名字来自 docs/redwar.proto 的 CountryUser。
# field3(baseid) 是 string，解码器常把它当嵌套 protobuf 读，还原方式与
# _target_id() 相同，见那条函数的注释。
_USER_INT = (
    (1, "leaguePoint"), (2, "leagueTitle"), (6, "lvl"), (7, "morale"),
    (8, "combatPowerValue"), (9, "officerPosition"), (10, "vipType"),
    (11, "vipLevel"), (12, "countryID"), (13, "regionID"),
)


def _as_text(v):
    """把解码器吐出来的值尽量还原成可读字符串。中文名优先，避免误当 hex。"""
    if isinstance(v, bytes):
        for enc in ("utf-8", "gbk"):
            try:
                t = v.decode(enc).strip()
                if t:
                    return t
            except UnicodeDecodeError:
                continue
        return None
    if isinstance(v, str) and v.strip():
        s = v.strip()
        if any("\u4e00" <= c <= "\u9fff" for c in s):
            return s
        try:
            t = s.encode("latin-1").decode("utf-8").strip()
            if t and any("\u4e00" <= c <= "\u9fff" for c in t):
                return t
        except UnicodeError:
            pass
        if len(s) >= 4 and len(s) % 2 == 0 and all(c in "0123456789abcdefABCDEF" for c in s):
            try:
                t = bytes.fromhex(s).decode("utf-8")
                if t.isprintable() and t.strip():
                    return t.strip()
            except (ValueError, UnicodeDecodeError):
                pass
        return s
    if isinstance(v, dict):
        cjk, other = [], []
        for x in v.values():
            t = _as_text(x)
            if not t or (t.isdigit() and len(t) >= 8):
                continue
            (cjk if any("\u4e00" <= c <= "\u9fff" for c in t) else other).append(t)
        return (cjk or other or [None])[0]
    return None


def _as_uid(v):
    """还原 CountryUser.baseid。真人 uid 是字符串；NPC 支援兵会走 _target_id 那条纠偏。"""
    if isinstance(v, str) and v.strip():
        return v.strip()
    if not isinstance(v, dict):
        return None
    raw = v.get("field6")
    if isinstance(raw, int) and not isinstance(raw, bool):
        try:
            tail = raw.to_bytes(8, "little").decode("latin-1")
        except (OverflowError, UnicodeDecodeError):
            return None
        if tail.isdigit():
            return "1" + tail
        if tail.isprintable():
            return tail.rstrip("\x00") or None
    for x in v.values():
        if isinstance(x, str) and x.isdigit() and len(x) >= 8:
            return x
    return None


def _parse_city_user(u):
    """把 RseCountryUserLst.user 的 fieldN 字典翻成带名字的玩家记录。"""
    if not isinstance(u, dict):
        return None
    out = {}
    uid = _as_uid(u.get("field3"))
    if uid:
        out["uid"] = uid
    name = _as_text(u.get("field5"))
    if name:
        out["name"] = name
    pic = _as_text(u.get("field4"))
    if pic:
        out["pic"] = pic
    for n, key in _USER_INT:
        v = u.get(f"field{n}")
        if isinstance(v, int) and not isinstance(v, bool):
            out[key] = v
        elif isinstance(v, bool) and key == "bLeaveLeague":
            out[key] = v
    leave = u.get("field14")
    if isinstance(leave, bool):
        out["bLeaveLeague"] = leave
    return out or None


def _users_in(data):
    """从一条 RseCountryUserLst 取出玩家列表。user 可能是 dict 或 list。"""
    if not isinstance(data, dict):
        return []
    user = data.get("user")
    if isinstance(user, dict):
        user = [user]
    if not isinstance(user, list):
        return []
    out = []
    for u in user:
        p = _parse_city_user(u)
        if p:
            out.append(p)
    return out


def _collect_city_users(rec, since, city_id):
    """从 rec.recent 里捞 since 之后、属于这座城的 RseCountryUserLst。"""
    hist = (getattr(rec, "recent", {}) or {}).get(USER_LST) or []
    latest = (rec.latest.get(USER_LST) if rec else None) or (None, None)
    rows = list(hist)
    if latest[0] is not None and all(seq != latest[0] for seq, _ in rows):
        rows.append(latest)
    got, seen = [], set()
    for seq, data in rows:
        if seq is None or seq <= since or not isinstance(data, dict):
            continue
        cid = data.get("nCityID")
        if cid not in (None, city_id):
            continue
        for p in _users_in(data):
            key = p.get("uid") or id(p)
            if key in seen:
                continue
            seen.add(key)
            got.append(p)
    return got


def _listed_user(users, uid):
    uid = str(uid or "")
    for p in users or []:
        if str(p.get("uid") or "") == uid:
            return p
    return None


def _wait_city_users(sock, rec, since, city_id, timeout=6.0):
    """type:3 开面板后等玩家列表。服务端不一定马上推，所以多收一会儿。"""
    deadline = time.time() + timeout
    best = []
    while time.time() < deadline:
        best = _collect_city_users(rec, since, city_id)
        if best:
            # 已经有人了再多收 0.8 秒，把同一批后续包捞齐
            extra = time.time() + 0.8
            while time.time() < extra:
                _beat()
                try:
                    sock.settimeout(0.4)
                    if not sock.recv(8192):
                        return _collect_city_users(rec, since, city_id)
                except TimeoutError:
                    continue
                except OSError:
                    return _collect_city_users(rec, since, city_id)
            return _collect_city_users(rec, since, city_id)
        _beat()
        try:
            sock.settimeout(0.5)
            if not sock.recv(8192):
                break
        except TimeoutError:
            continue
        except OSError:
            break
    return best


def list_city_players(rec, sock, config: dict, city_id: int,
                      country: int = 0, beat=None, on_page=None,
                      start_page: int = 0) -> dict:
    """只读：打开指定城市面板，收集城里所有玩家。

    走 RceCountryOpt type:3（开面板，不花行动力）。玩家在
    RseCountryUserLst；人多时用 pageInx 翻页。on_page(batch, page) 每页回调。
    start_page 从 0 起算，用来断线后续拉。返回另带 last_page。
    """
    conf = (config.get("国战", {}) or {})
    city_id = int(city_id)
    start_page = max(0, int(start_page or 0))
    country = int(country or conf.get("自己国家ID") or 0) \
        or _daily.read_my_country(rec)
    out = {"city": city_id, "owner": None, "userCnt": None, "玩家": [],
           "原因": "", "last_page": start_page, "start_page": start_page}
    if not city_id:
        out["原因"] = "城市 ID 不能为 0"
        return out
    if not country:
        out["原因"] = ("读不到自己的国家ID，停手；"
                      "可在 config 的「国战.自己国家ID」里手填，"
                      "或命令行加 --city-country")
        return out

    prev = _daily._BEAT
    if beat is not None:
        _daily._BEAT = beat
    try:
        return _list_city_players(rec, sock, city_id, country, out, on_page,
                                 start_page)
    finally:
        _daily._BEAT = prev


def _list_city_players(rec, sock, city_id, country, out, on_page=None,
                      start_page=0):
    # 先刷自己国家的面板，跟真客户端进世界地图的顺序一致；也用来确认连着。
    power, loc, _, panel = _panel(sock, rec, country)
    if panel is None:
        out["原因"] = "读不到国战面板，停手"
        return out
    log.info("[城市玩家] 自己国家=%s 当前城市=%s 行动力=%s；查询城市 %s，从第 %d 页开始",
             country, loc, power, city_id, start_page)

    players, seen = [], set()
    owner = None
    total = None
    used_country = country
    # 芝加哥实测约 6000 人、每页 15 条。
    page_size, max_pages = 15, 2000
    last_ok = start_page - 1

    for page in range(start_page, max_pages):
        since = _send(sock, rec, 3, country=used_country, city=city_id, page=page)
        cd = _wait(sock, rec, since, 3)
        if not isinstance(cd, dict):
            out["原因"] = (f"翻到第 {page} 页时没有回包（连接断开或超时），"
                          f"下次用 --city-page {page} 从这里继续")
            break
        ret = cd.get("ret")
        if ret not in (0, None):
            out["原因"] = (f"打开城市面板被拒 ret={ret}（停在第 {page} 页），"
                          f"下次用 --city-page {page} 继续")
            break

        got_city = _read_path(cd, "cityData.field3")
        owner = _read_path(cd, "cityData.field2")
        # cityData.field5 是 CityData.userCnt；国战里当「还有没有支援兵」用，
        # 玩家城里就是人数。RseCountryOpt.userCnt 是同一份计数的顶层字段。
        cnt = cd.get("userCnt")
        if not isinstance(cnt, int) or isinstance(cnt, bool):
            cnt = _read_path(cd, "cityData.field5")
        if isinstance(cnt, int) and not isinstance(cnt, bool):
            total = cnt

        if page == start_page and got_city not in (None, city_id):
            # 可能国家填错了：回包里带了归属国，换那个国家再开一次。
            if isinstance(owner, int) and owner and owner != used_country:
                log.info("[城市玩家] 面板回的城市是 %s、归属国 %s，"
                         "改用归属国重开", got_city, owner)
                used_country = owner
                since = _send(sock, rec, 3, country=used_country,
                              city=city_id, page=page)
                cd = _wait(sock, rec, since, 3)
                if not isinstance(cd, dict):
                    out["原因"] = "用归属国重开城市面板没有回包"
                    break
                got_city = _read_path(cd, "cityData.field3")
                owner = _read_path(cd, "cityData.field2")
                cnt = cd.get("userCnt")
                if not isinstance(cnt, int) or isinstance(cnt, bool):
                    cnt = _read_path(cd, "cityData.field5")
                if isinstance(cnt, int) and not isinstance(cnt, bool):
                    total = cnt
            if got_city not in (None, city_id):
                out["原因"] = (f"面板回的城市ID={got_city}，与请求的 {city_id} 不一致")
                break

        if total == 0:
            break

        batch = _wait_city_users(sock, rec, since, city_id)
        new = 0
        for p in batch:
            p["page"] = page
            key = p.get("uid")
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            players.append(p)
            new += 1
        last_ok = page
        out["last_page"] = page
        log.info("[城市玩家] 第 %d 页：本页 %d 人，累计 %d%s",
                 page, new, len(players),
                 f" / {total}" if total is not None else "")
        if on_page:
            on_page(batch, page)
        if new == 0:
            break
        if total is not None:
            max_pages = min(2000, max(page + 2,
                                      (int(total) + page_size - 1) // page_size + 2))
        if total is not None and start_page == 0 and len(players) >= total:
            break
        if total is not None and start_page > 0:
            # 续拉时 len(players) 只是本轮人数，用页码判断是否到末
            if (page + 1) * page_size >= int(total):
                break
        _nap(0.4)

    out["owner"] = owner
    out["userCnt"] = total
    out["玩家"] = players
    out["last_page"] = last_ok
    if not out["原因"] and not players and total != 0:
        out["原因"] = ("面板开了但没等到玩家列表。"
                      "空城、或人在别的国家视野外时会这样")
    return out


def attack_player(rec, sock, config: dict, uid, times: int = 1,
                  sweep: bool = False, city_id: int = 0, country: int = 0,
                  beat=None, card_used: int = 0, until_down: bool = False,
                  disp_name="", last_act: float = 0.0, page: int = 0) -> dict:
    """离线打指定玩家。照 2026-09-24 抓包：开城面板 type:3，再 type:14/19。

    不发 type:4（迁城）。人不在邻城时服务端会拒，把 ret 记下来就停。
    city/country 没给时从 city_players.db 按 uid 补。
    """
    from . import citydb

    uid = str(uid or "").strip()
    times = max(1, int(times or 1))
    act, name, cost = ((19, "扫荡", COST_SWEEP) if sweep
                       else (14, "攻击", COST_ATTACK))
    conf = (config.get("国战", {}) or {})
    cooldown = float(conf.get("扫荡间隔秒", 15))
    my = int(conf.get("自己国家ID") or 0) or _daily.read_my_country(rec)
    out = {"成功": 0, "停止原因": "", "目标": uid, "名字": str(disp_name or "").strip(),
           "城市": int(city_id or 0), "国家": int(country or 0),
           "动作": name, "用卡": 0}

    info = citydb.find_player(uid)
    if info:
        if not out["名字"]:
            out["名字"] = info.get("name") or ""
        if not out["城市"]:
            out["城市"] = int(info.get("city_id") or 0)
        if not out["国家"]:
            out["国家"] = int(info.get("city_country")
                            or info.get("country_id") or 0)
    if not uid:
        out["停止原因"] = "没给目标 baseid"
        return out
    if not out["城市"] or not out["国家"]:
        out["停止原因"] = ("库里没有这个人的城市/国家。"
                          "先 --city-players 拉过该城，或加 --atk-city / --city-country")
        return out
    if not my:
        _, _, _, panel = _panel(sock, rec, 0)
        v = _read_path(panel, "countryData.field5") if isinstance(panel, dict) else None
        if isinstance(v, int) and not isinstance(v, bool) and v > 0:
            my = v
        else:
            out["停止原因"] = "读不到自己的国家ID，停手"
            return out

    prev = _daily._BEAT
    if beat is not None:
        _daily._BEAT = beat
    try:
        return _attack_player(rec, sock, my, uid, times, act, name, cost,
                              cooldown, out, conf, card_used, until_down,
                              last_act, page)
    finally:
        _daily._BEAT = prev


def _attack_player(rec, sock, my, uid, times, act, name, cost, cooldown, out,
                   conf=None, card_used=0, until_down=False, last_act=0.0,
                   page=0):
    from . import citydb

    city, owner = out["城市"], out["国家"]
    card_item = int((conf or {}).get("国战恢复卡物品ID") or CARD_ITEM_ID)
    card_limit = max(0, int((conf or {}).get("单次最多用几张恢复卡", 1)))
    cap = 80 if until_down else max(1, int(times or 1))
    if until_down and int(times or 0) > 1:
        cap = int(times)
    last_act = float(last_act or 0)
    out["last_act"] = last_act
    if citydb.in_atk_fail(uid):
        log.info("[打人] %s 已在失败库，跳过", out["名字"] or uid)
        out["停止原因"] = "已在失败库，跳过"
        return out
    power, loc, _, panel = _panel(sock, rec, my)
    if panel is None:
        out["停止原因"] = "读不到国战面板，停手"
        return out
    if not citydb.can_reach(loc, city):
        here = citydb.city_name(loc) or loc
        there = citydb.city_name(city) or city
        out["停止原因"] = f"自己在 {loc} {here}，与目标城 {city} {there} 不相邻，停手"
        return out
    start_loc = loc
    who = out["名字"] or uid
    if until_down:
        log.info("[打人] 目标 %s uid=%s 城=%s 国=%s；自己在 %s 行动力=%s；"
                 "一直%s直到击退或打不过",
                 who, uid, city, owner, loc, power, name)
    else:
        log.info("[打人] 目标 %s uid=%s 城=%s 国=%s；自己在 %s 行动力=%s；准备%s %d 次",
                 who, uid, city, owner, loc, power, name, cap)

    merit0 = _read_path(panel, F_MERIT) or 0
    panel_miss = 0
    for i in range(1, cap + 1):
        power, loc, _, panel = _panel(sock, rec, my)
        if power is None:
            panel_miss += 1
            log.info("[打人] 中途读不到行动力（超时），打下一次")
            if panel_miss >= 2:
                log.info("[打人] 连续读不到行动力，换下一个")
                break
            continue
        panel_miss = 0
        if loc is not None and start_loc is not None and loc != start_loc:
            here = citydb.city_name(loc) or loc
            out["停止原因"] = (f"位置变了：{start_loc} → {loc} {here}"
                              f"（可能被遣返主城）")
            out["遣返"] = True
            log.info("[打人] %s", out["停止原因"])
            break
        if power < 15:
            used = card_used + out.get("用卡", 0)
            if used >= card_limit:
                if power < cost:
                    out["停止原因"] = (f"行动力只剩 {power}，{name}要 {cost}；"
                                      f"本次已用 {used}/{card_limit} 张恢复卡")
                    break
            else:
                sent, msg = _use_recovery_card(sock, rec, card_item)
                log.info("[打人] 行动力 %s < 15，%s（%d/%d）",
                         power, msg, used + (1 if sent else 0), card_limit)
                if sent:
                    out["用卡"] = out.get("用卡", 0) + 1
                    _nap(2.0)
                    power, loc, _, panel = _panel(sock, rec, my)
                elif power < cost:
                    out["停止原因"] = f"行动力只剩 {power}，{name}要 {cost}；{msg}"
                    break
        if power is None:
            log.info("[打人] 用卡后读不到行动力（超时），打下一次")
            continue
        if power < cost:
            out["停止原因"] = f"行动力只剩 {power}，{name}要 {cost}"
            break
        since = _send(sock, rec, 3, country=owner, city=city, page=page)
        cd = _wait(sock, rec, since, 3, timeout=WAIT_ATK)
        if not isinstance(cd, dict):
            log.info("[打人] 第 %d 次打开目标城市面板超时，打下一次", i)
            continue
        got_owner = _read_path(cd, "cityData.field2")
        if isinstance(got_owner, int) and got_owner:
            owner = got_owner
            out["国家"] = owner
        listed = _wait_city_users(sock, rec, since, city, timeout=2.0)
        p = _listed_user(listed, uid)
        morale = p.get("morale") if p else None
        if p and p.get("name"):
            out["名字"] = p["name"]
            who = p["name"]
        shown = (morale if isinstance(morale, int)
                 and not isinstance(morale, bool) and morale > 0 else None)
        if until_down:
            log.info("[打人] %s 攻击前士气=%s", who,
                     shown if shown is not None else "不可见")
        if p is None:
            if out["成功"]:
                out["击退"] = True
                log.info("[打人] %s 已不在本页，视为击退", who)
            else:
                log.info("[打人] %s 不在本页名单，换下一个（不打）", who)
            break
        wait = cooldown - (time.time() - last_act) if last_act else 0
        if wait > 0:
            log.info("[打人] 冷却，等 %.0f 秒", wait)
            _nap(wait)
        since = _send(sock, rec, act, country=owner, city=city, atk=uid)
        last_act = time.time()
        out["last_act"] = last_act
        r = _wait(sock, rec, since, act, timeout=WAIT_ATK)
        if not isinstance(r, dict):
            log.info("[打人] 第 %d 次%s回包超时（%.0f 秒），打下一次",
                     i, name, WAIT_ATK)
            continue
        ret = r.get("ret")
        if ret not in (0, None):
            if until_down and out["成功"]:
                log.info("[打人] %s 被拒 ret=%s，此前已打中，视为击退", who, ret)
                out["击退"] = True
                break
            if ret == 21:
                wait = cooldown - (time.time() - last_act) if last_act else cooldown
                if wait < 1:
                    wait = cooldown
                if not out.get("_waited_21"):
                    out["_waited_21"] = True
                    log.info("[打人] %s ret=21 是冷却未到，等 %.0f 秒再打（不记失败）",
                             who, wait)
                    _nap(wait)
                    continue
                log.info("[打人] %s 仍是 ret=21，换下一个（不记失败）", who)
                out["停止原因"] = f"{name}被拒 ret=21（跳过，不记失败）"
                break
            why = f"{name}被拒 ret={ret}（打不到，不记失败）"
            out["停止原因"] = why
            log.info("[打人] %s", why)
            break
        merit = _read_path(r, F_MERIT)
        btl = _await_response(
            sock, rec, BTL, since, WAIT_BTL,
            want=lambda d: _def_from_btl(d, uid) is not None)
        defu = _def_from_btl(btl, uid) if btl else None
        if not defu:
            log.info("[打人] 第 %d 次%s战报超时（%.0f 秒），打下一次",
                     i, name, WAIT_BTL)
            out["成功"] += 1
            continue
        if defu and defu.get("field2"):
            nm = str(defu.get("field2") or "").strip()
            if nm:
                out["名字"] = nm
                who = nm
        after = defu.get("field8") if defu else None
        lost = defu.get("field14") if defu else None
        after_n = (after if isinstance(after, int) and not isinstance(after, bool)
                   else None)
        lost_n = (lost if isinstance(lost, int) and not isinstance(lost, bool)
                  else None)
        log.info("[打人] 第 %d 次%s回包 ret=%s 战功=%s 战报士气=%s 损失=%s",
                 i, name, ret, merit, after_n, lost_n)
        _beat()
        if lost_n is None:
            out["成功"] += 1
            citydb.clear_atk_fail(uid)
            log.info("[打人] %s 战报里没有损失字段，不记失败", who)
            if after_n is not None and after_n <= 0:
                out["击退"] = True
                break
            continue
        if lost_n < 150:
            why = (f"{who} 攻击后士气损失 {lost_n}"
                   f"（剩余 {after_n}，须≥150才算打得动）")
            citydb.record_atk_fail(uid, city, ret, reason=why, name=who)
            out["记失败"] = True
            out["停止原因"] = why
            log.info("[打人] %s，已写入 atk_fail", why)
            break
        out["成功"] += 1
        citydb.clear_atk_fail(uid)
        log.info("[打人] %s 攻击后士气=%s 损失=%s，算成功", who, after_n, lost_n)
        if after_n is not None and after_n <= 0:
            out["击退"] = True
            log.info("[打人] %s 攻击后士气=0，视为击退", who)
            break
    power, loc, atk_times, panel = _panel(sock, rec, my)
    out["剩余行动力"] = power
    out["当前城市"] = loc
    out["今日攻击次数"] = atk_times
    if panel is not None:
        out["战功"] = (_read_path(panel, F_MERIT) or 0) - merit0
    if not out["停止原因"] and not out.get("击退") and not until_down and out["成功"] < cap:
        out["停止原因"] = f"只打成 {out['成功']}/{cap} 次"
    return out


def farm_city(rec, sock, config: dict, city_id, sweep=False, times=1,
              country=0, beat=None) -> dict:
    """现场翻页打这座城。打中后看士气损失，低于 150 才写入 atk_fail；
    打不到不入库。不读玩家库。跳过失败库，行动力低于 15 自动开卡，不迁城。"""
    from . import citydb

    conf = (config.get("国战", {}) or {})
    city_id = int(city_id or 0)
    country = int(country or conf.get("自己国家ID") or 0) \
        or _daily.read_my_country(rec)
    out = {"成功": 0, "失败": 0, "跳过": 0, "用卡": 0, "打过": 0,
           "停止原因": "", "城市": city_id}
    if not city_id:
        out["停止原因"] = "没给城市 ID"
        return out
    if not country:
        out["停止原因"] = "读不到自己的国家ID，停手"
        return out

    prev = _daily._BEAT
    if beat is not None:
        _daily._BEAT = beat
    try:
        power, loc, _, panel = _panel(sock, rec, country)
        if panel is None:
            out["停止原因"] = "读不到国战面板，停手"
            return out
        cname = citydb.city_name(city_id) or str(city_id)
        here_name = citydb.city_name(loc) or loc
        if not citydb.can_reach(loc, city_id):
            out["停止原因"] = (f"自己在 {loc} {here_name}，"
                              f"与目标 {city_id} {cname} 不相邻，停手")
            log.info("[打人] %s", out["停止原因"])
            return out
        start_loc = loc
        link = "同城" if loc == city_id else "相邻"
        log.info("[打人] 自己在 %s %s，目标 %s %s（%s）；行动力=%s",
                 loc, here_name, city_id, cname, link, power)

        fail = citydb.failed_uids()
        me = str(getattr(rec, "uid", "") or "")
        seen = set()
        last_act = 0.0
        owner = None
        total = None
        used_country = country
        page_size, max_pages = 15, 2000

        for page in range(0, max_pages):
            if out["停止原因"]:
                break
            power, loc, _, panel = _panel(sock, rec, country)
            if loc is not None and start_loc is not None and loc != start_loc:
                here = citydb.city_name(loc) or loc
                out["停止原因"] = (f"位置变了：{start_loc} → {loc} {here}"
                                  f"（可能被遣返主城）")
                log.info("[打人] %s", out["停止原因"])
                break
            since = _send(sock, rec, 3, country=used_country, city=city_id,
                          page=page)
            cd = _wait(sock, rec, since, 3)
            if not isinstance(cd, dict):
                out["停止原因"] = f"翻到第 {page} 页时没有回包"
                break
            ret = cd.get("ret")
            if ret not in (0, None):
                out["停止原因"] = f"打开城市面板被拒 ret={ret}（第 {page} 页）"
                break
            got_city = _read_path(cd, "cityData.field3")
            owner = _read_path(cd, "cityData.field2")
            cnt = cd.get("userCnt")
            if not isinstance(cnt, int) or isinstance(cnt, bool):
                cnt = _read_path(cd, "cityData.field5")
            if isinstance(cnt, int) and not isinstance(cnt, bool):
                total = cnt
            if page == 0 and got_city not in (None, city_id):
                if isinstance(owner, int) and owner and owner != used_country:
                    used_country = owner
                    since = _send(sock, rec, 3, country=used_country,
                                  city=city_id, page=page)
                    cd = _wait(sock, rec, since, 3)
                    if not isinstance(cd, dict):
                        out["停止原因"] = "用归属国重开城市面板没有回包"
                        break
                    got_city = _read_path(cd, "cityData.field3")
                    owner = _read_path(cd, "cityData.field2")
                    cnt = cd.get("userCnt")
                    if not isinstance(cnt, int) or isinstance(cnt, bool):
                        cnt = _read_path(cd, "cityData.field5")
                    if isinstance(cnt, int) and not isinstance(cnt, bool):
                        total = cnt
                if got_city not in (None, city_id):
                    out["停止原因"] = (f"面板回的城市ID={got_city}，"
                                      f"与请求的 {city_id} 不一致")
                    break
            if total == 0:
                break
            batch = _wait_city_users(sock, rec, since, city_id)
            log.info("[打人] 第 %d 页：现场 %d 人%s",
                     page, len(batch),
                     f" / {total}" if total is not None else "")
            if not batch:
                break
            for p in batch:
                uid = str(p.get("uid") or "").strip()
                if not uid or uid == me or uid in seen:
                    continue
                seen.add(uid)
                if uid in fail or citydb.in_atk_fail(uid):
                    log.info("[打人] %s 已在失败库，跳过", p.get("name") or uid)
                    out["跳过"] += 1
                    continue
                one = attack_player(
                    rec, sock, config, uid, times=0, sweep=sweep,
                    city_id=city_id,
                    country=int(owner or used_country or 0),
                    beat=beat, card_used=out["用卡"], until_down=True,
                    disp_name=p.get("name") or "", last_act=last_act,
                    page=page)
                if one.get("last_act"):
                    last_act = one["last_act"]
                if p.get("name") and not one.get("名字"):
                    one["名字"] = p["name"]
                reason = one.get("停止原因") or ""
                if reason == "已在失败库，跳过":
                    out["跳过"] += 1
                    continue
                out["用卡"] += one.get("用卡") or 0
                out["打过"] += 1
                if one.get("成功"):
                    out["成功"] += one["成功"]
                if one.get("击退"):
                    continue
                if one.get("记失败"):
                    fail.add(uid)
                    out["失败"] += 1
                    continue
                if "ret=21" in reason or "被拒" in reason:
                    continue
                if one.get("遣返") or "位置变了" in reason or "不相邻" in reason:
                    out["停止原因"] = reason
                    break
                if reason and ("行动力只剩" in reason or "恢复卡" in reason):
                    out["停止原因"] = reason
                    break
                if reason and ("读不到" in reason or "没有回包" in reason
                               or "超时" in reason):
                    continue
                if reason and not one.get("成功"):
                    out["停止原因"] = reason
                    break
            if out["停止原因"]:
                break
            if total is not None:
                max_pages = min(2000, max(page + 2,
                                          (int(total) + page_size - 1)
                                          // page_size + 2))
                if (page + 1) * page_size >= int(total) and page > 0:
                    break
                if page == 0 and len(seen) + out["跳过"] >= int(total):
                    break
            _nap(0.4)
        if not out["停止原因"]:
            out["停止原因"] = "这座城打完了" if out["打过"] else "这一页没有可打的人"
        return out
    finally:
        _daily._BEAT = prev
