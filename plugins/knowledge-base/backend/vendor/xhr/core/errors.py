"""统一错误类型 —— 对应 TS 版 ``packages/core/src/errors.ts``。

所有对外抛出的错误都应是 XhrError，且带机器可读的 code，
CLI / HTTP 层据此给出友好提示，不泄露内部堆栈。
"""
from __future__ import annotations

from typing import Optional

ERROR_MESSAGE: dict[str, str] = {
    "FILE_TOO_LARGE": "文件过大，超出可预览上限（100MB）",
    "ZIP_BOMB": "文件解压后体积异常，已拒绝处理",
    "TOO_MANY_ENTRIES": "文件内部条目过多，已拒绝处理",
    "ENCRYPTED": "该文件已加密，无法预览，请先移除密码",
    "UNSUPPORTED_FORMAT": "不支持的文件格式（当前支持 .xlsx / .xlsm / .xls）",
    "CORRUPTED": "文件已损坏，无法解析",
    "TIMEOUT": "解析超时，文件可能过于复杂",
    "INTERNAL": "内部错误",
    "MISSING_CONTENT": "未找到可渲染的内容",
}


class XhrError(Exception):
    """带机器可读 code 的统一异常。"""

    def __init__(self, code: str, detail: Optional[str] = None) -> None:
        super().__init__(ERROR_MESSAGE.get(code, ERROR_MESSAGE["INTERNAL"]))
        self.code = code
        self.detail = detail

    def to_json(self) -> dict[str, str]:
        """供 CLI / HTTP 层输出。"""
        return {"code": self.code, "message": str(self)}


def to_xhr_error(e: BaseException, fallback: str = "INTERNAL") -> XhrError:
    """把任意异常归一成 XhrError。"""
    if isinstance(e, XhrError):
        return e
    detail = f"{type(e).__name__}: {e}" if isinstance(e, BaseException) else str(e)
    return XhrError(fallback, detail)
