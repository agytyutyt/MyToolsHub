"""分页 —— 对应《设计文档》6.10（T-A02）。

分页锚点优先级（照抄）：
  ① w:lastRenderedPageBreak   ← Word 自己记录的渲染后分页位置，保真最高、零成本
  ② w:br w:type="page"        ← 用户显式分页符
  ③ w:pPr/w:pageBreakBefore   ← 段前分页
  ④ 表格跨页切开               ← V2（V1 整表进当前页）
  ⑤ 动态溢出分页               ← V2（需浏览器测量）

页块结构：{'blocks': [...], 'props': SectionProps, 'page_no': int}
"""
from __future__ import annotations

from typing import List

from .model import DocumentModel, ParagraphModel, RunText


def _split_paragraph_at_page_break(para: ParagraphModel):
    """段内显式分页符：把 run 数组拆成两段，后半段进新页。"""
    cut = None
    for i, r in enumerate(para.runs):
        if r.kind == "break" and r.break_type == "page":
            cut = i
            break
    if cut is None:
        return [para]
    head = para
    tail = ParagraphModel(
        id=para.id + "-b",
        props=para.props.copy(),
        runs=para.runs[cut + 1 :],
        sect_pr=para.sect_pr,
        bookmarks=[],
    )
    head.runs = para.runs[:cut]
    head.sect_pr = None
    return [head, tail]


def paginate(model: DocumentModel, opts) -> List[dict]:
    """按锚点把 blocks 切成页；flow 模式返回单页。"""
    sections = model.sections or [{"props": None, "start_block_index": 0, "end_block_index": len(model.blocks)}]

    if opts.mode == "flow":
        blocks = list(model.blocks)
        return [{"blocks": blocks, "props": (sections[-1]["props"] if sections else None), "page_no": 0}]

    from copy import copy as _copy

    pages: List[dict] = []
    current_blocks: List = []
    current_section = sections[0]["props"]

    def flush() -> None:
        nonlocal current_blocks
        pages.append(
            {
                "blocks": current_blocks,
                "props": current_section,
                "page_no": len(pages),
            }
        )
        current_blocks = []

    for sec_info in sections:
        sec_props = sec_info["props"]
        start = sec_info["start_block_index"]
        end = min(sec_info["end_block_index"], len(model.blocks))
        if end < start:
            end = start
        # 新节开始总是新页
        if current_blocks:
            flush()
        current_section = sec_props

        for idx in range(start, end):
            b = model.blocks[idx]
            if isinstance(b, ParagraphModel):
                pieces = _split_paragraph_at_page_break(b)
                for j, piece in enumerate(pieces):
                    if j > 0:
                        # 分页符之后的内容进新页
                        if current_blocks:
                            flush()
                    # ① lastRenderedPageBreak
                    if piece.last_rendered_page_break and current_blocks:
                        flush()
                    # ③ 段前分页
                    if piece.props.page_break_before and current_blocks:
                        flush()
                    current_blocks.append(piece)
            else:
                current_blocks.append(b)
    flush()
    if not pages:
        pages.append({"blocks": [], "props": current_section, "page_no": 0})
    return pages
