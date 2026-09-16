# -*- coding: utf-8 -*-
"""背景图片清理 —— 识别并删除表格文档里嵌的「工作表背景图片」。

为什么要有这一步
----------------
背景图片（Excel「页面布局 → 背景」插入的那张图）不属于数据，却会跟着文档一起流转：
它常被用来夹带水印、机构标识或来源标记。因此「过滤器」与「轨迹速写」都在**开始处理
上传文档之前**先跑一遍本模块——识别到背景图片就当场摘掉，后续过滤 / 分析 / 出产物
全过程只面对"干净"的文档。

做法（只动该动的字节）
----------------------
``.xlsx`` 本质是 zip（OOXML 包），工作表背景图片由三部分组成：

    xl/worksheets/sheetN.xml              <picture r:id="rIdK"/>      ← 引用
    xl/worksheets/_rels/sheetN.xml.rels   rIdK → ../media/imageM.png ← 关系
    xl/media/imageM.png                   图片二进制本体

删除 = 摘掉 ``<picture>`` 元素 + 删掉对应的 Relationship + 删掉**不再被任何关系引用**的
图片部件。除此之外包内其余部件**逐字节原样搬运**（不重排、不重压、不改时间戳），
所以样式、批注、宏、数据透视表、图片（非背景的那些）一概不受影响。图片本体若同时被
别处引用（例如同一张图既当背景又当浮动 logo），只解除背景引用、保留图片本体。

边界与安全
----------
- **未发现背景图片 → 原字节返回**：连 zip 都不重写，对存量文件零风险；
- **任何异常都不阻断上传**：文件照原样继续走流程，只把原因写进报告的 ``note``
  （背景清理是增值环节，不该让一个畸形文件把整条业务链路卡死）；
- ``.csv`` / ``.xls`` 不做检测：CSV 是纯文本没有图片容器；``.xls`` 是 BIFF 二进制流，
  本模块不解析它，且这两个格式在本框架里都是"读成二维表 → 重新生成输出文件"，
  产物天然不含背景图片（报告里如实说明，不含糊其辞）。

维护约定（重要）
----------------
规范 B-7 禁止插件之间 import 后端模块，因此本文件在 ``file-filter`` 与
``trajectory-sketch`` 两个插件内**各存一份完全相同的副本**（纯标准库，零插件依赖）。
改动必须两处同步；``test_bg_image.py`` 会断言两份副本字节一致，防止单边改动造成漂移。
"""
from __future__ import annotations

import io
import posixpath
import re
import zipfile
from typing import Any, Dict, List, Optional, Set, Tuple

#: 模块版本（两份副本必须相同，由 test_bg_image.py 断言）
MODULE_VERSION = "1.0.0"

#: 解压后总量上限（防 zip 炸弹；上传侧另有 20MB 原始大小限制）
MAX_TOTAL_UNCOMPRESSED = 256 * 1024 * 1024

#: 支持"识别 + 删除"的格式（其余格式返回原字节 + 说明）
SCANNED_EXTS = {"xlsx"}

_IMAGE_REL_TYPE = "/image"

_RELS_DIR = "_rels/"

#: 工作表部件：xl/worksheets/*.xml（排除 _rels）
_SHEET_PART_RE = re.compile(r"^xl/worksheets/[^/]+\.xml$", re.I)

#: 背景图片引用元素（无子元素的叶子节点，两种写法都收）
_PICTURE_RE = re.compile(rb"<picture\b[^>]*/>|<picture\b[^>]*>\s*</picture\s*>", re.I)

#: Relationship 元素（自闭合）
_REL_ELEMENT_RE = re.compile(rb"<Relationship\b[^>]*/>", re.I)

#: 命名空间前缀无关的属性取值（XML 属性名大小写敏感，故不加 re.I）
_ATTR_TMPL = r"(?<![\w:.\-])(?:[A-Za-z_][\w.\-]*:)?%s\s*=\s*[\"']([^\"']*)[\"']"

