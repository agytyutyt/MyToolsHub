"""XML 工具 —— 命名空间剥离 + 安全解析 + 关系表。

对应 TS 版 ``packages/parser-xlsx/src/raw.ts`` 里的 XMLParser 配置：
  · removeNSPrefix   → 这里在解析后统一剥离 {ns} 前缀；
  · trimValues:false → 保留 w:t / 文本节点的首尾空格（排版有意义）；
  · processEntities:false → stdlib ElementTree 不解析外部实体（防 XXE），
    未定义实体直接抛 ParseError → 当作损坏部件跳过。
"""
from __future__ import annotations

import posixpath
import xml.etree.ElementTree as ET
from typing import Iterable, Optional, Union

XmlInput = Union[bytes, bytearray, memoryview, str]

_REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


def _local(tag: str) -> str:
    return tag.rpartition("}")[2]


def parse_xml(data: Optional[XmlInput]) -> Optional[ET.Element]:
    """解析 XML；失败返回 None（调用方降级，绝不抛错）。

    ⚠️ 安全基线：
      · ElementTree 不解析外部实体 / DTD（防 XXE）；
      · 调用前已由 ingest 层做过条目数与解压体积限额（防放大攻击）。
    """
    if data is None or (isinstance(data, (bytes, bytearray, memoryview)) and not len(data)):
        return None
    try:
        root = ET.fromstring(bytes(data) if not isinstance(data, str) else data)
    except ET.ParseError:
        return None
    except ValueError:
        return None
    for el in root.iter():
        el.tag = _local(el.tag)
        if el.attrib:
            el.attrib = {_local(k): v for k, v in el.attrib.items()}
        # 剥离注释/PI 节点
    # 去掉 comment / PI 节点（tag 是函数对象，非 str）
    for parent in root.iter():
        for bad in [child for child in parent if not isinstance(child.tag, str)]:
            parent.remove(bad)
    return root


def child(el: Optional[ET.Element], name: str) -> Optional[ET.Element]:
    """第一个名为 name 的直接子元素。"""
    if el is None:
        return None
    for c in el:
        if c.tag == name:
            return c
    return None


def children(el: Optional[ET.Element], name: str) -> Iterable[ET.Element]:
    """所有名为 name 的直接子元素。"""
    if el is None:
        return []
    return [c for c in el if c.tag == name]


def text_of(el: Optional[ET.Element]) -> str:
    """元素的全部文本内容（含子孙节点）。"""
    if el is None:
        return ""
    return "".join(el.itertext())


def attr(el: Optional[ET.Element], name: str, default: str = "") -> str:
    if el is None:
        return default
    return el.get(name, default)


def attr_int(el: Optional[ET.Element], name: str, default: Optional[int] = None) -> Optional[int]:
    v = attr(el, name)
    if not v:
        return default
    try:
        return int(float(v))
    except ValueError:
        return default


def attr_float(el: Optional[ET.Element], name: str) -> Optional[float]:
    v = attr(el, name)
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        return None


def text_int(el: Optional[ET.Element], default: Optional[int] = None) -> Optional[int]:
    """子元素的文本 → int（如 <xdr:col>1</xdr:col>）。"""
    if el is None:
        return default
    t = (el.text or "").strip()
    if not t:
        return default
    try:
        return int(float(t))
    except ValueError:
        return default


def attr_bool(el: Optional[ET.Element], name: str) -> Optional[bool]:
    """OOXML 布尔属性：'1'/'true' → True；'0'/'false' → False；缺失 → None。"""
    v = attr(el, name)
    if v == "":
        return None
    if v in ("1", "true", "True"):
        return True
    if v in ("0", "false", "False"):
        return False
    return None


def parse_color_el(el: Optional[ET.Element]) -> Optional[dict]:
    """<color rgb= theme= indexed= tint= auto=> → raw color dict。"""
    if el is None:
        return None
    rgb = el.get("rgb")
    if rgb:
        return {"rgb": rgb}
    if el.get("theme") not in (None, ""):
        try:
            out: dict = {"theme": int(float(el.get("theme")))}
        except ValueError:
            return None
        tint = el.get("tint")
        if tint:
            try:
                out["tint"] = float(tint)
            except ValueError:
                pass
        return out
    if el.get("indexed") not in (None, ""):
        try:
            return {"indexed": int(float(el.get("indexed")))}
        except ValueError:
            return None
    if el.get("auto") is not None:
        return {"auto": 1}
    return None


def resolve_rel_target(base_dir: str, target: str) -> str:
    """关系表 Target → 包内完整路径。

    · '/xl/worksheets/sheet1.xml' → 绝对包路径
    · 'worksheets/sheet1.xml'     → 相对 base_dir 解析
    """
    t = (target or "").replace("\\", "/")
    if not t:
        return ""
    if t.startswith("/"):
        return posixpath.normpath(t[1:])
    return posixpath.normpath(posixpath.join(base_dir, t))


def read_relationships(files: dict[str, bytes], rels_path: str) -> dict[str, str]:
    """解析 .rels 文件：rId → 包内路径（跳过 External 目标）。"""
    root = parse_xml(files.get(rels_path))
    if root is None:
        return {}
    # rels 路径形如 'xl/_rels/workbook.xml.rels'，基准目录是 'xl'
    base_dir = posixpath.dirname(posixpath.dirname(rels_path))
    out: dict[str, str] = {}
    for rel in children(root, "Relationship"):
        rid = rel.get("Id") or ""
        target = rel.get("Target") or ""
        mode = rel.get("TargetMode") or ""
        if not rid or not target or mode.lower() == "external":
            continue
        out[rid] = resolve_rel_target(base_dir, target)
    return out
