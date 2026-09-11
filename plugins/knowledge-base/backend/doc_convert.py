"""知识库 —— 旧版 Office 格式转换（.doc → .docx、.xls → .xlsx）。

设计原则
--------
- **纯 Python 实现**：不依赖 WPS / MS Office / COM，内网与无 Office 的机器同样可用
  （COM 路线在服务账户、无桌面会话、未装 Office 的部署机上不可靠）。
- **格式统一**：转换后按目标格式落盘，库内只保留 docx / xlsx，
  前端渲染链路不必为老格式再引入额外渲染库。
- **保真范围**：
  - `.xls` → `.xlsx`：逐工作表搬迁，数字整值/浮点、日期、布尔、空值按类型还原，
    合并单元格与列宽不迁移（仅数据保真）。
  - `.doc` → `.docx`：提取正文段落与表格（按文档顺序），样式、图片、
    页眉页脚、批注一律丢弃；正文文字与表格内容保真。
- **优雅降级**：依赖缺失或文件无法解析时抛 `ConvertError`，
  由上传路由转成明确的 4xx 提示，绝不让插件 500（规范 B-4）。

依赖：openpyxl（写 xlsx）、xlrd（读 xls）、python-docx（写 docx）、olefile（读 doc）。
"""

import io
import struct

# ---------------- 可选依赖探测（缺依赖不报错，仅在调用时报 ConvertError） ----------------

try:
    import openpyxl
except Exception:  # pragma: no cover
    openpyxl = None

try:
    import xlrd
except Exception:  # pragma: no cover
    xlrd = None

try:
    from docx import Document as _DocxDocument
except Exception:  # pragma: no cover
    _DocxDocument = None

try:
    import olefile
except Exception:  # pragma: no cover
    olefile = None


class ConvertError(Exception):
    """转换失败（依赖缺失或文件无法解析），携带面向用户的提示文案。"""


# 依赖可用性（供 /status 自检与前端提示）
def availability():
    return {
        "openpyxl": openpyxl is not None,
        "xlrd": xlrd is not None,
        "python-docx": _DocxDocument is not None,
        "olefile": olefile is not None,
    }


# ===================== .xls → .xlsx =====================

def xls_to_xlsx(blob):
    """旧版 .xls（BIFF）→ .xlsx 字节流，逐工作表搬迁（数据保真）。"""
    if xlrd is None:
        raise ConvertError("服务器缺少 xlrd 依赖，无法转换 .xls，请安装后重试")
    if openpyxl is None:
        raise ConvertError("服务器缺少 openpyxl 依赖，无法转换 .xls，请安装后重试")
    try:
        book = xlrd.open_workbook(file_contents=blob)
    except Exception as e:
        raise ConvertError("该 .xls 文件无法解析（可能已损坏或非标准格式）") from e

    out = openpyxl.Workbook()
    out.remove(out.active)  # 去掉默认空表，按原表逐张重建
    try:
        for sheet_name in book.sheet_names():
            sheet = book.sheet_by_name(sheet_name)
            # Excel 工作表名上限 31 字符，超长截断（重名由 openpyxl 自动加序号后缀）
            ws = out.create_sheet(title=(sheet_name or "Sheet")[:31])
            for r in range(sheet.nrows):
                ws.append([_xls_cell(sheet.cell(r, c), book.datemode)
                           for c in range(sheet.ncols)])
        if not out.sheetnames:  # 原文件无工作表：兜一张空表，避免产物打不开
            out.create_sheet(title="Sheet1")
    except ConvertError:
        raise
    except Exception as e:
        raise ConvertError("该 .xls 文件内容异常，无法转换") from e

    buf = io.BytesIO()
    try:
        out.save(buf)
    except Exception as e:
        raise ConvertError("生成 .xlsx 失败，请稍后重试") from e
    data = buf.getvalue()
    if not data:
        raise ConvertError("生成 .xlsx 失败（产物为空）")
    return data


