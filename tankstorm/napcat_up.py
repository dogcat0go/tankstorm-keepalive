# tankstorm-keepalive  Copyright (C) 2026 Dimlitter
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# 本程序是自由软件：你可以依据自由软件基金会发布的 GNU Affero 通用公共许可证
# （第 3 版，或你选择的任何更新版本）之条款，再分发和/或修改它。
# 本程序希望能有用，但不提供任何担保；甚至不含适销性或特定用途适用性的默示担保。
# 详见随附的 LICENSE 文件，或 <https://www.gnu.org/licenses/>。
"""给一个攻打 QQ 起一台 NapCat。

一台容器只登一个 QQ。容器名 napcat-<QQ号>，数据在 napcat/<QQ号>/。
OneBot 映射到 127.0.0.1 上的空闲端口（从 3333 起），界面从 6099 起。
地址和两份 Token 写入 config.local.json。已经手写过配置的号不会再起第二台。
"""

import hashlib
import json
import os
import secrets
import shutil
import socket
import subprocess
import threading

from . import citydb
from .log import get_logger
from .paths import user_path

log = get_logger()

_IMAGE = os.environ.get("NAPCAT_IMAGE") or "mlikiowa/napcat-docker:latest"
_HTTP_START = 3333
_WEB_START = 6099
_lock = threading.Lock()


def ensure_napcat(uin: str, username: str = "", config: dict | None = None) -> dict:
    with _lock:
        return _ensure(uin, username, config)


def _ensure(uin: str, username: str = "", config: dict | None = None) -> dict:
    """拉起 napcat-<QQ号>，并把地址、Token 写进配置。config 是正在跑的网页配置，会一并改掉。"""
    uin = str(uin or "").strip()
    username = str(username or "").strip()
    out = {"ok": False, "qq": uin, "host": "", "web_host": "", "running": False, "created": False, "msg": ""}
    if not uin.isdigit() or not 5 <= len(uin) <= 12:
        out["msg"] = "QQ 号要是 5 到 12 位数字"
        return out
    if not shutil.which("docker"):
        out["msg"] = "这台机器没有 docker，装好之后再起"
        return out
    name = f"napcat-{uin}"
    try:
        found, running, http_port, web_port = _container(name)
    except OSError as exc:
        out["msg"] = _docker_hint(str(exc))
        return out
    if found:
        if not running:
            code, stdout, stderr = _docker(["start", name], 60)
            if code != 0:
                out["msg"] = _docker_hint(stderr or stdout)
                return out
            running = True
        try:
            _, running, http_port, web_port = _container(name)
        except OSError as exc:
            out["msg"] = _docker_hint(str(exc))
            return out
        if not http_port or not web_port:
            out["msg"] = f"{name} 已在，但没有读到 3000 和 6099 的端口映射"
            return out
        http_token, web_token = _tokens_from_volume(uin)
        if not http_token or not web_token:
            saved_http, saved_web = _tokens_from_config(uin)
            http_token = http_token or saved_http
            web_token = web_token or saved_web
        if not http_token or not web_token:
            out["msg"] = f"{name} 已在，但没有 Token。看 napcat/{uin}/config"
            return out
        note = _remember(uin, username, http_port, web_port, http_token, web_token, config)
        out.update(ok=True, running=running, host=f"127.0.0.1:{http_port}",
                   web_host=f"127.0.0.1:{web_port}", msg=f"{name} 已在跑。{note}")
        log.info("%s", out["msg"])
        return out
    if _already_configured(uin):
        out["ok"] = True
        out["msg"] = "这个号已经写了 NapCat，没有另起容器。原来那台继续用"
        return out
    try:
        used = _taken_ports()
        http_port = _free_port(_HTTP_START, used)
        used.add(http_port)
        web_port = _free_port(_WEB_START, used)
    except OSError as exc:
        out["msg"] = _docker_hint(str(exc))
        return out
    http_token = secrets.token_urlsafe(18)
    web_token = secrets.token_urlsafe(18)
    config_dir, qq_dir = _dirs(uin)
    _write_volume(config_dir, uin, http_token, web_token)
    uid = getattr(os, "getuid", lambda: 0)()
    gid = getattr(os, "getgid", lambda: 0)()
    mac = "02:42:" + ":".join(f"{b:02x}" for b in hashlib.sha256(uin.encode()).digest()[:4])
    code, stdout, stderr = _docker([
        "run", "-d", "--name", name, "--restart=always", "--mac-address", mac,
        "-p", f"127.0.0.1:{web_port}:6099",
        "-p", f"127.0.0.1:{http_port}:3000",
        "-e", f"NAPCAT_UID={uid}", "-e", f"NAPCAT_GID={gid}",
        "-e", f"WEBUI_TOKEN={web_token}",
        "-v", f"{config_dir}:/app/napcat/config",
        "-v", f"{qq_dir}:/app/.config/QQ",
        _IMAGE,
    ], 300)
    if code != 0:
        out["msg"] = _docker_hint(stderr or stdout)
        return out
    note = _remember(uin, username, http_port, web_port, http_token, web_token, config)
    out.update(
        ok=True, created=True, running=True,
        host=f"127.0.0.1:{http_port}", web_host=f"127.0.0.1:{web_port}",
        msg=(f"已起 {name}，OneBot 127.0.0.1:{http_port}，界面 127.0.0.1:{web_port}。"
             f"接着取登录码，用这个号扫一次。{note}"),
    )
    log.info("已起 %s，OneBot 127.0.0.1:%s，界面 127.0.0.1:%s", name, http_port, web_port)
    return out


