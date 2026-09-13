"""单位换算 —— 对应《设计文档》6.5 与 12.2 速查卡（T-603）。

⚠️ 三条最容易写错的（见 HANDOFF-DHR）：
  1. ``w:line`` 的含义由 ``w:lineRule`` 决定：auto=1/240 行、exact/atLeast=twips；
     不看 lineRule 直接 line/240 是 Word 方案的头号 bug。
  2. 切换属性 ``w:b w:val="0"`` 是显式关闭，必须覆盖继承值 —— 第二号 bug（在 styles.py）。
  3. ``eighth_pt_to_px`` 必须 max(1, …)，否则细边框在浏览器里看不见。
"""
from __future__ import annotations

import math
from typing import Any, Dict, Optional

# ── 长度 ──

def twip_to_px(twip: float) -> int:
    """1 twip = 1/20 pt = 1/1440 inch；96dpi 下 1pt = 4/3 px → px = twip/15。"""
    return round(twip / 15)


def twip_to_pt(twip: float) -> float:
    return twip / 20


def half_point_to_pt(v: float) -> float:
    """w:sz / w:szCs 为半磅：24 → 12pt。"""
    return v / 2


def twentieth_to_pt(v: float) -> float:
    """w:spacing（字符间距）为二十分之一磅：20 → 1pt。"""
    return v / 20


def half_point_to_px(v: float) -> float:
    """w:position（字符升降）为半磅，正值上移。"""
    return v / 2 * 4 / 3


def emu_to_px(emu: float) -> int:
    """1 inch = 914400 EMU，1 px = 9525 EMU。"""
    return round(emu / 9525)


def eighth_pt_to_px(v: float) -> int:
    """边框宽度 w:sz 单位为 1/8 pt；浏览器最小 1px。"""
    if not math.isfinite(v) or v <= 0:
        return 0
    return max(1, round(v / 8 * 4 / 3))


def chars100_to_em(v: float) -> float:
    """w:firstLineChars 等单位为「字符 ×100」：200 → 2em。"""
    return v / 100


def pct_to_percent(v: float) -> float:
    """w:tblW type=pct 单位为 1/50 %：5000 → 100%。"""
    return v / 50


def src_rect_ratio(v: float) -> float:
    """a:srcRect l/t/r/b 单位为 1/1000 %。"""
    return v / 1000


# ── 行距 ──

def parse_line_spacing(spacing: Optional[dict]) -> Optional[dict]:
    """解析 w:spacing 的行距。

    ⚠️ lineRule 缺省或 auto → line 单位 = 1/240 行；exact/atLeast → twips。
    @param spacing 形如 {'line': '360', 'lineRule': 'auto'}（属性已去命名空间）
    """
    if not spacing:
        return None
    line = spacing.get("line")
    if line in (None, ""):
        return None
    try:
        v = float(line)
    except (TypeError, ValueError):
        return None
    rule = str(spacing.get("lineRule") or "auto")
    if rule == "exact":
        return {"kind": "exact", "pt": twip_to_pt(v)}
    if rule == "atLeast":
        return {"kind": "atLeast", "pt": twip_to_pt(v)}
    return {"kind": "multiple", "value": v / 240}


def effective_line_height(line_spacing: Optional[dict], snap_to_grid: bool, doc_grid: Optional[dict]) -> Optional[dict]:
    """中文文档网格行距：snapToGrid 且 docGrid 为 lines/linesAndChars 时实际行距=网格行距。"""
    grid = doc_grid
    if snap_to_grid and grid and grid.get("type") in ("lines", "linesAndChars") and grid.get("linePitch"):
        return {"kind": "exact", "pt": twip_to_pt(grid["linePitch"])}
    return line_spacing


def line_spacing_to_css(ls: Optional[dict]) -> Optional[str]:
    """行距 → CSS line-height 值。"""
    if not ls:
        return None
    if ls["kind"] == "multiple":
        return f"{ls['value']:.4f}".rstrip("0").rstrip(".")
    return f"{ls['pt']:.2f}".rstrip("0").rstrip(".") + "pt"


#: 文档级限额（在 xhr.core.LIMITS 之上新增 Word 特有项）
DHR_LIMITS: Dict[str, int] = {
    "MAX_PARAGRAPHS": 200_000,
    "MAX_TABLE_CELLS": 500_000,
    "MAX_IMAGES": 5000,
    "MAX_IMAGE_BYTES": 20 * 1024 * 1024,
}
