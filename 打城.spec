# -*- mode: python ; coding: utf-8 -*-
# 公会版：不写账号、不核对有效期，只锁公会。打出来是 dist\打城-公会.exe
#   $env:TANKSTORM_LOCK_GUILD="公会ID"
#   py -3 -m PyInstaller --clean --noconfirm 打城.spec
# 不设这个变量时，打的是原来的账号版 dist\打城.exe

import os
import re

try:
    import webview
    _hooks = [os.path.join(os.path.dirname(webview.__file__), "__pyinstaller")]
except Exception:
    _hooks = []

_guild = os.environ.get("TANKSTORM_LOCK_GUILD", "").strip()
_gui = os.path.join(SPECPATH, "city_gui.py")
_orig = None
if _guild:
    if not _guild.isdigit() or int(_guild) <= 0 or int(_guild) > 2147483647:
        raise SystemExit("TANKSTORM_LOCK_GUILD 必须是 1 到 2147483647 的整数")
    _guild = str(int(_guild))
    with open(_gui, encoding="utf-8") as f:
        _orig = f.read()
    _new = _orig
    _lines = {
        "LOCK_SERVER": 'LOCK_SERVER = ""',
        "LOCK_IP": 'LOCK_IP = ""',
        "LOCK_ACCOUNT": 'LOCK_ACCOUNT = ""',
        "LOCK_QQS": "LOCK_QQS = ()",
        "LOCK_GUILD": 'LOCK_GUILD = "%s"' % _guild,
        "LOCK_OPEN": "LOCK_OPEN = True",
    }
    for _name, _line in _lines.items():
        _new, _n = re.subn(r"^%s = .*$" % _name, _line, _new, count=1, flags=re.M)
        if _n != 1:
            raise SystemExit("city_gui.py 里没有 %s" % _name)
    with open(_gui, "w", encoding="utf-8", newline="\n") as f:
        f.write(_new)
    print("公会版，只锁公会 %s，不写账号、不核对有效期" % _guild)

try:
    a = Analysis(
        ['city_gui.py'],
        pathex=[],
        binaries=[],
        datas=[('config.json', '.'), ('protocol.json', '.'), ('endpoints.json', '.'),
               ('tankstorm/schema.json', 'tankstorm'), ('tankstorm/wiremap.json', 'tankstorm')],
        hiddenimports=["webview", "clr"],
        hookspath=_hooks,
        hooksconfig={},
        runtime_hooks=[],
        excludes=[],
        noarchive=False,
        optimize=0,
    )
    pyz = PYZ(a.pure)
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name='打城-公会' if _guild else '打城',
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=True,
        upx_exclude=[],
        runtime_tmpdir=None,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
    )
finally:
    if _orig is not None:
        with open(_gui, "w", encoding="utf-8", newline="\n") as f:
            f.write(_orig)
