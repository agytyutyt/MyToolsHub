"""xhr CLI —— 命令行批量/单文件转换。对应 TS 版 ``packages/cli/src/index.ts``。"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from . import __version__, convert, ingest, parse_buffer
from .core import XhrError, to_xhr_error
from .normalize import normalize_xls_to_model
from .renderer import RenderOptions

USAGE = """xhr —— XLSX → HTML 重绘渲染引擎

用法:
  xhr convert <input.xlsx> [-o <output.html>] [options]
  xhr info    <input.xlsx>

通用选项:
  -o, --out <path>        输出 HTML 路径（默认与输入同名 .html）
      --mode <m>          document | fragment         （默认 document）
      --css <m>           class | inline               （默认 class）
      --freeze <m>        sticky | none                （默认 sticky）
      --sheet <n|name>    只渲染指定工作表
      --max-cells <n>     单元格渲染上限               （默认 200000）
      --no-media          不内联图片（大文件更快）
      --hidden            同时渲染隐藏工作表
      --with-runtime      内联客户端脚本（标签切换/搜索/打印）
      --fallback          .xls 强制走兜底通道（跳过 LibreOffice）
      --pretty            输出带缩进的 HTML
  -h, --help              显示帮助
  -v, --version           显示版本

示例:
  xhr convert 报表.xlsx -o 报表.html --pretty
  xhr info 报表.xlsx
