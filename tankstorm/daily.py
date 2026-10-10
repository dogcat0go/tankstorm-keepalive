# tankstorm-keepalive  Copyright (C) 2026 Dimlitter
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# 本程序是自由软件：你可以依据自由软件基金会发布的 GNU Affero 通用公共许可证
# （第 3 版，或你选择的任何更新版本）之条款，再分发和/或修改它。
# 本程序希望能有用，但不提供任何担保；甚至不含适销性或特定用途适用性的默示担保。
# 详见随附的 LICENSE 文件，或 <https://www.gnu.org/licenses/>。
"""每日任务：按固定顺序发出免费领取类请求。

安全设计（四层，缺一不可）
--------------------------
1. **白名单**：只允许发 TASKS 表里登记的 opcode，其余一律拒发。表以外的 opcode
   连构造的机会都没有。
2. **危险字段硬校验**：字段名命中 buy/cost/num/count/cnt/price 等的，值必须为 0，
   否则拒发并告警。这是防"免费次数用完自动买"的最后一道闸。
3. **状态闸门**：先读前置请求（开面板）响应里的剩余免费次数，>0 才发；
   读不到就保守拒发。这是照抄真实客户端的判断（见 Gate 的注释）。
4. **每日次数上限 + 冷却**：每天最多 N 次，可重复任务两次之间还要等冷却。

为什么必须硬校验，而不能依赖游戏的确认框
----------------------------------------
游戏里"消耗勋章"确实会弹确认框，但那是**纯客户端 UI**：
  · '是否消耗' 出现在 onAllBtn1Click / onAllBtn2Click（按钮处理器）
  · '确认购买' 出现在 Buytip::processPanel（面板类）
  · 协议里不存在任何二次确认消息，抽奖类消息都是单包完成
脚本直接发包**不经过任何对话框**，服务器收到就扣。所以确认框对脚本的保护是 0，
必须在我们这一侧把危险字段拦死。

任务顺序
--------
"每日任务领奖"(RceDailyTask) 必须排在最后 —— 前面那些操作本身会推进每日任务
进度，先领就漏了。TASKS 表的顺序即执行顺序，ORDER_LAST 里的排到末尾。

参数来源
--------
字段值优先用**抓包实测**（tools/capture_daily.py 从 logs 里提取真实客户端发的包），
而不是从 schema 猜。confidence 字段标明每个任务的参数是实测还是待确认；
待确认的任务默认不执行，需要在 config 里显式打开。
"""

import json
import os
import re
import select
import threading
import time
from datetime import date

from . import schema as _schema
from . import sender
from .log import LOG_DIR, get_logger
from .proto_encode import encode_message

log = get_logger()

STATE_FILE = os.path.join(LOG_DIR, "daily-state.json")

# 每日任务执行期间的心跳回调，由 socket_keepalive 在调用 run() 时传进来。
#
# 2026-08-29 实测：跑一轮 --daily 耗时 3 分 50 秒，期间发出的心跳是 **0 次**，
# 而 protocol.json 里心跳周期是 10 秒、真客户端抓包也是每 10 秒雷打不动一次
# （484 秒的连接里 48 次）。原先两种模式都有这个洞：--daily 压根没有心跳循环，
# --keepalive --daily 则是先跑完任务才进心跳循环。
# 那些查不出原因的"没等到响应"，很可能有它的份。
#
# 用单线程的"到点就发"而不是后台线程：心跳虽然走明文豁免、不碰 RC4，
# 但两个线程同时 sendall 会让帧字节交错，那是另一种更难查的坏法。
# 回调按线程分开。网页上每个攻打号一条线程，两个人同时做日常不能共用一个回调。
_beat_local = threading.local()
_state_local = threading.local()
_sock_local = threading.local()


def current_beat():
    return getattr(_beat_local, "fn", None)


def install_beat(fn):
    """换上这一线程的心跳回调，返回换之前的那个。"""
    prev = current_beat()
    _beat_local.fn = fn
    return prev


def bind_beat(fn):
    """fn 有值时暂时换上，返回还原函数。空的就不动。"""
    if fn is None:
        return lambda: None
    prev = install_beat(fn)

    def _restore():
        install_beat(prev)
    return _restore


def _beat() -> None:
    """该发心跳就发一次；没配回调就是空操作。"""
    fn = current_beat()
    if fn is None:
        return
    try:
        fn()
    except Exception as exc:                  # 心跳发不出去不该弄挂任务
        log.debug("心跳发送失败（忽略）: %s", exc)


def bind_sock(sock):
    """这一线程等任务的空隙从哪条连接读包。返回换之前的那个。"""
    prev = getattr(_sock_local, "sock", None)
    if sock is None:
        if hasattr(_sock_local, "sock"):
            del _sock_local.sock
        return prev
    _sock_local.sock = sock
    return prev


def _drain_incoming() -> None:
    """把已经到的包读掉。超级强攻靠这次读取才会走拒绝。

    只在做任务的这一条线程上读。心跳线程只发心跳，两边用同一把收发锁，
    不会同时 recv。
    """
    sock = getattr(_sock_local, "sock", None)
    if sock is None:
        return
    raw = sock
    while hasattr(raw, "_sock") and getattr(raw, "_sock", None) is not raw:
        raw = raw._sock
    try:
        readable, _, _ = select.select([raw], [], [], 0)
    except (OSError, TypeError, ValueError):
        return
    if not readable:
        return
    try:
        sock.settimeout(0.2)
        data = sock.recv(8192)
    except (TimeoutError, OSError):
        return
    if data == b"":
        raise OSError("服务器关闭连接")


class Stopped(BaseException):
    """网页关停了正在做的这项。不继承 Exception，各项自己的异常处理接不住。"""


def raise_if_stopped() -> None:
    from . import citydb
    if citydb.shutdown_due() or citydb.daily_job_stopped():
        raise Stopped()


def _nap(seconds: float) -> None:
    """等到点。心跳在独立线程发，这里把已经到达的包读掉。"""
    end = time.time() + max(0.0, float(seconds or 0))
    while True:
        raise_if_stopped()
        left = end - time.time()
        if left <= 0:
            return
        _beat()
        _drain_incoming()
        time.sleep(min(left, 0.5))


_FAIL_MARKS = ("失败", "拦截", "没收到", "未收到", "没有结算", "超时",
               "异常", "被拒", "不发", "认不出", "发送失败", "没领到",
               "需要花钱", "服务器返回", "执行异常")


def _is_failure(text) -> bool:
    """这一行要写到页面上的失败说明。跳过、冷却、没开不算失败。"""
    text = str(text or "").strip()
    if not text or text == "未开启":
        return False
    if text.startswith("今日已执行") or text.startswith("冷却") or text.startswith("占用"):
        return False
    if "跳过" in text and not any(mark in text for mark in _FAIL_MARKS):
        return False
    return any(mark in text for mark in _FAIL_MARKS)


def failure_brief(results) -> str:
    """这一轮里没做成的项，按发生顺序拼成一句。"""
    parts = []
    for key, value in (results or {}).items():
        text = str(value or "").strip()
        if _is_failure(text):
            parts.append(f"{key}：{text}")
    return "；".join(parts)


def finish_text(results) -> str:
    """整轮跑完后的说明。某一项失败写在说明里，整轮照样做完。"""
    failed = failure_brief(results)
    if not failed:
        return ""
    return failed + "。这一轮没有停，后面的任务已继续做"


# 页面说明里不展示这些协议字段。成功后面的 freeVisitCnt、名次积分那一长串都去掉。
_DUMP_NAMES = (
    "freeVisitCnt|leftFreeCnt|getTimes|leftTime|nResult|addsoul|addoil|"
    "addmetal|jungong|trainExp|nAddRes|bSignIn|nSignInDays|nReplenishDays|"
    "logonDays|nRankSelf|nRankSelfLast|nIntegralScore|nCanFightTimes|"
    "nJoinPlayers|bScoreGiftGain|bLastRankGet|ngetdailyawd|ncangetCnt|"
    "dayHasPK|lastBtlRank|curSesionBtlOver|bRankGet|field\\d+"
)
_DUMP_ASSIGN = (
    rf"(?:{_DUMP_NAMES})(?:\[[^\]]*\])?(?:\.field\d+(?:\[[^\]]*\])?)*"
    rf"\s*=\s*(?:\[[^\]]*\]|True|False|None|-?\d+(?:\.\d+)?)?"
)


def _strip_dumps(text) -> str:
    """去掉协议字段赋值。括号里全是字段的，整段拿掉。"""
    text = re.sub(rf"[（(]\s*(?:{_DUMP_ASSIGN}[\s,，]*)+[）)]", "", text)
    text = re.sub(rf"\s*{_DUMP_ASSIGN}", "", text)
    text = re.sub(r"[（(]\s*[）)]", "", text)
    text = re.sub(r"成功[：:]\s*$", "成功", text)
    text = re.sub(r"成功[：:]\s+", "成功 ", text)
    return text


def _only_dump(text) -> bool:
    """整段只剩协议字段，没有别的说明。"""
    raw = str(text or "")
    if not re.search(r"[A-Za-z_]\w*\s*=", raw):
        return False
    left = _strip_dumps(raw)
    left = re.sub(r"[\s；;，,。：:（）()成功]+", "", left)
    return left == ""


def page_result(text) -> str:
    """最近执行里的一项结果。去掉失败字样和字段、错误码解释。"""
    raw = str(text or "").strip()
    if not raw or raw == "未开启":
        return ""
    parts = []
    for piece in re.split(r"[；;]", raw):
        piece = _page_piece(piece)
        if piece:
            parts.append(piece)
    if parts:
        return "；".join(parts)
    if _only_dump(raw):
        return "成功"
    return ""


def _page_piece(text) -> str:
    text = str(text or "").strip()
    for noise in (
            "（该消息自己的错误码，含义未知）",
            "（再发就会扣券/勋章）",
            "（1 才是成功）",
            "（见 tools/capture_daily.py）",
            "。这一轮没有停，后面的任务已继续做",
            "这一轮没有停，后面的任务已继续做",
    ):
        text = text.replace(noise, "")
    text = re.sub(r"（错误码[^）]*）", "", text)
    text = re.sub(
        r"[（(][^）)]*(?:field\d|FreeCnt|VisitData|ret=|result=)[^）)]*[）)]",
        "", text)
    text = re.sub(
        r"\s*field\d+(?:\[[^\]]*\])?(?:\.field\d+(?:\[[^\]]*\])?)*\s*=\s*\S+",
        "", text)
    text = re.sub(r"\s+result=None", "", text)
    text = re.sub(r"成功[：:](?:ret|result|error|nret)=\d+", "成功", text)
    text = _strip_dumps(text)
    text = re.sub(r"今日已执行\s*\d+\s*/\s*\d+\s*次，跳过", "已做过", text)
    text = text.replace("今日已做过，跳过", "已做过")
    text = text.replace("参数未实测，已跳过", "未实测，已跳过")
    text = text.replace("占用中，服务器给的结束时刻还有", "占用中，还有")
    text = text.replace("失败：", "").replace("闸门拦截：", "")
    text = re.sub(r"\s+", " ", text).strip(" ；，。")
    if text in ("成功：", "成功:"):
        return "成功"
    return text


def _brief_keys():
    """说明里会出现的任务名。长的排前面，避免短名字切进长名字。"""
    keys = [task.key for task in TASKS]
    keys.extend(key for key, _name in CAMPAIGN_EXTRAS)
    return sorted(set(keys), key=len, reverse=True)


def _split_named(text):
    """把「名称：结果；名称：结果」拆开。结果自己的分号留在该项里。"""
    keys = _brief_keys()
    if not keys:
        return []
    pat = re.compile("(" + "|".join(re.escape(key) for key in keys) + ")：")
    matches = list(pat.finditer(text))
    if not matches:
        return []
    items = []
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        value = text[match.end():end].strip().strip("；; \n")
        if value:
            items.append((match.group(1), value))
    return items


def _trim_done_prefix(text):
    text = str(text or "").strip()
    for prefix in ("成功：", "成功:"):
        if text.startswith(prefix):
            return text[len(prefix):].strip()
    return text


def _group_brief(items) -> str:
    """已做过、成功收成一行名字。没做成的收成一行，并带上原因。"""
    done = []
    failed = []
    held = []
    other = []
    for key, text in items:
        pieces = [part.strip() for part in re.split(r"[；;]", str(text or "")) if part.strip()]
        rest = [part for part in pieces if part not in ("已做过", "成功")]
        if not pieces:
            continue
        if all(part.startswith("暂不执行") for part in pieces):
            held.append(key)
            continue
        if not rest:
            done.append(key)
            continue
        if key in ("征战第三次", "征战第4次"):
            joined = "；".join(rest)
            if ("打完" in joined and "没打完" not in joined
                    and not any(_is_failure(part) for part in rest)):
                done.append(key)
                continue
        shown_parts = []
        for part in rest:
            part = _trim_done_prefix(part).strip()
            if part and part not in ("成功", "已做过"):
                shown_parts.append(part)
        if not shown_parts:
            done.append(key)
            continue
        shown = "；".join(shown_parts)
        line = f"{key}：{shown}"
        if any(_is_failure(part) for part in rest):
            failed.append(line)
        else:
            other.append(line)
    lines = []
    if done:
        lines.append("已完成：" + "、".join(done))
    if failed:
        lines.append("失败：" + "；".join(failed))
    if held:
        lines.append("未执行：" + "、".join(held) + "。反复被拒会打断连接，先不做")
    if other:
        if done or failed or len(other) > 1:
            lines.append("其他：" + "；".join(other))
        else:
            lines.append(other[0])
    return "\n".join(lines)


def page_brief(results) -> str:
    """最近执行的说明。做完的收成一组，没做成的收成一组。整轮结束才用。"""
    items = []
    for key, value in (results or {}).items():
        text = page_result(value)
        if text:
            items.append((str(key), text))
    return _group_brief(items)


def page_task(key, text) -> str:
    """正在做的这一项。只写这一项，不带前面已经做过的。"""
    shown = page_result(text)
    key = str(key or "").strip()
    if not shown:
        return f"{key}：进行中" if key else ""
    if not key:
        return shown
    return f"{key}：{shown}"


def _clean_grouped(raw) -> str:
    """已经分过组的旧说明，去掉字段后再分一次。"""
    done = []
    items = []
    seen = set()
    for line in raw.split("\n"):
        line = line.strip()
        if not line:
            continue
        if line.startswith("已完成："):
            for part in re.split(r"[、,，]", line[len("已完成："):]):
                name = part.strip()
                if name and name not in seen:
                    done.append(name)
                    seen.add(name)
            continue
        body = line
        for prefix in ("失败：", "其他："):
            if body.startswith(prefix):
                body = body[len(prefix):]
                break
        for key, value in _split_named(body):
            if key in seen:
                continue
            text = page_result(value)
            if not text:
                continue
            items.append((key, text))
            seen.add(key)
    ordered = [(key, "成功") for key in done]
    ordered.extend(items)
    return _group_brief(ordered) or raw


def compact_detail(text) -> str:
    """页面上的旧说明也按这一组分，并去掉协议字段。"""
    raw = str(text or "").strip()
    if not raw:
        return raw
    if raw.startswith(("已完成：", "失败：", "其他：")):
        return _clean_grouped(raw)
    items = _split_named(raw)
    if not items:
        return raw
    return _group_brief(items) or raw


def display_job_detail(status, detail) -> str:
    """正在做时只留当前一项。做完才显示这一轮的总状态。"""
    raw = str(detail or "").strip()
    if status != "running":
        return compact_detail(raw)
    if not raw.startswith(("已完成：", "失败：", "其他：")):
        return raw
    items = []
    for line in raw.split("\n"):
        body = line.strip()
        for prefix in ("已完成：", "失败：", "其他："):
            if body.startswith(prefix):
                body = body[len(prefix):]
                break
        items.extend(_split_named(body))
    if items:
        key, value = items[-1]
        return page_task(key, value)
    if raw.startswith("已完成："):
        names = [part.strip() for part in re.split(
            r"[、,，]", raw.split("\n", 1)[0][len("已完成："):]) if part.strip()]
        if names:
            return f"{names[-1]}：成功"
    return raw


def _publish(text, on_fail) -> None:
    if not on_fail or not text:
        return
    try:
        on_fail(text)
    except Exception:
        log.info("日常进度没写上页面", exc_info=True)


def _publish_current(key, text, on_fail) -> None:
    """做着的时候，页面只更新这一项。"""
    _publish(page_task(key, text), on_fail)


def _publish_total(results, on_fail) -> None:
    """全部做完，换成这一轮的总状态。"""
    _publish(page_brief(results) or "这一轮没有要做的", on_fail)

# 字段名命中这些词 = 可能花钱/耗券，值必须为 0
#
# credit 就是勋章（RceWPCExplore.credit、RceMineModify.credit、
# RceCountryOpt.costCredit 都是），useItemID/useItemCnt 是"优先使用XX券"那个
# 勾选框对应的字段。游戏里免费次数用完后，同一个按钮会转而扣券或扣勋章，
# 所以这些字段必须钉死为 0。
DANGER_FIELD = re.compile(
    r"buy|cost|price|money|gold|coin|diamond|gem|pay|charge|"
    r"num|cnt|count|times|amount|soul|medal|credit|"
    r"item|card|ticket|discount", re.I)

# (opcode, 字段名) 白名单：抓包实证这个字段在**这一条消息里**不是花钱字段。
# 只按消息逐条放行，绝不整体放宽 DANGER_FIELD —— 同名字段在别的消息里照样危险。
#
# 0463 RceCountryOpt.count：这条消息里真正花钱的是 costCredit。count 在
#   2026-08-10 的开箱包里是免费的个数，值可以不是 0，不能被危险词拦下。
#   国家宝箱从 2026-10-04 起改走 type:26，不再发 count。
SAFE_FIELDS = {("0463", "count")}

