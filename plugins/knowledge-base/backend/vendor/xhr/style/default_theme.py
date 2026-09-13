"""Office 默认主题兜底 —— 对应 TS 版 ``packages/style/src/defaultTheme.ts``。

很多第三方生成器（早期 POI、某些报表工具、WPS 老版本）不写 theme1.xml，
此时必须用这套默认值，否则所有主题色都会退化成黑色/白色。
"""
from __future__ import annotations

#: 顺序固定（clrScheme 文档顺序）：dk1 lt1 dk2 lt2 accent1..accent6 hlink folHlink
DEFAULT_THEME_COLORS: list[str] = [
    "#000000",  # dk1  深色1
    "#FFFFFF",  # lt1  浅色1
    "#44546A",  # dk2  深色2
    "#E7E6E6",  # lt2  浅色2
    "#4472C4",  # accent1
    "#ED7D31",  # accent2
    "#A5A5A5",  # accent3
    "#FFC000",  # accent4
    "#5B9BD5",  # accent5
    "#70AD47",  # accent6
    "#0563C1",  # hlink   超链接
    "#954F72",  # folHlink 已访问超链接
]

#: 主题色下标 → 语义名（与 Office clrScheme 文档顺序一致）
THEME_COLOR_NAMES: list[str] = [
    "dk1", "lt1", "dk2", "lt2",
    "accent1", "accent2", "accent3", "accent4", "accent5", "accent6",
    "hlink", "folHlink",
]

#: ★ styles.xml 里 ``<color theme="N"/>`` 的索引 → clrScheme 文档顺序下标。
#:
#: Excel 的索引语义是 Background1/Text1/Background2/Text2（= lt1/dk1/lt2/dk2），
#: 与 clrScheme 的文档顺序（dk1,lt1,dk2,lt2）**两两交换**；accent 起不交换。
#: 已用 LibreOffice 26.8 实测验证（默认字体 theme=1 必须是黑色）。
THEME_INDEX_TO_SCHEME: list[int] = [1, 0, 3, 2, 4, 5, 6, 7, 8, 9, 10, 11]

DEFAULT_THEME_FONTS = {"majorFont": "Calibri Light", "minorFont": "Calibri"}