"""


def build_argparser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="xhr", description=USAGE, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["convert", "info"], nargs="?", default="convert")
    ap.add_argument("input", nargs="?")
    ap.add_argument("-o", "--out")
    ap.add_argument("--mode", choices=["document", "fragment"], default="document")
    ap.add_argument("--css", choices=["class", "inline"], default="class")
    ap.add_argument("--freeze", choices=["sticky", "none"], default="sticky")
    ap.add_argument("--sheet")
    ap.add_argument("--max-cells", type=int, default=200_000)
    ap.add_argument("--no-media", action="store_true")
    ap.add_argument("--hidden", action="store_true")
    ap.add_argument("--with-runtime", action="store_true")
    ap.add_argument("--fallback", action="store_true")
    ap.add_argument("--pretty", action="store_true")
    ap.add_argument("-v", "--version", action="store_true")
    return ap


def _sheet_opt(value):
    if value is None:
        return None
    return int(value) if value.isdigit() else value


def _render_options(args) -> RenderOptions:
    return RenderOptions(
        mode=args.mode,
        css_mode=args.css,
        freeze_mode=args.freeze,
        sheet=_sheet_opt(args.sheet),
        max_cells=max(1, args.max_cells),
        include_hidden_sheets=args.hidden,
        inline_images=not args.no_media,
        with_runtime=args.with_runtime,
        pretty=args.pretty,
    )


def _print_info(path: Path, model, ms: float, via=None) -> None:
    lines = []
    size_mb = path.stat().st_size / 1024 / 1024
    lines.append(f"文件：{path.name}（{size_mb:.2f}MB）")
    meta = model.meta
    normalized = f"（{meta.normalized_from} 归一化）" if meta.normalized_from else ""
    channel = f" ｜ 通道：{via}" if via else ""
    date1904 = " [1904 日期系统]" if meta.date1904 else ""
    lines.append(f"格式：{meta.source_format}{normalized}{channel}{date1904}")
    if meta.title:
        lines.append(f"标题：{meta.title}")
    if meta.creator:
        lines.append(f"作者：{meta.creator}")
    lines.append(f"主题次要字体：{model.theme.minor_font}")
    lines.append(f"主题色：{' '.join(f'{k}={model.theme.color_scheme.get(k)}' for k in ('accent1', 'accent2', 'accent3'))}")
    lines.append(f"样式种类：{len(model.style_table)}")
    lines.append(f"图片：{len(model.media)}")
    lines.append("")
    lines.append("工作表：")
    for s in model.sheets:
        r = s.used_range
        state = f"（{s.state}）" if s.state != "visible" else ""
        freeze = f"{s.views.freeze.x_split}列/{s.views.freeze.y_split}行" if s.views.freeze else "无"
        lines.append(
            f"  [{s.index}] {s.name}{state} 范围 {r.r1 - r.r0 + 1} 行 × {r.c1 - r.c0 + 1} 列 ｜ "
            f"合并 {len(s.merges)} 处 ｜ 冻结 {freeze}"
        )
    if meta.warnings:
        lines.append("")
        lines.append(f"降级提示（{len(meta.warnings)}）：")
        for w in meta.warnings[:10]:
            lines.append(f"  · {w}")
    lines.append("")
    lines.append(f"解析耗时 {ms}ms")
    sys.stdout.write("\n".join(lines) + "\n")


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or "-h" in argv or "--help" in argv:
        sys.stdout.write(USAGE)
        return 0
    if argv[0] in ("-v", "--version"):
        sys.stdout.write(f"xhr {__version__}\n")
        return 0

    args = build_argparser().parse_args(argv)
    if not args.input:
        sys.stderr.write("错误：缺少输入文件\n\n" + USAGE)
        return 1
    path = Path(args.input)
    if not path.is_file():
        sys.stderr.write(f"错误：文件不存在：{args.input}\n")
        return 1

    t0 = time.perf_counter()
    try:
        data = path.read_bytes()
        size_mb = len(data) / 1024 / 1024
        ing = ingest(data)

        if ing.format == "xls":
            result = normalize_xls_to_model(data, None)
            # CLI 兜底通道提示（与 TS 版一致）
            from . import FALLBACK_HINT

            if result.via == "xlrd" and not any("兜底" in w for w in result.model.meta.warnings):
                result.model.meta.warnings.insert(0, FALLBACK_HINT)
            model, via = result.model, result.via
            ms = (time.perf_counter() - t0) * 1000

            if args.command == "info":
                _print_info(path, model, ms, via)
                return 0

            rendered = __import__("xhr").render_workbook(model, _render_options(args))
            out_path = Path(args.out) if args.out else path.with_suffix(".html")
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(rendered.html, encoding="utf-8")
            ms = (time.perf_counter() - t0) * 1000
            channel = "LibreOffice 归一化（完整保真）" if via == "libreoffice" else "xlrd 兜底（低保真）"
            sys.stdout.write(
                f"已渲染 {len(model.sheets)} 个工作表 / {rendered.cell_count} 个单元格 / {rendered.class_count} 种样式\n"
                f".xls 通道：{channel}\n"
                f"输入 {size_mb:.2f}MB → 输出 {len(rendered.html.encode('utf-8')) / 1024:.1f}KB ｜ 耗时 {ms:.0f}ms\n"
                f"输出文件：{out_path}\n"
            )
            for w in model.meta.warnings[:8]:
                sys.stdout.write(f"  · {w}\n")
            return 0

        if not ing.files:
            sys.stderr.write(f"错误：不支持的格式（识别为 {ing.format}）\n")
            return 1

        model = parse_buffer(data)
        ms = (time.perf_counter() - t0) * 1000

        if args.command == "info":
            _print_info(path, model, ms)
            return 0

        from . import render_workbook

        rendered = render_workbook(model, _render_options(args))
        out_path = Path(args.out) if args.out else path.with_suffix(".html")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(rendered.html, encoding="utf-8")
        ms = (time.perf_counter() - t0) * 1000
        sys.stdout.write(
            f"已渲染 {len(model.sheets)} 个工作表 / {rendered.cell_count} 个单元格 / {rendered.class_count} 种样式\n"
            f"输入 {size_mb:.2f}MB → 输出 {len(rendered.html.encode('utf-8')) / 1024:.1f}KB ｜ 耗时 {ms:.0f}ms\n"
            f"输出文件：{out_path}\n"
        )
        if model.meta.warnings:
            sys.stdout.write(f"\n注意：{len(model.meta.warnings)} 条降级提示\n")
            for w in model.meta.warnings[:8]:
                sys.stdout.write(f"  · {w}\n")
        return 0
    except XhrError as e:
        sys.stderr.write(f"错误[{e.code}]：{e}\n")
        if e.detail and os.environ.get("XHR_DEBUG"):
            sys.stderr.write(f"  详情：{e.detail}\n")
        return 1
    except Exception as e:  # noqa: BLE001
        err = to_xhr_error(e)
        sys.stderr.write(f"错误[{err.code}]：{err}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
