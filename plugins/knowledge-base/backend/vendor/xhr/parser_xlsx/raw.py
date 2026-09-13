"""原始部件读取 —— 主题色 / 调色板 / 自定义数字格式 / date1904 / 共享字符串。

对应 TS 版 ``packages/parser-xlsx/src/raw.ts``。

需要自己读 XML 的东西：
  1. xl/theme/theme1.xml  → 主题色 clrScheme + 主/次字体
     （不读这个文件，所有主题色都会退化成黑/白 —— 最常见的保真度 bug）
  2. xl/styles.xml        → <indexedColors> 覆盖项 + 自定义 numFmt
  3. xl/workbook.xml      → workbookPr@date1904（1904 日期系统）
  4. xl/sharedStrings.xml → 共享字符串（含富文本 run）
"""
from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple, Union

from ..style import DEFAULT_INDEXED_PALETTE, DEFAULT_THEME_COLORS, DEFAULT_THEME_FONTS, normalize_hex6
from .xmlutil import (
    attr,
    attr_float,
    children,
    child,
    parse_color_el,
    parse_xml,
    read_relationships,
    resolve_rel_target,
    text_of,
)

#: theme 部件路径（真实文件也可能叫 theme2.xml，按 workbook rels 解析；
#: 这里保留常用回退路径）
THEME_FALLBACK_PATHS = ("xl/theme/theme1.xml",)


@dataclass
class ThemeResult:
    #: clrScheme 文档顺序（dk1 lt1 dk2 lt2 accent1..6 hlink folHlink）
    color_scheme: List[str]
    major_font: str
    minor_font: str


def _scheme_color(entry: Optional["object"]) -> str:
    """clrScheme 子节点（dk1/lt1/...）→ #RRGGBB。"""
    import xml.etree.ElementTree as ET

    if entry is None or not isinstance(entry, ET.Element):
        return "#000000"
    srgb = child(entry, "srgbClr")
    if srgb is not None and srgb.get("val"):
        return "#" + normalize_hex6(srgb.get("val"))
    sys_el = child(entry, "sysClr")
    if sys_el is not None:
        last = sys_el.get("lastClr")
        if last:
            return "#" + normalize_hex6(last)
        val = (sys_el.get("val") or "").lower()
        return "#FFFFFF" if val == "window" else "#000000"
    # scrgbClr / prstClr 等复杂形式：保守降级为黑
    return "#000000"


def read_theme(files: dict[str, bytes], theme_path: Optional[str] = None) -> ThemeResult:
    """解析 theme1.xml。

    clrScheme 的子节点顺序在 Office 中固定：
      dk1 lt1 dk2 lt2 accent1 accent2 accent3 accent4 accent5 accent6 hlink folHlink
    颜色节点可能是 <srgbClr val="RRGGBB"/> 或 <sysClr val="window" lastClr="FFFFFF"/>。
    """
    candidates = ([theme_path] if theme_path else []) + list(THEME_FALLBACK_PATHS)
    root: object = None
    for path in candidates:
        root = parse_xml(files.get(path))
        if root is not None:
            break
    colors: List[str] = []
    major = minor = ""
    if root is not None:
        theme_elements = child(root, "themeElements")
        clr = child(theme_elements, "clrScheme") if theme_elements is not None else None
        if clr is not None:
            for key in THEME_COLOR_ORDER:
                colors.append(_scheme_color(child(clr, key)))
        fonts_el = child(theme_elements, "fontScheme") if theme_elements is not None else None
        if fonts_el is not None:
            major_el = child(child(fonts_el, "majorFont"), "latin")
            minor_el = child(child(fonts_el, "minorFont"), "latin")
            major = (major_el.get("typeface") or "") if major_el is not None else ""
            minor = (minor_el.get("typeface") or "") if minor_el is not None else ""

    return ThemeResult(
        color_scheme=colors if len(colors) == 12 else list(DEFAULT_THEME_COLORS),
        major_font=major or DEFAULT_THEME_FONTS["majorFont"],
        minor_font=minor or DEFAULT_THEME_FONTS["minorFont"],
    )


THEME_COLOR_ORDER = ("dk1", "lt1", "dk2", "lt2", "accent1", "accent2", "accent3", "accent4", "accent5", "accent6", "hlink", "folHlink")


def read_indexed_colors(files: dict[str, bytes], styles_path: str = "xl/styles.xml") -> Optional[List[str]]:
    """读取 styles.xml 里的 <indexedColors> 覆盖项；无则返回 None。"""
    root = parse_xml(files.get(styles_path))
    if root is None:
        return None
    colors_el = child(root, "colors")
    indexed = child(colors_el, "indexedColors") if colors_el is not None else None
    if indexed is None:
        return None
    out: List[str] = []
    for rgb_el in children(indexed, "rgbColor"):
        out.append("#" + normalize_hex6(rgb_el.get("rgb") or ""))
    if not out:
        return None
    return out[:56] if len(out) >= 56 else out + DEFAULT_INDEXED_PALETTE[len(out) :]


