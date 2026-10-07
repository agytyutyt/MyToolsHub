# -*- coding: utf-8 -*-
"""子进程执行器（批次 4 T20，GD4 已拍板保留）：类 B 插件的进程隔离执行与生命周期。

- 默认「源码包子进程」：runtime python + plugin_host.py（注入共享 pylibs → 加载插件源码
  → mini-WSGI 派发其 register(app) 路由）；entry.backend 以 .exe 结尾走例外通道（同协议）。
- 默认用完退出；manifest `resident: true` 声明常驻 → 进程存活复用，死亡自动拉起（热插拔 L3）。
- `timeout_ms` 超时即杀；崩溃 → 审计日志 + 错误回传前端（不炸页面）。
- 审计：data_dir/plugin-audit.log（插件 id/时刻/方法路径/耗时/结果）。
"""
import base64
import json
import os
import subprocess
import threading
import time
import uuid

WORKER_BODY_LIMIT = 8 * 1024 * 1024   # 规范 §16.3 L-3：worker 响应体上限（大结果写临时文件传路径）


class PluginExecutor:
    def __init__(self, runtime_python, host_script, pylibs_dir, data_dir, log=None):
        self._python = runtime_python
        self._host = host_script
        self._pylibs = pylibs_dir
        self._audit_path = os.path.join(data_dir, "plugin-audit.log")
        self._log = log or (lambda m: None)
        self._residents = {}          # plugin_id → Popen
        self._locks = {}              # plugin_id → Lock（常驻串行化）
        self._lock_all = threading.Lock()

    # ---------------- 对外 ----------------

    def call(self, manifest, method, path, headers, body, timeout_ms):
        """派发一次请求；返回 (status, headers_dict, body_bytes)。不抛网络类异常。"""
        pid = manifest.get("id", "?")
        t0 = time.time()
        try:
            status, hdrs, resp_body = self._call_impl(manifest, method, path, headers, body, timeout_ms)
        except WorkerTimeout as exc:
            self._audit(pid, method, path, time.time() - t0, "timeout")
            return 504, {"Content-Type": "application/json"}, json.dumps(
                {"error": "本地插件超时", "plugin": pid, "timeout_ms": timeout_ms}).encode()
        except WorkerCrash as exc:
            self._audit(pid, method, path, time.time() - t0, "crash: %s" % exc)
            return 502, {"Content-Type": "application/json"}, json.dumps(
                {"error": "本地插件崩溃退出", "plugin": pid, "detail": str(exc)}).encode()
        self._audit(pid, method, path, time.time() - t0, status)
        return status, hdrs, resp_body

    def shutdown(self):
        """收割全部常驻 worker（托盘退出/关机路径）。"""
        with self._lock_all:
            for pid, proc in list(self._residents.items()):
                try:
                    proc.kill()
                except Exception:
                    pass
            self._residents.clear()

    # ---------------- 内部 ----------------

    def _call_impl(self, manifest, method, path, headers, body, timeout_ms):
        pid = manifest.get("id", "?")
        resident = bool(manifest.get("resident"))
        if resident:
            with self._lock_all:
                proc = self._residents.get(pid)
                if proc is None or proc.poll() is not None:
                    proc = self._spawn(manifest, resident=True)
                    self._residents[pid] = proc
                lock = self._locks.setdefault(pid, threading.Lock())
            with lock:
                try:
                    return self._roundtrip(proc, manifest, method, path, headers, body,
                                           timeout_ms, resident=True)
                except WorkerCrash:
                    # 崩溃后立即弹出常驻项：Windows 下子进程句柄状态滞后（管道 EOF 先到、
                    # poll() 迟迟不报退出码），不弹出会让下一请求复用死进程管道再吃 502。
                    # 与超时路径的弹出语义对齐；下次请求经 poll 判死自然重新拉起。
                    with self._lock_all:
                        if self._residents.get(pid) is proc:
                            self._residents.pop(pid, None)
                    raise
        proc = self._spawn(manifest, resident=False)
        try:
            return self._roundtrip(proc, manifest, method, path, headers, body,
                                   timeout_ms, resident=False)
        finally:
            try:
                proc.kill()
            except Exception:
                pass

    def _spawn(self, manifest, resident):
        plugin_dir = manifest.get("_dir_abs") or ""
        pid = manifest.get("id", "unknown")
        backend = (manifest.get("entry") or {}).get("backend") or ""
        if backend.lower().endswith(".exe"):
            exe = os.path.join(plugin_dir, backend)
            argv = [exe]
        else:
            argv = [self._python, self._host, "--plugin", plugin_dir]
            if resident:
                argv.append("--resident")
            if self._pylibs:
                argv += ["--pylibs", self._pylibs]
        err = self._stderr_file(pid)      # stderr 归日志（T20 契约）
        try:
            return subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=err)
        finally:
            err.close()

    def _stderr_file(self, plugin_id):
        d = os.path.join(os.path.dirname(self._audit_path), "plugin-logs")
        os.makedirs(d, exist_ok=True)
        return open(os.path.join(d, plugin_id + ".log"), "ab")

    def _roundtrip(self, proc, manifest, method, path, headers, body, timeout_ms, resident):
        req = {"request_id": uuid.uuid4().hex, "method": method, "path": path,
               "headers": headers or {}, "body_b64": base64.b64encode(body or b"").decode()}
        deadline = time.time() + max(timeout_ms or 60000, 1000) / 1000.0
        try:
            proc.stdin.write((json.dumps(req) + "\n").encode())
            proc.stdin.flush()
            line = proc.stdout.readline().decode("utf-8", "replace").strip()
        except (OSError, ValueError) as exc:
            raise WorkerCrash(str(exc))
        if time.time() > deadline:
            try:
                proc.kill()
            except Exception:
                pass
            if resident and not manifest.get("_killed"):
                self._residents.pop(manifest.get("id"), None)
            raise WorkerTimeout("worker 超时")
        if not line:
            raise WorkerCrash("worker 无响应退出（code=%s）" % proc.poll())
        resp = json.loads(line)
        if "error" in resp:
            raise WorkerCrash(resp["error"])
        body = base64.b64decode(resp.get("body_b64") or "")
        if len(body) > WORKER_BODY_LIMIT:
            # L-3 响应体上限 8MB（规范 §16.3）：超限回 502 并提示写临时文件传路径——
            # 此前该上限只在规范里声明、链路无强制（批次 10 适配核对发现）。
            return 502, {"Content-Type": "application/json"}, json.dumps(
                {"error": "本地插件响应体超过 %dMB 上限（规范 L-3）——大结果请写临时文件传路径"
                          % (WORKER_BODY_LIMIT // 1048576)}).encode()
        return resp.get("status", 200), resp.get("headers", {}), body

    def _audit(self, plugin_id, method, path, seconds, outcome):
        try:
            os.makedirs(os.path.dirname(self._audit_path), exist_ok=True)
            with open(self._audit_path, "a", encoding="utf-8") as f:
                f.write("%s %s %s %s %.2fs -> %s\n" % (
                    time.strftime("%Y-%m-%d %H:%M:%S"), plugin_id, method, path, seconds, outcome))
        except OSError:
            pass


class WorkerTimeout(Exception):
    pass


class WorkerCrash(Exception):
    pass
