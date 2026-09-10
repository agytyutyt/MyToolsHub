"""文件过滤器 —— 表格读写 / 列过滤 / 文本后处理核心模块。

纯函数层，不依赖 Flask；供 routes.py 调用。
其他插件按规范 B-7 禁止 import 本模块，请走 HTTP 接口 POST /api/file-filter/apply。

支持的输入格式：.xlsx（openpyxl）/ .xls（xlrd）/ .csv（标准库 csv）。
输出格式：.xlsx / .csv（.xls 输入统一输出 .xlsx，xlrd 只读）。
"""

import csv
import io
import re

try:
    import openpyxl
    OPENPYXL_AVAILABLE = True
except Exception:  # pragma: no cover
    openpyxl = None
    OPENPYXL_AVAILABLE = False

try:
    import xlrd
    XLRD_AVAILABLE = True
except Exception:  # pragma: no cover
    xlrd = None
    XLRD_AVAILABLE = False

MAX_ROWS = 100000
MAX_COLS = 256

ALLOWED_EXTS = {"xlsx", "xls", "csv"}


class TableError(Exception):
    """表格解析/写入相关异常，信息直接展示给前端。"""


# ===================== 单元格规整 =====================

def cell_to_str(v):
    """单元格转字符串（表头/文本比较用）；None → 空串。"""
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def clean_text(v):
    """比较用文本规整：去 BOM、全角空格转半角、去首尾空白。"""
    s = cell_to_str(v)
    s = s.replace("\uFEFF", "").replace("\u3000", " ")
    return s.strip()


def same_field(a, b):
    """字段名等价比较（规整后 casefold，容忍大小写差异）。"""
    return clean_text(a).casefold() == clean_text(b).casefold()


# ===================== 表格读取 =====================

def _check_size(rows):
    if len(rows) > MAX_ROWS + 1:
        raise TableError(f"表格行数超过上限（≤{MAX_ROWS} 行数据）")
    if rows and len(rows[0]) > MAX_COLS:
        raise TableError(f"表格列数超过上限（≤{MAX_COLS} 列）")


def _read_csv(path):
    """读取 CSV；utf-8-sig 优先，失败退回 gbk。全部单元格为字符串。"""
    for enc in ("utf-8-sig", "gbk"):
        try:
            with open(path, "r", encoding=enc, newline="") as f:
                rows = [list(r) for r in csv.reader(f)]
            if rows:
                return rows
            raise TableError("CSV 文件为空")
        except UnicodeDecodeError:
            continue
        except TableError:
            raise
        except Exception as e:
            raise TableError(f"CSV 解析失败（{type(e).__name__}）") from e
    raise TableError("无法识别的文件编码（请另存为 UTF-8 或 GBK 编码的 CSV）")


def _read_xlsx(path):
    if not OPENPYXL_AVAILABLE:
        raise TableError("后端缺少 openpyxl，无法解析 .xlsx，请执行：pip install openpyxl")
    try:
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    except Exception as e:
        raise TableError(f"Excel 文件解析失败（{type(e).__name__}）") from e
    try:
        ws = wb.active
        rows = [list(r) for r in ws.iter_rows(values_only=True)]
    finally:
        wb.close()
    # 去掉末尾全空行
    while rows and all(v is None or clean_text(v) == "" for v in rows[-1]):
        rows.pop()
    return rows


def _read_xls(path):
    if not XLRD_AVAILABLE:
        raise TableError("后端缺少 xlrd，无法解析 .xls，请执行：pip install xlrd")
    try:
        book = xlrd.open_workbook(path)
    except Exception as e:
        raise TableError(f"Excel 文件解析失败（{type(e).__name__}）") from e
    sheet = book.sheet_by_index(0)
    rows = []
    for r in range(sheet.nrows):
        row = []
        for c in range(sheet.ncols):
            cell = sheet.cell(r, c)
            if cell.ctype == xlrd.XL_CELL_DATE:
                row.append(xlrd.xldate_as_datetime(cell.value, book.datemode))
            else:
                row.append(cell.value)
        rows.append(row)
    while rows and all(v is None or clean_text(v) == "" for v in rows[-1]):
        rows.pop()
    return rows


