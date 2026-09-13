"""worksheet XML 解析 —— sheetN.xml → RawSheet（纯 XML 层，不做样式/IR 转换）。

对应 TS 版里 ExcelJS 承担的结构解析 + 自研补充读取（cf / merges / hyperlinks）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .xmlutil import (
    attr,
    attr_bool,
    attr_float,
    attr_int,
    child,
    children,
    parse_color_el,
    parse_xml,
    text_of,
)


@dataclass
class RawCell:
    row: int
    col: int
    #: cellXfs 下标（缺失为 None → 默认样式 0）
    s: Optional[int] = None
    #: 's' | 'inlineStr' | 'str' | 'n' | 'b' | 'e' | 'd' | ''
    t: str = "n"
    #: <v> 原始文本
    v: Optional[str] = None
    #: inlineStr 的 <is> 富文本 runs（text, rPr dict）
    is_runs: Optional[List[tuple]] = None
    #: 公式文本（<f>）
    formula: Optional[str] = None
    #: 共享公式 si
    shared_si: Optional[str] = None
    #: 是否是共享公式主格
    shared_master: bool = False


@dataclass
class RawRow:
    index: int
    ht_pt: Optional[float] = None
    hidden: bool = False
    outline_level: int = 0
    cells: List[RawCell] = field(default_factory=list)


@dataclass
class RawCol:
    min: int
    max: int
    width: Optional[float] = None
    hidden: bool = False
    outline_level: int = 0
    style: Optional[int] = None


@dataclass
class RawCfRule:
    type: str
    priority: int
    operator: Optional[str] = None
    formulas: List[str] = field(default_factory=list)
    dxf_id: Optional[int] = None
    stop_if_true: bool = False


@dataclass
class RawCfBlock:
    sqref: str
    rules: List[RawCfRule] = field(default_factory=list)


@dataclass
class RawHyperlink:
    ref: str
    rid: Optional[str] = None
    location: Optional[str] = None
    tooltip: Optional[str] = None


@dataclass
class RawSheet:
    part_path: str = ""
    rows: List[RawRow] = field(default_factory=list)
    cols: List[RawCol] = field(default_factory=list)
    merges: List[str] = field(default_factory=list)
    #: sheetView 属性 + pane
    view: dict = field(default_factory=dict)
    tab_color: Optional[dict] = None
    hyperlinks: List[RawHyperlink] = field(default_factory=list)
    cf_blocks: List[RawCfBlock] = field(default_factory=list)
    auto_filter: Optional[str] = None
    #: dataValidation sqref 列表
    data_validation_sqrefs: List[str] = field(default_factory=list)
    #: r:id → drawing / comments / vmlDrawing
    drawing_rid: Optional[str] = None
    legacy_drawing_rid: Optional[str] = None
    default_row_height_pt: Optional[float] = None
    default_col_width: Optional[float] = None
    dimension: Optional[str] = None


def parse_worksheet(files: dict[str, bytes], part_path: str) -> RawSheet:
    """解析一个工作表部件。任何局部异常都不外抛（返回能解析到的部分）。"""
    sheet = RawSheet(part_path=part_path)
    root = parse_xml(files.get(part_path))
    if root is None:
        return sheet

    # ── dimension（仅记录，实际范围由单元格反推）──
    dim = child(root, "dimension")
    if dim is not None:
        sheet.dimension = attr(dim, "ref")

    # ── sheetPr / tabColor ──
    sheet_pr = child(root, "sheetPr")
    if sheet_pr is not None:
        sheet.tab_color = parse_color_el(child(sheet_pr, "tabColor"))

    # ── sheetFormatPr ──
    fmt = child(root, "sheetFormatPr")
    if fmt is not None:
        sheet.default_row_height_pt = attr_float(fmt, "defaultRowHeight")
        sheet.default_col_width = attr_float(fmt, "defaultColWidth")

    # ── sheetViews / pane ──
    views_el = child(root, "sheetViews")
    view_el = child(views_el, "sheetView") if views_el is not None else None
    if view_el is not None:
        sheet.view = {
            "showGridLines": attr_bool(view_el, "showGridLines"),
            "showRowColHeaders": attr_bool(view_el, "showRowColHeaders"),
            "rightToLeft": attr_bool(view_el, "rightToLeft"),
            "zoomScale": attr_int(view_el, "zoomScale"),
            "tabSelected": attr_bool(view_el, "tabSelected"),
        }
        pane = child(view_el, "pane")
        if pane is not None:
            sheet.view["pane"] = {
                "xSplit": attr_int(pane, "xSplit", 0) or 0,
                "ySplit": attr_int(pane, "ySplit", 0) or 0,
                "topLeftCell": attr(pane, "topLeftCell"),
                "state": attr(pane, "state"),
            }

    # ── cols ──
    cols_el = child(root, "cols")
    if cols_el is not None:
        for col in children(cols_el, "col"):
            min_c = attr_int(col, "min", 0) or 0
            max_c = attr_int(col, "max", min_c) or min_c
            width = attr_float(col, "width")
            sheet.cols.append(
                RawCol(
                    min=min_c,
                    max=max_c,
                    width=width,
                    hidden=attr_bool(col, "hidden") is True,
                    outline_level=attr_int(col, "outlineLevel", 0) or 0,
                    style=attr_int(col, "style"),
                )
            )

    # ── sheetData ──
    sheet_data = child(root, "sheetData")
    if sheet_data is not None:
        for row_el in children(sheet_data, "row"):
            row_idx = attr_int(row_el, "r", 0) or 0
            row = RawRow(
                index=row_idx - 1,
                ht_pt=attr_float(row_el, "ht"),
                hidden=attr_bool(row_el, "hidden") is True,
                outline_level=attr_int(row_el, "outlineLevel", 0) or 0,
            )
            for c_el in children(row_el, "c"):
                cell = _parse_cell(c_el, row_idx)
                if cell is not None:
                    row.cells.append(cell)
            sheet.rows.append(row)

    # ── mergeCells ──
    merges_el = child(root, "mergeCells")
    if merges_el is not None:
        for m in children(merges_el, "mergeCell"):
            ref = attr(m, "ref")
            if ref:
                sheet.merges.append(ref)

    # ── hyperlinks ──
    hl_el = child(root, "hyperlinks")
    if hl_el is not None:
        for h in children(hl_el, "hyperlink"):
            sheet.hyperlinks.append(
                RawHyperlink(
                    ref=attr(h, "ref"),
                    rid=attr(h, "id") or None,  # r:id
                    location=attr(h, "location") or None,
                    tooltip=attr(h, "tooltip") or None,
                )
            )

    # ── conditionalFormatting ──
    for block in children(root, "conditionalFormatting"):
        sqref = attr(block, "sqref")
        cf = RawCfBlock(sqref=sqref)
        for rule_el in children(block, "cfRule"):
            formulas = [text_of(f) for f in children(rule_el, "formula")]
            cf.rules.append(
                RawCfRule(
                    type=attr(rule_el, "type") or "expression",
                    priority=attr_int(rule_el, "priority", 0) or 0,
                    operator=attr(rule_el, "operator") or None,
                    formulas=formulas,
                    dxf_id=attr_int(rule_el, "dxfId"),
                    stop_if_true=attr_bool(rule_el, "stopIfTrue") is True,
                )
            )
        if cf.rules:
            sheet.cf_blocks.append(cf)

    # ── autoFilter ──
    af = child(root, "autoFilter")
    if af is not None:
        sheet.auto_filter = attr(af, "ref") or None

    # ── dataValidations ──
    dv = child(root, "dataValidations")
    if dv is not None:
        for d in children(dv, "dataValidation"):
            sqref = attr(d, "sqref")
            if sqref:
                sheet.data_validation_sqrefs.append(sqref)

    # ── drawing ──
    drawing = child(root, "drawing")
    if drawing is not None:
        sheet.drawing_rid = attr(drawing, "id") or None  # r:id
    legacy = child(root, "legacyDrawing")
    if legacy is not None:
        sheet.legacy_drawing_rid = attr(legacy, "id") or None

    return sheet


def _parse_cell(c_el, row_idx_1based: int) -> Optional[RawCell]:
    ref = attr(c_el, "r")
    row = col = None
    if ref:
        from ..core import a1_to_rc

        try:
            rc = a1_to_rc(ref)
            row, col = rc.row, rc.col
        except ValueError:
            return None
    else:
        # 无 r 属性的畸形文件：按出现顺序推导列号（保守处理，行号用 row@r）
        return None

    t = attr(c_el, "t") or "n"
    v_el = child(c_el, "v")
    v_text = text_of(v_el) if v_el is not None else None

    cell = RawCell(row=row, col=col, t=t, v=v_text)

    s = attr_int(c_el, "s")
    cell.s = s

    f_el = child(c_el, "f")
    if f_el is not None:
        cell.formula = text_of(f_el) or None
        if (attr(f_el, "t") or "") == "shared":
            cell.shared_si = attr(f_el, "si") or None
            cell.shared_master = bool(cell.formula)

    if t == "inlineStr":
        is_el = child(c_el, "is")
        if is_el is not None:
            runs = []
            for r in children(is_el, "r"):
                t_el = child(r, "t")
                if t_el is not None:
                    from .raw import _rpr_to_font

                    runs.append((text_of(t_el), _rpr_to_font(child(r, "rPr"))))
            cell.is_runs = runs or None
            if not runs:
                t_el = child(is_el, "t")
                cell.v = text_of(t_el) if t_el is not None else text_of(is_el)

    return cell