# 这些任务放到最后执行（它们领的是"前面动作累积出来的"奖励）
ORDER_LAST = {"每日任务", "周任务"}

# 军事演习会把有人的场地逐个打下去，服务器回 ret=4，最后把连接打断。
# 矿区争夺同样不稳。先不做。开关保持关，最近执行里写明。
HELD_TASKS = ("军事演习", "矿区争夺")
HELD_STATUS = "暂不执行，反复被拒会打断连接"

# 连续多少轮收不到响应就放弃该任务（当天）。
# 设 3 是为了容忍偶发的网络抖动/响应慢，又不至于无限期地空发。
MAX_MISS = 3

# 服务器响应里 ret 的含义
# ------------------------
# 2026-08-09 从 SWF 字节码确认（不再是推断）：RseHeroVisit 的处理函数
# _-5TJ:_-18n::_-3gC 开头就是
#     if (msg.ret != 0) {
#         if (msg.ret == 1) { BUY.open(); return; }        // 弹充值/购买面板
#         POPUPS.alert(Locales.Get('heroRecruitError' + msg.ret));  return;
#     }
#     ... 正常流程 ...
# 所以：
#   ret == 0  成功
#   ret == 1  要花钱才能做（客户端会弹商店），对脚本而言就是失败
#   ret >= 2  具体错误，文案键是 heroRecruitError{ret}
# 文案本身在外部语言包里，SWF 常量池只有 'heroRecruitError' 这个前缀，
# 所以 ret=3 的中文说明还拿不到；但"非 0 即失败"这条已经是铁的。
#
# 保守起见，一旦判为失败就当天不再重试该任务 —— 宁可少做一次，
# 也不要在"次数已用完"的情况下反复撞墙。
#
# 曾经这里有个 RET_IS_PAYLOAD = {"RseWPCExplore"} 的例外，理由是"它的 ret 是
# 12672/76032 这种大数，装的是收益不是状态码"。那其实是 schema 字段名错位
# 导致的误读：RseWPCExplore 真正的 ret 是 7 号字段，而当时读的 6 号字段
# 是 leftTime（冷却秒数，12672 秒≈3.5 小时，数量级正好对得上）。
# 字段名修正后它和别的响应一样按 ret 判，例外已删除。

# 响应里这些字段若存在，用来展示"获得了什么"
REWARD_HINT = ("ret", "freeVisitCnt", "leftFreeCnt", "getTimes", "leftTime",
               "nResult", "addsoul", "addoil", "addmetal", "jungong",
               "trainExp", "nAddRes")


# 请求里用来区分"这是哪一步"的字段名。前置和动作常常是同一个 opcode
# （04a5 既是开面板又是训练），响应也就同名，光按消息名等会把**前置的回包**
# 当成动作的结果 —— 2026-08-12 实盘就这样：战略训练第 6、7 次服务器已经回
# ret=11 拒绝了，我们却匹配到前置那条 type:1 的回包，判成功继续打。
# 除了"这是哪一步"（OptType/type），还要带上"这是哪一档" —— 技能书将领和参谋
# 是同一个 opcode、都发 OptType:1，**只靠 ActiveType 区分**（0=将领 1=参谋）。
# 2026-08-13 03:13:06 两条回包同一秒到达，不认 ActiveType 就会互相串。
# subType/sceneID/nExlType 同理，是特工派遣/配件探索/军备的档位选择字段。
# ⚠️ 这是按**字段名逐条登记**的白名单，不做模糊匹配 —— 和 SAFE_FIELDS 一样，
# 宁可漏也不整体放宽。代价是新消息容易漏登记：`optype` 就是这么漏的，
# 它和已在表里的 `optType` 差一个字母（opt+Type vs op+type），小写化也对不上，
# 结果锦鲤心愿造不出判据被拒发（2026-09-01 实盘）。
# 漏登记的症状很好认：日志里报"认不出动作回包"，且会把当时的字段名列出来。
DISCRIMINATORS = ("type", "noptType", "OptType", "optType", "optype", "nType",
                  "ntype", "nOptType", "ActiveType", "activetype", "subType",
                  "sceneID", "nExlType")


def _field_names(schema, op):
    """opcode -> {字段号: 字段名}。schema 模块没有现成的 field_names。"""
    e = (getattr(schema, "SCHEMA", {}) or {}).get(op) if schema else None
    if not e:
        return {}
    return {int(k): v[0] for k, v in e.get("fields", {}).items()}


def _echo_want(fields, names):
    """按请求里的区分字段，造一个"这条响应是不是本次动作的回包"的判据。

    响应里没有这个字段就不强求（有些回包确实不回显），只在**回显了但对不上**
    时拒绝 —— 那必然是别的步骤的回包。
    """
    checks = [(names[fno], val)
              for fno, (_t, val) in fields.items()
              if names.get(fno) in DISCRIMINATORS]
    if not checks:
        return None

    def want(d):
        return all(d.get(n) == v for n, v in checks if n in d)
    return want


def _rse_name(rce_msg: str) -> str:
    """请求消息名 → 对应的响应消息名（RceXxx → RseXxx）。"""
    return "Rse" + rce_msg[3:] if rce_msg.startswith("Rce") else rce_msg


# 响应里表示"还剩几次"的字段（名字以修正后的 schema 为准）。
#
# 注意这里**不能**放 leftTime 和 finishVisitTime：它们是冷却/完成时刻（秒），
# 不是次数。此前把 leftTime 当次数用，读到 12672 就以为"还剩一万多次"。
# 也不能放 hasCreditVisit —— 它是 bool（"是否还能花勋章再来一次"），
# 而 Python 里 isinstance(True, int) 为真，会被当成"剩 1 次"。
LEFT_FIELDS = ("freeVisitCnt", "leftFreeCnt", "getTimes", "nfreeTimes",
               "nRemainFreeNum")


def _left_count(data):
    """从响应里取剩余次数。返回 None 表示这条响应没提供该信息。

    freeVisitCnt 这类字段是 [各档剩余次数] 的数组（实测 [2,1,1]→[1,1,1]），
    取最大值：任一档还有免费次数就算还能做。
    """
    if not isinstance(data, dict):
        return None
    for f in LEFT_FIELDS:
        v = data.get(f)
        if isinstance(v, bool):            # bool 是 int 的子类，必须先挡掉
            continue
        if isinstance(v, int):
            return v
        if isinstance(v, (list, tuple)):
            nums = [x for x in v if isinstance(x, int) and not isinstance(x, bool)]
            if nums:
                return max(nums)
    return None


_U64_MAX = 2 ** 64 - 1


def _show(v):
    """给人看的值。服务端用无符号 64 位表示 -1（"没有/未上榜"），
    直接打出 18446744073709551615 只会让人以为是天文数字。"""
    if isinstance(v, int) and not isinstance(v, bool) and v >= _U64_MAX - 8:
        return "无"
    return v


# 这些响应不带 ret，成败看 result（1=成功），语义与 ret 相反
RESULT_IS_STATUS = {"RseArenaOpt", "RseWorldArenaOpt", "RseRegionArenaOpt",
                    "RseHeroArenaOpt"}


def judge(rse_msg: str, data, ignore_left=False, success_flag=None):
    """判断一次任务的结果。返回 (是否成功, 说明, 是否应停止今日重试)。

    "没等到响应"和"服务器明确拒绝"必须区别对待
    ------------------------------------------
    以前两者都会把当天次数直接打满。结果是：网络抖一下、或者响应比 6 秒慢一点，
    这个任务当天就废了 —— 三档的任务一次超时就把三次机会全赔进去。
    实盘反馈"收不到返回内容于是就停止了"说的正是这个。

    现在只有**服务器明确回了非 0 的 ret** 才算"今天别做了"。
    收不到响应只当这一轮没做成，由调用方按连续失败次数决定何时放弃
    （见 run() 里的 miss 计数），免费次数本来就有闸门兜着，不会白撞墙。
    """
    if data is None:
        return False, "未收到响应（这一轮不算数，稍后再试）", False
    if not isinstance(data, dict):
        return True, str(data)[:80], False
    # success_flag：动作回包里这个布尔字段翻成 true 才算真做成了。
    # 给那些**没有 ret 也没有 result** 的消息用 —— judge() 找不到状态码就
    # 直接判成功，那是踩坑记录第 15 条（"一个请求成功不等于这件事做成了"）。
    # 锦鲤心愿的 RseDoubleElevenOfficer 就是这样：整条回包里没有任何状态码，
    # 唯一能说明领到了的就是 ngetdailyawd 由 false 翻成 true。
    if success_flag:
        v = data.get(success_flag)
        if v is True:
            return True, f"成功：{success_flag}=True", False
        if v is False:
            return False, f"{success_flag} 仍为 False，没领到", True
        return False, f"响应里没有 {success_flag}，认不出成败", False
    # 争霸战这一族（RseArenaOpt 等）没有 ret，用的是 **result，而且 1 才是成功**
    # ——语义和 ret 正好相反。不特判的话 judge() 找不到 ret 就直接判"成功"，
    # 又是一个假成功。2026-08-10 真客户端抓包实测：领取上期排名奖励回
    # {type:3, result:1}，随后 RseArenaInfo.bLastRankGet 由 false 翻 true。
    # 月卡回包没有 ret，成败看 nResult，0 才是领到。
    # 2026-10-10 三张都是 nResult=0，已领标记翻 true、剩余天数减 1。
    if rse_msg == "RseRedwarMonthCard":
        r = data.get("nResult")
        if not isinstance(r, int) or isinstance(r, bool):
            return False, "响应里没有 nResult，认不出成败", False
        if r != 0:
            return False, f"服务器返回 nResult={r}", True
        return True, "成功：nResult=0", False

    if rse_msg in RESULT_IS_STATUS:
        r = data.get("result")
        if not isinstance(r, int) or isinstance(r, bool):
            return False, "响应里没有 result，认不出成败", False
        if r == 1:
            return True, "成功：result=1", False
        return False, f"服务器返回 result={r}（1 才是成功）", True

    # 多数响应用 ret，少数用别的名字：RseBuildingModify（英雄培养）用 error，
    # 有的用 nret。取第一个存在的整数字段当状态码。
    ret = next((data[k] for k in ("ret", "error", "nret")
                if isinstance(data.get(k), int) and not isinstance(data.get(k), bool)),
               None)
    if isinstance(ret, int) and ret != 0:
        if ret == 1:
            why = "需要花钱才能做（客户端此时会弹商店），已跳过"
        elif rse_msg == "RseHeroVisit":
            # 这个文案键是从 RseHeroVisit 的处理函数里读出来的，只对它成立，
            # 别的消息各有各的错误表，套上去就是误导（曾经把英雄培养的
            # ret=81 写成 heroRecruitError81，其实完全不相干）。
            why = f"服务器返回 ret={ret}（错误码 heroRecruitError{ret}）"
        else:
            why = f"服务器返回 ret={ret}（该消息自己的错误码，含义未知）"
        return False, why, True
    bits = [f"{k}={data[k]}" for k in REWARD_HINT if k in data]
    msg = "成功" + ("：" + " ".join(bits) if bits else "")
    # 成功了，但如果响应说剩余次数已归零，就别再来了。
    # 分档任务除外：动作回包里的 leftFreeCnt 只是**当前这一档**的剩余，
    # 打完 10001 它就是 0，可 10002/10003 各还有一次，换档的事交给闸门判。
    if not ignore_left:
        left = _left_count(data)
        if left is not None and left <= 0:
            return True, msg + "（剩余次数已用完，今日到此为止）", True
    return True, msg, False


class FromServer:
    """字段值占位符：发送时从服务器某条消息里现取。

    公会捐献要带自己的游戏名（抓包实测 tarUserName='<玩家名>'），这种值不能
    写死在仓库里 —— 换个号就错，而且属于个人信息。登录时服务器下发的
    RseInit 里就有（字段 username），运行时取即可。

    取不到就**不发这条任务**，不猜、不留空。
    """

    def __init__(self, rse_msg, field):
        self.rse_msg = rse_msg
        self.field = field

    def resolve(self, rec):
        if rec is None:
            return None
        got = rec.latest.get(self.rse_msg)
        if not got or not isinstance(got[1], dict):
            return None
        v = got[1].get(self.field)
        return v if isinstance(v, str) and v else None

    def __repr__(self):
        return f"<{self.rse_msg}.{self.field}>"


def _aslist(v):
    """protobuf 的 repeated 字段只出现一次时解码成标量，多次才是 list。"""
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


class FromResponse:
    """字段值占位符：从**前置请求的响应**里算出来。

    和 FromServer 的区别是时机：FromServer 取的是登录时就固定的信息（玩家名），
    FromResponse 要等前置请求发出去、服务器把列表推回来才有 —— 比如
    "哪个演习场没人占"、"七天乐今天是第几天"。所以它在前置之后才解析。

    pick(响应字典) 返回值，或返回 None 表示"没有可用的"，此时整条任务跳过。

    fresh=True  要等**这一轮前置请求之后**才到的响应。军事演习、七天乐属于这类：
                前置发出去，服务器才把场地列表/领取状态推回来，不等就必然读空。
    fresh=False 用最近一条即可，不要求是这轮新到的。每日任务属于这类：
                RseDailyTask 是服务器主动推的，没有对应的前置请求可发。
    """

    def __init__(self, rse_msg, pick, desc="", fresh=True, timeout=6.0):
        self.rse_msg = rse_msg
        self.pick = pick
        self.desc = desc or f"{rse_msg} 里挑一个"
        self.fresh = fresh
        self.timeout = timeout

    def resolve(self, rec, sock=None, since=0.0):
        if rec is None:
            return None
        if self.fresh and sock is not None:
            # 和闸门一样要**等**：前置刚发出去，响应还在路上。
            # 早先这里是直接读 rec.latest，前置的回包但凡慢一点就读空，
            # 任务被判成"取不到值"而跳过。
            data = _await_response(sock, rec, self.rse_msg, since, self.timeout)
            if data is None:
                return None
            return self._pick(data)
        got = rec.latest.get(self.rse_msg)
        if not got or not isinstance(got[1], dict):
            return None
        return self._pick(got[1])

    def _pick(self, data):
        try:
            return self.pick(data)
        except Exception as exc:                       # 结构和预期不符就当没有
            log.debug("FromResponse(%s) 解析失败: %s", self.rse_msg, exc)
            return None

    def __repr__(self):
        return f"<{self.rse_msg}: {self.desc}>"


class Followup:
    """动作成功之后，按响应内容继续发的后续请求。

    有两类任务光发一包不够：
      · 每日任务 —— 活跃度够几档就领几档，一档一包

    build(上一条响应) 返回下一包的 fields，返回 None 就结束。
    会反复调用（最多 max_rounds 轮），所以"领三档奖励"这种一次跑完。
    """

    def __init__(self, opcode, build, max_rounds=5, desc=""):
        self.opcode = opcode
        self.build = build
        self.max_rounds = max_rounds
        self.desc = desc


def _resolve_fields(task, rec, sock=None, since=0.0):
    """把占位符换成真实值。返回 (fields, 缺失的说明)。

    sock/since 传下去是为了让 FromResponse 能**等**前置请求的回包
    （since = 发前置之前的时刻，只认这之后到的）。
    """
    out = {}
    for fno, (ftype, val) in task.fields.items():
        if hasattr(val, "resolve"):
            try:
                got = val.resolve(rec, sock, since)
            except TypeError:            # FromServer 只收 rec
                got = val.resolve(rec)
            if got is None:
                # 多数时候这不是故障，而是"今天这份已经领过了"之类的正常状态
                return None, f"{val!r} 没有可做的（已领过或暂时没有），跳过"
            val = got
        out[fno] = (ftype, val)
    return out, ""


# ---------------------------------------------------------------- 挑选器
#
# 全部按 2026-08-10 抓包的真实响应结构写，字段号见各处注释。

def _pick_free_wargame_site(data):
    """军事演习：从查询响应里挑一个没人占的演习场。

    响应结构（RseWarGameOpt type=1，分页推送，bLastMsg 标记最后一页）：
        field14.field2 = [ {field1: 场地号, field2..field5: 属性,
                            field6: 占领者uid, field7: 占领者名} , ... ]
    没人占的条目就是**没有 field6**。实测最后一页 241 个场地里 121 个是空的。
    """
    sites = []
    for blk in _aslist(data.get("field14")):
        if isinstance(blk, dict):
            sites += [e for e in _aslist(blk.get("field2")) if isinstance(e, dict)]
    for e in sites:
        if not e.get("field6") and isinstance(e.get("field1"), int):
            return e["field1"]
    return None


def _pick_sevendays_day(data):
    """七天乐：只领**当天**那份，已领过就返回 None。

    响应 RseSevenDays{type:0}：logonDays=登录到第几天，
    field3 = 7 个 bool，field3[day-1] 表示第 day 天是否已领。
    实测 logonDays=3、field3=[F,T,F,...]，客户端领的正是 day=3。
    只领当天是为了贴合实测行为，不去猜历史几天还能不能补领。
    """
    day = data.get("logonDays")
    got = data.get("field3")
    if not isinstance(day, int) or not isinstance(got, list):
        return None
    if day < 1 or day > len(got):
        return None
    return None if got[day - 1] else day


