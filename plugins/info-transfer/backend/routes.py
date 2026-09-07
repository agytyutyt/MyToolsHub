"""信息传输 —— JZToolsHub 后端插件路由。

两个功能：
- 信息封装：把用户提供的文字 / txt / markdown / 任意文档文件封装为
  JSON 信封 {v, fmt, name, data}，编码为静态二维码（单张或自动拆分多张）
  或二维码视频流（QR-transfer 协议，与 trajectory-convert / qr-video-decode
  插件互通，含 zfec 前向纠错）。word/excel/其他文档自 v2 起以「原始文件」
  完整传输（fmt=file，data 为文件字节 base64），不再提取文本。
- 信息解析：对封装产生的二维码图片（单张 / 多张 ZIP）或二维码视频流解码，
  先读取信封判断文档格式，再还原数据；file 格式直接还原原始文件
  （含扩展名与字节），旧格式（text/markdown/word/excel）按纯数据导出。

信封协议（envelope）：
    {"jzt": 1, "fmt": "<doc fmt>", "name": "<display name>", "data": <payload>}
    - jzt   ：协议标识与版本（固定 1）
    - fmt   ：文档格式声明（text / markdown / word / excel / file）
    - name  ：显示名；fmt=file 时为完整原始文件名（含扩展名）
    - data  ：文本内容（字符串）、纯数据表格（二维数组）或文件字节 base64（fmt=file）

静态二维码分页协议（单张装不下时自动拆分多张，每张独立可读一部分）：
    {"jzt": 1, "fmt": "...", "name": "...", "pg": {"i": 1, "n": 3}, "data": <part>}

并发设计（对齐 trajectory-convert / qr-video-decode 插件）：
- POST /encode 接收文件与参数，立即返回 task_id；
- 文档提取、编码、写视频/写图片放到后台线程执行；
- 前端通过 GET /status/<task_id> 轮询，完成后经 /download、/image 取产物。
"""

import base64
import io
import json
import math
import mimetypes
import os
import shutil
import threading
import time
import uuid
import zipfile
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
    import openpyxl
    OPENPYXL_AVAILABLE = True
except Exception:  # pragma: no cover
    openpyxl = None
    OPENPYXL_AVAILABLE = False

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

# 视频编码器候选（cv2 内置 FFmpeg 的 libopenh264 在部分机器缺 DLL 时，
# 依次尝试 MPEG-4 Part 2 回退；mp4v 浏览器可能无法直接播放但解码端不受影响）
VIDEO_FOURCC_CANDIDATES = ("avc1", "mp4v")

# 支持的文档格式（word/excel 为兼容保留：旧二维码仍可解析，封装端不再生成）
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


def extract_doc(filename, file_bytes):
    """从上传文件提取封装内容，返回 (fmt, name, data)。

    - .txt / .md：仍按纯文本/Markdown 提取（fmt=text/markdown，data 为字符串，
      name 不含扩展名）——文本体量小、两端可直接预览；
    - 其余任意文档（.docx/.xlsx/.xls/.doc/.wps/.pdf/...）：**完整传输原始文件**
      （v2 起，fmt=file，data 为文件字节的 base64，name 为完整文件名含扩展名），
      不做任何解析，样式/宏/图片原样保留。
    """
    name = os.path.basename(filename or "未命名")

    ext = name.lower().rsplit(".", 1)[-1] if "." in name else ""
    if ext == "txt":
        return "text", name.rsplit(".", 1)[0], _decode_text_bytes(file_bytes)
    if ext in ("md", "markdown"):
        return "markdown", name.rsplit(".", 1)[0], _decode_text_bytes(file_bytes)
    # 完整文件传输：不做解析，base64 封装
    return "file", name, base64.b64encode(file_bytes).decode("ascii")


# ===================== 信封封装 =====================

