"""normalize —— .xls 归一化编排层（M2）。

对应 TS 版 ``packages/normalize/src/index.ts``。

两条通道：
  1. LibreOffice 归一化（T-301）：.xls → .xlsx → 复用 parser_xlsx 全链路（保真度最高）
  2. xlrd 兜底（T-302）：无 LibreOffice / 归一化失败时直读，内容 100% 保留、样式降级
"""
from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from typing import Optional, Union

from ..core import XhrError, to_xhr_error
from ..ingest import unzip_guarded
from ..parser_xlsx import ParseOptions, parse_to_model, ParseInput
from .fallback import FALLBACK_WARNING, xlrd_to_model
from .libreoffice import convert_to_xlsx, convert_to_xlsx_guarded, find_soffice

__all__ = [
    "NormalizeOptions",
    "NormalizeResult",
    "normalize_xls_to_model",
    "find_soffice",
    "convert_to_xlsx",
    "convert_to_xlsx_guarded",
    "xlrd_to_model",
    "FALLBACK_WARNING",
]


@dataclass
class NormalizeOptions(ParseOptions):
    #: 强制跳过 LibreOffice，直接走 xlrd 兜底
    force_fallback: bool = False
    #: LibreOffice 转换超时毫秒（默认 60s）
    soffice_timeout_ms: int = 60_000
    #: 显式 soffice 路径（也可用环境变量 XHR_SOFFICE）
    soffice_path: Optional[str] = None


@dataclass
class NormalizeResult:
    model: WorkbookModel
    #: 走的哪条通道：'libreoffice'（完整保真）| 'xlrd'（内容 100%、样式降级）
    via: str


def _zip_input_of(xlsx_bytes: bytes) -> ParseInput:
    files, _stats, _skipped = unzip_guarded(xlsx_bytes)
    if "xl/workbook.xml" not in files:
        raise XhrError("CORRUPTED", "LibreOffice 归一化产物不是有效的 xlsx")
    return ParseInput(files=files, buffer=xlsx_bytes)


def normalize_xls_to_model(
    data: Union[bytes, bytearray, memoryview],
    options: Optional[NormalizeOptions] = None,
) -> NormalizeResult:
    """.xls → WorkbookModel 的统一入口。

    通道选择：
      1. LibreOffice 可用 → 归一化成 xlsx 后走 parse_to_model（完整保真链路）
      2. 不可用或归一化失败（含 TIMEOUT / CORRUPTED）→ xlrd 兜底

    ⚠️ 归一化失败后兜底而不是直接报错：用户拿到低保真内容好过拿到错误页。
    """
    opts = options or NormalizeOptions()
    data = bytes(data)

    if not opts.force_fallback and find_soffice(opts.soffice_path):
        out_dir = tempfile.mkdtemp(prefix="xhr-xls-")
        try:
            in_file = os.path.join(out_dir, "input.xls")
            with open(in_file, "wb") as f:
                f.write(data)
            xlsx_path = convert_to_xlsx_guarded(
                in_file, out_dir, soffice_path=opts.soffice_path, timeout_ms=opts.soffice_timeout_ms
            )
            with open(xlsx_path, "rb") as f:
                xlsx_bytes = f.read()
            model = parse_to_model(_zip_input_of(xlsx_bytes), opts)
            model.meta.normalized_from = "xls"
            return NormalizeResult(model=model, via="libreoffice")
        except Exception:  # noqa: BLE001 —— 归一化失败 → 落入兜底通道
            pass
        finally:
            import shutil

            shutil.rmtree(out_dir, ignore_errors=True)

    try:
        return NormalizeResult(model=xlrd_to_model(data), via="xlrd")
    except Exception as e:  # noqa: BLE001
        raise to_xhr_error(e) from e
