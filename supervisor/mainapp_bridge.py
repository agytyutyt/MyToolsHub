# -*- coding: utf-8 -*-
"""主应用侧 E 架构桥（T-O2-12b）：worker 化插件的路由切换 + 反代 + 统一 errorhandler。

职责切分（运行架构设计文档 §3 拍板）：
- **本桥在 admin 鉴权门之后接线**（app.py 于 register_plugin_backends() 后调用
  install_worker_proxy）——登录/超时/权限点全在代理跳完成，到不了 worker 的
  请求不存在越权；
- **路由切换**：注册表里有单元的插件 = worker 化 → 主应用跳过其进程内加载，
  `/api/<id>/*` 经 before_request 反代到 127.0.0.1:<port>（注册表 mtime 缓存，
  仅管理动作后变化——拍板：端口分配一次持久化）；
- **反代流式口径**（T03 教训）：请求体 SpooledTemporaryFile 按确切
  Content-Length 转发，响应流式回传、剥 hop 头；
- **统一 errorhandler（补课项）**：插件路由 Python 异常 → 该插件 503 + 其余
  照常（native 崩溃/OOM 由进程边界隔离，Python 异常由本兜底）——仅冻结态
  注册（源码调试模式保留 werkzeug 交互式调试器）；
- worker 不可达/熔断 → 503 JSON（管理页徽标可见，supervisor 控制接口恢复）。

本模块随主包运行，可 import jztools_data（与 supervisor_core 的 stdlib 纪律
相反——它是主包侧组件）。
"""

import json
import os
import tempfile
import shutil
import threading

import requests as _rq
from flask import Response, jsonify, request

_HOP = {"host", "content-length", "connection", "keep-alive",
        "transfer-encoding", "proxy-authenticate", "proxy-authorization",
        "te", "trailers", "upgrade"}
_DROP_RESP = {"content-length", "transfer-encoding", "connection", "keep-alive"}

_CACHE = {"mtime": None, "units": {}}
_CACHE_LOCK = threading.Lock()


def registry_path(data_root):
    return os.path.join(data_root, "config", "supervisor-units.json")


def worker_units_snapshot(data_root):
    """worker 化插件 {plugin_id: unit}；注册表文件 mtime 变化才重读（缓存恒正确）。"""
    p = registry_path(data_root)
    try:
        mtime = os.path.getmtime(p) if os.path.isfile(p) else None
    except OSError:
        mtime = None
    with _CACHE_LOCK:
        if mtime != _CACHE["mtime"]:
            units = {}
            if mtime is not None:
                try:
                    with open(p, "r", encoding="utf-8-sig") as f:
                        obj = json.load(f)
                    for u in obj.get("units") or []:
                        if isinstance(u, dict) and u.get("kind") == "plugin" \
                                and u.get("plugin_id") and u.get("enabled", True):
                            units[str(u["plugin_id"])] = u
                except Exception:
                    units = {}
            _CACHE["mtime"] = mtime
            _CACHE["units"] = units
        return dict(_CACHE["units"])


def supervisor_endpoint(data_root):
    """控制接口端点 {url, token}（store_admin 发布即部署用）；未部署 → None。"""
    p = registry_path(data_root)
    if not os.path.isfile(p):
        return None
    try:
        with open(p, "r", encoding="utf-8-sig") as f:
            port = int((json.load(f).get("supervisor") or {}).get("port") or 0)
    except Exception:
        return None
    tok_file = os.path.join(data_root, "config", ".supervisor_token")
    token = ""
    if os.path.isfile(tok_file):
        try:
            with open(tok_file, "r", encoding="utf-8") as f:
                token = f.read().strip()
        except Exception:
            token = ""
    if not port:
        return None
    return {"url": "http://127.0.0.1:%d" % port, "token": token}


