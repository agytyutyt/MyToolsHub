# -*- coding: utf-8 -*-
"""supervisor 核心（T-O2-11）：受管单元注册表 / 启动编排 / 健康探针 / 熔断拉起 /
日志归集 / Job Object 孤儿收割。

设计口径（docs/design/服务端插件运行架构-设计文档.md，2026-10-05 拍板）：
- **轻量纪律**：仅标准库，不 import 主包任何模块（jz_* / flask）——它必须比
  受管对象更"不倒"；
- **端口**：首次为插件单元 free_port() 挑选（限配置段，缺省 5101-5199）→
  **原子写回注册表长期占用**；冲突报错不起（不静默换号，保主应用反代缓存正确性）；
  worker 经环境变量 JZ_WORKER_PORT 接收，自己不做端口决策；
- **熔断**：连续 N 次（缺省 3）崩溃/健康失败 → 下线（circuit-open），不再自动
  拉起，恢复走控制接口 clear-circuit / restart；
- **Job Object**：子进程全部纳入 kill-on-close 的 Job——supervisor 被杀时全家
  同死（无孤儿），**必须**配套服务恢复策略（NSSM/SCM 失败自动重启）；
- **数据根**：环境变量 JZTOOLS_DATA_ROOT 优先，其次 --data-root 参数；注册表
  在 <数据根>\\config\\supervisor-units.json，令牌在 <数据根>\\config\\.supervisor_token。

与主包的边界：主包目录路径、main_url、jz_worker.py 脚本路径全部来自注册表
顶层 `worker` 节（部署配置），supervisor 不探测、不推导。
"""

import ctypes
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile

from supervisor import apply_chain

CREATE_NO_WINDOW = 0x08000000          # Windows：子进程不开控制台窗口
REGISTRY_SCHEMA = 1
DEFAULT_PORT_RANGE = (5101, 5199)
DEFAULT_CIRCUIT_THRESHOLD = 3
DEFAULT_HEALTH_INTERVAL = 10.0
DEFAULT_HEALTH_GRACE = 8.0          # 启动宽限：worker 导入期（0.5–2s+）不做探针
DEFAULT_STABLE_WINDOW = 60.0        # 稳定期：存活超过该时长才清空「快速崩溃连击」
HEALTH_TIMEOUT = 3.0
MAX_BACKOFF = 30.0


def _log_msg(msg):
    """进程级轻量日志（supervisor.log 由 run 入口接管后替换为写文件版）。"""
    sys.stderr.write("[supervisor] %s\n" % msg)


# ======================== 路径与注册表 ========================

def registry_path(data_root):
    return os.path.join(data_root, "config", "supervisor-units.json")


def token_path(data_root):
    return os.path.join(data_root, "config", ".supervisor_token")


def worker_log_path(data_root, unit_name):
    return os.path.join(data_root, "logs", "workers", "%s.log" % unit_name)


def load_registry(data_root):
    p = registry_path(data_root)
    if not os.path.isfile(p):
        return {"schema": REGISTRY_SCHEMA, "supervisor": {"port": 5100},
                "units": []}
    with open(p, "r", encoding="utf-8-sig") as f:
        obj = json.load(f)
    if not isinstance(obj, dict) or obj.get("schema") != REGISTRY_SCHEMA:
        raise ValueError("supervisor-units.json schema 不受支持：%r" % obj.get("schema"))
    obj.setdefault("units", [])
    return obj


def save_registry(data_root, obj):
    """原子写回（tmp + os.replace）——端口分配持久化走这里（拍板：分配一次长期占用）。"""
    obj["schema"] = REGISTRY_SCHEMA
    p = registry_path(data_root)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def find_unit(registry, name):
    for u in registry.get("units") or []:
        if isinstance(u, dict) and u.get("name") == name:
            return u
    return None


def _port_in_use(port):
    """端口是否被**真实监听者**占用（connect 探测）。

    不用 bind 探测：Windows 上 TIME_WAIT 残留连接会让同端口重启的 bind 探测
    误报占用（连接关闭后最多残留 ~4 分钟），worker 升级停换起会被卡死。
    """
    s = socket.socket()
    s.settimeout(0.5)
    try:
        s.connect(("127.0.0.1", int(port)))
        return True                      # 连得上 = 有监听者
    except OSError:
        return False
    finally:
        s.close()


