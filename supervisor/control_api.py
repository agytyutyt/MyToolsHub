# -*- coding: utf-8 -*-
"""supervisor 控制接口（T-O2-11c）：stdlib http.server + 令牌鉴权。

设计口径（运行架构设计文档 §2 拍板）：
- 仅绑 127.0.0.1 + 独立令牌（首次启动生成、写 <数据根>\\config\\.supervisor_token，
  主应用经同一文件读取——共享密钥面 = 本机文件系统，无网络传播）；
- 动作六件套：status / start / stop / restart / apply / rollback（+ clear-circuit）；
- admin 商店管理页的「部署运行面板」是该接口的薄客户端（store_admin / bridge）。

线程模型：ThreadingHTTPServer（每请求一线程）——控制面低频，无并发压力。
"""

import json
import os
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from supervisor import supervisor_core as sc

MAX_BODY = 8 * 1024 * 1024        # 控制接口只收 JSON 与路径，不含包体——包走文件系统


def ensure_token(data_root):
    """令牌：有则读，无则生成（首次启动）。返回 token 字符串。"""
    p = sc.token_path(data_root)
    if os.path.isfile(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                tok = f.read().strip()
            if tok:
                return tok
        except Exception:
            pass
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tok = secrets.token_hex(32)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(tok)
    os.replace(tmp, p)
    return tok


def make_handler(sup, token):
    class Handler(BaseHTTPRequestHandler):
        server_version = "JZSupervisor/1"

        def log_message(self, fmt, *args):      # 控制面日志进 supervisor 主日志
            try:
                sup._log("control: %s" % (fmt % args))
            except Exception:
                pass

        def _authed(self):
            got = self.headers.get("X-Supervisor-Token") or ""
            return bool(token) and secrets.compare_digest(got, token)

        def _json(self, obj, status=200):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > MAX_BODY:
                return {}
            try:
                return json.loads(self.rfile.read(n).decode("utf-8"))
            except Exception:
                return {}

        def do_GET(self):
            if not self._authed():
                return self._json({"ok": False, "error": "令牌缺失或不符"}, 401)
            if self.path.rstrip("/") in ("", "/status"):
                return self._json(sup.status())
            return self._json({"ok": False, "error": "未知动作（GET 仅 status）"}, 404)

        def do_POST(self):
            if not self._authed():
                return self._json({"ok": False, "error": "令牌缺失或不符"}, 401)
            action = self.path.strip("/").split("?")[0]
            body = self._body()
            name = str(body.get("unit") or body.get("id") or "")
            try:
                if action == "start":
                    return self._json(sup.start_unit(name))
                if action == "stop":
                    return self._json(sup.stop_unit(name))
                if action == "restart":
                    r = sup.stop_unit(name)
                    return self._json(sup.start_unit(name))
                if action == "clear-circuit":
                    return self._json(sup.clear_circuit(name))
                if action == "apply":
                    pkg = str(body.get("package") or "")
                    pid = str(body.get("plugin_id") or name)
                    if not pkg or not os.path.isfile(pkg):
                        return self._json({"ok": False, "error": "package 路径无效"}, 400)
                    return self._json(sup.apply_package(pid, pkg))
                if action == "rollback":
                    return self._json(sup.rollback_unit(name or pid_from(body)))
                return self._json({"ok": False, "error": "未知动作：%s" % action}, 404)
            except Exception as exc:
                return self._json({"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}, 500)

        def pid_from_unused(self, body):
            return str(body.get("plugin_id") or "")

    return Handler


def pid_from(body):
    return str(body.get("plugin_id") or "")


def start_control_server(sup, token, port):
    """启动控制接口（守护线程）；返回 server 实例（测试用）。"""
    handler = make_handler(sup, token)
    srv = ThreadingHTTPServer(("127.0.0.1", port), handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True, name="supervisor-control")
    t.start()
    return srv


def run(data_root, port=None, log=None):
    """supervisor 进程入口：注册表编排 + 控制接口 + 监控循环。"""
    sup = sc.Supervisor(data_root, log=log)
    tok = ensure_token(data_root)
    port = int(port or sup.supervisor_port())
    srv = start_control_server(sup, tok, port)
    log_msg = log or sc._log_msg
    log_msg("supervisor 就绪：控制接口 127.0.0.1:%d（令牌 %s），数据根 %s"
            % (port, tok[:8] + "…", data_root))
    sup.start_monitor()
    results = sup.start_all()
    log_msg("启动编排完成：%s" % json.dumps(results, ensure_ascii=False))
    try:
        while True:
            threading.Event().wait(3600)
    except KeyboardInterrupt:
        log_msg("收到退出信号，编排停机（Job Object 收割全部子进程）")
        sup.shutdown()
    finally:
        srv.shutdown()
    return 0
