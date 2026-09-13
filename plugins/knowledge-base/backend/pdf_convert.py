"""知识库 —— Word/Excel → PDF 转换（LibreOffice headless）。

设计原则
--------
- **外部进程调用，零 Python 依赖**：只用到 subprocess / tempfile / shutil / winreg
  等标准库，不进 PyInstaller 包、不需要 pip 安装（LibreOffice 是**目标机可选外部程序**）。
- **保真优先**：`soffice --headless --convert-to pdf` 走的是 LibreOffice 自身的
  打印/导出管线，doc/docx/xls/xlsx 四格式一个引擎全覆盖，样式保真度接近原生打印，
  远优于 mammoth（丢样式）与 SheetJS（丢列宽/边框/合并）。
- **优雅降级**：探测不到 LibreOffice 时不抛异常、不阻断任何功能——
  `detect_soffice()` 返回 `available=False`，上传/阅读/下载照常可用，
  阅读端回退降级渲染（《插件库优化方案-设计文档》§3.2 降级链，规范 B-4）。
- **服务可靠**：所有转换共用**独立于系统默认的固定 user profile**
  （`-env:UserInstallation` 指向 `<数据根>/.lo-profile`，跨调用复用——
  实测比每次新建 profile 快一倍以上），既避开 soffice 全局单实例互相抢占，
  又省掉每次的 profile 初始化；profile 损坏时自动重置重试一次。
  参数列表化调用（禁止 shell 拼接，SEC-10）；180s 超时；
  模块级锁串行化（与调用方线程池 max_workers=1 双重保险）。

配置（可选）
-----------
管理员可在下列任一位置放 `config.json` 显式指定 soffice 路径（B-5 配置优先）：

```json
{ "pdf": { "soffice_path": "C:\\\\Program Files\\\\LibreOffice\\\\program\\\\soffice.exe" } }
```

查找顺序（先数据根、后模块目录，前者便于运维改且不随程序升级被覆盖）：
1. `<数据根>/plugins/knowledge-base/config.json`
2. `<本模块目录>/config.json`
"""

import json
import os
import shutil
import subprocess
import tempfile
import threading

try:  # 仅 Windows 需要注册表探测与隐藏控制台窗口
    import winreg
except Exception:  # pragma: no cover
    winreg = None


class PdfConvertError(Exception):
    """转换失败，携带**面向用户**的提示文案（不回传堆栈，SEC-5）。"""


# 转换超时（秒）：实测 LibreOffice 冷启动 15~40s（见下方 profile 说明），
# 20MB 级大文档再翻倍，180s 留足余量。
DEFAULT_TIMEOUT = 180

# 固定 user profile 目录名（放在数据根下，跨转换复用）。
# 实测（25.8.7 / Windows）：每次新建临时 profile 需 37~40s，**复用固定 profile
# 只要 15~18s** —— 差别来自每次重建 profile 时的初始化与字体缓存。
PROFILE_DIRNAME = ".lo-profile"

# 模块级探测缓存 + 串行锁
_CACHE = {"available": None, "path": None, "version": None, "error": None}
_CACHE_LOCK = threading.Lock()
_CONVERT_LOCK = threading.Lock()

# Windows 常见安装路径（探测顺序：注册表优先，其次这里）
_COMMON_PATHS = (
    r"C:\Program Files\LibreOffice\program\soffice.exe",
    r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
    r"C:\Program Files\LibreOffice 7\program\soffice.exe",
    r"C:\Program Files (x86)\LibreOffice 7\program\soffice.exe",
)


# ===================== 配置读取 =====================

def _config_paths():
    """候选配置文件路径（数据根优先，其次模块目录）。"""
    out = []
    try:
        import jztools_data
        out.append(jztools_data.get_data_root_file(
            "plugins", "knowledge-base", "config.json"))
    except Exception:
        pass
    out.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"))
    return out


def _configured_path():
    """读配置里显式指定的 soffice 路径；未配置返回 None。"""
    for p in _config_paths():
        if not os.path.isfile(p):
            continue
        try:
            with open(p, "r", encoding="utf-8") as f:
                conf = json.load(f)
        except Exception:
            continue
        v = None
        if isinstance(conf, dict):
            pdf = conf.get("pdf")
            if isinstance(pdf, dict):
                v = pdf.get("soffice_path")
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


# ===================== 引擎探测 =====================

