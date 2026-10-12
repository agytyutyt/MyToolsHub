# -*- coding: utf-8 -*-
r"""多工作表读取与上传上限台架（2026-10-10 需求：多 sheet 顺序拼接 + 100MB 支持）。

覆盖：
  ① 多工作表顺序拼接：每表重复表头 → 跳过；无表头溢接表 → 整表为数据；
     空表跳过；逐表裁尾空行；列宽不齐 → 统一补齐；
  ② 行数上限抬升：12 万行 CSV 通过 read_rows（旧上限 10 万行会拒绝）；
  ③ 上传上限常量 = 100MB（SEC-3 数值口径）。

运行：python backend\test_excel_multi_sheet.py   （或 pytest 收集）
"""
from __future__ import annotations

import csv
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from excel_io import MAX_ROWS, TableError, read_rows  # noqa: E402
import report_store                                   # noqa: E402

_HEADER = ["用户ID", "开始时间", "地址", "LAI", "CI", "经度", "纬度"]


def _row(i):
    return ["19812344321", "2026-09-23 12:0%d:00" % (i % 10), "某地址%d" % i,
            "1366287", "51768%05d" % i, 112.02 + i * 1e-4, 22.93 + i * 1e-4]


def _write_xlsx(path, sheets):
    import openpyxl
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for name, rows in sheets:
        ws = wb.create_sheet(name)
        for r in rows:
            ws.append(r)
    wb.save(path)
    wb.close()


def test_multi_sheet_order_and_header_skip():
    """顺序拼接 + 重复表头跳过 + 无表头溢接 + 空表跳过 + 尾空行裁剪。"""
    path = os.path.join(tempfile.gettempdir(), "_ts_multi_sheet.xlsx")
    _write_xlsx(path, [
        ("Sheet1", [_HEADER, _row(1), _row(2)]),
        ("Sheet2", [_HEADER, _row(3), _row(4)]),                  # 重复表头 → 跳过
        ("Sheet3", [_row(5), _row(6)]),                           # 无表头 → 全为数据
        ("Sheet4", []),                                           # 空表 → 跳过
        ("Sheet5", [_HEADER] + [_row(7)] + [[None] * 7] * 3),     # 尾部全空行 → 裁掉
    ])
    rows = read_rows(path, "multi.xlsx")
    assert rows[0][:3] == _HEADER[:3], rows[0]
    data = rows[1:]
    assert len(data) == 7, "应合并 7 行数据，实得 %d" % len(data)
    # 顺序保持：第 5/6 行来自 Sheet3，第 7 行来自 Sheet5
    assert data[0][2] == "某地址1" and data[4][2] == "某地址5" and data[6][2] == "某地址7"
    assert all(len(r) == 7 for r in rows), "列宽应统一补齐"
    os.remove(path)


def test_multi_sheet_bom_and_space_header():
    """表头比较容错：BOM / 全角空格 / 首尾空白不影响"重复表头"判定。"""
    path = os.path.join(tempfile.gettempdir(), "_ts_multi_head.xlsx")
    noisy = ["\ufeff用户ID", "开始时间\u3000", " 地址 ", "LAI", "CI", "经度", "纬度"]
    _write_xlsx(path, [
        ("A", [_HEADER, _row(1)]),
        ("B", [noisy, _row(2)]),
    ])
    rows = read_rows(path, "h.xlsx")
    assert len(rows) == 3, "重复表头（含 BOM/全角空格）应被跳过，实得 %d 行" % len(rows)
    os.remove(path)


def test_row_limit_raised():
    """12 万行 CSV 通过（旧上限 10 万行会 TableError）；上限常量已抬至 200 万。"""
    assert MAX_ROWS == 2000000, MAX_ROWS
    path = os.path.join(tempfile.gettempdir(), "_ts_big_csv.csv")
    n = 120000
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(_HEADER)
        for i in range(n):
            w.writerow(_row(i))
    try:
        rows = read_rows(path, "big.csv")
        assert len(rows) == n + 1, len(rows)
    finally:
        os.remove(path)


def test_upload_limit_100mb():
    """上传上限 = 100MB（SEC-3 数值口径）。"""
    assert report_store.MAX_UPLOAD_BYTES == 100 * 1024 * 1024, report_store.MAX_UPLOAD_BYTES


def test_single_sheet_unchanged():
    """单 sheet 行为不回归（原尺寸/顺序不变）。"""
    path = os.path.join(tempfile.gettempdir(), "_ts_single.xlsx")
    _write_xlsx(path, [("Only", [_HEADER, _row(1), _row(2)])])
    rows = read_rows(path, "one.xlsx")
    assert len(rows) == 3 and rows[0][0] == "用户ID" and rows[2][2] == "某地址2"
    os.remove(path)


def main() -> int:
    passed = failed = 0
    for fn in (test_multi_sheet_order_and_header_skip, test_multi_sheet_bom_and_space_header,
               test_row_limit_raised, test_upload_limit_100mb, test_single_sheet_unchanged):
        try:
            fn()
            print("  [PASS] %s" % fn.__name__)
            passed += 1
        except Exception as exc:  # noqa: BLE001
            print("  [FAIL] %s: %r" % (fn.__name__, exc))
            failed += 1
    print("结果：PASS=%d FAIL=%d" % (passed, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
