#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""清除插件侧历史明文 LLM 配置（独立脚本，2026-10-16）。

背景
----
统一大模型落地**之前**，`case-report` / `character-graph` / `file-filter` 三个插件各在
``<数据根>/plugins/<id>/config.json`` 里存过一份 ``llm{base_url,api_key,model}``，**Key 是明文**。
统一后这份副本已无读取方（`jz_llm.resolve()` 的 `plugin_id` 不参与解析；兼容桥 2026-09-23 移除），
留着既是明文泄漏面，也让「Key 到底存在哪」说不清。

主体启动曾内置一次性迁移 `migrate_plugin_legacy_llm()` 负责「先收编、再清除」。该项目**尚未正式
上线**、不存在需要清理的存量数据根，故该迁移已于 **2026-10-16 从运行时删除**（销 C1b）。
本脚本把「清除明文副本」这项能力**独立保留**下来，用于日后在任意数据根（含**备份、离线机器、
历史镜像**）上做同样的清理——不依赖项目环境，也不受运行时删改影响。

用法
----
    python tools\maintenance\purge-legacy-llm-config.py                     # 只扫描并报告（默认 dry-run）
    python tools\maintenance\purge-legacy-llm-config.py --apply             # 清除（逐文件先备份）
    python tools\maintenance\purge-legacy-llm-config.py --root D:\data      # 指定数据根
    python tools\maintenance\purge-legacy-llm-config.py --emit keys.json    # 导出发现的接入信息（**含明文**）
    python tools\maintenance\purge-legacy-llm-config.py --root D:\data --apply --emit keys.json

约定与边界
----------
* **默认 dry-run**——不加 ``--apply`` 绝不写任何文件。
* ``--apply`` 时每个被改动的文件先**原样备份**为 ``<原文件>.bak-YYYYmmddHHMMSS``，再删除 ``llm`` 段；
  其余字段与顺序无关地原样保留，写盘口径对齐应用的 ``jztools_data.atomic_write_json``
  （utf-8 无 BOM / ``ensure_ascii=False`` / ``indent=2``）。
* **不做「收编」**：把明文 Key 搬进 ``<数据根>/config/llm.json`` 并加密落盘需要本机密钥与
  ``jz_llm``，本脚本刻意不 import 应用代码。需要保留接入信息时用 ``--emit`` 导出（明文，自行妥善
  保管），再到应用里重新录入；导出文件用完请删除。

退出码
------
    0  干净（无发现），或 ``--apply`` 全部成功
    1  dry-run 有发现（需处理）
    2  执行失败（有文件清除失败）
