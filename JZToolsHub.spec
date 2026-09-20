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
# 注：本清单同时是「插件依赖白名单」的唯一真源——出包工具 build-plugin-package.ps1 的
#     C-4 校验据此判断插件后端引用的第三方库是否在整包内（规范 U-2）。因此下面两项虽由
#     flask 的 hook 间接带进包，也必须显式登记，否则 admin 插件出包会被误判为"依赖未打包"。
# ★ 只保留**框架必需**依赖（2026-09-20 彻底模块化）：
#   - flask / werkzeug / waitress：框架本体（app.py 直接 import）
#   - cryptography：核心插件 admin 的账号密码/密钥加密（登录体系，必须随主体）
#   - pystray / PIL：托盘图标常驻（框架功能）
#   其余第三方库一律移出主包，改由「依赖组件包」按需安装（见 DEP_COMPONENT_PACKAGES）。
PACKAGES = [
    "flask",           # Web 框架本体（app.py 直接 import）
    "werkzeug",        # flask 的依赖；admin 插件用 werkzeug.security 做口令散列
    "waitress",        # 生产 WSGI 服务器（frozen 分支）
    "cryptography",    # admin 插件（Fernet 加密账号密码/密钥）——登录体系必需
    "pystray",         # 系统托盘图标（打包运行 GUI 常驻后台）
    "PIL",             # pystray 图标生成（pillow）
]

# 由「依赖组件包」按需提供、**不随主包**的第三方库（2026-09-20，T25 第 1 步）：
#   cv2(opencv-python) + numpy 只服务 info-transfer / trajectory-convert 的**视频码流模式**，
#   压缩后合计约 60 MB（主包 102.5 MB 的大头）→ 改为独立「依赖组件包」按需安装，
#   装到 <程序目录>/runtime/pylibs/，主体启动时注入 sys.path（见 app.py:_setup_dep_components）。
#   ★ 本清单同样是「插件依赖白名单」的一部分：出包工具 C-4 允许插件声明这些包，
#     但要求目标机装了对应依赖组件（缺则插件降级，提示"安装依赖组件包"）。
DEP_COMPONENT_PACKAGES = [
    # 由「依赖组件包」按需提供、**不随主包**（组件划分见 tools/dep-components.json）：
    #   numpy      → numpy, numpy.libs                （cv2/pandas 的公共底层）
    #   opencv     → cv2                              （视频码流）
    #   office     → openpyxl, docx, lxml, xlrd, olefile, pypdf（+ et-xmlfile / typing_extensions）
    #   qr         → qrcode, zfec, zxingcpp           （+ colorama）
    #   llm        → requests                         （+ urllib3 / certifi / idna / charset_normalizer）
    # 本清单同时是「插件依赖白名单」的一部分：出包工具 C-4 允许插件声明这些包，
    # 但要求目标机装了对应组件（缺则插件降级，后台/插件页面提示安装对应 <id> 依赖组件）。
    "cv2", "numpy",
    "openpyxl", "et_xmlfile", "docx", "lxml", "typing_extensions", "xlrd", "olefile", "pypdf",
    "qrcode", "colorama", "zfec", "zxingcpp",
    "requests", "urllib3", "certifi", "idna", "charset_normalizer",
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
    # 显式排除的包（2026-09-19 实测审计；见 docs/plan/主体与插件解耦-TODO.md T25 第 0 步）：
    #   pandas —— 体积 12.8 MB（压缩后 5.0 MB）却**无人使用**：本项目代码零 import，
    #             本机也无任何发行包依赖它；唯一引用是 openpyxl 的惰性分支
    #             `openpyxl/utils/dataframe.py: dataframe_to_rows()` 里的 `from pandas import Timestamp`，
    #             该函数全仓零调用。排除后出包体积直接下降。
    # ★ lxml 不在此列：它虽未被本项目直接 import，但**是 python-docx 的硬依赖**
    #   （python-docx 元数据声明 lxml>=3.1.0，docx 在 import 期即加载 lxml），
    #   排掉会打断 shared-docs / character-graph / info-transfer / knowledge-base。
    #   新增排除项前务必先用 `importlib.metadata.requires()` 做反向依赖核查。
    # 全部「由依赖组件提供」的包都显式排除：只从 PACKAGES 移除还不够，
    # PyInstaller 仍会以传递依赖把它们收集回来（实测 numpy 残留 21 MB），
    # 那会让"未装组件"场景被 _internal 里的副本掩盖，门控与提示全部失真。
    excludes=["pandas", "numpy", "cv2", "openpyxl", "et_xmlfile", "docx", "lxml", "typing_extensions",
              "xlrd", "olefile", "pypdf", "qrcode", "colorama", "zfec", "zxingcpp",
              "requests", "urllib3", "certifi", "idna", "charset_normalizer"],
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
    console=False,               # 打包运行：无控制台窗口，以系统托盘图标常驻后台
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
