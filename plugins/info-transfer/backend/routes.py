"""信息传输 —— JZToolsHub 后端插件路由。

两个功能：
- 信息封装：把用户提供的文字 / 文件封装为 JSON 信封 {v, fmt, name, ext, data}，
  编码为静态二维码（单张或自动拆分多张）或二维码视频流（QR-transfer 协议，
  与 trajectory-convert / qr-video-decode 插件互通，含 zfec 前向纠错）。
  两种传输模式（encode 接口 raw 参数选择）：
  * 精简传输（默认，raw=0）：word 提取正文文本、txt/markdown 提取文本源码、
    excel（.xlsx/.xlsm）提取内容构建二维数组，信封声明原始文件类型（fmt）
    与后缀名（ext）；只传输数据，不保留格式、宏等参数。其他格式自动回退
    原件传输并在任务结果中附 note 说明。
  * 原件传输（raw=1）：文件不做任何解析，fmt=file，data 为文件字节 base64，
    name 为完整原始文件名（含扩展名）。
- 信息解析：对封装产生的二维码图片（单张 / 多张 ZIP）或二维码视频流解码，
  先读取信封判断文档格式与原始后缀，再还原数据；file 格式直接还原原始文件
  （含扩展名与字节）；word → 重建 .docx，excel → 重建 .xlsx（纯数据，无样式），
  旧格式（text/markdown）按纯数据导出。

信封协议（envelope）：
    {"jzt": 1, "fmt": "<doc fmt>", "name": "<display name>",
     "ext": "<原始后缀名，精简传输时声明>", "data": <payload>}
    - jzt   ：协议标识与版本（固定 1）
    - fmt   ：文档格式声明（text / markdown / word / excel / file）
    - name  ：显示名；fmt=file 时为完整原始文件名（含扩展名）
    - ext   ：原始文件后缀名（如 docx / xlsx），精简传输时声明，供还原端
              按原始类型复原；旧二维码无此字段
    - data  ：文本内容（字符串）、纯数据表格（二维数组）或文件字节 base64（fmt=file）

静态二维码分页协议（单张装不下时自动拆分多张，每张独立可读一部分）：
    {"jzt": 1, "fmt": "...", "name": "...", "pg": {"i": 1, "n": 3}, "data": <part>}

并发设计（对齐 trajectory-convert / qr-video-decode 插件）：
- POST /encode 接收文件与参数，立即返回 task_id；
- 文档提取、编码、写视频/写图片放到后台线程执行；
- 前端通过 GET /status/<task_id> 轮询，完成后经 /download、/image 取产物。
"""

import base64
import csv as csv_mod
import io
import json
import lzma
import math
import mimetypes
import os
import re
import shutil
import struct
import threading
import time
import uuid
import zipfile
import zlib
import datetime
from concurrent.futures import ThreadPoolExecutor

from flask import jsonify, request, send_file

try:
    from jztools_admin.routes import get_session_user
except Exception:  # admin 插件缺失时兜底（理论上不会发生）
    get_session_user = None

import jztools_data

API_PREFIX = "/api/info-transfer"

# ===================== 依赖可用性探测 =====================
try:
    import qrcode
    from qrcode import constants as qrcode_constants
    from qrcode.exceptions import DataOverflowError as QR_OVERFLOW
    QRCODE_AVAILABLE = True
except Exception:  # pragma: no cover
    qrcode = None
    qrcode_constants = None
    QR_OVERFLOW = Exception
    QRCODE_AVAILABLE = False

if QRCODE_AVAILABLE:
    # qrcode 库 RS 多项式实现的边界缺陷：某纠错分块的数据段全为 0x00 时
    # （v2 二进制载荷 + 等长零填充会真实出现），Polynomial.__mod__ 对零首项
    # 系数求 glog 直接抛 ValueError(glog(0))。补零守卫：前导零系数的商为 0，
    # 数学上等价于跳过该次消元；全零数据块的纠错余式恒为全零，与 GF 运算
    # 结果一致。v1 的 ASCII 载荷不含 0x00 字节，此补丁对其行为零变化。
    from qrcode import base as _qr_base

    def _safe_poly_mod(self, other):
        difference = len(self) - len(other)
        if difference < 0:
            return self
        if self[0] == 0:
            tail = self.num[1:]
            if not tail:
                return self  # 余式为 0：create_bytes 对短 modPoly 自动按 0 补齐
            return _qr_base.Polynomial(tail, 0) % other
        ratio = _qr_base.glog(self[0]) - _qr_base.glog(other[0])
        num = [item ^ _qr_base.gexp(_qr_base.glog(other_item) + ratio)
               for item, other_item in zip(self, other)]
        if difference:
            num.extend(self[-difference:])
        return _qr_base.Polynomial(num, 0) % other

    _qr_base.Polynomial.__mod__ = _safe_poly_mod

try:
    import zfec
    ZFEC_AVAILABLE = True
except Exception:  # pragma: no cover
    zfec = None
    ZFEC_AVAILABLE = False

try:
    import cv2
    CV2_AVAILABLE = True
except Exception:  # pragma: no cover
    cv2 = None
    CV2_AVAILABLE = False

try:
    import numpy as np
    NUMPY_AVAILABLE = True
except Exception:  # pragma: no cover
    np = None
    NUMPY_AVAILABLE = False

try:
    import zxingcpp
    ZXING_AVAILABLE = True
except Exception:  # pragma: no cover
    zxingcpp = None
    ZXING_AVAILABLE = False

try:
    import openpyxl
    OPENPYXL_AVAILABLE = True
except Exception:  # pragma: no cover
    openpyxl = None
    OPENPYXL_AVAILABLE = False

try:
    import docx  # python-docx
    DOCX_AVAILABLE = True
except Exception:  # pragma: no cover
    docx = None
    DOCX_AVAILABLE = False

try:
    import xlrd  # 读取旧版 .xls（BIFF），xlrd 2.x 仅支持 xls
    XLRD_AVAILABLE = True
except Exception:  # pragma: no cover
    xlrd = None
    XLRD_AVAILABLE = False

try:
    import olefile  # 读取旧版 .doc/.ppt（OLE2 复合文档），纯 Python
    OLEFILE_AVAILABLE = True
except Exception:  # pragma: no cover
    olefile = None
    OLEFILE_AVAILABLE = False

try:
    import pypdf  # T12：pdf 逐页文本提取，纯 Python
    PYPDF_AVAILABLE = True
except Exception:  # pragma: no cover
    pypdf = None
    PYPDF_AVAILABLE = False

# ---- QR-transfer 编码基础量（与 trajectory-convert / qr-video-decode 一致） ----
MAX_FEC_M = 256
SIZE_INDEX = 3          # 帧头字段 base64 编码前字节数
SIZE_PREFIX = 4         # base64 字符数（3 字节 → 4 字符）
SIZE_DATASIZE = 4       # 原始数据长度前缀字节数
BYTE_ORDER = "big"
BOX_SIZE = 10           # 二维码渲染模块边长；取较大值提升有损压缩后的可解码率
FRAMERATE = 15
VIDEO_FOURCC = "avc1"   # H.264，浏览器 <video> 可直接播放
FEC_RATIO = 0.4         # 前向纠错比例：额外生成 1/(1-fec_ratio) 帧（视频有损压缩下取高冗余）
CAMERA_FRAME_REPEAT = 5  # 相机传输模式：每码连续重复的帧数（5/15s ≈ 333ms，便于手机摄像头捕获）
PAYLOAD_SAFETY = 0.9    # 单码载荷安全系数：实际载荷 ≤ 容量的 90%（满容量高熵载荷在部分
                        # 解码器下不稳定——cv2 回读实测可能整码失败甚至截断，见评估报告 §5.3）

# ===================== 协议 v2（JZ2 二进制帧，T07/T08） =====================
# v1：信封为 JSON 文本（data 经 base64 承载），FEC 帧头 12 字符 base64，全部 QR 内容
#     为 UTF-8 安全文本（cv2/ML Kit rawValue 直接可读）。
# v2：信封与 FEC share 帧二进制化（magic "JZ2"），载荷为原始字节（去双重 base64），
#     帧头自带 CRC32 与长度——损坏/截断载荷 100% 在帧层被拒。
# 兼容策略：
#   - 解码端双协议自适应：QR 内容以 b"JZ2" 开头走 v2 帧，否则按 v1 文本解析；
#     trajectory-convert 等外部插件产的 v1 流不受影响。
#   - 编码端由 PROTOCOL_V2 开关控制（默认 v2，两端同步发版；APP 未升级的目标机
#     可置 False 回退 v1 出码）。
PROTOCOL_V2 = True

JZ2_MAGIC = b"JZ2"
JZ2_VERSION = 2
JZ2_HEADER_LEN = 21      # magic3 + ver1 + flags1 + seg_i3 + seg_n3 + meta_len2 + body_len4 + crc32_4
# flags 位定义
JZ2_COMP_NONE = 0        # bit0-1：压缩算法
JZ2_COMP_ZLIB = 1
JZ2_COMP_XZ = 2          # T10 LZMA2(xz)
JZ2_FLAG_REBUILD = 0x04  # bit2：0=exact（字节一致）1=rebuild（内容等价重建）
JZ2_FLAG_FEC = 0x08      # bit3：0=信封帧 1=FEC share 帧
# fmt 枚举（meta 内 1 字节）
JZ2_FMT_CODES = {"text": 0, "markdown": 1, "word": 2, "excel": 3, "file": 4}
JZ2_FMT_NAMES = {v: k for k, v in JZ2_FMT_CODES.items()}
ZXING_NOTE = "pip install zxing-cpp"

# 视频编码器候选（cv2 内置 FFmpeg 的 libopenh264 在部分机器缺 DLL 时，
# 依次尝试 MPEG-4 Part 2 回退；mp4v 浏览器可能无法直接播放但解码端不受影响）
VIDEO_FOURCC_CANDIDATES = ("avc1", "mp4v")

# 支持的文档格式
# - text / markdown / word / excel / csv：精简传输（只传数据，不保留格式/宏）
# - file：原件传输（完整字节 base64，name 含扩展名）
DOC_FORMATS = ("text", "markdown", "word", "excel", "file")
FMT_LABELS = {
    "text": "纯文本",
    "markdown": "Markdown",
    "word": "Word 文档",
    "excel": "Excel 表格",
    "file": "原始文件",
}
FMT_EXT = {
    "text": ".txt",
    "markdown": ".md",
    "word": ".docx",
    "excel": ".xlsx",
}

# ===================== 可封装文件格式（内置清单） =====================
# 支持清单为内置常量（不持久化）；extract 指定精简提取方式：
#   text=文本解码 | docx=python-docx 提取 | openpyxl=提取二维数组(xlsx/xlsm) |
#   xlrd=提取二维数组(xls 97-2003) | csv=csv 模块解析 | pypdf=pdf 逐页文本(T12) |
#   ppt=OLE 记录扫描文本(T12) | none=不可精简（仅原件传输）
SUPPORTED_FORMATS = {
    "docx":     {"fmt": "word",     "label": "Word 文档",    "extract": "docx"},
    "doc":      {"fmt": "word",     "label": "Word 文档",    "extract": "doc"},
    "xlsx":     {"fmt": "excel",    "label": "Excel 表格",   "extract": "openpyxl"},
    "xlsm":     {"fmt": "excel",    "label": "Excel 表格",   "extract": "openpyxl"},
    "xls":      {"fmt": "excel",    "label": "Excel 表格",   "extract": "xlrd"},
    "csv":      {"fmt": "excel",    "label": "CSV 表格",     "extract": "csv"},
    "txt":      {"fmt": "text",     "label": "纯文本",       "extract": "text"},
    "md":       {"fmt": "markdown", "label": "Markdown",     "extract": "text"},
    "markdown": {"fmt": "markdown", "label": "Markdown",     "extract": "text"},
    "ppt":      {"fmt": "text",     "label": "PPT 演示",     "extract": "ppt"},
    "pptx":     {"fmt": "file",     "label": "PPT 演示",     "extract": "none"},
    "pdf":      {"fmt": "text",     "label": "PDF 文档",     "extract": "pypdf"},
}

# 上传大小上限（与框架层面独立的前置校验，SEC-3）
MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20MB

# ===================== 后台任务 =====================
WORKERS = 2
_executor = ThreadPoolExecutor(max_workers=WORKERS)
TASKS = {}
TASKS_LOCK = threading.Lock()
TASK_TTL_SECONDS = 30 * 60

_TASK_DIR = jztools_data.get_data_root_dir("plugins", "info-transfer", ".task_cache")


class TaskCanceled(Exception):
    """用户请求停止封装（协同取消，编码循环内检查点抛出）。"""


def _canceled(task_id):
    with TASKS_LOCK:
        t = TASKS.get(task_id)
        return bool(t and t.get("cancel"))


def _remove_task_files(task_id):
    """清理任务产物文件/目录（含未完成的部分文件，取消时调用）。"""
    try:
        names = os.listdir(_TASK_DIR)
    except OSError:
        return
    prefixes = (f"{task_id}.", f"{task_id}_")
    for name in names:
        if not name.startswith(prefixes):
            continue
        path = os.path.join(_TASK_DIR, name)
        try:
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
            else:
                os.remove(path)
        except OSError:
            pass


def set_task(task_id, **kwargs):
    with TASKS_LOCK:
        TASKS[task_id] = {**TASKS.get(task_id, {}), **kwargs}


def get_task(task_id):
    with TASKS_LOCK:
        return TASKS.get(task_id)


def cleanup_tasks():
    now = time.time()
    with TASKS_LOCK:
        expired = [
            tid for tid, t in TASKS.items()
            if t.get("created_at", 0) < now - TASK_TTL_SECONDS
        ]
        for tid in expired:
            TASKS.pop(tid, None)


def _task_file(task_id, ext):
    return os.path.join(_TASK_DIR, f"{task_id}{ext}")


def _static_image_file(task_id, index):
    """第 index（1 起）张静态二维码图片的缓存路径。"""
    return os.path.join(_TASK_DIR, f"{task_id}_{index:02d}.png")


def _frames_dir(task_id):
    """相机传输模式逐帧 PNG 的缓存目录（与 /frame 接口共用）。"""
    return os.path.join(_TASK_DIR, f"{task_id}_frames")


