# -*- coding: utf-8 -*-
"""V2「C 类引擎副本同源登记」补齐（2026-10-09）。

规则（设计文档 §5 V2）：client-plugins/ 下**与服务端逐字节相同**的拷贝文件，须在文件头
带同源登记（注明同源路径与独立演进口径）。本脚本：
  1) 对两个 C 类客户端工件逐文件比对服务端源：完全相同者加登记头（幂等，已有跳过）；
  2) 同步把登记头逻辑写入 trajectory-sketch 的生成脚本 adapt-ts-client.py（再生成不丢）；
  3) 部署版本递增（file-filter 1.6.0→1.6.1；trajectory-sketch 1.3.0→1.3.1，成对口径）。
"""
import io
import json
import os

ROOT = r"D:\JZToolsHub"

HEADER = ("# 同源副本登记（C 类客户端工件，2026-10-09）：本文件与 {src} 同源、\n"
          "# 独立演进（设计文档 §3.15/§3.16；规范 §16.15）。改动请只在本副本内进行。\n")


def add_headers(client_dir, server_dir, server_rel_prefix):
    added = skipped = 0
    for base, _dirs, files in os.walk(client_dir):
        for name in sorted(files):
            if not name.endswith(".py"):
                continue
            cp = os.path.join(base, name)
            rel = os.path.relpath(cp, client_dir).replace(os.sep, "/")
            sp = os.path.join(server_dir, rel)
            if not os.path.isfile(sp):
                continue                      # 客户端专有文件（垫片等）不加
            c = io.open(cp, encoding="utf-8", newline="").read()
            s = io.open(sp, encoding="utf-8", newline="").read()
            if c.startswith("# 同源副本登记"):
                skipped += 1
                continue
            if c != s:
                continue                      # 已适配改写的文件不加（仅逐字节同源者）
            head = HEADER.format(src="%s/%s" % (server_rel_prefix, rel))
            io.open(cp, "w", encoding="utf-8", newline="").write(head + c)
            added += 1
    print("  %s：加登记头 %d，已有 %d" % (os.path.basename(client_dir), added, skipped))


print("== 加盖同源登记 ==")
add_headers(os.path.join(ROOT, "client-plugins", "local-file-filter", "backend"),
            os.path.join(ROOT, "plugins", "file-filter", "backend"),
            "plugins/file-filter/backend")
add_headers(os.path.join(ROOT, "client-plugins", "local-trajectory-sketch", "backend"),
            os.path.join(ROOT, "plugins", "trajectory-sketch", "backend"),
            "plugins/trajectory-sketch/backend")

# 生成脚本同步：把登记步骤写进 adapt-ts-client.py（防再生成丢失）
ap = os.path.join(ROOT, "tools", "e2e", "adapt-ts-client.py")
s = io.open(ap, encoding="utf-8", newline="").read()
if "同源登记" not in s:
    old = 'print("adapt-ts-client：完成 + 编译通过")'
    new = '''# ---------------- 5) 同源登记头（V2：与服务端逐字节相同的拷贝须带登记，规范 §16.15） ----------------
_SRV = os.path.join(ROOT, "plugins", "trajectory-sketch", "backend")
_HDR = ("# 同源副本登记（C 类客户端工件，2026-10-09）：本文件与 {src} 同源、\\n"
        "# 独立演进（设计文档 §3.16；规范 §16.15）。改动请只在本副本内进行。\\n")
for _base, _dirs, _files in os.walk(DST):
    for _n in sorted(_files):
        if not _n.endswith(".py"):
            continue
        _cp = os.path.join(_base, _n)
        _rel = os.path.relpath(_cp, DST).replace(os.sep, "/")
        _sp = os.path.join(_SRV, _rel)
        if not os.path.isfile(_sp):
            continue
        _c = io.open(_cp, encoding="utf-8", newline="").read()
        if _c.startswith("# 同源副本登记"):
            continue
        if _c != io.open(_sp, encoding="utf-8", newline="").read():
            continue
        io.open(_cp, "w", encoding="utf-8", newline="").write(
            _HDR.format(src="plugins/trajectory-sketch/backend/" + _rel) + _c)
print("同源登记头：已按需补齐")

print("adapt-ts-client：完成 + 编译通过")'''
    assert s.count(old) == 1
    io.open(ap, "w", encoding="utf-8", newline="").write(s.replace(old, new, 1))
    print("adapt-ts-client.py：登记步骤已内置")

print("== 版本递增 ==")
for path, ov, nv in ((os.path.join(ROOT, "plugins", "file-filter", "manifest.json"), "1.6.0", "1.6.1"),
                     (os.path.join(ROOT, "client-plugins", "local-file-filter", "manifest.json"), "1.6.0", "1.6.1"),
                     (os.path.join(ROOT, "plugins", "trajectory-sketch", "manifest.json"), "1.3.0", "1.3.1"),
                     (os.path.join(ROOT, "client-plugins", "local-trajectory-sketch", "manifest.json"), "1.3.0", "1.3.1")):
    t = io.open(path, encoding="utf-8", newline="").read()
    old = '"version": "%s"' % ov
    assert t.count(old) == 1, path
    io.open(path, "w", encoding="utf-8", newline="").write(t.replace(old, '"version": "%s"' % nv, 1))
    print("  %s → %s" % (os.path.relpath(path, ROOT), nv))
print("v2-provenance：完成")