def _xls_cell(cell, datemode):
    """xlrd 单元格 → openpyxl 可写值（数字整值化、日期转文本、空值 → None）。"""
    ct, v = cell.ctype, cell.value
    if ct in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK):
        return None
    if ct == xlrd.XL_CELL_NUMBER:
        if isinstance(v, float) and (v != v or v in (float("inf"), float("-inf"))):
            return None
        return int(v) if float(v).is_integer() else v
    if ct == xlrd.XL_CELL_DATE:
        # 转成可读文本，避免依赖 openpyxl 日期序列号与数字格式的配合
        try:
            dt = xlrd.xldate.xldate_as_datetime(v, datemode)
            return dt.strftime("%Y-%m-%d %H:%M:%S") if (dt.hour or dt.minute or dt.second) \
                else dt.strftime("%Y-%m-%d")
        except Exception:
            return str(v)
    if ct == xlrd.XL_CELL_BOOLEAN:
        return bool(v)
    if ct == xlrd.XL_CELL_ERROR:
        return None
    return str(v) if v is not None else None


# ===================== .doc → .docx =====================

def doc_to_docx(blob):
    """旧版 .doc（Word 97-2003，OLE2 复合文档）→ .docx 字节流。

    提取范围：正文段落 + 表格（按文档出现顺序）。样式/图片/页眉页脚/批注丢弃。
    """
    if olefile is None:
        raise ConvertError("服务器缺少 olefile 依赖，无法转换 .doc，请安装后重试")
    if _DocxDocument is None:
        raise ConvertError("服务器缺少 python-docx 依赖，无法转换 .doc，请安装后重试")

    blocks = _doc_blocks(blob)  # [(kind, payload)]，kind ∈ {"p", "table"}
    if not blocks:
        raise ConvertError("该 .doc 文件未提取到可读内容（可能为空文档）")

    doc = _DocxDocument()
    for kind, payload in blocks:
        if kind == "p":
            doc.add_paragraph(payload)
        else:  # table：二维列表
            rows = payload
            cols = max((len(r) for r in rows), default=0)
            if not cols:
                continue
            table = doc.add_table(rows=len(rows), cols=cols)
            table.style = "Table Grid"
            for i, row in enumerate(rows):
                for j in range(cols):
                    table.cell(i, j).text = row[j] if j < len(row) else ""

    buf = io.BytesIO()
    try:
        doc.save(buf)
    except Exception as e:
        raise ConvertError("生成 .docx 失败，请稍后重试") from e
    data = buf.getvalue()
    if not data:
        raise ConvertError("生成 .docx 失败（产物为空）")
    return data


def _doc_blocks(blob):
    """解析 Word 二进制正文 → 段落 / 表格块序列。

    实现：olefile 读 WordDocument 流 → FIB 定位 CLX（分片表）→
    按 fc 标志逐片解码文本（8-bit cp1252 / 16-bit UTF-16LE）→
    按控制字符切分段落与表格（\\x07 单元格结束 / 行结束）。
    """
    try:
        ole = olefile.OleFileIO(io.BytesIO(blob))
    except Exception as e:
        raise ConvertError("该 .doc 文件无法解析（不是有效的 Word 二进制文档）") from e
    try:
        if not ole.exists("WordDocument"):
            raise ConvertError("该 .doc 文件结构异常（缺少 WordDocument 流）")
        wd = ole.openstream("WordDocument").read()
        if len(wd) < 0x1AA:
            raise ConvertError("该 .doc 文件结构异常（数据过短）")
        w_ident, n_fib = struct.unpack_from("<HH", wd, 0)
        if w_ident != 0xA5EC:
            raise ConvertError("该 .doc 文件不是 Word 二进制格式")
        if n_fib < 193:
            raise ConvertError("该 .doc 为 Word 6/95 早期格式，暂不支持转换")

        flags = struct.unpack_from("<H", wd, 0x0A)[0]
        tbl_name = "1Table" if flags & 0x0200 else "0Table"
        tbl = ole.openstream(tbl_name).read() if ole.exists(tbl_name) else b""
        fc_clx, lcb_clx = struct.unpack_from("<II", wd, 0x1A2)
        if lcb_clx <= 0 or fc_clx + lcb_clx > len(tbl):
            raise ConvertError("该 .doc 文件的分片表定位异常，无法提取内容")
        text = "".join(_parse_pieces(tbl[fc_clx:fc_clx + lcb_clx], wd))
    except ConvertError:
        raise
    except Exception as e:
        raise ConvertError("该 .doc 文件解析失败（文件损坏或格式异常）") from e
    finally:
        try:
            ole.close()
        except Exception:
            pass
    return _text_to_blocks(text)


