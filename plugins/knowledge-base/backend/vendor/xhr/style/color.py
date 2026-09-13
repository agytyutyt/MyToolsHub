"""颜色解析 —— 对应 TS 版 ``packages/style/src/color.ts``。

XLSX 中颜色有 5 种表示，必须全部支持：
  rgb(ARGB) / theme+tint / indexed / auto / sysClr

★ 主题色索引映射（Python 版修正，附实证）：
  设计文档 6.4.1 写的顺序是 0=dk1、1=lt1，但**真实 Excel 文件**里
  ``<color theme="1"/>`` 是默认黑色文字。经 LibreOffice 26.8 实测验证，
  theme 索引 0-3 与 clrScheme 文档顺序（dk1,lt1,dk2,lt2）存在**两两交换**：

      index 0 → lt1   index 1 → dk1   index 2 → lt2   index 3 → dk2

  （Excel UI 中叫 Background1/Text1/Background2/Text2，与 dk/lt 正好对调。）
  TS 实现按文档顺序直取，导致默认字体（theme=1）被解析成白色 ——
  本实现按上面的交换映射解析。
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Optional, Union

from .default_theme import THEME_INDEX_TO_SCHEME

_AHEX_RE = re.compile(r"^[0-9A-F]{6}$")


@dataclass
class ColorCtx:
    #: 12 个主题色，已转 #RRGGBB，**clrScheme 文档顺序**：dk1 lt1 dk2 lt2 accent1..6 hlink folHlink
    theme_colors: list[str]
    #: 56 色索引调色板
    indexed: list[str]


def normalize_hex6(s: str) -> str:
    """'FFFF0000' → 'FF0000'；'FF0000' → 'FF0000'；容错非法输入。"""
    t = (s or "").strip().lstrip("#").upper()
    if len(t) == 8:
        t = t[2:]  # 去掉 alpha
    if len(t) == 3:
        t = "".join(c + c for c in t)
    if not _AHEX_RE.match(t):
        t = (t + "000000")[:6]
        t = re.sub(r"[^0-9A-F]", "0", t)
    return t


def apply_tint(rgb: str, tint: float) -> str:
    """应用 Excel tint。范围 tint ∈ [-1, 1]。

    ⚠️ 两个必须记住的点：
      1. 必须逐通道线性计算，不能用 HSL 的 lighten/darken；
      2. 取整必须用 **floor（截断）**，不是 round。
         验证：Office 主题 accent1 #4472C4 + 浅色 40% → Excel 显示 #8EAADB。
         用 floor: R=142(0x8E) ✅；用 round: R=143(0x8F) ❌ 每通道偏 1。
         同理"黑色 +50%"在 Excel 里是 #7F7F7F（floor=127=0x7F）。
    """
    hex6 = normalize_hex6(rgb)
    if not tint:
        return "#" + hex6
    out: list[str] = []
    for i in (0, 2, 4):
        c = int(hex6[i : i + 2], 16)
        if tint < 0:
            v = c * (1 + tint)            # 变暗：按比例缩向 0
        else:
            v = c * (1 - tint) + 255 * tint  # 变亮：按比例混向白色 255
        v = max(0, min(255, math.floor(v)))
        out.append(f"{v:02X}")
    return "#" + "".join(out)


def resolve_color(
    c: Optional[dict],
    ctx: ColorCtx,
    default_hex: str = "#000000",
) -> str:
    """统一颜色解析入口。

    ``c`` 的形状（解析层已归一化）::

        {'rgb': 'AARRGGBB'} | {'theme': int, 'tint'?: float}
        | {'indexed': int} | {'auto': 1} | {'sysClr': str, 'lastClr'?: str}
    """
    if not c:
        return default_hex
    if "rgb" in c and c["rgb"]:
        return "#" + normalize_hex6(str(c["rgb"]))
    if "theme" in c and isinstance(c["theme"], (int, float)):
        idx = THEME_INDEX_TO_SCHEME[int(c["theme"])]
        base = ctx.theme_colors[idx] if 0 <= idx < len(ctx.theme_colors) else None
        if not base:
            return default_hex
        return apply_tint(base.lstrip("#"), float(c.get("tint") or 0))
    if "indexed" in c and isinstance(c["indexed"], (int, float)):
        n = int(c["indexed"])
        if n == 64:
            return "#000000"  # 系统前景色（windowText）
        if n == 65:
            return "#FFFFFF"  # 系统背景色（window）
        p = ctx.indexed
        return p[n] if 0 <= n < len(p) else (p[n % 56] if p else default_hex)
    if "sysClr" in c:
        last = c.get("lastClr")
        if last:
            return "#" + normalize_hex6(str(last))
        val = str(c.get("sysClr") or "")
        if re.search(r"window$", val, re.IGNORECASE) and not re.search(r"windowtext", val, re.IGNORECASE):
            return "#FFFFFF"
        return "#000000"
    return default_hex


def hex_to_rgba(hex_color: str, alpha: float) -> str:
    """6 位 hex + 透明度 → rgba() 字符串。"""
    h = normalize_hex6(hex_color)
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"rgba({r},{g},{b},{alpha})"


def is_dark(hex_color: str) -> bool:
    """判断颜色是否为"深色"（相对亮度 sRGB 近似）。"""
    h = normalize_hex6(hex_color)
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return 0.299 * r + 0.587 * g + 0.114 * b < 140