def _clean_task_files():
    os.makedirs(_TASK_DIR, exist_ok=True)
    now = time.time()
    for name in os.listdir(_TASK_DIR):
        path = os.path.join(_TASK_DIR, name)
        try:
            if now - os.path.getmtime(path) > TASK_TTL_SECONDS:
                if os.path.isdir(path):
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    os.remove(path)
        except Exception:
            pass


def _viewer():
    """当前登录用户上下文；未登录返回 None。"""
    if get_session_user is None:
        return None
    try:
        return get_session_user()
    except Exception:
        return None


def _task_owned_by(task, user):
    """任务归属校验：非创建者（且非超管）视为任务不存在（404，纵深防御）。"""
    owner = task.get("created_by")
    if not owner:
        return True
    if user is None:
        return False
    if user.get("super_admin"):
        return True
    return owner == user.get("username")


# ===================== 文档提取 =====================

def _cell_text(v):
    """把单元格值转为可读文本（不带样式，纯数据）。"""
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _decode_text_bytes(file_bytes):
    """文本文件解码：优先 UTF-8（含 BOM），失败时依次尝试 GBK/GB18030、Big5。"""
    if file_bytes.startswith(b"\xef\xbb\xbf"):
        return file_bytes.decode("utf-8-sig")
    try:
        file_bytes.decode("utf-8")
        return file_bytes.decode("utf-8")
    except UnicodeDecodeError:
        pass
    for enc in ("gb18030", "big5"):
        try:
            return file_bytes.decode(enc)
        except UnicodeDecodeError:
            continue
    return file_bytes.decode("utf-8", errors="replace")


class LeanUnsupported(Exception):
    """精简模式无法提取该文件（封装端应回退原件传输）。"""


def extract_doc_raw(filename, file_bytes):
    """原件传输：不做任何解析，返回 (fmt="file", 完整文件名, base64 字节, ext=None)。"""
    name = os.path.basename(filename or "未命名")
    return "file", name, base64.b64encode(file_bytes).decode("ascii"), None


def _json_cell(v):
    """单元格值 → JSON 安全值（数字/布尔保留，日期转文本，None 保持 null）。"""
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, (datetime.datetime, datetime.date, datetime.time)):
        return str(v)
    return str(v)


def _extract_docx_text(file_bytes):
    """docx → 逐段纯文本（正文段落 + 表格行，按文档顺序；空段保留为空行）。

    只提取文本数据，样式/宏/图片/页眉页脚一律丢弃。失败抛 LeanUnsupported。
    """
    if not DOCX_AVAILABLE:
        raise LeanUnsupported("服务器未安装 python-docx")
    try:
        document = docx.Document(io.BytesIO(file_bytes))
        lines = []
        paras = iter(document.paragraphs)
        tables = iter(document.tables)
        for child in document.element.body.iterchildren():
            tag = child.tag.rsplit("}", 1)[-1]
            if tag == "p":
                lines.append(next(paras).text)
            elif tag == "tbl":
                for row in next(tables).rows:
                    lines.append("\t".join(cell.text.strip() for cell in row.cells))
        return "\n".join(lines)
    except LeanUnsupported:
        raise
    except StopIteration:
        raise LeanUnsupported("docx 结构异常")
    except Exception as e:
        raise LeanUnsupported("docx 精简提取失败（文件损坏或格式异常）") from e


def _trim_empty_rows(rows):
    """去除二维数组尾部空行与超宽空列（精简载荷）。"""
    while rows and all(v in (None, "") for v in rows[-1]):
        rows.pop()
    width = 0
    for r in rows:
        for i in range(len(r) - 1, -1, -1):
            if r[i] not in (None, ""):
                width = max(width, i + 1)
                break
    return [r[:width] for r in rows] if width else []


def _extract_xlsx_rows(file_bytes):
    """xlsx/xlsm → 二维数组（取活动工作表；值取缓存计算结果）。"""
    if not OPENPYXL_AVAILABLE:
        raise LeanUnsupported("服务器未安装 openpyxl")
    wb = None
    try:
        wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True, read_only=True)
        ws = wb.active
        if ws is None:
            raise LeanUnsupported("工作簿没有活动工作表")
        rows = [[_json_cell(v) for v in row] for row in ws.iter_rows(values_only=True)]
        return _trim_empty_rows(rows)
    except LeanUnsupported:
        raise
    except Exception as e:
        raise LeanUnsupported("excel 解析失败") from e
    finally:
        if wb is not None:
            try:
                wb.close()
            except Exception:
                pass


def _xls_cell(cell, datemode):
    """xlrd 单元格 → JSON 安全值（数字整值化、日期转文本、空值→null）。"""
    ct, v = cell.ctype, cell.value
    if ct in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK):
        return None
    if ct == xlrd.XL_CELL_NUMBER:
        if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
            return None
        return int(v) if float(v).is_integer() else v
    if ct == xlrd.XL_CELL_DATE:
        try:
            dt = xlrd.xldate.xldate_as_datetime(v, datemode)
            return dt.strftime("%Y-%m-%d %H:%M:%S") if (dt.hour or dt.minute or dt.second) \
                else dt.strftime("%Y-%m-%d")
        except Exception:
            return str(v)
    if ct == xlrd.XL_CELL_BOOLEAN:
        return bool(v)
    return str(v) if v is not None else None


def _extract_xls_rows(file_bytes):
    """xls（97-2003 二进制）→ 二维数组（取第一个工作表，依赖 xlrd）。"""
    if not XLRD_AVAILABLE:
        raise LeanUnsupported("服务器未安装 xlrd，无法精简提取 xls")
    try:
        book = xlrd.open_workbook(file_contents=file_bytes)
        ws = book.sheet_by_index(0)
        rows = [[_xls_cell(ws.cell(r, c), book.datemode) for c in range(ws.ncols)]
                for r in range(ws.nrows)]
        return _trim_empty_rows(rows)
    except LeanUnsupported:
        raise
    except Exception as e:
        raise LeanUnsupported("xls 解析失败") from e


_CSV_INT_RE = re.compile(r"-?(0|[1-9]\d*)")
_CSV_FLOAT_RE = re.compile(r"-?(0|[1-9]\d*)(\.\d+)?")
_CSV_BOOL = {"true": True, "false": False}


def _csv_cell(v):
    """CSV 单元格类型推断：整数/小数→数值、TRUE/FALSE→布尔、前导零等保持文本。"""
    s = (v or "").strip()
    if s == "":
        return ""
    low = s.lower()
    if low in _CSV_BOOL:
        return _CSV_BOOL[low]
    if _CSV_INT_RE.fullmatch(s):
        try:
            return int(s)
        except ValueError:
            return s
    if "." in s and _CSV_FLOAT_RE.fullmatch(s):
        try:
            return float(s)
        except ValueError:
            return s
    return s


def _extract_csv_rows(file_bytes):
    """csv → 二维数组（按 Excel 习惯做类型推断；编码兼容 UTF-8/GBK 系）。"""
    text = _decode_text_bytes(file_bytes)
    try:
        rows = [list(row) for row in csv_mod.reader(io.StringIO(text))]
    except Exception as e:
        raise LeanUnsupported("csv 解析失败") from e
    return _trim_empty_rows([[_csv_cell(v) for v in row] for row in rows])


def _clean_doc_text(text):
    """doc 提取文本清洗：控制字符 → 可读文本。

    \r 段落符→换行；\x0b 软换行→换行；\x07 表格单元格/行结束→制表符；
    域代码（\x13 域开始 \x14 域分隔 \x15 域结束）→ 删指令保留结果；
    图片锚点(\x01/\x08)、脚注标记(\x02)、可选连字符(\x1f)等 → 删除。
    """
    out = []
    in_field_instr = False  # \x13..\x14 之间是域指令，丢弃
    for ch in text:
        if ch == "\x13":
            in_field_instr = True
        elif ch == "\x14":
            in_field_instr = False
        elif ch == "\x15":
            in_field_instr = False
        elif in_field_instr:
            continue
        elif ch == "\r" or ch == "\x0b":
            out.append("\n")
        elif ch == "\x07":
            out.append("\t")
        elif ch == "\x1e":
            out.append("-")
        elif ch in ("\x01", "\x02", "\x03", "\x04", "\x08", "\x1f"):
            continue
        else:
            out.append(ch)
    cleaned = "".join(out)
    cleaned = "\n".join(line.rstrip() for line in cleaned.split("\n")).strip()
    return cleaned


def _extract_doc_text(file_bytes):
    """doc（Word 97-2003 二进制，OLE2 复合文档）→ 正文文本（含表格，制表符分隔）。

    实现：olefile 读 WordDocument/0Table|1Table 流 → FIB 定位 CLX →
    解析 PlcPcd 分片表 → 按 fc 标志逐片解码（8-bit cp1252 / 16-bit UTF-16LE）。
    仅提取主文档正文（不含页眉页脚/脚注/批注）；样式/宏/图片一律丢弃。
    """
    if not OLEFILE_AVAILABLE:
        raise LeanUnsupported("服务器未安装 olefile，无法精简提取 doc")
    try:
        ole = olefile.OleFileIO(io.BytesIO(file_bytes))
    except Exception as e:
        raise LeanUnsupported("doc 精简提取失败（不是有效的 OLE2 文档）") from e
    try:
        if not ole.exists("WordDocument"):
            raise LeanUnsupported("doc 精简提取失败（缺少 WordDocument 流）")
        wd = ole.openstream("WordDocument").read()
        if len(wd) < 0x1AA:
            raise LeanUnsupported("doc 精简提取失败（WordDocument 流过短）")
        wIdent, nFib = struct.unpack_from("<HH", wd, 0)
        if wIdent != 0xA5EC:
            raise LeanUnsupported("doc 精简提取失败（非 Word 二进制格式）")
        if nFib < 193:
            raise LeanUnsupported("doc 为 Word 6/95 早期格式，暂不支持精简提取")
        flags = struct.unpack_from("<H", wd, 0x0A)[0]
        tbl_name = "1Table" if flags & 0x0200 else "0Table"
        tbl = ole.openstream(tbl_name).read() if ole.exists(tbl_name) else b""
        fcClx, lcbClx = struct.unpack_from("<II", wd, 0x1A2)
        if lcbClx <= 0 or fcClx + lcbClx > len(tbl):
            raise LeanUnsupported("doc 精简提取失败（分片表定位异常）")
        pieces = _parse_doc_pieces(tbl[fcClx:fcClx + lcbClx], wd)
        if pieces is None:
            raise LeanUnsupported("doc 精简提取失败（分片表解析失败）")
        text = "".join(pieces)
    except LeanUnsupported:
        raise
    except Exception as e:
        raise LeanUnsupported("doc 精简提取失败（文件损坏或格式异常）") from e
    finally:
        try:
            ole.close()
        except Exception:
            pass
    return _clean_doc_text(text)


def _extract_pdf_text(file_bytes):
    """T12：pdf 逐页提取文本（pypdf，纯 Python）。

    加密 pdf（空密码可解则继续，否则回退）；扫描版/纯图版提取为空 →
    LeanUnsupported 自动回退原件并提示。
    """
    if not PYPDF_AVAILABLE:
        raise LeanUnsupported("服务器未安装 pypdf，无法精简提取 pdf")
    try:
        reader = pypdf.PdfReader(io.BytesIO(file_bytes))
        if reader.is_encrypted:
            try:
                if not reader.decrypt(""):
                    raise LeanUnsupported("pdf 已加密，无法精简提取（已按原件传输）")
            except LeanUnsupported:
                raise
            except Exception:
                raise LeanUnsupported("pdf 已加密，无法精简提取（已按原件传输）")
        parts = []
        for page in reader.pages:
            try:
                t = page.extract_text() or ""
            except Exception:
                t = ""  # 单页解析失败跳过，不整体回退
            t = t.strip()
            if t:
                parts.append(t)
        text = "\n\n".join(parts).strip()
    except LeanUnsupported:
        raise
    except Exception as e:
        raise LeanUnsupported("pdf 精简提取失败（文件损坏或格式异常）") from e
    if not text:
        raise LeanUnsupported("pdf 无可提取文本（扫描版/纯图版），已按原件传输"
                              "（建议：另存为文本类格式再精简传输）")
    return text


def _extract_ppt_text(file_bytes):
    """T12：ppt（PowerPoint 97 二进制）提取文本——扫描 PowerPoint Document 流中的
    TextCharsAtom(0x0FA0, UTF-16LE) / TextBytesAtom(0x0FA8, 8-bit ANSI) 记录。

    采用顺序滑描（非递归遍历）：非目标记录逐字节滑过，目标记录按 recLen 消费——
    对嵌套容器结构与未知记录天然鲁棒；加密流解出的垃圾文本经控制字符比例
    校验拦截（>30% 视为提取失败回退原件）。
    """
    if not OLEFILE_AVAILABLE:
        raise LeanUnsupported("服务器未安装 olefile，无法精简提取 ppt")
    try:
        ole = olefile.OleFileIO(io.BytesIO(file_bytes))
    except Exception as e:
        raise LeanUnsupported("ppt 精简提取失败（不是有效的 OLE2 文档）") from e
    try:
        if not ole.exists("PowerPoint Document"):
            raise LeanUnsupported("ppt 精简提取失败（缺少 PowerPoint Document 流）")
        data = ole.openstream("PowerPoint Document").read()
        parts = []
        i, n = 0, len(data)
        while i + 8 <= n:
            rec_type = struct.unpack_from("<H", data, i + 2)[0]
            rec_len = struct.unpack_from("<I", data, i + 4)[0]
            if rec_type in (0x0FA0, 0x0FA8) and i + 8 + rec_len <= n and rec_len <= 0x100000:
                body = data[i + 8:i + 8 + rec_len]
                try:
                    txt = body.decode("utf-16-le") if rec_type == 0x0FA0 else body.decode("gbk")
                except Exception:
                    try:
                        txt = body.decode("cp1252")
                    except Exception:
                        txt = ""
                if txt:
                    parts.append(txt)
                i += 8 + rec_len
            else:
                i += 1
        text = _clean_doc_text("".join(parts)).strip()
        if not text:
            raise LeanUnsupported("ppt 无可提取文本（加密/纯图版），已按原件传输")
        # 垃圾文本拦截：控制字符（除换行/制表）占比过高 → 加密流或非文本内容
        ctrl = sum(1 for ch in text if ord(ch) < 32 and ch not in "\n\t")
        if len(text) and ctrl / len(text) > 0.3:
            raise LeanUnsupported("ppt 文本提取异常（可能已加密），已按原件传输")
        return text
    except LeanUnsupported:
        raise
    except Exception as e:
        raise LeanUnsupported("ppt 精简提取失败（文件损坏或格式异常）") from e
    finally:
        try:
            ole.close()
        except Exception:
            pass


