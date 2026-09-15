#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""主包校验：结构 + 内容 + （可选）解压冒烟。出包后必跑。

用法：
    python tools\verify-package.py                          # 校验 deploy 下最新的 JZToolsHub-v*.zip
    python tools\verify-package.py deploy\JZToolsHub-v2.0.0.zip
    python tools\verify-package.py ... --smoke               # 再加「解压 → 隔离数据根启动 → HTTP 断言」

--smoke 会自动挑一个**空闲端口**（不碰本机 5000 上正在跑的服务），用
JZTOOLS_DATA_ROOT 指向临时目录（**不污染真实数据根** `%USERPROFILE%\.jztoolshub`），
断言首页 200 / 登录页 200 / 匿名 /api/* 401 后杀掉进程。冒烟目录默认保留（批量删除会撞
工具会话的 safe-delete），路径会在结尾打印。
"""
import argparse
import glob
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REQUIRED = ["JZToolsHub.exe", "start.bat", "一键安装.bat", "一键卸载.bat", "install.ps1",
            "config/tools.json", "version.json", "README.md", "HANDOFF.md",
            "插件设计规范.md", "移动端APP.md", "_internal/python3*"]
TEMPLATE_MIN = 4
BAD_IN_PLUGINS = re.compile(r"(__pycache__|/data/|/out/|\.task_cache/|/config\.json$|\.pyc$)")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def http(port, path, timeout=5):
    url = "http://127.0.0.1:%d%s" % (port, path)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception:
        return None, b""


def main():
    ap = argparse.ArgumentParser(description="主包校验")
    ap.add_argument("zipfile", nargs="?", default="", help="zip 路径（缺省取 deploy 下最新的 JZToolsHub-v*.zip）")
    ap.add_argument("--smoke", action="store_true", help="解压 + 隔离数据根启动 + HTTP 断言")
    args = ap.parse_args()

    path = args.zipfile
    if not path:
        cands = [p for p in glob.glob(os.path.join(ROOT, "deploy", "JZToolsHub-v*.zip"))]
        if not cands:
            raise SystemExit("deploy 下没有 JZToolsHub-v*.zip")
        path = max(cands, key=os.path.getmtime)
    path = os.path.abspath(path)
    print("校验目标：%s（%.1f MB）" % (path, os.path.getsize(path) / 1048576))

    fails = []

    def check(ok, label, detail=""):
        print("  %s %s%s" % ("[OK]" if ok else "[!!]", label, ("  — " + detail) if detail else ""))
        if not ok:
            fails.append(label)

    z = zipfile.ZipFile(path)
    names = z.namelist()
    files = [n for n in names if not n.endswith("/")]

    check(z.testzip() is None, "zip CRC 全量校验")
    check(len(files) > 2500, "文件数合理", "%d 文件 / %d 条目" % (len(files), len(names)))
    top = set(n.split("/")[0] for n in names)
    check("JZToolsHub" not in top and "dist" not in top,
          "无顶层目录前缀（解压即见 JZToolsHub.exe）", "顶层：%d 项" % len(top))

    for pat in REQUIRED:
        if "*" in pat:
            check(any(fnmatch_like(n, pat) for n in names), "存在 %s" % pat)
        else:
            check(pat in names, "存在 %s" % pat)
    if "_internal/python3" not in names and not any(n.startswith("_internal/python3") for n in names):
        check(False, "存在 _internal/python3*.dll")

    v = {}
    try:
        v = json.loads(z.read("version.json").decode("utf-8-sig"))
        check(True, "version.json 可解析（UTF-8 无 BOM）", json.dumps(v, ensure_ascii=False))
    except Exception as e:
        check(False, "version.json 可解析", str(e))
    for k in ("app", "schema", "commit", "built_at", "python", "offline"):
        check(k in v, "version.json 含字段 %s" % k)
    check(bool(re.match(r"^\d+\.\d+\.\d+$", str(v.get("app", "")))), "app 为三段式版本号", str(v.get("app")))
    check("-dirty" not in str(v.get("commit", "")), "commit 无 -dirty 干净戳", str(v.get("commit")))

    tpl = [n for n in files if n.endswith(".template.json")]
    check(len(tpl) >= TEMPLATE_MIN, "配置模板 >= %d" % TEMPLATE_MIN, "实际 %d：%s" % (len(tpl), ", ".join(tpl)))

    bad = [n for n in names if n.startswith("plugins/") and BAD_IN_PLUGINS.search(n)]
    check(not bad, "plugins/ 无运行态夹带（data/__pycache__/config.json/*.pyc）",
          ("夹带 %d 项：%s" % (len(bad), bad[:3])) if bad else "")

    try:
        bat = z.read("start.bat").decode("gbk")
        check("JZToolsHub.exe" in bat, "start.bat 为 GBK 且内容正确")
    except Exception as e:
        check(False, "start.bat 为 GBK", str(e))

    print("  [--] zip sha256 = %s" % hashlib.sha256(open(path, "rb").read()).hexdigest())

    if args.smoke:
        print("==> 解压冒烟（端口自动挑选，数据根隔离）")
        work = os.path.join(os.environ.get("TEMP", "."), "jz_verify_%d" % os.getpid())
        droot = os.path.join(work, "_dataroot")
        dest = os.path.join(work, "app")
        t0 = time.time()
        z.extractall(dest)
        print("  解压完成 %.1fs → %s" % (time.time() - t0, dest))
        port = free_port()
        env = dict(os.environ, JZTOOLS_DATA_ROOT=droot, JZTOOLS_PORT=str(port), JZTOOLS_HOST="127.0.0.1")
        log = open(os.path.join(work, "smoke.log"), "wb")
        proc = subprocess.Popen([os.path.join(dest, "JZToolsHub.exe")], cwd=dest, env=env,
                               stdout=log, stderr=subprocess.STDOUT)
        up = None
        for i in range(30):
            code, _ = http(port, "/")
            if code == 200:
                up = i * 2
                break
            if proc.poll() is not None:
                break
            time.sleep(2)
        check(up is not None, "服务启动并响应首页 200", "%s 秒（端口 %d）" % (up, port) if up is not None else "启动失败/超时")
        if up is not None:
            check(http(port, "/login")[0] == 200, "登录页 200")
            check(http(port, "/api/tools")[0] == 401, "匿名 /api/tools 401（强制登录生效）")
            check(os.path.isdir(droot), "隔离数据根已生成", droot)
            check(os.path.exists(os.path.join(droot, "config", "admin.json")), "数据根 config/admin.json 已生成")
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
        log.close()
        data = open(os.path.join(work, "smoke.log"), "rb").read()
        if data:
            print("  ---- 进程日志尾部 ----")
            print(data.decode("utf-8", "replace")[-800:])
        print("  冒烟目录（可手工删除）：%s" % work)

    print()
    if fails:
        print("校验失败：%d 项 —— %s" % (len(fails), "；".join(fails)))
        return 1
    print("校验通过：全部检查项绿。")
    return 0


def fnmatch_like(name, pat):
    import fnmatch
    return fnmatch.fnmatch(name, pat)


if __name__ == "__main__":
    sys.exit(main())
