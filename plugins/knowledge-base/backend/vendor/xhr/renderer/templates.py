"""HTML 骨架与基础 CSS —— 对应 TS 版 ``packages/renderer-dom/src/templates.ts``。

base CSS 只负责"表格骨架"（边框合并、内边距、冻结、隐藏、标签栏等），
字体/颜色/对齐等**全部由单元格自身的 class 提供** —— 样式来源唯一，可预测。
"""
from __future__ import annotations

import re
from typing import Optional

from ..core import WorkbookModel, esc

_HEXPREFIX_RE = re.compile(r"[^A-Za-z0-9_-]")


def base_css(prefix: str) -> str:
    """基础 CSS（不含单元格样式规则）。"""
    p = prefix
    return f"""
.{p}{{font-family:Calibri,Arial,"Segoe UI",Helvetica,"Microsoft YaHei","微软雅黑",sans-serif;font-size:13px;color:#111827;background:#fff}}
.{p} *{{box-sizing:border-box}}
.{p}-tabs{{display:flex;gap:2px;border-bottom:1px solid #D0D5DD;padding:0 4px;flex-wrap:wrap}}
.{p}-tab{{appearance:none;border:1px solid transparent;border-bottom:none;background:transparent;font:inherit;font-size:12px;padding:5px 12px;cursor:pointer;border-radius:6px 6px 0 0;color:#475467}}
.{p}-tab:hover{{background:#F2F4F7}}
.{p}-tab[aria-selected="true"]{{background:#fff;border-color:#D0D5DD;color:#1D4ED8;font-weight:700}}
.{p}-viewport{{overflow:auto;position:relative;max-height:80vh}}
.{p}-t{{border-collapse:collapse;border-spacing:0;table-layout:fixed}}
.{p}-t.{p}--sticky{{border-collapse:separate;border-spacing:0}}
.{p}-t td{{padding:0 2px;font-variant-numeric:tabular-nums;empty-cells:show;overflow:hidden}}
/*
 * Excel 默认显示网格线（浅灰 1px），所以这里也给 td 一个默认边框。
 * ⚠️ 选择器特异性很关键：默认网格用的是「元素+类」(.{p}-t td → 0,1,1)，
 *    而真实边框所在的样式类必须以「元素+双类」(.{p}-t td.{p}-cN → 0,2,1) 输出，
 *    否则默认网格会盖掉文档里真实的边框（曾实测到整表边框消失）。
 */
.{p}-t td{{border:1px solid #D9D9D9}}
.{p}-t.{p}--nogrid td{{border-color:transparent}}
/* 冻结：sticky 单元格必须有不透明背景，否则滚动时下层文字会透上来 */
.{p}-fr{{position:sticky;z-index:2}}
.{p}-fc{{position:sticky;z-index:1}}
.{p}-fx{{position:sticky;z-index:3}}
.{p}-fz{{background-color:#FFFFFF}}
.{p}-hid{{display:none}}
.{p}-truncated{{padding:8px 10px;font-size:12px;color:#854F0B;background:#FAEEDA;border-top:1px solid #FAC775}}
.{p}-warnings{{margin:0 0 8px;padding:8px 10px;font-size:12px;color:#854F0B;background:#FAEEDA;border-left:3px solid #D97706}}
/* ── 批注角标（右上小红三角 + 悬浮显示全文） ── */
.{p}-cm{{display:inline-block;width:0;height:0;border-top:5px solid #E11D48;border-left:5px solid transparent;vertical-align:top;margin-left:1px;cursor:help;position:relative;top:-1px}}
/* ── 单元格内嵌图片（WPS DISPIMG） ── */
.{p}-img{{max-width:100%;max-height:100%;object-fit:contain;vertical-align:middle}}
/* ── 浮动图片层：不拦截鼠标事件，避免挡住单元格文本选择 ── */
.{p}-drawings{{position:absolute;left:0;top:0;pointer-events:none;z-index:4}}
.{p}-drawings img{{position:absolute;object-fit:fill}}
/* ── 自动筛选箭头（纯装饰，无交互） ── */
.{p}-filter{{float:right;width:0;height:0;border-left:3px solid transparent;border-right:3px solid transparent;border-top:4px solid #6B7280;margin:4px 1px 0 2px}}
/* ── 超链接 ── */
.{p}-t a{{color:#1D4ED8;text-decoration:underline;text-underline-offset:2px}}
.{p}-zoom{{transform-origin:top left}}
/* ── 工具栏（runtime 注入：搜索 + 命中计数 + 打印按钮） ── */
.{p}-toolbar{{display:flex;gap:8px;align-items:center;margin:0 0 8px}}
.{p}-search{{flex:0 1 240px;padding:5px 10px;font:inherit;font-size:12px;border:1px solid #D0D5DD;border-radius:6px;background:#fff;color:#111827}}
.{p}-count{{font-size:12px;color:#475467}}
.{p}-print{{appearance:none;border:1px solid #D0D5DD;background:#fff;border-radius:6px;padding:5px 12px;font:inherit;font-size:12px;cursor:pointer;color:#475467}}
.{p}-print:hover{{background:#F2F4F7}}
/* 搜索命中：outline 不覆盖单元格底色（底色本身承载语义） */
.{p}-t td.{p}-hit{{outline:2px solid #F59E0B;outline-offset:-2px}}
/* 打印态（beforeprint 时 body 加 xhr-printing）：解除视口高度、隐藏工具栏 */
body.{p}-printing .{p}-viewport{{max-height:none;overflow:visible}}
body.{p}-printing .{p}-toolbar{{display:none}}
@media print{{
  .{p}-viewport{{max-height:none;overflow:visible}}
  .{p}-tabs{{display:none}}
  .{p}-t{{page-break-inside:auto}}
  .{p}-filter{{display:none}}
}}
""".strip()


def sanitize_prefix(prefix: object) -> str:
    """清理 CSS 前缀。

    ⚠️ 安全：前缀会被拼进 CSS 选择器与 HTML 的 class 属性。
    只保留 [A-Za-z0-9_-]，为空时回落到 xhr。
    """
    t = _HEXPREFIX_RE.sub("", prefix) if isinstance(prefix, str) else ""
    return t or "xhr"


def document_shell(css: str, body: str, title: str, runtime_script: Optional[str] = None, prefix: str = "xhr") -> str:
    """完整文档外壳。"""
    runtime = f"<script>\n{runtime_script}\n</script>\n" if runtime_script else ""
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="generator" content="xhr (XLSX/XLS to HTML renderer)">
<title>{esc(title)}</title>
<style>
{css}
</style>
</head>
<body style="margin:0;padding:12px;background:#F2F4F7">
<div class="{prefix}">
{body}
</div>
{runtime}</body>
</html>"""


def doc_title(model: WorkbookModel) -> str:
    t = (model.meta.title or "").strip()
    if t:
        return t
    first = model.sheets[0].name.strip() if model.sheets else ""
    return f"{first} - 预览" if first else "工作表预览"