def _registry_soffice():
    """注册表探测：HKLM\\SOFTWARE\\LibreOffice\\UNO\\InstallPath 的 Path 值。"""
    if winreg is None:
        return None
    for root, sub in ((winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\LibreOffice\UNO\InstallPath"),
                      (winreg.HKEY_LOCAL_MACHINE,
                       r"SOFTWARE\WOW6432Node\LibreOffice\UNO\InstallPath"),
                      (winreg.HKEY_CURRENT_USER, r"SOFTWARE\LibreOffice\UNO\InstallPath")):
        try:
            with winreg.OpenKey(root, sub) as key:
                path, _ = winreg.QueryValueEx(key, "Path")
        except Exception:
            continue
        if path:
            cand = os.path.join(str(path), "soffice.exe")
            if os.path.isfile(cand):
                return cand
            cand2 = os.path.join(str(path), "program", "soffice.exe")
            if os.path.isfile(cand2):
                return cand2
            # Path 值本身可能已指向 program 目录
            if os.path.isfile(str(path)):
                return str(path)
    return None


def _spawn_kwargs():
    """Windows 下隐藏子进程控制台窗口（服务进程不该弹黑框）。"""
    if os.name != "nt":
        return {}
    try:
        info = subprocess.STARTUPINFO()
        info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        info.wShowWindow = 0  # SW_HIDE
        return {"startupinfo": info}
    except Exception:  # pragma: no cover
        return {}


# 版本探测超时（秒）：注意 `soffice --version` 在个别环境（如「管理安装」解包、
# URE 未注册的场景）会**启动后不退出**，因此必须设较短超时并允许失败——
# 版本串仅用于 /status 展示，取不到不影响引擎可用性判断。
VERSION_TIMEOUT = 10


def _version_of(path):
    """读 soffice 版本串（`soffice --version`）；失败返回 None（不影响可用性）。

    已实测：解包版 LibreOffice 的 `--version` 可能挂起 60s+ 不退出，
    故这里超时压到 `VERSION_TIMEOUT` 并把异常一并吞掉（B-4）。
    """
    try:
        r = subprocess.run([path, "--version"], timeout=VERSION_TIMEOUT,
                           capture_output=True, **_spawn_kwargs())
        out = (r.stdout or b"").decode("utf-8", errors="replace").strip()
        if out:
            return out.splitlines()[0][:80]
    except subprocess.TimeoutExpired:
        return None      # 挂起：放弃版本串，引擎本身仍可用
    except Exception:
        pass
    return None


def detect_soffice(refresh=False):
    """探测 LibreOffice 可执行文件（结果模块级缓存）。

    返回 `{"available": bool, "path": str|None, "version": str|None, "error": str|None}`。

    探测顺序：配置 `pdf.soffice_path` → 注册表 → 常见安装路径 → PATH。
    任何异常都被吞掉转成 `available=False`，绝不让调用方 500（B-4）。
    """
    with _CACHE_LOCK:
        if not refresh and _CACHE["available"] is not None:
            return dict(_CACHE)

        found, err = None, None
        # 1) 配置显式指定
        cfg = _configured_path()
        if cfg:
            if os.path.isfile(cfg):
                found = cfg
            else:
                err = f"已配置 soffice 路径但文件不存在：{cfg}"
        # 2) 注册表
        if found is None:
            found = _registry_soffice()
        # 3) 常见安装路径
        if found is None:
            for p in _COMMON_PATHS:
                if os.path.isfile(p):
                    found = p
                    break
        # 4) PATH
        if found is None:
            for name in ("soffice", "soffice.exe", "libreoffice"):
                p = shutil.which(name)
                if p:
                    found = p
                    break

        if found is None:
            _CACHE.update({"available": False, "path": None, "version": None,
                           "error": err or "未检测到 LibreOffice"})
        else:
            _CACHE.update({"available": True, "path": found,
                           "version": _version_of(found), "error": None})
        return dict(_CACHE)


def engine_status(refresh=False):
    """对外封装（供 /status 自检，B-4）；`refresh=True` 强制重新探测。"""
    try:
        return detect_soffice(refresh=refresh)
    except Exception as e:  # pragma: no cover
        return {"available": False, "path": None, "version": None, "error": str(e)}


# ===================== 转换 =====================

def _profile_dir():
    """固定 user profile 目录（跨转换复用，显著提速）。

    优先放数据根（持久、运维可清理），数据根不可用时回退系统临时目录。
    """
    try:
        import jztools_data
        p = jztools_data.get_data_root_file("plugins", "knowledge-base", PROFILE_DIRNAME)
        try:
            os.makedirs(p, exist_ok=True)
            return p
        except OSError:
            pass
    except Exception:
        pass
    p = os.path.join(tempfile.gettempdir(), "jztools_kb_" + PROFILE_DIRNAME)
    try:
        os.makedirs(p, exist_ok=True)
    except OSError:
        pass
    return p


def _run_once(soffice, src_path, work, profile, timeout):
    """跑一次转换，返回 (returncode, produced_path|None, stderr_tail)。"""
    profile_uri = "file:///" + os.path.abspath(profile).replace("\\", "/").lstrip("/")
    cmd = [soffice, "--headless", "--norestore", "--nolockcheck", "--nodefault",
           "-env:UserInstallation=" + profile_uri,
           "--convert-to", "pdf", "--outdir", work,
           os.path.abspath(src_path)]
    try:
        proc = subprocess.run(cmd, timeout=timeout,
                              capture_output=True, **_spawn_kwargs())
        rc = proc.returncode
        err = (proc.stderr or b"").decode("utf-8", "replace")[-300:]
    except subprocess.TimeoutExpired:
        return "timeout", None, ""
    except FileNotFoundError:
        return "missing", None, ""
    except Exception:
        return "spawn", None, ""

    base = os.path.splitext(os.path.basename(src_path))[0]
    produced = os.path.join(work, base + ".pdf")
    if not os.path.isfile(produced):
        pdfs = [f for f in os.listdir(work) if f.lower().endswith(".pdf")]
        produced = os.path.join(work, pdfs[0]) if pdfs else None
    return rc, produced, err


def convert_to_pdf(src_path, dst_pdf, timeout=DEFAULT_TIMEOUT):
    """把 `src_path`（doc/docx/xls/xlsx）转换为 PDF 并**原子落盘**到 `dst_pdf`。

    成功返回 None；失败抛 `PdfConvertError`（文案可直接展示给用户）。

    要点：
    - **固定 user profile**（`-env:UserInstallation`，复用 `<数据根>/.lo-profile`）：
      避开 soffice 全局单实例冲突，同时省掉每次重建 profile 的初始化开销
      （实测 37~40s → 15~18s）；profile 损坏时自动重置重试一次；
    - 工作目录建在 `dst_pdf` 同目录下 → `os.replace` 同分区，原子且可跨盘符规避；
    - 参数列表化调用，绝不 `shell=True`（SEC-10）；
    - 全程持模块级锁串行（soffice CPU 开销大，并发反而更慢）；
    - 无论成功失败，`finally` 清理临时工作目录。
    """
    info = detect_soffice()
    if not info.get("available"):
        raise PdfConvertError("服务器未检测到 LibreOffice，无法生成 PDF 预览")
    soffice = info.get("path")
    if not soffice or not os.path.isfile(soffice):
        raise PdfConvertError("服务器未检测到 LibreOffice，无法生成 PDF 预览")
    if not src_path or not os.path.isfile(src_path):
        raise PdfConvertError("待转换的源文件不存在")

    with _CONVERT_LOCK:
        dst_dir = os.path.dirname(os.path.abspath(dst_pdf))
        try:
            os.makedirs(dst_dir, exist_ok=True)
        except OSError as e:
            raise PdfConvertError("PDF 输出目录不可写，请联系管理员") from e
        # 建在目标同目录：保证 os.replace 同分区（Windows 跨盘符 replace 会失败）
        work = tempfile.mkdtemp(prefix=".pdfwork-", dir=dst_dir)
        try:
            # 固定 profile（复用提速）；建不出来时退回临时 profile（保证可用）
            profile = _profile_dir()
            try:
                os.makedirs(profile, exist_ok=True)
            except OSError:
                profile = os.path.join(work, "profile")
                os.makedirs(profile, exist_ok=True)
            fixed = profile != os.path.join(work, "profile")

            rc, produced, _err = _run_once(soffice, src_path, work, profile, timeout)
            # 固定 profile 可能因异常退出留下脏状态（升级/强杀），
            # 失败时重置 profile 重试一次，避免"一次损坏、永久失败"。
            if (rc != 0 or produced is None) and fixed:
                shutil.rmtree(profile, ignore_errors=True)
                os.makedirs(profile, exist_ok=True)
                rc, produced, _err = _run_once(soffice, src_path, work, profile, timeout)

            if rc == "timeout":
                raise PdfConvertError("PDF 生成超时（超过 3 分钟），请稍后重试")
            if rc in ("missing", "spawn"):
                raise PdfConvertError("PDF 生成失败（无法启动转换程序）")
            if rc != 0 or not produced:
                # rc==0 但没产物：源文件损坏或格式不支持
                raise PdfConvertError("PDF 生成失败，请检查文档是否损坏或另存后重试")
            size = os.path.getsize(produced)
            if size <= 0:
                raise PdfConvertError("PDF 生成失败（产物为空）")
            try:
                with open(produced, "rb") as f:
                    head = f.read(5)
            except OSError as e:
                raise PdfConvertError("PDF 产物不可读，请稍后重试") from e
            if head != b"%PDF-":
                raise PdfConvertError("PDF 生成失败，请检查文档是否损坏或另存后重试")

            try:
                os.replace(produced, dst_pdf)   # 同分区原子替换
            except OSError as e:
                raise PdfConvertError("PDF 保存失败，请稍后重试") from e
            return None
        finally:
            shutil.rmtree(work, ignore_errors=True)
