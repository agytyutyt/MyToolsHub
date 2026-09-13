"""@xhr/style —— 颜色、数字格式、日期、样式解析、去重、条件格式。"""
from .color import ColorCtx, apply_tint, hex_to_rgba, is_dark, normalize_hex6, resolve_color
from .cf import (
    UNSUPPORTED_TYPES,
    ApplyCfResult,
    apply_conditional_formatting,
    apply_style_patch,
    match_rule,
    resolve_dxf_patch,
)
from .date import (
    DATE1904_OFFSET,
    date_to_serial,
    has_date_part,
    has_time_part,
    is_date_format,
    is_numeric_format,
    is_text_format,
    serial_to_date,
    to_serial_1900,
)
from .default_theme import DEFAULT_THEME_COLORS, DEFAULT_THEME_FONTS, THEME_COLOR_NAMES, THEME_INDEX_TO_SCHEME
from .expr import compile_expr, eval_bool, is_supported_expr, shift_formula
from .indexed_palette import DEFAULT_INDEXED_PALETTE
from .numfmt import (
    BUILTIN_DATE_IDS,
    BUILTIN_NUMFMT,
    FormatValueResult,
    build_num_fmt_map,
    format_value,
    num_fmt_code_of,
)
from .style_dedup import StyleTable, style_key
from .style_resolver import (
    StyleContext,
    create_default_style,
    create_style_context,
    normalize_text_rotation,
    normalize_underline,
    resolve_alignment,
    resolve_border,
    resolve_fill,
    resolve_font,
    resolve_num_fmt,
    resolve_style,
    to_raw_color,
)

__all__ = [
    "ColorCtx", "apply_tint", "hex_to_rgba", "is_dark", "normalize_hex6", "resolve_color",
    "UNSUPPORTED_TYPES", "ApplyCfResult", "apply_conditional_formatting", "apply_style_patch",
    "match_rule", "resolve_dxf_patch",
    "DATE1904_OFFSET", "date_to_serial", "has_date_part", "has_time_part", "is_date_format",
    "is_numeric_format", "is_text_format", "serial_to_date", "to_serial_1900",
    "DEFAULT_THEME_COLORS", "DEFAULT_THEME_FONTS", "THEME_COLOR_NAMES", "THEME_INDEX_TO_SCHEME",
    "compile_expr", "eval_bool", "is_supported_expr", "shift_formula",
    "DEFAULT_INDEXED_PALETTE",
    "BUILTIN_DATE_IDS", "BUILTIN_NUMFMT", "FormatValueResult", "build_num_fmt_map",
    "format_value", "num_fmt_code_of",
    "StyleTable", "style_key",
    "StyleContext", "create_default_style", "create_style_context", "normalize_text_rotation",
    "normalize_underline", "resolve_alignment", "resolve_border", "resolve_fill", "resolve_font",
    "resolve_num_fmt", "resolve_style", "to_raw_color",
]
