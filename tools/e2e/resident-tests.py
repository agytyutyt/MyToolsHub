# -*- coding: utf-8 -*-
r"""F3 resident/timeout 崩溃语义台架（批次 9 T-O9-7）。

经真实能力层派发链（create_capability_app → PluginExecutor → plugin_host 子进程）验证：
  1) resident=true 常驻：多请求复用同一进程（内存状态跨请求存活、pid 不变）；
  2) 非 resident 缺省：每请求新进程（状态不跨请求、pid 变化）；
  3) 超时：sleep 超过 manifest.timeout_ms → 504「本地插件超时」，后续请求自动恢复；
  4) 崩溃：后端 os._exit(1) → 502「本地插件崩溃退出」，下次请求自动拉起（状态重置）；
  5) 查询串透传（T-O9-7 顺带修复的派发缺陷）：/echo?t=… 原样到达后端。

用法：python tools/e2e/resident-tests.py   （退出码 0=全过）
"""
import json
import os
import shutil
import sys
import tempfile
import time
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "client"))

# 与控制台编码和解：本台架打印 ✅/❌，GBK 控制台（Windows 缺省）会在 print 处抛
# UnicodeEncodeError 直接崩掉整个台架（§B-20 同类隐患统一处置）。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


from werkzeug.serving import make_server

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append(ok)
    print("  %s %s%s" % ("✅" if ok else "❌", name, (" ｜ " + str(detail)) if detail and not ok else ""))


BACKEND_SRC = '''import os
_state = {"n": 0, "pid": os.getpid()}
from flask import jsonify, request


def register(app):
    @app.get("/count")
    def _count():
        _state["n"] += 1
        return jsonify({"n": _state["n"], "pid": _state["pid"]})

    @app.get("/sleep/<t>")
    def _sleep(t):
        import time
        time.sleep(float(t))
        return jsonify({"ok": True})

    @app.get("/echo")
    def _echo():
        return jsonify({"q": request.args.get("t", "")})

    @app.get("/boom")
    def _boom():
        os._exit(1)
'''


def make_plugin(lp_dir, pid, resident, timeout_ms, idle_ttl=0):
    d = os.path.join(lp_dir, pid)
    os.makedirs(os.path.join(d, "backend"), exist_ok=True)
    os.makedirs(os.path.join(d, "frontend"), exist_ok=True)
    m = {"id": pid, "name": pid, "icon": "🧪", "version": "1.0.0",
         "runtime": "local", "kind": "local",
         "entry": {"frontend": "index.html", "backend": "routes.py"},
         "timeout_ms": timeout_ms}
    if resident:
        m["resident"] = True
    if idle_ttl:
        m["idle_ttl"] = idle_ttl          # F6：空闲回收阈值（秒；缺省不回收）
    with open(os.path.join(d, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False)
    with open(os.path.join(d, "backend", "__init__.py"), "w", encoding="utf-8") as f:
        f.write("")
    with open(os.path.join(d, "backend", "routes.py"), "w", encoding="utf-8") as f:
        f.write(BACKEND_SRC)
    with open(os.path.join(d, "frontend", "index.html"), "w", encoding="utf-8") as f:
        f.write("<html>r</html>")


def main():
    work = tempfile.mkdtemp(prefix="resident-")
    os.environ["JZCLIENT_DATA_DIR"] = os.path.join(work, "clientdata")  # 官方测试隔离口
    try:
        from flask import Flask
        srv = make_server("127.0.0.1", 0, Flask("stub"), threaded=True)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        addr = "http://127.0.0.1:%d" % srv.server_port   # 死地址不用，仅占位

        lp_dir = os.path.join(work, "lp")
        os.makedirs(lp_dir)
        make_plugin(lp_dir, "local-res-r", resident=True, timeout_ms=2500)
        make_plugin(lp_dir, "local-res-n", resident=False, timeout_ms=2500)
        make_plugin(lp_dir, "local-res-t", resident=True, timeout_ms=2500, idle_ttl=2)

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
            log=lambda m: None,
            shell_token="bench-shell-1")
        c = app.test_client()
        # 壳自证（2026-10-11 需求① G2-6）：台架模拟壳——先 boot 种 Cookie 再探测
        c.get("/shell-boot?_shell=bench-shell-1&next=/")

        def count(pid):
            return c.get("/api/local/%s/count" % pid).get_json()

        # 1) resident 常驻复用：状态跨请求存活
        r1, r2 = count("local-res-r"), count("local-res-r")
        check("1a resident 状态跨请求存活（n=1→2）", r1["n"] == 1 and r2["n"] == 2, (r1, r2))
        check("1b resident 同进程复用（pid 不变）", r1["pid"] == r2["pid"])

        # 2) 非 resident 缺省：每请求新进程
        n1, n2 = count("local-res-n"), count("local-res-n")
        check("2a 非 resident 状态不跨请求（均 n=1）", n1["n"] == 1 and n2["n"] == 1, (n1, n2))
        check("2b 非 resident 每请求新进程（pid 变化）", n1["pid"] != n2["pid"])

        # 3) 查询串透传（顺带修复的派发缺陷）
        r = c.get("/api/local/local-res-r/echo?t=hello").get_json()
        check("3  查询串随派发透传", r.get("q") == "hello", r)

        # 4) 超时 → 504，后续请求恢复
        resp = c.get("/api/local/local-res-r/sleep/6")
        check("4a 超时回 504", resp.status_code == 504, (resp.status_code, resp.get_json()))
        rc = count("local-res-r")
        check("4b 超时击杀后请求自动恢复", isinstance(rc.get("n"), int), rc)

        # 5) 崩溃 → 502，下次请求自动拉起（状态重置）
        resp = c.get("/api/local/local-res-r/boom")
        check("5a 崩溃回 502", resp.status_code == 502, (resp.status_code, resp.get_json()))
        rc = count("local-res-r")
        check("5b 崩溃后自动拉起（n 重置=1）", rc.get("n") == 1, rc)

        # 6) F6 idle 回收：ttl>0 的常驻 worker 空闲后**干净收回**（常驻表弹出、非崩溃路径），
        #    下次请求静默重启（新 pid、状态重置）；对照：未声明 idle_ttl 的常驻不回收。
        t1 = count("local-res-t")
        pid1 = t1["pid"]
        count("local-res-r")                      # 确保对照项有活着的常驻（5b 后已重启）
        time.sleep(4.5)                           # > ttl(2s) + 收割节拍（自适应 ttl/2=1s）
        check("6a 空闲超 ttl 已干净收回（常驻表弹出）",
              executor._residents.get("local-res-t") is None,
              executor._residents.get("local-res-t"))
        t2 = count("local-res-t")
        check("6b 回收后请求静默重启（新进程、状态重置 n=1）",
              t2["pid"] != pid1 and t2["n"] == 1, (t1, t2))
        check("6c 对照：未声明 idle_ttl 的常驻不回收",
              executor._residents.get("local-res-r") is not None)

        ok = all(RESULTS)
        print("== F3 resident 语义台架：%d/%d 通过 ==" % (sum(RESULTS), len(RESULTS)))
        return 0 if ok else 1
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
