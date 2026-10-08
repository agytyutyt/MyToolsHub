# -*- coding: utf-8 -*-
"""子进程执行器（批次 4 T20，GD4 已拍板保留）：类 B 插件的进程隔离执行与生命周期。

- 默认「源码包子进程」：runtime python + plugin_host.py（注入共享 pylibs → 加载插件源码
  → mini-WSGI 派发其 register(app) 路由）；entry.backend 以 .exe 结尾走例外通道（同协议）。
- 默认用完退出；manifest `resident: true` 声明常驻 → 进程存活复用，死亡自动拉起（热插拔 L3）。
- **idle 回收（F6，2026-10-09）**：manifest 可选 `idle_ttl`（秒）——常驻 worker 空闲超过该值
  由收割线程**干净收回**（弹出常驻表 + kill，非崩溃路径：下次请求静默重启，不弹"崩溃"），
  归还内存；缺省不回收（=既有行为不变）。**前置纪律**：任务态必须已外置到 `.task_cache`
  （内存 TASKS 字典会随回收丢失）——只有任务态外置后的插件才可声明 idle_ttl。
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
        self._resident_ttl = {}       # plugin_id → idle_ttl 秒（manifest 声明；0/缺省=不回收）
        self._last_used = {}          # plugin_id → 最近一次请求时刻（回收判据）
        self._reaper = None           # 空闲收割线程（有 ttl>0 的常驻单元才起）
        self._reaper_stop = threading.Event()

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
        self._reaper_stop.set()
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
                    self._last_used[pid] = time.time()
                try:
                    self._resident_ttl[pid] = max(0, int(manifest.get("idle_ttl") or 0))
                except (TypeError, ValueError):
                    self._resident_ttl[pid] = 0
                self._ensure_reaper()
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
                finally:
                    self._last_used[pid] = time.time()   # 在途/异常也算活动（避免误回收）
        proc = self._spawn(manifest, resident=False)
        try:
            return self._roundtrip(proc, manifest, method, path, headers, body,
                                   timeout_ms, resident=False)
        finally:
            try:
                proc.kill()
            except Exception:
                pass

    # ---------------- 空闲收割（F6） ----------------

    def _ensure_reaper(self):
        """有 ttl>0 的常驻单元时才起收割线程（一次；客户端最简形态，避免无谓线程）。"""
        if self._reaper is not None or not any(t > 0 for t in self._resident_ttl.values()):
            return
        self._reaper = threading.Thread(target=self._reaper_loop, daemon=True,
                                        name="worker-idle-reaper")
        self._reaper.start()

    def _tick(self):
        """收割节拍：随最小 ttl 自适应（ttl/2），夹在 1-15s（单测用小 ttl 也能秒级收敛）。"""
        ttls = [t for t in self._resident_ttl.values() if t > 0]
        return max(1.0, min(15.0, (min(ttls) / 2.0) if ttls else 15.0))

    def _reaper_loop(self):
        while not self._reaper_stop.wait(self._tick()):
            try:
                self._reap_idle()
            except Exception as exc:                      # 收割绝不影响正常请求
                self._log("idle 收割线程异常（继续运行）：%r" % exc)

    def _reap_idle(self):
        """把空闲 ≥ idle_ttl 的常驻 worker **干净收回**（非崩溃路径：静默重启、不弹崩溃）。

        拿不到该插件的串行锁 = 有在途请求 → 本拍跳过；弹出与 kill 同持 _lock_all，
        并使 _last_used 复位，避免与在途请求竞态（在途结束会再刷一次活动时刻）。
        """
        now = time.time()
        with self._lock_all:
            ids = list(self._residents.keys())
        for pid in ids:
            ttl = self._resident_ttl.get(pid, 0)
            if ttl <= 0:
                continue
            if now - self._last_used.get(pid, now) < ttl:
                continue
            lock = self._locks.get(pid)
            if lock is None or not lock.acquire(blocking=False):
                continue                                  # 在途请求：跳过
            try:
                with self._lock_all:
                    proc = self._residents.get(pid)
                    if proc is None:
                        continue
                    idle = now - self._last_used.get(pid, now)
                    if idle < ttl:                        # 二查（拿锁期间可能刚有活动）
                        continue
                    self._residents.pop(pid, None)
                try:
                    proc.kill()
                except Exception:
                    pass
                self._log("worker idle 回收：%s（空闲 %.0fs ≥ idle_ttl %ss；下次请求静默重启）"
                          % (pid, idle, ttl))
                self._audit(pid, "-", "-", idle, "idle-reap")
            finally:
                lock.release()

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
            # CREATE_NO_WINDOW：运行时 python.exe 是控制台子系统程序，GUI 父进程不加该标志
            # 会弹出黑色控制台窗口（真机报告：打开插件弹黑窗，2026-10-08）；stdio 管道不受影响。
            return subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=err,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
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
