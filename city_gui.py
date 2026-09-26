# tankstorm-keepalive  Copyright (C) 2026 Dimlitter
# SPDX-License-Identifier: AGPL-3.0-or-later
"""打指定城市，或先移动到该城再打。窗口程序，不带钩子版 SWF。"""

import logging
import queue
import sys
import threading
import tkinter as tk
from tkinter import ttk

import main as cli
from tankstorm import citydb, socket_keepalive
from tankstorm.log import get_logger
from tankstorm.qq_login import QQSession

log = get_logger()

# 9/25 点选过的成就建筑。只有帝国大厦在抓包里对上了名字。
BUILDINGS = [
    (10138, "帝国大厦"),
    (10129, ""),
    (10126, ""),
    (10132, ""),
    (10135, ""),
    (10136, ""),
    (10133, ""),
    (10137, ""),
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
        ttk.Radiobutton(modes, text="攻击这座城（人已在同城或相邻）",
                        variable=self.mode, value="atk",
                        command=self._on_mode).pack(anchor="w")
        ttk.Radiobutton(modes, text="先移动到这座城，路上的敌城和目标城都打",
                        variable=self.mode, value="move",
                        command=self._on_mode).pack(anchor="w")
        fund_row = ttk.Frame(modes)
        fund_row.pack(anchor="w")
        ttk.Radiobutton(fund_row, text="自动拨款",
                        variable=self.mode, value="fund",
                        command=self._on_mode).pack(side="left")
        ttk.Label(fund_row, text="次数").pack(side="left", padx=(8, 4))
        self.fund_times = tk.IntVar(value=1)
        ttk.Spinbox(fund_row, from_=1, to=30, width=4,
                    textvariable=self.fund_times).pack(side="left")
        self.sweep = tk.BooleanVar(value=True)
        ttk.Checkbutton(top, text="扫荡（只对「攻击这座城」生效，移动过去时固定扫荡）",
                        variable=self.sweep).grid(row=3, column=1, sticky="w", pady=(6, 0))

        # 勾选项以后可以继续加每日任务。现在先放征战世界。
        self.do_war = tk.BooleanVar(value=False)
        war = ttk.Frame(top)
        war.grid(row=4, column=1, sticky="w", pady=(6, 0))
        ttk.Checkbutton(war, text="征战世界", variable=self.do_war).pack(side="left")
        ttk.Label(war, text="关卡").pack(side="left", padx=(8, 4))
        self.pve_stages = tk.StringVar()
        ttk.Entry(war, textvariable=self.pve_stages, width=24).pack(side="left")
        ttk.Label(war, text="留空用 config「征战.关卡」").pack(side="left", padx=(8, 0))

        bar = ttk.Frame(top)
        bar.grid(row=5, column=1, sticky="w", pady=(10, 0))
        self.login_btn = ttk.Button(bar, text="扫码登录", command=self._login)
        self.login_btn.pack(side="left")
        self.go_btn = ttk.Button(bar, text="开始", command=self._go)
        self.go_btn.pack(side="left", padx=(8, 0))
        self.status = tk.StringVar(value="正在读城市目录…")
        ttk.Label(bar, textvariable=self.status).pack(side="left", padx=(12, 0))

        self.qr = ttk.Label(top)
        self.qr.grid(row=6, column=1, sticky="w", pady=(8, 0))

        self.logbox = tk.Text(self, height=16, wrap="word", state="disabled")
        self.logbox.pack(fill="both", expand=True, padx=12, pady=(0, 12))

        self.after(200, self._drain)
        threading.Thread(target=self._load_cities, daemon=True).start()
        self._refresh_login()

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
        self.pick_label.set("建筑" if self.mode.get() == "fund" else "城市")
        self.city.set("")
        self._filter()

    def _catalog(self):
        if self.mode.get() == "fund":
            return [(i, name, "") for i, name in BUILDINGS]
        return self._cities

    def _show_cities(self):
        if self.mode.get() != "fund":
            self._filter()
        n = len(self._cities)
        cur = self.status.get()
        note = f"城市 {n} 座"
        if cur.startswith("已登录") or cur.startswith("未登录") or cur.startswith("登录态"):
            self.status.set(f"{cur} · {note}")
        else:
            self.status.set(note)

    def _filter(self, _evt=None):
        key = self.city.get().strip()
        self.hits.delete(0, "end")
        shown = 0
        for cid, name, nation in self._catalog():
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

    def _city_id(self):
        text = self.city.get().strip().split()
        if not text or not text[0].isdigit():
            return 0
        return int(text[0])

    def _refresh_login(self):
        qq = QQSession()
        self.status.set("已登录 " + str(qq.uin) if qq.is_valid() and qq.uin
                        else ("登录态还在" if qq.is_valid() else "未登录"))

    def _set_busy(self, busy):
        self._busy = busy
        state = "disabled" if busy else "normal"
        self.go_btn.configure(state=state)
        self.login_btn.configure(state=state)

    def _login(self):
        if self._busy:
            return
        self._set_busy(True)
        self.status.set("等待扫码…")

        def work():
            qq = QQSession()
            config = cli.load_config()
            push = (config.get("登录", {}) or {}).get("推送登录QQ号") or qq.uin or None

            def on_qr(path, pushed=False):
                self._ui(lambda p=path, z=pushed: self._show_qr(p, z))

            ok = qq.qr_login(on_qr=on_qr, push_uin=push)
            uin = qq.uin
            self._ui(lambda o=ok, u=uin: self._login_done(o, u))

        threading.Thread(target=work, daemon=True).start()

    def _show_qr(self, path, pushed):
        try:
            self._photo = tk.PhotoImage(file=path)
            self.qr.configure(image=self._photo)
        except tk.TclError as exc:
            log.info("二维码打不开：%s", exc)
        self.status.set("请在手机 QQ 点确认" if pushed else "用另一台设备扫码")

    def _login_done(self, ok, uin):
        self._set_busy(False)
        self.qr.configure(image="")
        self.status.set(f"已登录 {uin}，填写城市后点开始" if ok else "登录没有完成")

    def _go(self):
        if self._busy:
            return
        target = self._city_id()
        mode = self.mode.get()
        do_city = mode in ("atk", "move")
        do_fund = mode == "fund"
        do_war = bool(self.do_war.get())
        if do_fund and not target:
            self.status.set("先填写建筑 ID，或从列表里点一座")
            return
        if do_city and not target and not do_war:
            self.status.set("先填写城市 ID，或从列表里点一座")
            return
        if do_city and not target:
            do_city = False
        if not do_city and not do_fund and not do_war:
            self.status.set("先填写城市，或勾选征战世界")
            return
        sweep = bool(self.sweep.get())
        try:
            times = int(self.fund_times.get())
        except (TypeError, ValueError, tk.TclError):
            times = 1
        self._set_busy(True)
        self.status.set("执行中…")

        def work():
            codes = []
            try:
                qq = QQSession()
                config = cli.load_config()
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
            except Exception as exc:
                log.exception("执行失败")
                tail = f"失败：{exc}"
            self._ui(lambda t=tail: self._done(t))

        threading.Thread(target=work, daemon=True).start()

    def _done(self, text):
        self._set_busy(False)
        self.status.set(text)

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
