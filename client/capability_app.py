# -*- coding: utf-8 -*-
"""能力层装配（批次 1 T08/T09）：路由分区四路裁决 + 会话中继骨架 + 本地直出。

路由分区（方案 §6.1 表；业务逻辑禁入本层）：
  ① 本地直出：/ 、/tool/<id>（客户端自带 static 副本）、/plugin/local-*/ 、/local-admin（批次 4）
  ② 清单合并：/api/tools*（批次 3 T16 实现合并，本批次先原样中继）
  ③ 本地插件 API：/api/local/<id>/*（批次 4 T20 执行器，本批次占位）
  ④ 中继远程：其余 /api/* 与类 A /plugin/<id>/*
  ⑤ /local-api/*：客户端自有本地接口（配置/健康），永不中继

中继实现沿用 T03 spike 已验证口径：请求体 spool 临时文件按确切 Content-Length
转发（直传 werkzeug 流会被 requests 判为未知长度转 chunked 而挂起）；响应流式回传；
REST 无 WS（方案 §2.1 已核实）。
"""
import shutil
import tempfile
import threading
import time

import requests
from flask import Flask, Response, jsonify, request, send_from_directory

_HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host",
}
_DROP_RESP = {"content-length", "transfer-encoding", "connection", "keep-alive", "content-encoding"}


class RemoteHealth:
    """远程健康状态（T09 探针口径：GET /login 匿名可达即认为服务在）。"""

    def __init__(self):
        self._lock = threading.Lock()
        self.ok = None          # None=未知（未探测），True/False
        self.last_error = ""

    def mark(self, ok, error=""):
        with self._lock:
            self.ok = ok
            if error:
                self.last_error = error

    def snapshot(self):
        with self._lock:
            return {"remote_ok": self.ok, "last_error": self.last_error}