def _next_mine_to_occupy(data):
    """矿区争夺：探索响应里挑一个无人占领的矿，返回占矿请求的字段。

    响应 RseResourceOpt{type:2} 的 field5 是探到的矿列表：
        {field1: 矿ID}                      ← 无人占领
        {field1: 矿ID, field2: 占领者名, field6: 占领者uid, ...}  ← 有人
    实测探到 120007(空) / 120058 / 120053，客户端占的正是 120007。
    """
    for e in _aslist(data.get("field5")):
        if isinstance(e, dict) and isinstance(e.get("field1"), int) \
                and not e.get("field6"):
            return {1: ("int32", 3), 2: ("int32", e["field1"])}
    return None


def _eligible_gift_tier(data):
    """每日任务：返回一个"活跃度已达标且还没领"的档位，没有就 None。

    响应 RseDailyTask：
        dailyTask.field2 = 当前活跃度
        getGift = [{field1: 档位(10/30/50/80/100), field2: 0未领/1已领}, ...]
    实测活跃度 64，领了 10/30/50 三档，80/100 因为没达标领不了。
    """
    task = data.get("dailyTask")
    if isinstance(task, list):
        task = task[-1] if task else None
    act = task.get("field2") if isinstance(task, dict) else None
    if not isinstance(act, int):
        return None
    for e in _aslist(data.get("getGift")):
        if not isinstance(e, dict):
            continue
        tier, taken = e.get("field1"), e.get("field2")
        if isinstance(tier, int) and tier <= act and not taken:
            return tier
    return None


def _next_daily_gift(data):
    """每日任务的后续领取：还有达标未领的档位就继续领。"""
    tier = _eligible_gift_tier(data)
    return None if tier is None else {5: ("int32", tier)}


class Gate:
    """动作前的免费次数闸门：读**前置请求的响应**，还有免费次数才发动作。

    为什么这样是对的（2026-08-09 从 SWF 反汇编确认）
    ------------------------------------------------
    真实客户端点"开采"时走的是 _-5TJ:_-18n::_-1RB：

        var left:int = _-h1.freeVisitCnt[type];   // _-h1 就是 RseHeroOpen 响应本身
        if (left > 0) {
            req.type = type; req.free = 1; Transport.Send(req);   // 免费档
        } else {
            ... 走扣道具 / 扣勋章的分支 ...
        }

    也就是说**免费次数就在开面板的响应里**，动作之前就能读到。
    此前一版把闸门去掉了，理由是"剩余次数只有动作响应里才有，首次运行必然
    等不到"——那是因为当时读的是动作响应 RseHeroVisit；而客户端读的是
    开面板响应 RseHeroOpen。前置请求本来就要发，它的响应正好就是闸门数据。

    读不到状态就**不发**：宁可这一轮不做，也不能在免费次数已尽时把请求发出去，
    那正是客户端会转而扣券/扣勋章的位置。

    **必须按档位取，不能取数组最大值**（2026-08-10 抓包纠正）
    ------------------------------------------------------
    freeVisitCnt 是 [低级, 中级, 高级] 各自的剩余免费次数，实测低级 3 次、
    中级 1 次、高级 0 次（高级从来就没有免费次数）。三个档位互相独立。
    用完低级之后数组是 `[0, 1, 0]` —— 取最大值会得到 1，闸门放行，
    然后照样发 type=0，服务端照样拒。所以 index 必须对上任务实际发的档位。

    rse_msg  等哪条响应（如 RseHeroOpen），它是 prelude 的回包
    field    读哪个字段（如 freeVisitCnt）
    index    该字段是分档数组时取第几档，要和任务发的 type/subType/sceneID 对上；
             响应给的是标量时忽略此项
    claimed_flag  该字段是"领过了吗"的布尔标记而不是次数：False 放行、True 拦下
    """

    def __init__(self, rse_msg, field, index=0, timeout=6.0,
                 claimed_flag=False, count_field=None):
        self.rse_msg = rse_msg
        self.field = field
        self.index = index
        self.timeout = timeout
        self.claimed_flag = claimed_flag
        self.count_field = count_field


class Tiers:
    """一个任务的多个档位，每档有各自独立的免费次数。

    闸门读到的数组第 i 项就是第 i 档还剩几次，把 field 换成 values[i]
    就是打那一档。实测：
        英雄开采/将领冶炼  type      = 0 / 1 / 2      freeVisitCnt=[3,1,1]
        特工派遣           subType   = 1001/1002/1003 freeVisitCnt=[3,1,1,…]
        配件探索           sceneID   = 10001/2/3      leftFreeCnt=[3,1,1]
    三档要一档一档领干净 —— 早先只打第 0 档，等于白扔掉中级和高级各一次。
    """

    def __init__(self, field, values):
        self.field = field
        self.values = values


class Task:
    """一个每日任务 = 前置请求 + 一条动作消息。

    **prelude 是必须的**（2026-08-09 定位）：真实客户端每个动作前都会先发一个
    "开面板 / 查询"请求，服务端据此建立会话上下文。此前脚本直接发动作、跳过这步，
    结果就是"参数一模一样却不生效" —— 抓包顺序铁证：

        RceHeroOpen{type:0}      → RceHeroVisit{free:1,type:0}
        RceAdmiralOpen{type:0}   → RceAdmiralVisit{free:1,type:0}
        RceCountryOpen{}         → RceCountryOpt{type:15}
        RceWPCBaseOpen{type:1}   → RceWPCExplore{sceneID:10001}
        RceWarCollegeOpt{type:1} → RceWarCollegeOpt{type:4,...}
        RceWarGameOpt{type:1}    → RceWarGameOpt{type:2,siteID:N}
        RceTrenchMortarOpt{optType:0} → {optType:1,subType:1001}
        RceJunBeiOpt{type:21}    → RceJunBeiOpt{type:22,nExlType:1}
        RceAdviserOpt{noptType:1}→ RceAdviserOpt{noptType:11}
        RceResourceOpt{type:1}   → RceResourceOpt{type:2,...}

    prelude 形如 [(opcode, {字段号: (类型, 值)}), ...]，按序发送，不判成败。
    """

    def __init__(self, key, name, opcode, msg, fields, confidence,
                 note="", max_per_day=1, gate=None, cooldown_sec=0,
                 prelude=(), followup=None, tiers=None, cooldown_until=None,
                 report=(), runner=None, success_flag=None,
                 counts_itself=False):
        # success_flag：动作回包里哪个布尔字段为 true 才算成功。只给那些
        # 既没有 ret 也没有 result 的消息用，别的一律走 judge() 的状态码。
        self.success_flag = success_flag
        # runner：自定义执行器，签名 runner(rec, sock, config) -> (是否成功, 说明)。
        # 给那些"形状不合流水线"的任务用 —— 国战要先召唤支援兵、再从服务端推来的
        # 列表里读出目标 ID 才能攻击，不是一条静态字段表能表达的。
        # 有 runner 的任务跳过前置/闸门/安全检查那一整套，由执行器自己负责，
        # 所以执行器内部必须自己守住"先查询、读到依据才做"这条铁律。
        self.runner = runner
        # counts_itself：执行器自己记今日次数。征战打关时不能把免费重开记成 2/2。
        self.counts_itself = bool(counts_itself)
        # report：前置响应里值得报给用户看的字段（排名、积分、剩余挑战次数…）。
        # 闸门数据本来就读到了，顺手带进结果里，推送时就能看到"现在排第几"。
        self.report = tuple(report)
        # cooldown_until：从响应里读"下次可用时刻"（unix 秒）的字段路径。
        # 比写死一个 cooldown_sec 靠谱得多 —— 占矿的时长随矿的等级变，
        # 演习场占领时长也是服务端定的，猜一个数必然是错的。
        self.cooldown_until = cooldown_until
        self.prelude = list(prelude)
        self.gate = gate                # 读 prelude 的响应，免费次数 >0 才发
        self.followup = followup        # 动作成功后按响应继续发（占矿、领多档奖励）
        self.tiers = tiers              # 多档位任务：逐档把免费次数领干净
        # 很多任务并非"一天一次"：军事演习占领后有时长，结束才能再占（每天 3 次）；
        # 英雄训练 8 小时可重复。cooldown_sec>0 表示两次执行之间要等这么久，
        # 配合 max_per_day 一起限制。守护进程会周期性地重跑任务轮次。
        self.cooldown_sec = cooldown_sec
        self.key = key
        self.name = name
        self.opcode = opcode
        self.msg = msg
        self.fields = fields            # {字段号: (类型, 值)}
        self.confidence = confidence    # "实测" | "待确认"
        self.note = note
        self.max_per_day = max_per_day

    def danger_fields(self):
        """返回违反"危险字段必须为 0"的字段列表。"""
        bad = []
        for fno, (ftype, val) in self.fields.items():
            fname = self.field_names.get(fno, "") if hasattr(self, "field_names") else ""
            if fname and DANGER_FIELD.search(fname) and val not in (0, "", None):
                bad.append(f"{fname}={val}")
        return bad


# ---------------------------------------------------------------- 任务表
#
# fields 形如 {字段号: ("int32"|"string", 值)}；字段号来自 docs/redwar.proto。
# 标 "实测" 的参数来自解密后的真实上行请求；"待确认" 的默认跳过，
# 需先用 tools/capture_daily.py 从抓包里提取真实值后再转正。

def _t(*a, **kw):
    return Task(*a, **kw)


