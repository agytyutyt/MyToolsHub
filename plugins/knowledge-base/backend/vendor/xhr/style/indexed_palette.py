"""内置 56 色索引调色板 —— 对应 TS 版 ``packages/style/src/indexedPalette.ts``。

当 styles.xml 里没有 <indexedColors> 时使用。
indexed=64 → 系统前景色（默认黑）；indexed=65 → 系统背景色（默认白）。
"""
from __future__ import annotations

DEFAULT_INDEXED_PALETTE: list[str] = [
    # 0-7
    "#000000", "#FFFFFF", "#FF0000", "#00FF00", "#0000FF", "#FFFF00", "#FF00FF", "#00FFFF",
    # 8-15
    "#000000", "#FFFFFF", "#FF0000", "#00FF00", "#0000FF", "#FFFF00", "#FF00FF", "#00FFFF",
    # 16-23
    "#800000", "#008000", "#000080", "#808000", "#800080", "#008080", "#C0C0C0", "#808080",
    # 24-31
    "#9999FF", "#993366", "#FFFFCC", "#CCFFFF", "#660066", "#FF8080", "#0066CC", "#CCCCFF",
    # 32-39
    "#000080", "#FF00FF", "#FFFF00", "#00FFFF", "#800080", "#800000", "#008080", "#0000FF",
    # 40-47
    "#00CCFF", "#CCFFFF", "#CCFFCC", "#FFFF99", "#99CCFF", "#FF99CC", "#CC99FF", "#FFCC99",
    # 48-55
    "#3366FF", "#33CCCC", "#99CC00", "#FFCC00", "#FF9900", "#FF6600", "#666699", "#969696",
]
