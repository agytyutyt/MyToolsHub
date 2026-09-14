r"""知识库 —— Office(docx/doc/xlsx/xls) → 只读 HTML 预览渲染（xhr/dhr 引擎适配层）。

背景（《插件库优化方案》阶段 8，替代原方案）
--------
原方案两套管线全部废弃：
- Word：LibreOffice → PDF（`pdf_convert.py`，已删除）+ pdf.js 按打印分页渲染；
- Excel：`xlsx_render.py`（已删除）自实现的手绘 HTML 表格。

现改为上游 `D:\TestWorkSpace\xlsx-html-preview` 的**双引擎**（原样 vendor 在
`vendor/` 下，零改动拷贝，核心链路纯标准库、零第三方依赖）：
- `xhr`：.xlsx/.xlsm 直读；.xls 自动选通道（LibreOffice 归一化 → xlrd 兜底，
  内容 100% 保留、样式降级并在 warnings 提示）；
- `dhr`：.docx/.docm 直读；.doc 经 LibreOffice 归一化（不可用时明确报错）。
两引擎自带 IR 中间层、格式化显示文本（不输出原始序列值）、统一错误码
（用户可读文案、不含堆栈，SEC-5 同口径）、安全白名单（URL 协议/字体/图片魔数）。

调用方式
--------
- 引擎包不在包上下文内（dhr 顶层 `from xhr.core import ...`），因此把
  `vendor/` 插到 `sys.path` 后按顶层包导入（上游 demo 同款做法）；
- soffice 路径（三档优先级）：插件 `config.json` 的 `office.soffice_path`（新）或
  `pdf.soffice_path`（兼容旧配置）→ 进程环境变量 `XHR_SOFFICE` →
  随包分发的便携副本 `<程序目录>/runtime/libreoffice/program/soffice.exe`。
  最终写入环境变量 `XHR_SOFFICE`（两引擎的 find_soffice 均在调用时读取该变量）；
  第三档使离线部署包**无需目标机安装 LibreOffice、无需改配置**即可启用高保真通道；
- 渲染亚秒级（50 页 Word 实测约 0.25s），**按需同步渲染**，不再走
  pdf_status/html_status 异步状态机；模块级锁串行防大文件并发挤内存；
- 失败抛 `OfficeRenderError`（文案可直接展示给用户），调用方（routes.py）
  让前端回退 mammoth / SheetJS 降级渲染，上传/阅读/下载不受影响（B-4）。
"""

import glob
import json
import os
import sys
import threading

# ---- vendor 引擎导入（必须在 import xhr/dhr 之前完成 sys.path 注入）----
_VENDOR_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor")
if _VENDOR_DIR not in sys.path:
    sys.path.insert(0, _VENDOR_DIR)

try:
    import dhr as _dhr  # noqa: E402  （dhr 内部依赖顶层 xhr，一并可用）
    import xhr as _xhr  # noqa: E402
except Exception as _e:  # pragma: no cover —— 引擎缺失时插件其余功能不受影响（B-4）
    _dhr = None
    _xhr = None
    _IMPORT_ERROR = str(_e)
else:
    _IMPORT_ERROR = None

try:
    from xhr.normalize.libreoffice import find_soffice as _find_soffice  # noqa: E402
except Exception:  # pragma: no cover
    _find_soffice = None


class OfficeRenderError(Exception):
    """渲染失败，携带**面向用户**的提示文案（不回传堆栈/路径，SEC-5）。"""


# 渲染串行锁：引擎为纯 CPU 解析 + 偶发 LibreOffice 子进程，串行防大文件并发挤内存
_RENDER_LOCK = threading.Lock()

# Excel 渲染单元格上限（xhr 默认 20 万格会产出数十 MB HTML，压到与旧方案同量级；
# 超出由引擎截断并在预览顶部显示提示条）
MAX_CELLS = 120_000

# CSS 前缀：避免与宿主页面（.paper/.sheet-table 等）样式互相污染
WORD_CSS_PREFIX = "kbdoc"
SHEET_CSS_PREFIX = "kbsheet"


# ===================== 配置与探测 =====================

def _config_paths():
    """候选配置文件路径（数据根优先，其次模块目录；口径同旧 pdf_convert）。"""
    out = []
    try:
        import jztools_data
        out.append(jztools_data.get_data_root_file(
            "plugins", "knowledge-base", "config.json"))
    except Exception:
        pass
    out.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"))
    return out


def _configured_soffice():
    """读配置里显式指定的 soffice 路径（office.soffice_path 新键优先，
    兼容旧 pdf.soffice_path）；未配置返回 None。"""
    for p in _config_paths():
        if not os.path.isfile(p):
            continue
        try:
            with open(p, "r", encoding="utf-8") as f:
                conf = json.load(f)
        except Exception:
            continue
        if not isinstance(conf, dict):
            continue
        for section in ("office", "pdf"):
            node = conf.get(section)
            if isinstance(node, dict):
                v = node.get("soffice_path")
                if isinstance(v, str) and v.strip():
                    return v.strip()
    return None


