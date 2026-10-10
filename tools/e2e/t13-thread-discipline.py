# -*- coding: utf-8 -*-
"""T13 线程纪律专项（RP5）——UI 主线程 × waitress × 托盘 × worker 四方并发的可闭环境部分。

真源：`docs/plan/桌面端适配-TODO清单.md` T13 行（验收点：全部场景无死锁 / 无残余进程）。
本台架覆盖**无需 GUI 就能确定性验证**的那部分；GUI 相关场景（关窗→托盘→再开窗、首次关窗气泡、
远程中断再开窗）留在真机清单，本文件末尾如实列出，不冒充已验。

段位：
  A 静态纪律：线程创建必须守护化、无无超时 join、退出通道唯一（AST/正则）
  B 并发不死锁：真实能力层 + 真实 worker 子进程，8 路并发（含长任务与崩溃任务）全部返回
  C 无残余进程：常驻 worker 在宿主关停与起停循环后不残留（Windows 进程存活实测）
  D 不可达不悬挂：中继（remote）为死地址时请求在上限内降级返回，UI 线程不被拖住

    python tools/e2e/t13-thread-discipline.py
"""
import ast
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, "client"))

# 与控制台编码和解：本台架打印 ✅/❌，GBK 控制台（Windows 缺省）会在 print 处抛
# UnicodeEncodeError 直接崩掉整个台架（§B-20 同类隐患统一处置）。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, ROOT)

RESULTS = []

# 静态纪律白名单（都有明确理由，改代码时同步改这里）
DAEMON_EXEMPT = {
    ("client/shell_window.py", "webview.start() 内部起的 UI 主线程由 pywebview 管理，不是我们创建的"),
}
EXIT_WHITELIST = {
    ("client/capability_app.py", "restart-exit 通道：重启前延迟 os._exit 是唯一退出口（批次 2 T09 设计）"),
    ("client/shell_window.py", "on_exit：托盘「退出」菜单项，先 geo._flush() + icon.stop() 再结束进程（GD5 拍板口径）"),
}


def check(name, ok, detail=""):
    RESULTS.append(bool(ok))
    print("  %s %s%s" % ("✅" if ok else "❌", name,
                         (" ｜ " + str(detail)) if detail and not ok else ""))


def client_sources():
    d = os.path.join(ROOT, "client")
    for fn in sorted(os.listdir(d)):
        if fn.endswith(".py"):
            yield os.path.join(d, fn)


# ---------------------------------------------------------------- A 段（静态）

def section_a():
    print("== A 段：线程创建与退出纪律（静态） ==")
    non_daemon, naked_join, exits = [], [], []
    for path in client_sources():
        rel = os.path.relpath(path, ROOT).replace(os.sep, "/")
        src = open(path, encoding="utf-8").read()
        try:
            tree = ast.parse(src)
        except SyntaxError as exc:
            check("A0 %s 可解析" % rel, False, exc)
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            name = ""
            if isinstance(f, ast.Attribute):
                name = f.attr
            elif isinstance(f, ast.Name):
                name = f.id
            if name == "Thread" and isinstance(f, ast.Attribute) and f.value.id in ("threading", "Thread"):
                kw = {k.arg for k in node.keywords}
                if "daemon" not in kw:
                    non_daemon.append((rel, node.lineno))
            if name == "join" and not node.args and not node.keywords:
                naked_join.append((rel, node.lineno))
            if name == "_exit":
                exits.append((rel, node.lineno))
    bad = [x for x in non_daemon if not any(x[0] == p for p, _ in DAEMON_EXEMPT)]
    check("A1 所有 threading.Thread 均 daemon=True（非守护线程会挂住退出=残余进程根因）",
          not bad, bad)
    check("A2 无「无超时的 join()」（无超时 join 在 worker/UI 交叉等待时即死锁面）",
          not naked_join, naked_join)
    bad_exit = [x for x in exits if not any(x[0] == p for p, _ in EXIT_WHITELIST)]
    check("A3 os._exit 只出现在登记的退出通道（其余位置=绕过收割线程的硬退出）", not bad_exit, bad_exit)


