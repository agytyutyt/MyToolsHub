"""高级特性解析 —— 图片锚点 / WPS DISPIMG / 批注 / 媒体收集。

对应 TS 版 ``packages/parser-xlsx/src/advanced.ts``。

⚠️ 关键实测结论（TS 版 14.4/14.5 节，Python 原生解析同样适用）：
  · 锚点偏移 <xdr:colOff>/<xdr:rowOff> 的单位是 EMU，换算 1px = 9525 EMU。
    （设计初稿的 /1024、/256 是 BIFF 约定，用在 xlsx 上错得离谱。）
  · 锚定类型直接按 XML 结构判定：twoCellAnchor（有 to）/ oneCellAnchor（有 ext），
    不存在 editAs 误标问题（那是 ExcelJS 的读取缺陷）。
"""
from __future__ import annotations

import base64
import re
from typing import Callable, Dict, List, Optional, Tuple

from ..core import (
    DrawingModel,
    MediaAsset,
    Rect,
    a1_range_to_rect,
)
from ..ingest import read_text, verify_image
from .raw import sheet_rels_path
from .xmlutil import attr, attr_int, child, children, parse_xml, read_relationships, text_int, text_of

#: OOXML 的 EMU 与像素换算：1px = 9525 EMU（96 DPI 下的精确定义值）
EMU_PER_PX = 9525


def _clamp01(n: float) -> float:
    if n != n:  # NaN
        return 0.0
    return 0.0 if n < 0 else (1.0 if n > 1 else n)


# ─────────────────────────────────────────────────────────────
# 浮动图片
# ─────────────────────────────────────────────────────────────


def read_drawings(
    files: dict[str, bytes],
    drawing_part_path: str,
    col_width_px_of: Callable[[int], int],
    row_height_px_of: Callable[[int], int],
    warnings: List[str],
) -> List[DrawingModel]:
    """解析 xl/drawings/drawingN.xml 的锚点 → DrawingModel 列表。"""
    out: List[DrawingModel] = []
    root = parse_xml(files.get(drawing_part_path))
    if root is None:
        return out

    # rId → media 文件名
    drawing_rels = read_relationships(files, sheet_rels_path(drawing_part_path))

    def media_id_of(rid: Optional[str]) -> Optional[str]:
        if not rid:
            return None
        target = drawing_rels.get(rid)
        if not target:
            return None
        return target.rsplit("/", 1)[-1]

    idx = 0

    def anchor_points(el) -> Optional[Tuple[dict, Optional[dict], Optional[dict]]]:
        """解析 from/to/ext 三组锚点坐标。

        ⚠️ <xdr:from><xdr:col>1</xdr:col><xdr:colOff>0</xdr:colOff>… 是
        **子元素文本**，不是属性。
        """
        from_el = child(el, "from")
        to_el = child(el, "to")
        ext_el = child(el, "ext")

        def point(p) -> Optional[dict]:
            if p is None:
                return None
            col = text_int(child(p, "col"))
            row = text_int(child(p, "row"))
            if col is None or row is None:
                return None
            return {
                "col": col,
                "row": row,
                "col_off": text_int(child(p, "colOff")) or 0,
                "row_off": text_int(child(p, "rowOff")) or 0,
            }

        f = point(from_el)
        if f is None:
            return None
        t = point(to_el)
        ext: Optional[dict] = None
        if ext_el is not None:
            cx = attr_int(ext_el, "cx")
            cy = attr_int(ext_el, "cy")
            if cx is not None and cy is not None:
                ext = {"cx": cx, "cy": cy}
        return f, t, ext

    def pic_media(el) -> Optional[str]:
        """<xdr:pic> → media 资源 id（'image1.png'）。"""
        pic = child(el, "pic")
        if pic is None:
            return None
        blip = None
        blip_fill = child(pic, "blipFill")
        if blip_fill is not None:
            blip = child(blip_fill, "blip")
        return media_id_of(attr(blip, "embed") or None) if blip is not None else None

    for anchor in list(children(root, "twoCellAnchor")) + list(children(root, "oneCellAnchor")) + list(
        children(root, "absoluteAnchor")
    ):
        pts = anchor_points(anchor)
        if pts is None:
            continue
        f, t, ext = pts
        media_id = pic_media(anchor)
        if child(anchor, "pic") is not None:
            kind = "image"
        elif child(anchor, "graphicFrame") is not None:
            kind = "chart"
        elif child(anchor, "sp") is not None or child(anchor, "cxnSp") is not None:
            kind = "shape"
        else:
            kind = "unknown"

        drawing = DrawingModel(id=f"pic{idx}", kind=kind, media_id=media_id)
        idx += 1

        col_w = max(1, col_width_px_of(f["col"]))
        row_h = max(1, row_height_px_of(f["row"]))

        if kind == "image" and not media_id:
            warnings.append(f"第 {idx} 张图片无法定位媒体资源，已跳过")
            continue

        if t is not None:
            to_col_w = max(1, col_width_px_of(t["col"]))
            to_row_h = max(1, row_height_px_of(t["row"]))
            drawing.anchor = {
                "from": {
                    "row": f["row"],
                    "col": f["col"],
                    "dx": _clamp01(f["col_off"] / EMU_PER_PX / col_w),
                    "dy": _clamp01(f["row_off"] / EMU_PER_PX / row_h),
                },
                "to": {
                    "row": t["row"],
                    "col": t["col"],
                    "dx": _clamp01(t["col_off"] / EMU_PER_PX / to_col_w),
                    "dy": _clamp01(t["row_off"] / EMU_PER_PX / to_row_h),
                },
            }
        elif ext is not None:
            drawing.one_cell = {
                "row": f["row"],
                "col": f["col"],
                "width_px": max(1, round(ext["cx"] / EMU_PER_PX)),
                "height_px": max(1, round(ext["cy"] / EMU_PER_PX)),
            }
        else:
            continue
        out.append(drawing)

    return out


