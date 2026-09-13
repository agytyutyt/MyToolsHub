"""@xhr/core —— IR 类型、A1 记法、转义、限额、单位换算、错误类型。"""
from .a1 import RC, Rect, a1_range_to_rect, a1_to_rc, col_from_name, col_name, rc_to_a1, rect_to_a1
from .errors import ERROR_MESSAGE, XhrError, to_xhr_error
from .escape import SAFE_IMAGE_MIME, esc, esc_attr, esc_text, is_safe_image_mime, safe_url
from .limits import EXCEL_MAX_COL, EXCEL_MAX_ROW, LIMITS
from .model import (
    AlignmentStyle,
    BorderSide,
    BorderStyle,
    CellModel,
    CommentModel,
    CfRuleModel,
    DrawingModel,
    FillStyle,
    FontStyle,
    FreezeModel,
    HyperlinkModel,
    MediaAsset,
    MetaInfo,
    NumFmtStyle,
    PageSetupModel,
    RichRun,
    RowModel,
    SheetModel,
    SheetViewModel,
    StyleModel,
    ThemeInfo,
    WorkbookModel,
    ColModel,
)
from .units import (
    DEFAULT_COL_WIDTH_PX,
    DEFAULT_MDW,
    DEFAULT_ROW_HEIGHT_PX,
    build_offsets,
    col_width_to_px,
    emu_to_px,
    pt_to_px,
    px_to_col_width,
    px_to_pt,
    row_height_to_px,
)

__all__ = [
    "RC", "Rect", "a1_range_to_rect", "a1_to_rc", "col_from_name", "col_name", "rc_to_a1", "rect_to_a1",
    "ERROR_MESSAGE", "XhrError", "to_xhr_error",
    "SAFE_IMAGE_MIME", "esc", "esc_attr", "esc_text", "is_safe_image_mime", "safe_url",
    "EXCEL_MAX_COL", "EXCEL_MAX_ROW", "LIMITS",
    "AlignmentStyle", "BorderSide", "BorderStyle", "CellModel", "ColModel", "CommentModel",
    "CfRuleModel", "DrawingModel", "FillStyle", "FontStyle", "FreezeModel", "HyperlinkModel",
    "MediaAsset", "MetaInfo", "NumFmtStyle", "PageSetupModel", "RichRun", "RowModel",
    "SheetModel", "SheetViewModel", "StyleModel", "ThemeInfo", "WorkbookModel",
    "DEFAULT_COL_WIDTH_PX", "DEFAULT_MDW", "DEFAULT_ROW_HEIGHT_PX", "build_offsets",
    "col_width_to_px", "emu_to_px", "pt_to_px", "px_to_col_width", "px_to_pt", "row_height_to_px",
]
