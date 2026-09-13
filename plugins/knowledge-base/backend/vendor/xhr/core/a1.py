"""A1 记法 ↔ {row, col} 互转 —— 对应 TS 版 ``packages/core/src/a1.ts``。

约定：IR 内部坐标为 0-based；A1 记法的列从 A=1 起算（无 0）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Tuple


@dataclass(frozen=True)
class RC:
    row: int
    col: int


@dataclass
class Rect:
    """闭区间范围（0-based）。

    注意是可变的：解析层在扩展 usedRange（如浮动图片锚点）时会就地改写 r1/c1。
    """

    r0: int
    c0: int
    r1: int
    c1: int


_A1_RE = re.compile(r"^\$?([A-Za-z]+)\$?([0-9]+)$")


def a1_to_rc(a1: str) -> RC:
    """\"B3\" → RC(row=2, col=1)。支持 $B$3 绝对引用写法。"""
    m = _A1_RE.match(a1.strip())
    if not m:
        raise ValueError(f"bad A1 reference: {a1}")
    letters, digits = m.group(1), m.group(2)
    col = 0
    for ch in letters.upper():
        col = col * 26 + (ord(ch) - 64)  # 'A' = 1
    return RC(row=int(digits) - 1, col=col - 1)


def rc_to_a1(row: int, col: int) -> str:
    """(row=0, col=26) → \"AA1\"。"""
    s = ""
    c = col + 1
    while c > 0:
        r = (c - 1) % 26
        s = chr(65 + r) + s
        c = (c - 1) // 26
    return f"{s}{row + 1}"


def a1_range_to_rect(ref: str) -> Rect:
    """\"A1:C3\" → 闭区间范围；单格 \"A1\" 也可以。"""
    parts = ref.split(":")
    a = a1_to_rc(parts[0])
    b = a1_to_rc(parts[1]) if len(parts) > 1 else a
    return Rect(
        r0=min(a.row, b.row),
        c0=min(a.col, b.col),
        r1=max(a.row, b.row),
        c1=max(a.col, b.col),
    )


def rect_to_a1(r: Rect) -> str:
    a = rc_to_a1(r.r0, r.c0)
    b = rc_to_a1(r.r1, r.c1)
    return a if a == b else f"{a}:{b}"


def col_name(col: int) -> str:
    """列号 → 列标（0 → \"A\"）。"""
    return rc_to_a1(0, col).rstrip("0123456789")


def col_from_name(name: str) -> int:
    """列标 → 列号（\"A\" → 0，\"AA\" → 26）。非法输入返回 -1。"""
    s = name.upper()
    if not s or len(s) > 3:
        return -1
    n = 0
    for ch in s:
        c = ord(ch)
        if c < 65 or c > 90:
            return -1
        n = n * 26 + (c - 64)
    return n - 1
