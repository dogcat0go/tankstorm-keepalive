# -*- mode: python ; coding: utf-8 -*-
# 试用版（只锁公会就不要设 TANKSTORM_LOCK_QQ）：
#   $env:TANKSTORM_LOCK_GUILD="690422"; pyinstaller --clean --noconfirm 打城.spec
# 授权版：锁 QQ，包里自带 10 分钟。每次打开先登录，再 POST /api/clock，
# 正文 mono_ms 是单调计时毫秒，读响应 remaining_ms，之后本机倒计时。
#   $env:TANKSTORM_LOCK_QQ="QQ号"
#   $env:TANKSTORM_LICENSE_URL="https://服务器"
#   $env:TANKSTORM_LICENSE_USER="会员名"
#   $env:TANKSTORM_LICENSE_PASS="会员密码"

import importlib.util
import os

_bind_path = os.path.join(SPECPATH, "tools", "trial_bind.py")
_bind_spec = importlib.util.spec_from_file_location("_trial_bind_tool", _bind_path)
_bind = importlib.util.module_from_spec(_bind_spec)
_bind_spec.loader.exec_module(_bind)
_trial = _bind.begin(SPECPATH)

try:
    a = Analysis(
    ['city_gui.py'],
    pathex=[],
    binaries=[],
    datas=[('config.json', '.'), ('protocol.json', '.'), ('endpoints.json', '.'), ('tankstorm/schema.json', 'tankstorm')],
    hiddenimports=[],
    hookspath=[],
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
    name='打城',
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
    if _trial:
        _bind.pack(os.path.join(DISTPATH, "打城.exe"))
finally:
    _bind.end(_trial)