def _remember(uin, username, http_port, web_port, http_token, web_token, config) -> str:
    spec = {
        "cookie": f"accounts/qq-{uin}.json",
        "NapCat": {
            "地址": f"http://127.0.0.1:{http_port}",
            "Token": http_token,
            "界面": f"http://127.0.0.1:{web_port}",
            "界面Token": web_token,
        },
    }
    notes = []
    if username:
        user_id = citydb.user_id_by_name(username)
        if not user_id:
            notes.append(f"没有登录账号 {username}，没有绑定")
        else:
            why = citydb.bind_attack_account(user_id, uin)
            if why:
                notes.append(why)
            else:
                owner = _config_user(uin)
                if owner and owner != username:
                    notes.append(f"配置里这个号属于 {owner}，没有改成 {username}")
                else:
                    spec["用户"] = username
                    notes.append(f"已绑到 {username}")
    _store(spec, uin, config)
    return "".join(notes)


def _store(spec: dict, uin: str, config: dict | None) -> None:
    path = user_path("config.local.json")
    local = {}
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            local = loaded
    _merge_slot(local, uin, spec)
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(local, f, ensure_ascii=False, indent=4)
        f.write("\n")
    os.replace(tmp, path)
    if isinstance(config, dict):
        _merge_slot(config, uin, spec)


def _merge_slot(tree: dict, uin: str, spec: dict) -> None:
    login = tree.get("登录")
    if not isinstance(login, dict):
        login = {}
        tree["登录"] = login
    accounts = login.get("账号")
    if not isinstance(accounts, dict):
        accounts = {}
        login["账号"] = accounts
    slot = _slot(accounts, uin) or f"qq-{uin}"
    current = accounts.get(slot)
    if not isinstance(current, dict):
        current = {}
    current["cookie"] = spec["cookie"]
    if spec.get("用户"):
        current["用户"] = spec["用户"]
    box = current.get("NapCat")
    if not isinstance(box, dict):
        box = {}
    box.update(spec["NapCat"])
    current["NapCat"] = box
    accounts[slot] = current


def _slot(accounts: dict, uin: str) -> str:
    for key in (f"qq-{uin}", uin):
        if isinstance(accounts.get(key), dict):
            return key
    suffix = f"qq-{uin}.json"
    for key, item in accounts.items():
        if isinstance(item, dict) and str(item.get("cookie") or "").endswith(suffix):
            return key
    return ""


def _already_configured(uin: str) -> bool:
    path = user_path("config.local.json")
    if not os.path.isfile(path):
        return False
    with open(path, encoding="utf-8") as f:
        loaded = json.load(f)
    accounts = ((loaded or {}).get("登录") or {}).get("账号") or {}
    if not isinstance(accounts, dict):
        return False
    slot = _slot(accounts, uin)
    box = (accounts.get(slot) or {}).get("NapCat") if slot else None
    if not isinstance(box, dict):
        return False
    return all(str(box.get(key) or "").strip() for key in ("地址", "Token", "界面", "界面Token"))


def _dirs(uin: str) -> tuple:
    root = user_path(os.path.join("napcat", uin))
    config_dir = os.path.join(root, "config")
    qq_dir = os.path.join(root, "qq")
    os.makedirs(config_dir, exist_ok=True)
    os.makedirs(qq_dir, exist_ok=True)
    return config_dir, qq_dir


def _write_volume(config_dir: str, uin: str, http_token: str, web_token: str) -> None:
    _write_json(os.path.join(config_dir, "webui.json"), {
        "host": "0.0.0.0", "port": 6099, "token": web_token, "loginRate": 3,
    })
    body = {
        "network": {
            "httpServers": [{
                "name": "httpServer",
                "enable": True,
                "port": 3000,
                "host": "0.0.0.0",
                "enableCors": True,
                "enableWebsocket": False,
                "messagePostFormat": "array",
                "token": http_token,
                "debug": False,
            }],
            "httpClients": [],
            "websocketServers": [],
            "websocketClients": [],
        },
        "musicSignUrl": "",
        "enableLocalFile2Url": False,
        "parseMultMsg": False,
    }
    _write_json(os.path.join(config_dir, "onebot11.json"), body)
    _write_json(os.path.join(config_dir, f"onebot11_{uin}.json"), body)


def _config_user(uin: str) -> str:
    path = user_path("config.local.json")
    if not os.path.isfile(path):
        return ""
    try:
        with open(path, encoding="utf-8") as f:
            loaded = json.load(f)
    except (OSError, ValueError):
        return ""
    accounts = ((loaded or {}).get("登录") or {}).get("账号") or {}
    if not isinstance(accounts, dict):
        return ""
    slot = _slot(accounts, uin)
    item = accounts.get(slot) if slot else None
    if not isinstance(item, dict):
        return ""
    return str(item.get("用户") or "").strip()


