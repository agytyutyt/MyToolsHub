"""dhr —— DOC / DOCX → HTML 重绘渲染引擎（Python 实现）。

对应《设计文档》03-设计文档-DOC-DOCX-HTML渲染引擎.md 的 Python 落地，
与 xhr（Excel 引擎）共享安全基建（ingest 限额、LibreOffice 归一化、
图片魔数校验、超链接白名单）。

用法::

    from dhr import convert, ConvertOptions

    result = convert(open("a.docx", "rb").read(), ConvertOptions(mode="flow"))
    print(result.html)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from xhr.core import XhrError, to_xhr_error

from .ingest import ingest_doc
from .model import DocumentModel
from .renderer import RenderOptions, RenderResult, render_document


@dataclass
class ParseOptions:
    #: 'final' 接受全部修订 | 'original' 拒绝全部 | 'markup' 带标记
    revision_mode: str = "final"


class ConvertOptions(RenderOptions):
    """一站式选项 = 渲染选项 + 解析选项。"""

    def __init__(self, **kwargs) -> None:
        parse = kwargs.pop("parse", None)
        super().__init__(**kwargs)
        self.parse: ParseOptions = parse or ParseOptions()


class ConvertResult:
    __slots__ = ("html", "model", "class_count")

    def __init__(self, html: str, model: DocumentModel, class_count: int) -> None:
        self.html = html
        self.model = model
        self.class_count = class_count

    @property
    def cell_count(self) -> int:
        return self.model.meta.get("stats", {}).get("paragraphCount", 0)


def parse_to_model(data: bytes, options: Optional[ParseOptions] = None) -> DocumentModel:
    """docx 字节 → DocumentModel（不含 .doc 归一化）。"""
    from .parser import parse_to_model as _parse

    opts = options or ParseOptions()
    ing = ingest_doc(data)
    if ing["format"] not in ("docx", "docm", "dotx"):
        raise XhrError("UNSUPPORTED_FORMAT", f"parser 只处理 docx 容器，收到 {ing['format']}")
    return _parse(ing["files"], opts)


def normalize_doc(data: bytes, soffice_path: Optional[str] = None) -> DocumentModel:
    """`.doc` → DocumentModel（LibreOffice 归一化通道，T-901）。"""
    from .normalize import docx_files_of, normalize_doc_to_docx

    docx_bytes = normalize_doc_to_docx(data, soffice_path=soffice_path)
    model = parse_to_model(docx_bytes, ParseOptions())
    model.meta["normalizedFrom"] = "doc"
    model.meta["sourceFormat"] = "doc"
    if not model.meta.get("warnings"):
        model.meta["warnings"] = []
    model.meta["warnings"].insert(0, ".doc 经 LibreOffice 归一化渲染；分页位置可能不精确，建议使用流式模式")
    return model


def convert(data: bytes, options: Optional[ConvertOptions] = None) -> ConvertResult:
    """一站式 API：字节流 → HTML。

    支持 .docx / .docm / .dotx / .doc（自动归一化）/ .rtf（报 UNSUPPORTED_FORMAT 提示）。
    """
    opts = options or ConvertOptions()
    data = bytes(data)
    ing = ingest_doc(data)

    if ing["format"] == "doc":
        model = normalize_doc(data)
    elif ing["format"] == "rtf":
        raise XhrError("UNSUPPORTED_FORMAT", "RTF 暂不支持，请先用 Word/WPS 另存为 .docx")
    else:
        parse_opts = opts.parse if isinstance(opts.parse, ParseOptions) else ParseOptions(revision_mode=opts.revision_mode)
        model = parse_to_model(data, parse_opts)

    rendered = render_document(model, opts)
    return ConvertResult(rendered.html, model, rendered.class_count)


__version__ = "0.1.0"

__all__ = [
    "convert",
    "ConvertOptions",
    "ConvertResult",
    "ParseOptions",
    "parse_to_model",
    "normalize_doc",
    "RenderOptions",
    "render_document",
    "XhrError",
    "to_xhr_error",
    "__version__",
]
