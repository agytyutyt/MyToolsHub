# -*- coding: utf-8 -*-
"""能力层装配（批次 1 T08/T09）：路由分区四路裁决 + 会话中继骨架 + 本地直出。

路由分区（方案 §6.1 表；业务逻辑禁入本层）：
  ① 本地直出：/ 、/tool/<id>（客户端自带 static 副本）、/plugin/local-*/ 、/local-admin（批次 4）
  ①′ server 前端包直出：/plugin/<id>/<file> 且命中已装前端包文件清单（批次 8 T-O2-4，
      需求⑧：未装/停用自动回退中继直出）
  ② 清单合并：/api/tools*（批次 3 T16；批次 8 起隐藏已停用条目）
  ③ 本地插件 API：/api/local/<id>/*（批次 4 T20 执行器，本批次占位）
  ④ 中继远程：其余 /api/* 与类 A /plugin/<id>/*
  ⑤ /local-api/*：客户端自有本地接口（配置/健康/商店安装），永不中继

中继实现沿用 T03 spike 已验证口径：请求体 spool 临时文件按确切 Content-Length
转发（直传 werkzeug 流会被 requests 判为未知长度转 chunked 而挂起）；响应流式回传；
REST 无 WS（方案 §2.1 已核实）。
"""
import json
import os
import re
import shutil
import sys as _sys
import tempfile
import threading
import uuid
import time

import requests
from flask import Flask, Response, jsonify, request, send_from_directory

