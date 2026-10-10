"""LibreOffice 归一化服务 —— 对应 TS 版 ``packages/normalize/src/libreoffice.ts``。

职责：把 .xls（BIFF8）转换成 .xlsx，然后复用现有全链路。

⚠️ 三条铁律（违反任何一条都会出现"静默失败"）：
  1. ``-env:UserInstallation`` 必须带唯一临时 profile —— 否则第二个并发进程
     会返回退出码 0 但不产出文件；
  2. 不能只看退出码，必须**检查输出文件真的存在**；
  3. 超时要杀进程树（Windows 下 kill 外壳会留孤儿 soffice.bin）。

【本地适配 · P2-21】本目录随 kb 插件分发，相对上游有一处**非上游 bugfix** 的
接缝改动（profile 提供者钩子），登记于 vendor/README.md 补丁清单第 6 条；
上游整目录同步时需按该条重打，默认行为（不注册钩子）与上游完全一致。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable, Optional

from ..core import XhrError

#: soffice 常见安装路径（按平台）
if sys.platform == "win32":
    _SOFFICE_CANDIDATES = [
        r"C:\Program Files\LibreOffice\program\soffice.exe",
        r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
    ]
elif sys.platform == "darwin":
    _SOFFICE_CANDIDATES = ["/Applications/LibreOffice.app/Contents/MacOS/soffice"]
else:
    _SOFFICE_CANDIDATES = ["/usr/bin/soffice", "/usr/local/bin/soffice"]


def find_soffice(explicit: Optional[str] = None) -> Optional[str]:
    """查找 soffice 可执行文件。

    @param explicit 显式指定的路径（opts.soffice_path / 环境变量 XHR_SOFFICE）
    """
    env = os.environ.get("XHR_SOFFICE")
    for p in [explicit, env, *_SOFFICE_CANDIDATES]:
        if p and os.path.isfile(p):
            return p
    # PATH 上找 soffice / soffice.exe
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if not d:
            continue
        for name in (("soffice.exe", "soffice.bat") if sys.platform == "win32" else ("soffice",)):
            p = os.path.join(d, name)
            if os.path.isfile(p):
                return p
    return None


def _default_runner(cmd: str, args: list, timeout_ms: int) -> None:
    """真实执行器：Popen + 超时杀进程树。"""
    creationflags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW
    proc = subprocess.Popen([cmd, *args], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=creationflags)
    try:
        proc.wait(timeout=timeout_ms / 1000)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except OSError:
            pass
        if sys.platform == "win32":
            try:
                # Windows 下 kill 只杀 soffice 外壳会留孤儿 soffice.bin，兜底杀进程树
                subprocess.Popen(
                    ["taskkill", "/pid", str(proc.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=creationflags,
                )
            except OSError:
                pass  # 进程可能已退出
        raise XhrError("TIMEOUT", f"LibreOffice 转换超过 {timeout_ms}ms，已强制终止")
    if proc.returncode != 0:
        # 铁律 2：退出码只是必要条件，输出存在性由调用方检查
        raise XhrError("CORRUPTED", f"LibreOffice 退出码 {proc.returncode}")


# ===================== 【本地适配 · P2-21】profile 提供者接缝 =====================
# 上游行为：每次转换 mkdtemp 新建 profile、finally 立即 rmtree（冷启动 ~37-40s）。
# 本接缝允许多个转换**复用一个持久 profile**（热启动 ~15-18s），由上层注册
# 「提供者」实现槽位池、TTL 与失败清理；不注册时行为与上游逐字节等价。
#
# 约定：provider() 返回 ``(profile_dir, release)`` 或 ``None``；
#   - ``None`` → 回退到上游的「临时 profile 新建即删」路径；
#   - ``release(ok: bool)`` 由本函数在结束时调用：ok=False（转换失败）时
#     提供方应清理该槽位 profile（铁律不回退：失败仍清理）。
# provider 自身抛异常 → 同样回退临时路径（由本函数兜住，不外泄）。
_profile_provider: Optional[Callable] = None


def set_profile_provider(provider: Optional[Callable]) -> None:
    """注册/清除 profile 提供者（None = 恢复上游默认行为）。"""
    global _profile_provider
    _profile_provider = provider


def _acquire_profile() -> Optional[tuple]:
    prov = _profile_provider
    if prov is None:
        return None
    try:
        lease = prov()
    except Exception:  # noqa: BLE001 —— 提供者异常不得影响转换（降级临时 profile）
        return None
    if not lease:
        return None
    try:
        path, release = lease
    except Exception:  # noqa: BLE001
        return None
    if not path or not callable(release):
        return None
    return (str(path), release)
# ===================== 接缝结束 =====================


def convert_to_xlsx(
    input_path: str,
    out_dir: str,
    soffice_path: Optional[str] = None,
    timeout_ms: int = 60_000,
    runner: Optional[Callable[[str, list, int], None]] = None,
) -> str:
    """把 .xls 转成 .xlsx，返回输出文件路径。

    输出文件名固定为 <输入主名>.xlsx（LibreOffice 的行为），调用方按需重命名。
    """
    soffice = find_soffice(soffice_path)
    if not soffice:
        raise XhrError("UNSUPPORTED_FORMAT", "未找到 LibreOffice（soffice），无法归一化 .xls")

    # 【P2-21 接缝】优先向上层租用持久 profile；取不到则回退上游「新建即删」
    lease = _acquire_profile()
    owned = lease is None  # owned=True 表示 profile 由本函数创建并负责删除
    if owned:
        profile = tempfile.mkdtemp(prefix="xhr-lo-")
        release = None
    else:
        profile, release = lease
    ok = False
    try:
        profile_url = Path(profile).as_posix()
        args = [
            "--headless",
            "--norestore",
            "--nolockcheck",
            f"-env:UserInstallation=file:///{profile_url}",
            "--convert-to",
            "xlsx",
            "--outdir",
            out_dir,
            input_path,
        ]
        run = runner or _default_runner
        run(soffice, args, timeout_ms)

        # 铁律 2：退出码 0 不代表成功，必须确认输出文件存在
        base = Path(input_path).stem
        out_path = str(Path(out_dir) / f"{base}.xlsx")
        if not os.path.isfile(out_path):
            raise XhrError("CORRUPTED", f"LibreOffice 未产出输出文件（输入：{Path(input_path).name}）")
        ok = True
        return out_path
    finally:
        if owned:
            # 卫生要求：临时 profile 必须清理
            shutil.rmtree(profile, ignore_errors=True)
        else:
            # 持久槽位：交还提供方；失败（ok=False）由提供方清理该槽位
            try:
                release(ok)
            except Exception:  # noqa: BLE001 —— 提供方异常不得掩盖转换结果
                pass


#: 并发信号量（LibreOffice 并发多了会互相干扰）
import threading  # noqa: E402

_SEMA = threading.Semaphore(2)


def convert_to_xlsx_guarded(
    input_path: str,
    out_dir: str,
    soffice_path: Optional[str] = None,
    timeout_ms: int = 60_000,
    max_concurrent: int = 2,
) -> str:
    """带并发控制的转换入口（对外推荐使用）。"""
    with _SEMA:
        return convert_to_xlsx(input_path, out_dir, soffice_path=soffice_path, timeout_ms=timeout_ms)