#: <sheet .../> 元素（workbook.xml 里工作表清单）
_SHEET_ELEMENT_RE = re.compile(rb"<sheet\b[^>]*?/?>")


class BackgroundImageError(Exception):
    """背景图片清理失败（信息可直接展示，SEC-5：不透出堆栈与路径）。"""


# --------------------------------------------------------------------------
# 属性 / 关系解析
# --------------------------------------------------------------------------

def _attr(element: Any, name: str) -> str:
    """取元素上某属性（忽略命名空间前缀），取不到返回空串。

    ``<sheet name="S1" sheetId="1" r:id="rId1"/>`` 里取 ``id`` 只会拿到 ``rId1``：
    正则带前视断言，不会把 ``sheetId`` 误当成 ``id``。
    """
    text = element.decode("utf-8", "replace") if isinstance(element, (bytes, bytearray)) else str(element)
    m = re.search(_ATTR_TMPL % re.escape(name), text)
    return m.group(1) if m else ""


def _parse_relationships(data: Optional[bytes]) -> List[Dict[str, str]]:
    """解析 ``.rels`` 部件里的 Relationship 列表（保留原始元素文本供改写用）。"""
    out: List[Dict[str, str]] = []
    if not data:
        return out
    for m in _REL_ELEMENT_RE.finditer(data):
        raw = m.group(0)
        out.append({
            "raw": raw.decode("utf-8", "replace"),
            "id": _attr(raw, "Id"),
            "type": _attr(raw, "Type"),
            "target": _attr(raw, "Target"),
            "mode": _attr(raw, "TargetMode"),
        })
    return out


def _rels_part_for(part: str) -> str:
    """部件名 → 其关系部件名（``xl/worksheets/sheet1.xml`` → ``xl/worksheets/_rels/sheet1.xml.rels``）。"""
    folder, _, filename = part.rpartition("/")
    return "%s/%s%s.rels" % (folder, _RELS_DIR, filename) if folder else "%s%s.rels" % (_RELS_DIR, filename)


def _part_of_rels(rels_part: str) -> str:
    """关系部件名 → 被描述部件名（``xl/_rels/workbook.xml.rels`` → ``xl/workbook.xml``）。"""
    folder, _, filename = rels_part.rpartition("/")
    if folder.endswith("_rels"):
        parent = folder[:-len("_rels")].rstrip("/")
        target = filename[:-len(".rels")]
        return "%s/%s" % (parent, target) if parent else target
    return ""


def _resolve_target(base_part: str, target: str) -> str:
    """把关系里的 Target 解析为包内部件名（支持 ``../media/x.png`` / ``/xl/media/x.png``）。"""
    path = (target or "").replace("\\", "/").split("#", 1)[0]
    if not path:
        return ""
    if path.startswith("/"):
        return posixpath.normpath(path.lstrip("/"))
    return posixpath.normpath(posixpath.join(posixpath.dirname(base_part), path))


def _is_external(rel: Dict[str, str]) -> bool:
    return rel.get("mode", "").strip().lower() == "external"


def _is_image(rel: Dict[str, str]) -> bool:
    return rel.get("type", "").lower().endswith(_IMAGE_REL_TYPE)


# --------------------------------------------------------------------------
# 报告文本
# --------------------------------------------------------------------------

def _kb(n: int) -> str:
    return "%.1f KB" % (n / 1024.0) if n >= 1024 else "%d B" % n


def _join_names(names: List[str], limit: int = 3) -> str:
    uniq = []
    for n in names:
        if n and n not in uniq:
            uniq.append(n)
    if not uniq:
        return ""
    if len(uniq) > limit:
        return "、".join(uniq[:limit]) + " 等 %d 个工作表" % len(uniq)
    return "、".join(uniq)