def build_envelope(fmt, name, data):
    """构造信封 JSON 字节（解析端以此判断文档格式）。"""
    env = {"jzt": 1, "fmt": fmt, "name": name, "data": data}
    return json.dumps(env, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def parse_envelope(obj):
    """校验并解析信封，返回 (fmt, name, data)；不合法时抛 ValueError。"""
    if not isinstance(obj, dict):
        raise ValueError("内容不是「信息传输」封装的数据（缺少信封结构）")
    if obj.get("jzt") != 1:
        raise ValueError("信封协议版本不受支持（jzt != 1）")
    fmt = obj.get("fmt")
    if fmt not in DOC_FORMATS:
        raise ValueError(f"未知的文档格式声明：{fmt}")
    return fmt, str(obj.get("name") or "未命名"), obj.get("data")


# ===================== QR-transfer 编码（视频） =====================

def encode_index(n):
    b = n.to_bytes(SIZE_INDEX, BYTE_ORDER)
    return base64.encodebytes(b).replace(b"\n", b"")


def encode_fecinfo(k, m):
    return encode_index((k << 8) + m)


def encode_data(b):
    return base64.encodebytes(b).replace(b"\n", b"")


def _chunk_data(data, version, err):
    """按 QR 版本容量切块（对齐 qrtransfer.py / trajectory-convert）。"""
    maxchunksize = qrcode.util.BIT_LIMIT_TABLE[err][version]
    chunk_prefix_size = max(qrcode.util.mode_sizes_for_version(version).values()) + 4
    maxchunksize = (maxchunksize - chunk_prefix_size) // 8
    maxchunksize -= SIZE_PREFIX * 3
    maxchunksize = int(maxchunksize / 4) * 3
    if maxchunksize <= 0:
        raise ValueError(f"二维码版本 {version} 过小，无法承载数据（请调大二维码版本）")
    nchunks = max(1, math.ceil(len(data) / maxchunksize))
    chunks = []
    for i in range(nchunks):
        beg = i * maxchunksize
        chunk = data[beg:beg + maxchunksize]
        if len(chunk) < maxchunksize:
            chunk += b"\0" * (maxchunksize - len(chunk))
        chunks.append(chunk)
    return chunks


def _fec_encode(data_list, fec_ratio=FEC_RATIO):
    """zfec 前向纠错，返回 (share_list, k, m)。"""
    size = len(data_list)
    chunksize = len(data_list[0])
    blocksize = math.ceil(size / (1 - fec_ratio))
    nblocks = math.ceil(blocksize / MAX_FEC_M)
    blocksize = math.ceil(blocksize / nblocks)
    if size % nblocks:
        data_list = data_list + [b"\0" * chunksize] * (nblocks - size % nblocks)
    m = blocksize
    k = len(data_list) // nblocks
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
                    frames_dir=None):
    """把字节数据按 QR-transfer 编码为二维码视频文件。返回 (nframes, k, m)。

    frame_repeat：每张二维码连续写入的帧数（fps 恒为 FRAMERATE 不变，
    仅延长单码停留时间，便于手机摄像头捕获；帧头协议与解析端均不受影响）。
    frames_dir：可选。同时把每张二维码 PNG 落盘到该目录（frame_0001.png 起，
    0 填充保证字典序 == 播放序），供前端相机传输模式做 JS 定时轮播——
    绕开浏览器视频管线，避免其丢帧"追赶"破坏每码停留时长的一致性。
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
    payload = len(raw_bytes).to_bytes(SIZE_DATASIZE, BYTE_ORDER) + raw_bytes
    chunks = _chunk_data(payload, version, err)
    share_list, k, m = _fec_encode(chunks)
    codes = _qrformat_encode(share_list, k, m)

    total = len(codes)
    writer = None
    if frames_dir:
        os.makedirs(frames_dir, exist_ok=True)
    try:
        for i, code in enumerate(codes):
            raw = code.encode("ascii")
            # 渲染并验证原始图像可解码（cv2 对个别 mask 有缺陷，失败则换 mask）
            png = _qr_png_bytes(raw, version, err)
            if not _cv2_can_decode(png):
                for mask in range(8):
                    png = _qr_png_bytes(raw, version, err, mask_pattern=mask)
                    if _cv2_can_decode(png):
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


def _cv2_can_decode(png_bytes):
    """用 cv2 验证 PNG 字节中的二维码能否被解码（cv2 对个别 mask 有解码缺陷）。"""
    if not (CV2_AVAILABLE and NUMPY_AVAILABLE):
        return True  # 无 cv2 时跳过验证（解析端也将缺少 cv2，验证无意义）
    try:
        arr = np.frombuffer(png_bytes, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            return False
        detector = cv2.QRCodeDetector()
        if _decode_multi(detector, img):
            return True
        text, _pts, _sq = detector.detectAndDecode(img)
        return bool(text)
    except Exception:
        return False


def _render_verified_qr(data_bytes, version, err, out_path):
    """渲染静态二维码并保证 cv2 可解码：默认 mask 验证失败时逐个换 mask 重试。

    OpenCV 的 QRCodeDetector 对个别 mask 图案（如 mask 0/2）的特定数据存在
    解码缺陷；qrcode 库按惩罚分自动选中的 mask 可能恰好命中。本函数在生成
    侧即用 cv2 回读验证，避免「生成正常、自家解析端却读不出」的往返失败。
    """
    png = _qr_png_bytes(data_bytes, version, err)
    if _cv2_can_decode(png):
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


def encode_static_output(fmt, name, env_bytes, version, out_dir, prefix):
    """生成静态二维码图片文件，返回 (文件路径列表, 页数)。

    单张装得下时输出 1 张完整信封（任何扫码器扫码即可读出 JSON）；
    装不下时自动拆分为多张，每张是独立可读的 JSON（首页带 fmt/name，
    续页带页码；解析端按页码重组完整信封）。
    """
    if not QRCODE_AVAILABLE:
        raise RuntimeError("后端缺少 qrcode 依赖，请先安装：pip install qrcode")
    err = qrcode_constants.ERROR_CORRECT_L
    single = os.path.join(out_dir, f"{prefix}_01.png")
    try:
        _render_verified_qr(env_bytes, version, err, single)
        return [single], 1
    except QR_OVERFLOW:
        pass
    except Exception:
        raise
    pages = _build_static_pages(fmt, name, env_bytes, version, err)
    paths = []
    for i, page_json in enumerate(pages):
        p = os.path.join(out_dir, f"{prefix}_{i + 1:02d}.png")
        _render_verified_qr(page_json.encode("utf-8"), version, err, p)
        paths.append(p)
    return paths, len(pages)


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
    """从 PNG/JPG 图片字节中解码二维码文本（cv2，多码图取全部）。"""
    if not (CV2_AVAILABLE and NUMPY_AVAILABLE):
        raise RuntimeError("后端缺少 opencv-python / numpy，无法解析图片，请执行：pip install opencv-python numpy")
    arr = np.frombuffer(file_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("无法读取图片文件（支持 PNG / JPG）")
    detector = cv2.QRCodeDetector()
    found = _decode_multi(detector, img)
    if not found:
        text, _pts, _ = detector.detectAndDecode(img)
        if text:
            found = [text]
    if not found:
        raise ValueError("图片中未识别到二维码")
    return found


def decode_video_frames(video_path, progress_cb=None):
    """从二维码视频文件逐帧解码（cv2 QRCodeDetector），返回文本列表（去重保序）。"""
    if not (CV2_AVAILABLE and NUMPY_AVAILABLE):
        raise RuntimeError("后端缺少 opencv-python / numpy，无法解析视频，请执行：pip install opencv-python numpy")
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise ValueError("无法读取视频文件")
    seen = []
    seen_set = set()
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
    detector = cv2.QRCodeDetector()
    fi = 0
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            fi += 1
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

def envelope_to_file(fmt, name, data):
    """把信封数据还原为 (文件字节, mimetype, 导出文件名)。

    - file：base64 解码直接还原原始文件（文件名 = name，mime 按扩展名推断）；
    - text / markdown：直接文本导出 .txt / .md；
    - word（兼容旧码）：还原为 .txt（纯文本，无样式，声明格式）；
    - excel：还原为 .xlsx（纯数据表格，无样式）。
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
        if not OPENPYXL_AVAILABLE:
            raise RuntimeError("后端缺少 openpyxl，无法导出 .xlsx，请执行：pip install openpyxl")
        if not isinstance(data, list):
            raise ValueError("Excel 数据结构异常（应为二维数组）")
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
    # text / markdown / word → 纯文本导出（word 声明为 txt，无样式）
    text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, indent=2)
    if not text.endswith("\n"):
        text += "\n"
    ext = FMT_EXT.get(fmt, ".txt")
    mime = "text/markdown" if fmt == "markdown" else "text/plain"
    return text.encode("utf-8"), mime, f"{safe}{ext}"


