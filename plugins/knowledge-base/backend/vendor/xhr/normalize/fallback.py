"""xlrd 兜底通道 —— 对应 TS 版 ``packages/normalize/src/sheetjs-fallback.ts``（T-302）。

无 LibreOffice 时直读 .xls（BIFF8），构建 WorkbookModel。
保真度降级（部分样式丢失）但**内容 100% 保留**，meta.warnings 必须带降级提示。

Python 侧用 xlrd（BIFF8 专用库，formatting_info=True 时可读到 XF/字体/填充/边框/
对齐/数字格式/合并/列宽行高），样式能力比 TS 版的 SheetJS CE 兜底更强。
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from ..core import (
    DEFAULT_COL_WIDTH_PX,
    DEFAULT_ROW_HEIGHT_PX,
    LIMITS,
    XhrError,
    CellModel,
    ColModel,
    MetaInfo,
    Rect,
    RowModel,
    SheetModel,
    ThemeInfo,
    WorkbookModel,
    col_width_to_px,
    row_height_to_px,
)
from ..style import (
    DEFAULT_INDEXED_PALETTE,
    DEFAULT_THEME_COLORS,
    DEFAULT_THEME_FONTS,
    THEME_COLOR_NAMES,
    StyleContext,
    StyleTable,
    create_style_context,
    format_value,
    resolve_style,
)

FALLBACK_WARNING = "当前环境未配置 LibreOffice，.xls 文件以低保真模式渲染（部分样式可能丢失）"

#: BIFF 边框线型码 → 名称
_BORDER_STYLES = {
    0: None, 1: "thin", 2: "medium", 3: "dashed", 4: "dotted", 5: "thick", 6: "double",
    7: "hair", 8: "mediumDashed", 9: "dashDot", 10: "mediumDashDot", 11: "dashDotDot",
    12: "mediumDashDotDot", 13: "slantDashDot",
}

#: BIFF 填充图案码 → 名称
_FILL_PATTERNS = {
    0: None, 1: "solid", 2: "mediumGray", 3: "darkGray", 4: "lightGray",
    5: "darkHorizontal", 6: "darkVertical", 7: "darkDown", 8: "darkUp", 9: "darkGrid",
    10: "darkTrellis", 11: "lightHorizontal", 12: "lightVertical", 13: "lightDown",
    14: "lightUp", 15: "lightGrid", 16: "lightTrellis", 17: "gray125", 18: "gray0625",
}

_H_ALIGN = {0: "general", 1: "left", 2: "center", 3: "right", 4: "fill", 5: "justify", 6: "centerContinuous", 7: "distributed"}
_V_ALIGN = {0: "top", 1: "center", 2: "bottom", 3: "justify", 4: "distributed"}


def _colour_to_raw(book, colour_index) -> Optional[dict]:
    """xlrd 调色板索引 → {'rgb': 'AARRGGBB'}。"""
    try:
        idx = int(colour_index)
    except (TypeError, ValueError):
        return None
    if idx in (0, 0x7FFF):
        return None
    rgb = getattr(book, "colour_map", {}).get(idx)
    if not rgb:
        return None
    r, g, b = rgb
    return {"rgb": f"FF{r:02X}{g:02X}{b:02X}"}


def _xlrd_style_to_raw(book, xf) -> dict:
    """xlrd XF 记录 → resolve_style 认识的 raw 形状（逐字段兜底，绝不抛错）。"""
    out: dict = {}
    try:
        font = book.font_list[xf.font_index]
        out["font"] = {
            "name": font.name or None,
            "size": (font.height / 20.0) if getattr(font, "height", 0) else None,
            "bold": bool(getattr(font, "bold", False) or getattr(font, "weight", 400) >= 700),
            "italic": bool(getattr(font, "italic", False)),
            "underline": {1: "single", 2: "double", 33: "singleAccounting", 34: "doubleAccounting"}.get(
                getattr(font, "underline_type", 0)
            ),
            "strike": bool(getattr(font, "struck_out", False)),
            "color": _colour_to_raw(book, getattr(font, "colour_index", None)),
            "vertAlign": {1: "superscript", 2: "subscript"}.get(getattr(font, "escapement", 0)),
        }
    except Exception:  # noqa: BLE001
        pass

    try:
        bg = xf.background
        pattern = _FILL_PATTERNS.get(bg.fill_pattern)
        if pattern:
            fg = _colour_to_raw(book, bg.pattern_colour_index)
            bgc = _colour_to_raw(book, bg.background_colour_index) or {"rgb": "FFFFFFFF"}
            out["fill"] = {"type": "pattern", "pattern": pattern, "fgColor": fg, "bgColor": bgc}
    except Exception:  # noqa: BLE001
        pass

    try:
        bd = xf.border
        border: dict = {}
        for side, attr_ls, attr_c in (
            ("top", "top_line_style", "top_colour_index"),
            ("bottom", "bottom_line_style", "bottom_colour_index"),
            ("left", "left_line_style", "left_colour_index"),
            ("right", "right_line_style", "right_colour_index"),
        ):
            style_name = _BORDER_STYLES.get(getattr(bd, attr_ls, 0))
            if style_name:
                border[side] = {"style": style_name, "color": _colour_to_raw(book, getattr(bd, attr_c, None))}
        if border:
            out["border"] = border
    except Exception:  # noqa: BLE001
        pass

    try:
        al = xf.alignment
        alignment: dict = {
            "horizontal": _H_ALIGN.get(getattr(al, "hor_align", 0)),
            "vertical": _V_ALIGN.get(getattr(al, "vert_align", 2)),
        }
        rotation = getattr(al, "rotation", 0) or 0
        if rotation:
            alignment["textRotation"] = 255 if rotation == 255 else rotation
        if getattr(al, "wrap_text", False) or getattr(al, "text_wrapped", False):
            alignment["wrapText"] = True
        out["alignment"] = {k: v for k, v in alignment.items() if v is not None}
    except Exception:  # noqa: BLE001
        pass

    try:
        fmt = book.format_map.get(xf.format_key)
        if fmt is not None and getattr(fmt, "format_str", ""):
            out["numFmt"] = fmt.format_str
    except Exception:  # noqa: BLE001
        pass

    return out


def _cell_type_of(ctype: int) -> str:
    # xlrd: XL_CELL_TEXT=1, NUMBER=2, DATE=3, BOOLEAN=4, ERROR=5
    return {1: "s", 2: "n", 3: "d", 4: "b", 5: "e"}.get(ctype, "s")


def _display_text(cell, num_fmt_code: str, date1904: bool) -> str:
    """w（显示文本）：数字/日期走我们的格式化，其余按类型兜底。"""
    ctype, v = cell.ctype, cell.value
    if ctype in (2, 3):
        return format_value(v, num_fmt_code, date1904).text
    if ctype == 4:
        return "TRUE" if v else "FALSE"
    if v is None:
        return ""
    return str(v)


def _sheet_to_model(
    book,
    name: str,
    index: int,
    style_table: StyleTable,
    ctx: StyleContext,
    warnings: List[str],
    state: str,
) -> SheetModel:
    sheet = book.sheet_by_index(index)
    nrows = min(sheet.nrows, LIMITS["MAX_ROWS_PER_SHEET"])
    ncols = min(sheet.ncols, LIMITS["MAX_COLS_PER_SHEET"])
    truncated = sheet.nrows > nrows
    date1904 = book.datemode == 1

    merges: List[Rect] = [
        Rect(rlo, clo, rhi - 1, chi - 1) for (rlo, rhi, clo, chi) in (sheet.merged_cells or [])
    ]
    covered = set()
    merge_by_origin: Dict[Tuple[int, int], Rect] = {}
    for m in merges:
        merge_by_origin[(m.r0, m.c0)] = m
        for r in range(m.r0, m.r1 + 1):
            for c in range(m.c0, m.c1 + 1):
                if (r, c) != (m.r0, m.c0):
                    covered.add((r, c))

    rows: List[RowModel] = []
    min_c, max_c = 1 << 30, -1
    cell_count = 0
    for r in range(nrows):
        cells: Dict[int, CellModel] = {}
        for c in range(ncols):
            if cell_count >= LIMITS["MAX_CELLS_PER_SHEET_RENDER"]:
                truncated = True
                break
            try:
                cell = sheet.cell(r, c)
                xf_index = sheet.cell_xf_index(r, c)
            except Exception:  # noqa: BLE001
                continue
            if cell.ctype in (0, 6):  # EMPTY / BLANK：无内容（含样式的空白格数量庞大，保守跳过）
                continue
            try:
                raw_style = _xlrd_style_to_raw(book, book.xf_list[xf_index])
            except Exception:  # noqa: BLE001
                raw_style = {}
            has_style = any(k in raw_style for k in ("fill", "border")) or bool(
                raw_style.get("font", {}).get("bold")
            )
            if (r, c) in covered and not has_style:
                continue  # 被合并覆盖且无样式的格不输出
            num_fmt_code = raw_style.get("numFmt") or "General"
            model = CellModel(
                row=r,
                col=c,
                type=_cell_type_of(cell.ctype),
                v=cell.value,
                w=_display_text(cell, num_fmt_code, date1904),
                style_id=style_table.intern(resolve_style(raw_style, ctx)),
            )
            origin = merge_by_origin.get((r, c))
            if origin:
                model.merge = {"rowSpan": origin.r1 - origin.r0 + 1, "colSpan": origin.c1 - origin.c0 + 1}
            cells[c] = model
            min_c = min(min_c, c)
            max_c = max(max_c, c)
            cell_count += 1
        rowinfo = (sheet.rowinfo_map or {}).get(r)
        height_px = 0
        hidden = False
        outline = 0
        if rowinfo is not None:
            if getattr(rowinfo, "height", 0):
                height_px = row_height_to_px(rowinfo.height / 20.0)
            hidden = bool(getattr(rowinfo, "hidden", False))
            outline = int(getattr(rowinfo, "outline_level", 0) or 0)
        if not cells and height_px == 0 and not hidden:
            continue
        rows.append(RowModel(index=r, height_px=height_px, hidden=hidden, outline_level=outline, cells=cells))

    if truncated:
        warnings.append(f"工作表「{name}」超出单表渲染上限，已截断")

    # 列宽：colinfo width 单位是 1/256 字符宽
    cols: List[ColModel] = []
    for colx, info in sorted((sheet.colinfo_map or {}).items()):
        width_ch = (getattr(info, "width", 0) or 0) / 256.0
        width_px = col_width_to_px(width_ch) if width_ch > 0 else 0
        hidden = bool(getattr(info, "hidden", False))
        if width_px <= 0 and not hidden:
            continue
        cols.append(ColModel(index=colx, width_px=width_px or DEFAULT_COL_WIDTH_PX, hidden=hidden))

    r0 = rows[0].index if rows else 0
    r1 = rows[-1].index if rows else 0
    return SheetModel(
        id=f"sheet{index + 1}",
        name=name,
        index=index,
        state=state,
        used_range=Rect(r0, (min_c if rows else 0), r1, (max_c if rows else 0)),
        rows=rows,
        cols=cols,
        default_col_width_px=DEFAULT_COL_WIDTH_PX,
        default_row_height_px=DEFAULT_ROW_HEIGHT_PX,
        merges=merges,
    )


def xlrd_to_model(data: bytes) -> WorkbookModel:
    """xlrd 工作簿 → WorkbookModel（公开入口）。"""
    try:
        import xlrd
    except ImportError as e:
        raise XhrError(
            "UNSUPPORTED_FORMAT",
            ".xls 兜底通道需要 xlrd：pip install 'xhr[xls]'，或安装 LibreOffice 获得完整保真",
        ) from e

    warnings: List[str] = [FALLBACK_WARNING]
    try:
        book = xlrd.open_workbook(file_contents=data, formatting_info=True)
    except Exception as e:  # noqa: BLE001
        raise XhrError("CORRUPTED", f"{type(e).__name__}: {e}") from e

    style_table = StyleTable()
    ctx = create_style_context(
        theme_colors=list(DEFAULT_THEME_COLORS),
        indexed=list(DEFAULT_INDEXED_PALETTE),
        default_font_name=DEFAULT_THEME_FONTS["minorFont"],
    )
    # 先登记默认样式为 0 号（与 xlsx 链路一致：默认样式必须是 styleTable[0]）
    style_table.intern(resolve_style({}, ctx))

    sheets: List[SheetModel] = []
    for i in range(min(book.nsheets, LIMITS["MAX_SHEETS"])):
        name = book.sheet_names()[i]
        visibility = book.sheet_by_index(i).visibility
        state = {0: "visible", 1: "hidden", 2: "veryHidden"}.get(visibility, "visible")
        try:
            sheets.append(_sheet_to_model(book, name, i, style_table, ctx, warnings, state))
        except Exception as e:  # noqa: BLE001
            warnings.append(f"工作表「{name}」解析失败：{type(e).__name__}: {e}")

    cell_total = 0
    max_rows = 0
    max_cols = 0
    for s in sheets:
        cell_total += sum(len(r.cells) for r in s.rows)
        max_rows = max(max_rows, s.used_range.r1 + 1)
        max_cols = max(max_cols, s.used_range.c1 + 1)

    return WorkbookModel(
        meta=MetaInfo(
            source_format="xls",
            creator=getattr(book, "user_name", None),
            date1904=book.datemode == 1,
            warnings=warnings,
            stats={"sheetCount": len(sheets), "cellCount": cell_total, "maxRows": max_rows, "maxCols": max_cols},
        ),
        theme=ThemeInfo(
            color_scheme={n: DEFAULT_THEME_COLORS[i] for i, n in enumerate(THEME_COLOR_NAMES)},
            major_font=DEFAULT_THEME_FONTS["majorFont"],
            minor_font=DEFAULT_THEME_FONTS["minorFont"],
        ),
        indexed_palette=list(DEFAULT_INDEXED_PALETTE),
        style_table=style_table.all(),
        sheets=sheets,
    )