# ---------------------------------------------------------------- B/C/D 段（运行时）

BACKEND_SRC = '''import os
import time
from flask import jsonify, request

_state = {"n": 0, "pid": os.getpid()}


def register(app):
    @app.get("/count")
    def _count():
        _state["n"] += 1
        return jsonify({"n": _state["n"], "pid": _state["pid"]})

    @app.get("/slow/<t>")
    def _slow(t):
        time.sleep(float(t))
        return jsonify({"ok": True, "pid": os.getpid()})

    @app.get("/boom")
    def _boom():
        os._exit(1)
'''


def make_plugin(lp_dir, pid, resident=True, timeout_ms=8000):
    d = os.path.join(lp_dir, pid)
    os.makedirs(os.path.join(d, "backend"), exist_ok=True)
    os.makedirs(os.path.join(d, "frontend"), exist_ok=True)
    m = {"id": pid, "name": pid, "icon": "🧪", "version": "1.0.0",
         "runtime": "local", "kind": "local",
         "entry": {"frontend": "index.html", "backend": "routes.py"},
         "timeout_ms": timeout_ms}
    if resident:
        m["resident"] = True
    with open(os.path.join(d, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False)
    open(os.path.join(d, "backend", "__init__.py"), "w").close()
    with open(os.path.join(d, "backend", "routes.py"), "w", encoding="utf-8") as f:
        f.write(BACKEND_SRC)
    with open(os.path.join(d, "frontend", "index.html"), "w", encoding="utf-8") as f:
        f.write("<html>t13</html>")


def alive(pid):
    """Windows 进程存活实测（不依赖 psutil）。"""
    if pid is None:
        return False
    import ctypes
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not h:
        return False
    try:
        code = ctypes.c_ulong()
        if ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code)):
            return code.value == STILL_ACTIVE
        return True
    finally:
        ctypes.windll.kernel32.CloseHandle(h)


def boot(work, remote_addr):
    from plugin_executor import PluginExecutor
    import capability_app
    lp_dir = os.path.join(work, "lp")
    os.makedirs(lp_dir, exist_ok=True)
    make_plugin(lp_dir, "local-t13-res")
    make_plugin(lp_dir, "local-t13-eph", resident=False)
    build_cfg = {"_program_dir": os.path.join(work, "D_program"),
                 "product": {"version": "0.0.1"}, "plugin_api": 1,
                 "paths": {"data_dirname": "D_client_data"}}
    os.makedirs(build_cfg["_program_dir"], exist_ok=True)
    pylibs = os.path.join(work, "pylibs")
    os.makedirs(pylibs, exist_ok=True)
    executor = PluginExecutor(sys.executable, os.path.join(ROOT, "client", "plugin_host.py"),
                              pylibs, work, lambda m: None)
    app = capability_app.create_capability_app(
        build_cfg, {"remote_address": remote_addr}, "http://127.0.0.1:1",
        os.path.join(work, "static"), lp_dir,
        os.path.join(ROOT, "client", "fallback.html"),
        data_dir=os.path.join(work, "data"), executor=executor, log=lambda m: None)
    return app.test_client(), executor