def _program_dir():
    """部署根目录（程序目录）：打包运行时为 exe 同层，源码运行时为仓库根。

    与 app.py::BASE_DIR 口径一致，用于定位随包分发的 runtime/ 目录。
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    # 源码运行：<仓库根>/plugins/knowledge-base/backend/office_render.py
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.abspath(os.path.join(here, os.pardir, os.pardir, os.pardir))


#: 随包分发的便携 LibreOffice 候选路径（相对部署根目录）。
#: 离线部署包把 LibreOffice MSI 以「管理安装」方式解包到 runtime/libreoffice/
#: （不写注册表、无需管理员），应用在此自动探测 soffice.exe，实现**零配置**可用。
#: 多列几个候选是为了兼容 msiexec /a 与 7z 两种解包方式、以及人工放在别处的场景。
_BUNDLED_SOFFICE_CANDIDATES = (
    ("runtime", "libreoffice", "program", "soffice.exe"),
    ("runtime", "libreoffice", "LibreOffice", "program", "soffice.exe"),
    ("libreoffice", "program", "soffice.exe"),
)


def _bundled_soffice():
    """随包分发的便携 soffice 路径；不存在返回 None。"""
    base = _program_dir()
    for parts in _BUNDLED_SOFFICE_CANDIDATES:
        p = os.path.join(base, *parts)
        if os.path.isfile(p):
            return p
    # 兜底：不同解包方式（msiexec /a、7z）与不同版本会多套一层目录，
    # 在 runtime/libreoffice 下有限深度搜索 program/soffice.exe。
    root = os.path.join(base, "runtime", "libreoffice")
    if os.path.isdir(root):
        for depth in range(1, 3):
            for hit in sorted(glob.glob(os.path.join(root, *(["*"] * depth),
                                                     "program", "soffice.exe"))):
                if os.path.isfile(hit):
                    return hit
    return None


def _apply_soffice_env():
    """把 soffice 路径注入 XHR_SOFFICE（引擎每次调用都读环境变量，
    改配置无需重启）。返回生效路径或 None。

    优先级：配置显式指定 > 进程环境变量 > 随包分发的便携 LibreOffice。
    最后一级是离线部署的关键：目标机没装 LibreOffice 时，直接使用运行时
    目录里解包好的便携副本，无需改配置、无需注册表。
    """
    explicit = _configured_soffice()
    if explicit and os.path.isfile(explicit):
        # 显式配置优先级最高（setdefault 可能被进程环境里的旧值占位）
        os.environ["XHR_SOFFICE"] = explicit
        return explicit
    env = os.environ.get("XHR_SOFFICE") or None
    if env:
        return env
    bundled = _bundled_soffice()
    if bundled:
        os.environ["XHR_SOFFICE"] = bundled
        return bundled
    return None


def soffice_available():
    """LibreOffice 是否可用（仅 .doc 归一化 / .xls 高保真通道需要）。"""
    if _find_soffice is None:
        return False
    try:
        return bool(_find_soffice(_apply_soffice_env()))
    except Exception:
        return False


def availability():
    """依赖自检（B-4）：引擎包可导入即可用。"""
    if _xhr is None or _dhr is None:
        return {"available": False, "soffice": None,
                "error": "渲染引擎未加载（vendor 缺失或导入失败：%s）" % _IMPORT_ERROR}
    return {"available": True, "soffice": soffice_available() or None, "error": None}


# ===================== 渲染 =====================

def _raise_from_xhr(e):
    """XhrError → OfficeRenderError（message 已是用户可读文案）。"""
    msg = str(e)
    if getattr(e, "code", "") == "TIMEOUT":
        msg = "预览转换超时，文件可能过于复杂，请稍后重试"
    raise OfficeRenderError(msg) from e


def render_word(data):
    """Word（docx/docm/doc 字节）→ {"kind","html","warnings"}。

    - mode="flow"：流式连续排版（无打印分页，阅读动线与网页一致；.doc 归一化
      场景上游也推荐 flow）；
    - output="fragment"：`<style>` + `<div class="kbdoc">` 片段，便于嵌入宿主容器；
    - 图片内联 base64（离线内网无外链可图床）。
    """
    if _dhr is None:
        raise OfficeRenderError("服务器渲染引擎未加载，无法生成 Word 预览")
    if not data:
        raise OfficeRenderError("文件内容为空，无法生成预览")
    opts = _dhr.ConvertOptions(
        mode="flow",
        output="fragment",
        css_prefix=WORD_CSS_PREFIX,
        media_mode="base64",
    )
    _apply_soffice_env()
    try:
        with _RENDER_LOCK:
            result = _dhr.convert(bytes(data), opts)
    except Exception as e:
        _raise_from_xhr(e)
    meta = getattr(result.model, "meta", None)
    warnings = list(getattr(meta, "warnings", None) or [])
    return {"kind": "word", "html": result.html, "warnings": warnings[:5],
            "truncated": False}


def render_sheet(data):
    """Excel（xlsx/xlsm/xls 字节）→ {"kind","html","warnings","truncated"}。

    - mode="fragment"：`<style>` + `<div class="kbsheet">`（含页签与多个
      `.kbsheet-sheet[hidden]`，页签切换由前端接线）；
    - .xls 由引擎自动选通道（LibreOffice 归一化 → xlrd 兜底，降级进 warnings）。
    """
    if _xhr is None:
        raise OfficeRenderError("服务器渲染引擎未加载，无法生成表格预览")
    if not data:
        raise OfficeRenderError("文件内容为空，无法生成预览")
    opts = _xhr.ConvertOptions(mode="fragment", css_prefix=SHEET_CSS_PREFIX,
                               max_cells=MAX_CELLS)
    _apply_soffice_env()
    try:
        with _RENDER_LOCK:
            result = _xhr.convert(bytes(data), opts)
    except Exception as e:
        _raise_from_xhr(e)
    meta = getattr(result.model, "meta", None)
    warnings = list(getattr(meta, "warnings", None) or [])
    return {"kind": "sheet", "html": result.html, "warnings": warnings[:5],
            "truncated": bool(result.truncated)}