def create_capability_app(build_cfg, client_cfg, address, static_dir,
                          local_plugins_dir, fallback_file, on_address_change=None):
    """装配能力层 Flask 应用。address=None 时所有页面请求走本地兜底页。"""
    app = Flask(
        __name__,
        static_folder=static_dir,
        static_url_path="/static",
    )
    app.config["JSON_AS_ASCII"] = False
    health = RemoteHealth()
    upstream = requests.Session()
    state = {"address": address}
    _probe = {"ts": 0.0, "ok": False}
    _probe_lock = threading.Lock()

    def _remote_alive():
        """5 秒缓存的轻量探针（GET /login，T09 口径）。

        主界面/工具页是本地直出，远程挂死时页面本身仍会加载、前端 API 会卡
        「加载中」——导航门控在这里：探针失败就把页面导航换成本地兜底页，
        远程恢复后自动放行。缓存 5 秒避免每次导航都打远程。
        """
        now = time.time()
        with _probe_lock:
            if now - _probe["ts"] < 5:
                return _probe["ok"]
        ok = False
        if state["address"]:
            try:
                upstream.get(state["address"] + "/login", timeout=2)
                ok = True
            except requests.RequestException:
                ok = False
        with _probe_lock:
            _probe["ts"] = now
            _probe["ok"] = ok
        if not ok:
            health.mark(False, "探针失败：远程不可达")
        return ok

    def _fallback_page():
        resp = send_from_directory(fallback_dir_or(app), "fallback.html")
        resp.status_code = 503
        return resp

    # ---------------- 本地接口（永不中继） ----------------

    @app.get("/local-api/config")
    def local_config():
        return jsonify({
            "address": state["address"] or "",
            "product": build_cfg.get("product"),
            "webview2": build_cfg.get("webview2"),
            "health": health.snapshot(),
        })

    @app.post("/local-api/config/address")
    def local_set_address():
        data = request.get_json(force=True, silent=True) or {}
        from client_config import normalize_address, save_client_config
        addr = normalize_address(data.get("address"))
        cfg = dict(client_cfg)
        cfg["remote_address"] = addr or ""
        save_client_config(build_cfg, cfg)
        state["address"] = addr
        if on_address_change:
            try:
                on_address_change(addr)
            except Exception:
                pass
        return jsonify({"ok": True, "address": addr or ""})

    @app.get("/local-api/health")
    def local_health():
        return jsonify(dict(state["address"] and {"address": state["address"]} or {},
                            **health.snapshot()))

    # ---------------- 本地直出（主界面为客户端自带副本，页面只有一份） ----------------

    @app.get("/")
    def index():
        if not state["address"] or not _remote_alive():
            return _fallback_page()
        return send_from_directory(static_dir, "index.html")

    @app.get("/tool/<tool_id>")
    def tool_page(tool_id):
        # 工具外壳页是通用件：iframe 的 src 决定内容来自远程（类 A）还是本地（类 B/C）
        if tool_id.startswith("local-"):
            return send_from_directory(static_dir, "tool.html")   # 本地插件离线可用
        if not state["address"] or not _remote_alive():
            return _fallback_page()
        return send_from_directory(static_dir, "tool.html")

    @app.get("/favicon.ico")
    def favicon():
        try:
            return send_from_directory(static_dir, "favicon.ico")
        except Exception:
            return Response(b"", status=204)

    @app.get("/plugin/local-<plugin_id>/<path:filename>")
    def local_plugin_assets(plugin_id, filename):
        return send_from_directory(local_plugins_dir, "local-%s/frontend/%s" % (plugin_id, filename))

    @app.get("/local-admin")
    def local_admin():
        return jsonify({"error": "本地插件管理页在批次 4 T21 落地"}), 501

    # ---------------- 本地插件 API（批次 4 T20 执行器） ----------------

    @app.route("/api/local/<plugin_id>/<path:rest>",
               methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
    def local_plugin_api(plugin_id, rest):
        return jsonify({"error": "本地插件执行器未启用（批次 4 T20）"}), 501

    # ---------------- 中继（其余全部转发远程） ----------------

    def client_headers():
        h = {}
        for k, v in request.headers.items():
            if k.lower() in _HOP_HEADERS or k.lower() == "content-length":
                continue
            h[k] = v
        return h

    def forward(path):
        """T03 spike 口径的转发（会话镜像/清单合并为批次 3 T14/T16 增量）。"""
        if not state["address"]:
            return _fallback_response()
        qs = request.query_string.decode("utf-8", "replace")
        url = state["address"] + "/" + path + (("?" + qs) if qs else "")
        headers = client_headers()
        data = None
        if request.method in ("POST", "PUT", "PATCH"):
            body = tempfile.SpooledTemporaryFile(max_size=512 * 1024)
            shutil.copyfileobj(request.stream, body, 1024 * 1024)
            body.seek(0)
            data = body
            if request.content_length:
                headers["Content-Length"] = str(request.content_length)
        try:
            r = upstream.request(request.method, url, headers=headers, data=data,
                                 stream=True, allow_redirects=False, timeout=(5, 300))
        except requests.RequestException as exc:
            health.mark(False, "%s: %s" % (type(exc).__name__, exc))
            return _fallback_response(upstream_error=str(exc))
        health.mark(True)
        out = [(k, v) for k, v in r.headers.items() if k.lower() not in _DROP_RESP]
        return Response(r.iter_content(chunk_size=64 * 1024),
                        status=r.status_code, headers=out)

    def _fallback_response(upstream_error=""):
        # 页面请求 → 本地兜底页；API 请求 → 502 JSON（都不依赖远程渲染）
        if _wants_page():
            resp = send_from_directory(fallback_dir_or(app), "fallback.html")
            resp.status_code = 503
            return resp
        return jsonify({"error": "远程服务器不可达",
                        "detail": upstream_error,
                        "address": state["address"] or ""}), 502

    def _wants_page():
        if request.path.startswith("/api/"):
            return False
        accept = request.headers.get("Accept", "")
        return "text/html" in accept or "*/*" in accept or accept == ""

    @app.route("/", defaults={"path": ""}, methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"])
    @app.route("/<path:path>", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"])
    def relay(path):
        # ② 清单合并（批次 3 T16）——本批次原样中继
        return forward(path)

    # 兜底页目录：由 fallback_file 推导（同一目录）
    def fallback_dir_or(_app):
        import os
        return os.path.dirname(fallback_file)

    app.config["CAPABILITY_HEALTH"] = health
    app.config["CAPABILITY_STATE"] = state
    return app