def read_table(path, filename):
    """读取表格文件，返回 (headers, rows)。

    headers: 首行字符串列表；rows: 后续数据行（保留原始类型，写入时还原）。
    行长度与表头对齐（不足补 None，超出截断）。
    """
    ext = (filename or "").lower().rsplit(".", 1)[-1]
    if ext not in ALLOWED_EXTS:
        raise TableError(f"不支持的文件格式：.{ext}（仅支持 xlsx / xls / csv）")
    if ext == "csv":
        raw = _read_csv(path)
    elif ext == "xlsx":
        raw = _read_xlsx(path)
    else:
        raw = _read_xls(path)
    _check_size(raw)
    if not raw:
        raise TableError("表格为空")
    headers = [clean_text(h) for h in raw[0]]
    if not any(h for h in headers):
        raise TableError("表格为空或缺少表头（首行）")
    n = len(headers)
    rows = []
    for r in raw[1:]:
        if all(v is None or clean_text(v) == "" for v in r):
            continue  # 跳过全空行
        row = list(r[:n])
        if len(row) < n:
            row.extend([None] * (n - len(row)))
        rows.append(row)
    return headers, rows


# ===================== 列过滤（硬过滤） =====================

def filter_columns(headers, rows, keep):
    """只保留与 keep 名单匹配的列（规整后精确匹配），返回 (headers2, rows2, kept, removed)。

    keep: 保留字段名列表；kept: [(原表头, 匹配的保留字段)]；removed: 被删除的原表头列表。
    """
    keep_clean = [(clean_text(k), clean_text(k).casefold()) for k in (keep or []) if clean_text(k)]
    fold = {kf: k for k, kf in keep_clean}
    idx, kept, removed = [], [], []
    for i, h in enumerate(headers):
        hf = clean_text(h).casefold()
        if hf and hf in fold:
            idx.append(i)
            kept.append((h, fold[hf]))
        else:
            removed.append(h)
    rows2 = [[(r[i] if i < len(r) else None) for i in idx] for r in rows]
    return [headers[i] for i in idx], rows2, kept, removed


# ===================== 文本后处理（脱敏 / 正则替换） =====================

def compile_rules(rules):
    """把规则列表编译为 [(regex_or_None, pattern, replacement)]；无效规则跳过。"""
    out = []
    for rule in (rules or []):
        if not isinstance(rule, dict) or rule.get("enabled") is False:
            continue
        pattern = str(rule.get("pattern") or "")
        if not pattern:
            continue
        replacement = rule.get("replacement")
        replacement = "" if replacement is None else str(replacement)
        if rule.get("is_regex"):
            try:
                out.append((re.compile(pattern), pattern, replacement))
            except re.error:
                continue  # SEC-6：非法正则包裹处理，跳过而非中断
        else:
            out.append((None, pattern, replacement))
    return out


def _apply_text(s, compiled):
    """对一段文本依次应用全部规则，返回 (新文本, 替换次数)。"""
    count = 0
    for regex, pattern, replacement in compiled:
        if regex is not None:
            s, n = regex.subn(replacement, s)
            count += n
        elif pattern in s:
            count += s.count(pattern)
            s = s.replace(pattern, replacement)
    return s, count


def post_process(headers, rows, rules):
    """对表头与所有字符串单元格应用文本/正则替换（脱敏）。

    返回 (headers2, rows2, replace_count)；非字符串单元格（数字/日期）原样保留。
    """
    compiled = compile_rules(rules)
    if not compiled:
        return list(headers), [list(r) for r in rows], 0
    count = 0
    headers2 = []
    for h in headers:
        s, n = _apply_text(cell_to_str(h), compiled)
        count += n
        headers2.append(s)
    rows2 = []
    for r in rows:
        new_row = []
        for v in r:
            if isinstance(v, str):
                s, n = _apply_text(v, compiled)
                count += n
                new_row.append(s)
            else:
                new_row.append(v)
        rows2.append(new_row)
    return headers2, rows2, count


# ===================== 表格写出 =====================

def write_table(path, ext, headers, rows):
    """把 (headers, rows) 写为 .xlsx 或 .csv（xls 不支持写出，由调用方转为 xlsx）。"""
    ext = (ext or "").lower()
    if ext == "csv":
        _write_csv(path, headers, rows)
    elif ext == "xlsx":
        _write_xlsx(path, headers, rows)
    else:
        raise TableError(f"不支持的输出格式：.{ext}")


def _write_csv(path, headers, rows):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(headers)
        for r in rows:
            w.writerow([cell_to_str(v) if v is not None else "" for v in r])


def _write_xlsx(path, headers, rows):
    if not OPENPYXL_AVAILABLE:
        raise TableError("后端缺少 openpyxl，无法生成 .xlsx，请执行：pip install openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws.append([cell_to_str(h) for h in headers])
    for r in rows:
        ws.append([v if v is not None else "" for v in r])
    # 简易列宽（按前 200 行最长值，1~40 字符封顶）
    for i, h in enumerate(headers, start=1):
        width = max(len(cell_to_str(h)), 8)
        for r in rows[:200]:
            if i <= len(r):
                width = max(width, len(cell_to_str(r[i - 1])))
        ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = min(width + 2, 40)
    wb.save(path)