def _build_note(report: Dict[str, Any]) -> str:
    ext = report.get("ext") or ""
    if not report.get("scanned"):
        if ext == "csv":
            return "CSV 为纯文本格式，不含嵌入图片"
        if ext == "xls":
            return "旧版 .xls 不做背景图片检测（输出文件为重新生成的 .xlsx，不含背景图片）"
        return report.get("reason") or "未做背景图片检测"
    found, removed = report.get("found") or 0, report.get("removed") or 0
    kept_parts = report.get("kept_parts") or 0
    if not found:
        note = "未发现背景图片"
        if report.get("other_images"):
            note += "（另有 %d 张普通嵌入图片，非背景，未删除）" % report["other_images"]
        return note
    if removed:
        sheets = _join_names([s for img in (report.get("images") or []) if not img.get("kept")
                              for s in (img.get("sheets") or [])])
        note = "已删除背景图片 %d 张（%s）" % (removed, _kb(report.get("bytes") or 0))
        if sheets:
            note += "，来自工作表 %s" % sheets
        if kept_parts:
            note += "；另有 %d 处引用因图片本体被其他位置使用，只解除引用不删图" % kept_parts
        if report.get("other_images"):
            note += "（另有 %d 张普通嵌入图片未删除）" % report["other_images"]
        return note
    if kept_parts:
        return "已解除背景图片引用 %d 处（图片本体仍被其他位置引用，已保留）" % found
    return "检测到 %d 处背景图片引用，但图片本体不在文件内，已解除引用" % found


# --------------------------------------------------------------------------
# 主入口
# --------------------------------------------------------------------------

def strip_background_images(blob: bytes, ext: str) -> Tuple[bytes, Dict[str, Any]]:
    """识别并删除文档里嵌入的背景图片。

    参数
    ----
    blob : 上传文件的原始字节（调用方已完成大小上限校验）
    ext  : 文件扩展名（``xlsx`` / ``xls`` / ``csv``，大小写与前导点均可）

    返回 ``(处理后的字节, 报告)``。报告字段：

    ============  ==========================================================
    scanned       是否真的做了扫描
    found         识别到的背景图片处数
    removed       实际删除的张数
    bytes         删除的图片字节数合计
    images        ``[{part, bytes, sheets, rel, kept}]``（kept=本体因被别处引用而保留）
    other_images  非背景的普通嵌入图片张数（未删除，仅提示）
    note          可直接展示的中文结论
    ============  ==========================================================
    """
    ext = (ext or "").strip().lower().lstrip(".")
    report: Dict[str, Any] = {
        "ext": ext, "scanned": False, "found": 0, "removed": 0, "bytes": 0,
        "images": [], "other_images": 0, "kept_parts": 0, "note": "",
    }
    if ext not in SCANNED_EXTS:
        report["note"] = _build_note(report)
        return blob, report
    if not blob:
        report["reason"] = "文件为空，未做检测"
        report["note"] = _build_note(report)
        return blob, report

    try:
        clean, report = _strip_xlsx(blob, report)
    except BackgroundImageError as exc:
        report["reason"] = str(exc)
        report["note"] = _build_note(report)
        return blob, report
    except Exception:                                  # 兜底：绝不阻断上传
        report["reason"] = "背景图片检测失败，已按原文件继续处理"
        report["note"] = _build_note(report)
        return blob, report
    report["note"] = _build_note(report)
    return clean, report


# --------------------------------------------------------------------------
# xlsx 实现
# --------------------------------------------------------------------------

def _read_all(zin: zipfile.ZipFile, infos: List[zipfile.ZipInfo]) -> Dict[str, bytes]:
    return {i.filename: zin.read(i) for i in infos if not i.is_dir()}


def _read_rels(zin: zipfile.ZipFile, infos: List[zipfile.ZipInfo]) -> Dict[str, bytes]:
    """只读 ``.rels`` 部件（统计"普通嵌入图片"用；比整包读取便宜得多）。"""
    return {i.filename: zin.read(i) for i in infos
            if not i.is_dir() and i.filename.lower().endswith(".rels")}


