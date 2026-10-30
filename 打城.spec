# -*- mode: python ; coding: utf-8 -*-

import os

try:
    import webview
    _hooks = [os.path.join(os.path.dirname(webview.__file__), "__pyinstaller")]
except Exception:
    _hooks = []

a = Analysis(
    ['city_gui.py'],
    pathex=[],
    binaries=[],
    datas=[('config.json', '.'), ('protocol.json', '.'), ('endpoints.json', '.'), ('tankstorm/schema.json', 'tankstorm')],
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
