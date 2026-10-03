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
    type:4   移动或占领相邻城市   2026-09-24 抓包：先 type:3 开目标城，
             再 type:4，countryID 填这座城当时的归属国。人从 6201 进 9303
             时行动力 197→182，再占领 9302、9301 各 -5，当前位置改为目标城。

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
F_CD = "countryData.field10"        # proto cdTime；攻击回包里通常缺省（0）
_CD_GEARS = (5, 10, 15)              # 出手间隔只有这三档


def _gear_up(sec):
    """比当前档更长的下一档。已经是 15 就停在 15。"""
    for g in _CD_GEARS:
        if g > float(sec or 0) + 0.2:
            return float(g)
    return float(_CD_GEARS[-1])


def _gear_down(sec):
    """比当前档更短的下一档。已经是 5 就停在 5。"""
    prev = float(_CD_GEARS[0])
    for g in _CD_GEARS:
        if g >= float(sec or 0) - 0.2:
            return prev
        prev = float(g)
    return prev


def _cd_until_from(data):
    """从 countryData.cdTime 取下次可出手时刻。没有或为 0 返回 None。"""
    v = _read_path(data, F_CD) if isinstance(data, dict) else None
    if isinstance(v, list):
        v = v[0] if v else None
    if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
        return None
    now = time.time()
    if v > 1_000_000_000:
        return float(v)
    if v <= 180:
        return now + float(v)
    return None


def _sleep_cd(last_act, cd_until, cd_sec, tag, *, on_wait=None):
    now = time.time()
    until = float(cd_until or 0)
    if until <= 0 and last_act:
        until = last_act + float(cd_sec or 0)
    wait = until - now
    if wait > 0.05:
        if on_wait is not None:
            return on_wait(wait)
        log.info("[%s] 冷却，等 %.0f 秒", tag, wait)
        _nap(wait)
    return True


def _apply_cd(out, sent_at, data, fallback):
    """成功出手后记下下次可打时刻。优先服务器 cdTime，否则用学到的间隔。"""
    out["_n21"] = 0
    got = _cd_until_from(data)
    sec = float(out.get("cd_sec") or fallback or _CD_GEARS[0])
    if got:
        out["cd_until"] = got
        if sent_at:
            sec = max(got - sent_at, 1.0)
        left = max(0.0, got - time.time())
        log.info("[冷却] 服务器给到 %.0f 秒后", left)
    else:
        hold = int(out.get("_cd_hold") or 0)
        if hold > 0:
            out["_cd_hold"] = hold - 1
        elif sec > _CD_GEARS[0]:
            sec = _gear_down(sec)
            log.info("[冷却] 间隔试短一档，%.0f 秒", sec)
        out["cd_until"] = (sent_at or time.time()) + sec
    out["cd_sec"] = sec
    out["last_act"] = sent_at