def read_custom_num_fmts(files: dict[str, bytes], styles_path: str = "xl/styles.xml") -> dict[int, str]:
    """读取自定义数字格式（numFmtId >= 164）。"""
    root = parse_xml(files.get(styles_path))
    if root is None:
        return {}
    fmts_el = child(root, "numFmts")
    out: dict[int, str] = {}
    if fmts_el is None:
        return out
    for n in children(fmts_el, "numFmt"):
        try:
            fid = int(float(n.get("numFmtId") or ""))
        except ValueError:
            continue
        code = n.get("formatCode") or ""
        if code:
            out[fid] = code
    return out


def read_workbook_meta(files: dict[str, bytes], workbook_path: str = "xl/workbook.xml") -> dict:
    """读取 workbookPr@date1904 与工作簿级信息（sheet 列表由 parser.py 处理）。"""
    root = parse_xml(files.get(workbook_path))
    if root is None:
        return {"date1904": False, "sheets": [], "rels": {}}
    pr = child(root, "workbookPr")
    date1904 = (attr(pr, "date1904") or "").lower() in ("1", "true")
    sheets: List[dict] = []
    sheets_el = child(root, "sheets")
    if sheets_el is not None:
        for s in children(sheets_el, "sheet"):
            sheets.append(
                {
                    "name": attr(s, "name"),
                    "sheet_id": attr(s, "sheetId"),
                    # r:id 剥离命名空间后就是 id
                    "rid": attr(s, "id"),
                    "state": attr(s, "state") or "visible",
                }
            )
    return {"date1904": date1904, "sheets": sheets}


# ─────────────────────────────────────────────────────────────
# 共享字符串
# ─────────────────────────────────────────────────────────────


@dataclass
class SharedText:
    """共享字符串条目：plain 与 runs 二选一（runs 存在时是富文本）。"""

    plain: str = ""
    runs: Optional[List[Tuple[str, Optional[dict]]]] = None  # (text, rPr dict)


def _rpr_to_font(rpr: Optional[object]) -> Optional[dict]:
    """<rPr>（富文本运行属性）→ raw font dict（ExcelJS 形状）。"""
    import xml.etree.ElementTree as ET

    if rpr is None or not isinstance(rpr, ET.Element):
        return None
    out: dict = {}
    rfont = child(rpr, "rFont")
    if rfont is not None and rfont.get("val"):
        out["name"] = rfont.get("val")
    sz = child(rpr, "sz")
    if sz is not None and sz.get("val"):
        try:
            out["size"] = float(sz.get("val"))
        except ValueError:
            pass
    if child(rpr, "b") is not None:
        out["bold"] = True
    if child(rpr, "i") is not None:
        out["italic"] = True
    if child(rpr, "strike") is not None:
        out["strike"] = True
    u = child(rpr, "u")
    if u is not None:
        out["underline"] = u.get("val") or True
    color = parse_color_el(child(rpr, "color"))
    if color:
        out["color"] = color
    va = child(rpr, "vertAlign")
    if va is not None and va.get("val"):
        out["vertAlign"] = va.get("val")
    scheme = child(rpr, "scheme")
    if scheme is not None and scheme.get("val"):
        out["scheme"] = scheme.get("val")
    return out or None


def read_shared_strings(files: dict[str, bytes], path: Optional[str] = None) -> List[SharedText]:
    """解析 xl/sharedStrings.xml。"""
    root = parse_xml(files.get(path or "xl/sharedStrings.xml"))
    out: List[SharedText] = []
    if root is None:
        return out
    for si in children(root, "si"):
        runs: List[Tuple[str, Optional[dict]]] = []
        for r in children(si, "r"):
            t = child(r, "t")
            if t is None:
                continue
            runs.append((text_of(t), _rpr_to_font(child(r, "rPr"))))
        if runs:
            plain = "".join(t for t, _ in runs)
            out.append(SharedText(plain=plain, runs=runs))
        else:
            t = child(si, "t")
            out.append(SharedText(plain=text_of(t) if t is not None else text_of(si)))
    return out


def part_dir(part_path: str) -> str:
    return posixpath.dirname(part_path)


def sheet_rels_path(part_path: str) -> str:
    """'xl/worksheets/sheet1.xml' → 'xl/worksheets/_rels/sheet1.xml.rels'。"""
    d, base = posixpath.split(part_path)
    return posixpath.join(d, "_rels", base + ".rels")
