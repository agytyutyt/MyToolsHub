"""dhr CLI —— 命令行文档转换（T-607）。"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from xhr.core import XhrError, to_xhr_error

from . import __version__, convert, ConvertOptions

USAGE = """dhr —— DOC / DOCX → HTML 重绘渲染引擎

用法:
  dhr convert <input.docx|.doc> [-o <output.html>] [options]
  dhr info     <input.docx|.doc>

通用选项:
  -o, --out <path>       输出 HTML 路径（默认与输入同名 .html）
      --mode <m>         flow | paged | print        （默认 flow）
      --css <m>          class | inline               （默认 class）
      --revisions <m>    final | original | markup    （默认 final）
      --no-headers       不渲染页眉
      --no-footers       不渲染页脚
      --no-footnotes     不渲染脚注尾注
      --comments         渲染批注（默认不渲染）
      --pretty           输出带缩进的 HTML（体积略大）
  -h, --help             显示帮助
  -v, --version          显示版本

示例:
  dhr convert 合同.docx -o 合同.html
  dhr convert 老文档.doc --mode flow
"""


def build_argparser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="dhr", description=USAGE, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["convert", "info"], nargs="?", default="convert")
    ap.add_argument("input", nargs="?")
    ap.add_argument("-o", "--out")
    ap.add_argument("--mode", choices=["flow", "paged", "print"], default="flow")
    ap.add_argument("--css", choices=["class", "inline"], default="class")
    ap.add_argument("--revisions", choices=["final", "original", "markup"], default="final")
    ap.add_argument("--no-headers", action="store_true")
    ap.add_argument("--no-footers", action="store_true")
    ap.add_argument("--no-footnotes", action="store_true")
    ap.add_argument("--comments", action="store_true")
    ap.add_argument("--pretty", action="store_true")
    ap.add_argument("-v", "--version", action="store_true")
    return ap


def _print_info(path: Path, model, ms: float) -> None:
    lines = []
    size_kb = path.stat().st_size / 1024
    meta = model.meta
    lines.append(f"文件：{path.name}（{size_kb:.1f}KB）")
    normalized = f"（{meta.get('normalizedFrom')} 归一化）" if meta.get("normalizedFrom") else ""
    lines.append(f"格式：{meta.get('sourceFormat')}{normalized}")
    if meta.get("title"):
        lines.append(f"标题：{meta['title']}")
    if meta.get("author"):
        lines.append(f"作者：{meta['author']}")
    lines.append(f"主题：major={model.theme.get('majorFont', {}).get('latin')} minor={model.theme.get('minorFont', {}).get('latin')}")
    stats = meta.get("stats", {})
    lines.append(f"规模：段落 {stats.get('paragraphCount', 0)} ｜ 表格 {stats.get('tableCount', 0)} ｜ 图片 {stats.get('imageCount', 0)}")
    lines.append(f"样式：{len(model.styles)} 种 ｜ 编号定义：{len(model.numbering)} 组 ｜ 节：{len(model.sections)}")
    if model.meta.get("warnings"):
        lines.append("")
        lines.append(f"降级提示（{len(model.meta['warnings'])}）：")
        for w in model.meta["warnings"][:10]:
            lines.append(f"  · {w}")
    lines.append("")
    lines.append(f"解析耗时 {ms:.0f}ms")
    sys.stdout.write("\n".join(lines) + "\n")


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or "-h" in argv or "--help" in argv:
        sys.stdout.write(USAGE)
        return 0
    if argv[0] in ("-v", "--version"):
        sys.stdout.write(f"dhr {__version__}\n")
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
        opts = ConvertOptions(
            mode=args.mode,
            css_mode=args.css,
            revision_mode=args.revisions,
            render_headers=not args.no_headers,
            render_footers=not args.no_footers,
            render_footnotes=not args.no_footnotes,
            render_comments=args.comments,
        )
        result = convert(data, opts)
        ms = (time.perf_counter() - t0) * 1000

        if args.command == "info":
            _print_info(path, result.model, ms)
            return 0

        out_path = Path(args.out) if args.out else path.with_suffix(".html")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(result.html, encoding="utf-8")
        ms = (time.perf_counter() - t0) * 1000
        sys.stdout.write(
            f"已渲染 {len(result.model.blocks)} 个块（段落 {result.model.meta['stats']['paragraphCount']} / "
            f"表格 {result.model.meta['stats']['tableCount']}）\n"
            f"输入 {len(data) / 1024:.1f}KB → 输出 {len(result.html.encode('utf-8')) / 1024:.1f}KB ｜ 耗时 {ms:.0f}ms\n"
            f"输出文件：{out_path}\n"
        )
        if result.model.meta.get("warnings"):
            for w in result.model.meta["warnings"][:8]:
                sys.stdout.write(f"  · {w}\n")
        return 0
    except XhrError as e:
        sys.stderr.write(f"错误[{e.code}]：{e}\n")
        if e.detail and os.environ.get("DHR_DEBUG"):
            sys.stderr.write(f"  详情：{e.detail}\n")
        return 1
    except Exception as e:  # noqa: BLE001
        err = to_xhr_error(e)
        sys.stderr.write(f"错误[{err.code}]：{err}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
