# -*- mode: python ; coding: utf-8 -*-
"""JZToolsHub —— PyInstaller 打包配置（后端单目录可执行程序）。

打包策略（部署 = 后端 exe + 前端源码同层）：
- 后端框架（app.py + Flask + 全部第三方依赖）编译进 _internal/；
- config / static / plugins / docs 保留为 exe 同层源码目录（前端可快速修改、
  配置与插件运行数据可读写），由 build-deploy.ps1 组装部署目录；
- 插件后端在运行时经 importlib 动态加载，PyInstaller 静态扫描看不到其
  第三方导入，必须在此显式 collect_all（隐藏导入 + 数据 + 二进制）。
"""

from PyInstaller.utils.hooks import collect_all

# 插件后端动态导入的第三方库（app.py 未直接 import，需显式收集）
PACKAGES = [
    "waitress",        # 生产 WSGI 服务器（frozen 分支）
    "cryptography",    # admin 插件（Fernet 加密）
    "requests",        # case-report / character-graph（大模型调用）
    "docx",            # python-docx（shared-docs / character-graph）
    "openpyxl",        # shared-docs / trajectory-convert
    "xlrd",            # shared-docs / trajectory-convert
    "qrcode",          # trajectory-convert
    "zfec",            # trajectory-convert / qr-video-decode
    "cv2",             # trajectory-convert（opencv）
    "numpy",           # trajectory-convert
    "pypdf",           # character-graph
]

datas = []
binaries = []
hiddenimports = []
for _pkg in PACKAGES:
    try:
        _d, _b, _h = collect_all(_pkg)
        datas += _d
        binaries += _b
        hiddenimports += _h
    except Exception:
        pass

a = Analysis(
    ["app.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
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
    name="JZToolsHub",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,                # 保留控制台窗口：显示服务日志，关闭即停止服务
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="JZToolsHub",
)