# ===================== 后台封装任务 =====================

def _run_encode(task_id, mode, filename, file_bytes, text_input, qr_version, frame_repeat=1):
    try:
        set_task(task_id, status="running", created_at=time.time())

        if filename is not None:
            set_task(task_id, progress=0.05, stage="提取文档信息")
            fmt, name, data = extract_doc(filename, file_bytes)
            if fmt == "file":
                set_task(task_id, progress=0.2, stage="封装原始文件")
            elif fmt == "excel":
                set_task(task_id, progress=0.2, stage="封装表格数据")
            else:
                set_task(task_id, progress=0.2, stage="封装文本数据")
        else:
            fmt, name, data = "text", "文字信息", text_input or ""
            set_task(task_id, progress=0.2, stage="封装文字数据")

        env_bytes = build_envelope(fmt, name, data)
        # 产物命名用显示名：file 格式的 name 含扩展名，产物名去掉扩展名
        base_name = name
        if fmt == "file" and "." in base_name:
            base_name = base_name.rsplit(".", 1)[0]

        if mode == "static":
            set_task(task_id, progress=0.4, stage="生成静态二维码")
            img_paths, count = encode_static_output(fmt, name, env_bytes, qr_version,
                                                    _TASK_DIR, task_id)
            if count == 1:
                single = img_paths[0]
                size = os.path.getsize(single) if os.path.exists(single) else 0
                set_task(task_id, status="done", progress=1.0,
                         output_type="static", fmt=fmt, name=name,
                         env_size=len(env_bytes),
                         image_count=1, image_size=size,
                         image_name=f"{base_name}二维码.png")
            else:
                zip_path = _task_file(task_id, ".zip")
                with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                    for i, p in enumerate(img_paths):
                        zf.write(p, f"{base_name}_二维码_第{i + 1}张（共{count}张）.png")
                size = os.path.getsize(zip_path) if os.path.exists(zip_path) else 0
                set_task(task_id, status="done", progress=1.0,
                         output_type="static", fmt=fmt, name=name,
                         env_size=len(env_bytes),
                         image_count=count, zip_size=size,
                         zip_name=f"{base_name}二维码（共{count}张）.zip")
            return

        def progress(done, total):
            set_task(task_id, progress=round(0.2 + 0.8 * done / max(total, 1), 4),
                     stage=f"QR 编码 {done}/{total} 帧")

        set_task(task_id, progress=0.25, stage="编码二维码视频")
        out_path = _task_file(task_id, ".mp4")
        fdir = _frames_dir(task_id) if frame_repeat > 1 else None
        nframes, k, m = encode_to_video(env_bytes, qr_version, out_path,
                                        progress_cb=progress, frame_repeat=frame_repeat,
                                        frames_dir=fdir)
        size = os.path.getsize(out_path) if os.path.exists(out_path) else 0
        set_task(task_id, status="done", progress=1.0,
                 output_type="video", fmt=fmt, name=name,
                 env_size=len(env_bytes),
                 nframes=nframes, k=k, m=m, video_size=size,
                 frame_count=(nframes if fdir else 0),
                 video_name=f"{base_name}二维码流.mp4")
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
        raw = reassemble_qrtransfer(codes)
        set_task(task_id, progress=0.9, stage="解析信封")
        try:
            env = json.loads(raw.decode("utf-8"))
        except Exception:
            raise ValueError("重组数据不是「信息传输」封装的信封（可能是其他来源的二维码流）")
        fmt, name, data = parse_envelope(env)
        data_size = len(raw)
        set_task(task_id, status="done", progress=1.0,
                 fmt=fmt, name=name, data_size=data_size,
                 payload_json=json.dumps({"jzt": 1, "fmt": fmt, "name": name, "data": data},
                                         ensure_ascii=False))
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
            "openpyxl": OPENPYXL_AVAILABLE,
        })

    # ---------- 信息封装 ----------

    @app.post(f"{API_PREFIX}/encode")
    def it_encode():
        """接收文字 / 文件与参数，后台执行封装，立即返回 task_id。"""
        mode = request.form.get("mode", "static")
        if mode not in ("static", "video"):
            mode = "static"
        try:
            version = int(float(request.form.get("qr_version", 15)))
        except (TypeError, ValueError):
            version = 15
        if not 1 <= version <= 40:
            return jsonify({"ok": False, "detail": "二维码版本须在 1-40 之间"}), 400

        # 相机传输模式为视频默认行为：每码连续重复 CAMERA_FRAME_REPEAT 帧（约 0.33s），
        # 配合前端帧序列轮播与 APP 自动识别，手机对准屏幕即可传输
        frame_repeat = CAMERA_FRAME_REPEAT

        f = request.files.get("file")
        text_input = request.form.get("text")
        if f and f.filename:
            filename = os.path.basename(f.filename or "")
            if not filename:
                return jsonify({"ok": False, "detail": "文件名非法"}), 400
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
                         file_bytes, text_input, version, frame_repeat)
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
        if task.get("status") == "done":
            payload.update({
                "output_type": task.get("output_type"),
                "fmt": task.get("fmt"),
                "name": task.get("name"),
                "env_size": task.get("env_size", 0),
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
        return jsonify(payload)

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
                texts = []
                bad_pages = 0
                try:
                    with zipfile.ZipFile(io.BytesIO(file_bytes)) as zf:
                        for info in zf.infolist():
                            if info.is_dir() or not info.filename.lower().endswith((".png", ".jpg", ".jpeg")):
                                continue
                            try:
                                texts.extend(decode_image_file(zf.read(info)))
                            except (ValueError, RuntimeError):
                                bad_pages += 1  # 个别页模糊/破损时跳过，后续按缺页提示
                except zipfile.BadZipFile:
                    raise ValueError("无法读取 ZIP 文件")
                # ZIP 内按文件名排序保证页序（解码顺序已是文件序）
                if not texts:
                    raise ValueError("ZIP 中未找到可识别的二维码图片"
                                     + (f"（{bad_pages} 张识别失败）" if bad_pages else ""))
            else:
                texts = decode_image_file(file_bytes)
            env, page_info = _parse_scanned_texts(texts)
            fmt, name, data = parse_envelope(env)
        except ValueError as e:
            return jsonify({"ok": False, "detail": str(e)}), 400
        except RuntimeError as e:
            return jsonify({"ok": False, "detail": str(e)}), 503
        except Exception:
            return jsonify({"ok": False, "detail": "解析失败，请确认二维码清晰完整"}), 500

        return jsonify({
            "ok": True,
            "fmt": fmt,
            "name": name,
            "fmt_label": FMT_LABELS.get(fmt, fmt),
            "pages": (page_info or {}).get("n", 1),
            "payload": {"jzt": 1, "fmt": fmt, "name": name, "data": data},
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
                "data_size": task.get("data_size", 0),
                "payload_json": task.get("payload_json", ""),
            })
        if task.get("status") == "error":
            payload["detail"] = task.get("detail", "解析失败")
        return jsonify(payload)

    @app.post(f"{API_PREFIX}/export")
    def it_export():
        """把解析出的数据导出为文件（txt / md / xlsx）。

        请求体：{"payload": {jzt,fmt,name,data}, "export": "auto"}。
        """
        body = request.get_json(silent=True) or {}
        payload = body.get("payload")
        if not isinstance(payload, dict):
            return jsonify({"ok": False, "detail": "缺少解析数据"}), 400
        try:
            fmt, name, data = parse_envelope(payload)
        except ValueError as e:
            return jsonify({"ok": False, "detail": str(e)}), 400
        try:
            content, mime, fname = envelope_to_file(fmt, name, data)
        except ValueError as e:
            return jsonify({"ok": False, "detail": str(e)}), 400
        except RuntimeError as e:
            return jsonify({"ok": False, "detail": str(e)}), 503
        return send_file(io.BytesIO(content), mimetype=mime, as_attachment=True,
                         download_name=fname)

    @app.get(f"{API_PREFIX}/ping")
    def it_ping():
        return jsonify({"ok": True})
