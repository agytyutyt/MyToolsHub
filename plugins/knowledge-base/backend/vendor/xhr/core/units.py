"""尺寸换算 —— Excel 单位 → CSS 像素。对应 TS 版 ``packages/core/src/units.ts``。"""
from __future__ import annotations

import math
from typing import Sequence

#: 默认字体的 '0' 字符宽度（px）。Calibri 11 @96DPI = 7px；中文默认字体也近似 7。
DEFAULT_MDW = 7


def col_width_to_px(width: float, mdw: int = DEFAULT_MDW) -> int:
    """Excel 列宽（字符数）→ 像素。+5 为 Excel 左右内边距（2+2）与 1px 网格留白。"""
    if not math.isfinite(width) or width <= 0:
        return 0
    return round(width * mdw + 5)


def px_to_col_width(px: float, mdw: int = DEFAULT_MDW) -> float:
    return max(0.0, (px - 5) / mdw)


def row_height_to_px(pt: float) -> int:
    """Excel 行高（磅 pt）→ 像素。1pt = 1/72 inch，CSS 96dpi 下 = 4/3 px。"""
    if not math.isfinite(pt) or pt <= 0:
        return 0
    return round(pt * 4 / 3)


def pt_to_px(pt: float) -> float:
    return pt * 4 / 3


def px_to_pt(px: float) -> float:
    return px * 3 / 4


#: Excel 默认行高 15pt = 20px；默认列宽 8.43 字符 ≈ 64px
DEFAULT_ROW_HEIGHT_PX = row_height_to_px(15)
DEFAULT_COL_WIDTH_PX = col_width_to_px(8.43)


def emu_to_px(emu: float) -> int:
    """EMU → px（1 px = 9525 EMU）。"""
    return round(emu / 9525)


def build_offsets(sizes: Sequence[float]) -> list[float]:
    """前缀和：out[i] = 前 i 项的累计值（用于冻结窗格的 sticky 偏移）。"""
    out: list[float] = [0.0]
    for s in sizes:
        out.append(out[-1] + (s or 0))
    return out
