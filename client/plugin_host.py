# -*- coding: utf-8 -*-
"""插件子进程引导器（批次 4 T20，GD4）——在独立 Python 进程内承载一个类 B 插件。

用法：<python> plugin_host.py --plugin <插件目录> --pylibs <pylibs 目录> [--resident]

协议 stdio-json-v1：
  stdin  每行一个请求：{"request_id","method","path","headers","body_b64"}
  stdout 每行一个响应：{"request_id","status","headers","body_b64"}
  常驻模式循环处理直到 stdin 关闭；非常驻模式处理完当前请求即退出。

派发 = mini-WSGI：插件 routes.register(app) 后经 werkzeug test_client 派发
（与进程内注册同构，插件代码零感知）。
"""
import time as _time

#: 阶段 2/S2-4：冷启分段埋点基准 —— 放在**第一条语句**，使"解释器就绪前"的
#: 分界尽量靠近进程入口（该段无法在进程内测到，由父侧 worker-cold-roundtrip 与
#: worker-import 之差反推：≈解释器引导 + 到本行为止的启动成本）。
_T0 = _time.perf_counter()

import argparse
import base64
import io
import json
import os
import sys


def _perf_note(stage):
    """打一条冷启分段（stderr → 执行器把它落到 plugin-logs/<id>.log）。

    只在启动期调用 1–3 次，**不进请求路径**；异常一律吞掉（埋点绝不能影响启动）。
    ⚠ **默认关闭**，需显式 `JZ_WORKER_PERF=1` 才输出 —— 否则会污染生产 plugin-logs
    （本卡风险清单里写明的一条）。测量脚本/台架按需开。
    """
    if not os.environ.get("JZ_WORKER_PERF"):
        return
    try:
        sys.stderr.write("[perf] worker-%s %d %.0fms\n"
                         % (stage, os.getpid(), (_time.perf_counter() - _T0) * 1000.0))
        sys.stderr.flush()
    except Exception:
        pass

_here = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_here)
# 两种布局都要能找到内核模块（jz_plugin_host / jz_deps / jz_api / jztools_data）：
#   源仓库：模块在仓库根（_ROOT）；打包态：模块在 <bundle>/kernel/（随 spec datas 落盘）——
#   2026-10-08 起打包态 worker 由自带运行时 python 拉起（T07 遗留销项），此处按目录探测。
for _p in (_here, os.path.join(_here, "kernel"), _ROOT, os.path.join(_ROOT, "kernel")):
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

import jz_plugin_host  # noqa: E402

_perf_note("import")      # 本模块 import 栈（含 jz_plugin_host）到此结束


def build_app(plugin_dir):
    manifest = json.load(open(os.path.join(plugin_dir, "manifest.json"), encoding="utf-8"))
    plugins_parent = os.path.dirname(plugin_dir)
    plugin_id = os.path.basename(plugin_dir)
    from flask import Flask
    app = Flask("plugin_host")
    routes = jz_plugin_host.load_backend_module(plugins_parent, plugin_id)
    bp = getattr(routes, "bp", None)
    if bp is not None:
        app.register_blueprint(bp)            # worker 无前缀语义（前缀由能力层剥除）
        return app, manifest
    register = getattr(routes, "register", None)
    if not callable(register):
        raise RuntimeError("插件 %s 未导出 register(app)" % plugin_id)
    register(app)
    return app, manifest


def dispatch(app, req):
    from werkzeug.test import EnvironBuilder
    body = base64.b64decode(req.get("body_b64") or "")
    builder = EnvironBuilder(
        method=req.get("method", "GET"),
        path=req.get("path", "/"),
        headers=list((req.get("headers") or {}).items()),
        data=body,
    )
    with app.test_client() as tc:
        resp = tc.open(builder)
    return {
        "request_id": req.get("request_id"),
        "status": resp.status_code,
        "headers": dict(resp.headers),
        "body_b64": base64.b64encode(resp.get_data()).decode("ascii"),
    }


def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


#: 并发派发上限（R2.3）：manifest/启动参数声明值会被夹到这个上限内。
MAX_CONCURRENCY = 8
_emit_lock = None


def _concurrent_emit(obj):
    """并发模式下的响应输出：**必须加写锁**，否则两个响应行会交错成半行。"""
    global _emit_lock
    if _emit_lock is None:
        import threading as _th
        _emit_lock = _th.Lock()
    with _emit_lock:
        emit(obj)


def _handle_one(app, line):
    try:
        _concurrent_emit(dispatch(app, json.loads(line)))
    except Exception as exc:
        _concurrent_emit({"error": "%s: %s" % (type(exc).__name__, exc)})


def serve_concurrent(app, n):
    """并发派发（R2.3）：**单读线程**读请求 → 线程池处理 → 每行响应在写锁下输出。

    ⚠ 只在本进程被显式要求 concurrency>1 时启用；默认 1 走原来的串行循环，行为与历史一致。
    插件后端若**非线程安全**（模块级可变状态），必须保持默认 1 —— 这是 opt-in 的唯一理由。
    stdin 关闭时等在途请求做完再退出（#3 语义：常驻 worker 由父进程 EOF 触发自杀）。
    """
    from concurrent.futures import ThreadPoolExecutor
    pool = ThreadPoolExecutor(max_workers=n, thread_name_prefix="plugreq")
    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            pool.submit(_handle_one, app, line)
    finally:
        pool.shutdown(wait=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plugin", required=True)
    ap.add_argument("--pylibs", default="")
    ap.add_argument("--resident", action="store_true")
    ap.add_argument("--concurrency", type=int, default=1,
                    help="并发派发线程数（R2.3；默认 1＝串行，与历史一致）")
    args = ap.parse_args()

    plugin_dir = os.path.abspath(args.plugin)
    if args.pylibs and os.path.isdir(args.pylibs):
        # 执行器传的是**完整 pylibs 目录** → 显式 pylibs_dir（旧写法 dirname() 会多拼一层
        # runtime：<prog>/runtime/runtime/pylibs → 依赖组件全 import 不到、插件误报
        # 「依赖未满足」——2026-10-08 真机报告）
        jz_plugin_host.setup_dep_components(pylibs_dir=args.pylibs)
    _perf_note("deps")        # pylibs 注入完（会改变后续 import 成本，故分界卡在它之后）
    app, _manifest = build_app(plugin_dir)
    _perf_note("ready")       # 插件 import + register(app) 完 —— 此后可接请求

    _conc = max(1, min(int(args.concurrency or 1), MAX_CONCURRENCY))
    _perf_note("concurrency=%d" % _conc)
    if args.resident and _conc > 1:
        serve_concurrent(app, _conc)
        return
    if args.resident:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                emit(dispatch(app, json.loads(line)))
            except Exception as exc:
                emit({"error": "%s: %s" % (type(exc).__name__, exc)})
    else:
        line = sys.stdin.readline().strip()
        if line:
            try:
                emit(dispatch(app, json.loads(line)))
            except Exception as exc:
                emit({"error": "%s: %s" % (type(exc).__name__, exc)})


if __name__ == "__main__":
    main()
