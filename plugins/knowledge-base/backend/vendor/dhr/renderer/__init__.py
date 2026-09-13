"""DHR 渲染器 —— IR → HTML 字符串（T-606 / T-803 / T-A02..A05）。

纯函数：输入 DocumentModel + RenderOptions，输出字符串。
流式（flow）为默认模式；分页（paged）按锚点切块（paginate.py）。

列表渲染策略（设计文档 6.11，重要）：不要用原生 <ul>/<ol> + list-style 承载
Word 编号 —— 中文编号、符号字体、图片项目符号 CSS 表达不了。统一渲染为
`<p class="dhr-li">` + 自绘 marker（悬挂缩进）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional

from xhr.core import esc, esc_text

from ..model import (
    DocumentModel,
    DrawingModel,
    FieldModel,
    FurnitureModel,
    ListMarker,
    ParagraphModel,
    RunBreak,
    RunDrawing,
    RunField,
    RunHyphen,
    RunNoteRef,
    RunRuby,
    RunSym,
    RunTab,
    RunText,
    TableModel,
)
from .css import border_spec_css, para_props_to_css, run_props_to_css, sanitize_font_name


@dataclass
class RenderOptions:
    mode: str = "flow"                 # 'flow' | 'paged' | 'print'
    output: str = "document"           # 'document' | 'fragment'
    css_mode: str = "class"            # 'class' | 'inline'
    render_headers: bool = True
    render_footers: bool = True
    render_footnotes: bool = True
    render_endnotes: bool = True
    render_comments: bool = False
    #: 'final' 接受全部 | 'original' 拒绝全部 | 'markup' 带标记
    revision_mode: str = "final"
    media_mode: str = "base64"         # 'base64' | 'url'
    cjk_typography: bool = True
    css_prefix: str = "dhr"
    pagination: Optional[dict] = None
    with_runtime: bool = False

    def __post_init__(self) -> None:
        if self.pagination is None:
            self.pagination = {"tolerancePx": 2, "maxPasses": 1000}


DEFAULT_OPTIONS = RenderOptions()


@dataclass
class RenderResult:
    html: str
    class_count: int


def sanitize_prefix(prefix: object) -> str:
    t = re.sub(r"[^A-Za-z0-9_-]", "", prefix) if isinstance(prefix, str) else ""
    return t or "dhr"


class _Ctx:
    def __init__(self, model: DocumentModel, opts: RenderOptions) -> None:
        self.model = model
        self.opts = opts
        self.p = sanitize_prefix(opts.css_prefix)
        self.class_table: dict = {}
        self.class_rules: List[str] = []

    def class_of(self, decls: List[str]) -> int:
        if self.opts.css_mode == "inline":
            return -1
        key = ";".join(decls)
        cid = self.class_table.get(key)
        if cid is None:
            cid = len(self.class_rules)
            self.class_table[key] = cid
            self.class_rules.append(key)
        return cid

    def media_src(self, media_id: Optional[str]) -> Optional[str]:
        if not media_id:
            return None
        for m in self.model.media:
            if m["id"] == media_id:
                if self.opts.media_mode != "base64" or not m.get("data_base64"):
                    return None
                mime = m.get("mime") or "image/png"
                if mime == "image/svg+xml":
                    return None  # SVG 可携带脚本，永不内联
                return f"data:{mime};base64,{m['data_base64']}"
        return None


# ─────────────────────────────────────────────────────────────
# Run 渲染
# ─────────────────────────────────────────────────────────────


def _open_link(run) -> str:
    link = getattr(run, "_link", None)
    if link is None:
        return ""
    if link.href:
        return f'<a href="{esc(link.href)}" target="_blank" rel="noopener noreferrer">'
    if link.anchor:
        return f'<a href="#{esc(link.anchor)}">'
    return ""


def _run_html(run, ctx: _Ctx) -> str:
    p = ctx.p
    kind = run.kind
    link_open = _open_link(run)
    link_close = "</a>" if link_open else ""
    rev = getattr(run, "_rev", None)
    rev_open = rev_close = ""
    if rev == "ins" and ctx.opts.revision_mode == "markup":
        rev_open, rev_close = '<ins style="text-decoration:underline;color:#2E7D32">', "</ins>"
    elif rev == "del" and ctx.opts.revision_mode == "markup":
        rev_open, rev_close = '<del style="text-decoration:line-through;color:#C62828">', "</del>"

    if kind == "text":
        if run.props is not None and run.props.vanish:
            return ""  # 隐藏文字跳过
        inner = esc_text(run.text)
        if rev_open:
            inner = rev_open + inner + rev_close
        if run.props is None:
            return link_open + inner + link_close
        decls = run_props_to_css(run.props)
        if not decls:
            return link_open + inner + link_close
        cid = ctx.class_of(decls)
        if cid >= 0:
            return f'{link_open}<span class="{p}-c{cid}">{inner}</span>{link_close}'
        return f'{link_open}<span style="{";".join(decls)}">{inner}</span>{link_close}'
    if kind == "tab":
        return f'{link_open}<span class="{p}-tab"></span>{link_close}'
    if kind == "break":
        if run.break_type == "page":
            return f'{link_open}<div class="{p}-pagebreak"></div>{link_close}'
        if run.break_type == "column":
            return f'{link_open}<br class="{p}-colbreak">{link_close}'
        return f"{link_open}<br>{link_close}"
    if kind == "sym":
        font = sanitize_font_name(run.font)
        style = f'font-family:"{font}"' if font else ""
        return f'{link_open}<span style="{style}">{esc(run.char)}</span>{link_close}'
    if kind == "noBreakHyphen":
        return f'{link_open}<span class="{p}-nbhy">&#8209;</span>{link_close}'
    if kind == "softHyphen":
        return f"{link_open}&#173;{link_close}"
    if kind == "drawing":
        return f"{link_open}{_drawing_html(run.drawing, ctx)}{link_close}"
    if kind == "field":
        return f"{link_open}{_field_html(run.field, ctx)}{link_close}"
    if kind == "footnoteRef":
        return f'<sup class="{p}-noteref"><a href="#{p}-fn{run.note_id}">{run.note_id}</a></sup>'
    if kind == "endnoteRef":
        return f'<sup class="{p}-noteref"><a href="#{p}-en{run.note_id}">{run.note_id}</a></sup>'
    if kind == "ruby":
        return f'<ruby>{esc_text(run.base)}<rt>{esc_text(run.rt)}</rt></ruby>'
    return ""


def _drawing_html(d: DrawingModel, ctx: _Ctx) -> str:
    p = ctx.p
    src = ctx.media_src(d.media_id)
    if d.kind == "image" and src:
        style = ""
        if d.width_px:
            style += f"width:{d.width_px}px;"
        if d.height_px:
            style += f"height:{d.height_px}px;"
        if d.crop:
            style += "object-fit:cover;"  # 裁剪 V1 近似
        return f'<img class="{p}-img" src="{esc(src)}" alt="{esc(d.alt_text or "")}" style="{style}" loading="lazy">'
    if d.kind == "chart":
        return f'<span class="{p}-placeholder">[[图表（预览版不支持）]]</span>'
    if d.kind == "image":
        return f'<span class="{p}-placeholder">[[图片不可用]]</span>'
    return f'<span class="{p}-placeholder">[[形状]]</span>'


def _field_html(f: FieldModel, ctx: _Ctx) -> str:
    p = ctx.p
    cached_runs = f.cached_runs or []
    cached_html = "".join(_run_html(r, ctx) for r in cached_runs)
    if f.type == "HYPERLINK":
        if f.target:
            return f'<a href="{esc(f.target)}" target="_blank" rel="noopener noreferrer">{cached_html}</a>'
        return cached_html
    if f.type in ("PAGE", "NUMPAGES", "SECTION"):
        if cached_html:
            return f'<span class="{p}-field" data-field="{f.type}">{cached_html}</span>'
        return '<span class="dhr-field">1</span>'
    if f.type in ("REF", "PAGEREF", "NOTEREF"):
        m = re.search(r"(?:REF|PAGEREF|NOTEREF)\s+(\S+)", f.instruction or "")
        anchor = m.group(1) if m else ""
        body = cached_html or "1"
        return f'<a href="#{esc(anchor)}">{body}</a>'
    if f.type == "TOC":
        # 由 headingLevel 重新生成目录（比域缓存准，设计文档 6.8）
        rows = []
        for item in ctx.model.toc:
            indent = (item["level"] - 1) * 18
            rows.append(
                f'<div class="{p}-toc-line" style="margin-left:{indent}px">'
                f'<a href="#{esc(item["anchor"])}">{esc_text(item["text"])}</a></div>'
            )
        return f'<div class="{p}-toc">{"".join(rows)}</div>'
    if cached_html:
        return cached_html
    return ""


# ─────────────────────────────────────────────────────────────
# 段落 / 列表 / 表格渲染
# ─────────────────────────────────────────────────────────────


def _marker_html(marker: ListMarker, ctx: _Ctx) -> str:
    p = ctx.p
    if marker.image_media_id:
        src = ctx.media_src(marker.image_media_id)
        if src:
            return f'<img class="{p}-marker-img" src="{esc(src)}" alt="">'
    text = esc(marker.text)
    if marker.props:
        decls = run_props_to_css(marker.props)
        if decls:
            cid = ctx.class_of(decls)
            if cid >= 0:
                return f'<span class="{p}-marker {p}-c{cid}">{text}</span>'
            return f'<span class="{p}-marker" style="{";".join(decls)}">{text}</span>'
    return f'<span class="{p}-marker">{text}</span>'


def _para_html(para: ParagraphModel, ctx: _Ctx, index: int) -> str:
    p = ctx.p
    decls = para_props_to_css(para.props, ctx.model.meta.get("defaultTabStopPx") or 48.0)
    cid = ctx.class_of(decls) if decls else -1

    inner = "".join(_run_html(r, ctx) for r in para.runs)
    if not inner:
        inner = "<br>"  # 空段落保持占一行

    if para.list_marker is not None:
        marker = para.list_marker
        pad = marker.indent_px or 36.0
        marker_html = _marker_html(marker, ctx) if (marker.text or marker.image_media_id) else ""
        content = f'{marker_html}<span class="{p}-li-text">{inner}</span>'
        li_style = f"padding-left:{pad:.0f}px;text-indent:-{pad:.0f}px"
        style_attr = f' style="{li_style}'
        if cid >= 0 and decls:
            style_attr += ";" + ";".join(decls)
        style_attr += '"'
        return f'<p class="{p}-li" role="listitem" data-marker="{esc(marker.text)}"{style_attr}>{content}</p>'

    tag = f"h{para.heading_level}" if para.heading_level else "p"
    cls_attr = f' class="{p}-c{cid}"' if cid >= 0 and decls else ""
    style_attr = f' style="{";".join(decls)}"' if (cid < 0 and decls) else ""
    anchor = f' id="{p}-block-{index}"'
    return f"<{tag}{cls_attr}{style_attr}{anchor}>{inner}</{tag}>"


def _cell_html(cell, ctx: _Ctx, tag: str) -> str:
    p = ctx.p
    decls: List[str] = []
    if cell.shd_fill:
        decls.append(f"background-color:{cell.shd_fill}")
    if cell.v_align == "center":
        decls.append("vertical-align:middle")
    elif cell.v_align == "bottom":
        decls.append("vertical-align:bottom")
    if cell.borders:
        b = cell.borders
        for side in ("top", "left", "bottom", "right"):
            spec = getattr(b, side)
            if spec and spec.style not in ("nil", "none"):
                decls.append(f"border-{side}:{border_spec_css(spec)}")
    if cell.vertical:
        decls.append("writing-mode:vertical-rl")
    if cell.no_wrap:
        decls.append("white-space:nowrap")
    attrs = ""
    if cell.colspan > 1:
        attrs += f' colspan="{cell.colspan}"'
    if cell.rowspan > 1:
        attrs += f' rowspan="{cell.rowspan}"'
    style_attr = f' style="{";".join(decls)}"' if decls else ""
    inner_blocks = []
    for i, b in enumerate(cell.blocks):
        if b.kind == "paragraph":
            inner_blocks.append(_para_html(b, ctx, i))
        else:
            inner_blocks.append(_table_html(b, ctx))
    content = "".join(inner_blocks) or "<p></p>"
    return f"<{tag}{attrs}{style_attr}>{content}</{tag}>"


def _table_html(tbl: TableModel, ctx: _Ctx) -> str:
    p = ctx.p
    props = tbl.props
    decls: List[str] = []
    if props.width_pct is not None:
        decls.append(f"width:{props.width_pct:g}%")
    elif props.width_px:
        decls.append(f"width:{props.width_px:.0f}px")
    if props.layout == "fixed":
        decls.append("table-layout:fixed")
    if props.align == "center":
        decls.append("margin-left:auto;margin-right:auto")
    elif props.align == "right":
        decls.append("margin-left:auto")
    if props.indent_px:
        decls.append(f"margin-left:{props.indent_px:.0f}px")
    if props.shd_fill:
        decls.append(f"background-color:{props.shd_fill}")
    b = props.borders
    if b:
        for side in ("top", "left", "bottom", "right"):
            spec = getattr(b, side)
            if spec and spec.style not in ("nil", "none"):
                decls.append(f"border-{side}:{border_spec_css(spec)}")
    cid = ctx.class_of(decls) if decls else -1
    cls_attr = f' class="{p}-t {p}-c{cid}"' if decls else f' class="{p}-t"'

    out: List[str] = [f"<table{cls_attr}>"]
    if props.grid:
        out.append("<colgroup>")
        for w in props.grid:
            out.append(f'<col style="width:{w:.0f}px">')
        out.append("</colgroup>")
    cm = props.cell_margin
    if any(cm.get(k) for k in ("top", "left", "bottom", "right")):
        out.append(
            f'<style>.{p}-t td,.{p}-t th{{padding:{cm.get("top", 0):.0f}px {cm.get("right", 8):.0f}px '
            f'{cm.get("bottom", 0):.0f}px {cm.get("left", 8):.0f}px}}</style>'
        )

    header_rows = [r for r in tbl.rows if r.is_header]
    body_rows = [r for r in tbl.rows if not r.is_header]
    # ⚠️ w:tblHeader 行放进 <thead>（打印时自动重复，设计文档 7.3）
    if header_rows:
        out.append("<thead>")
        for row in header_rows:
            out.append(_row_html(row, ctx))
        out.append("</thead>")
    if body_rows or not header_rows:
        out.append("<tbody>")
        for row in (body_rows if header_rows else tbl.rows):
            out.append(_row_html(row, ctx))
        out.append("</tbody>")
    out.append("</table>")
    return "".join(out)


def _row_html(row, ctx: _Ctx) -> str:
    style = ""
    if row.height_px:
        if row.height_rule == "exact":
            style = f' style="height:{row.height_px:.0f}px;overflow:hidden"'
        else:
            style = f' style="height:{row.height_px:.0f}px"'
    cells = "".join(_cell_html(c, ctx, "th" if row.is_header else "td") for c in row.cells)
    return f"<tr{style}>{cells}</tr>"


# ─────────────────────────────────────────────────────────────
# 家具（页眉页脚 / 脚注尾注 / 批注）
# ─────────────────────────────────────────────────────────────


def _furniture_html(fm: FurnitureModel, ctx: _Ctx) -> str:
    parts = []
    for i, b in enumerate(fm.blocks):
        if b.kind == "paragraph":
            parts.append(_para_html(b, ctx, i))
        else:
            parts.append(_table_html(b, ctx))
    return "".join(parts)


def _notes_html(notes: List[FurnitureModel], kind: str, ctx: _Ctx) -> str:
    p = ctx.p
    if not notes:
        return ""
    title = "脚注" if kind == "footnote" else "尾注"
    items = []
    for fm in notes:
        body = "".join(
            _para_html(b, ctx, i) if b.kind == "paragraph" else _table_html(b, ctx)
            for i, b in enumerate(fm.blocks)
        )
        suffix = "fn" if kind == "footnote" else "en"
        items.append(f'<li id="{p}-{suffix}{fm.note_id}">{body}</li>')
    return f'<div class="{p}-notes"><div class="{p}-notes-title">{title}</div><ol>{"".join(items)}</ol></div>'


def _pick_hf(model: DocumentModel, sec, page_no: int, kind: str) -> Optional[FurnitureModel]:
    refs = sec.header_refs if kind == "header" else sec.footer_refs
    by_type = {r["type"]: r["part_id"] for r in refs}
    wanted = None
    if sec.title_pg and page_no == 0 and "first" in by_type:
        wanted = by_type["first"]
    elif page_no % 2 == 1 and "even" in by_type:
        wanted = by_type["even"]
    elif "default" in by_type:
        wanted = by_type["default"]
    if not wanted:
        return None
    for fm in model.furniture:
        if fm.type == kind and fm.part_id == wanted:
            return fm
    return None


# ─────────────────────────────────────────────────────────────
# 顶层渲染
# ─────────────────────────────────────────────────────────────


def _base_css(prefix: str) -> str:
    p = prefix
    return f"""