TASKS = [
    # ---- 纯领取（第一批）----
    # ⚠️ 2026-08-13 纠正：**以前根本没在签到**，只发了查询那一条。
    # 原注释写"客户端只发 nType=0"是当初看漏了第二条。8/10 真客户端抓包实际是两条：
    #   {nType:0, nActivetype:0} 查询 → bSignIn:false, nSignInDays:8
    #   {nType:1, nActivetype:0} 签到 → bSignIn:true,  nSignInDays:9
    # 响应里 field9 是当月 31 天的 bool 数组，签到后对应那天由 false 翻 true。
    # nDay 和 nGiftID 客户端确实不发（服务端自己算今天是第几天），这点原来没错。
    _t("每日签到", "每日签到", "04a4", "RceDailySignIn",
       {2: ("int32", 1), 4: ("int32", 0)},
       "实测", "8/10 抓包：{nType:0} 查询 → {nType:1} 签到。"
               "闸门读 bSignIn：false 才签，true 说明今天签过了",
       prelude=[("04a4", {2: ("int32", 0), 4: ("int32", 0)})],
       gate=Gate("RseDailySignIn", "bSignIn", claimed_flag=True),
       report=("bSignIn", "nSignInDays", "nReplenishDays")),

    # ⚠️ 2026-08-29 实测：服务端**不再响应** RceSevenDays(047a)。
    # 请求 18:55:21 发出，到 18:55:29 收到别的任务的回包为止，整整 7 秒零回应；
    # 同一轮里其它任务都是请求→回包 1 秒内。活动已下线，config.json 里默认关掉了。
    # 任务定义保留：万一哪天活动回归，打开开关即可。
    # 限时活动，会不定期上下线：2026-08-29 下线（7 秒零回包），09-02 又恢复。
    # 下线期间发出去只是超时跳过，不影响别的任务，所以默认开着就行。
    _t("七天乐", "七天乐领奖", "047a", "RceSevenDays",
       {1: ("int32", 1),
        2: ("int32", FromResponse("RseSevenDays", _pick_sevendays_day,
                                  "取当天且未领的那天")),
        3: ("int32", 1)},
       "实测", "8/10 抓包：{type:0} 查询 → {type:1,day:3,gifttype:1} 领取。"
               "响应 logonDays=登录到第几天，field3[day-1]=该天是否已领。"
               "只领当天那份，不去猜历史几天能否补领",
       prelude=[("047a", {1: ("int32", 0)})],
       # ⚠️ RseSevenDays 既没有 ret 也没有 result，judge() 找不到状态码就直接
       # 判成功。真正能说明领到了的是 field3[day-1] 由 false 翻 true
       # （2026-09-02 实盘：领前全 false，领第 2 天后变成 [F,T,F,F,F,F,F]）。
       # 眼下先把它 report 出来，让这个状态在日志和推送里看得见 ——
       # 判据本身还差一步：success_flag 只能盯单个布尔字段，表达不了
       # "数组里第 day-1 格"，要补得先扩 success_flag 的写法。
       report=("logonDays", "field3")),

    _t("每日资源", "每日免费资源", "0444", "RceGetDailyRes",
       {}, "待确认", "无字段或字段未知，需实测"),

    _t("战功排名", "战功榜奖励", "04a2", "RceZhanGongRank",
       {1: ("int32", 0)}, "待确认", "需实测"),

    # 2026-10-10 00:42 抓包。三张都走 0408，type=0 只查询，type=1 才领取。
    # activetype 不是数组下标：0 领第一张（下标 0，勋章 +300），
    # 4 领第二张（下标 1），2 领第三张（下标 2）。
    # 查询回包 threeMonthCardGetAwd 对应位为 false、剩余天数 > 0 才发。
    # 以前这项发的是 type=0，只开面板，并不领取。
    _t("超值月卡", "超值月卡", "0408", "RceRedwarMonthCard",
       {1: ("int32", 1), 2: ("int32", 0)},
       "实测", "2026-10-10：{type:0,activetype:0} 查询 → "
               "{type:1,activetype:0} 领取。勋章 +300，天数 75→74",
       prelude=[("0408", {1: ("int32", 0), 2: ("int32", 0)})],
       gate=Gate("RseRedwarMonthCard", "threeMonthCardGetAwd", index=0,
                 claimed_flag=True, count_field="threeMonthCardCnt"),
       report=("threeMonthCardGetAwd", "threeMonthCardCnt")),

    _t("VIP月卡", "VIP月卡", "0408", "RceRedwarMonthCard",
       {1: ("int32", 1), 2: ("int32", 4)},
       "实测", "2026-10-10：{type:1,activetype:4} 领取。"
               "道具 21633 +1，天数 15→14",
       prelude=[("0408", {1: ("int32", 0), 2: ("int32", 0)})],
       gate=Gate("RseRedwarMonthCard", "threeMonthCardGetAwd", index=1,
                 claimed_flag=True, count_field="threeMonthCardCnt"),
       report=("threeMonthCardGetAwd", "threeMonthCardCnt")),

    _t("福利月卡", "福利月卡", "0408", "RceRedwarMonthCard",
       {1: ("int32", 1), 2: ("int32", 2)},
       "实测", "2026-10-10：{type:1,activetype:2} 领取。"
               "道具 10061、10095 各 +1，天数 15→14",
       prelude=[("0408", {1: ("int32", 0), 2: ("int32", 0)})],
       gate=Gate("RseRedwarMonthCard", "threeMonthCardGetAwd", index=2,
                 claimed_flag=True, count_field="threeMonthCardCnt"),
       report=("threeMonthCardGetAwd", "threeMonthCardCnt")),

    # ---- 有免费次数的：必须带 guard，免费用完就停 ----
    #
    # 截图实测各模块的免费额度与付费变体：
    #   开采石油   免费 3/3、1/1        第三档 500 勋章
    #   金属冶炼   免费 3/3、1/1        高级   500 勋章
    #   特工派遣   免费 3/3、1/1        高级   需紫色特工令
    #   配件探索   免费 1/1             10次 100 勋章、50次 500 勋章
    #   军备制造   免费 3/3             10次 300 勋章、50次 1500 勋章
    #   矿区探索   低/中级 刷新 5/5     高级   100 勋章
    #   征战世界   免费重征 2/2         付费重征 1/1
    # 一律只做免费档，且 guard 读到剩余次数 >0 才发。

    _t("英雄开采", "英雄中心·开采石油", "0402", "RceHeroVisit",
       {3: ("int32", 1), 4: ("int32", 0)},
       "实测", "实测顺序：RceHeroOpen{type:0} 开面板 → RceHeroVisit{free:1,type:0}。"
               "8/10 抓包：RseHeroOpen.freeVisitCnt=[低级,中级,高级]=[3,1,0]，"
               "三次免费后依次 [2,1,0]→[1,1,0]→[0,1,0]。"
               "第四次客户端改发 {free:0,credit:20} 走付费档 —— 我们只做第 0 档免费",
       max_per_day=5,
       prelude=[("0400", {3: ("int32", 0)})],
       gate=Gate("RseHeroOpen", "freeVisitCnt"),
       tiers=Tiers(4, [0, 1, 2])),

    _t("将领冶炼", "将领·金属冶炼", "0450", "RceAdmiralVisit",
       {3: ("int32", 1), 4: ("int32", 0)},
       "实测", "实测顺序：RceAdmiralOpen{type:0} → RceAdmiralVisit{free:1,type:0}",
       max_per_day=5,
       prelude=[("044e", {3: ("int32", 0)})],
       gate=Gate("RseAdmiralOpen", "freeVisitCnt"),
       tiers=Tiers(4, [0, 1, 2])),

    # 2026-10-10 抓包。type=1 只查询。type=11 是拨款，只写 type，
    # 一次把一部分铸造兑成代币（42→20，代币 +22），拨到 0 为止。
    _t("将领授勋", "将领·授勋拨款", "04c1", "RceMedalHonorOpt",
       {}, "实测",
       "2026-10-10：RceAdmiralOpen{type:0} → "
       "{type:1,nCreditType:0,bUseItem:false} 查询 → {type:11} 拨款。"
       "nCastNum>0 才发。不带点券、不用道具。回包看 nType 和 nResult",
       runner=lambda rec, sock, config: daily_medal(rec, sock, config)),

    # 将领和参谋是两份独立的技能书，各领各的。原先是一个任务 max_per_day=2，
    # 但 fields 写死 ActiveType=0，跑第二次只是把将领那份又领一遍。
    # 8/10 抓包实测客户端确实发了两组：{0,0}→{1,0} 和 {0,1}→{1,1}。
    # 2026-10-04 抓包：type=75 的 id 是建筑类型 56 的英雄中心，不是 10049。
    # 一次把已开的空槽都放上等级最高、没在培养、没死、没满 80 级的英雄。
    # 开几个槽由 VIP 决定。不发勋章，也不发培养书（那是 type=78）。
    _t("英雄培养", "英雄中心·培养（8 小时一轮）", "0414", "RceBuildingModify",
       {}, "实测",
       "2026-10-04 抓包：先开英雄面板，再按槽发 type=75。"
       "建筑取类型 56。金属 1 万、石油 5 千，不耗勋章。",
       max_per_day=3,
       runner=lambda rec, sock, config: daily_train(rec, sock, config)),

    # 技能书不是"一天一次"，是**每 24 小时一次**，而且以前既没闸门也没冷却，
    # 每轮都照发。2026-08-13 03:13 帧日志实测：
    #   查询 {OptType:0,ActiveType:0} → ret=0, field4=[
    #        {field1:1, field2:0, field3:1786609115},   ← 免费档：剩 0 次
    #        {field1:2, field2:3, field3:0},
    #        {field1:3, field2:0, field3:0}]
    #   领取 {OptType:1,ActiveType:0} → **ret=1**（被拒，当时还在冷却里）
    # field3 是"下次可用时刻"（unix 秒），实测正好是上次领取 + 24 小时；
    # 将领和参谋各有各的计时（两者相差 4 秒，正是昨天两次领取的间隔）。
    # 所以闸门读免费档的剩余次数，冷却读免费档的下次可用时刻，都按 field1==1
    # 选元素而不是按下标。
    _t("技能书将领", "将领·免费技能书", "048a", "RceBookCollection",
       {1: ("int32", 1), 2: ("int32", 0)},
       "实测", "实测 {OptType:0,ActiveType:0} 查询 → {OptType:1,ActiveType:0} 领取。"
               "闸门读 field4 里 field1==1（免费档）的 field2 剩余次数，"
               "冷却读同一元素的 field3（上次领取+24 小时）",
       prelude=[("048a", {1: ("int32", 0), 2: ("int32", 0)})],
       gate=Gate("RseBookCollection", "field4[field1=1].field2"),
       cooldown_until="field4[field1=1].field3"),

    _t("技能书参谋", "参谋·免费技能书", "048a", "RceBookCollection",
       {1: ("int32", 1), 2: ("int32", 1)},
       "实测", "实测 {OptType:0,ActiveType:1} 查询 → {OptType:1,ActiveType:1} 领取。"
               "闸门与冷却同「技能书将领」，但参谋这一档自己单独计时",
       prelude=[("048a", {1: ("int32", 0), 2: ("int32", 1)})],
       gate=Gate("RseBookCollection", "field4[field1=1].field2"),
       cooldown_until="field4[field1=1].field3"),

    # ⚠️ 只做一次。曾经放开到 3 次、靠"服务器拒绝再收手"，那是错的：
    # 读不到剩余次数就等于不知道还免不免费，撞过头就开始扣勋章。
    # 规矩是**先查询、没次数就不做**，查不到次数就只做一次。
    # RseAdviserOpt 有 nVistAdvCnt 字段疑似次数，但实测响应里没带，暂不敢用。
    # 参谋分两档：noptType 11=普通、12=高级，各有独立的免费次数。
    # **不是每天都能做** —— 8/12 抓包实测高级用掉后，下次可用时刻推到了
    # 2.6 天以后。所以绝不能按"一天一次"发，必须读次数。
    #   sVisitData.field4 = [普通剩余, 高级剩余]   实测 [0,1] → 用掉高级 → [0,0]
    #   sVisitData.field5 = [普通下次可用时刻, 高级下次可用时刻]（unix 秒）
    # 用券版是再带一个 nitemID 字段，我们永远不发，所以花不掉券。
    _t("参谋操作", "参谋·免费招募（普通+高级）", "04da", "RceAdviserOpt",
       {1: ("int32", 11)},
       "实测", "8/12 抓包：{noptType:1} 查询 → {noptType:11} 普通 / {noptType:12} 高级。"
               "闸门读 sVisitData.field4=[普通,高级] 剩余次数；"
               "field5 是下次可用时刻，实测高级间隔约 2.6 天，绝非每日",
       max_per_day=4,
       prelude=[("04da", {1: ("int32", 1)})],
       gate=Gate("RseAdviserOpt", "sVisitData.field4"),
       tiers=Tiers(1, [11, 12])),

    # 争霸战·领取上期排名奖励。2026-08-10 真客户端抓包（全过程-低级英雄招募）实证：
    #   开面板 RceArenaInfo{type:1} → RseArenaInfo{..., bLastRankGet:false,
    #                                 nRankSelf:633, nRankSelfLast:870,
    #                                 nCanFightTimes:10, nIntegralScore:14798}
    #   领奖   RceArenaOpt{type:3, indexself:0} → RseArenaOpt{type:3, result:1}
    #   领完再开面板 → bLastRankGet **翻成 true**，nIntegralScore 14798→15015
    # 所以闸门就是 bLastRankGet：false 才领，true 说明领过了。
    # ⚠️ RseArenaOpt 没有 ret，成败看 result（1=成功），见 RESULT_IS_STATUS。
    # 抓包里客户端还发过 RceArenaOpt{type:5}，那只是取/刷自己的排名（回包把
    # indexself 填成 633，633 就是排名本身），不是领奖，故不做。
    # RseArenaInfo.bScoreGiftGain（积分礼包是否已领）和 RceArenaOpt.nGainScoreGift
    # 看着就是"领积分"那一档，但抓包里客户端从没领过，**没有实测参数，先不做**。
    _t("争霸战领奖", "争霸战·领取上期排名奖励", "0469", "RceArenaOpt",
       {1: ("int32", 3), 4: ("int32", 0)},
       "实测", "8/10 真客户端抓包：RceArenaInfo{type:1} 开面板 → "
               "RceArenaOpt{type:3,indexself:0} 领奖，回包 result=1，"
               "随后 bLastRankGet 由 false 翻 true",
       prelude=[("0468", {1: ("int32", 1)})],
       gate=Gate("RseArenaInfo", "bLastRankGet", claimed_flag=True),
       report=("nRankSelf", "nRankSelfLast", "nIntegralScore",
               "nCanFightTimes", "nJoinPlayers", "bScoreGiftGain")),

    # 争霸战·挑战。形状不合流水线（每打一场都要重新取一次可挑战名单、
    # 从里面挑目标，名次还会跟着变），所以走 runner。排在领奖之后：
    # 先把上期的奖领了，再开打。
    # 延迟导入：arena 反过来要用 daily 的 _await_response/_nap，
    # 模块级 import 会成环。
    _t("争霸战挑战", "争霸战·把剩余次数打完", "0469", "RceArenaOpt",
       {}, "实测",
       "8/29 抓包实测三场：RceArenaInfo{type:1} 开面板读 nCanFightTimes → "
       "RceArenaRankInfo{type:2,nIndex,nCountry} 取可挑战名单 → "
       "RceArenaOpt{type:1,uidself,uidfight,indexself,indexfight,"
       "countryidself} 挑战。打到 nCanFightTimes 归零，不留剩余。"
       "优先打 NPC（短 uid）；已经是第一名时改打第 2～10 名。"
       "成败以面板变化为准",
       runner=lambda rec, sock, config: __import__(
           "tankstorm.arena", fromlist=["daily_challenge"]
       ).daily_challenge(rec, sock, config)),

    # 锦鲤心愿宝箱（限时活动）。8/30 抓包实测：
    #   {optype:0, activetype:1} 查询 → ngetdailyawd=false
    #   {optype:2, activetype:1} 领取 → ngetdailyawd=true
    # 消息名叫 RceDoubleElevenOfficer（双十一军官），游戏把同一个 opcode 复用给
    # 各种限时活动，靠 activetype 区分；activetype=1 就是锦鲤心愿这一档。
    # ⚠️ 回包里**既没有 ret 也没有 result**，judge() 找不到状态码会直接判成功。
    # 所以用 success_flag 盯住 ngetdailyawd —— 它翻成 true 才是真领到了。
    # 真客户端只发 optype(1) 和 activetype(4)，id(2)/recday(3) 一个都不发，照抄。
    # 限时活动，会不定期上下线（七天乐 08-29 下线、09-02 又回来了，这一族都这样）。
    # 下线期间发出去只是超时跳过，不影响别的任务，所以默认开着。
    _t("锦鲤心愿", "限时活动·领锦鲤心愿宝箱", "04af", "RceDoubleElevenOfficer",
       {1: ("int32", 2), 4: ("int32", 1)},
       "实测", "8/30 抓包实测：optype:0 查询 → optype:2 领取，"
               "ngetdailyawd 由 false 翻 true。不消耗任何资源",
       prelude=[("04af", {1: ("int32", 0), 4: ("int32", 1)})],
       gate=Gate("RseDoubleElevenOfficer", "ngetdailyawd", claimed_flag=True),
       success_flag="ngetdailyawd",
       report=("ngetdailyawd", "ncangetCnt")),

    # 功勋商城补支援兵。形状不合流水线（要先读背包库存、算差额、还要在买完之后
    # 核对勋章有没有被扣），所以走 runner。**默认关闭**，见 tankstorm/shop.py。
    _t("补支援兵", "功勋商城·补摩多军团支援兵", "041f", "RcePurchase",
       {}, "实测",
       "8/30 抓包实测：RcePurchase{credit:100n, type:SHOP, shopID:1199, "
       "credittype:2, buynum:n} → 背包 10118 增加 n 个、功勋减少 100n、"
       "勋章分文未动。这是全项目唯一会主动花钱的请求，默认关闭",
       runner=lambda rec, sock, config: __import__(
           "tankstorm.shop", fromlist=["daily_restock"]
       ).daily_restock(rec, sock, config)),

    # 国战：世界地图里攻击摩多军团。形状不合流水线（要先召唤支援兵、再从服务端
    # 推来的 RseCountryUserLst 里读出目标 ID 才能打），所以走 runner。
    # 延迟导入：country_war 反过来要用 daily 的 _await_response/_nap，
    # 模块级 import 会成环。
    _t("国战攻击", "国战·攻击摩多军团 10 次", "0463", "RceCountryOpt",
       {}, "实测",
       "8/29 抓包实测：type:3 开驻地面板 → type:45 召唤支援兵 → "
       "type:14 普通攻击（每次 5 点行动力、dayatktimes+1）。"
       "五种请求已与真客户端逐字段比对一致",
       runner=lambda rec, sock, config: __import__(
           "tankstorm.country_war", fromlist=["daily_attack"]
       ).daily_attack(rec, sock, config)),

    _t("特工派遣", "远程火炮·特工派遣", "04d6", "RceTrenchMortarOpt",
       {1: ("int32", 1), 2: ("int32", 1001)},
       "实测", "实测 optType:0 查询 → optType:1 + subType 1001/1002/1003 三档。"
               "查询响应 RseTrenchMortarOpt.freeVisitCnt 给出各档剩余免费次数",
       max_per_day=5,
       prelude=[("04d6", {1: ("int32", 0)})],
       gate=Gate("RseTrenchMortarOpt", "freeVisitCnt"),
       tiers=Tiers(2, [1001, 1002, 1003])),

    # 8/10 抓包：客户端把 7 个字段全都显式写了（除 sceneID 外一律 0），
    # 不是只发 sceneID。protobuf 里"显式写 0"和"不写"在服务端是
    # hasX=true 和 false 的区别，既然不要钱就照抄客户端。
    _t("配件探索", "配件中心·基地探索", "043a", "RceWPCExplore",
       {1: ("int32", 10001), 2: ("int32", 0), 3: ("int32", 0),
        4: ("int32", 0), 5: ("int32", 0), 6: ("int32", 0), 7: ("int32", 0)},
       "实测", "实测 RceWPCBaseOpen{type:1} 开面板 → RceWPCExplore{sceneID:10001}。"
               "useItemID/useItemCnt 是库存上报不是消耗，此处一律不发。"
               "开面板响应 RseWPCBaseOpen.leftFreeCnt 才是剩余免费次数，"
               "leftTime 是冷却秒数（此前把它当次数用过）",
       max_per_day=5,
       prelude=[("0437", {1: ("int32", 1)})],
       gate=Gate("RseWPCBaseOpen", "leftFreeCnt"),
       tiers=Tiers(1, [10001, 10002, 10003])),

    _t("军备制造", "军备研究·制造", "04e1", "RceJunBeiOpt",
       {1: ("int32", 22), 5: ("int32", 1)},
       "实测", "8/10+8/12 抓包：type:21 → type:0 → type:21 三步前置，"
               "再 {type:22, nExlType:1} 普通 / nExlType:4 自动化制造厂（高级）。"
               "nExlType 是 5 号字段 —— 此前误写成 2 号(nJunBeiID)。"
               "闸门读 field14[].field1=[普通剩余, 高级剩余]，"
               "field14[].field2 是下次可用时刻；高级实测用掉后推到 0.9 天以后，"
               "同样不是每天都能做。用券版再带 nItemID，我们不发",
       max_per_day=4,
       prelude=[("04e1", {1: ("int32", 21)}),
                ("04e1", {1: ("int32", 0)}),
                ("04e1", {1: ("int32", 21)})],
       gate=Gate("RseJunBeiOpt", "field14[].field1"),
       tiers=Tiers(5, [1, 4])),

    # 开面板响应里的 skilltraintimes 就是剩余训练次数（实测 5→4→…→0）。
    # 次数用完后服务器直接回 ret=11 拒绝，**不会扣费**（要加次数得自己去买），
    # 所以这里本来就是安全的；加闸门只是别再发那两个注定失败的包。
    _t("战略训练", "战争学院·战略技能训练", "04a5", "RceWarCollegeOpt",
       {1: ("int32", 4), 2: ("int32", 0), 3: ("int32", 1)},
       "实测", "8/10 抓包：{type:1} 开面板 → {type:4,trainskilltype:1} 训练。"
               "开面板响应 skilltraintimes = 剩余次数；用完后 ret=11 拒绝，不扣费",
       max_per_day=10,
       prelude=[("04a5", {1: ("int32", 1), 2: ("int32", 0),
                          3: ("int32", 0)})],
       gate=Gate("RseWarCollegeOpt", "skilltraintimes")),

    # 占领是真正拿收益的那一步。有空场就占空场；没有就打这次查到的场。
    # tokenNum 是当天剩余占领次数（实测 3，占一次变 2），正好当闸门。
    # 形状不合"挑一个就发一包"：没有空位时要换目标接着打，所以走 runner。
    _t("军事演习", "战争学院·军事演习（占场地）", "04a7", "RceWarGameOpt",
       {}, "实测",
       "8/10 抓包：{type:1} 查询（分页推送，bLastMsg 为最后一页）→ "
       "{type:2,siteID} 占领空场。空场 = 条目里没有 field6(占领者)。"
       "没有空位时按这次查到的场地挨个打，请求再带上占领者 uid，占住一场就停。"
       "不发 forceFlag。bOccupySite=true 才算占住，siteEndTime 是结束时刻",
       max_per_day=3,
       gate=Gate("RseWarGameOpt", "tokenNum"),
       cooldown_until="siteEndTime",
       runner=lambda rec, sock, config: _run_wargame(rec, sock, config)),

    # 探索只是找矿，占下来才有产出。没有无主矿就打这次探到的有主矿。
    _t("矿区争夺", "矿区争夺·探索并占矿", "049a", "RceResourceOpt",
       {}, "实测",
       "8/10 抓包：{type:1} 查询 → {type:2,searchType:1} 探索 → "
       "{type:3,resourceID} 占无主矿。没有无主矿时按这次探到的矿挨个打，"
       "再带上占领者 uid，占住一个就停。searchTimes 是剩余搜索次数。"
       "resourceEndTime 在占矿回包里",
       max_per_day=5,
       gate=Gate("RseResourceOpt", "searchTimes"),
       cooldown_until="resourceEndTime",
       runner=lambda rec, sock, config: _run_mine(rec, sock, config)),

    # 开打和命令行 --pve 同一套：type=7 打当前关，一个数字就打到这一关。
    # 已经到了或超过终点时，每免费重开一次就从第 1 关再打到终点。一天最多 2 次。
    _t("征战世界", "征战世界·重开征战（推进活跃度）", "045b", "RcePVEFightOpt",
       {},
       "实测", "和 --pve 一样发 type=7 打关。没到终点就从当前关打到终点，不重开。"
               "到了或超过终点，type=2 免费重开一次，再从第 1 关打到终点。"
               "打到终点后还有免费次数，再重开一次再打。这一轮没打过就停。"
               "第三次、第4次重开后也从第 1 关打到终点，打完才算做成。"
               "留空只打当前关。8/10 抓包：type=2 响应 result=0，关卡回到第 1 关",
       max_per_day=2,
       runner=lambda rec, sock, config: _run_campaign_task(rec, sock, config),
       counts_itself=True),

    # 2026-10-04 抓包：type:10/11 已经不是这条链路，接着发会 ret=13。
    # 现在是按今日战功领 1..7 档，每档一包 type:26。形状不合流水线，走 runner。
    _t("国家宝箱", "国家·按战功领宝箱", "0463", "RceCountryOpt",
       {}, "实测",
       "2026-10-04 抓包：RceCountryOpen{} → {type:15} 读今日战功和领取标记 "
       "→ {type:26, dailyGiftId:N} 逐档领。旧的 type:10/11 会 ret=13",
       runner=lambda rec, sock, config: __import__(
           "tankstorm.country_war", fromlist=["daily_chest"]
       ).daily_chest(rec, sock, config)),

    # 参加公会战。2026-08-30 抓包定案：**type:70 就是"参加"**，判据是回包里的
    # userGuild.field17.field1（上次参加时刻）被刷新到"刚刚"。
    #
    # 这一项欠了很久，之前两次都判错，原因值得记下来：type:70 的回包顶层只有
    # type/ret/userGuild，看着就是"查看自己的公会"，于是被归进浏览类放过了。
    # 真正的状态变化埋在两层嵌套加一个通用字段名里。8/30 抓包按 pcap 包时间还原：
    #   type:73 发于 …602.4 → 回包 field17.field1 = 1787556135（六天前的旧值）
    #   type:80 发于 …603.5 → 回包 还是 1787556135
    #   type:70 发于 …607.7 → 回包 **1788068608**，同一秒
    # 前后差 0.3 秒，而它前面两条读到的都还是旧值 —— 只可能是 type:70 写的。
    # 公会战**一天一场**（用户确认），所以按自然日限流：今天参加过就不再发。
    #
    # 形状不合流水线（闸门要同时看 dayHasPK 和那个时间戳是不是今天），走 runner。
    _t("公会战参加", "公会战·参加", "0479", "RceGuildOpt",
       {}, "实测",
       "8/30 抓包实测：type:73 读 dayHasPK 和上次参加时刻 → type:70 参加。"
       "回包里 userGuild.field17.field1 被刷新成请求发出的同一秒。"
       "公会战一天一场，故按自然日限流",
       runner=lambda rec, sock, config: __import__(
           "tankstorm.guild", fromlist=["daily_join"]
       ).daily_join(rec, sock, config)),

    # 顺序按抓包来。抓包里每个 RceGuildOpt 都带着这一串 0，照抄。
    # 这一项只打开战报面板（type:14 回包是 userGuild + log），不参战、不领奖。
    _t("公会战", "公会战·看战报", "0479", "RceGuildOpt",
       {2: ("int32", 14), 4: ("int32", 0), 13: ("int32", 0), 17: ("int32", 0),
        18: ("int32", 0), 22: ("int32", 0), 23: ("int32", 0)},
       "实测", "8/10 抓包：type:0 → type:2 → type:73 → type:14。"
               "type:73 的回包带 dayHasPK＝今天有没有公会战活动。"
               "这一项只是看战报；「参加」是单独的『公会战参加』任务（type:70）",
       prelude=[("0479", {2: ("int32", 0), 4: ("int32", 0), 13: ("int32", 0),
                          17: ("int32", 0), 18: ("int32", 0),
                          22: ("int32", 0), 23: ("int32", 0)}),
                ("0479", {2: ("int32", 2), 4: ("int32", 0), 13: ("int32", 0),
                          17: ("int32", 0), 18: ("int32", 0),
                          22: ("int32", 0), 23: ("int32", 0)}),
                ("0479", {2: ("int32", 73), 4: ("int32", 0), 13: ("int32", 0),
                          17: ("int32", 0), 18: ("int32", 0),
                          22: ("int32", 0), 23: ("int32", 0)})],
       report=("dayHasPK", "lastBtlRank", "curSesionBtlOver", "bRankGet")),

    # 2026-10-04 抓包是一键：金属石油、功勋、军令各捐到当天上限，不是只发一次。
    _t("公会捐献", "公会·捐献", "0479", "RceGuildOpt",
       {}, "实测",
       "2026-10-04 抓包：type:0 读今日次数 → type:16 按档捐满。"
       "金属石油一天 3 次，功勋 6 次，军令 6 次，合计 15。"
       "界面按已经捐成的次数计。不捐勋章。",
       max_per_day=15,
       counts_itself=True,
       runner=lambda rec, sock, config: __import__(
           "tankstorm.guild", fromlist=["daily_donate"]
       ).daily_donate(rec, sock, config)),

    # ---- 必须最后执行 ----
    _t("周任务", "周任务领奖", "04de", "RceWeekQuestOpt",
       {1: ("int32", 0), 2: ("int32", 0)}, "待确认", "需实测"),

    # 必须最后执行：前面每做一项，活跃度就涨一截，先领就少领。
    # 实测活跃度 64 时能领 10/30/50 三档，80/100 没达标领不了。
    _t("每日任务", "每日任务·按活跃度领奖", "043d", "RceDailyTask",
       {5: ("int32", FromResponse("RseDailyTask", _eligible_gift_tier,
                                  "取一个达标且未领的档位", fresh=False))},
       "实测", "8/10 抓包：客户端发 {giftID:10} / {giftID:30} / {giftID:50}，"
               "giftID 是 5 号字段（此前写的 getGift/taskId 是错的）。"
               "服务器持续推 RseDailyTask，dailyTask.field2 是当前活跃度，"
               "getGift=[{档位, 是否已领}]。一轮把所有达标档位领完",
       followup=Followup("043d", _next_daily_gift, max_rounds=5,
                         desc="继续领剩下达标的档位")),
]

