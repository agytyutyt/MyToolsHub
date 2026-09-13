"""摄入层的安全原语 —— 对应 TS 版 ``packages/ingest/src/safety.ts``。

只放「与具体格式无关、可独立单测」的纯函数，便于攻击用例覆盖。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Union

# ─────────────────────────────────────────────────────────────
# Zip Slip
# ─────────────────────────────────────────────────────────────


def is_unsafe_entry_name(name: str) -> bool:
    """ZIP 条目名是否不安全。

    本引擎全程在内存中操作、不写盘，Zip Slip 的直接危害有限，
    但仍在解包阶段直接拒绝（纵深防御）：
      · 含 ``..``（目录穿越）
      · 以 ``/`` 或 ``\\`` 开头（绝对路径）
      · 以盘符开头（``C:`` 这类）
      · 含 ``\\``（ZIP 规范用 ``/``）
      · 含控制字符（可用于截断文件名）
      · 空名
    """
    if not name:
        return True
    if ".." in name:
        return True
    if name.startswith("/") or name.startswith("\\"):
        return True
    if re.match(r"^[A-Za-z]:", name):
        return True
    if "\\" in name:
        return True
    if re.search(r"[\x00-\x1f]", name):
        return True
    return False


# ─────────────────────────────────────────────────────────────
# 图片魔数
# ─────────────────────────────────────────────────────────────

#: 允许内联的图片类型（故意不含 image/svg+xml：SVG 内可嵌脚本）
IMAGE_MIME_BY_EXT = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "bmp": "image/bmp",
    "webp": "image/webp",
}


@dataclass
class ImageVerdict:
    ok: bool
    mime: Optional[str] = None
    reason: Optional[str] = None


def sniff_image_mime(data: Union[bytes, bytearray, memoryview]) -> Optional[str]:
    """按文件头魔数判断图片真实类型；无法识别返回 None。

    ⚠️ 为什么必须做二次校验：xlsx 本质是个 ZIP，攻击者可以把任意字节
       命名为 xl/media/image1.png。让「声明类型」必须与「真实字节」一致，
       成本几乎为零。
    """
    b = bytes(data)
    if len(b) < 12:
        return None
    # PNG: 89 50 4E 47 0D 0A 1A 0A
    if b.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    # JPEG: FF D8 FF
    if b.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    # GIF: 'GIF87a' / 'GIF89a'
    if b.startswith(b"GIF8"):
        return "image/gif"
    # BMP: 'BM' + 头部长度至少 14 字节
    if b.startswith(b"BM") and len(b) >= 14:
        return "image/bmp"
    # WEBP: 'RIFF'....'WEBP'
    if b.startswith(b"RIFF") and b[8:12] == b"WEBP":
        return "image/webp"
    return None


def verify_image(ext: str, data: Union[bytes, bytearray, memoryview]) -> ImageVerdict:
    """校验一张图片：扩展名与魔数都必须落在白名单内，且两者必须一致。"""
    declared = IMAGE_MIME_BY_EXT.get(ext.lower())
    if not declared:
        return ImageVerdict(False, reason=f"不支持的扩展名 .{ext}（仅允许 png/jpeg/gif/bmp/webp）")
    actual = sniff_image_mime(data)
    if not actual:
        return ImageVerdict(False, reason="文件头不是已知的图片格式（可能是伪装成图片的其它内容）")
    if actual != declared:
        return ImageVerdict(False, reason=f"扩展名声明为 {declared}，但实际内容为 {actual}")
    return ImageVerdict(True, mime=actual)
