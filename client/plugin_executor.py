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
import queue
import subprocess
import threading
import time
import uuid

WORKER_BODY_LIMIT = 8 * 1024 * 1024   # 规范 §16.3 L-3：worker 响应体上限（大结果写临时文件传路径）

#: 审计队列上限（S2-5，2026-10-17）：满则丢弃并计数，**绝不阻塞请求**（口径同主包 access_log 的 2000）。
AUDIT_QUEUE_MAX = 2000

#: 预热探针路径（R1.4，2026-10-17）：**保留路径**——插件不应实现它，命中即 404 属预期。
#: 选它而不用 `GET /` 的原因：个别插件首页可能带副作用，探针必须确定无副作用。
WARMUP_PROBE_PATH = "/__warmup_probe__"


class PluginExecutor:
    def __init__(self, runtime_python, host_script, pylibs_dir, data_dir, log=None,
                 data_root_fn=None):
        self._python = runtime_python
        self._host = host_script
        self._pylibs = pylibs_dir
        self._audit_path = os.path.join(data_dir, "plugin-audit.log")
        self._log = log or (lambda m: None)
        self._data_root_fn = data_root_fn   # G3-2：spawn 时现取账号数据根（显式注入 env）
        self._residents = {}          # plugin_id → Popen
        self._locks = {}              # plugin_id → Lock（常驻串行化）
        self._lock_all = threading.Lock()
        self._resident_ttl = {}       # plugin_id → idle_ttl 秒（manifest 声明；0/缺省=不回收）
        self._last_used = {}          # plugin_id → 最近一次请求时刻（回收判据）
        self._reaper = None           # 空闲收割线程（有 ttl>0 的常驻单元才起）
        self._reaper_stop = threading.Event()
        # S2-5（2026-10-17）：审计异步化——请求路径只入队，后台批量落盘（范式同 access_log）
        self._audit_q = queue.Queue(maxsize=AUDIT_QUEUE_MAX)
        self._audit_thread = None
        self._audit_lock = threading.Lock()
        self._audit_stop = threading.Event()
        self._audit_dropped = 0
        # R2.3（2026-10-17）：并发派发的管道状态（仅 concurrency>1 的插件会用到）
        self._pipes = {}

    # ---------------- 对外 ----------------

    def call(self, manifest, method, path, headers, body, timeout_ms, user=""):
        """派发一次请求；返回 (status, headers_dict, body_bytes)。不抛网络类异常。

        user：调用者真实登录身份（能力层从会话镜像取；V9「审计日志落真实身份」；离线/未登录传空）。
        """
        pid = manifest.get("id", "?")
        t0 = time.time()
        try:
            status, hdrs, resp_body = self._call_impl(manifest, method, path, headers, body, timeout_ms)
        except WorkerTimeout as exc:
            self._audit(pid, method, path, time.time() - t0, "timeout", user)
            return 504, {"Content-Type": "application/json"}, json.dumps(
                {"error": "本地插件超时", "plugin": pid, "timeout_ms": timeout_ms}).encode()
        except WorkerCrash as exc:
            self._audit(pid, method, path, time.time() - t0, "crash: %s" % exc, user)
            return 502, {"Content-Type": "application/json"}, json.dumps(
                {"error": "本地插件崩溃退出", "plugin": pid, "detail": str(exc)}).encode()
        self._audit(pid, method, path, time.time() - t0, status, user)
        return status, hdrs, resp_body

    def warmup(self, manifest):
        """预热一个常驻插件的 worker（R1.4，2026-10-17）。

        ⚠ 为什么**必须打一次真实往返**：`_spawn`（Popen）只要 2–12ms，冷启 ≈1.3s 全在
        **首个往返**的解释器启动 + 插件 import + `register(app)` 里（P2-4 基线实测）。
        故预热 = 对该插件发一次无副作用探针（`WARMUP_PROBE_PATH`，404 即算成功），
        把这段开销提前到「用户点导航」那一刻付掉。

        非 resident 插件不预热：一次性短命进程（请求时 spawn、用完 kill）预热无意义。
        幂等：resident 分支复用既有进程，连打不叠加 worker。
        → {"warmed": bool, "resident": bool, "reason": str}
        """
        if not manifest.get("resident"):
            return {"warmed": False, "resident": False, "reason": "非 resident 插件（无需预热）"}
        try:
            self.call(manifest, "GET", WARMUP_PROBE_PATH, {}, b"", 30000)
        except Exception as exc:      # 预热失败不抛：真实请求届时按既有错误语义处理
            return {"warmed": False, "resident": True,
                    "reason": "%s: %s" % (type(exc).__name__, exc)}
        return {"warmed": True, "resident": True, "reason": ""}

    def shutdown(self):
        """收割全部常驻 worker（托盘退出/关机路径）。"""
        self.flush_audit()        # S2-5：先落盘审计队列，再收割 worker（退出顺序：审计→worker）
        self._reaper_stop.set()
        with self._lock_all:
            for pid, proc in list(self._residents.items()):
                try:
                    proc.kill()
                except Exception:
                    pass
            self._residents.clear()

    def stop_residents(self):
        """停掉全部常驻 worker（账号切换/登出，G3-2）：防子进程持旧账号数据根写串。

        下次请求按当前 manifest 正常拉起（spawn 时注入新数据根），非崩溃路径。"""
        with self._lock_all:
            procs = list(self._residents.values())
            self._residents.clear()
            self._last_used.clear()
            self._resident_ttl.clear()
        for proc in procs:
            try:
                proc.kill()
            except Exception:
                pass
        if procs:
            self._log("已停止 %d 个常驻 worker（账号上下文变化）" % len(procs))

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
            if self._conc_of(manifest) > 1:
                # 并发（opt-in）：不占每插件串行锁，按 request_id 配对等待
                try:
                    return self._roundtrip_concurrent(proc, manifest, method, path,
                                                      headers, body, timeout_ms, True)
                except WorkerCrash:
                    with self._lock_all:
                        if self._residents.get(pid) is proc:
                            self._residents.pop(pid, None)
                        self._pipes.pop(pid, None)
                    raise
                finally:
                    self._last_used[pid] = time.time()
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
                _n = self._conc_of(manifest)
                if _n > 1:
                    argv += ["--concurrency", str(_n)]   # R2.3：并发派发（opt-in）
            if self._pylibs:
                argv += ["--pylibs", self._pylibs]
        err = self._stderr_file(pid)      # stderr 归日志（T20 契约）
        # G3-2：spawn 时现取账号数据根显式注入（不依赖父进程环境继承的时序——
        # 账号切换后 os.environ 已被能力层改写，但显式传 env 消除时序歧义）
        env = None
        if self._data_root_fn is not None:
            root = self._data_root_fn() or ""
            if root:
                env = dict(os.environ, JZTOOLS_DATA_ROOT=root)
        try:
            # CREATE_NO_WINDOW：运行时 python.exe 是控制台子系统程序，GUI 父进程不加该标志
            # 会弹出黑色控制台窗口（真机报告：打开插件弹黑窗，2026-10-08）；stdio 管道不受影响。
            _t0 = time.perf_counter()
            proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=err, env=env,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            # 阶段 0（2026-10-17）：进程创建耗时；冷启真实成本见 _cold_note（含解释器与插件 import）
            self._log("[perf] worker-spawn %s %.0fms"
                      % (pid, (time.perf_counter() - _t0) * 1000.0))
            return proc
        finally:
            err.close()

    def _stderr_file(self, plugin_id):
        d = os.path.join(os.path.dirname(self._audit_path), "plugin-logs")
        os.makedirs(d, exist_ok=True)
        return open(os.path.join(d, plugin_id + ".log"), "ab")

    def _cold_note(self, manifest, t0):
        """阶段 0（2026-10-17）：每个插件只记一次**首个往返**耗时。

        Popen 只测到进程创建；这里包含解释器启动 + 插件 import + register(app)，
        才是「冷启」的真实成本（worker 崩溃后重启不再重复采样，属已知局限）。
        """
        pid = manifest.get("id") or "?"
        seen = getattr(self, "_cold_seen", None)
        if seen is None:
            seen = self._cold_seen = set()
        if pid in seen:
            return
        seen.add(pid)
        self._log("[perf] worker-cold-roundtrip %s %.0fms"
                  % (pid, (time.perf_counter() - t0) * 1000.0))

    def _roundtrip(self, proc, manifest, method, path, headers, body, timeout_ms, resident):
        _t_all = time.perf_counter()
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
            self._cold_note(manifest, _t_all)
            return 502, {"Content-Type": "application/json"}, json.dumps(
                {"error": "本地插件响应体超过 %dMB 上限（规范 L-3）——大结果请写临时文件传路径"
                          % (WORKER_BODY_LIMIT // 1048576)}).encode()
        self._cold_note(manifest, _t_all)
        return resp.get("status", 200), resp.get("headers", {}), body

    # ---------------- R2.3：并发派发（opt-in；concurrency<=1 走原串行路径） ----------------

    def _conc_of(self, manifest):
        """该插件的声明并发度（缺省 1；夹在 1..8）。非声明即现状。"""
        try:
            n = int(manifest.get("concurrency") or 1)
        except (TypeError, ValueError):
            n = 1
        return max(1, min(n, 8))

    def _ensure_pipe(self, pid, proc):
        """为某 worker 起读线程 + future 表 + 写锁（仅并发模式用；每进程一个）。"""
        st = self._pipes.get(pid)
        if st is not None and st.get("proc") is proc and not st["reader"].is_alive() is False:
            return st
        st = {"proc": proc, "futures": {}, "wlock": threading.Lock()}
        th = threading.Thread(target=self._reader_loop, args=(pid, proc),
                              daemon=True, name="pipe-read-" + str(pid))
        st["reader"] = th
        self._pipes[pid] = st
        th.start()
        return st

    def _reader_loop(self, pid, proc):
        """单读线程：把 worker 的响应行按 request_id 派发给等待的 future。

        响应可能**乱序**（并发模式）或顺序（串行 worker 兼容）——按 id 配对两种情况都对。
        """
        st = self._pipes.get(pid) or {}
        try:
            for raw in proc.stdout:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    resp = json.loads(line)
                except Exception:
                    continue
                rid = resp.get("request_id")
                fut = (st.get("futures") or {}).pop(rid, None)
                if fut is not None and not fut.done():
                    fut.set_result(resp)
        except Exception:
            pass
        finally:
            for rid, fut in list((st.get("futures") or {}).items()):
                if not fut.done():
                    fut.set_exception(WorkerCrash("worker 无响应退出"))

    def _roundtrip_concurrent(self, proc, manifest, method, path, headers, body,
                              timeout_ms, resident):
        """并发往返：写请求（写锁下）→ 等对应 request_id 的 future（不占串行锁）。"""
        import concurrent.futures as _cf
        pid = manifest.get("id", "?")
        st = self._ensure_pipe(pid, proc)
        rid = uuid.uuid4().hex
        req = {"request_id": rid, "method": method, "path": path,
               "headers": headers or {}, "body_b64": base64.b64encode(body or b"").decode()}
        fut = _cf.Future()
        st["futures"][rid] = fut
        try:
            with st["wlock"]:
                proc.stdin.write((json.dumps(req) + "\n").encode())
                proc.stdin.flush()
        except (OSError, ValueError) as exc:
            st["futures"].pop(rid, None)
            raise WorkerCrash(str(exc))
        timeout = max(timeout_ms or 60000, 1000) / 1000.0
        try:
            resp = fut.result(timeout=timeout)
        except _cf.TimeoutError:
            # 超时：丢弃该 future（worker 侧可能仍在跑；晚到的响应会因 id 已弹出而被忽略）
            st["futures"].pop(rid, None)
            if resident and not manifest.get("_killed"):
                self._residents.pop(pid, None)
            raise WorkerTimeout("worker 超时")
        if "error" in resp:
            raise WorkerCrash(resp["error"])
        out = base64.b64decode(resp.get("body_b64") or "")
        if len(out) > WORKER_BODY_LIMIT:
            return 502, {"Content-Type": "application/json"}, json.dumps(
                {"error": "本地插件响应体超过 %dMB 上限（规范 L-3）——大结果请写临时文件传路径"
                          % (WORKER_BODY_LIMIT // 1048576)}).encode()
        return resp.get("status", 200), resp.get("headers", {}), out

    def _audit(self, plugin_id, method, path, seconds, outcome, user=""):
        """审计行**入队**（S2-5，2026-10-17）：请求路径不再触碰文件 IO。

        原实现每请求一次 `makedirs` + `open` + `write` + `close`，在插件高频请求下是可见开销。
        现改为内存队列 + 后台批量 flush —— 范式与主包 `jz_host/access_log.py` 的
        「HTTP 线程只入队、QueueListener 后台落盘」一致（那边用 logging 的 QueueHandler，
        本处是纯文本行，故用裸 `queue.Queue`）。

        ⚠ **语义变化（重要）**：审计从「每请求同步落盘」变为**尽力落盘** —— 进程被强杀时可能丢
        尾部若干行；退出路径必须调 :meth:`flush_audit`（已接进 :meth:`shutdown`）。队列满时丢弃
        并计数（首次与每千次记一行到客户端日志），**绝不阻塞请求**。
        """
        line = "%s user=%s %s %s %s %.2fs -> %s\n" % (
            time.strftime("%Y-%m-%d %H:%M:%S"), user or "-", plugin_id, method, path,
            seconds, outcome)
        try:
            self._audit_q.put_nowait(line)
        except queue.Full:
            self._audit_dropped += 1
            if self._audit_dropped in (1, 1000) or self._audit_dropped % 10000 == 0:
                self._log("审计队列已满，开始丢弃审计行（累计 %d 行）" % self._audit_dropped)
            return
        self._ensure_audit_flusher()

    def _ensure_audit_flusher(self):
        """首个审计行到达时惰性起后台 flush 线程（最简形态：不在装配期白起线程）。"""
        if self._audit_thread is not None:
            return
        with self._audit_lock:
            if self._audit_thread is not None:
                return
            th = threading.Thread(target=self._audit_loop, daemon=True, name="audit-flush")
            self._audit_thread = th
            th.start()

    def _audit_loop(self):
        """后台批量落盘：最多等 0.5s 攒一批（≤256 行）再一次写。"""
        while True:
            try:
                first = self._audit_q.get(timeout=0.5)
            except queue.Empty:
                if self._audit_stop.is_set():
                    return
                continue
            buf = [first]
            while len(buf) < 256:
                try:
                    buf.append(self._audit_q.get_nowait())
                except queue.Empty:
                    break
            self._audit_write(buf)

    def _audit_write(self, lines):
        try:
            os.makedirs(os.path.dirname(self._audit_path), exist_ok=True)
            with open(self._audit_path, "a", encoding="utf-8") as f:
                f.writelines(lines)
        except OSError:
            pass

    def flush_audit(self, timeout=2.0):
        """把队列里剩余审计行落盘（退出路径**必须先调**：flush 审计 → 收割 worker → 退出）。

        timeout 是 join 上限：超时后仍会把队列里剩余的直接写掉，避免后台线程被卡住时丢日志；
        但若后台线程正卡在磁盘 IO 上，本函数不会无限等（退出不能因为审计而挂住）。
        """
        self._audit_stop.set()
        th = self._audit_thread
        if th is not None:
            th.join(timeout)
        rest = []
        while True:
            try:
                rest.append(self._audit_q.get_nowait())
            except queue.Empty:
                break
        if rest:
            self._audit_write(rest)



class WorkerTimeout(Exception):
    pass


class WorkerCrash(Exception):
    pass
