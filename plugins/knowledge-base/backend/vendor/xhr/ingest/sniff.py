"""格式识别 —— 对应 TS 版 ``packages/ingest/src/sniff.ts``。

⚠️ 只认魔数，不信扩展名（.xlsx 实际是 CSV / .doc 实际是 RTF 的情况很常见）。
"""
from __future__ import annotations

import re
from typing import Iterable, List, Sequence, Union

SourceFormat = str  # 'xlsx' | 'xlsm' | 'xlsb' | 'xls' | 'csv' | 'zip' | 'unknown'

SIG_ZIP = b"PK\x03\x04"
SIG_ZIP_EMPTY = b"PK\x05\x06"
SIG_CFB = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
SIG_BIFF = b"\x09\x08"

_CSV_HEAD_RE = re.compile(r'^[\t\n\r\x20-\x7e\u4e00-\u9fff,"\' ]+$')


def sniff(buffer: Union[bytes, bytearray, memoryview]) -> SourceFormat:
    """魔数识别。返回粗粒度格式；ZIP 内部细分由 detect_zip_kind 完成。"""
    buf = bytes(buffer[:8])
    if len(buf) < 8:
        return "unknown"
    if buf.startswith(SIG_ZIP) or buf.startswith(SIG_ZIP_EMPTY):
        return "zip"
    if buf.startswith(SIG_CFB):
        return "xls"  # 也可能是加密的 xlsx，由上层进一步区分
    if buf.startswith(SIG_BIFF):
        return "xls"
    # 纯文本无 BOM → 可能是 CSV
    head = bytes(buffer[:512]).decode("utf-8", errors="ignore")
    if _CSV_HEAD_RE.match(head):
        return "csv"
    return "unknown"


def detect_zip_kind(content_types_xml: str, entries: Iterable[str]) -> SourceFormat:
    """对 ZIP 容器进一步判定：xlsx / xlsm / xlsb。

    依据 [Content_Types].xml 中的部件类型声明。
    """
    entries_lower = {e.lower() for e in entries}
    ct = content_types_xml
    if "xl/workbook.bin" in entries_lower or re.search(
        r"spreadsheetml\.sheet\.binary\.macroEnabled", ct, re.IGNORECASE
    ):
        return "xlsb"
    if re.search(r"spreadsheetml\.sheet\.macroEnabled", ct, re.IGNORECASE):
        return "xlsm"
    if re.search(r"spreadsheetml\.sheet\.main", ct) or "xl/workbook.xml" in entries_lower:
        return "xlsx"
    return "zip"


# CFB 流名在目录项中以 UTF-16LE 存储
def _utf16le(name: str) -> bytes:
    return name.encode("utf-16-le")


def detect_cfb_kind(buffer: Union[bytes, bytearray, memoryview], scan_limit: int = 4 * 1024 * 1024) -> str:
    """判断 CFB 容器里是 .xls、.doc 还是「加密的 xlsx」。

    ⚠️ 加密的 xlsx 外层同样是 CFB（含 EncryptedPackage 流），
       不能当成 xls 去解析，必须早失败并给友好提示。

    实现说明：完整解析 CFB 目录需要 FAT 链遍历；这里扫描文件头部的
    目录扇区区域（目录通常位于文件前部）查找 UTF-16LE 编码的流名。
    该启发式对真实文件足够可靠，且能被 ``verify`` 测试覆盖。

    Returns:
        'encrypted' | 'xls' | 'doc' | 'unknown'
    """
    data = bytes(buffer[:scan_limit])
    names = ("EncryptedPackage", "EncryptionInfo", "Workbook", "Book", "WordDocument")
    found: List[str] = []
    for n in names:
        if _utf16le(n) in data:
            found.append(n)
    if "EncryptedPackage" in found or "EncryptionInfo" in found:
        return "encrypted"
    # ⚠️ WordDocument 必须先于 Workbook/Book 判断：LibreOffice 生成的 .doc 里
    #    可能包含 "Book" 字样的其它流/字符串，先查 Workbook 会把 .doc 误判成 .xls
    if "WordDocument" in found:
        return "doc"
    if "Workbook" in found or "Book" in found:
        return "xls"
    return "unknown"