def ensure_unit_port(data_root, registry, unit):
    """确保插件单元有端口：缺 → 配置段内挑空闲并**写回注册表**；已分配 → 复检占用。

    占用即抛错（拍板：冲突报错不起，不静默换号）。
    """
    port = unit.get("port")
    if port:
        if _port_in_use(port):
            raise RuntimeError("单元 %s 的端口 %s 已被其他进程占用（拒不换号，请人工处理）"
                               % (unit.get("name"), port))
        return int(port)
    lo, hi = registry.get("port_range") or DEFAULT_PORT_RANGE
    used = {int(u.get("port")) for u in registry.get("units") or [] if u.get("port")}
    for cand in range(int(lo), int(hi) + 1):
        if cand in used:
            continue
        if not _port_in_use(cand):
            unit["port"] = cand
            save_registry(data_root, registry)          # 分配一次、持久化（§2.1）
            return cand
    raise RuntimeError("端口段 %s-%s 已耗尽，无法为单元 %s 分配端口" % (lo, hi, unit.get("name")))


# ======================== Job Object（孤儿收割） ========================

class JobObject:
    """Windows Job Object（kill-on-close）：supervisor 退出 → 全部子进程同死。

    拍板代价（§2）：supervisor 崩溃 = 全平台重启，必须配套 NSSM/SCM 服务恢复。
    非 Windows / 句柄不可用时静默降级（孤儿收割失效但不影响其余职责）。
    """

    def __init__(self):
        self._handle = None
        if sys.platform == "win32":
            try:
                k32 = ctypes.windll.kernel32
                h = k32.CreateJobObjectW(None, None)
                if not h:
                    return
                cls = ctypes.c_ulonglong(9)   # JobObjectExtendedLimitInformation
                # JOBOBJECT_EXTENDED_LIMIT_INFORMATION：BasicLimit.LimitFlags
                # = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE (0x2000)
                buf = ctypes.create_string_buffer(144)
                ctypes.memset(buf, 0, 144)
                # BasicLimitInformation.LimitFlags 位于结构体偏移 20（SIZE_T×2+ DWORD×2）
                import struct
                struct.pack_into("<I", buf, 20, 0x2000)
                if k32.SetInformationJobObject(h, cls, buf, 144):
                    self._handle = h
                else:
                    k32.CloseHandle(h)
            except Exception:
                self._handle = None

    def assign(self, proc):
        """把子进程纳入 Job（Popen 的进程句柄直接复用）。"""
        if self._handle is None or proc is None:
            return False
        try:
            return bool(ctypes.windll.kernel32.AssignProcessToJobObject(
                self._handle, int(proc._handle)))
        except Exception:
            return False

    def close(self):
        if self._handle:
            ctypes.windll.kernel32.CloseHandle(self._handle)
            self._handle = None


# ======================== Supervisor 本体 ========================