def try_supervisor_apply(data_root, plugin_id, package_path, timeout=60.0):
    """发布即部署的 supervisor 分支。返回 (handled, result)：

    - (True, {ok..}) —— supervisor 接管（无论成败，调用方不再走进程内链）；
    - (False, None) —— 该插件未 worker 化（无单元），调用方走既有进程内链。
    """
    units = worker_units_snapshot(data_root)
    if plugin_id not in units:
        return False, None
    ep = supervisor_endpoint(data_root)
    if ep is None:
        return True, {"ok": False,
                      "error": "插件已 worker 化但 supervisor 控制接口不可达（检查管理进程）"}
    try:
        r = _rq.post(ep["url"] + "/apply",
                     headers={"X-Supervisor-Token": ep["token"]},
                     json={"plugin_id": plugin_id, "unit": plugin_id,
                           "package": os.path.abspath(package_path)},
                     timeout=timeout)
    except _rq.RequestException as exc:
        return True, {"ok": False, "error": "supervisor 控制接口请求失败：%s" % exc}
    try:
        return True, r.json()
    except Exception:
        return True, {"ok": False, "error": "supervisor 响应不可解析（HTTP %s）" % r.status_code}


def _proxy_to_worker(pid, unit):
    """反代一次请求（鉴权已过——本函数只做转发）。worker 不可达 → 503。"""
    port = int(unit.get("port") or 0)
    qs = request.query_string.decode("utf-8", "replace")
    url = "http://127.0.0.1:%d%s%s" % (port, request.path,
                                       ("?" + qs) if qs else "")
    headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP}
    if os.environ.get("JZ_BRIDGE_DEBUG"):
        print("[bridge] %s %s → 127.0.0.1:%d headers=%s"
              % (request.method, request.path, port, dict(headers)), flush=True)
    data = None
    if request.method in ("POST", "PUT", "PATCH"):
        body = tempfile.SpooledTemporaryFile(max_size=512 * 1024)
        shutil.copyfileobj(request.stream, body, 1024 * 1024)
        body.seek(0)
        data = body
        if request.content_length:
            headers["Content-Length"] = str(request.content_length)
    try:
        r = _rq.request(request.method, url, headers=headers, data=data,
                        stream=True, allow_redirects=False, timeout=(5, 300))
    except _rq.RequestException as exc:
        return jsonify({"error": "插件服务暂时不可用（worker 未运行或熔断中）",
                        "plugin": pid, "detail": str(exc)}), 503
    out = [(k, v) for k, v in r.headers.items() if k.lower() not in _DROP_RESP]
    return Response(r.iter_content(chunk_size=64 * 1024),
                    status=r.status_code, headers=out)


def install_worker_proxy(app, base_dir, register_errorhandler=None):
    """主应用接线（app.py 在 register_plugin_backends 之后调用一次）。

    - before_request：/api/<worker化插件id>/* → 反代（admin 鉴权门之后执行）；
    - errorhandler（仅冻结态注册，缺省 None=自动判定）：插件路径异常 → 503，
      其余路径维持既有行为。
    """
    data_root = None
    try:
        import jztools_data
        data_root = jztools_data.get_data_root()
    except Exception:
        data_root = os.path.join(base_dir, "data")

    def _dispatch():
        units = worker_units_snapshot(data_root)
        if not units or not request.path.startswith("/api/"):
            return None
        seg = request.path[len("/api/"):].split("/", 1)[0]
        if seg and seg in units:
            return _proxy_to_worker(seg, units[seg])
        return None

    app.before_request(_dispatch)

    if register_errorhandler is None:
        register_errorhandler = not app.debug      # 源码调试模式不注册（保调试器）

    if register_errorhandler:
        from werkzeug.exceptions import HTTPException

        @app.errorhandler(Exception)
        def _plugin_errorhandler(exc):
            """统一兜底（设计 §6 补课项）：插件异常 → 该插件 503；其余照常。"""
            if isinstance(exc, HTTPException):
                return exc                          # 404/403 等语义原样
            p = request.path
            seg = p[len("/api/"):].split("/", 1)[0] if p.startswith("/api/") else ""
            units = worker_units_snapshot(data_root)
            known = seg and (seg in units or seg in (getattr(app, "config", {})
                                                     .get("_PLUGIN_IDS") or set()))
            if known:
                app.logger.exception("插件 %s 请求异常 → 503（其余不受影响）", seg)
                return jsonify({"error": "插件 %s 暂时不可用（内部错误）" % seg,
                                "plugin": seg}), 503
            app.logger.exception("请求处理异常（非插件路径）：%s", p)
            return jsonify({"error": "服务器内部错误"}), 500

    return app