def _parse_pieces(clx, wd):
    """解析 CLX → 逐片解码文本（结构异常抛 ConvertError）。

    PlcPcd = (n+1)×4B CP + n×8B PCD；PCD = 2B 头 + 4B fc + 2B prm。
    fc bit30(0x40000000) 置位 → 8-bit cp1252，偏移 = (fc & 0x3FFFFFFF) // 2；
    否则 16-bit UTF-16LE，偏移 = fc & 0x3FFFFFFF。片长 = CP[k+1] - CP[k] 字符。
    """
    i, pcd_data = 0, None
    while i < len(clx):
        tag = clx[i]
        if tag == 1:  # Prc：跳过
            if i + 3 > len(clx):
                raise ConvertError("该 .doc 分片表解析失败（Prc 越界）")
            cb = struct.unpack_from("<H", clx, i + 1)[0]
            i += 3 + cb
        elif tag == 2:  # Pcdt
            if i + 5 > len(clx):
                raise ConvertError("该 .doc 分片表解析失败（Pcdt 越界）")
            lcb = struct.unpack_from("<I", clx, i + 1)[0]
            pcd_data = clx[i + 5:i + 5 + lcb]
            break
        else:
            raise ConvertError("该 .doc 分片表解析失败（未知标记）")
    if pcd_data is None or len(pcd_data) < 16 or (len(pcd_data) - 4) % 12:
        raise ConvertError("该 .doc 分片表解析失败（Pcdt 结构异常）")
    n = (len(pcd_data) - 4) // 12
    if n < 1 or n > 100000:
        raise ConvertError("该 .doc 分片表解析失败（分片数异常）")
    cps = struct.unpack_from("<%dI" % (n + 1), pcd_data, 0)
    if cps[0] != 0:
        raise ConvertError("该 .doc 分片表解析失败（CP 起点异常）")

    parts = []
    pcd_base = 4 * (n + 1)
    for k in range(n):
        fc = struct.unpack_from("<I", pcd_data, pcd_base + k * 8 + 2)[0]
        ln = cps[k + 1] - cps[k]
        if cps[k + 1] < cps[k]:
            raise ConvertError("该 .doc 分片表解析失败（CP 递减）")
        if fc & 0x40000000:  # 8-bit cp1252
            off = (fc & 0x3FFFFFFF) // 2
            parts.append(wd[off:off + ln].decode("cp1252", errors="replace"))
        else:  # 16-bit UTF-16LE
            off = fc & 0x3FFFFFFF
            parts.append(wd[off:off + 2 * ln].decode("utf-16-le", errors="replace"))
    return parts


