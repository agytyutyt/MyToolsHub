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
import argparse
import base64
import io
import json
import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_here)
sys.path.insert(0, _ROOT)
sys.path.insert(0, _here)

import jz_plugin_host  # noqa: E402


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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plugin", required=True)
    ap.add_argument("--pylibs", default="")
    ap.add_argument("--resident", action="store_true")
    args = ap.parse_args()

    plugin_dir = os.path.abspath(args.plugin)
    if args.pylibs and os.path.isdir(args.pylibs):
        jz_plugin_host.setup_dep_components(os.path.dirname(args.pylibs))
    app, _manifest = build_app(plugin_dir)

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