.{p}{{font-family:Calibri,Arial,"Segoe UI",Helvetica,"Microsoft YaHei","微软雅黑",sans-serif;font-size:12pt;color:#111827;background:#fff;line-height:1.36}}
.{p} *{{box-sizing:border-box}}
.{p}-body{{margin:0 auto;padding:32px}}
.{p}-body > :first-child{{margin-top:0}}
.{p}-t{{border-collapse:collapse;margin:8px 0}}
.{p}-t td,.{p}-t th{{border:1px solid #666;padding:2px 8px;vertical-align:top;text-align:left}}
.{p}-t th{{background:#F2F4F7;font-weight:700}}
.{p}-li{{margin:2px 0}}
.{p}-marker{{display:inline-block}}
.{p}-marker-img{{height:1em;vertical-align:-0.15em}}
.{p}-tab{{display:inline-block;width:48px}}
.{p}-pagebreak{{break-after:page;height:0;overflow:hidden;border:0}}
.{p}-colbreak{{break-after:column}}
.{p}-page{{position:relative;overflow:hidden;background:#fff;box-shadow:0 1px 6px rgba(0,0,0,.18);margin:0 auto 16px}}
.{p}-pagebody{{position:relative}}
.{p}-pagehead{{position:absolute;top:0;left:0;right:0;overflow:hidden}}
.{p}-pagefoot{{position:absolute;bottom:0;left:0;right:0;overflow:hidden}}
.{p}-notes{{margin-top:24px;padding-top:8px;border-top:1px solid #D0D5DD;font-size:10pt;color:#475467}}
.{p}-notes-title{{font-weight:700;margin-bottom:4px}}
.{p}-noteref a{{text-decoration:none;color:#1D4ED8}}
.{p}-toc{{margin:8px 0}}
.{p}-toc-line{{margin:2px 0}}
.{p}-toc-line a{{text-decoration:none;color:inherit}}
.{p}-img{{max-width:100%}}
.{p}-placeholder{{display:inline-block;padding:8px 12px;border:1px dashed #98A2B3;border-radius:6px;color:#475467;background:#F9FAFB;font-size:10pt}}
.{p}-warnings{{margin:0 0 16px;padding:8px 12px;font-size:11pt;color:#854F0B;background:#FAEEDA;border-left:3px solid #D97706}}
@media print{{
  .{p}-page{{box-shadow:none;margin:0;break-after:page;overflow:visible}}
  .{p}-body{{padding:0}}
  .{p}-warnings{{display:none}}
}}
""".strip()


def _wrap_list_blocks(blocks_html: List[str], list_class: str) -> List[str]:
    """把连续的列表项 <p class="dhr-li"> 包进 role=list 容器（无障碍）。"""
    out: List[str] = []
    in_list = False
    for html in blocks_html:
        is_li = 'role="listitem"' in html
        if is_li and not in_list:
            out.append(f'<div class="{list_class}" role="list">')
            in_list = True
        elif not is_li and in_list:
            out.append("</div>")
            in_list = False
        out.append(html)
    if in_list:
        out.append("</div>")
    return out


def render_document(model: DocumentModel, options: Optional[RenderOptions] = None) -> RenderResult:
    from ..paginate import paginate

    opts = options or DEFAULT_OPTIONS
    p = sanitize_prefix(opts.css_prefix)
    ctx = _Ctx(model, opts)
    ctx.p = p

    pages = paginate(model, opts)

    warnings_html = ""
    if model.meta.get("warnings"):
        shown = "<br>".join(esc_text(w) for w in model.meta["warnings"][:5])
        more = f"<br>…另有 {len(model.meta['warnings']) - 5} 条" if len(model.meta["warnings"]) > 5 else ""
        warnings_html = f'<div class="{p}-warnings">该文档包含预览不完全支持的特性：<br>{shown}{more}</div>'

    page_htmls: List[str] = []
    flow_blocks: List[str] = []
    for page in pages:
        blocks_html: List[str] = []
        for i, b in enumerate(page["blocks"]):
            if b.kind == "paragraph":
                blocks_html.append(_para_html(b, ctx, i))
            else:
                blocks_html.append(_table_html(b, ctx))
        blocks_html = _wrap_list_blocks(blocks_html, f"{p}-list")
        page_html = "".join(blocks_html)

        if opts.mode == "paged":
            sec = page["props"]
            head_html = foot_html = ""
            if opts.render_headers:
                fm = _pick_hf(model, sec, page["page_no"], "header")
                if fm:
                    head_html = (
                        f'<div class="{p}-pagehead" style="left:{sec.margins["left"]:.0f}px;'
                        f'right:{sec.margins["right"]:.0f}px;top:{sec.margins["header"]:.0f}px">'
                        f'{_furniture_html(fm, ctx)}</div>'
                    )
            if opts.render_footers:
                fm = _pick_hf(model, sec, page["page_no"], "footer")
                if fm:
                    foot_html = (
                        f'<div class="{p}-pagefoot" style="left:{sec.margins["left"]:.0f}px;'
                        f'right:{sec.margins["right"]:.0f}px;bottom:{sec.margins["footer"]:.0f}px">'
                        f'{_furniture_html(fm, ctx)}</div>'
                    )
            body_style = (
                f'padding:{sec.margins["top"]:.0f}px {sec.margins["right"]:.0f}px '
                f'{sec.margins["bottom"]:.0f}px {sec.margins["left"]:.0f}px'
            )
            col_style = f";column-count:{sec.cols}" if sec.cols > 1 else ""
            page_htmls.append(
                f'<div class="{p}-page" style="width:{sec.page_size[0]}px;height:{sec.page_size[1]}px">'
                f"{head_html}"
                f'<div class="{p}-pagebody" style="{body_style}{col_style}">{page_html}</div>'
                f"{foot_html}</div>"
            )
        else:
            flow_blocks.append(page_html)

    notes_html = ""
    if opts.render_footnotes:
        notes_html += _notes_html([fm for fm in model.furniture if fm.type == "footnote"], "footnote", ctx)
    if opts.render_endnotes:
        notes_html += _notes_html([fm for fm in model.furniture if fm.type == "endnote"], "endnote", ctx)
    if opts.render_comments:
        comments = [fm for fm in model.furniture if fm.type == "comment"]
        if comments:
            items = "".join(
                f'<div class="{p}-comment"><b>{esc(fm.author or "批注")}：</b>'
                + "".join(_para_html(b, ctx, i) for i, b in enumerate(fm.blocks))
                + "</div>"
                for fm in comments
            )
            notes_html += f'<div class="{p}-comments">{items}</div>'

    if opts.mode == "paged":
        inner = warnings_html + "".join(page_htmls)
        body_tag = f'<div class="{p}-pages">{inner}</div>'
    else:
        sec = model.sections[-1]["props"] if model.sections else None
        width = sec.content_width_px if sec else 602
        col_style = f";column-count:{sec.cols};column-gap:{sec.col_space_px:.0f}px" if sec and sec.cols > 1 else ""
        heads = [fm for fm in model.furniture if fm.type == "header" and fm.hf_type in ("default", "first")] if opts.render_headers else []
        feet = [fm for fm in model.furniture if fm.type == "footer" and fm.hf_type in ("default", "first")] if opts.render_footers else []
        headers_html = f'<div class="{p}-furniture">{_furniture_html(heads[0], ctx)}</div>' if heads else ""
        footers_html = f'<div class="{p}-furniture">{_furniture_html(feet[0], ctx)}</div>' if feet else ""
        inner = headers_html + warnings_html + "".join(flow_blocks) + notes_html + footers_html
        body_tag = (
            f'<div class="{p}-body" style="max-width:{width + 64}px;margin:0 auto;padding:32px{col_style}">{inner}</div>'
        )

    css_lines: List[str] = [_base_css(p)]
    for i, decls in enumerate(ctx.class_rules):
        if not decls:
            continue
        css_lines.append(f".{p} .{p}-c{i}{{{decls}}}")
    css = "\n".join(css_lines)

    title = (model.meta.get("title") or "文档预览").strip() or "文档预览"
    if opts.output == "document":
        html = (
            '<!DOCTYPE html>\n<html lang="zh-CN">\n<head>\n<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            '<meta name="generator" content="dhr (DOC/DOCX to HTML renderer)">\n'
            f"<title>{esc(title)}</title>\n<style>\n{css}\n</style>\n</head>\n"
            f'<body style="margin:0;padding:16px;background:#F2F4F7">\n<div class="{p}" data-mode="{opts.mode}">\n'
            f"{body_tag}\n</div>\n</body>\n</html>"
        )
    else:
        html = f'<style>\n{css}\n</style>\n<div class="{p}">{body_tag}</div>'

    return RenderResult(html=html, class_count=len(ctx.class_rules))