# 白名单：动作 opcode 和前置 opcode 都要在内 —— 前置请求同样是真实发出的包，
# 漏掉的话等于给它开了后门，绕过白名单检查。
ALLOWED_OPCODES = ({t.opcode for t in TASKS}
                   | {op for t in TASKS for op, _ in t.prelude})


def ordered_tasks():
    """按执行顺序返回任务：普通任务在前，ORDER_LAST 里的排到最后。"""
    head = [t for t in TASKS if t.key not in ORDER_LAST]
    tail = [t for t in TASKS if t.key in ORDER_LAST]
    # 尾部内部再排：周任务 → 每日任务（每日任务绝对最后）
    tail.sort(key=lambda t: 0 if t.key == "周任务" else 1)
    return head + tail


# 免费两次走「征战世界」。这两项默认关，打开后跑一轮再做对应的那一次。
# 重开之后要从第 1 关打到终点，打完才算做成；只重开不算完成。
CAMPAIGN_EXTRAS = (
    ("征战第三次", "第三次征战"),
    ("征战第4次", "第4次征战"),
)


def switch_keys():
    """页面上可以保存的开关。普通任务，再加上第三次、第4次征战。"""
    keys = [task.key for task in ordered_tasks()]
    for key, _name in CAMPAIGN_EXTRAS:
        if key not in keys:
            keys.append(key)
    return keys


# ---------------------------------------------------------------- 每日状态

def state_path_for_qq(uin: str) -> str:
    """每个攻打号一份今日次数。命令行不带号时仍用原来的 daily-state.json。"""
    digits = "".join(ch for ch in str(uin or "") if ch.isdigit())
    if not digits:
        return STATE_FILE
    return os.path.join(LOG_DIR, f"daily-state-{digits}.json")


def _state_path() -> str:
    return getattr(_state_local, "path", None) or STATE_FILE


class using_state:
    """这一线程读写指定的今日次数文件。网页按攻打号分开，互不影响。"""

    def __init__(self, path: str):
        self.path = path
        self.prev = None

    def __enter__(self):
        self.prev = getattr(_state_local, "path", None)
        _state_local.path = self.path
        return self.path

    def __exit__(self, *_exc):
        if self.prev is None:
            if hasattr(_state_local, "path"):
                del _state_local.path
        else:
            _state_local.path = self.prev
        return False


def _load_state(path=None):
    path = path or _state_path()
    try:
        with open(path, encoding="utf-8") as f:
            st = json.load(f)
    except (OSError, ValueError):
        st = {}
    if st.get("date") != date.today().isoformat():
        st = {"date": date.today().isoformat(), "done": {}, "last": {}}
    return st


def _save_state(st, path=None):
    path = path or _state_path()
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
    except OSError as exc:
        log.debug("每日状态保存失败(忽略): %s", exc)


def task_board(uin: str, switches: dict) -> list:
    """页面上的今日进度。不连游戏。没绑攻打号时次数显示 0。"""
    switches = switches if isinstance(switches, dict) else {}
    if str(uin or "").strip():
        st = _load_state(state_path_for_qq(uin))
    else:
        st = {"done": {}}
    done = st.get("done") or {}
    rows = []
    for task in ordered_tasks():
        try:
            count = int(done.get(task.key) or 0)
        except (TypeError, ValueError):
            count = 0
        name = task.name
        if task.key in HELD_TASKS:
            name = name + "（暂不执行）"
        rows.append({
            "key": task.key,
            "name": name,
            "on": False if task.key in HELD_TASKS else bool(switches.get(task.key)),
            "done": count,
            "max": int(task.max_per_day or 1),
        })
    for key, name in CAMPAIGN_EXTRAS:
        try:
            count = int(done.get(key) or 0)
        except (TypeError, ValueError):
            count = 0
        rows.append({
            "key": key,
            "name": name,
            "on": bool(switches.get(key)),
            "done": count,
            "max": 1,
            "extra": True,
        })
    return rows


# ---------------------------------------------------------------- 执行

_SELECT = re.compile(r"^([A-Za-z_]\w*)\[(\w+)=(-?\d+)\]$")


def _read_path(data, path):
    """按路径从响应里取值，支持嵌套、"从每个元素里挑一个字段"和"按标记选元素"。

    形如：
        "freeVisitCnt"              顶层字段
        "sVisitData.field4"         嵌套一层（参谋的 [低级剩余, 高级剩余]）
        "field14[].field1"          列表里逐个取 field1（军备的 [{剩余,冷却}, …]）
        "field4[field1=1].field2"   列表里挑 field1==1 的那个元素，再取 field2

    最后一种是给技能书用的：RseBookCollection.field4 是
    [{field1:档位, field2:剩余, field3:下次可用}, …]，免费档是 field1==1。
    **按标记挑而不是按下标挑** —— 下标会随服务端调整档位顺序而错位，
    而错位在这个项目里已经犯过一次（schema 字段名按位置对齐，错了 198 个消息）。

    取不到返回 None。
    """
    cur = data
    for part in path.split("."):
        sel = _SELECT.match(part)
        if sel:
            name, key, val = sel.group(1), sel.group(2), int(sel.group(3))
            if not isinstance(cur, dict):
                return None
            items = cur.get(name)
            if items is None:
                return None
            if not isinstance(items, list):
                items = [items]
            cur = next((e for e in items
                        if isinstance(e, dict) and e.get(key) == val), None)
            if cur is None:
                return None
            continue
        pluck = part.endswith("[]")
        if pluck:
            part = part[:-2]
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
        if cur is None:
            return None
        if pluck:
            if not isinstance(cur, list):
                cur = [cur]
            return cur                     # 下一段负责从每个元素里挑
    return cur


def read_my_country(rec):
    """读"我是哪个国家"。返回 int；读不到返回 None。

    国战和争霸战的请求都要带这个值（争霸战的名次还是**本国内部**排名），
    以前写死在 config 里是 3 —— 那只是这个号是英国而已，换个号就错。

    两个来源都由服务端在**登录时主动推来**，不用额外发请求：

      · `RseFightSimpInfo.countryid` —— schema 解出了真名，首选
      · `RseLoad.countryData.field5` —— 登录响应里的同一份 countryData，兜底

    可信度的旁证：`RseLoad.countryData` 和国战面板 `RseCountryOpt.countryData`
    是同一个结构，country_war 早就验过的 field6(当前城市 3201)、field13(行动力)、
    field17(累计战功) 在这里全部对得上，field5 就在它们中间。
    2026-08-29 和 08-30 两份抓包里两个来源都等于 3，与玩家所述"我是英国"一致。

    读不到就返回 None，由调用方停手 —— 猜一个国家 ID 发出去，轻则请求无效，
    重则打到别的国家头上。
    """
    def _pick(msg, path):
        got = rec.latest.get(msg) if rec else None
        if not got or not isinstance(got[1], dict):
            return None
        v = _read_path(got[1], path)
        return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else None

    named = _pick("RseFightSimpInfo", "countryid")
    backup = _pick("RseLoad", "countryData.field5")
    if named and backup and named != backup:
        # 两个来源打架就以带名字的那个为准，但一定要吼出来 —— 说明其中一个
        # 字段的含义理解错了，不该悄悄用下去。
        log.warning("国家ID 两个来源不一致：RseFightSimpInfo.countryid=%s，"
                    "RseLoad.countryData.field5=%s，取前者", named, backup)
    return named or backup


def _read_counts(data, path):
    """取"每档剩余次数"。返回 int、list[int] 或 None。"""
    if "[]." in path:
        head, leaf = path.split("[].", 1)
        items = _read_path(data, head + "[]")
        if not isinstance(items, list):
            return None
        out = []
        for e in items:
            v = e.get(leaf) if isinstance(e, dict) else None
            out.append(v if isinstance(v, int) and not isinstance(v, bool) else 0)
        return out or None
    return _read_path(data, path)


def _month_card_status(data, index):
    """这一张的剩余天数，以及今天还能不能领。读不到返回 None。"""
    if not isinstance(data, dict):
        return None
    counts = _aslist(data.get("threeMonthCardCnt"))
    flags = _aslist(data.get("threeMonthCardGetAwd"))
    if index >= len(counts) and index >= len(flags):
        return None
    left = counts[index] if index < len(counts) else None
    bit = flags[index] if index < len(flags) else None
    if not isinstance(left, int) or isinstance(left, bool) or left <= 0:
        return "没有剩余天数"
    if bit is True:
        return f"剩余 {left} 天，今日已领"
    if bit is False:
        return f"剩余 {left} 天，今日可领"
    return f"剩余 {left} 天"


_MONTH_CARDS = (("超值月卡", 0), ("VIP月卡", 1), ("福利月卡", 2))


def _check_gate(gate, data, tiers=None):
    """判断闸门。返回 (是否放行, 说明, 免费次数是否已归零, 该打第几档)。

    第三项用来区分"确定没次数了"和"读不到"：前者可以把今日次数打满，
    后者只是这一轮不做，不该消耗配额。
    第四项是本次要打的档位下标（不分档的任务返回 None）。
    """
    if data is None:
        return (False, f"{gate.timeout:.0f}s 内没收到 {gate.rse_msg}，"
                       "这一轮不做（宁可少做也不误扣券/勋章）", False, None)
    raw = _read_counts(data, gate.field)
    if raw is None:
        return (False, f"{gate.rse_msg} 里没有 {gate.field}，这一轮不做",
                False, None)
    whole = raw

    if gate.claimed_flag and isinstance(raw, (list, tuple)):
        # 月卡 threeMonthCardGetAwd 是三张各自的已领标记。布尔进下面的次数
        # 分支会被当成 0，三张都会被拦下。
        if gate.index >= len(raw):
            return (False, f"{gate.field}={list(raw)} 没有第 {gate.index} 张，"
                           "这一轮不做", False, None)
        bit = raw[gate.index]
        if not isinstance(bit, bool):
            return (False, f"{gate.field}[{gate.index}]={bit!r} 不是领取标记，"
                           "这一轮不做", False, None)
        if gate.count_field:
            text = _month_card_status(data, gate.index)
            if not text:
                return (False, f"{gate.field} 读不出这张月卡，这一轮不做",
                        False, None)
            if text.endswith("今日可领"):
                return (True, text, False, None)
            done_today = text.endswith("今日已领") or text == "没有剩余天数"
            return (False, text, done_today, None)
        if bit:
            return (False, f"已经领过了（{gate.field}[{gate.index}]=True）",
                    True, None)
        return (True, f"还没领（{gate.field}[{gate.index}]=False）",
                False, None)

    if isinstance(raw, (list, tuple)):
        nums = [x if isinstance(x, int) and not isinstance(x, bool) else 0
                for x in raw]
        if tiers:
            # 分档任务：从低到高找第一个还有次数的档，逐档领干净
            n = min(len(nums), len(tiers.values))
            for i in range(n):
                if nums[i] > 0:
                    return (True, f"第 {i} 档（{tiers.field}={tiers.values[i]}）"
                                  f"还有 {nums[i]} 次（{gate.field}={whole}）",
                            False, i)
            return (False, f"各档免费次数都已用完（{gate.field}={whole}）",
                    True, None)
        # 不分档但服务端给的是数组：只看约定的那一档
        if gate.index >= len(nums):
            return (False, f"{gate.field}={whole} 没有第 {gate.index} 档，"
                           "这一轮不做", False, None)
        cnt = nums[gate.index]
        if cnt <= 0:
            return (False, f"第 {gate.index} 档免费次数已用完 "
                           f"{gate.field}={whole}（再发就会扣券/勋章）", True, None)
        return (True, f"第 {gate.index} 档还有 {cnt} 次（{gate.field}={whole}）",
                False, None)

    if gate.claimed_flag:
        # 布尔型闸门："领过了吗"。False=还没领→放行，True=领过了→今天别再来。
        # 争霸战上期排名奖励就是这种：RseArenaInfo.bLastRankGet，
        # 2026-08-10 真客户端抓包里它在领奖前后由 false 翻 true。
        if not isinstance(raw, bool):
            return (False, f"{gate.field}={raw!r} 不是布尔标记，这一轮不做",
                    False, None)
        if raw:
            return (False, f"已经领过了（{gate.field}=True）", True, None)
        return True, f"还没领（{gate.field}=False）", False, None

    if isinstance(raw, bool) or not isinstance(raw, int):
        return (False, f"{gate.field}={raw!r} 不是次数，这一轮不做", False, None)
    if raw <= 0:
        return (False, f"免费次数已用完 {gate.field}={whole}（再发就会扣券/勋章）",
                True, None)
    return True, f"剩余 {raw} 次（{gate.field}={whole}）", False, None


