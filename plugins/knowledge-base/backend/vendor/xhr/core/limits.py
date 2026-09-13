"""解析限额 —— 对应 TS 版 ``packages/core/src/limits.ts``。

防 Zip Bomb / 超大文件 / 畸形文件导致 DoS。任何解析入口都必须先过这些闸门。
"""
from __future__ import annotations

LIMITS: dict[str, int] = {
    "MAX_FILE_BYTES": 100 * 1024 * 1024,          # 100MB（压缩后）
    "MAX_UNCOMPRESSED_BYTES": 1024 * 1024 * 1024,  # 1GB（解压后）
    "MAX_COMPRESS_RATIO": 200,                     # 解压比上限
    "MAX_ZIP_ENTRIES": 5000,
    "MAX_SHEETS": 200,
    "MAX_ROWS_PER_SHEET": 200_000,
    "MAX_COLS_PER_SHEET": 16_384,                  # Excel 硬上限
    "MAX_CELLS_TOTAL": 2_000_000,
    "MAX_CELLS_PER_SHEET_RENDER": 200_000,         # 单表渲染上限，超出则截断
    "PARSE_TIMEOUT_MS": 30_000,
}

# Excel 硬边界常量（1-based）
EXCEL_MAX_ROW = 1_048_576
EXCEL_MAX_COL = 16_384
