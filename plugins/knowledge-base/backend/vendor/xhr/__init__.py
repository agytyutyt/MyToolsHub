"""xhr —— XLSX / XLS → HTML 重绘渲染引擎（Python 实现）。

对应 TS 版的根包 ``src/index.ts``：把用户上传的 .xlsx / .xls 渲染成 HTML 预览，
继承原文档样式，只读、不可编辑。

用法::

    from xhr import convert

    result = convert(b"……xlsx 字节……", mode="document", with_runtime=True)
    print(result.html)
"""
from typing import Optional  # 本地补丁：见 vendor/README.md（上游缺该导入，见文件末尾说明）

from .core import *  # noqa: F401,F403
from .ingest import IngestResult, ingest, sniff  # noqa: F401
from .style import *  # noqa: F401,F403
from .layout import *  # noqa: F401,F403
from .parser_xlsx import ParseInput, ParseOptions, parse_buffer, parse_to_model  # noqa: F401
from .renderer import (  # noqa: F401
    DEFAULT_RENDER_OPTIONS,
    RenderOptions,
    RenderResult,
    render_one_sheet,
    render_workbook,
    runtime_script,
)
from .normalize import NormalizeOptions, NormalizeResult, normalize_xls_to_model  # noqa: F401

from .core import XhrError, to_xhr_error, WorkbookModel  # noqa: F401
from .renderer.table import RenderContext, cell_content, cell_inner  # noqa: F401

__version__ = "0.1.0"

__all__ = [
    "convert",
    "ConvertOptions",
    "ConvertResult",
    "parse_buffer",
    "parse_to_model",
    "render_workbook",
    "render_one_sheet",
    "normalize_xls_to_model",
    "ingest",
    "sniff",
    "XhrError",
    "to_xhr_error",
    "WorkbookModel",
    "RenderOptions",
    "DEFAULT_RENDER_OPTIONS",
    "runtime_script",
    "__version__",
]


class ConvertOptions(RenderOptions):
    """一站式 API 选项 = 渲染选项 + 解析选项。"""

    def __init__(self, **kwargs) -> None:
        parse = kwargs.pop("parse", None)
        force_fallback = kwargs.pop("force_fallback", False)
        super().__init__(**kwargs)
        self.parse: ParseOptions = parse or ParseOptions()
        self.force_fallback: bool = force_fallback


class ConvertResult:
    """convert() 的返回值。"""

    __slots__ = ("html", "model", "cell_count", "class_count", "truncated")

    def __init__(self, html: str, model: WorkbookModel, cell_count: int, class_count: int, truncated: bool) -> None:
        self.html = html
        self.model = model
        self.cell_count = cell_count
        self.class_count = class_count
        self.truncated = truncated

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"ConvertResult(cell_count={self.cell_count}, class_count={self.class_count}, "
            f"truncated={self.truncated})"
        )


def convert(data: bytes, options: Optional[ConvertOptions] = None) -> ConvertResult:
    """一站式 API：字节流 → HTML。

    支持 .xlsx / .xlsm / .xls：
      · .xlsx 走 parser_xlsx 全链路
      · .xls 自动选择通道（LibreOffice 归一化 → 不可用则 xlrd 兜底）

    @example
        result = convert(buf, ConvertOptions(mode="document"))
        print(result.html)
    """
    opts = options or ConvertOptions()
    data = bytes(data)
    ing = ingest(data)

    if ing.format == "xls":
        result = normalize_xls_to_model(
            data,
            NormalizeOptions(
                force_fallback=opts.force_fallback,
                **{
                    k: getattr(opts.parse, k)
                    for k in ("max_cells_per_sheet", "max_cells_total", "inline_media", "max_image_bytes")
                },
            ),
        )
        if result.via == "xlrd" and not any("兜底" in w for w in result.model.meta.warnings):
            result.model.meta.warnings.insert(0, FALLBACK_HINT)
        rendered = render_workbook(result.model, opts)
        return ConvertResult(rendered.html, result.model, rendered.cell_count, rendered.class_count, rendered.truncated)

    if not ing.files:
        raise XhrError("UNSUPPORTED_FORMAT", f"format={ing.format}（支持 .xlsx / .xlsm / .xls）")

    model = parse_to_model(ParseInput(files=ing.files, buffer=data), opts.parse)
    rendered = render_workbook(model, opts)
    return ConvertResult(rendered.html, model, rendered.cell_count, rendered.class_count, rendered.truncated)


FALLBACK_HINT = ".xls 兜底通道渲染（样式保真度有限）。建议安装 LibreOffice 或另存为 .xlsx 获得完整保真度。"
