# -*- coding: utf-8 -*-
"""包命名族改版（方案 A，2026-10-08）：JZToolsHub-<id>-后端/前端-v…（同插件两包排序相邻）。

改动面：出包脚本命名 / 商店命名表 / 批量升级解析器（兼容旧名）/ 插件集与验收过滤器 /
batch8 台架期望 / 商店设计文档命名族小节。
"""
import io
import py_compile

# ── 1) build-plugin-package.ps1 ──
p = "tools/build-plugin-package.ps1"
s = io.open(p, encoding="utf-8").read()
old = '$ToolIds   = @("JZToolsHub-插件")'
new = '# 命名族（2026-10-08 改版）：JZToolsHub-<id>-后端-v<版本>.zip —— id 前置使前后端包排序相邻\n$NameTail  = "后端"'
assert old in s; s = s.replace(old, new, 1)
old = '$zipName = "$($ToolIds[0])-$Id-v$Version.zip"'
new = '$zipName = "JZToolsHub-$Id-$NameTail-v$Version.zip"'
assert old in s; s = s.replace(old, new, 1)
old = '$frontendZipName = "JZToolsHub-前端-$Id-v$Version.zip"'
new = '$frontendZipName = "JZToolsHub-$Id-前端-v$Version.zip"'
assert old in s; s = s.replace(old, new, 1)
s = s.replace("# JZToolsHub-前端-<id>-v<版本>.zip = frontend/ 静态文件 + frontend-manifest.json（文件清单）。",
              "# JZToolsHub-<id>-前端-v<版本>.zip = frontend/ 静态文件 + frontend-manifest.json（文件清单）。", 1)
io.open(p, "w", encoding="utf-8", newline="").write(s)
print("build-plugin-package.ps1 ok")

# ── 2) store_admin.py 命名表 ──
p = "plugins/admin/backend/store_admin.py"
s = io.open(p, encoding="utf-8").read()
old = '''    "server": "JZToolsHub-插件-{id}-v{version}.zip",
    "server_frontend": "JZToolsHub-前端-{id}-v{version}.zip",'''
new = '''    "server": "JZToolsHub-{id}-后端-v{version}.zip",
    "server_frontend": "JZToolsHub-{id}-前端-v{version}.zip",'''
assert old in s; s = s.replace(old, new, 1)
s = s.replace("# 包文件命名族（设计文档 §4.1 注）：上架落 packages\\ 时统一改名，消费方按名辨形态",
              "# 包文件命名族（设计文档 §4.1 注）：上架落 packages\\ 时统一改名，消费方按名辨形态。\n"
              "# 2026-10-08 改版（方案 A）：id 前置到「后端/前端」之前——同插件两包按名排序相邻；", 1)
io.open(p, "w", encoding="utf-8", newline="").write(s)
py_compile.compile(p, doraise=True)
print("store_admin.py ok")

# ── 3) plugin_admin.py 文件名解析：兼容新旧 ──
p = "plugins/admin/backend/plugin_admin.py"
s = io.open(p, encoding="utf-8").read()
old = 'PKG_PLUGIN_RE = re.compile(r"^JZToolsHub-插件-(?P<id>[A-Za-z0-9._-]+)-v(?P<ver>\\d+(?:\\.\\d+)*)\\.zip$")'
new = ('PKG_PLUGIN_RE = re.compile(r"^JZToolsHub-(?:(?P<id0>[A-Za-z0-9._-]+)-后端|插件-(?P<id1>[A-Za-z0-9._-]+))'
       '-v(?P<ver>\\d+(?:\\.\\d+)*)\\.zip$")   # 兼容旧名（插件-<id>）与新名（<id>-后端）')
assert old in s, "PKG_PLUGIN_RE 锚点"
s = s.replace(old, new, 1)
io.open(p, "w", encoding="utf-8", newline="").write(s)
py_compile.compile(p, doraise=True)
print("plugin_admin.py ok")
