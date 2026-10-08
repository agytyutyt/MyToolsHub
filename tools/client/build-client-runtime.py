# -*- coding: utf-8 -*-
r"""客户端「内置 Python 运行时」分件（T07/T20 遗留销项，2026-10-08）。

背景：打包态插件 worker（GD4 子进程）需要独立解释器。能力层 exe 是 PyInstaller 冻结程序，
不能当脚本解释器用（实测 `YFJZ_Client.exe plugin_host.py …` 会再走一次客户端启动并撞单实例
互斥 →「已有实例在运行，本实例退出」），而 client_config.runtime_python_exe 在未配
paths.runtime_python_dir 时回落 sys.executable（该 exe）→ B-heavy / C 类本地后端在打包态
起不来（页面能出、走后端的操作 502）。本脚本产出 runtime/python/（官方 embeddable + Flask 栈）。

用法：
  python tools/client/build-client-runtime.py [--emb-zip <zip>] [--out <dir>] [--zip]

流程：
  ①源：--emb-zip > 缓存 .workbuddy/cache/python-<ver>-embed-amd64.zip > python.org 下载
    （版本 = 构建机解释器版本；须与客户端冻结运行时同 minor：cp314）
  ②解包到 <out>（缺省 deploy/桌面端/<product>/runtime/python；同名旧目录先清）
  ③补 Flask 栈：flask/werkzeug/jinja2/itsdangerous/click/markupsafe/blinker 按
    Requires-Dist 递归闭包，从构建机 site-packages 复制（版本与主包/客户端清单一致，离线可靠）
  ④写 python3XX._pth：python3XX.zip / . / Lib\site-packages；**不启用 site**——
    worker 环境确定化（不吃用户态 site-packages）；共享 pylibs 由执行器 --pylibs 注入
  ⑤自检：<python.exe> -c "import flask, werkzeug, jinja2, …"（失败即红）
  ⑥--zip：产出可分发的分件 deploy/桌面端/运行时/JZClient-运行时-python<ver>-v1.zip
    （zip 内根为 python/，解包进 runtime/ 即得 runtime/python/）

分件形态与 webview2 内核一致：程序包更新不重下运行时；运行时可在多版本客户端间复用。
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
CFG = os.path.join(HERE, "client-build-config.json")
CACHE_DIR = os.path.join(ROOT, ".workbuddy", "cache")
FLASK_ROOTS = ("flask", "werkzeug", "jinja2", "itsdangerous", "click", "markupsafe", "blinker")


def py_ver():
    v = sys.version_info
    return "%d.%d.%d" % (v.major, v.minor, v.micro), "python%d%d" % (v.major, v.minor)


def fetch_emb(ver, override):
    if override:
        if not os.path.isfile(override):
            print("--emb-zip 不存在：%s" % override)
            sys.exit(1)
        return override
    cache = os.path.join(CACHE_DIR, "python-%s-embed-amd64.zip" % ver)
    if os.path.isfile(cache):
        print("使用缓存：%s" % os.path.relpath(cache, ROOT))
        return cache
    os.makedirs(CACHE_DIR, exist_ok=True)
    url = "https://www.python.org/ftp/python/%s/python-%s-embed-amd64.zip" % (ver, ver)
    print("下载：%s" % url)
    urllib.request.urlretrieve(url, cache)
    return cache


def need_req(req):
    """该 Requires-Dist 在当前解释器上是否必装：marker 求值（extra 恒为空 → extra 专属跳过）。"""
    if ";" not in req:
        return True
    marker = req.split(";", 1)[1].strip()
    try:
        from packaging.markers import Marker, default_environment
        env = default_environment()
        env["extra"] = ""
        return bool(Marker(marker).evaluate(env))
    except Exception:
        return not any(k in marker for k in ("extra", "python_version", "sys_platform", "platform_"))


def dist_closure(roots):
    """按 Requires-Dist 递归闭包（marker 求值过滤）；缺包即报错——清单不完整要红灯。"""
    from importlib import metadata
    seen, dists = set(), []

    def walk(name):
        try:
            dist = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            print("构建机缺包：%s" % name)
            sys.exit(1)
        key = (dist.metadata["Name"] or name).lower()
        if key in seen:
            return
        seen.add(key)
        dists.append(dist)
        for req in (dist.requires or []):
            if not need_req(req):
                continue
            dep = re.split(r"[\s\[\]<>!=~;(]", req.strip(), maxsplit=1)[0]
            if dep:
                walk(dep)

    for r in roots:
        walk(r)
    return dists


def copy_dists(dists, site_dir):
    names = []
    for dist in dists:
        n = 0
        for f in (dist.files or []):
            rel = str(f).replace("/", os.sep)
            if rel.startswith("..") or rel.endswith(".pyc"):   # 脚本/上级目录条目不要
                continue
            src = str(dist.locate_file(f))
            if not os.path.isfile(src):
                continue
            dst = os.path.join(site_dir, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            n += 1
        names.append("%s %s" % (dist.metadata["Name"], dist.version))
    return names


def dir_size(path):
    total = 0
    for base, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(base, f))
            except OSError:
                pass
    return total


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--emb-zip", default="", help="离线 embeddable zip（缺省走缓存/官方下载）")
    ap.add_argument("--out", default="", help="输出目录（缺省 deploy/桌面端/<product>/runtime/python）")
    ap.add_argument("--zip", action="store_true", dest="make_zip",
                    help="同时产出可分发的运行时 zip（deploy/桌面端/运行时/）")
    args = ap.parse_args()

    with open(CFG, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    product = cfg["product"]["name"]
    ver, tag = py_ver()
    out = args.out or os.path.join(ROOT, "deploy", "桌面端", product, "runtime", "python")
    emb = fetch_emb(ver, args.emb_zip)

    print("[1/4] 解包 embeddable（%s）→ %s" % (os.path.basename(emb), out))
    if os.path.isdir(out):
        shutil.rmtree(out)
    os.makedirs(out, exist_ok=True)
    with zipfile.ZipFile(emb) as zf:
        zf.extractall(out)

    print("[2/4] 补 Flask 栈（Requires-Dist 闭包，自构建机 site-packages 复制）")
    dists = dist_closure(FLASK_ROOTS)
    site_dir = os.path.join(out, "Lib", "site-packages")
    os.makedirs(site_dir, exist_ok=True)
    names = copy_dists(dists, site_dir)
    print("      " + "、".join(names))

    print("[3/4] 写 %s._pth（不启用 site：worker 环境确定化）" % tag)
    pth = os.path.join(out, "%s._pth" % tag)
    with open(pth, "w", encoding="utf-8", newline="\r\n") as f:
        f.write("\n".join([
            "%s.zip" % tag,
            ".",
            r"Lib\site-packages",
            "# import site   # 不开：确定性隔离（共享 pylibs 由执行器 --pylibs 注入）",
        ]) + "\n")
    with open(os.path.join(out, "README-运行时.txt"), "w", encoding="utf-8", newline="\r\n") as f:
        f.write("JZClient 内置 Python 运行时（插件 worker 专用，分件）\n"
                "来源：https://www.python.org/ftp/python/%s/python-%s-embed-amd64.zip\n"
                "组件：%s\n"
                "生成：tools/client/build-client-runtime.py（%s）\n"
                "用途：能力层经构建配置 paths.runtime_python_dir 拉起插件 worker 子进程\n"
                % (ver, ver, "、".join(names), time.strftime("%Y-%m-%d %H:%M:%S")))

    print("[4/4] 自检")
    py = os.path.join(out, "python.exe")
    r = subprocess.run([py, "-c",
                        "import flask, werkzeug, jinja2, itsdangerous, click, markupsafe, blinker;"
                        "import importlib.metadata as m;"
                        "from werkzeug.test import EnvironBuilder;"
                        "app = flask.Flask('probe');"
                        "print('flask', m.version('flask'), '/ werkzeug', m.version('werkzeug'))"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        print("自检失败：%s" % ((r.stderr or "")[-500:]))
        return 1
    print("      " + (r.stdout or "").strip())
    print("完成：%s（%.1f MB）" % (out, dir_size(out) / 1048576.0))

    if args.make_zip:
        zdir = os.path.join(ROOT, "deploy", "桌面端", "运行时")
        os.makedirs(zdir, exist_ok=True)
        zpath = os.path.join(zdir, "JZClient-运行时-python%s-v1.zip" % ver)
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as zf:
            for base, _dirs, files in os.walk(out):
                for fn in files:
                    full = os.path.join(base, fn)
                    arc = os.path.join("python", os.path.relpath(full, out)).replace("\\", "/")
                    zf.write(full, arc)
        print("分件：%s（%.1f MB）" % (zpath, os.path.getsize(zpath) / 1048576.0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
