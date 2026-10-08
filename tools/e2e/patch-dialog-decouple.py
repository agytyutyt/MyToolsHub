# -*- coding: utf-8 -*-
"""对话框解耦切换（2026-10-08）：两页改用框架级 JZDialog（样式随脚本注入，随用随调）。

- store/index.html / admin-store.html：加 jz-dialog.js 引用
- store.js / admin-store.js：删本地实现，改别名到 JZDialog.open（调用点零改动）
- admin-store.html：删本页 .st-dlg/.st-modal 手提样式（由 JZDialog 自带）
- 版本戳：store.js v10 / admin-store.js v7
"""
import io

# ── A. store/index.html ──
p = "plugins/store/frontend/index.html"
s = io.open(p, encoding="utf-8").read()
old = '<script src="/plugin/store/store.js?v=9"></script>'
assert old in s
s = s.replace(old, '<script src="/static/js/jz-dialog.js?v=1"></script>\n'
                   '<script src="/plugin/store/store.js?v=10"></script>', 1)
io.open(p, "w", encoding="utf-8", newline="").write(s)
print("store index ok")

# ── B. store.js：本地 stDialog → JZDialog 别名 ──
p = "plugins/store/frontend/store.js"
s = io.open(p, encoding="utf-8").read()
start = s.find("  // ---------- 自绘对话框（替换原生 confirm/prompt")
end = s.find("  function fmtSize(n) {")
assert start >= 0 and end > start, "store stDialog 段定位"
s = s[:start] + ("  // 自绘对话框 = 框架级组件（static/js/jz-dialog.js，样式随脚本注入，随用随调；2026-10-08 解耦）\n"
                 "  var stDialog = function (opts) { return window.JZDialog.open(opts); };\n\n") + s[end:]
io.open(p, "w", encoding="utf-8", newline="").write(s)
print("store.js ok")

# ── C. admin-store.html：加引用 + 删手提样式块 ──
p = "plugins/admin/frontend/admin-store.html"
s = io.open(p, encoding="utf-8").read()
old = '<script src="/plugin/admin/js/admin-store.js?v=6"></script>'
assert old in s
s = s.replace(old, '<script src="/static/js/jz-dialog.js?v=1"></script>\n'
                   '<script src="/plugin/admin/js/admin-store.js?v=7"></script>', 1)
import re
m = re.search(r"    /\* 自绘对话框（替换原生 confirm/prompt；与商店页同款）。\n(?:.*\n)*?    \.st-dlg-danger:hover\{background:rgba\(197,34,31,\.18\)\}\n\n", s)
assert m, "admin 手提样式块定位"
s = s[:m.start()] + s[m.end():]
io.open(p, "w", encoding="utf-8", newline="").write(s)
print("admin-store.html ok")

# ── D. admin-store.js：本地 adDialog → JZDialog 别名 ──
p = "plugins/admin/frontend/js/admin-store.js"
s = io.open(p, encoding="utf-8").read()
start = s.find("  // 自绘对话框（替换原生 confirm/prompt；与商店页同款交互")
end = s.find("  function chip(text, kind) {")
assert start >= 0 and end > start, "admin adDialog 段定位"
s = s[:start] + ("  // 自绘对话框 = 框架级组件（static/js/jz-dialog.js；2026-10-08 解耦，样式随脚本注入）\n"
                 "  var adDialog = function (opts) { return window.JZDialog.open(opts); };\n\n") + s[end:]
io.open(p, "w", encoding="utf-8", newline="").write(s)
print("admin-store.js ok")
