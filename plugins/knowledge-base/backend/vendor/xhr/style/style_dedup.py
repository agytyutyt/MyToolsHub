"""样式去重 —— 对应 TS 版 ``packages/style/src/styleDedup.ts``。

渲染性能与产物体积的关键：一个 1 万格的表，实际不同样式通常只有 20~200 种。
"""
from __future__ import annotations

from typing import List

from ..core import StyleModel


def style_key(style: StyleModel) -> str:
    """样式键：只取影响视觉的字段（isDefault 等不参与）。"""
    f = style.font
    fill = style.fill
    b = style.border
    a = style.alignment
    gradient = ""
    if fill.gradient:
        gradient = f"{fill.gradient.get('degree', 0)}|" + ",".join(
            f"{s.get('pos', 0)}:{s.get('color', '')}" for s in fill.gradient.get("stops", [])
        )

    def side(s) -> str:
        return f"{s.style}:{s.color}" if s else ""

    return repr(
        [
            f.name, f.size_pt, f.bold, f.italic, f.underline, f.strike, f.color, f.vert_align or "",
            fill.type, fill.fg_color or "", fill.bg_color or "", fill.pattern or "", gradient,
            side(b.top), side(b.bottom), side(b.left), side(b.right),
            side(b.diagonal_up), side(b.diagonal_down),
            a.h, a.v, a.wrap, a.shrink_to_fit, a.indent, a.text_rotation, a.reading_order,
            style.num_fmt.code,
        ]
    )


class StyleTable:
    """登记样式并去重，返回样式表下标。"""

    def __init__(self) -> None:
        self._map: dict[str, int] = {}
        self._list: List[StyleModel] = []

    def intern(self, style: StyleModel) -> int:
        key = style_key(style)
        hit = self._map.get(key)
        if hit is not None:
            return hit
        sid = len(self._list)
        self._map[key] = sid
        self._list.append(style)
        return sid

    def __len__(self) -> int:
        return len(self._list)

    def at(self, sid: int) -> StyleModel:
        return self._list[sid] if 0 <= sid < len(self._list) else None  # type: ignore[return-value]

    def all(self) -> List[StyleModel]:
        return self._list