def _sheet_display_names(parts: Dict[str, bytes]) -> Dict[str, str]:
    """``{工作表部件名: 用户看到的工作表名}``（解析失败时返回空表，不影响删除）。"""
    names: Dict[str, str] = {}
    workbook = parts.get("xl/workbook.xml")
    rels = parts.get("xl/_rels/workbook.xml.rels")
    if not workbook or not rels:
        return names
    rid2part = {}
    for rel in _parse_relationships(rels):
        if rel["id"] and not _is_external(rel):
            rid2part[rel["id"]] = _resolve_target("xl/workbook.xml", rel["target"])
    for m in _SHEET_ELEMENT_RE.finditer(workbook):
        part = rid2part.get(_attr(m.group(0), "id"))
        if part:
            names[part] = _attr(m.group(0), "name") or part
    return names


def _count_other_images(parts: Dict[str, bytes], background_parts: Set[str]) -> int:
    """统计非背景的普通嵌入图片（仍被图片关系引用、且不属于被摘掉的背景）张数。"""
    seen: Set[str] = set()
    for name, data in parts.items():
        if not name.lower().endswith(".rels"):
            continue
        base = _part_of_rels(name)
        for rel in _parse_relationships(data):
            if _is_image(rel) and not _is_external(rel) and rel["target"]:
                part = _resolve_target(base, rel["target"])
                if part and part not in background_parts:
                    seen.add(part)
    return len(seen)


def _strip_xlsx(blob: bytes, report: Dict[str, Any]) -> Tuple[bytes, Dict[str, Any]]:
    try:
        zin = zipfile.ZipFile(io.BytesIO(blob))
    except Exception:
        report["reason"] = "文件不是有效的 xlsx 压缩包，未做背景图片检测"
        return blob, report
    with zin:
        infos = zin.infolist()
        if sum(i.file_size for i in infos) > MAX_TOTAL_UNCOMPRESSED:
            raise BackgroundImageError("文件解压后超过 256MB，已跳过背景图片检测")
        if any(i.flag_bits & 0x1 for i in infos):
            raise BackgroundImageError("压缩包已加密，无法读取内容，已跳过背景图片检测")
        # 先只读工作表部件：绝大多数文档没有背景图片，命中不了就直接原样返回
        sheet_parts = [i.filename for i in infos
                       if not i.is_dir() and _SHEET_PART_RE.match(i.filename)]
        hits = {name: zin.read(name) for name in sheet_parts}
        hits = {name: data for name, data in hits.items() if _PICTURE_RE.search(data)}
        if not hits:
            report["scanned"] = True
            report["other_images"] = _count_other_images(_read_rels(zin, infos), set())
            return blob, report
        parts = _read_all(zin, infos)

    # ---- 逐张摘除背景图片引用 ----
    sheet_names = _sheet_display_names(parts)
    removed_targets: List[str] = []                    # 待判定是否删除的图片部件
    for name in sorted(hits):
        data = hits[name]
        rel_ids: List[str] = []
        for m in _PICTURE_RE.finditer(data):
            rid = _attr(m.group(0), "id")
            if rid and rid not in rel_ids:
                rel_ids.append(rid)
        parts[name] = _PICTURE_RE.sub(b"", data)       # 摘掉 <picture> 元素

        rels_name = _rels_part_for(name)
        rels_data = parts.get(rels_name)
        by_id = {rel["id"]: rel for rel in _parse_relationships(rels_data)}
        for rid in rel_ids:
            rel = by_id.get(rid) or {}
            part = _resolve_target(name, rel.get("target", ""))
            body = parts.get(part) if part else None
            if part and body is not None and _is_image(rel) and not _is_external(rel):
                removed_targets.append(part)
                report["found"] += 1
                report["images"].append({
                    "part": part, "bytes": len(body), "rel": rid,
                    "sheets": [sheet_names.get(name, name)], "kept": False,
                })
            else:                                      # 引用存在但本体不在包内
                report["found"] += 1
                report["images"].append({
                    "part": part or "", "bytes": 0, "rel": rid,
                    "sheets": [sheet_names.get(name, name)], "kept": True,
                })

        if rels_data:
            keep_ids = set(rel_ids)
            kept_rels = [rel["raw"] for rel in _parse_relationships(rels_data) if rel["id"] not in keep_ids]
            if kept_rels:
                new_rels = _drop_relationships(rels_data, keep_ids)
                parts[rels_name] = new_rels
            else:
                parts.pop(rels_name, None)             # 关系已清空 → 整个 .rels 部件也删掉

    # ---- 只删除"不再被任何关系引用"的图片本体 ----
    still_referenced: Set[str] = set()
    for name, data in parts.items():
        if not name.lower().endswith(".rels"):
            continue
        base = _part_of_rels(name)
        for rel in _parse_relationships(data):
            if not _is_external(rel) and rel["target"]:
                still_referenced.add(_resolve_target(base, rel["target"]))
    dropped: Set[str] = set()
    kept_parts: Set[str] = set()
    for part in removed_targets:
        (kept_parts if part in still_referenced else dropped).add(part)
    for img in report["images"]:
        if img["part"] in kept_parts:
            img["kept"] = True                         # 本体被别处引用，只解除背景引用
    report["removed"] = len(dropped)
    report["kept_parts"] = len(kept_parts)
    # 体积按"实际删掉的图片本体"统计（同一张图被多个工作表引用只算一次）
    report["bytes"] = sum(len(parts[p]) for p in sorted(dropped) if p in parts)

    report["scanned"] = True
    report["images"] = [img for img in report["images"] if img["part"]]
    report["other_images"] = _count_other_images(parts, set(removed_targets))
    parts = {name: data for name, data in parts.items() if name not in dropped}
    parts = _drop_content_type_overrides(parts, dropped)
    return _rewrite_zip(infos, parts, zin.comment), report


