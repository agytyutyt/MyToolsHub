# -*- coding: utf-8 -*-
r"""商店 catalog 链路探针（复现桌面端「商店目录加载失败」）。

链路 = 冻结主包(exe) ←能力层中继← 页面 fetch：
  1) 起真服务（deploy/服务端/JZToolsHub-v2.3.23/JZToolsHub.exe，隔离数据根）；
  2) 起源码态能力层（create_capability_app，指向真服务）；
  3) 经能力层 /api/login 登录（镜像会话 + 浏览器 jar 双份）；
  4) GET /tool/store（壳页）与 GET /plugin/store/api/catalog（页面 fetch 同款）——
     逐项打印 status / Content-Type / body 前 120 字节，定位 HTML 是谁回的。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "client"))

SERVER_PORT = 61791
CLIENT_PORT = 61792


def wait_http(url, tries=60):
    for _ in range(tries):
        time.sleep(0.5)
        try:
            urllib.request.urlopen(url, timeout=2)
            return True
        except Exception:
            continue
    return False


def main():
    work = tempfile.mkdtemp(prefix="store-probe-")
    try:
        # 1) 真服务
        app_dir = os.path.join(ROOT, "deploy", "服务端", "JZToolsHub-v2.3.23")
        exe = os.path.join(app_dir, "JZToolsHub.exe")
        env = dict(os.environ, JZTOOLS_DATA_ROOT=os.path.join(work, "srv-data"),
                   JZTOOLS_PORT=str(SERVER_PORT), JZTOOLS_HOST="127.0.0.1")
        srv = subprocess.Popen([exe], cwd=app_dir, env=env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        assert wait_http("http://127.0.0.1:%d/login" % SERVER_PORT), "服务未起"

        # 2) 能力层（源码态，指向真服务）
        import capability_app
        build_cfg = {"_program_dir": os.path.join(work, "D_program"),
                     "product": {"version": "0.0.1"}, "plugin_api": 1,
                     "paths": {"data_dirname": "D_client_data"}}
        os.makedirs(build_cfg["_program_dir"], exist_ok=True)
        os.environ["JZCLIENT_DATA_DIR"] = os.path.join(work, "clientdata")
        app = capability_app.create_capability_app(
            build_cfg, {"remote_address": "http://127.0.0.1:%d" % SERVER_PORT},
            "http://127.0.0.1:%d" % SERVER_PORT,
            os.path.join(ROOT, "static"), os.path.join(work, "lp"),
            os.path.join(ROOT, "client", "fallback.html"),
            data_dir=os.path.join(work, "cap-data"), log=lambda m: None)
        c = app.test_client()

        # 3) 登录（经中继）
        r = c.post("/api/login", json={"username": "admin", "password": "admin123"})
        print("login:", r.status_code, (r.get_json() or {}))

        # 4) 页面与接口
        for path in ("/tool/store", "/plugin/store/index.html",
                     "/plugin/store/api/catalog"):
            rr = c.get(path)
            ct = rr.headers.get("Content-Type", "")
            body = rr.get_data()[:120]
            print("%-34s -> %s | %s | %r" % (path, rr.status_code, ct[:40], body))
        return 0
    finally:
        try:
            srv.terminate()
        except Exception:
            pass
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