"""

import argparse
import io
import json
import os
import shutil
import sys
from datetime import datetime

# 输出流按 UTF-8 走：正文/帮助里有「↔」这类非 GBK 字符，Windows 控制台默认代码页直接 print
# 会抛 UnicodeEncodeError；被出包/巡检脚本接走时会把一次本该通过的检查误判成红灯。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:      # 非标准流（被重定向/捕获）时静默跳过
        pass

MASK = "••••••••"


def default_root():
    """按应用的解析口径猜默认数据根：环境变量 > 用户目录默认位置。"""
    env = os.environ.get("JZTOOLS_DATA_ROOT")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    return os.path.join(os.path.expanduser("~"), ".jztoolshub")


def find_configs(root):
    """递归找出 <root>/plugins 下所有 config.json（含嵌套布局，如 <id>/backend/config.json）。"""
    plugins = os.path.join(root, "plugins")
    found = []
    if not os.path.isdir(plugins):
        return found
    for dirpath, _dirnames, filenames in os.walk(plugins):
        for name in filenames:
            if name == "config.json":
                found.append(os.path.join(dirpath, name))
    return sorted(found)


def load_hits(paths):
    """返回 [(path, data, llm)]：只含确实带 `llm` 段的文件；不可读/非法的计入 bad 由调用方报告。"""
    hits, bad = [], []
    for path in paths:
        try:
            with io.open(path, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
        except Exception as e:
            bad.append((path, str(e)))
            continue
        llm = data.get("llm") if isinstance(data, dict) else None
        if isinstance(llm, dict):
            hits.append((path, data, llm))
    return hits, bad


def atomic_write_json(path, obj):
    """对齐 jztools_data.atomic_write_json：utf-8 无 BOM / ensure_ascii=False / indent=2。"""
    text = json.dumps(obj, ensure_ascii=False, indent=2)
    tmp = path + ".tmp-%d" % os.getpid()
    with io.open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser(
        description="清除插件侧历史明文 LLM 配置（独立脚本，默认 dry-run）")
    ap.add_argument("--root", default=None,
                    help="数据根目录（默认为 %%JZTOOLS_DATA_ROOT%% 或 ~/.jztoolshub）")
    ap.add_argument("--apply", action="store_true",
                    help="真正执行清除（默认只扫描报告）；改动前逐文件 .bak 备份")
    ap.add_argument("--emit", default=None, metavar="FILE",
                    help="把发现的接入信息导出到 FILE（**含明文 Key**，用完请删除）")
    ap.add_argument("--quiet", action="store_true", help="只打印结论行")
    args = ap.parse_args()

    root = os.path.abspath(args.root or default_root())
    print("数据根：%s" % root)
    print("模式：%s" % ("APPLY（会写盘）" if args.apply else "DRY-RUN（只读）"))

    paths = find_configs(root)
    if not paths:
        print("未找到任何 <数据根>/plugins/**/config.json —— 无需处理。")
        return 0

    hits, bad = load_hits(paths)
    for path, err in bad:
        print("  [跳过] 不可解析：%s（%s）" % (os.path.relpath(path, root), err))

    if not hits:
        print("扫描 %d 个 config.json，**均无历史 llm 段** —— 无需处理。" % len(paths))
        return 0

    print("")
    print("发现 %d 处历史明文 llm 段（共扫描 %d 个 config.json）：" % (len(hits), len(paths)))
    for path, _data, llm in hits:
        rel = os.path.relpath(path, root)
        key = (llm.get("api_key") or "").strip()
        url = str(llm.get("url") or llm.get("base_url") or "").strip()
        model = str(llm.get("model") or "").strip()
        print("  - %s" % rel)
        print("      api_key=%s  model=%s" % (MASK if key else "(空)", model or "(空)"))
        print("      url=%s" % (url or "(空)"))

    if args.emit:
        payload = {"purged_at": datetime.now().isoformat(timespec="seconds"),
                   "data_root": root,
                   "entries": [{"path": os.path.relpath(p, root), "llm": llm}
                               for p, _d, llm in hits]}
        with io.open(args.emit, "w", encoding="utf-8", newline="") as f:
            f.write(json.dumps(payload, ensure_ascii=False, indent=2))
        print("")
        print("⚠ 已导出到 %s —— **该文件含明文 API Key**，录入到应用后请立即删除。" % args.emit)

    if not args.apply:
        print("")
        print("这是 dry-run。确认无误后加 --apply 执行清除。")
        return 1

    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    cleared, failed = [], []
    for path, data, _llm in hits:
        backup = "%s.bak-%s" % (path, stamp)
        try:
            shutil.copy2(path, backup)
        except OSError as e:
            failed.append("%s（备份失败：%s）" % (os.path.relpath(path, root), e.strerror or e))
            continue
        data.pop("llm", None)
        try:
            atomic_write_json(path, data)
            cleared.append(os.path.relpath(path, root))
        except OSError as e:
            failed.append("%s（写入失败：%s）" % (os.path.relpath(path, root), e.strerror or e))

    print("")
    print("==== 汇总：清除 %d 处 / 失败 %d 处 ====" % (len(cleared), len(failed)))
    for rel in cleared:
        print("  [已清除] %s   （备份 %s.bak-%s）" % (rel, rel, stamp))
    for msg in failed:
        print("  [失败]   %s" % msg)
    if len(cleared):
        print("")
        print("备份文件保留在各自目录，确认无误后可自行删除。")
    return 2 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
