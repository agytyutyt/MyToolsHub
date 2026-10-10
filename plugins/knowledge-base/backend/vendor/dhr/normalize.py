"""`.doc` 归一化 —— LibreOffice `.doc → .docx`，复用现有链路（T-901 / M3）。

与 Excel 版同构（复用 xhr.normalize.libreoffice 的探测与执行器），只把
`--convert-to xlsx` 换成 `--convert-to docx`。五条铁律不变：
  1. 唯一临时 UserInstallation profile（并发安全）；
  2. 不能只看退出码，必须检查输出文件存在；
  3. 输出名固定为 <主名>.docx；
  4. 超时杀进程树；
  5. 临时目录清理。

⚠️ 归一化后的 docx 没有 `w:lastRenderedPageBreak` → paged 模式分页不准，
   `.doc` 建议默认 flow 模式（设计文档 6.2 / R-2）。

【本地适配 · P2-21】本文件相对上游有一处**非上游 bugfix** 的接缝改动
（profile 提供者钩子，与 xhr 版同构），登记于 vendor/README.md 补丁清单第 7 条；
上游整目录同步时需按该条重打，默认行为（不注册钩子）与上游完全一致。
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import Callable, Optional

from xhr.core import XhrError
from xhr.ingest import unzip_guarded
from xhr.normalize.libreoffice import find_soffice

#: 并发信号量（与 Excel 版一致，LO 并发多了会互相干扰）
import threading

_SEMA = threading.Semaphore(2)


def _run_soffice(soffice: str, args: list, timeout_ms: int) -> None:
    import subprocess
    import sys

    creationflags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW
    proc = subprocess.Popen(
        [soffice, *args],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )
    try:
        proc.wait(timeout=timeout_ms / 1000)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except OSError:
            pass
        if sys.platform == "win32":
            try:
                subprocess.Popen(
                    ["taskkill", "/pid", str(proc.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=creationflags,
                )
            except OSError:
                pass
        raise XhrError("TIMEOUT", f"LibreOffice 转换超过 {timeout_ms}ms，已强制终止")
    if proc.returncode != 0:
        raise XhrError("CORRUPTED", f"LibreOffice 退出码 {proc.returncode}")


# ===================== 【本地适配 · P2-21】profile 提供者接缝 =====================
# 与 xhr/normalize/libreoffice.py 同构：不注册时行为与上游完全一致（每次新建即删）；
# 注册后由上层提供持久 profile 槽位（热启动），失败仍由提供方清理该槽位。
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


def normalize_doc_to_docx(data: bytes, soffice_path: Optional[str] = None, timeout_ms: int = 60_000) -> bytes:
    """.doc 字节 → .docx 字节。失败抛 XhrError（TIMEOUT/CORRUPTED/UNSUPPORTED_FORMAT）。"""
    soffice = find_soffice(soffice_path)
    if not soffice:
        raise XhrError("UNSUPPORTED_FORMAT", "未找到 LibreOffice（soffice），无法归一化 .doc；请安装 LibreOffice 或另存为 .docx")

    work_dir = tempfile.mkdtemp(prefix="dhr-doc-")
    # 【P2-21 接缝】优先租用持久 profile；取不到则回退上游「新建即删」
    lease = _acquire_profile()
    owned = lease is None
    if owned:
        profile = tempfile.mkdtemp(prefix="dhr-lo-")
        release = None
    else:
        profile, release = lease
    ok = False
    try:
        in_file = os.path.join(work_dir, "input.doc")
        with open(in_file, "wb") as f:
            f.write(data)
        profile_url = Path(profile).as_posix()
        args = [
            "--headless",
            "--norestore",
            "--nolockcheck",
            f"-env:UserInstallation=file:///{profile_url}",
            "--convert-to",
            "docx",
            "--outdir",
            work_dir,
            in_file,
        ]
        with _SEMA:
            _run_soffice(soffice, args, timeout_ms)
        out_path = os.path.join(work_dir, "input.docx")
        # 铁律 2：退出码 0 不代表成功，必须检查输出文件存在
        if not os.path.isfile(out_path):
            raise XhrError("CORRUPTED", "LibreOffice 未产出输出文件")
        with open(out_path, "rb") as f:
            docx_bytes = f.read()
        ok = True
        return docx_bytes
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
        if owned:
            shutil.rmtree(profile, ignore_errors=True)
        else:
            # 持久槽位：交还提供方；失败（ok=False）由提供方清理该槽位
            try:
                release(ok)
            except Exception:  # noqa: BLE001 —— 提供方异常不得掩盖转换结果
                pass


def docx_files_of(data: bytes) -> dict:
    """docx 字节 → 解包条目。"""
    files, _stats, _skipped = unzip_guarded(data)
    if "word/document.xml" not in files:
        raise XhrError("CORRUPTED", "LibreOffice 归一化产物不是有效的 docx")
    return files