def section_bcd():
    print("== B/C/D 段：并发、残余进程与不可达降级（真实能力层 + 真实 worker 子进程） ==")
    work = tempfile.mkdtemp(prefix="t13-")
    os.environ["JZCLIENT_DATA_DIR"] = os.path.join(work, "clientdata")
    try:
        c, executor = boot(work, "http://127.0.0.1:59999")   # 中继恒不可达：同时是 D 段前提

        # B1 八路并发（常驻 + 非常驻 + 长任务）全部返回，无悬挂
        got, errors = [], []

        def hit(path):
            try:
                r = c.get(path)
                got.append((path, r.status_code))
            except Exception as exc:
                errors.append((path, repr(exc)[:80]))

        threads = []
        for i in range(5):
            threads.append(threading.Thread(target=hit, args=("/api/local/local-t13-res/count",)))
        for i in range(2):
            threads.append(threading.Thread(target=hit, args=("/api/local/local-t13-eph/count",)))
        threads.append(threading.Thread(target=hit, args=("/api/local/local-t13-res/slow/1.2",)))
        t0 = time.time()
        for t in threads:
            t.daemon = True
            t.start()
        for t in threads:
            t.join(timeout=60)
        hung = [t for t in threads if t.is_alive()]
        check("B1 8 路并发全部返回且无悬挂线程（死锁会表现为 join 超时）",
              not hung and len(got) == 8 and not errors,
              "悬挂=%d got=%d errors=%s 用时 %.1fs" % (len(hung), len(got), errors, time.time() - t0))

        # B2 常驻 worker 在长任务**进行中**被关停（「worker 运行中关窗」的等价路径）
        slow = threading.Thread(target=lambda: c.get("/api/local/local-t13-res/slow/2.0"), daemon=True)
        slow.start()
        time.sleep(0.4)
        pids = [p.pid for p in list(getattr(executor, "_residents", {}).values())]
        executor.shutdown()                       # 托盘退出/关机路径的收割口
        slow.join(timeout=15)
        time.sleep(0.6)
        left = [p for p in pids if alive(p)]
        check("B2 worker 在途时收割：无残余 worker 进程（Windows 实测 pid 存活）",
              not left and slow.is_alive() is False, "残余 pid=%s" % left)

        # C 起停循环不泄漏进程
        seen = set()
        for _ in range(3):
            r = c.get("/api/local/local-t13-res/count")
            pid = (r.get_json() or {}).get("pid")
            if pid:
                seen.add(int(pid))
            executor.shutdown()
        time.sleep(0.8)
        leaked = [p for p in seen if alive(p)]
        check("C1 起停 ×3 轮不残留 worker（每轮都换新进程，全部被收割）",
              not leaked and len(seen) >= 2, "残留=%s 见过=%s" % (leaked, sorted(seen)))

        # C2 崩溃任务（os._exit）不拖垮宿主、后续请求仍可服务
        c.get("/api/local/local-t13-res/boom")
        time.sleep(0.4)
        r = c.get("/api/local/local-t13-res/count")
        check("C2 worker 自毁后宿主不死锁且静默重启（仍有 200）",
              r.status_code == 200 and (r.get_json() or {}).get("n") == 1, r.status_code)

        # D 中继不可达时本地面不被拖住（UI 线程等不起远端超时）
        t0 = time.time()
        r_local = c.get("/api/tools")
        dt = time.time() - t0
        check("D1 死地址下本地清单接口仍快速返回（不悬挂 UI）",
              r_local.status_code == 200 and dt < 20, "%.1fs" % dt)
        t0 = time.time()
        r_h = c.get("/local-api/health")
        hj = r_h.get_json() or {}
        check("D2 /local-api/health 在上限内返回并如实申报远程不可达（remote_ok=False）",
              r_h.status_code == 200 and time.time() - t0 < 20
              and hj.get("remote_ok") in (False, None), "%.1fs %s" % (time.time() - t0, hj))
    finally:
        import shutil
        shutil.rmtree(work, ignore_errors=True)


def main():
    section_a()
    section_bcd()
    print("\n== T13 线程纪律台架：%d/%d 通过 ==" % (sum(RESULTS), len(RESULTS)))
    print("仍需真机人工（本台架不覆盖，勿当已验）：")
    print("  ① 关窗 → 托盘 → 再开窗（页面状态不丢）；② 首次关窗气泡一次；")
    print("  ③ 远程中断时再开窗；④ 托盘菜单五项逐项点击；⑤ 真机长时间挂机观察残余内存/线程。")
    return 1 if not all(RESULTS) else 0


if __name__ == "__main__":
    sys.exit(main())
