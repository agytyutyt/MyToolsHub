"""parser_xlsx 主编排 —— xlsx → WorkbookModel。

对应 TS 版 ``packages/parser-xlsx/src/exceljs-adapter.ts``。
TS 版用 ExcelJS 解析结构；Python 版没有等价库，按《设计文档》6.3 的
「自研解析器」路线用 stdlib(zipfile + ElementTree)实现同样的职责：

  · ExcelJS 的结构解析   → sheet_parser / styles_parser（含 applyXxx 继承）
  · ExcelJS 的富文本     → sharedStrings <r> runs
  · 我们负责的部分不变   → 主题色、数字格式、IR 规范化、限额与降级

三个必须守住的点（与 TS 版一致）：
  1. cell.w 一定取自格式化后的显示文本，绝不用 str(value)
  2. 主题色由 raw.py 读 theme1.xml 得到，不依赖解析库
  3. 任何单表异常都不影响其它表（try/except 到 sheet 粒度）
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from ..core import (
    DEFAULT_COL_WIDTH_PX,
    DEFAULT_ROW_HEIGHT_PX,
    LIMITS,
    XhrError,
    a1_range_to_rect,
    a1_to_rc,
    CellModel,
    ColModel,
    CommentModel,
    CfRuleModel,
    FreezeModel,
    HyperlinkModel,
    MediaAsset,
    MetaInfo,
    Rect,
    RichRun,
    RowModel,
    SheetModel,
    SheetViewModel,
    StyleModel,
    ThemeInfo,
    WorkbookModel,
    col_width_to_px,
    row_height_to_px,
)
from ..style import (
    DEFAULT_INDEXED_PALETTE,
    StyleContext,
    StyleTable,
    THEME_COLOR_NAMES,
    apply_conditional_formatting,
    create_style_context,
    format_value,
    is_date_format,
    normalize_underline,
    resolve_color,
    resolve_dxf_patch,
    resolve_style,
)
from .advanced import (
    collect_media,
    extract_disp_img_id,
    read_cell_images,
    read_comments,
    read_drawings,
)
from .raw import SharedText, read_custom_num_fmts, read_indexed_colors, read_shared_strings, read_theme, read_workbook_meta, sheet_rels_path
from .sheet_parser import RawCell, RawSheet, parse_worksheet
from .styles_parser import StylesBundle, parse_styles
from .xmlutil import children, parse_xml, read_relationships

_SAFE_URL_RE = re.compile(r"^(https?|mailto|tel|ftp):", re.IGNORECASE)
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


@dataclass
class ParseOptions:
    max_cells_per_sheet: int = LIMITS["MAX_CELLS_PER_SHEET_RENDER"]
    max_cells_total: int = LIMITS["MAX_CELLS_TOTAL"]
    inline_media: bool = True
    max_image_bytes: int = 5 * 1024 * 1024


@dataclass
class ParseInput:
    """ZIP 解包后的条目 + 原始字节（自研解析器只依赖 files，buffer 保留对齐 TS API）。"""

    files: Dict[str, bytes]
    buffer: Optional[bytes] = None
    file_hash: str = ""


# ─────────────────────────────────────────────────────────────
# 入口
# ─────────────────────────────────────────────────────────────


def parse_to_model(input_: ParseInput, options: Optional[ParseOptions] = None) -> WorkbookModel:
    """解析入口：xlsx → WorkbookModel。

    @raises XhrError('CORRUPTED') 当工作簿无法加载时
    """
    opts = options or ParseOptions()
    files = input_.files
    warnings: List[str] = []

    # ① workbook.xml：sheet 列表 + date1904 + rels
    wb_meta = read_workbook_meta(files)
    if not wb_meta["sheets"]:
        raise XhrError("CORRUPTED", "workbook.xml has no sheets")
    wb_rels = read_relationships(files, "xl/_rels/workbook.xml.rels")

    # ② 主题 / 调色板 / 自定义数字格式
    theme_path = next((p for p in wb_rels.values() if p.startswith("xl/theme/")), None)
    theme = read_theme(files, theme_path)
    indexed_palette = read_indexed_colors(files) or list(DEFAULT_INDEXED_PALETTE)
    num_fmts = read_custom_num_fmts(files)
    bundle = parse_styles(files)
    if bundle.num_fmts:
        num_fmts.update(bundle.num_fmts)

    ctx = create_style_context(
        theme_colors=theme.color_scheme,
        indexed=indexed_palette,
        num_fmt_map=num_fmts,
        default_font_name=theme.minor_font,
    )

    # ③ 共享字符串
    shared_strings = read_shared_strings(files)

    # ④ 逐表解析（sheet 粒度 try/except —— 永不因单表崩溃）
    style_table = StyleTable()
    sheets: List[SheetModel] = []
    total_cells = 0
    sheet_infos = wb_meta["sheets"]
    if len(sheet_infos) > LIMITS["MAX_SHEETS"]:
        warnings.append(
            f"工作表数量 {len(sheet_infos)} 超过上限 {LIMITS['MAX_SHEETS']}，仅解析前 {LIMITS['MAX_SHEETS']} 个"
        )
        sheet_infos = sheet_infos[: LIMITS["MAX_SHEETS"]]

    for order, meta in enumerate(sheet_infos):
        name = meta["name"]
        part = wb_rels.get(meta["rid"])
        if not part:
            warnings.append(f"工作表「{name}」缺少部件映射，已跳过")
            continue
        state = meta["state"] if meta["state"] in ("visible", "hidden", "veryHidden") else "visible"
        try:
            raw_sheet = parse_worksheet(files, part)
            sheet, n_cells = _build_sheet(
                raw_sheet, name, order, state, files, wb_rels, shared_strings,
                bundle, style_table, ctx, opts, wb_meta["date1904"], warnings,
            )
            total_cells += n_cells
            sheets.append(sheet)
            if total_cells >= opts.max_cells_total:
                warnings.append(f"单元格总数达到上限 {opts.max_cells_total}，剩余工作表未解析")
                break
        except Exception as e:  # noqa: BLE001
            warnings.append(f"工作表「{name}」解析失败：{type(e).__name__}: {e}")

    # ⑤ 媒体 / WPS DISPIMG
    media = collect_media(files, opts.inline_media, opts.max_image_bytes, warnings)
    cell_image_map = read_cell_images(files)
    for img_name, media_id in cell_image_map.items():
        if not any(m.id.lower() == media_id.lower() for m in media):
            warnings.append(f"DISPIMG 图片 {img_name} 指向的资源 {media_id} 不可用，该单元格将不显示图片")

    stats = {
        "sheetCount": len(sheets),
        "cellCount": total_cells,
        "maxRows": max((s.used_range.r1 + 1 for s in sheets), default=0),
        "maxCols": max((s.used_range.c1 + 1 for s in sheets), default=0),
    }

    return WorkbookModel(
        meta=MetaInfo(
            file_hash=input_.file_hash,
            source_format="xlsx",
            date1904=wb_meta["date1904"],
            warnings=warnings,
            stats=stats,
        ),
        theme=ThemeInfo(
            color_scheme={
                n: (theme.color_scheme[i] if i < len(theme.color_scheme) else "#000000")
                for i, n in enumerate(THEME_COLOR_NAMES)
            },
            major_font=theme.major_font,
            minor_font=theme.minor_font,
        ),
        indexed_palette=indexed_palette,
        style_table=style_table.all(),
        sheets=sheets,
        media=media,
        num_fmt_map=num_fmts,
        cell_image_map=cell_image_map,
    )


def parse_buffer(data: bytes, options: Optional[ParseOptions] = None) -> WorkbookModel:
    """便捷 API：字节流直接解析（内部完成 ingest + 解包）。"""
    from ..ingest import ingest

    ing = ingest(data)
    if not ing.files:
        raise XhrError("UNSUPPORTED_FORMAT", f"parser_xlsx 只处理 zip 容器，收到 {ing.format}")
    return parse_to_model(ParseInput(files=ing.files, buffer=bytes(data)), options)


# ─────────────────────────────────────────────────────────────
# 工作表构建
# ─────────────────────────────────────────────────────────────


def _build_sheet(
    raw: RawSheet,
    name: str,
    order: int,
    state: str,
    files: Dict[str, bytes],
    wb_rels: Dict[str, str],
    shared_strings: List[SharedText],
    bundle: StylesBundle,
    style_table: StyleTable,
    ctx: StyleContext,
    opts: ParseOptions,
    date1904: bool,
    warnings: List[str],
) -> Tuple[SheetModel, int]:
    """RawSheet → SheetModel（对应 TS 的 buildSheet）。"""
    min_r, min_c = 1 << 30, 1 << 30
    max_r, max_c = -1, -1
    cell_count = 0
    truncated = False

    sheet_rels = read_relationships(files, sheet_rels_path(raw.part_path))

    rows: List[RowModel] = []
    raw_rows = [r for r in raw.rows if 0 <= r.index < LIMITS["MAX_ROWS_PER_SHEET"]]
    if len(raw.rows) > len(raw_rows):
        truncated = True

    # 样式缓存：cellXf 下标 → StyleModel（resolve_style 幂等，intern 负责去重）
    style_cache: Dict[int, StyleModel] = {}

    def style_of(s_index: Optional[int]) -> StyleModel:
        key = s_index if s_index is not None else 0
        hit = style_cache.get(key)
        if hit is None:
            raw_style = (
                bundle.cell_xf_styles[key]
                if 0 <= key < len(bundle.cell_xf_styles)
                else (bundle.cell_xf_styles[0] if bundle.cell_xf_styles else {})
            )
            hit = resolve_style(raw_style, ctx)
            style_cache[key] = hit
        return hit

    def has_visual(style: StyleModel) -> bool:
        """只有样式没有值的格子要保留（色块 / 边框底纹）。"""
        f = style.fill
        b = style.border
        return (
            (f.type == "solid" and bool(f.fg_color))
            or f.type == "pattern"
            or f.type == "gradient"
            or bool(b.top or b.bottom or b.left or b.right)
        )

    # ── 合并（先于行解析：覆盖格必须保留在 IR 中）──
    # ⚠️ XML 里存在的覆盖格（<c r="B2" s="1"/>）必须保留：ExcelJS 会为合并区
    #    物化覆盖格；渲染层用 mergeIndex 跳过它们的 <td> 输出。
    merges: List[Rect] = []
    for ref in raw.merges:
        try:
            merges.append(a1_range_to_rect(ref))
        except ValueError:
            warnings.append(f"工作表「{name}」存在非法合并区域 {ref}，已跳过")

    def _is_covered_pos(r: int, c: int) -> bool:
        for m in merges:
            if m.r0 <= r <= m.r1 and m.c0 <= c <= m.c1 and not (r == m.r0 and c == m.c0):
                return True
        return False

    for rr in raw_rows:
        cells: Dict[int, CellModel] = {}
        for rc in rr.cells:
            if rc.col >= LIMITS["MAX_COLS_PER_SHEET"]:
                continue
            if cell_count >= opts.max_cells_per_sheet:
                truncated = True
                break
            style = style_of(rc.s)
            style_id = style_table.intern(style)
            cell = _build_cell(rc, style, style_id, shared_strings, ctx, date1904)
            if cell is None:
                if not has_visual(style) and not _is_covered_pos(rc.row, rc.col):
                    continue
                # 无值但有样式（色块/边框）或处于合并覆盖区 → 保留空格
                cell = CellModel(row=rc.row, col=rc.col, type="empty", v=None, w="", style_id=style_id)
            cells[rc.col] = cell
            cell_count += 1
            min_r = min(min_r, rc.row)
            max_r = max(max_r, rc.row)
            min_c = min(min_c, rc.col)
            max_c = max(max_c, rc.col)
        height_px = row_height_to_px(rr.ht_pt) if rr.ht_pt and rr.ht_pt > 0 else 0
        if not cells and height_px == 0 and not rr.hidden:
            continue
        rows.append(
            RowModel(index=rr.index, height_px=height_px, hidden=rr.hidden, outline_level=rr.outline_level, cells=cells)
        )

    if truncated:
        warnings.append(f"工作表「{name}」超出单元格上限，已截断预览")

    used_range = Rect(0, 0, 0, 0) if max_r < 0 else Rect(min_r, min_c, max_r, max_c)

    # ⚠️ 合并区可能落在 usedRange 之外（openpyxl 的 merge_cells 会把覆盖格从
    #    XML 里删掉，TS 侧由 ExcelJS 物化覆盖格达到同样效果）——
    #    不扩展的话 rowspan/colspan 会溢出表格。
    for m in merges:
        if m.r1 > used_range.r1:
            used_range.r1 = m.r1
        if m.c1 > used_range.c1:
            used_range.c1 = m.c1
    used_range.r1 = min(used_range.r1, LIMITS["MAX_ROWS_PER_SHEET"] - 1)
    used_range.c1 = min(used_range.c1, LIMITS["MAX_COLS_PER_SHEET"] - 1)

    # ── 列宽 / 隐藏（展开 min..max 区间）──
    cols: List[ColModel] = []
    seen_cols: Dict[int, bool] = {}
    for rc in raw.cols:
        for idx in range(rc.min - 1, min(rc.max, LIMITS["MAX_COLS_PER_SHEET"])):
            if idx < 0 or idx in seen_cols:
                continue
            seen_cols[idx] = True
            width_px = col_width_to_px(rc.width) if rc.width and rc.width > 0 else 0
            if width_px == 0 and not rc.hidden and rc.outline_level == 0:
                continue
            cols.append(ColModel(index=idx, width_px=width_px, hidden=rc.hidden, outline_level=rc.outline_level))
    cols.sort(key=lambda c: c.index)

    default_col_width_px = (
        col_width_to_px(raw.default_col_width)
        if raw.default_col_width and raw.default_col_width > 0
        else DEFAULT_COL_WIDTH_PX
    )
    default_row_height_px = (
        row_height_to_px(raw.default_row_height_pt)
        if raw.default_row_height_pt and raw.default_row_height_pt > 0
        else DEFAULT_ROW_HEIGHT_PX
    )

    # ── 视图 / 冻结（⚠️ 只有 state=frozen 的 pane 才是冻结，split 不是）──
    view = raw.view or {}
    pane = view.get("pane") or {}
    freeze: Optional[FreezeModel] = None
    if pane.get("state") in ("frozen", "frozenSplit") and (pane.get("xSplit") or pane.get("ySplit")):
        top_left_row, top_left_col = int(pane.get("ySplit") or 0), int(pane.get("xSplit") or 0)
        if pane.get("topLeftCell"):
            try:
                tl = a1_to_rc(pane["topLeftCell"])
                top_left_row, top_left_col = tl.row, tl.col
            except ValueError:
                pass
        freeze = FreezeModel(
            x_split=int(pane.get("xSplit") or 0),
            y_split=int(pane.get("ySplit") or 0),
            top_left_row=top_left_row,
            top_left_col=top_left_col,
        )
    zoom = view.get("zoomScale")
    views = SheetViewModel(
        show_grid_lines=view.get("showGridLines") is not False,
        show_row_col_headers=view.get("showRowColHeaders") is not False,
        zoom=zoom if isinstance(zoom, int) and zoom > 0 else 100,
        right_to_left=view.get("rightToLeft") is True,
        freeze=freeze,
        tab_selected=view.get("tabSelected") is not False,
    )

    # ── 标签颜色 ──
    tab_color: Optional[str] = None
    if raw.tab_color:
        tab_color = resolve_color(raw.tab_color, ctx.color_ctx, "") or None

    # ── 超链接 ──
    hyperlinks: List[HyperlinkModel] = []
    unsafe_link_seen = False
    for h in raw.hyperlinks:
        if not h.ref or not h.rid:
            continue  # 站内锚点（location，无 r:id）在只读预览里没有意义
        target = _external_target(files, sheet_rels_path(raw.part_path), h.rid)
        if not target:
            continue
        safe = _sanitize_href(target)
        if not safe:
            if not unsafe_link_seen:
                unsafe_link_seen = True
                warnings.append(f"工作表「{name}」存在不允许的链接协议，已按纯文本显示")
            continue
        try:
            rect = a1_range_to_rect(h.ref)
        except ValueError:
            continue
        hyperlinks.append(HyperlinkModel(r0=rect.r0, c0=rect.c0, r1=rect.r1, c1=rect.c1, target=safe, tooltip=h.tooltip))
        hl_index = len(hyperlinks) - 1
        for row in rows:
            if row.index < rect.r0 or row.index > rect.r1:
                continue
            for c_idx in range(rect.c0, rect.c1 + 1):
                cell = row.cells.get(c_idx)
                if cell is not None and cell.hyperlink_id is None:
                    cell.hyperlink_id = hl_index

    # ── 批注（commentsN.xml 经 sheet rels 定位）──
    comments: List[CommentModel] = []
    comments_path = next(
        (t for t in sheet_rels.values() if "comments" in t.lower() and t.endswith(".xml")),
        None,
    )
    if comments_path:
        comment_by_rc: Dict[Tuple[int, int], dict] = {}
        for rec in read_comments(files, comments_path):
            try:
                rc_ = a1_to_rc(rec["ref"].split(":")[0])
            except ValueError:
                continue
            comment_by_rc[(rc_.row, rc_.col)] = rec
        for row in rows:
            for c_idx, cell in row.cells.items():
                rec = comment_by_rc.get((row.index, c_idx))
                if rec:
                    cell.comment_id = len(comments)
                    comments.append(CommentModel(row=row.index, col=c_idx, text=rec["text"], author=rec.get("author")))

    # ── 条件格式（含原生 stopIfTrue；dxf 已从 styles.xml 展开）──
    cf_rules: List[CfRuleModel] = []
    for block in raw.cf_blocks:
        ranges: List[Rect] = []
        for ref in (block.sqref or "").split():
            try:
                ranges.append(a1_range_to_rect(ref))
            except ValueError:
                warnings.append(f"条件格式范围「{ref}」无法解析，已跳过")
        if not ranges:
            continue
        for rule in block.rules:
            patch = None
            if rule.dxf_id is not None and 0 <= rule.dxf_id < len(bundle.dxfs):
                patch = resolve_dxf_patch(bundle.dxfs[rule.dxf_id], ctx)
            cf_rules.append(
                CfRuleModel(
                    type=rule.type,
                    ranges=ranges,
                    priority=rule.priority,
                    operator=rule.operator,
                    formulas=rule.formulas,
                    dxf_id=rule.dxf_id,
                    stop_if_true=rule.stop_if_true,
                    patch=patch,
                )
            )

    # ── 浮动图片（需要列宽/行高把 EMU 偏移换算成格内比例）──
    rows_by_index = {r.index: r for r in rows}
    cols_by_index = {c.index: c for c in cols}

    def col_width_of(c: int) -> int:
        col = cols_by_index.get(c)
        return col.width_px if col and col.width_px > 0 else default_col_width_px

    def row_height_of(r: int) -> int:
        row = rows_by_index.get(r)
        return row.height_px if row and row.height_px > 0 else default_row_height_px

    drawings = []
    if raw.drawing_rid:
        drawing_part = sheet_rels.get(raw.drawing_rid) or wb_rels.get(raw.drawing_rid)
        if drawing_part:
            drawings = read_drawings(files, drawing_part, col_width_of, row_height_of, warnings)

    # ⚠️ 浮动图片不占单元格，可能落在 usedRange 右侧/下方空白区，必须扩展范围
    #    （不扩展的话锚点列/行超出表格，宽高会被算成 0/1px）
    for d in drawings:
        if d.anchor:
            if d.anchor["to"]["row"] > used_range.r1:
                used_range.r1 = d.anchor["to"]["row"]
            if d.anchor["to"]["col"] > used_range.c1:
                used_range.c1 = d.anchor["to"]["col"]
        elif d.one_cell:
            if d.one_cell["row"] > used_range.r1:
                used_range.r1 = d.one_cell["row"]
            if d.one_cell["col"] > used_range.c1:
                used_range.c1 = d.one_cell["col"]

    sheet = SheetModel(
        id=f"sheet{order + 1}",
        name=name or f"Sheet{order + 1}",
        index=order,
        state=state,
        tab_color=tab_color,
        used_range=used_range,
        rows=rows,
        cols=cols,
        default_col_width_px=default_col_width_px,
        default_row_height_px=default_row_height_px,
        merges=merges,
        views=views,
        conditional_formattings=cf_rules,
        drawings=drawings,
        comments=comments,
        hyperlinks=hyperlinks,
        auto_filter=(a1_range_to_rect(raw.auto_filter) if raw.auto_filter else None),
        data_validations=_parse_dv_ranges(raw.data_validation_sqrefs),
    )

    # 条件格式：就地叠加到单元格样式（渲染层无需感知）
    cf_res = apply_conditional_formatting(sheet, style_table, ctx, on_warn=lambda m: warnings.append(f"工作表「{name}」{m}"))
    if cf_res.skipped_rules > 0:
        warnings.append(f"工作表「{sheet.name}」有 {cf_res.skipped_rules} 条条件格式规则暂不支持，已跳过")

    return sheet, cell_count


# ─────────────────────────────────────────────────────────────
# 单元格
# ─────────────────────────────────────────────────────────────


def _build_cell(
    rc: RawCell,
    style: StyleModel,
    style_id: int,
    shared_strings: List[SharedText],
    ctx: StyleContext,
    date1904: bool,
) -> Optional[CellModel]:
    """RawCell → CellModel。

    ★ 铁律：w 必须取格式化后的显示文本（数字与日期一律由我们自己的
    format_value 格式化），绝不能用 str(v) —— 否则日期变成 45352。
    """
    row, col = rc.row, rc.col
    num_fmt_code = style.num_fmt.code

    # ⚠️ WPS DISPIMG：这个公式没有计算意义，引用的是 cellimages.xml 里的嵌入图。
    #    不拦截的话单元格会显示成 "#VALUE!" 而不是图片。
    if rc.formula:
        disp = extract_disp_img_id(rc.formula)
        if disp:
            return CellModel(row=row, col=col, type="empty", v=None, w="", f=rc.formula, style_id=style_id, disp_img_id=disp)

    if rc.t == "s":
        try:
            entry = shared_strings[int((rc.v or "").strip())]
        except (ValueError, IndexError):
            return None
        if entry.runs:
            runs = [RichRun(text=text, font=_rich_font(rpr, ctx)) for text, rpr in entry.runs]
            return CellModel(row=row, col=col, type="s", v=entry.plain, w=entry.plain, rich_text=runs, style_id=style_id)
        return CellModel(row=row, col=col, type="s", v=entry.plain, w=entry.plain, style_id=style_id)

    if rc.t == "inlineStr":
        if rc.is_runs:
            plain = ""
            runs = []
            for text, rpr in rc.is_runs:
                plain += text
                runs.append(RichRun(text=text, font=_rich_font(rpr, ctx)))
            return CellModel(row=row, col=col, type="s", v=plain, w=plain, rich_text=runs, style_id=style_id)
        text = rc.v or ""
        return CellModel(row=row, col=col, type="s", v=text, w=text, style_id=style_id)

    if rc.t == "b":
        val = (rc.v or "").strip() in ("1", "true", "TRUE")
        return CellModel(row=row, col=col, type="b", v=val, w="TRUE" if val else "FALSE", style_id=style_id)

    if rc.t == "e":
        err = (rc.v or "").strip() or "#VALUE!"
        return CellModel(row=row, col=col, type="e", v=err, w=err, style_id=style_id)

    if rc.t == "str":
        text = rc.v or ""
        return CellModel(row=row, col=col, type="str", v=text, w=text, f=rc.formula, style_id=style_id)

    if rc.t == "d" and rc.v:
        serial = _iso_to_serial(rc.v.strip())
        if serial is not None:
            return CellModel(
                row=row, col=col, type="d", v=serial,
                w=format_value(serial, num_fmt_code, date1904).text,
                f=rc.formula, style_id=style_id,
            )

    # 数字（含日期序列号）
    v_text = (rc.v or "").strip() if rc.v is not None else ""
    if v_text == "":
        # 公式但无缓存值 → 显示为空
        if rc.formula:
            return CellModel(row=row, col=col, type="empty", v=None, w="", f=rc.formula, style_id=style_id)
        return None
    try:
        num = float(v_text)
    except ValueError:
        return None
    w = format_value(num, num_fmt_code, date1904).text
    ttype = "d" if is_date_format(num_fmt_code) else "n"
    return CellModel(row=row, col=col, type=ttype, v=num, w=w, f=rc.formula, style_id=style_id)


def _rich_font(rpr: Optional[dict], ctx: StyleContext) -> Optional[dict]:
    """富文本 run 的字体属性 → Partial 字典（不给 styleId，避免污染全局样式表）。"""
    if not rpr:
        return None
    out: dict = {}
    if isinstance(rpr.get("name"), str) and rpr["name"]:
        out["name"] = rpr["name"]
    size = rpr.get("size")
    if isinstance(size, (int, float)) and size > 0:
        out["size_pt"] = size
    if rpr.get("bold") is True:
        out["bold"] = True
    if rpr.get("italic") is True:
        out["italic"] = True
    u = normalize_underline(rpr.get("underline"))
    if u:
        out["underline"] = u
    if rpr.get("strike") is True:
        out["strike"] = True
    color = resolve_color(rpr.get("color"), ctx.color_ctx, "")
    if color:
        out["color"] = color
    if rpr.get("vertAlign") in ("superscript", "subscript"):
        out["vert_align"] = rpr["vertAlign"]
    return out or None


def _iso_to_serial(text: str) -> Optional[float]:
    """ISO 日期字符串（t='d'）→ 序列号。"""
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    from ..style.date import date_to_serial

    return date_to_serial(dt if dt.tzinfo else dt.replace(tzinfo=None))


def _sanitize_href(target: str) -> Optional[str]:
    """超链接协议白名单：http/https/mailto/tel/ftp，其余降级为纯文本。

    ⚠️ javascript: / data: / vbscript: 是 XSS 的经典载体，必须挡掉。
    """
    t = (target or "").strip()
    if not t or t.startswith("#"):
        return None
    if not _SAFE_URL_RE.match(t):
        return None
    return t


def _external_target(files: Dict[str, bytes], rels_path: str, rid: str) -> Optional[str]:
    """从 rels XML 原文里取目标（External 与内部目标都要；hyperlink 只用 External）。"""
    root = parse_xml(files.get(rels_path))
    if root is None:
        return None
    for rel in children(root, "Relationship"):
        if rel.get("Id") == rid:
            if (rel.get("TargetMode") or "").lower() == "external":
                return rel.get("Target")
            return None
    return None


def _parse_dv_ranges(sqrefs: List[str]) -> List[Rect]:
    out: List[Rect] = []
    for sqref in sqrefs:
        for ref in sqref.split():
            try:
                out.append(a1_range_to_rect(ref))
            except ValueError:
                continue
    return out