def _tokens_from_config(uin: str) -> tuple:
    path = user_path("config.local.json")
    if not os.path.isfile(path):
        return "", ""
    try:
        with open(path, encoding="utf-8") as f:
            loaded = json.load(f)
    except (OSError, ValueError):
        return "", ""
    accounts = ((loaded or {}).get("登录") or {}).get("账号") or {}
    if not isinstance(accounts, dict):
        return "", ""
    slot = _slot(accounts, uin)
    box = (accounts.get(slot) or {}).get("NapCat") if slot else None
    if not isinstance(box, dict):
        return "", ""
    return str(box.get("Token") or "").strip(), str(box.get("界面Token") or "").strip()


def _tokens_from_volume(uin: str) -> tuple:
    config_dir = os.path.join(user_path("napcat"), uin, "config")
    web_token = ""
    web_path = os.path.join(config_dir, "webui.json")
    if os.path.isfile(web_path):
        try:
            with open(web_path, encoding="utf-8") as f:
                web_token = str((json.load(f) or {}).get("token") or "")
        except (OSError, ValueError):
            web_token = ""
    http_token = ""
    for name in (f"onebot11_{uin}.json", "onebot11.json"):
        path = os.path.join(config_dir, name)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        servers = ((data or {}).get("network") or {}).get("httpServers") or []
        if servers and isinstance(servers[0], dict):
            http_token = str(servers[0].get("token") or "")
            if http_token:
                break
    return http_token, web_token


def _write_json(path: str, body: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(body, f, ensure_ascii=False, indent=4)
        f.write("\n")


def _container(name: str) -> tuple:
    code, stdout, stderr = _docker(["inspect", name], 30)
    if code != 0:
        text = f"{stdout}\n{stderr}"
        if "No such object" in text or "no such object" in text.lower():
            return False, False, 0, 0
        raise OSError(text.strip() or "docker inspect 失败")
    try:
        info = json.loads(stdout)
    except ValueError as exc:
        raise OSError("docker inspect 返回的不是容器信息") from exc
    if isinstance(info, list):
        info = info[0] if info else {}
    if not isinstance(info, dict):
        raise OSError("docker inspect 返回的不是容器信息")
    running = bool((info.get("State") or {}).get("Running"))
    http_port, web_port = _published((info.get("HostConfig") or {}).get("PortBindings"))
    return True, running, http_port, web_port


def _published(bindings) -> tuple:
    http_port = web_port = 0
    if not isinstance(bindings, dict):
        return 0, 0
    for key, vals in bindings.items():
        if not vals or not isinstance(vals, list):
            continue
        try:
            host_port = int((vals[0] or {}).get("HostPort") or 0)
        except (TypeError, ValueError):
            continue
        if str(key).startswith("3000"):
            http_port = host_port
        elif str(key).startswith("6099"):
            web_port = host_port
    return http_port, web_port


def _taken_ports() -> set:
    code, stdout, stderr = _docker(["ps", "-a", "--format", "{{.Names}}"], 30)
    used = set()
    if code != 0:
        raise OSError((stderr or stdout).strip() or "docker ps 失败")
    for name in stdout.split():
        if not name.startswith("napcat-"):
            continue
        try:
            _, _, http_port, web_port = _container(name)
        except OSError:
            continue
        used.add(http_port)
        used.add(web_port)
    path = user_path("config.local.json")
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as f:
                loaded = json.load(f)
        except (OSError, ValueError):
            loaded = {}
        accounts = ((loaded or {}).get("登录") or {}).get("账号") or {}
        if isinstance(accounts, dict):
            for item in accounts.values():
                box = item.get("NapCat") if isinstance(item, dict) else None
                if not isinstance(box, dict):
                    continue
                for key in ("地址", "界面"):
                    used.add(_port_of(box.get(key)))
    used.discard(0)
    return used


def _port_of(url) -> int:
    host = str(url or "")
    if "://" in host:
        host = host.split("://", 1)[1]
    host = host.split("/", 1)[0]
    if host.count(":") != 1:
        return 0
    try:
        return int(host.rsplit(":", 1)[1])
    except ValueError:
        return 0


def _free_port(start: int, used: set) -> int:
    for port in range(start, start + 200):
        if port in used:
            continue
        sock = socket.socket()
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            continue
        finally:
            sock.close()
        return port
    raise OSError(f"从 {start} 起没有空闲端口")


def _docker(args: list, timeout: int) -> tuple:
    try:
        proc = subprocess.run(
            ["docker", *args], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise OSError("docker 超时") from exc
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _docker_hint(text: str) -> str:
    raw = " ".join(str(text or "").split())
    if "permission denied" in raw.lower() or "connect to the docker daemon" in raw.lower():
        return "连不上 Docker。把运行这条命令的用户加入 docker 组，重新登录后再试"
    if not raw:
        return "docker 没有成功"
    return raw[:400]