def _check_safety(task, field_names, fields=None):
    """发送前的安全检查。返回 (是否放行, 原因)。

    fields 传的是**已经把占位符解析成真值**的字段表；不传就用任务表原始定义。
    """
    if task.opcode not in ALLOWED_OPCODES:
        return False, f"opcode {task.opcode} 不在白名单"
    for fno, (ftype, val) in (fields or task.fields).items():
        fname = field_names.get(fno, f"field{fno}")
        if (task.opcode, fname) in SAFE_FIELDS:
            continue
        if DANGER_FIELD.search(fname) and val not in (0, False, "", None):
            return False, f"危险字段 {fname}={val!r} 非零，拒发（防止消耗资产）"
    return True, ""


# 客户端 HeroConstant.HERO_CENTER_TYPE。getBuildingByType(56) 的 id 才是培养请求的 id。
# 2026-10-04 这号上类型 56 是 10130；10049 是类型 17，发过去 error=9。
_HERO_CENTER = 56
_TRAIN_METAL = 10000          # HeroBaseData.trainCostR1，表里 42 个英雄全是这个数
_TRAIN_OIL = 5000             # trainCostR2，同样全表一个价
_HERO_LEVEL_CAP = 80          # HERO_LEVEL_TOP_LIMIT，到了就不再培养


def _one(v):
    if isinstance(v, bool) or not isinstance(v, int):
        return None
    return v


def _rows(v):
    if isinstance(v, dict):
        return [v]
    if isinstance(v, list) and all(isinstance(x, dict) for x in v):
        return v
    return None


def _open_slots(level, endtime, now):
    """和 HeroTrainingPanel 的锁一致。过期或没有 VIP 只剩第 0 槽。

    VIP 3 开第 1 槽，VIP 5 开第 2 槽，VIP 正好 7 才开第 3 槽。
    """
    if level <= 0 or endtime < now:
        return 1
    n = 1
    if level >= 3:
        n = 2
    if level >= 5:
        n = 3
    if level == 7:
        n = 4
    return n


def _train_slots(panel):
    """upgradeherolst.field3 是槽位。field1 是英雄编号，0 或没写就是空的。
    field3 是剩余秒数。2026-10-04 开面板时四个槽都是空的。"""
    box = panel.get("upgradeherolst") if isinstance(panel, dict) else None
    raw = box.get("field3") if isinstance(box, dict) else None
    rows = _rows(raw)
    if not rows:
        return None
    out = []
    for row in rows:
        hero = row.get("field1", 0)
        left = row.get("field3", 0)
        hero = 0 if hero is None else _one(hero)
        left = 0 if left is None else _one(left)
        if hero is None or left is None or hero < 0 or left < 0:
            return None
        out.append((hero, left))
    return out


def _ready_heroes(panel, busy):
    """getReadyForTrainingHeroList：没死、不在槽里、没到 80 级。按等级从高到低。"""
    rows = _rows(panel.get("hero") if isinstance(panel, dict) else None)
    if not rows:
        return None
    if all(_one(row.get("field2")) is None for row in rows):
        return None
    ready = []
    for row in rows:
        hid = _one(row.get("field2"))
        level = _one(row.get("field3"))
        atk = row.get("field14")
        health = _one(atk.get("field4")) if isinstance(atk, dict) else None
        if hid is None or level is None or health is None or hid <= 0:
            continue
        if health <= 0 or hid in busy or level >= _HERO_LEVEL_CAP:
            continue
        ready.append((hid, level))
    ready.sort(key=lambda item: (-item[1], -item[0]))
    return ready


def daily_medal(rec, sock, config):
    """将领授勋拨款。2026-10-10 抓包：开将领面板后查 nCastNum，大于 0 才发 type=11。

    一次请求只兑掉一部分（实测 42→20，代币 +22），所以拨到 0 或不再减少为止。
    拨款包只写 type。不带点券、不用道具。回包步骤是 nType，nResult=0 才算成。
    """
    def _num(data, key):
        v = data.get(key) if isinstance(data, dict) else None
        if isinstance(v, bool) or not isinstance(v, int):
            return None
        return v

    before = rec.seq_mark() if rec else 0
    sender.send_frame(sock, "044e",
                      encode_message({3: ("int32", 0)}, omit_zero=False),
                      rec.rc4_c2s)
    _nap(0.4)
    sender.send_frame(
        sock, "04c1",
        encode_message({2: ("int32", 1), 5: ("int32", 0), 6: ("bool", False)},
                       omit_zero=False),
        rec.rc4_c2s)
    panel = _await_response(
        sock, rec, "RseMedalHonorOpt", before, 6.0,
        want=lambda d: isinstance(d, dict) and d.get("nType") == 1)
    left = _num(panel, "nCastNum")
    tokens = _num(panel, "nTokenNum")
    if panel is None or left is None:
        return False, "失败：未收到授勋面板，读不到铸造次数"
    if left <= 0:
        return True, "成功：没有可拨款的铸造次数"
    start = tokens
    rounds = 0
    while left > 0 and rounds < 20:
        raise_if_stopped()
        mark = rec.seq_mark() if rec else 0
        sender.send_frame(sock, "04c1",
                          encode_message({2: ("int32", 11)}),
                          rec.rc4_c2s)
        got = _await_response(
            sock, rec, "RseMedalHonorOpt", mark, 6.0,
            want=lambda d: isinstance(d, dict) and d.get("nType") == 11)
        result = _num(got, "nResult")
        now = _num(got, "nCastNum")
        now_tokens = _num(got, "nTokenNum")
        if got is None or result is None:
            if rounds:
                break
            return False, "失败：未收到拨款回包"
        if result != 0:
            if rounds:
                break
            return False, f"失败：拨款被拒 nResult={result}"
        if now is None or now >= left:
            break
        rounds += 1
        left = now
        if now_tokens is not None:
            tokens = now_tokens
        _nap(0.4)
    if rounds == 0:
        return False, "失败：铸造次数没有减少"
    gain = ""
    if isinstance(start, int) and isinstance(tokens, int):
        gain = f"，代币 {start}→{tokens}"
    tail = "铸造次数已用完" if left <= 0 else f"还剩铸造 {left} 次"
    return True, f"成功：拨款 {rounds} 次{gain}，{tail}"


