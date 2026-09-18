#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""主包「组装」阶段：dist\JZToolsHub + 仓库源码 → deploy\<DeployName>\（增量合并）。

**为什么不用 build-deploy.ps1 全程跑**（详见 docs\guide\打包部署手册.md §0）：
  ① 它开头 `Remove-Item -Recurse` 删 dist/ build/ 部署目录 —— 本机工具会话的 safe-delete
     会把「批量删除 >50 项」拦成进程中断；
  ② 整包构建（PyInstaller 冷缓存 ~6 min + 组装 + 压缩 ~3 min）会撞工具 600 s 命令上限。
所以拆两段：PyInstaller 与压缩各自单跑，组装交给本脚本 —— **只合并、从不删库**。

与 build-deploy.ps1 的对应关系（行为逐一复刻，勿擅自改口径）：
  §3.1   dist\JZToolsHub\*（exe + _internal）
  §3.2   static/ plugins/ wheels/ tools/ + docs/README.md + docs/guide/ + docs/design/
         + README/HANDOFF/插件设计规范/移动端APP
         + config\tools.json + install.ps1 + 一键安装.bat + 一键卸载.bat
         复制与清理均按 tools\plugin-payload-rules.json（与「插件包」共用同一份真源）
         ★ docs 分层随包（2026-09-17）：只带「交付层 guide + 设计层 design + 索引」，
           内部层 docs/eval（一次性评估）、docs/plan（活清单）、docs/archive（历史留证）
           不随包 —— 口径与 build-deploy.ps1 §3.2 一致，改一处要两处一起改
  §3.2.1 模板自检：*.template.json >= 4，否则中止（模板被误删是历史事故）
  §3.3   logs/
  §3.4   version.json（UTF-8 **无 BOM**，Python json.load 遇 BOM 会报错）
  §4     start.bat（**GBK**，即 PowerShell 的 -Encoding Default）

用法：
    python tools\build-deploy-local.py                    # 版本：在既有部署目录基础上自动递增 patch
    python tools\build-deploy-local.py -Version 2.1.0     # 指定版本
    python tools\build-deploy-local.py --force            # 允许与上一版同号（只改了文档/工具时）
    python tools\build-deploy-local.py --check-only       # 只做前置检查 + 打印计划，不写任何文件
