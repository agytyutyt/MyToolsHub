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
import io
import json
import os
import re
import shutil
import sys as _sys
import tempfile
import threading
import time
import zipfile

import requests
from flask import Flask, Response, jsonify, request, send_from_directory

import jz_deps
import jz_plugin_host
import local_registry
from jz_relay import JzRelay
from plugin_executor import PluginExecutor
from session_mirror import SessionMirror

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
                          local_plugins_dir, fallback_file, on_address_change=None,
                          data_dir=None, executor=None, log=None):
    """装配能力层 Flask 应用。address=None 时所有页面请求走本地兜底页。"""
    _log = log or (lambda m: None)
    app = Flask(
        __name__,
        static_folder=static_dir,
        static_url_path="/static",
    )
    app.config["JSON_AS_ASCII"] = False
    health = RemoteHealth()
    upstream = requests.Session()
    state = {"address": address}

    # ---- 会话镜像 / 本地插件注册表 / jz_relay 门面（批次 3 T14/T16/T17） ----
    from client_config import data_dir as _cfg_data_dir, whitelist_path
    _data_dir = data_dir or _cfg_data_dir(build_cfg)
    mirror = SessionMirror(os.path.join(_data_dir, "session.bin"), _log)
    local_manifests = local_registry.scan_local_plugins(local_plugins_dir, _log)
    try:
        with open(whitelist_path(), "r", encoding="utf-8") as f:
            whitelist = json.load(f)
    except Exception:
        whitelist = {"allow": []}
        _log("relay_whitelist.json 加载失败，jz_relay 全拒绝")
    relay = JzRelay(upstream, lambda: state["address"], mirror, whitelist, _log)
    _sys.modules["jz_relay"] = relay          # 插件后端 import jz_relay 即用
    app.config["SESSION_MIRROR"] = mirror
    app.config["JZ_RELAY"] = relay
    app.config["LOCAL_MANIFESTS"] = local_manifests

    # ---- 类 C 子应用派发 + 类 B 执行器状态（批次 4 T19/T20；pylibs 注入复用主包机制） ----
    import jz_deps
    jz_plugin_host.setup_dep_components(build_cfg.get("_program_dir") or os.getcwd(), _log)
    local_states = {}
    hybrid_apps = {}      # id → Flask 子应用（进程内；装卸/升级=字典操作，天然全热插拔）

    def _refresh_local_states():
        for m in local_manifests:
            pid = m.get("id")
            pdir = os.path.join(local_plugins_dir, m.get("_dir", ""))
            enabled = (client_cfg.get("local_state") or {}).get(pid, True) \
                and m.get("enabled", True)
            if not enabled:
                local_states[pid] = {"status": "disabled"}
                hybrid_apps.pop(pid, None)
                continue
            if m.get("kind") == "hybrid":
                verdict = jz_deps.evaluate(
                    pid, pdir, m,
                    app_dir=build_cfg.get("_program_dir"),
                    app_version=(build_cfg.get("product") or {}).get("version"),
                    plugin_api=build_cfg.get("plugin_api", "1.0"))
                local_states[pid] = {"status": verdict["status"],
                                     "missing": verdict.get("missing", []),
                                     "degraded": verdict.get("degraded", []),
                                     "reason": verdict.get("reason", "")}
                if verdict["status"] == "blocked":
                    hybrid_apps.pop(pid, None)
                    _log("本地插件 %s 不可运行（%s），跳过挂载" % (pid, verdict.get("reason")))
                    continue
                try:
                    routes = jz_plugin_host.load_backend_module(local_plugins_dir, pid)
                    sub = Flask("local_" + pid.replace("-", "_"))
                    bp = getattr(routes, "bp", None)
                    if bp is not None:
                        sub.register_blueprint(bp)     # 挂载前缀由能力层剥除后派发
                    else:
                        reg = getattr(routes, "register", None)
                        if not callable(reg):
                            raise RuntimeError("未导出 register(app) 或 bp")
                        reg(sub)
                    hybrid_apps[pid] = sub            # 子应用派发：热插拔不受 Flask 首请求后注册限制
                    _log("本地插件子应用已挂载：%s" % pid)
                except Exception as exc:
                    local_states[pid] = {"status": "error", "reason": str(exc)}
                    _log("本地插件 %s 挂载失败，已隔离：%s" % (pid, exc))
            elif (m.get("entry") or {}).get("backend"):
                local_states[pid] = {"status": "ok", "mode": "worker"}

    _refresh_local_states()
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
        return send_from_directory(fallback_dir_or(app), "local_admin.html")

    # ---------------- 本地插件 API（批次 4 T20 执行器：类 B 子进程派发） ----------------

    def _find_local(plugin_id):
        return local_registry.find(local_manifests, plugin_id) or \
            local_registry.find(local_manifests, "local-" + plugin_id)

    @app.route("/api/local/<plugin_id>", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
    @app.route("/api/local/<plugin_id>/<path:rest>",
               methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
    def local_plugin_api(plugin_id, rest=""):
        m = _find_local(plugin_id)
        if m is None:
            return jsonify({"error": "本地插件不存在"}), 404
        pid = m.get("id", plugin_id)
        st = local_states.get(pid, {})
        if st.get("status") == "blocked":
            return jsonify({"error": "本地插件不可运行（依赖缺失）",
                            "missing": st.get("missing", []), "hint": st.get("reason", "")}), 503
        if st.get("status") == "disabled":
            return jsonify({"error": "本地插件已停用"}), 404
        # hybrid → 进程内子应用；worker → 子进程执行器。两者都剥挂载前缀：
        # 显式 mount 优先；未声明（默认 worker）剥规范前缀 /api/local/<id>。
        mount = ((m.get("entry") or {}).get("mount") or "").rstrip("/")
        if not mount:
            mount = "/api/local/" + pid
        worker_path = request.path[len(mount):] or "/"
        worker_headers = {k: v for k, v in request.headers.items()
                          if k.lower() not in ("host", "content-length")}
        if m.get("kind") == "hybrid":
            sub = hybrid_apps.get(pid)
            if sub is None:
                return jsonify({"error": "本地插件子应用未挂载"}), 503
            with sub.test_client() as tc:
                resp = tc.open(worker_path, method=request.method,
                               headers=list(worker_headers.items()),
                               data=request.get_data())
            hop = {"connection", "keep-alive", "transfer-encoding", "content-length"}
            return Response(resp.data, status=resp.status_code,
                            headers=[(k, v) for k, v in resp.headers
                                     if k.lower() not in hop])
        if executor is None:
            return jsonify({"error": "子进程执行器未配置"}), 501
        status, hdrs, body = executor.call(
            m, request.method, worker_path, worker_headers,
            request.get_data(), m.get("timeout_ms") or 60000)
        hop = {"connection", "keep-alive", "transfer-encoding", "content-length"}
        return Response(body, status=status,
                        headers=[(k, v) for k, v in (hdrs or {}).items()
                                 if k.lower() not in hop])

    # ---------------- 本地插件管理（批次 4 T21，GD7 A+C） ----------------

    @app.get("/local-api/plugins")
    def lp_list():
        out = []
        for m in local_manifests:
            st = dict(local_states.get(m.get("id"), {}))
            out.append({"id": m.get("id"), "name": m.get("name"),
                        "version": m.get("version"), "kind": m.get("kind"),
                        "mount": (m.get("entry") or {}).get("mount"),
                        "enabled": st.get("status") != "disabled", "state": st,
                        "requires": m.get("requires") or {}})
        return jsonify({"plugins": out, "store": build_cfg.get("plugin_store") or {}})

    @app.post("/local-api/plugins/install")
    def lp_install():
        f = request.files.get("file")
        if f is None:
            return jsonify({"error": "未选择插件包"}), 400
        try:
            zf = zipfile.ZipFile(io.BytesIO(f.read()))
        except zipfile.BadZipFile:
            return jsonify({"error": "插件包不是有效的 zip"}), 400
        names = zf.namelist()
        if "manifest.json" not in names:
            return jsonify({"error": "包根缺少 manifest.json"}), 400
        for n in names:                                    # 路径安全（防 ../ 逃逸）
            if n.startswith("/") or ".." in n.split("/"):
                return jsonify({"error": "插件包含不安全路径：%s" % n}), 400
        try:
            m = json.loads(zf.read("manifest.json").decode("utf-8"))
        except Exception as exc:
            return jsonify({"error": "manifest.json 不可解析：%s" % exc}), 400
        pid = m.get("id", "")
        if m.get("runtime") != "local" or not pid.startswith("local-") \
                or not _is_safe_id(pid):
            return jsonify({"error": "id 必须 local- 前缀且 runtime=local"}), 400
        kind = m.get("kind", "local")
        if kind not in ("local", "hybrid"):
            return jsonify({"error": "kind 仅支持 local / hybrid"}), 400
        frontend = (m.get("entry") or {}).get("frontend") or "index.html"
        if "frontend/" + frontend not in names:
            return jsonify({"error": "包内缺少 frontend/%s" % frontend}), 400
        if kind == "hybrid":
            backend = (m.get("entry") or {}).get("backend")
            if not backend or "backend/" + backend.split("/")[-1] not in names:
                return jsonify({"error": "hybrid 插件缺少 backend"}), 400
        mount = ((m.get("entry") or {}).get("mount") or "").rstrip("/")
        if kind == "hybrid" and not mount.startswith("/api/local/" + pid):
            return jsonify({"error": "mount 必须以 /api/local/%s 开头" % pid}), 400
        for other in local_manifests:                       # mount 冲突检查（双方均非空才比）
            om = ((other.get("entry") or {}).get("mount") or "").rstrip("/")
            if other.get("id") != pid and om and mount and (
                    mount == om or mount.startswith(om + "/")
                    or om.startswith(mount + "/")):
                return jsonify({"error": "mount 与 %s 冲突" % other.get("id")}), 400
        target = os.path.join(local_plugins_dir, pid)
        if os.path.isdir(target):
            shutil.rmtree(target)                           # 升级 = 覆盖替换（zip 根即包根）
        zf.extractall(target)
        _rescan()
        restart_required = kind == "hybrid"
        return jsonify({"ok": True, "id": pid, "restart_required": restart_required,
                        "hint": "类 C 插件需重启能力层生效（会话已持久化，免重登）"
                        if restart_required else ""})

    @app.post("/local-api/plugins/uninstall")
    def lp_uninstall():
        data = request.get_json(force=True, silent=True) or {}
        pid = data.get("id", "")
        target = os.path.join(local_plugins_dir, pid)
        if not pid.startswith("local-") or not os.path.isdir(target):
            return jsonify({"error": "本地插件不存在"}), 404
        kind = next((m.get("kind") for m in local_manifests if m.get("id") == pid), "local")
        shutil.rmtree(target, ignore_errors=True)
        _rescan()
        return jsonify({"ok": True, "restart_required": kind == "hybrid"})

    @app.post("/local-api/plugins/state")
    def lp_state():
        data = request.get_json(force=True, silent=True) or {}
        pid = data.get("id", "")
        if not pid.startswith("local-"):
            return jsonify({"error": "非法 id"}), 400
        from client_config import load_client_config, save_client_config
        cfg = dict(load_client_config(build_cfg))
        states = dict(cfg.get("local_state") or {})
        states[pid] = bool(data.get("enabled"))
        cfg["local_state"] = states
        save_client_config(build_cfg, cfg)
        _rescan()
        kind = next((m.get("kind") for m in local_manifests if m.get("id") == pid), "local")
        return jsonify({"ok": True, "restart_required": kind == "hybrid" and data.get("enabled")})

    @app.post("/local-api/plugins/check-update")
    def lp_check_update():
        store = build_cfg.get("plugin_store") or {}
        idx = store.get("index_path") or ""
        if not idx or not os.path.isfile(idx):
            return jsonify({"error": "未配置共享盘索引或索引不存在", "updates": []}), 200
        try:
            with open(idx, "r", encoding="utf-8") as f:
                index = json.load(f)
        except Exception as exc:
            return jsonify({"error": "索引不可解析：%s" % exc, "updates": []}), 200
        updates = []
        for entry in index.get("packages", []):
            pid = entry.get("id", "")
            cur = next((m.get("version") for m in local_manifests if m.get("id") == pid), None)
            if cur is None or _ver_lt(cur, entry.get("version", "")):
                updates.append({"id": pid, "version": entry.get("version"),
                                "file": entry.get("file", ""), "changelog": entry.get("changelog", "")})
        return jsonify({"updates": updates})

    @app.post("/local-api/restart")
    def lp_restart():
        """类 C 升级/装卸后的能力层秒级重启（§6.7；会话镜像已持久化，免重登）。"""
        import subprocess as _sp
        exe = sys.executable if getattr(sys, "frozen", False) else None
        if not exe:
            return jsonify({"ok": False, "error": "仅打包态支持自重启（源码态请手动重启）"}), 400
        def _reexec():
            time.sleep(0.6)
            os._exit(0)
        threading.Thread(target=_reexec, daemon=True).start()
        # 打包态由外部守护拉起（批次 2 托盘/批次 5 安装器配合）；当前口径：退出即由安装器/脚本重启
        return jsonify({"ok": True, "note": "能力层即将退出，请由启动器重新拉起"})

    def _ver_lt(a, b):
        def nums(v):
            return [int(x) for x in re.findall(r"\d+", str(v))]
        return nums(a) < nums(b) if nums(a) and nums(b) else False

    def _is_safe_id(pid):
        import re as _re
        return bool(_re.fullmatch(r"local-[A-Za-z0-9_-]+", pid))

    def _rescan():
        nonlocal local_manifests
        local_manifests[:] = local_registry.scan_local_plugins(local_plugins_dir, _log)
        app.config["LOCAL_MANIFESTS"] = local_manifests
        local_states.clear()
        _refresh_local_states()
        alive = {m.get("id") for m in local_manifests}
        for pid in list(hybrid_apps):
            if pid not in alive:
                hybrid_apps.pop(pid, None)    # 卸载即弹（热插拔 L2/L3）

    # ---------------- 中继（其余全部转发远程） ----------------

    def client_headers():
        h = {}
        for k, v in request.headers.items():
            if k.lower() in _HOP_HEADERS or k.lower() == "content-length":
                continue
            h[k] = v
        # 会话镜像补位（T14）：浏览器 Cookie 优先，同名镜像让位
        mc = mirror.header()
        if mc:
            existing = h.pop("Cookie", None)
            parts = [p.strip() for p in existing.split(";")] if existing else []
            name = mc.split("=", 1)[0]
            if not any(p.startswith(name + "=") for p in parts):
                parts.append(mc)
            if parts:
                h["Cookie"] = "; ".join(parts)
        return h

    def forward(path):
        """T03 spike 口径的转发 + 会话镜像维护（T14）。"""
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
        sc = r.headers.get("Set-Cookie")          # 滑动续期/登录都会重发
        if sc:
            mirror.update_from_set_cookie(sc)
        if r.status_code == 401:                  # 会话失效统一信号
            mirror.clear()
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

    # ---------------- 清单合并与本地工具元数据（T16） ----------------

    @app.get("/api/tools")
    def api_tools_merged():
        if not state["address"]:
            return _fallback_response()
        headers = client_headers()
        try:
            r = upstream.get(state["address"] + "/api/tools",
                             headers=headers, timeout=(5, 60))
        except requests.RequestException as exc:
            health.mark(False, "%s: %s" % (type(exc).__name__, exc))
            return _fallback_response(upstream_error=str(exc))
        health.mark(True)
        sc = r.headers.get("Set-Cookie")
        if sc:
            mirror.update_from_set_cookie(sc)
        if r.status_code == 401:
            mirror.clear()
        if r.status_code != 200:
            return Response(r.content, status=r.status_code,
                            content_type=r.headers.get("Content-Type", "application/json"))
        try:
            data = r.json()
        except Exception:
            return Response(r.content, status=200,
                            content_type=r.headers.get("Content-Type", "application/json"))
        base = data.get("tools") if isinstance(data, dict) else data
        base = base if isinstance(base, list) else []
        start = max([t.get("order", 0) for t in base] or [0]) + 1000
        merged = local_registry.tool_items(local_manifests, start)
        categories = data.get("categories") if isinstance(data, dict) else None
        if isinstance(categories, list) and all(c.get("id") != "local" for c in categories):
            categories.append(dict(local_registry.LOCAL_CATEGORY))
        if isinstance(data, dict):
            data["tools"] = base + merged
        else:
            data = base + merged
        return jsonify(data)

    @app.get("/api/tools/<tool_id>")
    def api_tool_meta(tool_id):
        if tool_id.startswith("local-"):
            m = local_registry.find(local_manifests, tool_id)
            if m is None:
                return jsonify({"error": "本地插件不存在"}), 404
            return jsonify(local_registry.tool_item(m, 0))
        return forward("api/tools/" + tool_id)

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