def daily_train(rec, sock, config):
    """把英雄中心已开的空槽都放上能培养的英雄。

    返回 (是否记一次, 说明, 下次时刻或 None)。槽放满才给下次时刻。
    """
    load = (rec.latest.get("RseLoad") or (None, None))[1] if rec else None
    if not isinstance(load, dict):
        return False, "读不到基地数据，不培养"
    buildings = _rows(load.get("buildingdata"))
    bid = None
    if buildings:
        for b in buildings:
            if b.get("field5") == _HERO_CENTER:
                bid = _one(b.get("field4"))
                break
    if not bid or bid <= 0:
        return False, "读不到英雄中心，不培养"
    vip = load.get("vipData")
    level = _one(vip.get("field1")) if isinstance(vip, dict) else None
    endtime = _one(vip.get("field2")) if isinstance(vip, dict) else None
    if level is None or endtime is None:
        return False, "读不到 VIP，不培养（读不到就不做）"
    wallet = (rec.latest.get("RseUserInfo") or (None, None))[1]
    metal = _one(wallet.get("metal")) if isinstance(wallet, dict) else None
    oil = _one(wallet.get("oil")) if isinstance(wallet, dict) else None
    if metal is None or oil is None:
        return False, "读不到金属或石油，不培养（读不到就不做）"

    before = rec.seq_mark()
    sender.send_frame(sock, "0400",
                      encode_message({3: ("int32", 0)}, omit_zero=False),
                      rec.rc4_c2s)
    _nap(0.4)
    panel = _await_response(sock, rec, "RseHeroOpen", before, 6,
                            want=lambda d: d.get("type") == 0)
    slots = _train_slots(panel)
    if slots is None:
        return False, "读不到培养槽，不培养（读不到就不做）"
    opened = _open_slots(level, endtime, time.time())
    opened = min(opened, len(slots))
    busy = {hero for hero, _left in slots if hero}
    heroes = _ready_heroes(panel, busy)
    if heroes is None:
        return False, "读不到英雄，不培养（读不到就不做）"

    empty = [i for i in range(opened) if slots[i][0] == 0]
    soonest = min((left for hero, left in slots[:opened] if hero and left > 0),
                  default=0)

    def wait():
        return int(time.time() + soonest) if soonest else None

    if not empty:
        mins = max(1, soonest // 60) if soonest else 0
        return (False,
                f"已开的 {opened} 个槽都在培养，最早还要 {mins} 分钟，跳过",
                wait())
    if not heroes:
        return False, "没有能培养的英雄，跳过"

    done = []
    for index in empty:
        if metal < _TRAIN_METAL or oil < _TRAIN_OIL:
            why = "金属或石油不够"
            break
        if not heroes:
            why = "没有能培养的英雄"
            break
        hid, _level = heroes.pop(0)
        _nap(0.4)
        mark = rec.seq_mark()
        sender.send_frame(
            sock, "0414",
            encode_message({2: ("int32", bid), 3: ("int32", 75),
                            15: ("int32", hid), 16: ("int32", index)},
                           omit_zero=False),
            rec.rc4_c2s)
        got = _await_response(sock, rec, "RseBuildingModify", mark, 6,
                              want=lambda d: d.get("type") == 75)
        if not isinstance(got, dict):
            return False, f"槽 {index} 没有回包" + _trained(done)
        err = got.get("error")
        now = _train_slots(got)
        placed = now[index][0] if now and index < len(now) else None
        if err != 0 or placed != hid:
            return False, (f"槽 {index} 服务器返回 error={err}"
                           + _trained(done))
        metal -= _TRAIN_METAL
        oil -= _TRAIN_OIL
        done.append(f"槽{index}英雄{hid}")
        if now:
            slots = now
            soonest = min((left for hero, left in slots[:opened]
                           if hero and left > 0), default=soonest)
    else:
        why = ""
    text = "、".join(done)
    if why:
        head = f"培养了 {text}；" if text else ""
        return False, head + why + "，跳过", wait() if not _still_empty(slots, opened) else None
    return True, "成功：培养了 " + text, wait()


def _trained(done):
    return ("，已培养 " + "、".join(done)) if done else ""


def _still_empty(slots, opened):
    return any(slots[i][0] == 0 for i in range(min(opened, len(slots))))


def run(rec, sock, config: dict, schema=None, beat=None, on_fail=None) -> dict:
    """执行每日任务。rec 是 Recorder（提供 C→S 的 RC4），sock 是已登录的 socket。

    返回 {任务名: 结果字符串}。
    """
    # schema 默认用项目自带的那份。
    #
    # 2026-08-13 定位：两个调用点（socket_keepalive 的保活带跑和 --daily）
    # 都是 daily.run(rec, sock, config)，schema 一直是 None，于是
    # _field_names() 永远返回 {}，_echo_want() 拿不到字段名、造不出判据，
    # **所有任务都失去了"前置回包 vs 动作回包"的区分能力** —— 谁先到算谁。
    # 实盘后果：技能书发出 {OptType:1} 后 2 毫秒就"成功"，其实读到的是
    # 前置 {OptType:0} 的回包；真正的动作回包 4 秒后才到，ret=1（被拒）。
    # 公会战同样，type=14 读到的是前置 type=0 的回包。
    if schema is None:
        schema = _schema

    # 心跳回调挂在这一线程上，_nap()/_await_response() 沿路都会调它。
    # 跑完就摘掉，免得下一次调用还拿着上一个连接的 socket。
    install_beat(beat)
    prev_sock = bind_sock(sock)
    try:
        return _run(rec, sock, config, schema, on_fail)
    finally:
        install_beat(None)
        bind_sock(prev_sock)


def _campaign_goal(config):
    """页面或这一轮参数里填的终点。空的表示这一轮只做免费重开。"""
    raw = (config.get("征战") or {}).get("终点") if isinstance(config, dict) else None
    text = str(raw or "").strip()
    return text or None


def _campaign_interval(config):
    raw_gap = 1
    if isinstance(config, dict):
        raw_gap = (config.get("征战") or {}).get("间隔秒", 1)
    if raw_gap is None or raw_gap == "":
        raw_gap = 1
    return float(raw_gap)


def campaign_pushing() -> bool:
    """这一轮还在往终点打。第三次、第4次要等打到终点再做。"""
    return bool(getattr(_state_local, "campaign_pushing", False))


def current_campaign_round():
    """正在打的是第几次征战。没有就 None。"""
    try:
        n = int(getattr(_state_local, "campaign_round", None))
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def note_campaign_round(n) -> None:
    """记下正在打第几次。网页说明用。"""
    try:
        _state_local.campaign_round = int(n)
    except (TypeError, ValueError):
        if hasattr(_state_local, "campaign_round"):
            del _state_local.campaign_round


def set_campaign_progress(fn) -> None:
    """征战打关时回调当前关。网页用它改最近执行的说明。"""
    if fn is None:
        if hasattr(_state_local, "campaign_progress"):
            del _state_local.campaign_progress
        return
    _state_local.campaign_progress = fn


def note_campaign_stage(stage) -> None:
    """打到这一关。没有回调就不管。"""
    fn = getattr(_state_local, "campaign_progress", None)
    if fn is None or stage is None:
        return
    try:
        fn(int(stage))
    except (TypeError, ValueError):
        return
    except Exception:
        log.info("征战进度没写上页面", exc_info=True)


def campaign_round(rec, sock, stages, interval=1.0):
    """按 --pve 打征战。到了或超过终点时，每次免费重开后都从第 1 关再打到终点。"""
    from . import pve

    st = _load_state()
    try:
        already = int((st.get("done") or {}).get("征战世界") or 0)
    except (TypeError, ValueError):
        already = 0
    try:
        ok, why, total, pushing = pve.campaign(rec, sock, stages, already, interval)
    except Exception:
        _state_local.campaign_pushing = True
        raise
    st = _load_state()
    st.setdefault("done", {})["征战世界"] = int(total)
    _save_state(st)
    _state_local.campaign_pushing = bool(pushing)
    return ok, why


def _future_stamp(data, key):
    """结束时刻。只认未来 30 天以内的 unix 秒。"""
    if not isinstance(data, dict):
        return None
    ts = data.get(key)
    now = time.time()
    if isinstance(ts, bool) or not isinstance(ts, int):
        return None
    if now < ts < now + 30 * 86400:
        return ts
    return None


def _daily_wait(config):
    conf = (config or {}).get("每日任务", {}) or {}
    try:
        timeout = float(conf.get("响应等待秒", 6))
    except (TypeError, ValueError):
        timeout = 6.0
    try:
        gap = float(conf.get("间隔秒", 3))
    except (TypeError, ValueError):
        gap = 3.0
    return timeout, gap


def _task_of(key):
    return next(t for t in TASKS if t.key == key)


def _wargame_sites(pages):
    sites, seen = [], set()
    for data in pages:
        for blk in _aslist(data.get("field14") if isinstance(data, dict) else None):
            if not isinstance(blk, dict):
                continue
            for e in _aslist(blk.get("field2")):
                if not isinstance(e, dict):
                    continue
                sid = e.get("field1")
                if isinstance(sid, bool) or not isinstance(sid, int) or sid in seen:
                    continue
                seen.add(sid)
                sites.append(e)
    return sites


def _wargame_attempts(sites, me):
    """有空场只占那一个。没有就按探测顺序打有主的，自己的和停战中的跳过。"""
    free_id = _pick_free_wargame_site({"field14": {"field2": sites}})
    if isinstance(free_id, int) and not isinstance(free_id, bool):
        return [(f"占领空场 {free_id}",
                 {1: ("int32", 2), 2: ("int32", free_id)})]
    now = time.time()
    attempts = []
    for e in sites:
        uid = e.get("field6")
        if not isinstance(uid, str) or not uid or (me and uid == me):
            continue
        if e.get("field12") is True:
            continue
        truce = e.get("field13")
        if isinstance(truce, int) and not isinstance(truce, bool) and truce > now:
            continue
        sid = e["field1"]
        name = e.get("field7") if isinstance(e.get("field7"), str) and e.get("field7") else uid
        # 空场的实测包只有 type+siteID。有主的多带占领者 uid，不发 forceFlag。
        attempts.append((f"打下场 {sid}（{name}）",
                         {1: ("int32", 2), 2: ("int32", sid), 3: ("string", uid)}))
    return attempts


def _mine_attempts(data, me):
    """有无主矿只占那一个。没有就按探测顺序打有主的。"""
    free = _next_mine_to_occupy(data)
    if free:
        return [("占下探到的无主矿", free)]
    attempts = []
    for e in _aslist(data.get("field5") if isinstance(data, dict) else None):
        if not isinstance(e, dict):
            continue
        rid = e.get("field1")
        uid = e.get("field6")
        if isinstance(rid, bool) or not isinstance(rid, int):
            continue
        if not isinstance(uid, str) or not uid or (me and uid == me):
            continue
        name = e.get("field2") if isinstance(e.get("field2"), str) and e.get("field2") else uid
        attempts.append((f"打矿 {rid}（{name}）",
                         {1: ("int32", 3), 2: ("int32", rid), 4: ("string", uid)}))
    return attempts


def _contest(rec, sock, task, attempts, hold, left, left_key, timeout, gap):
    """按顺序打，占住就停。

    ret=1（要花钱）或没回包，整串都停。
    只是这一场被拒或没打赢，还有免费次数就换下一个。
    响应里带回剩余次数就改信那个数；没带回就每发一次减一，减到 0 不再发。
    矿区 left 为空：探到的几个都打完为止，但 ret=1 照样停。
    """
    names = _field_names(_schema, task.opcode)
    rse = _rse_name(task.msg)
    tried = 0
    saw_left = False
    for label, fields in attempts:
        if left is not None and left <= 0:
            return False, f"打了 {tried} 个都没占住，免费次数已用完", None
        safe, why = _check_safety(task, names, fields)
        if not safe:
            return False, f"安全检查拦截：{why}", None
        before = rec.seq_mark() if rec else 0
        desc = ", ".join(f"{names.get(k, k)}={v[1]!r}"
                         for k, v in sorted(fields.items()))
        try:
            sender.send_frame(sock, task.opcode,
                              encode_message(fields, omit_zero=False),
                              rec.rc4_c2s)
        except OSError:
            raise
        except Exception as exc:
            return False, f"发送失败：{exc}", None
        log.info("[%s] 已发送 %s {%s}", task.key, label, desc)
        data = _await_response(sock, rec, rse, before, timeout,
                               want=_echo_want(fields, names))
        judged, why, _stop = judge(rse, data, ignore_left=True)
        log.info("[%s] %s %s", task.key, "✅" if judged else "❌", why)
        tried += 1
        if isinstance(data, dict) and left_key:
            n = data.get(left_key)
            if isinstance(n, int) and not isinstance(n, bool) and n >= 0:
                left = n
                saw_left = True
            elif left is not None and not saw_left:
                left -= 1
        elif left is not None and not saw_left:
            left -= 1
        if judged:
            held = hold(data)
            if held:
                until = held if isinstance(held, int) else None
                text = f"{label}: {why}"
                if isinstance(until, int):
                    mins = (until - time.time()) / 60
                    text += f"；占用至 {mins:.0f} 分钟后"
                return True, text, until
            if left is not None and left <= 0:
                return False, f"打了 {tried} 个都没占住，免费次数已用完", None
            _nap(gap)
            continue
        ret = data.get("ret") if isinstance(data, dict) else None
        if data is None or ret == 1:
            return False, f"{label}: {why}", None
        if left is not None and left <= 0:
            return False, f"{label}: {why}；免费次数已用完，停下", None
        _nap(gap)
    if tried:
        return False, f"打了 {tried} 个都没占住", None
    return False, "没有可打的目标", None


def _send_op(sock, rec, task, fields):
    sender.send_frame(sock, task.opcode,
                      encode_message(fields, omit_zero=False), rec.rc4_c2s)


def _collect_wargame_pages(sock, rec, since, timeout):
    """把这次查询推来的场地页攒齐。bLastMsg 到了就停，超时就用已经拿到的。"""
    deadline = time.time() + timeout
    pages, seen = [], set()
    while time.time() < deadline:
        hist = (getattr(rec, "recent", {}) or {}).get("RseWarGameOpt") or []
        if not hist:
            got = (getattr(rec, "latest", {}) or {}).get("RseWarGameOpt")
            hist = [got] if got else []
        for seq, data in hist:
            if not isinstance(data, dict) or seq in seen or seq <= since:
                continue
            if data.get("type") not in (None, 1):
                continue
            if ("field14" not in data and "tokenNum" not in data
                    and data.get("bLastMsg") is not True):
                continue
            seen.add(seq)
            pages.append(data)
        if any(p.get("bLastMsg") is True for p in pages):
            break
        _beat()
        try:
            sock.settimeout(0.5)
            if not sock.recv(8192):
                break
        except TimeoutError:
            continue
        except OSError:
            break
    return pages


def _run_wargame(rec, sock, config):
    """有空场就占。没有就按这次查到的场地挨个打，占住就停。"""
    task = _task_of("军事演习")
    if rec is None or sock is None:
        return False, "没有连接，不打"
    timeout, gap = _daily_wait(config)
    since = rec.seq_mark()
    try:
        _send_op(sock, rec, task, {1: ("int32", 1)})
    except OSError:
        raise
    except Exception as exc:
        return False, f"发送失败：{exc}"
    log.info("[%s] 查询场地", task.key)
    pages = _collect_wargame_pages(sock, rec, since, timeout)
    if pages and not any(p.get("bLastMsg") is True for p in pages):
        log.info("[%s] 场地列表没等齐最后一页，按已收到的继续", task.key)
    panel = next((p for p in pages
                  if isinstance(p.get("tokenNum"), int)
                  and not isinstance(p.get("tokenNum"), bool)), None)
    for p in pages:
        if p.get("bOccupySite") is not True:
            continue
        ts = _future_stamp(p, "siteEndTime")
        if ts:
            mins = (ts - time.time()) / 60
            return False, f"已经占着场地，占用至 {mins:.0f} 分钟后", ts
        return False, "已经占着演习场，这一轮不打"
    passed, why, _exhausted, _tier = _check_gate(task.gate, panel)
    if not passed:
        return False, f"闸门拦截：{why}"
    log.info("[%s] 闸门放行：%s", task.key, why)
    sites = _wargame_sites(pages)
    me = str(getattr(rec, "uid", "") or "")
    attempts = _wargame_attempts(sites, me)
    if not attempts:
        n = len(sites)
        if n:
            return False, f"探测到 {n} 个演习场，没有空位，也没有能打的，跳过"
        return False, "没有探测到演习场，跳过"
    if attempts[0][0].startswith("打"):
        log.info("[%s] 没有空位，按探测顺序打 %d 个，占住就停",
                 task.key, len(attempts))
    left = panel.get("tokenNum") if isinstance(panel, dict) else None

    def hold(data):
        if not isinstance(data, dict) or data.get("bOccupySite") is not True:
            return None
        return _future_stamp(data, "siteEndTime") or True

    return _contest(rec, sock, task, attempts, hold, left, "tokenNum",
                    timeout, gap)


def _run_mine(rec, sock, config):
    """先探索。有无主矿就占。没有就按这次探到的矿挨个打，占住就停。"""
    task = _task_of("矿区争夺")
    if rec is None or sock is None:
        return False, "没有连接，不打"
    timeout, gap = _daily_wait(config)
    query = {1: ("int32", 1), 2: ("int32", 0), 3: ("int32", 0), 4: ("string", "")}
    before = rec.seq_mark()
    try:
        _send_op(sock, rec, task, query)
    except OSError:
        raise
    except Exception as exc:
        return False, f"发送失败：{exc}"
    log.info("[%s] 查询矿区", task.key)
    panel = _await_response(
        sock, rec, "RseResourceOpt", before, timeout,
        want=lambda d: (isinstance(d, dict)
                        and isinstance(d.get("searchTimes"), int)
                        and not isinstance(d.get("searchTimes"), bool)))
    ts = _future_stamp(panel, "resourceEndTime")
    if ts:
        mins = (ts - time.time()) / 60
        return False, f"已经占着矿，占用至 {mins:.0f} 分钟后", ts
    passed, why, _exhausted, _tier = _check_gate(task.gate, panel)
    if not passed:
        return False, f"闸门拦截：{why}"
    log.info("[%s] 闸门放行：%s", task.key, why)
    explore = {1: ("int32", 2), 2: ("int32", 0), 3: ("int32", 1), 4: ("string", "")}
    names = _field_names(_schema, task.opcode)
    safe, why = _check_safety(task, names, explore)
    if not safe:
        return False, f"安全检查拦截：{why}"
    before = rec.seq_mark()
    try:
        _send_op(sock, rec, task, explore)
    except OSError:
        raise
    except Exception as exc:
        return False, f"发送失败：{exc}"
    log.info("[%s] 已发送探索", task.key)
    data = _await_response(
        sock, rec, "RseResourceOpt", before, timeout,
        want=lambda d: isinstance(d, dict) and d.get("type") == 2 and "field5" in d,
        relaxed=lambda d: isinstance(d, dict) and d.get("type") == 2)
    judged, why, _stop = judge("RseResourceOpt", data, ignore_left=True)
    if not judged:
        return False, f"探索: {why}"
    ts = _future_stamp(data, "resourceEndTime")
    if ts:
        mins = (ts - time.time()) / 60
        return False, f"已经占着矿，占用至 {mins:.0f} 分钟后", ts
    me = str(getattr(rec, "uid", "") or "")
    attempts = _mine_attempts(data, me)
    if not attempts:
        n = len(_aslist(data.get("field5")))
        if n:
            return False, f"这次探到 {n} 个矿，没有无主的，也没有能打的，跳过"
        return False, "这次没探到矿，跳过"
    if attempts[0][0].startswith("打"):
        log.info("[%s] 没有无主矿，按探测顺序打 %d 个，占住就停",
                 task.key, len(attempts))

    def hold(resp):
        return _future_stamp(resp, "resourceEndTime")

    return _contest(rec, sock, task, attempts, hold, None, None, timeout, gap)


def _run_campaign_task(rec, sock, config):
    raw = _campaign_goal(config) or ""
    return campaign_round(rec, sock, raw, _campaign_interval(config))


def _run(rec, sock, config, schema, on_fail=None):
    _state_local.campaign_pushing = False
    if hasattr(_state_local, "campaign_round"):
        del _state_local.campaign_round
    conf = (config.get("每日任务", {}) or {})
    if not conf.get("启用", False):
        log.info("每日任务未启用（config.json 每日任务.启用=false）")
        return {}, {}

    switches = conf.get("任务", {})
    allow_unverified = conf.get("允许未实测参数", False)
    gap = float(conf.get("间隔秒", 3))

    resp_timeout = float(conf.get("响应等待秒", 6))
    st = _load_state()
    results, details = {}, {}

    log.info("=== 每日任务开始（实发）===")

    # 某一项失败、抛异常，都只记下这一项，接着做下一项。
    # 连接断了才停，后面的任务没有连接可发。
    for task in ordered_tasks():
        try:
            raise_if_stopped()
            if task.key in HELD_TASKS:
                results[task.key] = HELD_STATUS
                log.info("[%s] %s", task.key, results[task.key])
                continue
            if not switches.get(task.key, False):
                results[task.key] = "未开启"
                continue

            done = st["done"].get(task.key, 0)
            # 免费重开记满 2 次之后，填了终点仍要接着打，不能把整项跳过。
            if done >= task.max_per_day and not (
                    task.key == "征战世界" and _campaign_goal(config)):
                results[task.key] = f"今日已执行 {done}/{task.max_per_day} 次，跳过"
                log.info("[%s] %s", task.key, results[task.key])
                continue

            # 冷却之一：服务器明确告诉我们"到这个时刻才能再做"（占领结束时刻等）。
            # 这个是读来的，优先于任何写死的秒数。
            until = st.get("until", {}).get(task.key, 0)
            if until and time.time() < until:
                wait = until - time.time()
                results[task.key] = (f"占用中，服务器给的结束时刻还有 "
                                     f"{wait / 60:.0f} 分钟")
                log.info("[%s] %s", task.key, results[task.key])
                continue

            # 冷却之二：写死的秒数，只在服务器没给时刻时才用（如英雄培养的 8 小时）
            if task.cooldown_sec:
                last = st.get("last", {}).get(task.key, 0)
                wait = task.cooldown_sec - (time.time() - last)
                if wait > 0:
                    results[task.key] = f"冷却中，还需 {wait / 60:.0f} 分钟"
                    log.info("[%s] %s", task.key, results[task.key])
                    continue

            if task.confidence != "实测" and not allow_unverified:
                results[task.key] = "参数未实测，已跳过（见 tools/capture_daily.py）"
                log.warning("[%s] %s", task.key, results[task.key])
                continue

            _publish_current(task.key, None, on_fail)
            field_names = _field_names(schema, task.opcode)

            # 一个任务在**一轮里就要把当天的次数做完**，而不是做一次就走。
            # freeVisitCnt=[3,1,1] 是三个档位各自的免费次数（低级 3 次、中级 1 次、
            # 高级 1 次），三档都要领；战略训练一天 10 次同样的包。
            # 早先每轮只发一次，等于绝大多数次数根本没用上。
            if task.runner is not None:
                try:
                    got = task.runner(rec, sock, config)
                except OSError:
                    raise
                except Exception as exc:
                    got = (False, f"执行异常：{exc}")
                    log.exception("[%s] 自定义执行器抛异常", task.key)
                until = None
                if isinstance(got, tuple) and len(got) == 3:
                    ok, why, until = got
                elif isinstance(got, tuple) and len(got) == 2:
                    ok, why = got
                else:
                    ok, why = False, "执行器没有返回结果"
                results[task.key] = why
                log.info("[%s] %s %s", task.key, "✅" if ok else "❌", why)
                # 第三个值是下次还能再做的时刻。英雄培养一轮把槽放满后要等槽空出来，
                # 不能把当天次数直接打满，否则 8 小时后不会再放。
                if (isinstance(until, (int, float)) and not isinstance(until, bool)
                        and until > time.time()):
                    st.setdefault("until", {})[task.key] = int(until)
                if getattr(task, "counts_itself", False):
                    fresh = _load_state()
                    if isinstance(fresh.get("done"), dict):
                        st["done"] = fresh["done"]
                    if until:
                        _save_state(st)
                elif ok:
                    if until:
                        st["done"][task.key] = st["done"].get(task.key, 0) + 1
                    else:
                        st["done"][task.key] = task.max_per_day
                    _save_state(st)
                elif until:
                    _save_state(st)
                continue

            ran = 0
            while st["done"].get(task.key, 0) < task.max_per_day:
                raise_if_stopped()
                done = st["done"].get(task.key, 0)
                more = _do_once(task, sock, rec, st, results, details,
                                field_names, resp_timeout, gap, done, schema)
                if not more:
                    break
                ran += 1
                _publish_current(task.key, results.get(task.key), on_fail)
                # 服务器说这东西被占用到某时刻（占了演习场/占了矿），就别接着刷了
                if st.get("until", {}).get(task.key, 0) > time.time():
                    break
                # 靠**固定冷却**限流的任务，成功一次就到此为止。
                # 冷却只在进任务前查了一次，循环里不再查，于是英雄培养成功之后
                # 3 秒又发一次，服务端回 error=81（已在培养中）——
                # 报出来像是任务失败，其实第一次已经成功了。2026-08-29 实盘复现。
                if task.cooldown_sec:
                    log.info("[%s] 靠 %.0f 小时冷却限流，本轮做完一次即止",
                             task.key, task.cooldown_sec / 3600)
                    break
                _nap(gap)
            if ran > 1:
                log.info("[%s] 本轮共成功 %d 次（今日 %d/%d）", task.key, ran,
                         st["done"].get(task.key, 0), task.max_per_day)
            continue
        except OSError:
            raise
        except Exception as exc:
            results[task.key] = f"执行异常：{exc}"
            log.exception("[%s] 这一项没做成，继续下一项", task.key)
        finally:
            if _is_failure(results.get(task.key)):
                log.info("[%s] 这一项没做成，继续下一项", task.key)
            if results.get(task.key) not in (None, "未开启"):
                _publish_current(task.key, results.get(task.key), on_fail)


    _run_campaign_extras(
        rec, sock, switches, st, results,
        stages=_campaign_goal(config) or "",
        interval=_campaign_interval(config),
        on_fail=on_fail)
    got = (rec.latest.get("RseRedwarMonthCard") if rec else None) or (None, None)
    panel = got[1] if isinstance(got, tuple) and isinstance(got[1], dict) else None
    if panel:
        for key, index in _MONTH_CARDS:
            if results.get(key) == "未开启":
                text = _month_card_status(panel, index)
                if text:
                    results[key] = text

    _publish_total(results, on_fail)

    log.info("=== 每日任务结束 ===")
    for k, v in results.items():
        log.info("  %-12s %s", k, v)

    return results, details


def _run_campaign_extras(rec, sock, switches, st, results, stages="", interval=1.0,
                         on_fail=None):
    """第三次、第4次。开关在登录账号上，默认关。免费两次仍走「征战世界」。

    重开之后必须从第 1 关打到终点，打完才算这一次做成。只重开不算完成。
    这一轮还在往终点打时先不做。第三次没打到终点时，不发第4次。
    """
    from . import pve

    if campaign_pushing():
        for key, _name in CAMPAIGN_EXTRAS:
            if not (switches or {}).get(key):
                continue
            results[key] = "这一轮先打关卡，免费重开之后再做"
            log.info("[%s] %s", key, results[key])
        return

    def _done_of(key):
        try:
            return int((st.get("done") or {}).get(key) or 0) >= 1
        except (TypeError, ValueError):
            return False

    third_open = bool((switches or {}).get("征战第三次"))
    third_done = (not third_open) or _done_of("征战第三次")
    for key, kind in (("征战第三次", 3), ("征战第4次", 4)):
        if not (switches or {}).get(key):
            continue
        if key == "征战第4次" and not third_done:
            results[key] = "第三次还没打完，这一轮先不做第4次"
            log.info("[%s] %s", key, results[key])
            _publish_current(key, results[key], on_fail)
            continue
        _publish_current(key, None, on_fail)
        try:
            ok, why, finished, pushing = pve.extra_campaign(
                rec, sock, stages, interval, kind, already_done=_done_of(key))
        except OSError:
            raise
        except Exception as exc:
            ok, why, finished, pushing = False, f"执行异常：{exc}", False, True
            log.exception("[%s] 抛异常", key)
        results[key] = why if ok else (
            why if str(why).startswith("失败") else f"失败：{why}")
        log.info("[%s] %s %s", key, "✅" if ok else "❌", results[key])
        _publish_current(key, results[key], on_fail)
        if finished:
            st.setdefault("done", {})[key] = 1
            _save_state(st)
            if key == "征战第三次":
                third_done = True
        elif key == "征战第三次":
            third_done = False
        if pushing:
            _state_local.campaign_pushing = True


def _do_once(task, sock, rec, st, results, details, field_names,
             resp_timeout, gap, done, schema=None):
    """做这个任务一次。返回 True 表示成功且可以接着再做一次。

    返回 False 的情形都不该重试：闸门拦下、服务器拒绝、没等到响应、发送失败。
    """
    if True:
        # 前置请求：真实客户端每个动作前都会先开面板/查询，服务端据此建上下文。
        # 少了这步，动作发出去参数再对也不生效 —— 这是 2026-08-09 定位到的根因。
        # 发前置之前先记下消息序号：闸门和 FromResponse 都只认这之后到的响应，
        # 免得把登录爆发期推来的旧数据当成这一轮的回包。
        # 用序号不用时间戳 —— time.time() 在 Windows 上粒度约 15.6ms，
        # 服务器回得快时会和发送时刻落在同一个 tick，导致收到了也判成超时。
        gate_before = rec.seq_mark() if rec else 0
        tier = None
        panel_note = ""
        for pop, pfields in task.prelude:
            try:
                sender.send_frame(sock, pop,
                                  encode_message(pfields, omit_zero=False),
                                  rec.rc4_c2s)
                log.debug("[%s] 前置 %s", task.key, pop)
                _nap(0.4)
            except Exception as exc:
                log.warning("[%s] 前置请求 %s 失败: %s", task.key, pop, exc)
        if task.prelude:
            _nap(0.6)           # 给服务端一点时间把面板数据推回来

        # 闸门：前置请求的响应里就有剩余免费次数，客户端正是读它决定
        # 走免费档还是扣券/扣勋章档。读不到就不发。
        if task.gate:
            # 只认真正带着次数字段的那条 —— 同名消息可能连来好几条。
            # 还要认前置自己的区分字段：技能书将领({ActiveType:0})和参谋
            # ({ActiveType:1}) 是同一条消息、各有各的计时，光看"有没有 field4"
            # 会把对方的那条读进来。
            gfield = task.gate.field
            pre_want = None
            if task.prelude:
                pop, pfields = task.prelude[-1]
                pre_want = _echo_want(pfields, _field_names(schema, pop))

            def _has_counts(d, _f=gfield):
                return _read_counts(d, _f) is not None

            def _gwant(d, _w=pre_want):
                return _has_counts(d) and (_w is None or _w(d))

            # 严格判据挑不到就退回"只要带次数字段就行"：面板回包不一定回显
            # 请求里的 type，硬要求会把唯一带次数的那条否掉。
            gdata = _await_response(sock, rec, task.gate.rse_msg,
                                    gate_before, task.gate.timeout,
                                    want=_gwant, relaxed=_has_counts)
            passed, why, exhausted, tier = _check_gate(task.gate, gdata,
                                                       task.tiers)
            if not passed:
                if done == 0 or "已用完" not in why:
                    results[task.key] = f"闸门拦截：{why}"
                log.info("[%s] 闸门拦截：%s", task.key, why)
                if exhausted:
                    if task.cooldown_until:
                        # 靠冷却限流的任务，"次数为 0"只说明**现在**不能做，
                        # 不等于今天做不了了。技能书是 24 小时一轮，
                        # 上午拦下、下午到点还该再领 —— 这时候记满当天次数，
                        # 当天就再也不会重试。改为把服务端给的下次可用时刻记下来。
                        _note_until(task, st, results, gdata)
                        _save_state(st)
                    else:
                        # 确定没免费次数了，今天别再来
                        st["done"][task.key] = task.max_per_day
                        _save_state(st)
                return False
            log.info("[%s] 闸门放行：%s", task.key, why)
            # 前置响应里的看点（排名/积分/剩余次数）记下来，最后并进结果一起推送
            if task.report and isinstance(gdata, dict):
                bits = [f"{f}={_show(gdata[f])}"
                        for f in task.report if f in gdata]
                if bits:
                    panel_note = " ".join(bits)
                    log.info("[%s] 面板：%s", task.key, panel_note)

        # 没有闸门的任务也要能报面板（公会战就靠它把 dayHasPK 读出来）。
        # 闸门那条路已经顺手取过了，这里只补没闸门的情形。
        if task.report and not panel_note and task.prelude:
            pop, pfields = task.prelude[-1]
            pname = (getattr(schema, "SCHEMA", {}) or {}).get(pop, {}).get("name")
            if pname:
                pw = _echo_want(pfields, _field_names(schema, pop))
                pdata = _await_response(sock, rec, _rse_name(pname),
                                        gate_before, 3.0, want=pw)
                if isinstance(pdata, dict):
                    bits = [f"{f}={_show(pdata[f])}"
                            for f in task.report if f in pdata]
                    if bits:
                        panel_note = " ".join(bits)
                        log.info("[%s] 面板：%s", task.key, panel_note)

        # 占位符现在才解析：像"挑一个空演习场""七天乐第几天"这类值，
        # 必须等前置请求的响应回来才算得出。解析在安全检查之前，
        # 所以检查看到的就是真正要发出去的内容。
        fields, why = _resolve_fields(task, rec, sock, gate_before)
        if fields is None:
            if done == 0:
                results[task.key] = why
            log.info("[%s] %s", task.key, why)
            return False

        # 档位：把档位字段换成本次要打的那一档（低级/中级/高级）
        if task.tiers and tier is not None:
            fields = dict(fields)
            fields[task.tiers.field] = ("int32", task.tiers.values[tier])

        ok, why = _check_safety(task, field_names, fields)
        if not ok:
            results[task.key] = f"安全检查拦截：{why}"
            log.error("[%s] %s", task.key, results[task.key])
            return False

        # 前置和动作同一个 opcode 时，响应也同名，必须靠区分字段（OptType/type…）
        # 才能认出哪条是动作的回包。造不出判据就**不发** —— 发了也分不清结果，
        # 只会把前置的 ret=0 当成"成功"。2026-08-13 实盘就是这么假成功的。
        if any(pop == task.opcode for pop, _ in task.prelude) \
                and _echo_want(fields, field_names) is None:
            seen = "、".join(f"{n}={field_names.get(n, '?')}"
                            for n in sorted(fields)) or "（一个字段都没解出来）"
            results[task.key] = (f"认不出动作回包（前置与动作同 opcode，而请求里的"
                                 f"字段 {seen} 没有一个在 DISCRIMINATORS 里），"
                                 f"这一轮不做")
            log.error("[%s] %s", task.key, results[task.key])
            return False

        # omit_zero=False：任务表里列的字段一个都不能省，0 也要显式写出来。
        # 真实客户端就是这么发的，省掉等于把 type/expType 这些字段整个丢了。
        body = encode_message(fields, omit_zero=False)
        desc = ", ".join(f"{field_names.get(k, k)}={v[1]!r}"
                         for k, v in sorted(fields.items()))

        rse = _rse_name(task.msg)
        # 记下发送前的消息序号，避免把上一次的旧响应误当成本次结果
        before = rec.seq_mark() if rec else 0
        try:
            sender.send_frame(sock, task.opcode, body, rec.rc4_c2s)
            # 注意：这里**不能**立刻给 done 计数。
            # 之前在这里就 +1，导致失败的尝试也在烧每日配额 —— 跑几次失败之后
            # 所有任务都显示"今日已执行 N/N，跳过"，明明一次都没成。
            # 计数放到判定成功之后。
            st.setdefault("last", {})[task.key] = time.time()   # 冷却仍按发送计时
            _save_state(st)
            log.info("[%s] 已发送 %s(%s)  {%s}", task.key, task.msg, task.opcode, desc)
        except Exception as exc:
            results[task.key] = f"发送失败：{exc}"
            log.error("[%s] %s", task.key, results[task.key])
            return False

        # 收响应并判成败 —— 不能只管发不管结果，否则"三次机会"用完了还在撞墙。
        # 必须按区分字段挑出**本次动作**的回包，别把前置的回包当成结果。
        data = _await_response(sock, rec, rse, before, resp_timeout,
                               want=_echo_want(fields, field_names))
        # 分档任务的"剩余次数"要看开面板响应的整个数组，不能信动作回包里那个
        # 标量 —— 它只说当前这一档没了（配件探索打完 10001 就报 leftFreeCnt=0，
        # 而 10002/10003 其实各还有一次）。换档交给闸门。
        ok, why, stop = judge(rse, data, ignore_left=bool(task.tiers),
                              success_flag=task.success_flag)
        # 动作回包里若也带着这些字段，用它 —— 那是**做完之后**的状态。
        # 否则会出现"✅ 成功（面板：bSignIn=False）"这种自相矛盾的话：
        # False 是签到**前**闸门读到的值，签完已经是 True 了。
        if task.report and isinstance(data, dict):
            after = [f"{f}={_show(data[f])}" for f in task.report if f in data]
            if after:
                panel_note = " ".join(after)
        if panel_note:
            why = f"{why}（{panel_note}）"
        results[task.key] = why
        if ok:
            # 只有**确认成功**才算用掉一次每日额度
            st["done"][task.key] = done + 1
            st.setdefault("miss", {}).pop(task.key, None)   # 成功就清零重试计数
            _save_state(st)
            log.info("[%s] ✅ %s（今日 %d/%d）", task.key, why,
                     done + 1, task.max_per_day)
            extra, last_data, fu_ok = _run_followup(task, sock, rec, data,
                                                    field_names, resp_timeout,
                                                    gap)
            if extra:
                results[task.key] = why + "；" + "；".join(extra)
            if fu_ok is False:
                # 有些任务的"动作"其实只是开面板，真正干活的是后续步骤。
                # 后续被拒还把当天次数记满，就等于这一天再也不会重试了。
                # 所以后续失败时退回这一次计数。
                st["done"][task.key] = done
                _save_state(st)
                log.info("[%s] 后续步骤未成，本次不计入当天次数，稍后可再试",
                         task.key)
                if data is not None:
                    details[task.key] = data
                return False
            # 记下服务器给的"下次可用时刻"。矿区的 resourceEndTime 只在占矿
            # （后续步骤）的回包里，所以后续的响应也要看。
            _note_until(task, st, results, last_data or data)
            if stop:
                # 成功了，但响应说剩余次数已归零 —— 别再循环了
                st["done"][task.key] = task.max_per_day
                _save_state(st)
                if data is not None:
                    details[task.key] = data
                return False
        else:
            log.warning("[%s] ❌ %s", task.key, why)
            if stop:
                st.setdefault("miss", {}).pop(task.key, None)
                if task.cooldown_sec or task.cooldown_until:
                    # 靠冷却限流的任务，被拒多半只是"还没到时候"，不是"今天没了"。
                    # 英雄培养就是这样：上一轮 8 小时没走完，服务器回 ret=81，
                    # 这时候把当天次数打满是错的 —— 等冷却过去还该再试。
                    log.info("[%s] 本次被拒，等冷却过去再试", task.key)
                else:
                    # 服务器明确拒绝，且没有冷却依据，今天别再撞了
                    st["done"][task.key] = task.max_per_day
                    log.info("[%s] 今日不再重试", task.key)
                _save_state(st)
            elif data is None:
                # 只是没等到响应。允许后面几轮再试，但不能无限试下去，
                # 连续 MAX_MISS 轮都收不到就认了。
                miss = st.setdefault("miss", {}).get(task.key, 0) + 1
                st["miss"][task.key] = miss
                if miss >= MAX_MISS:
                    st["done"][task.key] = task.max_per_day
                    log.info("[%s] 连续 %d 轮收不到响应，今日不再重试",
                             task.key, miss)
                else:
                    log.info("[%s] 第 %d/%d 次没等到响应，下一轮还会再试",
                             task.key, miss, MAX_MISS)
                _save_state(st)
        if data is not None:
            details[task.key] = data
        return ok


def _note_until(task, st, results, data):
    """把服务器给的"下次可用时刻"记进状态，供下一轮判冷却。

    只认落在合理区间的 unix 秒（既不能是过去，也不能远到离谱），
    免得把某个恰好是大整数的字段错当成时间戳。
    """
    if not task.cooldown_until or not isinstance(data, dict):
        return
    ts = _read_path(data, task.cooldown_until)
    if isinstance(ts, list):
        ts = max((x for x in ts if isinstance(x, int)), default=None)
    now = time.time()
    if not isinstance(ts, int) or isinstance(ts, bool):
        return
    if not (now < ts < now + 30 * 86400):     # 未来 30 天以内才当真
        return
    st.setdefault("until", {})[task.key] = ts
    _save_state(st)
    mins = (ts - now) / 60
    log.info("[%s] 服务器给的下次可用时刻：%s（还有 %.0f 分钟）", task.key,
             time.strftime("%m-%d %H:%M", time.localtime(ts)), mins)
    results[task.key] = results.get(task.key, "") + f"；占用至 {mins:.0f} 分钟后"


def _run_followup(task, sock, rec, data, field_names, resp_timeout, gap):
    """动作成功后按响应继续发（占矿、把达标的奖励档位领完）。

    返回 (每一轮的说明列表, 最后一条响应, 成败)。第二项用来读"下次可用时刻"
    —— 矿区的 resourceEndTime 只出现在占矿那一步的回包里。
    第三项：True=至少发出去一轮且都成了，False=发出去但被拒，
    None=压根没有后续步骤可发（这时不该影响主动作的判定）。
    任何一轮失败或没东西可发就停 —— 后续步骤本来就是"有就做，没有就算"，
    不该把整条任务判成失败。
    """
    fu = task.followup
    if fu is None or data is None:
        return [], None, None
    out, last, seen, verdict = [], data, set(), None
    for _ in range(fu.max_rounds):
        try:
            nxt = fu.build(last)
        except Exception as exc:
            log.debug("[%s] 后续步骤构造失败: %s", task.key, exc)
            break
        if not nxt:
            break
        # 同一包别发第二遍。服务器对领奖的即时回包里状态还没更新
        # （实测领了 giftID=10，回包里它仍标着"未领"），照着算就会再领一次。
        sig = tuple(sorted((k, v[1]) for k, v in nxt.items()))
        if sig in seen:
            log.debug("[%s] 后续步骤重复（%s），停", task.key, sig)
            break
        seen.add(sig)
        ok, why = _check_safety(task, field_names, nxt)
        if not ok:
            log.error("[%s] 后续步骤被安全检查拦截：%s", task.key, why)
            out.append(f"{fu.desc}: 被安全检查拦截（{why}）")
            verdict = False       # 拦下了就是没做成，别让主动作顶着"成功"过关
            break
        rse = _rse_name(task.msg)
        before = rec.seq_mark() if rec else 0
        desc = ", ".join(f"{field_names.get(k, k)}={v[1]!r}"
                         for k, v in sorted(nxt.items()))
        try:
            sender.send_frame(sock, fu.opcode,
                              encode_message(nxt, omit_zero=False),
                              rec.rc4_c2s)
            log.info("[%s] 后续 %s(%s) {%s}", task.key, fu.desc, fu.opcode, desc)
        except Exception as exc:
            log.warning("[%s] 后续步骤发送失败: %s", task.key, exc)
            break
        _nap(gap)
        last = _await_response(sock, rec, rse, before, resp_timeout)
        ok, why, _stop = judge(rse, last)
        log.info("[%s] 后续结果：%s %s", task.key, "✅" if ok else "❌", why)
        out.append(f"{fu.desc}: {why}")
        verdict = bool(ok)
        if not ok or last is None:
            break
    return out, last, verdict


def _pick_recent(rec, rse_msg, since_seq, want=None):
    """从这条消息的近期历史里挑一条序号更新、且满足 want 的。

    只看 rec.latest 是不够的：服务器会连着推同名但内容不同的两条
    （RseWPCBaseOpen 先 type:0 带 leftFreeCnt，再 type:3 只有擂台信息），
    latest 会被后一条冲掉，闸门就以为"没有这个字段"。
    """
    hist = getattr(rec, "recent", {}).get(rse_msg)
    if hist:
        for seq, data in reversed(hist):
            if seq > since_seq and (want is None or want(data)):
                return data
        return None
    got = rec.latest.get(rse_msg)                 # 老录制器没有 recent
    if got and got[0] > since_seq and (want is None or want(got[1])):
        return got[1]
    return None


def _await_response(sock, rec, rse_msg, since_seq, timeout, want=None,
                    relaxed=None):
    """发完请求后等对应的服务器响应。

    since_seq 是**消息到达序号**（rec.seq_mark()），只认序号更大的，
    这样既能排掉旧数据，又不受时钟粒度影响。
    want 可选，用来在同名的几条里挑出真正有用的那条。

    relaxed 是**退而求其次**的判据：等不到严格匹配时用它再捞一次。
    闸门要用到 —— 有些面板回包的 type 不是对请求的回显而是"回包种类"
    （RceWPCBaseOpen 发 type:1，回包却是 type:0 的面板 + type:3 的冠军信息），
    拿它当区分字段会把唯一带次数的那条否掉。2026-08-13 实盘就这么把配件探索
    明明还有 3 次免费的一轮跳过了。所以严格判据只用来"优先挑"，挑不到就放宽。
    """
    if rec is None:
        return None
    deadline = time.time() + timeout
    while time.time() < deadline:
        raise_if_stopped()
        hit = _pick_recent(rec, rse_msg, since_seq, want)
        if hit is not None:
            return hit
        _beat()          # 等回包是耗时大头，心跳得在这儿续上
        try:
            sock.settimeout(0.5)
            if not sock.recv(8192):
                break
        except TimeoutError:
            continue
        except OSError:
            break
    if relaxed is not None:
        hit = _pick_recent(rec, rse_msg, since_seq, relaxed)
        if hit is not None:
            log.debug("等不到严格匹配的 %s，退回用宽松判据挑到一条", rse_msg)
            return hit
    return None
