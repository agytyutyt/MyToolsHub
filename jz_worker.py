# -*- coding: utf-8 -*-
"""插件后端 worker 引导件（T-O2-12a，需求⑩ E 架构）。

一个插件后端包 = 一个自有 Flask 实例进程，`register(app)` 契约原样保留——
业务插件零改动（运行架构设计文档 §3 拍板：身份已收敛 jz_api 门面）。

拉起方式（supervisor 编排，worker 自身不做任何路径/端口决策）：
- frozen：``JZToolsHub.exe --worker <插件id>``（supervisor 注入环境变量）
- 源码/venv：``<python> jz_worker.py --worker <插件id>``（--data-root 等可用参数覆盖）

supervisor 注入的环境变量：
    JZ_WORKER_PORT     本 worker 绑定端口（拍板：分配一次持久化回注册表）
    JZTOOLS_DATA_ROOT  数据根（与主应用同根——jztools_data 解析同源）
    JZ_WORKER_MAIN_URL 主应用地址（回环取会话信息用，http://127.0.0.1:5000）
    JZ_PLUGINS_DIR     插件目录（缺省 <主包>\\plugins）
    JZ_MAIN_PKG_DIR    主包目录（依赖组件注入 jz_plugin_host 用）

身份与通信（§3.1 拍板）：
- **鉴权权威在主应用**（会话守卫/强制登录/权限点全在反代跳 before_request）；
- worker 侧两级 session_user provider：① 本地验签 Cookie（共享 SECRET_KEY，
  鉴权锚点）→ ② 每请求回环 GET <主应用>/api/session 取全量信息 dict（同请求链
  现算，零陈旧）；**回环失败 → None（fail-closed 拍板）**；
- 对会话只读：不 re-sign、不写 last_active、回环响应的 Set-Cookie 丢弃；
- admin.json 只读 `secret_key` 单字段（共享密钥存放点），其余字段归 admin。
"""

import json
import os
import sys
import threading
import time


def _main_pkg_dir():
    for cand in (os.environ.get("JZ_MAIN_PKG_DIR"),
                 os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                 if not getattr(sys, "frozen", False) else None,
                 os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else None):
        if cand and os.path.isdir(cand):
            return os.path.abspath(cand)
    return os.getcwd()


def _shared_secret_key(main_pkg_dir, data_root):
    """读共享 SECRET_KEY（admin.json 的 secret_key 单字段；信任面纪律见模块头）。"""
    for root in (data_root, main_pkg_dir):
        p = os.path.join(root, "config", "admin.json")
        if os.path.isfile(p):
            try:
                with open(p, "r", encoding="utf-8-sig") as f:
                    key = (json.load(f) or {}).get("secret_key")
                if key:
                    return str(key)
            except Exception:
                pass
    raise RuntimeError("读不到共享 SECRET_KEY（config/admin.json）——worker 无法验签会话")


class _Loopback:
    """主应用回环客户端（信息面）：session_user 每请求取、org_tree 30s TTL（拍板）。"""

    def __init__(self, main_url):
        self._url = (main_url or "").rstrip("/")
        self._org_cache = {"ts": 0.0, "data": None}
        self._lock = threading.Lock()

    def _get(self, path, cookie, timeout=8.0):
        if not self._url:
            return None
        import urllib.request
        req = urllib.request.Request(self._url + path)
        if cookie:
            req.add_header("Cookie", cookie)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                if r.status != 200:
                    return None
                return json.loads(r.read().decode("utf-8"))
        except Exception:
            return None

    def session_user(self, cookie):
        """回环 /api/session（既有公开端点）→ {"user": 全量 dict}；失败 → None（fail-closed）。"""
        data = self._get("/api/session", cookie)
        if not isinstance(data, dict):
            return None
        user = data.get("user")
        return user if isinstance(user, dict) else None

    def org_tree(self, cookie):
        """回环 /api/org/tree（框架级只读端点）+ 30s TTL 缓存（拍板：做）。"""
        now = time.time()
        with self._lock:
            if now - self._org_cache["ts"] < 30.0:
                return self._org_cache["data"]
            data = self._get("/api/org/tree", cookie)
            if data is not None:
                self._org_cache = {"ts": now, "data": data}
            return self._org_cache["data"]


