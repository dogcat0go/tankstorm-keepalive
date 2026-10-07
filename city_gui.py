# tankstorm-keepalive  Copyright (C) 2026 Dimlitter
# SPDX-License-Identifier: AGPL-3.0-or-later
"""打指定城市，或先移动到该城再打。窗口程序，不带钩子版 SWF。"""

import json
import logging
import os
import queue
import shutil
import sys
import threading
import tkinter as tk
from tkinter import ttk

import main as cli
from tankstorm import citydb, license_gate, lockqq, socket_keepalive
from tankstorm.log import get_logger
from tankstorm.qq_login import QQSession

log = get_logger()

BUILDINGS = [
    (10132, "比萨斜塔"),
    (10133, "埃菲尔铁塔"),
    (10134, "大本钟"),
    (10135, "女神像"),
    (10136, "红场"),
    (10137, "帝国大厦"),
    (10138, "万磁陀螺"),
    (10139, "英雄徽章雕塑"),
]


class _UiLog(logging.Handler):
    def __init__(self, q):
        super().__init__(logging.INFO)
        self.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))
        self.q = q

    def emit(self, record):
        self.q.put(("log", self.format(record)))


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("坦克风暴 · 打城")
        self.geometry("720x640")
        self.minsize(640, 520)
        self._q = queue.Queue()
        self._busy = False
        self._session_ok = False
        self._license_stopped = False
        self._cities = []
        self._photo = None
        log.addHandler(_UiLog(self._q))

        top = ttk.Frame(self, padding=12)
        top.pack(fill="x")
        self.pick_label = tk.StringVar(value="城市")
        ttk.Label(top, textvariable=self.pick_label).grid(row=0, column=0, sticky="w")
        self.city = tk.StringVar()
        ent = ttk.Entry(top, textvariable=self.city)
        ent.grid(row=0, column=1, sticky="ew", padx=(8, 0))
        ent.bind("<KeyRelease>", self._filter)
        top.columnconfigure(1, weight=1)

        self.hits = tk.Listbox(top, height=6)
        self.hits.grid(row=1, column=1, sticky="ew", pady=(6, 0))
        self.hits.bind("<<ListboxSelect>>", self._pick)

        self.mode = tk.StringVar(value="atk")
        modes = ttk.Frame(top)
        modes.grid(row=2, column=1, sticky="w", pady=(10, 0))
        atk_row = ttk.Frame(modes)
        atk_row.pack(anchor="w")
        ttk.Radiobutton(atk_row, text="攻击这座城（人已在同城或相邻）",
                        variable=self.mode, value="atk",
                        command=self._on_mode).pack(side="left")
        ttk.Button(atk_row, text="仇敌", command=self._foe_window).pack(
            side="left", padx=(8, 0))
        ttk.Radiobutton(modes, text="先移动到这座城，路上的敌城和目标城都打",
                        variable=self.mode, value="move",
                        command=self._on_mode).pack(anchor="w")
        fund_row = ttk.Frame(modes)
        fund_row.pack(anchor="w")
        ttk.Radiobutton(fund_row, text="自动拨款",
                        variable=self.mode, value="fund",
                        command=self._on_mode).pack(side="left")
        self.building = tk.StringVar(
            value=f"{BUILDINGS[0][0]} {BUILDINGS[0][1]}")
        ttk.Combobox(
            fund_row, textvariable=self.building, width=18, state="readonly",
            values=[f"{i} {name}" for i, name in BUILDINGS],
        ).pack(side="left", padx=(8, 0))
        ttk.Label(fund_row, text="次数").pack(side="left", padx=(8, 4))
        self.fund_times = tk.IntVar(value=1)
        ttk.Spinbox(fund_row, from_=1, to=30, width=4,
                    textvariable=self.fund_times).pack(side="left")
        self.sweep = tk.BooleanVar(value=True)
        ttk.Checkbutton(top, text="扫荡（只对「攻击这座城」生效，移动过去时固定扫荡）",
                        variable=self.sweep).grid(row=3, column=1, sticky="w", pady=(6, 0))

        ttk.Radiobutton(modes, text="打本国首都卫星城旁的摩多军团（先走到相邻城，再召唤志愿兵）",
                        variable=self.mode, value="mordor",
                        command=self._on_mode).pack(anchor="w")
        card_row = ttk.Frame(modes)
        card_row.pack(anchor="w", pady=(4, 0))
        ttk.Label(card_row, text="本次最多用恢复卡").pack(side="left")
        self.card_limit = tk.IntVar(value=self._card_limit())
        ttk.Spinbox(card_row, from_=0, to=999, width=5,
                    textvariable=self.card_limit).pack(side="left", padx=(8, 0))

        # 勾选项以后可以继续加每日任务。现在先放征战世界。
        self.do_war = tk.BooleanVar(value=False)
        war = ttk.Frame(top)
        war.grid(row=4, column=1, sticky="w", pady=(6, 0))
        ttk.Checkbutton(war, text="征战世界", variable=self.do_war).pack(side="left")
        ttk.Label(war, text="最终关卡").pack(side="left", padx=(8, 4))
        self.pve_stages = tk.StringVar()
        ttk.Entry(war, textvariable=self.pve_stages, width=8).pack(side="left")
        ttk.Label(war, text="留空用 config，0 表示打到过不去").pack(side="left", padx=(8, 0))

        pwd_row = ttk.Frame(top)
        pwd_row.grid(row=5, column=1, sticky="w", pady=(10, 0))
        ttk.Label(pwd_row, text="QQ").pack(side="left")
        self.pwd_uin = tk.StringVar()
        self.pwd_box = ttk.Combobox(pwd_row, textvariable=self.pwd_uin, width=13,
                                    values=list(self._load_pwds()))
        self.pwd_box.pack(side="left", padx=(4, 8))
        self.pwd_box.bind("<<ComboboxSelected>>", self._on_pwd_pick)
        self.pwd_box.bind("<FocusOut>", self._on_pwd_focus)
        ttk.Label(pwd_row, text="密码").pack(side="left")
        self.pwd_pass = tk.StringVar()
        ttk.Entry(pwd_row, textvariable=self.pwd_pass, show="*", width=16).pack(
            side="left", padx=(4, 8))
        self.pwd_btn = ttk.Button(pwd_row, text="密码登录", command=self._pwd_login)
        self.pwd_btn.pack(side="left")

        bar = ttk.Frame(top)
        bar.grid(row=6, column=1, sticky="w", pady=(10, 0))
        self.login_btn = ttk.Button(bar, text="扫码登录", command=self._login)
        self.login_btn.pack(side="left")
        self.go_btn = ttk.Button(bar, text="开始", command=self._go)
        self.go_btn.pack(side="left", padx=(8, 0))
        self.stop_btn = ttk.Button(bar, text="停止", command=self._stop, state="disabled")
        self.stop_btn.pack(side="left", padx=(8, 0))
        self.status = tk.StringVar(value="正在读城市目录…")
        ttk.Label(bar, textvariable=self.status).pack(side="left", padx=(12, 0))
        self.remain = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.remain).pack(side="left", padx=(12, 0))

        self.qr = ttk.Label(top)
        self.qr.grid(row=7, column=1, sticky="w", pady=(8, 0))

        self.logbox = tk.Text(self, height=16, wrap="word", state="disabled")
        self.logbox.pack(fill="both", expand=True, padx=12, pady=(0, 12))

        self.after(200, self._drain)
        threading.Thread(target=self._load_cities, daemon=True).start()
        self._refresh_login()
        if license_gate.enabled():
            license_gate.start()
            self.after(1000, self._tick_license)

    def _load_cities(self):
        try:
            rows = citydb.list_cities()
        except Exception as exc:
            self._q.put(("log", f"城市目录没拉下来：{exc}"))
            self._ui(lambda: self.status.set("城市目录失败，仍可手填城市 ID"))
            return
        self._cities = [(int(i), name or "", n or "") for i, name, _, n in rows]
        self._ui(self._show_cities)

    def _on_mode(self):
        self._filter()

    def _show_cities(self):
        self._filter()
        n = len(self._cities)
        cur = self.status.get()
        note = f"城市 {n} 座"
        if cur.startswith(("已登录", "未登录", "登录态", "已到期")):
            self.status.set(f"{cur} · {note}")
        else:
            self.status.set(note)

    def _filter(self, _evt=None):
        key = self.city.get().strip()
        self.hits.delete(0, "end")
        shown = 0
        for cid, name, nation in self._cities:
            line = f"{cid}  {name}  {nation}"
            if key and key not in line:
                continue
            self.hits.insert("end", line)
            shown += 1
            if shown >= 40:
                break

    def _pick(self, _evt=None):
        sel = self.hits.curselection()
        if not sel:
            return
        self.city.set(self.hits.get(sel[0]).split()[0])

    def _card_limit(self):
        try:
            n = int((cli.load_config().get("国战") or {}).get("单次最多用几张恢复卡") or 1)
        except (TypeError, ValueError):
            n = 1
        return max(0, min(999, n))

    def _save_card_limit(self, n):
        path = cli.paths.user_path("config.json")
        try:
            with open(path, encoding="utf-8-sig") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            log.info("恢复卡张数没写入配置：%s", exc)
            return
        if not isinstance(data, dict):
            return
        war = data.get("国战")
        if not isinstance(war, dict):
            war = {}
            data["国战"] = war
        war["单次最多用几张恢复卡"] = int(n)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
        os.replace(tmp, path)

    def _save_foes(self, uids):
        path = cli.paths.user_path("config.json")
        try:
            with open(path, encoding="utf-8-sig") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            log.info("仇敌名单没写入配置：%s", exc)
            return False
        if not isinstance(data, dict):
            return False
        war = data.get("国战")
        if not isinstance(war, dict):
            war = {}
            data["国战"] = war
        war["仇敌UID"] = list(uids)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
        os.replace(tmp, path)
        return True

    def _foe_window(self):
        from tankstorm.country_war import _clean_foe_uids

        win = tk.Toplevel(self)
        win.title("仇敌优先级")
        win.geometry("460x460")
        win.transient(self)
        ttk.Label(
            win,
            text="越靠上越优先。粘贴时多出来的空格会自动去掉。\n"
                 "攻击这座城，或先移动到这座城时，沿途和目标城每次进攻后的等待里"
                 "会循环看前 5 页，先打这几页里排到的仇敌。",
            wraplength=420,
        ).pack(anchor="w", padx=12, pady=(12, 6))
        uids = _clean_foe_uids(
            (cli.load_config().get("国战") or {}).get("仇敌UID") or [])
        show = tk.Listbox(win, font=("Consolas", 12), height=10, activestyle="none")
        show.pack(fill="both", expand=True, padx=12)

        def redraw():
            show.delete(0, "end")
            for i, uid in enumerate(uids, 1):
                show.insert("end", f"{i:>2}    {uid}")

        redraw()
        paste = tk.Text(win, height=3, font=("Consolas", 11))
        paste.pack(fill="x", padx=12, pady=(8, 0))

        def add():
            for uid in _clean_foe_uids(paste.get("1.0", "end")):
                if uid not in uids:
                    uids.append(uid)
            paste.delete("1.0", "end")
            redraw()

        def selected():
            got = show.curselection()
            return got[0] if got else None

        def remove():
            i = selected()
            if i is None:
                return
            uids.pop(i)
            redraw()

        def move(step):
            i = selected()
            j = None if i is None else i + step
            if j is None or j < 0 or j >= len(uids):
                return
            uids[i], uids[j] = uids[j], uids[i]
            redraw()
            show.selection_set(j)
            show.see(j)

        def save():
            add()
            if not self._save_foes(uids):
                return
            log.info("仇敌优先级已保存 %d 个", len(uids))
            win.destroy()

        bar = ttk.Frame(win)
        bar.pack(fill="x", padx=12, pady=8)
        ttk.Button(bar, text="添加", command=add).pack(side="left")
        ttk.Button(bar, text="删除", command=remove).pack(side="left", padx=(8, 0))
        ttk.Button(bar, text="上移", command=lambda: move(-1)).pack(side="left", padx=(8, 0))
        ttk.Button(bar, text="下移", command=lambda: move(1)).pack(side="left", padx=(8, 0))
        ttk.Button(bar, text="保存", command=save).pack(side="right")

    def _city_id(self):
        text = self.city.get().strip().split()
        if not text or not text[0].isdigit():
            return 0
        return int(text[0])

    def _building_id(self):
        text = self.building.get().strip().split()
        if not text or not text[0].isdigit():
            return 0
        return int(text[0])

    def _pwd_path(self):
        return cli.paths.user_path("pwd_accounts.json")

    def _load_pwds(self):
        try:
            with open(self._pwd_path(), encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(k).strip(): v for k, v in data.items()
                if str(k).strip().isdigit() and isinstance(v, str) and v}

    def _save_pwds(self, data):
        path = self._pwd_path()
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    def _qq_cookie(self, uin):
        u = str(uin or "").strip().lstrip("o0")
        return cli.paths.user_path(os.path.join("qq", u, "cookies.json"))

    def _on_pwd_focus(self, _evt=None):
        uin = self.pwd_uin.get().strip()
        saved = self._load_pwds().get(uin)
        if saved:
            self.pwd_pass.set(saved)

    def _on_pwd_pick(self, _evt=None):
        uin = self.pwd_uin.get().strip()
        self.pwd_pass.set(self._load_pwds().get(uin, ""))
        src = self._qq_cookie(uin)
        if uin.isdigit() and os.path.isfile(src) and not self._busy:
            shutil.copyfile(src, cli.paths.user_path("cookies.json"))
            self.status.set(f"已切换到 {uin}")

    def _refresh_login(self):
        qq = QQSession()
        self._session_ok = bool(qq.is_valid() and qq.uin)
        self.status.set("已登录 " + str(qq.uin) if self._session_ok
                        else ("登录态还在" if qq.is_valid() else "未登录"))
        if not self.pwd_uin.get():
            self.pwd_uin.set(lockqq.bind_uin() or str(qq.uin or ""))
        saved = self._load_pwds()
        uin = self.pwd_uin.get().strip()
        if uin in saved and not self.pwd_pass.get():
            self.pwd_pass.set(saved[uin])
        self.pwd_box["values"] = list(saved)

    def _set_busy(self, busy, stoppable=False):
        self._busy = busy
        state = "disabled" if busy else "normal"
        self.go_btn.configure(state=state)
        self.login_btn.configure(state=state)
        self.pwd_btn.configure(state=state)
        self.stop_btn.configure(state="normal" if stoppable else "disabled")

    def _stop(self):
        if not self._busy:
            return
        from tankstorm import daily
        daily.request_stop()
        self.stop_btn.configure(state="disabled")
        self.status.set("正在停止…")

    def _login(self):
        if self._busy:
            return
        self._set_busy(True)
        self.status.set("等待扫码…")

        def work():
            qq = QQSession()
            config = cli.load_config()
            push = (lockqq.bind_uin()
                    or (config.get("登录", {}) or {}).get("推送登录QQ号")
                    or qq.uin or None)

            def on_qr(path, pushed=False):
                self._ui(lambda p=path, z=pushed: self._show_qr(p, z))

            ok = qq.qr_login(on_qr=on_qr, push_uin=push)
            blocked = ok and lockqq.refuse(qq)
            uin = qq.uin
            self._ui(lambda o=ok and not blocked, u=uin, b=blocked:
                     self._login_done(o, u, b))

        threading.Thread(target=work, daemon=True).start()

    def _pwd_login(self):
        if self._busy:
            return
        uin = self.pwd_uin.get().strip()
        password = self.pwd_pass.get()
        if not uin or not password:
            self.status.set("先填写 QQ 和密码")
            return
        self._set_busy(True)
        self.status.set("密码登录中…")

        def work():
            qq = QQSession(cookie_file=self._qq_cookie(uin))

            def status(msg):
                self._ui(lambda m=msg: self.status.set(m))

            result = qq.password_login(uin, password, low_login=True, on_status=status)
            ok = bool(result.get("ok"))
            blocked = ok and lockqq.refuse(qq)
            msg = result.get("msg") or ""
            self._ui(lambda o=ok and not blocked, u=qq.uin or uin, b=blocked, m=msg:
                     self._pwd_done(o, u, b, m))

        threading.Thread(target=work, daemon=True).start()

    def _pwd_done(self, ok, uin, blocked=False, msg=""):
        self._login_done(ok, uin, blocked)
        if ok and not blocked:
            key = str(uin).strip().lstrip("o0")
            pwds = self._load_pwds()
            pwds[key] = self.pwd_pass.get()
            self._save_pwds(pwds)
            self.pwd_uin.set(key)
            self.pwd_box["values"] = list(pwds)
            src = self._qq_cookie(key)
            if os.path.isfile(src):
                shutil.copyfile(src, cli.paths.user_path("cookies.json"))
        elif not blocked and msg:
            self.status.set(msg)

    def _show_qr(self, path, pushed):
        try:
            self._photo = tk.PhotoImage(file=path)
            self.qr.configure(image=self._photo)
        except tk.TclError as exc:
            log.info("二维码打不开：%s", exc)
        self.status.set("请在手机 QQ 点确认" if pushed else "用另一台设备扫码")

    def _login_done(self, ok, uin, blocked=False):
        self._set_busy(False)
        self.qr.configure(image="")
        self._session_ok = bool(ok and not blocked)
        if blocked:
            self.status.set(f"此版本只允许 QQ {lockqq.bind_uin()}")
        elif self._session_ok and license_gate.blocked():
            self.status.set("已到期")
            self.go_btn.configure(state="disabled")
        else:
            self.status.set(f"已登录 {uin}，填写城市后点开始" if ok else "登录没有完成")

    def _go(self):
        if self._busy:
            return
        mode = self.mode.get()
        target = self._building_id() if mode == "fund" else self._city_id()
        do_city = mode in ("atk", "move")
        do_mordor = mode == "mordor"
        do_fund = mode == "fund"
        do_war = bool(self.do_war.get())
        if do_fund and not target:
            self.status.set("先选择要拨款的建筑")
            return
        if do_city and not target and not do_war:
            self.status.set("先填写城市 ID，或从列表里点一座")
            return
        if do_city and not target:
            do_city = False
        if not do_city and not do_fund and not do_war and not do_mordor:
            self.status.set("先填写城市，或勾选征战世界")
            return
        if self._session_ok and license_gate.enabled() and not license_gate.allow_run():
            self.status.set("已到期" if license_gate.blocked() else "正在核对剩余时长")
            if license_gate.blocked():
                self.go_btn.configure(state="disabled")
            return
        try:
            cards = max(0, int(self.card_limit.get()))
        except (TypeError, ValueError, tk.TclError):
            cards = 1
        self._save_card_limit(cards)
        sweep = bool(self.sweep.get())
        try:
            times = int(self.fund_times.get())
        except (TypeError, ValueError, tk.TclError):
            times = 1
        from tankstorm import daily
        daily.clear_stop()
        self._set_busy(True, stoppable=True)
        self.status.set("执行中…")

        def work():
            codes = []
            try:
                qq = QQSession()
                if lockqq.refuse(qq):
                    want = lockqq.bind_uin()
                    self._ui(lambda w=want: self._done(f"此版本只允许 QQ {w}"))
                    return
                if qq.uin and not license_gate.allow_run():
                    note = "已到期" if license_gate.blocked() else "正在核对剩余时长"
                    self._ui(lambda n=note: self._done(n))
                    return
                config = cli.load_config()
                if do_mordor:
                    config.setdefault("国战", {})["单次最多用几张恢复卡"] = cards
                    config["国战"]["自动使用国战恢复卡"] = True
                    codes.append(socket_keepalive.run_own_legion_once(qq, config))
                if do_fund:
                    codes.append(socket_keepalive.run_fund_once(
                        qq, config, target, times))
                elif do_city:
                    if mode == "move":
                        codes.append(socket_keepalive.run_move_once(
                            qq, config, target, sweep=True))
                    else:
                        codes.append(socket_keepalive.run_farm_city_once(
                            qq, config, target, sweep=sweep))
                if do_war:
                    codes.append(socket_keepalive.run_pve_once(
                        qq, config, self.pve_stages.get().strip()))
                tail = "完成" if codes and not any(codes) else f"结束，代码 {codes}"
                if license_gate.blocked():
                    tail = "已到期"
                elif daily.stopped():
                    tail = "已停止"
            except Exception as exc:
                log.exception("执行失败")
                if license_gate.blocked():
                    tail = "已到期"
                elif daily.stopped():
                    tail = "已停止"
                else:
                    tail = f"失败：{exc}"
            self._ui(lambda t=tail: self._done(t))

        threading.Thread(target=work, daemon=True).start()

    def _done(self, text):
        self._set_busy(False)
        self.status.set(text)
        if text == "已到期":
            self.go_btn.configure(state="disabled")

    def _tick_license(self):
        if not license_gate.enabled():
            return
        license_gate.beat()
        left = license_gate.seconds_left()
        if left is None:
            self.remain.set("正在核对剩余时长")
        elif left <= 0:
            self.remain.set("已到期" if self._session_ok or self._busy else "剩余 00:00")
            if self._session_ok or self._busy:
                self.status.set("已到期")
                self.go_btn.configure(state="disabled")
                if self._busy and not self._license_stopped:
                    self._license_stopped = True
                    from tankstorm import daily
                    daily.request_stop()
        else:
            self.remain.set(license_gate.remain_text())
            if self.status.get() == "正在核对剩余时长" and self._session_ok:
                self.status.set("已登录，填写城市后点开始")
        self.after(1000, self._tick_license)

    def _ui(self, fn):
        self._q.put(("ui", fn))

    def _drain(self):
        while True:
            try:
                kind, payload = self._q.get_nowait()
            except queue.Empty:
                break
            if kind == "ui":
                payload()
                continue
            self.logbox.configure(state="normal")
            self.logbox.insert("end", payload + "\n")
            self.logbox.see("end")
            self.logbox.configure(state="disabled")
        self.after(200, self._drain)


def main():
    for name in ("config.json", "endpoints.json", "protocol.json"):
        cli.paths.ensure_user_copy(name)
    App().mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
