"""打包时把试用 QQ、公会 ID 加密写进 tankstorm/_trial_bind.py，构建完还原，再给 exe 加壳。"""

import hashlib
import io
import os
import re
import shutil
import subprocess
import urllib.request
import zipfile

_REL = os.path.join("tankstorm", "_trial_bind.py")


def _seal(uin: str) -> tuple:
    raw = uin.encode("utf-8")
    seed = os.urandom(16)
    stream = b""
    block = seed
    while len(stream) < len(raw):
        block = hashlib.sha256(block).digest()
        stream += block
    mixed = bytes(raw[i] ^ stream[i] ^ ((i * 31) & 0xFF) for i in range(len(raw)))
    cut = len(mixed) // 2
    blob = mixed[cut:] + mixed[:cut]
    return seed, blob


def begin(root: str):
    qq = os.environ.get("TANKSTORM_LOCK_QQ", "").strip()
    guild = os.environ.get("TANKSTORM_LOCK_GUILD", "").strip()
    url = os.environ.get("TANKSTORM_LICENSE_URL", "").strip().rstrip("/")
    user = os.environ.get("TANKSTORM_LICENSE_USER", "").strip()
    password = os.environ.get("TANKSTORM_LICENSE_PASS", "")
    if not qq and not guild and not url:
        return None
    if qq and (not qq.isdigit() or not 5 <= len(qq) <= 15):
        raise SystemExit("TANKSTORM_LOCK_QQ 必须是 5 到 15 位数字")
    if url:
        if not url.startswith(("http://", "https://")):
            raise SystemExit("TANKSTORM_LICENSE_URL 必须以 http:// 或 https:// 开头")
        if not qq:
            raise SystemExit("授权包必须同时设置 TANKSTORM_LOCK_QQ")
        if not re.fullmatch(r"[A-Za-z0-9_\u4e00-\u9fff]{2,32}", user):
            raise SystemExit("TANKSTORM_LICENSE_USER 用 2 到 32 位字母、数字或中文")
        if len(password) < 6 or len(password) > 72:
            raise SystemExit("TANKSTORM_LICENSE_PASS 长度要在 6 到 72")
    if guild:
        if not guild.isdigit() or int(guild) <= 0 or int(guild) > 2147483647:
            raise SystemExit("TANKSTORM_LOCK_GUILD 必须是 1 到 2147483647 的整数")
        guild = str(int(guild))
    qq_seed, qq_blob = _seal(qq) if qq else ((), ())
    guild_seed, guild_blob = _seal(guild) if guild else ((), ())
    user_seed, user_blob = _seal(user) if user else ((), ())
    pass_seed, pass_blob = _seal(password) if password else ((), ())
    path = os.path.join(root, _REL)
    with open(path, encoding="utf-8") as f:
        old = f.read()
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(
            "SEED = %r\nBLOB = %r\nGUILD_SEED = %r\nGUILD_BLOB = %r\n"
            "LICENSE_URL = %r\nGRACE_SEC = %d\n"
            "LICENSE_USER_SEED = %r\nLICENSE_USER_BLOB = %r\n"
            "LICENSE_PASS_SEED = %r\nLICENSE_PASS_BLOB = %r\n" % (
                tuple(qq_seed), tuple(qq_blob), tuple(guild_seed), tuple(guild_blob),
                url, 600 if url else 0,
                tuple(user_seed), tuple(user_blob),
                tuple(pass_seed), tuple(pass_blob)))
    print("试用锁已加密写入（%s）" % "、".join(
        x for x in (("QQ" if qq else ""), ("公会" if guild else ""),
                    ("授权" if url else "")) if x))
    return path, old


def end(state) -> None:
    if not state:
        return
    path, old = state
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(old)


_UPX_URL = "https://github.com/upx/upx/releases/download/v5.2.1/upx-5.2.1-win64.zip"


def _upx_exe() -> str:
    found = shutil.which("upx")
    if found:
        return found
    here = os.path.join(os.path.dirname(os.path.abspath(__file__)), "upx")
    exe = os.path.join(here, "upx.exe")
    if os.path.isfile(exe):
        return exe
    if os.name != "nt":
        raise SystemExit("未找到 UPX，试用包需要加壳。")
    os.makedirs(here, exist_ok=True)
    print("正在下载 UPX")
    req = urllib.request.Request(_UPX_URL, headers={"User-Agent": "tankstorm-build"})
    with urllib.request.urlopen(req, timeout=120) as r:
        payload = r.read()
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        name = next(n for n in zf.namelist() if n.endswith("upx.exe"))
        with zf.open(name) as src, open(exe, "wb") as dst:
            dst.write(src.read())
    return exe


def pack(exe: str) -> None:
    """用 UPX 压最终的 exe。附加在文件尾部的打包数据会保留。"""
    if not os.path.isfile(exe):
        raise SystemExit(f"找不到要加壳的文件：{exe}")
    print("正在加壳")
    # 带控制流保护的 exe，UPX 默认拒绝，--force 才压。
    subprocess.check_call([_upx_exe(), "--force", "--best", "--lzma", exe])
    print("加壳完成")
