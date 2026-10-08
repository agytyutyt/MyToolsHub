# -*- mode: python ; coding: utf-8 -*-
"""JZClient 出包 spec（批次 1 T07）。

命名/版本从构建配置单源读取（tools/client/client-build-config.json）——
改名/改版本只动那份 JSON，本 spec 无硬编码。
批次 1 打包范围：能力层服务（无 UI 层，pywebview 在批次 2 T10 接入）；
内核模块（jz_deps/jz_api/jztools_data）以源码形态随包（kernel/），
批次 3/4 代码真正 import 时改为分析式打包。
"""
import json
import os

from PyInstaller.utils.hooks import collect_all

SPEC_DIR = SPECPATH                       # client/
ROOT = os.path.dirname(SPEC_DIR)          # 仓库根
_cfg = json.load(open(os.path.join(ROOT, "tools", "client", "client-build-config.json"),
                      encoding="utf-8"))
NAME = _cfg["product"]["name"]

# pywebview 后端为动态 import（T01 spike 结论：--collect-all webview 才能进包）
_w_datas, _w_bins, _w_hidden = collect_all("webview")
_p_datas, _p_bins, _p_hidden = collect_all("pystray")

a = Analysis(
    [os.path.join(ROOT, "client", "client_main.py")],
    pathex=[ROOT, os.path.join(ROOT, "client")],
    binaries=_w_bins + _p_bins,
    datas=[
        (os.path.join(ROOT, "tools", "client", "client-build-config.json"), "."),
        (os.path.join(ROOT, "client", "fallback.html"), "."),
        (os.path.join(ROOT, "client", "local_admin.html"), "."),
        (os.path.join(ROOT, "client", "plugin_host.py"), "."),
        (os.path.join(ROOT, "client", "relay_whitelist.json"), "."),
        (os.path.join(ROOT, "static"), "static"),
        (os.path.join(ROOT, "jz_deps.py"), "kernel"),
        (os.path.join(ROOT, "jz_api.py"), "kernel"),
        (os.path.join(ROOT, "jztools_data.py"), "kernel"),
        # 插件 worker 由自带运行时 python 拉起 plugin_host.py，宿主模块只能吃盘上文件
        # （冻结 app 走 PYZ 够用）——随包落 kernel/（2026-10-08 T07 遗留销项）
        (os.path.join(ROOT, "jz_plugin_host.py"), "kernel"),
    ] + _w_datas + _p_datas,
    hiddenimports=["waitress"] + _w_hidden + _p_hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,            # 与主包同口径：GUI 子系统，诊断走 client.log
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name=NAME,
)
