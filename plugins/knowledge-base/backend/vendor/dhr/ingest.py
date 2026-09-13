"""DHR 摄入层 —— docx / docm / dotx / doc / rtf 识别（T-604，复用 xhr.ingest）。

⚠️ 加密的 docx 外层也是 CFB，必须区分 ``WordDocument``（doc）与
   ``EncryptedPackage``（加密 docx）—— 与 Excel 版同源逻辑。
"""
from __future__ import annotations

from typing import Dict, Optional

from xhr.core import XhrError
from xhr.ingest import detect_cfb_kind, read_text, unzip_guarded


def detect_docx_kind(content_types_xml: str, entries) -> str:
    """对 ZIP 容器判定 docx / docm / dotx。"""
    ct = content_types_xml
    entries_lower = {e.lower() for e in entries}
    if "word/document.xml" in entries_lower:
        import re

        if re.search(r"wordprocessingml\.document\.macroEnabled", ct, re.IGNORECASE):
            return "docm"
        if re.search(r"wordprocessingml\.template", ct, re.IGNORECASE):
            return "dotx"
        return "docx"
    return "zip"


def ingest_doc(data: bytes) -> dict:
    """识别文档格式并解包。

    @returns {'format': ..., 'files': {...} | None}
    @raises XhrError FILE_TOO_LARGE / ZIP_BOMB / ENCRYPTED / UNSUPPORTED_FORMAT / CORRUPTED
    """
    data = bytes(data)
    if len(data) == 0:
        raise XhrError("CORRUPTED", "empty input")
    from xhr.core import LIMITS

    if len(data) > LIMITS["MAX_FILE_BYTES"]:
        raise XhrError("FILE_TOO_LARGE", f"{len(data)} bytes")

    if data.startswith(b"PK\x03\x04") or data.startswith(b"PK\x05\x06"):
        files, _stats, _skipped = unzip_guarded(data)
        fmt = detect_docx_kind(read_text(files.get("[Content_Types].xml")), files.keys())
        if fmt == "zip":
            raise XhrError("UNSUPPORTED_FORMAT", "ZIP 容器但不是有效的 docx（缺少 word/document.xml）")
        return {"format": fmt, "files": files}

    if data.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        kind = detect_cfb_kind(data)
        if kind == "encrypted":
            raise XhrError("ENCRYPTED")
        if kind == "doc":
            return {"format": "doc", "files": None}
        if kind == "xls":
            raise XhrError("UNSUPPORTED_FORMAT", "该文件是 Excel 工作簿，请使用 xhr 引擎")
        raise XhrError("UNSUPPORTED_FORMAT", "无法识别的 OLE2 文档")

    head = data[:16]
    if head.startswith(b"{\\rtf1"):
        return {"format": "rtf", "files": None}

    raise XhrError("UNSUPPORTED_FORMAT", "magic=" + " ".join(f"{b:02x}" for b in data[:4]))
