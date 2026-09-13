"""摄入层 —— 对应 TS 版 ``packages/ingest/src/index.ts``。

职责：识别格式 + 安全限额校验 + 解包（只解一次，供下游复用）。
"""
from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Union

from ..core import LIMITS, XhrError
from .safety import IMAGE_MIME_BY_EXT, ImageVerdict, is_unsafe_entry_name, sniff_image_mime, verify_image
from .sniff import SourceFormat, detect_cfb_kind, detect_zip_kind, sniff

__all__ = [
    "IngestResult",
    "ZipStats",
    "ingest",
    "unzip_guarded",
    "read_text",
    "sniff",
    "detect_zip_kind",
    "detect_cfb_kind",
    "is_unsafe_entry_name",
    "verify_image",
    "sniff_image_mime",
    "IMAGE_MIME_BY_EXT",
    "ImageVerdict",
]


@dataclass
class ZipStats:
    entry_count: int = 0
    total_uncompressed_bytes: int = 0
    compress_ratio: float = 0.0


@dataclass
class IngestResult:
    format: SourceFormat
    size_bytes: int
    #: ZIP 解包后的条目（仅 ZIP 类格式有）
    files: Optional[Dict[str, bytes]] = None
    #: ZIP 条目名列表
    entries: Optional[List[str]] = None
    #: 因条目名不安全（Zip Slip）而被跳过的条目
    skipped_entries: Optional[List[str]] = None
    #: 若为加密文件，上层应给友好提示
    encrypted: bool = False


def read_text(data: Optional[bytes]) -> str:
    """读文本：去掉 UTF-8 BOM，按 UTF-8 解码。"""
    if not data:
        return ""
    if data[:3] == b"\xef\xbb\xbf":
        data = data[3:]
    return data.decode("utf-8", errors="replace")


def unzip_guarded(data: bytes) -> tuple[Dict[str, bytes], ZipStats, List[str]]:
    """解包 ZIP 并做限额校验。

    ⚠️ 两个关键点：
      1. Python 的 ``ZipInfo.file_size`` / ``compress_size`` 来自中央目录，
         可以在**解压前**拿到原始尺寸 —— 触发解压比 / Zip Bomb 时能提前中止。
      2. 条目名先过 Zip Slip 校验，不合格的直接**跳过**（而非中止）：
         为一个坏条目放弃整个文件是不合适的。
    """
    stats = ZipStats()
    skipped: List[str] = []
    files: Dict[str, bytes] = {}

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            infos = zf.infolist()

            # ── 第一道闸门：按中央目录声明做预检（不解压） ──
            if len(infos) > LIMITS["MAX_ZIP_ENTRIES"]:
                raise XhrError("TOO_MANY_ENTRIES", f"entries={len(infos)}")
            total_declared = 0
            for info in infos:
                total_declared += info.file_size
            if total_declared > LIMITS["MAX_UNCOMPRESSED_BYTES"]:
                raise XhrError("ZIP_BOMB", f"declared total={total_declared}")
            ratio = (total_declared / len(data)) if data else 0.0
            if ratio > LIMITS["MAX_COMPRESS_RATIO"]:
                raise XhrError("ZIP_BOMB", f"ratio={ratio:.1f} entries={len(infos)}")

            # ── 解压（预算递减，防声明尺寸造假） ──
            budget = LIMITS["MAX_UNCOMPRESSED_BYTES"]
            for info in infos:
                name = info.filename
                stats.entry_count += 1
                stats.total_uncompressed_bytes += info.file_size
                if is_unsafe_entry_name(name):
                    skipped.append(name)
                    continue
                if name.endswith("/"):
                    continue  # 目录条目
                if info.file_size > budget:
                    raise XhrError("ZIP_BOMB", f"entry={name} budget={budget}")
                with zf.open(info) as f:
                    chunk = f.read(budget + 1)
                if len(chunk) > budget:
                    raise XhrError("ZIP_BOMB", f"entry={name} exceeded budget")
                files[name] = chunk
                budget -= len(chunk)
    except zipfile.BadZipFile as e:
        raise XhrError("CORRUPTED", str(e)) from e
    except RuntimeError as e:  # zipfile 对加密条目抛 RuntimeError
        raise XhrError("CORRUPTED", str(e)) from e

    stats.compress_ratio = ratio if data else 0.0
    return files, stats, skipped


def ingest(data: Union[bytes, bytearray, memoryview]) -> IngestResult:
    """摄入入口。

    @throws XhrError（FILE_TOO_LARGE / ZIP_BOMB / ENCRYPTED / UNSUPPORTED_FORMAT / CORRUPTED）
    """
    buf = bytes(data)
    size = len(buf)

    if size == 0:
        raise XhrError("CORRUPTED", "empty input")
    if size > LIMITS["MAX_FILE_BYTES"]:
        raise XhrError("FILE_TOO_LARGE", f"{size} bytes")

    coarse = sniff(buf)

    if coarse == "zip":
        files, stats, skipped = unzip_guarded(buf)
        entries = list(files.keys())
        content_types = read_text(files.get("[Content_Types].xml"))
        fmt = detect_zip_kind(content_types, entries)
        return IngestResult(
            format=fmt,
            size_bytes=size,
            files=files,
            entries=entries,
            skipped_entries=skipped or None,
        )

    if coarse == "xls":
        # CFB：可能是 .xls，也可能是加密的 xlsx / 加密的 xls。
        # 这里做加密粗判；真正的流枚举由 normalize 层负责。
        kind = detect_cfb_kind(buf)
        result = IngestResult(format="xls", size_bytes=size)
        if kind == "encrypted":
            result.encrypted = True
        return result

    if coarse == "csv":
        raise XhrError("UNSUPPORTED_FORMAT", "csv is not supported in this engine (V1)")

    raise XhrError(
        "UNSUPPORTED_FORMAT",
        "magic=" + " ".join(f"{b:02x}" for b in buf[:4]),
    )