之后：powershell -ExecutionPolicy Bypass -File build-deploy.ps1 -ZipOnly
"""
import argparse
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY_BASELINE = "3.14"
TEMPLATE_MIN = 4

# §3.2 复制清单（顺序与 build-deploy.ps1 一致，勿随意增删）
# ★ docs 只列「随包层」：guide（交付手册）+ design（设计文档）+ 根索引
#   不随包：docs/eval、docs/plan、docs/archive（内部评估稿 / 活清单 / 历史留证）
COPY_DIRS = ["static", "plugins", "wheels", "tools", "docs/guide", "docs/design"]
COPY_FILES = ["install.ps1", "一键安装.bat", "一键卸载.bat", "docs/README.md",
              "README.md", "HANDOFF.md", "插件设计规范.md", "移动端APP.md"]

START_BAT = """@echo off
title JZToolsHub
echo 正在启动 JZToolsHub...（无窗口运行，托盘图标常驻右下角）
echo 浏览器访问 http://localhost:5000 ；退出服务请右键托盘图标选择「退出服务」
start "" /min JZToolsHub.exe
exit
"""


def load_rules():
    p = os.path.join(ROOT, "tools", "plugin-payload-rules.json")
    with open(p, encoding="utf-8") as f:
        r = json.load(f)
    return set(r["exclude_dirs"]), set(r["exclude_files"]), list(r["exclude_globs"])


def excluded(name, is_dir, rules):
    dirs, files, globs = rules
    if is_dir:
        return name in dirs
    if name in files:
        return True
    return any(fnmatch.fnmatch(name.lower(), g.lower()) for g in globs)


def merge_copy(src, dst, rules, stats, check_only, prefix=""):
    """递归合并复制（过滤运行态），语义等价 Copy-Item -Recurse -Force。"""
    for entry in sorted(os.scandir(src), key=lambda e: e.name):
        target = os.path.join(dst, entry.name)
        if entry.is_dir():
            if excluded(entry.name, True, rules):
                stats["skipped_dirs"].append(prefix + entry.name)
                continue
            if not check_only:
                os.makedirs(target, exist_ok=True)
            merge_copy(entry.path, target, rules, stats, check_only, prefix + entry.name + "/")
        else:
            if excluded(entry.name, False, rules):
                stats["skipped_files"].append(prefix + entry.name)
                continue
            if os.path.exists(target):
                stats["overwritten"] += 1
            else:
                stats["added"].append(prefix + entry.name)
                if not check_only:
                    os.makedirs(os.path.dirname(target), exist_ok=True)
            if not check_only:
                shutil.copy2(entry.path, target)


def cleanup_plugins(app, rules, stats, check_only):
    """§3.2 清理：对 plugins/ 再跑一遍同规则（兜底既有残留）。"""
    pdir = os.path.join(app, "plugins")
    if not os.path.isdir(pdir):
        return
    for cur, dirs, files in os.walk(pdir, topdown=True):
        keep = []
        for d in dirs:
            if excluded(d, True, rules):
                stats["cleaned"].append(os.path.relpath(os.path.join(cur, d), app))
                if not check_only:
                    shutil.rmtree(os.path.join(cur, d), ignore_errors=True)
            else:
                keep.append(d)
        dirs[:] = keep
        for f in files:
            if excluded(f, False, rules):
                stats["cleaned"].append(os.path.relpath(os.path.join(cur, f), app))
                if not check_only:
                    os.remove(os.path.join(cur, f))


def git_stamp():
    def run(*a):
        try:
            return subprocess.run(["git", "-C", ROOT, *a], capture_output=True, text=True).stdout.strip()
        except Exception:
            return ""
    commit = run("rev-parse", "--short", "HEAD") or "unknown"
    dirty = "-dirty" if run("status", "--porcelain") else ""
    return commit + dirty, bool(dirty)


def next_version(prev, explicit, force):
    """与 build-deploy.ps1 §82-91 同口径：未指定则递增 patch；同号需 --force。"""
    if not explicit:
        if re.match(r"^\d+\.\d+$", prev or ""):
            a, b = prev.split(".")
            explicit = "%s.%d" % (a, int(b) + 1)
        elif re.match(r"^\d+\.\d+\.\d+$", prev or ""):
            a, b, c = (prev or "0.0.0").split(".")
            explicit = "%s.%s.%d" % (a, b, int(c) + 1)
        else:
            explicit = "1.0.0"
        print("  [版本] 未指定 -Version，自动递增为 %s（上一版：%s）" % (explicit, prev or "无"))
    if prev and explicit == prev and not force:
        raise SystemExit("版本号与上一版相同（%s）→ 目标机模板同步不会触发。请指定更大的 -Version，"
                         "或（仅当只改了文档/工具时）加 --force 明确同号重出。" % explicit)
    if not re.match(r"^\d+\.\d+\.\d+$", explicit):
        print("  [警告] 版本号 %s 非三段式：插件包 min_app_version 校验会退化成「无法比较→跳过」。" % explicit)
    return explicit


def main():
    ap = argparse.ArgumentParser(description="主包组装（增量合并，不删库）")
    ap.add_argument("-Version", dest="version", default="", help="版本号（缺省在既有部署目录上递增 patch）")
    ap.add_argument("--force", action="store_true", help="允许与上一版同号重出（只改了文档/工具时）")
    ap.add_argument("--check-only", action="store_true", help="只做前置检查与差异报告，不写任何文件")
    ap.add_argument("-DeployName", dest="deploy_name", default="JZToolsHub", help="部署目录名")
    args = ap.parse_args()

    dist = os.path.join(ROOT, "dist", args.deploy_name)
    deploy = os.path.join(ROOT, "deploy")
    app = os.path.join(deploy, args.deploy_name)
    rules = load_rules()

    # ---- 前置检查 ----
    print("==> 前置检查")
    exe = os.path.join(dist, "JZToolsHub.exe")
    if not os.path.exists(exe):
        raise SystemExit("dist\\%s 不完整（缺 JZToolsHub.exe）。先跑 PyInstaller：\n"
                         "  python -m PyInstaller --noconfirm --clean --distpath dist --workpath build JZToolsHub.spec"
                         % args.deploy_name)
    pyver = "%d.%d.%d" % sys.version_info[:3]
    if not pyver.startswith(PY_BASELINE):
        print("  [警告] 当前解释器 %s 与基线 %s 不一致（version.json 会照实记录）" % (pyver, PY_BASELINE))
    commit, dirty = git_stamp()
    if dirty:
        print("  [警告] 工作区不干净 → version.json.commit 会记 %s。" % commit)
        print("         要干净戳：先 commit（含包内文档 README/HANDOFF），临时文件一律放仓库外。")
    prev = ""
    vf = os.path.join(app, "version.json")
    if os.path.exists(vf):
        try:
            prev = json.load(open(vf, encoding="utf-8-sig")).get("app", "")
        except Exception as e:
            print("  [警告] 读既有 version.json 失败：%s" % e)
    print("  dist: %s（exe %.1f MB）" % (dist, os.path.getsize(exe) / 1048576))
    print("  部署目录: %s（既有版本 %s）" % (app, prev or "无"))
    print("  commit: %s | Python: %s" % (commit, pyver))

    version = next_version(prev, args.version, args.force)
    if args.check_only:
        print("==> [--check-only] 以下为计划，不写任何文件")

    # ---- 组装 ----
    stats = {"added": [], "overwritten": 0, "skipped_dirs": [], "skipped_files": [], "cleaned": []}
    if not args.check_only:
        os.makedirs(app, exist_ok=True)
    merge_copy(dist, app, rules, stats, args.check_only)                      # §3.1
    for d in COPY_DIRS:                                                       # §3.2
        s = os.path.join(ROOT, d)
        if os.path.isdir(s):
            merge_copy(s, os.path.join(app, d), rules, stats, args.check_only)
    for f in COPY_FILES:
        s = os.path.join(ROOT, f)
        if not os.path.exists(s):
            continue
        t = os.path.join(app, f)
        if os.path.exists(t):
            stats["overwritten"] += 1
        else:
            stats["added"].append(f)
        if not args.check_only:
            shutil.copy2(s, t)
    if not args.check_only:
        os.makedirs(os.path.join(app, "config"), exist_ok=True)
        shutil.copy2(os.path.join(ROOT, "config", "tools.json"),
                     os.path.join(app, "config", "tools.json"))
    cleanup_plugins(app, rules, stats, args.check_only)

    # ---- §3.2.1 模板自检 ----
    tpl = []
    for cur, _d, files in os.walk(os.path.join(app, "plugins")):
        tpl += [f for f in files if f.endswith(".template.json")]
    if len(tpl) < TEMPLATE_MIN:
        raise SystemExit("模板自检失败：仅 %d 个 *.template.json（期望 >= %d）。"
                         "检查清理规则是否误删模板（模板不得命名为 config.json）。" % (len(tpl), TEMPLATE_MIN))

    # ---- §3.3 / §3.4 / §4 ----
    if not args.check_only:
        os.makedirs(os.path.join(app, "logs"), exist_ok=True)
        obj = {"app": version, "schema": 1, "commit": commit,
               "built_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
               "python": pyver, "offline": ""}
        with open(vf, "w", encoding="utf-8", newline="") as f:   # UTF-8 无 BOM
            f.write(json.dumps(obj, indent=4, ensure_ascii=False))
        with open(os.path.join(app, "start.bat"), "w", encoding="gbk", newline="\r\n") as f:
            f.write(START_BAT)

    total = sum(len(fs) for _c, _d, fs in os.walk(app)) if os.path.isdir(app) else 0
    print("==> %s完成" % ("计划" if args.check_only else "组装"))
    print("  版本 %s | 覆盖 %d | 新增 %d | 跳过运行态目录 %d | 清理 %d | 模板 %d | 现有文件 %d"
          % (version, stats["overwritten"], len(stats["added"]),
             len(stats["skipped_dirs"]), len(stats["cleaned"]), len(tpl), total))
    if stats["added"]:
        print("  新增文件：" + ", ".join(stats["added"][:10]) + ("…" if len(stats["added"]) > 10 else ""))
    if stats["cleaned"]:
        print("  清理（前 5）：" + ", ".join(stats["cleaned"][:5]))
    if not args.check_only:
        print("\n下一步：powershell -ExecutionPolicy Bypass -File build-deploy.ps1 -ZipOnly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
