"""renderer —— IR → HTML 字符串。

纯函数：输入 WorkbookModel + 选项，输出字符串。服务端 / 测试都能跑。
对应 TS 版 ``packages/renderer-dom/src/index.ts``。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Union

from ..core import SheetModel, WorkbookModel, esc_text
from .css import (
    border_to_css,
    decls_to_rule,
    effective_align,
    font_family,
    gradient_to_css,
    is_blank_style,
    pattern_to_css,
    sanitize_font_name,
    style_to_css_decls,
    vertical_align_wrapper,
    CJK_FALLBACK,
    LATIN_FALLBACK,
    MONO_FALLBACK,
)
from .runtime import runtime_script
from .table import (
    DEFAULT_RENDER_OPTIONS,
    RenderContext,
    RenderOptions,
    cell_content,
    cell_inner,
    render_sheet,
)
from .templates import base_css, document_shell, doc_title, sanitize_prefix

__all__ = [
    "RenderOptions", "DEFAULT_RENDER_OPTIONS", "RenderContext", "RenderResult",
    "render_workbook", "render_one_sheet", "render_sheet", "cell_content", "cell_inner",
    "runtime_script", "base_css", "sanitize_prefix", "document_shell", "doc_title",
    "border_to_css", "decls_to_rule", "effective_align", "font_family", "gradient_to_css",
    "is_blank_style", "pattern_to_css", "sanitize_font_name", "style_to_css_decls",
    "vertical_align_wrapper", "CJK_FALLBACK", "LATIN_FALLBACK", "MONO_FALLBACK",
]


@dataclass
class RenderResult:
    html: str
    #: 实际渲染的单元格数
    cell_count: int
    #: 样式类数量（体积观测用）
    class_count: int
    #: 是否发生截断
    truncated: bool


#: 只放行 #RRGGBB，杜绝把用户内容拼进 CSS 值
import re  # noqa: E402

_HEX_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")


def _safe_hex(c: Optional[str]) -> Optional[str]:
    return c if c and _HEX_COLOR.match(c) else None


def _pick_sheets(model: WorkbookModel, opts: RenderOptions) -> List[SheetModel]:
    # ⚠️ veryHidden 永不渲染（API 参考的明确约定）；includeHiddenSheets 只放开 hidden
    sheets = [
        s
        for s in model.sheets
        if s.state == "visible" or (opts.include_hidden_sheets and s.state == "hidden")
    ]
    if opts.sheet is not None:
        target = opts.sheet
        if isinstance(target, int):
            one = sheets[target] if 0 <= target < len(sheets) else None
        else:
            one = next((s for s in sheets if s.name == target), None)
        sheets = [one] if one else []
    return sheets


def render_workbook(model: WorkbookModel, options: Optional[RenderOptions] = None) -> RenderResult:
    """渲染整个工作簿。

    输出结构：
      <style>去重后的 CSS 类</style>
      <div class="xhr">
        <div class="xhr-tabs">…</div>
        <div class="xhr-sheet">…<table>…</table></div>
      </div>
    """
    opts = options or RenderOptions()
    # ⚠️ 前缀会被拼进 CSS 选择器与 class 属性，必须先清理
    opts.css_prefix = sanitize_prefix(opts.css_prefix or DEFAULT_RENDER_OPTIONS.css_prefix)
    p = opts.css_prefix
    ctx = RenderContext(model, opts)

    sheets = _pick_sheets(model, opts)
    nl = "\n" if opts.pretty else ""

    body: List[str] = []

    # 降级/告警提示（不静默）
    if model.meta.warnings:
        shown = "<br>".join(esc_text(w) for w in model.meta.warnings[:5])
        more = (
            f"<br>…另有 {len(model.meta.warnings) - 5} 条" if len(model.meta.warnings) > 5 else ""
        )
        body.append(
            f'<div class="{p}-warnings">该文件包含预览不完全支持的特性：<br>{shown}{more}</div>'
        )

    # 工作表标签（多表时才显示）
    if len(sheets) > 1:
        body.append(f'<div class="{p}-tabs" role="tablist">')
        for i, s in enumerate(sheets):
            tab = _safe_hex(s.tab_color)
            tab_style = f' style="border-top:3px solid {tab}"' if tab else ""
            selected = "true" if i == 0 else "false"
            body.append(
                f'<button class="{p}-tab" role="tab" aria-selected="{selected}" data-target="{i}"{tab_style}>{esc_text(s.name)}</button>'
            )
        body.append("</div>")

    # 表格主体
    cell_count = 0
    truncated = False
    for i, s in enumerate(sheets):
        r = render_sheet(s, i, ctx, visible=(i == 0))
        body.append(r.html)
        cell_count += r.cell_count
        truncated = truncated or r.truncated

    if not sheets:
        body.append(f'<div class="{p}-warnings">没有可渲染的工作表（全部为隐藏或被过滤）。</div>')

    # CSS：基础骨架 + 逐类规则
    css_lines: List[str] = [base_css(p)]
    for i, decls in enumerate(ctx.class_rules):
        if not decls:
            continue
        # ⚠️ 选择器必须是 `.xhr-t td.xhr-cN`（特异性 0,2,1）而不是 `.xhr-cN`（0,1,0）：
        #    baseCss 里的默认网格线是 `.xhr-t td`（0,1,1），若这里只用单类选择器，
        #    文档里的真实边框会被默认网格线盖掉，整表看起来都是细灰线。
        css_lines.append(f".{p}-t td.{p}-c{i}{{{decls}}}")
    css = "\n".join(css_lines)

    inner = nl.join(body)

    if opts.mode == "document":
        html = document_shell(
            css=css,
            body=inner,
            title=doc_title(model),
            runtime_script=runtime_script(p) if opts.with_runtime else None,
            prefix=p,
        )
    else:
        html = f'<style>\n{css}\n</style>\n<div class="{p}">\n{inner}\n</div>'

    return RenderResult(html=html, cell_count=cell_count, class_count=len(ctx.class_rules), truncated=truncated)


def render_one_sheet(
    model: WorkbookModel,
    sheet: Union[int, str],
    options: Optional[RenderOptions] = None,
) -> RenderResult:
    """只渲染某一张工作表（返回片段）。"""
    opts = options or RenderOptions()
    opts.sheet = sheet
    return render_workbook(model, opts)