def _parse_doc_pieces(clx, wd):
    """解析 CLX → 逐片解码文本（返回拼接字符串；结构异常返回 None）。

    PlcPcd = (n+1)×4B CP + n×8B PCD；PCD = 2B 头 + 4B fc + 2B prm。
    fc bit30(0x40000000) 置位 → 8-bit cp1252，偏移 = (fc & 0x3FFFFFFF) // 2；
    否则 16-bit UTF-16LE，偏移 = fc & 0x3FFFFFFF。片长 = CP[k+1] - CP[k] 字符。
    """
    try:
        i = 0
        pcd_data = None
        while i < len(clx):
            tag = clx[i]
            if tag == 1:  # Prc：跳过
                if i + 3 > len(clx):
                    return None
                cb = struct.unpack_from("<H", clx, i + 1)[0]
                i += 3 + cb
            elif tag == 2:  # Pcdt
                if i + 5 > len(clx):
                    return None
                lcb = struct.unpack_from("<I", clx, i + 1)[0]
                pcd_data = clx[i + 5:i + 5 + lcb]
                break
            else:
                return None
        if pcd_data is None or len(pcd_data) < 16 or (len(pcd_data) - 4) % 12:
            return None
        n = (len(pcd_data) - 4) // 12
        if n < 1 or n > 100000:
            return None
        cps = struct.unpack_from("<%dI" % (n + 1), pcd_data, 0)
        if cps[0] != 0:
            return None
        parts = []
        pcd_base = 4 * (n + 1)
        for k in range(n):
            fc = struct.unpack_from("<I", pcd_data, pcd_base + k * 8 + 2)[0]
            ln = cps[k + 1] - cps[k]
            if cps[k + 1] < cps[k]:
                return None
            if fc & 0x40000000:  # 8-bit
                off = (fc & 0x3FFFFFFF) // 2
                parts.append(wd[off:off + ln].decode("cp1252", errors="replace"))
            else:  # 16-bit
                off = fc & 0x3FFFFFFF
                parts.append(wd[off:off + 2 * ln].decode("utf-16-le", errors="replace"))
        return parts
    except Exception:
        return None


def extract_doc_lean(filename, file_bytes):
    """精简传输提取：返回 (fmt, name, data, ext)。

    提取方式由持久化配置（config.json）驱动：
    - text：.txt/.md 文本解码（markdown 为源码）；
    - docx：.docx 提取正文文本与表格（纯数据）；
    - xlrd：.xlsx/.xlsm/.xls 提取内容构建二维数组；
    - csv：.csv 解析构建二维数组；
    - none（如 .doc 旧版二进制格式）：不可精简 → 抛 LeanUnsupported，
      由调用方自动回退原件传输并提示。
    """
    name = os.path.basename(filename or "未命名")
    ext = name.lower().rsplit(".", 1)[-1] if "." in name else ""
    base = name.rsplit(".", 1)[0] if "." in name else name
    item = SUPPORTED_FORMATS.get(ext)
    if item is None:
        raise LeanUnsupported(f".{ext}" if ext else "无扩展名文件")
    if item["extract"] == "none":
        raise LeanUnsupported(
            f"{item['label']}（.{ext}）不支持精简提取，已按原件传输"
            "（建议：pdf 可先另存为文本类格式再精简传输以大幅减少二维码数量）")
    fmt, ext_decl = item["fmt"], ext
    extract = item["extract"]
    if extract == "text":
        return fmt, base, _decode_text_bytes(file_bytes), ext_decl
    if extract == "docx":
        return fmt, base, _extract_docx_text(file_bytes), ext_decl
    if extract == "doc":
        return fmt, base, _extract_doc_text(file_bytes), ext_decl
    if extract == "pypdf":
        return fmt, base, _extract_pdf_text(file_bytes), ext_decl
    if extract == "ppt":
        return fmt, base, _extract_ppt_text(file_bytes), ext_decl
    if extract == "csv":
        return fmt, base, _extract_csv_rows(file_bytes), ext_decl
    if extract == "openpyxl":
        return fmt, base, _extract_xlsx_rows(file_bytes), ext_decl
    return fmt, base, _extract_xls_rows(file_bytes), ext_decl


# ===================== 信封封装 =====================

def _pack_data(fmt, data):
    """把信封 data 序列化并按需 zlib 压缩，返回 (承载值, 是否压缩)。

    - str / excel 二维数组：序列化为 UTF-8 字节后 zlib 压缩（level 9），
      仅当 base64(压缩) 确实小于原始字节时才压缩（zip=1 标记）；
      小文本/小表格压缩不划算 → 保持原形态（旧版信封形状，全端兼容）。
    - fmt=file：data 为文件字节 base64。对 base64 文本同样试压一轮 zlib：
      OLE 类（doc/ppt/xls）与文本类源文件的 base64 仍有大量冗余
      （实测 ppt 10.4×、doc 12×、pdf 2.9×，见《信息传输格式开销评估与优化方案》）；
      已压缩容器（docx/xlsx/jpg）试压不划算自动保持原样。
      解析端按 zip=1 解压与 fmt 无关（解压结果是 base64 串，照常 b64decode），
      网页端与 APP（≥v1.7）天然兼容，无需改动。
    """
    if isinstance(data, str):
        if fmt == "file":
            raw = data.encode("ascii")
        else:
            raw = data.encode("utf-8")
    elif isinstance(data, list):
        raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    else:
        return data, False
    try:
        packed = base64.b64encode(zlib.compress(raw, 9)).decode("ascii")
    except Exception:  # 压缩失败兜底为原样承载
        return data, False
    if len(packed) < len(raw):
        return packed, True
    return data, False


def _unpack_data(fmt, data):
    """解压 zip=1 信封的 data（base64 → zlib inflate → 文本/二维数组）。"""
    try:
        raw = zlib.decompress(base64.b64decode(data or "", validate=True))
    except Exception:
        raise ValueError("压缩数据解码失败")
    if fmt == "excel":
        try:
            rows = json.loads(raw.decode("utf-8"))
        except Exception:
            raise ValueError("Excel 数据结构异常（应为二维数组）")
        if not isinstance(rows, list):
            raise ValueError("Excel 数据结构异常（应为二维数组）")
        return rows
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("文本解码失败")


