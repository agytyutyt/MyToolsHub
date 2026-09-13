"""OOXML 解析器 —— word/document.xml → DocumentModel（T-605 / M1 / M4）。

⚠️ 本文件里每条"坑"注释都对应 HANDOFF-DHR §2 的一节，改动前先对照。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from xhr.core import XhrError
from xhr.parser_xlsx.xmlutil import (
    attr,
    attr_int,
    attr_float,
    children,
    child,
    parse_xml,
    read_relationships,
    text_of,
)

from .model import (
    BorderSpec,
    Borders,
    DocumentModel,
    DrawingModel,
    FieldModel,
    FurnitureModel,
    ListMarker,
    NumberingDef,
    ParaProps,
    ParagraphModel,
    RunBreak,
    RunDrawing,
    RunField,
    RunHyphen,
    RunNoteRef,
    RunProps,
    RunRuby,
    RunSym,
    RunTab,
    RunText,
    SectionProps,
    StyleDef,
    TableProps,
    TableCellModel,
    TableModel,
    TableRowModel,
    TabStop,
)
from .styles import (
    StyleSet,
    heading_level_of,
    parse_ppr,
    parse_rpr,
    resolve_word_color,
)
from .units import DHR_LIMITS, emu_to_px, eighth_pt_to_px, pct_to_percent, src_rect_ratio, twip_to_px, twip_to_pt

#: 超链接协议白名单（设计文档 8.7：http/https/mailto/tel）
_SAFE_URL_RE = re.compile(r"^(https?|mailto|tel):", re.IGNORECASE)


def _sanitize_href(target: Optional[str]) -> Optional[str]:
    t = (target or "").strip()
    if not t or t.startswith("#"):
        return None
    if not _SAFE_URL_RE.match(t):
        return None
    return t


@dataclass
class _Link:
    href: Optional[str] = None
    anchor: Optional[str] = None


class _Ctx:
    """解析上下文。"""

    def __init__(self, files: Dict[str, bytes], opts) -> None:
        self.files = files
        self.opts = opts
        self.warnings: List[str] = []
        self.rels = read_relationships(files, "word/_rels/document.xml.rels")
        self.external_rels = self._read_external_rels()
        self.theme = self._read_theme()
        self.style_set = self._read_styles()
        numbering_el = parse_xml(files.get("word/numbering.xml"))
        numbering_rels = read_relationships(files, "word/_rels/numbering.xml.rels")
        self.numbering: List[NumberingDef] = _parse_numbering_safe(numbering_el, numbering_rels, self.warnings)
        self.settings = self._read_settings()
        self.paragraph_count = 0
        self.table_count = 0
        self.image_count = 0
        self.cell_count = 0
        self._bookmark_seq = 0

    # ── rels ──
    def _read_external_rels(self) -> Dict[str, str]:
        """外部关系（TargetMode=External，如超链接）。"""
        root = parse_xml(self.files.get("word/_rels/document.xml.rels"))
        out: Dict[str, str] = {}
        if root is None:
            return out
        for rel in children(root, "Relationship"):
            if (rel.get("TargetMode") or "").lower() == "external":
                rid = rel.get("Id") or ""
                if rid:
                    out[rid] = rel.get("Target") or ""
        return out

    # ── theme ──
    def _read_theme(self) -> dict:
        import posixpath

        theme_path = next((p for p in self.rels.values() if p.startswith("word/theme/")), None)
        root = parse_xml(self.files.get(theme_path)) if theme_path else None
        colors: List[str] = []
        major = {"latin": "", "eastAsia": ""}
        minor = {"latin": "", "eastAsia": ""}
        if root is not None:
            te = child(root, "themeElements")
            clr = child(te, "clrScheme") if te is not None else None
            if clr is not None:
                for entry in clr:
                    if not isinstance(entry.tag, str):
                        continue
                    srgb = child(entry, "srgbClr")
                    if srgb is not None and srgb.get("val"):
                        colors.append("#" + srgb.get("val").upper())
                        continue
                    sys_el = child(entry, "sysClr")
                    if sys_el is not None:
                        last = sys_el.get("lastClr")
                        colors.append("#" + last.upper() if last else "#000000")
                    else:
                        colors.append("#000000")
            fonts_el = child(te, "fontScheme") if te is not None else None
            if fonts_el is not None:
                for dst, tag in ((major, "majorFont"), (minor, "minorFont")):
                    f_el = child(fonts_el, tag)
                    if f_el is not None:
                        latin = child(f_el, "latin")
                        ea = child(f_el, "ea")
                        dst["latin"] = (latin.get("typeface") if latin is not None else "") or ""
                        dst["eastAsia"] = (ea.get("typeface") if ea is not None else "") or ""
        if len(colors) != 12:
            from .styles import DEFAULT_THEME_COLORS

            colors = list(DEFAULT_THEME_COLORS)
        names = ["dk1", "lt1", "dk2", "lt2", "accent1", "accent2", "accent3", "accent4", "accent5", "accent6", "hlink", "folHlink"]
        return {
            "colorScheme": dict(zip(names, colors)),
            "majorFont": major,
            "minorFont": minor,
        }

    # ── styles ──
    def _read_styles(self) -> StyleSet:
        ss = StyleSet()
        root = parse_xml(self.files.get("word/styles.xml"))
        if root is None:
            return ss
        dd = child(root, "docDefaults")
        if dd is not None:
            rprd = child(child(dd, "rPrDefault"), "rPr")
            r = parse_rpr(rprd, self.theme)
            if r:
                ss.defaults_run = r
            pprd = child(child(dd, "pPrDefault"), "pPr")
            p = parse_ppr(pprd, self.theme)
            if p:
                # 样式默认里的 numPr 私有键无意义，去掉
                for k in ("_num_id", "_num_ilvl"):
                    p.__dict__.pop(k, None)
                ss.defaults_para = p
        for st_el in children(root, "style"):
            st = StyleDef(
                id=st_el.get("styleId") or "",
                type=st_el.get("type") or "paragraph",
            )
            name_el = child(st_el, "name")
            if name_el is not None:
                st.name = name_el.get("val")
            based = child(st_el, "basedOn")
            if based is not None:
                st.based_on = based.get("val")
            link_el = child(st_el, "link")
            if link_el is not None:
                st.link = link_el.get("val")
            if (st_el.get("default") or "").lower() in ("1", "true"):
                st.is_default = True
            st.para = parse_ppr(child(st_el, "pPr"), self.theme)
            st.run = parse_rpr(child(st_el, "rPr"), self.theme)
            if st.para:
                for k in ("_num_id", "_num_ilvl"):
                    st.para.__dict__.pop(k, None)
            if st.id:
                ss.styles[st.id] = st
        return ss

    # ── settings ──
    def _read_settings(self) -> dict:
        root = parse_xml(self.files.get("word/settings.xml"))
        out: dict = {"compat": {}, "defaultTabStopPx": 720 / 15}
        if root is None:
            return out
        compat = child(root, "compat")
        if compat is not None:
            for el in compat:
                if isinstance(el.tag, str) and el.tag.startswith("compatSetting"):
                    continue
                val = (el.get("val") or "").lower()
                out["compat"][el.tag] = val not in ("0", "false")
        dts = child(root, "defaultTabStop")
        if dts is not None and dts.get("val"):
            try:
                out["defaultTabStopPx"] = twip_to_px(float(dts.get("val")))
            except ValueError:
                pass
        return out


def _parse_numbering_safe(el, rels: dict, warnings: List[str]) -> List[NumberingDef]:
    try:
        from .numbering import parse_numbering

        return parse_numbering(el, rels)
    except Exception as e:  # noqa: BLE001
        warnings.append(f"numbering.xml 解析失败（列表降级为普通段落）：{type(e).__name__}: {e}")
        return []


# ─────────────────────────────────────────────────────────────
# 主体解析
# ─────────────────────────────────────────────────────────────


def parse_to_model(files: Dict[str, bytes], opts=None) -> DocumentModel:
    """docx（已解包条目）→ DocumentModel。"""
    from xhr.ingest import read_text

    ctx = _Ctx(files, opts)
    model = DocumentModel()
    body_root = parse_xml(files.get("word/document.xml"))
    if body_root is None:
        raise XhrError("CORRUPTED", "word/document.xml 无法解析")
    body = child(body_root, "body")

    blocks: List[object] = []
    sections: List[dict] = []
    body_sect: Optional[SectionProps] = None

    if body is not None:
        pending_sect: Optional[SectionProps] = None
        for el in body:
            if not isinstance(el.tag, str):
                continue
            if el.tag == "p":
                para = _parse_paragraph(el, ctx)
                blocks.append(para)
                ctx.paragraph_count += 1
                if para.sect_pr is not None:
                    sections.append({"props": para.sect_pr, "end": len(blocks)})
            elif el.tag == "tbl":
                tbl = _parse_table(el, ctx)
                blocks.append(tbl)
                ctx.table_count += 1
            elif el.tag == "sectPr":
                body_sect = _parse_sectpr(el, ctx)
            elif el.tag == "altChunk":
                ctx.warnings.append("文档包含 altChunk 外部内容块，已按安全策略拒绝渲染")
                para = ParagraphModel(id=f"altchunk-{len(blocks)}")
                para.runs = [RunText(text="[[外部内容块已跳过 (altChunk)]]")]
                blocks.append(para)
            elif el.tag == "sdt":
                # 结构化文档标签：展开内容
                content = child(child(el, "sdtContent"), None)
                sdt_content = child(el, "sdtContent")
                if sdt_content is not None:
                    for sub in sdt_content:
                        if not isinstance(sub.tag, str):
                            continue
                        if sub.tag == "p":
                            blocks.append(_parse_paragraph(sub, ctx))
                            ctx.paragraph_count += 1
                        elif sub.tag == "tbl":
                            blocks.append(_parse_table(sub, ctx))
                            ctx.table_count += 1

    # ── 分节切分 ──
    final_sect = body_sect or SectionProps()
    prev = 0
    for sec in sections:
        model.sections.append(
            {"props": sec["props"], "start_block_index": prev, "end_block_index": sec["end"]}
        )
        prev = sec["end"]
    model.sections.append(
        {"props": final_sect, "start_block_index": prev, "end_block_index": len(blocks)}
    )
    model.blocks = blocks

    # ── 编号标记 ──
    numbered = [b for b in blocks if isinstance(b, ParagraphModel) and getattr(b.props, "_num_id", None) is not None]
    from .numbering import assign_list_markers

    assign_list_markers(numbered, ctx.numbering, ctx.warnings)

    # ── 页眉页脚 / 脚注尾注 / 批注 ──
    model.furniture = _parse_furniture(files, ctx)

    # ── 媒体 ──
    model.media = _collect_media(files, ctx)

    # ── 元数据 ──
    core = parse_xml(files.get("docProps/core.xml"))
    title = author = modified = None
    if core is not None:
        t = child(core, "title")
        title = text_of(t) if t is not None else None
        creator = child(core, "creator")
        author = text_of(creator) if creator is not None else None
        mod = child(core, "modified")
        modified = text_of(mod) if mod is not None else None

    doc_grid = _read_doc_grid(final_sect, model.sections)
    model.meta = {
        "fileHash": "",
        "sourceFormat": "docx",
        "title": title,
        "author": author,
        "modifiedAt": modified,
        "compat": ctx.settings.get("compat", {}),
        "defaultTabStopPx": ctx.settings.get("defaultTabStopPx"),
        "docGrid": doc_grid,
        "warnings": ctx.warnings,
        "stats": {
            "paragraphCount": ctx.paragraph_count,
            "tableCount": ctx.table_count,
            "imageCount": ctx.image_count,
        },
    }
    model.theme = ctx.theme
    model.styles = list(ctx.style_set.styles.values())
    model.defaults_run = ctx.style_set.defaults_run
    model.defaults_para = ctx.style_set.defaults_para
    model.numbering = ctx.numbering
    model.toc = _build_toc(model.blocks)
    return model


def _read_doc_grid(final_sect: SectionProps, sections: List[dict]) -> Optional[dict]:
    grid = getattr(final_sect, "_doc_grid", None)
    if grid:
        return grid
    for sec in sections:
        g = getattr(sec["props"], "_doc_grid", None)
        if g and g.get("type") in ("lines", "linesAndChars"):
            return g
    return getattr(final_sect, "_doc_grid", None)


# ─────────────────────────────────────────────────────────────
# 段落
# ─────────────────────────────────────────────────────────────


def _parse_paragraph(p_el, ctx: _Ctx, link: Optional[_Link] = None) -> ParagraphModel:
    para = ParagraphModel(id=f"p{ctx.paragraph_count}")
    ppr_el = child(p_el, "pPr")
    # ⚠️ 段落内携带的分节符（pPr/sectPr）表示"本段是上一节的末尾"（T-A01）
    if ppr_el is not None:
        sect_in_p = ppr_el.find("sectPr")
        if sect_el_found := (sect_in_p is not None):
            para.sect_pr = _parse_sectpr(sect_in_p, ctx)
    partial = parse_ppr(ppr_el, ctx.theme)
    if partial is None:
        partial = ParaProps()
    else:
        partial = partial.copy()
    para.props = ctx.style_set.resolve_para(partial.style_id, partial)
    # numPr 私有键从 partial 透传
    for k in ("_num_id", "_num_ilvl"):
        if k in partial.__dict__:
            setattr(para.props, k, partial.__dict__[k])

    style_name = ctx.style_set.style_name_of(para.props.style_id)
    para.heading_level = heading_level_of(para.props, style_name)

    field_stack: List[FieldModel] = []
    runs: List = para.runs

    def emit(run) -> None:
        if link is not None and not isinstance(run, RunField):
            try:
                run._link = link  # noqa: B010 —— 渲染层读取
            except Exception:
                pass
        # 复杂域：separate 与 end 之间的 run 是域缓存结果，只进缓存不进正文流
        #（否则缓存文本会渲染两遍——压力测试实测）
        if field_stack and field_stack[-1]["stage"] == "cached" and not isinstance(run, RunField):
            field_stack[-1]["cached"].append(run)
            return
        runs.append(run)

    def collect(el, active_link: Optional[_Link]) -> None:
        for el2 in el:
            if not isinstance(el2.tag, str):
                continue
            _dispatch(el2, active_link)

    def _dispatch(el, active_link: Optional[_Link]) -> None:
        nonlocal field_stack
        tag = el.tag
        if tag == "r":
            _parse_run(el, ctx, para, emit, field_stack, active_link)
        elif tag == "hyperlink":
            lnk = _Link()
            rid = el.get("id")
            anchor = el.get("anchor")
            if rid:
                target = ctx.external_rels.get(rid)
                safe = _sanitize_href(target)
                if safe:
                    lnk.href = safe
                elif target:
                    ctx.warnings.append("存在协议不被允许的超链接，已降级为纯文本")
            if anchor:
                lnk.anchor = anchor
            collect(el, lnk if (lnk.href or lnk.anchor) else active_link)
        elif tag == "fldSimple":
            instr = el.get("instr") or ""
            fld = FieldModel(instruction=instr, type=_field_type(instr))
            cached: List = []
            saved = runs[:]
            runs.clear()
            collect(el, active_link)
            cached = runs[:]
            runs[:] = saved
            fld.cached_runs = cached
            if fld.type == "HYPERLINK":
                m = re.search(r'HYPERLINK\s+"([^"]+)"', instr)
                if m:
                    fld.target = _sanitize_href(m.group(1)) or m.group(1)
            emit(RunField(field=fld))
        elif tag == "ins":
            if ctx.opts.revision_mode == "original":
                return  # 拒绝全部：跳过插入内容
            for sub in el:
                if not isinstance(sub.tag, str) or sub.tag != "r":
                    continue
                if ctx.opts.revision_mode == "markup":
                    before = len(runs)
                    _parse_run(sub, ctx, para, emit, field_stack, active_link)
                    for r in runs[before:]:
                        r._rev = "ins"
                else:
                    _parse_run(sub, ctx, para, emit, field_stack, active_link)
        elif tag == "del":
            # final：删除内容整体跳过；original（拒绝全部）：删除内容是"原文"要保留；
            # markup：保留并加 <del> 标记
            if ctx.opts.revision_mode == "final":
                return
            for sub in el:
                if not isinstance(sub.tag, str) or sub.tag != "r":
                    continue
                if ctx.opts.revision_mode == "markup":
                    before = len(runs)
                    _parse_run(sub, ctx, para, emit, field_stack, active_link, del_mode=True)
                    for r in runs[before:]:
                        r._rev = "del"
                else:
                    _parse_run(sub, ctx, para, emit, field_stack, active_link, del_mode=True)
        elif tag in ("sdt", "smartTag", "smartTagPr"):
            content = child(el, "sdtContent") or el
            collect(content, active_link)
        elif tag == "bookmarkStart":
            para.bookmarks.append({"id": el.get("id") or "", "name": el.get("name") or ""})
        elif tag == "proofErr" or tag == "bookmarkEnd" or tag == "commentRangeStart" or tag == "commentRangeEnd":
            pass
        elif tag == "altChunk":
            ctx.warnings.append("段落内 altChunk 外部内容块已按安全策略拒绝渲染")
            emit(RunText(text="[[外部内容块已跳过 (altChunk)]]"))
        elif tag == "pPr":
            return
        else:
            # 未知元素：尝试继续解析内部 run（永不丢文字）
            collect(el, active_link)

    collect(p_el, link)
    return para


def _parse_run(r_el, ctx: _Ctx, para: ParagraphModel, emit, field_stack: List, link, del_mode: bool = False) -> None:
    rpr_el = child(r_el, "rPr")
    partial = parse_rpr(rpr_el, ctx.theme)
    props = ctx.style_set.resolve_run(para.props.style_id, para.props.mark_run_props, partial)
    # 修订删除文本
    for el in r_el:
        if not isinstance(el.tag, str):
            continue
        tag = el.tag
        if tag == "t":
            if del_mode:
                continue
            text = el.text or ""
            # 多个 w:t 拼接（Word 会拆分）；xml:space 已由解析器保留
            emit(RunText(text=text, props=props))
        elif tag == "delText":
            if del_mode:
                emit(RunText(text=el.text or "", props=props))
            continue
        elif tag == "tab":
            emit(RunTab(props=props))
        elif tag == "br" or tag == "cr":
            bt = (el.get("type") or ("page" if tag == "cr" and False else "textWrapping"))
            emit(RunBreak(break_type=bt, props=props))
        elif tag == "sym":
            char = el.get("char") or ""
            font = el.get("font") or ""
            try:
                ch = chr(int(char, 16)) if char else ""
            except ValueError:
                ch = ""
            emit(RunSym(char=ch, font=font, props=props))
        elif tag == "noBreakHyphen":
            emit(RunHyphen(kind="noBreakHyphen", props=props))
        elif tag == "softHyphen":
            emit(RunHyphen(kind="softHyphen", props=props))
        elif tag == "drawing":
            d = _parse_drawing(el, ctx, layout="inline")
            if d is not None:
                emit(RunDrawing(drawing=d, props=props))
        elif tag == "pict":
            d = _parse_vml_pict(el, ctx)
            if d is not None:
                emit(RunDrawing(drawing=d, props=props))
        elif tag == "object":
            ctx.warnings.append("OLE 嵌入对象不支持内容解析，已渲染为占位")
            emit(RunText(text="[[嵌入对象]]", props=props))
        elif tag == "fldChar":
            ftype = el.get("fldCharType")
            if ftype == "begin":
                fld = FieldModel(dirty=(el.get("dirty") or "").lower() == "true")
                field_stack.append({"field": fld, "instr": [], "cached": [], "stage": "instr"})
            elif ftype == "separate":
                if field_stack:
                    field_stack[-1]["stage"] = "cached"
            elif ftype == "end":
                if field_stack:
                    item = field_stack.pop()
                    fld = item["field"]
                    fld.instruction = "".join(item["instr"])
                    fld.type = _field_type(fld.instruction)
                    fld.cached_runs = item["cached"]
                    if fld.type == "HYPERLINK":
                        m = re.search(r'HYPERLINK\s+"([^"]+)"', fld.instruction)
                        if m:
                            fld.target = _sanitize_href(m.group(1)) or m.group(1)
                    if not field_stack:  # 只在最外层输出
                        emit(RunField(field=fld))
                    else:
                        # 嵌套域：并入外层缓存
                        field_stack[-1]["cached"].append(RunField(field=fld))
            elif tag == "instrText":
                pass
        elif tag == "instrText":
            if field_stack:
                field_stack[-1]["instr"].append(el.text or "")
        elif tag == "footnoteReference":
            try:
                nid = int(el.get("id") or 0)
            except ValueError:
                nid = 0
            emit(RunNoteRef(kind="footnoteRef", note_id=nid, props=props))
        elif tag == "endnoteReference":
            try:
                nid = int(el.get("id") or 0)
            except ValueError:
                nid = 0
            emit(RunNoteRef(kind="endnoteRef", note_id=nid, props=props))
        elif tag == "ruby":
            base = text_of(child(el, "rubyBase") or None)
            rt = text_of(child(child(el, "rt"), "rubyPr") or None)
            rt_el = child(el, "rt")
            rt = text_of(rt_el) if rt_el is not None else ""
            base_el = child(el, "rubyBase")
            base_t = text_of(base_el) if base_el is not None else ""
            emit(RunRuby(base=base_t, rt=rt, props=props))
        elif tag in ("lastRenderedPageBreak", "contentPart", "separator", "endnoteRef", "footnoteRef"):
            if tag == "lastRenderedPageBreak":
                para.last_rendered_page_break = True
        elif tag in ("rPr", "sectPr"):
            continue
        else:
            # 未知：不丢文字，尝试深层取文本
            t = text_of(el)
            if t:
                emit(RunText(text=t, props=props))


def _field_type(instr: str) -> str:
    m = re.match(r"\s*([A-Za-z0-9]+)", instr or "")
    if not m:
        return "unknown"
    kw = m.group(1).upper()
    return kw if kw in ("PAGE", "NUMPAGES", "TOC", "REF", "PAGEREF", "HYPERLINK", "DATE", "TIME", "SECTION", "NOTEREF", "MERGEFIELD") else "unknown"


# ─────────────────────────────────────────────────────────────
# 图片（DrawingML + VML）
# ─────────────────────────────────────────────────────────────


def _parse_drawing(el, ctx: _Ctx, layout: str) -> Optional[DrawingModel]:
    wp = child(el, "inline")
    if wp is None:
        wp = child(el, "anchor")
    if wp is None:
        return None
    layout = "inline" if wp.tag == "inline" else "anchor"
    d = DrawingModel(id=wp.get("docPr_id") or "", layout=layout)
    doc_pr = child(wp, "docPr")
    if doc_pr is not None:
        d.id = doc_pr.get("id") or ""
        d.alt_text = doc_pr.get("descr")
    ext = child(wp, "extent")
    if ext is not None:
        d.width_px = emu_to_px(float(ext.get("cx") or 0))
        d.height_px = emu_to_px(float(ext.get("cy") or 0))
    # blip
    blip = None
    graphic = child(child(wp, "graphic"), "graphicData")
    pic = child(graphic, "pic") if graphic is not None else None
    if pic is not None:
        blip_fill = child(pic, "blipFill")
        blip = child(blip_fill, "blip") if blip_fill is not None else None
        if blip is not None:
            src = child(blip_fill, "srcRect")  # type: ignore[union-attr]
            if src is not None:
                d.crop = {
                    "l": src_rect_ratio(float(src.get("l") or 0)),
                    "t": src_rect_ratio(float(src.get("t") or 0)),
                    "r": src_rect_ratio(float(src.get("r") or 0)),
                    "b": src_rect_ratio(float(src.get("b") or 0)),
                }
    if layout == "anchor":
        wrap = None
        for c in wp:
            if isinstance(c.tag, str) and c.tag.startswith("wrap"):
                wrap = c.tag[4:].lower()
                break
        position = child(wp, "positionH")
        position_v = child(wp, "positionV")
        d.anchor = {
            "behindDoc": (wp.get("behindDoc") or "0") == "1",
            "wrap": wrap or "none",
            "posX": float(position.get("posOffset") or 0) / 9525 if position is not None else None,
            "posY": float(position_v.get("posOffset") or 0) / 9525 if position_v is not None else None,
            "relativeFrom": (position.get("relativeFrom") if position is not None else None) or "",
        }
        if wrap not in (None, "none"):
            ctx.warnings.append("浮动图片的环绕排版暂不支持，已降级为嵌入式")
    if blip is not None:
        rid = blip.get("embed")
        if rid:
            target = ctx.rels.get(rid)
            if target:
                d.media_id = target.rsplit("/", 1)[-1]
                d.kind = "image"
        link_rid = blip.get("link")
        if link_rid and ctx.external_rels.get(link_rid):
            ctx.warnings.append("外部链接图片（r:link）按安全策略禁止加载，已渲染占位")
            d.kind = "unknown"
            d.media_id = None
    if d.kind != "image":
        graphic_data_tag = graphic.get("uri") if graphic is not None else ""
        if "chart" in (graphic_data_tag or ""):
            d.kind = "chart"
            ctx.warnings.append("图表暂不支持渲染，已显示占位")
        elif "math" in (graphic_data_tag or ""):
            d.kind = "unknown"
            ctx.warnings.append("公式（OMML）暂不支持渲染，已显示占位")
        else:
            d.kind = "shape" if d.kind == "unknown" else d.kind
    ctx.image_count += 1
    return d


def _parse_vml_pict(el, ctx: _Ctx) -> Optional[DrawingModel]:
    shape = None
    for e in el.iter():
        if isinstance(e.tag, str) and e.tag in ("shape", "rect", "oval", "roundrect", "imagedata"):
            shape = e
            break
    if shape is None:
        return None
    d = DrawingModel(layout="inline")
    img = None
    for e in el.iter():
        if isinstance(e.tag, str) and e.tag == "imagedata":
            img = e
            break
    style = shape.get("style") or ""
    m = re.search(r"width\s*:\s*([\d.]+)pt", style)
    if m:
        d.width_px = round(float(m.group(1)) * 4 / 3)
    m = re.search(r"height\s*:\s*([\d.]+)pt", style)
    if m:
        d.height_px = round(float(m.group(1)) * 4 / 3)
    if img is not None:
        rid = img.get("id")
        target = ctx.rels.get(rid or "")
        if target:
            d.media_id = target.rsplit("/", 1)[-1]
            d.kind = "image"
            ctx.image_count += 1
        else:
            d.kind = "unknown"
    else:
        d.kind = "shape"
    return d


# ─────────────────────────────────────────────────────────────
# 表格
# ─────────────────────────────────────────────────────────────


def _parse_borders_el(el, theme: dict) -> Optional[Borders]:
    if el is None:
        return None
    borders = Borders()
    for side in ("top", "left", "bottom", "right", "between", "bar", "insideH", "insideV"):
        b = child(el, side)
        if b is None:
            continue
        val = b.get("val") or "single"
        if val in ("nil", "none"):
            continue
        from .styles import _border_attrs

        color = resolve_word_color(_border_attrs(b), theme) or "#000000"
        spec = BorderSpec(
            style=val,
            width_px=eighth_pt_to_px(float(b.get("sz") or 4)),
            space_pt=float(b.get("space") or 0),
            color=color,
        )
        if side == "insideH":
            borders.between = spec
        elif side == "insideV":
            continue
        else:
            setattr(borders, side, spec)
    return borders or None


def _parse_table(tbl_el, ctx: _Ctx) -> TableModel:
    tbl = TableModel()
    props = tbl.props
    tbl_pr = child(tbl_el, "tblPr")

    if tbl_pr is not None:
        w_el = child(tbl_pr, "tblW")
        if w_el is not None:
            wtype = w_el.get("type") or "dxa"
            try:
                wv = float(w_el.get("w") or 0)
            except ValueError:
                wv = 0
            if wtype == "dxa":
                props.width_px = twip_to_px(wv)
            elif wtype == "pct":
                props.width_pct = pct_to_percent(wv)
        layout_el = child(tbl_pr, "tblLayout")
        if layout_el is not None and layout_el.get("type"):
            props.layout = layout_el.get("type")
        jc = child(tbl_pr, "jc")
        if jc is not None and jc.get("val") in ("left", "center", "right"):
            props.align = jc.get("val")
        ind = child(tbl_pr, "tblInd")
        if ind is not None and ind.get("w"):
            try:
                props.indent_px = twip_to_px(float(ind.get("w")))
            except ValueError:
                pass
        props.borders = _parse_borders_el(child(tbl_pr, "tblBorders"), ctx.theme)
        shd = child(tbl_pr, "shd")
        if shd is not None:
            from .styles import _shd_attrs

            fill = resolve_word_color(_shd_attrs(shd), ctx.theme)
            if fill:
                props.shd_fill = fill
        mar = child(tbl_pr, "tblCellMar")
        if mar is not None:
            for side in ("top", "left", "bottom", "right"):
                m_el = child(mar, side)
                if m_el is not None and m_el.get("w"):
                    try:
                        props.cell_margin[side] = twip_to_px(float(m_el.get("w")))
                    except ValueError:
                        pass

    grid: List[float] = []
    grid_el = child(tbl_el, "tblGrid")
    if grid_el is not None:
        for col in children(grid_el, "gridCol"):
            try:
                grid.append(twip_to_px(float(col.get("w") or 0)))
            except ValueError:
                grid.append(0.0)
    props.grid = grid

    # 行与单元格
    raw_rows: List[Tuple[TableRowModel, List[Tuple[TableCellModel, int, Optional[str]]]]] = []
    for tr in children(tbl_el, "tr"):
        row = TableRowModel()
        tr_pr = child(tr, "trPr")
        if tr_pr is not None:
            h_el = child(tr_pr, "trHeight")
            if h_el is not None:
                try:
                    row.height_px = twip_to_px(float(h_el.get("val") or 0))
                except ValueError:
                    row.height_px = None
                row.height_rule = h_el.get("hRule") or "atLeast"
            if child(tr_pr, "cantSplit") is not None:
                row.cant_split = True
            if child(tr_pr, "tblHeader") is not None:
                row.is_header = True
        cells: List[Tuple[TableCellModel, int, Optional[str]]] = []
        for tc in children(tr, "tc"):
            if ctx.cell_count >= DHR_LIMITS["MAX_TABLE_CELLS"]:
                ctx.warnings.append("表格单元格数达到上限，超出部分被截断")
                break
            tc_pr = child(tc, "tcPr")
            cell = TableCellModel()
            span = 1
            vmerge: Optional[str] = None
            if tc_pr is not None:
                w_el = child(tc_pr, "tcW")
                if w_el is not None and (w_el.get("type") or "dxa") == "dxa":
                    try:
                        cell.width_px = twip_to_px(float(w_el.get("w") or 0))
                    except ValueError:
                        cell.width_px = None
                gs = child(tc_pr, "gridSpan")
                if gs is not None and gs.get("val"):
                    try:
                        span = max(1, int(float(gs.get("val"))))
                    except ValueError:
                        span = 1
                vm = child(tc_pr, "vMerge")
                if vm is not None:
                    vmerge = vm.get("val") or "continue"
                va = child(tc_pr, "vAlign")
                if va is not None and va.get("val"):
                    cell.v_align = {"top": "top", "center": "center", "bottom": "bottom"}.get(va.get("val"), "top")
                shd = child(tc_pr, "shd")
                if shd is not None:
                    from .styles import _shd_attrs

                    fill = resolve_word_color(_shd_attrs(shd), ctx.theme)
                    if fill:
                        cell.shd_fill = fill
                cell.borders = _parse_borders_el(child(tc_pr, "tcBorders"), ctx.theme)
                td_el = child(tc_pr, "textDirection")
                if td_el is not None and td_el.get("val") in ("tbRl", "btLr"):
                    cell.vertical = True
                if child(tc_pr, "noWrap") is not None:
                    cell.no_wrap = True
            # 单元格内容：至少一个段落（空单元格也保留一个空段）
            inner: List = []
            for sub in tc:
                if not isinstance(sub.tag, str):
                    continue
                if sub.tag == "p":
                    inner.append(_parse_paragraph(sub, ctx))
                    ctx.paragraph_count += 1
                elif sub.tag == "tbl":
                    inner.append(_parse_table(sub, ctx))
                    ctx.table_count += 1
            if not inner:
                inner.append(ParagraphModel(id=f"cell-{ctx.cell_count}"))
            cell.blocks = inner
            cell.colspan = span
            cells.append((cell, span, vmerge))
            ctx.cell_count += 1
        raw_rows.append((row, cells))

    # ⚠️ vMerge → rowspan 统计（7.3 伪代码；必须考虑 gridSpan 对列游标的影响）
    table_rows: List[TableRowModel] = []
    skip: set = set()  # (row_idx, cell_idx) 被 vMerge 覆盖
    for r_idx, (row, cells) in enumerate(raw_rows):
        col_cursor = 0
        for c_idx, (cell, span, vmerge) in enumerate(cells):
            if vmerge == "continue":
                skip.add((r_idx, c_idx))
                col_cursor += span
                continue
            if vmerge == "restart":
                # 向下数连续 continue（该格覆盖的列范围内）
                n = 0
                r2 = r_idx + 1
                while r2 < len(raw_rows):
                    cont = _find_continue(raw_rows[r2][1], col_cursor, span)
                    if cont is None:
                        break
                    n += 1
                    r2 += 1
                cell.rowspan = n + 1
            col_cursor += span
        for c_idx, (cell, span, vmerge) in enumerate(cells):
            if (r_idx, c_idx) in skip:
                continue
            row.cells.append(cell)
        table_rows.append(row)
    tbl.rows = table_rows
    return tbl


def _find_continue(cells: List[Tuple[TableCellModel, int, Optional[str]]], col_start: int, span: int):
    """在某一行的单元格列表里找覆盖 [col_start, col_start+span) 的 vMerge=continue 格。"""
    cursor = 0
    for cell, cspan, vmerge in cells:
        cell_start, cell_end = cursor, cursor + cspan
        cursor = cell_end
        if vmerge == "continue" and cell_start <= col_start and cell_end >= col_start + span:
            return cell
        if cell_start > col_start:
            break
    return None


# ─────────────────────────────────────────────────────────────
# 分节属性
# ─────────────────────────────────────────────────────────────


def _parse_sectpr(sect_el, ctx: _Ctx) -> SectionProps:
    sp = SectionProps()
    pg_sz = child(sect_el, "pgSz")
    w = h = 0
    if pg_sz is not None:
        w = float(pg_sz.get("w") or 11906)
        h = float(pg_sz.get("h") or 16838)
        sp.orientation = pg_sz.get("orient") or "portrait"
        if sp.orientation == "landscape" and w < h:
            w, h = h, w
    sp.page_size = (twip_to_px(w), twip_to_px(h))
    mar = child(sect_el, "pgMar")
    if mar is not None:
        for key, attr_name in (("top", "top"), ("right", "right"), ("bottom", "bottom"),
                               ("left", "left"), ("header", "header"), ("footer", "footer")):
            try:
                sp.margins[key] = twip_to_px(float(mar.get(attr_name) or 0))
            except ValueError:
                pass
    sp.content_width_px = max(1, sp.page_size[0] - sp.margins["left"] - sp.margins["right"])
    cols_el = child(sect_el, "cols")
    if cols_el is not None:
        try:
            sp.cols = max(1, int(float(cols_el.get("num") or 1)))
        except ValueError:
            sp.cols = 1
        try:
            sp.col_space_px = twip_to_px(float(cols_el.get("space") or 0))
        except ValueError:
            pass
    if child(sect_el, "titlePg") is not None:
        sp.title_pg = True
    pn = child(sect_el, "pgNumType")
    if pn is not None:
        sp.pg_num_type = {"start": pn.get("start"), "fmt": pn.get("fmt")}
    for kind, tag in (("header", "headerReference"), ("footer", "footerReference")):
        for ref in children(sect_el, tag):
            rid = ref.get("id")
            target = ctx.rels.get(rid or "")
            if target:
                getattr(sp, f"{kind}_refs").append(
                    {"type": ref.get("type") or "default", "part_id": target}
                )
    grid = child(sect_el, "docGrid")
    if grid is not None:
        gtype = grid.get("type")
        if gtype:
            try:
                pitch = float(grid.get("linePitch") or 0)
            except ValueError:
                pitch = 0
            sp._doc_grid = {"type": gtype, "linePitch": pitch}  # type: ignore[attr-defined]
    return sp


# ─────────────────────────────────────────────────────────────
# 家具（页眉页脚 / 脚注尾注 / 批注）
# ─────────────────────────────────────────────────────────────


def _parse_furniture(files: Dict[str, bytes], ctx: _Ctx) -> List[FurnitureModel]:
    out: List[FurnitureModel] = []
    for rid, target in ctx.rels.items():
        kind = None
        if "header" in target and target.startswith("word/header"):
            kind = "header"
        elif "footer" in target and target.startswith("word/footer"):
            kind = "footer"
        if kind:
            fm = _parse_furniture_part(files, ctx, target, kind=kind)
            if fm is not None:
                # 从 sectPr 反查 hfType
                out.append(fm)
    for part, ntype in (("word/footnotes.xml", "footnote"), ("word/endnotes.xml", "endnote")):
        root = parse_xml(files.get(part))
        if root is None:
            continue
        for el in children(root, ntype):
            ntype_attr = el.get("type") or ""
            if ntype_attr in ("separator", "continuationSeparator"):
                continue
            try:
                nid = int(float(el.get("id") or 0))
            except ValueError:
                continue
            fm = _parse_furniture_blocks(el, ctx, part_id=f"{part}#{nid}")
            if fm is not None:
                fm.type = ntype
                fm.note_id = nid
                out.append(fm)
    comments_root = parse_xml(files.get("word/comments.xml"))
    if comments_root is not None:
        for el in children(comments_root, "comment"):
            try:
                cid = int(float(el.get("id") or 0))
            except ValueError:
                continue
            fm = _parse_furniture_blocks(el, ctx, part_id=f"comments#{cid}")
            if fm is not None:
                fm.type = "comment"
                fm.note_id = cid
                fm.author = el.get("author")
                fm.date = el.get("date")
                out.append(fm)
    return out


def _parse_furniture_part(files: Dict[str, bytes], ctx: _Ctx, target: str, kind: str) -> Optional[FurnitureModel]:
    root = parse_xml(files.get(target))
    if root is None:
        return None
    # part 内部自己的 rels（页眉可能带图片）
    import posixpath

    d, base = posixpath.split(target)
    rels_path = posixpath.join(d, "_rels", base + ".rels")
    inner_rels = read_relationships(files, rels_path)
    old = ctx.rels
    ctx.rels = {**old, **inner_rels} if inner_rels else ctx.rels
    try:
        fm = _parse_furniture_blocks(root, ctx, part_id=target)
    finally:
        ctx.rels = old
    if fm is not None:
        fm.type = kind
        fm.hf_type = "default"
    return fm


def _parse_furniture_blocks(root_el, ctx: _Ctx, part_id: str) -> Optional[FurnitureModel]:
    fm = FurnitureModel(part_id=part_id)
    blocks: List = []
    count = 0
    for el in root_el:
        if not isinstance(el.tag, str):
            continue
        if el.tag == "p":
            blocks.append(_parse_paragraph(el, ctx))
            count += 1
        elif el.tag == "tbl":
            blocks.append(_parse_table(el, ctx))
            count += 1
    if not count:
        return None
    fm.blocks = blocks
    return fm


# ─────────────────────────────────────────────────────────────
# 媒体
# ─────────────────────────────────────────────────────────────


def _collect_media(files: Dict[str, bytes], ctx: _Ctx) -> List[dict]:
    from xhr.ingest import verify_image
    import base64

    out: List[dict] = []
    for target in ctx.rels.values():
        if not target.startswith("word/media/"):
            continue
        base = target.rsplit("/", 1)[-1]
        if not base or "." not in base:
            continue
        ext = base.rsplit(".", 1)[-1].lower()
        data = files.get(target)
        if data is None:
            continue
        verdict = verify_image(ext, data)
        if not verdict.ok:
            ctx.warnings.append(f"忽略可疑图片 {base}：{verdict.reason}")
            continue
        if len(data) > DHR_LIMITS["MAX_IMAGE_BYTES"]:
            ctx.warnings.append(f"图片 {base} 体积超限，已跳过")
            continue
        out.append(
            {
                "id": base,
                "mime": verdict.mime,
                "data_base64": base64.b64encode(data).decode("ascii"),
            }
        )
    return out


# ─────────────────────────────────────────────────────────────
# 目录重算（TOC 域）
# ─────────────────────────────────────────────────────────────


def _build_toc(blocks: List) -> List[dict]:
    """由 headingLevel 重新生成目录（比域缓存更准）。"""
    toc: List[dict] = []
    for i, b in enumerate(blocks):
        if isinstance(b, ParagraphModel) and b.heading_level:
            text = "".join(r.text for r in b.runs if isinstance(r, RunText))
            toc.append({"level": b.heading_level, "text": text, "anchor": f"dhr-block-{i}"})
    return toc
