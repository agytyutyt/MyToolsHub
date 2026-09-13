"""DHR 中间表示（IR）—— 对应《设计文档》第 5 章（T-602）。

约定：
  · IR 内部一律使用「已换算的最终值」（px / pt），不存 twip / 半磅 / EMU；
  · RunProps / ParaProps 是**扁平的最终值**——继承合并只在 IR 构建阶段做一次，
    渲染层不再做任何继承计算；
  · 属性合并语义：``None`` 表示「未设置，继承」，显式 False 表示「显式关闭」
    （切换属性 w:b w:val="0" 的关键）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Union

# ─────────────────────────────────────────────────────────────
# 属性对象（扁平最终值）
# ─────────────────────────────────────────────────────────────

#: RunProps 的全部字段（merge 用）
RUN_FIELDS = (
    "font_latin", "font_east_asia", "font_cs", "font_hint",
    "size_pt", "bold", "italic",
    "underline", "underline_color", "strike", "dstrike",
    "color", "highlight", "shd_fill", "shd_color", "shd_pattern",
    "letter_spacing_pt", "position_pt", "vert_align",
    "caps", "small_caps", "vanish", "lang", "em", "rtl",
)

PARA_FIELDS = (
    "style_id", "align",
    "indent_left_px", "indent_right_px",
    "first_line_px", "first_line_em", "hanging_px", "hanging_em",
    "space_before_pt", "space_after_pt", "line_spacing",
    "borders", "shd_fill", "tabs", "vertical",
    "snap_to_grid", "keep_next", "keep_lines", "page_break_before",
    "widow_control", "outline_lvl", "mark_run_props",
    "bidi", "contextual_spacing",
)


@dataclass
class RunProps:
    """字符属性（合并后的最终值；None=继承默认）。"""

    font_latin: Optional[str] = None
    font_east_asia: Optional[str] = None
    font_cs: Optional[str] = None
    font_hint: Optional[str] = None
    size_pt: Optional[float] = None
    bold: Optional[bool] = None
    italic: Optional[bool] = None
    #: 'single'/'double'/'dotted'/'dashed'/'wave'/... （none=None）
    underline: Optional[str] = None
    underline_color: Optional[str] = None
    strike: Optional[bool] = None
    dstrike: Optional[bool] = None
    color: Optional[str] = None
    #: 16 个 Word 命名高亮色名
    highlight: Optional[str] = None
    shd_fill: Optional[str] = None
    shd_color: Optional[str] = None
    shd_pattern: Optional[str] = None
    letter_spacing_pt: Optional[float] = None
    position_pt: Optional[float] = None
    #: 'superscript' | 'subscript'
    vert_align: Optional[str] = None
    caps: Optional[bool] = None
    small_caps: Optional[bool] = None
    vanish: Optional[bool] = None
    lang: Optional[str] = None
    #: 'emboss'|'imprint'|'outline'|'shadow'（CSS 无对应，记录不渲染）
    em: Optional[str] = None
    rtl: Optional[bool] = None

    def copy(self) -> "RunProps":
        out = RunProps(**{k: getattr(self, k) for k in RUN_FIELDS})
        for k, v in self.__dict__.items():
            if k.startswith("_"):
                out.__dict__[k] = v
        return out


@dataclass
class BorderSpec:
    style: str = "single"
    width_px: int = 1
    space_pt: float = 0.0
    color: str = "#000000"


@dataclass
class Borders:
    top: Optional[BorderSpec] = None
    left: Optional[BorderSpec] = None
    bottom: Optional[BorderSpec] = None
    right: Optional[BorderSpec] = None
    between: Optional[BorderSpec] = None
    bar: Optional[BorderSpec] = None


@dataclass
class TabStop:
    pos_px: float
    val: str = "left"
    leader: Optional[str] = None


@dataclass
class ParaProps:
    style_id: Optional[str] = None
    #: left/center/right/both/distribute/start/end
    align: Optional[str] = None
    indent_left_px: Optional[float] = None
    indent_right_px: Optional[float] = None
    first_line_px: Optional[float] = None
    #: ★ 中文首行缩进优先用 em（firstLineChars 换算），字号变化时跟着变
    first_line_em: Optional[float] = None
    hanging_px: Optional[float] = None
    hanging_em: Optional[float] = None
    space_before_pt: Optional[float] = None
    space_after_pt: Optional[float] = None
    #: {'kind': 'multiple'|'exact'|'atLeast', ...}
    line_spacing: Optional[dict] = None
    borders: Optional[Borders] = None
    shd_fill: Optional[str] = None
    tabs: Optional[List[TabStop]] = None
    vertical: Optional[bool] = None
    snap_to_grid: Optional[bool] = None
    keep_next: Optional[bool] = None
    keep_lines: Optional[bool] = None
    page_break_before: Optional[bool] = None
    widow_control: Optional[bool] = None
    #: 大纲级别 0-9（0=1 级标题）
    outline_lvl: Optional[int] = None
    #: 段落标记的字符属性（pPr/rPr，影响整段默认 run 样式）
    mark_run_props: Optional[RunProps] = None
    bidi: Optional[bool] = None
    contextual_spacing: Optional[bool] = None

    def copy(self) -> "ParaProps":
        out = ParaProps(**{k: getattr(self, k) for k in PARA_FIELDS})
        for k, v in self.__dict__.items():
            if k.startswith("_"):
                out.__dict__[k] = v
        return out


# ─────────────────────────────────────────────────────────────
# 行内内容（Run 可辨识联合）
# ─────────────────────────────────────────────────────────────


@dataclass
class RunText:
    kind: str = "text"
    text: str = ""
    props: Optional[RunProps] = None


@dataclass
class RunTab:
    kind: str = "tab"
    props: Optional[RunProps] = None


@dataclass
class RunBreak:
    kind: str = "break"
    #: 'textWrapping' | 'page' | 'column'
    break_type: str = "textWrapping"
    props: Optional[RunProps] = None


@dataclass
class RunSym:
    kind: str = "sym"
    char: str = ""
    font: str = ""
    props: Optional[RunProps] = None


@dataclass
class RunHyphen:
    kind: str = "noBreakHyphen"  # 或 'softHyphen'
    props: Optional[RunProps] = None


@dataclass
class RunDrawing:
    kind: str = "drawing"
    drawing: Optional["DrawingModel"] = None
    props: Optional[RunProps] = None


@dataclass
class RunField:
    kind: str = "field"
    field: Optional["FieldModel"] = None
    props: Optional[RunProps] = None


@dataclass
class RunNoteRef:
    kind: str = "footnoteRef"  # 或 'endnoteRef'
    note_id: int = 0
    props: Optional[RunProps] = None


@dataclass
class RunRuby:
    kind: str = "ruby"
    base: str = ""
    rt: str = ""
    props: Optional[RunProps] = None


Run = Union[RunText, RunTab, RunBreak, RunSym, RunHyphen, RunDrawing, RunField, RunNoteRef, RunRuby]


# ─────────────────────────────────────────────────────────────
# 图片 / 域 / 列表标记
# ─────────────────────────────────────────────────────────────


@dataclass
class DrawingModel:
    id: str = ""
    #: 'image' | 'shape' | 'chart' | 'textbox' | 'ole' | 'unknown'
    kind: str = "unknown"
    media_id: Optional[str] = None
    #: 'inline' | 'anchor'
    layout: str = "inline"
    width_px: int = 0
    height_px: int = 0
    #: 裁剪比例（左/上/右/下，0-1）
    crop: Optional[dict] = None
    anchor: Optional[dict] = None
    alt_text: Optional[str] = None


@dataclass
class FieldModel:
    instruction: str = ""
    #: PAGE/NUMPAGES/TOC/REF/PAGEREF/HYPERLINK/DATE/TIME/SECTION/NOTEREF/MERGEFIELD/unknown
    type: str = "unknown"
    cached_runs: Optional[List[Run]] = None
    target: Optional[str] = None
    dirty: bool = False


@dataclass
class ListMarker:
    #: 已展开的标记文本，如 "1."、"•"、"一、"
    text: str = ""
    num_fmt: str = "decimal"
    ilvl: int = 0
    props: Optional[RunProps] = None
    image_media_id: Optional[str] = None
    #: 标记后的间距（px）
    follow_gap_px: float = 0.0
    indent_px: float = 0.0
    jc: str = "left"


# ─────────────────────────────────────────────────────────────
# 块：段落 / 表格
# ─────────────────────────────────────────────────────────────


@dataclass
class ParagraphModel:
    kind: str = "paragraph"
    id: str = ""
    props: ParaProps = field(default_factory=ParaProps)
    list_marker: Optional[ListMarker] = None
    #: 1-6（由 outlineLvl / 内置样式名推导）
    heading_level: Optional[int] = None
    #: Word 记录的渲染后分页位置
    last_rendered_page_break: bool = False
    runs: List[Run] = field(default_factory=list)
    #: 段落末尾携带的分节符
    sect_pr: Optional["SectionProps"] = None
    bookmarks: List[dict] = field(default_factory=list)


@dataclass
class TableCellModel:
    width_px: Optional[float] = None
    colspan: int = 1
    rowspan: int = 1
    v_align: str = "top"
    shd_fill: Optional[str] = None
    borders: Optional[Borders] = None
    vertical: bool = False
    no_wrap: bool = False
    blocks: List["Block"] = field(default_factory=list)


@dataclass
class TableRowModel:
    height_px: Optional[float] = None
    #: 'auto' | 'atLeast' | 'exact'
    height_rule: str = "auto"
    cant_split: bool = False
    is_header: bool = False
    cells: List[TableCellModel] = field(default_factory=list)


@dataclass
class TableProps:
    width_px: Optional[float] = None
    width_pct: Optional[float] = None
    layout: str = "fixed"
    align: str = "left"
    indent_px: float = 0.0
    borders: Optional[Borders] = None
    cell_margin: dict = field(default_factory=lambda: {"top": 0.0, "left": 8.0, "bottom": 0.0, "right": 8.0})
    shd_fill: Optional[str] = None
    #: 列宽网格（px）→ <colgroup>
    grid: List[float] = field(default_factory=list)


@dataclass
class TableModel:
    kind: str = "table"
    props: TableProps = field(default_factory=TableProps)
    rows: List[TableRowModel] = field(default_factory=list)


Block = Union[ParagraphModel, TableModel]


# ─────────────────────────────────────────────────────────────
# 分节 / 家具 / 编号 / 顶层模型
# ─────────────────────────────────────────────────────────────


@dataclass
class SectionProps:
    page_size: Tuple[int, int] = (794, 1123)  # A4 纵向（px）
    orientation: str = "portrait"
    margins: dict = field(default_factory=lambda: {"top": 96.0, "right": 96.0, "bottom": 96.0, "left": 96.0, "header": 48.0, "footer": 48.0})
    content_width_px: float = 602.0
    cols: int = 1
    col_space_px: float = 0.0
    title_pg: bool = False
    pg_num_type: Optional[dict] = None
    header_refs: List[dict] = field(default_factory=list)   # {'type','part_id'}
    footer_refs: List[dict] = field(default_factory=list)


@dataclass
class FurnitureModel:
    part_id: str = ""
    #: 'header' | 'footer' | 'footnote' | 'endnote' | 'comment'
    type: str = "header"
    #: header/footer 的类型：'default' | 'first' | 'even'
    hf_type: str = "default"
    #: 脚注/尾注/批注的引用编号
    note_id: int = 0
    author: Optional[str] = None
    date: Optional[str] = None
    blocks: List[Block] = field(default_factory=list)


@dataclass
class NumberingLevel:
    num_fmt: str = "decimal"
    lvl_text: str = ""
    start: int = 1
    jc: str = "left"
    indent_px: float = 0.0
    hanging_px: float = 0.0
    props: Optional[RunProps] = None
    is_lgl: bool = False
    pic_bullet_media_id: Optional[str] = None


@dataclass
class NumberingDef:
    num_id: int = 0
    abstract_num_id: int = 0
    levels: Dict[int, NumberingLevel] = field(default_factory=dict)
    overrides: Dict[int, dict] = field(default_factory=dict)


@dataclass
class StyleDef:
    id: str = ""
    name: Optional[str] = None
    #: 'paragraph' | 'character' | 'table' | 'numbering'
    type: str = "paragraph"
    based_on: Optional[str] = None
    is_default: bool = False
    link: Optional[str] = None
    para: Optional[ParaProps] = None
    run: Optional[RunProps] = None


@dataclass
class DocumentModel:
    meta: dict = field(default_factory=dict)
    theme: dict = field(default_factory=dict)
    font_table: List[dict] = field(default_factory=list)
    #: docDefaults 解析后的默认属性
    defaults_run: RunProps = field(default_factory=RunProps)
    defaults_para: ParaProps = field(default_factory=ParaProps)
    styles: List[StyleDef] = field(default_factory=list)
    numbering: List[NumberingDef] = field(default_factory=list)
    #: 正文块序列（跨节连续）
    blocks: List[Block] = field(default_factory=list)
    sections: List[dict] = field(default_factory=list)  # {'props', 'start_block_index', 'end_block_index'}
    furniture: List[FurnitureModel] = field(default_factory=list)
    media: List[dict] = field(default_factory=list)     # {'id','mime','data_base64',...}
    toc: List[dict] = field(default_factory=list)
