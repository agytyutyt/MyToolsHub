# -*- coding: utf-8 -*-
"""安装进度回调台架（2026-10-08）：pkg_install 三个安装入口的 progress 回调。

自制最小 zip（依赖组件包 / 本地插件包 / server 前端包），装进临时目录，断言：
安装成功 + 回调次数 = 解压文件数 + 计数单调 + 终值 = 总数；另证「回调抛异常不打断安装」。
服务端 _InstallProgress 台账折算由能力层同进程写，页面对接见 store.js 的 setProg。
"""
import json
import os
import shutil
import sys
import tempfile
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "client"))
sys.path.insert(0, ROOT)                       # jz_deps（组件包立即应用用）在仓库根

import pkg_install  # noqa: E402

FAILS = []
TOTAL = []


def check(name, cond, extra=""):
    TOTAL.append(name)
    print(("ok   " if cond else "FAIL ") + name + (("   " + str(extra)) if extra else ""))
    if not cond:
        FAILS.append(name)


def mkzip(path, files):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)


def collector():
    calls = []

    def cb(done, total):
        calls.append((done, total))

    return calls, cb


def monotone(calls):
    return all(calls[i][0] <= calls[i + 1][0] for i in range(len(calls) - 1))


tmp = tempfile.mkdtemp(prefix="jz-install-prog-")
try:
    # ---- ① 依赖组件包（payload/pylibs；manifest.json 不算进度） ----
    zp = os.path.join(tmp, "dep.zip")
    mkzip(zp, {
        "manifest.json": json.dumps({"components": [{
            "id": "demo-comp", "version": "1.0", "files": ["a.py", "sub/b.py", "c.py"]}]}),
        "payload/pylibs/a.py": "A = 1\n",
        "payload/pylibs/sub/b.py": "B = 1\n",
        "payload/pylibs/c.py": "C = 1\n",
        "payload/pylibs/manifest.json": "{}",
    })
    prog_dir = os.path.join(tmp, "program")
    os.makedirs(prog_dir)
    calls, cb = collector()
    res = pkg_install.install_component_zip(zp, prog_dir, progress=cb)
    check("组件包安装 ok", res.get("ok"), res)
    check("组件包回调 3 次", len(calls) == 3, calls)
    check("组件包终值 (3,3)", bool(calls) and calls[-1] == (3, 3), calls)
    check("组件包单调", monotone(calls), calls)

    calls2, cb2 = collector()
    pkg_install.install_component_zip(zp, prog_dir, progress=cb2)
    check("组件包重复装（覆盖替换）仍上报", len(calls2) == 3, calls2)

    def bad(done, total):
        raise RuntimeError("boom")

    res = pkg_install.install_component_zip(zp, prog_dir, progress=bad)
    check("回调抛异常不打断安装", res.get("ok"), res)

    # ---- ② 本地插件包（zip 根即插件目录） ----
    zp2 = os.path.join(tmp, "local.zip")
    mkzip(zp2, {
        "manifest.json": json.dumps({"id": "local-demo", "runtime": "local", "kind": "local",
                                     "entry": {"frontend": "index.html"}}),
        "frontend/index.html": "<html></html>",
        "frontend/js/app.js": "1;\n",
    })
    lp = os.path.join(tmp, "local-plugins")
    calls3, cb3 = collector()
    res = pkg_install.install_local_zip(zp2, lp, [], None, progress=cb3)
    check("本地插件包安装 ok", res.get("ok"), res)
    check("本地插件包回调 3 次（manifest + 2 前端文件）", len(calls3) == 3, calls3)
    check("本地插件包终值 (3,3)", bool(calls3) and calls3[-1] == (3, 3), calls3)

    # ---- ③ server 前端包（frontend/ 载荷） ----
    zp3 = os.path.join(tmp, "fe.zip")
    mkzip(zp3, {
        "frontend-manifest.json": json.dumps({"id": "store-demo", "version": "1.0",
                                              "files": ["index.html", "a.js"]}),
        "frontend/index.html": "<html></html>",
        "frontend/a.js": "1;\n",
        "README.md": "不在 frontend/ 下，不计进度",
    })
    fd = os.path.join(tmp, "frontends")
    calls4, cb4 = collector()
    res = pkg_install.install_frontend_zip(zp3, fd, progress=cb4)
    check("前端包安装 ok", res.get("ok"), res)
    check("前端包回调 2 次", len(calls4) == 2, calls4)
    check("前端包终值 (2,2)", bool(calls4) and calls4[-1] == (2, 2), calls4)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print("-" * 46)
print("install-progress：%d/%d 项通过，%d 项失败"
      % (len(TOTAL) - len(FAILS), len(TOTAL), len(FAILS)))
if FAILS:
    print("失败项：" + "、".join(FAILS))
sys.exit(1 if FAILS else 0)
