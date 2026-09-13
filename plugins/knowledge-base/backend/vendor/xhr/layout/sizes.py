"""列宽 / 行高 / 隐藏 —— 对应 TS 版 ``packages/layout/src/sizes.ts``。

输出"覆盖 usedRange 的完整数组"：col_widths[c] / row_heights[r] 必有值，
因为 <colgroup> 必须输出所有列，少一列后面的列宽全部错位。
"""
from __future__ import annotations

from typing import List, Optional

from ..core import (
    DEFAULT_COL_WIDTH_PX,
    DEFAULT_ROW_HEIGHT_PX,
    ColModel,
    RowModel,
    SheetModel,
    build_offsets,
)


class SheetMetrics:
    """工作表尺寸度量。"""

    __slots__ = (
        "col_widths", "row_heights", "col_hidden", "row_hidden",
        "col_offsets", "row_offsets", "total_width", "total_height",
    )

    def __init__(
        self,
        col_widths: List[int],
        row_heights: List[int],
        col_hidden: List[bool],
        row_hidden: List[bool],
    ) -> None:
        self.col_widths = col_widths
        self.row_heights = row_heights
        self.col_hidden = col_hidden
        self.row_hidden = row_hidden
        #: 前缀和：col_offsets[c] = 前 c 列的累计宽度（冻结窗格 sticky 偏移用）
        self.col_offsets = build_offsets(col_widths)
        self.row_offsets = build_offsets(row_heights)
        self.total_width = self.col_offsets[len(col_widths)] if col_widths else 0
        self.total_height = self.row_offsets[len(row_heights)] if row_heights else 0


def compute_metrics(sheet: SheetModel, col_count: int, row_count: int) -> SheetMetrics:
    """计算工作表的尺寸度量。

    @param col_count 实际要输出的列数（= usedRange 宽度，不超过限额）
    @param row_count 实际要输出的行数
    """
    col_widths = [sheet.default_col_width_px or DEFAULT_COL_WIDTH_PX] * col_count
    col_hidden = [False] * col_count
    row_heights = [sheet.default_row_height_px or DEFAULT_ROW_HEIGHT_PX] * row_count
    row_hidden = [False] * row_count

    for col in sheet.cols or []:
        if col.index < 0 or col.index >= col_count:
            continue
        if col.width_px > 0:
            col_widths[col.index] = col.width_px
        col_hidden[col.index] = bool(col.hidden)

    for row in sheet.rows or []:
        if row.index < 0 or row.index >= row_count:
            continue
        if row.height_px > 0:
            row_heights[row.index] = row.height_px
        row_hidden[row.index] = bool(row.hidden)

    return SheetMetrics(col_widths, row_heights, col_hidden, row_hidden)


def is_empty_row(row: Optional[RowModel]) -> bool:
    """行是否"完全空白"（既无文本也无富文本）。

    ⚠️ 仅用于裁剪尾部空行；不要用它跳过中间行——
       中间行可能只有背景色/边框而无文本，跳掉会破坏表格结构。
    """
    if row is None:
        return True
    if not row.cells:
        return True
    for cell in row.cells.values():
        if cell.w != "" or (cell.rich_text or []):
            return False
    return True


def ensure_col(cols: List[ColModel], index: int) -> ColModel:
    """取列对象（按需创建）。"""
    for c in cols:
        if c.index == index:
            return c
    c = ColModel(index=index)
    cols.append(c)
    return c