def _drop_relationships(data: bytes, drop_ids: Set[str]) -> bytes:
    """按 Id 删掉指定的 Relationship 元素（其余字节原样保留）。"""
    def repl(m):
        return b"" if _attr(m.group(0), "Id") in drop_ids else m.group(0)
    return _REL_ELEMENT_RE.sub(repl, data)


def _drop_content_type_overrides(parts: Dict[str, bytes], dropped: Set[str]) -> Dict[str, bytes]:
    """删掉被删除部件在 [Content_Types].xml 里的 Override 登记（Default 规则无需处理）。"""
    ct = parts.get("[Content_Types].xml")
    if not ct or not dropped:
        return parts
    targets = {"/" + p for p in dropped}
    def repl(m):
        return b"" if _attr(m.group(0), "PartName") in targets else m.group(0)
    new_ct = re.sub(rb"<Override\b[^>]*/>", repl, ct)
    if new_ct != ct:
        parts = dict(parts)
        parts["[Content_Types].xml"] = new_ct
    return parts


def _rewrite_zip(infos: List[zipfile.ZipInfo], parts: Dict[str, bytes], comment: bytes) -> bytes:
    """按原顺序重写 zip：``parts`` 里没有的部件即视为已删除。

    其余部件连同时间戳、压缩方式逐字节搬运，包注释也一并保留。
    """
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        dst.comment = comment
        for info in infos:
            if info.is_dir():
                dst.writestr(_clone_info(info), b"")
                continue
            data = parts.get(info.filename)
            if data is None:                           # 空 .rels 等被整部件删除
                continue
            dst.writestr(_clone_info(info), data, compress_type=info.compress_type)
    return out.getvalue()


def _clone_info(info: zipfile.ZipInfo) -> zipfile.ZipInfo:
    """克隆 zip 条目元信息。

    刻意**不复制 flag_bits**：源包若带 data descriptor 标志位，而写出时并不写
    descriptor，沿用该标志会产出损坏的 zip（读取方会一路读到错误的长度）。
    """
    new = zipfile.ZipInfo(info.filename, date_time=info.date_time)
    new.compress_type = info.compress_type
    new.external_attr = info.external_attr
    new.internal_attr = info.internal_attr
    new.create_system = info.create_system
    new.comment = info.comment
    return new
