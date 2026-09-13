"""冻结窗格 —— 对应 TS 版 ``packages/layout/src/freeze.ts``。

方案 A（本项目采用）：CSS position:sticky
  1. 前 ySplit 行的 <td> 加 xhr-freeze-row + top:<累计行高>px
  2. 前 xSplit 列的 <td> 加 xhr-freeze-col + left:<累计列宽>px
  3. 交叉区 z-index 更高
  4. ⚠️ sticky 单元格必须有不透明背景，否则滚动时下层文字会透上来
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional

from ..core import FreezeModel, SheetViewModel


@dataclass
class FreezePlan:
    x_split: int = 0
    y_split: int = 0
    row_tops: List[float] = field(default_factory=list)
    col_lefts: List[float] = field(default_factory=list)
    #: 是否存在冻结
    active: bool = False


def plan_freeze(view: SheetViewModel, row_offsets: List[float], col_offsets: List[float]) -> FreezePlan:
    """由视图信息 + 尺寸前缀和，算出每个冻结单元格的 sticky 偏移。"""
    f = view.freeze
    if not f or (f.x_split <= 0 and f.y_split <= 0):
        return FreezePlan()
    y_split = max(0, f.y_split)
    x_split = max(0, f.x_split)

    row_tops = [(row_offsets[r] if r < len(row_offsets) else 0) for r in range(y_split)]
    col_lefts = [(col_offsets[c] if c < len(col_offsets) else 0) for c in range(x_split)]
    return FreezePlan(x_split=x_split, y_split=y_split, row_tops=row_tops, col_lefts=col_lefts, active=True)


_A1_RE = re.compile(r"^\$?([A-Za-z]+)\$?([0-9]+)$")


def freeze_from_view(view: Optional[dict]) -> Optional[FreezeModel]:
    """从解析库给出的视图对象提取冻结信息（与 TS 版同名函数对齐）。

    自研解析: <pane xSplit ySplit topLeftCell state="frozen"/>
    """
    if not view:
        return None

    def num_or_0(v: object) -> int:
        return int(v) if isinstance(v, (int, float)) and v > 0 else 0

    x_split = num_or_0(view.get("xSplit"))
    y_split = num_or_0(view.get("ySplit"))
    if x_split == 0 and y_split == 0:
        return None

    top_left = view.get("topLeftCell")
    top_left_row, top_left_col = y_split, x_split
    if isinstance(top_left, str) and top_left.strip():
        m = _A1_RE.match(top_left.strip())
        if m:
            col = 0
            for ch in m.group(1).upper():
                col = col * 26 + (ord(ch) - 64)
            top_left_row = int(m.group(2)) - 1
            top_left_col = col - 1
    return FreezeModel(x_split=x_split, y_split=y_split, top_left_row=top_left_row, top_left_col=top_left_col)