import jz_deps
import jz_plugin_host
import local_registry
import pkg_install
import plugin_source
from client_config import hidden_tools, set_hidden_tool
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

    # ---- 本地插件身份注入（批次 9 T-O9-4 / F1，Q4 拍板）----
    # 登录/续期后经上游 /api/session 缓存真实身份：hybrid 经 jz_api provider 生效，
    # worker 经派发链 X-JZ-User 头生效（jz_api 垫片兜底读头）；离线/未登录降级匿名。
    _identity = {"user": None, "ts": 0.0}

    def refresh_identity():
        if state["address"] and time.time() - _identity["ts"] >= 300:
            try:
                r = upstream.get(state["address"] + "/api/session",
                                 headers=client_headers(), timeout=(3, 8))
                user = (r.json() or {}).get("user") if r.status_code == 200 else None
                _identity["user"] = user if isinstance(user, dict) else None
            except Exception:
                _identity["user"] = None
            _identity["ts"] = time.time()
        return _identity["user"]

    def invalidate_identity():
        _identity["user"] = None
        _identity["ts"] = 0.0

    _mirror_update, _mirror_clear = mirror.update_from_set_cookie, mirror.clear

    def _mirror_update_hook(set_cookie):
        _mirror_update(set_cookie)            # 登录/滑动续期/删除型 Cookie → 身份可能变化
        invalidate_identity()

    def _mirror_clear_hook():
        _mirror_clear()                       # 登出/401 → 身份立即作废
        invalidate_identity()

    mirror.update_from_set_cookie = _mirror_update_hook
    mirror.clear = _mirror_clear_hook

    try:
        import jz_api as _jz_api
        _jz_api.register_providers(get_session_user=refresh_identity)
    except Exception as exc:
        _log("jz_api 身份 provider 注册失败（本地插件按匿名降级）：%r" % exc)
    app.config["SESSION_MIRROR"] = mirror
    app.config["JZ_RELAY"] = relay
    app.config["LOCAL_MANIFESTS"] = local_manifests

    # ---- 类 C 子应用派发 + 类 B 执行器状态（批次 4 T19/T20；pylibs 注入复用主包机制） ----
    import jz_deps
    _program_dir = build_cfg.get("_program_dir") or os.getcwd()
    # 暂存中的依赖组件先应用（与主包 app.py 同口径：setup 之前、DLL 未映射时），再注入路径
    try:
        _applied = jz_deps.apply_pending_components(_program_dir)
        if _applied:
            _log("已应用暂存依赖组件：%s" % ", ".join(_applied))
    except Exception as _exc:
        _log("应用暂存依赖组件失败（保留暂存，下次启动再试）：%s" % _exc)
    jz_plugin_host.setup_dep_components(_program_dir, _log)
    # 装依赖组件后免重启：jz_deps.refresh_flags 重算前先补注入路径（与主包同款注册）
    jz_deps.register_component_rescanner(
        lambda: jz_plugin_host.setup_dep_components(_program_dir, _log))
    # server 前端包目录（需求⑧：<数据目录>\server-frontends\，与 local-plugins\ 同级）
    frontends_dir = os.path.join(_data_dir, "server-frontends")
    frontends = {f["id"]: f for f in pkg_install.scan_frontends(frontends_dir)}
    local_states = {}
    hybrid_apps = {}      # id → Flask 子应用（进程内；装卸/升级=字典操作，天然全热插拔）

    def _refresh_local_states():
        from client_config import load_client_config
        # 停用状态读磁盘最新配置（lp_state 免重启写盘后 rescan 立即生效，不用启动快照）
        states_cfg = (load_client_config(build_cfg).get("local_state") or {})
        for m in local_manifests:
            pid = m.get("id")
            pdir = os.path.join(local_plugins_dir, m.get("_dir", ""))
            enabled = states_cfg.get(pid, True) and m.get("enabled", True)
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

    @app.get("/plugin/<plugin_id>/<path:filename>")
    def server_frontend_assets(plugin_id, filename):
        """server 前端包本地直出（需求⑧）：仅 GET 且文件命中已装前端包清单才本地伺服；
        其余（未安装/已停用/清单外路径/非 GET）照旧中继——零安装体验与浏览器路径不破坏。"""
        fe = frontends.get(plugin_id)
        if request.method == "GET" and fe and _frontend_enabled(plugin_id) \
                and filename in (fe.get("files") or []):
            return send_from_directory(os.path.join(frontends_dir, plugin_id), filename)
        return forward("plugin/%s/%s" % (plugin_id, filename))

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
        qs = request.query_string.decode("utf-8", "replace")
        if qs:                                # 查询串随派发透传（file-filter mappings?limit 等在用）
            worker_path += "?" + qs
        worker_headers = {k: v for k, v in request.headers.items()
                          if k.lower() not in ("host", "content-length")}
        user = refresh_identity()             # F1：worker 链身份注入（仅本地回环派发链写入）
        if user:
            worker_headers["X-JZ-User"] = json.dumps(user, ensure_ascii=False)
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
            request.get_data(), m.get("timeout_ms") or 60000,
            user=(user or {}).get("username") or "-")      # V9：审计落真实身份
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
                        "server_data": bool(m.get("server_data")),   # V5：数据出机明示
                        "requires": m.get("requires") or {}})
        # server 前端包（需求⑧）与已装依赖组件（dep-slot）：商店页状态感知的依据
        from client_config import load_client_config
        cfg_states = (load_client_config(build_cfg).get("local_state") or {})
        fronts = []
        for fid, f in frontends.items():
            fronts.append({"id": fid, "version": f.get("version"),
                           "files": len(f.get("files") or []),
                           "enabled": bool(cfg_states.get(fid, True))})
        return jsonify({"plugins": out, "store": build_cfg.get("plugin_store") or {},
                        "server_frontends": fronts,
                        "hidden_tools": hidden_tools(build_cfg),
                        "components": pkg_install.installed_components(_program_dir)})

    @app.get("/local-api/plugins/hidden")
    def lp_hidden_list():
        """本机从主界面移除的服务器侧工具 id 列表（客户端本机显示状态）。"""
        return jsonify({"hidden_tools": hidden_tools(build_cfg)})

    @app.post("/local-api/plugins/hidden")
    def lp_hidden_set():
        """写本机显示状态：{id, hidden}。桌面端「卸载」置 hidden=true、「安装」置 false。

        **只改客户端数据目录的 config.json**（服务器端后端包/运行状态一概不动；
        服务器端撤下只发生在商店管理·超级管理员的 停用/卸载）。
        """
        data = request.get_json(silent=True) or {}
        tid = str(data.get("id") or "").strip()
        hidden = bool(data.get("hidden"))
        if not tid or not re.fullmatch(r"[A-Za-z0-9_-]+", tid):
            return jsonify({"ok": False, "error": "id 非法：%r" % tid}), 400
        ids = set_hidden_tool(build_cfg, tid, hidden)
        _log("本机工具显示状态：%s %s（hidden_tools=%s）"
             % ("已移除" if hidden else "已恢复", tid, ids))
        return jsonify({"ok": True, "hidden_tools": ids})

    @app.post("/local-api/plugins/install")
    def lp_install():
        f = request.files.get("file")
        if f is None:
            return jsonify({"error": "未选择插件包"}), 400
        # 校验+落盘收口到 pkg_install 单入口（批次 8 接缝③；外部行为不变）。
        # 临时名唯一（P2）：并发上传不互踩（原固定 .upload.tmp 有对撞风险）。
        os.makedirs(local_plugins_dir, exist_ok=True)
        fd, spool = tempfile.mkstemp(prefix="upload-", suffix=".zip", dir=local_plugins_dir)
        os.close(fd)
        try:
            f.save(spool)
            result = pkg_install.install_local_zip(spool, local_plugins_dir,
                                                   local_manifests, _log)
        finally:
            try:
                os.remove(spool)
            except OSError:
                pass
        if not result.get("ok"):
            return jsonify({"error": result.get("error", "安装失败")}), 400
        _rescan()
        return jsonify({"ok": True, "id": result["id"],
                        "restart_required": result.get("restart_required"),
                        "hint": result.get("hint", "")})

    @app.post("/local-api/plugins/uninstall")
    def lp_uninstall():
        data = request.get_json(force=True, silent=True) or {}
        pid = data.get("id", "")
        if not pid.startswith("local-"):
            # server 前端本地副本（需求⑧生命周期）：删副本 → 自动回退中继直出
            r = pkg_install.remove_frontend(frontends_dir, pid)
            if not r.get("ok"):
                return jsonify({"error": r.get("error", "卸载失败")}), 404
            _rescan_frontends()
            return jsonify({"ok": True, "restart_required": False})
        target = os.path.join(local_plugins_dir, pid)
        if not os.path.isdir(target):
            return jsonify({"error": "本地插件不存在"}), 404
        kind = next((m.get("kind") for m in local_manifests if m.get("id") == pid), "local")
        shutil.rmtree(target, ignore_errors=True)
        purged = False
        if data.get("purge_data"):
            # 清除数据（可选）：数据根 plugins/<id>（配置/缓存/本地业务数据）；id 先严格校验防穿越
            import re as _re
            if not _re.fullmatch(r"[A-Za-z0-9_-]{1,64}", str(pid)):
                return jsonify({"error": "非法 id，拒绝清除数据"}), 400
            droot = os.path.join(_data_dir, "plugins", pid)
            base = os.path.abspath(os.path.join(_data_dir, "plugins"))
            if os.path.abspath(droot).startswith(base + os.sep) and os.path.isdir(droot):
                shutil.rmtree(droot, ignore_errors=True)
                purged = True
        _rescan()
        return jsonify({"ok": True, "restart_required": kind == "hybrid", "data_purged": purged})

    @app.post("/local-api/plugins/state")
    def lp_state():
        data = request.get_json(force=True, silent=True) or {}
        pid = data.get("id", "")
        if not pid.startswith("local-") and pid not in frontends:
            return jsonify({"error": "非法 id"}), 400
        from client_config import load_client_config, save_client_config
        cfg = dict(load_client_config(build_cfg))
        states = dict(cfg.get("local_state") or {})
        states[pid] = bool(data.get("enabled"))
        cfg["local_state"] = states
        save_client_config(build_cfg, cfg)
        if pid.startswith("local-"):
            _rescan()
            kind = next((m.get("kind") for m in local_manifests if m.get("id") == pid), "local")
            return jsonify({"ok": True,
                            "restart_required": kind == "hybrid" and data.get("enabled")})
        # server 前端：停用 = 本地伺服关闭自动回退中继直出（副本保留），即时生效
        return jsonify({"ok": True, "restart_required": False})

    @app.post("/local-api/plugins/check-update")
    def lp_check_update():
        """检查更新：经 Source 抽象读目录（server=商店 / share=共享盘，接缝①②收口）。"""
        from client_config import load_client_config
        src = plugin_source.make_source(build_cfg, load_client_config(build_cfg),
                                        upstream, lambda: state["address"], mirror, _log)
        catalog, err = src.get_catalog()
        if err:
            return jsonify({"error": err, "source": src.type, "updates": []}), 200
        updates = []
        installed = {m.get("id"): m.get("version") for m in local_manifests}
        for fid, f in frontends.items():                 # server 前端本地副本也参与比对
            installed.setdefault(fid, f.get("version"))
        for entry in catalog.get("packages") or []:
            pid = str(entry.get("id") or "")
            cur = installed.get(pid)
            if cur is None or _ver_lt(cur, entry.get("version", "")):
                updates.append({"id": pid, "version": entry.get("version"),
                                "file": entry.get("file", ""),
                                "changelog": entry.get("changelog", ""),
                                "runtime": entry.get("runtime", "local")})
        return jsonify({"updates": updates, "source": src.type})

    @app.post("/local-api/restart")
    def lp_restart():
        """类 C 升级 / DLL 依赖安装后的能力层重启（§6.7；会话镜像已持久化，免重登）。

        接棒方式（2026-10-08 改版）：新实例带 --await-pid <本进程 pid> 拉起，由**新实例**
        用 WaitForSingleObject 等旧进程真实退出后再抢单实例互斥——不看时间差，不用 cmd。
        原实现两处致命伤：①裸名 sys（本文件是 import sys as _sys）→ NameError → 端点
        从未成功执行过（前端只见 500 HTML，报「自动重启失败」）；②cmd+timeout 的 1 秒
        延迟在无控制台进程里不可靠。现打包态与源码态同法支持。"""
        import subprocess as _sp
        my_pid = os.getpid()
        if getattr(_sys, "frozen", False):
            argv = [_sys.executable, "--await-pid", str(my_pid)]
            cwd = os.path.dirname(_sys.executable)
        else:
            script = os.path.abspath(_sys.argv[0])
            argv = [_sys.executable, script, "--await-pid", str(my_pid)]
            cwd = os.path.dirname(script)
        flags = getattr(_sp, "DETACHED_PROCESS", 0) | getattr(_sp, "CREATE_NEW_PROCESS_GROUP", 0)
        try:
            proc = _sp.Popen(argv, cwd=cwd, creationflags=flags, close_fds=True)
        except Exception as exc:
            _log("restart：拉起新实例失败：%r（argv=%r）" % (exc, argv))
            return jsonify({"ok": False, "error": "拉起新实例失败：%s" % exc}), 500
        _log("restart：新实例已拉起 pid=%s（%s）；本进程 pid=%s 将于 0.5s 后退出接棒"
             % (proc.pid, " ".join(argv), my_pid))

        def _reexec():
            time.sleep(0.5)
            _log("restart：旧进程退出（os._exit 0）")
            os._exit(0)

        threading.Thread(target=_reexec, daemon=True, name="restart-exit").start()
        return jsonify({"ok": True, "note": "客户端即将重启（新实例待本进程退出后接棒）"})

    def _ver_lt(a, b):
        def nums(v):
            return [int(x) for x in re.findall(r"\d+", str(v))]
        return nums(a) < nums(b) if nums(a) and nums(b) else False

    # ---------------- 插件商店安装闭环（批次 8 T-O2-4，需求⑤⑧；§3.5） ----------------
    # 能力层独立取 catalog 与拉包（以用户会话；不信任页面传参的 URL/哈希，只收 id+version），
    # sha256 必校验，安装按 runtime 分派：local=完整插件 / server=前端包 / dep-slot=依赖包。

    def _store_source():
        from client_config import load_client_config
        return plugin_source.make_source(build_cfg, load_client_config(build_cfg),
                                         upstream, lambda: state["address"], mirror, _log)

    def _find_catalog_entry(catalog, pid, version):
        entry = next((p for p in catalog.get("packages") or []
                      if str(p.get("id")) == pid), None)
        if entry is None:
            return None, "商店目录中没有条目：%s" % pid
        if version:
            ver = entry if str(entry.get("version")) == str(version) else None
            if ver is None:
                return None, "条目 %s 没有版本 %s（可能已下架）" % (pid, version)
        else:
            ver = entry
        return ver, None

    def _pull_and_verify(src, pid, version, expect_sha, filename, artifact=None, progress=None):
        """拉包 → spool 落盘 → sha256 对 catalog 声明值（防传输损坏，C7）。返回本地路径或 (None, err)。"""
        spool_dir = os.path.join(_data_dir, "downloads")
        dest = os.path.join(spool_dir, os.path.basename(filename or ("%s-%s.zip" % (pid, version))))
        path, err = src.download(pid, version, dest, artifact=artifact, progress=progress)
        if err:
            return None, err
        if expect_sha:
            actual = pkg_install.sha256_file(path)
            if actual.lower() != str(expect_sha).lower():
                try:
                    os.remove(path)
                except OSError:
                    pass
                return None, "包 sha256 与目录声明不符（介质损坏或被篡改）"
        return path, None

    @app.post("/local-api/store/preflight")
    def store_preflight():
        """依赖三态预检（§3.5 ①）：requires 逐项评估 + 缺依赖映射到可装依赖包（O1 会合点）。"""
        data = request.get_json(force=True, silent=True) or {}
        pid = str(data.get("id") or "")
        version = str(data.get("version") or "")
        catalog, err = _store_source().get_catalog()
        if err:
            return jsonify({"error": err, "installable": False, "deps": []}), 200
        ver, err = _find_catalog_entry(catalog, pid, version)
        if err:
            return jsonify({"error": err, "installable": False, "deps": []}), 200
        manifest = {"requires": ver.get("requires") or {}}
        try:
            verdict = jz_deps.evaluate("store:" + pid, "", manifest,
                                       app_dir=_program_dir,
                                       app_version=(build_cfg.get("product") or {}).get("version"),
                                       plugin_api=build_cfg.get("plugin_api", "1.0"))
        except Exception as exc:      # 预检是建议性面（D-8：提示不阻断），评估炸了不拦安装
            _log("preflight 依赖评估异常（按可安装继续）：%s" % exc)
            return jsonify({"installable": True, "reason": "", "deps": [],
                            "preflight_error": str(exc)})
        # 缺依赖 → 商店依赖类别有对应包则标 resolve=dep-package（页面给「同时安装」勾选）；
        # external/vendored 行作提示性返回（P2-5）：不触发确认联动——vendored 随包自带此刻
        # 必然「缺失」（尚未安装），external 由插件挂载时的三态判定兜底展示。
        dep_entries = [p for p in catalog.get("packages") or []
                       if p.get("runtime") == "dep-slot"]
        # 服务端（A 类）条目的依赖属服务器侧：客户端装的是前端包，本地有没有 docx/flask
        # 不决定安装结果——行标 informational（前端不联动本地「同时安装」，2026-10-08）。
        server_side = (ver.get("runtime") == "server")
        deps = []
        for row in verdict.get("detail") or []:
            if row.get("state") == "ok":
                continue
            kind = row.get("kind")
            if kind == "python_package":
                name = str(row.get("name") or "")
                resolve = "missing"
                for dep in dep_entries:
                    provides = [str(x) for x in (dep.get("provides") or [])]
                    if dep.get("id") == name or name in provides:
                        deps.append({"name": name, "require": row.get("require"),
                                     "state": row.get("state"),
                                     "required": row.get("required", True),
                                     "informational": server_side,
                                     "resolve": "dep-package", "dep_id": dep.get("id"),
                                     "dep_version": dep.get("version")})
                        break
                else:
                    deps.append({"name": name, "require": row.get("require"),
                                 "state": row.get("state"),
                                 "required": row.get("required", True),
                                 "informational": server_side,
                                 "resolve": "missing"})
            elif kind in ("external", "vendored"):
                deps.append({"name": str(row.get("name") or ""),
                             "require": row.get("require"), "state": row.get("state"),
                             "required": row.get("required", True),
                             "resolve": "manual", "informational": True})
        blocked_framework = bool(verdict.get("reason"))
        return jsonify({"installable": not blocked_framework,
                        "reason": verdict.get("reason", ""),
                        "deps": deps})

    # 安装进度台账（2026-10-08：安装按钮内的进度条）。token 由前端生成随 install 请求带上，
    # 只读展示面：拉包占每包权重 35%、解压占 65%，按目录声明 size 加权折算单调百分比。
    _install_jobs = {}   # token → {status, phase, pct, error}

    @app.get("/local-api/store/install/progress")
    def store_install_progress():
        st = _install_jobs.get(request.args.get("id") or "")
        return jsonify(st or {"status": "unknown"})

    class _InstallProgress(object):
        """多包安装 → 单调百分比（权重=目录 size，缺省等权）。写台账失败静默，绝不影响安装。"""

        def __init__(self, token):
            self.token = token
            self.weights = [1]
            self.total = 1.0
            self.idx = 0
            self.done_w = 0.0
            self.pct = 0
            self.label = ""

        def set_plan(self, weights):
            self.weights = [max(int(w or 0), 1) for w in weights] or [1]
            self.total = float(sum(self.weights))
            self.idx, self.done_w = 0, 0.0
            self._push("准备", 0)

        def _push(self, phase, pct):
            self.pct = max(self.pct, min(99, int(pct)))
            if not self.token:
                return
            try:
                _install_jobs[self.token] = {"status": "running", "phase": phase,
                                             "pct": self.pct, "error": ""}
                while len(_install_jobs) > 200:          # 台账上界（淘汰最旧）
                    _install_jobs.pop(next(iter(_install_jobs)))
            except Exception:
                pass

        def _w(self):
            return self.weights[min(self.idx, len(self.weights) - 1)]

        def begin(self, idx, label):
            self.idx, self.label = idx, label
            self._push("准备安装 %s" % label, self.done_w / self.total * 100)

        def pull(self, received, total):
            frac = (float(received) / float(total)) if total else 0.0
            self._push("下载 %s" % self.label, (self.done_w + self._w() * 0.35 * frac)
                       / self.total * 100)

        def extract(self, done, total):
            frac = (float(done) / float(total)) if total else 0.0
            self._push("解压 %s" % self.label, (self.done_w + self._w() * (0.35 + 0.65 * frac))
                       / self.total * 100)

        def pkg_done(self):
            self.done_w += self._w()
            self._push("%s 完成" % self.label, self.done_w / self.total * 100)

    def _prog_cb(prog, kind):
        """进度回调包装（prog 为空返回 None；回调内异常吞掉，绝不打断安装）。"""
        if prog is None:
            return None

        def _cb(a, b):
            try:
                getattr(prog, kind)(a, b)
            except Exception:
                pass

        return _cb

    @app.post("/local-api/store/install")
    def store_install():
        """商店安装（§3.5 ③）：拉包 → sha256 → pkg_install 单入口 → 落位 → rescan。"""
        data = request.get_json(force=True, silent=True) or {}
        pid = str(data.get("id") or "")
        version = str(data.get("version") or "")
        include_deps = bool(data.get("include_deps"))
        src = _store_source()
        catalog, err = src.get_catalog()
        if err:
            return jsonify({"ok": False, "error": err}), 200
        ver, err = _find_catalog_entry(catalog, pid, version)
        if err:
            return jsonify({"ok": False, "error": err}), 200
        runtime = ver.get("runtime") or "local"

        # 进度台账（可选）：权重 = 依赖包 size（按序）+ 主件 size；顺序与下方安装顺序一致
        prog = None
        main_idx = 0
        if data.get("progress_id"):
            prog = _InstallProgress(str(data.get("progress_id"))[:64])
            dep_w = []
            for dep in (data.get("deps") or []):
                dep_id = str(dep.get("dep_id") or "")
                if not dep_id:
                    continue
                de = next((p for p in catalog.get("packages") or []
                           if p.get("id") == dep_id and p.get("runtime") == "dep-slot"), None)
                dep_w.append((de or {}).get("size"))
            prog.set_plan(dep_w + [ver.get("size")])
            main_idx = len(dep_w)

        # ① 联合安装依赖（include_deps 勾选；dep-slot 条目逐个装，失败不阻断主件）
        dep_results = []
        if include_deps:
            dep_list = [d for d in (data.get("deps") or []) if str(d.get("dep_id") or "")]
            for i, dep in enumerate(dep_list):
                dep_id = str(dep.get("dep_id") or "")
                dep_ver = str(dep.get("dep_version") or "")
                d_entry = next((p for p in catalog.get("packages") or []
                                if p.get("id") == dep_id and p.get("runtime") == "dep-slot"), None)
                if d_entry is None or (dep_ver and str(d_entry.get("version")) != dep_ver):
                    dep_results.append({"id": dep_id, "ok": False, "error": "目录中找不到该依赖包"})
                    continue
                if prog:
                    prog.begin(i, "依赖 %s" % dep_id)
                d_path, d_err = _pull_and_verify(src, dep_id, d_entry.get("version"),
                                                 d_entry.get("sha256"), d_entry.get("file"),
                                                 progress=_prog_cb(prog, "pull"))
                if d_err:
                    dep_results.append({"id": dep_id, "ok": False, "error": d_err})
                    continue
                d_res = pkg_install.install_component_zip(d_path, _program_dir,
                                                          progress=_prog_cb(prog, "extract"))
                try:
                    os.remove(d_path)
                except OSError:
                    pass
                dep_results.append(dict(d_res, id=dep_id))
                if prog:
                    prog.pkg_done()

        # ② 主件按 runtime 分派
        if runtime == "server":
            # 需求⑧：桌面端「安装」server 条目 = 仅装前端包（页面本地直出，API 照旧中继）
            if not ver.get("frontend_file"):
                return jsonify({"ok": False,
                                "error": "该条目没有前端包工件（服务器侧尚未发布前端包）"}), 200
            if prog:
                prog.begin(main_idx, "前端包 %s" % pid)
            path, err = _pull_and_verify(src, pid, ver.get("version"),
                                         ver.get("frontend_sha256"), ver.get("frontend_file"),
                                         artifact="frontend",
                                         progress=_prog_cb(prog, "pull"))
            if err:
                return jsonify({"ok": False, "error": err, "dep_results": dep_results}), 200
            res = pkg_install.install_frontend_zip(path, frontends_dir,
                                                   progress=_prog_cb(prog, "extract"))
            try:
                os.remove(path)
            except OSError:
                pass
            if not res.get("ok"):
                return jsonify({"ok": False, "error": res.get("error"),
                                "dep_results": dep_results}), 200
            _rescan_frontends()
            return jsonify({"ok": True, "id": res["id"], "frontend": True,
                            "version": res.get("version"), "restart_required": False,
                            "dep_results": dep_results})

        if runtime == "dep-slot":
            if prog:
                prog.begin(main_idx, "依赖包 %s" % pid)
            path, err = _pull_and_verify(src, pid, ver.get("version"),
                                         ver.get("sha256"), ver.get("file"),
                                         progress=_prog_cb(prog, "pull"))
            if err:
                return jsonify({"ok": False, "error": err, "dep_results": dep_results}), 200
            res = pkg_install.install_component_zip(path, _program_dir,
                                                    progress=_prog_cb(prog, "extract"))
            try:
                os.remove(path)
            except OSError:
                pass
            if not res.get("ok"):
                return jsonify({"ok": False, "error": res.get("error"),
                                "dep_results": dep_results}), 200
            return jsonify({"ok": True, "id": res["id"], "kind": "component",
                            "restart_required": res.get("pending", False),
                            "hint": "组件含 DLL，下次启动能力层时生效"
                            if res.get("pending") else "",
                            "dep_results": dep_results})

        # runtime=local：完整本地插件包
        if not str(pid).startswith("local-"):
            return jsonify({"ok": False,
                            "error": "目录条目类别与包形态不符（local 条目须 local- 前缀）"}), 200
        if prog:
            prog.begin(main_idx, "插件包 %s" % pid)
        path, err = _pull_and_verify(src, pid, ver.get("version"), ver.get("sha256"),
                                     ver.get("file"), progress=_prog_cb(prog, "pull"))
        if err:
            return jsonify({"ok": False, "error": err, "dep_results": dep_results}), 200
        res = pkg_install.install_local_zip(path, local_plugins_dir, local_manifests, _log,
                                            progress=_prog_cb(prog, "extract"))
        try:
            os.remove(path)
        except OSError:
            pass
        if not res.get("ok"):
            return jsonify({"ok": False, "error": res.get("error"),
                            "dep_results": dep_results}), 200
        _rescan()
        return jsonify({"ok": True, "id": res["id"],
                        "restart_required": res.get("restart_required"),
                        "hint": res.get("hint", ""), "dep_results": dep_results})

    _dl_jobs = {}   # dl_id → {received,total,status,path,error,filename}（下载进度台账）

    @app.post("/local-api/store/download")
    def store_download():
        """桌面端「下载包」：启动后台拉包任务，返回任务 id（前端轮询 /download/progress）。"""
        data = request.get_json(force=True, silent=True) or {}
        pid = str(data.get("id") or "")
        version = str(data.get("version") or "")
        src = _store_source()
        catalog, err = src.get_catalog()
        if err:
            return jsonify({"ok": False, "error": err}), 200
        ver, err = _find_catalog_entry(catalog, pid, version)
        if err:
            return jsonify({"ok": False, "error": err}), 200
        # 下载语义（2026-10-08 需求）：全部插件都给「下载」（仅下载不安装）——
        # server 条目取**客户端工件（前端包）**（后端包不对用户暴露，留档走构建侧）；其余 = 自身包。
        artifact = None
        if (ver.get("runtime") or "local") == "server":
            if not ver.get("frontend_file"):
                return jsonify({"ok": False,
                                "error": "该条目没有前端包工件（服务器侧尚未发布前端包）"}), 200
            artifact = "frontend"
            filename = ver.get("frontend_file")
            expect_sha = ver.get("frontend_sha256")
        else:
            filename = ver.get("file") or ("%s-%s.zip" % (pid, ver.get("version")))
            expect_sha = ver.get("sha256")
        dl_id = uuid.uuid4().hex
        _dl_jobs[dl_id] = {"received": 0, "total": None, "status": "running",
                           "path": "", "error": "", "filename": filename}

        def _worker():
            st = _dl_jobs.get(dl_id)
            if st is None:
                return
            try:
                def _prog(received, total):
                    st["received"], st["total"] = received, total
                path, err = _pull_and_verify(src, pid, ver.get("version"),
                                             expect_sha, filename, artifact=artifact,
                                             progress=_prog)
                if err:
                    st["status"], st["error"] = "error", err
                else:
                    st.update(status="done", path=path)
            except Exception as exc:
                st["status"], st["error"] = "error", str(exc)

        threading.Thread(target=_worker, daemon=True, name="store-dl-" + dl_id[:8]).start()
        return jsonify({"ok": True, "id": dl_id, "filename": filename})

    @app.get("/local-api/store/download/progress")
    def store_download_progress():
        st = _dl_jobs.get(request.args.get("id") or "")
        if st is None:
            return jsonify({"ok": False, "status": "error", "error": "任务不存在"}), 200
        return jsonify({"ok": True, "received": st["received"], "total": st["total"],
                        "status": st["status"], "path": st["path"], "error": st["error"],
                        "filename": st["filename"]})

    @app.get("/local-api/store/downloaded")
    def store_downloaded():
        """校验下载状态（点击下载前先查）：catalog 文件名在 downloads 下存在且大小一致 → 已下载。"""
        pid = request.args.get("id") or ""
        version = request.args.get("version") or ""
        src = _store_source()
        catalog, err = src.get_catalog()
        if err:
            return jsonify({"ok": False, "error": err}), 200
        ver, err = _find_catalog_entry(catalog, pid, version)
        if err:
            return jsonify({"ok": False, "error": err}), 200
        filename = os.path.basename(str(ver.get("file") or ("%s-%s.zip" % (pid, version))))
        path = os.path.join(_data_dir, "downloads", filename)
        exists = os.path.isfile(path)
        try:
            size_ok = exists and (not ver.get("size")
                                  or os.path.getsize(path) == int(ver.get("size")))
        except (OSError, TypeError, ValueError):
            size_ok = exists
        return jsonify({"ok": True, "downloaded": bool(size_ok), "filename": filename})

    def _component_dirs(cid):
        pylibs = os.path.join(_program_dir, "runtime", "pylibs")
        return (os.path.join(pylibs, cid),
                os.path.join(pylibs, ".pending", cid),
                os.path.join(pylibs, ".pending-remove", cid))

    def _sweep_pending_component_removals():
        """启动清扫：.pending-remove 下的组件目录（上次卸载时被占用的）此刻未映射，直接删除。"""
        rm_root = os.path.join(_program_dir, "runtime", "pylibs", ".pending-remove")
        if not os.path.isdir(rm_root):
            return
        for name in os.listdir(rm_root):
            shutil.rmtree(os.path.join(rm_root, name), ignore_errors=True)
        try:
            os.rmdir(rm_root)
            _log("已清扫待卸载依赖组件（.pending-remove）")
        except OSError:
            pass

    _sweep_pending_component_removals()

    @app.post("/local-api/store/uninstall-dep")
    def store_uninstall_dep():
        """依赖组件卸载：纯 Python 即时删（刷新旗标）；原生库被映射无法删 → 移入
        .pending-remove，下次启动清除（与安装的 .pending 语义对偶）。"""
        data = request.get_json(force=True, silent=True) or {}
        cid = str(data.get("id") or "")
        import re as _re
        if not _re.fullmatch(r"[A-Za-z0-9_+.-]{1,64}", cid):
            return jsonify({"ok": False, "error": "非法组件 id"}), 400
        active, staged, rm = _component_dirs(cid)
        if not os.path.isdir(active) and not os.path.isdir(staged):
            return jsonify({"ok": False, "error": "该依赖组件未安装"}), 404
        pending_remove = False
        if os.path.isdir(active):
            try:
                shutil.rmtree(active)
            except OSError:
                os.makedirs(os.path.dirname(rm), exist_ok=True)
                try:
                    if os.path.isdir(rm):
                        shutil.rmtree(rm, ignore_errors=True)
                    os.rename(active, rm)      # 同卷改名，句柄不再阻塞
                    pending_remove = True
                except OSError as exc:
                    return jsonify({"ok": False, "error": "删除失败：%s" % exc}), 500
        if os.path.isdir(staged):
            shutil.rmtree(staged, ignore_errors=True)   # 待应用暂存一并取消
        try:
            jz_plugin_host.setup_dep_components(_program_dir, _log)   # 重注入（已删路径无副作用）；
            # 依赖旗标由各插件 install_refresher 在请求期刷新（既有机制）
        except Exception:
            pass
        except Exception:
            pass
        return jsonify({"ok": True, "pending_remove": pending_remove,
                        "note": "原生库已被占用，将在下次启动客户端后清除" if pending_remove else ""})

    @app.post("/local-api/store/reveal")
    def store_reveal():
        """下载完成后打开 downloads 目录并定位文件（留档定位；失败原因显式返回）。

        用 explorer /select 而非 os.startfile：前者把窗口置前并选中文件，
        后者常被前台客户端窗口遮挡（真机反馈"提示已打开但看不到文件夹"）。"""
        import subprocess as _sp
        d = os.path.join(data_dir, "downloads")
        os.makedirs(d, exist_ok=True)
        data = request.get_json(silent=True) or {}
        p = str(data.get("path") or "")
        try:
            if p and os.path.isfile(p) and os.path.abspath(p).lower().startswith(os.path.abspath(d).lower()):
                _sp.Popen('explorer /select,"%s"' % os.path.abspath(p))
            else:
                os.startfile(d)
            return jsonify({"ok": True})
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 500

    @app.get("/local-api/config/plugin-source")
    def plugin_source_get():
        from client_config import load_client_config
        cfg = plugin_source.load_source_config(build_cfg, load_client_config(build_cfg))
        return jsonify({"ok": True, "plugin_source": cfg})

    @app.post("/local-api/config/plugin-source")
    def plugin_source_set():
        data = request.get_json(force=True, silent=True) or {}
        ptype = data.get("type")
        if ptype not in ("server", "share"):
            return jsonify({"error": "type 仅支持 server / share"}), 400
        if ptype == "share" and not str(data.get("index_path") or "").strip():
            return jsonify({"error": "share 源需要 index_path"}), 400
        from client_config import load_client_config, save_client_config
        cfg = dict(load_client_config(build_cfg))
        cfg["plugin_source"] = {"type": ptype,
                                "index_path": str(data.get("index_path") or "")}
        save_client_config(build_cfg, cfg)
        return jsonify({"ok": True, "plugin_source": cfg["plugin_source"],
                        "note": "已写入客户端配置，立即生效（免重启）"})

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

    def _rescan_frontends():
        """重扫 server 前端包注册表（装卸后调用；停用只读配置，无需重扫）。"""
        frontends.clear()
        frontends.update({f["id"]: f for f in pkg_install.scan_frontends(frontends_dir)})

    def _frontend_enabled(pid):
        """server 前端本地直出开关：客户端配置 local_state（停用 = 回退中继直出）。"""
        from client_config import load_client_config
        return bool((load_client_config(build_cfg).get("local_state") or {}).get(pid, True))

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

    def _local_only_tools_response():
        # F5（批次 9 T-O9-5，Q3 配套）：远程不可达/未配置时降级为仅本地条目——
        # 本地条目（local-plugins）是离线网格的唯一载体，A 类离线显兜底页（既有口径）
        enabled = [m for m in local_manifests
                   if local_states.get(m.get("id"), {}).get("status") != "disabled"]
        return jsonify({"tools": local_registry.tool_items(enabled, 1000),
                        "categories": [dict(local_registry.LOCAL_CATEGORY)],
                        "degraded": True})

    @app.get("/api/tools")
    def api_tools_merged():
        # 注意：本装饰器必须紧跟本函数——F5 曾把辅助函数插在两者之间，致 /api/tools
        # 全局误走降级分支（2026-10-08 台架抓出；插入带装饰器函数前一律锚定装饰器行）
        if not state["address"]:
            return _local_only_tools_response()
        headers = client_headers()
        try:
            r = upstream.get(state["address"] + "/api/tools",
                             headers=headers, timeout=(5, 60))
        except requests.RequestException as exc:
            health.mark(False, "%s: %s" % (type(exc).__name__, exc))
            return _local_only_tools_response()
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
        # 本机移除（桌面端「卸载」的显示面，2026-10-08）：服务器侧条目在本机不再下发。
        # 只影响本机（客户端数据目录 config.json），服务器端运行与全局清单不受影响。
        hidden = set(hidden_tools(build_cfg))
        if hidden:
            dropped = [str(t.get("id") or "") for t in base if str(t.get("id") or "") in hidden]
            base = [t for t in base if str(t.get("id") or "") not in hidden]
            if dropped:
                _log("本机已移除的工具不再下发：%s" % "、".join(dropped))
        start = max([t.get("order", 0) for t in base] or [0]) + 1000
        # 停用即从工具网格隐藏（需求⑦：停用为一等操作；本地 API 404 语义保留作兜底）
        enabled_manifests = [m for m in local_manifests
                             if local_states.get(m.get("id"), {}).get("status") != "disabled"]
        merged = local_registry.tool_items(enabled_manifests, start)
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