class Supervisor:
    """受管单元容器：main + 各插件 worker 的编排、健康、熔断、日志。"""

    def __init__(self, data_root, log=None):
        self.data_root = os.path.abspath(data_root)
        self._log = log or _log_msg
        self._lock = threading.RLock()
        self._procs = {}          # unit_name -> Popen
        self._fail = {}           # unit_name -> 连续探针失败计数（存活但探针不通）
        self._crash = {}          # unit_name -> 快速崩溃连击（稳定期统计，见下）
        self._started_at = {}     # unit_name -> 最近一次启动时刻（宽限/稳定期基准）
        self._circuit = {}        # unit_name -> True（熔断下线中）
        self._applying = set()    # 升级编排中的单元——监控跳过（否则停换起窗口会被误熔断）
        self._stop_flag = threading.Event()
        self._stop_requested = set()      # 人工 stop 的单元（不自动拉起）
        self._job = JobObject()
        self._registry = load_registry(self.data_root)
        self._monitor = None

    # ---- 基本量 ----

    @property
    def registry(self):
        with self._lock:
            return self._registry

    def _save(self):
        with self._lock:
            save_registry(self.data_root, self._registry)

    def supervisor_port(self):
        return int((self._registry.get("supervisor") or {}).get("port") or 5100)

    def worker_cfg(self):
        """顶层 worker 节：拉起 worker 所需的部署配置（exe/脚本/主应用 URL/插件目录）。"""
        return self._registry.get("worker") or {}

    # ---- 命令组装 ----

    def _unit_cmd(self, unit, port):
        """组装 worker 启动命令。worker 自身不做任何端口/路径决策（全部注入）。"""
        wc = self.worker_cfg()
        if unit.get("kind") == "main":
            cmd = list(unit.get("cmd") or [])
            if not cmd:
                raise RuntimeError("main 单元缺少 cmd")
            return cmd
        pid = unit.get("plugin_id") or unit.get("name")
        interp = unit.get("interpreter") or "main"
        if interp == "main":
            exe = wc.get("main_exe")            # frozen：JZToolsHub.exe
            script = wc.get("worker_script")    # 源码：jz_worker.py
            python = wc.get("python") or sys.executable
            if exe:
                return [exe, "--worker", pid]
            if script:
                return [python, script, "--worker", pid]
            raise RuntimeError("注册表 worker 节缺 main_exe / worker_script，无法拉起插件 worker")
        # venv：unit.interpreter = venv python 路径（worker_script 必须为源码 .py）
        script = wc.get("worker_script")
        if not script:
            raise RuntimeError("venv 单元需要 worker_script（源码形态 .py）")
        return [interp, script, "--worker", pid]

    def _unit_env(self, unit, port):
        env = dict(os.environ)
        env["JZ_WORKER_PORT"] = str(port)
        env["JZTOOLS_DATA_ROOT"] = self.data_root          # 与主应用同一数据根
        wc = self.worker_cfg()
        if wc.get("main_url"):
            env["JZ_WORKER_MAIN_URL"] = wc["main_url"]
        if wc.get("plugins_dir"):
            env["JZ_PLUGINS_DIR"] = wc["plugins_dir"]
        if wc.get("main_pkg_dir"):
            env["JZ_MAIN_PKG_DIR"] = wc["main_pkg_dir"]
        return env

    # ---- 启动 / 停止 ----

    def start_unit(self, name):
        with self._lock:
            unit = find_unit(self._registry, name)
            if unit is None:
                return {"ok": False, "error": "单元不存在：%s" % name}
            if not unit.get("enabled", True):
                return {"ok": False, "error": "单元已停用：%s" % name}
            if self._circuit.get(name):
                return {"ok": False, "error": "单元处于熔断下线（先 clear-circuit）：%s" % name}
            old = self._procs.get(name)
            if old is not None and old.poll() is None:
                return {"ok": True, "note": "已在运行", "pid": old.pid}
            try:
                port = ensure_unit_port(self.data_root, self._registry, unit)
                cmd = self._unit_cmd(unit, port)
                env = self._unit_env(unit, port)
            except Exception as exc:
                self._log("单元 %s 启动前置失败：%s" % (name, exc))
                return {"ok": False, "error": str(exc)}
            lpath = worker_log_path(self.data_root, name)
            os.makedirs(os.path.dirname(lpath), exist_ok=True)
            lf = open(lpath, "ab")
            try:
                flags = CREATE_NO_WINDOW if sys.platform == "win32" else 0
                proc = subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT,
                                        env=env, cwd=(self.worker_cfg().get("main_pkg_dir")
                                                      or os.getcwd()),
                                        creationflags=flags)
            except Exception as exc:
                lf.close()
                self._log("单元 %s 拉起失败：%s" % (name, exc))
                return {"ok": False, "error": "拉起失败：%s" % exc}
            self._job.assign(proc)                        # 孤儿收割（kill-on-close）
            self._procs[name] = proc
            self._stop_requested.discard(name)
            self._fail[name] = 0
            self._started_at[name] = time.time()
            self._log("单元已拉起：%s（pid=%s，port=%s，cmd=%s）"
                      % (name, proc.pid, port, " ".join(cmd)))
            return {"ok": True, "pid": proc.pid, "port": port}

    def stop_unit(self, name, timeout=8.0):
        with self._lock:
            self._stop_requested.add(name)
            proc = self._procs.pop(name, None)
        if proc is None:
            return {"ok": True, "note": "未在运行"}
        try:
            proc.terminate()
            proc.wait(timeout=timeout)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        self._log("单元已停止：%s" % name)
        return {"ok": True}

    # ---- 健康探针 + 熔断（监控线程） ----

    def _health_url(self, unit):
        if unit.get("kind") == "main":
            return unit.get("health_url") or ""
        port = unit.get("port")
        return "http://127.0.0.1:%s/_worker/health" % port if port else ""

    def _probe(self, url):
        if not url:
            return False
        try:
            with urllib.request.urlopen(url, timeout=HEALTH_TIMEOUT) as r:
                return r.status == 200
        except Exception:
            return False

    def _monitor_loop(self):
        """健康监控 + 熔断（双计数器）：

        - _fail：进程存活但探针连续失败 N 次 → 熔断（恢复即清零）；
        - _crash：**快速崩溃连击**——进程在稳定期窗口（缺省 60s）内再次退出则 +1，
          存活超过窗口才清零。路由触发型毒插件每次重启后探针都正常，若只用
          _fail 永远到不了阈值（2026-10-05 台架设计时发现）；两者任一达阈值即熔断。
        """
        while not self._stop_flag.is_set():
            interval = float((self._registry.get("health_interval")
                              or DEFAULT_HEALTH_INTERVAL))
            threshold = int(self._registry.get("circuit_threshold")
                            or DEFAULT_CIRCUIT_THRESHOLD)
            grace = float(self._registry.get("health_grace") or DEFAULT_HEALTH_GRACE)
            stable = float(self._registry.get("stable_window") or DEFAULT_STABLE_WINDOW)
            now = time.time()
            for unit in list(self._registry.get("units") or []):
                name = unit.get("name")
                if not unit.get("enabled", True) or self._circuit.get(name) \
                        or name in self._applying:
                    continue
                if self._stop_flag.is_set():
                    break
                proc = self._procs.get(name)
                if proc is None:
                    continue        # 未托管（未启动/人工已停）——不探针、不计数
                alive = proc.poll() is None
                started = self._started_at.get(name, 0.0)
                if alive and now - started < grace:
                    continue                        # 启动宽限：导入期不探针不计数
                ok = alive and self._probe(self._health_url(unit))
                if ok:
                    self._fail[name] = 0
                    if now - started >= stable:
                        self._crash[name] = 0       # 稳定期存活 → 崩溃连击清零
                    continue
                if not alive:
                    # 死亡记账（同一具进程只记一次：记账后即摘除，防重拉失败时重复累计）
                    self._procs.pop(name, None)
                    streak = self._crash.get(name, 0) + (1 if now - started < stable else 0)
                    if now - started >= stable:
                        streak = 1                  # 长稳后的一次退出不延续旧连击
                    self._crash[name] = streak
                    fails = self._fail.get(name, 0)
                    self._log("单元 %s 进程退出（code=%s，快速崩溃连击 %d/%d）"
                              % (name, proc.returncode, streak, threshold))
                else:
                    fails = self._fail.get(name, 0) + 1
                    self._fail[name] = fails
                    self._log("单元 %s 探针失败（%d/%d，alive）" % (name, fails, threshold))
                if fails >= threshold or self._crash.get(name, 0) >= threshold:
                    self._circuit[name] = True
                    if alive:
                        self.stop_unit(name)
                    self._log("!! 单元 %s 熔断下线（探针连败 %d / 崩溃连击 %d）——"
                              "控制接口 clear-circuit / restart 恢复"
                              % (name, fails, self._crash.get(name, 0)))
                    continue
                if not alive and name not in self._stop_requested:
                    # 崩溃拉起（指数退避）
                    backoff = min(2.0 * max(self._crash.get(name, 1), 1), MAX_BACKOFF)
                    self._log("单元 %s → %.1fs 后重拉" % (name, backoff))
                    self._stop_flag.wait(backoff)
                    if self._stop_flag.is_set():
                        break
                    self.start_unit(name)
            self._stop_flag.wait(max(0.2, interval))

    def start_monitor(self):
        if self._monitor is None or not self._monitor.is_alive():
            self._stop_flag.clear()
            self._monitor = threading.Thread(target=self._monitor_loop,
                                             daemon=True, name="supervisor-monitor")
            self._monitor.start()

    # ---- 状态 / 熔断恢复 ----

    def status(self):
        rows = []
        for unit in list(self._registry.get("units") or []):
            name = unit.get("name")
            proc = self._procs.get(name)
            rows.append({
                "name": name, "kind": unit.get("kind"),
                "plugin_id": unit.get("plugin_id"),
                "port": unit.get("port"),
                "running": bool(proc is not None and proc.poll() is None),
                "pid": proc.pid if proc is not None and proc.poll() is None else None,
                "circuit_open": bool(self._circuit.get(name)),
                "fail_count": self._fail.get(name, 0),
                "crash_streak": self._crash.get(name, 0),
                "enabled": bool(unit.get("enabled", True)),
                "installed_version": unit.get("installed_version") or "",
            })
        return {"ok": True, "data_root": self.data_root, "units": rows}

    def clear_circuit(self, name):
        with self._lock:
            self._circuit.pop(name, None)
            self._fail[name] = 0
            self._crash[name] = 0
        return {"ok": True, "note": "熔断已清除（可 start/restart）"}

    # ---- 升级编排（委托 apply_chain；不打 restart_pending——设计 §2 拍板） ----

    def apply_package(self, plugin_id, package_path, timeout=30.0):
        return apply_chain.apply_package(self, plugin_id, package_path, timeout=timeout)

    def rollback_unit(self, plugin_id):
        return apply_chain.rollback_unit(self, plugin_id)

    # ---- 全量启停 ----

    def start_all(self):
        order = [u for u in self._registry.get("units") or []]
        order.sort(key=lambda u: 0 if u.get("kind") == "main" else 1)
        results = []
        for unit in order:
            if unit.get("enabled", True):
                results.append({unit["name"]: self.start_unit(unit["name"])})
        return results

    def shutdown(self):
        self._stop_flag.set()
        for name in [n for n in self._procs if n != "main"]:
            self.stop_unit(name, timeout=3.0)
        for name in list(self._procs):
            self.stop_unit(name, timeout=5.0)
        self._job.close()