def _retry_cd21(out, last_ok, fallback, tag, who="", code=21, *, on_wait=None):
    """冷却回包不带剩余秒。升到下一档（5→10→15），只补等这一档剩下的时间。"""
    now = time.time()
    sec = float(out.get("cd_sec") or fallback or _CD_GEARS[0])
    nxt = _gear_up(sec)
    base = last_ok or now
    if nxt > sec + 0.2:
        out["cd_sec"] = nxt
        out["_cd_hold"] = 8
        until = base + nxt
    else:
        until = max(base + sec, now + 1.0)
    wait = max(0.0, until - time.time())
    out["cd_until"] = until
    n = int(out.get("_n21") or 0) + 1
    out["_n21"] = n
    who = f"{who} " if who else ""
    log.info("[%s] %sret=%s 冷却未到，间隔升到 %.0f 秒，再等 %.0f 秒（第 %d 次）",
             tag, who, code, out.get("cd_sec") or nxt, wait, n)
    if wait > 0.05:
        if on_wait is not None:
            if on_wait(wait) is False:
                return -1
        else:
            _nap(wait)
    return n


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
    自动锁敌这一单里，最近 1 小时用满上限就不再开。
    """
    from . import citydb

    if citydb.lock_cards_active():
        why = citydb.lock_card_block()
        if why:
            return False, why
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
    if citydb.lock_cards_active():
        citydb.note_lock_card()
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
        attack_only: bool = False, tally=None) -> dict:
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
    cooldown = float(_CD_GEARS[0])
    rounds = int(rounds or conf.get("默认次数", 0))
    use_card = bool(conf.get("自动使用国战恢复卡", False))
    card_limit = int(conf.get("单次最多用几张恢复卡", 100))
    card_item = int(conf.get("国战恢复卡物品ID", CARD_ITEM_ID))

    out = {"扫荡": 0, "攻击": 0, "召唤": 0, "战功": 0, "用卡": 0, "停止原因": "",
           "cd_sec": cooldown}
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
    # beat 为 None 时不能换这一线程的心跳回调。作为每日任务被调用时，
    # daily.run() 已经装好了，这里再装上 None 就把它废了。
    # 2026-08-29 实盘：国战跑了两分半，"共发心跳 0 次"，就是这么来的。
    restore_beat = _daily.bind_beat(beat)
    try:
        return _loop(rec, sock, rounds, country, npc_country, npc_city,
                     cooldown, out, attack_only, use_card, card_limit,
                     card_item, tally)
    finally:
        restore_beat()


def _loop(rec, sock, rounds, country, npc_country, npc_city, cooldown, out,
          attack_only=False, use_card=False, card_limit=1,
          card_item=CARD_ITEM_ID, tally=None):
    merit0 = None
    last_act = 0.0
    located = False
    force_summon = False     # 上一击被拒时置位，下一轮强制换个新目标
    fails = 0                # 连续被拒次数
    target = None            # 当前攻击目标，跨轮保留
    npc_morale = None        # 它剩多少士气
    cards_used = 0           # 本次用掉几张恢复卡

    from . import citydb

    for i in range(1, rounds + 1):
        stopped = _manual_stop()
        if stopped:
            out["停止原因"] = stopped
            break
        if citydb.attack_paused():
            out["停止原因"] = "已暂停"
            break
        power, city, atk_times, panel = _panel(sock, rec, country)
        if power is None:
            out["停止原因"] = "读不到国战面板（行动力未知），停手"
            break
        got_cd = _cd_until_from(panel)
        if got_cd:
            out["cd_until"] = got_cd
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

        # 冷却对扫荡和普通攻击共用。间隔优先读回包 countryData.cdTime，
        # 没有则用学到的秒数；ret=21 不带剩余秒，1 秒后再打。
        _sleep_cd(last_act, out.get("cd_until"),
                  out.get("cd_sec") or cooldown, "国战")
        prev_act = last_act
        since = _send(sock, rec, act, country=npc_country, city=npc_city,
                      atk=target)
        sent_at = time.time()
        r = _wait(sock, rec, since, act)
        if not isinstance(r, dict):
            last_act = sent_at
            _apply_cd(out, sent_at, None, cooldown)
            out["停止原因"] = f"{name}没有回包，停手"
            break
        ret = r.get("ret")
        if ret == 21:
            last_act = prev_act
            if _retry_cd21(out, last_act, cooldown, "国战") > 20:
                out["停止原因"] = f"{name}冷却一直没好（ret=21），停手"
                break
            continue
        if ret not in (0, None):
            # 目标可能刚被打死（士气归零），换一个再试。给一次机会，
            # 连着两次被拒就停 —— 不拿服务端的拒绝当探针反复撞。
            last_act = prev_act
            fails += 1
            if fails >= 2:
                out["停止原因"] = f"{name}连续两次被服务端拒绝 ret={ret}，停手"
                break
            log.info("[国战] %s被拒 ret=%s，换个支援兵重试一次", name, ret)
            force_summon = True
            _nap(1.0)
            continue
        fails = 0
        last_act = sent_at
        _apply_cd(out, sent_at, r, cooldown)

        out["扫荡" if act == 19 else "攻击"] += 1
        if tally is not None:
            tally["n"] = int(tally.get("n") or 0) + 1
            note = tally.get("note")
            if note:
                try:
                    note(tally["n"])
                except Exception:
                    pass
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


def _modo_stop(reason: str) -> bool:
    """这一座打不下去了，另一座也不要再去。"""
    text = str(reason or "")
    if text in ("已暂停", "已手动关停"):
        return True
    return any(k in text for k in ("被别人打败", "连接", "读不到自己的国家", "读不到当前", "出不了首都"))


def farm_modo_order(rec, sock, config, card_limit, beat=None, tally=None) -> dict:
    """按攻打号的国家，走进首都旁边两座魔多军团，召唤支援兵再打。

    恢复卡两座共用。第一座最多用一半，剩下的留给第二座。0 表示不用卡。
    """
    from . import citydb

    conf = config.get("国战") or {}
    country = int(conf.get("自己国家ID") or 0) or _daily.read_my_country(rec)
    out = {"攻击": 0, "召唤": 0, "扫荡": 0, "用卡": 0, "停止原因": "", "说明": ""}
    if not country:
        out["停止原因"] = "读不到攻打号的国家，停手"
        return out
    stands = citydb.modo_stands(country)
    if len(stands) < 2:
        cname = citydb.country_name(country) or str(country)
        out["停止原因"] = f"{cname} 首都旁边没有两座魔多军团"
        return out
    budget = int(card_limit or 0)
    if budget < 0:
        budget = 0
    notes = []
    used = 0
    for index, (stand, npc, name) in enumerate(stands[:2]):
        label = f"{stand} {name}".strip()
        log.info("[魔多] 走向 %s，驻地 %s", label, npc)
        walked = walk_to(
            rec, sock, config, stand, beat=beat,
            march_only=True, enter_target=True)
        why = str((walked or {}).get("停止原因") or "")
        if why:
            out["停止原因"] = why
            break
        cap = citydb.modo_first_cards(budget) if index == 0 else budget - used
        if cap < 0:
            cap = 0
        fight_config = dict(config)
        war = dict(config.get("国战") or {})
        war["摩多驻地城市ID"] = int(npc)
        war["自己国家ID"] = int(country)
        war["自动使用国战恢复卡"] = cap > 0
        war["单次最多用几张恢复卡"] = cap
        fight_config["国战"] = war
        fought = run(rec, sock, fight_config, rounds=100000, beat=beat, tally=tally)
        hits = (fought.get("扫荡") or 0) + (fought.get("攻击") or 0)
        out["攻击"] += hits
        out["扫荡"] += fought.get("扫荡") or 0
        out["召唤"] += fought.get("召唤") or 0
        out["用卡"] += fought.get("用卡") or 0
        used = out["用卡"]
        reason = str(fought.get("停止原因") or "")
        if "未开启自动使用国战恢复卡" in reason:
            reason = reason.replace("；未开启自动使用国战恢复卡", "")
        line = f"{name} 召唤 {fought.get('召唤') or 0} 次，打 {hits} 次"
        if reason and "已打满" not in reason:
            short = reason if len(reason) <= 48 else reason[:48].rstrip() + "…"
            line += f"（{short}）"
        notes.append(line)
        if _modo_stop(reason):
            out["停止原因"] = reason
            break
        if ("行动力" in reason or "恢复卡" in reason) and (budget <= 0 or used >= budget):
            out["停止原因"] = reason
            break
        if reason and "已打满" not in reason and "行动力" not in reason and "恢复卡" not in reason:
            out["停止原因"] = "" if index == 0 else reason
            if index == 0:
                continue
            break
    summary = "；".join(notes)
    if out["用卡"]:
        summary = (summary + "。" if summary else "") + f"用了 {out['用卡']} 张恢复卡"
    if out["停止原因"] and out["停止原因"] not in summary:
        summary = (summary + "。" if summary else "") + out["停止原因"]
    out["说明"] = summary
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
                      start_page: int = 0, end_page=None) -> dict:
    """只读：打开指定城市面板，收集城里所有玩家。

    走 RceCountryOpt type:3（开面板，不花行动力）。玩家在
    RseCountryUserLst；人多时用 pageInx 翻页。on_page(batch, page) 每页回调。
    start_page、end_page 都从 0 起算，结束页包含在内。不传 end_page 就翻到城尾。
    返回另带 last_page。
    """
    conf = (config.get("国战", {}) or {})
    city_id = int(city_id)
    start_page = max(0, int(start_page or 0))
    if end_page is not None:
        end_page = int(end_page)
    country = int(country or conf.get("自己国家ID") or 0) \
        or _daily.read_my_country(rec)
    out = {"city": city_id, "owner": None, "userCnt": None, "玩家": [],
           "原因": "", "last_page": start_page, "start_page": start_page}
    if not city_id:
        out["原因"] = "城市 ID 不能为 0"
        return out
    if end_page is not None and end_page < start_page:
        out["原因"] = f"结束页 {end_page} 小于起始页 {start_page}"
        return out
    if not country:
        out["原因"] = ("读不到自己的国家ID，停手；"
                      "可在 config 的「国战.自己国家ID」里手填，"
                      "或命令行加 --city-country")
        return out

    restore_beat = _daily.bind_beat(beat)
    try:
        return _list_city_players(rec, sock, city_id, country, out, on_page,
                                 start_page, end_page)
    finally:
        restore_beat()


def _list_city_players(rec, sock, city_id, country, out, on_page=None,
                      start_page=0, end_page=None):
    # 先刷自己国家的面板，跟真客户端进世界地图的顺序一致；也用来确认连着。
    power, loc, _, panel = _panel(sock, rec, country)
    if panel is None:
        out["原因"] = "读不到国战面板，停手"
        return out
    span = f"，到第 {end_page} 页止" if end_page is not None else ""
    log.info("[城市玩家] 自己国家=%s 当前城市=%s 行动力=%s；查询城市 %s，从第 %d 页开始%s",
             country, loc, power, city_id, start_page, span)

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
        if end_page is not None and page >= end_page:
            log.info("[城市玩家] 已到指定结束页 %d，本轮停", end_page)
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


def _miss_text(who, drop=None, left=None, lost=None, after=None) -> str:
    """这一击没有把人打掉。说明里写上玩家和士气，方便对战报。"""
    who = str(who or "").strip() or "对方"
    parts = [f"没打过 {who}"]
    if isinstance(drop, int) and not isinstance(drop, bool) and drop > 0:
        bit = f"自己士气降低{drop}"
        if isinstance(left, int) and not isinstance(left, bool):
            bit += f"，剩余{left}"
        parts.append(bit)
    if isinstance(lost, int) and not isinstance(lost, bool):
        bit = f"击伤{lost}"
        if isinstance(after, int) and not isinstance(after, bool):
            bit += f"，对方剩余{after}"
        parts.append(bit)
        if lost < 150:
            parts.append("须≥150才算打得动")
    return "，".join(parts)


def attack_player(rec, sock, config: dict, uid, times: int = 1,
                  sweep: bool = False, city_id: int = 0, country: int = 0,
                  beat=None, card_used: int = 0, until_down: bool = False,
                  disp_name="", last_act: float = 0.0, page: int = 0,
                  cd_until: float = 0.0, cd_sec=None, on_wait=None) -> dict:
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
    learned = cd_sec not in (None, 0)
    cooldown = float(cd_sec if learned else _CD_GEARS[0])
    my = int(conf.get("自己国家ID") or 0) or _daily.read_my_country(rec)
    out = {"成功": 0, "停止原因": "", "目标": uid, "名字": str(disp_name or "").strip(),
           "城市": int(city_id or 0), "国家": int(country or 0),
           "动作": name, "用卡": 0, "cd_sec": cooldown,
           "cd_until": float(cd_until or 0),
           "_cd_hold": 8 if learned else 0}

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

    restore_beat = _daily.bind_beat(beat)
    try:
        return _attack_player(rec, sock, my, uid, times, act, name, cost,
                              cooldown, out, conf, card_used, until_down,
                              last_act, page, cd_until, on_wait)
    finally:
        restore_beat()


def _lost_city_reason(loc, city, start=None) -> str:
    """人和目标城不再挨着。分不清是对方离开，还是自己被打回去。"""
    from . import citydb
    here = citydb.city_name(loc) or loc
    there = citydb.city_name(city) or city
    tail = "可能是对方离开了这座城，也可能是自己被打回去了。"
    if start is not None and start != loc:
        return (f"人从 {start} 到了 {loc} {here}，"
                f"和 {city} {there} 不再相邻。{tail}")
    return f"人在 {loc} {here}，和 {city} {there} 不相邻。{tail}"


def _attack_player(rec, sock, my, uid, times, act, name, cost, cooldown, out,
                   conf=None, card_used=0, until_down=False, last_act=0.0,
                   page=0, cd_until=0.0, on_wait=None):
    from . import citydb

    city, owner = out["城市"], out["国家"]
    card_item = int((conf or {}).get("国战恢复卡物品ID") or CARD_ITEM_ID)
    card_limit = max(0, int((conf or {}).get("单次最多用几张恢复卡", 100)))
    cap = 80 if until_down else max(1, int(times or 1))
    if until_down and int(times or 0) > 1:
        cap = int(times)
    last_act = float(last_act or 0)
    out["last_act"] = last_act
    out["cd_until"] = float(cd_until or out.get("cd_until") or 0)
    out["cd_sec"] = float(out.get("cd_sec") or cooldown)
    if citydb.same_failed(uid, out.get("名字")):
        log.info("[打人] %s 已在失败库，跳过", out["名字"] or uid)
        out["停止原因"] = "已在失败库，跳过"
        return out
    power, loc, _, panel = _panel(sock, rec, my)
    if panel is None:
        out["停止原因"] = "读不到国战面板，停手"
        return out
    citydb.note_attack_here(loc)
    if not citydb.can_reach(loc, city):
        out["停止原因"] = _lost_city_reason(loc, city)
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

    raw_merit = _read_path(panel, F_MERIT)
    merit0 = raw_merit if isinstance(raw_merit, int) and not isinstance(raw_merit, bool) else 0
    panel_miss = 0
    for i in range(1, cap + 1):
        stopped = _manual_stop()
        if stopped:
            out["停止原因"] = stopped
            break
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
            citydb.note_attack_here(loc)
            if _is_capital(loc):
                out["停止原因"] = _defeated_text(loc)
            else:
                out["停止原因"] = _lost_city_reason(loc, city, start_loc)
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
        if p and p.get("name"):
            out["名字"] = p["name"]
            who = p["name"]
        if p is None:
            if out["成功"]:
                out["击退"] = True
                log.info("[打人] %s 已不在本页，视为击退", who)
            else:
                log.info("[打人] %s 不在本页名单，换下一个（不打）", who)
            break
        got_cd = _cd_until_from(cd) or _cd_until_from(panel)
        if got_cd:
            out["cd_until"] = got_cd
        if _sleep_cd(last_act, out.get("cd_until"),
                     out.get("cd_sec") or cooldown, "打人",
                     on_wait=on_wait) is False:
            break
        prev_act = last_act
        since = _send(sock, rec, act, country=owner, city=city, atk=uid)
        sent_at = time.time()
        r = _wait(sock, rec, since, act, timeout=WAIT_ATK)
        if not isinstance(r, dict):
            last_act = sent_at
            _apply_cd(out, sent_at, None, cooldown)
            log.info("[打人] 第 %d 次%s回包超时（%.0f 秒），打下一次",
                     i, name, WAIT_ATK)
            continue
        ret = r.get("ret")
        if ret == 21:
            last_act = prev_act
            out["last_act"] = last_act
            n21 = _retry_cd21(out, last_act, cooldown, "打人", who,
                              on_wait=on_wait)
            if n21 < 0:
                break
            if n21 > 20:
                log.info("[打人] %s 冷却一直没好，换下一个（不记失败）", who)
                out["停止原因"] = f"{name}被拒 ret=21（跳过，不记失败）"
                break
            continue
        if ret not in (0, None):
            last_act = prev_act
            out["last_act"] = last_act
            if until_down and out["成功"]:
                log.info("[打人] %s 被拒 ret=%s，此前已打中，视为击退", who, ret)
                out["击退"] = True
                break
            why = f"{name}被拒 ret={ret}（打不到，不记失败）"
            out["停止原因"] = why
            log.info("[打人] %s", why)
            break
        last_act = sent_at
        _apply_cd(out, sent_at, r, cooldown)
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
        mine = _read_path(r, F_MORALE)
        if not isinstance(mine, int) or isinstance(mine, bool):
            mine = _read_path(panel, F_MORALE) if isinstance(panel, dict) else None
        mine_n = mine if isinstance(mine, int) and not isinstance(mine, bool) else None
        mine_before = _read_path(panel, F_MORALE) if isinstance(panel, dict) else None
        if not isinstance(mine_before, int) or isinstance(mine_before, bool):
            mine_before = None
        drop = None
        if mine_before is not None and mine_n is not None and mine_before > mine_n:
            drop = mine_before - mine_n
        out["击伤"] = lost_n
        out["对方剩余"] = after_n
        out["自己降低"] = drop
        out["自己剩余"] = mine_n
        log.info("[打人] 第 %d 次%s回包 ret=%s 战功=%s 战报士气=%s 击伤=%s 自己士气=%s",
                 i, name, ret, merit, after_n, lost_n, mine_n)
        if mine_n is not None and mine_n <= 0:
            power, loc, _, panel = _panel(sock, rec, my)
            if isinstance(loc, int) and not isinstance(loc, bool) and loc > 0:
                citydb.note_attack_here(loc)
            out["停止原因"] = _defeated_text(loc)
            out["遣返"] = True
            log.info("[打人] %s", out["停止原因"])
            break
        _beat()
        if lost_n is None:
            out["成功"] += 1
            citydb.clear_atk_fail(uid)
            log.info("[打人] %s 战报里没有击伤，不记失败", who)
            if after_n is not None and after_n <= 0:
                out["击退"] = True
                break
            continue
        if lost_n < 150:
            why = _miss_text(who, out.get("自己降低"), out.get("自己剩余"), lost_n, after_n)
            citydb.record_atk_fail(uid, city, ret, reason=why, name=who)
            out["记失败"] = True
            out["停止原因"] = why
            log.info("[打人] %s，已写入 atk_fail", why)
            break
        out["成功"] += 1
        citydb.clear_atk_fail(uid)
        log.info("[打人] %s 攻击后士气=%s 击伤=%s，自己士气=%s，算成功",
                 who, after_n, lost_n, mine_n)
        if after_n is not None and after_n <= 0:
            out["击退"] = True
            log.info("[打人] %s 攻击后士气=0，视为击退", who)
            break
    power, loc, atk_times, panel = _panel(sock, rec, my)
    out["剩余行动力"] = power
    out["当前城市"] = loc
    out["今日攻击次数"] = atk_times
    if panel is not None:
        now_merit = _read_path(panel, F_MERIT)
        if (isinstance(now_merit, int) and not isinstance(now_merit, bool)
                and isinstance(merit0, int) and not isinstance(merit0, bool)):
            out["战功"] = now_merit - merit0
    if not out.get("击退") and not out["停止原因"] and out.get("成功"):
        out["停止原因"] = _miss_text(
            out.get("名字") or uid, out.get("自己降低"), out.get("自己剩余"),
            out.get("击伤"), out.get("对方剩余"))
    elif not out["停止原因"] and not out.get("击退") and not until_down and out["成功"] < cap:
        out["停止原因"] = f"只打成 {out['成功']}/{cap} 次"
    return out


def _note_repel(out, tally, name: str = "") -> None:
    """记下击退一个人。tally 里的 note 把累计人数和最新名字写到订单上。"""
    out["击退"] = int(out.get("击退") or 0) + 1
    who = str(name or "").strip()
    if who:
        out["最近击退"] = who
    if not tally:
        return
    tally["n"] = int(tally.get("n") or 0) + 1
    if who:
        tally["name"] = who
    note = tally.get("note")
    if not note:
        return
    try:
        note(tally["n"], who or tally.get("name") or "")
    except TypeError:
        try:
            note(tally["n"])
        except Exception:
            pass
    except Exception:
        pass


def _manual_stop() -> str:
    """这一单被网页关停了。返回停止原因，没有则空。"""
    from . import citydb
    if citydb.fighting_order_stopped():
        return "已手动关停"
    return ""


def order_clear_targets(found, priority=None) -> list:
    """清城时的出手顺序。优先名单里数字小的先打，同级按第一次扫到的先后。其余人保持扫到的顺序，排在后面。"""
    priority = priority or {}
    ranked, rest = [], []
    for i, item in enumerate(found or []):
        page, player = item
        uid = str((player or {}).get("uid") or "").strip()
        rank = priority.get(uid)
        if rank is None:
            rest.append((i, page, player))
        else:
            ranked.append((int(rank), i, page, player))
    ranked.sort()
    ordered = [(page, player) for _, _, page, player in ranked]
    ordered.extend((page, player) for _, page, player in rest)
    return ordered


def scan_due(last, gap, now=None) -> bool:
    """距上次扫页是否已经过了配置的秒数。0 表示每次出手冷却都可以扫。"""
    try:
        gap = int(gap or 0)
    except (TypeError, ValueError):
        gap = 0
    if gap <= 0:
        return True
    now = time.time() if now is None else float(now)
    return now - float(last or 0) >= gap


def next_clear_target(found, priority, seen, me=""):
    """下一场打谁。跳过自己和这场已经处理过的人，其余仍按优先级。"""
    me = str(me or "")
    seen = seen or set()
    for page, player in order_clear_targets(found, priority):
        uid = str((player or {}).get("uid") or "").strip()
        if not uid or uid == me or uid in seen:
            continue
        return page, player
    return None


def farm_city(rec, sock, config: dict, city_id, sweep=False, times=1,
              country=0, beat=None, pass_block=False, tally=None,
              pages=None, priority=None, scan_sec=0) -> dict:
    """现场翻页打这座城。打中后看士气损失，低于 150 才写入 atk_fail；
    打不到不入库。不读玩家库。跳过失败库，行动力低于 15 自动开卡，不迁城。

    给定 pages 时只扫这一段。出手后的冷却由攻打号自己再扫同一段页，
    扫完还没到点就把剩余时间睡完。scan_sec 大于 0 时，两次扫页至少隔这么多秒。
    新出现的人按优先级接着打。"""
    from . import citydb

    conf = (config.get("国战", {}) or {})
    city_id = int(city_id or 0)
    country = int(country or conf.get("自己国家ID") or 0) \
        or _daily.read_my_country(rec)
    out = {"成功": 0, "失败": 0, "跳过": 0, "用卡": 0, "打过": 0, "击退": 0,
           "停止原因": "", "城市": city_id}
    if not city_id:
        out["停止原因"] = "没给城市 ID"
        return out
    if not country:
        out["停止原因"] = "读不到自己的国家ID，停手"
        return out

    restore_beat = _daily.bind_beat(beat)
    try:
        power, loc, _, panel = _panel(sock, rec, country)
        if panel is None:
            out["停止原因"] = "读不到国战面板，停手"
            return out
        cname = citydb.city_name(city_id) or str(city_id)
        here_name = citydb.city_name(loc) or loc
        citydb.note_attack_here(loc)
        if not citydb.can_reach(loc, city_id):
            out["停止原因"] = _lost_city_reason(loc, city_id)
            log.info("[打人] %s", out["停止原因"])
            return out
        start_loc = loc
        link = "同城" if loc == city_id else "相邻"
        log.info("[打人] 自己在 %s %s，目标 %s %s（%s）；行动力=%s",
                 loc, here_name, city_id, cname, link, power)

        fail = citydb.failed_names()
        me = str(getattr(rec, "uid", "") or "")
        seen = set()
        last_act = 0.0
        cd_until = 0.0
        cd_sec = float(_CD_GEARS[0])
        owner = None
        total = None
        used_country = country
        page_size, max_pages = 15, 2000
        limited = pages is not None
        try:
            scan_gap = int(scan_sec or 0)
        except (TypeError, ValueError):
            scan_gap = 0
        if scan_gap < 0:
            scan_gap = 0
        last_scan = 0.0
        if limited:
            start_page = max(0, int(pages[0]))
            end_page = max(start_page, int(pages[1]))
        else:
            start_page, end_page = 0, max_pages - 1
        scanned = []

        def _strike(page, p):
            nonlocal last_act, cd_until, cd_sec
            stopped = _manual_stop()
            if stopped:
                out["停止原因"] = stopped
                return "break"
            if citydb.attack_paused():
                out["停止原因"] = "已暂停"
                return "break"
            uid = str(p.get("uid") or "").strip()
            if not uid or uid == me or uid in seen:
                return "continue"
            seen.add(uid)
            if citydb.same_failed(uid, p.get("name"), fail):
                log.info("[打人] %s 已在失败库，跳过", p.get("name") or uid)
                out["跳过"] += 1
                if pass_block:
                    who = (str(p.get("name") or "").strip()
                           or str(fail.get(uid) or "").strip() or uid)
                    out["挡路"] = who
                    out["停止原因"] = f"{city_id} {cname} 有 {who} 挡路，路径不通"
                    return "break"
                return "continue"
            one = attack_player(
                rec, sock, config, uid, times=0, sweep=sweep,
                city_id=city_id,
                country=int(owner or used_country or 0),
                beat=beat, card_used=out["用卡"], until_down=True,
                disp_name=p.get("name") or "", last_act=last_act,
                page=page, cd_until=cd_until, cd_sec=cd_sec,
                on_wait=_during_wait if limited else None)
            if one.get("last_act"):
                last_act = one["last_act"]
            if one.get("cd_until"):
                cd_until = one["cd_until"]
            if one.get("cd_sec"):
                cd_sec = one["cd_sec"]
            if out["停止原因"]:
                return "break"
            if p.get("name") and not one.get("名字"):
                one["名字"] = p["name"]
            reason = one.get("停止原因") or ""
            if reason == "已在失败库，跳过":
                out["跳过"] += 1
                if pass_block:
                    who = (one.get("名字") or p.get("name") or uid)
                    out["挡路"] = who
                    out["停止原因"] = f"{city_id} {cname} 有 {who} 挡路，路径不通"
                    return "break"
                return "continue"
            out["用卡"] += one.get("用卡") or 0
            out["打过"] += 1
            if one.get("成功"):
                out["成功"] += one["成功"]
            if one.get("击退"):
                _note_repel(out, tally, one.get("名字") or p.get("name") or "")
                return "continue"
            if one.get("记失败") or ("没打过" in reason and not one.get("击退")):
                if one.get("记失败"):
                    who = one.get("名字") or p.get("name") or uid
                    fail[uid] = str(one.get("名字") or p.get("name") or "").strip()
                    out["失败"] += 1
                    names = out.setdefault("挡路人", [])
                    if who not in names:
                        names.append(who)
                if pass_block:
                    who = one.get("名字") or p.get("name") or uid
                    who = "、".join(out.get("挡路人") or []) or who
                    out["挡路"] = who
                    if "没打过" in reason:
                        out["停止原因"] = reason
                    else:
                        out["停止原因"] = f"{city_id} {cname} 有 {who} 挡路，路径不通"
                    return "break"
                who = one.get("名字") or p.get("name") or uid
                log.info("[打人] %s 打不过，已记入战败库，继续清这座城", who)
                return "continue"
            if "ret=21" in reason or "被拒" in reason:
                return "continue"
            if one.get("遣返") or "位置变了" in reason or "不相邻" in reason:
                out["停止原因"] = reason
                return "break"
            if reason and ("行动力只剩" in reason or "恢复卡" in reason):
                out["停止原因"] = reason
                return "break"
            if reason and ("读不到" in reason or "没有回包" in reason
                           or "超时" in reason):
                return "continue"
            if reason and not one.get("成功"):
                out["停止原因"] = reason
                return "break"
            return "continue"

        def _pull(page, strict):
            nonlocal owner, total, used_country
            stopped = _manual_stop()
            if stopped:
                out["停止原因"] = stopped
                return "stop", None
            if citydb.attack_paused():
                out["停止原因"] = "已暂停"
                return "stop", None
            power, loc, _, panel = _panel(sock, rec, country)
            if loc is not None and start_loc is not None and loc != start_loc:
                citydb.note_attack_here(loc)
                out["停止原因"] = _lost_city_reason(loc, city_id, start_loc)
                log.info("[打人] %s", out["停止原因"])
                return "stop", None
            since = _send(sock, rec, 3, country=used_country, city=city_id,
                          page=page)
            cd = _wait(sock, rec, since, 3)
            if not isinstance(cd, dict):
                if strict:
                    out["停止原因"] = f"翻到第 {page} 页时没有回包"
                    return "stop", None
                return "soft", None
            ret = cd.get("ret")
            if ret not in (0, None):
                if strict:
                    out["停止原因"] = f"打开城市面板被拒 ret={ret}（第 {page} 页）"
                    return "stop", None
                return "soft", None
            got_city = _read_path(cd, "cityData.field3")
            owner = _read_path(cd, "cityData.field2")
            cnt = cd.get("userCnt")
            if not isinstance(cnt, int) or isinstance(cnt, bool):
                cnt = _read_path(cd, "cityData.field5")
            if isinstance(cnt, int) and not isinstance(cnt, bool):
                total = cnt
            if page == start_page and got_city not in (None, city_id):
                if isinstance(owner, int) and owner and owner != used_country:
                    used_country = owner
                    since = _send(sock, rec, 3, country=used_country,
                                  city=city_id, page=page)
                    cd = _wait(sock, rec, since, 3)
                    if not isinstance(cd, dict):
                        if strict:
                            out["停止原因"] = "用归属国重开城市面板没有回包"
                            return "stop", None
                        return "soft", None
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
                    return "stop", None
            if total == 0:
                log.info("[打人] 第 %d 页：现场 0 人，没有可打的目标", page)
                return "end", None
            batch = _wait_city_users(sock, rec, since, city_id)
            log.info("[打人] 第 %d 页：现场 %d 人%s",
                     page, len(batch),
                     f" / {total}" if total is not None else "")
            if not batch:
                return "end", None
            if pass_block:
                stuck = next((
                    p for p in batch
                    if str(p.get("uid") or "").strip()
                    and str(p.get("uid") or "").strip() != me
                    and citydb.same_failed(
                        str(p.get("uid") or "").strip(), p.get("name"), fail)
                ), None)
                if stuck:
                    stuck_uid = str(stuck.get("uid") or "").strip()
                    who = (str(stuck.get("name") or "").strip()
                           or str(fail.get(stuck_uid) or "").strip()
                           or stuck_uid)
                    log.info("[打人] %s 在 %s %s 挡路", who, city_id, cname)
                    out["跳过"] += 1
                    out["挡路"] = who
                    out["停止原因"] = f"{city_id} {cname} 有 {who} 挡路，路径不通"
                    return "stop", None
            return "ok", batch

        def _refresh(strict):
            nonlocal scanned
            found = []
            known = set()
            for page in range(start_page, end_page + 1):
                if out["停止原因"]:
                    return False
                kind, batch = _pull(page, strict)
                if kind == "stop":
                    return False
                if kind == "soft":
                    log.info("[打人] 冷却里第 %d 页没扫成，沿用上一份名单", page)
                    return True
                if kind == "end":
                    break
                for p in batch:
                    uid = str(p.get("uid") or "").strip()
                    if not uid or uid == me or uid in known:
                        continue
                    known.add(uid)
                    found.append((page, p))
                if total is not None and len(found) >= int(total):
                    break
                if page < end_page:
                    _nap(0.4)
            scanned = found
            return True

        def _cd_wait():
            now = time.time()
            until = float(cd_until or 0)
            if until <= 0 and last_act:
                until = last_act + float(cd_sec or 0)
            return max(0.0, until - now)

        def _during_wait(wait):
            nonlocal last_scan
            deadline = time.time() + max(0.0, float(wait or 0))
            if scan_due(last_scan, scan_gap):
                if scan_gap > 0:
                    log.info("[打人] 冷却 %.0f 秒，扫指定页（扫页冷却 %d 秒）",
                             wait, scan_gap)
                else:
                    log.info("[打人] 冷却 %.0f 秒，扫指定页", wait)
                if not _refresh(False):
                    return False
                last_scan = time.time()
            else:
                left_gap = max(0.0, scan_gap - (time.time() - last_scan))
                log.info("[打人] 冷却 %.0f 秒，扫页冷却还剩 %.0f 秒", wait, left_gap)
            left = deadline - time.time()
            if left > 0.05 and not out["停止原因"]:
                _nap(left)
            return not out["停止原因"]

        if limited:
            if scan_gap > 0:
                log.info("[打人] 扫页冷却 %d 秒", scan_gap)
            if _refresh(True) and not out["停止原因"]:
                last_scan = time.time()
                looked = False
                while not out["停止原因"]:
                    nxt = next_clear_target(scanned, priority, seen, me)
                    if nxt is None:
                        wait = _cd_wait()
                        if wait > 0.05 and not looked:
                            looked = True
                            if _during_wait(wait) is False:
                                break
                            continue
                        break
                    looked = False
                    if _strike(nxt[0], nxt[1]) == "break":
                        break
        else:
            for page in range(start_page, max_pages):
                kind, batch = _pull(page, True)
                if kind != "ok":
                    break
                for p in batch:
                    if _strike(page, p) == "break":
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
            if limited and not out["打过"]:
                out["停止原因"] = "这几页没有可打的人"
            else:
                out["停止原因"] = "这座城打完了" if out["打过"] else "这一页没有可打的人"
        if not out.get("挡路") and out.get("挡路人"):
            out["挡路"] = "、".join(out["挡路人"])
        return out
    finally:
        restore_beat()


def _road_blocked(reason, fought=None) -> bool:
    """这座城打不过，要避开重算。失败库里的人，或这一轮一个都没打赢。"""
    text = reason or ""
    if "挡路" in text or "不可通行" in text:
        return True
    fought = fought or {}
    return bool(fought.get("失败") and not fought.get("成功"))


def _names_standing(sock, rec, city, country) -> str:
    """这座城里现在站着谁。战败表对得上的人排在前面。"""
    from . import citydb

    fail = citydb.failed_names()
    me = str(getattr(rec, "uid", "") or "")
    country = int(country or 0)
    since = _send(sock, rec, 3, country=country, city=int(city))
    cd = _wait(sock, rec, since, 3)
    owner = _read_path(cd, "cityData.field2") if isinstance(cd, dict) else None
    if isinstance(owner, int) and not isinstance(owner, bool) and owner and owner != country:
        since = _send(sock, rec, 3, country=int(owner), city=int(city))
        _wait(sock, rec, since, 3)
    matched, others = [], []
    for p in _wait_city_users(sock, rec, since, city):
        uid = str(p.get("uid") or "").strip()
        if not uid or uid == me:
            continue
        live = str(p.get("name") or "").strip()
        recorded = str(fail.get(uid) or "").strip() if isinstance(fail, dict) else ""
        shown = live or recorded or uid
        if citydb.same_failed(uid, live, fail):
            if shown not in matched:
                matched.append(shown)
        elif shown not in others:
            others.append(shown)
    names = matched or others
    if len(names) <= 6:
        return "、".join(names)
    return "、".join(names[:6]) + f" 等 {len(names)} 人"


def live_plan(sock, rec, here, target, my, avoid=None, stop_on_block=False,
              avoid_why=None) -> dict:
    """先按本地归属规划，再开面板核对路线上每座城的占领国，变了就重算。

    目录里的国家是原属国。9316 这类城被法国占了之后，不看面板仍会写成黑暗联盟。
    stop_on_block 时，路上有打不过的人就避开这座城再规划一次。
    新路线是通路就接着走；再遇到人挡，或没有别的路，就停。
    """
    from . import citydb

    known = {}
    blocked = {int(c) for c in (avoid or ())}
    why = {}
    for key, name in (avoid_why or {}).items():
        try:
            cid = int(key)
        except (TypeError, ValueError):
            continue
        text = str(name or "").strip()
        if cid > 0 and text:
            why[cid] = text
    for city in list(blocked):
        if why.get(int(city)):
            continue
        who = _names_standing(sock, rec, city, my)
        if who:
            why[int(city)] = who
            log.info("[路线] %s 此前没记下挡路的人，现场是 %s", city, who)
    fail = citydb.failed_names()
    me = str(getattr(rec, "uid", "") or "")
    plan = citydb.plan_route(here, target, my, blocked, why)
    # 自动单只给一次改道。非自动单仍按归属变化最多重算 6 次。
    rounds = 2 if stop_on_block else 6
    replanned_block = False
    for _ in range(rounds):
        seq = list(plan.get("路径") or [])
        if target not in seq:
            seq.append(int(target))
        changed = False
        hit = None
        for city in seq:
            if city in known:
                continue
            info = _open_city(sock, rec, city, my)
            if not info:
                known[city] = None
                continue
            known[city] = info["owner"]
            citydb.record_occupy(city, info["owner"], info["userCnt"])
            name = citydb.city_name(city) or city
            log.info("[路线] %s %s 面板归属国=%s", city, name, info["owner"])
            changed = True
            others = info["userCnt"]
            if info.get("here") == int(city) and others:
                others -= 1
            if city == int(target) or info["owner"] == my or not others:
                continue
            since = _send(sock, rec, 3, country=int(info["owner"] or my),
                          city=int(city))
            _wait(sock, rec, since, 3)
            bad = []
            for p in _wait_city_users(sock, rec, since, city):
                uid = str(p.get("uid") or "").strip()
                if not uid or uid == me or uid not in fail:
                    continue
                live = str(p.get("name") or "").strip()
                if not citydb.same_failed(uid, live, fail):
                    log.info("[路线] %s %s 的 %s 不在战败表（这个 uid 记的是 %s），直接打",
                             city, name, live or uid, fail.get(uid) or "空名字")
                    continue
                bad.append(live or str(fail.get(uid) or "").strip() or uid)
            if bad:
                blocked.add(int(city))
                who = "、".join(bad)
                why[int(city)] = who
                if stop_on_block:
                    hit = (int(city), name, who)
                    break
                log.info("[路线] %s %s 有 %s 挡路，规划时跳过",
                         city, name, who)
        if stop_on_block:
            plan["避开"] = blocked
            if hit is None:
                return plan
            city, name, who = hit
            pause = f"{city} {name} 有 {who} 挡路，路径不通"
            if replanned_block:
                plan["暂停"] = pause
                log.info("[路线] %s", plan["暂停"])
                return plan
            log.info("[路线] %s %s 有 %s 挡路，避开这座城再规划一次",
                     city, name, who)
            nxt = citydb.plan_route(here, target, my, blocked, why)
            path = [int(c) for c in (nxt.get("路径") or [])]
            # 人当前所在的城可以留在路线开头，其余已避开的城再出现就算没绕开。
            still = [c for c in path if c in blocked and c != int(here)]
            if not path or still:
                plan["暂停"] = pause
                log.info("[路线] %s", plan["暂停"])
                return plan
            plan = nxt
            plan["避开"] = blocked
            replanned_block = True
            log.info("[路线] 避开 %s %s 后还有通路，继续", city, name)
            continue
        nxt = citydb.plan_route(here, target, my, blocked, why)
        if (not changed and nxt.get("路径") == plan.get("路径")
                and nxt.get("须占领") == plan.get("须占领")):
            plan = nxt
            break
        plan = nxt
    plan["避开"] = blocked
    return plan


def _open_city(sock, rec, city, country, retry=True):
    """type:3 打开目标城。返回归属国和人数；面板对不上就用回包里的归属国再开一次。"""
    since = _send(sock, rec, 3, country=int(country or 0), city=int(city))
    cd = _wait(sock, rec, since, 3)
    if not isinstance(cd, dict) or cd.get("ret") not in (0, None):
        return None
    owner = _read_path(cd, "cityData.field2") or country
    got = _read_path(cd, "cityData.field3")
    cnt = cd.get("userCnt")
    if not isinstance(cnt, int) or isinstance(cnt, bool):
        cnt = _read_path(cd, "cityData.field5")
    if (retry and got not in (None, int(city)) and isinstance(owner, int)
            and owner and owner != country):
        return _open_city(sock, rec, city, owner, retry=False)
    need = _read_path(cd, "cityData.field7")
    here = _read_path(cd, F_CITY)
    power = _read_path(cd, F_POWER)
    return {"owner": int(owner or 0), "userCnt": int(cnt or 0),
            "need": int(need) if isinstance(need, int) and not isinstance(need, bool) else 0,
            "here": int(here) if isinstance(here, int) and not isinstance(here, bool) else 0,
            "power": power}


_MOVE_CD = {"cd_sec": float(_CD_GEARS[0]), "last_act": 0.0,
            "cd_until": 0.0, "_n21": 0, "_cd_hold": 0}


def _commit_move(sock, rec, city, owner, my=0):
    """发占领前再开一次面板：人在哪、城里还有没有别人、城属于谁。

    人已经在目标城就不再发。敌城还有人也不发。所属以这次面板为准。
    移动和占领的间隔也是 5、10、15 秒。ret=30 升一档再试。
    """
    for _ in range(6):
        info = _open_city(sock, rec, city, owner, retry=False)
        if not info:
            return None, "占领前打不开城市"
        owner = int(info["owner"] or owner or 0)
        cnt = info["userCnt"]
        here = info.get("here") or 0
        if here == int(city) and cnt:
            cnt -= 1
        if here == int(city):
            log.info("[移动] 查看：人在 %s，目标 %s 所属 %s，其他人 %d，人已在城里",
                     here, city, owner, cnt)
            return {"here": int(city), "power": info.get("power")}, ""
        if cnt and int(my or 0) and owner != int(my):
            log.info("[移动] 查看：人在 %s，目标 %s 所属 %s，其他人 %d，先攻打",
                     here, city, owner, cnt)
            return None, f"城里还有 {cnt} 人，所属 {owner}"
        action = "移动" if int(my or 0) and owner == int(my) else "占领"
        send_country = int(my if action == "移动" else owner)
        log.info("[移动] 查看：人在 %s，目标 %s 所属 %s，其他人 %d，选择%s（国家 %s）",
                 here, city, owner, cnt, action, send_country)
        _sleep_cd(_MOVE_CD.get("last_act") or 0, _MOVE_CD.get("cd_until") or 0,
                  _MOVE_CD.get("cd_sec"), "移动")
        sent = time.time()
        since = _send(sock, rec, 4, country=send_country, city=int(city))
        got = _wait(sock, rec, since, 4)
        if not isinstance(got, dict):
            return None, "移动没有回包"
        if got.get("ret") in (0, None):
            here = _read_path(got, F_CITY)
            if here not in (None, int(city)):
                return None, f"移动后位置是 {here}，不是 {city}"
            _apply_cd(_MOVE_CD, sent, got, _CD_GEARS[0])
            return {"here": int(here or city), "power": _read_path(got, F_POWER)}, ""
        if got.get("ret") != 30:
            return None, f"移动被拒 ret={got.get('ret')}"
        if _retry_cd21(_MOVE_CD, _MOVE_CD.get("last_act") or 0, _CD_GEARS[0],
                       "移动", code=30) > 4:
            return None, "移动被拒 ret=30"
    return None, "移动被拒 ret=30"


def _is_capital(city) -> bool:
    """编号第 2 位是 1 的城是首都。被别人打败一般会回到这里。"""
    try:
        s = str(int(city or 0))
    except (TypeError, ValueError):
        return False
    return len(s) >= 2 and s[1] == "1"


def _defeated_text(loc) -> str:
    """自己被打败。人已经在首都时写明首都。"""
    from . import citydb

    try:
        loc = int(loc or 0)
    except (TypeError, ValueError):
        loc = 0
    if _is_capital(loc):
        name = citydb.city_name(loc) or ""
        return f"被别人打败，已回到首都 {loc} {name}".strip()
    if loc > 0:
        name = citydb.city_name(loc) or ""
        return f"被别人打败，人到了 {loc} {name}".strip()
    return "被别人打败，已回到首都"


def _clear_sent_home(sock, rec, loc, my) -> str:
    """清城途中被打回首都，且士气不满。不消耗恢复卡。人一开始就在首都时不走这里。"""
    if not _is_capital(loc):
        return ""
    _, _, _, panel = _panel(sock, rec, my)
    morale = _read_path(panel, F_MORALE) if isinstance(panel, dict) else None
    if isinstance(morale, int) and not isinstance(morale, bool) and 0 <= morale < 100:
        return _defeated_text(loc)
    return ""


def _halt_for_home(sock, rec, config, loc, my, uid, allow_leave=False) -> str:
    """清城途中被打回首都就停。人一开始就在首都时，补士气再出城，和 --move 一样。

    打某个人时仍按原来的方式补士气再出城。
    """
    if not str(uid or "").strip() and not allow_leave:
        home = _clear_sent_home(sock, rec, loc, my)
        if home:
            log.info("[移动] %s", home)
            return home
    return _ready_to_leave(sock, rec, config, loc, my)


def _ready_to_leave(sock, rec, config, loc, my) -> str:
    """人在首都且士气低于 100 时出不了城。用一张国战恢复卡，士气和行动力会一起回满。"""
    s = str(int(loc or 0))
    if len(s) < 2 or s[1] != "1":
        return ""
    _, _, _, panel = _panel(sock, rec, my)
    morale = _read_path(panel, F_MORALE) if isinstance(panel, dict) else None
    if not isinstance(morale, int) or isinstance(morale, bool) or morale >= 100:
        return ""
    item = int((config.get("国战") or {}).get("国战恢复卡物品ID") or CARD_ITEM_ID)
    sent, msg = _use_recovery_card(sock, rec, item)
    log.info("[移动] 人在首都，士气 %s < 100，出不去，%s", morale, msg)
    if not sent:
        return f"士气 {morale} 低于 100，出不了首都；{msg}"
    _nap(2.0)
    _, _, _, panel = _panel(sock, rec, my)
    morale = _read_path(panel, F_MORALE) if isinstance(panel, dict) else None
    if isinstance(morale, int) and not isinstance(morale, bool) and morale < 100:
        return f"用了恢复卡，士气仍是 {morale}，出不了首都"
    return ""


def retreat_stop(path, hops=3) -> int:
    """路径第 0 个是当前城。沿路最多走进 hops 座，返回落点。走不了返回 0。"""
    seq = []
    for city in path or []:
        try:
            seq.append(int(city))
        except (TypeError, ValueError):
            continue
    try:
        hops = int(hops or 0)
    except (TypeError, ValueError):
        return 0
    if len(seq) <= 1 or hops <= 0:
        return 0
    return seq[min(len(seq) - 1, hops)]


def retreat_toward(rec, sock, config, name="马奇诺", hops=3, beat=None,
                   city_id=0, mode="hops") -> dict:
    """自动索敌打完后的后退。hops 朝目标只走几座城；city 走进指定城。都不打目标城。"""
    from . import citydb

    mode = str(mode or "hops")
    if mode not in ("hops", "city"):
        mode = "hops"
    label = str(name or "").strip()
    try:
        target = int(city_id or 0)
    except (TypeError, ValueError):
        target = 0
    try:
        hops = int(hops or 0)
    except (TypeError, ValueError):
        hops = 3
    if hops <= 0:
        hops = 3
    out = {"说明": "", "走到": 0, "步数": 0, "方式": mode}
    if mode == "hops" and not label:
        label = "马奇诺"
    if target <= 0 and label:
        target = citydb.city_id_named(label)
    if target > 0 and not label:
        label = citydb.city_name(target) or str(target)
    if target <= 0 or (mode == "city" and not citydb.city_name(target)):
        out["说明"] = f"城市目录里没有{label or '这座城'}"
        return out
    if mode == "city":
        moved = walk_to(rec, sock, config, target, beat=beat,
                        march_only=True, enter_target=True)
    else:
        moved = walk_to(rec, sock, config, target, beat=beat,
                        march_only=True, max_steps=hops)
    landed = int(moved.get("走到") or 0)
    steps = int(moved.get("步数") or 0)
    why = str(moved.get("停止原因") or "").strip()
    landed_name = citydb.city_name(landed) or str(landed or "")
    out["走到"] = landed
    out["步数"] = steps
    if mode == "city":
        if why:
            out["说明"] = f"退到{label}时停下：{why}"
        elif steps <= 0 and landed == target:
            out["说明"] = f"人已经在 {target} {label}，不用再退"
        elif landed == target:
            out["说明"] = f"已退到 {landed} {landed_name}".strip()
        elif steps <= 0:
            out["说明"] = f"没能退到 {target} {label}"
        else:
            out["说明"] = f"朝{label}退了{steps}城，停在 {landed} {landed_name}".strip()
        return out
    if why:
        out["说明"] = f"朝{label}后退时停下：{why}"
    elif steps <= 0:
        out["说明"] = f"人已经在{label}这一侧的{hops}城内，不用再退"
    else:
        out["说明"] = f"朝{label}退了{steps}城，停在 {landed} {landed_name}".strip()
    return out


def walk_to(rec, sock, config, target, sweep=False, beat=None,
             avoid=None, replanned=False, uid="", hold_if_blocked=False,
             tally=None, avoid_why=None, march_only=False, max_steps=None,
             enter_target=False, clear_plan=None) -> dict:
    """先按最短路径走。敌城打不过就避开它重算一次；再受阻就停。

    本国城可以一次走到最远。敌城有人就先打，空城直接占领。
    编号第 2 位是 1 或 2、又不是自己国家的城不能占领，直接暂停。
    march_only 只走路，不打目标城。max_steps 是最多走进几座城，不含起点。
    enter_target 为真时，后退要走进目标城，而不是停在相邻城。
    """
    from . import citydb

    target = int(target or 0)
    conf = config.get("国战") or {}
    my = int(conf.get("自己国家ID") or 0) or _daily.read_my_country(rec)
    out = {"停止原因": "", "走到": 0, "移动": 0, "目标": target, "击退": 0, "步数": 0}
    if not my:
        out["停止原因"] = "读不到自己的国家ID"
        return out
    power, loc, _, panel = _panel(sock, rec, my)
    if panel is None or not loc:
        out["停止原因"] = "读不到当前所在城市"
        return out
    out["走到"] = int(loc)
    start_here = int(loc)
    citydb.note_attack_here(loc)
    if max_steps is not None:
        try:
            max_steps = int(max_steps)
        except (TypeError, ValueError):
            max_steps = None
    if max_steps is not None and max_steps <= 0:
        return out
    plan = live_plan(sock, rec, loc, target, my, avoid,
                     stop_on_block=hold_if_blocked, avoid_why=avoid_why)
    avoid = set(plan.get("避开") or avoid or ())
    for line in citydb.format_route(plan):
        log.info("[路线] %s", line)
    if plan.get("暂停"):
        out["停止原因"] = plan["暂停"]
        return out
    if not plan.get("路径"):
        out["停止原因"] = plan.get("原因") or "没有通路"
        return out
    if citydb.attack_paused():
        out["停止原因"] = "已暂停"
        return out
    seq = list(plan["路径"])
    if max_steps is not None:
        seq = seq[: max_steps + 1]
    elif march_only and enter_target and seq and int(seq[-1]) != int(target):
        seq.append(int(target))
    owned = citydb.city_map()
    far_i = 0
    for i, c in enumerate(seq):
        if not i:
            continue
        if (owned.get(c) or {}).get("owner") != my:
            break
        far_i = i
    occupy_from = 1
    blocked_at = 0
    block_who = ""

    restore_beat = _daily.bind_beat(beat)
    try:
        stopped = _manual_stop()
        if stopped:
            out["停止原因"] = stopped
            return out
        def _still_at_start_capital() -> bool:
            """这一单开始时人就在首都，还没走出这步。按 --move 补士气再出城。"""
            return (not out["移动"] and int(out["走到"]) == start_here
                    and _is_capital(start_here))

        if far_i:
            blocked = _halt_for_home(
                sock, rec, config, out["走到"], my, uid,
                allow_leave=_still_at_start_capital())
            if blocked:
                out["停止原因"] = blocked
                return out
            city = seq[far_i]
            name = citydb.city_name(city) or str(city)
            log.info("[移动] 前方最远的本国城是 %s %s，一次走到（不跨过敌城）",
                     city, name)
            info = _open_city(sock, rec, city, my)
            if not info:
                out["停止原因"] = f"打不开 {city} {name}"
                return out
            if info["owner"] != my:
                log.info("[移动] %s %s 实际归属 %s，不是本国，改为从下一座城逐城走",
                         city, name, info["owner"])
            else:
                moved, why = _commit_move(sock, rec, city, info["owner"], my)
                if not moved:
                    out["停止原因"] = f"{city} {name}：{why}"
                    return out
                out["移动"] += 1
                out["步数"] = far_i
                out["走到"] = moved["here"]
                citydb.note_attack_here(moved["here"])
                occupy_from = far_i + 1
                log.info("[移动] 进入 %s %s，行动力 %s",
                         moved["here"], name, moved.get("power"))
        for city in seq[occupy_from:]:
            stopped = _manual_stop()
            if stopped:
                out["停止原因"] = stopped
                break
            if citydb.attack_paused():
                out["停止原因"] = "已暂停"
                break
            if city == target and not march_only:
                break
            blocked = _halt_for_home(
                sock, rec, config, out["走到"], my, uid,
                allow_leave=_still_at_start_capital())
            if blocked:
                out["停止原因"] = blocked
                break
            name = citydb.city_name(city) or str(city)
            info = _open_city(sock, rec, city, my)
            if not info:
                out["停止原因"] = f"打不开 {city} {name}"
                break
            owner, cnt = info["owner"], info["userCnt"]
            citydb.record_occupy(city, owner, cnt)
            if info.get("here") == int(city) and cnt:
                cnt -= 1
            moved, why = None, ""
            if info.get("here") == int(city) and (owner == my or not cnt):
                moved = {"here": int(city), "power": info.get("power")}
                log.info("[移动] 人已经在 %s %s", city, name)
            elif (owner != my and citydb.fort_locked(city)
                  and (owned.get(city) or {}).get("home") != 21):
                out["停止原因"] = (f"{city} {name} 不是自己国家，"
                                  "编号第2位是1或2，不能占领，暂停移动")
                break
            elif owner != my and cnt:
                log.info("[移动] %s %s 有 %d 个敌方，先打这座城", city, name, cnt)
                fought = farm_city(rec, sock, config, city, sweep=True,
                                   country=my, beat=beat, pass_block=True,
                                   tally=tally)
                reason = fought.get("停止原因") or ""
                if any(k in reason for k in ("行动力", "遣返", "不相邻", "恢复卡", "位置变了", "已暂停", "被别人打败", "已手动关停", "没打过")):
                    out["停止原因"] = reason
                    break
                if _road_blocked(reason, fought):
                    blocked_at = city
                    block_who = fought.get("挡路") or block_who
                    log.info("[移动] %s %s 有打不过的敌方，这座城不可通行，准备改道",
                             city, name)
                    break
                info = _open_city(sock, rec, city, owner or my)
                if not info:
                    out["停止原因"] = f"打完后打不开 {city} {name}"
                    break
                owner, cnt = info["owner"], info["userCnt"]
                if info.get("here") == int(city) and cnt:
                    cnt -= 1
                citydb.record_occupy(city, owner, cnt)
                if info.get("here") == int(city):
                    moved = {"here": int(city), "power": info.get("power")}
                    log.info("[移动] 人已经在 %s %s，不用再发占领", city, name)
                elif owner == my or not cnt:
                    log.info("[移动] %s %s 人已清空，占领", city, name)
                    moved, why = _commit_move(sock, rec, city, owner or my, my)
                    if not moved and "ret=30" in (why or ""):
                        blocked_at = city
                        log.info("[移动] %s %s 占领被拒 ret=30，避开它重新规划",
                                 city, name)
                else:
                    blocked_at = city
                    block_who = fought.get("挡路") or block_who
                    log.info("[移动] %s %s 打完还有 %d 人，避开这座城重新规划",
                             city, name, cnt)
                    break
            elif owner != my:
                info = _open_city(sock, rec, city, owner, retry=False)
                if info:
                    owner = info["owner"] or owner
                    need = info.get("need") or 0
                    power = info.get("power")
                    cnt = info["userCnt"]
                    if info.get("here") == int(city) and cnt:
                        cnt -= 1
                    if (isinstance(power, int) and not isinstance(power, bool)
                            and need and power < need and not cnt
                            and owner != my):
                        item = int(conf.get("国战恢复卡物品ID") or CARD_ITEM_ID)
                        sent, msg = _use_recovery_card(sock, rec, item)
                        log.info("[移动] 行动力 %s < 这一步要的 %s，%s",
                                 power, need, msg)
                        if not sent:
                            out["停止原因"] = (f"行动力 {power} 不够占领 "
                                              f"{city} {name}（要 {need}）；{msg}")
                            break
                        _nap(2.0)
                if cnt and owner != my:
                    log.info("[移动] %s %s 再看还有 %d 人，所属 %s，改为先打",
                             city, name, cnt, owner)
                    fought = farm_city(rec, sock, config, city, sweep=True,
                                       country=my, beat=beat, pass_block=True, tally=tally)
                    reason = fought.get("停止原因") or ""
                    if any(k in reason for k in ("行动力", "遣返", "不相邻", "恢复卡", "位置变了", "已暂停", "被别人打败", "已手动关停", "没打过")):
                        out["停止原因"] = reason
                        break
                    if _road_blocked(reason, fought):
                        blocked_at = city
                        block_who = fought.get("挡路") or block_who
                        log.info("[移动] %s %s 有打不过的敌方，这座城不可通行，准备改道",
                                 city, name)
                        break
                    info = _open_city(sock, rec, city, owner or my)
                    if info and info.get("here") == int(city):
                        moved = {"here": int(city), "power": info.get("power")}
                        why = ""
                    elif info and (info["owner"] == my or not info["userCnt"]):
                        moved, why = _commit_move(sock, rec, city,
                                                  info["owner"] or owner, my)
                    else:
                        blocked_at = city
                        block_who = fought.get("挡路") or block_who
                        log.info("[移动] %s %s 打完还有人，避开这座城重新规划",
                                 city, name)
                else:
                    moved, why = _commit_move(sock, rec, city, owner or my, my)
                if not moved and "城里还有" in (why or ""):
                    log.info("[移动] %s %s %s，改为先打", city, name, why)
                    fought = farm_city(rec, sock, config, city, sweep=True,
                                       country=my, beat=beat, pass_block=True, tally=tally)
                    reason = fought.get("停止原因") or ""
                    if any(k in reason for k in ("行动力", "遣返", "不相邻", "恢复卡", "位置变了", "已暂停", "被别人打败", "已手动关停", "没打过")):
                        out["停止原因"] = reason
                        break
                    if _road_blocked(reason, fought):
                        blocked_at = city
                        block_who = fought.get("挡路") or block_who
                        log.info("[移动] %s %s 有打不过的敌方，这座城不可通行，准备改道",
                                 city, name)
                        break
                    info = _open_city(sock, rec, city, owner or my)
                    if info and (info.get("here") == int(city)
                                 or info["owner"] == my or not info["userCnt"]):
                        moved, why = _commit_move(sock, rec, city,
                                                  info["owner"] or my, my)
                if not moved and "ret=30" in (why or ""):
                    log.info("[移动] %s %s 占领被拒 ret=30，先尝试攻打", city, name)
                    fought = farm_city(rec, sock, config, city, sweep=True,
                                       country=my, beat=beat, pass_block=True, tally=tally)
                    reason = fought.get("停止原因") or ""
                    log.info("[移动] %s %s 攻打结束：%s，成功 %d，失败 %d",
                             city, name, reason, fought.get("成功") or 0,
                             fought.get("失败") or 0)
                    if any(k in reason for k in ("行动力", "遣返", "不相邻", "恢复卡", "位置变了", "已暂停", "被别人打败", "已手动关停", "没打过")):
                        out["停止原因"] = reason
                        break
                    if _road_blocked(reason, fought):
                        blocked_at = city
                        block_who = fought.get("挡路") or block_who
                        log.info("[移动] %s %s 有打不过的敌方，这座城不可通行，准备改道",
                                 city, name)
                        break
                    if not fought.get("打过") and not fought.get("失败"):
                        out["停止原因"] = (f"{city} {name} 空城没有人可打，"
                                          f"占领仍被拒：{why}")
                        break
                    info = _open_city(sock, rec, city, owner or my)
                    if info and info.get("here") == int(city):
                        moved = {"here": int(city), "power": info.get("power")}
                        why = ""
                    elif info and not info["userCnt"]:
                        moved, why = _commit_move(sock, rec, city,
                                                  info["owner"] or owner, my)
                    if not moved:
                        blocked_at = city
                        block_who = fought.get("挡路") or block_who
                        log.info("[移动] %s %s 打完仍进不去，避开这座城重新规划",
                                 city, name)
            else:
                moved, why = _commit_move(sock, rec, city, my, my)
                if not moved and "ret=7" in (why or ""):
                    moved = {"here": int(city), "power": info.get("power")}
                    why = ""
                    log.info("[移动] %s %s 已在城里（ret=7），继续", city, name)
                elif not moved and cnt and "ret=30" in (why or ""):
                    log.info("[移动] %s %s 进不去（城里 %d 人），先把人清掉再进",
                             city, name, cnt)
                    fought = farm_city(rec, sock, config, city, sweep=True,
                                       country=my, beat=beat, tally=tally)
                    reason = fought.get("停止原因") or ""
                    if any(k in reason for k in ("行动力", "遣返", "不相邻", "恢复卡", "位置变了", "已暂停", "被别人打败", "已手动关停", "没打过")):
                        out["停止原因"] = reason
                        break
                    if fought.get("失败") and not fought.get("成功"):
                        who = fought.get("挡路") or ""
                        if who:
                            out["停止原因"] = f"{city} {name} 有 {who} 挡路，路径不通"
                        else:
                            out["停止原因"] = f"{city} {name} 剩下的人都打不过，路径不通"
                        break
                    info = _open_city(sock, rec, city, my)
                    if info and info.get("here") == int(city):
                        moved, why = {"here": int(city), "power": info.get("power")}, ""
                    elif info and not info["userCnt"]:
                        log.info("[移动] %s %s 人已清空，再走进去", city, name)
                        moved, why = _commit_move(sock, rec, city, my, my)
            if blocked_at:
                break
            if not moved:
                out["停止原因"] = f"{city} {name}：{why or '还没走进去'}"
                break
            out["移动"] += 1
            out["步数"] = int(out.get("步数") or 0) + 1
            out["走到"] = moved["here"]
            citydb.note_attack_here(moved["here"])
            log.info("[移动] 进入 %s %s，行动力 %s",
                     moved["here"], name, moved.get("power"))
        if blocked_at:
            cname = citydb.city_name(blocked_at) or blocked_at
            if not block_who:
                block_who = _names_standing(sock, rec, blocked_at, my)
            if replanned:
                who = block_who or (avoid_why or {}).get(blocked_at) or ""
                if who:
                    out["停止原因"] = (
                        f"{blocked_at} {cname} 有 {who} 挡路，路径不通。"
                        f"避开这座城重新规划后还是过不去")
                else:
                    out["停止原因"] = (
                        f"路径上 {blocked_at} {cname} 有打不过的人，这条路不通。"
                        f"避开这座城重新规划后还是过不去")
                return out
            log.info("[路线] %s %s 攻打失败，按最短路径重新规划，避开它",
                     blocked_at, cname)
            why = dict(avoid_why or {})
            if block_who:
                why[int(blocked_at)] = block_who
            left = None
            if max_steps is not None:
                left = max(0, int(max_steps) - int(out.get("步数") or 0))
                if left <= 0:
                    return out
            nxt = walk_to(rec, sock, config, target, sweep=sweep, beat=beat,
                          avoid=set(avoid or ()) | {blocked_at}, replanned=True,
                          uid=uid, tally=tally, avoid_why=why,
                          march_only=march_only, max_steps=left,
                          enter_target=enter_target, clear_plan=clear_plan)
            out["移动"] += nxt.get("移动") or 0
            out["步数"] = int(out.get("步数") or 0) + int(nxt.get("步数") or 0)
            if nxt.get("走到"):
                out["走到"] = nxt["走到"]
                citydb.note_attack_here(nxt["走到"])
            if nxt.get("攻击") is not None:
                out["攻击"] = nxt["攻击"]
            out["停止原因"] = nxt.get("停止原因") or ""
            return out
        if out["停止原因"]:
            return out
        if march_only:
            return out
        if not citydb.can_reach(out["走到"], target):
            here = citydb.city_name(out["走到"]) or out["走到"]
            there = citydb.city_name(target) or target
            out["停止原因"] = (
                f"停在 {out['走到']} {here}，还没挨到目标 {target} {there}。"
                f"路上有打不过的人时，这条路就不通")
            return out
        tname = citydb.city_name(target) or target
        if uid:
            log.info("[移动] 人在 %s，只打目标 %s %s 里的 UID %s",
                     out["走到"], target, tname, uid)
            info = citydb.find_player(uid) or {}
            page = int(info.get("page") or 0)
            hitp = attack_player(
                rec, sock, config, uid, city_id=int(target), sweep=True,
                beat=beat, until_down=True, page=page)
            out["攻击"] = hitp.get("成功") or 0
            if hitp.get("击退"):
                _note_repel(out, tally, hitp.get("名字") or "")
            if hitp.get("击退") or out["攻击"]:
                out["停止原因"] = "" if hitp.get("击退") else (hitp.get("停止原因") or "")
            else:
                out["停止原因"] = hitp.get("停止原因") or f"没打到 UID {uid}"
            return out
        plan = clear_plan if isinstance(clear_plan, dict) and clear_plan.get("pages") else None
        if plan:
            start, end = plan["pages"]
            gap = int(plan.get("scan_sec") or 0)
            extra = f"，扫页冷却 {gap} 秒" if gap > 0 else ""
            log.info("[移动] 人在 %s，按清城配置打 %s %s 第 %d–%d 页%s",
                     out["走到"], target, tname, int(start) + 1, int(end) + 1, extra)
            fought = farm_city(rec, sock, config, target, sweep=True,
                               country=my, beat=beat, tally=tally,
                               pages=(int(start), int(end)),
                               priority=plan.get("priority") or {},
                               scan_sec=gap)
            hit = fought.get("成功") or 0
            reason = fought.get("停止原因") or ""
            if reason in ("这座城打完了", "这一页没有可打的人", "这几页没有可打的人"):
                out["停止原因"] = "" if (fought.get("打过") or hit) else "这几页没有可打的人"
            elif any(k in reason for k in ("行动力", "遣返", "不相邻", "恢复卡", "位置变了", "已暂停", "被别人打败", "已手动关停")):
                out["停止原因"] = reason
            elif fought.get("失败") and not fought.get("成功") and not hit:
                out["停止原因"] = f"{target} {tname} 剩下的人都打不过，这座城清不完，已停止"
            else:
                out["停止原因"] = reason
            out["攻击"] = hit
            return out
        log.info("[移动] 人在 %s，开始清目标 %s %s 里的人",
                 out["走到"], target, tname)
        hit = 0
        last_cnt = None
        while True:
            stopped = _manual_stop()
            if stopped:
                out["停止原因"] = stopped
                break
            if citydb.attack_paused():
                out["停止原因"] = "已暂停"
                break
            info = _open_city(sock, rec, target, my)
            if not info:
                out["停止原因"] = f"打不开目标 {target} {tname}"
                break
            left = info["userCnt"]
            if info.get("here") == int(target) and left:
                left -= 1
            if not left:
                log.info("[移动] 目标 %s %s 城里已经没人", target, tname)
                out["停止原因"] = ""
                break
            if last_cnt is not None and left >= last_cnt:
                if hit:
                    log.info("[移动] 目标 %s %s 还剩 %d 人，打得动的已经打完，打不过的在战败库",
                             target, tname, left)
                    out["停止原因"] = ""
                else:
                    out["停止原因"] = f"{target} {tname} 还有 {left} 人，再打人数没减少"
                break
            last_cnt = left
            log.info("[移动] 目标 %s %s 还有 %d 人", target, tname, left)
            fought = farm_city(rec, sock, config, target, sweep=True,
                               country=my, beat=beat, tally=tally)
            hit += fought.get("成功") or 0
            reason = fought.get("停止原因") or ""
            if any(k in reason for k in ("行动力", "遣返", "不相邻", "恢复卡", "位置变了", "已暂停", "被别人打败", "已手动关停")):
                out["停止原因"] = reason
                break
            if fought.get("成功"):
                continue
            if hit and (fought.get("失败") or fought.get("跳过") or reason in (
                    "这座城打完了", "这一页没有可打的人", "这几页没有可打的人")):
                log.info("[移动] 目标 %s %s 打得动的已经打完，打不过的记在战败库",
                         target, tname)
                out["停止原因"] = ""
                break
            if fought.get("失败") and not fought.get("成功"):
                out["停止原因"] = f"{target} {tname} 剩下的人都打不过，这座城清不完，已停止"
                break
            if not fought.get("成功") and not fought.get("失败"):
                out["停止原因"] = reason or f"{target} {tname} 还有人但没打到"
                break
        out["攻击"] = hit
        return out
    finally:
        restore_beat()