# ─────────────────────────────────────────────────────────────
# WPS DISPIMG 嵌入图片
# ─────────────────────────────────────────────────────────────


def read_cell_images(files: dict[str, bytes]) -> Dict[str, str]:
    """读取 xl/cellimages.xml（WPS 私有部件），建立 DISPIMG 图片名 → media 资源 id 映射。

    结构（已去命名空间前缀）：
      <cellImages><cellImage><pic>
        <nvPicPr><cNvPr name="ID_xxx"/></nvPicPr>
        <blipFill><blip embed="rId1"/></blipFill>
      </pic></cellImage></cellImages>

    ⚠️ 中文场景必做：WPS 表格里插入的图片大量以 DISPIMG 形式存在，
       不处理的话单元格只会显示 #VALUE!。
    """
    xml = files.get("xl/cellimages.xml")
    if not xml:
        return {}
    root = parse_xml(xml)
    if root is None:
        return {}

    rels = read_relationships(files, "xl/_rels/cellimages.xml.rels")
    out: Dict[str, str] = {}
    for ci in children(root, "cellImage"):
        pic = child(ci, "pic")
        if pic is None:
            continue
        nv = child(pic, "nvPicPr")
        name = attr(child(nv, "cNvPr"), "name") if nv is not None else ""
        blip_fill = child(pic, "blipFill")
        embed = attr(child(blip_fill, "blip"), "embed") if blip_fill is not None else ""
        if not name or not embed:
            continue
        target = rels.get(embed)
        if not target:
            continue
        # target 可能是 'media/image1.png' / '/xl/media/image1.png'
        base = target.rsplit("/", 1)[-1]
        if base:
            out[name] = base
    return out


DISPIMG_RE = re.compile(r'DISPIMG\s*\(\s*"([^"]+)"', re.IGNORECASE)


def extract_disp_img_id(formula: Optional[str]) -> Optional[str]:
    """从 DISPIMG 公式里提取图片名。形如 _xlfn.DISPIMG("ID_1A2B",1)。"""
    if not formula:
        return None
    m = DISPIMG_RE.search(formula)
    return m.group(1) if m else None


# ─────────────────────────────────────────────────────────────
# 批注
# ─────────────────────────────────────────────────────────────


def read_comments(files: dict[str, bytes], comments_path: str) -> List[dict]:
    """解析 xl/commentsN.xml → [{ref, author, text}]。"""
    root = parse_xml(files.get(comments_path))
    if root is None:
        return []
    authors: List[str] = []
    authors_el = child(root, "authors")
    if authors_el is not None:
        for a in children(authors_el, "author"):
            authors.append(text_of(a))

    out: List[dict] = []
    list_el = child(root, "commentList")
    if list_el is None:
        return out
    for c in children(list_el, "comment"):
        ref = attr(c, "ref")
        if not ref:
            continue
        try:
            aid = int(attr(c, "authorId", "") or -1)
        except ValueError:
            aid = -1
        author = authors[aid] if 0 <= aid < len(authors) else None
        text_el = child(c, "text")
        text = text_of(text_el) if text_el is not None else ""
        out.append({"ref": ref, "author": author, "text": text})
    return out


# ─────────────────────────────────────────────────────────────
# 媒体收集
# ─────────────────────────────────────────────────────────────

MIME_BY_EXT = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "bmp": "image/bmp",
    "webp": "image/webp",
}


def collect_media(
    files: dict[str, bytes],
    inline_media: bool = True,
    max_image_bytes: int = 5 * 1024 * 1024,
    warnings: Optional[List[str]] = None,
) -> List[MediaAsset]:
    """收集 xl/media/* 下的图片（扩展名 × 魔数双重校验，SVG 永不内联）。"""
    warn = warnings if warnings is not None else []
    out: List[MediaAsset] = []
    if not inline_media:
        return out
    for name in sorted(files.keys()):
        if not name.startswith("xl/media/"):
            continue
        base = name[len("xl/media/") :]
        if not base or base.endswith("/"):
            continue
        ext = base.rsplit(".", 1)[-1].lower() if "." in base else ""
        mime = MIME_BY_EXT.get(ext)
        if not mime:
            warn.append(f"忽略不支持的图片类型：{name}（仅允许 png/jpeg/gif/bmp/webp）")
            continue
        data = files[name]
        # ⚠️ 魔数二次校验：只看扩展名等于允许「声明成 PNG 的任意内容」通过白名单
        verdict = verify_image(ext, data)
        if not verdict.ok:
            warn.append(f"忽略可疑图片 {name}：{verdict.reason}")
            continue
        if len(data) > max_image_bytes:
            warn.append(f"图片 {name} 体积超限（{len(data)} 字节），已跳过")
            continue
        out.append(MediaAsset(id=base, mime=verdict.mime or mime, data_base64=base64.b64encode(data).decode("ascii")))
    return out
