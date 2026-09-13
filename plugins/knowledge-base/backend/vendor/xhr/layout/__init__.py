"""@xhr/layout —— 列宽行高、合并索引、冻结窗格。"""
from .freeze import FreezePlan, freeze_from_view, plan_freeze
from .merge import MAX_COVERED_CELLS, MergeIndex, build_merge_index, is_covered_by, normalize_merges
from .sizes import SheetMetrics, compute_metrics, ensure_col, is_empty_row

__all__ = [
    "FreezePlan", "freeze_from_view", "plan_freeze",
    "MAX_COVERED_CELLS", "MergeIndex", "build_merge_index", "is_covered_by", "normalize_merges",
    "SheetMetrics", "compute_metrics", "ensure_col", "is_empty_row",
]
