# -*- coding: utf-8 -*-
r"""F1 身份注入台架（批次 9 T-O9-4）。

验证链路：
  1) 未登录：hybrid（provider 链）与 worker（X-JZ-User 头链）均按匿名降级（user=None）；
  2) 登录（POST /api/login 经中继，stub 发会话 Cookie）→ 能力层缓存身份 →
     hybrid 经 jz_api provider、worker 经派发链注入头，两者都拿到真实身份；
  3) 登出（POST /api/logout → 401 统一失效信号）→ 身份立即作废回匿名；
  4) jz_api 直测：provider 优先；无 provider 无请求上下文 → None。

用法：python tools/e2e/identity-inject-tests.py   （退出码 0=全过）
"""
import json
import os
import shutil
import sys
import tempfile
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "client"))

from werkzeug.serving import make_server

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append(ok)
    print("  %s %s%s" % ("✅" if ok else "❌", name, (" ｜ " + str(detail)) if detail and not ok else ""))


BACKEND_SRC = '''import jz_api
from flask import jsonify


def register(app):
    @app.get("/ping")
    def idprobe_ping():
        return jsonify({"user": jz_api.get_session_user()})
'''


def make_plugin(lp_dir, pid, kind):
    d = os.path.join(lp_dir, pid)
    os.makedirs(os.path.join(d, "backend"), exist_ok=True)
    os.makedirs(os.path.join(d, "frontend"), exist_ok=True)
    entry = {"frontend": "index.html", "backend": "routes.py"}
    m = {"id": pid, "name": pid, "icon": "🧪", "version": "1.0.0",
         "runtime": "local", "kind": kind, "entry": entry}
    if kind == "hybrid":
        m["server_data"] = True
        m["entry"]["mount"] = "/api/local/" + pid
    with open(os.path.join(d, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False)
    with open(os.path.join(d, "backend", "__init__.py"), "w", encoding="utf-8") as f:
        f.write("")
    with open(os.path.join(d, "backend", "routes.py"), "w", encoding="utf-8") as f:
        f.write(BACKEND_SRC)
    with open(os.path.join(d, "frontend", "index.html"), "w", encoding="utf-8") as f:
        f.write("<html>idprobe</html>")


def main():
    work = tempfile.mkdtemp(prefix="idprobe-")
    os.environ["JZCLIENT_DATA_DIR"] = os.path.join(work, "clientdata")  # 官方测试隔离口：停用态不落真实用户目录
    try:
        from flask import Flask, jsonify, request

        upstream = Flask("stub-upstream")

        @upstream.post("/api/login")
        def _login():
            resp = jsonify({"ok": True})
            resp.headers["Set-Cookie"] = "sid=admin1; Path=/"
            return resp

        @upstream.get("/api/session")
        def _session():
            print("    [stub] /api/session Cookie=%r" % (request.headers.get("Cookie"),))
            if "sid=" in (request.headers.get("Cookie") or ""):
                return jsonify({"user": {"username": "admin", "super_admin": True,
                                         "display_name": "管理员"}})
            return jsonify({"user": None})

        @upstream.post("/api/logout")
        def _logout():
            # 真实登出 = 会话清除 + 删除型 Set-Cookie（401 为统一失效信号）
            resp = jsonify({"ok": True})
            resp.headers["Set-Cookie"] = ("sid=; Path=/; "
                                          "Expires=Thu, 01 Jan 1970 00:00:00 GMT")
            return resp, 401

        srv = make_server("127.0.0.1", 0, upstream, threaded=True)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        addr = "http://127.0.0.1:%d" % srv.server_port

        lp_dir = os.path.join(work, "lp")
        os.makedirs(lp_dir)
        make_plugin(lp_dir, "local-idprobe-h", "hybrid")
        make_plugin(lp_dir, "local-idprobe-w", "local")

        import capability_app
        from plugin_executor import PluginExecutor
        build_cfg = {"_program_dir": os.path.join(work, "D_program"),
                     "product": {"version": "0.0.1"}, "plugin_api": 1,
                     "paths": {"data_dirname": "D_client_data"}}
        os.makedirs(build_cfg["_program_dir"], exist_ok=True)
        pylibs = os.path.join(work, "pylibs")
        os.makedirs(pylibs)
        executor = PluginExecutor(sys.executable,
                                  os.path.join(ROOT, "client", "plugin_host.py"),
                                  pylibs, work, lambda m: None)
        app = capability_app.create_capability_app(
            build_cfg, {"remote_address": addr}, addr,
            os.path.join(work, "static"), lp_dir,
            os.path.join(ROOT, "client", "fallback.html"),
            data_dir=os.path.join(work, "data"), executor=executor,
            log=lambda m: print("    [cap] %s" % m))
        c = app.test_client()

        def ping(pid):
            r = c.get("/api/local/%s/ping" % pid)
            if r.status_code != 200:
                print("    [ping %s] status=%s body=%s" % (pid, r.status_code, r.get_data()[:120]))
            return (r.get_json() or {}).get("user")

        # 1) 未登录 → 匿名
        check("1a 未登录 hybrid 匿名", ping("local-idprobe-h") is None)
        check("1b 未登录 worker 匿名", ping("local-idprobe-w") is None)

        # 2) 登录 → 双链身份
        r = c.post("/api/login", json={"username": "admin", "password": "x"})
        check("2a 登录经中继成功", r.status_code == 200 and r.get_json().get("ok"))
        u = ping("local-idprobe-h")
        check("2b hybrid provider 链拿到身份", (u or {}).get("username") == "admin", u)
        u = ping("local-idprobe-w")
        check("2c worker X-JZ-User 头链拿到身份", (u or {}).get("username") == "admin", u)
        check("2d 身份字段完整（super_admin）", (u or {}).get("super_admin") is True, u)

        # 3) 登出（401 统一失效信号）→ 立即回匿名
        c.post("/api/logout")
        check("3a 登出后 hybrid 匿名", ping("local-idprobe-h") is None)
        check("3b 登出后 worker 匿名", ping("local-idprobe-w") is None)

        # 4) jz_api 直测：provider 优先；无 provider 无上下文 → None
        import jz_api
        jz_api.register_providers(get_session_user=lambda: {"username": "pv"})
        check("4a provider 优先不被兜底遮蔽", jz_api.get_session_user() == {"username": "pv"})
        jz_api._PROVIDERS["session_user"] = None   # register_providers 不支持显式清除，直清验证兜底
        check("4b 无 provider 无请求上下文 → None", jz_api.get_session_user() is None)

        # 5) F5 清单降级合并（T-O9-5）：上游不可达 → 200 仅本地条目（本地条目=离线网格唯一载体）
        app2 = capability_app.create_capability_app(
            build_cfg, {"remote_address": "http://127.0.0.1:9"}, "http://127.0.0.1:9",
            os.path.join(work, "static"), lp_dir,
            os.path.join(ROOT, "client", "fallback.html"),
            data_dir=os.path.join(work, "data"), executor=executor,
            log=lambda m: None)
        c2 = app2.test_client()
        r = c2.get("/api/tools")
        d = r.get_json()
        check("5a 上游死端口清单仍 200", r.status_code == 200 and d.get("degraded") is True,
              (r.status_code, d.get("degraded")))
        ids = [t["id"] for t in d.get("tools", [])]
        check("5b 仅本地条目出格", "local-idprobe-h" in ids and "local-idprobe-w" in ids
              and all(not i.startswith("remote") for i in ids), ids)
        check("5c 分类仅本地插件", [x.get("id") for x in d.get("categories", [])] == ["local"],
              d.get("categories"))
        r = c2.get("/api/tools/local-idprobe-h")
        check("5d 本地条目元数据可取", r.status_code == 200 and
              r.get_json().get("id") == "local-idprobe-h")
        c2.post("/local-api/plugins/state", json={"id": "local-idprobe-w", "enabled": False})
        ids = [t["id"] for t in c2.get("/api/tools").get_json()["tools"]]
        check("5e 停用条目降级清单同样隐藏", "local-idprobe-w" not in ids, ids)

        ok = all(RESULTS)
        print("== F1+F5 台架：%d/%d 通过 ==" % (sum(RESULTS), len(RESULTS)))
        return 0 if ok else 1
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