def run_worker(plugin_id, port=None, data_root=None, main_url=None,
               plugins_dir=None, main_pkg_dir=None):
    """worker 主流程：装配 Flask → 注入 provider → register(app) → waitress。"""
    main_pkg_dir = os.path.abspath(main_pkg_dir or _main_pkg_dir())
    data_root = os.path.abspath(
        data_root or os.environ.get("JZTOOLS_DATA_ROOT")
        or os.path.join(main_pkg_dir, "data"))
    plugins_dir = plugins_dir or os.environ.get("JZ_PLUGINS_DIR") \
        or os.path.join(main_pkg_dir, "plugins")
    main_url = (main_url or os.environ.get("JZ_WORKER_MAIN_URL") or "").rstrip("/")
    if main_pkg_dir not in sys.path:      # 源码/venv 形态：内核模块（jz_*）可 import
        sys.path.insert(0, main_pkg_dir)

    port = int(port or os.environ.get("JZ_WORKER_PORT") or 0)
    if not port:
        raise RuntimeError("worker 端口未注入（JZ_WORKER_PORT）——应由 supervisor 拉起")

    import jz_api
    import jz_deps
    import jz_plugin_host
    import jztools_data
    from flask import Flask, g, jsonify, request

    # 依赖组件注入（与主应用/能力层同一份机制；幂等）
    try:
        jz_plugin_host.setup_dep_components(main_pkg_dir)
    except Exception:
        pass

    secret = _shared_secret_key(main_pkg_dir, data_root)
    loopback = _Loopback(main_url)

    app = Flask("jz_worker_%s" % plugin_id.replace("-", "_"))
    app.secret_key = secret                     # 共享密钥：Cookie 验签的鉴权锚点
    app.config["JSON_AS_ASCII"] = False

    def _cookie():
        return request.headers.get("Cookie") or ""

    def _session_user():
        """两级 provider（§3.1 拍板）：本地验签 → 回环取信息；任一失败 → None。"""
        try:
            username = session_get("user")
        except Exception:
            return None
        if not username:
            return None
        return loopback.session_user(_cookie())      # fail-closed：失败即 None

    def session_get(key):
        from flask import session as flask_session
        return flask_session.get(key)

    jz_api.register_providers(
        get_session_user=_session_user,
        set_operation=lambda op: setattr(g, "_operation", op),   # worker 自有访问日志
        get_org_tree=lambda: loopback.org_tree(_cookie()),
        # user_llm_*：随主包 /api/llm 推理面提供后接（jz_llm 适配项）；首期不注册 → 降级
    )
    # ★ provider 注册必须先于 register(app)（插件加载时 import jz_api 即用）

    manifest = {}
    try:
        with open(os.path.join(plugins_dir, plugin_id, "manifest.json"),
                  "r", encoding="utf-8-sig") as f:
            manifest = json.load(f) or {}
    except Exception:
        pass

    @app.get("/_worker/health")
    def _health():
        """探针：无鉴权、无业务数据（§2.1 拍板口径）。"""
        return jsonify({"ok": True, "plugin": plugin_id,
                        "version": manifest.get("version") or ""})

    # 加载插件后端：register(app) 契约原样（与主应用同一 load 机制，jz_plugin_host）
    routes = jz_plugin_host.load_backend_module(plugins_dir, plugin_id)
    register = getattr(routes, "register", None)
    bp = getattr(routes, "bp", None)
    if callable(register):
        register(app)
    elif bp is not None:
        app.register_blueprint(bp)
    else:
        raise RuntimeError("插件 %s 未导出 register(app) / bp（plugins_dir=%r）"
                           % (plugin_id, plugins_dir))

    from waitress import serve
    serve(app, host="127.0.0.1", port=port, threads=4)
    return 0


def main(argv=None):
    argv = list(sys.argv if argv is None else argv)
    if "--worker" not in argv:
        print("用法：jz_worker.py --worker <插件id> [--port N] [--data-root R] "
              "[--main-url U] [--plugins-dir D] [--pkg-dir P]", file=sys.stderr)
        return 2
    def arg(name, default=None):
        if name in argv:
            i = argv.index(name)
            if i + 1 < len(argv):
                return argv[i + 1]
        return default
    plugin_id = arg("--worker")
    try:
        return run_worker(
            plugin_id,
            port=arg("--port"),
            data_root=arg("--data-root"),
            main_url=arg("--main-url"),
            plugins_dir=arg("--plugins-dir"),
            main_pkg_dir=arg("--pkg-dir"),
        )
    except Exception as exc:
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