def build_envelope(fmt, name, data, ext=None, rebuild=False):
    """构造信封 JSON 字节（解析端以此判断文档格式/原始后缀/压缩标记）。

    精简传输的文本与二维数组经 zlib 压缩（zip=1）后 base64 承载，
    消除 JSON 语法与重复文本冗余——重复度高的表格/文档典型压缩比 5-10 倍；
    fmt=file 与压缩无收益的小数据保持原形态。

    PROTOCOL_V2=True 时返回 JZ2 二进制信封帧（原始字节载荷，去双重 base64 +
    CRC32 完整性校验）；编码/解析两端按 b"JZ2" 魔数双协议自适应。
    rebuild=T11 拆包重组开关（仅 v2 生效；fmt=file 且 ZIP 容器时拆包重建，
    更小才启用，否则回退纯压缩——字节精确诉求不开此开关）。
    """
    if PROTOCOL_V2:
        return build_envelope_v2(fmt, name, data, ext, rebuild=rebuild)
    env = {"jzt": 1, "fmt": fmt, "name": name}
    if ext:
        env["ext"] = ext
    packed, zipped = _pack_data(fmt, data)
    if zipped:
        env["zip"] = 1
    env["data"] = packed
    return json.dumps(env, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def parse_envelope(obj):
    """校验并解析信封，返回 (fmt, name, data, ext)；不合法时抛 ValueError。

    - ext 为精简传输声明的原始后缀名（旧二维码无此字段时为 None）；
    - zip=1 时 data 为 zlib 压缩载荷，自动解压还原为文本 / 二维数组。
    """
    if not isinstance(obj, dict):
        raise ValueError("内容不是「信息传输」封装的数据（缺少信封结构）")
    if obj.get("jzt") != 1:
        raise ValueError("信封协议版本不受支持（jzt != 1）")
    fmt = obj.get("fmt")
    if fmt not in DOC_FORMATS:
        raise ValueError(f"未知的文档格式声明：{fmt}")
    ext = obj.get("ext")
    ext = str(ext).lstrip(".").lower() if isinstance(ext, str) and ext else None
    data = obj.get("data")
    if obj.get("zip") == 1:
        data = _unpack_data(fmt, data)
    return fmt, str(obj.get("name") or "未命名"), data, ext


# ===================== 协议 v2：JZ2 二进制帧（T07 信封 v2 / T08 二进制直载） =====================
# 帧结构（21B 定长帧头 + meta + payload）：
#   magic 3B "JZ2" | ver 1B | flags 1B | seg_i 3B | seg_n 3B | meta_len 2B | body_len 4B | crc32 4B
#   flags bit0-1 = 压缩算法（0=none 1=zlib 2=xz 预留）；bit2 = mode（0=exact 1=rebuild）；
#   bit3 = 帧类型（0=信封帧 1=FEC share 帧）
#   CRC32 覆盖「帧头（除 CRC 字段自身）+ meta + payload」——帧头/载荷任何位翻转都会被拒。
# 两种帧：
#   信封帧（静态码直接承载）：meta = fmt(1B)+orig_len(4B)+name_len(2B)+name+ext_len(1B)+ext；
#       payload = 数据原始字节（压缩与否由 flags 标注）。静态多页时 meta 每页重复、
#       payload 为整份数据的切片，收齐后拼接再解压。
#   share 帧（视频流）：meta = k(1B)+m(1B)；payload = zfec share 原始字节（不再 base64）。
# 解码端双协议自适应：内容以 b"JZ2" 开头走 v2，否则按 v1 文本解析（外部插件 v1 流不受影响）。

def _xz_compress(raw):
    """LZMA2(XZ) 压缩：preset 9|EXTREME，字典 ≤16MB（T10）。失败返回 None（fail-open 回退 zlib）。"""
    try:
        return lzma.compress(
            raw, format=lzma.FORMAT_XZ,
            filters=[{"id": lzma.FILTER_LZMA2,
                      "preset": 9 | lzma.PRESET_EXTREME,
                      "dict_size": 1 << 24}])
    except Exception:
        return None


def _xz_decompress(data):
    """XZ 解压（损坏流抛异常，由调用方归一为信封损坏错误）。"""
    return lzma.decompress(data, format=lzma.FORMAT_XZ)


def _compress_best(raw):
    """T10 试压：zlib(level 9) → LZMA2(xz, 9|EXTREME, 字典 16MB)，取更小者。

    返回 (packed, comp)；xz 失败 fail-open 回退 zlib/none。
    """
    best, best_comp = raw, JZ2_COMP_NONE
    try:
        packed = zlib.compress(raw, 9)
    except Exception:
        packed = None
    if packed is not None and len(packed) < len(best):
        best, best_comp = packed, JZ2_COMP_ZLIB
    packed_xz = _xz_compress(raw)
    if packed_xz is not None and len(packed_xz) < len(best):
        best, best_comp = packed_xz, JZ2_COMP_XZ
    return best, best_comp


# T11 拆包重组适用格式（ZIP 容器：容器级压缩无收益，拆包后内容可再压缩）
REBUILD_EXTS = {"docx", "xlsx", "xlsm", "pptx", "zip"}


def _zip_pack_stream(file_bytes):
    """T11：ZIP 容器 → 连续流。格式：[条目数 2B] + 每条目
    [name_len 2B][name utf8][content_len 4B][content]（目录条目跳过；
    加密/损坏容器返回 None，由调用方回退原件压缩路径）。"""
    try:
        with zipfile.ZipFile(io.BytesIO(file_bytes)) as z:
            infos = [i for i in z.infolist() if not i.is_dir()]
            if not infos or len(infos) > 0xFFFF:
                return None
            out = io.BytesIO()
            out.write(len(infos).to_bytes(2, "big"))
            for i in infos:
                name_b = i.filename.encode("utf-8")
                if len(name_b) > 0xFFFF:
                    return None
                content = z.read(i)
                out.write(len(name_b).to_bytes(2, "big"))
                out.write(name_b)
                out.write(len(content).to_bytes(4, "big"))
                out.write(content)
            return out.getvalue()
    except Exception:
        return None


def _zip_unpack_stream(stream):
    """T11：连续流 → [(name, content), ...]；越界/截断/残留字节均抛 ValueError。"""
    if len(stream) < 2:
        raise ValueError("重建流长度不足")
    n = int.from_bytes(stream[:2], "big")
    pos = 2
    entries = []
    for _ in range(n):
        if pos + 2 > len(stream):
            raise ValueError("重建流条目名长度越界")
        name_len = int.from_bytes(stream[pos:pos + 2], "big")
        pos += 2
        if pos + name_len + 4 > len(stream):
            raise ValueError("重建流条目头越界")
        name = stream[pos:pos + name_len].decode("utf-8")
        pos += name_len
        clen = int.from_bytes(stream[pos:pos + 4], "big")
        pos += 4
        if pos + clen > len(stream):
            raise ValueError("重建流条目内容越界")
        entries.append((name, stream[pos:pos + clen]))
        pos += clen
    if pos != len(stream):
        raise ValueError("重建流长度不符（数据损坏）")
    return entries


def _zip_rebuild(entries):
    """T11：条目列表 → 重建 ZIP（内容等价，条目顺序保留、时间戳不保证）。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, content in entries:
            zi = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            z.writestr(zi, content)
    return buf.getvalue()


def _pack_data_v2(fmt, data):
    """v2 载荷序列化：返回 (payload_bytes, comp, orig_len)——不 base64，直接原始字节。

    - str：fmt=file 先 base64 解码还原文件字节，其余按 UTF-8 编码；
    - list（word 段落数组 / excel 二维数组）：紧凑 JSON 序列化；
    - T10 试压顺序：zlib(level 9) → LZMA2(xz, 9|EXTREME, 字典 16MB)，取更小者；
      更小才启用对应算法（flags 标注），xz 失败 fail-open 回退 zlib/none。
      （v1 路径保持 zlib——旧信封无算法字段，解析端仅支持 Inflater。）
    """
    if isinstance(data, (bytes, bytearray)):
        raw = bytes(data)
    elif isinstance(data, str):
        if fmt == "file":
            try:
                raw = base64.b64decode(data, validate=True)
            except Exception:
                raw = data.encode("utf-8")
        else:
            raw = data.encode("utf-8")
    elif isinstance(data, list):
        raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    else:
        raise ValueError("v2 载荷类型不支持")
    best, comp = _compress_best(raw)
    return best, comp, len(raw)


def _env_meta_v2(fmt, name, ext, orig_len):
    """信封帧 meta：fmt(1B) + orig_len(4B) + name_len(2B) + name(utf8) + ext_len(1B) + ext。"""
    fmt_code = JZ2_FMT_CODES.get(fmt)
    if fmt_code is None:
        raise ValueError(f"未知的文档格式声明：{fmt}")
    name_b = (name or "未命名").encode("utf-8")[:65535]
    ext_b = (ext or "").lstrip(".").lower().encode("utf-8")[:255]
    return (bytes([fmt_code])
            + int(orig_len).to_bytes(4, BYTE_ORDER)
            + len(name_b).to_bytes(2, BYTE_ORDER) + name_b
            + bytes([len(ext_b)]) + ext_b)


def _jz2_frame(kind, seg_i, seg_n, meta, payload, comp=JZ2_COMP_NONE, mode=0):
    """构造 JZ2 帧。kind="env" 信封帧 / "fec" share 帧；CRC32 覆盖帧头+meta+payload。"""
    flags = (comp & 0x03)
    if mode:
        flags |= JZ2_FLAG_REBUILD
    if kind == "fec":
        flags |= JZ2_FLAG_FEC
    header_no_crc = (JZ2_MAGIC
                     + bytes([JZ2_VERSION, flags])
                     + int(seg_i).to_bytes(3, BYTE_ORDER)
                     + int(seg_n).to_bytes(3, BYTE_ORDER)
                     + len(meta).to_bytes(2, BYTE_ORDER)
                     + len(payload).to_bytes(4, BYTE_ORDER))
    crc = zlib.crc32(header_no_crc + meta + payload) & 0xFFFFFFFF
    return header_no_crc + crc.to_bytes(4, BYTE_ORDER) + meta + payload


def parse_frame_jz2(frame):
    """校验并解析 JZ2 帧，返回 dict(comp/mode/is_fec/seg_i/seg_n/meta/payload)。

    长度不足 / magic / 版本 / CRC 任一不符即抛 ValueError（损坏与截断 100% 被拒）。
    """
    if not isinstance(frame, (bytes, bytearray)) or len(frame) < JZ2_HEADER_LEN:
        raise ValueError("JZ2 帧长度不足（数据损坏或被截断）")
    frame = bytes(frame)
    if frame[:3] != JZ2_MAGIC:
        raise ValueError("JZ2 帧标识不符")
    if frame[3] != JZ2_VERSION:
        raise ValueError(f"JZ2 协议版本不受支持（{frame[3]}）")
    flags = frame[4]
    seg_i = int.from_bytes(frame[5:8], BYTE_ORDER)
    seg_n = int.from_bytes(frame[8:11], BYTE_ORDER)
    meta_len = int.from_bytes(frame[11:13], BYTE_ORDER)
    body_len = int.from_bytes(frame[13:17], BYTE_ORDER)
    crc = int.from_bytes(frame[17:21], BYTE_ORDER)
    if len(frame) < JZ2_HEADER_LEN + meta_len + body_len:
        raise ValueError("JZ2 帧载荷不完整（数据被截断）")
    if (zlib.crc32(frame[:JZ2_HEADER_LEN - 4] + frame[JZ2_HEADER_LEN:JZ2_HEADER_LEN + meta_len + body_len])
            & 0xFFFFFFFF) != crc:
        raise ValueError("JZ2 帧 CRC32 校验失败（数据损坏）")
    meta = frame[JZ2_HEADER_LEN:JZ2_HEADER_LEN + meta_len]
    payload = frame[JZ2_HEADER_LEN + meta_len:JZ2_HEADER_LEN + meta_len + body_len]
    return {
        "comp": flags & 0x03,
        "mode": 1 if flags & JZ2_FLAG_REBUILD else 0,
        "is_fec": bool(flags & JZ2_FLAG_FEC),
        "seg_i": seg_i, "seg_n": seg_n,
        "meta": meta, "payload": payload,
    }


def build_envelope_v2(fmt, name, data, ext=None, rebuild=False):
    """构造 v2 JZ2 信封帧字节（单帧不分页；静态多页由分页端对 payload 切片）。

    rebuild=True（T11，仅 fmt=file 且 ext ∈ REBUILD_EXTS）：ZIP 容器拆包为连续流
    再压缩（flags 置 mode=rebuild）。仅当拆包路径确实比直接压缩更小时启用，
    否则静默回退纯压缩路径（字节精确）。
    """
    payload, comp, orig_len = _pack_data_v2(fmt, data)
    mode = 0
    if rebuild and fmt == "file":
        ext_n = (ext or "").lstrip(".").lower()
        if ext_n in REBUILD_EXTS:
            try:
                file_bytes = base64.b64decode(data, validate=True)
            except Exception:
                file_bytes = None
            if file_bytes is not None:
                stream = _zip_pack_stream(file_bytes)
                if stream is not None:
                    packed_s, comp_s = _compress_best(stream)
                    if len(packed_s) < len(payload):
                        payload, comp = packed_s, comp_s
                        orig_len, mode = len(stream), 1   # orig_len=拼接流长度（解析端先校验再拆流）
    meta = _env_meta_v2(fmt, name, ext, orig_len)
    return _jz2_frame("env", 0, 1, meta, payload, comp=comp, mode=mode)


def parse_envelope_v2(frame_bytes):
    """解析 v2 JZ2 信封帧 → 规范化为 v1 信封 dict（下游 parse_envelope 零改动）。

    - fmt=file：payload（原始文件字节）转回 base64 字符串；
    - fmt=excel：payload JSON 还原为二维数组（word 精简载荷是纯文本，原样返回）；
    - 压缩载荷自动解压；orig_len 长度校验不符时拒收。
    """
    f = parse_frame_jz2(frame_bytes)
    if f["is_fec"]:
        raise ValueError("JZ2 帧类型不符（FEC 分帧不能直接作为信封）")
    meta = f["meta"]
    if len(meta) < 8:
        raise ValueError("JZ2 信封元数据不完整")
    fmt = JZ2_FMT_NAMES.get(meta[0])
    if fmt is None:
        raise ValueError(f"未知的文档格式声明：{meta[0]}")
    orig_len = int.from_bytes(meta[1:5], BYTE_ORDER)
    name_len = int.from_bytes(meta[5:7], BYTE_ORDER)
    pos = 7
    name_b = meta[pos:pos + name_len]
    pos += name_len
    ext_len = meta[pos] if pos < len(meta) else 0
    pos += 1
    ext_b = meta[pos:pos + ext_len]
    try:
        name = name_b.decode("utf-8") or "未命名"
    except Exception:
        name = "未命名"
    try:
        ext = ext_b.decode("utf-8") or None
    except Exception:
        ext = None
    payload = f["payload"]
    if f["comp"] == JZ2_COMP_ZLIB:
        try:
            payload = zlib.decompress(payload)
        except Exception:
            raise ValueError("压缩数据解码失败")
    elif f["comp"] == JZ2_COMP_XZ:
        try:
            payload = _xz_decompress(payload)
        except Exception:
            raise ValueError("压缩数据解码失败")
    if orig_len and len(payload) != orig_len:
        raise ValueError("解压后数据长度不符（数据损坏）")
    if f["mode"] and fmt == "file":
        # T11 mode=rebuild：payload 为 ZIP 容器拼接流 → 拆流重建 zip（内容等价，
        # 条目顺序保留、时间戳不保证）。重建在解析层完成，下游 envelope_to_file
        # 与 APP 导出零改动；orig_len 校验对象是拼接流长度。
        try:
            payload = _zip_rebuild(_zip_unpack_stream(payload))
        except ValueError:
            raise
        except Exception:
            raise ValueError("重建流解析失败（数据损坏）")
    if fmt == "file":
        data = base64.b64encode(payload).decode("ascii")
    elif fmt == "excel":
        # 与 v1 _unpack_data 语义一致：仅 excel 载荷是 JSON 二维数组；
        # word 精简载荷是纯文本（docx/doc 正文提取），不做 JSON 解析。
        try:
            data = json.loads(payload.decode("utf-8"))
        except Exception:
            raise ValueError("Excel 数据结构异常（应为二维数组）")
        if not isinstance(data, list):
            raise ValueError("Excel 数据结构异常（应为二维数组）")
    else:
        try:
            data = payload.decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError("文本解码失败")
    env = {"jzt": 1, "fmt": fmt, "name": name, "data": data}
    if ext:
        env["ext"] = ext
    if f["mode"]:
        env["mode"] = 1   # T11 重建标识：UI 标注"还原方式：重建（内容等价，非字节一致）"
    return env


def _qr_byte_capacity(version, err):
    """QR byte 模式单码内容字节上限（bit 级扣除模式/计数/终止符开销）。"""
    maxbits = qrcode.util.BIT_LIMIT_TABLE[err][version]
    prefix_bits = max(qrcode.util.mode_sizes_for_version(version).values()) + 4
    return max(0, (maxbits - prefix_bits) // 8)


def _max_chunk_size_v2(version, err, meta_len):
    """v2 单帧载荷上限 = QR 内容上限 − 21B 帧头 − meta 长度，再乘安全系数。"""
    cap = _qr_byte_capacity(version, err) - JZ2_HEADER_LEN - meta_len
    cap = int(cap * PAYLOAD_SAFETY)
    if cap <= 0:
        raise ValueError(f"二维码版本 {version} 过小，无法承载数据（请调大二维码版本）")
    return cap


def _qrformat_encode_v2(share_list, k, m):
    """v2 FEC share 帧：JZ2 帧头（kind=fec，meta=k,m）+ share 原始字节（不再 base64）。"""
    nframes = len(share_list)
    meta = bytes([k, m])
    return [_jz2_frame("fec", i, nframes, meta, share)
            for i, share in enumerate(share_list)]


def reassemble_qrtransfer_v2(codes):
    """按 v2 JZ2 share 帧重组（codes 为解码端输出的原始内容 bytes 列表）。

    - CRC 校验失败的帧按缺失处理（FEC 容忍，缺 ≤ m-k 帧仍可还原）；
    - 有效帧少于 k 时报错（文案与 v1 一致）；
    - 返回原始字节（已剥 4B 长度前缀）。
    """
    if not ZFEC_AVAILABLE:
        raise RuntimeError("后端缺少 zfec 依赖，无法纠错重组，请执行：pip install zfec")
    data_list = None
    total = k = m = None
    for code in codes:
        if not isinstance(code, (bytes, bytearray)) or not bytes(code).startswith(JZ2_MAGIC):
            continue
        try:
            f = parse_frame_jz2(code)
        except ValueError:
            continue  # 损坏帧 → 缺失，交由 FEC 容错
        if not f["is_fec"] or len(f["meta"]) != 2:
            continue
        kk, mm = f["meta"][0], f["meta"][1]
        if total is None:
            total, k, m = f["seg_n"], kk, mm
            data_list = [None] * total
        elif total != f["seg_n"] or k != kk or m != mm:
            continue
        if 0 <= f["seg_i"] < total and data_list[f["seg_i"]] is None:
            data_list[f["seg_i"]] = f["payload"]

    if data_list is None or k is None:
        raise ValueError("未识别到有效的 JZ2 二维码帧")
    got = sum(1 for d in data_list if d is not None)
    if got < k:
        raise ValueError(
            f"有效帧不足：收到 {got}/{total} 帧，少于纠错阈值 k={k}，无法恢复（视频可能被截断）"
        )
    nblocks = total // m
    decoder = zfec.Decoder(k, m)
    recovered_blocks = []
    for i in range(nblocks):
        blocks, blocknums = [], []
        for j in range(m):
            d = data_list[i + j * nblocks]
            if d is not None:
                blocks.append(d)
                blocknums.append(j)
        if len(blocks) < k:
            raise ValueError(
                f"第 {i + 1}/{nblocks} 组有效分块不足（{len(blocks)} < k={k}），无法纠错"
            )
        recovered_blocks.append(decoder.decode(blocks[:k], blocknums[:k]))
    recovered = []
    for j in range(k):
        for i in range(nblocks):
            recovered.append(recovered_blocks[i][j])
    joined = b"".join(recovered)
    data_size = int.from_bytes(joined[:SIZE_DATASIZE], BYTE_ORDER)
    return joined[SIZE_DATASIZE:SIZE_DATASIZE + data_size]


def _build_static_pages_v2(env_frame, version, err, max_pages=200):
    """把 v2 信封帧按容量拆分为多页 JZ2 帧（meta 每页重复，payload 切片）。

    返回 JZ2 帧 bytes 列表；单帧装得下时返回 [原帧]。
    每页 CRC 独立校验；收齐 seg_n 页后拼 payload 再统一解压。
    """
    f = parse_frame_jz2(env_frame)
    cap = _max_chunk_size_v2(version, err, len(f["meta"]))
    payload = f["payload"]
    if len(payload) <= cap:
        return [env_frame]
    n = math.ceil(len(payload) / cap)
    if n > max_pages:
        raise ValueError(
            f"数据量过大，需拆分超过 {max_pages} 张静态二维码，"
            "请调大二维码版本或改用「二维码视频流」模式"
        )
    pages = []
    for i in range(n):
        pages.append(_jz2_frame("env", i, n, f["meta"],
                                payload[i * cap:(i + 1) * cap],
                                comp=f["comp"], mode=f["mode"]))
    return pages


def _envelope_from_raw(raw):
    """重组产物 → 信封 dict：v2 JZ2 帧 / v1 JSON 双协议判别。"""
    if isinstance(raw, (bytes, bytearray)) and bytes(raw).startswith(JZ2_MAGIC):
        return parse_envelope_v2(raw)
    try:
        env = json.loads((raw if isinstance(raw, str) else bytes(raw).decode("utf-8")))
    except Exception:
        raise ValueError("重组数据不是「信息传输」封装的信封（可能是其他来源的二维码流）")
    return env


def _parse_scanned_codes(codes):
    """把扫码内容（原始 bytes 列表）重组为完整信封 dict，返回 (env, page_info)。

    - v2 信封帧：按 seg_i/seg_n 收集，meta 一致性校验，拼 payload 后统一解析；
    - v1 文本：转 str 后走原 _parse_scanned_texts 逻辑（单张 / pg 多页）。
    """
    v2_pages = {}
    v2_total = None
    v2_first = None
    texts = []
    for c in codes:
        if isinstance(c, (bytes, bytearray)) and bytes(c).startswith(JZ2_MAGIC):
            try:
                f = parse_frame_jz2(c)
            except ValueError:
                continue  # 损坏页跳过，按缺页提示
            if f["is_fec"]:
                continue
            if v2_total is None:
                v2_total = f["seg_n"]
                v2_first = f
            elif v2_total != f["seg_n"] or f["meta"] != v2_first["meta"]:
                continue  # 其他任务的分页流，忽略
            v2_pages[f["seg_i"]] = f
            continue
        try:
            texts.append(c.decode("utf-8") if isinstance(c, (bytes, bytearray)) else str(c))
        except Exception:
            continue
    if v2_pages:
        if len(v2_pages) < v2_total:
            raise ValueError(
                f"静态二维码页数不全：收到 {len(v2_pages)}/{v2_total} 张，请补齐后再解析"
            )
        missing = [i for i in range(v2_total) if i not in v2_pages]
        if missing:
            raise ValueError(f"缺少第 {missing[0] + 1}/{v2_total} 张二维码，无法还原完整数据")
        payload = b"".join(v2_pages[i]["payload"] for i in range(v2_total))
        full = _jz2_frame("env", 0, 1, v2_first["meta"], payload,
                          comp=v2_first["comp"], mode=v2_first["mode"])
        env = parse_envelope_v2(full)
        return env, {"n": v2_total, "mode": v2_first["mode"]}
    return _parse_scanned_texts(texts)


# ===================== QR-transfer 编码（视频） =====================

def encode_index(n):
    b = n.to_bytes(SIZE_INDEX, BYTE_ORDER)
    return base64.encodebytes(b).replace(b"\n", b"")


def encode_fecinfo(k, m):
    return encode_index((k << 8) + m)


def encode_data(b):
    return base64.encodebytes(b).replace(b"\n", b"")


def _max_chunk_size(version, err):
    """单帧可承载的原始字节数（含 90% 安全系数，向下取 3 的倍数保证 base64 无填充）。

    安全系数用于规避「满容量高熵载荷在部分解码器下不稳定」的实测问题
    （cv2 回读对满容量高熵载荷可能整码失败甚至截断）。
    """
    maxchunksize = qrcode.util.BIT_LIMIT_TABLE[err][version]
    chunk_prefix_size = max(qrcode.util.mode_sizes_for_version(version).values()) + 4
    maxchunksize = (maxchunksize - chunk_prefix_size) // 8
    maxchunksize -= SIZE_PREFIX * 3
    maxchunksize = int(maxchunksize / 4) * 3
    if maxchunksize <= 0:
        raise ValueError(f"二维码版本 {version} 过小，无法承载数据（请调大二维码版本）")
    maxchunksize = int(maxchunksize * PAYLOAD_SAFETY) // 3 * 3
    return max(3, maxchunksize)


def _chunk_data(data, version, err, maxchunk=None):
    """按 QR 版本容量切块（对齐 qrtransfer.py / trajectory-convert）。

    块尺寸含 PAYLOAD_SAFETY 安全系数；块间以固定长度 + 零填充定界，
    重组端按序拼接后按 4 字节长度前缀截断，不依赖块尺寸本身 → 缩小块尺寸
    与旧解析端完全兼容。maxchunk：外部指定块尺寸（v2 二进制帧口径），缺省
    按 v1 文本口径计算。
    """
    maxchunksize = maxchunk or _max_chunk_size(version, err)
    nchunks = max(1, math.ceil(len(data) / maxchunksize))
    chunks = []
    for i in range(nchunks):
        beg = i * maxchunksize
        chunk = data[beg:beg + maxchunksize]
        if len(chunk) < maxchunksize:
            chunk += b"\0" * (maxchunksize - len(chunk))
        chunks.append(chunk)
    return chunks


def _fec_plan(size):
    """按数据块数计算 FEC 分块计划，返回 (blocksize, nblocks, k, m)（不编码）。"""
    blocksize = math.ceil(size / (1 - FEC_RATIO))
    nblocks = math.ceil(blocksize / MAX_FEC_M)
    blocksize = math.ceil(blocksize / nblocks)
    padded = size + (nblocks - size % nblocks if size % nblocks else 0)
    return blocksize, nblocks, padded // nblocks, blocksize


def _fec_encode(data_list, fec_ratio=FEC_RATIO):
    """zfec 前向纠错，返回 (share_list, k, m)。"""
    size = len(data_list)
    chunksize = len(data_list[0])
    blocksize, nblocks, k, m = _fec_plan(size)
    if size % nblocks:
        data_list = data_list + [b"\0" * chunksize] * (nblocks - size % nblocks)
    encoder = zfec.Encoder(k, m)
    mapped = []
    for i in range(nblocks):
        mapped.append(encoder.encode(data_list[i::nblocks]))
    out = []
    for i in range(blocksize):
        for j in range(nblocks):
            out.append(mapped[j][i])
    return out, k, m


def _qrformat_encode(share_list, k, m):
    nframes = len(share_list)
    codes = []
    for i, share in enumerate(share_list):
        header = encode_index(i) + encode_index(nframes) + encode_fecinfo(k, m)
        codes.append((header + encode_data(share)).decode("ascii"))
    return codes


def _open_video_writer(out_path, size):
    """依次尝试编码器候选，返回 (writer, fourcc)；全部失败抛 RuntimeError。"""
    for fourcc in VIDEO_FOURCC_CANDIDATES:
        writer = cv2.VideoWriter(out_path, cv2.VideoWriter_fourcc(*fourcc),
                                 FRAMERATE, size)
        if writer.isOpened():
            return writer, fourcc
        writer.release()
    raise RuntimeError(
        "视频编码器不可用（缺少 OpenH264 运行库）。"
        "请下载 openh264 DLL 放入 cv2 目录后重试，或改用「静态二维码」模式。"
    )


def encode_to_video(raw_bytes, version, out_path, progress_cb=None, frame_repeat=1,
                    frames_dir=None, cancel_check=None):
    """把字节数据按 QR-transfer 编码为二维码视频文件。返回 (nframes, k, m)。

    frame_repeat：每张二维码连续写入的帧数（fps 恒为 FRAMERATE 不变，
    仅延长单码停留时间，便于手机摄像头捕获；帧头协议与解析端均不受影响）。
    frames_dir：可选。同时把每张二维码 PNG 落盘到该目录（frame_0001.png 起，
    0 填充保证字典序 == 播放序），供前端相机传输模式做 JS 定时轮播——
    绕开浏览器视频管线，避免其丢帧"追赶"破坏每码停留时长的一致性。
    cancel_check：可选。无参函数，返回真值时抛 TaskCanceled（用户停止封装）。
    """
    if not (QRCODE_AVAILABLE and ZFEC_AVAILABLE and CV2_AVAILABLE and NUMPY_AVAILABLE):
        missing = [
            name for name, ok in (
                ("qrcode", QRCODE_AVAILABLE), ("zfec", ZFEC_AVAILABLE),
                ("opencv-python(cv2)", CV2_AVAILABLE), ("numpy", NUMPY_AVAILABLE),
            ) if not ok
        ]
        raise RuntimeError("后端缺少依赖：" + "、".join(missing) +
                           "，请先安装：pip install qrcode zfec opencv-python numpy")

    err = qrcode_constants.ERROR_CORRECT_L
    is_v2 = raw_bytes.startswith(JZ2_MAGIC)
    payload = len(raw_bytes).to_bytes(SIZE_DATASIZE, BYTE_ORDER) + raw_bytes
    if is_v2:
        # v2：share 载荷为原始字节（不再 base64），块尺寸按 JZ2 帧口径计算
        chunks = _chunk_data(payload, version, err,
                             maxchunk=_max_chunk_size_v2(version, err, 2))
    else:
        chunks = _chunk_data(payload, version, err)
    share_list, k, m = _fec_encode(chunks)
    if is_v2:
        codes = _qrformat_encode_v2(share_list, k, m)      # list[bytes]
    else:
        codes = _qrformat_encode(share_list, k, m)         # list[str]

    total = len(codes)
    writer = None
    if frames_dir:
        os.makedirs(frames_dir, exist_ok=True)
    try:
        for i, code in enumerate(codes):
            if cancel_check and cancel_check():
                raise TaskCanceled()
            raw = code if is_v2 else code.encode("ascii")
            # 渲染并验证原始图像可解码（解码器对个别 mask 有缺陷，失败则换 mask）。
            # 逐字节比对只对默认 mask 做一次（能读出 ≠ 读对，高熵载荷下可能
            # 截断）；失败即降级宽松档换 mask——视频帧有 FEC 兜底，且高熵帧可能
            # 所有 mask 都无法逐字节一致（真实接收端 ML Kit/jsQR 更稳），
            # 不因桌面解码器缺陷阻断生成。v2 二进制载荷只能用 zxing-cpp 验证。
            png = _qr_png_bytes(raw, version, err)
            verify = _zxing_can_decode if is_v2 else _cv2_can_decode
            if not verify(png, expected=raw) and not verify(png):
                for mask in range(8):
                    png = _qr_png_bytes(raw, version, err, mask_pattern=mask)
                    if verify(png):
                        break
            if frames_dir:
                with open(os.path.join(frames_dir, f"frame_{i + 1:04d}.png"), "wb") as f:
                    f.write(png)
            gray = cv2.imdecode(np.frombuffer(png, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
            img = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)  # FFMPEG 要求 3 通道
            if writer is None:
                h, w = img.shape[:2]
                writer, _ = _open_video_writer(out_path, (w, h))
            for _ in range(max(1, frame_repeat)):
                writer.write(img)
            if progress_cb and (i % 4 == 0 or i == total - 1):
                progress_cb(i + 1, total)
    finally:
        if writer is not None:
            writer.release()
    return total, k, m


# ===================== 静态二维码 =====================

def _qr_png_bytes(data_bytes, version, err, mask_pattern=None):
    """渲染二维码为 PNG 字节（可指定 mask 图案；None=自动选优）。"""
    kwargs = {}
    if mask_pattern is not None:
        kwargs["mask_pattern"] = mask_pattern
    qr = qrcode.QRCode(version=version, error_correction=err,
                       box_size=BOX_SIZE, border=4, **kwargs)
    qr.add_data(data_bytes, optimize=0)
    qr.make(fit=False)
    buf = io.BytesIO()
    qr.make_image(fill_color="black", back_color="white").save(buf, format="PNG")
    return buf.getvalue()


def _cv2_can_decode(png_bytes, expected=None):
    """用 cv2 验证 PNG 字节中的二维码能否被解码（cv2 对个别 mask 有解码缺陷）。

    expected 给定时升级为「逐字节比对」：能读出 ≠ 读对（cv2 实测对满容量
    高熵载荷可能返回截断内容），只有解码结果与期望字节完全一致才算通过。
    """
    if not (CV2_AVAILABLE and NUMPY_AVAILABLE):
        return True  # 无 cv2 时跳过验证（解析端也将缺少 cv2，验证无意义）
    try:
        arr = np.frombuffer(png_bytes, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            return False
        detector = cv2.QRCodeDetector()

        def _match(text):
            if not text:
                return False
            if expected is None:
                return True
            try:
                return text.encode("utf-8") == expected
            except Exception:
                return False

        for text in _decode_multi(detector, img):
            if _match(text):
                return True
        text, _pts, _sq = detector.detectAndDecode(img)
        return _match(text)
    except Exception:
        return False


def _zxing_can_decode(png_bytes, expected=None):
    """用 zxing-cpp 验证 PNG 中的二维码可解码（返回原始 bytes，二进制载荷无损）。

    v2 JZ2 二进制载荷在 cv2 下无法可靠解码（detectAndDecode 输出 str 会破坏
    非 UTF-8 字节），生成端自检一律走 zxing。zxing 缺失时返回 True 跳过验证
    （解析端同样缺 zxing，验证无意义）。
    """
    if not (ZXING_AVAILABLE and NUMPY_AVAILABLE and CV2_AVAILABLE):
        return True  # 解码器不可用时跳过验证
    try:
        arr = np.frombuffer(png_bytes, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            return False
        results = zxingcpp.read_barcodes(img)
        if not results:
            return False
        if expected is None:
            return True
        return any(r.bytes == expected for r in results)
    except Exception:
        return False


def _render_verified_qr(data_bytes, version, err, out_path):
    """渲染静态二维码并保证桌面解码器可解码：默认 mask 验证失败时逐个换 mask 重试。

    OpenCV 的 QRCodeDetector 对个别 mask 图案（如 mask 0/2）的特定数据存在
    解码缺陷；qrcode 库按惩罚分自动选中的 mask 可能恰好命中。本函数在生成
    侧即用桌面解码器回读验证，避免「生成正常、自家解析端却读不出」的往返失败。

    验证策略：默认 mask 先做「逐字节比对」（能读出 ≠ 读对，高熵载荷下可能
    返回截断内容），通过即用；否则降级宽松档（仅要求可解码）并允许换
    mask——静态页载荷已有 90% 容量余量，且真实接收端 ML Kit / jsQR 更稳，
    不因桌面解码器的比对缺陷阻断生成。
    v2 JZ2 二进制载荷（b"JZ2" 开头）用 zxing-cpp 验证（cv2 会破坏非 UTF-8 字节）。
    """
    if data_bytes.startswith(JZ2_MAGIC):
        png = _qr_png_bytes(data_bytes, version, err)
        if _zxing_can_decode(png, expected=data_bytes) or _zxing_can_decode(png):
            with open(out_path, "wb") as f:
                f.write(png)
            return out_path
        for mask in range(8):
            png = _qr_png_bytes(data_bytes, version, err, mask_pattern=mask)
            if _zxing_can_decode(png):
                with open(out_path, "wb") as f:
                    f.write(png)
                return out_path
        with open(out_path, "wb") as f:
            f.write(png)
        return out_path
    png = _qr_png_bytes(data_bytes, version, err)
    if _cv2_can_decode(png, expected=data_bytes) or _cv2_can_decode(png):
        with open(out_path, "wb") as f:
            f.write(png)
        return out_path
    for mask in range(8):
        png = _qr_png_bytes(data_bytes, version, err, mask_pattern=mask)
        if _cv2_can_decode(png):
            with open(out_path, "wb") as f:
                f.write(png)
            return out_path
    # 全部 mask 均无法通过 cv2 验证（理论不可达）：落盘默认版本并交由上层报错
    with open(out_path, "wb") as f:
        f.write(png)
    return out_path


def _single_capacity(version, err):
    """单张二维码可容纳的 UTF-8 字节数（预留安全余量）。"""
    try:
        capacity = qrcode.util.BIT_LIMIT_TABLE[err][version] // 8
    except KeyError:
        capacity = 800
    return max(48, capacity - 16)


def _char_boundary(raw, idx):
    """把字节下标回退到 UTF-8 字符边界。"""
    if idx >= len(raw):
        return len(raw)
    while idx > 0 and (raw[idx] & 0xC0) == 0x80:
        idx -= 1
    return idx


def _fit_page_chunk(tpl, raw, pos, capacity):
    """在 raw[pos:] 上二搜索出最大字节长度，使整页 JSON 的 UTF-8 字节数不超容量。

    tpl(data_str) 返回该页完整 JSON 字符串。返回 (data_str, 消耗字节数)。
    """
    lo, hi = 1, min(len(raw) - pos, capacity)
    best = 0
    while lo <= hi:
        mid = (lo + hi) // 2
        end = _char_boundary(raw, pos + mid)
        if end <= pos:
            hi = mid - 1
            continue
        try:
            cand = raw[pos:end].decode("utf-8")
        except UnicodeDecodeError:
            hi = mid - 1
            continue
        if len(tpl(cand).encode("utf-8")) <= capacity:
            best = end - pos
            lo = mid + 1
        else:
            hi = mid - 1
    if best == 0:
        return "", 0
    end = _char_boundary(raw, pos + best)
    return raw[pos:end].decode("utf-8"), end - pos


def _build_static_pages(fmt, name, env_bytes, version, err, max_pages=200):
    """把信封拆分为多页 JSON 字符串（首页带 fmt/name，续页带页码）。

    按整页实际序列化字节数逐页拟合容量（自动兼容 JSON 转义膨胀与
    多字节字符），页码先用 999 占位拟合，最终以实际页数重建（实际
    页数 ≤ 200，位数不增，长度只会更短，容量约束仍成立）。
    """
    capacity = _single_capacity(version, err)
    raw = env_bytes

    def head_tpl(data_str):
        return json.dumps({"jzt": 1, "fmt": fmt, "name": name,
                           "pg": {"i": 1, "n": 999}, "data": data_str},
                          ensure_ascii=False, separators=(",", ":"))

    def rest_tpl(data_str):
        return json.dumps({"jzt": 1, "pg": {"i": 999, "n": 999}, "data": data_str},
                          ensure_ascii=False, separators=(",", ":"))

    chunks = []  # (data_str, 是否首页)
    pos = 0
    while pos < len(raw):
        tpl = head_tpl if pos == 0 else rest_tpl
        chunk, used = _fit_page_chunk(tpl, raw, pos, capacity)
        if used <= 0:
            raise ValueError("二维码版本过小，无法承载数据（请调大二维码版本）")
        chunks.append((chunk, pos == 0))
        pos += used
        if len(chunks) > max_pages:
            raise ValueError(
                f"数据量过大，需拆分超过 {max_pages} 张静态二维码，"
                "请调大二维码版本或改用「二维码视频流」模式"
            )
    total = len(chunks)
    pages = []
    for i, (chunk, is_head) in enumerate(chunks, 1):
        if is_head:
            pages.append(json.dumps({"jzt": 1, "fmt": fmt, "name": name,
                                     "pg": {"i": i, "n": total}, "data": chunk},
                                    ensure_ascii=False, separators=(",", ":")))
        else:
            pages.append(json.dumps({"jzt": 1, "pg": {"i": i, "n": total},
                                     "data": chunk},
                                    ensure_ascii=False, separators=(",", ":")))
    return pages


def encode_static_output(fmt, name, env_bytes, version, out_dir, prefix, cancel_check=None):
    """生成静态二维码图片文件，返回 (文件路径列表, 页数)。

    v1：单张装得下输出 1 张完整信封 JSON；装不下自动拆分多张（首页带 fmt/name，
    续页带页码），每张独立可读。
    v2（env_bytes 为 JZ2 帧）：按容量把信封 payload 拆页，每页 = JZ2 帧
    （meta 每页重复 + payload 切片 + 独立 CRC）。
    cancel_check：可选。无参函数，返回真值时抛 TaskCanceled（用户停止封装）。
    """
    if not QRCODE_AVAILABLE:
        raise RuntimeError("后端缺少 qrcode 依赖，请先安装：pip install qrcode")
    err = qrcode_constants.ERROR_CORRECT_L
    if env_bytes.startswith(JZ2_MAGIC):
        pages = _build_static_pages_v2(env_bytes, version, err)
        paths = []
        for i, frame in enumerate(pages):
            if cancel_check and cancel_check():
                raise TaskCanceled()
            p = os.path.join(out_dir, f"{prefix}_{i + 1:02d}.png")
            _render_verified_qr(frame, version, err, p)
            paths.append(p)
        return paths, len(pages)
    single = os.path.join(out_dir, f"{prefix}_01.png")
    try:
        if cancel_check and cancel_check():
            raise TaskCanceled()
        _render_verified_qr(env_bytes, version, err, single)
        return [single], 1
    except QR_OVERFLOW:
        pass
    except Exception:
        raise
    pages = _build_static_pages(fmt, name, env_bytes, version, err)
    paths = []
    for i, page_json in enumerate(pages):
        if cancel_check and cancel_check():
            raise TaskCanceled()
        p = os.path.join(out_dir, f"{prefix}_{i + 1:02d}.png")
        _render_verified_qr(page_json.encode("utf-8"), version, err, p)
        paths.append(p)
    return paths, len(pages)


def estimate_output(mode, fmt, name, env_bytes, version, frame_repeat):
    """估算封装产物规模（不渲染码图，纯字符串/整数运算，供前端提前引导）。

    - static：拟合分页得到页数；分页失败（数据量超大 / 版本过小）时
      pages=None 并带回后端同款错误文案（供前端把错误转成引导）；
    - video：按 _chunk_data + _fec_plan 口径估算帧数，秒数按
      每码 frame_repeat/FRAMERATE 折算（相机模式 ≈ 观看一遍的时长）。
    """
    err = qrcode_constants.ERROR_CORRECT_L
    # env_bytes 可能是 bytes（v2 JZ2 帧 / v1 JSON 字节）或 str（旧调用口径），
    # 魔数判别前不做统一转码——v1 静态分页按字符切片需要 str。
    is_v2 = (isinstance(env_bytes, (bytes, bytearray))
             and bytes(env_bytes).startswith(JZ2_MAGIC))
    if mode == "static":
        try:
            if is_v2:
                pages = len(_build_static_pages_v2(env_bytes, version, err))
            else:
                pages = len(_build_static_pages(fmt, name, env_bytes, version, err))
        except ValueError as e:
            return {"kind": "static", "pages": None, "codes": 0,
                    "seconds": 0, "error": str(e)}
        return {"kind": "static", "pages": pages,
                "codes": pages, "seconds": 0}
    payload = SIZE_DATASIZE + len(env_bytes)
    if is_v2:
        mc = _max_chunk_size_v2(version, err, 2)      # share 帧 meta = k,m 两字节
    else:
        mc = _max_chunk_size(version, err)
    nchunks = max(1, math.ceil(payload / mc))
    blocksize, nblocks, k, m = _fec_plan(nchunks)
    nframes = blocksize * nblocks
    seconds = round(nframes * max(1, frame_repeat) / FRAMERATE, 1)
    return {"kind": "video", "pages": 0, "codes": nframes,
            "seconds": seconds, "k": k, "m": m}


# ===================== 二维码解码 =====================

def _decode_multi(detector, img):
    """兼容不同 OpenCV 版本的 detectAndDecodeMulti 返回签名，返回解码文本列表。"""
    res = detector.detectAndDecodeMulti(img)
    if not res:
        return []
    if isinstance(res[0], bool):  # (retval, texts, points[, straight])
        return [t for t in (res[1] or []) if t] if res[0] else []
    return [t for t in (res[0] or []) if t]  # (texts, points, straight)


def decode_image_file(file_bytes):
    """从 PNG/JPG 图片字节中解码二维码，返回内容字节列表（多码图取全部）。

    优先 zxing-cpp（返回原始 bytes，v2 二进制载荷无损）；不可用时回退 cv2
    （仅对 v1 文本载荷可靠——v2 二进制码必须安装 zxing-cpp）。
    """
    if not (CV2_AVAILABLE and NUMPY_AVAILABLE):
        raise RuntimeError("后端缺少 opencv-python / numpy，无法解析图片，请执行：pip install opencv-python numpy")
    arr = np.frombuffer(file_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("无法读取图片文件（支持 PNG / JPG）")
    if ZXING_AVAILABLE:
        results = zxingcpp.read_barcodes(img)
        if results:
            return [r.bytes for r in results]
        return []
    detector = cv2.QRCodeDetector()
    found = _decode_multi(detector, img)
    if not found:
        text, _pts, _ = detector.detectAndDecode(img)
        if text:
            found = [text]
    if not found:
        raise ValueError("图片中未识别到二维码")
    return [t.encode("utf-8", "surrogateescape") if isinstance(t, str) else t
            for t in found]


def decode_video_frames(video_path, progress_cb=None):
    """从二维码视频文件逐帧解码，返回内容字节列表（去重保序）。

    优先 zxing-cpp（v2 二进制载荷无损）；不可用时回退 cv2（仅 v1 文本可靠）。
    """
    if not (CV2_AVAILABLE and NUMPY_AVAILABLE):
        raise RuntimeError("后端缺少 opencv-python / numpy，无法解析视频，请执行：pip install opencv-python numpy")
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise ValueError("无法读取视频文件")
    seen = []
    seen_set = set()
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
    detector = None if ZXING_AVAILABLE else cv2.QRCodeDetector()
    fi = 0
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            fi += 1
            found = []
            if ZXING_AVAILABLE:
                try:
                    results = zxingcpp.read_barcodes(frame)
                    found = [r.bytes for r in results]
                except Exception:
                    found = []
            else:
                try:
                    found = _decode_multi(detector, frame)
                except Exception:
                    found = []
                if not found:
                    try:
                        # detectAndDecode 返回 (decoded_str, points, straight_qrcode)
                        text, _pts, _sq = detector.detectAndDecode(frame)
                        found = [text] if isinstance(text, str) and text else []
                    except Exception:
                        found = []
                found = [t.encode("utf-8", "surrogateescape") if isinstance(t, str) else t
                         for t in found]
            for t in found:
                if t not in seen_set:
                    seen_set.add(t)
                    seen.append(t)
            if progress_cb and total and fi % 10 == 0:
                progress_cb(min(fi, total), total)
    finally:
        cap.release()
    if progress_cb and total:
        progress_cb(total, total)
    if not seen:
        raise ValueError("视频中未识别到二维码帧")
    return seen


def reassemble_any(codes):
    """v1/v2 双协议自适应重组：codes 为解码端输出的原始内容 bytes 列表。"""
    if any(isinstance(c, (bytes, bytearray)) and bytes(c).startswith(JZ2_MAGIC)
           for c in codes):
        return reassemble_qrtransfer_v2(codes)
    texts = []
    for c in codes:
        if isinstance(c, (bytes, bytearray)):
            try:
                texts.append(c.decode("utf-8"))
            except Exception:
                continue
        else:
            texts.append(c)
    return reassemble_qrtransfer(texts)


def reassemble_qrtransfer(codes):
    """按 QR-transfer 协议重组帧文本列表，返回原始字节。

    与 qr-video-decode 插件后端逻辑一致：帧头 4+4+4 字符，zfec 纠错重组。
    """
    if not ZFEC_AVAILABLE:
        raise RuntimeError("后端缺少 zfec 依赖，无法纠错重组，请执行：pip install zfec")

    def decode_index(b64):
        return int.from_bytes(base64.b64decode(b64.encode("ascii")), BYTE_ORDER)

    data_list = None
    k = m = None
    for code in codes:
        if not isinstance(code, str) or len(code) < 12:
            continue
        try:
            idx = decode_index(code[:4])
            n = decode_index(code[4:8])
            fec = decode_index(code[8:12])
        except Exception:
            continue
        km, mm = (fec >> 8) & 255, fec & 255
        if data_list is None:
            data_list = [None] * n
            k, m = km, mm
        elif n != len(data_list) or km != k or mm != m:
            continue
        if 0 <= idx < len(data_list) and data_list[idx] is None:
            data_list[idx] = base64.b64decode(code[12:].encode("ascii"))

    if data_list is None or k is None:
        raise ValueError("未识别到有效的二维码帧（非 QR-transfer 视频流）")
    nframes = len(data_list)
    missing = [i for i, d in enumerate(data_list) if d is None]
    if len(missing) > nframes - k:
        raise ValueError(
            f"有效帧不足：收到 {nframes - len(missing)}/{nframes} 帧，"
            f"少于纠错阈值 k={k}，无法恢复（视频可能被截断）"
        )

    nblocks = nframes // m
    decoder = zfec.Decoder(k, m)
    recovered_blocks = []
    for i in range(nblocks):
        packet = [data_list[i + j * nblocks] for j in range(m)]
        blocks, blocknums = [], []
        for j, d in enumerate(packet):
            if d is not None:
                blocks.append(d)
                blocknums.append(j)
        if len(blocks) < k:
            raise ValueError(
                f"第 {i + 1}/{nblocks} 组有效分块不足（{len(blocks)} < k={k}），无法纠错"
            )
        recovered_blocks.append(decoder.decode(blocks[:k], blocknums[:k]))

    recovered = []
    for j in range(k):
        for i in range(nblocks):
            recovered.append(recovered_blocks[i][j])
    joined = b"".join(recovered)
    data_size = int.from_bytes(joined[:SIZE_DATASIZE], BYTE_ORDER)
    return joined[SIZE_DATASIZE:SIZE_DATASIZE + data_size]


def _parse_scanned_texts(texts):
    """把扫码得到的文本列表重组为完整信封 dict，返回 (env, page_info)。

    - 单张完整信封（无 pg 字段）→ 直接返回；
    - 多页分片 → 按 pg.i 排序拼接 data 得到完整 JSON。
    """
    first_page = None
    pages = {}
    total = None
    for t in texts:
        try:
            obj = json.loads(t)
        except Exception:
            continue
        if not isinstance(obj, dict) or obj.get("jzt") != 1:
            continue
        pg = obj.get("pg")
        if not pg:
            return obj, None  # 单张完整信封
        try:
            i, n = int(pg.get("i")), int(pg.get("n"))
        except (TypeError, ValueError):
            continue
        if total is None:
            total = n
        first_page = first_page if first_page is not None else (obj if i == 1 else None)
        if i == 1:
            first_page = obj
        pages[i] = obj
    if total and first_page and len(pages) >= 1:
        if len(pages) < total:
            raise ValueError(
                f"静态二维码页数不全：收到 {len(pages)}/{total} 张，请补齐后再解析"
            )
        parts = []
        for i in range(1, total + 1):
            if i not in pages:
                raise ValueError(f"缺少第 {i}/{total} 张二维码，无法还原完整数据")
            parts.append(str(pages[i].get("data") or ""))
        merged = json.loads("".join(parts))
        return merged, {"n": total}
    raise ValueError("未识别到「信息传输」封装的二维码内容")


# ===================== 信封 → 导出文件 =====================

def envelope_to_file(fmt, name, data, ext=None):
    """把信封数据还原为 (文件字节, mimetype, 导出文件名)。

    - file：base64 解码直接还原原始文件（文件名 = name，mime 按扩展名推断）；
    - text / markdown：直接文本导出 .txt / .md；
    - word：重建 .docx（纯文本逐段，无样式/宏；旧码与旧导出保持一致的数据形态）；
    - excel：重建 .xlsx（纯数据表格，无样式）。
    """
    safe = "".join(ch for ch in (name or "未命名") if ch not in '\\/:*?"<>|').strip() or "未命名"
    if fmt == "file":
        try:
            raw = base64.b64decode(data or "", validate=True)
        except Exception:
            raise ValueError("文件数据异常（base64 解码失败）")
        if not raw:
            raise ValueError("文件数据为空")
        mime = mimetypes.guess_type(safe)[0] or "application/octet-stream"
        return raw, mime, safe
    if fmt == "excel":
        if not isinstance(data, list):
            raise ValueError("Excel 数据结构异常（应为二维数组）")
        # 声明为 csv → 还原为 .csv 文本（BOM + CRLF + 标准转义，Excel 打开不乱码）
        if (ext or "").lower() == "csv":
            buf = io.StringIO()
            buf.write("\uFEFF")
            writer = csv_mod.writer(buf, lineterminator="\r\n")
            for row in data:
                if isinstance(row, list):
                    writer.writerow([_cell_text(v) if not isinstance(v, bool) and not isinstance(v, (int, float))
                                     else ("TRUE" if v is True else "FALSE" if v is False else v)
                                     for v in row])
                else:
                    writer.writerow([_cell_text(row)])
            return buf.getvalue().encode("utf-8"), "text/csv", f"{safe}.csv"
        if not OPENPYXL_AVAILABLE:
            raise RuntimeError("后端缺少 openpyxl，无法导出 .xlsx，请执行：pip install openpyxl")
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "数据"[:31] if len("数据") <= 31 else "数据"
        for row in data:
            if isinstance(row, list):
                ws.append([_cell_text(v) if not isinstance(v, (int, float, bool)) or v is None
                           else v for v in row])
            else:
                ws.append([_cell_text(row)])
        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", f"{safe}.xlsx"
    if fmt == "word":
        if not DOCX_AVAILABLE:
            raise RuntimeError("后端缺少 python-docx，无法导出 .docx，请执行：pip install python-docx")
        text = data if isinstance(data, str) else ""
        if not text.endswith("\n"):
            text += "\n"
        d = docx.Document()
        for line in text.split("\n")[:-1]:
            d.add_paragraph(line)
        buf = io.BytesIO()
        d.save(buf)
        mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        return buf.getvalue(), mime, f"{safe}.docx"
    # text / markdown → 纯文本导出
    text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, indent=2)
    if not text.endswith("\n"):
        text += "\n"
    ext_out = f".{ext}" if ext and fmt in ("text", "markdown") else FMT_EXT.get(fmt, ".txt")
    if fmt == "markdown" and not ext_out.startswith(".m"):
        ext_out = FMT_EXT[fmt]  # markdown 固定 .md（避免 txt 声明等异常后缀）
    mime = "text/markdown" if fmt == "markdown" else "text/plain"
    return text.encode("utf-8"), mime, f"{safe}{ext_out}"


# ===================== 后台封装任务 =====================

def _run_encode(task_id, mode, filename, file_bytes, text_input, qr_version,
                frame_repeat=1, raw_mode=False, rebuild=False):
    try:
        set_task(task_id, status="running", created_at=time.time())
        note = ""
        if _canceled(task_id):
            raise TaskCanceled()
        if filename is not None:
            if raw_mode:
                set_task(task_id, progress=0.05, stage="封装原始文件")
                fmt, name, data, ext = extract_doc_raw(filename, file_bytes)
            else:
                set_task(task_id, progress=0.05, stage="提取文档信息")
                try:
                    fmt, name, data, ext = extract_doc_lean(filename, file_bytes)
                except LeanUnsupported as reason:
                    # 白名单内但不可精简（旧版格式/解析失败）→ 自动回退原件传输并提示
                    fmt, name, data, ext = extract_doc_raw(filename, file_bytes)
                    note = f"{str(reason) or '文档解析失败'}，已按原件传输"
                if fmt == "file":
                    set_task(task_id, progress=0.2, stage="封装原始文件")
                elif fmt == "excel":
                    set_task(task_id, progress=0.2, stage="封装表格数据")
                else:
                    set_task(task_id, progress=0.2, stage="封装文本数据")
        else:
            fmt, name, data, ext = "text", "文字信息", text_input or "", None
            set_task(task_id, progress=0.2, stage="封装文字数据")

        env_bytes = build_envelope(fmt, name, data, ext, rebuild=rebuild)
        # T11：产物是否走了拆包重建路径（供前端标注"还原方式：重建"）
        rebuilt = False
        if isinstance(env_bytes, (bytes, bytearray)) and env_bytes.startswith(JZ2_MAGIC):
            try:
                rebuilt = bool(parse_frame_jz2(env_bytes)["mode"])
            except Exception:
                rebuilt = False
        # 产物命名用显示名：file 格式的 name 含扩展名，产物名去掉扩展名
        base_name = name
        if fmt == "file" and "." in base_name:
            base_name = base_name.rsplit(".", 1)[0]

        # 产物规模预估（不渲染码图）：供前端展示「预计码数/耗时」；
        # 静态模式分页不可行时在此快速失败（发码前引导，而非渲染中途报错）
        try:
            estimate = estimate_output(mode, fmt, name, env_bytes, qr_version,
                                       frame_repeat)
        except Exception:
            estimate = None
        if estimate:
            set_task(task_id, estimate=estimate)
        if mode == "static" and estimate and estimate.get("pages") is None:
            raise ValueError(estimate.get("error") or "数据量过大，无法拆分为静态二维码")

        if mode == "static":
            set_task(task_id, progress=0.4, stage="生成静态二维码")
            img_paths, count = encode_static_output(fmt, name, env_bytes, qr_version,
                                                    _TASK_DIR, task_id,
                                                    cancel_check=lambda: _canceled(task_id))
            if count == 1:
                single = img_paths[0]
                size = os.path.getsize(single) if os.path.exists(single) else 0
                set_task(task_id, status="done", progress=1.0,
                         output_type="static", fmt=fmt, name=name, ext=ext,
                         env_size=len(env_bytes), qr_version=qr_version,
                         image_count=1, image_size=size,
                         image_name=f"{base_name}二维码.png", note=note,
                         rebuilt=rebuilt)
            else:
                zip_path = _task_file(task_id, ".zip")
                with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                    for i, p in enumerate(img_paths):
                        zf.write(p, f"{base_name}_二维码_第{i + 1}张（共{count}张）.png")
                size = os.path.getsize(zip_path) if os.path.exists(zip_path) else 0
                set_task(task_id, status="done", progress=1.0,
                         output_type="static", fmt=fmt, name=name, ext=ext,
                         env_size=len(env_bytes),
                         image_count=count, zip_size=size,
                         zip_name=f"{base_name}二维码（共{count}张）.zip", note=note,
                         rebuilt=rebuilt)
            return

        def progress(done, total):
            set_task(task_id, progress=round(0.2 + 0.8 * done / max(total, 1), 4),
                     stage=f"QR 编码 {done}/{total} 帧")

        set_task(task_id, progress=0.25, stage="编码二维码视频")
        out_path = _task_file(task_id, ".mp4")
        fdir = _frames_dir(task_id) if frame_repeat > 1 else None
        nframes, k, m = encode_to_video(env_bytes, qr_version, out_path,
                                        progress_cb=progress, frame_repeat=frame_repeat,
                                        frames_dir=fdir,
                                        cancel_check=lambda: _canceled(task_id))
        size = os.path.getsize(out_path) if os.path.exists(out_path) else 0
        set_task(task_id, status="done", progress=1.0,
                 output_type="video", fmt=fmt, name=name, ext=ext,
                 env_size=len(env_bytes), qr_version=qr_version,
                 nframes=nframes, k=k, m=m, video_size=size,
                 frame_count=(nframes if fdir else 0),
                 video_name=f"{base_name}二维码流.mp4", note=note,
                 rebuilt=rebuilt)
    except TaskCanceled:
        # 用户停止：清理已写入的部分产物，任务标记为 canceled
        _remove_task_files(task_id)
        set_task(task_id, status="canceled", progress=0.0, stage="",
                 detail="已停止封装")
    except ValueError as e:
        set_task(task_id, status="error", detail=str(e))
    except RuntimeError as e:
        set_task(task_id, status="error", detail=str(e))
    except Exception as e:
        # SEC-5：非预期异常不向前端透出内部细节
        set_task(task_id, status="error", detail=f"封装失败（{type(e).__name__}）")


# ===================== 后台解析任务 =====================

def _run_decode(task_id, video_path):
    """后台解析视频流：逐帧解码 → zfec 重组 → 信封解析。"""
    try:
        set_task(task_id, status="running", created_at=time.time())
        set_task(task_id, progress=0.1, stage="逐帧扫描二维码")

        def progress(done, total):
            set_task(task_id, progress=round(0.1 + 0.6 * done / max(total, 1), 4),
                     stage=f"解码 {done}/{total} 帧")

        codes = decode_video_frames(video_path, progress_cb=progress)
        set_task(task_id, progress=0.75, stage=f"收到 {len(codes)} 帧，纠错重组中")
        raw = reassemble_any(codes)
        set_task(task_id, progress=0.9, stage="解析信封")
        env = _envelope_from_raw(raw)
        fmt, name, data, ext = parse_envelope(env)
        data_size = len(raw)
        # payload 中 data 已解压还原（parse_envelope），与压缩前形态一致
        payload = {"jzt": 1, "fmt": fmt, "name": name, "data": data}
        if ext:
            payload["ext"] = ext
        set_task(task_id, status="done", progress=1.0,
                 fmt=fmt, name=name, ext=ext, data_size=data_size,
                 payload_json=json.dumps(payload, ensure_ascii=False))
    except ValueError as e:
        set_task(task_id, status="error", detail=str(e))
    except RuntimeError as e:
        set_task(task_id, status="error", detail=str(e))
    except Exception as e:
        set_task(task_id, status="error", detail=f"解析失败（{type(e).__name__}）")


# ===================== 路由注册 =====================

def register(app) -> None:
    """插件入口：由 JZToolsHub 主应用在启动时调用。"""

    @app.get(f"{API_PREFIX}/status")
    def it_status():
        return jsonify({
            "ok": True,
            "qrcode": QRCODE_AVAILABLE,
            "zfec": ZFEC_AVAILABLE,
            "cv2": CV2_AVAILABLE,
            "numpy": NUMPY_AVAILABLE,
            "zxing": ZXING_AVAILABLE,
            "protocol": 2 if PROTOCOL_V2 else 1,
            "openpyxl": OPENPYXL_AVAILABLE,
            "docx": DOCX_AVAILABLE,
        })

    @app.get(f"{API_PREFIX}/formats")
    def it_formats():
        """可封装文件格式清单（内置 SUPPORTED_FORMATS）。

        返回 [{ext, fmt, label, extract}...]，extract=none 表示仅原件传输。
        前端据此动态生成 accept 白名单与校验。
        """
        formats = [
            {"ext": ext, **item} for ext, item in SUPPORTED_FORMATS.items()
        ]
        return jsonify({"ok": True, "formats": formats})

    # ---------- 信息封装 ----------

    @app.post(f"{API_PREFIX}/encode")
    def it_encode():
        """接收文字 / 文件与参数，后台执行封装，立即返回 task_id。

        文件格式白名单见 SUPPORTED_FORMATS（Word / Excel / CSV / Txt / Markdown /
        PPT / PDF；后三类仅原件传输）；多文件由前端逐个提交、每文件一个独立任务。
        raw=1 为原件传输（文件原样封装）；默认 raw=0 精简传输
        （word/txt/md 提取文本、excel 构建二维数组，声明原始类型与后缀；
        精简不可行时自动回退原件并在任务 note 说明）。
        """
        mode = request.form.get("mode", "static")
        if mode not in ("static", "video"):
            mode = "static"
        try:
            version = int(float(request.form.get("qr_version", 15)))
        except (TypeError, ValueError):
            version = 15
        if not 1 <= version <= 40:
            return jsonify({"ok": False, "detail": "二维码版本须在 1-40 之间"}), 400
        raw_mode = request.form.get("raw", "0").strip().lower() in ("1", "true", "on", "yes")
        # T11 拆包重组开关（默认关：字节精确；开启后 ZIP 容器拆包重建，更小但内容等价）
        rebuild_mode = request.form.get("rebuild", "0").strip().lower() in ("1", "true", "on", "yes")

        # 相机传输模式为视频默认行为：每码连续重复 CAMERA_FRAME_REPEAT 帧（约 0.33s），
        # 配合前端帧序列轮播与 APP 自动识别，手机对准屏幕即可传输
        frame_repeat = CAMERA_FRAME_REPEAT

        f = request.files.get("file")
        text_input = request.form.get("text")
        if f and f.filename:
            filename = os.path.basename(f.filename or "")
            if not filename:
                return jsonify({"ok": False, "detail": "文件名非法"}), 400
            ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
            if ext not in SUPPORTED_FORMATS:
                allowed = " / ".join(sorted(SUPPORTED_FORMATS.keys()))
                return jsonify({"ok": False,
                                "detail": f"仅支持 {allowed} 格式文件"}), 400
            file_bytes = f.read()
            if len(file_bytes) > MAX_UPLOAD_BYTES:
                return jsonify({"ok": False, "detail": "文件过大（上限 20MB）"}), 400
            if not file_bytes:
                return jsonify({"ok": False, "detail": "文件为空"}), 400
        elif text_input is not None and text_input.strip():
            filename, file_bytes = None, None
        else:
            return jsonify({"ok": False, "detail": "请输入文字或选择文件"}), 400
        if mode == "video" and not (ZFEC_AVAILABLE and CV2_AVAILABLE and NUMPY_AVAILABLE):
            return jsonify({"ok": False, "detail": "后端缺少视频编码依赖（zfec / opencv-python / numpy）"}), 503

        task_id = uuid.uuid4().hex[:12]
        cleanup_tasks()
        _clean_task_files()
        viewer = _viewer()
        set_task(task_id, status="pending", progress=0.0, created_at=time.time(),
                 created_by=(viewer or {}).get("username", ""))
        _executor.submit(_run_encode, task_id, mode, filename,
                         file_bytes, text_input, version, frame_repeat, raw_mode,
                         rebuild_mode)
        return jsonify({"ok": True, "task_id": task_id})

    @app.get(f"{API_PREFIX}/task/<task_id>")
    def it_task_status(task_id):
        task = get_task(task_id)
        if not task:
            return jsonify({"ok": False, "detail": "任务不存在或已过期"}), 404
        if not _task_owned_by(task, _viewer()):
            return jsonify({"ok": False, "detail": "任务不存在或已过期"}), 404
        payload = {
            "ok": True,
            "status": task.get("status"),
            "progress": task.get("progress", 0.0),
            "stage": task.get("stage", "") or "",
        }
        if task.get("estimate"):
            payload["estimate"] = task["estimate"]
        if task.get("rebuilt"):
            payload["rebuilt"] = True   # T11：还原方式为重建（内容等价，非字节一致）
        if task.get("status") == "done":
            payload.update({
                "output_type": task.get("output_type"),
                "fmt": task.get("fmt"),
                "name": task.get("name"),
                "ext": task.get("ext"),
                "note": task.get("note", "") or "",
                "env_size": task.get("env_size", 0),
                "qr_version": task.get("qr_version"),
            })
            if task.get("output_type") == "static":
                payload.update({
                    "image_count": task.get("image_count", 1),
                    "image_size": task.get("image_size", 0),
                    "zip_size": task.get("zip_size", 0),
                    "image_name": task.get("image_name", ""),
                    "zip_name": task.get("zip_name", ""),
                })
            else:
                payload.update({
                    "nframes": task.get("nframes", 0),
                    "k": task.get("k"),
                    "m": task.get("m"),
                    "video_size": task.get("video_size", 0),
                    "video_name": task.get("video_name", ""),
                    "frame_count": task.get("frame_count", 0),
                })
        if task.get("status") == "error":
            payload["detail"] = task.get("detail", "任务失败")
        if task.get("status") == "canceled":
            payload["detail"] = task.get("detail", "已停止封装")
        return jsonify(payload)

    @app.post(f"{API_PREFIX}/encode/<task_id>/cancel")
    def it_encode_cancel(task_id):
        """请求停止封装任务：设置取消标志，编码循环在检查点协同终止。

        任务已结束（done/error/canceled）或不存在时返回 ok=false（幂等友好）。
        """
        task = get_task(task_id)
        if not task:
            return jsonify({"ok": False, "detail": "任务不存在或已过期"}), 404
        if not _task_owned_by(task, _viewer()):
            return jsonify({"ok": False, "detail": "任务不存在或已过期"}), 404
        if task.get("status") not in ("pending", "running"):
            return jsonify({"ok": False, "detail": "任务已结束，无需停止"}), 409
        set_task(task_id, cancel=True)
        return jsonify({"ok": True})

    @app.get(f"{API_PREFIX}/download/<task_id>")
    @app.get(f"{API_PREFIX}/download/<task_id>/<path:filename>")
    def it_download(task_id, filename=None):
        """下载封装产物（单张 PNG / 多张 ZIP / 视频 MP4）。"""
        task = get_task(task_id)
        if not task or task.get("status") != "done":
            return jsonify({"ok": False, "detail": "任务不存在或未完成"}), 404
        if not _task_owned_by(task, _viewer()):
            return jsonify({"ok": False, "detail": "任务不存在或未完成"}), 404
        if task.get("output_type") == "static":
            if task.get("image_count", 1) > 1:
                path = _task_file(task_id, ".zip")
                if not os.path.isfile(path):
                    return jsonify({"ok": False, "detail": "ZIP 文件已被清理"}), 404
                return send_file(path, mimetype="application/zip", as_attachment=True,
                                 download_name=task.get("zip_name", "二维码.zip"))
            path = _task_file(task_id, ".png")
            if not os.path.isfile(path):
                # 单张静态码直接以 _01.png 落盘（与 /image 预览共用同一文件）
                alt = _static_image_file(task_id, 1)
                path = alt if os.path.isfile(alt) else path
            if not os.path.isfile(path):
                return jsonify({"ok": False, "detail": "图片文件已被清理"}), 404
            return send_file(path, mimetype="image/png", as_attachment=True,
                             download_name=task.get("image_name", "二维码.png"))
        path = _task_file(task_id, ".mp4")
        if not os.path.isfile(path):
            return jsonify({"ok": False, "detail": "视频文件已被清理"}), 404
        return send_file(path, mimetype="video/mp4", as_attachment=True,
                         download_name=task.get("video_name", "二维码流.mp4"))

    @app.get(f"{API_PREFIX}/image/<task_id>/<int:index>")
    def it_static_image(task_id, index):
        """返回某一张静态二维码图片（供页面预览）。index 从 1 开始。"""
        task = get_task(task_id)
        if (not task or task.get("status") != "done"
                or task.get("output_type") != "static"):
            return jsonify({"ok": False, "detail": "任务不存在或未完成"}), 404
        if not _task_owned_by(task, _viewer()):
            return jsonify({"ok": False, "detail": "任务不存在或未完成"}), 404
        total = task.get("image_count", 1)
        if not 1 <= index <= total:
            return jsonify({"ok": False, "detail": "图片序号非法"}), 404
        path = _static_image_file(task_id, index)
        if not os.path.isfile(path):
            return jsonify({"ok": False, "detail": "图片已被清理"}), 404
        return send_file(path, mimetype="image/png")

    @app.get(f"{API_PREFIX}/frame/<task_id>/<int:index>")
    def it_video_frame(task_id, index):
        """返回相机传输模式视频流第 index（1 起）张码图（JS 定时轮播用）。"""
        task = get_task(task_id)
        if (not task or task.get("status") != "done"
                or task.get("output_type") != "video"
                or not task.get("frame_count")):
            return jsonify({"ok": False, "detail": "任务不存在或无帧序列"}), 404
        if not _task_owned_by(task, _viewer()):
            return jsonify({"ok": False, "detail": "任务不存在或无帧序列"}), 404
        total = task.get("frame_count", 0)
        if not 1 <= index <= total:
            return jsonify({"ok": False, "detail": "帧序号非法"}), 404
        path = os.path.join(_frames_dir(task_id), f"frame_{index:04d}.png")
        if not os.path.isfile(path):
            return jsonify({"ok": False, "detail": "帧文件已被清理"}), 404
        return send_file(path, mimetype="image/png")

    # ---------- 信息解析 ----------

    @app.post(f"{API_PREFIX}/decode")
    def it_decode():
        """接收二维码图片（单张 / 多张 ZIP）或二维码视频，解析出封装数据。

        图片（含 ZIP 内多张）同步解析（毫秒级）；视频走后台任务轮询。
        请求体：multipart，字段 file=图片/ZIP/视频，type=image|zip|video。
        """
        f = request.files.get("file")
        if not f or not f.filename:
            return jsonify({"ok": False, "detail": "请选择图片、ZIP 或视频文件"}), 400
        dtype = request.form.get("type", "image")
        if dtype not in ("image", "zip", "video"):
            dtype = "image"
        file_bytes = f.read()
        if len(file_bytes) > MAX_UPLOAD_BYTES:
            return jsonify({"ok": False, "detail": "文件过大（上限 20MB）"}), 400

        if dtype == "video":
            # 视频逐帧解码较慢 → 后台任务
            task_id = uuid.uuid4().hex[:12]
            cleanup_tasks()
            os.makedirs(_TASK_DIR, exist_ok=True)
            tmp_video = _task_file(task_id, os.path.splitext(f.filename)[1] or ".mp4")
            with open(tmp_video, "wb") as vf:
                vf.write(file_bytes)
            viewer = _viewer()
            set_task(task_id, status="pending", progress=0.0, created_at=time.time(),
                     created_by=(viewer or {}).get("username", ""))
            _executor.submit(_run_decode, task_id, tmp_video)
            return jsonify({"ok": True, "task_id": task_id, "async": True})

        # 图片 / ZIP：同步解析
        try:
            if dtype == "zip":
                codes = []
                bad_pages = 0
                try:
                    with zipfile.ZipFile(io.BytesIO(file_bytes)) as zf:
                        for info in zf.infolist():
                            if info.is_dir() or not info.filename.lower().endswith((".png", ".jpg", ".jpeg")):
                                continue
                            try:
                                codes.extend(decode_image_file(zf.read(info)))
                            except (ValueError, RuntimeError):
                                bad_pages += 1  # 个别页模糊/破损时跳过，后续按缺页提示
                except zipfile.BadZipFile:
                    raise ValueError("无法读取 ZIP 文件")
                # ZIP 内按文件名排序保证页序（解码顺序已是文件序）
                if not codes:
                    raise ValueError("ZIP 中未找到可识别的二维码图片"
                                     + (f"（{bad_pages} 张识别失败）" if bad_pages else ""))
            else:
                codes = decode_image_file(file_bytes)
            env, page_info = _parse_scanned_codes(codes)
            fmt, name, data, ext = parse_envelope(env)
        except ValueError as e:
            return jsonify({"ok": False, "detail": str(e)}), 400
        except RuntimeError as e:
            return jsonify({"ok": False, "detail": str(e)}), 503
        except Exception:
            return jsonify({"ok": False, "detail": "解析失败，请确认二维码清晰完整"}), 500

        payload = {"jzt": 1, "fmt": fmt, "name": name, "data": data}
        if ext:
            payload["ext"] = ext
        return jsonify({
            "ok": True,
            "fmt": fmt,
            "name": name,
            "ext": ext,
            "fmt_label": FMT_LABELS.get(fmt, fmt),
            "pages": (page_info or {}).get("n", 1),
            "payload": payload,
        })

    @app.get(f"{API_PREFIX}/decode/<task_id>")
    def it_decode_status(task_id):
        """轮询视频解析任务状态。"""
        task = get_task(task_id)
        if not task:
            return jsonify({"ok": False, "detail": "任务不存在或已过期"}), 404
        if not _task_owned_by(task, _viewer()):
            return jsonify({"ok": False, "detail": "任务不存在或已过期"}), 404
        payload = {
            "ok": True,
            "status": task.get("status"),
            "progress": task.get("progress", 0.0),
            "stage": task.get("stage", "") or "",
        }
        if task.get("status") == "done":
            payload.update({
                "fmt": task.get("fmt"),
                "name": task.get("name"),
                "ext": task.get("ext"),
                "data_size": task.get("data_size", 0),
                "payload_json": task.get("payload_json", ""),
            })
        if task.get("status") == "error":
            payload["detail"] = task.get("detail", "解析失败")
        return jsonify(payload)

    @app.post(f"{API_PREFIX}/export")
    def it_export():
        """把解析出的数据导出为文件（txt / md / docx / xlsx）。

        请求体：{"payload": {jzt,fmt,name,ext,data}, "export": "auto"}。
        """
        body = request.get_json(silent=True) or {}
        payload = body.get("payload")
        if not isinstance(payload, dict):
            return jsonify({"ok": False, "detail": "缺少解析数据"}), 400
        try:
            fmt, name, data, ext = parse_envelope(payload)
        except ValueError as e:
            return jsonify({"ok": False, "detail": str(e)}), 400
        try:
            content, mime, fname = envelope_to_file(fmt, name, data, ext)
        except ValueError as e:
            return jsonify({"ok": False, "detail": str(e)}), 400
        except RuntimeError as e:
            return jsonify({"ok": False, "detail": str(e)}), 503
        return send_file(io.BytesIO(content), mimetype=mime, as_attachment=True,
                         download_name=fname)

    @app.get(f"{API_PREFIX}/ping")
    def it_ping():
        return jsonify({"ok": True})