def _text_to_blocks(text):
    """正文流 → 段落 / 表格块序列。

    Word 二进制正文的结构控制字符（实测词法）：

    - `\\r` 段落结束符；
    - `\\x07` 单元格结束符：**紧跟单元格文本**，例如 `姓名\\x07部门\\x07工号\\x07`；
    - **行结束 = 一个空单元格的 `\\x07`**，即某行写完后紧跟的 `\\x07`（前面无文本）
      使该行闭合，形如 `…1001\\x07\\x07`；
    - 表格结束后由 `\\r` 起一个普通段落。

    因此判定规则：
    - `\\x07` 且缓冲区**非空** → 追加一个单元格；
    - `\\x07` 且缓冲区**为空** → 当前行结束（收行）；若此时仍无累积行，
      说明是表格的收尾符，直接结束整表。
    - `\\r` 时若处于表格中（有未闭合行）→ 先闭合该行；否则按普通段落处理。

    另有软换行 `\\x0b` → 段内换行；域代码 `\\x13..\\x15` 丢弃指令保留结果；
    `\\x01/\\x02/\\x03/\\x04/\\x08/\\x1f/\\x00`（图片锚点、脚注标记、可选连字符）删除。
    """
    # 1) 域代码处理 + 控制字符归一（保留 \r 段落符 与 \x07 表格符 用于结构切分）
    out = []
    in_field = False
    for ch in text:
        if ch == "\x13":
            in_field = True
        elif ch in ("\x14", "\x15"):
            in_field = False
        elif in_field:
            continue
        elif ch == "\x0b":
            out.append("\n")          # 软换行 → 段内换行
        elif ch in ("\x01", "\x02", "\x03", "\x04", "\x08", "\x1f", "\x00"):
            continue                   # 图片锚点 / 脚注 / 可选连字符等 → 删除
        elif ch == "\x1e":
            out.append("-")            # 不换行连字符
        else:
            out.append(ch)
    stream = "".join(out)

    # 2) 状态机：\\r 与 \\x07 切分段落与表格
    #    blocks 按「出现顺序」累积：表格一旦闭合即就地写入 blocks，
    #    保证段落与表格的相对顺序与原文档一致。
    blocks = []
    rows = []          # 已闭合的表格行
    cells = []         # 当前行已收集的单元格
    buf = []           # 当前单元格 / 段落缓冲（原样，含空白）

    def buf_text():
        """缓冲区 → 展示文本：首尾换行/空白去掉，但段内空白保留。
        注意：判空一律用 `buf`（原缓冲）而非 strip 后的结果，
        否则「仅含空格的单元格」会被误判成行/表结束符。"""
        return "".join(buf).strip("\n \t")

    def reset_buf():
        """必须原地清空：`buf_text` 闭包捕获的是 buf 这个列表对象，
        重新绑定 `buf = []` 会让闭包读到旧列表（幽灵内容）。"""
        del buf[:]

    def emit_para(txt):
        if txt:
            blocks.append(("p", txt))

    def close_row():
        """闭合当前行：若缓冲区还有残留文本（未以 \\x07 结束的末单元格），
        先并入本行，再收行。空缓冲区直接收行，不补空单元格
        （行结束符本身是空的 \\x07，不能当成一列）。"""
        if buf:
            cells.append(buf_text())
        if cells:
            rows.append(list(cells))
        del cells[:]

    def close_table():
        """闭合整表：就地写入 blocks（保持与段落的先后顺序）。"""
        if rows:
            # 去掉尾部全空行（表格收尾符可能导致）
            while rows and all(c == "" for c in rows[-1]):
                rows.pop()
            if rows:
                blocks.append(("table", [list(r) for r in rows]))
        del rows[:]

    for ch in stream:
        if ch == "\x07":
            if buf:
                cells.append(buf_text())    # 单元格结束
            else:
                # 空缓冲区遇 \x07：本行结束；若当前无行则是表格收尾符
                if cells:
                    close_row()
                else:
                    close_table()
            reset_buf()
        elif ch == "\r":
            # \r 出现即意味着"表格区块结束"（或是一段普通文本）。
            # 必须先把已积累的行封表，否则后续段落会插到表格前面（顺序错乱）。
            if cells:
                close_row()
            close_table()
            emit_para(buf_text())
            reset_buf()
        else:
            buf.append(ch)

    # 收尾：未闭合的行 / 表格 / 尾段落（顺序：先封行、再封表、最后尾段）
    if cells:
        close_row()
    close_table()
    emit_para(buf_text())
    return blocks


# ===================== 统一入口 =====================

def convert_legacy(ext, blob):
    """按扩展名把旧版格式转为现代格式。

    返回 `(new_blob, new_ext)`；不支持或无需要时返回 `None`。
    """
    if ext == "xls":
        return xls_to_xlsx(blob), "xlsx"
    if ext == "doc":
        return doc_to_docx(blob), "docx"
    return None
