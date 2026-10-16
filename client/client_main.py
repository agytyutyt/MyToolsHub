# -*- coding: utf-8 -*-
"""客户端入口（批次 1）：能力层服务（UI 层 pywebview 在批次 2 T10 接入）。

启动序列：单实例互斥 → 读构建/客户端配置 → 解析地址 → 绑定 127.0.0.1 随机空闲端口
→ waitress（threads=8 同主包口径）→ 日志留痕（CAPABILITY listening 行为 smoke 探测锚点）。
"""
import os
import socket
import subprocess
import sys
import time
import traceback

_here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_here))  # 仓库根：内核模块（jz_deps 等）可 import

from client_config import (  # noqa: E402
    load_build_config, load_client_config, resolve_address, static_dir,
    fallback_page, log_path, pid_file_path, port_file_path,
)
from single_instance import acquire_or_exit  # noqa: E402

_LOG_LOCK = __import__("threading").Lock()


def log(msg):
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    with _LOG_LOCK:
        try:
            with open(log_path(), "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


def free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def handle_stop(build_cfg):
    """--stop：结束运行中的实例（无窗阶段的运维手段；托盘在批次 2 T10）。

    校验 PID 对应的镜像名确属本程序，防止 PID 复用误杀无关进程。
    """
    product = (build_cfg.get("product") or {})
    exe_name = product.get("name", "JZClient") + ".exe"
    pf = pid_file_path()
    if not os.path.isfile(pf):
        print("未发现运行中的实例（无 %s）" % pf)
        return 0
    pid = open(pf, "r", encoding="utf-8").read().strip()
    out = subprocess.run(["tasklist", "/FI", "PID eq %s" % pid, "/FO", "CSV", "/NH"],
                         capture_output=True, text=True).stdout
    image = out.split(",")[0].strip('"') if "," in out else ""
    if exe_name.lower() not in image.lower():
        print("PID %s 已不是本程序（当前镜像 %r），清理残留 pid 文件" % (pid, image))
        try:
            os.remove(pf)
        except OSError:
            pass
        return 0
    # D-3（2026-10-17）：/T 连进程树一起收，防常驻 worker 卡死时成孤儿（旧实现无 /T）
    subprocess.run(["taskkill", "/PID", pid, "/T", "/F"], capture_output=True)
    print("已结束实例 PID=%s（%s）" % (pid, image))
    try:
        os.remove(pf)
        os.remove(port_file_path())
    except OSError:
        pass
    return 0


def await_prev_instance():
    """重启接棒（--await-pid <pid>；由 /local-api/restart 拉起的新实例带上）。

    用 OpenProcess(SYNCHRONIZE)+WaitForSingleObject 等旧实例**真实退出**再继续抢
    单实例互斥——不看时间差、不用 cmd（2026-10-08 改版，旧实现靠 timeout 延迟 + 0.6s
    赛跑，在无控制台进程里不可靠）。拿不到句柄（进程已不在）直接放行。
    """
    try:
        i = sys.argv.index("--await-pid")
        pid = sys.argv[i + 1] if i + 1 < len(sys.argv) else ""
    except ValueError:
        return
    if not str(pid).isdigit():
        return
    why = "未等待"
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x00100000, False, int(pid))     # SYNCHRONIZE
        if not h:
            why = "旧进程已不存在"
        else:
            try:
                rc = k32.WaitForSingleObject(h, 20000)
                why = "旧进程已退出" if rc == 0 else "等待超时（20s，仍继续启动）"
            finally:
                k32.CloseHandle(h)
    except Exception as exc:
        why = "等待异常（按已退出继续）：%r" % exc
    log("重启接棒：等待旧进程 pid=%s → %s" % (pid, why))
    time.sleep(0.6)          # webview2 子进程退出 / 配置目录释放的收尾宽限


