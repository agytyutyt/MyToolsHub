"""表格渲染 —— IR → HTML 字符串。

对应 TS 版 ``packages/renderer-dom/src/table.ts``（设计文档 6.9、8.2、8.3）。

关键规则（请勿"优化"掉）：
  1. 被合并覆盖的单元格必须**完全不输出 <td>**，输出空 td 会让整行错位
  2. 必须 table-layout:fixed + <colgroup>，否则浏览器会按内容重算列宽，Excel 列宽全失效
  3. 输出的是 cell.w（格式化文本），不是 cell.v
  4. 所有文本经 esc_text 转义
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from ..core import (
    DEFAULT_COL_WIDTH_PX,
    DEFAULT_ROW_HEIGHT_PX,
    CellModel,
    MediaAsset,
    RichRun,
    SheetModel,
    StyleModel,
    WorkbookModel,
    esc_attr,
    esc_text,
    is_safe_image_mime,
    pt_to_px,
)
from ..layout import MergeIndex, SheetMetrics, build_merge_index, compute_metrics, plan_freeze
from ..style import is_date_format
from .css import CJK_FALLBACK, font_family, num_css, style_to_css_decls, vertical_align_wrapper

_RENDER_MAX_ROWS = 200_000


@dataclass
class RenderOptions:
    #: 输出完整 HTML 文档还是片段
    mode: str = "document"          # 'document' | 'fragment'
    #: CSS 用 class（体积小）还是内联 style（邮件 / 严格 CSP 环境）
    css_mode: str = "class"         # 'class' | 'inline'
    #: 是否渲染隐藏工作表
    include_hidden_sheets: bool = False
    #: 是否输出行号列标（V1 仅预留）
    show_headers: bool = False
    #: 单元格渲染上限（超出则截断并提示）
    max_cells: int = 200_000
    #: 是否内联图片为 base64
    inline_images: bool = True
    #: 冻结实现方式
    freeze_mode: str = "sticky"     # 'sticky' | 'none'
    #: 是否附带客户端交互脚本（仅 mode='document' 时内联生效）
    with_runtime: bool = False
    #: CSS 前缀，避免与宿主页面冲突
    css_prefix: str = "xhr"
    #: 只渲染指定工作表（索引或名称）；None 则渲染全部可见表
    sheet: Optional[object] = None
    #: 输出缩进换行，便于人工检查（体积略大）
    pretty: bool = False


DEFAULT_RENDER_OPTIONS = RenderOptions()


class RenderContext:
    """渲染过程中的共享状态（CSS 类去重表就在这里面）。"""

    def __init__(self, model: WorkbookModel, opts: RenderOptions) -> None:
        self.model = model
        self.opts = opts
        self._class_table: Dict[str, int] = {}
        self.class_rules: List[str] = []
        self.stats = {"cells": 0, "truncated": False, "classes": 0}

    def class_of(self, decls: List[str]) -> int:
        """声明串 → class 序号；inline 模式返回 -1（调用方改用 style 属性）。"""
        if self.opts.css_mode == "inline":
            return -1
        key = ";".join(decls)
        cid = self._class_table.get(key)
        if cid is None:
            cid = len(self.class_rules)
            self._class_table[key] = cid
            self.class_rules.append(key)
        self.stats["classes"] = len(self.class_rules)
        return cid


@dataclass
class _CellAttrs:
    cls: str = ""
    style: str = ""
    attrs: str = ""


def _round2(n: float) -> float:
    return round(n * 100) / 100


def _round1(n: float) -> float:
    return round(n * 10) / 10


def _num1(n) -> str:
    """去掉尾随 .0 的 px 数值字符串（对齐 JS 数字语义）。"""
    return num_css(_round1(n))


# ─────────────────────────────────────────────────────────────
# 单元格
# ─────────────────────────────────────────────────────────────


def _build_cell_attrs(
    abs_row: int,
    abs_col: int,
    rel_row: int,
    rel_col: int,
    cell: Optional[CellModel],
    merge_index: MergeIndex,
    ctx: RenderContext,
    freeze,
    metrics: SheetMetrics,
    style_table: List[StyleModel],
) -> _CellAttrs:
    classes: List[str] = []
    styles: List[str] = []
    p = ctx.opts.css_prefix

    # ── 样式 ──
    # ⚠️ vendor 补丁（上游 bug）：XML 里不存在的格子没有任何样式，不能落到
    #    style_table[0]——dedup 表按「首次出现顺序」分配 id，[0] 是首个被 intern
    #    的样式而非「默认样式」（例：A1 是蓝底大标题时，整片空白区都会被染蓝，
    #    而 Excel 里这些格子只有网格线）。缺格一律按无样式处理。
    if cell is not None:
        style_id = cell.style_id
        style = style_table[style_id] if 0 <= style_id < len(style_table) else None
    else:
        style = None
    is_date_fmt = bool(style and is_date_format(style.num_fmt.code))
    decls = style_to_css_decls(style, cell_type=cell.type if cell else "s", is_date_fmt=is_date_fmt) if style else []
    cid = ctx.class_of(decls)
    if cid >= 0:
        if decls:
            classes.append(f"{p}-c{cid}")
    elif decls:
        styles.append(";".join(decls))

    # ── 冻结 ──
    # ⚠️ freeze / metrics 的数组下标一律是**相对 usedRange 的索引**，
    #    传绝对行列号会在 usedRange 左上角不为 A1 时整体错位。
    if freeze.active:
        frozen_row = rel_row < freeze.y_split
        frozen_col = rel_col < freeze.x_split
        if frozen_row and frozen_col:
            classes.append(f"{p}-fx")
            styles.append(f"top:{_num1(freeze.row_tops[rel_row] if rel_row < len(freeze.row_tops) else 0)}px")
            styles.append(f"left:{_num1(freeze.col_lefts[rel_col] if rel_col < len(freeze.col_lefts) else 0)}px")
        elif frozen_row:
            classes.append(f"{p}-fr")
            styles.append(f"top:{_num1(freeze.row_tops[rel_row] if rel_row < len(freeze.row_tops) else 0)}px")
        elif frozen_col:
            classes.append(f"{p}-fc")
            styles.append(f"left:{_num1(freeze.col_lefts[rel_col] if rel_col < len(freeze.col_lefts) else 0)}px")
        if frozen_row or frozen_col:
            classes.append(f"{p}-fz")

    # ── 隐藏列 ──
    if rel_col < len(metrics.col_hidden) and metrics.col_hidden[rel_col]:
        classes.append(f"{p}-hid")

    # ── 合并（merges 存的是绝对坐标）──
    attrs = ""
    merge = merge_index.master_of(abs_row, abs_col)
    if merge:
        row_span, col_span = merge_index.spans_of(merge)
        if col_span > 1:
            attrs += f' colspan="{col_span}"'
        if row_span > 1:
            attrs += f' rowspan="{row_span}"'

    return _CellAttrs(cls=" ".join(classes), style=";".join(styles), attrs=attrs)


def cell_content(cell: Optional[CellModel]) -> str:
    """单元格内容：优先富文本，否则转义后的显示文本。"""
    if not cell:
        return ""
    if cell.rich_text:
        return "".join(_run_html(run) for run in cell.rich_text)
    return esc_text(cell.w or "")


def cell_inner(
    sheet: SheetModel,
    cell: Optional[CellModel],
    ctx: RenderContext,
    style_table: List[StyleModel],
) -> str:
    """单元格最终内容 —— 在 cell_content 基础上叠加三类装饰。

    顺序有讲究：
      1. DISPIMG 嵌入图片优先（此时单元格没有文本）
      2. 上下标包裹
      3. 批注角标附在文本之后
      4. 超链接把整体包成 <a>（含角标，这样角标也可点，与 Excel 行为一致）
    """
    if not cell:
        return ""
    p = ctx.opts.css_prefix

    # ── WPS DISPIMG 嵌入图片 ──
    if cell.disp_img_id:
        media_id = ctx.model.cell_image_map.get(cell.disp_img_id)
        asset = _find_media_asset(ctx.model.media, media_id) if media_id else None
        src = _media_src(asset, ctx.opts.inline_images) if asset else None
        if src:
            return f'<img class="{p}-img" src="{esc_attr(src)}" alt="">'
        # 找不到资源时保留可读的占位，不静默丢失信息
        return f'<span class="{p}-img-missing">[图片]</span>'

    inner = cell_content(cell)

    # ── 上下标包裹（见 css.vertical_align_wrapper 说明）──
    style = style_table[cell.style_id] if 0 <= cell.style_id < len(style_table) else None
    if style:
        wrap = vertical_align_wrapper(style)
        if wrap:
            inner = f'<span style="{wrap}">{inner}</span>'

    # ── 批注角标 ──
    if cell.comment_id is not None and 0 <= cell.comment_id < len(sheet.comments):
        cm = sheet.comments[cell.comment_id]
        title = f"{cm.author}:\n{cm.text}" if cm.author else cm.text
        inner += f'<span class="{p}-cm" title="{esc_attr(title)}"></span>'

    # ── 超链接 ──
    if cell.hyperlink_id is not None and 0 <= cell.hyperlink_id < len(sheet.hyperlinks):
        hl = sheet.hyperlinks[cell.hyperlink_id]
        title = f' title="{esc_attr(hl.tooltip)}"' if hl.tooltip else ""
        inner = f'<a href="{esc_attr(hl.target)}" target="_blank" rel="noopener noreferrer"{title}>{inner}</a>'

    return inner


def _find_media_asset(media: List[MediaAsset], asset_id: str) -> Optional[MediaAsset]:
    lower = asset_id.lower()
    for m in media:
        if m.id.lower() == lower:
            return m
    return None


def _media_src(asset: MediaAsset, inline: bool) -> Optional[str]:
    """media → 可直接放进 src 的值（data URL 或外链）。"""
    if asset.url:
        return asset.url
    if not inline or not asset.data_base64:
        return None
    # 纵深防御：即使 parser 层的魔数校验被绕过，这里也绝不允许 svg 进入 data: URL
    if not is_safe_image_mime(asset.mime):
        return None
    return f"data:{asset.mime};base64,{asset.data_base64}"


def _run_html(run: RichRun) -> str:
    text = esc_text(run.text)
    f = run.font
    if not f:
        return text
    decls: List[str] = []
    if f.get("name"):
        decls.append(f"font-family:{font_family(f['name'])}")
    if isinstance(f.get("size_pt"), (int, float)):
        decls.append(f"font-size:{num_css(_round2(pt_to_px(f['size_pt'])))}px")
    if f.get("bold"):
        decls.append("font-weight:700")
    if f.get("italic"):
        decls.append("font-style:italic")
    deco: List[str] = []
    if f.get("strike"):
        deco.append("line-through")
    if f.get("underline"):
        deco.append("underline")
    if deco:
        decls.append("text-decoration:" + " ".join(deco))
    if f.get("color"):
        decls.append(f"color:{f['color']}")
    if f.get("vert_align") == "superscript":
        decls.append("vertical-align:super")
    if f.get("vert_align") == "subscript":
        decls.append("vertical-align:sub")
    if not decls:
        return text
    return f'<span style="{";".join(decls)}">{text}</span>'


# ─────────────────────────────────────────────────────────────
# 工作表
# ─────────────────────────────────────────────────────────────


@dataclass
class SheetRenderResult:
    html: str
    #: 该表实际渲染的单元格数
    cell_count: int
    truncated: bool


def render_sheet(sheet: SheetModel, sheet_index: int, ctx: RenderContext, visible: bool) -> SheetRenderResult:
    model = ctx.model
    opts = ctx.opts
    p = opts.css_prefix
    style_table = model.style_table

    r0, c0 = sheet.used_range.r0, sheet.used_range.c0
    r1, c1 = sheet.used_range.r1, sheet.used_range.c1
    col_count = max(1, c1 - c0 + 1)
    row_count = max(1, r1 - r0 + 1)

    merge_index = build_merge_index(sheet.merges)
    metrics = compute_metrics(sheet, col_count, min(row_count, _RENDER_MAX_ROWS))
    freeze = (
        plan_freeze(sheet.views, metrics.row_offsets, metrics.col_offsets)
        if opts.freeze_mode == "sticky"
        else _no_freeze()
    )

    # 截断：按 max_cells 反推行数
    max_rows_by_cells = max(1, opts.max_cells // col_count)
    render_row_count = min(row_count, max_rows_by_cells)
    truncated = render_row_count < row_count
    if truncated:
        ctx.stats["truncated"] = True

    rows_by_index = {row.index: row for row in sheet.rows}

    out: List[str] = []
    nl = "\n" if opts.pretty else ""

    # 自动筛选：只有范围首行需要画下拉箭头
    af = sheet.auto_filter
    filter_row = af.r0 if af else -1
    filter_c0, filter_c1 = (af.c0, af.c1) if af else (0, -1)

    # 缩放：Excel 的 zoomScale 只影响显示比例，用 CSS transform 近似
    zoom = sheet.views.zoom if sheet.views.zoom and sheet.views.zoom != 100 else 0

    hidden_attr = "" if visible else " hidden"
    rtl = ' dir="rtl"' if sheet.views.right_to_left else ""
    out.append(
        f'<div class="{p}-sheet" data-sheet="{sheet_index}" data-name="{esc_attr(sheet.name)}"{hidden_attr}{rtl}>'
    )
    if zoom:
        out.append(
            f'<div class="{p}-zoom" style="transform:scale({zoom / 100:.4f});transform-origin:top left">'
        )
    out.append(f'<div class="{p}-viewport">')
    sticky = f" {p}--sticky" if freeze.active else ""
    nogrid = "" if sheet.views.show_grid_lines else f" {p}--nogrid"
    out.append(
        f'<table class="{p}-t{sticky}{nogrid}" style="table-layout:fixed;width:{metrics.total_width}px">'
    )

    # 列宽：必须输出所有列，少一列后面的列宽全部错位
    out.append("<colgroup>")
    for c in range(col_count):
        w = 0 if (c < len(metrics.col_hidden) and metrics.col_hidden[c]) else (
            metrics.col_widths[c] if c < len(metrics.col_widths) else DEFAULT_COL_WIDTH_PX
        )
        out.append(f'<col style="width:{w}px">')
    out.append("</colgroup>")

    out.append("<tbody>")

    rendered = 0
    for i in range(render_row_count):
        r = r0 + i
        row = rows_by_index.get(r)
        h = metrics.row_heights[i] if i < len(metrics.row_heights) else (row.height_px if row and row.height_px else DEFAULT_ROW_HEIGHT_PX)
        hidden = (metrics.row_hidden[i] if i < len(metrics.row_hidden) else False) or (row.hidden if row else False)
        out.append('<tr style="display:none">' if hidden else f'<tr style="height:{h}px">')

        for j in range(col_count):
            c = c0 + j
            # 被合并覆盖 → 完全跳过，不输出 <td>
            if merge_index.is_covered(r, c):
                continue

            cell = row.cells.get(c) if row else None
            ca = _build_cell_attrs(r, c, i, j, cell, merge_index, ctx, freeze, metrics, style_table)

            content = cell_inner(sheet, cell, ctx, style_table)
            # 自动筛选箭头
            if af and r == filter_row and filter_c0 <= c <= filter_c1:
                content += f'<span class="{p}-filter" aria-hidden="true"></span>'

            class_attr = f' class="{ca.cls}"' if ca.cls else ""
            style_attr = f' style="{ca.style}"' if ca.style else ""
            out.append(f"<td{class_attr}{style_attr}{ca.attrs}>{content}</td>")
            rendered += 1
        out.append("</tr>")

    out.append("</tbody>")
    out.append("</table>")

    # ── 浮动图片层 ──
    drawings_html = _render_drawings(sheet, ctx, metrics, c0, r0)
    if drawings_html:
        out.append(drawings_html)

    if truncated:
        out.append(
            f'<div class="{p}-truncated">仅预览前 {render_row_count} 行（共 {row_count} 行），完整内容请下载原文件</div>'
        )

    out.append("</div>")  # viewport
    if zoom:
        out.append("</div>")  # zoom 包裹层
    out.append("</div>")  # sheet

    ctx.stats["cells"] += rendered
    return SheetRenderResult(html=nl.join(out), cell_count=rendered, truncated=truncated)


def _no_freeze():
    from ..layout import FreezePlan

    return FreezePlan()


# ─────────────────────────────────────────────────────────────
# 浮动图片
# ─────────────────────────────────────────────────────────────


def _render_drawings(sheet: SheetModel, ctx: RenderContext, metrics: SheetMetrics, c0: int, r0: int) -> str:
    """渲染浮动图片层。

    定位算法（⚠️ 锚点里的 dx/dy 在 IR 中是 0~1 的**比例**，
    解析层已把 EMU 除以 9525 再除以列宽，这里直接乘列宽即可）：
      twoCellAnchor：left = 起始列偏移 + dx×列宽；right = 结束列偏移 + dx×列宽
      oneCellAnchor：left = 起始列偏移，尺寸用 ext 的像素值
    """
    drawings = sheet.drawings or []
    if not drawings:
        return ""
    p = ctx.opts.css_prefix

    imgs: List[str] = []
    for d in drawings:
        if d.kind != "image" or not d.media_id:
            if d.kind != "image":
                imgs.append('<div style="position:absolute;left:0;top:0;width:0;height:0"></div>')
            continue
        asset = _find_media_asset(ctx.model.media, d.media_id)
        src = _media_src(asset, ctx.opts.inline_images) if asset else None
        if not src:
            continue

        # 相对索引：metrics 的数组下标是相对于 usedRange 的
        def col_w(rel: int) -> float:
            return metrics.col_widths[rel] if 0 <= rel < len(metrics.col_widths) else sheet.default_col_width_px

        def row_h(rel: int) -> float:
            return metrics.row_heights[rel] if 0 <= rel < len(metrics.row_heights) else sheet.default_row_height_px

        def col_x(rel: int) -> float:
            return metrics.col_offsets[rel] if 0 <= rel < len(metrics.col_offsets) else 0

        def row_y(rel: int) -> float:
            return metrics.row_offsets[rel] if 0 <= rel < len(metrics.row_offsets) else 0

        left = top = width = height = 0.0

        if d.anchor:
            frm = d.anchor["from"]
            to = d.anchor["to"]
            fr, fc = frm["row"] - r0, frm["col"] - c0
            tr, tc = to["row"] - r0, to["col"] - c0
            if fr < 0 or fc < 0:
                continue
            left = col_x(fc) + frm["dx"] * col_w(fc)
            top = row_y(fr) + frm["dy"] * row_h(fr)
            right = col_x(tc) + to["dx"] * col_w(tc)
            bottom = row_y(tr) + to["dy"] * row_h(tr)
            width = max(1, right - left)
            height = max(1, bottom - top)
        elif d.one_cell:
            fr, fc = d.one_cell["row"] - r0, d.one_cell["col"] - c0
            if fr < 0 or fc < 0:
                continue
            left = col_x(fc)
            top = row_y(fr)
            width = d.one_cell["width_px"]
            height = d.one_cell["height_px"]
        else:
            continue

        imgs.append(
            f'<img src="{esc_attr(src)}" alt="" style="left:{_num1(left)}px;top:{_num1(top)}px;width:{_num1(width)}px;height:{_num1(height)}px">'
        )

    if not imgs:
        return ""
    return f'<div class="{p}-drawings">{"".join(imgs)}</div>'
