"""IR（中间表示）类型定义 —— 对应 TS 版 ``packages/core/src/model.ts``，与设计文档第 5 章一致。

约定：
  1. 坐标全部使用 0-based 的 {row, col}，仅在与 XML 交互时转 A1 记法；
  2. 单位约定：IR 内部一律使用「已换算的最终值」（像素 px 或 磅 pt），
     不存 twip / EMU / 字符宽等原始单位 —— 换算只在解析层做一次。
  3. Python 版差异：``CellModel.v`` 不再使用 ``Date`` 对象 —— 日期就是原始
     序列号（number），从源头消除「Date ↔ 序列号」双向换算的坑（TS 版第 13.2 节修正）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union

from .a1 import Rect

# ─────────────────────────────────────────────────────────────
# 单元格
# ─────────────────────────────────────────────────────────────

#: 单元格值类型（对齐 Excel / ECMA-376）：
#:   empty 空 / n 数字（含日期序列号）/ s 共享字符串 / str 公式缓存字符串 /
#:   b 布尔 / e 错误值 / inlineStr 内联字符串 / d 日期（数值型序列号 + 日期格式码）
CellType = str


@dataclass
class RichRun:
    """富文本片段（单元格内多段不同样式）。"""

    text: str
    #: 指向 styleTable 的下标（仅字体相关属性生效）；与 font 二选一
    style_id: Optional[int] = None
    #: 直接内联的字体属性（Partial[FontStyle]）
    font: Optional[dict] = None


@dataclass
class CellModel:
    row: int
    col: int
    type: CellType
    #: 原始值（数字/字符串/布尔/None）。日期保持为序列号。
    v: Union[str, int, float, bool, None] = None
    #: ★ 显示文本：已按 numFmt 格式化后的字符串。渲染器一律输出 w，不要输出 v。
    w: str = ""
    #: 原始公式串（只读展示用，不做计算）
    f: Optional[str] = None
    #: 样式表中的下标
    style_id: int = 0
    #: 富文本；存在时优先于 w 渲染
    rich_text: Optional[List[RichRun]] = None
    #: 合并信息：主单元格给出跨度；被覆盖的单元格为 'covered'（渲染时跳过）
    merge: Optional[Union[dict, str]] = None
    #: comments 数组下标
    comment_id: Optional[int] = None
    #: hyperlinks 数组下标
    hyperlink_id: Optional[int] = None
    #: 条件格式最终生效的样式（TS 版遗留字段；本实现直接改写 styleId）
    cf_style_id: Optional[int] = None
    #: WPS 嵌入图片：DISPIMG 函数引用的图片 id
    disp_img_id: Optional[str] = None


@dataclass
class RowModel:
    index: int
    #: 像素高度；未自定义时为 0（渲染时取 defaultRowHeightPx）
    height_px: int = 0
    hidden: bool = False
    #: 分级显示层级（1-8），0 表示无
    outline_level: int = 0
    collapsed: bool = False
    #: 稀疏存储：col → CellModel
    cells: Dict[int, CellModel] = field(default_factory=dict)
    #: 行级默认样式
    style_id: Optional[int] = None


@dataclass
class ColModel:
    index: int
    #: 像素宽度；0 表示未自定义
    width_px: int = 0
    hidden: bool = False
    outline_level: int = 0
    collapsed: bool = False
    style_id: Optional[int] = None


@dataclass
class FreezeModel:
    x_split: int = 0
    y_split: int = 0
    #: 滚动区左上角单元格（0-based）
    top_left_row: int = 0
    top_left_col: int = 0


@dataclass
class SheetViewModel:
    show_grid_lines: bool = True
    show_row_col_headers: bool = True
    #: 百分比 100 = 100%
    zoom: int = 100
    right_to_left: bool = False
    freeze: Optional[FreezeModel] = None
    tab_selected: bool = True


@dataclass
class PageSetupModel:
    orientation: str = "portrait"
    paper_size: str = "A4"
    margins: dict = field(default_factory=lambda: {"top": 0.75, "right": 0.7, "bottom": 0.75, "left": 0.7})
    fit_to_width: Optional[int] = None
    fit_to_height: Optional[int] = None
    print_area: Optional[str] = None
    print_titles: Optional[dict] = None
    #: 手动分页：行号数组（0-based，表示"在此行之前分页"）
    row_breaks: List[int] = field(default_factory=list)
    col_breaks: List[int] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────
# 条件格式
# ─────────────────────────────────────────────────────────────

#: cfRule type：cellIs / expression / containsText / ... / colorScale（V2）等
CfType = str


@dataclass
class CfRuleModel:
    type: CfType
    #: 适用范围（可多段）
    ranges: List[Rect] = field(default_factory=list)
    priority: int = 0
    operator: Optional[str] = None
    formulas: List[str] = field(default_factory=list)
    #: dxfs 下标（原生解析路径一般已展开为 patch）
    dxf_id: Optional[int] = None
    #: 命中后是否停止后续规则
    stop_if_true: bool = False
    #: 已解析的差异样式（字段粒度的部分覆盖语义）
    patch: Optional[dict] = None
    extra: Optional[dict] = None


@dataclass
class CommentModel:
    row: int
    col: int
    text: str = ""
    author: Optional[str] = None
    visible: bool = False


@dataclass
class HyperlinkModel:
    r0: int
    c0: int
    r1: int
    c1: int
    #: 已做白名单校验的 URL
    target: str
    tooltip: Optional[str] = None


@dataclass
class DrawingModel:
    id: str
    #: 'image' | 'shape' | 'chart' | 'unknown'
    kind: str
    media_id: Optional[str] = None
    #: twoCellAnchor：起止单元格 + 内部偏移（比例 0-1）
    anchor: Optional[dict] = None
    #: oneCellAnchor：只锚定左上，用像素尺寸
    one_cell: Optional[dict] = None


@dataclass
class MediaAsset:
    id: str
    mime: str
    #: base64（不含 data: 前缀，渲染时再拼）
    data_base64: Optional[str] = None
    url: Optional[str] = None
    width_px: Optional[int] = None
    height_px: Optional[int] = None


# ─────────────────────────────────────────────────────────────
# 样式模型（已完全解析、已去主题化）
# ─────────────────────────────────────────────────────────────

HAlign = str
VAlign = str


@dataclass
class FontStyle:
    name: str = "Calibri"
    size_pt: float = 11.0
    bold: bool = False
    italic: bool = False
    #: 0=无 1=单下划线 2=双下划线 33=会计单下划线 34=会计双下划线
    underline: int = 0
    strike: bool = False
    #: 已解析为 #RRGGBB
    color: str = "#000000"
    #: 'superscript' | 'subscript' | None
    vert_align: Optional[str] = None
    #: 'major' | 'minor' | None
    scheme: Optional[str] = None
    #: 字体回退链（渲染时拼接）
    fallback: Optional[str] = None


@dataclass
class FillStyle:
    #: 'none' | 'solid' | 'pattern' | 'gradient'
    type: str = "none"
    #: 纯色/图案前景色（solid 时即单元格底色）
    fg_color: Optional[str] = None
    #: 图案背景色（铺底色）
    bg_color: Optional[str] = None
    pattern: Optional[str] = None
    #: 渐变：角度（度）+ 色标
    gradient: Optional[dict] = None


@dataclass
class BorderSide:
    style: str = "thin"
    color: str = "#000000"


@dataclass
class BorderStyle:
    top: Optional[BorderSide] = None
    bottom: Optional[BorderSide] = None
    left: Optional[BorderSide] = None
    right: Optional[BorderSide] = None
    #: 斜边框：V2
    diagonal_up: Optional[BorderSide] = None
    diagonal_down: Optional[BorderSide] = None


@dataclass
class AlignmentStyle:
    h: HAlign = "general"
    v: VAlign = "bottom"
    wrap: bool = False
    shrink_to_fit: bool = False
    #: 缩进级别，1 级 ≈ 7px
    indent: int = 0
    #: 正数=逆时针，负数=顺时针，255=竖排单字
    text_rotation: int = 0
    #: 0=按上下文 1=从左到右 2=从右到左
    reading_order: int = 0


@dataclass
class NumFmtStyle:
    #: 内置 id（0-49）或自定义 id（>=164）
    id: int = 0
    #: 格式代码，如 "#,##0.00"
    code: str = "General"


@dataclass
class StyleModel:
    font: FontStyle = field(default_factory=FontStyle)
    fill: FillStyle = field(default_factory=FillStyle)
    border: BorderStyle = field(default_factory=BorderStyle)
    alignment: AlignmentStyle = field(default_factory=AlignmentStyle)
    num_fmt: NumFmtStyle = field(default_factory=NumFmtStyle)
    #: 只读渲染可忽略 locked；hidden=True 表示公式隐藏
    protection: dict = field(default_factory=lambda: {"locked": True, "hidden": False})
    #: 前导单引号（显示为文本）
    quote_prefix: bool = False
    #: 是否为"默认样式"（用于跳过冗余 CSS 输出）
    is_default: bool = False


# ─────────────────────────────────────────────────────────────
# 工作表与工作簿
# ─────────────────────────────────────────────────────────────


@dataclass
class SheetModel:
    id: str = ""
    name: str = ""
    index: int = 0
    #: 'visible' | 'hidden' | 'veryHidden'
    state: str = "visible"
    tab_color: Optional[str] = None
    #: 实际使用范围（0-based，闭区间）
    used_range: Rect = field(default_factory=lambda: Rect(0, 0, 0, 0))
    rows: List[RowModel] = field(default_factory=list)
    cols: List[ColModel] = field(default_factory=list)
    default_col_width_px: int = 64
    default_row_height_px: int = 20
    merges: List[Rect] = field(default_factory=list)
    views: SheetViewModel = field(default_factory=SheetViewModel)
    page: PageSetupModel = field(default_factory=PageSetupModel)
    conditional_formattings: List[CfRuleModel] = field(default_factory=list)
    drawings: List[DrawingModel] = field(default_factory=list)
    comments: List[CommentModel] = field(default_factory=list)
    hyperlinks: List[HyperlinkModel] = field(default_factory=list)
    auto_filter: Optional[Rect] = None
    data_validations: List[Rect] = field(default_factory=list)


@dataclass
class MetaInfo:
    file_hash: str = ""
    #: 'xlsx' | 'xls' | 'xlsm' | 'xlsb' | 'csv' | 'unknown'
    source_format: str = "xlsx"
    #: 若经过归一化，记录原始格式
    normalized_from: Optional[str] = None
    title: Optional[str] = None
    creator: Optional[str] = None
    last_modified_by: Optional[str] = None
    modified_at: Optional[str] = None
    #: 1904 日期系统标记
    date1904: bool = False
    #: 解析过程中的降级/告警，前端可提示用户
    warnings: List[str] = field(default_factory=list)
    stats: dict = field(default_factory=dict)


@dataclass
class ThemeInfo:
    #: dk1, lt1, dk2, lt2, accent1..accent6, hlink, folHlink → #RRGGBB
    color_scheme: Dict[str, str] = field(default_factory=dict)
    major_font: str = "Calibri Light"
    minor_font: str = "Calibri"


@dataclass
class WorkbookModel:
    meta: MetaInfo = field(default_factory=MetaInfo)
    theme: ThemeInfo = field(default_factory=ThemeInfo)
    #: 56 色索引调色板
    indexed_palette: List[str] = field(default_factory=list)
    #: 全局样式表（去重后），单元格只存下标
    style_table: List[StyleModel] = field(default_factory=list)
    sheets: List[SheetModel] = field(default_factory=list)
    media: List[MediaAsset] = field(default_factory=list)
    #: 自定义数字格式 id → code
    num_fmt_map: Dict[int, str] = field(default_factory=dict)
    #: WPS DISPIMG：图片名（ID_xxx）→ media 资源 id
    cell_image_map: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """供缓存 / 快照测试使用的纯 JSON 结构（去掉 media 二进制）。"""
        from dataclasses import asdict

        d = asdict(self)
        for m in d.get("media", []):
            m.pop("data_base64", None)
        return d