def main():
    build_cfg = load_build_config()
    if "--stop" in sys.argv:
        return handle_stop(build_cfg)
    if "--await-pid" in sys.argv:
        await_prev_instance()
    product = build_cfg.get("product") or {}
    if not acquire_or_exit("JZClient-" + product.get("name", "JZClient")):
        log("已有实例在运行，本实例退出")
        return 0

    client_cfg = load_client_config(build_cfg)
    address = resolve_address(build_cfg, client_cfg)
    data_dir = client_config_data_dir(build_cfg)
    local_plugins = os.path.join(data_dir, "local-plugins")
    os.makedirs(local_plugins, exist_ok=True)

    os.environ["JZTOOLS_DATA_ROOT"] = data_dir        # jztools_data/jz_deps 口径统一
    from client_config import program_dir, runtime_python_exe, host_script_path
    build_cfg["_program_dir"] = program_dir()

    log("=== %s %s 启动（frozen=%s） ===" % (
        product.get("name"), product.get("version"), getattr(sys, "frozen", False)))
    log("远程地址：%s（来源：客户端配置文件/构建配置默认）" % (address or "未配置 → 兜底页"))
    log("数据目录：%s" % data_dir)

    from plugin_executor import PluginExecutor
    worker_python = runtime_python_exe(build_cfg)
    executor = PluginExecutor(
        worker_python, host_script_path(),
        os.path.join(program_dir(), "runtime", "pylibs"), data_dir, log,
        data_root_fn=lambda: os.environ.get("JZTOOLS_DATA_ROOT") or "")
    # 插件 worker 运行时留痕（打包态应为 runtime/python/python.exe；缺则回落本进程解释器=构建缺陷）
    log("插件 worker 运行时：%s" % worker_python)

    from capability_app import create_capability_app
    # 本地端口壳自证 run token（G2-6）：进程内随机；JZCLIENT_SHELL_TOKEN 仅供台架/验收覆盖。
    # 同一值传能力层（种 Cookie 校验）与壳窗口（造 boot URL）。
    import secrets as _secrets
    shell_token = os.environ.get("JZCLIENT_SHELL_TOKEN") or _secrets.token_urlsafe(24)
    app = create_capability_app(
        build_cfg=build_cfg,
        client_cfg=client_cfg,
        address=address,
        static_dir=static_dir(),
        local_plugins_dir=local_plugins,
        fallback_file=fallback_page(),
        data_dir=data_dir,
        executor=executor,
        log=log,
        shell_token=shell_token,
    )

    port = free_port()
    # 当前实例的端口/PID 落独立文件（client.log 是追加式，历史端口行会误导排障）
    with open(port_file_path(), "w", encoding="utf-8") as f:
        f.write(str(port))
    with open(pid_file_path(), "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))

    # 能力层转后台线程（webview.start 占主线程，批次 2 T10）
    import threading
    from waitress import serve

    def _serve():
        try:
            serve(app, host="127.0.0.1", port=port, threads=8)
        except Exception:
            log("waitress 退出：\n" + traceback.format_exc())

    threading.Thread(target=_serve, daemon=True, name="capability-http").start()
    log("CAPABILITY listening http://127.0.0.1:%d (pid=%d)" % (port, os.getpid()))

    try:
        from shell_window import run_shell
        # D-3（2026-10-17）：组合根把「托盘退出的常驻 worker 收割口」注入 UI 层。
        # 否则退出只靠子进程 stdin EOF 自杀，在途长任务不被即时收割（旧缺口）。
        run_shell(build_cfg, client_cfg, port, data_dir, log, shell_token=shell_token,
                  reap_workers=executor.shutdown)
    except Exception:
        log("UI 层异常退出：\n" + traceback.format_exc())
        return 1
    finally:
        try:
            os.remove(port_file_path())
            os.remove(pid_file_path())
        except OSError:
            pass
    return 0


def client_config_data_dir(build_cfg):
    from client_config import data_dir
    return data_dir(build_cfg)


if __name__ == "__main__":
    sys.exit(main())
