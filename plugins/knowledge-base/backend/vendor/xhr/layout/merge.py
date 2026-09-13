"""合并单元格索引 —— 对应 TS 版 ``packages/layout/src/merge.ts``。

渲染时：
  · 命中主单元格 (r0,c0) → 输出 <td rowspan colspan>
  · 命中被覆盖区 → 完全跳过，不输出 <td>（输出空 td 会让整行错位）

⚠️ 第三方导出器（POI / ClosedXML / 某些在线表格）常产生越界或重叠的合并区域，
   必须先做 clamp + 去重，否则渲染会给出错位的表格。
"""
from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Set, Tuple

from ..core import EXCEL_MAX_COL, EXCEL_MAX_ROW, Rect

#: 合并区面积上限（防止畸形文件构造海量覆盖坐标）
MAX_COVERED_CELLS = 2_000_000


def normalize_merges(merges: Iterable[Rect]) -> List[Rect]:
    """规整合并区：clamp 到合法范围、去掉退化（单格）与越界项、按面积降序。"""
    out: List[Rect] = []
    for m in merges:
        r0 = max(0, min(m.r0, m.r1))
        r1 = min(EXCEL_MAX_ROW - 1, max(m.r0, m.r1))
        c0 = max(0, min(m.c0, m.c1))
        c1 = min(EXCEL_MAX_COL - 1, max(m.c0, m.c1))
        if r1 <= r0 and c1 <= c0:
            continue  # 退化为单格：Excel 中单格合并无意义
        out.append(Rect(r0, c0, r1, c1))
    # 面积大的优先保留，避免小区域破坏大区域
    out.sort(key=lambda m: -((m.r1 - m.r0) * (m.c1 - m.c0)))
    return out


class MergeIndex:
    """合并索引：主格映射 + 覆盖集合。"""

    def __init__(self) -> None:
        self.master: Dict[Tuple[int, int], Rect] = {}
        self._covered: Set[Tuple[int, int]] = set()

    @property
    def size(self) -> int:
        return len(self.master)

    def is_covered(self, row: int, col: int) -> bool:
        return (row, col) in self._covered

    def master_of(self, row: int, col: int) -> Optional[Rect]:
        return self.master.get((row, col))

    @staticmethod
    def spans_of(m: Rect) -> Tuple[int, int]:
        """返回 (rowSpan, colSpan)。"""
        return m.r1 - m.r0 + 1, m.c1 - m.c0 + 1


def build_merge_index(merges_raw: Iterable[Rect]) -> MergeIndex:
    merges = normalize_merges(merges_raw)
    idx = MergeIndex()
    budget = MAX_COVERED_CELLS

    for m in merges:
        key = (m.r0, m.c0)
        if key in idx.master or key in idx._covered:
            continue  # 同一起点重复定义（保留先到的/更大的）或主格已被更大合并区覆盖
        # ⚠️ 非法嵌套/重叠容错（第三方导出器会出现）：若与已登记合并区有任何
        #    重叠，整块丢弃当前（较小）合并区 —— HTML 无法表达重叠的
        #    rowspan/colspan，部分丢弃会让网格错位，只能整体放弃。
        overlap = False
        for r in range(m.r0, m.r1 + 1):
            for c in range(m.c0, m.c1 + 1):
                if (r, c) in idx.master or (r, c) in idx._covered:
                    overlap = True
                    break
            if overlap:
                break
        if overlap:
            continue

        idx.master[key] = m
        for r in range(m.r0, m.r1 + 1):
            if budget <= 0:
                break
            for c in range(m.c0, m.c1 + 1):
                if budget <= 0:
                    break
                if r == m.r0 and c == m.c0:
                    continue
                idx._covered.add((r, c))
                budget -= 1
    return idx


def is_covered_by(row: int, col: int, active: Iterable[Rect]) -> bool:
    """判断某坐标是否落在若干合并区之内（不含主格）—— 供虚拟滚动等场景使用。"""
    for m in active:
        if m.r0 <= row <= m.r1 and m.c0 <= col <= m.c1:
            return not (row == m.r0 and col == m.c0)
    return False
